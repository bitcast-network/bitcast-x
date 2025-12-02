"""
Simple pool configuration manager for pools_config.json.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Any
import bittensor as bt


class PoolManager:
    """Loads and manages pool configurations from pools_config.json."""
    
    def __init__(self, config_path: Optional[str] = None):
        """Initialize with config file path."""
        if config_path is None:
            # pools_config.json is now in the same directory
            config_path = Path(__file__).parent / "pools_config.json"
        
        self.config_path = Path(config_path)
        self.pools = self._load_pools()
    
    def _load_pools(self) -> Dict[str, Dict[str, Any]]:
        """Load pool configurations from JSON."""
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)
            
            pools = {}
            for pool_data in config.get('pools', []):
                name = pool_data.get('name', '').lower()
                pools[name] = {
                    'keywords': [kw.lower() for kw in pool_data.get('keywords', [])],
                    'initial_accounts': [acc.lower() for acc in pool_data.get('initial_accounts', [])],
                    'max_seed_accounts': pool_data.get('max_seed_accounts', 150),  # Default to 150 for seed selection
                    'min_interaction_weight': pool_data.get('min_interaction_weight', 0),  # Default to 0 (no filtering)
                    'min_tweets': pool_data.get('min_tweets', 1),  # Default to 1 (at least one tweet with keywords)
                    'lang': pool_data.get('lang')  # Optional language filter (None if not specified)
                }
            
            bt.logging.info(f"Loaded {len(pools)} pools: {list(pools.keys())}")
            return pools
            
        except Exception as e:
            bt.logging.error(f"Failed to load pools config: {e}")
            raise
    
    def get_pool(self, pool_name: str) -> Optional[Dict[str, Any]]:
        """Get pool configuration."""
        return self.pools.get(pool_name.lower())
    
    def get_pools(self) -> List[str]:
        """Get list of available pool names."""
        return list(self.pools.keys())