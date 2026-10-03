"""Campaign contracts remain pinned across feed changes and protocol retirement."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from bitcast_x.campaigns import CampaignFeed, CampaignRecord
from bitcast_x.protocol import CampaignAccess, MiningProtocol
from bitcast_x.rewards import TweetReward
from bitcast_x.validator.store import ValidatorStore

NOW = datetime(2026, 8, 5, tzinfo=UTC)
HOTKEY = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
MUTABLE_CAMPAIGN_FIELDS = (
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
    "max_members",
)


def campaign(campaign_id: str) -> CampaignRecord:
    return CampaignRecord(
        access=CampaignAccess(
            campaign_id=campaign_id,
            mechanism_id=1,
            mining_protocol=MiningProtocol.PRECLAIM_V2,
            scoring_close_block=20,
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


def mutate_campaign_contract(record: CampaignRecord, field: str) -> CampaignRecord:
    """Return one valid campaign whose named consensus field differs."""

    if field in {"mechanism_id", "scoring_close_block", "exclusive_miner_hotkey"}:
        access_updates: dict[str, object] = {
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
        "max_members": 2,
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


def test_zero_value_v3_campaign_reopens_after_contract_edit(tmp_path) -> None:
    """Reproduce the quarantined campaign's empty V3 state and recover it in place."""

    store = ValidatorStore(tmp_path / "validator.sqlite3")
    original = campaign("083_bittensor")
    changed = original.model_copy(update={"tag": "@@Bitcast_network"})
    store.bind_campaign_protocols((original,))
    store.persist_reconciliation(
        snapshot_id="old-snapshot",
        campaign_id=original.access.campaign_id,
        campaign_json=original.model_dump_json(),
        results=[],
    )
    store.persist_scores(original.access.campaign_id, [])
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
    assert store.publication_succeeded(original.access.campaign_id) is False
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
    assert store.scored_reconciliation(changed.access.campaign_id) is None
    assert store.campaign_rewards(changed.access.campaign_id, changed.model_dump_json()) is None


@pytest.mark.parametrize("field", MUTABLE_CAMPAIGN_FIELDS)
def test_complete_campaign_contract_adopts_latest_feed_before_results_freeze(
    tmp_path, field: str
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    original = campaign("same")
    changed = mutate_campaign_contract(original, field)

    assert store.bind_campaign_protocols((original,)) == (original,)
    assert store.bind_campaign_protocols((changed,)) == (changed,)
    assert ValidatorStore(store.path).bind_campaign_protocols((changed,)) == (changed,)


@pytest.mark.parametrize("field", MUTABLE_CAMPAIGN_FIELDS)
def test_complete_campaign_contract_uses_frozen_version_after_results_freeze(
    tmp_path, caplog: pytest.LogCaptureFixture, field: str
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    original = campaign("same")
    unrelated = campaign("unrelated")
    store.bind_campaign_protocols((original, unrelated))
    freeze_positive_campaign(store, original)

    with caplog.at_level("ERROR"):
        bound = store.bind_campaign_protocols(
            (mutate_campaign_contract(original, field), unrelated)
        )

    assert bound == (original, unrelated)
    assert "using frozen contract campaign=same" in caplog.text


def test_featured_pin_does_not_freeze_campaign_contract(tmp_path) -> None:
    """A pinned featured tweet never blocks a campaign edit before settlement.

    Covers the 112_ares incident: the brand raised max_members after the
    featured tweet was pinned, and every cycle rejected the edit.
    """

    original = campaign("same").model_copy(update={"max_members": 350})
    changed = original.model_copy(update={"max_members": 750})
    store = ValidatorStore(tmp_path / "validator.sqlite3")
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
    selection = store.featured_tweet_selection("same")
    assert selection is not None and selection.tweet_id == "1"
    # The pin records the adopted contract so older releases accept it on rollback.
    with sqlite3.connect(tmp_path / "validator.sqlite3") as connection:
        pin_contract = connection.execute(
            "SELECT campaign_json FROM featured_tweet_selections WHERE campaign_id = 'same'"
        ).fetchone()[0]
    assert pin_contract == changed.model_dump_json()

    # Settled economics freeze whichever contract was in force at settlement.
    freeze_positive_campaign(store, changed)
    assert store.bind_campaign_protocols((changed.model_copy(update={"max_members": 1000}),)) == (
        changed,
    )


def retired_contract_json(record: CampaignRecord) -> str:
    """Return a contract as stored by releases that still ran legacy campaigns."""

    return record.model_dump_json().replace('"preclaim_v2"', '"legacy_connection"')


def test_retired_campaign_mode_is_rejected_by_the_feed_model() -> None:
    payload = feed(campaign("retired")).model_dump(mode="json")
    payload["campaigns"][0]["access"]["mining_protocol"] = "legacy_connection"

    with pytest.raises(ValidationError, match="mining_protocol"):
        CampaignFeed.model_validate(payload)


@pytest.mark.parametrize(
    "stored_contract",
    (
        pytest.param(lambda _record: "unreadable", id="unreadable"),
        pytest.param(retired_contract_json, id="retired-mode"),
    ),
)
def test_unreadable_frozen_contract_is_quarantined_not_fatal(
    tmp_path, caplog: pytest.LogCaptureFixture, stored_contract: Callable[[CampaignRecord], str]
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    frozen = campaign("frozen")
    unaffected = campaign("unaffected")
    store.bind_campaign_protocols((frozen, unaffected))
    freeze_positive_campaign(store, frozen)
    with sqlite3.connect(tmp_path / "validator.sqlite3") as connection:
        connection.execute(
            "UPDATE campaign_protocols SET campaign_contract_json = ? WHERE campaign_id = ?",
            (stored_contract(frozen), "frozen"),
        )
        connection.execute(
            "UPDATE reconciliations SET campaign_json = ? WHERE campaign_id = ?",
            (stored_contract(frozen), "frozen"),
        )
    current = campaign("new")

    with caplog.at_level("CRITICAL"):
        bound = store.bind_campaign_protocols((frozen, unaffected, current))
        reconciled = store.reconciled_campaigns()

    assert bound == (unaffected, current)
    assert reconciled == []
    assert "quarantined campaign with unreadable frozen contract campaign=frozen" in caplog.text


def test_archived_legacy_binding_does_not_block_current_campaigns(tmp_path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    retired = campaign("retired")
    with sqlite3.connect(tmp_path / "validator.sqlite3") as connection:
        connection.execute(
            """
            INSERT INTO campaign_protocols(
                campaign_id, mining_protocol, exclusive_miner_hotkey, campaign_contract_json
            ) VALUES ('retired', 'legacy_connection', NULL, ?)
            """,
            (retired_contract_json(retired),),
        )
    current = campaign("new")

    assert store.bind_campaign_protocols((current,)) == (current,)
    with sqlite3.connect(tmp_path / "validator.sqlite3") as connection:
        assert connection.execute(
            "SELECT mining_protocol FROM campaign_protocols WHERE campaign_id = ?",
            ("retired",),
        ).fetchone() == ("legacy_connection",)
