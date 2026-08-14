"""Publish every legacy output from the self-contained v3 validator."""

import logging
import time
from dataclasses import replace
from datetime import UTC, date, datetime

from bitcast_x.campaigns import CampaignFeed
from bitcast_x.legacy.bonuses import FeaturedSelectionStore, monitoring_bonus_rewards
from bitcast_x.legacy.connections import ConnectionStore
from bitcast_x.legacy.constants import LEGACY_NOCODE_UID
from bitcast_x.legacy.pricing import LegacyPricingSnapshot
from bitcast_x.legacy.publishing import (
    ACCOUNT_CONNECTIONS_PAYLOAD_TYPE,
    REFERRAL_BONUSES_PAYLOAD_TYPE,
    brief_tweets_payload,
    capped_monitoring_scores,
    connection_payload,
    referral_payload,
)
from bitcast_x.legacy.snapshots import LegacySnapshotStore
from bitcast_x.protocol import MiningProtocol
from bitcast_x.publishing import BRIEF_TWEETS_PAYLOAD_TYPE, DataPublisher
from bitcast_x.rewards import TweetReward
from bitcast_x.validator.scoring import ScoredAttribution

LOGGER = logging.getLogger(__name__)


class LegacyResultPublisher:
    """Publish legacy tweets, connection state, and due referral liabilities."""

    def __init__(
        self,
        connections: ConnectionStore,
        publisher: DataPublisher,
        *,
        data_client_url: str,
        snapshots: LegacySnapshotStore,
        nocode_uid: int = LEGACY_NOCODE_UID,
    ) -> None:
        self._connections = connections
        self._publisher = publisher
        self._base = data_client_url.rstrip("/")
        self._nocode_uid = nocode_uid
        self._snapshots = snapshots
        self._featured = FeaturedSelectionStore(snapshots.root.parent / "featured")

    async def publish(
        self,
        feed: CampaignFeed,
        scored: list[ScoredAttribution],
        rewards: list[TweetReward],
        *,
        block: int,
        hotkey_to_uid: dict[str, int],
        pricing: LegacyPricingSnapshot,
        now: datetime | None = None,
    ) -> int:
        """Publish one deterministic cycle; return accepted request count."""

        timestamp = now or datetime.now(UTC)
        accepted = 0
        connections = self._connections.all()
        accepted += int(
            await self._publisher.publish(
                endpoint=f"{self._base}/api/v1/x-account-connections",
                payload_type=ACCOUNT_CONNECTIONS_PAYLOAD_TYPE,
                run_id=f"v3-legacy:{feed.snapshot_id}:{block}:connections",
                payload=connection_payload(connections, timestamp=timestamp),
            )
        )
        scores_by_campaign: dict[str, list[ScoredAttribution]] = {}
        for item in scored:
            scores_by_campaign.setdefault(item.attribution.campaign_id, []).append(item)
        for campaign in sorted(feed.campaigns, key=lambda item: item.access.campaign_id):
            if campaign.access.mining_protocol is not MiningProtocol.LEGACY_CONNECTION:
                continue
            campaign_scores = scores_by_campaign.get(campaign.access.campaign_id, [])
            campaign_rewards = [
                item for item in rewards if item.campaign_id == campaign.access.campaign_id
            ]
            snapshot = self._snapshots.load(campaign.primary_pool, campaign.access.campaign_id)
            publication_scores = campaign_scores
            if snapshot is None and block <= campaign.access.scoring_close_block:
                publication_scores = capped_monitoring_scores(campaign, campaign_scores)
                selection = self._featured.select(campaign, publication_scores, now=timestamp)
                campaign_rewards = monitoring_bonus_rewards(campaign, publication_scores, selection)
            else:
                selection = self._featured.load(campaign)
                if selection is not None:
                    campaign_rewards = [
                        replace(item, featured_tweet_id=selection.tweet_id)
                        for item in campaign_rewards
                    ]
            success = await self._publisher.publish(
                endpoint=f"{self._base}/api/v1/brief-tweets",
                payload_type=BRIEF_TWEETS_PAYLOAD_TYPE,
                run_id=(f"v3-legacy:{feed.snapshot_id}:{block}:{campaign.access.campaign_id}"),
                payload=brief_tweets_payload(
                    campaign,
                    campaign_rewards,
                    publication_scores,
                    hotkey_to_uid,
                    pricing=pricing,
                    timestamp=timestamp,
                    snapshot=snapshot,
                ),
            )
            accepted += int(success)
            LOGGER.info(
                "event=campaign_publication campaign=%s protocol=legacy_connection "
                "success=%s rows=%s timestamp_seconds=%s",
                campaign.access.campaign_id,
                str(success).lower(),
                len(publication_scores),
                int(time.time()),
            )
        account_to_uid = self._connections.resolve_uids(hotkey_to_uid, nocode_uid=self._nocode_uid)
        due = self._connections.due_referrals(timestamp.date())
        if due:
            payload = referral_payload(
                due,
                account_to_uid,
                payout_date=date.fromisoformat(timestamp.date().isoformat()),
                timestamp=timestamp,
            )
            if payload["bonuses"]:
                accepted += int(
                    await self._publisher.publish(
                        endpoint=f"{self._base}/api/v1/referral-bonuses",
                        payload_type=REFERRAL_BONUSES_PAYLOAD_TYPE,
                        run_id=(
                            f"v3-legacy:{feed.snapshot_id}:{block}:referrals:{timestamp.date()}"
                        ),
                        payload=payload,
                    )
                )
        return accepted
