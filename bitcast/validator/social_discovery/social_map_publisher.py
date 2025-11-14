"""
Social Map Publisher - Handles publishing social map data to external endpoints.

This module can be run standalone to republish social map data or imported
by the social discovery system for integrated publishing.
"""

import asyncio
import json
import os
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import numpy as np
import bittensor as bt
from dotenv import load_dotenv

# Initialize environment BEFORE importing custom modules when running standalone
if __name__ == "__main__":
    # Load environment variables from .env file
    env_path = Path(__file__).parents[1] / '.env'  # bitcast/validator/.env
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        print(f"Loaded environment variables from {env_path}")

# Now import custom modules that use bt.logging
from bitcast.validator.utils.config import (
    X_SOCIAL_MAP_ENDPOINT,
    WALLET_NAME,
    HOTKEY_NAME
)
from bitcast.validator.utils.data_publisher import get_global_publisher, initialize_global_publisher


async def publish_social_map(
    pool_name: str,
    social_map_data: Dict,
    adjacency_matrix: np.ndarray,
    usernames: List[str],
    run_id: str
) -> bool:
    """
    Publish social map data using unified API format.
    
    Args:
        pool_name: Name of the social discovery pool
        social_map_data: Social map with metadata and status
        adjacency_matrix: Network adjacency matrix
        usernames: Sorted list of usernames
        run_id: Validation cycle identifier
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        bt.logging.info(f"🗺️ Publishing social map for pool '{pool_name}' to {X_SOCIAL_MAP_ENDPOINT}")
        
        # Create payload structure
        payload_data = {
            "pool_name": pool_name,
            "metadata": social_map_data['metadata'],
            "accounts": social_map_data['accounts'],
            "adjacency_matrix": adjacency_matrix.tolist(),
            "usernames": usernames,
            "timestamp": datetime.now().isoformat()
        }
        
        # Use global publisher
        publisher = get_global_publisher()
        success = await publisher.publish_unified_payload(
            payload_type="x_social_map",
            run_id=run_id,
            payload_data=payload_data,
            endpoint=X_SOCIAL_MAP_ENDPOINT
        )
        
        if success:
            bt.logging.info(f"✅ Social map published for pool '{pool_name}' and run {run_id}")
        else:
            bt.logging.warning(f"⚠️ Social map publishing failed for pool '{pool_name}' and run {run_id}")
            
        return success
            
    except Exception as e:
        bt.logging.error(f"Critical social map publishing error: {e}")
        return False


async def republish_latest_social_map(
    pool_name: str,
    run_id: Optional[str] = None
) -> bool:
    """
    Re-publish the latest social map data for a pool.
    
    Args:
        pool_name: Pool to re-publish data for
        run_id: New run ID (auto-generated if not provided)
        
    Returns:
        bool: Success status
    """
    try:
        # Find latest metadata file
        social_maps_dir = Path(__file__).parent / "social_maps" / pool_name
        if not social_maps_dir.exists():
            bt.logging.error(f"No social maps directory found for pool '{pool_name}'")
            return False
        
        metadata_files = list(social_maps_dir.glob("*_metadata.json"))
        if not metadata_files:
            bt.logging.error(f"No metadata files found for pool '{pool_name}'")
            return False
        
        # Get latest metadata file
        latest_metadata_file = max(metadata_files, key=lambda f: f.stat().st_mtime)
        timestamp_prefix = latest_metadata_file.stem.replace('_metadata', '')
        
        bt.logging.info(f"Found latest social map data: {timestamp_prefix}")
        
        # Load metadata to get original run_id
        with open(latest_metadata_file, 'r') as f:
            metadata = json.load(f)
        
        # Load social map
        social_map_file = social_maps_dir / f"{timestamp_prefix}.json"
        with open(social_map_file, 'r') as f:
            social_map_data = json.load(f)
        
        # Load adjacency matrix
        matrix_file = social_maps_dir / f"{timestamp_prefix}_adjacency.json"
        with open(matrix_file, 'r') as f:
            matrix_data = json.load(f)
        
        adjacency_matrix = np.array(matrix_data['adjacency_matrix'])
        usernames = matrix_data['usernames']
        
        # Use original run_id from metadata if not provided
        if run_id is None:
            run_id = metadata.get('run_id')
            if not run_id:
                # Fallback: generate new run_id if metadata doesn't have one
                publisher = get_global_publisher()
                vali_hotkey = publisher.wallet.hotkey.ss58_address
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                run_id = f"vali_x_{vali_hotkey}_{timestamp}"
        
        bt.logging.info(f"Re-publishing social map for pool '{pool_name}' with run_id: {run_id}")
        
        # Re-publish using existing function
        return await publish_social_map(
            pool_name=pool_name,
            social_map_data=social_map_data,
            adjacency_matrix=adjacency_matrix,
            usernames=usernames,
            run_id=run_id
        )
        
    except Exception as e:
        bt.logging.error(f"Failed to republish social map for pool '{pool_name}': {e}")
        return False


def main():
    """Main entry point for standalone execution."""
    try:
        # Create argument parser with all options
        parser = argparse.ArgumentParser(
            description="Republish the latest social map data for a pool"
        )
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)
        bt.subtensor.add_args(parser)
        
        parser.add_argument(
            "--pool-name",
            type=str,
            default="tao",
            help="Name of the pool to republish (default: tao)"
        )
        
        # Build args list from environment variables for wallet config
        args_list = ['--logging.debug']  # Enable debug logging by default
        if WALLET_NAME:
            args_list.extend(['--wallet.name', WALLET_NAME])
        if HOTKEY_NAME:
            args_list.extend(['--wallet.hotkey', HOTKEY_NAME])
        
        # Parse configuration with env-based wallet args
        config = bt.config(parser, args=args_list)
        bt.logging.set_config(config=config.logging)
        
        # Initialize global publisher with properly configured wallet
        wallet = bt.wallet(config=config)
        initialize_global_publisher(wallet)
        bt.logging.info("🌐 Global publisher initialized for standalone mode")
        
        # Run republish (run_id auto-generated in republish function)
        success = asyncio.run(republish_latest_social_map(
            pool_name=config.pool_name,
            run_id=None
        ))
        
        if success:
            print(f"✅ Successfully republished social map for pool '{config.pool_name}'")
        else:
            print(f"❌ Failed to republish social map for pool '{config.pool_name}'")
            exit(1)
            
    except Exception as e:
        print(f"❌ Error: {e}")
        exit(1)


if __name__ == "__main__":
    main()


