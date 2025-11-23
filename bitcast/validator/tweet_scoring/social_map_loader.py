"""
Social map loading utilities for tweet scoring.

Provides functions to load social maps and extract member information.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import bittensor as bt


def _parse_social_map_timestamp(filename: str) -> Optional[datetime]:
    """
    Parse timestamp from social map filename.
    
    Expected format: YYYY.MM.DD_HH.MM.SS.json or YYYY.MM.DD_HH.MM.SS_downloaded.json
    
    Args:
        filename: The filename (not full path)
        
    Returns:
        Timezone-aware datetime in UTC, or None if parsing fails
    """
    try:
        # Remove .json and _downloaded suffix
        name = filename.replace('.json', '').replace('_downloaded', '')
        
        # Parse timestamp: YYYY.MM.DD_HH.MM.SS
        dt = datetime.strptime(name, "%Y.%m.%d_%H.%M.%S")
        return dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


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
    
    # Get latest file by filename timestamp
    latest_file = max(
        social_map_files,
        key=lambda f: _parse_social_map_timestamp(f.name) or datetime.min.replace(tzinfo=timezone.utc)
    )
    
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


def get_relegated_members(social_map: Dict) -> List[str]:
    """
    Extract relegated members from social map.
    
    Args:
        social_map: Social map data dictionary
        
    Returns:
        Sorted list of relegated member usernames
    """
    accounts = social_map.get('accounts', {})
    
    relegated_members = [
        username for username, data in accounts.items()
        if data.get('status') == 'relegated'
    ]
    
    relegated_members.sort()
    
    bt.logging.debug(f"Extracted {len(relegated_members)} relegated members")
    
    return relegated_members


def _map_was_updated_during_period(
    pool_name: str,
    start_date: datetime,
    end_date: datetime
) -> bool:
    """
    Check if any social maps were created during the period.
    
    Args:
        pool_name: Name of the pool
        start_date: Period start date
        end_date: Period end date
        
    Returns:
        True if any maps created in (start_date, end_date], False otherwise
    """
    social_maps_dir = Path(__file__).parents[1] / "social_discovery" / "social_maps" / pool_name
    
    if not social_maps_dir.exists():
        return False
    
    map_files = [
        f for f in social_maps_dir.glob("*.json")
        if not f.name.endswith(('_adjacency.json', '_metadata.json'))
        and not f.name.startswith('recursive_summary_')
    ]
    
    # Check if any maps created during period (exclusive start, inclusive end)
    for f in map_files:
        map_timestamp = _parse_social_map_timestamp(f.name)
        if map_timestamp and start_date < map_timestamp <= end_date:
            return True
    
    return False


def get_active_members_for_period(
    pool_name: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None
) -> List[str]:
    """
    Get active member usernames for a period.
    
    If brief spans a social map update, includes relegated members
    (they were active when they posted, before being relegated).
    
    Args:
        pool_name: Name of the pool
        start_date: Optional brief start date
        end_date: Optional brief end date
        
    Returns:
        Sorted list of active (and possibly relegated) member usernames
    """
    # Load latest map only
    social_map, _ = load_latest_social_map(pool_name)
    
    # Get active members (in + promoted)
    active = get_active_members(social_map)
    
    # If no date range, just return active
    if not start_date or not end_date:
        return active
    
    # If map was updated during brief, include relegated
    # (they were active when they posted, before the update)
    if _map_was_updated_during_period(pool_name, start_date, end_date):
        relegated = get_relegated_members(social_map)
        combined = sorted(set(active + relegated))
        bt.logging.info(
            f"Map updated during brief: including {len(relegated)} relegated members "
            f"({len(active)} active + {len(relegated)} relegated = {len(combined)} total)"
        )
        return combined
    
    return active

