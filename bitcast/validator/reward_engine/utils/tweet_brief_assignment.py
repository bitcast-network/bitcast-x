"""
One-to-one assignment of tweets to briefs for reward attribution.

A single tweet can satisfy the filtering criteria of multiple briefs, but each
tweet should only be rewarded once. This module resolves those overlaps by
assigning every tweet to exactly one brief before rewards are calculated.

Assignment is a greedy maximum-weight matching: each candidate (tweet, brief)
pair is weighted by the tweet's *estimated* payout in that brief, and pairs are
awarded highest-weight-first subject to two constraints:

  1. one-to-one      - each tweet is assigned to at most one brief
  2. per-account cap - each account contributes at most ``max_tweets`` tweets to
                       a given brief (the brief's existing max_tweets limit)

Greedy is chosen deliberately over an exact optimum (e.g. Hungarian matching):
the per-account overlaps are tiny, and greedy is "usually optimal" while staying
simple to read and reason about. The one case it handles well — and the reason
this module exists — is two capped briefs sharing two qualifying tweets from the
same account: greedy routes one tweet to each brief rather than stacking both on
the higher-paying brief and wasting a slot.

Tweets already frozen into a brief's reward snapshot on a previous run are passed
in as ``committed_tweet_ids`` and are never reassigned, so staggered emission
timing degrades gracefully (the earlier-emitting brief keeps its tweets).
"""

from typing import Dict, List, Optional, Set, Tuple

from bitcast.validator.utils.config import REWARD_SMOOTHING_EXPONENT


def _estimate_payouts(
    daily_budget: float,
    tweets: List[Dict],
    smoothing_exponent: float,
) -> List[float]:
    """Estimate each tweet's USD payout within a single brief.

    Mirrors the proportional, power-law-smoothed split used for the final reward
    calculation (see ``TwitterEvaluator._calculate_tweet_targets``) so assignment
    weights reflect how rewards are actually distributed. This is a pre-dedup
    estimate: the denominator uses every tweet that passed the brief, before any
    are reassigned elsewhere.
    """
    smoothed = [max(t.get('score', 0.0), 0.0) ** smoothing_exponent for t in tweets]
    total = sum(smoothed)
    if total <= 0:
        return [0.0] * len(tweets)
    return [daily_budget * (s / total) for s in smoothed]


def assign_tweets_to_briefs(
    brief_candidates: List[Dict],
    committed_tweet_ids: Optional[Set[str]] = None,
    smoothing_exponent: float = REWARD_SMOOTHING_EXPONENT,
) -> Dict[str, Set[str]]:
    """Assign each tweet to exactly one brief via greedy max-weight matching.

    Args:
        brief_candidates: One entry per brief awaiting assignment, each a dict::

            {
                'brief_id':     str,
                'daily_budget': float,
                'max_tweets':   Optional[int],  # per-account cap (None/0 = no cap)
                'tweets':       [{'tweet_id': str, 'author': str, 'score': float}, ...],
            }

            ``tweets`` are the tweets that passed that brief's filtering.
        committed_tweet_ids: Tweet ids already locked into a brief's reward
            snapshot on a previous run; excluded from (re)assignment.
        smoothing_exponent: Power-law exponent for payout estimation.

    Returns:
        Mapping of ``brief_id -> set(tweet_id)``. Every brief in
        ``brief_candidates`` is present, possibly with an empty set.
    """
    assigned: Dict[str, Set[str]] = {c['brief_id']: set() for c in brief_candidates}
    taken: Set[str] = set(committed_tweet_ids or set())
    caps: Dict[str, Optional[int]] = {}
    per_account_count: Dict[Tuple[str, str], int] = {}

    # Build weighted candidate edges across all briefs.
    edges: List[Tuple[float, str, str, str]] = []  # (payout, brief_id, author, tweet_id)
    for candidate in brief_candidates:
        brief_id = candidate['brief_id']
        caps[brief_id] = candidate.get('max_tweets') or None  # 0/None => no cap
        tweets = candidate.get('tweets', [])
        payouts = _estimate_payouts(
            candidate.get('daily_budget', 0.0), tweets, smoothing_exponent
        )
        for tweet, payout in zip(tweets, payouts):
            tweet_id = tweet.get('tweet_id')
            if not tweet_id:
                continue
            edges.append((payout, brief_id, tweet.get('author', ''), tweet_id))

    # Highest payout first; deterministic tie-break on (brief_id, tweet_id).
    edges.sort(key=lambda e: (-e[0], e[1], e[3]))

    for _payout, brief_id, author, tweet_id in edges:
        if tweet_id in taken:
            continue  # already assigned to another brief (or frozen in a snapshot)
        cap = caps[brief_id]
        account_key = (author, brief_id)
        if cap is not None and per_account_count.get(account_key, 0) >= cap:
            continue  # this account has filled its slots in this brief
        assigned[brief_id].add(tweet_id)
        taken.add(tweet_id)
        per_account_count[account_key] = per_account_count.get(account_key, 0) + 1

    return assigned
