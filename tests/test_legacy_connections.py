"""Frozen legacy connection-state compatibility tests."""

import sqlite3
from pathlib import Path

import pytest

from bitcast_x.errors import ProtocolError
from bitcast_x.legacy.connections import ConnectionStore, decode_referral_code

HOTKEY = "5FLSigC9H8sTAQG4q4FUFz3FK8t9vM7uU5KZJf5LrG1xVJdC"


def _database(path: Path, *, version: int = 2) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE connections (
                connection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tweet_id BIGINT NOT NULL,
                tag VARCHAR(100) NOT NULL,
                account_username VARCHAR(100) NOT NULL UNIQUE,
                added DATETIME NOT NULL,
                updated DATETIME NOT NULL,
                referral_code VARCHAR(100),
                referred_by VARCHAR(100),
                referee_amount REAL DEFAULT 50.0,
                referrer_amount REAL DEFAULT 50.0,
                payout_date DATE
            )
            """
        )
        connection.execute(f"PRAGMA user_version = {version}")
        connection.executemany(
            """
            INSERT INTO connections (
                tweet_id, tag, account_username, added, updated, referral_code,
                referred_by, referee_amount, referrer_amount, payout_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, f"Stitch-hk:{HOTKEY}", "Alice", "a", "b", None, None, 0, 0, None),
                (2, "Stitch3-builder", "Bob", "a", "b", "Y2Fyb2w", "Carol", 30, 20, "2026-08-09"),
            ],
        )


def test_existing_schema_routes_hotkey_and_nocode_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "connections.db"
    _database(path)
    store = ConnectionStore(path)
    store.validate()

    assert store.resolve_uids({HOTKEY: 7}, nocode_uid=114) == {"alice": 7, "bob": 114}
    assert store.all()[1].referee_amount == 30
    assert store.all()[1].payout_date is not None


def test_wrong_schema_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "connections.db"
    _database(path, version=1)
    with pytest.raises(ProtocolError, match="schema 1"):
        ConnectionStore(path).validate()


def test_referral_codec_matches_legacy() -> None:
    assert decode_referral_code("Y2Fyb2w") == "carol"
