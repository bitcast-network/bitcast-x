"""Tests for emission windows and equal exclusive/open economics."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from bitcast_x.campaigns import CampaignFeed, CampaignRecord, EcosystemMap
from bitcast_x.protocol import AttributionReason, AttributionResult, CampaignAccess, MiningProtocol
from bitcast_x.validator.rewards import RewardCoordinator, preview_performance_rewards
from bitcast_x.validator.scoring import ScoredAttribution
from bitcast_x.validator.store import ValidatorStore
from bitcast_x.x_provider import Tweet

MINER_A = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
MINER_B = "5FHneW46xGXgs5mUiveU4sbTyGBzmst2jfFvCw9zThqAXhGK"
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class UnusedScorer:
    async def score(
        self,
        _feed: object,
        _results: object,
        *,
        defer_unavailable_tweets: bool = False,
    ) -> list[ScoredAttribution]:
        del defer_unavailable_tweets
        raise AssertionError("scoring is not used by shadow_weights")


class CountingScorer:
    def __init__(self) -> None:
        self.campaign_ids: list[str] = []

    async def score(
        self,
        feed: CampaignFeed,
        results: list[AttributionResult],
        *,
        defer_unavailable_tweets: bool = False,
    ) -> list[ScoredAttribution]:
        del feed, defer_unavailable_tweets
        self.campaign_ids.extend(sorted({item.campaign_id for item in results}))
        return []


def record(campaign_id: str, *, exclusive: str | None = None) -> CampaignRecord:
    return CampaignRecord(
        access=CampaignAccess(
            campaign_id=campaign_id,
            mechanism_id=1,
            mining_protocol=MiningProtocol.PRECLAIM_V2,
            scoring_close_block=20,
            exclusive_miner_hotkey=exclusive,
        ),
        title=campaign_id,
        brief="brief",
        ecosystem_id="eco",
        opens_at=NOW,
        closes_at=NOW + timedelta(days=1),
        reward_pool_usd="700",
        emission_start_block=30,
        emission_end_block=40,
    )


def scored(campaign_id: str, tweet_id: str, miner: str) -> ScoredAttribution:
    return ScoredAttribution(
        attribution=AttributionResult(
            tweet_id=tweet_id,
            campaign_id=campaign_id,
            accepted=True,
            reason=AttributionReason.ACCEPTED,
            miner_hotkey=miner,
        ),
        tweet=Tweet(
            tweet_id=tweet_id,
            author_x_id=tweet_id,
            created_at=NOW + timedelta(hours=1),
            text="tweet",
            author=f"creator{tweet_id}",
        ),
        score=10.0,
        author_influence=5.0,
        baseline_score=10.0,
        details=(),
    )


def test_exclusive_and_open_campaigns_use_identical_floor_and_multiplier(tmp_path: Path) -> None:
    open_campaign = record("open")
    exclusive_campaign = record("exclusive", exclusive=MINER_B)
    feed = CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(open_campaign, exclusive_campaign),
        ecosystem_maps=(
            EcosystemMap(
                ecosystem_id="eco",
                name="Eco",
                eligible_creator_x_ids=("1", "2"),
                updated_at=NOW,
            ),
        ),
    )
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    coordinator = RewardCoordinator(store, UnusedScorer())  # type: ignore[arg-type]

    weights, floors = coordinator.shadow_weights(
        feed,
        [scored("open", "1", MINER_A), scored("exclusive", "2", MINER_B)],
        block=35,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
    )

    assert weights == {0: 0.0, 1: 0.5, 2: 0.5}
    assert [item.daily_usd_floor for item in floors] == [100.0, 100.0]


def test_outside_emission_window_burns_without_provisional_payment(tmp_path: Path) -> None:
    campaign = record("open")
    feed = CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(campaign,),
        ecosystem_maps=(),
    )
    coordinator = RewardCoordinator(
        ValidatorStore(tmp_path / "validator.sqlite3"),
        UnusedScorer(),  # type: ignore[arg-type]
    )

    weights, floors = coordinator.shadow_weights(
        feed,
        [scored("open", "1", MINER_A)],
        block=29,
        hotkey_to_uid={MINER_A: 1},
        uids=[0, 1],
    )

    assert weights == {0: 1.0, 1: 0.0}
    assert floors == []


def test_preview_performance_rewards_are_zero_dollar_and_respect_current_cap() -> None:
    campaign = record("campaign").model_copy(update={"max_tweets_per_creator": 1})
    lower = scored("campaign", "1", MINER_A).model_copy(
        update={
            "tweet": scored("campaign", "1", MINER_A).tweet.model_copy(
                update={
                    "author_x_id": "creator",
                    "author": "alice",
                    "views_count": 100,
                    "favorite_count": 5,
                }
            ),
            "author_followers_count": 100,
        }
    )
    higher = scored("campaign", "2", MINER_A).model_copy(
        update={
            "tweet": scored("campaign", "2", MINER_A).tweet.model_copy(
                update={
                    "author_x_id": "creator",
                    "author": "alice",
                    "views_count": 1_000,
                    "favorite_count": 100,
                }
            ),
            "score": 20.0,
            "author_followers_count": 100,
        }
    )

    rewards = preview_performance_rewards(campaign, [lower, higher])

    assert [item.tweet_id for item in rewards] == ["2"]
    assert rewards[0].daily_usd_floor == 0.0
    assert rewards[0].performance_bonus_pct == 20.0
    assert rewards[0].performance_bonus_breakdown == {
        "views": 5.0,
        "views_per_follower": 5.0,
        "total_engagements": 5.0,
        "engagement_per_view": 5.0,
    }


def test_preview_performance_rewards_distinguish_zero_metrics_from_no_reward() -> None:
    campaign = record("campaign")
    passing = scored("campaign", "1", MINER_A)
    failed = scored("campaign", "2", MINER_A).model_copy(update={"meets_brief": False})

    rewards = preview_performance_rewards(campaign, [passing, failed])

    assert [item.tweet_id for item in rewards] == ["1"]
    assert rewards[0].performance_bonus_pct == 0.0
    assert rewards[0].performance_bonus_breakdown == {
        "views": 0.0,
        "views_per_follower": 0.0,
        "total_engagements": 0.0,
        "engagement_per_view": 0.0,
    }


def test_same_tweet_is_globally_assigned_once_with_duplicate_reason(tmp_path: Path) -> None:
    campaign_a = record("a")
    campaign_b = record("b")
    feed = CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(campaign_a, campaign_b),
        ecosystem_maps=(),
    )
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    coordinator = RewardCoordinator(store, UnusedScorer())  # type: ignore[arg-type]

    weights, floors = coordinator.shadow_weights(
        feed,
        [scored("a", "1", MINER_A), scored("b", "1", MINER_B)],
        block=35,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
    )

    assert weights == {0: 0.0, 1: 1.0, 2: 0.0}
    assert [(item.campaign_id, item.tweet_id) for item in floors] == [("a", "1")]
    assert store.campaign_rewards("b", campaign_b.model_dump_json()) is None
    assert store.campaign_finalized("b") is False


def test_earlier_campaign_reserves_tweet_across_later_emission_window(tmp_path: Path) -> None:
    campaign_a = record("a")
    campaign_b = record("b").model_copy(
        update={"emission_start_block": 41, "emission_end_block": 50}
    )
    feed = CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(campaign_a, campaign_b),
        ecosystem_maps=(),
    )
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    coordinator = RewardCoordinator(store, UnusedScorer())  # type: ignore[arg-type]
    evidence = [scored("a", "1", MINER_A), scored("b", "1", MINER_B)]

    first_weights, _ = coordinator.shadow_weights(
        feed,
        evidence,
        block=35,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
    )
    later_weights, later_floors = coordinator.shadow_weights(
        feed,
        evidence,
        block=45,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
    )

    assert first_weights == {0: 0.0, 1: 1.0, 2: 0.0}
    assert later_weights == {0: 1.0, 1: 0.0, 2: 0.0}
    assert later_floors == []
    assert store.campaign_rewards("b", campaign_b.model_dump_json()) is None
    assert store.campaign_finalized("b") is False


async def test_freeze_scores_skips_campaigns_before_scoring_close(tmp_path: Path) -> None:
    closed = record("closed")
    future = record("future").model_copy(
        update={
            "access": record("future").access.model_copy(update={"scoring_close_block": 50}),
            "emission_start_block": 60,
            "emission_end_block": 70,
        }
    )
    feed = CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(closed, future),
        ecosystem_maps=(),
    )
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    attribution = scored("closed", "1", MINER_A).attribution
    store.persist_reconciliation(
        snapshot_id=feed.snapshot_id,
        campaign_id="closed",
        campaign_json=closed.model_dump_json(),
        results=[attribution],
    )
    scorer = CountingScorer()
    coordinator = RewardCoordinator(store, scorer)  # type: ignore[arg-type]

    result = await coordinator.freeze_scores(feed, [attribution])

    assert result == []
    assert scorer.campaign_ids == ["closed"]
    assert store.scored_reconciliation(feed.snapshot_id, "future") is None


async def test_only_current_cycle_completion_releases_zero_value_campaign(
    tmp_path: Path,
) -> None:
    campaign = record("campaign")
    feed = CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(campaign,),
        ecosystem_maps=(),
    )
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    store.persist_reconciliation(
        snapshot_id="stale-snapshot",
        campaign_id="campaign",
        campaign_json=campaign.model_dump_json(),
        results=[],
    )
    coordinator = RewardCoordinator(store, CountingScorer())  # type: ignore[arg-type]

    incomplete_scores = await coordinator.freeze_scores(
        feed,
        [],
        reconciled_campaign_ids=(),
    )
    coordinator.shadow_weights(
        feed,
        incomplete_scores,
        block=35,
        hotkey_to_uid={},
        uids=[0],
        persist=False,
    )

    assert coordinator.pending_reward_campaign_ids(feed, block=35) == ("campaign",)

    completed_scores = await coordinator.freeze_scores(
        feed,
        [],
        reconciled_campaign_ids=("campaign",),
    )
    weights, rewards = coordinator.shadow_weights(
        feed,
        completed_scores,
        block=35,
        hotkey_to_uid={},
        uids=[0],
        persist=False,
    )

    assert weights == {0: 1.0}
    assert rewards == []
    assert coordinator.pending_reward_campaign_ids(feed, block=35) == ()
    assert store.campaign_rewards("campaign", campaign.model_dump_json()) is None


def test_frozen_campaign_keeps_emitting_if_later_feed_omits_it(tmp_path: Path) -> None:
    campaign = record("campaign")
    initial_feed = CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(campaign,),
        ecosystem_maps=(),
    )
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    item = scored("campaign", "1", MINER_A)
    store.persist_reconciliation(
        snapshot_id=initial_feed.snapshot_id,
        campaign_id="campaign",
        campaign_json=campaign.model_dump_json(),
        results=[item.attribution],
    )
    coordinator = RewardCoordinator(store, UnusedScorer())  # type: ignore[arg-type]

    first, _ = coordinator.shadow_weights(
        initial_feed,
        [item],
        block=35,
        hotkey_to_uid={MINER_A: 1},
        uids=[0, 1],
    )
    rotated_feed = initial_feed.model_copy(update={"snapshot_id": "snapshot-2", "campaigns": ()})
    replay, floors = coordinator.shadow_weights(
        rotated_feed,
        [],
        block=36,
        hotkey_to_uid={MINER_A: 1},
        uids=[0, 1],
    )

    assert first == replay == {0: 0.0, 1: 1.0}
    assert len(floors) == 1


def test_final_rewards_replay_preview_feature_instead_of_reselecting(tmp_path: Path) -> None:
    campaign = record("campaign")
    feed = CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(campaign,),
        ecosystem_maps=(),
    )
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    store.pin_featured_tweet_selection(
        campaign_id="campaign",
        campaign_json=campaign.model_dump_json(),
        tweet_id="1",
        selection_pool=("1",),
        selected_block=19,
        selected_at=NOW,
    )
    first = scored("campaign", "1", MINER_A).model_copy(
        update={
            "tweet": scored("campaign", "1", MINER_A).tweet.model_copy(update={"views_count": 200})
        }
    )
    second = scored("campaign", "2", MINER_B).model_copy(
        update={
            "tweet": scored("campaign", "2", MINER_B).tweet.model_copy(update={"views_count": 100})
        }
    )
    coordinator = RewardCoordinator(store, UnusedScorer())  # type: ignore[arg-type]

    _weights, floors = coordinator.shadow_weights(
        feed,
        [first, second],
        block=35,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
    )

    assert {item.featured_tweet_id for item in floors} == {"1"}
    assert {item.tweet_id for item in floors if item.featured_tweet_bonus} == {"1"}
    selection = store.featured_tweet_selection("campaign", campaign.model_dump_json())
    assert selection is not None and selection.tweet_id == "1"


def test_final_rewards_wait_for_pinned_feature_evidence_then_self_heal(
    tmp_path: Path,
) -> None:
    campaign = record("campaign")
    feed = CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(campaign,),
        ecosystem_maps=(),
    )
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    selected = scored("campaign", "1", MINER_A)
    available = scored("campaign", "2", MINER_B)
    store.persist_reconciliation(
        snapshot_id="snapshot",
        campaign_id="campaign",
        campaign_json=campaign.model_dump_json(),
        results=[available.attribution],
    )
    store.pin_featured_tweet_selection(
        campaign_id="campaign",
        campaign_json=campaign.model_dump_json(),
        tweet_id="1",
        selection_pool=("1",),
        selected_block=19,
        selected_at=NOW,
    )
    coordinator = RewardCoordinator(store, UnusedScorer())  # type: ignore[arg-type]

    weights, floors = coordinator.shadow_weights(
        feed,
        [available],
        block=35,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
        persist=False,
    )

    assert weights == {0: 1.0, 1: 0.0, 2: 0.0}
    assert floors == []
    assert coordinator.pending_reward_campaign_ids(feed, block=35) == ("campaign",)
    assert store.campaign_rewards("campaign", campaign.model_dump_json()) is None

    recovered_weights, recovered_floors = coordinator.shadow_weights(
        feed,
        [selected, available],
        block=36,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
        persist=False,
    )

    assert recovered_weights[1] > 0
    assert recovered_weights[2] > 0
    assert len(recovered_floors) == 2
    assert coordinator.pending_reward_campaign_ids(feed, block=36) == ()
