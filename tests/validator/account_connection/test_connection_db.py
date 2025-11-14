"""
Unit tests for ConnectionDatabase.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
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
        
        # Cleanup
        if db_path.exists():
            db_path.unlink()
    
    def test_database_creation(self, temp_db):
        """Test that database file is created."""
        db = ConnectionDatabase(db_path=temp_db)
        assert temp_db.exists()
    
    def test_table_creation(self, temp_db):
        """Test that table is created."""
        db = ConnectionDatabase(db_path=temp_db)
        
        # Verify table exists by trying to query it
        connections = db.get_all_connections()
        assert connections == []
    
    def test_upsert_new_connection(self, temp_db):
        """Test inserting a new connection."""
        db = ConnectionDatabase(db_path=temp_db)
        
        is_new = db.upsert_connection(
            pool_name="test",
            tweet_id=123456789,
            tag="bitcast-UID{abc12345}",
            account_username="testuser"
        )
        
        assert is_new is True
        assert db.get_connection_count() == 1
    
    def test_upsert_existing_connection(self, temp_db):
        """Test updating an existing connection."""
        db = ConnectionDatabase(db_path=temp_db)
        
        # Insert first time
        is_new1 = db.upsert_connection(
            pool_name="test",
            tweet_id=123456789,
            tag="bitcast-UID{abc12345}",
            account_username="testuser"
        )
        
        # Insert same pool-account-tag combination again (should update)
        is_new2 = db.upsert_connection(
            pool_name="test",
            tweet_id=987654321,  # Different tweet ID
            tag="bitcast-UID{abc12345}",
            account_username="testuser"
        )
        
        assert is_new1 is True
        assert is_new2 is False
        assert db.get_connection_count(pool_name="test") == 1  # Still only one connection
        
        # Verify tweet_id was updated
        connections = db.get_all_connections(pool_name="test")
        assert connections[0]['tweet_id'] == 987654321
    
    def test_multiple_accounts_same_tag(self, temp_db):
        """Test that multiple accounts can use the same tag."""
        db = ConnectionDatabase(db_path=temp_db)
        
        # Insert same tag for different accounts
        db.upsert_connection("test", 123, "bitcast-UID{abc12345}", "user1")
        db.upsert_connection("test", 456, "bitcast-UID{abc12345}", "user2")
        db.upsert_connection("test", 789, "bitcast-UID{abc12345}", "user3")
        
        assert db.get_connection_count(pool_name="test") == 3
        
        # Query by tag should return all three
        connections = db.get_connections_by_tag("bitcast-UID{abc12345}", pool_name="test")
        assert len(connections) == 3
    
    def test_get_connections_by_tag(self, temp_db):
        """Test querying connections by tag."""
        db = ConnectionDatabase(db_path=temp_db)
        
        db.upsert_connection("test", 123, "bitcast-UID{abc12345}", "user1")
        db.upsert_connection("test", 456, "bitcast-xxyz78900", "user2")
        db.upsert_connection("test", 789, "bitcast-UID{abc12345}", "user3")
        
        # Query first tag
        connections = db.get_connections_by_tag("bitcast-UID{abc12345}", pool_name="test")
        assert len(connections) == 2
        assert all(c['tag'] == "bitcast-UID{abc12345}" for c in connections)
        
        # Query second tag
        connections = db.get_connections_by_tag("bitcast-xxyz78900", pool_name="test")
        assert len(connections) == 1
        assert connections[0]['account_username'] == "user2"
    
    def test_get_connections_by_account(self, temp_db):
        """Test querying connections by account."""
        db = ConnectionDatabase(db_path=temp_db)
        
        db.upsert_connection("test", 123, "bitcast-UID{abc12345}", "user1")
        db.upsert_connection("test", 456, "bitcast-xxyz78900", "user1")
        db.upsert_connection("test", 789, "bitcast-UID{def67890}", "user2")
        
        # Query first account
        connections = db.get_connections_by_account("user1", pool_name="test")
        assert len(connections) == 2
        assert all(c['account_username'] == "user1" for c in connections)
        
        # Query second account
        connections = db.get_connections_by_account("user2", pool_name="test")
        assert len(connections) == 1
        assert connections[0]['tag'] == "bitcast-UID{def67890}"
    
    def test_connection_exists(self, temp_db):
        """Test checking if connection exists."""
        db = ConnectionDatabase(db_path=temp_db)
        
        # Should not exist initially
        assert db.connection_exists("test", "user1", "bitcast-UID{abc12345}") is False
        
        # Insert connection
        db.upsert_connection("test", 123, "bitcast-UID{abc12345}", "user1")
        
        # Should exist now
        assert db.connection_exists("test", "user1", "bitcast-UID{abc12345}") is True
        
        # Different account should not exist
        assert db.connection_exists("test", "user2", "bitcast-UID{abc12345}") is False
    
    def test_username_case_insensitive(self, temp_db):
        """Test that usernames are stored and queried case-insensitively."""
        db = ConnectionDatabase(db_path=temp_db)
        
        # Insert with mixed case
        db.upsert_connection("test", 123, "bitcast-UID{abc12345}", "TestUser")
        
        # Query with different case should work
        connections = db.get_connections_by_account("testuser", pool_name="test")
        assert len(connections) == 1
        
        # Check existence with different case
        assert db.connection_exists("test", "TESTUSER", "bitcast-UID{abc12345}") is True
    
    def test_multiple_pools_separate_tables(self, temp_db):
        """Test that different pools can have separate data in the same table."""
        db = ConnectionDatabase(db_path=temp_db)
        
        # Insert into first pool
        db.upsert_connection("tao", 123, "bitcast-UID{abc12345}", "user1")
        
        # Insert into second pool
        db.upsert_connection("btc", 456, "bitcast-xxyz78900", "user2")
        
        # Each pool should only see its own connections
        assert db.get_connection_count(pool_name="tao") == 1
        assert db.get_connection_count(pool_name="btc") == 1
        
        connections1 = db.get_all_connections(pool_name="tao")
        connections2 = db.get_all_connections(pool_name="btc")
        
        assert connections1[0]['tag'] == "bitcast-UID{abc12345}"
        assert connections2[0]['tag'] == "bitcast-xxyz78900"
        
        # Total count across all pools
        assert db.get_connection_count() == 2
    
    def test_get_all_connections(self, temp_db):
        """Test getting all connections for a pool."""
        db = ConnectionDatabase(db_path=temp_db)
        
        # Insert multiple connections
        db.upsert_connection("test", 123, "bitcast-UID{abc12345}", "user1")
        db.upsert_connection("test", 456, "bitcast-xxyz78900", "user2")
        db.upsert_connection("test", 789, "bitcast-UID{def67890}", "user3")
        
        connections = db.get_all_connections(pool_name="test")
        assert len(connections) == 3
    
    def test_connection_has_timestamps(self, temp_db):
        """Test that connections have added and updated timestamps."""
        db = ConnectionDatabase(db_path=temp_db)
        
        db.upsert_connection("test", 123, "bitcast-UID{abc12345}", "user1")
        
        connections = db.get_all_connections(pool_name="test")
        assert len(connections) == 1
        
        connection = connections[0]
        assert 'added' in connection
        assert 'updated' in connection
        assert connection['added'] is not None
        assert connection['updated'] is not None
    
    def test_get_accounts_with_uids_comprehensive(self, temp_db):
        """Test account-to-UID mapping with all tag types and sorting."""
        db = ConnectionDatabase(db_path=temp_db)
        
        hotkey1 = "5DNmHotkey1"
        hotkey2 = "5DNmHotkey2"
        
        # Mix of tag types to test all code paths
        db.upsert_connection("test", 123, f"bitcast-hk:{hotkey1}", "user1")  # Valid hotkey -> UID 0
        db.upsert_connection("test", 456, "bitcast-xabc12345", "user2")    # No-code -> NOCODE_UID (68)
        db.upsert_connection("test", 789, f"bitcast-hk:{hotkey2}", "user3")  # Valid hotkey -> UID 1
        db.upsert_connection("test", 111, "bitcast-hk:InvalidKey", "user4")  # Invalid hotkey -> None
        
        mock_metagraph = MagicMock()
        mock_metagraph.hotkeys = [hotkey1, hotkey2]
        
        accounts = db.get_accounts_with_uids("test", mock_metagraph)
        
        # Verify all tag types mapped correctly and sorted by UID (None last)
        assert len(accounts) == 4
        assert accounts[0] == {'account_username': 'user1', 'uid': 0}
        assert accounts[1] == {'account_username': 'user3', 'uid': 1}
        assert accounts[2] == {'account_username': 'user2', 'uid': NOCODE_UID}
        assert accounts[3] == {'account_username': 'user4', 'uid': None}

    def test_get_accounts_with_uids_deduplication(self, temp_db):
        """Test that only the most recent connection per account is returned."""
        import time
        
        db = ConnectionDatabase(db_path=temp_db)
        
        hotkey1 = "5DNmHotkey1"
        hotkey2 = "5DNmHotkey2"
        
        # user1 posts first tag
        db.upsert_connection("test", 111, f"bitcast-hk:{hotkey1}", "user1")
        
        # Small delay to ensure different timestamps
        time.sleep(0.01)
        
        # user1 posts second tag (more recent) - this should be the one returned
        db.upsert_connection("test", 222, f"bitcast-hk:{hotkey2}", "user1")
        
        # Mock metagraph
        mock_metagraph = MagicMock()
        mock_metagraph.hotkeys = [hotkey1, hotkey2]  # UIDs: 0, 1
        
        accounts = db.get_accounts_with_uids("test", mock_metagraph)
        
        # Should only return one entry for user1 (the most recent = hotkey2 = UID 1)
        assert len(accounts) == 1
        assert accounts[0] == {'account_username': 'user1', 'uid': 1}

