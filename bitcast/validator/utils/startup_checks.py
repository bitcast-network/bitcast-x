"""
Startup checks for validator initialization.

Includes social map auto-download for all validators.
"""
from pathlib import Path
from typing import List
import bittensor as bt

from bitcast.validator.utils.config import REFERENCE_VALIDATOR_ENDPOINT
from bitcast.validator.social_discovery.social_map_client import SocialMapClient
from bitcast.validator.social_discovery.pool_manager import PoolManager


def needs_social_map(pool_dir: Path) -> bool:
    """
    Check if a pool directory needs a social map download.
    
    Args:
        pool_dir: Path to pool's social_maps directory
        
    Returns:
        True if directory is empty or has no valid social maps
    """
    if not pool_dir.exists():
        return True
    
    # Check for valid social map files
    social_map_files = [
        f for f in pool_dir.glob("*.json")
        if not f.name.endswith('_adjacency.json')
        and not f.name.endswith('_metadata.json')
        and not f.name.startswith('recursive_summary_')
    ]
    
    return len(social_map_files) == 0


async def check_and_download_social_maps() -> None:
    """
    Check each pool's social maps and download any that are missing.
    Runs for all validators on startup.
    
    For each active pool, checks if social map exists. If not, downloads from reference validator.
    
    Raises:
        RuntimeError: If download fails and social maps are required
    """
    bt.logging.info("🔍 Checking social maps for all active pools...")
    
    # Get active pools
    pool_manager = PoolManager()
    active_pools = [
        name for name, config in pool_manager.pools.items() 
        if config.get('active', True)
    ]
    
    if not active_pools:
        bt.logging.warning("No active pools found - validator may not function correctly")
        return
    
    # Check which pools need social maps
    pools_needing_maps: List[str] = []
    social_maps_base = Path(__file__).parents[1] / "social_discovery" / "social_maps"
    
    for pool_name in active_pools:
        pool_dir = social_maps_base / pool_name
        if needs_social_map(pool_dir):
            pools_needing_maps.append(pool_name)
            bt.logging.info(f"📭 No social maps found for pool '{pool_name}'")
    
    if not pools_needing_maps:
        bt.logging.info("✅ All pools have social maps - no download needed")
        return
    
    # Download missing social maps
    bt.logging.info(
        f"📥 Downloading social maps for {len(pools_needing_maps)} pool(s): "
        f"{', '.join(pools_needing_maps)}"
    )
    
    client = SocialMapClient()
    failed_downloads: List[str] = []
    
    for pool_name in pools_needing_maps:
        bt.logging.info(f"Downloading social map for '{pool_name}'...")
        result = await client.download_social_map(pool_name)
        
        if result:
            bt.logging.info(f"✅ Downloaded social map for '{pool_name}'")
        else:
            bt.logging.warning(f"⚠️ Failed to download social map for '{pool_name}'")
            failed_downloads.append(pool_name)
    
    # Check if any pools still have NO maps at all (fatal error)
    pools_without_maps: List[str] = []
    for pool_name in failed_downloads:
        pool_dir = social_maps_base / pool_name
        if needs_social_map(pool_dir):
            pools_without_maps.append(pool_name)
    
    # Only exit if pools have NO maps at all
    if pools_without_maps:
        error_msg = (
            f"❌ Validator cannot start - pools with NO social maps: {', '.join(pools_without_maps)}\n"
            f"Download failed and no existing maps found. Please check:\n"
            f"  1. Reference validator API is accessible at {REFERENCE_VALIDATOR_ENDPOINT}\n"
            f"  2. Network connectivity is working\n"
            f"  3. Reference validator has social maps for these pools\n"
            f"  4. Pool names are correct in pool configuration"
        )
        bt.logging.error(error_msg)
        raise RuntimeError(error_msg)
    
    # Log summary
    if failed_downloads:
        bt.logging.warning(
            f"⚠️ Download failed for {len(failed_downloads)} pool(s): {', '.join(failed_downloads)}, "
            f"but validator can continue with existing social maps"
        )
    else:
        bt.logging.info("✅ All required social maps downloaded successfully")

