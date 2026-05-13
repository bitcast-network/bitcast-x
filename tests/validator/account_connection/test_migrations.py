"""Tests for the connections DB schema migration runner."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from bitcast.validator.account_connection import migrations
from bitcast.validator.account_connection.connection_db import ConnectionDatabase


def _user_version(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _table_columns(db_path: Path, table: str) -> set:
    with sqlite3.connect(db_path) as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _seed_legacy_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE connections (
                connection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_name VARCHAR(50) NOT NULL,
                tweet_id BIGINT NOT NULL,
                tag VARCHAR(100) NOT NULL,
                account_username VARCHAR(100) NOT NULL,
                added DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                referral_code VARCHAR(100),
                referred_by VARCHAR(100),
                referee_amount REAL DEFAULT 50.0,
                referrer_amount REAL DEFAULT 50.0,
                payout_date DATE,
                UNIQUE(pool_name, account_username, tag)
            );
            CREATE INDEX idx_pool_name ON connections(pool_name);
            INSERT INTO connections
                (pool_name, tweet_id, tag, account_username, added, updated,
                 referral_code, referred_by, referee_amount, referrer_amount)
            VALUES
                ('tao', 1, 'tag-old', 'alice', '2026-01-01 00:00:00', '2026-01-01 00:00:00',
                 'code1', 'refA', 30.0, 30.0),
                ('foo', 2, 'tag-new', 'alice', '2026-01-02 00:00:00', '2026-01-02 00:00:00',
                 'code2', 'refB', 80.0, 80.0),
                ('tao', 3, 'tag-bob', 'bob',   '2026-01-01 00:00:00', '2026-01-01 00:00:00',
                 NULL, NULL, 50.0, 50.0);
        """)


@pytest.fixture
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    db_path.unlink()  # start without the file
    yield db_path
    if db_path.exists():
        db_path.unlink()
    for sibling in db_path.parent.glob(db_path.stem + ".db.bak.*"):
        sibling.unlink()


@pytest.fixture
def legacy_db():
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    _seed_legacy_db(db_path)
    yield db_path
    if db_path.exists():
        db_path.unlink()
    for sibling in db_path.parent.glob(db_path.stem + ".db.bak.*"):
        sibling.unlink()


def test_fresh_db_creates_v1_schema(tmp_db):
    migrations.run_migrations(tmp_db)

    assert _user_version(tmp_db) == migrations.SCHEMA_VERSION
    cols = _table_columns(tmp_db, "connections")
    assert "pool_name" not in cols
    assert "account_username" in cols
    assert "referee_amount" in cols
    # No backup made for a fresh DB.
    assert list(tmp_db.parent.glob(tmp_db.stem + ".db.bak.*")) == []


def test_legacy_db_collapses_and_stamps_version(legacy_db):
    migrations.run_migrations(legacy_db)

    assert _user_version(legacy_db) == migrations.SCHEMA_VERSION
    cols = _table_columns(legacy_db, "connections")
    assert "pool_name" not in cols

    with sqlite3.connect(legacy_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM connections ORDER BY account_username")]

    assert {r['account_username'] for r in rows} == {'alice', 'bob'}

    alice = next(r for r in rows if r['account_username'] == 'alice')
    # Most recent tag/tweet wins.
    assert alice['tag'] == 'tag-new'
    assert alice['tweet_id'] == 2
    # Highest referee_amount wins (here also the most recent).
    assert alice['referee_amount'] == 80.0
    assert alice['referred_by'] == 'refB'
    assert alice['referral_code'] == 'code2'
    # Earliest added preserved.
    assert alice['added'] == '2026-01-01 00:00:00'


def test_legacy_db_takes_backup(legacy_db):
    migrations.run_migrations(legacy_db)
    backups = list(legacy_db.parent.glob(legacy_db.stem + ".db.bak.*"))
    assert len(backups) == 1


def test_migration_is_idempotent(legacy_db):
    migrations.run_migrations(legacy_db)
    first_backups = list(legacy_db.parent.glob(legacy_db.stem + ".db.bak.*"))

    migrations.run_migrations(legacy_db)  # second call is a no-op
    second_backups = list(legacy_db.parent.glob(legacy_db.stem + ".db.bak.*"))

    assert _user_version(legacy_db) == migrations.SCHEMA_VERSION
    assert second_backups == first_backups  # no extra backup taken


def test_pool_agnostic_unversioned_db_gets_stamped_without_backup(tmp_db):
    # Simulate a DB created by an earlier checkout of this branch (new schema,
    # but never stamped with user_version).
    ConnectionDatabase(db_path=tmp_db)
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    # Wipe any backup the first-time init may have produced (it shouldn't).
    for sibling in tmp_db.parent.glob(tmp_db.stem + ".db.bak.*"):
        sibling.unlink()

    migrations.run_migrations(tmp_db)

    assert _user_version(tmp_db) == migrations.SCHEMA_VERSION
    assert list(tmp_db.parent.glob(tmp_db.stem + ".db.bak.*")) == []


def test_connection_database_init_runs_migrations(legacy_db):
    """ConnectionDatabase() on a legacy DB should silently upgrade it."""
    ConnectionDatabase(db_path=legacy_db)

    assert _user_version(legacy_db) == migrations.SCHEMA_VERSION
    assert "pool_name" not in _table_columns(legacy_db, "connections")
