"""
Score calculator with weighted engagement scoring.

Calculates tweet scores by multiplying influence scores with engagement weights.
"""

from typing import Dict, List, Tuple
import bittensor as bt

from bitcast.validator.utils.config import (
    PAGERANK_RETWEET_WEIGHT,
    PAGERANK_QUOTE_WEIGHT,
    BASELINE_TWEET_SCORE_FACTOR
)
from .engagement_analyzer import EngagementAnalyzer


class ScoreCalculator:
    """Calculates weighted scores for tweets based on engagement."""
    
    def __init__(
        self,
        considered_accounts: Dict[str, float],
        retweet_weight: float = None,
        quote_weight: float = None
    ):
        """
        Initialize calculator with influence scores and weights.
        
        Args:
            considered_accounts: Map of username -> influence_score
            retweet_weight: Weight for retweets (default: PAGERANK_RETWEET_WEIGHT)
            quote_weight: Weight for quotes (default: PAGERANK_QUOTE_WEIGHT)
        """
        self.considered_accounts = considered_accounts
        self.retweet_weight = retweet_weight if retweet_weight is not None else PAGERANK_RETWEET_WEIGHT
        self.quote_weight = quote_weight if quote_weight is not None else PAGERANK_QUOTE_WEIGHT
        
        # Calculate minimum influence score from considered accounts
        # Used as fallback for accounts not in the social map
        if considered_accounts:
            self.min_influence_score = min(considered_accounts.values())
        else:
            self.min_influence_score = 0.0
        
        bt.logging.info(
            f"ScoreCalculator initialized: "
            f"{len(considered_accounts)} accounts, "
            f"RT weight={self.retweet_weight}, "
            f"Quote weight={self.quote_weight}, "
            f"Min influence score={self.min_influence_score:.6f}"
        )
    
    def calculate_tweet_score(
        self,
        engagements: Dict[str, str],
        author_influence_score: float
    ) -> Tuple[float, List[Dict]]:
        """
        Calculate weighted score for a tweet.
        
        Score = (author_influence_score × BASELINE_TWEET_SCORE_FACTOR) + Σ(influence_score × engagement_weight)
        
        All tweets get a baseline score from the author's influence, plus additional
        score from engagement (retweets and quotes).
        
        Args:
            engagements: Dict mapping username -> engagement_type ("retweet" or "quote")
            author_influence_score: The influence score of the tweet's author
            
        Returns:
            Tuple of (total_score, engagement_details)
            engagement_details is list of dicts with username, influence_score,
            engagement_type, and weighted_contribution
        """
        details = []
        
        # Start with baseline score from author's influence
        total_score = author_influence_score * BASELINE_TWEET_SCORE_FACTOR
        
        # Add engagement contributions
        for username, engagement_type in engagements.items():
            # Look up influence score
            influence_score = self.considered_accounts.get(username)
            
            if influence_score is None:
                bt.logging.warning(
                    f"Account @{username} not found in considered accounts, skipping"
                )
                continue
            
            # Calculate weighted contribution
            if engagement_type == "retweet":
                weight = self.retweet_weight
            elif engagement_type == "quote":
                weight = self.quote_weight
            else:
                bt.logging.warning(
                    f"Unknown engagement type '{engagement_type}' for @{username}, skipping"
                )
                continue
            
            contribution = influence_score * weight
            total_score += contribution
            
            details.append({
                'username': username,
                'influence_score': round(influence_score, 6),
                'engagement_type': engagement_type,
                'weighted_contribution': round(contribution, 6)
            })
        
        # Round final score
        total_score = round(total_score, 6)
        
        return total_score, details
    
    def score_tweets_batch(
        self,
        tweets: List[Dict],
        all_tweets: List[Dict],
        engagement_analyzer: EngagementAnalyzer
    ) -> List[Dict]:
        """
        Score a batch of tweets.
        
        Args:
            tweets: List of tweets to score (must have 'author' and 'tweet_id')
            all_tweets: All tweets to search for engagements
            engagement_analyzer: Analyzer to detect engagements
            
        Returns:
            List of scored tweet dicts with complete metadata
        """
        scored_tweets = []
        
        for tweet in tweets:
            # Get engagements for this tweet
            engagements = engagement_analyzer.get_engagements_for_tweet(
                tweet,
                all_tweets,
                self.considered_accounts
            )
            
            # Get author's influence score
            # Use minimum influence score from considered accounts as fallback
            author = tweet.get('author', '')
            author_influence_score = self.considered_accounts.get(author, self.min_influence_score)
            
            # Calculate score
            score, details = self.calculate_tweet_score(engagements, author_influence_score)
            
            # Separate retweets and quotes
            retweets = [d['username'] for d in details if d['engagement_type'] == 'retweet']
            quotes = [d['username'] for d in details if d['engagement_type'] == 'quote']
            
            # Build scored tweet object with engagement metrics
            tweet_id = tweet.get('tweet_id', '')
            
            scored_tweet = {
                'tweet_id': tweet_id,
                'author': author,
                'text': tweet.get('text', ''),
                'url': f"https://twitter.com/{author}/status/{tweet_id}",
                'created_at': tweet.get('created_at', ''),
                'lang': tweet.get('lang', 'und'),
                'score': score,
                'retweets': retweets,
                'quotes': quotes,
                # Preserve engagement metrics from Twitter API
                'favorite_count': tweet.get('favorite_count', 0),
                'retweet_count': tweet.get('retweet_count', 0),
                'reply_count': tweet.get('reply_count', 0),
                'quote_count': tweet.get('quote_count', 0),
                'bookmark_count': tweet.get('bookmark_count', 0)
            }
            
            # Include quoted_tweet_id if present (for QRT filtering transparency)
            if tweet.get('quoted_tweet_id'):
                scored_tweet['quoted_tweet_id'] = tweet['quoted_tweet_id']
            
            scored_tweets.append(scored_tweet)
        
        # Sort by score descending
        scored_tweets.sort(key=lambda t: t['score'], reverse=True)
        
        bt.logging.info(
            f"Scored {len(scored_tweets)} tweets, "
            f"average score: {sum(t['score'] for t in scored_tweets) / len(scored_tweets):.6f}"
            if scored_tweets else "Scored 0 tweets"
        )
        
        return scored_tweets
