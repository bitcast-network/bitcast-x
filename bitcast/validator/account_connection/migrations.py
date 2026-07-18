"""
Schema migrations for the connections SQLite database.

Each migration moves the DB from version N-1 to version N. Versions are
tracked via ``PRAGMA user_version``. ``run_migrations`` is idempotent: it
inspects the current version, applies any pending migrations in order, and
stamps the new version. Destructive migrations take a timestamped backup
before rewriting the table.

To add a new migration:
  1. Bump ``SCHEMA_VERSION``.
  2. Add a ``_to_v{N}`` function below.
  3. Register it in ``MIGRATIONS``.
"""

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import bittensor as bt


SCHEMA_VERSION = 1


_LATEST_TABLE_SQL = """
CREATE TABLE connections (
    connection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id BIGINT NOT NULL,
    tag VARCHAR(100) NOT NULL,
    account_username VARCHAR(100) NOT NULL UNIQUE,
    added DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    referral_code VARCHAR(100),
    referred_by VARCHAR(100),
    referee_amount REAL DEFAULT 50.0,
    referrer_amount REAL DEFAULT 50.0,
    payout_date DATE
)
"""

_LATEST_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tag ON connections(tag)",
    "CREATE INDEX IF NOT EXISTS idx_tweet_id ON connections(tweet_id)",
    "CREATE INDEX IF NOT EXISTS idx_account ON connections(account_username)",
    "CREATE INDEX IF NOT EXISTS idx_added ON connections(added)",
    "CREATE INDEX IF NOT EXISTS idx_payout_date ON connections(payout_date)",
]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    # PRAGMA statements cannot use bound parameters; `table` is an internal constant.
    # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
    return column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _create_indexes(conn: sqlite3.Connection) -> None:
    for sql in _LATEST_INDEXES:
        conn.execute(sql)


def _backup(db_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_suffix(f".db.bak.{ts}")
    shutil.copy2(db_path, backup_path)
    bt.logging.info(f"Backed up connections DB to {backup_path}")
    return backup_path


def _collapse_pool_scoped_table(conn: sqlite3.Connection) -> Tuple[int, int, List[str]]:
    """
    Rewrite the legacy pool-scoped ``connections`` table into the pool-agnostic
    schema (one row per ``account_username``).

    Collapse rules per account:
      - tag, tweet_id, updated      : from the row with the most recent ``updated``
      - added                       : earliest ``added`` across the user's rows
      - referee_amount, referrer_amount,
        referred_by, referral_code  : from the row with the highest referee_amount
                                      (ties broken by most recent ``updated``)
      - payout_date                 : earliest non-null (preserves any scheduled payout)

    Returns (pre_rows, post_rows, collapsed_accounts).
    """
    rows = conn.execute("""
        SELECT account_username, tag, tweet_id, added, updated,
               referral_code, referred_by, referee_amount, referrer_amount, payout_date
        FROM connections
    """).fetchall()

    per_user: Dict[str, Dict[str, Any]] = {}
    seen_counts: Dict[str, int] = {}

    for (account_username, tag, tweet_id, added, updated,
         referral_code, referred_by, referee_amount, referrer_amount, payout_date) in rows:
        seen_counts[account_username] = seen_counts.get(account_username, 0) + 1

        slot = per_user.setdefault(account_username, {
            "recent": None,
            "best": None,
            "earliest_added": added,
            "payout_date": None,
        })

        if slot["recent"] is None or (updated or "") > (slot["recent"]["updated"] or ""):
            slot["recent"] = {"tag": tag, "tweet_id": tweet_id, "updated": updated}

        if added and (slot["earliest_added"] is None or added < slot["earliest_added"]):
            slot["earliest_added"] = added

        candidate_amt = referee_amount or 0.0
        best = slot["best"]
        if (
            best is None
            or candidate_amt > (best["referee_amount"] or 0.0)
            or (
                candidate_amt == (best["referee_amount"] or 0.0)
                and (updated or "") > (best["updated"] or "")
            )
        ):
            slot["best"] = {
                "referee_amount": referee_amount,
                "referrer_amount": referrer_amount,
                "referred_by": referred_by,
                "referral_code": referral_code,
                "updated": updated,
            }

        if payout_date is not None and (slot["payout_date"] is None or payout_date < slot["payout_date"]):
            slot["payout_date"] = payout_date

    conn.execute("ALTER TABLE connections RENAME TO connections_legacy")
    conn.execute(_LATEST_TABLE_SQL)

    for account_username, slot in per_user.items():
        recent = slot["recent"]
        best = slot["best"] or {}
        conn.execute("""
            INSERT INTO connections (
                tweet_id, tag, account_username, added, updated,
                referral_code, referred_by, referee_amount, referrer_amount, payout_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            recent["tweet_id"], recent["tag"], account_username,
            slot["earliest_added"], recent["updated"],
            best.get("referral_code"), best.get("referred_by"),
            best.get("referee_amount"), best.get("referrer_amount"),
            slot["payout_date"],
        ))

    conn.execute("DROP TABLE connections_legacy")
    conn.execute("DROP INDEX IF EXISTS idx_pool_name")
    _create_indexes(conn)

    collapsed = sorted(u for u, c in seen_counts.items() if c > 1)
    return len(rows), len(per_user), collapsed


def _to_v1(db_path: Path, conn: sqlite3.Connection) -> None:
    """
    v0 → v1: pool-agnostic connections table with UNIQUE(account_username).

    Three cases handled idempotently:
      - no ``connections`` table       → create at latest schema (fresh DB).
      - legacy schema (pool_name col)  → backup + collapse rows into new schema.
      - already pool-agnostic          → no-op (just stamp the version).
    """
    if not _table_exists(conn, "connections"):
        conn.execute(_LATEST_TABLE_SQL)
        _create_indexes(conn)
        bt.logging.debug("Created connections table at schema v1")
        return

    if _has_column(conn, "connections", "pool_name"):
        bt.logging.info("Detected legacy pool-scoped connections table; collapsing to pool-agnostic schema")
        _backup(db_path)
        pre, post, collapsed = _collapse_pool_scoped_table(conn)
        bt.logging.info(
            f"Migration v0→v1 complete: {pre} rows → {post} rows; "
            f"{len(collapsed)} account(s) collapsed"
        )
        return

    bt.logging.debug("connections table already at schema v1; stamping version")


MIGRATIONS: Dict[int, Callable[[Path, sqlite3.Connection], None]] = {
    1: _to_v1,
}


def run_migrations(db_path: Path) -> int:
    """
    Apply any pending migrations to bring the DB up to ``SCHEMA_VERSION``.

    Returns the version the DB is now at. Safe to call on every startup —
    no-op when the DB is already current.
    """
    db_path = Path(db_path)
    with sqlite3.connect(db_path) as conn:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current >= SCHEMA_VERSION:
            return current

        for version in range(current + 1, SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise RuntimeError(f"No migration registered for schema version {version}")
            migration(db_path, conn)
            # PRAGMA statements cannot use bound parameters; `version` is an int from the loop above.
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()

        return SCHEMA_VERSION
