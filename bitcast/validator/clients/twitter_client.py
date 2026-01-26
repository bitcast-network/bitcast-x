"""
Twitter client with switchable API provider support and intelligent caching.

Coordinates between multiple Twitter API providers (Desearch.ai, RapidAPI)
with manual provider selection via configuration.
"""

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
import bittensor as bt

from bitcast.validator.utils.config import (
    TWITTER_API_PROVIDER,
    DESEARCH_API_KEY,
    RAPID_API_KEY,
    INITIAL_FETCH_DAYS,
    INCREMENTAL_FETCH_DAYS,
    MAX_TWEETS_PER_FETCH,
    TWITTER_CACHE_FRESHNESS
)
from bitcast.validator.utils.twitter_cache import (
    get_cached_user_tweets,
    cache_user_tweets,
    get_cached_user_info,
    cache_user_info
)

from .twitter_provider import TwitterProvider
from .desearch_provider import DesearchProvider
from .rapidapi_provider import RapidAPIProvider


class TwitterClient:
    """
    Twitter API client with intelligent caching and pluggable API providers.
    
    Supports manual switching between Desearch.ai and RapidAPI via TWITTER_API_PROVIDER config.
    Handles caching, tweet processing, and delegates API access to configured provider.
    """
    
    def __init__(self, api_key: Optional[str] = None, 
                 max_retries: int = 3, retry_delay: float = 2.0, rate_limit_delay: float = 1.0,
                 posts_only: bool = True, provider: Optional[str] = None):
        """
        Initialize client with API provider selection.
        
        Args:
            api_key: Optional API key (uses env vars if not provided)
            max_retries: Maximum number of API request retries
            retry_delay: Delay in seconds between retries
            rate_limit_delay: Delay in seconds between API calls
            posts_only: If True, use only /posts endpoint (faster)
            provider: Override provider ('desearch' or 'rapidapi')
        """
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.rate_limit_delay = rate_limit_delay
        self.posts_only = posts_only
        
        # Determine which provider to use
        self.provider_name = provider or TWITTER_API_PROVIDER
        
        # Initialize selected provider
        self.provider = self._create_provider(
            self.provider_name, api_key
        )
        
        endpoint_mode = "posts-only" if self.posts_only else "dual-endpoint"
        bt.logging.info(
            f"TwitterClient initialized with {self.provider_name} provider "
            f"({TWITTER_CACHE_FRESHNESS/3600:.1f}h cache, {endpoint_mode} mode)"
        )
    
    def _create_provider(self, provider_name: str, 
                        api_key: Optional[str]) -> TwitterProvider:
        """
        Create and validate a provider instance.
        
        Args:
            provider_name: Provider to create ('desearch' or 'rapidapi')
            api_key: Optional API key (uses env vars if not provided)
            
        Returns:
            Initialized provider instance
            
        Raises:
            ValueError: If provider name unknown or API key invalid
        """
        if provider_name == 'desearch':
            key = api_key or DESEARCH_API_KEY
            if not key:
                raise ValueError("DESEARCH_API_KEY not configured")
            provider = DesearchProvider(
                api_key=key,
                max_retries=self.max_retries,
                retry_delay=self.retry_delay,
                rate_limit_delay=self.rate_limit_delay
            )
            if not provider.validate_api_key():
                raise ValueError(
                    "Invalid DESEARCH_API_KEY format. "
                    "Expected: dt_$YOUR_KEY"
                )
            return provider
            
        elif provider_name == 'rapidapi':
            key = api_key or RAPID_API_KEY
            if not key:
                raise ValueError("RAPID_API_KEY not configured")
            provider = RapidAPIProvider(
                api_key=key,
                max_retries=self.max_retries,
                retry_delay=self.retry_delay,
                rate_limit_delay=self.rate_limit_delay
            )
            if not provider.validate_api_key():
                raise ValueError("Invalid RAPID_API_KEY")
            return provider
            
        else:
            raise ValueError(
                f"Unknown provider: {provider_name}. "
                f"Options: 'desearch', 'rapidapi'"
            )
    
    def _validate_tweet_authors(self, tweets: List[Dict], username: str) -> List[Dict]:
        """
        Filter tweets to only those authored by the specified username.
        
        Args:
            tweets: List of tweet dictionaries
            username: Expected author username (already lowercased)
            
        Returns:
            Filtered list of tweets from the specified author only
        """
        validated_tweets = []
        
        for tweet in tweets:
            author = tweet.get('author', '').lower() if tweet.get('author') else None
            
            if author == username:
                # Tweet is from the expected author
                validated_tweets.append(tweet)
            elif not author:
                # Set author field for tweets missing it (e.g., from cache)
                tweet['author'] = username
                validated_tweets.append(tweet)
            # else: skip - tweet from someone else (e.g., reply TO user FROM someone else)
        
        return validated_tweets
    
    def _post_process_tweets(
        self, 
        tweets: List[Dict], 
        username: str
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Apply consistent filtering, sorting, and deletion removal to tweets from any source.
        
        Processes tweets through validation, sorting, and deletion filtering to ensure 
        consistent output regardless of whether tweets came from fresh cache, stale cache, 
        or API fetch.
        
        IMPORTANT: The cache stores ALL tweets indefinitely without limits.
        The visible_tweets returned to the caller include ALL tweets (only deleted tweets removed).
        Callers apply their own date filtering and limits as needed.
        
        Args:
            tweets: Raw tweet list (from cache or API)
            username: Twitter username for author validation
        
        Returns:
            Tuple of (visible_tweets, all_tweets_for_cache)
            - visible_tweets: ALL tweets (excludes deleted tweets with missing_count >= 2)
            - all_tweets_for_cache: ALL tweets for cache storage (includes deleted)
        """
        all_tweets = tweets.copy()
        
        # 1. Validate author - filter to only tweets from this user
        original_count = len(all_tweets)
        all_tweets = self._validate_tweet_authors(all_tweets, username)
        if len(all_tweets) < original_count:
            filtered_count = original_count - len(all_tweets)
            bt.logging.debug(f"Filtered {filtered_count} tweets from other authors")
        
        # 2. Sort by date (most recent first)
        def get_tweet_date(tweet):
            try:
                if tweet.get('created_at'):
                    return datetime.strptime(tweet['created_at'], '%a %b %d %H:%M:%S %z %Y')
                return datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)
            except ValueError:
                return datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)
        
        all_tweets.sort(key=get_tweet_date, reverse=True)
        
        # Cache path: Store ALL tweets (no date cutoff, no count limit)
        # Note: all_tweets is already a copy from line 175, safe to reference directly
        tweets_to_cache = all_tweets
        
        # Visible tweets path: Return ALL tweets, only filter deleted ones
        # Callers apply their own date filtering and limits as needed
        # Filter deleted tweets (missing_count >= 2) from visible tweets only
        visible_tweets = [t for t in all_tweets if t.get('missing_count', 0) < 2]
        deleted_count = len(all_tweets) - len(visible_tweets)
        if deleted_count > 0:
            bt.logging.debug(f"Filtered {deleted_count} deleted tweets from returned tweets (kept in cache)")
        
        bt.logging.debug(f"Cache will store {len(tweets_to_cache)} tweets (including {deleted_count} deleted), returning {len(visible_tweets)} tweets")
        
        return visible_tweets, tweets_to_cache
    
    def fetch_user_tweets(self, username: str, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Fetch tweets for a user with intelligent caching and incremental updates.
        
        Three fetch strategies:
        - Initial (no cache): Bootstrap with 30 days of history
        - Incremental (stale cache): Refresh last 4 days only
        - Force refresh: Thorough 30-day update (merges with cache)
        
        Args:
            username: Twitter username to fetch tweets for
            force_refresh: If True, fetch 30 days (thorough refresh, merges with cache)
        
        Filters to only tweets authored by the user (the tweetsandreplies API returns
        both user's tweets AND replies from others).
        
        Fetches up to MAX_TWEETS_PER_FETCH tweets from API when cache is stale.
        Returns ALL cached tweets regardless of age - callers apply their own date filtering.
        Uses smart cache merging to preserve historical tweets while fetching recent updates.
        
        Returns dict with 'user_info', 'tweets', and 'cache_info'
        """
        username = username.lower()
        
        # Check cache and determine fetch strategy
        cached_data = get_cached_user_tweets(username)
        last_updated = cached_data.get('last_updated') if cached_data else None
        
        # Determine fetch strategy: initial / incremental / force / fresh
        if not cached_data:
            # INITIAL FETCH: No cache exists - bootstrap with 30 days
            fetch_days = INITIAL_FETCH_DAYS
            base_time = datetime.now()
            bt.logging.info(f"Initial fetch for @{username} (last {fetch_days} days)")
            
        elif force_refresh:
            # FORCE REFRESH: Thorough update - fetch 30 days (merges with cache)
            fetch_days = INITIAL_FETCH_DAYS
            base_time = last_updated
            bt.logging.info(f"Force refresh for @{username} (fetching last {fetch_days} days)")
            
        elif last_updated and (datetime.now() - last_updated).total_seconds() < TWITTER_CACHE_FRESHNESS:
            # FRESH CACHE: Use cached data without fetching
            bt.logging.debug(f"Using cached tweets for @{username} ({len(cached_data['tweets'])} tweets)")
            
            # Apply post-processing: sorting, validation, and deletion removal
            visible_tweets, tweets_to_cache = self._post_process_tweets(
                tweets=cached_data['tweets'],
                username=username
            )
            
            # Update cache with post-processed tweets while preserving original timestamp
            cache_data = {
                'user_info': cached_data['user_info'],
                'tweets': tweets_to_cache,
                'last_updated': last_updated  # Keep original to maintain freshness window
            }
            cache_user_tweets(username, cache_data)
            
            return {
                'user_info': cached_data['user_info'],
                'tweets': visible_tweets,
                'cache_info': {
                    'cache_hit': True, 
                    'new_tweets': 0,
                    'cached_tweets': len(cached_data['tweets']),
                    'provider_used': 'cache'
                }
            }
        else:
            # INCREMENTAL UPDATE: Cache is stale - refresh last 4 days
            fetch_days = INCREMENTAL_FETCH_DAYS
            base_time = last_updated
            bt.logging.info(f"Incremental update for @{username} (last {fetch_days} days)")
        
        # Calculate cutoff for API pagination stopping point
        incremental_cutoff = base_time - timedelta(days=fetch_days)
        
        # API fetch limit (used for pagination)
        # When using dual-endpoint mode, fetch 200 per endpoint (400 total)
        # When using posts-only mode, fetch 400 from single endpoint
        if self.posts_only:
            tweet_limit = MAX_TWEETS_PER_FETCH  # 400 for single endpoint
        else:
            tweet_limit = 200  # 200 per endpoint in dual-endpoint mode
        
        # Fetch from provider
        try:
            tweets, user_info, api_fetch_succeeded = self.provider.fetch_user_tweets(
                username=username,
                incremental_cutoff=incremental_cutoff,
                tweet_limit=tweet_limit,
                posts_only=self.posts_only
            )
            
            if not api_fetch_succeeded:
                bt.logging.warning(
                    f"Provider ({self.provider_name}) returned no tweets for @{username}"
                )
                tweets = []
                user_info = None
                
        except Exception as e:
            bt.logging.error(
                f"Provider ({self.provider_name}) failed for @{username}: {e}"
            )
            tweets = []
            user_info = None
            api_fetch_succeeded = False
        
        # Reset missing counter for newly fetched tweets
        for tweet in tweets:
            tweet['missing_count'] = 0
        
        # Smart merge: combine new tweets with cached tweets, track deleted tweets
        all_tweets = tweets.copy()
        cached_count = 0
        incremented_missing = 0
        
        if cached_data and cached_data.get('tweets'):
            new_tweet_ids = {t['tweet_id'] for t in tweets if t.get('tweet_id')}
            
            for cached_tweet in cached_data['tweets']:
                tweet_id = cached_tweet.get('tweet_id')
                if not tweet_id or tweet_id in new_tweet_ids:
                    continue
                
                # Increment missing counter if API succeeded and tweet within fetch window
                if api_fetch_succeeded and cached_tweet.get('created_at'):
                    try:
                        tweet_date = datetime.strptime(cached_tweet['created_at'], '%a %b %d %H:%M:%S %z %Y')
                        cutoff_with_tz = incremental_cutoff.replace(tzinfo=tweet_date.tzinfo)
                        
                        if tweet_date >= cutoff_with_tz:
                            cached_tweet['missing_count'] = cached_tweet.get('missing_count', 0) + 1
                            incremented_missing += 1
                    except (ValueError, AttributeError):
                        pass
                
                all_tweets.append(cached_tweet)
                cached_count += 1
            
            bt.logging.debug(f"Merged: {len(tweets)} new + {cached_count} cached = {len(all_tweets)} total" + 
                           (f", {incremented_missing} missing++" if incremented_missing > 0 else ""))
        
        # Apply post-processing: sorting, validation, and deletion removal
        visible_tweets, tweets_to_cache = self._post_process_tweets(
            tweets=all_tweets,
            username=username
        )
        
        # Cache all tweets including deleted ones
        cache_data = {
            'user_info': (cached_data.get('user_info') if cached_data else None) or user_info or {'username': username, 'followers_count': 0},
            'tweets': tweets_to_cache,
            'last_updated': datetime.now()
        }
        cache_user_tweets(username, cache_data)
        
        return {
            'user_info': cache_data['user_info'],
            'tweets': visible_tweets,
            'cache_info': {
                'cache_hit': bool(cached_data), 
                'new_tweets': len(tweets), 
                'cached_tweets': cached_count,
                'provider_used': self.provider_name
            }
        }
    
    def check_user_relevance(self, username: str, keywords: List[str], min_followers: int = 0, lang: Optional[str] = None, min_tweets: int = 1) -> bool:
        """Check if user tweets about keywords and meets follower threshold.
        
        Args:
            username: Twitter username to check
            keywords: List of keywords to search for
            min_followers: Minimum follower count threshold
            lang: Optional language filter (e.g., 'en', 'zh'). If specified, user must have at least 
                  one tweet in this language, but keywords are checked across all tweets.
            min_tweets: Minimum number of tweets containing keywords for user to be considered relevant
        
        Returns:
            True if user is relevant (meets all criteria), False otherwise
        """
        result = self.fetch_user_tweets(username)
        
        if not result['tweets']:
            return False
        
        # Check followers
        followers = result['user_info'].get('followers_count', 0)
        if followers < min_followers:
            return False
        
        # Check language requirement if specified
        if lang is not None:
            total_tweets = len(result['tweets'])
            lang_tweets = [t for t in result['tweets'] if t.get('lang') == lang]
            lang_match_count = len(lang_tweets)
            
            bt.logging.info(f"@{username}: {lang_match_count}/{total_tweets} tweets match lang='{lang}'")
            
            if not lang_tweets:
                return False  # No tweets in target language
        
        # Count tweets with keywords across ALL tweets (regardless of language)
        keywords_lower = [kw.lower() for kw in keywords]
        tweets_with_keywords = 0
        
        for tweet in result['tweets']:
            text_lower = tweet['text'].lower()
            
            # Check if tweet contains any keyword
            has_keyword = any(
                kw in text_lower if kw.startswith(('#', '$'))
                else bool(re.search(r'\b' + re.escape(kw) + r'\b', text_lower))
                for kw in keywords_lower
            )
            
            if has_keyword:
                tweets_with_keywords += 1
                if tweets_with_keywords >= min_tweets:
                    return True
        
        return False
