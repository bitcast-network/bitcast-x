"""Brief fetching and filtering utilities for reward engine."""

import requests
import bittensor as bt
from datetime import datetime, timezone
from diskcache import Cache
import os
from threading import Lock
import atexit
from bitcast.validator.utils.config import (
    BITCAST_BRIEFS_ENDPOINT,
    CACHE_DIRS,
    EMISSIONS_PERIOD,
    REWARDS_DELAY_DAYS
)
from bitcast.validator.utils.error_handling import log_and_raise_api_error


class BriefsCache:
    """Thread-safe cache for campaign briefs."""
    
    _instance = None
    _lock = Lock()
    _cache: Cache = None
    _cache_dir = CACHE_DIRS["briefs"]

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
            # Register cleanup on program exit
            atexit.register(cls.cleanup)

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


# Initialize cache
BriefsCache.initialize_cache()


def assign_brief_states(briefs: list) -> list:
    """
    Assign state to each brief based on lifecycle position.
    
    States:
    - 'scoring': A to C (start_date to end_date + REWARDS_DELAY_DAYS inclusive)
      Ensures REWARDS_DELAY_DAYS full days after brief ends before emission starts
    - 'emission': D to E (end_date + REWARDS_DELAY_DAYS + 1 to end_date + REWARDS_DELAY_DAYS + EMISSIONS_PERIOD)
    
    Example with REWARDS_DELAY_DAYS=2:
      Brief ends Nov 12 → Scoring includes Nov 13, Nov 14 (2 full days) → Emission starts Nov 15
    
    Returns:
        Active briefs with 'state' field added (expired briefs excluded)
    """
    from datetime import timedelta
    
    current_date = datetime.now(timezone.utc).date()
    active_briefs = []
    
    for brief in briefs:
        start_date = datetime.strptime(brief['start_date'], "%Y-%m-%d").date()
        end_date = datetime.strptime(brief['end_date'], "%Y-%m-%d").date()
        
        # Scoring phase: A to C (from start through wait period - inclusive)
        if start_date <= current_date <= end_date + timedelta(days=REWARDS_DELAY_DAYS):
            brief['state'] = 'scoring'
            active_briefs.append(brief)
        # Emission phase: D to E (after wait period through emissions)
        elif end_date + timedelta(days=REWARDS_DELAY_DAYS + 1) <= current_date <= end_date + timedelta(days=REWARDS_DELAY_DAYS + EMISSIONS_PERIOD):
            brief['state'] = 'emission'
            active_briefs.append(brief)
        # else: not started yet or expired
    
    scoring_count = sum(1 for b in active_briefs if b['state'] == 'scoring')
    emission_count = len(active_briefs) - scoring_count
    bt.logging.info(f"Brief states: {scoring_count} scoring, {emission_count} emission")
    
    return active_briefs


def get_briefs():
    """
    Fetch all briefs from the server and assign states.
    
    Returns:
        List of brief dictionaries with 'state' field added
        
    Raises:
        RuntimeError: If API fails and no cached data available
    """
    cache = BriefsCache.get_cache()
    cache_key = "briefs"
    
    try:
        # Always try to fetch from API first
        response = requests.get(BITCAST_BRIEFS_ENDPOINT)
        response.raise_for_status()
        briefs_data = response.json()
        
        # Handle both "items" and "briefs" keys in the response
        briefs_list = briefs_data.get("items") or []
        bt.logging.info(f"Fetched {len(briefs_list)} briefs from API.")

        # Store the successful API response in cache
        cache.set(cache_key, briefs_list)
        
        # Assign states before returning
        return assign_brief_states(briefs_list)

    except requests.exceptions.RequestException as e:
        # Try to return cached data if available
        cached_briefs = cache.get(cache_key)
        if cached_briefs is not None:
            bt.logging.warning("Using cached briefs due to API error")
            return assign_brief_states(cached_briefs)
        
        # No cached data available - this is a real error
        log_and_raise_api_error(
            error=e,
            endpoint=BITCAST_BRIEFS_ENDPOINT,
            context="Content briefs fetch"
        )

