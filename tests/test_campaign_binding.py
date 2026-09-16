"""Campaign contracts remain pinned across feed changes and protocol retirement."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from bitcast_x.campaigns import CampaignFeed, CampaignRecord
from bitcast_x.errors import ProtocolError
from bitcast_x.protocol import CampaignAccess, MiningProtocol
from bitcast_x.rewards import TweetReward
from bitcast_x.validator.service import ensure_supported_campaigns
from bitcast_x.validator.store import ValidatorStore

NOW = datetime(2026, 8, 5, tzinfo=UTC)
HOTKEY = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
MUTABLE_CAMPAIGN_FIELDS = (
    "mining_protocol",
    "mechanism_id",
    "scoring_close_block",
    "exclusive_miner_hotkey",
    "display",
    "brief",
    "pools",
    "opens_at",
    "closes_at",
    "reward_pool_usd",
    "required_terms",
    "tag",
    "quoted_tweet_id",
    "inclusion_keywords",
    "prompt_version",
    "max_tweets_per_creator",
    "cap",
    "emission_start_block",
    "emission_end_block",
)


def campaign(
    campaign_id: str,
    protocol: MiningProtocol,
    exclusive_miner_hotkey: str | None = None,
) -> CampaignRecord:
    return CampaignRecord(
        access=CampaignAccess(
            campaign_id=campaign_id,
            mechanism_id=1,
            mining_protocol=protocol,
            scoring_close_block=20,
            exclusive_miner_hotkey=exclusive_miner_hotkey,
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


def mutate_campaign_contract(record: CampaignRecord, field: str) -> CampaignRecord:
    """Return one valid campaign whose named consensus field differs."""

    if field in {
        "mining_protocol",
        "mechanism_id",
        "scoring_close_block",
        "exclusive_miner_hotkey",
    }:
        access_updates: dict[str, object] = {
            "mining_protocol": MiningProtocol.LEGACY_CONNECTION,
            "mechanism_id": 2,
            "scoring_close_block": 21,
            "exclusive_miner_hotkey": HOTKEY,
        }
        return record.model_copy(
            update={"access": record.access.model_copy(update={field: access_updates[field]})}
        )
    updates: dict[str, object] = {
        "display": "changed display",
        "brief": "changed brief",
        "pools": ("other",),
        "opens_at": NOW - timedelta(hours=1),
        "closes_at": NOW + timedelta(days=2),
        "reward_pool_usd": "701",
        "required_terms": ("#required",),
        "tag": "#tag",
        "quoted_tweet_id": "123",
        "inclusion_keywords": ("keyword",),
        "prompt_version": 2,
        "max_tweets_per_creator": 2,
        "cap": 0.5,
        "emission_start_block": 31,
        "emission_end_block": 41,
    }
    return record.model_copy(update={field: updates[field]})


def feed(*campaigns: CampaignRecord) -> CampaignFeed:
    return CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=campaigns,
        ecosystem_maps=(),
    )


def freeze_positive_campaign(store: ValidatorStore, record: CampaignRecord) -> None:
    """Persist the smallest positive economic outcome that makes a contract final."""

    campaign_id = record.access.campaign_id
    campaign_json = record.model_dump_json()
    store.persist_reconciliation(
        snapshot_id="frozen",
        campaign_id=campaign_id,
        campaign_json=campaign_json,
        results=[],
    )
    store.persist_campaign_rewards(
        snapshot_id="frozen",
        campaign_id=campaign_id,
        campaign_json=campaign_json,
        rewards=[
            TweetReward(
                campaign_id=campaign_id,
                tweet_id="1",
                creator_x_id="creator",
                miner_hotkey=HOTKEY,
                score=1.0,
                daily_usd_floor=1.0,
            )
        ],
        decisions=[],
    )


def test_campaign_protocol_change_is_adopted_before_results_freeze(tmp_path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    original = campaign("same", MiningProtocol.LEGACY_CONNECTION)
    changed = campaign("same", MiningProtocol.PRECLAIM_V2)

    assert store.bind_campaign_protocols((original,)) == (original,)
    assert store.bind_campaign_protocols((changed,)) == (changed,)


def test_campaign_exclusive_miner_change_is_adopted_before_results_freeze(tmp_path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    original = campaign("same", MiningProtocol.PRECLAIM_V2, HOTKEY)
    changed = campaign("same", MiningProtocol.PRECLAIM_V2, "5" + "F" * 47)

    assert store.bind_campaign_protocols((original,)) == (original,)
    assert store.bind_campaign_protocols((changed,)) == (changed,)


def test_zero_value_v3_campaign_reopens_after_contract_edit(tmp_path) -> None:
    """Reproduce the quarantined campaign's empty V3 state and recover it in place."""

    store = ValidatorStore(tmp_path / "validator.sqlite3")
    original = campaign("083_bittensor", MiningProtocol.PRECLAIM_V2)
    changed = original.model_copy(update={"tag": "@@Bitcast_network"})
    store.bind_campaign_protocols((original,))
    store.persist_reconciliation(
        snapshot_id="old-snapshot",
        campaign_id=original.access.campaign_id,
        campaign_json=original.model_dump_json(),
        results=[],
    )
    store.persist_scores("old-snapshot", original.access.campaign_id, [])
    store.persist_campaign_rewards(
        snapshot_id="old-snapshot",
        campaign_id=original.access.campaign_id,
        campaign_json=original.model_dump_json(),
        rewards=[],
        decisions=[],
    )
    store.record_publication(
        "old-snapshot",
        original.access.campaign_id,
        run_id="v3:old-snapshot:083_bittensor",
        payload={"brief_id": original.access.campaign_id, "tweets": []},
        succeeded=True,
    )

    assert store.campaign_finalized(original.access.campaign_id) is False
    assert store.publication_succeeded("old-snapshot", original.access.campaign_id) is False
    assert store.bind_campaign_protocols((changed,)) == (changed,)

    store.persist_reconciliation(
        snapshot_id="new-snapshot",
        campaign_id=changed.access.campaign_id,
        campaign_json=changed.model_dump_json(),
        results=[],
    )

    assert (
        store.reconciliation(
            "new-snapshot",
            changed.access.campaign_id,
            changed.model_dump_json(),
        )
        == []
    )
    assert store.scored_reconciliation("new-snapshot", changed.access.campaign_id) is None
    assert store.campaign_rewards(changed.access.campaign_id, changed.model_dump_json()) is None


@pytest.mark.parametrize("field", MUTABLE_CAMPAIGN_FIELDS)
def test_complete_campaign_contract_adopts_latest_feed_before_results_freeze(
    tmp_path, field: str
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    original = campaign("same", MiningProtocol.PRECLAIM_V2)
    changed = mutate_campaign_contract(original, field)

    assert store.bind_campaign_protocols((original,)) == (original,)
    assert store.bind_campaign_protocols((changed,)) == (changed,)
    assert ValidatorStore(store.path).bind_campaign_protocols((changed,)) == (changed,)


@pytest.mark.parametrize("field", MUTABLE_CAMPAIGN_FIELDS)
def test_complete_campaign_contract_uses_frozen_version_after_results_freeze(
    tmp_path, caplog: pytest.LogCaptureFixture, field: str
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    original = campaign("same", MiningProtocol.PRECLAIM_V2)
    store.bind_campaign_protocols((original,))
    freeze_positive_campaign(store, original)

    with caplog.at_level("ERROR"):
        bound = store.bind_campaign_protocols((mutate_campaign_contract(original, field),))

    assert bound == (original,)
    assert "using frozen contract campaign=same" in caplog.text


def test_pre_close_featured_pin_adopts_edited_contract(tmp_path) -> None:
    """A brief edited while its campaign is open must be adopted, not rejected.

    Reproduces the 112_ares incident: the featured tweet is pinned near close,
    the brand then raises max_members while the brief is still live, and the
    validator must serve the edited contract instead of logging a rejected
    mutation every cycle until emissions end.
    """

    original = campaign("same", MiningProtocol.PRECLAIM_V2).model_copy(update={"max_members": 350})
    changed = original.model_copy(update={"max_members": 750})
    scoring_close = original.access.scoring_close_block
    store = ValidatorStore(
        tmp_path / "validator.sqlite3",
        finalized_block_provider=lambda: scoring_close - 1,
    )
    store.bind_campaign_protocols((original,))
    store.pin_featured_tweet_selection(
        campaign_id="same",
        campaign_json=original.model_dump_json(),
        tweet_id="1",
        selection_pool=("1", "2"),
        selected_block=5,
        selected_at=NOW,
    )

    assert store.bind_campaign_protocols((changed,)) == (changed,)

    # The pin's stored contract must follow the adopted edit so replay agrees.
    selection = store.featured_tweet_selection("same", changed.model_dump_json())
    assert selection is not None and selection.tweet_id == "1"
    assert ValidatorStore(
        tmp_path / "validator.sqlite3",
        finalized_block_provider=lambda: scoring_close - 1,
    ).bind_campaign_protocols((changed,)) == (changed,)


def test_post_close_featured_pin_still_rejects_edits(tmp_path) -> None:
    """Once scoring closes, a pinned campaign's contract stays frozen."""

    scoring_close = 20
    store = ValidatorStore(
        tmp_path / "validator.sqlite3",
        finalized_block_provider=lambda: scoring_close + 1,
    )
    original = campaign("same", MiningProtocol.PRECLAIM_V2).model_copy(update={"max_members": 350})
    changed = original.model_copy(update={"max_members": 750})
    store.bind_campaign_protocols((original,))
    store.pin_featured_tweet_selection(
        campaign_id="same",
        campaign_json=original.model_dump_json(),
        tweet_id="1",
        selection_pool=("1", "2"),
        selected_block=5,
        selected_at=NOW,
    )

    assert store.bind_campaign_protocols((changed,)) == (original,)
    assert ValidatorStore(
        tmp_path / "validator.sqlite3",
        finalized_block_provider=lambda: scoring_close + 1,
    ).bind_campaign_protocols((changed,)) == (original,)


def test_frozen_rewards_reject_edits_even_before_scoring_close(tmp_path) -> None:
    """Settled economics freeze the contract regardless of the scoring window."""

    scoring_close = 20
    store = ValidatorStore(
        tmp_path / "validator.sqlite3",
        finalized_block_provider=lambda: scoring_close - 1,
    )
    original = campaign("same", MiningProtocol.PRECLAIM_V2).model_copy(update={"max_members": 350})
    store.bind_campaign_protocols((original,))
    store.pin_featured_tweet_selection(
        campaign_id="same",
        campaign_json=original.model_dump_json(),
        tweet_id="1",
        selection_pool=("1", "2"),
        selected_block=5,
        selected_at=NOW,
    )
    # An edit lands while the campaign is still open and is adopted.
    edited = original.model_copy(update={"max_members": 999})
    assert store.bind_campaign_protocols((edited,)) == (edited,)
    with sqlite3.connect(tmp_path / "validator.sqlite3") as connection:
        stored_json = connection.execute(
            "SELECT campaign_contract_json FROM campaign_protocols WHERE campaign_id = 'same'"
        ).fetchone()[0]
    assert edited.model_dump_json() == stored_json
    # Settled economics then freeze the ADOPTED contract in place.
    freeze_positive_campaign(store, edited)

    # Any further edit is rejected in favor of the frozen adopted contract.
    assert store.bind_campaign_protocols((edited.model_copy(update={"max_members": 1000}),)) == (
        edited,
    )


def test_featured_pin_replay_still_rejects_after_pre_close_adoption(tmp_path) -> None:
    """Adoption refreshes the pin; a DIFFERENT contract must still be refused."""

    scoring_close = 20
    store = ValidatorStore(
        tmp_path / "validator.sqlite3",
        finalized_block_provider=lambda: scoring_close - 1,
    )
    original = campaign("same", MiningProtocol.PRECLAIM_V2)
    changed = original.model_copy(update={"brief": "edited brief"})
    store.bind_campaign_protocols((original,))
    store.pin_featured_tweet_selection(
        campaign_id="same",
        campaign_json=original.model_dump_json(),
        tweet_id="1",
        selection_pool=("1", "2"),
        selected_block=5,
        selected_at=NOW,
    )
    assert store.bind_campaign_protocols((changed,)) == (changed,)

    with pytest.raises(ProtocolError, match="changed after featured tweet selection"):
        store.featured_tweet_selection("same", original.model_dump_json())


def test_frozen_campaign_mutation_does_not_stall_unrelated_campaigns(tmp_path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    frozen = campaign("frozen", MiningProtocol.PRECLAIM_V2)
    unaffected = campaign("unaffected", MiningProtocol.PRECLAIM_V2)
    store.bind_campaign_protocols((frozen, unaffected))
    freeze_positive_campaign(store, frozen)

    bound = store.bind_campaign_protocols((mutate_campaign_contract(frozen, "brief"), unaffected))

    assert bound == (frozen, unaffected)


def test_unreadable_frozen_campaign_contract_quarantines_only_that_campaign(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    frozen = campaign("frozen", MiningProtocol.PRECLAIM_V2)
    unaffected = campaign("unaffected", MiningProtocol.PRECLAIM_V2)
    store.bind_campaign_protocols((frozen, unaffected))
    freeze_positive_campaign(store, frozen)
    with sqlite3.connect(tmp_path / "validator.sqlite3") as connection:
        connection.execute(
            """
            UPDATE campaign_protocols
            SET campaign_contract_json = 'unreadable'
            WHERE campaign_id = 'frozen'
            """
        )

    with caplog.at_level("CRITICAL"):
        bound = store.bind_campaign_protocols(
            (mutate_campaign_contract(frozen, "brief"), unaffected)
        )

    assert bound == (unaffected,)
    assert "quarantined campaign with unreadable frozen contract campaign=frozen" in caplog.text


def test_rank_cutoff_upgrade_preserves_already_frozen_campaign_results(tmp_path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    original = campaign("same", MiningProtocol.PRECLAIM_V2)
    ranked = original.model_copy(update={"max_members": 150})
    store.bind_campaign_protocols((original,))
    store.persist_reconciliation(
        snapshot_id="frozen",
        campaign_id="same",
        campaign_json=original.model_dump_json(),
        results=[],
    )

    assert store.bind_campaign_protocols((ranked,)) == (ranked,)


def test_published_rank_cutoff_cannot_change_after_results_freeze(tmp_path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    original = campaign("same", MiningProtocol.PRECLAIM_V2).model_copy(update={"max_members": 150})
    changed = original.model_copy(update={"max_members": 151})
    store.bind_campaign_protocols((original,))
    freeze_positive_campaign(store, original)

    assert store.bind_campaign_protocols((changed,)) == (original,)


def test_identical_campaign_contract_can_be_observed_repeatedly(tmp_path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    original = campaign("same", MiningProtocol.PRECLAIM_V2)

    store.bind_campaign_protocols((original,))
    store.bind_campaign_protocols((original,))


def test_legacy_campaign_reintroduction_rejects_the_complete_cycle() -> None:
    snapshot = feed(
        campaign("new", MiningProtocol.PRECLAIM_V2),
        campaign("retired", MiningProtocol.LEGACY_CONNECTION),
    )
    with pytest.raises(ProtocolError, match="legacy campaign processing is retired: retired"):
        ensure_supported_campaigns(snapshot)


def test_preclaim_and_empty_feeds_do_not_require_imported_legacy_state() -> None:
    ensure_supported_campaigns(feed(campaign("new", MiningProtocol.PRECLAIM_V2)))
    ensure_supported_campaigns(feed())


def test_retired_frozen_campaign_cannot_be_reintroduced_as_preclaim(tmp_path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    retired = campaign("retired", MiningProtocol.LEGACY_CONNECTION)
    store.bind_campaign_protocols((retired,))
    freeze_positive_campaign(store, retired)

    replacement = campaign("retired", MiningProtocol.PRECLAIM_V2)
    bound = store.bind_campaign_protocols((replacement,))

    assert bound == (retired,)
    with pytest.raises(ProtocolError, match="legacy campaign processing is retired: retired"):
        ensure_supported_campaigns(feed(*bound))
    assert ValidatorStore(store.path).bind_campaign_protocols((replacement,)) == (retired,)


def test_archived_legacy_binding_does_not_block_current_campaigns(tmp_path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    retired = campaign("retired", MiningProtocol.LEGACY_CONNECTION)
    store.bind_campaign_protocols((retired,))
    current = campaign("new", MiningProtocol.PRECLAIM_V2)
    bound = store.bind_campaign_protocols((current,))
    assert bound == (current,)
    ensure_supported_campaigns(feed(*bound))
    with sqlite3.connect(tmp_path / "validator.sqlite3") as connection:
        assert connection.execute(
            "SELECT mining_protocol FROM campaign_protocols WHERE campaign_id = ?",
            ("retired",),
        ).fetchone() == ("legacy_connection",)
