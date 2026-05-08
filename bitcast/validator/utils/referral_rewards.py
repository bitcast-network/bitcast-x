"""Shared referral reward calculation helpers."""

from math import log10
from typing import Mapping, Any


def compute_referral_reward(followers: int, influence: float, max_amount: float = 100.0) -> float:
    """
    Compute the referral bonus (USD) from the referee's followers and influence score.

    Followers component (80% weight): log-scales from 1,000 to 25,000 followers.
    Influence component (20% weight): log-scales from 1 to 1,000 influence score.
    Result is in the range [$0, max_amount].
    """
    follower_raw = max_amount * log10(max(followers, 1000) / 1000) / log10(25000 / 1000)
    follower_score = 0.8 * max(0.0, min(follower_raw, max_amount))

    influence_raw = max_amount * log10(max(influence, 1)) / log10(1000)
    influence_score = 0.2 * max(0.0, min(influence_raw, max_amount))

    return round(follower_score + influence_score, 2)


def compute_referral_reward_from_account(account_info: Mapping[str, Any] | None, max_amount: float = 100.0) -> float:
    """Compute the referral bonus from a social-map account record."""
    if not account_info:
        return 0.0

    followers = int(account_info.get("followers_count", 0) or 0)
    influence = float(account_info.get("score", 0.0) or 0.0)
    return compute_referral_reward(followers, influence, max_amount=max_amount)
