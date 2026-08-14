"""Local legacy tweet discovery and connection-based attribution."""

import logging
from datetime import timedelta

from bitcast_x.campaigns import (
    CampaignFeed,
    CampaignRecord,
    eligible_creator_ids_for_campaign,
)
from bitcast_x.legacy.connections import ConnectionStore
from bitcast_x.legacy.constants import LEGACY_NOCODE_UID
from bitcast_x.legacy.tweet_store import LegacyTweetStore
from bitcast_x.protocol import AttributionReason, AttributionResult, MiningProtocol
from bitcast_x.validator.scoring import AttributionScorer, ScoredAttribution
from bitcast_x.x_provider import Tweet, XProvider

LOGGER = logging.getLogger(__name__)


def legacy_search_queries(campaign: CampaignRecord) -> tuple[str, ...]:
    """Reproduce v2's *separate* QRT and tag searches, unioned through the store.

    v2 issued one search per configured selector and merged both into the
    cumulative store (``discovery.discover_tweets``). ANDing them into a single
    query is strictly narrower: on ``064_bitcast`` the combined query returns 86
    results where the QRT-only and tag-only searches return 100 each, over
    materially different author sets. The tag/QRT conjunction still applies —
    ``LegacyTweetStore.campaign_tweets`` enforces it, matching v2's
    ``ContentFilter`` — this only widens what reaches the store.
    """

    window = (
        f"since:{campaign.opens_at:%Y-%m-%d} "
        f"until:{(campaign.closes_at + timedelta(days=1)):%Y-%m-%d}"
    )
    queries: list[str] = []
    if campaign.quoted_tweet_id is not None:
        queries.append(f"quoted_tweet_id:{campaign.quoted_tweet_id} {window}")
    if campaign.tag is not None:
        queries.append(f"{campaign.tag} {window}")
    if not queries:
        raise ValueError(f"legacy campaign {campaign.access.campaign_id} has no tag or QRT")
    return tuple(queries)


class LegacyAttributionEngine:
    """Discover legacy campaign tweets and attribute them through frozen connections."""

    def __init__(
        self,
        connections: ConnectionStore,
        x_provider: XProvider,
        scorer: AttributionScorer,
        tweet_store: LegacyTweetStore,
        *,
        nocode_uid: int = LEGACY_NOCODE_UID,
    ) -> None:
        self._connections = connections
        self._x = x_provider
        self._scorer = scorer
        self._tweets = tweet_store
        self._nocode_uid = nocode_uid

    def validate_state(self) -> None:
        """Fail readiness when the frozen connection import is unavailable or invalid."""

        self._connections.validate()
        self._tweets.validate()

    async def score_feed(
        self,
        feed: CampaignFeed,
        *,
        hotkey_to_uid: dict[str, int],
    ) -> list[ScoredAttribution]:
        """Return independently scored legacy attributions for the complete feed."""

        uid_to_hotkey = {uid: hotkey for hotkey, uid in hotkey_to_uid.items()}
        account_to_uid = self._connections.resolve_uids(hotkey_to_uid, nocode_uid=self._nocode_uid)
        attributions: list[AttributionResult] = []
        tweet_evidence: dict[str, Tweet] = {}
        for campaign in sorted(feed.campaigns, key=lambda item: item.access.campaign_id):
            if campaign.access.mining_protocol is not MiningProtocol.LEGACY_CONNECTION:
                continue
            for query in legacy_search_queries(campaign):
                result = await self._x.search_tweets(query, count=100)
                if not result.provider_available:
                    LOGGER.warning(
                        "legacy campaign search unavailable; using cached tweets "
                        "campaign_id=%s query=%s",
                        campaign.access.campaign_id,
                        query,
                    )
                else:
                    self._tweets.merge(result.tweets)
            for tweet in self._tweets.campaign_tweets(feed, campaign):
                tweet_evidence[tweet.tweet_id] = tweet
                miner_uid = account_to_uid.get(tweet.author.casefold())
                miner_hotkey = uid_to_hotkey.get(miner_uid) if miner_uid is not None else None
                if miner_hotkey is None or not self._eligible(feed, campaign, tweet):
                    continue
                attributions.append(
                    AttributionResult(
                        tweet_id=tweet.tweet_id,
                        campaign_id=campaign.access.campaign_id,
                        accepted=True,
                        reason=AttributionReason.ACCEPTED,
                        miner_hotkey=miner_hotkey,
                    )
                )
        legacy_feed = feed.model_copy(
            update={
                "campaigns": tuple(
                    item
                    for item in feed.campaigns
                    if item.access.mining_protocol is MiningProtocol.LEGACY_CONNECTION
                )
            }
        )
        return await self._scorer.score(
            legacy_feed,
            attributions,
            tweet_evidence=tweet_evidence,
        )

    @staticmethod
    def _eligible(feed: CampaignFeed, campaign: CampaignRecord, tweet: Tweet) -> bool:
        if not campaign.opens_at <= tweet.created_at <= campaign.closes_at:
            return False
        return tweet.author_x_id in eligible_creator_ids_for_campaign(feed, campaign)
