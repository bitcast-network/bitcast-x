"""
Connection database manager for storing account-tag connections.

Uses SQLite with a single connections table for all pools.
"""

import sqlite3
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional, List, Dict, Any
import bittensor as bt

from bitcast.validator.utils.config import NOCODE_UID, SIMULATE_CONNECTIONS


class ConnectionDatabase:
    """
    Manages SQLite database for account connection tracking.
    
    Uses a single connections table with pool_name column.
    """
    
    def __init__(self, db_path: Optional[Path] = None):
        """
        Initialize database connection.
        
        Args:
            db_path: Optional custom database path. Defaults to:
                    bitcast/validator/account_connection/connections.db
        """
        # Default database path
        if db_path is None:
            db_path = Path(__file__).parent / "connections.db"
        
        self.db_path = Path(db_path)
        
        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize schema on first use
        self.initialize_schema()
        
        bt.logging.debug(f"ConnectionDatabase initialized at {self.db_path}")
    
    def initialize_schema(self) -> None:
        """
        Create connections table and indexes if they don't exist.
        
        Table structure:
        - connection_id: Auto-incrementing primary key
        - pool_name: Name of the pool (e.g., 'tao')
        - tweet_id: ID of the tweet containing the tag
        - tag: The connection tag (e.g., bitcast-hk:...)
        - account_username: Twitter username that posted the tag
        - added: Timestamp when first discovered
        - updated: Timestamp of last update (for duplicate detection)
        - referral_code: Raw referral code (if provided)
        - referred_by: Decoded X handle of referrer (if provided)
        - referee_amount: USD bonus for the referee (default 50.0)
        - referrer_amount: USD bonus for the referrer (default 50.0)
        - payout_date: Date when referral bonus is paid (nullable, set once)
        
        UNIQUE constraint on (pool_name, account_username, tag) ensures one connection per pool-account-tag combination.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Create single connections table
            create_table_sql = """
            CREATE TABLE IF NOT EXISTS connections (
                connection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_name VARCHAR(50) NOT NULL,
                tweet_id BIGINT NOT NULL,
                tag VARCHAR(100) NOT NULL,
                account_username VARCHAR(100) NOT NULL,
                display_name VARCHAR(100),
                added DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                referral_code VARCHAR(100),
                referred_by VARCHAR(100),
                referee_amount REAL DEFAULT 50.0,
                referrer_amount REAL DEFAULT 50.0,
                payout_date DATE,
                UNIQUE(pool_name, account_username, tag)
            )
            """
            cursor.execute(create_table_sql)
            
            # Add columns that may be missing on existing databases
            existing_columns = {row[1] for row in cursor.execute("PRAGMA table_info(connections)").fetchall()}
            new_columns = [
                ("display_name", "VARCHAR(100)"),
                ("referral_code", "VARCHAR(100)"),
                ("referred_by", "VARCHAR(100)"),
                ("referee_amount", "REAL DEFAULT 50.0"),
                ("referrer_amount", "REAL DEFAULT 50.0"),
                ("payout_date", "DATE"),
            ]
            for col_name, col_type in new_columns:
                if col_name not in existing_columns:
                    cursor.execute(f"ALTER TABLE connections ADD COLUMN {col_name} {col_type}")
                    bt.logging.info(f"Added column {col_name} to connections table")
            
            # Create indexes for efficient querying
            indexes = [
                "CREATE INDEX IF NOT EXISTS idx_pool_name ON connections(pool_name)",
                "CREATE INDEX IF NOT EXISTS idx_tag ON connections(tag)",
                "CREATE INDEX IF NOT EXISTS idx_tweet_id ON connections(tweet_id)",
                "CREATE INDEX IF NOT EXISTS idx_account ON connections(account_username)",
                "CREATE INDEX IF NOT EXISTS idx_added ON connections(added)",
                "CREATE INDEX IF NOT EXISTS idx_payout_date ON connections(payout_date)",
            ]
            
            for index_sql in indexes:
                cursor.execute(index_sql)
            
            conn.commit()
            bt.logging.debug("Schema initialized for connections table")
    
    def upsert_connection(
        self,
        pool_name: str,
        tweet_id: int, 
        tag: str, 
        account_username: str,
        display_name: Optional[str] = None,
        referral_code: Optional[str] = None,
        referred_by: Optional[str] = None,
        referee_amount: float = 50.0,
        referrer_amount: float = 50.0,
    ) -> bool:
        """
        Insert new connection or update existing one.
        
        If a connection with the same pool_name, account_username and tag already exists,
        updates the updated timestamp and tweet_id. Otherwise, inserts a new record.
        Referral fields are only stored on the initial insert; updates preserve existing values.
        
        Returns:
            True if a new connection was inserted, False if existing was updated
        """
        pool_name = pool_name.lower()
        account_username = account_username.lower()
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            check_sql = """
            SELECT connection_id FROM connections
            WHERE pool_name = ? AND account_username = ? AND tag = ?
            """
            cursor.execute(check_sql, (pool_name, account_username, tag))
            existing = cursor.fetchone()
            
            if existing:
                update_sql = """
                UPDATE connections
                SET tweet_id = ?, updated = ?
                WHERE pool_name = ? AND account_username = ? AND tag = ?
                """
                cursor.execute(update_sql, (tweet_id, datetime.now(timezone.utc), pool_name, account_username, tag))
                conn.commit()
                bt.logging.debug(f"Updated connection: {pool_name}/{account_username} - {tag}")
                return False
            else:
                insert_sql = """
                INSERT INTO connections (pool_name, tweet_id, tag, account_username, display_name, added, updated,
                                        referral_code, referred_by, referee_amount, referrer_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                now = datetime.now(timezone.utc)
                cursor.execute(insert_sql, (pool_name, tweet_id, tag, account_username, display_name, now, now,
                                           referral_code, referred_by, referee_amount, referrer_amount))
                conn.commit()
                bt.logging.debug(f"Inserted new connection: {pool_name}/{account_username} - {tag}")
                return True
    
    def get_referrals_for_payout(self, payout_date: date, pool_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all referrals scheduled for payout on a specific date.
        
        Args:
            payout_date: The date to check for payouts
            pool_name: Optional pool name to filter by
            
        Returns:
            List of referral dictionaries with connection info
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            if pool_name:
                select_sql = """
                SELECT * FROM connections
                WHERE payout_date = ? AND pool_name = ?
                ORDER BY added DESC
                """
                cursor.execute(select_sql, (payout_date, pool_name.lower()))
            else:
                select_sql = """
                SELECT * FROM connections
                WHERE payout_date = ?
                ORDER BY added DESC
                """
                cursor.execute(select_sql, (payout_date,))
            
            results = cursor.fetchall()
            return [dict(row) for row in results]
    
    def set_payout_date(self, connection_id: int, payout_date: date) -> bool:
        """
        Set payout date for a referral. Only sets if currently null (one-time).
        
        Args:
            connection_id: The connection ID to update
            payout_date: Date when referral bonus is paid
            
        Returns:
            True if the date was set, False if already set
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            update_sql = """
            UPDATE connections
            SET payout_date = ?
            WHERE connection_id = ? AND payout_date IS NULL
            """
            cursor.execute(update_sql, (payout_date, connection_id))
            conn.commit()
            
            return cursor.rowcount > 0
    
    def get_all_connections_with_referrals(self, pool_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all connections that have referral information.
        
        Args:
            pool_name: Optional pool name to filter by
            
        Returns:
            List of connection dictionaries with referral fields
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            if pool_name:
                select_sql = """
                SELECT * FROM connections
                WHERE pool_name = ? AND referred_by IS NOT NULL
                ORDER BY added DESC
                """
                cursor.execute(select_sql, (pool_name.lower(),))
            else:
                select_sql = """
                SELECT * FROM connections
                WHERE referred_by IS NOT NULL
                ORDER BY added DESC
                """
                cursor.execute(select_sql)
            
            results = cursor.fetchall()
            return [dict(row) for row in results]
    
    def get_connections_by_tag(self, tag: str, pool_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all connections for a specific tag.
        
        Args:
            tag: The connection tag to search for
            pool_name: Optional pool name to filter by
            
        Returns:
            List of connection dictionaries with all fields
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            if pool_name:
                select_sql = """
                SELECT * FROM connections
                WHERE tag = ? AND pool_name = ?
                ORDER BY added DESC
                """
                cursor.execute(select_sql, (tag, pool_name.lower()))
            else:
                select_sql = """
                SELECT * FROM connections
                WHERE tag = ?
                ORDER BY added DESC
                """
                cursor.execute(select_sql, (tag,))
            
            results = cursor.fetchall()
            return [dict(row) for row in results]
    
    def get_connections_by_account(self, account_username: str, pool_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all connections for a specific account.
        
        Args:
            account_username: Twitter username
            pool_name: Optional pool name to filter by
            
        Returns:
            List of connection dictionaries with all fields
        """
        account_username = account_username.lower()
        
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            if pool_name:
                select_sql = """
                SELECT * FROM connections
                WHERE account_username = ? AND pool_name = ?
                ORDER BY added DESC
                """
                cursor.execute(select_sql, (account_username, pool_name.lower()))
            else:
                select_sql = """
                SELECT * FROM connections
                WHERE account_username = ?
                ORDER BY added DESC
                """
                cursor.execute(select_sql, (account_username,))
            
            results = cursor.fetchall()
            return [dict(row) for row in results]
    
    def connection_exists(self, pool_name: str, account_username: str, tag: str) -> bool:
        """
        Check if a connection already exists for a pool-account-tag combination.
        
        Args:
            pool_name: Name of the pool
            account_username: Twitter username
            tag: The connection tag
            
        Returns:
            True if connection exists, False otherwise
        """
        pool_name = pool_name.lower()
        account_username = account_username.lower()
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            check_sql = """
            SELECT 1 FROM connections
            WHERE pool_name = ? AND account_username = ? AND tag = ?
            LIMIT 1
            """
            cursor.execute(check_sql, (pool_name, account_username, tag))
            
            return cursor.fetchone() is not None
    
    def get_all_connections(self, pool_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all connections, optionally filtered by pool.
        
        Args:
            pool_name: Optional pool name to filter by
            
        Returns:
            List of all connection dictionaries
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            if pool_name:
                select_sql = """
                SELECT * FROM connections
                WHERE pool_name = ?
                ORDER BY added DESC
                """
                cursor.execute(select_sql, (pool_name.lower(),))
            else:
                select_sql = """
                SELECT * FROM connections
                ORDER BY added DESC
                """
                cursor.execute(select_sql)
            
            results = cursor.fetchall()
            return [dict(row) for row in results]
    
    def get_connection_count(self, pool_name: Optional[str] = None) -> int:
        """
        Get total number of connections, optionally filtered by pool.
        
        Args:
            pool_name: Optional pool name to filter by
            
        Returns:
            Number of connections
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            if pool_name:
                count_sql = "SELECT COUNT(*) FROM connections WHERE pool_name = ?"
                cursor.execute(count_sql, (pool_name.lower(),))
            else:
                count_sql = "SELECT COUNT(*) FROM connections"
                cursor.execute(count_sql)
            
            result = cursor.fetchone()
            return result[0] if result else 0
    
    def get_accounts_with_uids(
        self, 
        pool_name: str, 
        metagraph: "bt.metagraph"
    ) -> List[Dict[str, Any]]:
        """
        Get account-to-UID mappings for a pool.
        
        For bitcast-x<code> tags (e.g., bitcast-xabc123), uses NOCODE_UID (68).
        For bitcast-hk:{hotkey} tags, looks up UID in metagraph.
        Connections with unresolvable hotkeys have uid=None.
        
        If an account has multiple connections, only the most recent one (by updated timestamp) is returned.
        Results are sorted by UID in ascending order, with None values last.
        
        Args:
            pool_name: Name of the pool to query
            metagraph: Bittensor metagraph for UID lookups
            
        Returns:
            List of dictionaries with 'account_username' and 'uid' fields, sorted by UID.
            Each account appears only once (most recent connection).
            
        Example:
            >>> db = ConnectionDatabase()
            >>> accounts = db.get_accounts_with_uids("tao", validator.metagraph)
            >>> print(accounts[0])
            {'account_username': 'user1', 'uid': 42}
        """
        connections = self.get_all_connections(pool_name=pool_name)
        
        # Build account-to-UID mapping, keeping only most recent per account
        account_map = {}
        for conn in connections:
            tag = conn['tag']
            username = conn['account_username'].lower()  # Normalize to lowercase
            updated = conn['updated']
            uid = None
            
            if tag.startswith('bitcast-x'):
                # No-code mining tag
                uid = NOCODE_UID
                bt.logging.debug(f"Tag {tag} mapped to NOCODE_UID {NOCODE_UID}")
                
            elif tag.startswith('bitcast-hk:'):
                # Hotkey tag - extract and look up
                # Strip any referral code suffix before looking up hotkey
                hotkey_part = tag.split('bitcast-hk:', 1)[1]
                hotkey = hotkey_part.split('-')[0]  # Remove referral code suffix if present
                try:
                    uid = metagraph.hotkeys.index(hotkey)
                    bt.logging.debug(f"Hotkey {hotkey} found at UID {uid}")
                except ValueError:
                    uid = None
                    bt.logging.warning(f"Hotkey {hotkey} not found in metagraph")
            else:
                # Unknown tag format
                uid = None
                bt.logging.warning(f"Unknown tag format: {tag}")
            
            # Keep most recent connection per account
            if username not in account_map or updated > account_map[username]['updated']:
                account_map[username] = {
                    'account_username': username,
                    'uid': uid,
                    'updated': updated
                }
        
        # Convert to list and remove updated timestamp (internal use only)
        accounts = [
            {'account_username': acc['account_username'], 'uid': acc['uid']}
            for acc in account_map.values()
        ]
        
        # Add simulated connections if enabled
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
        
        # Sort by UID (None values last)
        accounts.sort(key=lambda x: (x['uid'] is None, x['uid'] or 0))
        return accounts

