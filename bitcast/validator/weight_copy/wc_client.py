"""
Client for fetching weights from the reference validator's API.
Handles API communication, error handling, and weight format conversion.
"""
import httpx
import bittensor as bt
import numpy as np
from typing import Optional, Tuple
from bitcast.validator.utils.config import REFERENCE_VALIDATOR_ENDPOINT


class WeightCopyClient:
    """Client for fetching weights from reference validator."""
    
    def __init__(self, timeout: float = 10.0):
        self.base_url = REFERENCE_VALIDATOR_ENDPOINT
        self.timeout = timeout
        bt.logging.info(f"WeightCopyClient initialized with endpoint: {self.base_url}")
        
    async def fetch_weights(self) -> Optional[Tuple[np.ndarray, np.ndarray, int]]:
        """
        Fetch weights from reference validator API.
        
        Returns:
            Tuple of (scores, hotkeys, step) if successful, None if failed
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/weights")
                response.raise_for_status()
                
                data = response.json()
                
                # Extract weights data
                weights_data = data.get("weights", [])
                total_miners = data.get("total_miners", 0)
                step = data.get("step", 0)
                
                if not weights_data:
                    bt.logging.warning("No weights data received from reference validator")
                    return None
                
                # Convert to numpy arrays for validator use
                scores = np.zeros(total_miners, dtype=np.float32)
                hotkeys = np.empty(total_miners, dtype=object)
                
                for weight_info in weights_data:
                    uid = weight_info["uid"]
                    scores[uid] = weight_info["raw_weight"]
                    hotkeys[uid] = weight_info["hotkey"]
                
                bt.logging.info(
                    f"✅ Successfully fetched weights from reference validator "
                    f"(step={step}, miners={total_miners})"
                )
                
                return scores, hotkeys, step
                
        except httpx.TimeoutException:
            bt.logging.warning(
                f"⚠️ Timeout connecting to reference validator at {self.base_url} "
                f"- continuing with existing weights"
            )
            return None
        except httpx.HTTPStatusError as e:
            bt.logging.warning(
                f"⚠️ HTTP error fetching weights from reference validator: {e.response.status_code} "
                f"- continuing with existing weights"
            )
            return None
        except Exception as e:
            bt.logging.warning(
                f"⚠️ Error fetching weights from reference validator: {e} "
                f"- continuing with existing weights"
            )
            return None

