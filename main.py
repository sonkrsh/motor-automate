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
        "last_run": monitor_instance.last_run,
        "last_voltage": monitor_instance.last_voltage,
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

@app.post("/api/turn-off")
async def manual_turn_off():
    """Triggers the turn-off shadow patch endpoint manually."""
    logger.info("Manual turn-off request received.")
    result = await TPLinkClient.turn_off()
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result
