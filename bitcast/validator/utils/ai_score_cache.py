"""
Caching for AI-detection scores (social discovery v2).

Two layers, both backed by the shared DiscoveryCache (diskcache):

  1. Per-tweet score  (key ``ai_tweet_{tweet_id}``)  -- permanent.
     Tweet text is immutable, so an its-ai result for a tweet never changes.

  2. Per-account score (key ``ai_acct_{username}_{date_bucket}``) -- 2-week TTL.
     The seeded sample rotates with the discovery-cycle bucket (see
     current_date_bucket), so the aggregate is recomputed once per cycle while
     still amortizing cost within it. Cycle-based (not UTC-daily) bucketing keeps
     the bucket identical for all validators running a pool within the same cycle.
"""

from typing import Optional

import bittensor as bt

from bitcast.validator.utils.twitter_cache import DiscoveryCache
from bitcast.validator.utils.config import AI_SCORE_TTL_SECONDS


def _tweet_key(tweet_id: str) -> str:
    return f"ai_tweet_{tweet_id}"


def _account_key(username: str, date_bucket: str) -> str:
    return f"ai_acct_{username.lower()}_{date_bucket}"


def get_cached_tweet_ai_score(tweet_id: str) -> Optional[float]:
    """Return cached per-tweet AI score, or None on miss."""
    if not tweet_id:
        return None
    return DiscoveryCache.get_cache().get(_tweet_key(tweet_id))


def cache_tweet_ai_score(tweet_id: str, score: float) -> None:
    """Cache a per-tweet AI score permanently (tweet text is immutable)."""
    if not tweet_id:
        return
    DiscoveryCache.get_cache().set(_tweet_key(tweet_id), float(score))


def get_cached_ai_score(username: str, date_bucket: str) -> Optional[float]:
    """Return cached per-account AI score for the given date bucket, or None."""
    return DiscoveryCache.get_cache().get(_account_key(username, date_bucket))


def cache_ai_score(username: str, date_bucket: str, score: float) -> None:
    """Cache a per-account AI score with the configured TTL."""
    DiscoveryCache.get_cache().set(
        _account_key(username, date_bucket), float(score), expire=AI_SCORE_TTL_SECONDS
    )
    bt.logging.debug(f"Cached AI score for @{username} ({date_bucket}): {score:.3f}")
