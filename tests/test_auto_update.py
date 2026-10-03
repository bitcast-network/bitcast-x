"""Safe source auto-update behavior."""

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bitcast_x import auto_update
from bitcast_x.auto_update import Release, SourceUpdateManager
from bitcast_x.config import Settings
from bitcast_x.miner.store import MinerStore
from bitcast_x.validator.store import ValidatorStore


def _logical_dump(path: Path) -> str:
    """Return schema version and content, ignoring WAL/checkpoint byte changes."""

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        return "\n".join((f"PRAGMA user_version = {version}", *connection.iterdump()))
    finally:
        connection.close()


def test_upgrade_check_uses_disposable_copies_without_changing_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    miner_path = state / "miner.sqlite3"
    validator_path = state / "validator.sqlite3"
    MinerStore(miner_path)
    ValidatorStore(validator_path)
    before = {path.name: _logical_dump(path) for path in (miner_path, validator_path)}

    versions = auto_update.verify_automatic_upgrade(state)

    assert versions.keys() == before.keys()
    assert {path.name: _logical_dump(path) for path in (miner_path, validator_path)} == before
    assert not list(state.glob("*upgrade*"))


def test_upgrade_check_rejects_campaign_contract_schema_upgrade(tmp_path: Path) -> None:
    state = tmp_path / "state"
    validator_path = state / "validator.sqlite3"
    ValidatorStore(validator_path)
    connection = sqlite3.connect(validator_path)
    try:
        connection.execute("ALTER TABLE campaign_protocols DROP COLUMN campaign_contract_json")
        connection.execute("PRAGMA user_version = 4")
    finally:
        connection.close()

    original = _logical_dump(validator_path)

    with pytest.raises(
        RuntimeError, match=r"manual schema upgrade for validator\.sqlite3: 4 -> \d+"
    ):
        auto_update.verify_automatic_upgrade(state)

    # The dump covers user_version and every table definition, so the original
    # database is still at version 4 without campaign_contract_json.
    assert _logical_dump(validator_path) == original


def test_upgrade_check_allows_rollback_safe_featured_selection_table(tmp_path: Path) -> None:
    state = tmp_path / "state"
    validator_path = state / "validator.sqlite3"
    ValidatorStore(validator_path)
    connection = sqlite3.connect(validator_path)
    try:
        connection.execute("DROP TABLE featured_tweet_selections")
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    finally:
        connection.close()

    assert auto_update.verify_automatic_upgrade(state) == {"validator.sqlite3": 6}
    connection = sqlite3.connect(validator_path)
    try:
        tables_before_activation = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "featured_tweet_selections" not in tables_before_activation
    finally:
        connection.close()

    ValidatorStore(validator_path)
    connection = sqlite3.connect(validator_path)
    try:
        tables_after_activation = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert "featured_tweet_selections" in tables_after_activation
    finally:
        connection.close()


def test_manager_refuses_update_from_modified_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    settings = Settings(state_dir=tmp_path / "state", auto_update_dir=tmp_path / "updates")
    manager = SourceUpdateManager(source, settings)
    monkeypatch.setattr(manager, "_git", lambda *args, **kwargs: " M tracked.py")

    with pytest.raises(RuntimeError, match="tracked changes"):
        manager.prepare(Release("a" * 40, source, Path(sys.executable)))


@pytest.mark.parametrize("update_suffix", (".", "updates"))
def test_manager_rejects_update_directory_overlapping_state(
    tmp_path: Path, update_suffix: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    state = tmp_path / "state"
    update_dir = state if update_suffix == "." else state / update_suffix
    with pytest.raises(ValueError, match="must not overlap"):
        SourceUpdateManager(
            source,
            Settings(state_dir=state, auto_update_dir=update_dir),
        )


def test_candidate_is_prepared_in_isolated_worktree_and_activated_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    sentinel = state / "campaign-feed.json"
    sentinel.write_text("runtime-cache", encoding="utf-8")
    settings = Settings(state_dir=state, auto_update_dir=tmp_path / "updates")
    manager = SourceUpdateManager(source, settings)
    old = "a" * 40
    target = "b" * 40
    release_root = settings.auto_update_dir / "releases" / target
    python = release_root / ".venv" / "bin" / "python"
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str, cwd: Path | None = None) -> str:
        calls.append(args)
        if args[:2] == ("status", "--porcelain"):
            return ""
        if args[:1] == ("fetch",):
            return ""
        if args == ("rev-parse", settings.auto_update_ref):
            return target
        if args[:2] == ("worktree", "add"):
            python.parent.mkdir(parents=True)
            python.touch()
            return ""
        raise AssertionError((args, cwd))

    def fake_subprocess_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0)

    checks: list[tuple[str, ...]] = []
    monkeypatch.setattr(manager, "_git", fake_git)
    monkeypatch.setattr(auto_update.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(manager, "_run", lambda *args, **kwargs: checks.append(args))

    candidate = manager.prepare(Release(old, source, Path(sys.executable)))
    assert candidate == Release(target, release_root, python)
    assert sentinel.read_text(encoding="utf-8") == "runtime-cache"
    assert any("_auto-update-check" in call for call in checks)

    manager.activate(candidate)
    active = json.loads((settings.auto_update_dir / "active.json").read_text())
    assert active["commit"] == target
    assert not list(settings.auto_update_dir.glob(".active.json.*"))
