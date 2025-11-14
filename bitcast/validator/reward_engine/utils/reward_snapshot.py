"""
Utilities for saving and loading reward snapshots.

Reward snapshots freeze the total USD allocation per UID at the first emission run,
ensuring stable daily payouts over the 7-day emission period.
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Tuple
import bittensor as bt


def save_reward_snapshot(brief_id: str, pool_name: str, snapshot_data: Dict) -> str:
    """
    Save reward snapshot for a brief to ensure stable payouts.
    
    Args:
        brief_id: Brief identifier
        pool_name: Pool name (used for directory organization)
        snapshot_data: Dict with keys: brief_id, pool_name, created_at, tweet_rewards
            tweet_rewards: List of dicts with tweet_id, author, uid, score, total_usd
        
    Returns:
        Path to saved snapshot file
    """
    # Create output directory (in reward_engine root, not utils)
    snapshot_dir = Path(__file__).parent.parent / "reward_snapshots" / pool_name
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate timestamp
    timestamp_str = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    
    # Save snapshot
    output_file = snapshot_dir / f"{brief_id}_{timestamp_str}.json"
    
    with open(output_file, 'w') as f:
        json.dump(snapshot_data, f, indent=2)
    
    bt.logging.debug(f"Saved reward snapshot to {output_file}")
    
    return str(output_file)


def load_reward_snapshot(brief_id: str, pool_name: str) -> Tuple[Dict, str]:
    """
    Load reward snapshot for a brief.
    
    Returns the OLDEST snapshot file (first emission run) as the canonical snapshot.
    
    Args:
        brief_id: Brief identifier
        pool_name: Pool name
        
    Returns:
        Tuple of (snapshot_data, file_path)
        
    Raises:
        FileNotFoundError: If no snapshot exists for this brief
    """
    snapshot_dir = Path(__file__).parent.parent / "reward_snapshots" / pool_name
    
    if not snapshot_dir.exists():
        raise FileNotFoundError(
            f"Reward snapshot directory does not exist for pool '{pool_name}'"
        )
    
    # Search for matching files
    pattern = f"{brief_id}_*.json"
    matching_files = list(snapshot_dir.glob(pattern))
    
    if not matching_files:
        raise FileNotFoundError(
            f"No reward snapshot found for brief '{brief_id}' in pool '{pool_name}'"
        )
    
    # Get OLDEST file (first emission run = canonical snapshot)
    oldest_file = min(matching_files, key=lambda f: f.stat().st_mtime)
    
    bt.logging.debug(f"Loading reward snapshot from: {oldest_file}")
    
    # Load snapshot
    with open(oldest_file, 'r') as f:
        data = json.load(f)
    
    return data, str(oldest_file)

