"""
Connection database manager for storing account-tag connections.

Uses SQLite with a single pool-agnostic connections table. Eligibility for a
given pool is resolved at query time by filtering connections whose
account_username appears in that pool's latest social map.
"""

import sqlite3
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional, List, Dict, Any, Set
import bittensor as bt

from bitcast.validator.utils.config import NOCODE_UID, SIMULATE_CONNECTIONS  # noqa: F401


class ConnectionDatabase:
    """
    Manages SQLite database for account connection tracking.

    The table stores one row per account_username. Pool membership is not
    stored; it is resolved at query time from each pool's latest social map.
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path(__file__).parent / "connections.db"

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.initialize_schema()

        bt.logging.debug(f"ConnectionDatabase initialized at {self.db_path}")

    def initialize_schema(self) -> None:
        """
        Create the connections table and indexes if they don't exist.

        Table structure (one row per account_username):
        - connection_id: Auto-incrementing primary key
        - tweet_id: ID of the tweet containing the tag
        - tag: The connection tag (e.g., bitcast-hk:...)
        - account_username: Twitter username that posted the tag (UNIQUE)
        - added: Timestamp when first discovered
        - updated: Timestamp of last update
        - referral_code: Raw referral code (if provided)
        - referred_by: Decoded X handle of referrer (if provided)
        - referee_amount: USD bonus for the referee
        - referrer_amount: USD bonus for the referrer
        - payout_date: Date when referral bonus is paid (nullable, set once)
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS connections (
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
            """)

            for index_sql in [
                "CREATE INDEX IF NOT EXISTS idx_tag ON connections(tag)",
                "CREATE INDEX IF NOT EXISTS idx_tweet_id ON connections(tweet_id)",
                "CREATE INDEX IF NOT EXISTS idx_account ON connections(account_username)",
                "CREATE INDEX IF NOT EXISTS idx_added ON connections(added)",
                "CREATE INDEX IF NOT EXISTS idx_payout_date ON connections(payout_date)",
            ]:
                cursor.execute(index_sql)

            conn.commit()
            bt.logging.debug("Schema initialized for connections table")

    def _load_pool_accounts(self, pool_name: str) -> Set[str]:
        """
        Return the lowercase set of accounts in a pool's latest social map.

        If no social map exists for the pool (or it is malformed), returns an
        empty set so the caller degrades to "no eligible accounts" rather than
        crashing the scoring cycle.
        """
        from bitcast.validator.tweet_scoring.social_map_loader import load_latest_social_map
        try:
            social_map, _ = load_latest_social_map(pool_name.lower())
        except (FileNotFoundError, ValueError) as e:
            bt.logging.warning(
                f"No usable social map for pool '{pool_name}' ({e}); "
                f"treating pool as having no eligible accounts."
            )
            return set()
        return {username.lower() for username in social_map.get("accounts", {})}

    def upsert_connection(
        self,
        tweet_id: int,
        tag: str,
        account_username: str,
        referral_code: Optional[str] = None,
        referred_by: Optional[str] = None,
        referee_amount: float = 50.0,
        referrer_amount: float = 50.0,
    ) -> bool:
        """
        Insert a new connection or update the existing row for the user.

        On update:
          - tag, tweet_id and updated are always refreshed (most-recent-tag wins).
          - referee_amount/referrer_amount/referred_by/referral_code are replaced
            only if the new referee_amount strictly exceeds the existing value
            AND payout_date has not been set. Otherwise the locked referral
            metadata is preserved.

        Returns True if a new row was inserted, False if an existing row was updated.
        """
        account_username = account_username.lower()

        if referred_by and referred_by.strip().lower().lstrip("@") == account_username:
            bt.logging.info(f"Ignoring self-referral for @{account_username}")
            referral_code = None
            referred_by = None

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            row = cursor.execute(
                "SELECT referee_amount, payout_date FROM connections WHERE account_username = ?",
                (account_username,),
            ).fetchone()

            now = datetime.now(timezone.utc)

            if row is None:
                cursor.execute(
                    """
                    INSERT INTO connections (
                        tweet_id, tag, account_username, added, updated,
                        referral_code, referred_by, referee_amount, referrer_amount
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tweet_id, tag, account_username, now, now,
                     referral_code, referred_by, referee_amount, referrer_amount),
                )
                conn.commit()
                bt.logging.debug(f"Inserted new connection: {account_username} - {tag}")
                return True

            existing_amount = row[0] or 0.0
            existing_payout_date = row[1]

            metadata_locked = existing_payout_date is not None
            replace_metadata = (not metadata_locked) and (referee_amount > existing_amount)

            if replace_metadata:
                cursor.execute(
                    """
                    UPDATE connections
                    SET tweet_id = ?, tag = ?, updated = ?,
                        referral_code = ?, referred_by = ?,
                        referee_amount = ?, referrer_amount = ?
                    WHERE account_username = ?
                    """,
                    (tweet_id, tag, now, referral_code, referred_by,
                     referee_amount, referrer_amount, account_username),
                )
            else:
                cursor.execute(
                    """
                    UPDATE connections
                    SET tweet_id = ?, tag = ?, updated = ?
                    WHERE account_username = ?
                    """,
                    (tweet_id, tag, now, account_username),
                )

            conn.commit()
            bt.logging.debug(f"Updated connection: {account_username} - {tag}")
            return False

    def get_referrals_for_payout(self, payout_date: date, pool_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all referrals scheduled for payout on a specific date, optionally filtered to a pool's eligible accounts."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM connections WHERE payout_date = ? ORDER BY added DESC",
                (payout_date,),
            )
            results = [dict(row) for row in cursor.fetchall()]

        if pool_name:
            allowed = self._load_pool_accounts(pool_name)
            results = [r for r in results if r["account_username"].lower() in allowed]
        return results

    def set_payout_date(self, connection_id: int, payout_date: date) -> bool:
        """Set payout date for a referral. Only sets if currently null (one-time)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE connections
                SET payout_date = ?
                WHERE connection_id = ? AND payout_date IS NULL
                """,
                (payout_date, connection_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_all_connections_with_referrals(self, pool_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all connections that have referral information, optionally filtered to a pool's eligible accounts."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM connections WHERE referred_by IS NOT NULL ORDER BY added DESC"
            )
            results = [dict(row) for row in cursor.fetchall()]

        if pool_name:
            allowed = self._load_pool_accounts(pool_name)
            results = [r for r in results if r["account_username"].lower() in allowed]
        return results

    def get_connections_by_tag(self, tag: str, pool_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all connections for a specific tag, optionally filtered to a pool's eligible accounts."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM connections WHERE tag = ? ORDER BY added DESC",
                (tag,),
            )
            results = [dict(row) for row in cursor.fetchall()]

        if pool_name:
            allowed = self._load_pool_accounts(pool_name)
            results = [r for r in results if r["account_username"].lower() in allowed]
        return results

    def get_connections_by_account(self, account_username: str) -> List[Dict[str, Any]]:
        """Get the connection row for a specific account (returns at most one row)."""
        account_username = account_username.lower()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM connections WHERE account_username = ? ORDER BY added DESC",
                (account_username,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def connection_exists(self, account_username: str) -> bool:
        """Check if a connection row exists for an account."""
        account_username = account_username.lower()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM connections WHERE account_username = ? LIMIT 1",
                (account_username,),
            )
            return cursor.fetchone() is not None

    def get_all_connections(self, pool_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all connection rows.

        If pool_name is provided, filters to accounts present in that pool's
        latest social map. Otherwise returns every row.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM connections ORDER BY added DESC")
            results = [dict(row) for row in cursor.fetchall()]

        if pool_name:
            allowed = self._load_pool_accounts(pool_name)
            results = [r for r in results if r["account_username"].lower() in allowed]
        return results

    def get_connection_count(self, pool_name: Optional[str] = None) -> int:
        """Get total number of connections, optionally filtered to a pool's eligible accounts."""
        if pool_name is None:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM connections")
                result = cursor.fetchone()
                return result[0] if result else 0
        return len(self.get_all_connections(pool_name=pool_name))

    def get_accounts_with_uids(
        self,
        pool_name: str,
        metagraph: "bt.metagraph"
    ) -> List[Dict[str, Any]]:
        """
        Get account-to-UID mappings for a pool.

        For no-code tags (Stitch3-{code} or legacy bitcast-x{code}), uses NOCODE_UID (68).
        For hotkey tags (Stitch-hk:{hotkey} or legacy bitcast-hk:{hotkey}), looks up UID in metagraph.
        Connections with unresolvable hotkeys have uid=None.

        Args:
            pool_name: Name of the pool to query (filters by pool's social map accounts)
            metagraph: Bittensor metagraph for UID lookups

        Returns:
            List of dictionaries with 'account_username' and 'uid' fields, sorted by UID.
        """
        connections = self.get_all_connections(pool_name=pool_name)

        account_map: Dict[str, Dict[str, Any]] = {}
        for conn in connections:
            tag = conn['tag']
            username = conn['account_username'].lower()
            updated = conn['updated']
            uid: Optional[int] = None

            if tag.startswith('Stitch3-') or tag.startswith('bitcast-x'):
                uid = NOCODE_UID
                bt.logging.debug(f"Tag {tag} mapped to NOCODE_UID {NOCODE_UID}")
            elif tag.startswith('Stitch-hk:') or tag.startswith('bitcast-hk:'):
                hotkey_part = tag.split(':', 1)[1]
                hotkey = hotkey_part.split('-')[0]
                try:
                    uid = metagraph.hotkeys.index(hotkey)
                    bt.logging.debug(f"Hotkey {hotkey} found at UID {uid}")
                except ValueError:
                    uid = None
                    bt.logging.warning(f"Hotkey {hotkey} not found in metagraph")
            else:
                uid = None
                bt.logging.warning(f"Unknown tag format: {tag}")

            if username not in account_map or updated > account_map[username]['updated']:
                account_map[username] = {
                    'account_username': username,
                    'uid': uid,
                    'updated': updated,
                }

        accounts = [
            {'account_username': acc['account_username'], 'uid': acc['uid']}
            for acc in account_map.values()
        ]

        if SIMULATE_CONNECTIONS:
            try:
                from bitcast.validator.tweet_scoring.social_map_loader import load_latest_social_map, get_active_members
                social_map, _ = load_latest_social_map(pool_name)
                connected = {acc['account_username'].lower() for acc in accounts}
                unconnected = [m for m in get_active_members(social_map) if m.lower() not in connected]
                accounts.extend({'account_username': m.lower(), 'uid': NOCODE_UID} for m in unconnected)
                if unconnected:
                    bt.logging.info(f"SIMULATE_CONNECTIONS: Added {len(unconnected)} connections to UID {NOCODE_UID}")
            except Exception as e:
                bt.logging.warning(f"Failed to simulate connections: {e}")

        accounts.sort(key=lambda x: (x['uid'] is None, x['uid'] or 0))
        return accounts
