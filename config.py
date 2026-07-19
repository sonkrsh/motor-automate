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

    MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "180"))
    VOLTAGE_MIN_THRESHOLD = float(os.getenv("VOLTAGE_MIN_THRESHOLD", "10.0"))
    VOLTAGE_MAX_THRESHOLD = float(os.getenv("VOLTAGE_MAX_THRESHOLD", "400.0"))

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
