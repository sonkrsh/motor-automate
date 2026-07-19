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
                else:
                    action = "OFF_FAILED"
                    details += f" Shutdown failed: {off_result.get('error')}."
                    logger.error(details)
            elif voltage <= Config.VOLTAGE_MIN_THRESHOLD:
                action = "NO_ACTION"
                details = f"Voltage too low ({voltage}V). Motor is off or power is cut."
                logger.info(details)
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
        """Infinite polling loop running in the background."""
        while True:
            try:
                if self.is_active:
                    logger.info("Executing scheduled voltage monitoring check...")
                    await self.check_now()
                else:
                    logger.info("Automation is currently paused. Skipping check.")
            except asyncio.CancelledError:
                logger.info("Monitoring loop cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop execution: {str(e)}")
            
            # Sleep for the configured interval (e.g., 3 minutes)
            await asyncio.sleep(Config.MONITOR_INTERVAL_SECONDS)

# Global monitor instance
monitor_instance = MotorMonitor()
