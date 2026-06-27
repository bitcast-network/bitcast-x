"""
Relevance gradient helpers for social discovery v2.

Replaces the legacy 2-tier keyword-count relevance gate with a continuous,
beta-smoothed on-topic ratio:

    smoothed_ratio = (relevant + alpha) / (total + alpha + beta)

where alpha = prior_mean * prior_strength and beta = (1 - prior_mean) * prior_strength.

The smoothing shrinks low-volume accounts toward the prior mean (so a 2/3-tweet
account can't game a high ratio) while leaving high-volume accounts judged on
their true ratio. The result feeds both an inclusion gate and the PageRank
personalization vector.
"""

from bitcast.validator.utils.config import (
    RELEVANCE_PRIOR_MEAN,
    RELEVANCE_PRIOR_STRENGTH,
    MIN_RELEVANT_TWEETS,
)


def beta_params(prior_mean: float, prior_strength: float) -> tuple:
    """Convert (mean, strength) parameterization to beta (alpha, beta)."""
    alpha = prior_mean * prior_strength
    beta = (1.0 - prior_mean) * prior_strength
    return alpha, beta


def smoothed_relevance_ratio(
    relevant: int,
    total: int,
    prior_mean: float = RELEVANCE_PRIOR_MEAN,
    prior_strength: float = RELEVANCE_PRIOR_STRENGTH,
) -> float:
    """
    Beta-smoothed fraction of on-topic tweets.

    Always strictly positive (alpha > 0), so it is safe to use directly as a
    personalization weight. With total == 0 it returns the prior mean.
    """
    alpha, beta = beta_params(prior_mean, prior_strength)
    return (relevant + alpha) / (total + alpha + beta)


def passes_relevance_gate(
    relevant: int,
    smoothed_ratio: float,
    min_ratio: float,
    min_relevant_tweets: int = MIN_RELEVANT_TWEETS,
) -> bool:
    """
    Inclusion gate: an account joins the graph iff it clears both the smoothed
    ratio floor AND the absolute relevant-tweet floor.
    """
    return relevant >= min_relevant_tweets and smoothed_ratio >= min_ratio
