"""Legacy ingestion payload parity tests."""

from datetime import UTC, date, datetime

from bitcast_x.campaigns import CampaignRecord
from bitcast_x.legacy import (
    Connection,
    LegacyPricingSnapshot,
    LegacyRewardSnapshot,
    LegacyTweetReward,
    brief_tweets_payload,
    capped_monitoring_scores,
    connection_payload,
    referral_payload,
)
from bitcast_x.protocol import AttributionReason, AttributionResult, CampaignAccess, MiningProtocol
from bitcast_x.validator.scoring import ScoredAttribution
from bitcast_x.x_provider import Tweet

MINER_A = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
PRICING = LegacyPricingSnapshot(alpha_price_usd=2.0, daily_miner_alpha=50.0)


def _connection() -> Connection:
    return Connection(
        connection_id=1,
        tweet_id="123",
        tag="Stitch3-builder",
        account_username="alice",
        added="2026-01-01",
        updated="2026-01-01",
        referral_code="Ym9i",
        referred_by="bob",
        referee_amount=30,
        referrer_amount=20,
        payout_date=date(2026, 8, 9),
    )


def test_connection_payload_preserves_existing_wire_fields() -> None:
    payload = connection_payload((_connection(),), timestamp=datetime(2026, 8, 8, tzinfo=UTC))
    assert payload == {
        "connections": [
            {
                "tweet_id": 123,
                "tag": "Stitch3-builder",
                "username": "alice",
                "referred_by": "bob",
                "referee_amount": 30,
                "referrer_amount": 20,
                "referee_amount_usd": 30,
                "referrer_amount_usd": 20,
            }
        ],
        "timestamp": "2026-08-08T00:00:00",
    }


def test_referral_payload_uses_locked_amounts_and_existing_uids() -> None:
    payload = referral_payload(
        (_connection(),),
        {"alice": 114, "bob": 9},
        payout_date=date(2026, 8, 9),
        timestamp=datetime(2026, 8, 9, tzinfo=UTC),
    )
    assert payload["bonuses"] == [
        {
            "referee": "alice",
            "referrer": "bob",
            "referee_uid": 114,
            "referrer_uid": 9,
            "referee_amount_usd": 30,
            "referrer_amount_usd": 20,
        }
    ]
    assert payload["total_usd"] == 50


def test_legacy_brief_payload_has_v2_core_without_preclaim_fields() -> None:
    campaign = CampaignRecord(
        access=CampaignAccess(
            campaign_id="legacy",
            mechanism_id=1,
            mining_protocol=MiningProtocol.LEGACY_CONNECTION,
            scoring_close_block=1,
        ),
        title="legacy",
        brief="brief",
        ecosystem_id="tao",
        opens_at=datetime(2026, 8, 1, tzinfo=UTC),
        closes_at=datetime(2026, 8, 2, tzinfo=UTC),
        reward_pool_usd="700",
    )
    item = ScoredAttribution(
        attribution=AttributionResult(
            tweet_id="1",
            campaign_id="legacy",
            accepted=True,
            reason=AttributionReason.ACCEPTED,
            miner_hotkey=MINER_A,
        ),
        tweet=Tweet(
            tweet_id="1",
            author_x_id="9",
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            text="tweet",
            author="alice",
        ),
        score=10,
        author_influence=5,
        baseline_score=10,
        details=(),
    )
    payload = brief_tweets_payload(campaign, [], [item], {MINER_A: 7}, pricing=PRICING)
    assert "attribution_decisions" not in payload
    assert "attribution" not in payload["tweets"][0]  # type: ignore[index]
    assert payload["tweets"][0]["tweet_id"] == "1"  # type: ignore[index]


def test_legacy_brief_payload_replays_tweet_evidence_from_frozen_snapshot() -> None:
    campaign = CampaignRecord(
        access=CampaignAccess(
            campaign_id="legacy",
            mechanism_id=1,
            mining_protocol=MiningProtocol.LEGACY_CONNECTION,
            scoring_close_block=1,
        ),
        title="legacy",
        brief="brief",
        ecosystem_id="tao",
        opens_at=datetime(2026, 8, 1, tzinfo=UTC),
        closes_at=datetime(2026, 8, 2, tzinfo=UTC),
        reward_pool_usd="700",
    )
    snapshot = LegacyRewardSnapshot(
        brief_id="legacy",
        pool_name="tao",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        tweet_rewards=(
            LegacyTweetReward(
                tweet_id="44",
                author="alice",
                uid=114,
                total_usd=70,
                text="frozen evidence",
                views_count=99,
            ),
        ),
    )
    payload = brief_tweets_payload(
        campaign,
        [],
        [],
        {},
        pricing=PRICING,
        snapshot=snapshot,
    )
    tweet = payload["tweets"][0]  # type: ignore[index]
    assert tweet["tweet_id"] == "44"
    assert tweet["text"] == "frozen evidence"
    assert tweet["usd_target"] == 10
    assert tweet["alpha_target"] == 5
    assert tweet["weight"] == 0.1
    assert payload["summary"]["uid_usd_targets"] == {114: 10}  # type: ignore[index]


def test_legacy_snapshot_replay_excludes_fresh_search_candidates() -> None:
    campaign = CampaignRecord(
        access=CampaignAccess(
            campaign_id="legacy",
            mechanism_id=1,
            mining_protocol=MiningProtocol.LEGACY_CONNECTION,
            scoring_close_block=1,
        ),
        title="legacy",
        brief="brief",
        ecosystem_id="tao",
        opens_at=datetime(2026, 8, 1, tzinfo=UTC),
        closes_at=datetime(2026, 8, 2, tzinfo=UTC),
        reward_pool_usd="700",
    )
    fresh = ScoredAttribution(
        attribution=AttributionResult(
            tweet_id="45",
            campaign_id="legacy",
            accepted=True,
            reason=AttributionReason.ACCEPTED,
            miner_hotkey=MINER_A,
        ),
        tweet=Tweet(
            tweet_id="45",
            author_x_id="10",
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            text="new search result",
            author="bob",
        ),
        score=20,
        author_influence=4,
        baseline_score=20,
        details=(),
    )
    snapshot = LegacyRewardSnapshot(
        brief_id="legacy",
        pool_name="tao",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        tweet_rewards=(
            LegacyTweetReward(
                tweet_id="44",
                author="alice",
                uid=114,
                total_usd=70,
            ),
        ),
    )

    payload = brief_tweets_payload(
        campaign,
        [],
        [fresh],
        {MINER_A: 7},
        pricing=PRICING,
        snapshot=snapshot,
    )

    assert [tweet["tweet_id"] for tweet in payload["tweets"]] == ["44"]  # type: ignore[index]


def test_monitoring_scores_apply_v1_creator_cap_and_retain_failures() -> None:
    campaign = CampaignRecord(
        access=CampaignAccess(
            campaign_id="legacy",
            mechanism_id=1,
            mining_protocol=MiningProtocol.LEGACY_CONNECTION,
            scoring_close_block=10,
        ),
        title="legacy",
        brief="brief",
        ecosystem_id="tao",
        opens_at=datetime(2026, 8, 1, tzinfo=UTC),
        closes_at=datetime(2026, 8, 2, tzinfo=UTC),
        reward_pool_usd="700",
        max_tweets_per_creator=2,
    )

    def item(
        tweet_id: str,
        *,
        score: float,
        views: int,
        likes: int,
        meets_brief: bool = True,
    ) -> ScoredAttribution:
        return ScoredAttribution(
            attribution=AttributionResult(
                tweet_id=tweet_id,
                campaign_id="legacy",
                accepted=True,
                reason=AttributionReason.ACCEPTED,
                miner_hotkey=MINER_A,
            ),
            tweet=Tweet(
                tweet_id=tweet_id,
                author_x_id="9",
                created_at=datetime(2026, 8, int(tweet_id), tzinfo=UTC),
                text="tweet",
                author="Alice",
                views_count=views,
                favorite_count=likes,
            ),
            score=score,
            author_influence=5,
            baseline_score=10,
            details=(),
            meets_brief=meets_brief,
        )

    scores = [
        item("1", score=10, views=20, likes=3),
        item("2", score=10, views=30, likes=1),
        item("3", score=9, views=100, likes=100),
        item("4", score=0, views=0, likes=0, meets_brief=False),
    ]

    assert [item.attribution.tweet_id for item in capped_monitoring_scores(campaign, scores)] == [
        "1",
        "2",
        "4",
    ]
