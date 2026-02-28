"""
Score calculator with weighted engagement scoring.

Calculates tweet scores by multiplying influence scores with engagement weights.
Includes cabal protection to reduce scores for accounts with high relationship scores.
"""

from typing import Dict, List, Optional, Tuple
import numpy as np
import bittensor as bt

from bitcast.validator.utils.config import (
    PAGERANK_RETWEET_WEIGHT,
    PAGERANK_QUOTE_WEIGHT,
    BASELINE_TWEET_SCORE_FACTOR
)


class ScoreCalculator:
    """Calculates weighted scores for tweets based on engagement."""
    
    def __init__(
        self,
        considered_accounts: Dict[str, float],
        relationship_scores: Optional[np.ndarray] = None,
        scores_username_to_idx: Optional[Dict[str, int]] = None,
        retweet_weight: float = None,
        quote_weight: float = None
    ):
        """
        Initialize calculator with influence scores, relationship scores, and weights.
        
        Args:
            considered_accounts: Map of username -> influence_score
            relationship_scores: Optional matrix of cumulative interaction scores for cabal protection
            scores_username_to_idx: Optional mapping of usernames to matrix indices
            retweet_weight: Weight for retweets (default: PAGERANK_RETWEET_WEIGHT)
            quote_weight: Weight for quotes (default: PAGERANK_QUOTE_WEIGHT)
        """
        self.considered_accounts = considered_accounts
        self.relationship_scores = relationship_scores
        self.scores_username_to_idx = scores_username_to_idx or {}
        self.retweet_weight = retweet_weight if retweet_weight is not None else PAGERANK_RETWEET_WEIGHT
        self.quote_weight = quote_weight if quote_weight is not None else PAGERANK_QUOTE_WEIGHT
        
        # Calculate minimum influence score from considered accounts
        # Used as fallback for accounts not in the social map
        if considered_accounts:
            self.min_influence_score = min(considered_accounts.values())
        else:
            self.min_influence_score = 0.0
        
        cabal_status = "enabled" if relationship_scores is not None else "disabled"
        bt.logging.info(
            f"ScoreCalculator initialized: "
            f"{len(considered_accounts)} accounts, "
            f"RT weight={self.retweet_weight}, "
            f"Quote weight={self.quote_weight}, "
            f"Min influence score={self.min_influence_score:.6f}, "
            f"Cabal protection={cabal_status}"
        )
    
    def calculate_tweet_score(
        self,
        engagements: Dict[str, str],
        author_influence_score: float,
        author: str = ""
    ) -> Tuple[float, List[Dict]]:
        """
        Calculate weighted score for a tweet with cabal protection.
        
        Score = (author_influence_score × BASELINE_TWEET_SCORE_FACTOR) + Σ(influence_score × engagement_weight × scale_factor)
        
        All tweets get a baseline score from the author's influence, plus additional
        score from engagement (retweets and quotes). Cabal protection applies a scaling
        factor based on relationship scores to reduce rewards for frequent mutual engagement.
        
        Args:
            engagements: Dict mapping username -> engagement_type ("retweet" or "quote")
            author_influence_score: The influence score of the tweet's author
            author: The username of the tweet's author (for cabal protection)
            
        Returns:
            Tuple of (total_score, engagement_details)
            engagement_details is list of dicts with username, influence_score,
            engagement_type, relationship_score, scale_factor, and weighted_contribution
        """
        details = []
        
        # Start with baseline score from author's influence
        total_score = author_influence_score * BASELINE_TWEET_SCORE_FACTOR
        
        # Add engagement contributions with cabal protection
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
            
            # Calculate base contribution
            base_contribution = influence_score * weight
            
            # Apply cabal protection scaling
            scale_factor = 1.0
            relationship_score = 0.0
            
            if self.relationship_scores is not None and author:
                engager_idx = self.scores_username_to_idx.get(username.lower())
                author_idx = self.scores_username_to_idx.get(author.lower())
                
                if engager_idx is not None and author_idx is not None:
                    relationship_score = float(self.relationship_scores[engager_idx, author_idx])
                    if relationship_score > 0:
                        scale_factor = 0.1 + (0.9 / relationship_score)
            
            contribution = base_contribution * scale_factor
            total_score += contribution
            
            # Log significant cabal penalties
            if scale_factor < 0.3:
                bt.logging.debug(
                    f"Cabal protection applied: @{username} → @{author}, "
                    f"relationship_score={relationship_score:.1f}, scale={scale_factor:.2f}"
                )
            
            details.append({
                'username': username,
                'influence_score': round(influence_score, 6),
                'engagement_type': engagement_type,
                'relationship_score': round(relationship_score, 2),
                'scale_factor': round(scale_factor, 3),
                'weighted_contribution': round(contribution, 6)
            })
        
        # Round final score
        total_score = round(total_score, 6)
        
        return total_score, details
