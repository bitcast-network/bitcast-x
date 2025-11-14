"""
Integration tests for ConnectionScanner.

IMPORTANT: These tests use isolated temporary databases to ensure they
NEVER touch the production database at:
    bitcast/validator/account_connection/connections.db

All ConnectionScanner instances in tests are created with db_path parameter
pointing to temporary files that are cleaned up after each test.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta, timezone
from bitcast.validator.account_connection.connection_scanner import (
    ConnectionScanner,
    get_active_pool_members
)
from bitcast.validator.account_connection.connection_db import ConnectionDatabase


class TestGetActivePoolMembers:
    """Test social map integration."""
    
    def test_get_active_members_from_real_pool(self):
        """Test loading active members from existing tao pool."""
        try:
            # This will only work if social_discovery has been run
            active_members = get_active_pool_members("tao")
            assert isinstance(active_members, list)
            # Should have at least some members
            if active_members:
                assert all(isinstance(m, str) for m in active_members)
                assert all(m.islower() for m in active_members)  # All lowercase
        except ValueError as e:
            # Expected if no social map exists yet
            assert "No social map" in str(e) or "not found" in str(e)
    
    def test_invalid_pool_name(self):
        """Test error handling for invalid pool."""
        with pytest.raises(ValueError) as exc_info:
            get_active_pool_members("nonexistent_pool")
        assert "not found" in str(exc_info.value).lower()
    
    def test_pool_name_case_insensitive(self):
        """Test that pool names are case-insensitive."""
        try:
            members1 = get_active_pool_members("tao")
            members2 = get_active_pool_members("TAO")
            members3 = get_active_pool_members("Tao")
            # If social map exists, all should return same results
            assert members1 == members2 == members3
        except ValueError:
            # Expected if no social map exists
            pass


class TestConnectionScanner:
    """Test connection scanner functionality."""
    
    @pytest.fixture
    def temp_db_path(self):
        """Provide a temporary database path for tests."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)
        yield db_path
        # Cleanup
        if db_path.exists():
            db_path.unlink()
    
    def test_scanner_initialization(self, temp_db_path):
        """Test scanner can be initialized."""
        scanner = ConnectionScanner(lookback_days=7, db_path=temp_db_path)
        
        assert scanner.lookback_days == 7
        assert scanner.twitter_client is not None
        assert scanner.database is not None
        assert scanner.tag_parser is not None
    
    def test_filter_recent_tweets_empty(self, temp_db_path):
        """Test filtering with no tweets."""
        scanner = ConnectionScanner(db_path=temp_db_path)
        result = scanner.filter_recent_tweets([])
        assert result == []
    
    def test_filter_recent_tweets_within_range(self, temp_db_path):
        """Test filtering keeps recent tweets."""
        scanner = ConnectionScanner(lookback_days=7, db_path=temp_db_path)
        
        # Create tweet from 3 days ago
        recent_date = datetime.now(timezone.utc) - timedelta(days=3)
        tweets = [
            {
                'tweet_id': 123,
                'text': 'Test tweet',
                'created_at': recent_date.isoformat()
            }
        ]
        
        result = scanner.filter_recent_tweets(tweets)
        assert len(result) == 1
        assert result[0]['tweet_id'] == 123
    
    def test_filter_recent_tweets_outside_range(self, temp_db_path):
        """Test filtering removes old tweets."""
        scanner = ConnectionScanner(lookback_days=7, db_path=temp_db_path)
        
        # Create tweet from 10 days ago (outside range)
        old_date = datetime.now(timezone.utc) - timedelta(days=10)
        tweets = [
            {
                'tweet_id': 123,
                'text': 'Old tweet',
                'created_at': old_date.isoformat()
            }
        ]
        
        result = scanner.filter_recent_tweets(tweets)
        assert len(result) == 0
    
    def test_filter_recent_tweets_mixed(self, temp_db_path):
        """Test filtering with mix of recent and old tweets."""
        scanner = ConnectionScanner(lookback_days=7, db_path=temp_db_path)
        
        recent_date = datetime.now(timezone.utc) - timedelta(days=3)
        old_date = datetime.now(timezone.utc) - timedelta(days=10)
        
        tweets = [
            {
                'tweet_id': 1,
                'text': 'Recent tweet',
                'created_at': recent_date.isoformat()
            },
            {
                'tweet_id': 2,
                'text': 'Old tweet',
                'created_at': old_date.isoformat()
            },
            {
                'tweet_id': 3,
                'text': 'Another recent tweet',
                'created_at': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            }
        ]
        
        result = scanner.filter_recent_tweets(tweets)
        assert len(result) == 2
        assert result[0]['tweet_id'] == 1
        assert result[1]['tweet_id'] == 3
    
    @patch('bitcast.validator.account_connection.connection_scanner.TwitterClient')
    def test_scan_account_with_tags(self, mock_twitter_client, temp_db_path):
        """Test scanning account that has tags in tweets."""
        # Setup mock
        mock_client_instance = Mock()
        mock_twitter_client.return_value = mock_client_instance
        
        recent_date = datetime.now(timezone.utc) - timedelta(days=1)
        mock_client_instance.fetch_user_tweets.return_value = {
            'tweets': [
                {
                    'tweet_id': 123456789,
                    'text': 'Check out bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq for more info',
                    'created_at': recent_date.isoformat()
                }
            ]
        }
        
        scanner = ConnectionScanner(db_path=temp_db_path)
        scanner.twitter_client = mock_client_instance
        
        found_tags = scanner.scan_account("testuser")
        
        assert len(found_tags) == 1
        assert found_tags[0][0] == 123456789  # tweet_id
        assert found_tags[0][1] == "HK"  # tag_type
        assert found_tags[0][2] == "bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq"  # full_tag
    
    @patch('bitcast.validator.account_connection.connection_scanner.TwitterClient')
    def test_scan_account_no_tags(self, mock_twitter_client, temp_db_path):
        """Test scanning account with no tags."""
        # Setup mock
        mock_client_instance = Mock()
        mock_twitter_client.return_value = mock_client_instance
        
        recent_date = datetime.now(timezone.utc) - timedelta(days=1)
        mock_client_instance.fetch_user_tweets.return_value = {
            'tweets': [
                {
                    'tweet_id': 123456789,
                    'text': 'Just a regular tweet with no tags',
                    'created_at': recent_date.isoformat()
                }
            ]
        }
        
        scanner = ConnectionScanner(db_path=temp_db_path)
        scanner.twitter_client = mock_client_instance
        
        found_tags = scanner.scan_account("testuser")
        
        assert len(found_tags) == 0
    
    @patch('bitcast.validator.account_connection.connection_scanner.TwitterClient')
    def test_scan_account_api_error(self, mock_twitter_client, temp_db_path):
        """Test handling of API errors during account scan."""
        # Setup mock to raise exception
        mock_client_instance = Mock()
        mock_twitter_client.return_value = mock_client_instance
        mock_client_instance.fetch_user_tweets.side_effect = Exception("API Error")
        
        scanner = ConnectionScanner(db_path=temp_db_path)
        scanner.twitter_client = mock_client_instance
        
        # Should not raise, should return empty list
        found_tags = scanner.scan_account("testuser")
        assert found_tags == []
    
    @patch('bitcast.validator.account_connection.connection_scanner.TwitterClient')
    def test_scan_account_excludes_retweets(self, mock_twitter_client, temp_db_path):
        """Test that retweets are excluded from scanning."""
        # Setup mock
        mock_client_instance = Mock()
        mock_twitter_client.return_value = mock_client_instance
        
        recent_date = datetime.now(timezone.utc) - timedelta(days=1)
        mock_client_instance.fetch_user_tweets.return_value = {
            'tweets': [
                {
                    'tweet_id': 111,
                    'text': 'Original tweet with bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq',
                    'created_at': recent_date.isoformat(),
                    'retweeted_user': None  # Not a retweet
                },
                {
                    'tweet_id': 222,
                    'text': 'RT @someone: Check out bitcast-xxyz78900',
                    'created_at': recent_date.isoformat(),
                    'retweeted_user': 'someone'  # This is a retweet
                },
                {
                    'tweet_id': 333,
                    'text': 'Another original with bitcast-hk:5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
                    'created_at': recent_date.isoformat(),
                    'retweeted_user': None  # Not a retweet
                }
            ]
        }
        
        scanner = ConnectionScanner(db_path=temp_db_path)
        scanner.twitter_client = mock_client_instance
        
        found_tags = scanner.scan_account("testuser")
        
        # Should find 2 tags (from tweets 111 and 333), but not from the retweet (222)
        assert len(found_tags) == 2
        assert found_tags[0][0] == 111  # First original tweet
        assert found_tags[0][2] == "bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq"
        assert found_tags[1][0] == 333  # Second original tweet
        assert found_tags[1][2] == "bitcast-hk:5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
        
        # Verify retweet was NOT included
        retweet_ids = [tag[0] for tag in found_tags]
        assert 222 not in retweet_ids
    
    def test_store_connection_new(self):
        """Test storing a new connection."""
        # Use temporary database
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)
        
        try:
            scanner = ConnectionScanner(db_path=db_path)
            
            is_new = scanner.store_connection(
                pool_name="test",
                tweet_id=123456789,
                tag="bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq",
                account_username="testuser"
            )
            
            assert is_new is True
            
            # Verify it's in database
            connections = scanner.database.get_connections_by_account("testuser", pool_name="test")
            assert len(connections) == 1
            assert connections[0]['tag'] == "bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq"
        finally:
            if db_path.exists():
                db_path.unlink()
    
    def test_store_connection_duplicate(self):
        """Test updating an existing connection."""
        # Use temporary database
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)
        
        try:
            scanner = ConnectionScanner(db_path=db_path)
            
            # Store first time
            is_new1 = scanner.store_connection(
                pool_name="test",
                tweet_id=123456789,
                tag="bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq",
                account_username="testuser"
            )
            
            # Store again (should update)
            is_new2 = scanner.store_connection(
                pool_name="test",
                tweet_id=987654321,  # Different tweet
                tag="bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq",  # Same tag
                account_username="testuser"  # Same user
            )
            
            assert is_new1 is True
            assert is_new2 is False
            
            # Should still be only one connection
            assert scanner.database.get_connection_count() == 1
        finally:
            if db_path.exists():
                db_path.unlink()

