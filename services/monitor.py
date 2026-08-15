import asyncio
import datetime
import json
import logging
import os
import re
import uuid
from collections import deque
from zoneinfo import ZoneInfo
from config import Config
from services.tplink import TPLinkClient

logger = logging.getLogger("motor_automate.monitor")


class MotorMonitor:
    """
    Voltage-polling session tied to the motor being ON, plus a persistent bucket.

    Voltage window (Condition 1): while the motor is ON, read voltage every
    POLL_INTERVAL_SECONDS into a rolling ~3-minute window. If a FULL window is
    entirely in the 10-430 band, shut the motor off.

    Bucket (Condition 2): every 10s where voltage > VOLTAGE_MAX, accrue the elapsed
    real time (in hours) into `bucket_progress`. When it reaches BUCKET_SIZE, shut
    the motor off. The bucket is cumulative across ON/OFF cycles and BOTH shutoff
    conditions; it is only cleared by a manual reset or the daily 11:00 reset. It is
    persisted to disk so it also survives process restarts.
    """

    def __init__(self):
        self.is_active = True          # Master enable for the safeguard
        self.last_run = None
        self.last_voltage = None
        self.history = []
        self.max_history_len = 100
        self._task = None

        self.lock = asyncio.Lock()

        # Voltage polling session
        self.poll_interval = Config.POLL_INTERVAL_SECONDS
        self.window_size = Config.VOLTAGE_WINDOW_SIZE
        self.voltage_window = deque(maxlen=self.window_size)
        self.session_active = False
        self.motor_on = False

        # Bucket (cumulative runtime hours above VOLTAGE_MAX)
        self.bucket_size = Config.BUCKET_SIZE
        self.bucket_progress = 0.0
        self.bucket_last_reset_date = None      # ISO date str of last daily reset
        self._last_accum_ts = None              # epoch secs of previous accrual tick
        self._state_file = Config.BUCKET_STATE_FILE

        self._load_state()

        # Schedules (UI-managed): times in SCHEDULE_TIMEZONE at which the motor
        # auto-turns ON. Fired once per day per schedule; no auto-off.
        self.timezone_name = Config.SCHEDULE_TIMEZONE
        try:
            self._tz = ZoneInfo(self.timezone_name)
        except Exception:
            logger.error(f"Unknown timezone {self.timezone_name}; using system local time.")
            self._tz = None
        self._schedules_file = Config.SCHEDULES_FILE
        self.schedules = []
        self._schedule_last_fired = {}     # id -> ISO date last fired (runtime only)
        self._load_schedules()

        # TP-Link cloud schedules: fetched (00:30 daily + manual + on startup),
        # cached, and used to turn the motor ON at each enabled ON-time.
        self.tplink_schedules = []
        self.tplink_last_fetch = None
        self._tplink_last_fetch_date = None
        self._tplink_last_fired = {}
        self.tplink_sync_hour = Config.TPLINK_SYNC_HOUR
        self.tplink_sync_minute = Config.TPLINK_SYNC_MINUTE

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load_state(self):
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, "r") as f:
                    data = json.load(f)
                self.bucket_progress = float(data.get("bucket_progress", 0.0))
                self.bucket_last_reset_date = data.get("last_reset_date")
                # A UI-set size persists across restarts; fall back to the config default.
                self.bucket_size = float(data.get("bucket_size", self.bucket_size))
                logger.info(
                    f"Loaded bucket state: {self.bucket_progress:.4f}h / {self.bucket_size} "
                    f"(last reset {self.bucket_last_reset_date})."
                )
        except Exception as e:
            logger.error(f"Could not load bucket state ({e}); starting fresh.")
            self.bucket_progress = 0.0
            self.bucket_last_reset_date = None

    def _save_state(self):
        try:
            with open(self._state_file, "w") as f:
                json.dump({
                    "bucket_progress": self.bucket_progress,
                    "last_reset_date": self.bucket_last_reset_date,
                    "bucket_size": self.bucket_size,
                }, f)
        except Exception as e:
            logger.error(f"Could not save bucket state: {e}")

    # ------------------------------------------------------------------ #
    # Background task lifecycle
    # ------------------------------------------------------------------ #
    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())
            logger.info("Motor Monitor background service started (idle).")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("Motor Monitor background service stopped.")

    # ------------------------------------------------------------------ #
    # Session control
    # ------------------------------------------------------------------ #
    def start_session(self):
        """Begin a fresh voltage window (motor ON). Bucket progress is NOT reset."""
        self.voltage_window.clear()
        self.session_active = True
        self.motor_on = True
        self._last_accum_ts = datetime.datetime.now().timestamp()
        logger.info(
            f"Polling session STARTED (every {self.poll_interval}s, window "
            f"{self.window_size}). Bucket carried over at {self.bucket_progress:.4f}h."
        )

    def stop_session(self, reason: str = ""):
        """Stop polling and clear the voltage window. Bucket progress is preserved."""
        if self.session_active:
            logger.info(f"Polling session STOPPED. {reason}".strip())
        self.session_active = False
        self.motor_on = False
        self.voltage_window.clear()
        self._last_accum_ts = None

    # ------------------------------------------------------------------ #
    # Bucket control
    # ------------------------------------------------------------------ #
    def set_bucket_size(self, size: float):
        """Update the bucket capacity (persisted). Current progress is unchanged."""
        self.bucket_size = float(size)
        self._save_state()
        logger.info(f"Bucket size set to {self.bucket_size}.")
        self.add_log(self.last_voltage, "BUCKET_SIZE", f"Bucket size set to {self.bucket_size} hrs.")

    def reset_bucket(self, reason: str = "manual"):
        """Reset bucket progress to zero (manual button or daily 11:00 reset)."""
        self.bucket_progress = 0.0
        self.bucket_last_reset_date = datetime.date.today().isoformat()
        self._save_state()
        logger.info(f"Bucket reset to 0 ({reason}).")
        self.add_log(self.last_voltage, "BUCKET_RESET", f"Bucket progress reset to 0 ({reason}).")

    def _maybe_daily_reset(self):
        """Reset the bucket once per day at/after the configured reset time."""
        now = datetime.datetime.now()
        today = now.date().isoformat()
        if self.bucket_last_reset_date == today:
            return
        reset_time = now.replace(
            hour=Config.BUCKET_RESET_HOUR, minute=Config.BUCKET_RESET_MINUTE,
            second=0, microsecond=0,
        )
        if now >= reset_time:
            self.reset_bucket(reason=f"daily {Config.BUCKET_RESET_HOUR:02d}:{Config.BUCKET_RESET_MINUTE:02d}")

    def bucket_state(self) -> dict:
        return {
            "bucket_progress": round(self.bucket_progress, 4),
            "bucket_size": self.bucket_size,
        }

    # ------------------------------------------------------------------ #
    # Schedules (auto turn-ON at set times)
    # ------------------------------------------------------------------ #
    _TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

    def _default_schedules(self):
        return [{"id": uuid.uuid4().hex[:8], "time": t, "enabled": True} for t in ("02:00", "03:00")]

    def _load_schedules(self):
        try:
            if os.path.exists(self._schedules_file):
                with open(self._schedules_file, "r") as f:
                    self.schedules = json.load(f).get("schedules", [])
            else:
                self.schedules = self._default_schedules()
                self._save_schedules()
            logger.info(f"Loaded {len(self.schedules)} schedule(s): "
                        f"{[s['time'] for s in self.schedules]} ({self.timezone_name}).")
        except Exception as e:
            logger.error(f"Could not load schedules ({e}); seeding defaults.")
            self.schedules = self._default_schedules()

    def _save_schedules(self):
        try:
            with open(self._schedules_file, "w") as f:
                json.dump({"schedules": self.schedules}, f)
        except Exception as e:
            logger.error(f"Could not save schedules: {e}")

    def add_schedule(self, time_str: str) -> dict:
        if not self._TIME_RE.match(time_str or ""):
            raise ValueError("Time must be in HH:MM 24-hour format.")
        if any(s["time"] == time_str for s in self.schedules):
            raise ValueError(f"A schedule for {time_str} already exists.")
        sch = {"id": uuid.uuid4().hex[:8], "time": time_str, "enabled": True}
        self.schedules.append(sch)
        self.schedules.sort(key=lambda s: s["time"])
        self._save_schedules()
        logger.info(f"Schedule added: {time_str} {self.timezone_name}.")
        return sch

    def remove_schedule(self, sid: str) -> bool:
        before = len(self.schedules)
        self.schedules = [s for s in self.schedules if s["id"] != sid]
        self._schedule_last_fired.pop(sid, None)
        if len(self.schedules) < before:
            self._save_schedules()
            return True
        return False

    def toggle_schedule(self, sid: str) -> bool:
        for s in self.schedules:
            if s["id"] == sid:
                s["enabled"] = not s.get("enabled", True)
                self._save_schedules()
                return True
        return False

    async def _run_due_schedules(self):
        """Fire any enabled schedule whose IST time is now (once per day)."""
        now = datetime.datetime.now(self._tz) if self._tz else datetime.datetime.now()
        hhmm = now.strftime("%H:%M")
        today = now.date().isoformat()
        for sch in self.schedules:
            if not sch.get("enabled", True):
                continue
            if sch["time"] == hhmm and self._schedule_last_fired.get(sch["id"]) != today:
                self._schedule_last_fired[sch["id"]] = today
                await self._scheduled_turn_on(sch)

    async def _scheduled_turn_on(self, sch: dict, source: str = "App"):
        label = f"{source} schedule {sch['time']} {self.timezone_name}"
        if self.motor_on:
            logger.info(f"{label} due, but motor already ON; skipping.")
            self.add_log(self.last_voltage, "SCHEDULE", f"{label}: motor already ON.")
            return
        logger.info(f"{label} fired: turning motor ON.")
        async with self.lock:
            result = await TPLinkClient.turn_on()
        if result.get("success"):
            self.start_session()
            self.add_log(self.last_voltage, "SCHEDULE_ON", f"{label}: motor turned ON.")
        else:
            self.add_log(self.last_voltage, "SCHEDULE_FAILED",
                         f"{label}: turn-on FAILED: {result.get('error')}.")

    # ------------------------------------------------------------------ #
    # TP-Link cloud schedules (fetched; drive the motor at ON-times)
    # ------------------------------------------------------------------ #
    async def fetch_tplink_schedules(self) -> list:
        """Fetch schedule rules from the TP-Link cloud and cache them."""
        try:
            rules = await TPLinkClient.get_schedules()
        except Exception as e:
            logger.error(f"TP-Link schedule fetch failed: {e}")
            return self.tplink_schedules
        parsed = []
        for r in rules:
            s_min = r.get("s_min", 0)
            parsed.append({
                "id": r.get("id"),
                "time": f"{s_min // 60:02d}:{s_min % 60:02d}",
                "on": bool(r.get("desired_states", {}).get("on")),
                "enabled": bool(r.get("enable")),
            })
        parsed.sort(key=lambda x: x["time"])
        self.tplink_schedules = parsed
        self.tplink_last_fetch = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"Fetched {len(parsed)} TP-Link schedule(s); cached.")
        return parsed

    async def _maybe_daily_tplink_fetch(self):
        """Re-fetch the cloud schedules once per day at/after the sync time."""
        now = datetime.datetime.now(self._tz) if self._tz else datetime.datetime.now()
        if self._tplink_last_fetch_date == now.date().isoformat():
            return
        sync_time = now.replace(hour=self.tplink_sync_hour, minute=self.tplink_sync_minute, second=0, microsecond=0)
        if now >= sync_time:
            self._tplink_last_fetch_date = now.date().isoformat()
            logger.info(f"Daily TP-Link schedule sync ({self.tplink_sync_hour:02d}:{self.tplink_sync_minute:02d}).")
            await self.fetch_tplink_schedules()

    async def _run_due_tplink_schedules(self):
        """Fire the motor ON at each enabled TP-Link ON-time (once per day)."""
        now = datetime.datetime.now(self._tz) if self._tz else datetime.datetime.now()
        hhmm = now.strftime("%H:%M")
        today = now.date().isoformat()
        for s in self.tplink_schedules:
            if not (s.get("on") and s.get("enabled")):
                continue
            key = f"tplink:{s.get('id')}"
            if s["time"] == hhmm and self._tplink_last_fired.get(key) != today:
                self._tplink_last_fired[key] = today
                await self._scheduled_turn_on({"time": s["time"]}, source="TP-Link")

    # ------------------------------------------------------------------ #
    # History helper
    # ------------------------------------------------------------------ #
    def add_log(self, voltage, action: str, details: str):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.history.insert(0, {
            "timestamp": now, "voltage": voltage, "action": action, "details": details,
        })
        if len(self.history) > self.max_history_len:
            self.history.pop()

    # ------------------------------------------------------------------ #
    # Voltage reads
    # ------------------------------------------------------------------ #
    async def check_now(self) -> dict:
        """Read voltage once for display. Read-only: no window feed, no shutdown."""
        async with self.lock:
            return await self._read_voltage_locked(feed_window=False)

    async def _read_voltage_locked(self, feed_window: bool) -> dict:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_run = now_str
        try:
            voltage = await TPLinkClient.get_voltage()
            self.last_voltage = voltage
            if feed_window and voltage is not None:
                self.voltage_window.append(voltage)
            self.add_log(voltage, "READING", f"Voltage reading: {voltage}.")
            return {"timestamp": now_str, "voltage": voltage, "action": "READING"}
        except Exception as e:
            details = f"Failed to check voltage: {str(e)}"
            logger.error(details)
            self.add_log(None, "CHECK_ERROR", details)
            return {"timestamp": now_str, "voltage": None, "action": "CHECK_ERROR"}

    # ------------------------------------------------------------------ #
    # Window evaluation
    # ------------------------------------------------------------------ #
    def _window_all_abnormal(self) -> bool:
        if len(self.voltage_window) < self.window_size:
            return False
        return all(
            Config.VOLTAGE_MIN_THRESHOLD < v < Config.VOLTAGE_MAX_THRESHOLD
            for v in self.voltage_window
        )

    # ------------------------------------------------------------------ #
    # Background loop
    # ------------------------------------------------------------------ #
    async def _run_loop(self):
        # Populate the TP-Link schedule cache once at startup.
        try:
            await self.fetch_tplink_schedules()
        except Exception as e:
            logger.error(f"Initial TP-Link schedule fetch failed: {e}")

        while True:
            try:
                # Daily reset, cloud sync, and schedule checks run regardless of motor state.
                self._maybe_daily_reset()
                await self._maybe_daily_tplink_fetch()
                await self._run_due_schedules()
                await self._run_due_tplink_schedules()
                if self.session_active and self.is_active:
                    await self._session_tick()
            except asyncio.CancelledError:
                logger.info("Monitoring loop cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop execution: {str(e)}")

            await asyncio.sleep(self.poll_interval)

    async def _session_tick(self):
        async with self.lock:
            if not (self.session_active and self.is_active):
                return

            res = await self._read_voltage_locked(feed_window=True)
            voltage = res.get("voltage")

            # --- Bucket accrual: real elapsed time attributed to this reading ---
            now_ts = datetime.datetime.now().timestamp()
            if self._last_accum_ts is not None and voltage is not None and voltage > Config.VOLTAGE_MAX_THRESHOLD:
                elapsed_hours = (now_ts - self._last_accum_ts) / 3600.0
                self.bucket_progress += elapsed_hours
                self._save_state()
            self._last_accum_ts = now_ts

            # --- Condition 1: sustained abnormal voltage for a full window ---
            if self._window_all_abnormal():
                logger.warning(
                    f"Condition 1: all {self.window_size} readings (~3 min) in the "
                    f"{Config.VOLTAGE_MIN_THRESHOLD}-{Config.VOLTAGE_MAX_THRESHOLD} band. Shutting off."
                )
                await self._shutdown("Condition 1: sustained abnormal voltage (~3 min).")
                return

            # --- Condition 2: bucket full ---
            if self.bucket_progress >= self.bucket_size:
                logger.warning(
                    f"Condition 2: bucket full ({self.bucket_progress:.4f} >= {self.bucket_size}). Shutting off."
                )
                await self._shutdown(
                    f"Condition 2: bucket full ({self.bucket_progress:.2f}/{self.bucket_size}h)."
                )
                return

    async def _shutdown(self, reason: str):
        """Turn the motor off, end the session, keep the bucket. Caller holds the lock."""
        off_result = await TPLinkClient.turn_off()
        if off_result.get("success"):
            self.add_log(self.last_voltage, "OFF_TRIGGERED", f"{reason} Motor shut off.")
            self.stop_session(reason)
        else:
            self.add_log(
                self.last_voltage, "OFF_FAILED",
                f"{reason} Shutdown FAILED: {off_result.get('error')}. Will retry."
            )
            logger.error("Auto shutdown failed; session continues and will retry.")


# Global monitor instance
monitor_instance = MotorMonitor()
