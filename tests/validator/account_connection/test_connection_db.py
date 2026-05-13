"""
Unit tests for ConnectionDatabase.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from bitcast.validator.account_connection.connection_db import ConnectionDatabase
from bitcast.validator.utils.config import NOCODE_UID


class TestConnectionDatabase:
    """Test database operations."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)

        yield db_path

        if db_path.exists():
            db_path.unlink()

    def test_database_creation(self, temp_db):
        ConnectionDatabase(db_path=temp_db)
        assert temp_db.exists()

    def test_table_creation(self, temp_db):
        db = ConnectionDatabase(db_path=temp_db)
        assert db.get_all_connections() == []

    def test_upsert_new_connection(self, temp_db):
        db = ConnectionDatabase(db_path=temp_db)

        is_new = db.upsert_connection(
            tweet_id=123456789,
            tag="bitcast-UID{abc12345}",
            account_username="testuser",
        )

        assert is_new is True
        assert db.get_connection_count() == 1

    def test_upsert_existing_connection_refreshes_tag_and_tweet(self, temp_db):
        db = ConnectionDatabase(db_path=temp_db)

        is_new1 = db.upsert_connection(
            tweet_id=123456789,
            tag="bitcast-UID{abc12345}",
            account_username="testuser",
        )
        is_new2 = db.upsert_connection(
            tweet_id=987654321,
            tag="bitcast-UID{abc12345}",
            account_username="testuser",
        )

        assert is_new1 is True
        assert is_new2 is False
        assert db.get_connection_count() == 1

        connections = db.get_all_connections()
        assert connections[0]['tweet_id'] == 987654321

    def test_upsert_keeps_higher_referral_amount(self, temp_db):
        """A higher referee_amount upsert replaces the locked metadata."""
        db = ConnectionDatabase(db_path=temp_db)

        db.upsert_connection(
            tweet_id=123456789,
            tag="Stitch3-abc123-refcode",
            account_username="testuser",
            referral_code="refcode",
            referred_by="referrer",
            referee_amount=12.5,
            referrer_amount=12.5,
        )

        db.upsert_connection(
            tweet_id=987654321,
            tag="Stitch3-abc123-refcode",
            account_username="testuser",
            referral_code="newcode",
            referred_by="newref",
            referee_amount=99.0,
            referrer_amount=99.0,
        )

        connection = db.get_all_connections()[0]
        assert connection['tweet_id'] == 987654321
        assert connection['referee_amount'] == 99.0
        assert connection['referrer_amount'] == 99.0
        assert connection['referred_by'] == "newref"
        assert connection['referral_code'] == "newcode"

    def test_upsert_preserves_locked_metadata_when_new_amount_lower(self, temp_db):
        """A lower referee_amount upsert keeps the previously locked metadata."""
        db = ConnectionDatabase(db_path=temp_db)

        db.upsert_connection(
            tweet_id=123456789,
            tag="Stitch3-abc123-refcode",
            account_username="testuser",
            referral_code="refcode",
            referred_by="referrer",
            referee_amount=80.0,
            referrer_amount=80.0,
        )

        db.upsert_connection(
            tweet_id=987654321,
            tag="Stitch3-abc123-refcode",
            account_username="testuser",
            referral_code="othercode",
            referred_by="otherref",
            referee_amount=20.0,
            referrer_amount=20.0,
        )

        connection = db.get_all_connections()[0]
        # tag/tweet_id refresh to most recent, amount/referral metadata stay locked
        assert connection['tweet_id'] == 987654321
        assert connection['referee_amount'] == 80.0
        assert connection['referrer_amount'] == 80.0
        assert connection['referred_by'] == "referrer"
        assert connection['referral_code'] == "refcode"

    def test_upsert_locks_metadata_once_payout_date_set(self, temp_db):
        """Once payout_date is set, referral metadata is fully locked."""
        from datetime import date
        db = ConnectionDatabase(db_path=temp_db)

        db.upsert_connection(
            tweet_id=1,
            tag="Stitch3-abc",
            account_username="testuser",
            referral_code="r1",
            referred_by="alice",
            referee_amount=30.0,
            referrer_amount=30.0,
        )
        conn_id = db.get_all_connections()[0]['connection_id']
        assert db.set_payout_date(conn_id, date(2026, 1, 1)) is True

        db.upsert_connection(
            tweet_id=2,
            tag="Stitch3-xyz",
            account_username="testuser",
            referral_code="r2",
            referred_by="bob",
            referee_amount=99.0,
            referrer_amount=99.0,
        )

        connection = db.get_all_connections()[0]
        assert connection['tweet_id'] == 2  # tag/tweet_id still refresh
        assert connection['referee_amount'] == 30.0  # amount locked
        assert connection['referred_by'] == "alice"  # referrer locked

    def test_upsert_connection_drops_self_referral(self, temp_db):
        db = ConnectionDatabase(db_path=temp_db)

        db.upsert_connection(
            tweet_id=123456789,
            tag="Stitch3-abc123-refcode",
            account_username="testuser",
            referral_code="refcode",
            referred_by="@TestUser",
            referee_amount=99.0,
            referrer_amount=99.0,
        )

        connection = db.get_all_connections()[0]
        assert connection['account_username'] == "testuser"
        assert connection['referral_code'] is None
        assert connection['referred_by'] is None

    def test_one_row_per_account(self, temp_db):
        """Different accounts get separate rows; same account collapses to one row."""
        db = ConnectionDatabase(db_path=temp_db)

        db.upsert_connection(tweet_id=123, tag="bitcast-UID{abc12345}", account_username="user1")
        db.upsert_connection(tweet_id=456, tag="bitcast-UID{abc12345}", account_username="user2")
        db.upsert_connection(tweet_id=789, tag="bitcast-UID{abc12345}", account_username="user3")

        assert db.get_connection_count() == 3

        connections = db.get_connections_by_tag("bitcast-UID{abc12345}")
        assert len(connections) == 3

    def test_same_account_new_tag_collapses(self, temp_db):
        """The same account re-tagging keeps one row with the most recent tag."""
        db = ConnectionDatabase(db_path=temp_db)

        db.upsert_connection(tweet_id=123, tag="bitcast-UID{abc12345}", account_username="user1")
        db.upsert_connection(tweet_id=456, tag="bitcast-UID{def67890}", account_username="user1")

        assert db.get_connection_count() == 1
        connection = db.get_all_connections()[0]
        assert connection['tag'] == "bitcast-UID{def67890}"
        assert connection['tweet_id'] == 456

    def test_get_connections_by_tag(self, temp_db):
        db = ConnectionDatabase(db_path=temp_db)

        db.upsert_connection(tweet_id=123, tag="bitcast-UID{abc12345}", account_username="user1")
        db.upsert_connection(tweet_id=456, tag="bitcast-xxyz78900", account_username="user2")
        db.upsert_connection(tweet_id=789, tag="bitcast-UID{abc12345}", account_username="user3")

        connections = db.get_connections_by_tag("bitcast-UID{abc12345}")
        assert len(connections) == 2
        assert all(c['tag'] == "bitcast-UID{abc12345}" for c in connections)

        connections = db.get_connections_by_tag("bitcast-xxyz78900")
        assert len(connections) == 1
        assert connections[0]['account_username'] == "user2"

    def test_get_connections_by_account(self, temp_db):
        db = ConnectionDatabase(db_path=temp_db)

        db.upsert_connection(tweet_id=789, tag="bitcast-UID{def67890}", account_username="user2")
        db.upsert_connection(tweet_id=123, tag="bitcast-UID{abc12345}", account_username="user1")

        connections = db.get_connections_by_account("user1")
        assert len(connections) == 1
        assert connections[0]['tag'] == "bitcast-UID{abc12345}"

        connections = db.get_connections_by_account("user2")
        assert connections[0]['tag'] == "bitcast-UID{def67890}"

    def test_connection_exists(self, temp_db):
        db = ConnectionDatabase(db_path=temp_db)

        assert db.connection_exists("user1") is False

        db.upsert_connection(tweet_id=123, tag="bitcast-UID{abc12345}", account_username="user1")

        assert db.connection_exists("user1") is True
        assert db.connection_exists("user2") is False

    def test_username_case_insensitive(self, temp_db):
        db = ConnectionDatabase(db_path=temp_db)

        db.upsert_connection(tweet_id=123, tag="bitcast-UID{abc12345}", account_username="TestUser")

        connections = db.get_connections_by_account("testuser")
        assert len(connections) == 1
        assert db.connection_exists("TESTUSER") is True

    def test_pool_filter_uses_social_map(self, temp_db):
        """pool_name filter resolves accounts from the pool's social map."""
        db = ConnectionDatabase(db_path=temp_db)

        db.upsert_connection(tweet_id=123, tag="bitcast-UID{abc12345}", account_username="alice")
        db.upsert_connection(tweet_id=456, tag="bitcast-UID{def67890}", account_username="bob")
        db.upsert_connection(tweet_id=789, tag="bitcast-UID{ghi13579}", account_username="carol")

        with patch.object(ConnectionDatabase, "_load_pool_accounts", return_value={"alice", "bob"}):
            assert db.get_connection_count(pool_name="tao") == 2
            usernames = {c['account_username'] for c in db.get_all_connections(pool_name="tao")}
            assert usernames == {"alice", "bob"}

    def test_pool_filter_with_missing_social_map_returns_empty(self, temp_db):
        """If the pool's social map is missing, pool-filtered queries return [] rather than crashing."""
        db = ConnectionDatabase(db_path=temp_db)
        db.upsert_connection(tweet_id=123, tag="bitcast-UID{abc12345}", account_username="alice")

        with patch(
            "bitcast.validator.tweet_scoring.social_map_loader.load_latest_social_map",
            side_effect=FileNotFoundError("no social map for ghost-pool"),
        ):
            assert db.get_all_connections(pool_name="ghost-pool") == []
            assert db.get_connection_count(pool_name="ghost-pool") == 0
            # Unfiltered queries still see the row
            assert len(db.get_all_connections()) == 1

    def test_get_all_connections(self, temp_db):
        db = ConnectionDatabase(db_path=temp_db)

        db.upsert_connection(tweet_id=123, tag="bitcast-UID{abc12345}", account_username="user1")
        db.upsert_connection(tweet_id=456, tag="bitcast-xxyz78900", account_username="user2")
        db.upsert_connection(tweet_id=789, tag="bitcast-UID{def67890}", account_username="user3")

        assert len(db.get_all_connections()) == 3

    def test_connection_has_timestamps(self, temp_db):
        db = ConnectionDatabase(db_path=temp_db)

        db.upsert_connection(tweet_id=123, tag="bitcast-UID{abc12345}", account_username="user1")

        connection = db.get_all_connections()[0]
        assert connection['added'] is not None
        assert connection['updated'] is not None

    def test_get_accounts_with_uids_comprehensive(self, temp_db):
        """Test account-to-UID mapping with all tag types and sorting."""
        db = ConnectionDatabase(db_path=temp_db)

        hotkey1 = "5DNmHotkey1"
        hotkey2 = "5DNmHotkey2"

        db.upsert_connection(tweet_id=123, tag=f"bitcast-hk:{hotkey1}", account_username="user1")
        db.upsert_connection(tweet_id=456, tag="bitcast-xabc12345", account_username="user2")
        db.upsert_connection(tweet_id=789, tag=f"bitcast-hk:{hotkey2}", account_username="user3")
        db.upsert_connection(tweet_id=111, tag="bitcast-hk:InvalidKey", account_username="user4")

        mock_metagraph = MagicMock()
        mock_metagraph.hotkeys = [hotkey1, hotkey2]

        with patch.object(
            ConnectionDatabase,
            "_load_pool_accounts",
            return_value={"user1", "user2", "user3", "user4"},
        ):
            accounts = db.get_accounts_with_uids("test", mock_metagraph)

        assert len(accounts) == 4
        assert accounts[0] == {'account_username': 'user1', 'uid': 0}
        assert accounts[1] == {'account_username': 'user3', 'uid': 1}
        assert accounts[2] == {'account_username': 'user2', 'uid': NOCODE_UID}
        assert accounts[3] == {'account_username': 'user4', 'uid': None}

    def test_get_accounts_with_uids_stitch3_tags(self, temp_db):
        db = ConnectionDatabase(db_path=temp_db)

        hotkey1 = "5DNmHotkey1"

        db.upsert_connection(tweet_id=123, tag=f"Stitch-hk:{hotkey1}", account_username="user1")
        db.upsert_connection(tweet_id=456, tag="Stitch3-abc12345", account_username="user2")

        mock_metagraph = MagicMock()
        mock_metagraph.hotkeys = [hotkey1]

        with patch.object(
            ConnectionDatabase,
            "_load_pool_accounts",
            return_value={"user1", "user2"},
        ):
            accounts = db.get_accounts_with_uids("test", mock_metagraph)

        assert len(accounts) == 2
        assert accounts[0] == {'account_username': 'user1', 'uid': 0}
        assert accounts[1] == {'account_username': 'user2', 'uid': NOCODE_UID}

    def test_get_accounts_with_uids_pool_filter(self, temp_db):
        """Only accounts in the pool's social map are returned."""
        db = ConnectionDatabase(db_path=temp_db)

        hotkey1 = "5DNmHotkey1"

        db.upsert_connection(tweet_id=123, tag=f"bitcast-hk:{hotkey1}", account_username="alice")
        db.upsert_connection(tweet_id=456, tag="bitcast-xabc12345", account_username="bob")

        mock_metagraph = MagicMock()
        mock_metagraph.hotkeys = [hotkey1]

        with patch.object(
            ConnectionDatabase,
            "_load_pool_accounts",
            return_value={"alice"},  # only alice is in the pool
        ):
            accounts = db.get_accounts_with_uids("tao", mock_metagraph)

        assert len(accounts) == 1
        assert accounts[0] == {'account_username': 'alice', 'uid': 0}

