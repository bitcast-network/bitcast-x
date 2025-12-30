"""
Social map loading utilities for tweet scoring.

Provides functions to load social maps and extract member information.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import bittensor as bt


def parse_social_map_filename(filename: str) -> Optional[datetime]:
    """
    Parse timestamp from social map filename.
    
    Handles formats:
    - 2025.11.23_03.44.25.json
    - 2025.11.23_03.44.25_downloaded.json
    - 2025.11.23_03.44.25_adjacency.json
    
    Args:
        filename: Social map filename
        
    Returns:
        Datetime in UTC, or None if parsing fails
    """
    try:
        # Remove extensions and suffixes
        base = filename.replace('_downloaded', '').replace('_adjacency', '').replace('_metadata', '').replace('.json', '')
        
        # Parse timestamp: 2025.11.23_03.44.25
        return datetime.strptime(base, "%Y.%m.%d_%H.%M.%S").replace(tzinfo=timezone.utc)
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
        key=lambda f: parse_social_map_filename(f.name) or datetime.min.replace(tzinfo=timezone.utc)
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


def get_active_members(
    social_map: Dict, 
    limit: Optional[int] = None
) -> List[str]:
    """
    Get top-ranked accounts from social map sorted by score.
    
    Args:
        social_map: Social map data dictionary
        limit: Optional limit on number of members (for brief-specific restrictions)
               If None, returns all accounts
        
    Returns:
        List of usernames, sorted by score (highest to lowest)
    """
    accounts = social_map.get('accounts', {})
    
    if not accounts:
        bt.logging.info("No accounts in social map")
        return []
    
    # Get all accounts with scores
    account_scores = [
        (username, data.get('score', 0.0))
        for username, data in accounts.items()
    ]
    
    # Sort by score descending, then username ascending for consistent ordering
    account_scores.sort(key=lambda x: (-x[1], x[0]))
    
    # Apply limit if specified
    if limit is not None:
        account_scores = account_scores[:limit]
    
    members = [username for username, _ in account_scores]
    
    bt.logging.info(
        f"Selected {len(members)} accounts" + 
        (f" (limited to top {limit})" if limit else "")
    )
    
    return members


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


def get_active_members_for_brief(
    pool_name: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    max_members: Optional[int] = None
) -> List[str]:
    """
    Get eligible accounts for a brief, considering all social maps active during the brief window.
    
    When a brief spans social map refreshes (every 2 weeks), this function merges
    the top N accounts from each relevant map to ensure accounts that were eligible
    when the brief started remain eligible even if they drop in rank later.
    
    If no date range is provided, returns active members from the latest map only.
    
    Args:
        pool_name: Pool name
        start_date: Brief start date (UTC). If None, uses latest map only.
        end_date: Brief end date (UTC). If None, uses latest map only.
        max_members: Number of top accounts to include from each map (if None, includes all)
        
    Returns:
        List of eligible account usernames, sorted by score in latest map
        
    Example:
        Brief Nov 20-25, max_members=150
        - Map from Nov 9: Takes top 150
        - Map from Nov 23 (mid-brief): Takes top 150
        - Merges to ~160-200 unique accounts (some overlap)
    """
    # If no date range provided, just return active members from latest map
    if start_date is None or end_date is None:
        social_map, _ = load_latest_social_map(pool_name)
        return get_active_members(social_map, limit=max_members)
    
    social_maps_dir = Path(__file__).parents[1] / "social_discovery" / "social_maps" / pool_name
    
    if not social_maps_dir.exists():
        raise FileNotFoundError(f"No social maps found for pool '{pool_name}'")
    
    # Get all social map files
    all_maps = [
        f for f in social_maps_dir.glob("*.json")
        if not f.name.endswith(('_adjacency.json', '_metadata.json'))
        and not f.name.startswith('recursive_summary_')
    ]
    
    if not all_maps:
        raise FileNotFoundError(f"No social maps found for pool '{pool_name}'")
    
    # Parse timestamps and sort chronologically
    maps_with_times = []
    for map_file in all_maps:
        timestamp = parse_social_map_filename(map_file.name)
        if timestamp:
            maps_with_times.append((map_file, timestamp))
    
    maps_with_times.sort(key=lambda x: x[1])
    
    # Find maps that were "active" during the brief window
    # A map is active from its creation until the next map is created
    relevant_maps = []
    
    for i, (map_file, map_time) in enumerate(maps_with_times):
        # Determine when this map stopped being active
        if i + 1 < len(maps_with_times):
            next_map_time = maps_with_times[i + 1][1]
        else:
            next_map_time = datetime.now(timezone.utc)
        
        # Check if this map was active during any part of the brief window
        # Map overlaps brief if: map_time <= brief_end AND next_map_time >= brief_start
        if map_time <= end_date and next_map_time >= start_date:
            relevant_maps.append((map_file, map_time))
    
    if not relevant_maps:
        # Fallback: use latest map
        bt.logging.warning(f"No maps found in brief window, using latest")
        relevant_maps = [maps_with_times[-1]]
    
    bt.logging.info(
        f"Brief {start_date.date()} to {end_date.date()} spans "
        f"{len(relevant_maps)} social map(s)"
    )
    
    # Collect top N from each relevant map
    all_eligible = set()
    
    for map_file, map_time in relevant_maps:
        with open(map_file, 'r') as f:
            social_map = json.load(f)
        
        # Get all accounts sorted by score
        account_scores = [
            (username, data.get('score', 0.0))
            for username, data in social_map['accounts'].items()
        ]
        account_scores.sort(key=lambda x: x[1], reverse=True)
        
        # Take top max_members (or all if not specified)
        if max_members:
            top_from_map = [username for username, _ in account_scores[:max_members]]
        else:
            top_from_map = [username for username, _ in account_scores]
        
        all_eligible.update(top_from_map)
        
        bt.logging.info(
            f"  → {len(top_from_map)} accounts from {map_file.name} "
            f"(created {map_time.date()})"
        )
    
    # Load latest map to get current scores for sorting
    latest_map_file = relevant_maps[-1][0]
    with open(latest_map_file, 'r') as f:
        latest_map = json.load(f)
    
    # Sort merged accounts by their score in latest map
    # Use username as secondary sort key for consistent ordering when scores are equal
    scores_dict = {
        username: data.get('score', 0.0)
        for username, data in latest_map['accounts'].items()
    }
    
    eligible_list = sorted(
        list(all_eligible),
        key=lambda x: (-scores_dict.get(x, 0.0), x)  # Sort by score desc, then username asc
    )
    
    bt.logging.info(
        f"Merged to {len(eligible_list)} unique eligible accounts across all maps"
    )
    
    return eligible_list
