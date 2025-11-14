"""
Utilities for loading scored tweets from disk.

Loads the most recent scored tweets file for a given brief_id,
searching across all pool directories.
"""

import json
from pathlib import Path
from typing import Dict, List, Tuple
import bittensor as bt


def load_latest_scored_tweets(brief_id: str) -> Tuple[Dict, str]:
    """
    Load the most recent scored tweets file for a given brief_id.
    
    Searches across all pool directories to find scored tweet files
    matching the brief_id pattern, then returns the most recent one.
    
    Args:
        brief_id: Brief identifier (e.g., '001_bitcast', 'my_brief_123')
        
    Returns:
        Tuple of (scored_tweets_data, file_path) where:
        - scored_tweets_data: Dict with 'metadata' and 'scored_tweets' keys
        - file_path: Absolute path to the loaded file
        
    Raises:
        FileNotFoundError: If no scored tweets files exist for the brief_id
        ValueError: If the loaded file has invalid structure
    """
    # Get the scored_tweets directory
    scored_tweets_dir = Path(__file__).parent.parent / "tweet_scoring" / "scored_tweets"
    
    if not scored_tweets_dir.exists():
        raise FileNotFoundError(
            f"Scored tweets directory does not exist: {scored_tweets_dir}\n"
            f"Please run tweet scoring first."
        )
    
    # Search for matching files across all pool subdirectories
    matching_files = []
    pattern = f"{brief_id}_*.json"
    
    # Search in all subdirectories (pool directories)
    for pool_dir in scored_tweets_dir.iterdir():
        if pool_dir.is_dir():
            matching_files.extend(pool_dir.glob(pattern))
    
    if not matching_files:
        raise FileNotFoundError(
            f"No scored tweets found for brief_id '{brief_id}'.\n"
            f"Please run tweet scoring first:\n"
            f"  python -m bitcast.validator.tweet_scoring.tweet_scorer --brief-id {brief_id}"
        )
    
    # Sort by modification time and get the most recent
    latest_file = max(matching_files, key=lambda f: f.stat().st_mtime)
    
    bt.logging.info(f"Loading scored tweets from: {latest_file}")
    
    # Load and validate the file
    try:
        with open(latest_file, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON from {latest_file}: {e}")
    
    # Validate structure
    validate_scored_tweets_structure(data)
    
    return data, str(latest_file)


def load_existing_scored_tweets(brief_id: str, pool_name: str) -> Tuple[Dict, str]:
    """
    Load existing scoring snapshot for a specific brief and pool.
    
    Only searches within the specified pool directory, not across all pools.
    Returns the OLDEST (first) snapshot file as the canonical snapshot.
    Used for snapshot detection during reward cycles.
    
    Args:
        brief_id: Brief identifier (e.g., '001_bitcast', 'my_brief_123')
        pool_name: Pool name to search within (e.g., 'tao', 'bittensor')
        
    Returns:
        Tuple of (scored_tweets_data, file_path) where:
        - scored_tweets_data: Dict with 'metadata' and 'scored_tweets' keys
        - file_path: Absolute path to the loaded file
        
    Raises:
        FileNotFoundError: If no scored tweets exist for this brief+pool combination
        ValueError: If the loaded file has invalid structure
    """
    # Get the pool-specific scored_tweets directory
    scored_tweets_dir = Path(__file__).parent.parent / "tweet_scoring" / "scored_tweets"
    pool_dir = scored_tweets_dir / pool_name
    
    if not pool_dir.exists():
        raise FileNotFoundError(
            f"No scored tweets directory found for pool '{pool_name}' at {pool_dir}"
        )
    
    # Search for matching files in this pool only
    pattern = f"{brief_id}_*.json"
    matching_files = list(pool_dir.glob(pattern))
    
    if not matching_files:
        raise FileNotFoundError(
            f"No scoring snapshot found for brief_id '{brief_id}' in pool '{pool_name}'"
        )
    
    # Use the OLDEST file (first snapshot) as the canonical snapshot
    # This ensures consistency - we always use the snapshot from when 
    # the brief first entered the reward window
    oldest_file = min(matching_files, key=lambda f: f.stat().st_mtime)
    
    bt.logging.debug(f"Loading scoring snapshot from {oldest_file}")
    
    # Load and validate the file
    try:
        with open(oldest_file, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON from {oldest_file}: {e}")
    
    # Validate structure
    validate_scored_tweets_structure(data)
    
    return data, str(oldest_file)


def validate_scored_tweets_structure(data: Dict) -> None:
    """
    Validate that scored tweets data has the required structure.
    
    Args:
        data: Loaded scored tweets data
        
    Raises:
        ValueError: If structure is invalid
    """
    if not isinstance(data, dict):
        raise ValueError("Scored tweets data must be a dictionary")
    
    if 'metadata' not in data:
        raise ValueError("Scored tweets data missing 'metadata' field")
    
    if 'scored_tweets' not in data:
        raise ValueError("Scored tweets data missing 'scored_tweets' field")
    
    metadata = data['metadata']
    required_metadata_fields = ['run_id', 'brief_id', 'created_at']
    
    for field in required_metadata_fields:
        if field not in metadata:
            raise ValueError(f"Metadata missing required field: '{field}'")
    
    if not isinstance(data['scored_tweets'], list):
        raise ValueError("'scored_tweets' must be a list")

