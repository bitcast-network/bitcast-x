"""
Simplified Twitter client with API access and caching for PageRank scoring.

Combines API communication, caching, and basic tweet processing in one module.
"""

import requests
import time
import re
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import bittensor as bt

from bitcast.validator.utils.config import (
    DESEARCH_API_KEY,
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

# Desearch.ai is required for scoring
USE_DESEARCH_API = True

class TwitterClient:
    """
    Twitter API client with intelligent caching and tweet processing.
    
    Handles API communication, caching, and basic tweet processing for
    PageRank-based network analysis.
    """
    
    def __init__(self, api_key: Optional[str] = None, 
                 max_retries: int = 3, retry_delay: float = 2.0, rate_limit_delay: float = 1.0,
                 posts_only: bool = True):
        """Initialize client with Desearch.ai API key (required for scoring)."""
        # Desearch.ai is required for scoring
        self.use_desearch = True
        self.api_key = api_key or DESEARCH_API_KEY
        if not self.api_key:
            raise ValueError("DESEARCH_API_KEY environment variable must be set for scoring")
        
        # Strip any whitespace that might have been introduced
        self.api_key = self.api_key.strip()
        self.base_url = "https://api.desearch.ai"
        # Desearch.ai requires format: dt_$API_KEY
        # The $ is a literal character, not a variable, so we concatenate strings
        # Check if API key already includes the full prefix
        if self.api_key.startswith('dt_$'):
            # Already has full prefix, use as-is
            auth_value = self.api_key
        elif self.api_key.startswith('$'):
            # Has $ but missing dt_ prefix
            auth_value = "dt_" + self.api_key
        else:
            # No prefix, add dt_$
            auth_value = "dt_$" + self.api_key
        
        self.headers = {
            "Authorization": auth_value,
            "Content-Type": "application/json"
        }
        
        # Debug: Log auth header format for troubleshooting (first run only)
        if not hasattr(TwitterClient, '_auth_logged'):
            masked_auth = auth_value[:15] + '...' + auth_value[-5:] if len(auth_value) > 20 else '***'
            bt.logging.info(f"Desearch.ai Authorization header format: {masked_auth} (key length: {len(self.api_key)})")
            TwitterClient._auth_logged = True
        
        # Configuration
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.rate_limit_delay = rate_limit_delay
        self.posts_only = posts_only
        
        endpoint_mode = "posts-only mode" if self.posts_only else "dual-endpoint mode"
        bt.logging.info(f"TwitterClient initialized with centralized cache ({TWITTER_CACHE_FRESHNESS/3600:.1f}h freshness, {endpoint_mode})")
    
    def _make_api_request(self, url: str, params: Dict) -> Tuple[Optional[Dict], Optional[str]]:
        """Make API request with retry logic for rate limits."""
        for attempt in range(self.max_retries):
            try:
                # Debug: Log the authorization header (masked for security)
                auth_header = self.headers.get('Authorization', '')
                if auth_header:
                    masked_auth = auth_header[:10] + '...' + auth_header[-5:] if len(auth_header) > 15 else '***'
                    bt.logging.debug(f"Making request to {url} with Authorization: {masked_auth}")
                
                response = requests.get(url, headers=self.headers, params=params, timeout=30)
                
                if response.status_code in [429, 500, 502, 503, 504]:
                    if attempt < self.max_retries - 1:
                        bt.logging.warning(f"API error {response.status_code}, retrying in {self.retry_delay}s...")
                        time.sleep(self.retry_delay)
                        continue
                    return None, f"Max retries on status {response.status_code}"
                
                response.raise_for_status()
                data = response.json()
                
                # Handle Desearch.ai response format: {"user": {...}, "tweets": [...]}
                if isinstance(data, dict) and 'tweets' in data:
                    return data, None
                
                # Handle Desearch.ai response format (list of tweets) - legacy
                if isinstance(data, list):
                    return data, None
                
                # Handle Desearch.ai response wrapped in 'data' key
                if isinstance(data, dict) and 'data' in data and isinstance(data['data'], list):
                    return data['data'], None
                
                # Normalize response structure - Legacy RapidAPI formats (for backward compatibility):
                # - Standard: {"data": {"user": {...}}}
                # - Paginated: {"user": {...}}
                if 'data' in data and 'user' in data['data']:
                    return data, None
                elif 'user' in data:
                    # Wrap paginated response to match standard structure
                    return {'data': data}, None
                elif 'errors' in data:
                    return None, f"API error: {data.get('errors')}"
                else:
                    if attempt < self.max_retries - 1:
                        time.sleep(self.retry_delay)
                        continue
                    return None, "Invalid response structure"
                
            except requests.exceptions.Timeout:
                if attempt < self.max_retries - 1:
                    bt.logging.warning(f"Request timeout, retrying...")
                    time.sleep(self.retry_delay)
                    continue
                return None, "Request timeout"
            except Exception as e:
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    continue
                return None, str(e)
        
        return None, "Max retries exceeded"
    
    def _validate_tweet_authors(self, tweets: List[Dict], username: str) -> List[Dict]:
        """
        Filter tweets to only those authored by the specified username.
        
        Handles backward compatibility by setting author field for old cache entries.
        
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
                # Backward compatibility: assume timeline owner for old cache entries
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
    
    def _fetch_from_single_endpoint(
        self,
        url: str,
        username: str,
        tweet_limit: int,
        incremental_cutoff: datetime
    ) -> Tuple[List[Dict], Optional[Dict], bool]:
        """
        Fetch tweets from a single Twitter API endpoint with pagination.
        
        Args:
            url: API endpoint URL
            username: Twitter username to fetch for
            tweet_limit: Maximum tweets to fetch
            incremental_cutoff: Date cutoff for pagination stopping
            
        Returns:
            Tuple of (tweets_list, user_info, api_succeeded)
        """
        params = {"username": username, "limit": "40"}
        
        tweets = []
        # Initialize user_info with requested username (guaranteed correct)
        # Followers count will be extracted from timeline owner's tweets if available
        user_info = {
            'username': username.lower(),
            'followers_count': 0
        }
        cursor = None
        api_fetch_succeeded = False
        max_pages = 10  # Limit pagination to prevent excessive API calls
        page_count = 0
        
        while len(tweets) < tweet_limit and page_count < max_pages:
            page_count += 1
            if cursor:
                params["cursor"] = cursor
            
            data, error = self._make_api_request(url, params)
            if error:
                bt.logging.error(f"API failed for @{username} at {url.split('/')[-1]}: {error}")
                break
            
            api_fetch_succeeded = True
            
            # Extract timeline data
            try:
                # Response is normalized by _make_api_request to {'data': {'user': ...}}
                timeline = data['data']['user']['result']['timeline']['timeline']
                instructions = timeline.get('instructions', [])
                
                # Collect entries and track pinned tweet IDs
                entries = []
                pinned_entry_ids = set()
                for instruction in instructions:
                    inst_type = instruction.get('type')
                    if inst_type == 'TimelinePinEntry':
                        entry = instruction.get('entry')
                        if entry:
                            entries.append(entry)
                            pinned_entry_ids.add(entry.get('entryId', ''))
                    elif inst_type == 'TimelineAddEntries':
                        entries.extend(instruction.get('entries', []))
                
            except KeyError:
                break
            
            cursor = None
            tweets_found = 0
            
            for entry in entries:
                entry_id = entry.get('entryId', '')
                
                # Handle cursor entries
                if entry_id.startswith('cursor-'):
                    if entry.get('content', {}).get('cursorType') == 'Bottom':
                        cursor = entry.get('content', {}).get('value')
                    continue
                
                # Handle regular tweet entries
                if entry_id.startswith('tweet-'):
                    tweet_data = self._parse_tweet(entry, username)
                    if tweet_data:
                        # Default author for /tweets endpoint (only returns user's tweets)
                        if not tweet_data.get('author') and '/user/tweets' in url and '/tweetsandreplies' not in url:
                            tweet_data['author'] = username
                        
                        # Filter by author during pagination
                        if (tweet_data.get('author') or '').lower() == username:
                            tweets.append(tweet_data)
                            tweets_found += 1
                        
                        # Check cutoff only for non-pinned tweets
                        is_pinned = entry_id in pinned_entry_ids
                        if not is_pinned and tweet_data.get('created_at'):
                            try:
                                tweet_date = datetime.strptime(tweet_data['created_at'], '%a %b %d %H:%M:%S %z %Y')
                                cutoff_with_tz = incremental_cutoff.replace(tzinfo=tweet_date.tzinfo)
                                if tweet_date < cutoff_with_tz:
                                    bt.logging.debug(f"Reached incremental cutoff for @{username}")
                                    cursor = None
                                    break
                            except ValueError:
                                pass
                    
                    # Extract followers count if not yet collected and tweet is from timeline owner
                    if user_info['followers_count'] == 0 and tweet_data.get('author', '').lower() == username:
                        followers = self._extract_followers_count(entry)
                        if followers:
                            user_info['followers_count'] = followers
                
                # Handle profile-conversation entries (contains multiple tweets)
                elif entry_id.startswith('profile-conversation-'):
                    conversation_tweets = self._parse_profile_conversation(entry, username)
                    
                    for tweet_data in conversation_tweets:
                        if tweet_data:
                            # Default author for /tweets endpoint (only returns user's tweets)
                            if not tweet_data.get('author') and '/user/tweets' in url and '/tweetsandreplies' not in url:
                                tweet_data['author'] = username
                            
                            # Filter by author during pagination
                            if (tweet_data.get('author') or '').lower() == username:
                                tweets.append(tweet_data)
                                tweets_found += 1
                
                # Check tweet limit
                if len(tweets) >= tweet_limit:
                    bt.logging.debug(f"Reached tweet limit ({tweet_limit}) for @{username}")
                    cursor = None
                    break
            
            if not cursor:
                break
            
            time.sleep(self.rate_limit_delay)  # Rate limiting
        
        return tweets, user_info, api_fetch_succeeded
    
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
                    'cached_tweets': len(cached_data['tweets'])
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
        
        # Desearch.ai: fetch from endpoint(s) based on posts_only mode
        all_tweets = []
        user_info = None
        api_fetch_succeeded = False
        
        # Select endpoints based on posts_only mode
        if self.posts_only:
            # Posts-only mode: faster, uses less quota (excludes replies)
            endpoints = [
                ("/twitter/user/posts", "posts", "username")  # posts endpoint uses "username" param
            ]
        else:
            # Dual-endpoint mode: complete tweet coverage (includes replies)
            endpoints = [
                ("/twitter/replies", "replies", "user"),  # replies endpoint uses "user" param
                ("/twitter/user/posts", "posts", "username")  # posts endpoint uses "username" param
            ]
        
        # Fetch from endpoint(s) in parallel
        with ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
            future_to_endpoint = {
                executor.submit(
                    self._fetch_from_desearch_endpoint,
                    endpoint_path, username, tweet_limit, incremental_cutoff, param_name
                ): (endpoint_path, endpoint_name)
                for endpoint_path, endpoint_name, param_name in endpoints
            }
            
            for future in as_completed(future_to_endpoint):
                endpoint_path, endpoint_name = future_to_endpoint[future]
                try:
                    tweets, endpoint_user_info, endpoint_success = future.result()
                    all_tweets.extend(tweets)
                    
                    # Use user_info from first successful endpoint
                    if endpoint_user_info and not user_info:
                        user_info = endpoint_user_info
                    
                    # Track if any endpoint succeeded
                    if endpoint_success:
                        api_fetch_succeeded = True
                        
                    bt.logging.debug(f"Fetched {len(tweets)} tweets from Desearch.ai {endpoint_name} endpoint for @{username}")
                except Exception as e:
                    bt.logging.warning(f"Failed to fetch from Desearch.ai {endpoint_name} endpoint for @{username}: {e}")
        
        # Deduplicate by tweet_id (handles overlap between endpoints and pinned tweets)
        unique_map = {t['tweet_id']: t for t in all_tweets if t.get('tweet_id')}
        tweets = list(unique_map.values())
        
        # Log fetch results
        if len(endpoints) > 1:
            overlap_count = len(all_tweets) - len(tweets)
            bt.logging.info(
                f"Fetched {len(all_tweets)} total tweets from {len(endpoints)} Desearch.ai endpoints for @{username}, "
                f"{len(tweets)} unique ({overlap_count} duplicates removed)"
            )
        elif len(all_tweets) != len(tweets):
            overlap_count = len(all_tweets) - len(tweets)
            bt.logging.info(
                f"Fetched {len(all_tweets)} tweets from Desearch.ai for @{username}, "
                f"{len(tweets)} unique ({overlap_count} duplicates removed)"
            )
        else:
            bt.logging.info(f"Fetched {len(tweets)} tweets from Desearch.ai for @{username}")
        
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
                'cached_tweets': cached_count
            }
        }
    
    def _parse_tweet_result(self, tweet_result: Dict) -> Optional[Dict]:
        """
        Parse tweet data from tweet_results.result object.
        
        Shared parsing logic for both regular tweets and profile-conversation items.
        """
        try:
            legacy = tweet_result.get('legacy', {})
            
            # Check for note_tweet (extended tweets)
            note_tweet = tweet_result.get('note_tweet', {}).get('note_tweet_results', {}).get('result', {})
            
            if note_tweet and note_tweet.get('text'):
                text = note_tweet['text']
                entity_set = note_tweet.get('entity_set', {})
                tagged_accounts = [m.get('screen_name', '').lower() 
                                 for m in entity_set.get('user_mentions', [])]
            else:
                text = legacy.get('full_text', '')
                if not text:
                    return None
                tagged_accounts = [m.get('screen_name', '').lower() 
                                 for m in legacy.get('entities', {}).get('user_mentions', [])]
            
            # Parse retweet
            is_retweet = text.startswith('RT @')
            retweeted_user = None
            retweeted_tweet_id = None
            if is_retweet:
                rt_match = re.match(r'RT @(\w+):', text)
                retweeted_user = rt_match.group(1).lower() if rt_match else None
                tagged_accounts = []
                retweeted_status = legacy.get('retweeted_status_result', {}).get('result', {})
                retweeted_tweet_id = retweeted_status.get('rest_id')
            
            # Parse quote tweet
            is_quote = legacy.get('is_quote_status', False) and not is_retweet
            quoted_user = None
            quoted_tweet_id = None
            if is_quote:
                # Check multiple sources for quoted tweet ID (Twitter API returns it in different places)
                # 1. Direct field in legacy object (most reliable)
                quoted_tweet_id = legacy.get('quoted_status_id_str')
                
                # 2. Nested object (alternative structure)
                if not quoted_tweet_id:
                    quoted_status_result = legacy.get('quoted_status_result', {}).get('result', {})
                    quoted_tweet_id = quoted_status_result.get('rest_id')
                
                # 3. Parse from permalink URL (fallback)
                if not quoted_tweet_id:
                    url = legacy.get('quoted_status_permalink', {}).get('expanded', '')
                    match = re.search(r'twitter\.com/([^/]+)/status/(\d+)', url)
                    if match:
                        quoted_user = match.group(1).lower()
                        quoted_tweet_id = match.group(2)
                
                # Extract quoted user if not already found
                if quoted_tweet_id and not quoted_user:
                    # Try to get from quoted_status_result
                    try:
                        quoted_status_result = legacy.get('quoted_status_result', {}).get('result', {})
                        quoted_user = quoted_status_result['core']['user_results']['result']['legacy']['screen_name'].lower()
                    except (KeyError, AttributeError, TypeError):
                        # Fallback to permalink URL
                        url = legacy.get('quoted_status_permalink', {}).get('expanded', '')
                        match = re.search(r'twitter\.com/([^/]+)/status/(\d+)', url)
                        if match:
                            quoted_user = match.group(1).lower()
            
            # Extract actual author from core.user_results (not the timeline owner)
            # This is crucial for detecting retweets that don't have "RT @" prefix
            author = None
            try:
                author = tweet_result['core']['user_results']['result']['legacy']['screen_name'].lower()
            except (KeyError, AttributeError, TypeError):
                try:
                    # Fallback: some API responses use 'core' instead of 'legacy'
                    author = tweet_result['core']['user_results']['result']['core']['screen_name'].lower()
                except (KeyError, AttributeError, TypeError):
                    pass
            
            return {
                'tweet_id': tweet_result.get('rest_id', ''),
                'created_at': legacy.get('created_at', ''),
                'text': text,
                'author': author,  # Actual tweet author (None if not found)
                'tagged_accounts': tagged_accounts,
                'retweeted_user': retweeted_user,
                'retweeted_tweet_id': retweeted_tweet_id,
                'quoted_user': quoted_user,
                'quoted_tweet_id': quoted_tweet_id,
                'lang': legacy.get('lang', 'und'),
                'favorite_count': legacy.get('favorite_count', 0),
                'retweet_count': legacy.get('retweet_count', 0),
                'reply_count': legacy.get('reply_count', 0),
                'quote_count': legacy.get('quote_count', 0),
                'bookmark_count': legacy.get('bookmark_count', 0),
                'in_reply_to_status_id': legacy.get('in_reply_to_status_id_str'),
                'in_reply_to_user': legacy.get('in_reply_to_screen_name')
            }
        except (KeyError, AttributeError):
            return None
    
    def _parse_tweet(self, entry: Dict, username: str) -> Optional[Dict]:
        """Extract tweet data from regular tweet entry."""
        try:
            tweet_result = entry['content']['itemContent']['tweet_results']['result']
            return self._parse_tweet_result(tweet_result)
        except (KeyError, AttributeError):
            return None
    
    def _extract_followers_count(self, entry: Dict) -> Optional[int]:
        """Extract followers count from tweet entry.
        
        Args:
            entry: Tweet entry from API response
            
        Returns:
            Followers count if available, None otherwise
        """
        try:
            user_data = entry['content']['itemContent']['tweet_results']['result']['core']['user_results']['result']['legacy']
            return user_data.get('followers_count', 0)
        except (KeyError, AttributeError):
            return None
    
    def _parse_profile_conversation(self, entry: Dict, username: str) -> List[Dict]:
        """Extract tweets from profile-conversation entry containing multiple tweets."""
        tweets = []
        
        try:
            items = entry.get('content', {}).get('items', [])
            for item in items:
                try:
                    item_content = item.get('item', {}).get('itemContent', {})
                    tweet_result = item_content.get('tweet_results', {}).get('result', {})
                    
                    if tweet_result:
                        tweet_data = self._parse_tweet_result(tweet_result)
                        if tweet_data:
                            tweets.append(tweet_data)
                except (KeyError, AttributeError):
                    continue
        except (KeyError, AttributeError):
            pass
        
        return tweets
    
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
    
    def _parse_desearch_tweet(self, desearch_data: Dict, username: str) -> Optional[Dict]:
        """
        Parse Desearch.ai API response to match RapidAPI format.
        
        Args:
            desearch_data: Raw tweet object from Desearch.ai API
            username: Expected username (for validation)
            
        Returns:
            Tweet dict in the same format as _parse_tweet_result
        """
        try:
            # Extract tweet data
            tweet_id = str(desearch_data.get('id', ''))
            if not tweet_id:
                return None
            
            text = desearch_data.get('text', '')
            if not text:
                return None
            
            # Use the username parameter (tweets don't have nested user object in Desearch.ai response)
            author_username = username.lower()
            
            created_at_iso = desearch_data.get('created_at', '')
            
            # Convert ISO 8601 to Twitter date format
            # "2023-01-01T00:00:00Z" -> "Wed Jan 01 00:00:00 +0000 2023"
            created_at = self._convert_iso_to_twitter_date(created_at_iso)
            
            # Extract engagement metrics
            like_count = desearch_data.get('like_count', 0)
            retweet_count = desearch_data.get('retweet_count', 0)
            reply_count = desearch_data.get('reply_count', 0)
            quote_count = desearch_data.get('quote_count', 0)
            bookmark_count = desearch_data.get('bookmark_count', 0)
            
            # Extract retweet info
            is_retweet = desearch_data.get('is_retweet', False)
            retweeted_user = None
            retweeted_tweet_id = None
            if is_retweet and desearch_data.get('retweet'):
                retweet_data = desearch_data['retweet']
                retweeted_tweet_id = str(retweet_data.get('id', ''))
                retweet_user = retweet_data.get('user', {})
                if retweet_user:
                    retweeted_user = retweet_user.get('username', '').lower()
            
            # Extract quote info
            is_quote = desearch_data.get('is_quote_tweet', False)
            quoted_user = None
            quoted_tweet_id = desearch_data.get('quoted_status_id')
            if quoted_tweet_id:
                quoted_tweet_id = str(quoted_tweet_id)
                # Try to extract from quote object if available
                if desearch_data.get('quote') and desearch_data['quote'].get('user'):
                    quoted_user = desearch_data['quote']['user'].get('username', '').lower()
            
            # Extract tagged accounts from entities (if available)
            tagged_accounts = []
            entities = desearch_data.get('entities', {})
            if entities:
                user_mentions = entities.get('user_mentions', [])
                if isinstance(user_mentions, list):
                    tagged_accounts = [m.get('screen_name', '').lower() for m in user_mentions if m.get('screen_name')]
            
            # Extract reply info
            in_reply_to_status_id = desearch_data.get('in_reply_to_status_id')
            if in_reply_to_status_id:
                in_reply_to_status_id = str(in_reply_to_status_id)
            in_reply_to_user = desearch_data.get('in_reply_to_screen_name', '').lower() if desearch_data.get('in_reply_to_screen_name') else None
            
            return {
                'tweet_id': tweet_id,
                'created_at': created_at,
                'text': text,
                'author': author_username,
                'tagged_accounts': tagged_accounts,
                'retweeted_user': retweeted_user,
                'retweeted_tweet_id': retweeted_tweet_id,
                'quoted_user': quoted_user,
                'quoted_tweet_id': quoted_tweet_id,
                'lang': desearch_data.get('lang', 'und'),
                'favorite_count': like_count,  # Map like_count to favorite_count
                'retweet_count': retweet_count,
                'reply_count': reply_count,
                'quote_count': quote_count,
                'bookmark_count': bookmark_count,
                'in_reply_to_status_id': in_reply_to_status_id,
                'in_reply_to_user': in_reply_to_user
            }
        except (KeyError, AttributeError, ValueError) as e:
            bt.logging.debug(f"Failed to parse Desearch.ai tweet: {e}")
            return None

    def _convert_iso_to_twitter_date(self, iso_date: str) -> str:
        """
        Convert ISO 8601 date to Twitter date format.
        
        "2023-01-01T00:00:00Z" -> "Wed Jan 01 00:00:00 +0000 2023"
        """
        try:
            # Handle both 'Z' and timezone offset formats
            if iso_date.endswith('Z'):
                dt = datetime.fromisoformat(iso_date.replace('Z', '+00:00'))
            else:
                dt = datetime.fromisoformat(iso_date)
            # Format to Twitter style
            return dt.strftime('%a %b %d %H:%M:%S %z %Y')
        except (ValueError, AttributeError):
            return iso_date  # Return original if conversion fails

    def _fetch_from_desearch_endpoint(
        self,
        endpoint_path: str,
        username: str,
        tweet_limit: int,
        incremental_cutoff: datetime,
        param_name: str = "username"
    ) -> Tuple[List[Dict], Optional[Dict], bool]:
        """
        Fetch tweets from Desearch.ai API for a user with pagination support.
        
        Args:
            endpoint_path: Desearch.ai endpoint path (e.g., "/twitter/user/posts" or "/twitter/replies")
            username: Twitter username to fetch tweets for
            tweet_limit: Maximum number of tweets to fetch
            incremental_cutoff: Date cutoff for pagination stopping
            param_name: Parameter name to use ("username" for posts, "user" for replies)
        
        Uses: {endpoint_path}?{param_name}={username}&count=100&start={start}
        Supports pagination to fetch up to 400 tweets per user.
        """
        url = f"{self.base_url}{endpoint_path}"
        
        tweets = []
        user_info = {
            'username': username.lower(),
            'followers_count': 0
        }
        api_fetch_succeeded = False
        
        # Pagination parameters
        count_per_page = 100
        start = 0
        # Calculate max pages needed: ceil(tweet_limit / count_per_page)
        max_pages = (tweet_limit + count_per_page - 1) // count_per_page
        pages_fetched = 0
        
        try:
            for page in range(max_pages):
                # Stop if we've reached the tweet limit
                if len(tweets) >= tweet_limit:
                    break
                
                params = {
                    param_name: username,
                    "count": count_per_page,
                    "start": start
                }
                
                data, error = self._make_api_request(url, params)
                if error:
                    # Check if it's a 401 error and log more details
                    if "401" in str(error) or "Unauthorized" in str(error):
                        # Log the auth header format (masked) for debugging
                        auth_header = self.headers.get('Authorization', '')
                        masked_auth = auth_header[:15] + '...' + auth_header[-5:] if len(auth_header) > 20 else '***'
                        bt.logging.error(
                            f"Desearch.ai API 401 Unauthorized for @{username}. "
                            f"Auth header format: {masked_auth} (key length: {len(self.api_key)}). "
                            f"Check if DESEARCH_API_KEY in .env is correct."
                        )
                    else:
                        bt.logging.error(f"Desearch.ai API failed for @{username} (page {page + 1}): {error}")
                    
                    # If first page fails, return empty; otherwise continue with what we have
                    if page == 0:
                        return tweets, user_info, False
                    break
                
                api_fetch_succeeded = True
                
                # Desearch.ai returns {"user": {...}, "tweets": [...]}
                if isinstance(data, dict) and 'tweets' in data:
                    tweet_list = data.get('tweets', [])
                    # Extract user info from response (only on first page)
                    if page == 0:
                        user_data = data.get('user', {})
                        if user_data:
                            user_info['followers_count'] = user_data.get('followers_count', 0)
                elif isinstance(data, list):
                    # Legacy format: list of tweets
                    tweet_list = data
                else:
                    # Try 'data' key as fallback
                    tweet_list = data.get('data', []) if isinstance(data, dict) else []
                
                if not isinstance(tweet_list, list):
                    bt.logging.warning(f"Unexpected Desearch.ai response format for @{username} (page {page + 1})")
                    break
                
                # If no tweets returned, we've reached the end
                if not tweet_list:
                    bt.logging.debug(f"No more tweets for @{username} (page {page + 1})")
                    break
                
                # Process tweets from this page
                page_tweets_count = 0
                reached_cutoff = False
                
                for tweet_data in tweet_list:
                    parsed_tweet = self._parse_desearch_tweet(tweet_data, username)
                    if not parsed_tweet:
                        continue
                    
                    # Check date cutoff
                    try:
                        tweet_date_str = parsed_tweet.get('created_at', '')
                        if tweet_date_str:
                            tweet_date = datetime.strptime(tweet_date_str, '%a %b %d %H:%M:%S %z %Y')
                            cutoff_with_tz = incremental_cutoff.replace(tzinfo=tweet_date.tzinfo)
                            if tweet_date < cutoff_with_tz:
                                reached_cutoff = True
                                break  # Stop if we've reached the cutoff
                    except (ValueError, AttributeError):
                        pass
                    
                    tweets.append(parsed_tweet)
                    page_tweets_count += 1
                    
                    # User info already extracted from response root 'user' key above
                    # Only extract from tweet if not already set
                    if user_info['followers_count'] == 0 and tweet_data.get('user'):
                        user_info['followers_count'] = tweet_data['user'].get('followers_count', 0)
                    
                    if len(tweets) >= tweet_limit:
                        break
                
                pages_fetched += 1
                bt.logging.debug(
                    f"Fetched {page_tweets_count} tweets from Desearch.ai for @{username} "
                    f"(page {pages_fetched}, total: {len(tweets)})"
                )
                
                # Stop if we've reached the date cutoff or tweet limit
                if reached_cutoff or len(tweets) >= tweet_limit:
                    break
                
                # If we got fewer tweets than requested, we've reached the end
                if len(tweet_list) < count_per_page:
                    bt.logging.debug(f"Reached end of tweets for @{username} (page {pages_fetched})")
                    break
                
                # Prepare for next page
                start += count_per_page
                
                # Rate limiting between pages
                if page < max_pages - 1:
                    time.sleep(self.rate_limit_delay)
            
            if api_fetch_succeeded:
                bt.logging.debug(
                    f"Fetched {len(tweets)} total tweets from Desearch.ai for @{username} "
                    f"(across {pages_fetched} page(s))"
                )
            
            return tweets, user_info, api_fetch_succeeded
            
        except Exception as e:
            bt.logging.error(f"Desearch.ai API error for @{username}: {e}")
            return tweets, user_info, False

