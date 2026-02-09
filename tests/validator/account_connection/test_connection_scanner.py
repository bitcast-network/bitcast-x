"""
Tests for ConnectionScanner (search-based).

Uses isolated temporary databases to ensure production database is never touched.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta, timezone
from bitcast.validator.account_connection.connection_scanner import (
    ConnectionScanner,
    get_social_map_accounts
)


class TestGetSocialMapAccounts:
    """Test social map loading."""
    
    def test_get_accounts_from_real_pool(self):
        """Test loading accounts from existing tao pool."""
        try:
            accounts = get_social_map_accounts("tao")
            assert isinstance(accounts, set)
            if accounts:
                assert all(isinstance(a, str) for a in accounts)
                assert all(a.islower() for a in accounts)
        except ValueError as e:
            assert "No social map" in str(e) or "not found" in str(e)
    
    def test_invalid_pool_name(self):
        with pytest.raises(ValueError):
            get_social_map_accounts("nonexistent_pool")
    
    def test_pool_name_case_insensitive(self):
        try:
            a1 = get_social_map_accounts("tao")
            a2 = get_social_map_accounts("TAO")
            assert a1 == a2
        except ValueError:
            pass


class TestConnectionScanner:
    """Test search-based connection scanner."""
    
    @pytest.fixture
    def temp_db_path(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)
        yield db_path
        if db_path.exists():
            db_path.unlink()
    
    @pytest.fixture
    def mock_twitter_client(self):
        """Provide a mock TwitterClient for tests."""
        return Mock()
    
    def test_scanner_initialization(self, temp_db_path, mock_twitter_client):
        scanner = ConnectionScanner(
            lookback_days=7, 
            db_path=temp_db_path,
            twitter_client=mock_twitter_client
        )
        
        assert scanner.lookback_days == 7
        assert scanner.twitter_client is not None
        assert scanner.database is not None
        assert scanner.tag_parser is not None
        assert scanner.search_tag == '@bitcast'
    
    def test_build_query(self, temp_db_path, mock_twitter_client):
        scanner = ConnectionScanner(
            lookback_days=7, 
            db_path=temp_db_path,
            twitter_client=mock_twitter_client
        )
        query = scanner._build_query()
        
        assert '@bitcast' in query
        assert 'since:' in query
    
    def test_extract_connections_from_tweets(self, temp_db_path, mock_twitter_client):
        """Test extracting tags from search results filtered by social map."""
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client
        )
        
        pool_accounts = {'alice', 'bob', 'charlie'}
        
        tweets = [
            {
                'tweet_id': '111',
                'author': 'alice',
                'text': '@bitcast bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq',
            },
            {
                'tweet_id': '222',
                'author': 'bob',
                'text': '@bitcast bitcast-xabc123',
            },
            {
                'tweet_id': '333',
                'author': 'unknown_user',  # Not in social map
                'text': '@bitcast bitcast-hk:5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
            },
            {
                'tweet_id': '444',
                'author': 'charlie',
                'text': '@bitcast no tags here just chatting',
            },
        ]
        
        connections = scanner._extract_connections_from_tweets(tweets, pool_accounts)
        
        # Should find 2 connections (alice and bob), not unknown_user or charlie (no tag)
        assert len(connections) == 2
        
        usernames = {c['username'] for c in connections}
        assert usernames == {'alice', 'bob'}
        
        alice_conn = next(c for c in connections if c['username'] == 'alice')
        assert alice_conn['tag'] == 'bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq'
        assert alice_conn['tweet_id'] == '111'
        
        bob_conn = next(c for c in connections if c['username'] == 'bob')
        assert bob_conn['tag'] == 'bitcast-xabc123'
    
    def test_extract_connections_skips_retweets(self, temp_db_path, mock_twitter_client):
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client
        )
        
        tweets = [
            {
                'tweet_id': '111',
                'author': 'alice',
                'text': '@bitcast bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq',
                'retweeted_user': 'someone',  # This is a retweet
            },
        ]
        
        connections = scanner._extract_connections_from_tweets(tweets, {'alice'})
        assert len(connections) == 0
    
    def test_extract_connections_multiple_tags_in_tweet(self, temp_db_path, mock_twitter_client):
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client
        )
        
        tweets = [
            {
                'tweet_id': '111',
                'author': 'alice',
                'text': '@bitcast bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq bitcast-xabc123',
            },
        ]
        
        connections = scanner._extract_connections_from_tweets(tweets, {'alice'})
        assert len(connections) == 2
    
    @patch('bitcast.validator.account_connection.connection_scanner.get_social_map_accounts')
    @pytest.mark.asyncio
    async def test_scan_pool(self, mock_get_accounts, temp_db_path, mock_twitter_client):
        """Test full pool scan flow."""
        mock_get_accounts.return_value = {'alice', 'bob'}
        
        # Single-sort search results (latest only)
        mock_twitter_client.search_tweets.return_value = {
            'tweets': [
                {
                    'tweet_id': '111',
                    'author': 'alice',
                    'text': '@bitcast bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq',
                },
                {
                    'tweet_id': '222',
                    'author': 'bob',
                    'text': '@bitcast bitcast-xtest123',
                },
            ],
            'api_succeeded': True,
        }
        
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client
        )
        
        stats = await scanner.scan_pool('tao', publish=False)
        
        assert stats['tags_found'] == 2
        assert stats['new_connections'] == 2
        assert stats['tweets_scanned'] == 2
        
        # Search called once (latest sort only)
        assert mock_twitter_client.search_tweets.call_count == 1
    
    @patch('bitcast.validator.account_connection.connection_scanner.get_social_map_accounts')
    @pytest.mark.asyncio
    async def test_scan_pool_no_results(self, mock_get_accounts, temp_db_path, mock_twitter_client):
        mock_get_accounts.return_value = {'alice'}
        
        mock_twitter_client.search_tweets.return_value = {
            'tweets': [],
            'api_succeeded': True,
        }
        
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client
        )
        
        stats = await scanner.scan_pool('tao', publish=False)
        
        assert stats['tags_found'] == 0
        assert stats['new_connections'] == 0
    
    @patch('bitcast.validator.account_connection.connection_scanner.get_social_map_accounts')
    @pytest.mark.asyncio
    async def test_scan_pool_api_failure(self, mock_get_accounts, temp_db_path, mock_twitter_client):
        mock_get_accounts.return_value = {'alice'}
        
        mock_twitter_client.search_tweets.return_value = {
            'tweets': [],
            'api_succeeded': False,
        }
        
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client
        )
        
        stats = await scanner.scan_pool('tao', publish=False)
        
        assert stats['tags_found'] == 0

    def test_store_connection_new(self, mock_twitter_client):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)
        
        try:
            scanner = ConnectionScanner(
                db_path=db_path,
                twitter_client=mock_twitter_client
            )
            
            is_new = scanner.database.upsert_connection(
                pool_name="test",
                tweet_id=123456789,
                tag="bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq",
                account_username="testuser"
            )
            
            assert is_new is True
            
            connections = scanner.database.get_connections_by_account("testuser", pool_name="test")
            assert len(connections) == 1
            assert connections[0]['tag'] == "bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq"
        finally:
            if db_path.exists():
                db_path.unlink()
    
    def test_store_connection_duplicate(self, mock_twitter_client):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)
        
        try:
            scanner = ConnectionScanner(
                db_path=db_path,
                twitter_client=mock_twitter_client
            )
            
            is_new1 = scanner.database.upsert_connection(
                pool_name="test",
                tweet_id=123456789,
                tag="bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq",
                account_username="testuser"
            )
            
            is_new2 = scanner.database.upsert_connection(
                pool_name="test",
                tweet_id=987654321,
                tag="bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq",
                account_username="testuser"
            )
            
            assert is_new1 is True
            assert is_new2 is False
            assert scanner.database.get_connection_count() == 1
        finally:
            if db_path.exists():
                db_path.unlink()
