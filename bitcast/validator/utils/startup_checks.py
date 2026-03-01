"""
Startup checks for validator initialization.

Includes social map auto-download for weight_copy and standard modes.
Validators in discovery mode generate their own social maps.
"""
from pathlib import Path
from typing import List
import bittensor as bt

from bitcast.validator.utils.config import REFERENCE_VALIDATOR_ENDPOINT, VALIDATOR_MODE
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
    
    Mode behavior:
    - weight_copy: Skip check (doesn't perform validation, no need for social maps)
    - standard: Download social maps from reference validator if missing
    - discovery: Download if missing (for quick start), then generates fresh ones via social discovery
    
    This ensures anyone can start in any mode from zero and get to a working state.
    
    Raises:
        RuntimeError: If download fails and social maps are required
    """
    # Weight copy mode doesn't perform validation - skip download
    if VALIDATOR_MODE == 'weight_copy':
        bt.logging.info("🔄 Weight copy mode - fetches weights only, skipping social map check")
        return
    
    bt.logging.info(f"🔍 Checking social maps for all active pools (mode: {VALIDATOR_MODE})...")
    
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


async def check_and_download_account_connections() -> None:
    """
    Check if account connections database is empty and download if needed.
    
    Mode behavior:
    - weight_copy: Download connections (doesn't run scanner)
    - standard & discovery: Download if empty, scanner will run periodically
    
    Only downloads if database is completely empty (fresh deployment).
    If database has any connections, assumes validator is operational.
    
    Does NOT raise exceptions - validator can function without connections.
    Connection scanner will run periodically and populate database.
    """
    bt.logging.info(f"🔍 Checking account connections database (mode: {VALIDATOR_MODE})...")
    
    try:
        # Import here to avoid circular dependencies
        from bitcast.validator.account_connection import ConnectionDatabase
        from bitcast.validator.account_connection.connection_client import ConnectionClient
        
        # Check if database has any connections
        db = ConnectionDatabase()
        connection_count = db.get_connection_count()
        
        if connection_count > 0:
            bt.logging.info(
                f"✅ Found {connection_count} existing connections - "
                f"no download needed"
            )
            return
        
        # Database is empty - try to download
        bt.logging.info(
            "📭 No connections found in database - downloading from reference validator"
        )
        
        client = ConnectionClient()
        success = await client.download_and_store_connections()
        
        if success:
            final_count = db.get_connection_count()
            bt.logging.info(
                f"✅ Downloaded {final_count} account connections successfully"
            )
        else:
            # Download failed - check if we can continue
            final_count = db.get_connection_count()
            if final_count == 0:
                error_msg = (
                    f"⚠️ Failed to download account connections and database is empty.\n"
                    f"Validator will continue but will not be able to reward miners until "
                    f"connection scanner runs.\n"
                    f"Please check:\n"
                    f"  1. Reference validator API is accessible at {REFERENCE_VALIDATOR_ENDPOINT}\n"
                    f"  2. Network connectivity is working\n"
                    f"  3. Reference validator has account connection data available"
                )
                bt.logging.warning(error_msg)
                # Note: Do NOT raise exception - validator can function without connections
                # Connection scanner will run periodically and populate database
            else:
                bt.logging.info(
                    f"⚠️ Download failed but found {final_count} connections - continuing"
                )
    except Exception as e:
        bt.logging.warning(
            f"⚠️ Error checking/downloading account connections: {e}\n"
            f"Validator will continue - connection scanner will populate database"
        )

