import httpx
import json
import logging
from config import Config

logger = logging.getLogger("motor_automate.tplink")

TOKEN_ERROR_MESSAGE = (
    "TP-Link token expired or invalid (HTTP 401). Update AUTHORIZATION or configure "
    "TPLINK_EMAIL and TPLINK_PASSWORD in .env for automatic re-authentication."
)


class TokenInvalidError(Exception):
    """Raised when the TP-Link cloud rejects the configured Authorization token."""

    def __init__(self, message: str = TOKEN_ERROR_MESSAGE):
        super().__init__(message)


def _token_error_result(extra_msg: str = "") -> dict:
    msg = f"{TOKEN_ERROR_MESSAGE} {extra_msg}".strip()
    logger.error(msg)
    return {"success": False, "error": msg, "token_error": True}


class TPLinkClient:
    @staticmethod
    def has_credentials() -> bool:
        """Returns True if TPLINK_EMAIL and TPLINK_PASSWORD are configured."""
        return bool(Config.TPLINK_EMAIL and Config.TPLINK_PASSWORD)

    @staticmethod
    async def login() -> dict:
        """
        Attempts to authenticate with TP-Link Cloud using TPLINK_EMAIL & TPLINK_PASSWORD.
        Updates Config.AUTHORIZATION and persists it to .env upon success.
        """
        if not TPLinkClient.has_credentials():
            logger.warning("No TPLINK_EMAIL and TPLINK_PASSWORD found in configuration. Cannot auto-login.")
            return {"success": False, "error": "No credentials configured in .env"}

        hosts_to_try = [
            Config.TPLINK_AUTH_HOST,
            "aps1-wap.i.tplinkcloud.com",
            "wap.tplinkcloud.com",
            "n-wap.i.tplinkcloud.com"
        ]
        # Remove duplicates while preserving order
        hosts_to_try = list(dict.fromkeys(hosts_to_try))

        term_id = Config.X_TERM_ID or "127F328301882DB826BE06799FCA06EA"
        payload = {
            "method": "login",
            "params": {
                "appType": Config.X_APP_NAME or "TP-Link_Tapo_Android",
                "cloudUserName": Config.TPLINK_EMAIL,
                "cloudPassword": Config.TPLINK_PASSWORD,
                "terminalUUID": term_id
            }
        }

        last_error = None
        async with httpx.AsyncClient(verify=False) as client:
            for host in hosts_to_try:
                url = f"https://{host}"
                try:
                    logger.info(f"Attempting cloud login via {url}...")
                    response = await client.post(
                        url,
                        json=payload,
                        headers={"Content-Type": "application/json; charset=UTF-8"},
                        timeout=12.0
                    )
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("error_code") == 0 and "result" in data:
                            token_val = data["result"].get("token")
                            if token_val:
                                formatted_token = Config.set_authorization(token_val)
                                logger.info(f"TP-Link cloud login SUCCESS via {host}. Fresh token cached.")
                                return {"success": True, "token": formatted_token, "host": host}
                        else:
                            last_error = f"Login API error from {host}: {data.get('msg') or data}"
                            logger.warning(last_error)
                    else:
                        last_error = f"HTTP {response.status_code} from {host}: {response.text}"
                        logger.warning(last_error)
                except Exception as e:
                    last_error = f"Connection error to {host}: {str(e)}"
                    logger.warning(last_error)

        logger.error(f"Cloud login failed on all endpoints: {last_error}")
        return {"success": False, "error": last_error or "Cloud login failed"}

    @staticmethod
    async def get_voltage(retry_on_401: bool = True) -> float:
        """
        Fetches the current voltage (labeled as current_power) from the Tapo device.
        Automatically tries cloud login and retry if HTTP 401 is received.
        """
        url = f"https://{Config.TPLINK_HOST}/v1/things/{Config.THING_ID}/features/currentPower"
        headers = Config.get_get_headers()
        
        async with httpx.AsyncClient(verify=False) as client:
            try:
                response = await client.get(url, headers=headers, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    voltage = float(data.get("current_power", 0.0))
                    logger.info(f"Fetched voltage successfully: {voltage}V")
                    return voltage
                elif response.status_code == 401:
                    logger.warning("Received HTTP 401 while fetching voltage.")
                    if retry_on_401 and TPLinkClient.has_credentials():
                        logger.info("Attempting automatic re-authentication for get_voltage...")
                        login_res = await TPLinkClient.login()
                        if login_res.get("success"):
                            # Retry once with refreshed token
                            return await TPLinkClient.get_voltage(retry_on_401=False)
                    raise TokenInvalidError()
                else:
                    logger.error(f"Failed to fetch voltage. HTTP {response.status_code}: {response.text}")
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request, response=response
                    )
            except TokenInvalidError:
                raise
            except Exception as e:
                logger.error(f"Error fetching voltage: {str(e)}")
                raise

    @staticmethod
    async def turn_off(retry_on_401: bool = True) -> dict:
        """
        Turns off the motor plug using the version shadow patch sequence.
        Automatically tries cloud login and retry if HTTP 401 is received.
        """
        url = f"https://{Config.TPLINK_HOST}/v1/things/{Config.THING_ID}/shadows"
        headers = Config.get_headers()
        
        probe_payload = {
            "state": {
                "desired": {
                    "on": False
                }
            },
            "version": 1
        }
        
        cur_version = None
        response = None
        async with httpx.AsyncClient(verify=False) as client:
            try:
                logger.info("Probing shadow endpoint for present version...")
                response = await client.patch(url, headers=headers, json=probe_payload, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    logger.info("Unexpected success on version 1. Plug state modified.")
                    return {"success": True, "message": "Plug turned off directly using version 1", "data": data}
                else:
                    logger.warning(f"Probe failed as expected. HTTP {response.status_code}: {response.text}")
            except httpx.HTTPError as e:
                response = getattr(e, "response", None)

            if response is None:
                return {"success": False, "error": "Shadow probe failed: no response from TP-Link cloud."}

            if response.status_code == 401:
                logger.warning("Received HTTP 401 during turn_off probe.")
                if retry_on_401 and TPLinkClient.has_credentials():
                    logger.info("Attempting automatic re-authentication for turn_off...")
                    login_res = await TPLinkClient.login()
                    if login_res.get("success"):
                        return await TPLinkClient.turn_off(retry_on_401=False)
                return _token_error_result()

            # Extract curVersion from response body
            try:
                err_data = response.json()
                if "data" in err_data and "curVersion" in err_data["data"]:
                    cur_version = err_data["data"]["curVersion"]
                    logger.info(f"Discovered current shadow version: {cur_version}")
                else:
                    raise ValueError(f"Could not extract curVersion from response: {err_data}")
            except Exception as e:
                logger.error(f"Error extracting shadow version: {str(e)}")
                return {"success": False, "error": f"Failed to discover version: {str(e)}"}
            
            # Step 2: PATCH with cur_version + 1
            target_version = cur_version + 1
            payload = {
                "state": {
                    "desired": {
                        "on": False
                    }
                },
                "version": target_version
            }
            
            try:
                logger.info(f"Sending PATCH request with version {target_version} to turn off plug...")
                response = await client.patch(url, headers=headers, json=payload, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    logger.info("Successfully turned off the motor plug.")
                    return {"success": True, "message": "Plug turned off successfully", "version": target_version, "data": data}
                elif response.status_code == 401:
                    logger.warning("Received HTTP 401 during turn_off patch.")
                    if retry_on_401 and TPLinkClient.has_credentials():
                        login_res = await TPLinkClient.login()
                        if login_res.get("success"):
                            return await TPLinkClient.turn_off(retry_on_401=False)
                    return _token_error_result()
                else:
                    logger.error(f"Failed to patch shadow. HTTP {response.status_code}: {response.text}")
                    return {"success": False, "error": f"Failed patch with status {response.status_code}: {response.text}"}
            except Exception as e:
                logger.error(f"Exception during shadow patch: {str(e)}")
                return {"success": False, "error": str(e)}

    @staticmethod
    async def turn_on(retry_on_401: bool = True) -> dict:
        """
        Turns on the motor plug using the version shadow patch sequence.
        Automatically tries cloud login and retry if HTTP 401 is received.
        """
        url = f"https://{Config.TPLINK_HOST}/v1/things/{Config.THING_ID}/shadows"
        headers = Config.get_headers()
        
        probe_payload = {
            "state": {
                "desired": {
                    "on": True
                }
            },
            "version": 1
        }
        
        cur_version = None
        response = None
        async with httpx.AsyncClient(verify=False) as client:
            try:
                logger.info("Probing shadow endpoint for present version (turn on)...")
                response = await client.patch(url, headers=headers, json=probe_payload, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    logger.info("Unexpected success on version 1. Plug state modified.")
                    return {"success": True, "message": "Plug turned on directly using version 1", "data": data}
            except httpx.HTTPError as e:
                response = getattr(e, "response", None)

            if response is None:
                return {"success": False, "error": "Shadow probe failed: no response from TP-Link cloud."}

            if response.status_code == 401:
                logger.warning("Received HTTP 401 during turn_on probe.")
                if retry_on_401 and TPLinkClient.has_credentials():
                    logger.info("Attempting automatic re-authentication for turn_on...")
                    login_res = await TPLinkClient.login()
                    if login_res.get("success"):
                        return await TPLinkClient.turn_on(retry_on_401=False)
                return _token_error_result()

            # Extract curVersion from response body
            try:
                err_data = response.json()
                if "data" in err_data and "curVersion" in err_data["data"]:
                    cur_version = err_data["data"]["curVersion"]
                    logger.info(f"Discovered current shadow version: {cur_version}")
                else:
                    raise ValueError(f"Could not extract curVersion from response: {err_data}")
            except Exception as e:
                logger.error(f"Error extracting shadow version: {str(e)}")
                return {"success": False, "error": f"Failed to discover version: {str(e)}"}
            
            # Step 2: PATCH with cur_version + 1
            target_version = cur_version + 1
            payload = {
                "state": {
                    "desired": {
                        "on": True
                    }
                },
                "version": target_version
            }
            
            try:
                logger.info(f"Sending PATCH request with version {target_version} to turn on plug...")
                response = await client.patch(url, headers=headers, json=payload, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    logger.info("Successfully turned on the motor plug.")
                    return {"success": True, "message": "Plug turned on successfully", "version": target_version, "data": data}
                elif response.status_code == 401:
                    logger.warning("Received HTTP 401 during turn_on patch.")
                    if retry_on_401 and TPLinkClient.has_credentials():
                        login_res = await TPLinkClient.login()
                        if login_res.get("success"):
                            return await TPLinkClient.turn_on(retry_on_401=False)
                    return _token_error_result()
                else:
                    logger.error(f"Failed to patch shadow. HTTP {response.status_code}: {response.text}")
                    return {"success": False, "error": f"Failed patch with status {response.status_code}: {response.text}"}
            except Exception as e:
                logger.error(f"Exception during shadow patch: {str(e)}")
                return {"success": False, "error": str(e)}

    @staticmethod
    async def get_schedules(retry_on_401: bool = True) -> list:
        """
        Fetches the active schedule rules for the device.
        Automatically tries cloud login and retry if HTTP 401 is received.
        """
        url = f"https://{Config.TPLINK_HOST}/v1/things/{Config.THING_ID}/rules?startIndex=0&ruleType=schedule"
        headers = Config.get_get_headers()
        
        async with httpx.AsyncClient(verify=False) as client:
            try:
                response = await client.get(url, headers=headers, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    rules = data.get("ruleList", [])
                    logger.info(f"Fetched {len(rules)} schedule rules successfully.")
                    return rules
                elif response.status_code == 401:
                    logger.warning("Received HTTP 401 while fetching schedules.")
                    if retry_on_401 and TPLinkClient.has_credentials():
                        login_res = await TPLinkClient.login()
                        if login_res.get("success"):
                            return await TPLinkClient.get_schedules(retry_on_401=False)
                    logger.error(TOKEN_ERROR_MESSAGE)
                    return []
                else:
                    logger.error(f"Failed to fetch schedules. HTTP {response.status_code}: {response.text}")
                    return []
            except Exception as e:
                logger.error(f"Error fetching schedules: {str(e)}")
                return []

