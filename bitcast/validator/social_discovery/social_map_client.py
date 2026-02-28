"""
Client for downloading social maps from reference validator.
Handles API communication, file saving, content deduplication, and error handling.
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
    
    def _get_latest_social_map_path(self, pool_dir: Path) -> Optional[Path]:
        """
        Get path to latest social map file in pool directory.
        
        Args:
            pool_dir: Directory containing social maps for a pool
            
        Returns:
            Path to latest social map file, or None if no files exist
        """
        if not pool_dir.exists():
            return None
        
        # Find social map files (exclude adjacency, metadata, and recursive summary files)
        social_map_files = [
            f for f in pool_dir.glob("*.json")
            if not f.name.endswith('_adjacency.json')
            and not f.name.endswith('_metadata.json')
            and not f.name.startswith('recursive_summary_')
        ]
        
        if not social_map_files:
            return None
        
        # Get latest by filename (timestamp-based)
        latest_file = max(social_map_files, key=lambda f: f.name)
        return latest_file
    
    def _is_content_identical(self, new_map: dict, existing_file: Path) -> bool:
        """
        Check if new social map content is identical to existing file.
        
        Args:
            new_map: New social map data to compare
            existing_file: Path to existing social map file
            
        Returns:
            True if content is identical, False otherwise
        """
        try:
            with open(existing_file, 'r') as f:
                existing_map = json.load(f)
            
            # Compare accounts data (the core content)
            new_accounts = new_map.get('accounts', {})
            existing_accounts = existing_map.get('accounts', {})
            
            # Quick check: same number of accounts
            if len(new_accounts) != len(existing_accounts):
                return False
            
            # Deep comparison: check all accounts and scores
            if new_accounts != existing_accounts:
                return False
            
            # Check metadata if present (optional, but good to compare)
            new_metadata = new_map.get('metadata', {})
            existing_metadata = existing_map.get('metadata', {})
            
            # Compare key metadata fields (ignore timestamps)
            metadata_keys = ['total_accounts', 'pool_difficulty', 'total_followers']
            for key in metadata_keys:
                if new_metadata.get(key) != existing_metadata.get(key):
                    return False
            
            return True
            
        except Exception as e:
            bt.logging.warning(f"Error comparing social map content: {e}")
            return False
    
    async def download_social_map(
        self, 
        pool_name: str, 
        save_dir: Optional[Path] = None
    ) -> Optional[str]:
        """
        Download and save social map for a pool with content deduplication.
        
        If the downloaded content is identical to the existing latest social map,
        skips saving a duplicate file and returns the existing file path.
        
        Args:
            pool_name: Name of pool to download map for
            save_dir: Optional directory to save to (default: auto-detect)
            
        Returns:
            Path to saved/existing file if successful, None if failed
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
                
                # Check for content deduplication
                existing_file = self._get_latest_social_map_path(save_dir)
                if existing_file and self._is_content_identical(social_map, existing_file):
                    bt.logging.info(
                        f"📋 Social map for '{pool_name}' is identical to existing file {existing_file.name} "
                        f"({data.get('total_accounts', 0)} accounts) - skipping duplicate save"
                    )
                    return str(existing_file)
                
                # Content is different or no existing file - save new map
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

