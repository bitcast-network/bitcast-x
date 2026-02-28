"""
Simple pool configuration manager that fetches from API.
"""

from typing import Dict, List, Optional, Any
import requests
import bittensor as bt
from bitcast.validator.utils.config import POOLS_API_URL


class PoolManager:
    """Loads and manages pool configurations from API."""
    
    def __init__(self, api_url: Optional[str] = None):
        """
        Initialize with API URL.
        
        Args:
            api_url: Optional API endpoint URL. Defaults to POOLS_API_URL from config.
        """
        self.api_url = api_url or POOLS_API_URL
        self.pools = self._load_pools()
    
    def _load_pools(self) -> Dict[str, Dict[str, Any]]:
        """Load pool configurations from API (only active pools)."""
        try:
            bt.logging.info(f"Fetching pools from API: {self.api_url}")
            response = requests.get(self.api_url, timeout=10)
            response.raise_for_status()
            config = response.json()
            
            pools = {}
            for pool_data in config.get('pools', []):
                # Only load pools where active is explicitly True
                if not pool_data.get('active', False):
                    continue
                
                name = pool_data.get('name', '').lower()
                pools[name] = {
                    'keywords': [kw.lower() for kw in pool_data.get('keywords', [])],
                    'initial_accounts': [acc.lower() for acc in pool_data.get('initial_accounts', [])],
                    'lang': pool_data.get('lang'),
                    'date_offset': pool_data.get('date_offset', 0),
                    'active': True,
                    # Two-stage discovery config
                    'core_min_interaction_weight': pool_data.get('core_min_interaction_weight', 2),
                    'core_min_tweets': pool_data.get('core_min_tweets', 5),
                    'core_max_seed_accounts': pool_data.get('core_max_seed_accounts', 100),
                    'extended_min_interaction_weight': pool_data.get('extended_min_interaction_weight', 1),
                    'extended_min_tweets': pool_data.get('extended_min_tweets', 1),
                    'extended_max_seed_accounts': pool_data.get('extended_max_seed_accounts', 300),
                    'max_discovery_iterations': pool_data.get('max_discovery_iterations', 3),
                    'convergence_threshold': pool_data.get('convergence_threshold', 0.90),
                }
            
            bt.logging.info(f"Loaded {len(pools)} active pools from API: {list(pools.keys())}")
            return pools
            
        except Exception as e:
            bt.logging.error(f"Failed to load pools from API: {e}")
            raise
    
    def get_pool(self, pool_name: str) -> Optional[Dict[str, Any]]:
        """Get pool configuration."""
        return self.pools.get(pool_name.lower())
    
    def get_pools(self) -> List[str]:
        """Get list of available pool names."""
        return list(self.pools.keys())