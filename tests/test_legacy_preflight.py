"""Read-only legacy cutover preflight tests."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from diskcache import Cache

from bitcast_x.errors import ProtocolError
from bitcast_x.legacy.preflight import inspect_legacy_state


def _state(root: Path) -> tuple[Path, Path, Path]:
    connections = root / "connections.db"
    with sqlite3.connect(connections) as database:
        database.execute(
            """
            CREATE TABLE connections (
                connection_id INTEGER PRIMARY KEY AUTOINCREMENT, tweet_id BIGINT NOT NULL,
                tag VARCHAR(100) NOT NULL, account_username VARCHAR(100) NOT NULL UNIQUE,
                added DATETIME NOT NULL, updated DATETIME NOT NULL, referral_code VARCHAR(100),
                referred_by VARCHAR(100), referee_amount REAL DEFAULT 50.0,
                referrer_amount REAL DEFAULT 50.0, payout_date DATE
            )
            """
        )
        database.execute("PRAGMA user_version = 2")
        database.execute(
            "INSERT INTO connections (tweet_id, tag, account_username, added, updated) "
            "VALUES (1, 'Stitch3-builder', 'alice', 'a', 'b')"
        )

    snapshots = root / "reward_snapshots"
    pool = snapshots / "tao"
    pool.mkdir(parents=True)
    (pool / "brief_1_2026.08.08_00.00.00.json").write_text(
        json.dumps(
            {
                "brief_id": "brief_1",
                "pool_name": "tao",
                "created_at": datetime(2026, 8, 8, tzinfo=UTC).isoformat(),
                "tweet_rewards": [{"tweet_id": "1", "author": "alice", "uid": 114, "total_usd": 7}],
            }
        )
    )

    tweets = root / "legacy_tweet_store"
    with Cache(tweets) as cache:
        cache.set("tweet:1", {"tweet_id": "1"})
        cache.set("engagements:1", {"retweeters": {}, "quoters": {}})
    return connections, snapshots, tweets


def test_preflight_reports_complete_import_without_mutating_it(tmp_path: Path) -> None:
    paths = _state(tmp_path)
    before = {path: path.stat().st_mtime_ns for path in (paths[0], paths[2] / "cache.db")}

    result = inspect_legacy_state(*paths)

    assert result | {"manifest_sha256": "ignored"} == {
        "status": "ok",
        "connections": 1,
        "snapshots": 1,
        "snapshot_tweets": 1,
        "cached_tweets": 1,
        "cached_engagements": 1,
        "manifest_sha256": "ignored",
    }
    assert len(result["manifest_sha256"]) == 64
    assert before == {path: path.stat().st_mtime_ns for path in before}


def test_preflight_rejects_snapshot_identity_mismatch(tmp_path: Path) -> None:
    connections, snapshots, tweets = _state(tmp_path)
    path = next(snapshots.glob("*/*.json"))
    payload = json.loads(path.read_text())
    payload["pool_name"] = "wrong"
    path.write_text(json.dumps(payload))

    with pytest.raises(ProtocolError, match="identity mismatch"):
        inspect_legacy_state(connections, snapshots, tweets)
