"""Temporary self-contained legacy campaign engine."""

from bitcast_x.legacy.cadence import LegacyCadence
from bitcast_x.legacy.collector import LegacyConnectionCollector, referral_reward
from bitcast_x.legacy.connections import Connection, ConnectionStore
from bitcast_x.legacy.engine import LegacyAttributionEngine, legacy_search_queries
from bitcast_x.legacy.pricing import LegacyPricingService, LegacyPricingSnapshot
from bitcast_x.legacy.publisher_service import LegacyResultPublisher
from bitcast_x.legacy.publishing import (
    brief_tweets_payload,
    capped_monitoring_scores,
    connection_payload,
    referral_payload,
)
from bitcast_x.legacy.rewards import LegacyRewardCoordinator
from bitcast_x.legacy.snapshots import (
    LegacyRewardSnapshot,
    LegacySnapshotStore,
    LegacyTweetReward,
    SnapshotReplay,
)
from bitcast_x.legacy.tweet_store import LegacyTweetStore

__all__ = [
    "Connection",
    "ConnectionStore",
    "LegacyConnectionCollector",
    "LegacyCadence",
    "LegacyRewardSnapshot",
    "LegacyResultPublisher",
    "LegacyRewardCoordinator",
    "LegacyPricingService",
    "LegacyPricingSnapshot",
    "LegacyAttributionEngine",
    "LegacySnapshotStore",
    "LegacyTweetReward",
    "LegacyTweetStore",
    "SnapshotReplay",
    "brief_tweets_payload",
    "capped_monitoring_scores",
    "connection_payload",
    "legacy_search_queries",
    "referral_payload",
    "referral_reward",
]
