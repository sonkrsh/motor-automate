import httpx
import json
import logging
from config import Config

logger = logging.getLogger("motor_automate.tplink")

class TPLinkClient:
    @staticmethod
    async def get_voltage() -> float:
        """
        Fetches the current voltage (labeled as current_power) from the Tapo device.
        """
        url = f"https://{Config.TPLINK_HOST}/v1/things/{Config.THING_ID}/features/currentPower"
        headers = Config.get_get_headers()
        
        # Using verify=False to bypass SSL errors (since curl experienced SSL certificate problems locally)
        async with httpx.AsyncClient(verify=False) as client:
            try:
                response = await client.get(url, headers=headers, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    voltage = float(data.get("current_power", 0.0))
                    logger.info(f"Fetched voltage successfully: {voltage}V")
                    return voltage
                else:
                    logger.error(f"Failed to fetch voltage. HTTP {response.status_code}: {response.text}")
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request, response=response
                    )
            except Exception as e:
                logger.error(f"Error fetching voltage: {str(e)}")
                raise

    @staticmethod
    async def turn_off() -> dict:
        """
        Turns off the motor plug using the version shadow patch sequence.
        """
        url = f"https://{Config.TPLINK_HOST}/v1/things/{Config.THING_ID}/shadows"
        headers = Config.get_headers()
        
        # Step 1: Probe the endpoint with version=1 to get the current present version
        probe_payload = {
            "state": {
                "desired": {
                    "on": False
                }
            },
            "version": 1
        }
        
        cur_version = None
        async with httpx.AsyncClient(verify=False) as client:
            try:
                logger.info("Probing shadow endpoint for present version...")
                response = await client.patch(url, headers=headers, json=probe_payload, timeout=10.0)
                if response.status_code == 200:
                    # In the very unlikely case that version 1 was actually correct
                    data = response.json()
                    logger.info("Unexpected success on version 1. Plug state modified.")
                    return {"success": True, "message": "Plug turned off directly using version 1", "data": data}
                else:
                    logger.warning(f"Probe failed as expected. HTTP {response.status_code}: {response.text}")
            except httpx.HTTPError as e:
                # Catching client HTTP status errors
                response = e.response
            
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
                else:
                    logger.error(f"Failed to patch shadow. HTTP {response.status_code}: {response.text}")
                    return {"success": False, "error": f"Failed patch with status {response.status_code}: {response.text}"}
            except Exception as e:
                logger.error(f"Exception during shadow patch: {str(e)}")
                return {"success": False, "error": str(e)}
