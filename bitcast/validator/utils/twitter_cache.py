"""
Caching utilities for social discovery and Twitter API data.

DiscoveryCache: Cache for social discovery user timeline fetches (90-day expiry)
- Used by social discovery to cache account timelines for network building
- Keys: user_tweets_{username}, user_info_{username}

For tweet scoring, use ScoringStore (accumulative, no expiry) instead.
"""

import os
import atexit
from datetime import datetime
from threading import Lock
from typing import Any, Dict, Optional
from diskcache import Cache
import bittensor as bt

from bitcast.validator.utils.config import CACHE_DIRS, DISCOVERY_CACHE_EXPIRY


class DiscoveryCache:
    """
    Thread-safe singleton cache for social discovery user timeline data.
    
    Stores account timelines fetched during social discovery with a 90-day expiry.
    Tweet scoring uses ScoringStore (accumulative, no expiry) instead.
    """
    
    _instance = None
    _lock = Lock()
    _cache: Cache = None
    _cache_dir = CACHE_DIRS["twitter"]

    @classmethod
    def initialize_cache(cls) -> None:
        """Initialize the cache if it hasn't been initialized yet."""
        if cls._cache is None:
            os.makedirs(cls._cache_dir, exist_ok=True)
            cls._cache = Cache(
                directory=cls._cache_dir,
                size_limit=1e9,  # 1GB
                disk_min_file_size=0,
                disk_pickle_protocol=4,
            )
            atexit.register(cls.cleanup)
            bt.logging.info(f"DiscoveryCache initialized at: {cls._cache_dir}")

    @classmethod
    def cleanup(cls) -> None:
        """Clean up resources."""
        if cls._cache is not None:
            with cls._lock:
                if cls._cache is not None:
                    cls._cache.close()
                    cls._cache = None

    @classmethod
    def get_cache(cls) -> Cache:
        """Thread-safe cache access."""
        if cls._cache is None:
            cls.initialize_cache()
        return cls._cache

    def __del__(self):
        """Ensure cleanup on object destruction."""
        self.cleanup()


def get_user_tweets_cache_key(username: str) -> str:
    """Generate cache key for user tweets."""
    return f"user_tweets_{username.lower()}"


def get_user_info_cache_key(username: str) -> str:
    """Generate cache key for user information."""
    return f"user_info_{username.lower()}"


def cache_user_tweets(username: str, data: Dict[str, Any]) -> None:
    """
    Cache user tweets data with expiry.
    
    Args:
        username: Twitter username
        data: Tweet data to cache
    """
    cache = DiscoveryCache.get_cache()
    cache_key = get_user_tweets_cache_key(username)
    
    data_with_timestamp = {
        **data,
        'last_updated': data.get('last_updated', datetime.now()),
        'cache_timestamp': datetime.now().isoformat()
    }
    
    cache.set(cache_key, data_with_timestamp, expire=DISCOVERY_CACHE_EXPIRY)
    bt.logging.debug(f"Cached tweets for @{username} (expires in {DISCOVERY_CACHE_EXPIRY}s)")


def get_cached_user_tweets(username: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve cached user tweets data.
    
    Args:
        username: Twitter username
        
    Returns:
        Cached data if available, None otherwise
    """
    cache = DiscoveryCache.get_cache()
    cache_key = get_user_tweets_cache_key(username)
    
    cached_data = cache.get(cache_key)
    if cached_data:
        bt.logging.debug(f"Cache hit for @{username}")
        return cached_data
    
    bt.logging.debug(f"Cache miss for @{username}")
    return None


def cache_user_info(username: str, user_info: Dict[str, Any]) -> None:
    """
    Cache user information with expiry.
    
    Args:
        username: Twitter username
        user_info: User information to cache
    """
    cache = DiscoveryCache.get_cache()
    cache_key = get_user_info_cache_key(username)
    
    info_with_timestamp = {
        **user_info,
        'cache_timestamp': datetime.now().isoformat()
    }
    
    cache.set(cache_key, info_with_timestamp, expire=DISCOVERY_CACHE_EXPIRY)
    bt.logging.debug(f"Cached user info for @{username}")


def get_cached_user_info(username: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve cached user information.
    
    Args:
        username: Twitter username
        
    Returns:
        Cached user info if available, None otherwise
    """
    cache = DiscoveryCache.get_cache()
    cache_key = get_user_info_cache_key(username)
    
    cached_info = cache.get(cache_key)
    if cached_info:
        bt.logging.debug(f"Cache hit for user info @{username}")
        return cached_info
    
    bt.logging.debug(f"Cache miss for user info @{username}")
    return None


def clear_empty_tweet_caches() -> Dict[str, int]:
    """
    Remove all cached entries that have no tweets.
    
    Useful for cleaning up cache entries that resulted from API errors
    or accounts that legitimately have no tweets.
    
    Returns:
        Dictionary with statistics:
        - 'checked': Number of cache entries checked
        - 'removed': Number of empty entries removed
        - 'preserved': Number of entries with tweets preserved
    """
    cache = DiscoveryCache.get_cache()
    
    stats = {
        'checked': 0,
        'removed': 0,
        'preserved': 0
    }
    
    for key in list(cache.iterkeys()):
        if not key.startswith('user_tweets_'):
            continue
        
        stats['checked'] += 1
        
        try:
            cached_data = cache.get(key)
            
            if cached_data and not cached_data.get('tweets'):
                username = key.replace('user_tweets_', '')
                cache.delete(key)
                stats['removed'] += 1
                bt.logging.info(f"Removed empty cache entry for @{username}")
            else:
                stats['preserved'] += 1
                
        except Exception as e:
            bt.logging.warning(f"Error processing cache key {key}: {e}")
    
    bt.logging.info(
        f"Cache cleanup complete: {stats['checked']} checked, "
        f"{stats['removed']} removed, {stats['preserved']} preserved"
    )
    
    return stats


# Initialize cache
DiscoveryCache.initialize_cache()
