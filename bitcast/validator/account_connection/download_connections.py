"""
Command-line tool to download account connections from reference validator.

Usage:
    python -m bitcast.validator.account_connection.download_connections
    python -m bitcast.validator.account_connection.download_connections --pool-name tao
    python -m bitcast.validator.account_connection.download_connections --force
"""
import asyncio
import argparse
import sys
from pathlib import Path
import bittensor as bt
from dotenv import load_dotenv

from bitcast.validator.account_connection.connection_client import ConnectionClient
from bitcast.validator.account_connection import ConnectionDatabase
from bitcast.validator.utils.config import REFERENCE_VALIDATOR_URL


async def download_connections(
    client: ConnectionClient, 
    db: ConnectionDatabase,
    pool_name: str = None,
    force: bool = False
) -> bool:
    """
    Download account connections with optional pool filter.
    
    Args:
        client: ConnectionClient instance
        db: ConnectionDatabase instance
        pool_name: Optional pool name to filter by
        force: If True, download even if database has existing connections
        
    Returns:
        True if successful, False if failed
    """
    # Check if database already has connections (unless force)
    if not force:
        existing_count = db.get_connection_count(pool_name=pool_name)
        if existing_count > 0:
            filter_msg = f" for pool '{pool_name}'" if pool_name else ""
            bt.logging.info(
                f"📦 Database already has {existing_count} connections{filter_msg}. "
                f"Use --force to download anyway."
            )
            return True
    
    # Download connections
    filter_msg = f" for pool '{pool_name}'" if pool_name else ""
    bt.logging.info(f"Downloading account connections{filter_msg}...")
    
    success = await client.download_and_store_connections(pool_name=pool_name)
    
    if success:
        final_count = db.get_connection_count(pool_name=pool_name)
        bt.logging.info(
            f"✅ Downloaded {final_count} account connections{filter_msg}"
        )
        return True
    else:
        bt.logging.error(f"❌ Failed to download account connections{filter_msg}")
        return False


async def main():
    """Main entry point for CLI."""
    try:
        # Load environment variables
        env_path = Path(__file__).parents[1] / '.env'
        if env_path.exists():
            load_dotenv(dotenv_path=env_path)
            print(f"Loaded environment variables from {env_path}")
        
        # Create argument parser
        parser = argparse.ArgumentParser(
            description="Download account connections from reference validator"
        )
        bt.logging.add_args(parser)
        
        parser.add_argument(
            "--pool-name",
            type=str,
            default=None,
            help="Filter connections for specific pool (e.g., 'tao')"
        )
        
        parser.add_argument(
            "--force",
            action="store_true",
            help="Download even if database has existing connections"
        )
        
        parser.add_argument(
            "--server-url",
            type=str,
            default=None,
            help="Override reference validator URL from environment"
        )
        
        # Parse arguments
        config = bt.config(parser)
        bt.logging.set_config(config=config.logging)
        
        # Initialize client
        if config.server_url:
            # Override endpoint with custom URL
            from bitcast.validator.utils import config as config_module
            config_module.REFERENCE_VALIDATOR_URL = config.server_url
            config_module.REFERENCE_VALIDATOR_ENDPOINT = f"{config.server_url}:8094"
            bt.logging.info(f"Using custom reference validator URL: {config.server_url}")
        
        client = ConnectionClient()
        db = ConnectionDatabase()
        bt.logging.info(f"Connected to reference validator at {client.base_url}")
        
        # Download connections
        success = await download_connections(
            client=client,
            db=db,
            pool_name=config.pool_name,
            force=config.force
        )
        
        if not success:
            sys.exit(1)
        
        bt.logging.info("\n✅ Download completed successfully")
        
    except KeyboardInterrupt:
        bt.logging.info("\nDownload cancelled by user")
        sys.exit(1)
    except Exception as e:
        bt.logging.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

