"""
Simplified Twitter client with API access and caching for PageRank scoring.

Combines API communication, caching, and basic tweet processing in one module.
"""

import requests
import time
import re
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import bittensor as bt

from bitcast.validator.utils.config import (
    RAPID_API_KEY,
    TWITTER_DEFAULT_LOOKBACK_DAYS,
    TWEET_FETCH_LIMIT,
    TWITTER_CACHE_FRESHNESS,
    FORCE_CACHE_REFRESH
)
from bitcast.validator.utils.twitter_cache import (
    get_cached_user_tweets,
    cache_user_tweets,
    get_cached_user_info,
    cache_user_info
)


class TwitterClient:
    """
    Twitter API client with intelligent caching and tweet processing.
    
    Handles API communication, caching, and basic tweet processing for
    PageRank-based network analysis.
    """
    
    def __init__(self, api_key: Optional[str] = None, 
                 max_retries: int = 3, retry_delay: float = 2.0, rate_limit_delay: float = 1.0,
                 lookback_hours: int = 96, force_cache_refresh: Optional[bool] = None):
        """Initialize client with API key and configuration options.
        
        Args:
            api_key: RapidAPI key for Twitter API access
            max_retries: Maximum number of API request retries (default: 3)
            retry_delay: Delay in seconds between retries (default: 2.0)
            rate_limit_delay: Delay in seconds between API calls (default: 1.0)
            lookback_hours: Hours to look back when updating stale cache (default: 96)
            force_cache_refresh: If True, always refresh cache (ignores freshness check)
        """
        self.api_key = api_key or RAPID_API_KEY
        if not self.api_key:
            raise ValueError("RAPID_API_KEY environment variable must be set")
        
        # Configuration
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.rate_limit_delay = rate_limit_delay
        self.lookback_hours = lookback_hours
        self.force_cache_refresh = force_cache_refresh if force_cache_refresh is not None else FORCE_CACHE_REFRESH
        
        self.headers = {
            "x-rapidapi-key": self.api_key,
            "x-rapidapi-host": "twitter-v24.p.rapidapi.com"
        }
        
        cache_mode = "forced refresh mode" if self.force_cache_refresh else f"with {TWITTER_CACHE_FRESHNESS/3600:.1f}h freshness check"
        bt.logging.info(f"TwitterClient initialized with centralized cache ({cache_mode})")
    
    def _make_api_request(self, url: str, params: Dict) -> Tuple[Optional[Dict], Optional[str]]:
        """Make API request with retry logic for rate limits."""
        for attempt in range(self.max_retries):
            try:
                response = requests.get(url, headers=self.headers, params=params, timeout=30)
                
                if response.status_code in [429, 500, 502, 503, 504]:
                    if attempt < self.max_retries - 1:
                        bt.logging.warning(f"API error {response.status_code}, retrying in {self.retry_delay}s...")
                        time.sleep(self.retry_delay)
                        continue
                    return None, f"Max retries on status {response.status_code}"
                
                response.raise_for_status()
                data = response.json()
                
                # Normalize response structure - Twitter API returns different formats:
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
    
    def fetch_user_tweets(self, username: str, force_refresh: bool = False, validate_author: bool = True) -> Dict[str, Any]:
        """
        Fetch tweets for a user with intelligent caching and incremental updates.
        
        Args:
            username: Twitter username to fetch tweets for
            force_refresh: If True, bypass cache and always fetch fresh data (default: False)
            validate_author: If True, filter tweets to only those authored by username (default: True)
                           The tweetsandreplies API returns both user's tweets AND replies from others,
                           so validation ensures only the timeline owner's tweets are included.
        
        Stops when either TWEET_FETCH_LIMIT is reached OR tweets older than TWITTER_DEFAULT_LOOKBACK_DAYS.
        Uses smart cache merging to preserve historical tweets while fetching recent updates.
        
        Returns dict with 'user_info', 'tweets', and 'cache_info'
        """
        tweet_limit = TWEET_FETCH_LIMIT
        username = username.lower()
        
        # Calculate cutoff date for lookback period
        cutoff_date = datetime.now() - timedelta(days=TWITTER_DEFAULT_LOOKBACK_DAYS)
        
        # Check cache and determine fetch strategy
        cached_data = get_cached_user_tweets(username)
        incremental_cutoff = cutoff_date
        
        if cached_data and not force_refresh:
            last_updated = cached_data.get('last_updated')
            
            # If updated within past freshness period, use cache completely (unless force refresh enabled)
            if not self.force_cache_refresh and last_updated and (datetime.now() - last_updated).total_seconds() < TWITTER_CACHE_FRESHNESS:
                bt.logging.debug(f"Using cached tweets for @{username} ({len(cached_data['tweets'])} tweets)")
                cached_tweets = cached_data['tweets']
                
                # Apply author validation to cached data
                if validate_author:
                    cached_tweets = self._validate_tweet_authors(cached_tweets, username)
                
                return {
                    'user_info': cached_data['user_info'],
                    'tweets': cached_tweets,
                    'cache_info': {'cache_hit': True, 'new_tweets': 0}
                }
            
            # Cache is stale - use incremental update with lookback buffer
            if last_updated:
                incremental_cutoff = max(
                    last_updated - timedelta(hours=self.lookback_hours),
                    cutoff_date
                )
                if self.force_cache_refresh:
                    bt.logging.info(f"Force cache refresh enabled - fetching updates for @{username} (lookback to {incremental_cutoff.strftime('%Y-%m-%d %H:%M')})")
                else:
                    bt.logging.info(f"Updating stale cache for @{username} (lookback to {incremental_cutoff.strftime('%Y-%m-%d %H:%M')})")
        
        if force_refresh:
            bt.logging.info(f"Force refresh enabled for @{username} - bypassing cache")
        
        # Fetch from API
        url = "https://twitter-v24.p.rapidapi.com/user/tweetsandreplies"
        params = {"username": username, "limit": "40"}
        
        tweets = []
        user_info = None
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
                bt.logging.error(f"API failed for @{username}: {error}")
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
                        # Filter by author during pagination if validation is enabled
                        if not validate_author or tweet_data.get('author', '').lower() == username:
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
                    
                    # Extract user info if not yet collected
                    if not user_info:
                        user_info = self._extract_user_info(entry, username)
                
                # Handle profile-conversation entries (contains multiple tweets)
                elif entry_id.startswith('profile-conversation-'):
                    conversation_tweets = self._parse_profile_conversation(entry, username)
                    for tweet_data in conversation_tweets:
                        if tweet_data:
                            # Filter by author during pagination if validation is enabled
                            if not validate_author or tweet_data.get('author', '').lower() == username:
                                tweets.append(tweet_data)
                                tweets_found += 1
                            
                            # Check cutoff (profile-conversation entries are not pinned)
                            if tweet_data.get('created_at'):
                                try:
                                    tweet_date = datetime.strptime(tweet_data['created_at'], '%a %b %d %H:%M:%S %z %Y')
                                    cutoff_with_tz = incremental_cutoff.replace(tzinfo=tweet_date.tzinfo)
                                    if tweet_date < cutoff_with_tz:
                                        bt.logging.debug(f"Reached incremental cutoff for @{username}")
                                        cursor = None
                                        break
                                except ValueError:
                                    pass
                            
                            # Extract user info if not yet collected
                            if not user_info and 'author' in tweet_data:
                                # Try to extract from the tweet data in profile-conversation
                                try:
                                    user_info = {
                                        'username': tweet_data['author'],
                                        'followers_count': 0  # Not available in profile-conversation items
                                    }
                                except (KeyError, AttributeError):
                                    pass
                
                # Check tweet limit
                if len(tweets) >= tweet_limit:
                    bt.logging.info(f"Reached tweet limit ({tweet_limit}) for @{username}")
                    cursor = None
                    break
            
            if not cursor or tweets_found == 0:
                break
            
            time.sleep(self.rate_limit_delay)  # Rate limiting
        
        # Deduplicate by tweet_id (pinned tweets appear on every page)
        unique_map = {t['tweet_id']: t for t in tweets if t.get('tweet_id')}
        tweets = list(unique_map.values())
        
        # Reset missing counter for newly fetched tweets
        for tweet in tweets:
            tweet['missing_count'] = 0
        
        bt.logging.info(f"Fetched {len(tweets)} new tweets for @{username}")
        
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
            
            # Sort by date (most recent first)
            def get_tweet_date(tweet):
                try:
                    if tweet.get('created_at'):
                        return datetime.strptime(tweet['created_at'], '%a %b %d %H:%M:%S %z %Y')
                    return datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)
                except ValueError:
                    return datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)
            
            all_tweets.sort(key=get_tweet_date, reverse=True)
            bt.logging.debug(f"Merged: {len(tweets)} new + {cached_count} cached = {len(all_tweets)} total" + 
                           (f", {incremented_missing} missing++" if incremented_missing > 0 else ""))
        
        # Validate author: filter to only tweets from timeline owner
        if validate_author:
            original_count = len(all_tweets)
            all_tweets = self._validate_tweet_authors(all_tweets, username)
            
            if len(all_tweets) < original_count:
                filtered_count = original_count - len(all_tweets)
                bt.logging.debug(f"Filtered {filtered_count} tweets from other authors (keeping {len(all_tweets)} from @{username})")
        
        # Filter deleted tweets (missing_count >= 2) before returning
        visible_tweets = [t for t in all_tweets if t.get('missing_count', 0) < 2]
        if len(visible_tweets) < len(all_tweets):
            bt.logging.debug(f"Filtered {len(all_tweets) - len(visible_tweets)} deleted tweets from @{username}")
        
        # Cache all tweets including deleted ones
        cache_data = {
            'user_info': (cached_data.get('user_info') if cached_data else None) or user_info or {'username': username, 'followers_count': 0},
            'tweets': all_tweets,
            'last_updated': datetime.now()
        }
        cache_user_tweets(username, cache_data)
        
        return {
            'user_info': cache_data['user_info'],
            'tweets': visible_tweets,
            'cache_info': {'cache_hit': bool(cached_data), 'new_tweets': len(tweets), 'cached_tweets': cached_count}
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
    
    def _extract_user_info(self, entry: Dict, username: str) -> Optional[Dict]:
        """Extract user info from tweet entry."""
        try:
            user_data = entry['content']['itemContent']['tweet_results']['result']['core']['user_results']['result']['legacy']
            return {
                'username': user_data.get('screen_name', username).lower(),
                'followers_count': user_data.get('followers_count', 0)
            }
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
    
    def check_user_relevance(self, username: str, keywords: List[str], min_followers: int = 0, lang: Optional[str] = None) -> bool:
        """Check if user tweets about keywords and meets follower threshold.
        
        Args:
            username: Twitter username to check
            keywords: List of keywords to search for
            min_followers: Minimum follower count threshold
            lang: Optional language filter (e.g., 'en', 'zh'). If specified, user must have at least 
                  one tweet in this language, but keywords are checked across all tweets.
        
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
        
        # Check keywords across ALL tweets (regardless of language)
        keywords_lower = [kw.lower() for kw in keywords]
        for tweet in result['tweets']:
            text_lower = tweet['text'].lower()
            # Use word boundaries to match whole words only
            if any(re.search(r'\b' + re.escape(kw) + r'\b', text_lower) for kw in keywords_lower):
                return True
        
        return False
    

