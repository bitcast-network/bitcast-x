"""End-to-end legacy ingestion endpoint routing tests."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bitcast_x.campaigns import CampaignFeed, CampaignRecord
from bitcast_x.legacy import (
    ConnectionStore,
    LegacyPricingSnapshot,
    LegacyResultPublisher,
    LegacyRewardSnapshot,
    LegacySnapshotStore,
    LegacyTweetReward,
)
from bitcast_x.protocol import AttributionReason, AttributionResult, CampaignAccess, MiningProtocol
from bitcast_x.rewards import TweetReward
from bitcast_x.validator.scoring import ScoredAttribution
from bitcast_x.x_provider import Tweet

NOW = datetime(2026, 8, 9, tzinfo=UTC)


class Publisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def publish(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return True


def _state(path: Path) -> ConnectionStore:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE connections (
                connection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tweet_id BIGINT NOT NULL, tag TEXT NOT NULL,
                account_username TEXT NOT NULL UNIQUE, added TEXT NOT NULL,
                updated TEXT NOT NULL, referral_code TEXT, referred_by TEXT,
                referee_amount REAL, referrer_amount REAL, payout_date DATE
            )
            """
        )
        connection.execute("PRAGMA user_version = 2")
        connection.execute(
            """
            INSERT INTO connections (
                tweet_id, tag, account_username, added, updated, referral_code,
                referred_by, referee_amount, referrer_amount, payout_date
            ) VALUES (1, 'Stitch3-builder', 'alice', 'a', 'b', 'Ym9i',
                      'bob', 20, 20, ?)
            """,
            (NOW.date().isoformat(),),
        )
    return ConnectionStore(path)


def _campaign() -> CampaignRecord:
    return CampaignRecord(
        access=CampaignAccess(
            campaign_id="legacy",
            mechanism_id=1,
            mining_protocol=MiningProtocol.LEGACY_CONNECTION,
            scoring_close_block=1,
        ),
        title="legacy",
        brief="brief",
        ecosystem_id="tao",
        opens_at=NOW - timedelta(days=2),
        closes_at=NOW + timedelta(days=2),
        reward_pool_usd="700",
    )


async def test_publisher_routes_connections_tweets_and_referrals(tmp_path: Path) -> None:
    snapshots = LegacySnapshotStore(tmp_path / "snapshots")
    snapshots.write_new(
        LegacyRewardSnapshot(
            brief_id="legacy",
            pool_name="tao",
            created_at=NOW,
            tweet_rewards=(
                LegacyTweetReward(tweet_id="2", author="alice", uid=114, total_usd=70, text="old"),
            ),
        )
    )
    publisher = Publisher()
    service = LegacyResultPublisher(
        _state(tmp_path / "connections.db"),
        publisher,  # type: ignore[arg-type]
        data_client_url="https://ingestion.test",
        snapshots=snapshots,
    )
    feed = CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(_campaign(),),
        ecosystem_maps=(),
    )
    rewards = [
        TweetReward(
            campaign_id="legacy",
            tweet_id="2",
            creator_x_id="1",
            miner_hotkey="unused",
            score=1,
            daily_usd_floor=10,
        )
    ]
    assert (
        await service.publish(
            feed,
            [],
            rewards,
            block=123,
            hotkey_to_uid={},
            pricing=LegacyPricingSnapshot(alpha_price_usd=2.0, daily_miner_alpha=50.0),
            now=NOW,
        )
        == 3
    )

    assert [call["run_id"] for call in publisher.calls] == [
        f"v3-legacy:{feed.snapshot_id}:123:connections",
        f"v3-legacy:{feed.snapshot_id}:123:{feed.campaigns[0].access.campaign_id}",
        f"v3-legacy:{feed.snapshot_id}:123:referrals:{NOW.date()}",
    ]
    assert [call["endpoint"] for call in publisher.calls] == [
        "https://ingestion.test/api/v1/x-account-connections",
        "https://ingestion.test/api/v1/brief-tweets",
        "https://ingestion.test/api/v1/referral-bonuses",
    ]
    tweet_payload = publisher.calls[1]["payload"]
    assert tweet_payload["tweets"][0]["tweet_id"] == "2"
    assert "attribution" not in tweet_payload["tweets"][0]
    assert "attribution_decisions" not in tweet_payload
    assert publisher.calls[2]["payload"]["bonuses"][0]["referee_uid"] == 154


async def test_publisher_caps_monitoring_but_not_first_emission_scores(tmp_path: Path) -> None:
    publisher = Publisher()
    service = LegacyResultPublisher(
        _state(tmp_path / "connections.db"),
        publisher,  # type: ignore[arg-type]
        data_client_url="https://ingestion.test",
        snapshots=LegacySnapshotStore(tmp_path / "snapshots"),
    )
    campaign = _campaign().model_copy(
        update={
            "access": _campaign().access.model_copy(update={"scoring_close_block": 200}),
            "closes_at": NOW + timedelta(days=1),
            "max_tweets_per_creator": 1,
        }
    )
    feed = CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(campaign,),
        ecosystem_maps=(),
    )

    def scored(tweet_id: str, score: float) -> ScoredAttribution:
        return ScoredAttribution(
            attribution=AttributionResult(
                tweet_id=tweet_id,
                campaign_id="legacy",
                accepted=True,
                reason=AttributionReason.ACCEPTED,
                miner_hotkey="5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA",
            ),
            tweet=Tweet(
                tweet_id=tweet_id,
                author_x_id="1",
                created_at=NOW,
                text="tweet",
                author="alice",
            ),
            score=score,
            author_influence=1,
            baseline_score=2,
            details=(),
        )

    scores = [scored("1", 1), scored("2", 2)]
    await service.publish(
        feed,
        scores,
        [],
        block=200,
        hotkey_to_uid={},
        pricing=LegacyPricingSnapshot(alpha_price_usd=2.0, daily_miner_alpha=50.0),
        now=NOW,
    )
    assert [item["tweet_id"] for item in publisher.calls[1]["payload"]["tweets"]] == ["2"]
    monitoring_tweet = publisher.calls[1]["payload"]["tweets"][0]
    assert monitoring_tweet["performance_bonus_pct"] == 0.0
    assert monitoring_tweet["performance_bonus_breakdown"] == {
        "views": 0.0,
        "views_per_follower": 0.0,
        "total_engagements": 0.0,
        "engagement_per_view": 0.0,
    }
    assert monitoring_tweet["featured_tweet_bonus"] is True
    assert publisher.calls[1]["payload"]["featured_tweet"]["tweet_id"] == "2"
    assert (tmp_path / "featured" / "tao" / "legacy.json").exists()

    publisher.calls.clear()
    await service.publish(
        feed,
        scores,
        [],
        block=201,
        hotkey_to_uid={},
        pricing=LegacyPricingSnapshot(alpha_price_usd=2.0, daily_miner_alpha=50.0),
        now=NOW,
    )
    assert [item["tweet_id"] for item in publisher.calls[1]["payload"]["tweets"]] == ["1", "2"]
