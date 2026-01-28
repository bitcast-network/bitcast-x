"""
Periodic social map downloader for standard mode.
Downloads social maps from reference validator when they become stale.
"""
import bittensor as bt
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from bitcast.validator.social_discovery.social_map_client import SocialMapClient
from bitcast.validator.social_discovery.pool_manager import PoolManager
from bitcast.validator.social_discovery.social_map_loader import get_latest_social_map_path


def is_social_map_stale(pool_dir: Path, max_age_hours: int = 24) -> bool:
    """
    Check if a pool's social map is stale (older than max_age_hours).
    
    Args:
        pool_dir: Directory containing social maps for a pool
        max_age_hours: Maximum age in hours before map is considered stale
        
    Returns:
        True if social map is stale or doesn't exist, False otherwise
    """
    if not pool_dir.exists():
        return True
    
    # Get latest social map
    latest_map = get_latest_social_map_path(pool_dir)
    if not latest_map:
        return True
    
    # Check age
    try:
        map_mtime = datetime.fromtimestamp(latest_map.stat().st_mtime, tz=timezone.utc)
        age = datetime.now(timezone.utc) - map_mtime
        is_stale = age > timedelta(hours=max_age_hours)
        
        if is_stale:
            bt.logging.debug(
                f"Social map for {pool_dir.name} is stale: "
                f"{age.total_seconds()/3600:.1f} hours old"
            )
        
        return is_stale
    except Exception as e:
        bt.logging.warning(f"Error checking social map age for {pool_dir.name}: {e}")
        return True


async def download_stale_social_maps(max_age_hours: int = 24) -> List[str]:
    """
    Download social maps for all active pools that are stale.
    
    Args:
        max_age_hours: Maximum age in hours before map is considered stale
        
    Returns:
        List of pool names for which social maps were downloaded
    """
    # Get active pools
    pool_manager = PoolManager()
    active_pools = [
        name for name, config in pool_manager.pools.items() 
        if config.get('active', True)
    ]
    
    if not active_pools:
        bt.logging.warning("No active pools found")
        return []
    
    # Check which pools need fresh maps
    pools_needing_maps: List[str] = []
    social_maps_base = Path(__file__).parents[0] / "social_maps"
    
    for pool_name in active_pools:
        pool_dir = social_maps_base / pool_name
        if is_social_map_stale(pool_dir, max_age_hours):
            pools_needing_maps.append(pool_name)
    
    if not pools_needing_maps:
        bt.logging.debug("All social maps are fresh")
        return []
    
    # Download fresh social maps
    bt.logging.info(
        f"📥 Downloading fresh social maps for {len(pools_needing_maps)} pool(s): "
        f"{', '.join(pools_needing_maps)}"
    )
    
    client = SocialMapClient()
    downloaded_pools: List[str] = []
    
    for pool_name in pools_needing_maps:
        bt.logging.info(f"Downloading social map for '{pool_name}'...")
        result = await client.download_social_map(pool_name)
        
        if result:
            bt.logging.info(f"✅ Downloaded fresh social map for '{pool_name}'")
            downloaded_pools.append(pool_name)
        else:
            bt.logging.warning(
                f"⚠️ Failed to download social map for '{pool_name}' - "
                f"will continue with existing map"
            )
    
    return downloaded_pools
