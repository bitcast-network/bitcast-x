"""
RapidAPI Twitter API provider implementation.

Implements the TwitterProvider interface for RapidAPI (twitter-v24) access.
Restored from main branch for dual-provider support.
"""
import requests
import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import bittensor as bt

from .twitter_provider import TwitterProvider


class RapidAPIProvider(TwitterProvider):
    """
    RapidAPI (twitter-v24) implementation of Twitter API access.
    
    Handles RapidAPI-specific API communication, complex response parsing,
    and cursor-based pagination logic.
    """
    
    def __init__(self, api_key: str, max_retries: int = 3,
                 retry_delay: float = 2.0, rate_limit_delay: float = 1.0):
        """
        Initialize RapidAPI provider.
        
        Args:
            api_key: RapidAPI key for Twitter API access
            max_retries: Maximum number of API request retries
            retry_delay: Delay in seconds between retries
            rate_limit_delay: Delay in seconds between API calls
        """
        self.api_key = api_key.strip()
        self.base_url = "https://twitter-v24.p.rapidapi.com"
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.rate_limit_delay = rate_limit_delay
        
        self.headers = {
            "x-rapidapi-key": self.api_key,
            "x-rapidapi-host": "twitter-v24.p.rapidapi.com"
        }
    
    def validate_api_key(self) -> bool:
        """
        Validate RapidAPI key (any non-empty string).
        
        Returns:
            True if key is non-empty, False otherwise
        """
        return bool(self.api_key and len(self.api_key) > 0)
    
    def fetch_user_tweets(
        self,
        username: str,
        incremental_cutoff: datetime,
        tweet_limit: int,
        posts_only: bool
    ) -> Tuple[List[Dict], Optional[Dict], bool]:
        """
        Fetch tweets for a user using RapidAPI.
        
        Args:
            username: Twitter username (lowercased)
            incremental_cutoff: Stop fetching tweets older than this date
            tweet_limit: Maximum number of tweets to fetch per endpoint
            posts_only: If True, fetch only from tweets endpoint.
                       If False, fetch from both tweetsandreplies and tweets endpoints.
        
        Returns:
            Tuple of (tweets_list, user_info, api_succeeded)
        """
        all_tweets = []
        user_info = None
        api_fetch_succeeded = False
        
        # Select endpoints based on posts_only mode
        if posts_only:
            # Posts-only mode: faster, uses less quota
            endpoints = ["/user/tweets"]
        else:
            # Dual-endpoint mode: complete tweet coverage (includes replies)
            endpoints = [
                "/user/tweetsandreplies",
                "/user/tweets"
            ]
        
        # Fetch from endpoint(s) in parallel
        with ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
            future_to_endpoint = {
                executor.submit(
                    self._fetch_from_endpoint,
                    endpoint_path, username, tweet_limit, incremental_cutoff
                ): endpoint_path
                for endpoint_path in endpoints
            }
            
            for future in as_completed(future_to_endpoint):
                endpoint_path = future_to_endpoint[future]
                try:
                    tweets, endpoint_user_info, endpoint_success = future.result()
                    all_tweets.extend(tweets)
                    
                    # Use user_info from first successful endpoint
                    if endpoint_user_info and not user_info:
                        user_info = endpoint_user_info
                    
                    # Track if any endpoint succeeded
                    if endpoint_success:
                        api_fetch_succeeded = True
                        
                    bt.logging.debug(
                        f"Fetched {len(tweets)} tweets from RapidAPI "
                        f"{endpoint_path.split('/')[-1]} endpoint for @{username}"
                    )
                except Exception as e:
                    bt.logging.warning(
                        f"Failed to fetch from RapidAPI {endpoint_path} "
                        f"endpoint for @{username}: {e}"
                    )
        
        # Deduplicate by tweet_id (handles overlap between endpoints and pinned tweets)
        unique_map = {t['tweet_id']: t for t in all_tweets if t.get('tweet_id')}
        tweets = list(unique_map.values())
        
        # Log fetch results
        if len(endpoints) > 1:
            overlap_count = len(all_tweets) - len(tweets)
            bt.logging.info(
                f"Fetched {len(all_tweets)} total tweets from {len(endpoints)} "
                f"RapidAPI endpoints for @{username}, {len(tweets)} unique "
                f"({overlap_count} duplicates removed)"
            )
        elif len(all_tweets) != len(tweets):
            overlap_count = len(all_tweets) - len(tweets)
            bt.logging.info(
                f"Fetched {len(all_tweets)} tweets from RapidAPI for @{username}, "
                f"{len(tweets)} unique ({overlap_count} duplicates removed)"
            )
        else:
            bt.logging.info(f"Fetched {len(tweets)} tweets from RapidAPI for @{username}")
        
        return tweets, user_info, api_fetch_succeeded
    
    def _make_api_request(self, url: str, params: Dict) -> Tuple[Optional[Dict], Optional[str]]:
        """
        Make API request with retry logic for rate limits.
        
        Args:
            url: API endpoint URL
            params: Query parameters
            
        Returns:
            Tuple of (response_data, error_message)
        """
        for attempt in range(self.max_retries):
            try:
                response = requests.get(url, headers=self.headers, params=params, timeout=30)
                
                if response.status_code in [429, 500, 502, 503, 504]:
                    if attempt < self.max_retries - 1:
                        bt.logging.warning(
                            f"API error {response.status_code}, retrying in {self.retry_delay}s..."
                        )
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
                    bt.logging.warning("Request timeout, retrying...")
                    time.sleep(self.retry_delay)
                    continue
                return None, "Request timeout"
            except Exception as e:
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    continue
                return None, str(e)
        
        return None, "Max retries exceeded"
    
    def _parse_tweet(self, tweet_result: Dict, username: str = None) -> Optional[Dict]:
        """
        Parse tweet data from RapidAPI response format.
        
        Args:
            tweet_result: Raw tweet object from RapidAPI
            username: Expected username (used as fallback if author extraction fails)
            
        Returns:
            Normalized tweet dict or None if parsing fails
        """
        try:
            legacy = tweet_result.get('legacy', {})
            
            # Check for note_tweet (extended tweets)
            note_tweet = tweet_result.get('note_tweet', {}).get('note_tweet_results', {}).get('result', {})
            
            if note_tweet and note_tweet.get('text'):
                text = note_tweet['text']
                entity_set = note_tweet.get('entity_set', {})
                tagged_accounts = [
                    m.get('screen_name', '').lower()
                    for m in entity_set.get('user_mentions', [])
                ]
            else:
                text = legacy.get('full_text', '')
                if not text:
                    return None
                tagged_accounts = [
                    m.get('screen_name', '').lower()
                    for m in legacy.get('entities', {}).get('user_mentions', [])
                ]
            
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
                # Check multiple sources for quoted tweet ID
                quoted_tweet_id = legacy.get('quoted_status_id_str')
                
                if not quoted_tweet_id:
                    quoted_status_result = legacy.get('quoted_status_result', {}).get('result', {})
                    quoted_tweet_id = quoted_status_result.get('rest_id')
                
                # Parse from permalink URL (fallback)
                if not quoted_tweet_id:
                    url = legacy.get('quoted_status_permalink', {}).get('expanded', '')
                    match = re.search(r'twitter\.com/([^/]+)/status/(\d+)', url)
                    if match:
                        quoted_user = match.group(1).lower()
                        quoted_tweet_id = match.group(2)
                
                # Extract quoted user if not already found
                if quoted_tweet_id and not quoted_user:
                    try:
                        quoted_status_result = legacy.get('quoted_status_result', {}).get('result', {})
                        quoted_user = (
                            quoted_status_result['core']['user_results']['result']['legacy']['screen_name']
                            .lower()
                        )
                    except (KeyError, AttributeError, TypeError):
                        url = legacy.get('quoted_status_permalink', {}).get('expanded', '')
                        match = re.search(r'twitter\.com/([^/]+)/status/(\d+)', url)
                        if match:
                            quoted_user = match.group(1).lower()
            
            # Extract actual author from core.user_results
            author = None
            try:
                author = (
                    tweet_result['core']['user_results']['result']['legacy']['screen_name']
                    .lower()
                )
            except (KeyError, AttributeError, TypeError):
                # Author extraction failed - leave as None
                # TwitterClient will reject this tweet with strict validation
                # DO NOT assume author=username because tweetsandreplies endpoint
                # can return tweets from other users (replies TO user FROM others)
                bt.logging.warning(
                    f"Failed to extract author from tweet {tweet_result.get('rest_id')} "
                    f"for @{username} - RapidAPI response may be malformed"
                )
                pass
            
            # Extract reply info
            in_reply_to_status_id = legacy.get('in_reply_to_status_id_str')
            in_reply_to_user = legacy.get('in_reply_to_screen_name')
            if in_reply_to_user:
                in_reply_to_user = in_reply_to_user.lower()
            
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
                'in_reply_to_status_id': in_reply_to_status_id,
                'in_reply_to_user': in_reply_to_user
            }
        except (KeyError, AttributeError):
            return None
    
    def _extract_followers_count(self, entry: Dict) -> Optional[int]:
        """
        Extract followers count from tweet entry.
        
        Args:
            entry: Tweet entry from API response
            
        Returns:
            Followers count if available, None otherwise
        """
        try:
            user_data = (
                entry['content']['itemContent']['tweet_results']['result']
                ['core']['user_results']['result']['legacy']
            )
            return user_data.get('followers_count', 0)
        except (KeyError, AttributeError):
            return None
    
    def _fetch_from_endpoint(
        self,
        endpoint_path: str,
        username: str,
        tweet_limit: int,
        incremental_cutoff: datetime
    ) -> Tuple[List[Dict], Optional[Dict], bool]:
        """
        Fetch tweets from RapidAPI endpoint with cursor-based pagination.
        
        Args:
            endpoint_path: RapidAPI endpoint path (e.g., "/user/tweets")
            username: Twitter username to fetch tweets for
            tweet_limit: Maximum number of tweets to fetch
            incremental_cutoff: Date cutoff for pagination stopping
        
        Returns:
            Tuple of (tweets_list, user_info, api_succeeded)
        """
        url = f"{self.base_url}{endpoint_path}"
        params = {"username": username, "limit": "40"}
        
        tweets = []
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
                bt.logging.error(
                    f"RapidAPI failed for @{username} at {endpoint_path.split('/')[-1]}: {error}"
                )
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
                    try:
                        tweet_result = (
                            entry['content']['itemContent']['tweet_results']['result']
                        )
                        tweet_data = self._parse_tweet(tweet_result, username)
                        
                        if tweet_data:
                            # Filter by author during pagination
                            if (tweet_data.get('author') or '').lower() == username:
                                tweets.append(tweet_data)
                                tweets_found += 1
                            
                            # Check cutoff only for non-pinned tweets
                            is_pinned = entry_id in pinned_entry_ids
                            if not is_pinned and tweet_data.get('created_at'):
                                try:
                                    tweet_date = datetime.strptime(
                                        tweet_data['created_at'], '%a %b %d %H:%M:%S %z %Y'
                                    )
                                    cutoff_with_tz = incremental_cutoff.replace(
                                        tzinfo=tweet_date.tzinfo
                                    )
                                    if tweet_date < cutoff_with_tz:
                                        bt.logging.debug(
                                            f"Reached incremental cutoff for @{username}"
                                        )
                                        cursor = None
                                        break
                                except ValueError:
                                    pass
                        
                        # Extract followers count if not yet collected
                        if user_info['followers_count'] == 0:
                            followers = self._extract_followers_count(entry)
                            if followers:
                                user_info['followers_count'] = followers
                                
                    except (KeyError, AttributeError):
                        continue
                
                # Handle profile-conversation entries (contains multiple tweets)
                elif entry_id.startswith('profile-conversation-'):
                    try:
                        items = entry.get('content', {}).get('items', [])
                        for item in items:
                            try:
                                item_content = item.get('item', {}).get('itemContent', {})
                                tweet_result = (
                                    item_content.get('tweet_results', {}).get('result', {})
                                )
                                
                                if tweet_result:
                                    tweet_data = self._parse_tweet(tweet_result, username)
                                    
                                    if tweet_data:
                                        # Filter by author during pagination
                                        if (tweet_data.get('author') or '').lower() == username:
                                            tweets.append(tweet_data)
                                            tweets_found += 1
                            except (KeyError, AttributeError):
                                continue
                    except (KeyError, AttributeError):
                        pass
                
                # Check tweet limit
                if len(tweets) >= tweet_limit:
                    bt.logging.debug(f"Reached tweet limit ({tweet_limit}) for @{username}")
                    cursor = None
                    break
            
            if not cursor:
                break
            
            time.sleep(self.rate_limit_delay)  # Rate limiting
        
        return tweets, user_info, api_fetch_succeeded
