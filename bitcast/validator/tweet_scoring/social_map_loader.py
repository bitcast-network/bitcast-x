"""
Social map loading utilities for tweet scoring.

Provides functions to load social maps and extract member information.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import bittensor as bt


def load_latest_social_map(pool_name: str) -> Tuple[Dict, str]:
    """
    Load the latest social map for a pool.
    
    Args:
        pool_name: Name of the pool
        
    Returns:
        Tuple of (social_map_data, file_path)
        
    Raises:
        FileNotFoundError: If no social map exists for the pool
        ValueError: If social map data is invalid
    """
    # Locate social maps directory
    social_maps_dir = Path(__file__).parents[1] / "social_discovery" / "social_maps" / pool_name
    
    if not social_maps_dir.exists():
        raise FileNotFoundError(
            f"No social map directory found for pool '{pool_name}' at {social_maps_dir}"
        )
    
    # Find social map files (exclude adjacency, metadata, and recursive summary files)
    social_map_files = [
        f for f in social_maps_dir.glob("*.json")
        if not f.name.endswith('_adjacency.json')
        and not f.name.endswith('_metadata.json')
        and not f.name.startswith('recursive_summary_')
    ]
    
    if not social_map_files:
        raise FileNotFoundError(
            f"No social map files found for pool '{pool_name}' in {social_maps_dir}"
        )
    
    # Get latest file by modification time
    latest_file = max(social_map_files, key=lambda f: f.stat().st_mtime)
    
    # Load and validate
    try:
        with open(latest_file, 'r') as f:
            social_map = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in social map file {latest_file}: {e}")
    
    # Validate structure
    if 'accounts' not in social_map:
        raise ValueError(f"Social map missing 'accounts' field: {latest_file}")
    
    bt.logging.info(f"Loaded social map from {latest_file}")
    
    return social_map, str(latest_file)


def get_active_members(social_map: Dict) -> List[str]:
    """
    Extract active members from social map.
    
    Active members have status 'in' or 'promoted'.
    
    Args:
        social_map: Social map data dictionary
        
    Returns:
        Sorted list of active member usernames
    """
    accounts = social_map.get('accounts', {})
    
    active_members = [
        username for username, data in accounts.items()
        if data.get('status') in ['in', 'promoted']
    ]
    
    active_members.sort()
    
    bt.logging.info(f"Extracted {len(active_members)} active members")
    
    return active_members


def get_considered_accounts(social_map: Dict, limit: int) -> List[Tuple[str, float]]:
    """
    Get top N accounts by score for engagement consideration.
    
    Args:
        social_map: Social map data dictionary
        limit: Number of top accounts to return
        
    Returns:
        List of (username, score) tuples sorted by score descending
    """
    accounts = social_map.get('accounts', {})
    
    # Get all accounts with scores
    account_scores = [
        (username, data.get('score', 0.0))
        for username, data in accounts.items()
    ]
    
    # Sort by score descending and take top N
    account_scores.sort(key=lambda x: x[1], reverse=True)
    considered = account_scores[:limit]
    
    bt.logging.info(
        f"Selected top {len(considered)} considered accounts "
        f"(requested: {limit}, available: {len(account_scores)})"
    )
    
    return considered

