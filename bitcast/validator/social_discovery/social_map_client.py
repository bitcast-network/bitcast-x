"""
Client for downloading social maps from reference validator.
Handles API communication, file saving, and error handling.
"""
import httpx
import bittensor as bt
import json
from pathlib import Path
from typing import Optional
from datetime import datetime

from bitcast.validator.utils.config import REFERENCE_VALIDATOR_ENDPOINT


class SocialMapClient:
    """Client for downloading social maps from reference validator."""
    
    def __init__(self, timeout: float = 30.0):
        self.base_url = REFERENCE_VALIDATOR_ENDPOINT
        self.timeout = timeout
    
    async def download_social_map(
        self, 
        pool_name: str, 
        save_dir: Optional[Path] = None
    ) -> Optional[str]:
        """
        Download and save social map for a pool.
        
        Args:
            pool_name: Name of pool to download map for
            save_dir: Optional directory to save to (default: auto-detect)
            
        Returns:
            Path to saved file if successful, None if failed
        """
        try:
            # Fetch social map from API
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.base_url}/social-map/{pool_name}")
                response.raise_for_status()
                
                data = response.json()
                
                # Extract social map data
                social_map = data.get("social_map")
                if not social_map:
                    bt.logging.error(f"No social map data in response for pool '{pool_name}'")
                    return None
                
                # Validate structure
                if 'accounts' not in social_map:
                    bt.logging.error(f"Social map missing 'accounts' field for pool '{pool_name}'")
                    return None
                
                # Determine save directory
                if save_dir is None:
                    # Auto-detect: bitcast/validator/social_discovery/social_maps/{pool_name}/
                    save_dir = Path(__file__).parents[1] / "social_discovery" / "social_maps" / pool_name
                
                # Ensure directory exists
                save_dir.mkdir(parents=True, exist_ok=True)
                
                # Generate filename with timestamp and _downloaded suffix
                timestamp = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
                filename = f"{timestamp}_downloaded.json"
                filepath = save_dir / filename
                
                # Save social map
                with open(filepath, 'w') as f:
                    json.dump(social_map, f, indent=2)
                
                bt.logging.info(
                    f"✅ Downloaded social map for '{pool_name}' "
                    f"({data.get('total_accounts', 0)} accounts) -> {filepath.name}"
                )
                
                return str(filepath)
                
        except httpx.TimeoutException:
            bt.logging.warning(
                f"⚠️ Timeout connecting to reference validator at {self.base_url} "
                f"while downloading social map for '{pool_name}'"
            )
            return None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                bt.logging.info(
                    f"📭 Social map for pool '{pool_name}' not found on reference validator. "
                    f"Pool may not exist or social discovery has not been run."
                )
            else:
                bt.logging.warning(
                    f"⚠️ HTTP error {e.response.status_code} downloading social map for '{pool_name}'"
                )
            return None
        except json.JSONDecodeError as e:
            bt.logging.error(f"❌ Invalid JSON received from API for pool '{pool_name}': {e}")
            return None
        except OSError as e:
            bt.logging.error(f"❌ File system error saving social map for '{pool_name}': {e}")
            return None
        except Exception as e:
            bt.logging.error(f"❌ Unexpected error downloading social map for '{pool_name}': {e}")
            return None

