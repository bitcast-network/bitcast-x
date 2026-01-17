"""
Engagement analyzer for detecting RT/QRT relationships.

Identifies which accounts retweeted or quote-tweeted specific tweets,
with self-engagement exclusion and optional participant exclusion.
"""

from typing import Dict, List, Optional, Set
import bittensor as bt


class EngagementAnalyzer:
    """Analyzes tweet engagement patterns (retweets and quotes)."""
    
    def get_engagements_for_tweet(
        self,
        original_tweet: Dict,
        all_tweets: List[Dict],
        considered_accounts_map: Dict[str, float],
        excluded_engagers: Optional[Set[str]] = None
    ) -> Dict[str, str]:
        """
        Get all engagements for a tweet from considered accounts.
        
        Args:
            original_tweet: The tweet to find engagements for
                           Must have 'author' and 'tweet_id' fields
            all_tweets: All tweets to search (should include 'author' field)
            considered_accounts_map: Map of username -> influence_score
                                    for filtering
            excluded_engagers: Optional set of usernames (lowercase) whose engagements
                              should be excluded. Used to prevent brief participants
                              from contributing to each other's scores.
            
        Returns:
            Dict mapping username -> engagement_type
            where engagement_type is "retweet" or "quote"
            If user both RTs and quotes, only "quote" is returned (higher priority)
        """
        tweet_id = original_tweet.get('tweet_id', '')
        author = original_tweet.get('author', '').lower()
        
        if not tweet_id or not author:
            bt.logging.warning("Tweet missing ID or author, cannot analyze engagement")
            return {}
        
        engagements = {}
        author_lower = author.lower()
        
        # Scan all tweets for engagements
        for tweet in all_tweets:
            tweet_author = tweet.get('author', '').lower()
            
            # Skip if tweet has no author
            if not tweet_author:
                continue
            
            # Skip self-engagement
            if tweet_author == author_lower:
                continue
            
            # Skip if not from a considered account
            if tweet_author not in considered_accounts_map:
                continue
            
            # Skip engagements from excluded accounts (e.g., other brief participants)
            if excluded_engagers and tweet_author in excluded_engagers:
                continue
            
            # Check for retweet - match by specific tweet ID
            retweeted_tweet_id = tweet.get('retweeted_tweet_id')
            if retweeted_tweet_id and retweeted_tweet_id == tweet_id:
                engagements[tweet_author] = "retweet"
            
            # Check for quote - match by specific tweet ID
            quoted_tweet_id = tweet.get('quoted_tweet_id')
            if quoted_tweet_id and quoted_tweet_id == tweet_id:
                engagements[tweet_author] = "quote"
        
        bt.logging.debug(
            f"Tweet {tweet_id[:10]}... by @{author}: "
            f"{len(engagements)} engagements from considered accounts"
        )
        
        return engagements

