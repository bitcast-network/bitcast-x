"""
Accumulative scoring store for tweet scoring.

Stores tweets and engagement data discovered from API searches.
Once a tweet or engagement is found, it is retained for 90 days.

Key design:
- Tweets are stored by tweet_id with all fields
- Engagements (RTs and QRTs) are stored per tweet, accumulating over time
- 90-day expiry - data persists for 90 days from last update
- Always make fresh API calls and merge results into the store
"""

import os
import atexit
from datetime import datetime
from threading import Lock
from typing import Dict, List, Optional, Set
from diskcache import Cache
import bittensor as bt

from bitcast.validator.utils.config import CACHE_DIRS, CACHE_EXPIRY_SECONDS


# Store directory alongside existing twitter cache
SCORING_STORE_DIR = os.path.join(CACHE_DIRS.get("twitter", "cache/twitter"), "tweet_store")


class ScoringStore:
    """
    Accumulative store for tweet scoring data.
    
    Stores tweets and engagements with 90-day expiry. Each scoring run:
    1. Makes fresh API calls
    2. Merges new tweets into the store (upsert)
    3. Queries the store for tweets matching brief criteria
    4. Merges new engagement data into the store
    5. Queries the store for all known engagements
    """
    
    _instance = None
    _lock = Lock()
    _cache: Cache = None
    
    @classmethod
    def get_instance(cls) -> 'ScoringStore':
        """Thread-safe singleton access."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    def __init__(self):
        if ScoringStore._cache is None:
            os.makedirs(SCORING_STORE_DIR, exist_ok=True)
            ScoringStore._cache = Cache(
                directory=SCORING_STORE_DIR,
                size_limit=2e9,  # 2GB
                disk_min_file_size=0,
                disk_pickle_protocol=4,
            )
            atexit.register(self.cleanup)
            bt.logging.info(f"ScoringStore initialized at: {SCORING_STORE_DIR}")
    
    @classmethod
    def cleanup(cls):
        """Clean up resources."""
        if cls._cache is not None:
            with cls._lock:
                if cls._cache is not None:
                    cls._cache.close()
                    cls._cache = None
    
    # -------------------------------------------------------------------------
    # Tweet storage
    # -------------------------------------------------------------------------
    
    def _tweet_key(self, tweet_id: str) -> str:
        return f"tweet:{tweet_id}"
    
    def store_tweet(self, tweet: Dict) -> bool:
        """
        Store or update a tweet. Merges new data with existing.
        
        Engagement stats are updated to the latest values.
        Other fields are set if not already present.
        
        Args:
            tweet: Normalized tweet dict from API (must have 'tweet_id')
            
        Returns:
            True if this was a new tweet, False if updated existing
        """
        tweet_id = tweet.get('tweet_id')
        if not tweet_id:
            return False
        
        key = self._tweet_key(tweet_id)
        existing = self._cache.get(key)
        
        if existing is None:
            # New tweet - store all fields
            record = {
                'tweet_id': tweet_id,
                'author': tweet.get('author', '').lower(),
                'text': tweet.get('text', ''),
                'created_at': tweet.get('created_at', ''),
                'lang': tweet.get('lang', 'und'),
                'quoted_tweet_id': tweet.get('quoted_tweet_id'),
                'quoted_user': tweet.get('quoted_user'),
                'retweeted_user': tweet.get('retweeted_user'),
                'retweeted_tweet_id': tweet.get('retweeted_tweet_id'),
                'tagged_accounts': tweet.get('tagged_accounts', []),
                'in_reply_to_status_id': tweet.get('in_reply_to_status_id'),
                'in_reply_to_user': tweet.get('in_reply_to_user'),
                # Engagement stats (updated on each merge)
                'favorite_count': tweet.get('favorite_count', 0),
                'retweet_count': tweet.get('retweet_count', 0),
                'reply_count': tweet.get('reply_count', 0),
                'quote_count': tweet.get('quote_count', 0),
                'bookmark_count': tweet.get('bookmark_count', 0),
                'views_count': tweet.get('views_count', 0),
                # Metadata
                'first_seen': datetime.now().isoformat(),
                'last_updated': datetime.now().isoformat(),
            }
            # 90-day expiry from config
            self._cache.set(key, record, expire=CACHE_EXPIRY_SECONDS)
            return True
        else:
            # Existing tweet - update engagement stats and timestamp
            existing['favorite_count'] = tweet.get('favorite_count', existing.get('favorite_count', 0))
            existing['retweet_count'] = tweet.get('retweet_count', existing.get('retweet_count', 0))
            existing['reply_count'] = tweet.get('reply_count', existing.get('reply_count', 0))
            existing['quote_count'] = tweet.get('quote_count', existing.get('quote_count', 0))
            existing['bookmark_count'] = tweet.get('bookmark_count', existing.get('bookmark_count', 0))
            existing['views_count'] = tweet.get('views_count', existing.get('views_count', 0))
            existing['last_updated'] = datetime.now().isoformat()
            self._cache.set(key, existing)
            return False
    
    def store_tweets(self, tweets: List[Dict]) -> Dict[str, int]:
        """
        Store multiple tweets, merging with existing data.
        
        Args:
            tweets: List of normalized tweet dicts
            
        Returns:
            Dict with 'new' and 'updated' counts
        """
        stats = {'new': 0, 'updated': 0}
        for tweet in tweets:
            is_new = self.store_tweet(tweet)
            if is_new:
                stats['new'] += 1
            else:
                stats['updated'] += 1
        return stats
    
    def get_tweet(self, tweet_id: str) -> Optional[Dict]:
        """Get a single tweet by ID."""
        return self._cache.get(self._tweet_key(tweet_id))
    
    def query_tweets(
        self,
        authors: Optional[Set[str]] = None,
        quoted_tweet_id: Optional[str] = None,
        tag: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[Dict]:
        """
        Query stored tweets matching criteria.
        
        All filters are ANDed together. Only provided filters are applied.
        
        Args:
            authors: Optional set of author usernames to include
            quoted_tweet_id: Optional - only tweets quoting this tweet ID
            tag: Optional - only tweets containing this tag/keyword
            start_date: Optional - only tweets on or after this date
            end_date: Optional - only tweets on or before this date
            
        Returns:
            List of matching tweet dicts
        """
        from datetime import timezone
        
        authors_lower = {a.lower() for a in authors} if authors else None
        tag_lower = tag.lower() if tag else None
        
        results = []
        
        for key in self._cache.iterkeys():
            if not key.startswith('tweet:'):
                continue
            
            tweet = self._cache.get(key)
            if tweet is None:
                continue
            
            # Filter by author
            if authors_lower and tweet.get('author', '').lower() not in authors_lower:
                continue
            
            # Filter by quoted_tweet_id
            if quoted_tweet_id and tweet.get('quoted_tweet_id') != quoted_tweet_id:
                continue
            
            # Filter by tag (case-insensitive substring match in text)
            if tag_lower and tag_lower not in tweet.get('text', '').lower():
                continue
            
            # Filter by date range
            if start_date or end_date:
                created_at = tweet.get('created_at', '')
                if created_at:
                    try:
                        tweet_date = datetime.strptime(created_at, '%a %b %d %H:%M:%S %z %Y')
                        tweet_date_utc = tweet_date.astimezone(timezone.utc)
                        
                        if start_date and tweet_date_utc < start_date:
                            continue
                        if end_date and tweet_date_utc > end_date:
                            continue
                    except ValueError:
                        # Include tweets with unparseable dates (permissive)
                        pass
            
            results.append(tweet)
        
        return results
    
    # -------------------------------------------------------------------------
    # Engagement storage (RTs and QRTs per tweet)
    # -------------------------------------------------------------------------
    
    def _engagement_key(self, tweet_id: str) -> str:
        return f"engagements:{tweet_id}"
    
    def store_retweeters(self, tweet_id: str, usernames: List[str]) -> Dict[str, int]:
        """
        Merge retweeters into the engagement record for a tweet.
        
        New retweeters are added; existing ones are preserved.
        
        Args:
            tweet_id: The tweet that was retweeted
            usernames: List of usernames who retweeted
            
        Returns:
            Dict with 'new' and 'total' counts
        """
        key = self._engagement_key(tweet_id)
        existing = self._cache.get(key) or {
            'tweet_id': tweet_id,
            'retweeters': {},
            'quoters': {},
        }
        
        new_count = 0
        for username in usernames:
            username_lower = username.lower()
            if username_lower not in existing['retweeters']:
                existing['retweeters'][username_lower] = {
                    'first_seen': datetime.now().isoformat()
                }
                new_count += 1
        
        existing['last_updated'] = datetime.now().isoformat()
        self._cache.set(key, existing, expire=CACHE_EXPIRY_SECONDS)

        return {'new': new_count, 'total': len(existing['retweeters'])}
    
    def store_quoters(self, tweet_id: str, qrt_tweets: List[Dict]) -> Dict[str, int]:
        """
        Merge quote-tweeters into the engagement record for a tweet.
        
        Stores both the quoter username and their quote tweet ID.
        
        Args:
            tweet_id: The tweet that was quoted
            qrt_tweets: List of quote tweet dicts (must have 'author' and 'tweet_id')
            
        Returns:
            Dict with 'new' and 'total' counts
        """
        key = self._engagement_key(tweet_id)
        existing = self._cache.get(key) or {
            'tweet_id': tweet_id,
            'retweeters': {},
            'quoters': {},
        }
        
        new_count = 0
        for qrt in qrt_tweets:
            author = qrt.get('author', '').lower()
            qrt_id = qrt.get('tweet_id', '')
            if author and author not in existing['quoters']:
                existing['quoters'][author] = {
                    'quote_tweet_id': qrt_id,
                    'first_seen': datetime.now().isoformat()
                }
                new_count += 1
        
        existing['last_updated'] = datetime.now().isoformat()
        self._cache.set(key, existing, expire=CACHE_EXPIRY_SECONDS)

        return {'new': new_count, 'total': len(existing['quoters'])}
    
    def get_engagements(self, tweet_id: str) -> Dict:
        """
        Get all known engagements for a tweet.
        
        Args:
            tweet_id: Tweet ID
            
        Returns:
            Dict with 'retweeters' and 'quoters' mappings, 
            or empty structure if none found
        """
        key = self._engagement_key(tweet_id)
        result = self._cache.get(key)
        if result:
            return result
        return {'tweet_id': tweet_id, 'retweeters': {}, 'quoters': {}}
    
    # -------------------------------------------------------------------------
    # Stats
    # -------------------------------------------------------------------------
    
    def get_stats(self) -> Dict[str, int]:
        """Get store statistics."""
        tweet_count = 0
        engagement_count = 0
        
        for key in self._cache.iterkeys():
            if key.startswith('tweet:'):
                tweet_count += 1
            elif key.startswith('engagements:'):
                engagement_count += 1
        
        return {
            'tweets': tweet_count,
            'engagement_records': engagement_count,
        }
