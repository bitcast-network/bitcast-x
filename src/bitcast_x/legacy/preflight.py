"""Read-only verification of the frozen v1/v2 state used at cutover."""

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from bitcast_x.errors import ProtocolError
from bitcast_x.legacy.connections import ConnectionStore
from bitcast_x.legacy.snapshots import LegacyRewardSnapshot


def inspect_legacy_state(
    connections_path: Path,
    snapshots_path: Path,
    tweet_store_path: Path,
) -> dict[str, Any]:
    """Validate a stopped v2 state copy without opening any file for writing."""

    connections = ConnectionStore(connections_path)
    connections.validate()
    connection_rows = connections.all()
    # Exercise tag parsing, uniqueness, dates and numeric fields before launch.
    connections.by_username()
    for row in connection_rows:
        _ = row.tag_type

    snapshot_files = sorted(snapshots_path.glob("*/*.json"))
    snapshots: list[LegacyRewardSnapshot] = []
    for path in snapshot_files:
        try:
            payload = TypeAdapter(dict[str, Any]).validate_json(path.read_bytes())
            snapshot = LegacyRewardSnapshot.model_validate(payload)
        except (OSError, ValueError) as exc:
            raise ProtocolError(f"invalid legacy reward snapshot: {path}") from exc
        if path.parent.name != snapshot.pool_name or not path.name.startswith(
            f"{snapshot.brief_id}_"
        ):
            raise ProtocolError(f"legacy reward snapshot identity mismatch: {path}")
        snapshots.append(snapshot)

    database = tweet_store_path / "cache.db"
    if not database.is_file():
        raise ProtocolError(f"legacy tweet store is missing: {database}")
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA quick_check(1)").fetchone()
            if result != ("ok",):
                raise ProtocolError(f"legacy tweet store integrity check failed: {result!r}")
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if not {"Cache", "Settings"}.issubset(tables):
                raise ProtocolError("legacy tweet store is not a diskcache database")
            tweet_rows, engagement_rows = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN raw = 1 AND key LIKE 'tweet:%' THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(
                        CASE WHEN raw = 1 AND key LIKE 'engagements:%' THEN 1 ELSE 0 END
                    ), 0)
                FROM Cache
                """
            ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise ProtocolError(f"legacy tweet store is unreadable: {database}") from exc

    files = [connections_path, database, *snapshot_files]
    return {
        "status": "ok",
        "connections": len(connection_rows),
        "snapshots": len(snapshots),
        "snapshot_tweets": sum(len(snapshot.tweet_rewards) for snapshot in snapshots),
        "cached_tweets": int(tweet_rows),
        "cached_engagements": int(engagement_rows),
        "manifest_sha256": _manifest_hash(files),
    }


def _manifest_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()
