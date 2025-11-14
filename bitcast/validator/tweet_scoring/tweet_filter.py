"""
Tweet content filtering for tweet scoring.

Filters tweets by language and tweet type.
"""

from typing import Dict, List, Optional, Tuple
import bittensor as bt


class TweetFilter:
    """Filters tweets by language, type, optional tag, and optional quoted tweet ID."""
    
    def __init__(self, language: Optional[str] = None, tag: Optional[str] = None, qrt: Optional[str] = None):
        """
        Initialize filter with pool configuration.
        
        Args:
            language: Optional language code (e.g., 'en', 'zh', 'fr')
                     If None, all languages accepted
            tag: Optional tag/string to filter by (e.g., '#bitcast', '@elon')
                 If None or empty string, all tweets accepted
            qrt: Optional quoted tweet ID to filter by (e.g., '1983210945288569177')
                 If None or empty string, all tweets accepted
                 If specified, only tweets that quote this specific tweet ID are accepted
        """
        self.language = language
        self.tag = tag.lower() if tag else None
        self.qrt = qrt
        
        bt.logging.debug(
            f"TweetFilter initialized: language={'any' if language is None else language}, "
            f"tag={'any' if tag is None else tag}, "
            f"qrt={'any' if qrt is None else qrt}"
        )
    
    def matches_tag(self, tweet_text: str) -> bool:
        """
        Check if tweet contains the required tag.
        
        Case-insensitive substring matching.
        
        Args:
            tweet_text: Tweet text content
            
        Returns:
            True if tag matches or no tag requirement
        """
        # No tag requirement - accept all (None or empty string)
        if not self.tag:
            return True
        
        # Check if tag appears in tweet (case-insensitive)
        return self.tag in tweet_text.lower()
    
    def matches_qrt(self, tweet: Dict) -> bool:
        """
        Check if tweet quoted the required tweet ID.
        
        Args:
            tweet: Tweet dictionary with 'quoted_tweet_id' field
            
        Returns:
            True if QRT matches or no QRT requirement
        """
        # No QRT requirement - accept all (None or empty string)
        if not self.qrt:
            return True
        
        # Check if tweet quoted the specific tweet ID
        quoted_tweet_id = tweet.get('quoted_tweet_id')
        return quoted_tweet_id == self.qrt
    
    def matches_language(self, tweet: Dict) -> bool:
        """
        Check if tweet matches language requirement.
        
        Permissive: If pool has no language requirement, accepts all.
        If pool has language requirement but tweet language is missing or
        'und' (undefined), accepts the tweet (permissive per BA).
        
        Args:
            tweet: Tweet dictionary with 'lang' field
            
        Returns:
            True if tweet matches language requirement
        """
        # No language requirement - accept all
        if self.language is None:
            return True
        
        tweet_lang = tweet.get('lang', '')
        
        # Permissive: accept undefined/missing language
        if not tweet_lang or tweet_lang == 'und':
            return True
        
        # Check exact match
        return tweet_lang == self.language
    
    def is_scoreable_tweet_type(self, tweet: Dict) -> bool:
        """
        Check if tweet is a scoreable type.
        
        Scoreable: original tweets and quote tweets
        Not scoreable: pure retweets
        
        Args:
            tweet: Tweet dictionary with 'text' field
            
        Returns:
            True if tweet should be scored
        """
        text = tweet.get('text', '')
        
        # Exclude pure retweets (start with "RT @")
        if text.startswith('RT @'):
            return False
        
        return True
    
    def filter_tweets(self, tweets: List[Dict]) -> List[Dict]:
        """
        Apply all filters to tweet list.
        
        Filters by:
        1. Tweet type (exclude pure RTs)
        2. Language (if specified)
        3. Tag (if specified)
        4. QRT (if specified)
        
        Args:
            tweets: List of tweet dictionaries
            
        Returns:
            Filtered list of tweets
        """
        filtered = []
        
        for tweet in tweets:
            # Filter 1: Tweet type
            if not self.is_scoreable_tweet_type(tweet):
                continue
            
            # Filter 2: Language
            if not self.matches_language(tweet):
                continue
            
            # Filter 3: Tag
            if not self.matches_tag(tweet.get('text', '')):
                continue
            
            # Filter 4: QRT
            if not self.matches_qrt(tweet):
                continue
            
            filtered.append(tweet)
        
        filter_desc = "type + language"
        if self.tag:
            filter_desc += f" + tag({self.tag})"
        if self.qrt:
            filter_desc += f" + qrt({self.qrt})"
        bt.logging.info(f"Filtered {len(tweets)} → {len(filtered)} tweets ({filter_desc})")
        
        return filtered

