"""Featured-pin release when an operator edit retro-excludes the pinned tweet.

Regression tests for the 2026-10 incident class: a campaign brief is edited
after its featured tweet was pinned, moving the scoring window so the pinned
tweet can no longer qualify. Reconciliation then rejects the pinned tweet on
every cycle while the fail-closed settlement gate defers the campaign's final
economics — and with them all mechanism weight submissions — for the rest of
the emission window.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from bitcast_x.campaigns import CampaignFeed, CampaignRecord
from bitcast_x.protocol import AttributionReason, AttributionResult, CampaignAccess, MiningProtocol
from bitcast_x.rewards import TweetReward
from bitcast_x.validator.rewards import RewardCoordinator
from bitcast_x.validator.scoring import ScoredAttribution
from bitcast_x.validator.store import ValidatorStore
from bitcast_x.x_provider import Tweet

MINER_A = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
MINER_B = "5FHneW46xGXgs5mUiveU4sbTyGBzmst2jfFvCw9zThqAXhGK"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


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


def record(
    campaign_id: str,
    *,
    opens_at: datetime = NOW,
    closes_at: datetime | None = None,
) -> CampaignRecord:
    return CampaignRecord(
        access=CampaignAccess(
            campaign_id=campaign_id,
            mechanism_id=1,
            mining_protocol=MiningProtocol.PRECLAIM_V2,
            scoring_close_block=20,
            exclusive_miner_hotkey=None,
        ),
        display=campaign_id,
        brief="brief",
        pools=("eco",),
        opens_at=opens_at,
        closes_at=closes_at or (opens_at + timedelta(days=1)),
        reward_pool_usd="700",
        emission_start_block=30,
        emission_end_block=40,
    )


def feed_of(campaign: CampaignRecord) -> CampaignFeed:
    return CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(campaign,),
        ecosystem_maps=(),
    )


def scored(
    campaign_id: str,
    tweet_id: str,
    miner: str,
    *,
    created_at: datetime | None = None,
) -> ScoredAttribution:
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
            created_at=created_at or (NOW + timedelta(hours=1)),
            text="tweet",
            author=f"creator{tweet_id}",
        ),
        score=10.0,
        author_influence=5.0,
        baseline_score=10.0,
        details=(),
    )


def pinned(
    store: ValidatorStore,
    campaign: CampaignRecord,
    tweet_id: str,
    *,
    selected_at: datetime,
) -> None:
    store.pin_featured_tweet_selection(
        campaign_id=campaign.access.campaign_id,
        campaign_json=campaign.model_dump_json(),
        tweet_id=tweet_id,
        selection_pool=(tweet_id,),
        selected_block=19,
        selected_at=selected_at,
    )


def persist_reconciliation(store: ValidatorStore, campaign: CampaignRecord) -> None:
    store.persist_reconciliation(
        snapshot_id="snapshot",
        campaign_id=campaign.access.campaign_id,
        campaign_json=campaign.model_dump_json(),
        results=[],
    )


async def test_edited_window_releases_pin_and_settlement_proceeds(tmp_path: Path) -> None:
    """Reproduction: brief edited to start the day after the pin was created.

    The pinned tweet scored while it was inside the original window, so its
    created_at survives in the persisted scores; the edit moved opens_at past
    it. The pin must be released and the campaign must settle without
    deferring weight submission.
    """

    original_window_open = NOW - timedelta(days=2)
    original_window_close = NOW - timedelta(days=1)
    campaign = record(
        "campaign",
        opens_at=original_window_open,
        closes_at=original_window_close,
    )
    store = ValidatorStore(
        tmp_path / "validator.sqlite3",
        finalized_block_provider=lambda: 10,
    )
    persist_reconciliation(store, campaign)
    # Normal cycle order: the contract binds before the pin exists.
    store.bind_campaign_protocols((campaign,))
    # Scored inside the ORIGINAL window, before the operator edit.
    pinned_tweet = scored(
        "campaign",
        "1",
        MINER_A,
        created_at=original_window_close - timedelta(hours=1),
    )
    store.persist_scores(feed_of(campaign).snapshot_id, "campaign", [pinned_tweet])
    # Pinned just before the original close, under the pre-edit contract.
    pinned(store, campaign, "1", selected_at=original_window_close - timedelta(minutes=1))
    # Mirror the #133 adoption path: the edited feed contract is bound while
    # scoring has not closed, refreshing the pin's stored contract.
    edited = campaign.model_copy(
        update={
            "opens_at": NOW,
            "closes_at": NOW + timedelta(hours=1),
        }
    )
    store.bind_campaign_protocols((edited,))
    edited_feed = feed_of(edited)
    qualifying = scored("campaign", "2", MINER_B)
    coordinator = RewardCoordinator(store, UnusedScorer())  # type: ignore[arg-type]

    # Before the release runs, the stuck pin defers all economics.
    stalled_weights, stalled_floors = coordinator.shadow_weights(
        edited_feed,
        [qualifying],
        block=35,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
        persist=False,
    )
    assert coordinator.pending_reward_campaign_ids(edited_feed, block=35) == ("campaign",)
    assert stalled_floors == []
    # The deferral branch falls back to the burn vector: all mass on uid 0.
    assert stalled_weights[0] == 1.0
    assert stalled_weights[1] == 0.0 and stalled_weights[2] == 0.0

    coordinator.release_ineligible_featured_selections(edited_feed)

    assert store.featured_tweet_selection("campaign", edited.model_dump_json()) is None
    events = store.featured_selection_release_events("campaign")
    assert len(events) == 1
    assert events[0]["tweet_id"] == "1"

    weights, floors = coordinator.shadow_weights(
        edited_feed,
        [qualifying],
        block=35,
        hotkey_to_uid={MINER_A: 1, MINER_B: 2},
        uids=[0, 1, 2],
        persist=False,
    )

    assert coordinator.pending_reward_campaign_ids(edited_feed, block=35) == ()
    assert floors
    assert weights[2] > 0


async def test_released_pin_is_never_reselected_from_excluded_tweet(tmp_path: Path) -> None:
    """After release, re-running the check is idempotent (no duplicate events)."""

    original_close = NOW - timedelta(days=1)
    campaign = record("campaign", opens_at=NOW - timedelta(days=2), closes_at=original_close)
    store = ValidatorStore(
        tmp_path / "validator.sqlite3",
        finalized_block_provider=lambda: 10,
    )
    persist_reconciliation(store, campaign)
    store.bind_campaign_protocols((campaign,))
    excluded = scored(
        "campaign",
        "1",
        MINER_A,
        created_at=original_close - timedelta(hours=1),
    )
    store.persist_scores(feed_of(campaign).snapshot_id, "campaign", [excluded])
    pinned(store, campaign, "1", selected_at=original_close - timedelta(minutes=1))
    edited = campaign.model_copy(update={"opens_at": NOW, "closes_at": NOW + timedelta(hours=1)})
    store.bind_campaign_protocols((edited,))
    coordinator = RewardCoordinator(store, UnusedScorer())  # type: ignore[arg-type]

    coordinator.release_ineligible_featured_selections(feed_of(edited))

    assert store.featured_tweet_selection("campaign", edited.model_dump_json()) is None
    # A release must be a one-time event per tweet, not a repeating action.
    coordinator.release_ineligible_featured_selections(feed_of(edited))
    assert len(store.featured_selection_release_events("campaign")) == 1


async def test_forward_window_move_without_scores_releases_via_pin_timestamp(
    tmp_path: Path,
) -> None:
    """A pin created after the edited close is released even with no stored score.

    When reconciliation already rejects the tweet under the edited contract,
    no persisted score exists to inspect. A lawful pin is always created
    before the contract's close, so a pin whose creation time is after the
    edited close proves the contract was replaced after pinning.
    """

    original_close = NOW - timedelta(days=1)
    campaign = record("campaign", opens_at=NOW - timedelta(days=2), closes_at=original_close)
    store = ValidatorStore(
        tmp_path / "validator.sqlite3",
        finalized_block_provider=lambda: 10,
    )
    persist_reconciliation(store, campaign)
    store.bind_campaign_protocols((campaign,))
    # Pinned before the original close; the tweet never scored afterwards,
    # so only the pin timestamp can witness the retro-edit.
    pinned(store, campaign, "1", selected_at=original_close - timedelta(minutes=1))
    edited = campaign.model_copy(update={"opens_at": NOW, "closes_at": NOW + timedelta(hours=1)})
    store.bind_campaign_protocols((edited,))
    coordinator = RewardCoordinator(store, UnusedScorer())  # type: ignore[arg-type]

    coordinator.release_ineligible_featured_selections(feed_of(edited))

    assert store.featured_tweet_selection("campaign", edited.model_dump_json()) is None
    assert len(store.featured_selection_release_events("campaign")) == 1


async def test_healthy_pin_is_untouched(tmp_path: Path) -> None:
    """A pin whose tweet still qualifies must never be released."""

    campaign = record("campaign")
    feed = feed_of(campaign)
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    persist_reconciliation(store, campaign)
    in_window = scored("campaign", "1", MINER_A)
    store.persist_scores(feed.snapshot_id, "campaign", [in_window])
    pinned(store, campaign, "1", selected_at=campaign.closes_at - timedelta(hours=1))
    coordinator = RewardCoordinator(store, UnusedScorer())  # type: ignore[arg-type]

    coordinator.release_ineligible_featured_selections(feed)

    assert store.featured_tweet_selection("campaign", campaign.model_dump_json()) is not None
    assert store.featured_selection_release_events("campaign") == []


async def test_transient_evidence_gap_keeps_conservative_deferral(tmp_path: Path) -> None:
    """A pin missing from the scored set without proof of exclusion still defers.

    Tweets that never scored under the active contract leave no persisted
    snapshot; without a deterministic witness the release must not fire, and
    the pin keeps blocking until evidence recovers — the pre-existing
    fail-closed behavior.
    """

    campaign = record("campaign")
    feed = feed_of(campaign)
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    persist_reconciliation(store, campaign)
    store.bind_campaign_protocols((campaign,))
    pinned(store, campaign, "1", selected_at=campaign.closes_at - timedelta(hours=1))
    coordinator = RewardCoordinator(store, UnusedScorer())  # type: ignore[arg-type]

    coordinator.release_ineligible_featured_selections(feed)

    assert store.featured_tweet_selection("campaign", campaign.model_dump_json()) is not None
    # The settlement gate itself still defers on the missing pin evidence.
    coordinator.shadow_weights(
        feed,
        [],
        block=35,
        hotkey_to_uid={MINER_A: 1},
        uids=[0, 1],
        persist=False,
    )
    assert coordinator.pending_reward_campaign_ids(feed, block=35) == ("campaign",)


async def test_frozen_economics_protect_the_pin_from_release(tmp_path: Path) -> None:
    """A campaign with settled economics never has its pin released."""

    campaign = record("campaign")
    feed = feed_of(campaign)
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    persist_reconciliation(store, campaign)
    store.bind_campaign_protocols((campaign,))
    in_window = scored("campaign", "1", MINER_A)
    store.persist_scores(feed.snapshot_id, "campaign", [in_window])
    pinned(store, campaign, "1", selected_at=campaign.closes_at - timedelta(hours=1))
    edited = campaign.model_copy(
        update={"opens_at": NOW - timedelta(days=6), "closes_at": NOW - timedelta(days=3)}
    )
    coordinator = RewardCoordinator(store, UnusedScorer())  # type: ignore[arg-type]

    # Freeze positive economics AFTER the pin, mimicking a settled campaign.
    store.persist_campaign_rewards(
        snapshot_id=feed.snapshot_id,
        campaign_id="campaign",
        campaign_json=campaign.model_dump_json(),
        rewards=[
            TweetReward(
                campaign_id="campaign",
                tweet_id="1",
                creator_x_id="1",
                miner_hotkey=MINER_A,
                score=10.0,
                daily_usd_floor=1.0,
            )
        ],
        decisions=[],
    )

    coordinator.release_ineligible_featured_selections(feed_of(edited))

    assert store.featured_selection_release_events("campaign") == []
