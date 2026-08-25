"""Tests for the temporary legacy-to-preclaim weight transition."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from bitcast_x.campaigns import CampaignFeed, CampaignRecord
from bitcast_x.errors import ProtocolError
from bitcast_x.protocol import CampaignAccess, MiningProtocol
from bitcast_x.rewards import TweetReward
from bitcast_x.validator.legacy import (
    LEGACY_TREASURY_UID,
    combine_weights,
    has_legacy_campaigns,
    preclaim_feed,
)
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


def test_combiner_preserves_legacy_and_redistributes_only_burn() -> None:
    combined = combine_weights(
        {0: 0.6, 114: 0.4},
        {0: 0.0, 2: 0.75, 3: 0.25, 114: 0.0},
        uids=[0, 2, 3, 114],
    )

    assert combined == pytest.approx({0: 0.0, 2: 0.45, 3: 0.15, 114: 0.4})


def test_combiner_adds_both_paths_for_the_same_uid() -> None:
    combined = combine_weights(
        {0: 0.5, 7: 0.5},
        {0: 0.0, 7: 0.25, 8: 0.75},
        uids=[0, 7, 8],
    )

    assert combined == {0: 0.0, 7: 0.625, 8: 0.375}


def test_shipped_legacy_treasury_uid_matches_v1() -> None:
    assert LEGACY_TREASURY_UID == 155


def test_combiner_routes_all_excess_to_treasury_without_productive_v2_miners() -> None:
    legacy = {0: 0.6, 114: 0.4, LEGACY_TREASURY_UID: 0.0}

    combined = combine_weights(
        legacy,
        {0: 1.0, 114: 0.0, LEGACY_TREASURY_UID: 0.0},
        uids=[0, 114, LEGACY_TREASURY_UID],
    )

    assert combined == {0: 0.0, 114: 0.4, LEGACY_TREASURY_UID: 0.6}


def test_combiner_adds_excess_to_existing_legacy_treasury_weight() -> None:
    combined = combine_weights(
        {0: 0.5, 7: 0.3, LEGACY_TREASURY_UID: 0.2},
        {0: 1.0, 7: 0.0, LEGACY_TREASURY_UID: 0.0},
        uids=[0, 7, LEGACY_TREASURY_UID],
    )

    assert combined == {0: 0.0, 7: 0.3, LEGACY_TREASURY_UID: 0.7}


def test_combiner_routes_excess_to_productive_v2_instead_of_treasury() -> None:
    combined = combine_weights(
        {0: 0.6, 114: 0.4, LEGACY_TREASURY_UID: 0.0},
        {0: 0.0, 2: 0.75, 3: 0.25, 114: 0.0, LEGACY_TREASURY_UID: 0.0},
        uids=[0, 2, 3, 114, LEGACY_TREASURY_UID],
    )

    assert combined == pytest.approx({0: 0.0, 2: 0.45, 3: 0.15, 114: 0.4, LEGACY_TREASURY_UID: 0.0})


def test_combiner_fails_closed_when_legacy_treasury_is_absent() -> None:
    with pytest.raises(ProtocolError, match="treasury UID 155 is absent"):
        combine_weights({0: 0.6, 114: 0.4}, {0: 1.0, 114: 0.0}, uids=[0, 114])


@pytest.mark.parametrize(
    ("legacy", "productive", "message"),
    [
        ({0: 0.9}, {0: 1.0}, "sum to one"),
        ({0: 1.0}, {0: 1.0, 9: 0.1}, "unknown UIDs"),
        ({0: 1.0}, {0: float("nan")}, "finite and non-negative"),
    ],
)
def test_combiner_rejects_invalid_vectors(
    legacy: dict[int, float], productive: dict[int, float], message: str
) -> None:
    with pytest.raises(ProtocolError, match=message):
        combine_weights(legacy, productive, uids=[0])


def test_campaign_router_keeps_only_preclaim_campaigns() -> None:
    complete = feed(
        campaign("legacy", MiningProtocol.LEGACY_CONNECTION),
        campaign("new", MiningProtocol.PRECLAIM_V2),
    )

    routed = preclaim_feed(complete)

    assert has_legacy_campaigns(complete)
    assert [item.access.campaign_id for item in routed.campaigns] == ["new"]


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
    with sqlite3.connect(store.path) as connection:
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
