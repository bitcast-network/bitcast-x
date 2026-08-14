"""Legacy first-run freeze, replay, and burn-residual tests."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bitcast_x.campaigns import CampaignFeed, CampaignRecord
from bitcast_x.legacy import (
    Connection,
    LegacyPricingSnapshot,
    LegacyRewardCoordinator,
    LegacyRewardSnapshot,
    LegacySnapshotStore,
    LegacyTweetReward,
)
from bitcast_x.protocol import AttributionReason, AttributionResult, CampaignAccess, MiningProtocol
from bitcast_x.validator.scoring import ScoredAttribution
from bitcast_x.x_provider import Tweet

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)


def _campaign() -> CampaignRecord:
    return CampaignRecord(
        access=CampaignAccess(
            campaign_id="legacy",
            mechanism_id=1,
            mining_protocol=MiningProtocol.LEGACY_CONNECTION,
            scoring_close_block=20,
        ),
        title="legacy",
        brief="brief",
        ecosystem_id="tao",
        opens_at=NOW,
        closes_at=NOW + timedelta(days=1),
        reward_pool_usd="700",
        emission_start_block=30,
        emission_end_block=40,
    )


def _score() -> ScoredAttribution:
    return ScoredAttribution(
        attribution=AttributionResult(
            tweet_id="1",
            campaign_id="legacy",
            accepted=True,
            reason=AttributionReason.ACCEPTED,
            miner_hotkey=MINER,
        ),
        tweet=Tweet(
            tweet_id="1",
            author_x_id="9",
            created_at=NOW,
            text="tweet",
            author="alice",
        ),
        score=10,
        author_influence=5,
        baseline_score=10,
        details=(),
    )


def test_first_run_freezes_then_replays_same_legacy_weight(tmp_path: Path) -> None:
    feed = CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(_campaign(),),
        ecosystem_maps=(),
    )
    coordinator = LegacyRewardCoordinator(LegacySnapshotStore(tmp_path))
    pricing = LegacyPricingSnapshot(alpha_price_usd=1, daily_miner_alpha=1000)

    assert coordinator.scoring_feed(feed).campaigns == feed.campaigns

    first_weights, first_floors = coordinator.calculate(
        feed,
        [_score()],
        block=35,
        hotkey_to_uid={MINER: 7},
        uids=[0, 7],
        pricing=pricing,
        now=NOW,
    )
    replay_weights, replay_floors = coordinator.calculate(
        feed,
        [],
        block=35,
        hotkey_to_uid={MINER: 7},
        uids=[0, 7],
        pricing=pricing,
        now=NOW + timedelta(days=1),
    )

    assert first_weights == {0: 0.9, 7: 0.1}
    assert replay_weights == first_weights
    assert first_floors[0].daily_usd_floor == 100
    assert replay_floors[0].daily_usd_floor == 100
    assert len(list((tmp_path / "tao").glob("legacy_*.json"))) == 1
    assert coordinator.scoring_feed(feed).campaigns == ()


def test_scoring_feed_keeps_unfrozen_and_nonlegacy_campaigns(tmp_path: Path) -> None:
    frozen = _campaign()
    unfrozen = _campaign().model_copy(
        update={
            "access": _campaign().access.model_copy(update={"campaign_id": "unfrozen"}),
        }
    )
    preclaim = _campaign().model_copy(
        update={
            "access": _campaign().access.model_copy(
                update={"campaign_id": "preclaim", "mining_protocol": "preclaim_v2"}
            ),
        }
    )
    feed = CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(frozen, unfrozen, preclaim),
        ecosystem_maps=(),
    )
    coordinator = LegacyRewardCoordinator(LegacySnapshotStore(tmp_path))
    coordinator.calculate(
        CampaignFeed(
            snapshot_id="snapshot",
            published_at=NOW,
            campaigns=(frozen,),
            ecosystem_maps=(),
        ),
        [_score()],
        block=35,
        hotkey_to_uid={MINER: 7},
        uids=[0, 7],
        pricing=LegacyPricingSnapshot(alpha_price_usd=1, daily_miner_alpha=1000),
        now=NOW,
    )

    assert [
        campaign.access.campaign_id for campaign in coordinator.scoring_feed(feed).campaigns
    ] == ["unfrozen", "preclaim"]


def test_replay_preserves_published_bonus_fields(tmp_path: Path) -> None:
    snapshot = LegacyRewardSnapshot(
        brief_id="legacy",
        pool_name="tao",
        created_at=NOW,
        tweet_rewards=(
            LegacyTweetReward(
                tweet_id="1",
                author="alice",
                uid=7,
                total_usd=70,
                miner_hotkey=MINER,
                performance_bonus_pct=12.5,
                performance_bonus_breakdown={"views": 5.0},
                featured_tweet_bonus=True,
            ),
        ),
    )
    replayed = LegacyRewardCoordinator._replayed_floors(_campaign(), snapshot, {MINER: 7})
    assert replayed[0].performance_bonus_pct == 12.5
    assert replayed[0].performance_bonus_breakdown == {"views": 5.0}
    assert replayed[0].featured_tweet_bonus is True


def test_legacy_rewards_above_emissions_scale_and_never_make_negative_burn(
    tmp_path: Path,
) -> None:
    coordinator = LegacyRewardCoordinator(LegacySnapshotStore(tmp_path))
    assert coordinator._weights({"a": {7: 2000}}, {"a": 1.0}, [0, 7], 1000) == {
        0: 0.0,
        7: 1.0,
    }


def test_legacy_rewards_apply_each_campaign_cap_before_global_scaling(
    tmp_path: Path,
) -> None:
    coordinator = LegacyRewardCoordinator(LegacySnapshotStore(tmp_path))
    assert coordinator._weights(
        {"capped": {7: 800}, "uncapped": {8: 200}},
        {"capped": 0.5, "uncapped": 1.0},
        [0, 7, 8],
        1000,
    ) == pytest.approx({0: 0.3, 7: 0.5, 8: 0.2})


def test_due_referral_usd_is_added_to_legacy_vector_and_normalized(tmp_path: Path) -> None:
    coordinator = LegacyRewardCoordinator(LegacySnapshotStore(tmp_path))
    referral = Connection(
        connection_id=1,
        tweet_id="1",
        tag="Stitch3-a",
        account_username="alice",
        added="a",
        updated="b",
        referral_code="Ym9i",
        referred_by="bob",
        referee_amount=30,
        referrer_amount=20,
        payout_date=NOW.date(),
    )
    weights = coordinator.apply_referrals(
        {0: 0.9, 7: 0.1, 8: 0.0},
        (referral,),
        {"alice": 7, "bob": 8},
        daily_usd=1000,
    )
    assert weights == {
        0: 0.9 / 1.05,
        7: 0.13 / 1.05,
        8: 0.02 / 1.05,
    }
