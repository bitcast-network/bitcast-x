"""Tests for emission windows and equal exclusive/open economics."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import TypeAdapter

from bitcast_x.campaigns import CampaignFeed, CampaignRecord
from bitcast_x.protocol import AttributionReason, AttributionResult, CampaignAccess, MiningProtocol
from bitcast_x.rewards import RewardDecision
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
        display=campaign_id,
        brief="brief",
        pools=("eco",),
        opens_at=NOW,
        closes_at=NOW + timedelta(days=1),
        reward_pool_usd="700",
        emission_start_block=30,
        emission_end_block=40,
    )


def campaign_feed(*campaigns: CampaignRecord) -> CampaignFeed:
    return CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=campaigns,
        ecosystem_maps=(),
    )


def reward_coordinator(
    store: ValidatorStore,
    *,
    scorer: UnusedScorer | CountingScorer | None = None,
) -> RewardCoordinator:
    return RewardCoordinator(
        store,
        scorer if scorer is not None else UnusedScorer(),  # type: ignore[arg-type]
        score_blend=0.0,
    )


def scored(
    campaign_id: str,
    tweet_id: str,
    miner: str,
    *,
    score: float = 10.0,
    views: int = 0,
    favorites: int = 0,
    followers: int = 0,
    creator: str | None = None,
) -> ScoredAttribution:
    """Return an accepted, scored tweet; tweets sharing ``creator`` share an author."""

    author_x_id = creator or tweet_id
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
            author_x_id=author_x_id,
            created_at=NOW + timedelta(hours=1),
            text="tweet",
            author=f"creator{author_x_id}",
            views_count=views,
            favorite_count=favorites,
        ),
        score=score,
        author_influence=5.0,
        baseline_score=10.0,
        details=(),
        author_followers_count=followers,
    )


def test_exclusive_and_open_campaigns_use_identical_floor_and_multiplier(tmp_path: Path) -> None:
    coordinator = reward_coordinator(ValidatorStore(tmp_path / "validator.sqlite3"))

    weights, floors = coordinator.shadow_weights(
        campaign_feed(record("open"), record("exclusive", exclusive=MINER_B)),
        [scored("open", "1", MINER_A), scored("exclusive", "2", MINER_B)],
        block=35,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
    )

    assert weights == {0: 0.0, 1: 0.5, 2: 0.5}
    assert [item.daily_usd_floor for item in floors] == [100.0, 100.0]


def test_outside_emission_window_burns_without_provisional_payment(tmp_path: Path) -> None:
    coordinator = reward_coordinator(ValidatorStore(tmp_path / "validator.sqlite3"))

    weights, floors = coordinator.shadow_weights(
        campaign_feed(record("open")),
        [scored("open", "1", MINER_A)],
        block=29,
        hotkey_to_uid={MINER_A: 1},
        uids=[0, 1],
    )

    assert weights == {0: 1.0, 1: 0.0}
    assert floors == []


def test_preview_performance_rewards_are_zero_dollar_and_respect_current_cap() -> None:
    campaign = record("campaign").model_copy(update={"max_tweets_per_creator": 1})
    lower = scored("campaign", "1", MINER_A, creator="9")
    higher = scored(
        "campaign", "2", MINER_A, creator="9", score=20.0, views=1_000, favorites=100, followers=100
    )

    rewards = preview_performance_rewards(campaign, [lower, higher])

    assert [item.tweet_id for item in rewards] == ["2"]
    assert rewards[0].daily_usd_floor == 0.0
    assert rewards[0].performance_bonus_pct == 20.0


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
    campaign_b = record("b")
    store = ValidatorStore(tmp_path / "validator.sqlite3")

    weights, floors = reward_coordinator(store).shadow_weights(
        campaign_feed(record("a"), campaign_b),
        [scored("a", "1", MINER_A), scored("b", "1", MINER_B)],
        block=35,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
    )

    assert weights == {0: 0.0, 1: 1.0, 2: 0.0}
    assert [(item.campaign_id, item.tweet_id) for item in floors] == [("a", "1")]
    assert store.campaign_rewards("b", campaign_b.model_dump_json()) is None
    assert store.campaign_finalized("b") is False
    # Zero-value economics stay replaceable, so campaign_rewards() hides them;
    # the audit row still records why campaign b's copy of the tweet lost.
    with sqlite3.connect(tmp_path / "validator.sqlite3") as connection:
        (decisions_json,) = connection.execute(
            "SELECT decisions_json FROM campaign_rewards WHERE campaign_id = 'b'"
        ).fetchone()
    assert TypeAdapter(list[RewardDecision]).validate_json(decisions_json) == [
        RewardDecision(
            campaign_id="b",
            tweet_id="1",
            miner_hotkey=MINER_B,
            accepted=False,
            reason=AttributionReason.DUPLICATE_TWEET,
        )
    ]


def test_earlier_campaign_reserves_tweet_across_later_emission_window(tmp_path: Path) -> None:
    campaign_b = record("b").model_copy(
        update={"emission_start_block": 41, "emission_end_block": 50}
    )
    feed = campaign_feed(record("a"), campaign_b)
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    coordinator = reward_coordinator(store)
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


async def test_freeze_scores_skips_campaigns_without_a_stored_reconciliation(
    tmp_path: Path,
) -> None:
    # freeze_scores takes no block. The reconciler gates on close by storing a
    # reconciliation only once a campaign's close is finalized.
    reconciled = record("reconciled")
    feed = campaign_feed(reconciled, record("unreconciled"))
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    attribution = scored("reconciled", "1", MINER_A).attribution
    store.persist_reconciliation(
        snapshot_id=feed.snapshot_id,
        campaign_id="reconciled",
        campaign_json=reconciled.model_dump_json(),
        results=[attribution],
    )
    scorer = CountingScorer()

    result = await reward_coordinator(store, scorer=scorer).freeze_scores(feed, [attribution])

    assert result == []
    assert scorer.campaign_ids == ["reconciled"]
    assert store.scored_reconciliation("unreconciled") is None


async def test_only_current_cycle_completion_releases_zero_value_campaign(
    tmp_path: Path,
) -> None:
    campaign = record("campaign")
    feed = campaign_feed(campaign)
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    store.persist_reconciliation(
        snapshot_id="stale-snapshot",
        campaign_id="campaign",
        campaign_json=campaign.model_dump_json(),
        results=[],
    )
    coordinator = reward_coordinator(store, scorer=CountingScorer())

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
    initial_feed = campaign_feed(campaign)
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    item = scored("campaign", "1", MINER_A)
    store.persist_reconciliation(
        snapshot_id=initial_feed.snapshot_id,
        campaign_id="campaign",
        campaign_json=campaign.model_dump_json(),
        results=[item.attribution],
    )
    coordinator = reward_coordinator(store)

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
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    store.pin_featured_tweet_selection(
        campaign_id="campaign",
        campaign_json=campaign.model_dump_json(),
        tweet_id="1",
        selection_pool=("1",),
        selected_block=19,
        selected_at=NOW,
    )

    _weights, floors = reward_coordinator(store).shadow_weights(
        campaign_feed(campaign),
        [
            scored("campaign", "1", MINER_A, views=200),
            scored("campaign", "2", MINER_B, views=100),
        ],
        block=35,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
    )

    assert {item.featured_tweet_id for item in floors} == {"1"}
    assert {item.tweet_id for item in floors if item.featured_tweet_bonus} == {"1"}
    selection = store.featured_tweet_selection("campaign")
    assert selection is not None and selection.tweet_id == "1"


def test_ineligible_featured_pin_settles_without_bonus_or_replacement(tmp_path: Path) -> None:
    """A pin that no longer qualifies is dropped; it never stalls settlement.

    Covers the 2026-10 incident: a scoring-window edit excluded the pinned
    tweet, and settlement (with every weight submission) waited on it.
    """

    excluded = record("excluded")
    feed = campaign_feed(excluded, record("unrelated"))
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    store.pin_featured_tweet_selection(
        campaign_id="excluded",
        campaign_json=excluded.model_dump_json(),
        tweet_id="1",
        selection_pool=("1", "2"),
        selected_block=19,
        selected_at=NOW,
    )
    coordinator = reward_coordinator(store)

    weights, floors = coordinator.shadow_weights(
        feed,
        [scored("excluded", "2", MINER_A), scored("unrelated", "3", MINER_B)],
        block=35,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
    )

    assert weights[1] > 0 and weights[2] > 0
    excluded_floors = [item for item in floors if item.campaign_id == "excluded"]
    assert [item.tweet_id for item in excluded_floors] == ["2"]
    assert not any(item.featured_tweet_bonus for item in excluded_floors)
    assert all(item.featured_tweet_id is None for item in excluded_floors)
    assert store.campaign_rewards("excluded", excluded.model_dump_json()) is not None
    assert coordinator.pending_reward_campaign_ids(feed, block=35) == ()
    # The unused pin is cleared once rewards freeze so older releases agree on rollback.
    assert store.featured_tweet_selection("excluded") is None


def test_eligible_pin_keeps_bonus_when_capped_out_of_assignment(tmp_path: Path) -> None:
    """A pinned tweet still qualifies while eligible, even if not itself assigned."""

    campaign = record("campaign").model_copy(update={"max_tweets_per_creator": 1})
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    store.pin_featured_tweet_selection(
        campaign_id="campaign",
        campaign_json=campaign.model_dump_json(),
        tweet_id="1",
        selection_pool=("1", "2"),
        selected_block=19,
        selected_at=NOW,
    )

    _weights, floors = reward_coordinator(store).shadow_weights(
        campaign_feed(campaign),
        [
            scored("campaign", "1", MINER_A, creator="9"),
            scored("campaign", "2", MINER_A, creator="9", score=20.0),
        ],
        block=35,
        hotkey_to_uid={MINER_A: 1},
        uids=[0, 1],
    )

    assert [item.tweet_id for item in floors] == ["2"]
    assert floors[0].featured_tweet_id == "1"
    assert floors[0].featured_tweet_bonus is True


def test_featured_tweet_does_not_change_after_rewards_freeze(tmp_path: Path) -> None:
    feed = campaign_feed(record("campaign"))
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    coordinator = reward_coordinator(store)
    _weights, settled = coordinator.shadow_weights(
        feed,
        [scored("campaign", "1", MINER_A)],
        block=35,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
    )

    _weights, later = coordinator.shadow_weights(
        feed,
        [scored("campaign", "1", MINER_A), scored("campaign", "2", MINER_B, views=1_000_000)],
        block=36,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
    )

    # Without a pin the feature is selected at settlement, and no pin is written.
    assert {item.featured_tweet_id for item in settled} == {"1"}
    assert store.featured_tweet_selection("campaign") is None
    assert later == settled
