"""Freeze accepted scores and derive shadow emission weights."""

import logging
from collections.abc import Collection

from bitcast_x.campaigns import CampaignFeed, CampaignRecord
from bitcast_x.errors import ReconciliationUnavailableError
from bitcast_x.protocol import AttributionResult, MiningProtocol
from bitcast_x.rewards import (
    RewardCampaign,
    RewardTweet,
    TweetReward,
    aggregate_productive_weights,
    apply_v2_bonuses,
    apply_v2_performance_bonus,
    assign_tweets_with_reasons,
    calculate_tweet_floors,
    reward_decisions,
)
from bitcast_x.validator.scoring import AttributionScorer, ScoredAttribution
from bitcast_x.validator.store import ValidatorStore

LOGGER = logging.getLogger(__name__)


def preview_performance_rewards(
    campaign: CampaignRecord,
    scored: list[ScoredAttribution],
) -> list[TweetReward]:
    """Build mutable zero-dollar bonus metadata for a pre-close preview.

    Preview assignment is intentionally scoped to the current campaign. It
    applies the campaign's per-creator cap and performance formula without
    freezing global assignment, featured selection, or payout economics.
    """

    candidate = RewardCampaign(
        campaign_id=campaign.access.campaign_id,
        reward_pool_usd=float(campaign.reward_pool_usd),
        max_tweets_per_creator=campaign.max_tweets_per_creator,
        tweets=tuple(
            _reward_tweet(campaign, item)
            for item in scored
            if item.attribution.campaign_id == campaign.access.campaign_id
            and item.attribution.miner_hotkey is not None
            and item.meets_brief
        ),
    )
    assigned = assign_tweets_with_reasons([candidate]).assigned[candidate.campaign_id]
    adjusted = apply_v2_performance_bonus(candidate, assigned)
    return [
        TweetReward(
            campaign_id=item.campaign_id,
            tweet_id=item.tweet_id,
            creator_x_id=item.creator_x_id,
            miner_hotkey=item.miner_hotkey,
            score=item.score,
            daily_usd_floor=0.0,
            performance_bonus_pct=item.performance_bonus_pct,
            performance_bonus_breakdown=item.performance_bonus_breakdown,
        )
        for item in adjusted.tweets
        if item.tweet_id in assigned
    ]


class RewardCoordinator:
    """Bridge frozen attribution into seven-day productive miner weights."""

    def __init__(self, store: ValidatorStore, scorer: AttributionScorer) -> None:
        self.store = store
        self._scorer = scorer
        self._completed_campaign_ids: frozenset[str] | None = None

    @property
    def completed_campaign_ids(self) -> frozenset[str]:
        """Return campaigns successfully scored during the latest feed cycle."""

        return self._completed_campaign_ids or frozenset()

    async def preview_scores(
        self,
        feed: CampaignFeed,
        attributions: list[AttributionResult],
    ) -> list[ScoredAttribution]:
        """Score mutable, pre-close results without freezing validator state."""

        return await self._scorer.score(
            feed,
            attributions,
            defer_unavailable_tweets=True,
        )

    async def freeze_scores(
        self,
        feed: CampaignFeed,
        attributions: list[AttributionResult],
        *,
        reconciled_campaign_ids: Collection[str] | None = None,
    ) -> list[ScoredAttribution]:
        """Fetch mutable engagement evidence once per campaign snapshot, then reuse it."""

        output: list[ScoredAttribution] = []
        completed_campaign_ids: set[str] = set()
        self._completed_campaign_ids = frozenset()
        for campaign in sorted(feed.campaigns, key=lambda item: item.access.campaign_id):
            if campaign.access.mining_protocol is not MiningProtocol.PRECLAIM_V2:
                continue
            campaign_id = campaign.access.campaign_id
            existing = (
                self.store.scored_reconciliation(feed.snapshot_id, campaign_id)
                if self.store.campaign_finalized(campaign_id)
                else None
            )
            if existing is not None:
                output.extend(existing)
                completed_campaign_ids.add(campaign_id)
                continue
            reconciled = (
                campaign_id in reconciled_campaign_ids
                if reconciled_campaign_ids is not None
                else self.store.campaign_reconciled(campaign_id)
            )
            if not reconciled:
                continue
            campaign_results = [item for item in attributions if item.campaign_id == campaign_id]
            try:
                scored = await self._scorer.score(
                    feed,
                    campaign_results,
                    defer_unavailable_tweets=True,
                )
            except ReconciliationUnavailableError as exc:
                LOGGER.warning(
                    "final scoring deferred campaign=%s error=%s",
                    campaign_id,
                    exc,
                )
                continue
            self.store.persist_scores(feed.snapshot_id, campaign_id, scored)
            output.extend(scored)
            completed_campaign_ids.add(campaign_id)
        self._completed_campaign_ids = frozenset(completed_campaign_ids)
        return output

    def shadow_weights(
        self,
        feed: CampaignFeed,
        scored: list[ScoredAttribution],
        *,
        block: int,
        hotkey_to_uid: dict[str, int],
        uids: list[int],
        persist: bool = True,
    ) -> tuple[dict[int, float], list[TweetReward]]:
        """Calculate and audit the current vector without an on-chain write."""

        by_campaign: dict[str, list[ScoredAttribution]] = {}
        for item in scored:
            by_campaign.setdefault(item.attribution.campaign_id, []).append(item)
        records = {item.access.campaign_id: item for item in self.store.reconciled_campaigns()}
        records.update({item.access.campaign_id: item for item in feed.campaigns})
        active_records: list[CampaignRecord] = []
        for campaign in records.values():
            if campaign.access.mining_protocol is not MiningProtocol.PRECLAIM_V2:
                continue
            start = campaign.emission_start_block
            end = campaign.emission_end_block
            if start is None or end is None or not start <= block <= end:
                continue
            active_records.append(campaign)

        frozen_floors: list[TweetReward] = []
        pending: list[tuple[CampaignRecord, RewardCampaign]] = []
        for campaign in active_records:
            campaign_id = campaign.access.campaign_id
            campaign_json = campaign.model_dump_json()
            frozen = self.store.campaign_rewards(campaign_id, campaign_json)
            if frozen is not None:
                frozen_floors.extend(frozen[0])
                continue
            if campaign_id not in by_campaign:
                stored_scores = (
                    self.store.scored_reconciliation(feed.snapshot_id, campaign_id)
                    if self.store.campaign_finalized(campaign_id)
                    else None
                )
                if stored_scores is None:
                    completed = (
                        campaign_id in self._completed_campaign_ids
                        if self._completed_campaign_ids is not None
                        else self.store.campaign_reconciled(campaign_id)
                    )
                    if not completed:
                        continue
                    stored_scores = []
                by_campaign[campaign_id] = stored_scores
            tweets = tuple(
                _reward_tweet(campaign, item)
                for item in by_campaign.get(campaign_id, [])
                if item.attribution.miner_hotkey is not None and item.meets_brief
            )
            pending.append(
                (
                    campaign,
                    RewardCampaign(
                        campaign_id=campaign_id,
                        reward_pool_usd=float(campaign.reward_pool_usd),
                        max_tweets_per_creator=campaign.max_tweets_per_creator,
                        tweets=tweets,
                    ),
                )
            )
        pending_campaigns = [item[1] for item in pending]
        outcome = assign_tweets_with_reasons(
            pending_campaigns,
            committed_tweet_ids=self.store.rewarded_tweet_ids(),
        )
        bonus_campaigns = [
            apply_v2_bonuses(campaign, outcome.assigned[campaign.campaign_id])
            for campaign in pending_campaigns
        ]
        new_floors = calculate_tweet_floors(bonus_campaigns, outcome.assigned)
        decisions = reward_decisions(bonus_campaigns, outcome, new_floors)
        for campaign, reward_campaign in pending:
            campaign_id = reward_campaign.campaign_id
            self.store.persist_campaign_rewards(
                snapshot_id=feed.snapshot_id,
                campaign_id=campaign_id,
                campaign_json=campaign.model_dump_json(),
                rewards=[item for item in new_floors if item.campaign_id == campaign_id],
                decisions=[item for item in decisions if item.campaign_id == campaign_id],
            )
        floors = frozen_floors + new_floors
        vector = aggregate_productive_weights(floors, hotkey_to_uid, uids)
        weights = {uid: float(vector[index]) for index, uid in enumerate(uids)}
        if persist:
            self.store.persist_shadow_weights(block, feed.snapshot_id, weights)
        return weights, floors

    def pending_reward_campaign_ids(
        self,
        feed: CampaignFeed,
        *,
        block: int,
    ) -> tuple[str, ...]:
        """Return active campaigns whose final economics are not frozen yet."""

        records = {item.access.campaign_id: item for item in self.store.reconciled_campaigns()}
        records.update({item.access.campaign_id: item for item in feed.campaigns})
        pending: list[str] = []
        for campaign in records.values():
            if campaign.access.mining_protocol is not MiningProtocol.PRECLAIM_V2:
                continue
            start = campaign.emission_start_block
            end = campaign.emission_end_block
            if start is None or end is None or not start <= block <= end:
                continue
            campaign_id = campaign.access.campaign_id
            frozen = self.store.campaign_rewards(campaign_id, campaign.model_dump_json())
            completed = (
                campaign_id in self._completed_campaign_ids
                if self._completed_campaign_ids is not None
                else self.store.campaign_reconciled(campaign_id)
            )
            if frozen is None and not completed:
                pending.append(campaign.access.campaign_id)
        return tuple(sorted(pending))


def _reward_tweet(
    campaign: CampaignRecord,
    item: ScoredAttribution,
) -> RewardTweet:
    return RewardTweet(
        campaign_id=campaign.access.campaign_id,
        tweet_id=item.attribution.tweet_id,
        creator_x_id=item.tweet.author_x_id,
        miner_hotkey=item.attribution.miner_hotkey or "",
        score=item.score,
        author_username=item.tweet.author,
        followers_count=item.author_followers_count,
        views_count=item.tweet.views_count,
        favorite_count=item.tweet.favorite_count,
        retweet_count=item.tweet.retweet_count,
        reply_count=item.tweet.reply_count,
        quote_count=item.tweet.quote_count,
        bookmark_count=item.tweet.bookmark_count,
        engagement_usernames=item.engagements,
    )
