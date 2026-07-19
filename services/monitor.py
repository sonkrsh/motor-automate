import asyncio
import datetime
import logging
from config import Config
from services.tplink import TPLinkClient

logger = logging.getLogger("motor_automate.monitor")

class MotorMonitor:
    def __init__(self):
        self.is_active = True  # Whether the automation check is enabled
        self.last_run = None
        self.last_voltage = None
        self.history = []  # List of past check logs
        self.max_history_len = 100
        self._task = None

        # Smart Polling Interval Configuration
        self.slow_interval = Config.MONITOR_SLOW_INTERVAL_SECONDS
        self.fast_interval = Config.MONITOR_FAST_INTERVAL_SECONDS
        self.current_interval = self.slow_interval

        # Schedules Configuration
        self.scheduled_windows = []  # List of (start_min, end_min) tuples
        self.last_schedule_update_date = None
        self.just_turned_on = False

    def start(self):
        """Starts the background task loop if not already running."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())
            logger.info("Motor Monitor background service started.")

    def stop(self):
        """Cancels the background task loop."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("Motor Monitor background service stopped.")

    def add_log(self, voltage: float, action: str, details: str):
        """Helper to append log entries to history with a timestamp."""
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = {
            "timestamp": now,
            "voltage": voltage,
            "action": action,
            "details": details
        }
        self.history.insert(0, log_entry)  # Add to top of list
        # Limit history size
        if len(self.history) > self.max_history_len:
            self.history.pop()

    async def update_schedules(self):
        """
        Queries active schedules from the TP-Link API and parses them to build time windows.
        Each time window is a tuple of (start_minute, end_minute) relative to midnight.
        """
        logger.info("Synchronizing daily schedules from TP-Link cloud...")
        try:
            rules = await TPLinkClient.get_schedules()
            
            # Filter active schedule rules
            active_rules = [r for r in rules if r.get("enable") is True]
            if not active_rules:
                self.scheduled_windows = []
                logger.info("No active schedule rules found.")
                self.last_schedule_update_date = datetime.date.today().isoformat()
                return

            # Reconstruct ON/OFF windows
            # Sort rules by s_min (start minute of the day)
            active_rules.sort(key=lambda x: x.get("s_min", 0))
            
            new_windows = []
            start_min = None
            
            for rule in active_rules:
                desired_on = rule.get("desired_states", {}).get("on")
                s_min = rule.get("s_min", 0)
                
                if desired_on is True:
                    # Found a scheduled ON rule
                    start_min = s_min
                elif desired_on is False and start_min is not None:
                    # Found a scheduled OFF rule that terminates the current active window
                    new_windows.append((start_min, s_min))
                    logger.info(f"Reconstructed schedule window: {start_min // 60:02d}:{start_min % 60:02d} to {s_min // 60:02d}:{s_min % 60:02d}")
                    start_min = None
                    
            # If there is a scheduled ON rule but no matching OFF rule, assume it runs for 1 hour
            if start_min is not None:
                end_min = min(start_min + 60, 1439)
                new_windows.append((start_min, end_min))
                logger.info(f"Reconstructed open schedule window (default 1h): {start_min // 60:02d}:{start_min % 60:02d} to {end_min // 60:02d}:{end_min % 60:02d}")

            self.scheduled_windows = new_windows
            self.last_schedule_update_date = datetime.date.today().isoformat()
            logger.info(f"Schedule sync complete. Active windows: {self.scheduled_windows}")
        except Exception as e:
            logger.error(f"Error during schedule update: {str(e)}")

    def is_in_scheduled_window(self) -> bool:
        """Checks if the current local time falls within any active scheduled window."""
        now = datetime.datetime.now()
        current_minute = now.hour * 60 + now.minute
        
        for start_min, end_min in self.scheduled_windows:
            if start_min <= current_minute <= end_min:
                return True
        return False

    async def check_now(self) -> dict:
        """
        Manually triggers a single voltage check and performs safety action if necessary.
        Returns the result dict.
        """
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_run = now_str
        
        try:
            voltage = await TPLinkClient.get_voltage()
            self.last_voltage = voltage
            
            # Condition check: Trigger off only when 10 < voltage < 400
            if Config.VOLTAGE_MIN_THRESHOLD < voltage < Config.VOLTAGE_MAX_THRESHOLD:
                action = "OFF_TRIGGERED"
                details = f"Under-voltage detected ({voltage}V). Initiating shutdown sequence."
                logger.warning(details)
                
                # Execute turn off
                off_result = await TPLinkClient.turn_off()
                if off_result.get("success"):
                    details += f" Shutdown success: {off_result.get('message')}."
                    # Once turned off, return interval back to slow mode (idle)
                    self.current_interval = self.slow_interval
                else:
                    action = "OFF_FAILED"
                    details += f" Shutdown failed: {off_result.get('error')}."
                    logger.error(details)
            elif voltage <= Config.VOLTAGE_MIN_THRESHOLD:
                action = "NO_ACTION"
                details = f"Voltage too low ({voltage}V). Motor is off or power is cut."
                logger.info(details)
                # Ensure we return to slow mode if the plug is verified OFF
                if not self.is_in_scheduled_window():
                    if getattr(self, 'just_turned_on', False):
                        logger.info("Motor was just manually started; bypassing idle transition to allow sensor boot-up.")
                        self.just_turned_on = False
                    else:
                        self.current_interval = self.slow_interval
            else:
                action = "NO_ACTION"
                details = f"Voltage normal ({voltage}V). Motor keeping operational."
                logger.info(details)
                
            self.add_log(voltage, action, details)
            return {"timestamp": now_str, "voltage": voltage, "action": action, "details": details}
            
        except Exception as e:
            action = "CHECK_ERROR"
            details = f"Failed to check voltage: {str(e)}"
            logger.error(details)
            self.add_log(0.0, action, details)
            return {"timestamp": now_str, "voltage": 0.0, "action": action, "details": details}

    async def _run_loop(self):
        """Tick-based background loop checking local state every 10 seconds (0 API calls when idle)."""
        # Initial schedule sync
        try:
            await self.update_schedules()
        except Exception as e:
            logger.error(f"Initial schedule sync failed: {str(e)}")
            
        # Run one initial check at startup to capture current state
        if self.is_active:
            try:
                logger.info("Performing initial startup voltage check...")
                initial_res = await self.check_now()
                initial_voltage = initial_res.get("voltage", 0.0)
                if initial_voltage > Config.VOLTAGE_MIN_THRESHOLD:
                    self.current_interval = self.fast_interval
                    logger.info("Motor is ON at startup. Setting interval to FAST mode.")
            except Exception as e:
                logger.error(f"Initial startup check failed: {str(e)}")

        last_api_check_time = datetime.datetime.now().timestamp()
        
        while True:
            try:
                # 1. Check if daily schedule update is needed (daily at 12:30 AM)
                now = datetime.datetime.now()
                current_timestamp = now.timestamp()
                today_str = now.date().isoformat()
                
                # Check if it is a new day and past 00:30 (12:30 AM)
                if self.last_schedule_update_date != today_str and (now.hour > 0 or (now.hour == 0 and now.minute >= 30)):
                    await self.update_schedules()
                
                # 2. Check if we are inside a scheduled window
                in_scheduled_window = self.is_in_scheduled_window()
                
                # 3. Determine if we should perform an API check
                should_check = False
                
                if in_scheduled_window:
                    self.current_interval = self.fast_interval
                    if current_timestamp - last_api_check_time >= self.fast_interval:
                        should_check = True
                else:
                    # If outside scheduled window, check only if we were actively in fast mode
                    if self.current_interval == self.fast_interval:
                        if current_timestamp - last_api_check_time >= self.fast_interval:
                            should_check = True
                
                # 4. Execute check if active
                if should_check and self.is_active:
                    logger.info(f"Executing scheduled API check. Current mode: FAST (interval: {self.current_interval}s)")
                    check_res = await self.check_now()
                    last_api_check_time = current_timestamp
                    
                    # Update interval based on check results
                    voltage = check_res.get("voltage", 0.0)
                    if voltage > Config.VOLTAGE_MIN_THRESHOLD:
                        self.current_interval = self.fast_interval
                    else:
                        if not in_scheduled_window:
                            self.current_interval = self.slow_interval
                            logger.info("Motor is OFF and outside scheduled window. Going IDLE (0 API calls).")
                            
            except asyncio.CancelledError:
                logger.info("Monitoring loop cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop execution: {str(e)}")
            
            # Sleep for 10 seconds locally (0 API calls, just wakes up to check local state)
            await asyncio.sleep(10)

# Global monitor instance
monitor_instance = MotorMonitor()
