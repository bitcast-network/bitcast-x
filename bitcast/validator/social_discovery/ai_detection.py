"""
Account-level AI-content scoring for social discovery v2.

Pipeline (compute_ai_scores), batched globally across accounts:
  1. Deterministically sample up to AI_SAMPLE_SIZE tweets per account with
     >= AI_MIN_TWEET_CHARS characters (seed = hash(date_bucket + username +
     tweet_id)). Determinism is critical: every validator must select the same
     tweets so independently computed scores agree.
  2. Pool every sampled tweet not already in the per-tweet cache across ALL
     accounts, dedup by tweet_id, and score them via its-ai's /v2/batch endpoint
     (a few large requests instead of one per tweet). Results are cached
     per-tweet permanently.
  3. Per account, average its sample's valid scores and bucketise (coarse bands
     absorb API jitter).

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
    ITS_AI_BATCH_SIZE,
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


def _tweet_score_key(tweet: dict) -> str:
    """Map a tweet to a stable key for the fresh-score map (tweet_id, else identity)."""
    return tweet.get('tweet_id') or f"_obj_{id(tweet)}"


def _batch_score_and_cache(
    tweets: List[dict],
    client,
    batch_size: int = ITS_AI_BATCH_SIZE,
    concurrency: int = AI_DETECTION_CONCURRENCY,
) -> Dict[str, float]:
    """Score ``tweets`` via its-ai's batch endpoint and cache each per-tweet.

    ``tweets`` should already exclude per-tweet-cache hits and be deduped. Chunks
    of up to ``batch_size`` are sent per request, with up to ``concurrency``
    requests in flight. Returns {score_key: score} for the items that scored.
    """
    if not tweets:
        return {}

    chunks = [tweets[i:i + batch_size] for i in range(0, len(tweets), batch_size)]

    def _run(chunk: List[dict]):
        return client.analyze_texts([t['text'] for t in chunk])

    if concurrency > 1 and len(chunks) > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            chunk_results = list(executor.map(_run, chunks))
    else:
        chunk_results = [_run(c) for c in chunks]

    fresh: Dict[str, float] = {}
    for chunk, results in zip(chunks, chunk_results):
        for tweet, score in zip(chunk, results):
            if score is None:
                continue
            tid = tweet.get('tweet_id')
            if tid:
                cache_tweet_ai_score(tid, score)
            fresh[_tweet_score_key(tweet)] = score
    return fresh


def _resolve_tweet_score(tweet: dict, fresh: Dict[str, float]) -> Optional[float]:
    """Per-tweet score from the cache (pre-existing hit) or this run's fresh batch."""
    tid = tweet.get('tweet_id')
    if tid:
        cached = get_cached_tweet_ai_score(tid)
        if cached is not None:
            return cached
    return fresh.get(_tweet_score_key(tweet))


def compute_ai_scores(
    usernames,
    date_bucket: Optional[str] = None,
    client=its_ai_client,
    tweets_provider: Optional[Callable[[str], Optional[List[dict]]]] = None,
) -> Dict[str, float]:
    """Compute per-account AI scores for a set of usernames.

    All sampled tweets needing scoring are pooled across accounts and sent to
    its-ai's batch endpoint in a few large requests (deduped, per-tweet-cached),
    rather than one request per tweet. Selection, caching, bucketising and the
    fail-open policy are unchanged, so results match the per-tweet path exactly.

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

    # Phase 1: resolve each uncached account to its deterministic tweet sample.
    pending: Dict[str, List[dict]] = {}
    for username in usernames:
        cached = get_cached_ai_score(username, bucket)
        if cached is not None:
            scores[username] = cached
            continue
        tweets = provider(username)
        if not tweets:
            continue
        sample = select_sample_tweets(username, tweets, bucket)
        if sample:
            pending[username] = sample

    # Phase 2: pool every sample tweet missing a per-tweet score, dedup, batch-score.
    # ItsAiConfigError (missing key / 401) propagates: it affects every account,
    # so we surface it rather than silently disabling dampening for the whole run.
    to_score: List[dict] = []
    seen_ids = set()
    for sample in pending.values():
        for tweet in sample:
            tid = tweet.get('tweet_id')
            if tid:
                if tid in seen_ids:
                    continue
                seen_ids.add(tid)
                if get_cached_tweet_ai_score(tid) is not None:
                    continue
            to_score.append(tweet)

    fresh = _batch_score_and_cache(to_score, client)

    # Phase 3: aggregate per account from cached + freshly scored tweets.
    for username, sample in pending.items():
        valid = [s for s in (_resolve_tweet_score(t, fresh) for t in sample) if s is not None]
        if not valid:
            continue
        score = bucketize(sum(valid) / len(valid))
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
