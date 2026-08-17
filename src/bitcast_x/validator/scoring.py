"""Freeze v2-compatible engagement scores for accepted attributions."""

import asyncio
import logging
from collections.abc import Callable

import numpy as np
from pydantic import BaseModel, ConfigDict

from bitcast_x.brief_filter import BriefEvaluation, BriefFilter
from bitcast_x.campaigns import (
    CampaignFeed,
    CampaignRecord,
    EcosystemMap,
    considered_accounts_for_campaign,
    ecosystem_map_at,
)
from bitcast_x.errors import ReconciliationUnavailableError
from bitcast_x.protocol import AttributionResult
from bitcast_x.scoring import EngagementContribution, calculate_tweet_score
from bitcast_x.x_provider import EngagementFetch, Tweet, TweetFetch, XProvider

LOGGER = logging.getLogger(__name__)


class ScoredAttribution(BaseModel):
    """Frozen accepted attribution plus independently recomputed tweet score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribution: AttributionResult
    tweet: Tweet
    score: float
    author_influence: float
    baseline_score: float
    details: tuple[EngagementContribution, ...]
    meets_brief: bool = True
    brief_reasoning: str = ""
    brief_detailed_breakdown: str | None = None
    llm_checks_used: int = 0
    engagements: tuple[str, ...] = ()
    author_followers_count: int = 0


class AttributionScorer:
    """Fetch engagement identities and apply the established v2 score formula."""

    def __init__(
        self,
        x_provider: XProvider,
        *,
        brief_filter: BriefFilter | None = None,
        max_concurrency: int = 16,
        engagement_merger: Callable[[str, EngagementFetch], EngagementFetch] | None = None,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        self._x = x_provider
        self._brief_filter = brief_filter
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._engagement_merger = engagement_merger

    async def score(
        self,
        feed: CampaignFeed,
        attributions: list[AttributionResult],
        *,
        tweet_evidence: dict[str, Tweet] | None = None,
        defer_unavailable_tweets: bool = False,
    ) -> list[ScoredAttribution]:
        """Score accepted results, optionally deferring unavailable tweet evidence."""

        accepted = [result for result in attributions if result.accepted]
        tweet_ids = sorted({item.tweet_id for item in accepted})
        evidence_values = await asyncio.gather(
            *(
                self._fetch_evidence(
                    item,
                    fallback_tweet=(tweet_evidence or {}).get(item),
                )
                for item in tweet_ids
            )
        )
        evidence = dict(zip(tweet_ids, evidence_values, strict=True))
        scored: list[ScoredAttribution] = []
        for item in accepted:
            try:
                scored.append(self._score_one(feed, item, *evidence[item.tweet_id]))
            except ReconciliationUnavailableError as exc:
                if not defer_unavailable_tweets:
                    raise
                LOGGER.warning(
                    "tweet scoring unavailable; deferring campaign=%s tweet_id=%s error=%s",
                    item.campaign_id,
                    item.tweet_id,
                    exc,
                )
        if self._brief_filter is not None:
            evaluations = await asyncio.gather(
                *(
                    self._evaluate_with_deferral(
                        feed,
                        item,
                        defer_unavailable=defer_unavailable_tweets,
                    )
                    for item in scored
                )
            )
            scored = [
                item.model_copy(
                    update={
                        "meets_brief": result.meets_brief,
                        "brief_reasoning": result.reasoning,
                        "brief_detailed_breakdown": result.detailed_breakdown,
                        "llm_checks_used": result.checks_used,
                    }
                )
                for item, result in zip(scored, evaluations, strict=True)
                if result is not None
            ]
            scored = _exclude_participant_engagement(scored)
        return sorted(
            scored,
            key=lambda item: (
                item.attribution.campaign_id,
                item.attribution.tweet_id,
                item.attribution.miner_hotkey or "",
            ),
        )

    async def _evaluate_with_deferral(
        self,
        feed: CampaignFeed,
        item: ScoredAttribution,
        *,
        defer_unavailable: bool,
    ) -> BriefEvaluation | None:
        try:
            return await self._evaluate(feed, item)
        except ReconciliationUnavailableError as exc:
            if not defer_unavailable:
                raise
            LOGGER.warning(
                "brief evaluation unavailable; deferring campaign=%s tweet_id=%s error=%s",
                item.attribution.campaign_id,
                item.attribution.tweet_id,
                exc,
            )
            return None

    async def _evaluate(
        self,
        feed: CampaignFeed,
        item: ScoredAttribution,
    ) -> BriefEvaluation:
        campaign = next(
            record
            for record in feed.campaigns
            if record.access.campaign_id == item.attribution.campaign_id
        )
        assert self._brief_filter is not None
        async with self._semaphore:
            return await self._brief_filter.evaluate(campaign, item.tweet)

    async def _fetch_evidence(
        self,
        tweet_id: str,
        *,
        fallback_tweet: Tweet | None = None,
    ) -> tuple[TweetFetch, EngagementFetch]:
        async with self._semaphore:
            tweet, engagements = await asyncio.gather(
                self._x.fetch_tweet_by_id(tweet_id),
                self._x.fetch_engagements(tweet_id),
            )
            if self._engagement_merger is not None:
                engagements = self._engagement_merger(tweet_id, engagements)
            if not tweet.provider_available and fallback_tweet is not None:
                tweet = TweetFetch(tweet=fallback_tweet, provider_available=True)
            return tweet, engagements

    def _score_one(
        self,
        feed: CampaignFeed,
        attribution: AttributionResult,
        tweet_result: TweetFetch,
        engagement_result: EngagementFetch,
    ) -> ScoredAttribution:
        if (
            not tweet_result.provider_available
            or tweet_result.tweet is None
            or not engagement_result.provider_available
        ):
            raise ReconciliationUnavailableError(
                f"scoring evidence unavailable for tweet {attribution.tweet_id}"
            )
        tweet = tweet_result.tweet
        campaign = next(
            item for item in feed.campaigns if item.access.campaign_id == attribution.campaign_id
        )
        tweet_ecosystem = _campaign_map_at(feed, campaign, tweet)
        try:
            considered, current_ecosystem = considered_accounts_for_campaign(
                feed, campaign, ecosystem_id=tweet_ecosystem.ecosystem_id
            )
        except ValueError as exc:
            raise ReconciliationUnavailableError(str(exc)) from exc
        author_account = next(
            (
                account
                for account in current_ecosystem.accounts
                if account.x_id == tweet.author_x_id
                or account.username.casefold() == tweet.author.casefold()
            ),
            None,
        )
        minimum = min(considered.values()) if considered else 0.0
        tweet_time_influence = next(
            (
                account.influence
                for account in tweet_ecosystem.accounts
                if account.x_id == tweet.author_x_id
                or account.username.casefold() == tweet.author.casefold()
            ),
            None,
        )
        current_influence = considered.get(tweet.author.lower())
        author_influence = max(
            value
            for value in (tweet_time_influence, current_influence, minimum)
            if value is not None
        )
        usernames = sorted(considered)
        indexes = {username: index for index, username in enumerate(usernames)}
        relationships = np.zeros((len(usernames), len(usernames)), dtype=np.float64)
        for edge in current_ecosystem.relationships:
            source = indexes.get(edge.source_username.lower())
            target = indexes.get(edge.target_username.lower())
            if source is not None and target is not None:
                relationships[source, target] = edge.score
        engagements = {
            username.lower(): kind
            for username, kind in engagement_result.engagements.items()
            if username.lower() != tweet.author.lower()
        }
        score, details = calculate_tweet_score(
            engagements,
            author_influence=author_influence,
            author=tweet.author,
            considered_accounts=considered,
            relationship_scores=relationships,
            username_to_index=indexes,
        )
        return ScoredAttribution(
            attribution=attribution,
            tweet=tweet,
            score=score,
            author_influence=round(author_influence, 6),
            baseline_score=round(author_influence * 2.0, 6),
            details=tuple(details),
            engagements=tuple(sorted(engagement_result.engagements)),
            author_followers_count=(
                author_account.followers_count if author_account is not None else 0
            ),
        )


def _campaign_map_at(feed: CampaignFeed, campaign: CampaignRecord, tweet: Tweet) -> EcosystemMap:
    """Select the first configured pool in which the tweet author is eligible."""
    available: list[EcosystemMap] = []
    for pool in campaign.pools:
        ecosystem = ecosystem_map_at(feed, pool, tweet.created_at)
        if ecosystem is None:
            continue
        available.append(ecosystem)
        if tweet.author_x_id in ecosystem.eligible_creator_x_ids:
            return ecosystem
    if not available:
        raise ReconciliationUnavailableError(
            f"no ecosystem map active when tweet {tweet.tweet_id} was published"
        )
    return available[0]


def _exclude_participant_engagement(
    scored: list[ScoredAttribution],
) -> list[ScoredAttribution]:
    """Port v2's post-filter rule: rewarded creators cannot boost one another."""

    passing_by_campaign: dict[str, set[str]] = {}
    for item in scored:
        if item.meets_brief:
            passing_by_campaign.setdefault(item.attribution.campaign_id, set()).add(
                item.tweet.author.casefold()
            )
    output: list[ScoredAttribution] = []
    for item in scored:
        participants = passing_by_campaign.get(item.attribution.campaign_id, set())
        remaining = tuple(
            detail for detail in item.details if detail.username.casefold() not in participants
        )
        if len(remaining) == len(item.details):
            output.append(item)
            continue
        output.append(
            item.model_copy(
                update={
                    "score": round(
                        item.baseline_score
                        + sum(detail.weighted_contribution for detail in remaining),
                        6,
                    ),
                    "details": remaining,
                }
            )
        )
    return output
