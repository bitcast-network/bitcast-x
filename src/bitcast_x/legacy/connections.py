"""Frozen legacy connection/referral state and account-to-miner routing."""

import base64
import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from bitcast_x.errors import ProtocolError

SCHEMA_VERSION = 2
_HOTKEY = r"([1-9A-HJ-NP-Za-km-z]{47,48})"
_REFERRAL = r"(?:-([a-z0-9_-]+))?"
_TAG_PATTERNS = (
    ("HK", re.compile(rf"Stitch-hk:{_HOTKEY}{_REFERRAL}", re.IGNORECASE)),
    ("X", re.compile(rf"Stitch3-([a-z0-9]+){_REFERRAL}", re.IGNORECASE)),
    ("HK", re.compile(rf"bitcast-hk:{_HOTKEY}{_REFERRAL}", re.IGNORECASE)),
    ("X", re.compile(rf"bitcast-x([a-z0-9]+){_REFERRAL}", re.IGNORECASE)),
)


@dataclass(frozen=True)
class Connection:
    """One money-bearing row from the frozen v2 SQLite schema."""

    connection_id: int
    tweet_id: str
    tag: str
    account_username: str
    added: str
    updated: str
    referral_code: str | None
    referred_by: str | None
    referee_amount: float
    referrer_amount: float
    payout_date: date | None

    @property
    def tag_type(self) -> str:
        """Return the frozen HK/X tag class or fail on corrupt state."""

        for tag_type, pattern in _TAG_PATTERNS:
            if pattern.fullmatch(self.tag):
                return tag_type
        raise ProtocolError(f"connection {self.connection_id} has an invalid tag")

    @property
    def hotkey(self) -> str | None:
        """Extract an explicitly tagged hotkey; no-code tags return null."""

        for tag_type, pattern in _TAG_PATTERNS:
            match = pattern.fullmatch(self.tag)
            if match is not None:
                return match.group(1) if tag_type == "HK" else None
        raise ProtocolError(f"connection {self.connection_id} has an invalid tag")


class ConnectionStore:
    """Operate the frozen v2 database without changing its schema or lock rules."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def validate(self) -> None:
        """Require the exact cutover schema before the validator can become ready."""

        if not self.path.is_file():
            raise ProtocolError(f"legacy connection database is missing: {self.path}")
        with closing(self._connect()) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise ProtocolError(
                    f"legacy connection database schema {version} != {SCHEMA_VERSION}"
                )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(connections)")}
        required = {
            "connection_id",
            "tweet_id",
            "tag",
            "account_username",
            "added",
            "updated",
            "referral_code",
            "referred_by",
            "referee_amount",
            "referrer_amount",
            "payout_date",
        }
        if columns != required:
            raise ProtocolError("legacy connection database columns do not match schema v2")

    def all(self) -> tuple[Connection, ...]:
        """Return every connection deterministically by normalized username."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT connection_id, tweet_id, tag, account_username, added, updated,
                       referral_code, referred_by, referee_amount, referrer_amount, payout_date
                FROM connections ORDER BY lower(account_username), connection_id
                """
            ).fetchall()
        return tuple(self._row(item) for item in rows)

    def by_username(self) -> dict[str, Connection]:
        """Return the unique lower-cased author mapping used by legacy attribution."""

        output: dict[str, Connection] = {}
        for item in self.all():
            username = item.account_username.casefold().lstrip("@")
            if username in output:
                raise ProtocolError(f"duplicate legacy connection for @{username}")
            output[username] = item
        return output

    def due_referrals(self, payout_date: date) -> tuple[Connection, ...]:
        """Return locked referrals due on one date without modifying them."""

        return tuple(
            item
            for item in self.all()
            if item.referred_by is not None and item.payout_date == payout_date
        )

    def upsert(
        self,
        *,
        tweet_id: str,
        tag: str,
        account_username: str,
        referral_code: str | None,
        referred_by: str | None,
        referee_amount: float,
        referrer_amount: float,
        now: datetime | None = None,
    ) -> bool:
        """Apply v2's newest-tag and strictly-higher unlocked-referral semantics."""

        username = account_username.casefold().lstrip("@")
        timestamp = now or datetime.now(UTC)
        with closing(self._write_connect()) as connection, connection:
            row = connection.execute(
                "SELECT referee_amount, payout_date FROM connections WHERE account_username = ?",
                (username,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO connections (
                        tweet_id, tag, account_username, added, updated, referral_code,
                        referred_by, referee_amount, referrer_amount
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tweet_id,
                        tag,
                        username,
                        timestamp.isoformat(),
                        timestamp.isoformat(),
                        referral_code,
                        referred_by,
                        referee_amount,
                        referrer_amount,
                    ),
                )
                return True
            replace = row[1] is None and referee_amount > float(row[0] or 0)
            if replace:
                connection.execute(
                    """
                    UPDATE connections SET tweet_id = ?, tag = ?, updated = ?,
                        referral_code = ?, referred_by = ?, referee_amount = ?,
                        referrer_amount = ? WHERE account_username = ?
                    """,
                    (
                        tweet_id,
                        tag,
                        timestamp.isoformat(),
                        referral_code,
                        referred_by,
                        referee_amount,
                        referrer_amount,
                        username,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE connections SET tweet_id = ?, tag = ?, updated = ? "
                    "WHERE account_username = ?",
                    (tweet_id, tag, timestamp.isoformat(), username),
                )
        return False

    def activate_referrals(
        self,
        participating_accounts: set[str],
        *,
        today: date | None = None,
    ) -> int:
        """Lock tomorrow as payout day on first participation, exactly once."""

        accounts = {item.casefold().lstrip("@") for item in participating_accounts}
        payout = (today or date.today()) + timedelta(days=1)
        if not accounts:
            return 0
        activated = 0
        with closing(self._write_connect()) as connection, connection:
            pending = connection.execute(
                "SELECT connection_id, account_username FROM connections "
                "WHERE referred_by IS NOT NULL AND payout_date IS NULL"
            ).fetchall()
            for connection_id, username in pending:
                if str(username).casefold().lstrip("@") not in accounts:
                    continue
                cursor = connection.execute(
                    "UPDATE connections SET payout_date = ? "
                    "WHERE connection_id = ? AND payout_date IS NULL",
                    (payout.isoformat(), connection_id),
                )
                activated += int(cursor.rowcount > 0)
        return activated

    def resolve_uids(
        self,
        hotkey_to_uid: dict[str, int],
        *,
        nocode_uid: int,
    ) -> dict[str, int]:
        """Map connected X authors to the same HK/no-code destinations as v2."""

        resolved: dict[str, int] = {}
        for username, item in self.by_username().items():
            if item.tag_type == "X":
                resolved[username] = nocode_uid
                continue
            hotkey = item.hotkey
            if hotkey is not None and hotkey in hotkey_to_uid:
                resolved[username] = hotkey_to_uid[hotkey]
        return resolved

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.path}?mode=ro"
        return sqlite3.connect(uri, uri=True)

    def _write_connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _row(row: Iterable[object]) -> Connection:
        values = list(row)
        payout = date.fromisoformat(str(values[10])) if values[10] is not None else None
        return Connection(
            connection_id=int(str(values[0])),
            tweet_id=str(values[1]),
            tag=str(values[2]),
            account_username=str(values[3]).casefold().lstrip("@"),
            added=str(values[4]),
            updated=str(values[5]),
            referral_code=str(values[6]) if values[6] is not None else None,
            referred_by=(str(values[7]).casefold().lstrip("@") if values[7] is not None else None),
            referee_amount=float(str(values[8] or 0)),
            referrer_amount=float(str(values[9] or 0)),
            payout_date=payout,
        )


def decode_referral_code(code: str | None) -> str | None:
    """Decode the frozen MySQL-compatible URL-safe base64 referral code."""

    if not code:
        return None
    try:
        padded = code + "=" * ((4 - len(code) % 4) % 4)
        return base64.b64decode(padded.replace("-", "+").replace("_", "/")).decode()
    except (ValueError, UnicodeDecodeError):
        return None
