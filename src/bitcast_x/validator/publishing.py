"""Adapt frozen v3 campaign outcomes onto the existing brief-tweets wire contract."""

import hashlib
import json
import logging
import time
from collections import defaultdict
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from bitcast_x.campaigns import CampaignFeed, CampaignRecord
from bitcast_x.errors import ProtocolError
from bitcast_x.protocol import AttributionReason, AttributionResult
from bitcast_x.publishing import BRIEF_TWEETS_PAYLOAD_TYPE, DataPublisher
from bitcast_x.rewards import RewardDecision, TweetReward
from bitcast_x.validator.preview import PreviewStore
from bitcast_x.validator.rewards import preview_performance_rewards
from bitcast_x.validator.scoring import ScoredAttribution
from bitcast_x.validator.store import ValidatorStore

LOGGER = logging.getLogger(__name__)

_PREVIEW_PUBLICATION_RETRY = timedelta(minutes=1)


class ShadowResultPublisher:
    """Publish each active campaign once using a deterministic idempotency run ID."""

    def __init__(
        self,
        store: ValidatorStore,
        publisher: DataPublisher,
        *,
        endpoint: str,
        preview_store: PreviewStore | None = None,
    ) -> None:
        self.store = store
        self._preview_store = preview_store or PreviewStore(store.path.parent / "preview-cache")
        self._publisher = publisher
        self._endpoint = endpoint

    async def publish_preview(
        self,
        feed: CampaignFeed,
        campaign: CampaignRecord,
        scored: list[ScoredAttribution],
        attributions: list[AttributionResult],
        *,
        block: int,
        hotkey_to_uid: dict[str, int],
    ) -> bool:
        """Publish a replaceable result snapshot for miner visibility."""

        scored_tweet_ids = {item.attribution.tweet_id for item in scored}
        publishable_attributions = [
            item for item in attributions if not item.accepted or item.tweet_id in scored_tweet_ids
        ]
        if not publishable_attributions:
            return False
        now = datetime.now(UTC)
        preview_rewards = preview_performance_rewards(campaign, scored)
        payload = create_brief_tweets_payload(
            campaign,
            preview_rewards,
            {(item.attribution.campaign_id, item.attribution.tweet_id): item for item in scored},
            hotkey_to_uid,
            attributions=publishable_attributions,
            timestamp=now,
        )
        semantic_payload = dict(payload)
        semantic_payload.pop("timestamp", None)
        payload_hash = hashlib.sha256(
            json.dumps(semantic_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        existing = self._preview_store.preview_publication(campaign.access.campaign_id)
        if existing is not None and existing.payload_hash == payload_hash:
            if existing.succeeded:
                return False
            if now - existing.attempted_at < _PREVIEW_PUBLICATION_RETRY:
                return False
            payload = existing.payload
            run_id = existing.run_id
        else:
            run_id = (
                f"v3-preview:{feed.snapshot_id}:{campaign.access.campaign_id}:{payload_hash[:16]}"
            )
        success = await self._publisher.publish(
            endpoint=self._endpoint,
            payload_type=BRIEF_TWEETS_PAYLOAD_TYPE,
            run_id=run_id,
            payload=payload,
        )
        self._preview_store.record_preview_publication(
            campaign.access.campaign_id,
            payload_hash=payload_hash,
            run_id=run_id,
            payload=payload,
            attempted_at=now,
            succeeded=success,
        )
        LOGGER.info(
            "event=campaign_preview_publication campaign=%s block=%s success=%s rows=%s",
            campaign.access.campaign_id,
            block,
            str(success).lower(),
            len(publishable_attributions),
        )
        return success

    async def publish(
        self,
        feed: CampaignFeed,
        scored: list[ScoredAttribution],
        rewards: list[TweetReward],
        *,
        block: int,
        hotkey_to_uid: dict[str, int],
        completed_campaign_ids: Collection[str] | None = None,
    ) -> int:
        """Publish active final results and replaceable zero-value status updates."""

        scored_by_key = {
            (item.attribution.campaign_id, item.attribution.tweet_id): item for item in scored
        }
        rewards_by_campaign: dict[str, list[TweetReward]] = defaultdict(list)
        for reward in rewards:
            rewards_by_campaign[reward.campaign_id].append(reward)
        published = 0
        records = {item.access.campaign_id: item for item in self.store.reconciled_campaigns()}
        records.update({item.access.campaign_id: item for item in feed.campaigns})
        for campaign in sorted(records.values(), key=lambda item: item.access.campaign_id):
            start = campaign.emission_start_block
            end = campaign.emission_end_block
            campaign_id = campaign.access.campaign_id
            if start is None or end is None or not start <= block <= end:
                continue
            if self.store.publication_succeeded(feed.snapshot_id, campaign_id):
                continue
            campaign_rewards = rewards_by_campaign.get(campaign_id, [])
            frozen_economics = self.store.campaign_rewards(
                campaign_id,
                campaign.model_dump_json(),
            )
            if frozen_economics is None:
                completed = (
                    campaign_id in completed_campaign_ids
                    if completed_campaign_ids is not None
                    else self.store.campaign_reconciled(campaign_id)
                )
                attributions = (
                    self.store.reconciliation(
                        feed.snapshot_id,
                        campaign_id,
                        campaign.model_dump_json(),
                    )
                    if completed
                    else None
                )
                if attributions is not None:
                    await self.publish_preview(
                        feed,
                        campaign,
                        [item for item in scored if item.attribution.campaign_id == campaign_id],
                        attributions,
                        block=block,
                        hotkey_to_uid=hotkey_to_uid,
                    )
                LOGGER.warning(
                    "final publication deferred campaign=%s block=%s; "
                    "positive reward assignment is incomplete",
                    campaign_id,
                    block,
                )
                continue
            frozen_rewards, reward_decisions = frozen_economics
            if campaign_rewards != frozen_rewards:
                raise ProtocolError(f"campaign {campaign_id} rewards changed before publication")
            stored_scores = self.store.scored_reconciliation(feed.snapshot_id, campaign_id) or []
            for item in stored_scores:
                scored_by_key.setdefault(
                    (item.attribution.campaign_id, item.attribution.tweet_id), item
                )
            attributions = self.store.reconciliation(
                feed.snapshot_id,
                campaign_id,
                campaign.model_dump_json(),
            )
            if attributions is None:
                raise ProtocolError(
                    f"campaign {campaign_id} cannot be published before reconciliation"
                )
            payload = create_brief_tweets_payload(
                campaign,
                campaign_rewards,
                scored_by_key,
                hotkey_to_uid,
                attributions=attributions,
                reward_decisions=reward_decisions,
            )
            run_id = f"v3:{feed.snapshot_id}:{campaign_id}"
            success = await self._publisher.publish(
                endpoint=self._endpoint,
                payload_type=BRIEF_TWEETS_PAYLOAD_TYPE,
                run_id=run_id,
                payload=payload,
            )
            self.store.record_publication(
                feed.snapshot_id,
                campaign_id,
                run_id=run_id,
                payload=payload,
                succeeded=success,
            )
            published += int(success)
            LOGGER.info(
                "event=campaign_publication campaign=%s protocol=preclaim_v2 "
                "success=%s rows=%s timestamp_seconds=%s",
                campaign_id,
                str(success).lower(),
                len(attributions),
                int(time.time()),
            )
        return published


def create_brief_tweets_payload(
    campaign: CampaignRecord,
    rewards: list[TweetReward],
    scored_by_key: dict[tuple[str, str], ScoredAttribution],
    hotkey_to_uid: dict[str, int],
    *,
    attributions: list[AttributionResult] | None = None,
    reward_decisions: list[RewardDecision] | None = None,
    timestamp: datetime | None = None,
) -> dict[str, object]:
    """Add v3 attribution and protocol decisions to the frozen v2 payload."""

    campaign_id = campaign.access.campaign_id
    tweets: list[dict[str, object]] = []
    uid_targets: dict[int, float] = defaultdict(float)
    rewards_by_tweet = {item.tweet_id: item for item in rewards}
    campaign_scores = sorted(
        (
            item
            for (item_campaign_id, _tweet_id), item in scored_by_key.items()
            if item_campaign_id == campaign_id
        ),
        key=lambda item: item.attribution.tweet_id,
    )
    for scored in campaign_scores:
        reward = rewards_by_tweet.get(scored.attribution.tweet_id)
        tweet = scored.tweet
        attribution = scored.attribution
        miner_hotkey = attribution.miner_hotkey
        uid = hotkey_to_uid.get(miner_hotkey) if miner_hotkey is not None else None
        if (
            reward is not None
            and reward.daily_usd_floor > 0
            and (miner_hotkey != reward.miner_hotkey or uid is None)
        ):
            raise ProtocolError(
                f"rewarded tweet {tweet.tweet_id} has no unambiguous registered miner"
            )
        if reward is not None and reward.daily_usd_floor > 0 and uid is not None:
            uid_targets[uid] += reward.daily_usd_floor
        daily_floor = reward.daily_usd_floor if reward is not None else 0.0
        tweets.append(
            {
                "tweet_id": tweet.tweet_id,
                "author": tweet.author,
                "text": tweet.text,
                "created_at": tweet.created_at.strftime("%a %b %d %H:%M:%S %z %Y"),
                "lang": tweet.lang,
                "favorite_count": tweet.favorite_count,
                "retweet_count": tweet.retweet_count,
                "reply_count": tweet.reply_count,
                "quote_count": tweet.quote_count,
                "bookmark_count": tweet.bookmark_count,
                "views_count": tweet.views_count,
                "score": reward.score if reward is not None else scored.score,
                "performance_bonus_pct": (
                    reward.performance_bonus_pct if reward is not None else 0.0
                ),
                "performance_bonus_breakdown": (
                    reward.performance_bonus_breakdown if reward is not None else None
                ),
                "featured_tweet_bonus": (
                    reward.featured_tweet_bonus if reward is not None else False
                ),
                "retweets": [
                    item.username for item in scored.details if item.engagement_type == "retweet"
                ],
                "quotes": [
                    item.username for item in scored.details if item.engagement_type == "quote"
                ],
                "author_influence": scored.author_influence,
                "baseline_score": scored.baseline_score,
                "score_breakdown": [
                    {
                        "u": item.username,
                        "t": "rt" if item.engagement_type == "retweet" else "qt",
                        "i": item.influence_score,
                        "s": item.scale_factor,
                        "c": item.weighted_contribution,
                    }
                    for item in scored.details
                ],
                "meets_brief": scored.meets_brief,
                "reasoning": scored.brief_reasoning,
                "prescreen_passed": True,
                "usd_target": daily_floor,
                "total_usd_target": daily_floor * 7,
                # These legacy fields have no preclaim_v2 equivalent. Null preserves
                # their established meanings instead of publishing fabricated values.
                "alpha_target": None,
                "weight": None,
                "attribution": {
                    "status": "accepted",
                    "reason": attribution.reason.value,
                    "miner_hotkey": miner_hotkey,
                    "miner_uid": uid,
                    "submission_id": attribution.submission_id,
                    "claim_id": attribution.claim_id,
                    "mining_protocol": campaign.access.mining_protocol.value,
                    "mechanism_id": campaign.access.mechanism_id,
                    "winner_score": attribution.winner_score,
                    "runner_up_score": attribution.runner_up_score,
                },
            }
        )
    now = timestamp or datetime.now(UTC)
    campaign_attributions = (
        [item.attribution for item in campaign_scores]
        if attributions is None
        else [item for item in attributions if item.campaign_id == campaign_id]
    )
    reward_decisions_by_tweet = {
        item.tweet_id: item for item in reward_decisions or [] if item.campaign_id == campaign_id
    }
    meets_brief_by_tweet = {item.attribution.tweet_id: item.meets_brief for item in campaign_scores}
    attribution_decisions = [
        _attribution_decision(
            campaign,
            item,
            hotkey_to_uid,
            finalized=reward_decisions is not None,
            reward_decision=reward_decisions_by_tweet.get(item.tweet_id),
            meets_brief=meets_brief_by_tweet.get(item.tweet_id),
        )
        for item in sorted(
            campaign_attributions,
            key=lambda item: (
                item.tweet_id,
                item.submission_id or "",
                item.claim_id or "",
                item.reason.value,
            ),
        )
    ]
    return {
        "brief_id": campaign_id,
        "tweets": tweets,
        "attribution_decisions": attribution_decisions,
        "featured_tweet": _featured_selection(campaign_id, rewards, scored_by_key),
        "summary": {
            "total_tweets": len(tweets),
            "total_usd_target": sum(uid_targets.values()),
            "unique_creators": len({item.tweet.author_x_id for item in campaign_scores}),
            "uid_usd_targets": dict(uid_targets),
            "attribution_accepted": sum(item.accepted for item in campaign_attributions),
            "attribution_pending": sum(item.pending for item in campaign_attributions),
            "attribution_rejected": sum(
                not item.accepted and not item.pending for item in campaign_attributions
            ),
        },
        "timestamp": now.isoformat(),
    }


def _attribution_decision(
    campaign: CampaignRecord,
    attribution: AttributionResult,
    hotkey_to_uid: dict[str, int],
    *,
    finalized: bool,
    reward_decision: RewardDecision | None,
    meets_brief: bool | None,
) -> dict[str, object]:
    miner_hotkey = attribution.miner_hotkey
    if attribution.pending:
        reward_status = "pending"
        reward_reason = attribution.reason.value
        daily_usd_floor = None
    elif not finalized:
        reward_status = "pending"
        reward_reason = None
        daily_usd_floor = None
    elif reward_decision is not None:
        reward_status = "rewarded" if reward_decision.accepted else "not_rewarded"
        reward_reason = reward_decision.reason.value
        daily_usd_floor = reward_decision.daily_usd_floor
    else:
        if attribution.accepted and meets_brief is None:
            reward_status = "pending"
            reward_reason = AttributionReason.EVIDENCE_UNAVAILABLE.value
            daily_usd_floor = None
        else:
            reward_status = "not_rewarded"
            reward_reason = (
                "brief_filter_rejected"
                if attribution.accepted and meets_brief is False
                else attribution.reason.value
            )
            daily_usd_floor = 0.0
    return {
        "tweet_id": attribution.tweet_id,
        "campaign_id": attribution.campaign_id,
        "status": (
            "pending" if attribution.pending else "accepted" if attribution.accepted else "rejected"
        ),
        "reason": attribution.reason.value,
        "miner_hotkey": miner_hotkey,
        "miner_uid": hotkey_to_uid.get(miner_hotkey) if miner_hotkey is not None else None,
        "submission_id": attribution.submission_id,
        "claim_id": attribution.claim_id,
        "mining_protocol": campaign.access.mining_protocol.value,
        "mechanism_id": campaign.access.mechanism_id,
        "winner_score": attribution.winner_score,
        "runner_up_score": attribution.runner_up_score,
        "reward_status": reward_status,
        "reward_reason": reward_reason,
        "daily_usd_floor": daily_usd_floor,
    }


def _featured_selection(
    campaign_id: str,
    rewards: list[TweetReward],
    scored_by_key: dict[tuple[str, str], ScoredAttribution],
) -> dict[str, object] | None:
    featured_id = next((item.featured_tweet_id for item in rewards if item.featured_tweet_id), None)
    if featured_id is None:
        return None
    selected = scored_by_key[(campaign_id, featured_id)]
    ranked = sorted(
        (scored_by_key[(campaign_id, item.tweet_id)] for item in rewards),
        key=lambda item: (-item.tweet.views_count, item.tweet.tweet_id),
    )[:5]
    return {
        "brief_id": campaign_id,
        "tweet_id": featured_id,
        "author": selected.tweet.author,
        "views_count": selected.tweet.views_count,
        "selected_at": max(item.tweet.created_at for item in ranked).isoformat(),
        "selection_pool": sorted(item.tweet.tweet_id for item in ranked),
        "selection_method": "sha256_mod",
    }
