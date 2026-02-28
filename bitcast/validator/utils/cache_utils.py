import os
import shutil
import bittensor as bt
from diskcache import Cache
from bitcast.validator.utils.config import CACHE_DIRS, CACHE_ROOT
from bitcast.validator.reward_engine.utils import BriefsCache
from bitcast.validator.utils.twitter_cache import DiscoveryCache

def clear_all_caches():
    """Clear all cache directories and instances."""
    bt.logging.info("Clearing all caches")
    try:
        clear_briefs_cache()
        clear_twitter_cache()
        
        # Clear all cache directories
        for cache_dir in CACHE_DIRS.values():
            if os.path.exists(cache_dir):
                bt.logging.debug(f"Clearing cache directory: {cache_dir}")
                shutil.rmtree(cache_dir)
                os.makedirs(cache_dir)
        bt.logging.info("Successfully cleared all caches")
    except Exception as e:
        bt.logging.error(f"Error clearing all caches: {str(e)}")
        raise

def clear_expired_caches():
    """Clear expired entries from all caches."""
    bt.logging.info("Clearing expired cache entries")
    try:
        clear_expired_briefs_cache()
        clear_expired_twitter_cache()
        bt.logging.info("Successfully cleared expired cache entries")
    except Exception as e:
        bt.logging.error(f"Error clearing expired cache entries: {str(e)}")
        raise

def clear_briefs_cache():
    """Clear Briefs cache."""
    bt.logging.info("Clearing Briefs cache")
    try:
        if BriefsCache._cache:
            BriefsCache._cache.clear()
            bt.logging.info("Successfully cleared Briefs cache")
        else:
            bt.logging.warning("Briefs cache not initialized")
    except Exception as e:
        bt.logging.error(f"Error clearing Briefs cache: {str(e)}")
        raise


def clear_expired_briefs_cache():
    """Clear expired Briefs cache entries."""
    bt.logging.info("Clearing expired Briefs cache entries")
    try:
        if BriefsCache._cache:
            BriefsCache._cache.expire()
            bt.logging.info("Successfully cleared expired Briefs cache entries")
        else:
            bt.logging.warning("Briefs cache not initialized")
    except Exception as e:
        bt.logging.error(f"Error clearing expired Briefs cache entries: {str(e)}")
        raise


def clear_twitter_cache():
    """Clear discovery cache (social discovery user timeline cache)."""
    bt.logging.info("Clearing discovery cache")
    try:
        if DiscoveryCache._cache:
            DiscoveryCache._cache.clear()
            bt.logging.info("Successfully cleared discovery cache")
        else:
            bt.logging.warning("Discovery cache not initialized")
    except Exception as e:
        bt.logging.error(f"Error clearing discovery cache: {str(e)}")
        raise

def clear_expired_twitter_cache():
    """Clear expired discovery cache entries."""
    bt.logging.info("Clearing expired discovery cache entries")
    try:
        if DiscoveryCache._cache:
            DiscoveryCache._cache.expire()
            bt.logging.info("Successfully cleared expired discovery cache entries")
        else:
            bt.logging.warning("Discovery cache not initialized")
    except Exception as e:
        bt.logging.error(f"Error clearing expired discovery cache entries: {str(e)}")
        raise