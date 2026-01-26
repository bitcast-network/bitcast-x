"""
Desearch.ai Twitter API provider implementation.

Implements the TwitterProvider interface for Desearch.ai API access.
"""
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import bittensor as bt

from .twitter_provider import TwitterProvider


class DesearchProvider(TwitterProvider):
    """
    Desearch.ai implementation of Twitter API access.
    
    Handles Desearch.ai-specific API communication, response parsing,
    and pagination logic.
    """
    
    def __init__(self, api_key: str, max_retries: int = 3,
                 retry_delay: float = 2.0, rate_limit_delay: float = 1.0):
        """
        Initialize Desearch.ai provider.
        
        Args:
            api_key: Desearch.ai API key (format: dt_$YOUR_KEY)
            max_retries: Maximum number of API request retries
            retry_delay: Delay in seconds between retries
            rate_limit_delay: Delay in seconds between API calls
        """
        self.api_key = api_key.strip()
        self.base_url = "https://api.desearch.ai"
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.rate_limit_delay = rate_limit_delay
        
        self.headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json"
        }
    
    def validate_api_key(self) -> bool:
        """
        Validate Desearch API key format (dt_$...).
        
        Returns:
            True if key starts with 'dt_$', False otherwise
        """
        if not self.api_key:
            return False
        if not self.api_key.startswith('dt_$'):
            return False
        return True
    
    def fetch_user_tweets(
        self,
        username: str,
        incremental_cutoff: datetime,
        tweet_limit: int,
        posts_only: bool
    ) -> Tuple[List[Dict], Optional[Dict], bool]:
        """
        Fetch tweets for a user using Desearch.ai API.
        
        Args:
            username: Twitter username (lowercased)
            incremental_cutoff: Stop fetching tweets older than this date
            tweet_limit: Maximum number of tweets to fetch per endpoint
            posts_only: If True, fetch only from posts endpoint.
                       If False, fetch from both posts and replies endpoints.
        
        Returns:
            Tuple of (tweets_list, user_info, api_succeeded)
        """
        all_tweets = []
        user_info = None
        api_fetch_succeeded = False
        
        # Select endpoints based on posts_only mode
        if posts_only:
            # Posts-only mode: faster, uses less quota
            endpoints = [
                ("/twitter/user/posts", "posts", "username")
            ]
        else:
            # Dual-endpoint mode: complete tweet coverage (includes replies)
            endpoints = [
                ("/twitter/replies", "replies", "user"),
                ("/twitter/user/posts", "posts", "username")
            ]
        
        # Fetch from endpoint(s) in parallel
        with ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
            future_to_endpoint = {
                executor.submit(
                    self._fetch_from_endpoint,
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
                        
                    bt.logging.debug(
                        f"Fetched {len(tweets)} tweets from Desearch.ai "
                        f"{endpoint_name} endpoint for @{username}"
                    )
                except Exception as e:
                    bt.logging.warning(
                        f"Failed to fetch from Desearch.ai {endpoint_name} "
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
                f"Desearch.ai endpoints for @{username}, {len(tweets)} unique "
                f"({overlap_count} duplicates removed)"
            )
        elif len(all_tweets) != len(tweets):
            overlap_count = len(all_tweets) - len(tweets)
            bt.logging.info(
                f"Fetched {len(all_tweets)} tweets from Desearch.ai for @{username}, "
                f"{len(tweets)} unique ({overlap_count} duplicates removed)"
            )
        else:
            bt.logging.info(f"Fetched {len(tweets)} tweets from Desearch.ai for @{username}")
        
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
                
                # Handle Desearch.ai response format: {"user": {...}, "tweets": [...]}
                if isinstance(data, dict) and 'tweets' in data:
                    return data, None
                
                # Handle Desearch.ai response format (list of tweets) - legacy
                if isinstance(data, list):
                    return data, None
                
                # Handle Desearch.ai response wrapped in 'data' key
                if isinstance(data, dict) and 'data' in data and isinstance(data['data'], list):
                    return data['data'], None
                
                # Handle alternative response formats from Desearch.ai API
                if 'data' in data and 'user' in data['data']:
                    return data, None
                elif 'user' in data:
                    # Normalize response structure for consistency
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
    
    def _parse_tweet(self, desearch_data: Dict, username: str) -> Optional[Dict]:
        """
        Parse Desearch.ai API response into normalized tweet format.
        
        Args:
            desearch_data: Raw tweet object from Desearch.ai API
            username: Expected username (for validation)
            
        Returns:
            Normalized tweet dict or None if parsing fails
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
                    tagged_accounts = [
                        m.get('screen_name', '').lower()
                        for m in user_mentions
                        if m.get('screen_name')
                    ]
            
            # Extract reply info
            in_reply_to_status_id = desearch_data.get('in_reply_to_status_id')
            if in_reply_to_status_id:
                in_reply_to_status_id = str(in_reply_to_status_id)
            in_reply_to_user = (
                desearch_data.get('in_reply_to_screen_name', '').lower()
                if desearch_data.get('in_reply_to_screen_name')
                else None
            )
            
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
        
        Args:
            iso_date: ISO 8601 formatted date string
            
        Returns:
            Twitter-formatted date string or original if conversion fails
            
        Example:
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
    
    def _fetch_from_endpoint(
        self,
        endpoint_path: str,
        username: str,
        tweet_limit: int,
        incremental_cutoff: datetime,
        param_name: str = "username"
    ) -> Tuple[List[Dict], Optional[Dict], bool]:
        """
        Fetch tweets from Desearch.ai API endpoint with pagination support.
        
        Args:
            endpoint_path: Desearch.ai endpoint path (e.g., "/twitter/user/posts")
            username: Twitter username to fetch tweets for
            tweet_limit: Maximum number of tweets to fetch
            incremental_cutoff: Date cutoff for pagination stopping
            param_name: Parameter name to use ("username" for posts, "user" for replies)
        
        Returns:
            Tuple of (tweets_list, user_info, api_succeeded)
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
                        masked_auth = (
                            auth_header[:15] + '...' + auth_header[-5:]
                            if len(auth_header) > 20
                            else '***'
                        )
                        bt.logging.error(
                            f"Desearch.ai API 401 Unauthorized for @{username}. "
                            f"Auth header format: {masked_auth} (key length: {len(self.api_key)}). "
                            f"Check if DESEARCH_API_KEY in .env is correct."
                        )
                    else:
                        bt.logging.error(
                            f"Desearch.ai API failed for @{username} (page {page + 1}): {error}"
                        )
                    
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
                    bt.logging.warning(
                        f"Unexpected Desearch.ai response format for @{username} (page {page + 1})"
                    )
                    break
                
                # If no tweets returned, we've reached the end
                if not tweet_list:
                    bt.logging.debug(f"No more tweets for @{username} (page {page + 1})")
                    break
                
                # Process tweets from this page
                page_tweets_count = 0
                reached_cutoff = False
                
                for tweet_data in tweet_list:
                    parsed_tweet = self._parse_tweet(tweet_data, username)
                    if not parsed_tweet:
                        continue
                    
                    # Check date cutoff
                    try:
                        tweet_date_str = parsed_tweet.get('created_at', '')
                        if tweet_date_str:
                            tweet_date = datetime.strptime(
                                tweet_date_str, '%a %b %d %H:%M:%S %z %Y'
                            )
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
