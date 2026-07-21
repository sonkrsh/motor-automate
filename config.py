import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class Config:
    TPLINK_HOST = os.getenv("TPLINK_HOST", "aps1-app-server.iot.i.tplinkcloud.com")
    THING_ID = os.getenv("THING_ID", "")
    AUTHORIZATION = os.getenv("AUTHORIZATION", "")
    APP_CID = os.getenv("APP_CID", "")
    USER_AGENT = os.getenv("USER_AGENT", "TP-Link_Tapo_Android/3.18.506(sdk_gphone64_arm64;Android 12)")
    
    X_APP_NAME = os.getenv("X_APP_NAME", "TP-Link_Tapo_Android")
    X_APP_VERSION = os.getenv("X_APP_VERSION", "3.18.506")
    X_LOCALE = os.getenv("X_LOCALE", "en_US")
    X_NET_TYPE = os.getenv("X_NET_TYPE", "wifi")
    X_OSPF = os.getenv("X_OSPF", "Android 12")
    X_STRICT = os.getenv("X_STRICT", "0")
    X_TERM_ID = os.getenv("X_TERM_ID", "")

    # A voltage reading is "abnormal" when MIN < reading < MAX.
    VOLTAGE_MIN_THRESHOLD = float(os.getenv("VOLTAGE_MIN_THRESHOLD", "10.0"))
    VOLTAGE_MAX_THRESHOLD = float(os.getenv("VOLTAGE_MAX_THRESHOLD", "400.0"))

    # Polling session: after the motor is turned on, read voltage every
    # POLL_INTERVAL_SECONDS and keep the last VOLTAGE_WINDOW_SIZE readings
    # (10s x 18 = ~3 minutes). The motor is shut off only when every reading
    # in a full window is abnormal (a sustained under-load / dry run).
    POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SEC", "10"))
    VOLTAGE_WINDOW_SIZE = int(os.getenv("VOLTAGE_WINDOW_SIZE", "18"))

    # Bucket: cumulative motor runtime (in HOURS) accrued while voltage > VOLTAGE_MAX.
    # BUCKET_SIZE is the capacity in units where 1 unit = 1 hour of runtime above max.
    # When bucket_progress >= BUCKET_SIZE the motor is shut off (Condition 2).
    BUCKET_SIZE = float(os.getenv("BUCKET_SIZE", "2"))
    # Automatic daily bucket reset time (local 24h clock).
    BUCKET_RESET_HOUR = int(os.getenv("BUCKET_RESET_HOUR", "11"))
    BUCKET_RESET_MINUTE = int(os.getenv("BUCKET_RESET_MINUTE", "0"))
    # Persistent bucket state file (survives restarts; reset only on manual/daily).
    BUCKET_STATE_FILE = os.getenv("BUCKET_STATE_FILE", "bucket_state.json")

    # Schedules: UI-managed times (in this timezone) at which the motor auto-turns ON.
    SCHEDULE_TIMEZONE = os.getenv("SCHEDULE_TIMEZONE", "Asia/Kolkata")   # IST
    SCHEDULES_FILE = os.getenv("SCHEDULES_FILE", "schedules.json")
    # Daily auto-fetch of TP-Link cloud schedules (local 24h clock).
    TPLINK_SYNC_HOUR = int(os.getenv("TPLINK_SYNC_HOUR", "0"))
    TPLINK_SYNC_MINUTE = int(os.getenv("TPLINK_SYNC_MINUTE", "30"))

    @classmethod
    def get_headers(cls) -> dict:
        return {
            'Content-Type': 'application/json; charset=UTF-8',
            'Accept-Encoding': 'gzip',
            'app-cid': cls.APP_CID,
            'Authorization': cls.AUTHORIZATION,
            'User-Agent': cls.USER_AGENT,
            'x-app-name': cls.X_APP_NAME,
            'x-app-version': cls.X_APP_VERSION,
            'x-locale': cls.X_LOCALE,
            'x-net-type': cls.X_NET_TYPE,
            'x-ospf': cls.X_OSPF,
            'x-strict': cls.X_STRICT,
            'x-term-id': cls.X_TERM_ID
        }

    @classmethod
    def get_get_headers(cls) -> dict:
        # GET request doesn't need Content-Type header
        headers = cls.get_headers().copy()
        if 'Content-Type' in headers:
            del headers['Content-Type']
        return headers
