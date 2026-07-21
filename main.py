import logging
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from services.monitor import monitor_instance
from services.tplink import TPLinkClient
from config import Config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("motor_automate.main")

app = FastAPI(title="Motor Automate")

# Configure templates directory
templates = Jinja2Templates(directory="templates")

# Request Models
class ToggleModel(BaseModel):
    active: bool

class BucketSizeModel(BaseModel):
    size: float

class ScheduleModel(BaseModel):
    time: str

@app.on_event("startup")
async def startup_event():
    """Startup task to initialize and start background polling."""
    logger.info("Application starting up, launching background monitor...")
    monitor_instance.start()

@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown task to clean up background monitoring loop."""
    logger.info("Application shutting down, stopping background monitor...")
    monitor_instance.stop()

@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request):
    """Renders the HTML monitoring dashboard."""
    return templates.TemplateResponse(request, "index.html")

@app.get("/api/status")
async def get_status():
    """API endpoint to get the current state and history log of the system."""
    return {
        "is_active": monitor_instance.is_active,
        "session_active": monitor_instance.session_active,
        "motor_status": "ON" if monitor_instance.motor_on else "OFF",
        "readings_count": len(monitor_instance.voltage_window),
        "window_size": monitor_instance.window_size,
        "last_run": monitor_instance.last_run,
        "last_voltage": monitor_instance.last_voltage,
        "bucket_progress": round(monitor_instance.bucket_progress, 4),
        "bucket_size": monitor_instance.bucket_size,
        "schedules": monitor_instance.schedules,
        "timezone": monitor_instance.timezone_name,
        "tplink_schedules": monitor_instance.tplink_schedules,
        "tplink_last_fetch": monitor_instance.tplink_last_fetch,
        "history": monitor_instance.history
    }

@app.post("/api/check")
async def trigger_manual_check():
    """Manually triggers a voltage check and safety logic immediately."""
    result = await monitor_instance.check_now()
    return result

@app.post("/api/toggle-automation")
async def toggle_automation(payload: ToggleModel):
    """Enables or disables the background 3-minute monitoring loop."""
    monitor_instance.is_active = payload.active
    state_str = "activated" if payload.active else "paused"
    logger.info(f"Automation safeguard has been manually {state_str}.")
    return {"success": True, "active": monitor_instance.is_active}

@app.get("/api/schedules")
async def list_schedules():
    """List the UI-managed auto turn-ON schedules."""
    return {"schedules": monitor_instance.schedules, "timezone": monitor_instance.timezone_name}

@app.post("/api/schedules")
async def add_schedule(payload: ScheduleModel):
    """Add an auto turn-ON schedule at HH:MM (in the configured timezone)."""
    try:
        sch = monitor_instance.add_schedule(payload.time)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(f"Schedule added via UI: {sch['time']}.")
    return {"success": True, "schedule": sch, "schedules": monitor_instance.schedules}

@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    """Remove a schedule by id."""
    if not monitor_instance.remove_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return {"success": True, "schedules": monitor_instance.schedules}

@app.post("/api/schedules/{schedule_id}/toggle")
async def toggle_schedule(schedule_id: str):
    """Enable/disable a schedule by id."""
    if not monitor_instance.toggle_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return {"success": True, "schedules": monitor_instance.schedules}

@app.get("/api/tplink-schedules")
async def tplink_schedules():
    """Fetch schedule rules from the TP-Link cloud, cache them, and return them.
    The cached ON-times drive the motor (auto turn-ON)."""
    schedules = await monitor_instance.fetch_tplink_schedules()
    return {"schedules": schedules, "last_fetch": monitor_instance.tplink_last_fetch}

@app.post("/api/bucket-size")
async def set_bucket_size(payload: BucketSizeModel):
    """Set the bucket capacity (in hours) from the UI. Persisted; progress unchanged."""
    if payload.size <= 0:
        raise HTTPException(status_code=400, detail="Bucket size must be greater than 0.")
    monitor_instance.set_bucket_size(payload.size)
    return {"success": True, **monitor_instance.bucket_state()}

@app.post("/api/reset-bucket")
async def reset_bucket():
    """Manually reset the cumulative bucket progress to zero."""
    logger.info("Manual bucket reset request received.")
    monitor_instance.reset_bucket(reason="manual")
    return {"success": True, **monitor_instance.bucket_state()}

@app.post("/api/turn-off")
async def manual_turn_off():
    """Triggers the turn-off shadow patch endpoint manually."""
    logger.info("Manual turn-off request received.")
    # Hold the monitor lock so this never overlaps a session tick's shutdown.
    async with monitor_instance.lock:
        result = await TPLinkClient.turn_off()
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    # Motor is off: end the polling session and clear its rolling history/timers.
    monitor_instance.stop_session("Motor turned OFF manually via Web UI.")
    logger.info("Motor turned OFF manually via Web UI. Polling session ended.")
    return result

@app.post("/api/turn-on")
async def manual_turn_on():
    """Triggers the turn-on shadow patch endpoint manually and starts monitoring."""
    logger.info("Manual turn-on request received.")
    # Hold the monitor lock so this never overlaps a session tick.
    async with monitor_instance.lock:
        result = await TPLinkClient.turn_on()
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    # Motor is on: begin a fresh 3-minute voltage polling session. The background
    # loop now reads every 10s and will shut the motor off only after a full
    # ~3-minute window of abnormal readings.
    monitor_instance.start_session()
    logger.info("Motor turned ON manually via Web UI. Voltage polling session started.")

    # Read once immediately so the dashboard shows a value right away (display only,
    # not counted toward the 3-minute window).
    try:
        await monitor_instance.check_now()
    except Exception as e:
        logger.error(f"Failed to run immediate display check after manual turn-on: {str(e)}")

    return result
