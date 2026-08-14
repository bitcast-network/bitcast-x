"""Operator-safe inspection and online backup of durable node state."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bitcast_x import __version__
from bitcast_x.sqlite import backup_database

STATE_DATABASES = ("miner.sqlite3", "validator.sqlite3")


def shadow_report(state_dir: Path) -> dict[str, Any]:
    """Return reproducible hashes for comparing independent validator shadow state."""

    path = state_dir / "validator.sqlite3"
    if not path.exists():
        raise FileNotFoundError(f"validator state database not found in {state_dir}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        weight_rows = connection.execute(
            "SELECT block, weights_json FROM shadow_weights ORDER BY block"
        ).fetchall()
        reward_rows = connection.execute(
            """
            SELECT campaign_id, rewards_json, decisions_json
            FROM campaign_rewards ORDER BY campaign_id
            """
        ).fetchall()
        attribution_rows = connection.execute(
            """
            SELECT campaign_id, campaign_json, results_json
            FROM reconciliations ORDER BY campaign_id
            """
        ).fetchall()
        score_rows = connection.execute(
            """
            SELECT campaign_id, scored_json
            FROM scored_reconciliations ORDER BY campaign_id
            """
        ).fetchall()
        llm_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'llm_evaluations'"
        ).fetchone()
        evaluation_rows = (
            connection.execute(
                "SELECT prompt_hash, result_json FROM llm_evaluations ORDER BY prompt_hash"
            ).fetchall()
            if llm_table is not None
            else []
        )
    finally:
        connection.close()
    weights = [(int(row["block"]), json.loads(row["weights_json"])) for row in weight_rows]
    rewards = [
        (
            str(row["campaign_id"]),
            json.loads(row["rewards_json"]),
            json.loads(row["decisions_json"]),
        )
        for row in reward_rows
    ]
    attributions = [
        (
            str(row["campaign_id"]),
            json.loads(row["campaign_json"]),
            json.loads(row["results_json"]),
        )
        for row in attribution_rows
    ]
    scores = [(str(row["campaign_id"]), json.loads(row["scored_json"])) for row in score_rows]
    evaluations = [
        (str(row["prompt_hash"]), json.loads(row["result_json"])) for row in evaluation_rows
    ]
    return {
        "latest_finalized_block": weights[-1][0] if weights else None,
        "shadow_blocks": len(weights),
        "campaigns_frozen": len(rewards),
        "attributions_sha256": _canonical_digest(attributions),
        "scores_sha256": _canonical_digest(scores),
        "llm_evaluations_sha256": _canonical_digest(evaluations),
        "weights_sha256": _canonical_digest(weights),
        "campaign_rewards_sha256": _canonical_digest(rewards),
    }


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def inspect_state(state_dir: Path) -> dict[str, Any]:
    """Return bounded health metadata without exposing protocol evidence or secrets."""

    databases: list[dict[str, Any]] = []
    for name in STATE_DATABASES:
        path = state_dir / name
        if not path.exists():
            continue
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            connection.close()
        databases.append(
            {
                "name": name,
                "bytes": path.stat().st_size,
                "schema_version": schema_version,
                "integrity": integrity,
            }
        )
    return {"package_version": __version__, "state_dir": str(state_dir), "databases": databases}


def backup_state(state_dir: Path, destination: Path) -> dict[str, Any]:
    """Create a new consistent backup directory and return its manifest."""

    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ValueError("backup destination must be an absent or empty directory")
    destination.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    for name in STATE_DATABASES:
        source = state_dir / name
        if not source.exists():
            continue
        backup_database(source, destination / name)
        files.append(name)
    if not files:
        raise FileNotFoundError(f"no Bitcast X state databases found in {state_dir}")
    manifest: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "package_version": __version__,
        "files": files,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest
