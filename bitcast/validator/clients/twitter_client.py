"""
Twitter client with switchable API provider support and caching.

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
    SOCIAL_DISCOVERY_FETCH_DAYS,
    MAX_TWEETS_PER_FETCH,
    CACHE_FRESHNESS_SECONDS,
)
from bitcast.validator.utils.twitter_cache import (
    get_cached_user_tweets,
    cache_user_tweets,
)
from bitcast.validator.utils.twitter_validators import is_valid_twitter_username

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
            f"({endpoint_mode} mode)"
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
            
            # Log multi-key setup
            key_count = provider.get_key_count()
            if key_count > 1:
                bt.logging.info(f"RapidAPI configured with {key_count} API keys for load balancing")
            
            return provider
            
        else:
            raise ValueError(
                f"Unknown provider: {provider_name}. "
                f"Options: 'desearch', 'rapidapi'"
            )
    
    def _validate_tweet_authors(self, tweets: List[Dict], username: str) -> List[Dict]:
        """
        Filter tweets to only those authored by the specified username.
        
        Strict validation: Rejects tweets with missing or mismatched author.
        Providers should always set the author field during parsing.
        
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
                # Strict validation: reject tweets with missing author
                # This indicates incomplete data from provider
                bt.logging.warning(
                    f"Rejecting tweet {tweet.get('tweet_id')} with missing author field "
                    f"(expected @{username})"
                )
            else:
                # Tweet from someone else (e.g., reply TO user FROM someone else)
                bt.logging.debug(
                    f"Filtering tweet {tweet.get('tweet_id')} from @{author} "
                    f"(expected @{username})"
                )
        
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
    
    def fetch_user_tweets(
        self,
        username: str,
        fetch_days: int = SOCIAL_DISCOVERY_FETCH_DAYS,
        skip_if_cache_fresh: bool = False,
    ) -> Dict[str, Any]:
        """
        Fetch tweets for a user, pulling fresh data and merging with cache.

        By default, always fetches from the API. When skip_if_cache_fresh=True,
        checks if cache was updated within 24 hours and skips API call if so.

        Args:
            username: Twitter username to fetch tweets for
            fetch_days: Number of days of tweet history to fetch from API
            skip_if_cache_fresh: If True, skip API call if cache was updated within freshness window

        Returns:
            Dict with 'user_info', 'tweets', and 'cache_info'
        """
        username = username.lower()
        
        # Validate username format - reject numeric IDs from suspended/deleted accounts
        if not is_valid_twitter_username(username):
            bt.logging.debug(
                f"Skipping invalid username: @{username} "
                f"(likely a numeric user ID from suspended/deleted account)"
            )
            return {
                'user_info': {'username': username, 'followers_count': 0},
                'tweets': [],
                'cache_info': {
                    'cache_hit': False,
                    'cache_fresh': False,
                    'cache_age_hours': 0,
                    'new_tweets': 0,
                    'cached_tweets': 0,
                    'provider_used': 'none'
                }
            }

        # Check cache for existing data
        cached_data = get_cached_user_tweets(username)

        # Check if cache is fresh and we should skip API call
        if skip_if_cache_fresh and cached_data:
            freshness_seconds = CACHE_FRESHNESS_SECONDS
            cache_timestamp = cached_data.get('cache_timestamp')

            if cache_timestamp:
                try:
                    cache_time = datetime.fromisoformat(cache_timestamp)
                    age_seconds = (datetime.now() - cache_time).total_seconds()

                    if age_seconds < freshness_seconds:
                        bt.logging.info(f"Cache fresh for @{username} ({age_seconds/3600:.1f}h old), skipping API call")
                        # Return cached data with cache hit info
                        visible_tweets, _ = self._post_process_tweets(
                            tweets=cached_data.get('tweets', []),
                            username=username
                        )
                        return {
                            'user_info': cached_data.get('user_info', {'username': username, 'followers_count': 0}),
                            'tweets': visible_tweets,
                            'cache_info': {
                                'cache_hit': True,
                                'cache_fresh': True,
                                'cache_age_hours': round(age_seconds / 3600, 1),
                                'new_tweets': 0,
                                'cached_tweets': len(cached_data.get('tweets', [])),
                                'provider_used': 'cache'
                            }
                        }
                except (ValueError, TypeError):
                    pass  # Fall through to API fetch if timestamp parsing fails

        # Use cache timestamp as cutoff when available (fetch only newer tweets)
        incremental_cutoff = None
        if cached_data:
            cache_timestamp = cached_data.get('cache_timestamp')
            if cache_timestamp:
                try:
                    cache_time = datetime.fromisoformat(cache_timestamp)
                    incremental_cutoff = cache_time - timedelta(hours=1)
                    bt.logging.info(
                        f"Fetching tweets for @{username} since cache "
                        f"({(datetime.now() - cache_time).total_seconds() / 3600:.1f}h ago)"
                    )
                except (ValueError, TypeError):
                    pass

        if incremental_cutoff is None:
            incremental_cutoff = datetime.now() - timedelta(days=fetch_days)
            bt.logging.info(f"Fetching tweets for @{username} (last {fetch_days} days)")
        
        # API fetch limit (used for pagination)
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
        
        # Merge new tweets with cached tweets, track deleted tweets
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
        
        # Smart merge of user_info
        final_user_info = {'username': username, 'followers_count': 0}
        
        if user_info and user_info.get('followers_count', 0) > 0:
            final_user_info = {**user_info, 'username': username}
        elif cached_data and cached_data.get('user_info'):
            cached_user_info = cached_data['user_info']
            final_user_info = {
                'username': username,
                'followers_count': cached_user_info.get('followers_count', 0)
            }
        
        # Only update cache timestamp if API fetch succeeded AND we have tweets to cache.
        # If the API returned 0 tweets with no prior cached tweets, the result is likely
        # a rate-limit artifact (empty timeline response after 429 retry) - skip the
        # timestamp update so the next run retries rather than treating it as fresh.
        if api_fetch_succeeded and tweets_to_cache:
            cache_data = {
                'user_info': final_user_info,
                'tweets': tweets_to_cache,
                'last_updated': datetime.now()
            }
            cache_user_tweets(username, cache_data)
        elif api_fetch_succeeded and not tweets_to_cache:
            bt.logging.debug(f"API succeeded but 0 tweets for @{username}, skipping cache timestamp update to allow retry")
            cache_data = cached_data or {}
        elif not cached_data:
            # No cached data and API failed - store with current timestamp (no other option)
            cache_data = {
                'user_info': final_user_info,
                'tweets': tweets_to_cache,
                'last_updated': datetime.now()
            }
            cache_user_tweets(username, cache_data)
        else:
            # API failed but we have cached data - preserve original timestamps
            # so next run will retry the API call
            bt.logging.debug(f"API failed for @{username}, preserving original cache timestamp for retry")
            cache_data = cached_data
        
        return {
            'user_info': final_user_info,
            'tweets': visible_tweets,
            'cache_info': {
                'cache_hit': bool(cached_data), 
                'new_tweets': len(tweets), 
                'cached_tweets': cached_count,
                'provider_used': self.provider_name
            }
        }
    
    def check_user_relevance(self, username: str, keywords: List[str], min_followers: int = 0, lang: Optional[str] = None, min_tweets: int = 1, skip_if_cache_fresh: bool = False) -> bool:
        """Check if user tweets about keywords and meets follower threshold.
        
        Args:
            username: Twitter username to check
            keywords: List of keywords to search for
            min_followers: Minimum follower count threshold
            lang: Optional language filter (e.g., 'en', 'zh'). If specified, user must have at least 
                  one tweet in this language, but keywords are checked across all tweets.
            min_tweets: Minimum number of tweets containing keywords for user to be considered relevant
            skip_if_cache_fresh: If True, skip API call if cache was updated within freshness window
        
        Returns:
            True if user is relevant (meets all criteria), False otherwise
        """
        result = self.fetch_user_tweets(username, skip_if_cache_fresh=skip_if_cache_fresh)
        
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
            
            # Check if tweet contains any keyword with exact matching
            # Hashtags/cashtags: use trailing word boundary only (# and $ are non-word chars)
            # Regular keywords: use word boundaries on both ends
            has_keyword = any(
                bool(re.search(re.escape(kw) + r'\b', text_lower)) if kw.startswith(('#', '$'))
                else bool(re.search(r'\b' + re.escape(kw) + r'\b', text_lower))
                for kw in keywords_lower
            )
            
            if has_keyword:
                tweets_with_keywords += 1
                if tweets_with_keywords >= min_tweets:
                    return True
        
        return False
    
    def search_tweets(self, query: str, max_results: int = 100, sort: str = "latest") -> Dict[str, Any]:
        """
        Search for tweets using X-style query syntax.
        
        Supports standard Twitter search operators:
        - Keywords: "bitcoin"
        - Hashtags: "#bitcast"
        - Mentions: "@username"
        - Quoted tweet: "quoted_tweet_id:123456"
        - Date filters: "since:2026-01-01 until:2026-01-15"
        
        Args:
            query: Search query string with X-style operators
            max_results: Maximum number of tweets to return (default: 100)
            sort: Sort order - "latest" or "top" (default: "latest")
        
        Returns:
            Dict with 'tweets' list and 'api_succeeded' boolean
        """
        try:
            tweets, api_succeeded = self.provider.search_tweets(
                query=query,
                max_results=max_results,
                sort=sort
            )
            
            return {
                'tweets': tweets,
                'api_succeeded': api_succeeded,
                'provider_used': self.provider_name
            }
            
        except Exception as e:
            bt.logging.error(f"Search failed for query '{query[:50]}...': {e}")
            return {
                'tweets': [],
                'api_succeeded': False,
                'provider_used': self.provider_name
            }
    
    def get_retweeters(self, tweet_id: str, max_results: int = 100) -> Dict[str, Any]:
        """
        Get list of usernames who retweeted a specific tweet.
        
        Args:
            tweet_id: The tweet ID to get retweeters for
            max_results: Maximum number of retweeters to return (default: 100)
        
        Returns:
            Dict with 'retweeters' list (usernames) and 'api_succeeded' boolean
        """
        try:
            usernames, api_succeeded = self.provider.get_retweeters(
                tweet_id=tweet_id,
                max_results=max_results
            )
            
            return {
                'retweeters': usernames,
                'api_succeeded': api_succeeded,
                'provider_used': self.provider_name
            }
            
        except Exception as e:
            bt.logging.error(f"Get retweeters failed for tweet {tweet_id}: {e}")
            return {
                'retweeters': [],
                'api_succeeded': False,
                'provider_used': self.provider_name
            }
