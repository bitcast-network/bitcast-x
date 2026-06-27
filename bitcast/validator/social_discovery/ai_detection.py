"""
Account-level AI-content scoring for social discovery v2.

Pipeline per account:
  1. Deterministically sample up to AI_SAMPLE_SIZE tweets with >= AI_MIN_TWEET_CHARS
     characters (seed = hash(date_bucket + username + tweet_id)). Determinism is
     critical: every validator must select the same tweets so independently
     computed scores agree.
  2. Score each sampled tweet via its-ai (concurrent, cached per-tweet permanently).
  3. Average the valid scores and bucketise (coarse bands absorb API jitter).

The resulting score (0.0 human .. 1.0 AI) is used to dampen the account's
outgoing PageRank influence via a sink node.

Failure policy is fail-open: an account with no eligible tweets, or whose samples
all fail, gets no score (no dampening) rather than a spurious penalty.
"""

import hashlib
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

import bittensor as bt

from bitcast.validator.clients import its_ai_client
from bitcast.validator.utils.twitter_cache import get_cached_user_tweets
from bitcast.validator.utils.ai_score_cache import (
    get_cached_tweet_ai_score,
    cache_tweet_ai_score,
    get_cached_ai_score,
    cache_ai_score,
)
from bitcast.validator.utils.config import (
    AI_SAMPLE_SIZE,
    AI_MIN_TWEET_CHARS,
    AI_DETECTION_CONCURRENCY,
    AI_SCORE_BUCKET,
)


def current_date_bucket() -> str:
    """Discovery-cycle bucket used to seed sampling.

    Aligned to the bi-weekly discovery cycle (not the wall-clock day) so the seed
    — and therefore the sampled tweets and resulting scores — is identical for all
    validators running a pool's discovery anywhere within the same cycle, rather
    than only within the same UTC day. This matches the per-account cache TTL
    (one cycle) and removes the UTC-midnight divergence seam. Computed from the
    same reference date / cycle length as the discovery scheduler.
    """
    # Imported here to avoid importing the heavy analyzer module at definition time.
    from bitcast.validator.social_discovery.social_discovery import (
        DISCOVERY_REFERENCE_DATE,
        DISCOVERY_CYCLE_DAYS,
    )
    days = (datetime.now(timezone.utc).date() - DISCOVERY_REFERENCE_DATE).days
    cycle = days // DISCOVERY_CYCLE_DAYS
    return f"cycle-{cycle}"


def bucketize(score: float, bucket: float = AI_SCORE_BUCKET) -> float:
    """Round a raw AI score to the nearest band so minor API jitter doesn't shift outcomes."""
    if bucket <= 0:
        return score
    return round(round(score / bucket) * bucket, 6)


def _tweet_seed_key(date_bucket: str, username: str, tweet: dict) -> str:
    tid = tweet.get('tweet_id') or tweet.get('text', '')
    digest = hashlib.sha256(f"{date_bucket}:{username.lower()}:{tid}".encode()).hexdigest()
    return digest


def select_sample_tweets(
    username: str,
    tweets: List[dict],
    date_bucket: str,
    sample_size: int = AI_SAMPLE_SIZE,
    min_chars: int = AI_MIN_TWEET_CHARS,
) -> List[dict]:
    """Deterministically pick up to ``sample_size`` eligible (>= min_chars) tweets."""
    eligible = [t for t in tweets if t.get('text') and len(t['text']) >= min_chars]
    eligible.sort(key=lambda t: _tweet_seed_key(date_bucket, username, t))
    return eligible[:sample_size]


def _score_tweet(tweet: dict, client) -> Optional[float]:
    """Score a single tweet via its-ai, using the permanent per-tweet cache."""
    tid = tweet.get('tweet_id')
    if tid:
        cached = get_cached_tweet_ai_score(tid)
        if cached is not None:
            return cached
    score = client.analyze_text(tweet['text'])
    if score is not None and tid:
        cache_tweet_ai_score(tid, score)
    return score


def compute_account_ai_score(
    username: str,
    tweets: List[dict],
    date_bucket: str,
    client=its_ai_client,
    concurrency: int = AI_DETECTION_CONCURRENCY,
) -> Optional[float]:
    """Bucketised mean AI score over the account's sampled tweets, or None to skip."""
    sample = select_sample_tweets(username, tweets, date_bucket)
    if not sample:
        return None

    if concurrency > 1 and len(sample) > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            results = list(executor.map(lambda t: _score_tweet(t, client), sample))
    else:
        results = [_score_tweet(t, client) for t in sample]

    valid = [r for r in results if r is not None]
    if not valid:
        return None

    return bucketize(sum(valid) / len(valid))


def compute_ai_scores(
    usernames,
    date_bucket: Optional[str] = None,
    client=its_ai_client,
    tweets_provider: Optional[Callable[[str], Optional[List[dict]]]] = None,
) -> Dict[str, float]:
    """Compute per-account AI scores for a set of usernames.

    Accounts are processed sequentially while each account's tweet samples are
    scored concurrently, so total in-flight its-ai requests stay within
    AI_DETECTION_CONCURRENCY (its-ai is comfortable at ~4x).

    Args:
        usernames: Iterable of usernames to score.
        date_bucket: Sampling seed bucket (defaults to the current discovery cycle).
        client: its-ai client module (injectable for tests).
        tweets_provider: Optional fn(username) -> tweets; defaults to the discovery cache.

    Returns:
        Dict of {username: ai_score} for accounts that produced a score.
    """
    bucket = date_bucket or current_date_bucket()
    provider = tweets_provider or _load_cached_tweets
    scores: Dict[str, float] = {}

    for username in usernames:
        cached = get_cached_ai_score(username, bucket)
        if cached is not None:
            scores[username] = cached
            continue

        tweets = provider(username)
        if not tweets:
            continue

        try:
            score = compute_account_ai_score(username, tweets, bucket, client=client)
        except its_ai_client.ItsAiConfigError:
            # Missing/invalid API key (or 401) affects every account — surface
            # loudly rather than silently disabling dampening for the whole run.
            raise
        except Exception as e:
            bt.logging.warning(f"AI scoring failed for @{username}: {e}")
            continue

        if score is not None:
            cache_ai_score(username, bucket, score)
            scores[username] = score

    if scores:
        flagged = sum(1 for v in scores.values() if v > 0)
        bt.logging.info(
            f"AI detection: scored {len(scores)} accounts ({flagged} with AI signal > 0)"
        )
    return scores


def _load_cached_tweets(username: str) -> Optional[List[dict]]:
    cached = get_cached_user_tweets(username)
    if cached and cached.get('tweets'):
        return cached['tweets']
    return None
