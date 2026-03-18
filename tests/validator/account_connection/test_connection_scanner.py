"""
Tests for ConnectionScanner (reply-based).

Uses isolated temporary databases to ensure production database is never touched.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
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
    """Test reply-based connection scanner."""

    @pytest.fixture
    def temp_db_path(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)
        yield db_path
        if db_path.exists():
            db_path.unlink()

    @pytest.fixture
    def mock_twitter_client(self):
        return Mock()

    def test_scanner_initialization(self, temp_db_path, mock_twitter_client):
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['111', '222']
        )

        assert scanner.twitter_client is not None
        assert scanner.database is not None
        assert scanner.tag_parser is not None
        assert scanner.tweet_ids == ['111', '222']

    def test_scanner_defaults_to_config(self, temp_db_path, mock_twitter_client):
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
        )
        assert isinstance(scanner.tweet_ids, list)

    def test_extract_connections_from_tweets(self, temp_db_path, mock_twitter_client):
        """Test extracting tags from replies filtered by social map."""
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['999']
        )

        pool_accounts = {'alice', 'bob', 'charlie'}

        tweets = [
            {
                'tweet_id': '111',
                'author': 'alice',
                'text': 'bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq',
            },
            {
                'tweet_id': '222',
                'author': 'bob',
                'text': 'bitcast-xabc123',
            },
            {
                'tweet_id': '333',
                'author': 'unknown_user',
                'text': 'bitcast-hk:5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY',
            },
            {
                'tweet_id': '444',
                'author': 'charlie',
                'text': 'no tags here just chatting',
            },
        ]

        connections = scanner._extract_connections_from_tweets(tweets, pool_accounts)

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
            twitter_client=mock_twitter_client,
            tweet_ids=['999']
        )

        tweets = [
            {
                'tweet_id': '111',
                'author': 'alice',
                'text': 'bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq',
                'retweeted_user': 'someone',
            },
        ]

        connections = scanner._extract_connections_from_tweets(tweets, {'alice'})
        assert len(connections) == 0

    def test_extract_connections_multiple_tags_in_tweet(self, temp_db_path, mock_twitter_client):
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['999']
        )

        tweets = [
            {
                'tweet_id': '111',
                'author': 'alice',
                'text': 'bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq bitcast-xabc123',
            },
        ]

        connections = scanner._extract_connections_from_tweets(tweets, {'alice'})
        assert len(connections) == 2

    @patch('bitcast.validator.account_connection.connection_scanner.get_social_map_accounts')
    @pytest.mark.asyncio
    async def test_scan_pool(self, mock_get_accounts, temp_db_path, mock_twitter_client):
        """Test full pool scan flow with replies."""
        mock_get_accounts.return_value = {'alice', 'bob'}

        mock_twitter_client.fetch_post_replies.return_value = {
            'tweets': [
                {
                    'tweet_id': '111',
                    'author': 'alice',
                    'text': 'bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq',
                    'in_reply_to_status_id': '9999',
                },
                {
                    'tweet_id': '222',
                    'author': 'bob',
                    'text': 'bitcast-xtest123',
                    'in_reply_to_status_id': '9999',
                },
            ],
            'api_succeeded': True,
        }

        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['9999']
        )

        stats = await scanner.scan_pool('tao', publish=False)

        assert stats['tags_found'] == 2
        assert stats['new_connections'] == 2
        assert stats['tweets_scanned'] == 2

        mock_twitter_client.fetch_post_replies.assert_called_once_with('9999')

    @patch('bitcast.validator.account_connection.connection_scanner.get_social_map_accounts')
    @pytest.mark.asyncio
    async def test_scan_pool_multiple_tweet_ids(self, mock_get_accounts, temp_db_path, mock_twitter_client):
        """Test scanning replies from multiple designated tweets."""
        mock_get_accounts.return_value = {'alice', 'bob'}

        mock_twitter_client.fetch_post_replies.side_effect = [
            {
                'tweets': [
                    {'tweet_id': '111', 'author': 'alice', 'text': 'bitcast-xabc', 'in_reply_to_status_id': '8888'},
                ],
                'api_succeeded': True,
            },
            {
                'tweets': [
                    {'tweet_id': '222', 'author': 'bob', 'text': 'bitcast-xdef', 'in_reply_to_status_id': '9999'},
                ],
                'api_succeeded': True,
            },
        ]

        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['8888', '9999']
        )

        stats = await scanner.scan_pool('tao', publish=False)

        assert stats['tags_found'] == 2
        assert stats['new_connections'] == 2
        assert mock_twitter_client.fetch_post_replies.call_count == 2

    @patch('bitcast.validator.account_connection.connection_scanner.get_social_map_accounts')
    @pytest.mark.asyncio
    async def test_scan_pool_no_results(self, mock_get_accounts, temp_db_path, mock_twitter_client):
        mock_get_accounts.return_value = {'alice'}

        mock_twitter_client.fetch_post_replies.return_value = {
            'tweets': [],
            'api_succeeded': True,
        }

        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['9999']
        )

        stats = await scanner.scan_pool('tao', publish=False)

        assert stats['tags_found'] == 0
        assert stats['new_connections'] == 0

    @patch('bitcast.validator.account_connection.connection_scanner.get_social_map_accounts')
    @pytest.mark.asyncio
    async def test_scan_pool_api_failure(self, mock_get_accounts, temp_db_path, mock_twitter_client):
        mock_get_accounts.return_value = {'alice'}

        mock_twitter_client.fetch_post_replies.return_value = {
            'tweets': [],
            'api_succeeded': False,
        }

        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['9999']
        )

        stats = await scanner.scan_pool('tao', publish=False)
        assert stats['tags_found'] == 0

    @patch('bitcast.validator.account_connection.connection_scanner.get_social_map_accounts')
    @pytest.mark.asyncio
    async def test_scan_pool_no_tweet_ids(self, mock_get_accounts, temp_db_path, mock_twitter_client):
        """Scanner gracefully handles empty tweet ID list."""
        mock_get_accounts.return_value = {'alice'}

        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=[]
        )

        stats = await scanner.scan_pool('tao', publish=False)
        assert stats['tweets_scanned'] == 0
        assert stats['tags_found'] == 0
        mock_twitter_client.fetch_post_replies.assert_not_called()

    def test_store_connection_new(self, mock_twitter_client):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)

        try:
            scanner = ConnectionScanner(
                db_path=db_path,
                twitter_client=mock_twitter_client,
                tweet_ids=['999']
            )

            is_new = scanner.database.upsert_connection(
                pool_name="test",
                tweet_id="123456789",
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

    def test_extract_connections_stitch3_tags(self, temp_db_path, mock_twitter_client):
        """Test extracting Stitch3 format tags from replies."""
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['999']
        )

        pool_accounts = {'alice', 'bob'}

        tweets = [
            {
                'tweet_id': '111',
                'author': 'alice',
                'text': 'Stitch-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq',
            },
            {
                'tweet_id': '222',
                'author': 'bob',
                'text': 'Stitch3-abc123',
            },
        ]

        connections = scanner._extract_connections_from_tweets(tweets, pool_accounts)

        assert len(connections) == 2
        alice_conn = next(c for c in connections if c['username'] == 'alice')
        assert alice_conn['tag'] == 'Stitch-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq'

        bob_conn = next(c for c in connections if c['username'] == 'bob')
        assert bob_conn['tag'] == 'Stitch3-abc123'

    def test_store_connection_duplicate(self, mock_twitter_client):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)

        try:
            scanner = ConnectionScanner(
                db_path=db_path,
                twitter_client=mock_twitter_client,
                tweet_ids=['999']
            )

            is_new1 = scanner.database.upsert_connection(
                pool_name="test",
                tweet_id="123456789",
                tag="bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq",
                account_username="testuser"
            )

            is_new2 = scanner.database.upsert_connection(
                pool_name="test",
                tweet_id="987654321",
                tag="bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq",
                account_username="testuser"
            )

            assert is_new1 is True
            assert is_new2 is False
            assert scanner.database.get_connection_count() == 1
        finally:
            if db_path.exists():
                db_path.unlink()
