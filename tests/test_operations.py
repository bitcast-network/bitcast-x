"""Tests for operational health, migrations, structured logs, and state backup."""

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from bitcast_x.campaigns import CampaignRecord
from bitcast_x.errors import ProtocolError
from bitcast_x.logging import JsonFormatter
from bitcast_x.miner.store import MinerStore
from bitcast_x.ops import RuntimeHealth, create_ops_app
from bitcast_x.protocol import CampaignAccess, MiningProtocol
from bitcast_x.release import source_revision
from bitcast_x.sqlite import apply_migrations
from bitcast_x.state import backup_state, inspect_state, shadow_report
from bitcast_x.validator.store import ValidatorStore


@pytest.mark.asyncio
async def test_validator_readiness_and_metrics_have_fixed_cardinality() -> None:
    health = RuntimeHealth.create()
    app = create_ops_app(health)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ops.test",
    ) as client:
        starting = await client.get("/ready")
        liveness = await client.get("/health")
        health.success(123)
        ready = await client.get("/ready")
        metrics = await client.get("/metrics")

    assert starting.status_code == 503
    assert liveness.json()["source_revision"] == source_revision()
    assert ready.json() == {"status": "ready", "last_finalized_block": 123}
    assert 'bitcast_x_validator_cycles_total{result="success"} 1' in metrics.text
    assert "bitcast_x_validator_finalized_block 123" in metrics.text


def test_sqlite_stores_migrate_and_online_backup_is_consistent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    MinerStore(state_dir / "miner.sqlite3")
    ValidatorStore(state_dir / "validator.sqlite3")

    info = inspect_state(state_dir)
    destination = tmp_path / "backup"
    manifest = backup_state(state_dir, destination)
    backup_info = inspect_state(destination)

    assert {item["schema_version"] for item in info["databases"]} == {2, 5}
    assert {item["integrity"] for item in backup_info["databases"]} == {"ok"}
    assert manifest["files"] == ["miner.sqlite3", "validator.sqlite3"]
    assert json.loads((destination / "manifest.json").read_text())["files"] == manifest["files"]


def test_migration_runner_rejects_state_from_newer_binary(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version = 99")
        with pytest.raises(ProtocolError, match="newer than supported"):
            apply_migrations(connection, ("CREATE TABLE example(id INTEGER);",))
    finally:
        connection.close()


def test_unversioned_existing_store_is_adopted_without_losing_state(tmp_path: Path) -> None:
    path = tmp_path / "legacy-validator.sqlite3"
    original = ValidatorStore(path, start_block=10)
    original.persist_block(10, [])
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version = 0")
    finally:
        connection.close()

    reopened = ValidatorStore(path, start_block=999)

    assert reopened.scanned_block() == 10
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
    finally:
        connection.close()


def test_campaign_contract_migration_backfills_frozen_reconciliation(tmp_path: Path) -> None:
    path = tmp_path / "validator.sqlite3"
    store = ValidatorStore(path)
    now = datetime(2026, 8, 13, tzinfo=UTC)
    original = CampaignRecord(
        access=CampaignAccess(
            campaign_id="frozen",
            mechanism_id=1,
            mining_protocol=MiningProtocol.PRECLAIM_V2,
            scoring_close_block=20,
        ),
        title="Frozen campaign",
        brief="original brief",
        ecosystem_id="eco",
        opens_at=now,
        closes_at=now + timedelta(days=1),
        reward_pool_usd="700",
        emission_start_block=30,
        emission_end_block=40,
    )
    store.bind_campaign_protocols((original,))
    store.persist_reconciliation(
        snapshot_id="snapshot",
        campaign_id="frozen",
        campaign_json=original.model_dump_json(),
        results=[],
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute("ALTER TABLE campaign_protocols DROP COLUMN campaign_contract_json")
        connection.execute("PRAGMA user_version = 4")
    finally:
        connection.close()

    reopened = ValidatorStore(path)
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT campaign_contract_json FROM campaign_protocols WHERE campaign_id = 'frozen'"
        ).fetchone()
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert row is not None and row[0] == original.model_dump_json()
    finally:
        connection.close()

    with pytest.raises(ProtocolError, match="changed after final results froze"):
        reopened.bind_campaign_protocols((original.model_copy(update={"brief": "mutated brief"}),))


def test_unreadable_validator_store_is_quarantined_and_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "validator.sqlite3"
    path.write_bytes(b"not a sqlite database")
    wal = Path(f"{path}-wal")
    shm = Path(f"{path}-shm")
    wal.write_bytes(b"preserved wal")
    shm.write_bytes(b"preserved shm")

    store = ValidatorStore(path, start_block=321)

    assert store.scanned_block() == 320
    assert path.read_bytes().startswith(b"SQLite format 3\x00")
    quarantined = sorted(tmp_path.glob("validator.sqlite3*.corrupt-*"))
    assert len(quarantined) == 3
    quarantined_by_name = {item.name.split(".corrupt-")[0]: item for item in quarantined}
    assert quarantined_by_name["validator.sqlite3"].read_bytes() == b"not a sqlite database"
    assert quarantined_by_name["validator.sqlite3-wal"].read_bytes() == b"preserved wal"
    # SQLite may rewrite the shared-memory sidecar while detecting corruption;
    # retaining the resulting bytes is the recoverability guarantee.
    assert quarantined_by_name["validator.sqlite3-shm"].stat().st_size > 0


def test_json_formatter_emits_bounded_standard_fields() -> None:
    record = logging.LogRecord(
        name="bitcast_x.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="cycle block=%s",
        args=(123,),
        exc_info=None,
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "bitcast_x.test"
    assert payload["message"] == "cycle block=123"
    assert set(payload) == {"timestamp", "level", "logger", "message"}


def test_shadow_report_is_stable_for_identical_frozen_state(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = ValidatorStore(first_dir / "validator.sqlite3")
    second = ValidatorStore(second_dir / "validator.sqlite3")
    first.persist_shadow_weights(10, "snapshot-a", {0: 0.0, 1: 1.0})
    second.persist_shadow_weights(10, "snapshot-b", {0: 0.0, 1: 1.0})

    first_report = shadow_report(first_dir)
    second_report = shadow_report(second_dir)

    assert first_report == second_report
    assert first_report["latest_finalized_block"] == 10
