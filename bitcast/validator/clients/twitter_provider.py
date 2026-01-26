"""
Abstract base class for Twitter API providers.

Defines the interface that all Twitter API provider implementations must follow.
This enables swappable API backends (Desearch.ai, RapidAPI, etc.) with consistent behavior.
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple
from datetime import datetime


class TwitterProvider(ABC):
    """
    Interface that all Twitter API providers must implement.
    
    Providers handle API-specific communication, response parsing, and pagination
    while producing a standardized tweet format for the TwitterClient.
    """
    
    @abstractmethod
    def __init__(self, api_key: str, max_retries: int = 3, 
                 retry_delay: float = 2.0, rate_limit_delay: float = 1.0):
        """
        Initialize provider with credentials and configuration.
        
        Args:
            api_key: API key for the provider (format varies by provider)
            max_retries: Maximum number of API request retries (default: 3)
            retry_delay: Delay in seconds between retries (default: 2.0)
            rate_limit_delay: Delay in seconds between API calls (default: 1.0)
        """
        pass
    
    @abstractmethod
    def fetch_user_tweets(
        self, 
        username: str, 
        incremental_cutoff: datetime,
        tweet_limit: int,
        posts_only: bool
    ) -> Tuple[List[Dict], Optional[Dict], bool]:
        """
        Fetch tweets for a user with pagination.
        
        This is the main method that TwitterClient calls to retrieve tweets.
        Implementations must handle:
        - API requests with pagination
        - Response parsing into normalized format
        - Date-based cutoff for incremental fetches
        - Dual-endpoint mode (posts + replies) vs posts-only mode
        
        Args:
            username: Twitter username to fetch tweets for (lowercased)
            incremental_cutoff: Stop fetching tweets older than this date
            tweet_limit: Maximum number of tweets to fetch per endpoint
            posts_only: If True, fetch only from posts endpoint (faster).
                       If False, fetch from both posts and replies endpoints.
        
        Returns:
            Tuple of (tweets_list, user_info, api_succeeded):
            - tweets_list: List of tweet dictionaries in normalized format
            - user_info: Dict with 'username' and 'followers_count', or None
            - api_succeeded: True if API call succeeded, False otherwise
            
        Normalized Tweet Format:
            {
                'tweet_id': str,              # Unique tweet identifier
                'created_at': str,            # Twitter date format: "Wed Jan 01 00:00:00 +0000 2023"
                'text': str,                  # Tweet text content
                'author': str,                # Tweet author username (lowercased)
                'tagged_accounts': List[str], # Mentioned usernames (lowercased)
                'retweeted_user': str|None,   # Original author if retweet
                'retweeted_tweet_id': str|None, # Original tweet ID if retweet
                'quoted_user': str|None,      # Quoted tweet author if quote
                'quoted_tweet_id': str|None,  # Quoted tweet ID if quote
                'lang': str,                  # Language code (e.g., 'en', 'und')
                'favorite_count': int,        # Like count
                'retweet_count': int,         # Retweet count
                'reply_count': int,           # Reply count
                'quote_count': int,           # Quote count
                'bookmark_count': int,        # Bookmark count
                'in_reply_to_status_id': str|None, # Parent tweet ID if reply
                'in_reply_to_user': str|None  # Parent tweet author if reply
            }
            
        User Info Format:
            {
                'username': str,         # Username (lowercased)
                'followers_count': int   # Number of followers
            }
        """
        pass
    
    @abstractmethod
    def validate_api_key(self) -> bool:
        """
        Validate that the API key is properly formatted for this provider.
        
        Different providers have different key format requirements:
        - Desearch.ai: Must start with 'dt_$'
        - RapidAPI: Any non-empty string
        
        Returns:
            True if API key is valid format, False otherwise
        """
        pass
