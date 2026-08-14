"""Consensus tweet engagement score preserved from Bitcast X v2."""

from typing import Protocol

from pydantic import BaseModel, ConfigDict

BASELINE_TWEET_SCORE_FACTOR = 2.0
RETWEET_WEIGHT = 1.0
QUOTE_WEIGHT = 3.0
CABAL_BASE = 0.1
CABAL_SCALE = 0.9
SCORE_ROUND_DIGITS = 6


class RelationshipScores(Protocol):
    """Dense or sparse relationship matrix lookup used by cabal protection."""

    def __getitem__(self, key: tuple[int, int]) -> float: ...


class EngagementContribution(BaseModel):
    """Auditable contribution from one considered account."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    username: str
    influence_score: float
    engagement_type: str
    relationship_score: float
    scale_factor: float
    weighted_contribution: float


def calculate_tweet_score(
    engagements: dict[str, str],
    *,
    author_influence: float,
    author: str,
    considered_accounts: dict[str, float],
    relationship_scores: RelationshipScores | None = None,
    username_to_index: dict[str, int] | None = None,
) -> tuple[float, list[EngagementContribution]]:
    """Apply v2's bit-identical baseline, engagement weights, and cabal scaling."""

    indexes = username_to_index or {}
    total = author_influence * BASELINE_TWEET_SCORE_FACTOR
    details: list[EngagementContribution] = []
    for username, engagement_type in engagements.items():
        influence = considered_accounts.get(username)
        if influence is None:
            continue
        if engagement_type == "retweet":
            weight = RETWEET_WEIGHT
        elif engagement_type == "quote":
            weight = QUOTE_WEIGHT
        else:
            continue
        relationship = 0.0
        scale = 1.0
        if relationship_scores is not None and author:
            engager_index = indexes.get(username.lower())
            author_index = indexes.get(author.lower())
            if engager_index is not None and author_index is not None:
                relationship = float(relationship_scores[engager_index, author_index])
                if relationship > 0:
                    scale = CABAL_BASE + CABAL_SCALE / relationship
        contribution = influence * weight * scale
        total += contribution
        details.append(
            EngagementContribution(
                username=username,
                influence_score=round(influence, 6),
                engagement_type=engagement_type,
                relationship_score=round(relationship, 2),
                scale_factor=round(scale, 3),
                weighted_contribution=round(contribution, 6),
            )
        )
    return round(total, SCORE_ROUND_DIGITS), details
