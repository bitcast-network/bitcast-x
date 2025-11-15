"""
Command-line tool to download social maps from reference validator.

Usage:
    python -m bitcast.validator.social_discovery.download_social_map --pool-name tao
    python -m bitcast.validator.social_discovery.download_social_map --all-pools
"""
import asyncio
import argparse
import sys
from pathlib import Path
import bittensor as bt
from dotenv import load_dotenv

from bitcast.validator.social_discovery.social_map_client import SocialMapClient
from bitcast.validator.social_discovery.pool_manager import PoolManager
from bitcast.validator.utils.config import REFERENCE_VALIDATOR_URL


async def download_pool_map(client: SocialMapClient, pool_name: str) -> bool:
    """
    Download social map for a single pool.
    
    Args:
        client: SocialMapClient instance
        pool_name: Name of pool to download
        
    Returns:
        True if successful, False if failed
    """
    bt.logging.info(f"Downloading social map for pool '{pool_name}'...")
    result = await client.download_social_map(pool_name)
    
    if result:
        bt.logging.info(f"✅ Successfully downloaded social map for '{pool_name}'")
        return True
    else:
        bt.logging.error(f"❌ Failed to download social map for '{pool_name}'")
        return False


async def download_all_pools(client: SocialMapClient) -> dict:
    """
    Download social maps for all active pools.
    
    Args:
        client: SocialMapClient instance
        
    Returns:
        Dict with pool names as keys and success status as values
    """
    pool_manager = PoolManager()
    active_pools = [
        name for name, config in pool_manager.pools.items() 
        if config.get('active', True)
    ]
    
    if not active_pools:
        bt.logging.warning("No active pools found in pool configuration")
        return {}
    
    bt.logging.info(f"Found {len(active_pools)} active pools: {', '.join(active_pools)}")
    
    results = {}
    for pool_name in active_pools:
        success = await download_pool_map(client, pool_name)
        results[pool_name] = success
    
    return results


async def main():
    """Main entry point for CLI."""
    try:
        # Load environment variables
        env_path = Path(__file__).parents[2] / '.env'
        if env_path.exists():
            load_dotenv(dotenv_path=env_path)
            print(f"Loaded environment variables from {env_path}")
        
        # Create argument parser
        parser = argparse.ArgumentParser(
            description="Download social maps from reference validator"
        )
        bt.logging.add_args(parser)
        
        parser.add_argument(
            "--pool-name",
            type=str,
            default=None,
            help="Specific pool to download map for (e.g., 'tao')"
        )
        
        parser.add_argument(
            "--all-pools",
            action="store_true",
            help="Download maps for all active pools"
        )
        
        parser.add_argument(
            "--server-url",
            type=str,
            default=None,
            help=f"Override reference validator URL (default: {REFERENCE_VALIDATOR_URL})"
        )
        
        # Parse arguments
        args_list = sys.argv[1:]
        
        # Add info logging if no logging level specified
        if not any(arg.startswith('--logging.') for arg in args_list):
            args_list.insert(0, '--logging.info')
        
        config = bt.config(parser, args=args_list)
        bt.logging.set_config(config=config.logging)
        
        # Validate arguments
        if not config.pool_name and not config.all_pools:
            bt.logging.error("Error: Must specify either --pool-name or --all-pools")
            parser.print_help()
            sys.exit(1)
        
        if config.pool_name and config.all_pools:
            bt.logging.error("Error: Cannot specify both --pool-name and --all-pools")
            sys.exit(1)
        
        # Initialize client
        if config.server_url:
            # Override endpoint with custom URL
            from bitcast.validator.utils import config as config_module
            config_module.REFERENCE_VALIDATOR_URL = config.server_url
            config_module.REFERENCE_VALIDATOR_ENDPOINT = f"{config.server_url}:8094"
            bt.logging.info(f"Using custom reference validator URL: {config.server_url}")
        
        client = SocialMapClient()
        bt.logging.info(f"Connected to reference validator at {client.base_url}")
        
        # Download social maps
        if config.all_pools:
            bt.logging.info("Downloading social maps for all active pools...")
            results = await download_all_pools(client)
            
            # Print summary
            success_count = sum(1 for success in results.values() if success)
            total_count = len(results)
            
            bt.logging.info(f"\n{'='*60}")
            bt.logging.info(f"Download Summary: {success_count}/{total_count} succeeded")
            bt.logging.info(f"{'='*60}")
            
            for pool_name, success in results.items():
                status = "✅ SUCCESS" if success else "❌ FAILED"
                bt.logging.info(f"  {pool_name}: {status}")
            
            # Exit with error if any failed
            if success_count < total_count:
                sys.exit(1)
        else:
            bt.logging.info(f"Downloading social map for pool '{config.pool_name}'...")
            success = await download_pool_map(client, config.pool_name)
            
            if not success:
                sys.exit(1)
        
        bt.logging.info("\n✅ All downloads completed successfully")
        
    except KeyboardInterrupt:
        bt.logging.info("\nDownload cancelled by user")
        sys.exit(1)
    except Exception as e:
        bt.logging.error(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

