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
from bitcast.validator.utils.twitter_validators import is_valid_twitter_username


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
                
                if isinstance(data, (dict, list)):
                    return data, None
                
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
    
    def _parse_tweet(self, desearch_data: Dict, username: Optional[str] = None) -> Optional[Dict]:
        """
        Parse a Desearch.ai tweet into normalized format.

        Args:
            desearch_data: Raw tweet object from Desearch.ai API
            username: Known author username (timeline context). If None, the author
                      is extracted from the tweet's embedded user object (search context).

        Returns:
            Normalized tweet dict or None if parsing fails
        """
        try:
            tweet_id = str(desearch_data.get('id', ''))
            if not tweet_id:
                return None

            text = desearch_data.get('text', '')
            if not text:
                return None

            # Author resolution: use provided username, or extract from embedded user object
            if username:
                author = username.lower()
            else:
                user_data = desearch_data.get('user', {})
                author = (user_data.get('username', '') or '').lower() if user_data else ''
                if not author or not is_valid_twitter_username(author):
                    candidates = [
                        (user_data.get('screen_name', '') or '').lower(),
                        (desearch_data.get('username', '') or '').lower(),
                        (desearch_data.get('screen_name', '') or '').lower(),
                    ]
                    author = next(
                        (c for c in candidates if c and is_valid_twitter_username(c)),
                        ''
                    )
                if not author:
                    return None

            created_at = self._convert_iso_to_twitter_date(desearch_data.get('created_at', ''))

            # Engagement metrics
            like_count = desearch_data.get('like_count', 0)
            retweet_count = desearch_data.get('retweet_count', 0)
            reply_count = desearch_data.get('reply_count', 0)
            quote_count = desearch_data.get('quote_count', 0)
            bookmark_count = desearch_data.get('bookmark_count', 0)
            views_count = desearch_data.get('view_count', desearch_data.get('views_count', 0))

            # Retweet info
            retweeted_user = None
            retweeted_tweet_id = None
            if desearch_data.get('is_retweet') and desearch_data.get('retweet'):
                retweet_data = desearch_data['retweet']
                retweeted_tweet_id = str(retweet_data.get('id', ''))
                retweet_user = retweet_data.get('user', {})
                if retweet_user:
                    rt_username = retweet_user.get('username', '').lower()
                    if is_valid_twitter_username(rt_username):
                        retweeted_user = rt_username

            # Quote info
            quoted_user = None
            quoted_tweet_id = desearch_data.get('quoted_status_id')
            if quoted_tweet_id:
                quoted_tweet_id = str(quoted_tweet_id)
                if desearch_data.get('quote') and desearch_data['quote'].get('user'):
                    qt_username = desearch_data['quote']['user'].get('username', '').lower()
                    if is_valid_twitter_username(qt_username):
                        quoted_user = qt_username

            # Tagged accounts
            tagged_accounts = []
            entities = desearch_data.get('entities', {})
            if entities:
                user_mentions = entities.get('user_mentions', [])
                if isinstance(user_mentions, list):
                    tagged_accounts = [
                        m.get('screen_name', '').lower()
                        for m in user_mentions
                        if m.get('screen_name') and is_valid_twitter_username(m.get('screen_name'))
                    ]

            # Reply info
            in_reply_to_status_id = desearch_data.get('in_reply_to_status_id')
            if in_reply_to_status_id:
                in_reply_to_status_id = str(in_reply_to_status_id)
            in_reply_to_user = None
            if desearch_data.get('in_reply_to_screen_name'):
                reply_username = desearch_data.get('in_reply_to_screen_name', '').lower()
                if is_valid_twitter_username(reply_username):
                    in_reply_to_user = reply_username

            return {
                'tweet_id': tweet_id,
                'created_at': created_at,
                'text': text,
                'author': author,
                'tagged_accounts': tagged_accounts,
                'retweeted_user': retweeted_user,
                'retweeted_tweet_id': retweeted_tweet_id,
                'quoted_user': quoted_user,
                'quoted_tweet_id': quoted_tweet_id,
                'lang': desearch_data.get('lang', 'und'),
                'favorite_count': like_count,
                'retweet_count': retweet_count,
                'reply_count': reply_count,
                'quote_count': quote_count,
                'bookmark_count': bookmark_count,
                'views_count': views_count,
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
        Fetch tweets from a Desearch.ai timeline endpoint with cursor-based pagination.

        /twitter/user/posts returns {"user": {...}, "tweets": [...], "next_cursor": "..."}
        /twitter/replies returns a plain list with no cursor (single page only).

        Args:
            endpoint_path: Desearch.ai endpoint path (e.g., "/twitter/user/posts")
            username: Twitter username to fetch tweets for
            tweet_limit: Maximum number of tweets to fetch
            incremental_cutoff: Stop fetching tweets older than this date
            param_name: Username param name ("username" for posts, "user" for replies)

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
        cursor = None
        max_pages = 20  # Safety cap (~20 tweets per page → up to 400 tweets)
        pages_fetched = 0

        try:
            for page in range(max_pages):
                if len(tweets) >= tweet_limit:
                    break

                params = {param_name: username, "count": 20}
                if cursor:
                    params["cursor"] = cursor

                data, error = self._make_api_request(url, params)
                if error:
                    if "401" in str(error) or "Unauthorized" in str(error):
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
                    if page == 0:
                        return tweets, user_info, False
                    break

                api_fetch_succeeded = True
                next_cursor = None

                # /twitter/user/posts → {"user": {...}, "tweets": [...], "next_cursor": "..."}
                # /twitter/replies   → plain list, no cursor
                if isinstance(data, dict) and 'tweets' in data:
                    tweet_list = data.get('tweets', [])
                    next_cursor = data.get('next_cursor')
                    if pages_fetched == 0:
                        user_data = data.get('user', {})
                        if user_data:
                            user_info['followers_count'] = user_data.get('followers_count', 0)
                elif isinstance(data, list):
                    tweet_list = data
                    next_cursor = None  # No pagination available for list responses
                else:
                    bt.logging.warning(
                        f"Unexpected Desearch.ai response format for @{username} (page {page + 1})"
                    )
                    break

                if not tweet_list:
                    bt.logging.debug(f"No more tweets for @{username} (page {page + 1})")
                    break

                page_tweets_count = 0
                reached_cutoff = False

                for tweet_data in tweet_list:
                    parsed_tweet = self._parse_tweet(tweet_data, username)
                    if not parsed_tweet:
                        continue

                    try:
                        tweet_date_str = parsed_tweet.get('created_at', '')
                        if tweet_date_str:
                            tweet_date = datetime.strptime(
                                tweet_date_str, '%a %b %d %H:%M:%S %z %Y'
                            )
                            cutoff_with_tz = incremental_cutoff.replace(tzinfo=tweet_date.tzinfo)
                            if tweet_date < cutoff_with_tz:
                                reached_cutoff = True
                                break
                    except (ValueError, AttributeError):
                        pass

                    if user_info['followers_count'] == 0 and tweet_data.get('user'):
                        user_info['followers_count'] = tweet_data['user'].get('followers_count', 0)

                    tweets.append(parsed_tweet)
                    page_tweets_count += 1

                    if len(tweets) >= tweet_limit:
                        break

                pages_fetched += 1
                bt.logging.debug(
                    f"Fetched {page_tweets_count} tweets from Desearch.ai for @{username} "
                    f"(page {pages_fetched}, total: {len(tweets)})"
                )

                if reached_cutoff or len(tweets) >= tweet_limit or not next_cursor:
                    break

                cursor = next_cursor
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
    
    def search_tweets(
        self,
        query: str,
        max_results: int = 100,
        sort: str = "latest"
    ) -> Tuple[List[Dict], bool]:
        """
        Search for tweets using X-style query syntax via Desearch.ai.

        Args:
            query: Search query string with X-style operators
            max_results: Maximum number of tweets to return (default: 100)
            sort: Sort order - "latest" or "top" (default: "latest")

        Returns:
            Tuple of (tweets_list, api_succeeded)
        """
        url = f"{self.base_url}/twitter"

        tweets = []
        api_succeeded = False
        sort_param = "Latest" if sort == "latest" else "Top"

        params = {"query": query, "sort": sort_param, "count": 100}
        data, error = self._make_api_request(url, params)
        if error:
            bt.logging.warning(f"Desearch search API error: {error}")
        else:
            api_succeeded = True
            tweet_list = data if isinstance(data, list) else data.get('tweets', [])
            for tweet_data in tweet_list:
                parsed_tweet = self._parse_tweet(tweet_data)
                if parsed_tweet:
                    tweets.append(parsed_tweet)
                    if len(tweets) >= max_results:
                        break

        bt.logging.info(f"Search returned {len(tweets)} tweets for query: {query[:50]}...")
        return tweets, api_succeeded
    
    def get_retweeters(
        self,
        tweet_id: str,
        max_results: int = 100
    ) -> Tuple[List[str], bool]:
        """
        Get list of usernames who retweeted a specific tweet via Desearch.ai.

        Paginates using next_cursor until max_results is reached or no more pages.
        Response format: {"users": [...], "next_cursor": "..."}

        Args:
            tweet_id: The tweet ID to get retweeters for
            max_results: Maximum number of retweeters to return (default: 100)

        Returns:
            Tuple of (usernames_list, api_succeeded)
        """
        url = f"{self.base_url}/twitter/post/retweeters"

        usernames = []
        api_succeeded = False
        cursor = None
        max_pages = 5

        try:
            for page in range(max_pages):
                if len(usernames) >= max_results:
                    break

                params = {"id": tweet_id}
                if cursor:
                    params["cursor"] = cursor

                data, error = self._make_api_request(url, params)
                if error:
                    bt.logging.warning(f"Desearch retweeters API error for tweet {tweet_id}: {error}")
                    if page == 0:
                        return usernames, False
                    break

                api_succeeded = True

                user_list = data.get('users', []) if isinstance(data, dict) else []
                next_cursor = data.get('next_cursor') if isinstance(data, dict) else None

                for user in user_list:
                    username = (user.get('username', '') or user.get('screen_name', '')).lower()
                    if username and is_valid_twitter_username(username):
                        usernames.append(username)
                        if len(usernames) >= max_results:
                            break

                if not next_cursor or not user_list:
                    break

                cursor = next_cursor
                time.sleep(self.rate_limit_delay)

        except Exception as e:
            bt.logging.error(f"Desearch retweeters API error for tweet {tweet_id}: {e}")

        bt.logging.debug(f"Found {len(usernames)} retweeters for tweet {tweet_id}")
        return usernames, api_succeeded
    
    def fetch_post_replies(
        self,
        tweet_id: str,
        max_results: int = 100
    ) -> Tuple[List[Dict], bool]:
        """
        Fetch replies to a specific tweet via Desearch.ai /twitter/replies/post.

        Uses since:{today} filtering to pull only the latest replies.
        """
        from datetime import date
        url = f"{self.base_url}/twitter/replies/post"
        params = {
            "post_id": tweet_id,
            "query": f"since:{date.today().isoformat()}",
            "count": min(max_results, 100),
        }

        tweets = []
        data, error = self._make_api_request(url, params)
        if error:
            bt.logging.warning(f"Desearch replies API error for tweet {tweet_id}: {error}")
            return tweets, False

        tweet_list = data if isinstance(data, list) else data.get('tweets', [])
        for tweet_data in tweet_list:
            parsed = self._parse_tweet(tweet_data)
            if parsed:
                tweets.append(parsed)
                if len(tweets) >= max_results:
                    break

        bt.logging.info(f"Fetched {len(tweets)} replies for tweet {tweet_id}")
        return tweets, True

    def fetch_tweet_by_id(
        self,
        tweet_id: str
    ) -> Tuple[Optional[Dict], bool]:
        """
        Fetch a single tweet by ID via Desearch.ai /twitter/post endpoint.
        
        Args:
            tweet_id: The tweet ID to fetch
        
        Returns:
            Tuple of (normalized_tweet, api_succeeded)
        """
        url = f"{self.base_url}/twitter/post"
        params = {"id": tweet_id}
        
        for attempt in range(self.max_retries):
            try:
                response = requests.get(url, headers=self.headers, params=params, timeout=30)
                
                if response.status_code in [429, 500, 502, 503, 504]:
                    bt.logging.warning(
                        f"Desearch post API error {response.status_code} for tweet {tweet_id} "
                        f"(attempt {attempt + 1}/{self.max_retries})"
                    )
                    if attempt < self.max_retries - 1:
                        time.sleep(self.retry_delay)
                    continue
                
                response.raise_for_status()
                data = response.json()
                
                if not data:
                    bt.logging.warning(f"Empty response for tweet {tweet_id}")
                    return None, True
                
                tweet_data = data
                if isinstance(data, list):
                    tweet_data = data[0] if data else None
                if not tweet_data:
                    return None, True
                
                parsed = self._parse_search_tweet(tweet_data)
                if parsed:
                    bt.logging.info(f"Fetched tweet {tweet_id} by @{parsed.get('author', '?')}")
                else:
                    bt.logging.warning(f"Failed to parse tweet {tweet_id}")
                
                return parsed, True
                
            except Exception as e:
                bt.logging.error(f"Desearch post API error for tweet {tweet_id}: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
        
        return None, False
