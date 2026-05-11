"""
One-shot deployment migration: collapse the legacy pool-scoped connections
table into the pool-agnostic schema (one row per account_username).

Usage:
    python -m bitcast.validator.account_connection.migrate_drop_pool_name [--dry-run]
                                                                          [--db PATH]
                                                                          [--no-backup]
                                                                          [--yes]

Defaults:
    --db        bitcast/validator/account_connection/connections.db (next to this script)
    --backup    enabled (timestamped copy alongside the DB)

The migration is destructive (it rewrites the connections table). By default
the script takes a backup first and prompts for confirmation. Pass --yes to
skip the prompt in a non-interactive deploy.

Collapse rules per account_username:
  - tag, tweet_id, updated      : from the most recent row
  - added                       : earliest across the user's rows
  - referee_amount, referrer_amount,
    referred_by, referral_code  : from the row with the highest referee_amount
                                  (ties broken by most recent updated)
  - payout_date                 : earliest non-null (preserves any scheduled payout)

This script is intentionally self-contained: no imports from the runtime
account_connection package. Run it once as part of deployment; the runtime
code will never trigger it.
"""

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple


NEW_SCHEMA_SQL = """
CREATE TABLE connections_new (
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

NEW_SCHEMA_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tag ON connections(tag)",
    "CREATE INDEX IF NOT EXISTS idx_tweet_id ON connections(tweet_id)",
    "CREATE INDEX IF NOT EXISTS idx_account ON connections(account_username)",
    "CREATE INDEX IF NOT EXISTS idx_added ON connections(added)",
    "CREATE INDEX IF NOT EXISTS idx_payout_date ON connections(payout_date)",
]


def _is_legacy_schema(conn: sqlite3.Connection) -> bool:
    columns = [r[1] for r in conn.execute("PRAGMA table_info(connections)").fetchall()]
    return "pool_name" in columns


def _summarise_legacy(db_path: Path) -> Dict[str, Any]:
    """Inspect the legacy DB and return a summary dict for the dry-run report."""
    with sqlite3.connect(db_path) as conn:
        if not _is_legacy_schema(conn):
            return {"already_migrated": True}

        total = conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0]
        distinct_users = conn.execute(
            "SELECT COUNT(DISTINCT account_username) FROM connections"
        ).fetchone()[0]
        collapse_rows: List[Tuple[str, int, str]] = conn.execute("""
            SELECT account_username, COUNT(*) c, GROUP_CONCAT(pool_name, ',') pools
            FROM connections
            GROUP BY account_username
            HAVING c > 1
            ORDER BY c DESC, account_username
        """).fetchall()

    return {
        "already_migrated": False,
        "pre_rows": total,
        "post_rows": distinct_users,
        "collapses": collapse_rows,
    }


def _print_summary(db_path: Path, summary: Dict[str, Any]) -> None:
    print(f"Connections DB: {db_path}")
    if summary["already_migrated"]:
        print("Schema is already pool-agnostic (no pool_name column). Nothing to do.")
        return

    print(f"Rows before:     {summary['pre_rows']}")
    print(f"Rows after:      {summary['post_rows']}")
    print(f"Will collapse:   {summary['pre_rows'] - summary['post_rows']} row(s) "
          f"across {len(summary['collapses'])} account(s)")
    if summary["collapses"]:
        print()
        print("Accounts with multiple legacy rows (will be collapsed):")
        for username, count, pools in summary["collapses"][:25]:
            print(f"  @{username}: {count} rows  (pools: {pools})")
        if len(summary["collapses"]) > 25:
            print(f"  ... and {len(summary['collapses']) - 25} more")


def _make_backup(db_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_suffix(f".db.bak.{ts}")
    shutil.copy2(db_path, backup_path)
    return backup_path


def migrate(db_path: Path) -> Dict[str, Any]:
    """
    Run the collapse migration in place on db_path.

    Raises FileNotFoundError if the DB does not exist.
    Raises ValueError if the DB is already on the new schema.
    Returns: {'pre_rows', 'post_rows', 'collapsed_accounts': [usernames]}.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"connections DB not found at {db_path}")

    with sqlite3.connect(db_path) as conn:
        if not _is_legacy_schema(conn):
            raise ValueError(f"{db_path} is already on the new schema (no pool_name column)")

        cursor = conn.cursor()
        rows = cursor.execute("""
            SELECT account_username, tag, tweet_id, added, updated,
                   referral_code, referred_by, referee_amount, referrer_amount, payout_date
            FROM connections
        """).fetchall()

        per_user: Dict[str, Dict[str, Any]] = {}
        for (account_username, tag, tweet_id, added, updated,
             referral_code, referred_by, referee_amount, referrer_amount, payout_date) in rows:
            slot = per_user.setdefault(account_username, {
                "recent": None,
                "best": None,
                "earliest_added": added,
                "payout_date": None,
            })

            if slot["recent"] is None or (updated or "") > (slot["recent"]["updated"] or ""):
                slot["recent"] = {
                    "tag": tag,
                    "tweet_id": tweet_id,
                    "added": added,
                    "updated": updated,
                }

            if added and (slot["earliest_added"] is None or added < slot["earliest_added"]):
                slot["earliest_added"] = added

            existing_best = slot["best"]
            candidate_amt = referee_amount or 0.0
            if (
                existing_best is None
                or candidate_amt > (existing_best["referee_amount"] or 0.0)
                or (
                    candidate_amt == (existing_best["referee_amount"] or 0.0)
                    and (updated or "") > (existing_best["updated"] or "")
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

        cursor.execute(NEW_SCHEMA_SQL)

        for account_username, slot in per_user.items():
            recent = slot["recent"]
            best = slot["best"] or {}
            cursor.execute("""
                INSERT INTO connections_new (
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

        cursor.execute("DROP TABLE connections")
        cursor.execute("ALTER TABLE connections_new RENAME TO connections")

        for index_sql in NEW_SCHEMA_INDEXES:
            cursor.execute(index_sql)
        cursor.execute("DROP INDEX IF EXISTS idx_pool_name")

        conn.commit()

    seen: Dict[str, int] = {}
    for r in rows:
        seen[r[0]] = seen.get(r[0], 0) + 1
    collapsed_accounts = sorted(u for u, c in seen.items() if c > 1)

    return {
        "pre_rows": len(rows),
        "post_rows": len(per_user),
        "collapsed_accounts": collapsed_accounts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collapse legacy pool-scoped connections.db into the pool-agnostic schema.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).parent / "connections.db",
        help="Path to connections.db (default: alongside this module)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned changes without modifying the DB",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the timestamped backup (NOT recommended for prod)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (use in non-interactive deploys)",
    )
    args = parser.parse_args()

    db_path: Path = args.db
    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist", file=sys.stderr)
        return 1

    summary = _summarise_legacy(db_path)
    _print_summary(db_path, summary)

    if summary["already_migrated"]:
        return 0

    if args.dry_run:
        print()
        print("DRY RUN - no changes made.")
        return 0

    if not args.yes:
        print()
        try:
            reply = input("Proceed with migration? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 1

    backup_path = None
    if not args.no_backup:
        backup_path = _make_backup(db_path)
        print(f"Backed up to: {backup_path}")

    try:
        result = migrate(db_path)
    except Exception as exc:
        print(f"ERROR: migration failed: {exc}", file=sys.stderr)
        if backup_path is not None:
            print(f"Restore from backup with: cp {backup_path} {db_path}", file=sys.stderr)
        return 2

    print()
    print("Migration complete.")
    print(f"  rows before:        {result['pre_rows']}")
    print(f"  rows after:         {result['post_rows']}")
    print(f"  accounts collapsed: {len(result['collapsed_accounts'])}")
    if result["collapsed_accounts"]:
        sample = ", ".join(f"@{u}" for u in result["collapsed_accounts"][:10])
        suffix = ", ..." if len(result["collapsed_accounts"]) > 10 else ""
        print(f"  collapsed: {sample}{suffix}")
    if backup_path is not None:
        print(f"  backup kept at:     {backup_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
