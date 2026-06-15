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
    get_social_map_accounts,
)
from bitcast.validator.account_connection.referral_code import encode_referral_code


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

    @staticmethod
    def _patch_pools(scanner: ConnectionScanner, pool_map: dict) -> None:
        """Bypass social-map loading by injecting a {pool: {usernames}} map."""
        scanner._pool_accounts = {p: set(u) for p, u in pool_map.items()}

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
        self._patch_pools(scanner, {'tao': {'alice', 'bob', 'charlie'}})

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

        connections = scanner._extract_connections_from_tweets(tweets, scanner._all_known_accounts())

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
        self._patch_pools(scanner, {'tao': {'alice'}})

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
        self._patch_pools(scanner, {'tao': {'alice'}})

        tweets = [
            {
                'tweet_id': '111',
                'author': 'alice',
                'text': 'bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq bitcast-xabc123',
            },
        ]

        connections = scanner._extract_connections_from_tweets(tweets, {'alice'})
        assert len(connections) == 2

    @pytest.mark.asyncio
    async def test_scan_all_pools(self, temp_db_path, mock_twitter_client):
        """End-to-end pool-agnostic scan inserts one row per author."""
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
        self._patch_pools(scanner, {'tao': {'alice', 'bob'}})

        stats = await scanner.scan_all_pools()

        assert stats['tags_found'] == 2
        assert stats['new_connections'] == 2
        assert stats['tweets_scanned'] == 2
        mock_twitter_client.fetch_post_replies.assert_called_once_with('9999')

    @pytest.mark.asyncio
    async def test_scan_multiple_tweet_ids(self, temp_db_path, mock_twitter_client):
        """Test scanning replies from multiple designated tweets."""
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
        self._patch_pools(scanner, {'tao': {'alice', 'bob'}})

        stats = await scanner.scan_all_pools()

        assert stats['tags_found'] == 2
        assert stats['new_connections'] == 2
        assert mock_twitter_client.fetch_post_replies.call_count == 2

    @pytest.mark.asyncio
    async def test_scan_no_results(self, temp_db_path, mock_twitter_client):
        mock_twitter_client.fetch_post_replies.return_value = {
            'tweets': [],
            'api_succeeded': True,
        }

        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['9999']
        )
        self._patch_pools(scanner, {'tao': {'alice'}})

        stats = await scanner.scan_all_pools()

        assert stats['tags_found'] == 0
        assert stats['new_connections'] == 0

    @pytest.mark.asyncio
    async def test_scan_api_failure(self, temp_db_path, mock_twitter_client):
        mock_twitter_client.fetch_post_replies.return_value = {
            'tweets': [],
            'api_succeeded': False,
        }

        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['9999']
        )
        self._patch_pools(scanner, {'tao': {'alice'}})

        stats = await scanner.scan_all_pools()
        assert stats['tags_found'] == 0

    @pytest.mark.asyncio
    async def test_scan_no_tweet_ids(self, temp_db_path, mock_twitter_client):
        """Scanner gracefully handles empty tweet ID list."""
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=[]
        )
        self._patch_pools(scanner, {'tao': {'alice'}})

        stats = await scanner.scan_all_pools()
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
                tweet_id="123456789",
                tag="bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq",
                account_username="testuser",
            )

            assert is_new is True

            connections = scanner.database.get_connections_by_account("testuser")
            assert len(connections) == 1
            assert connections[0]['tag'] == "bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq"
        finally:
            if db_path.exists():
                db_path.unlink()

    @patch('bitcast.validator.account_connection.connection_scanner.PoolManager')
    @patch('bitcast.validator.account_connection.connection_scanner.load_latest_social_map')
    def test_process_tweet_locks_referral_amount(
        self, mock_load_social_map, mock_pool_manager_cls, temp_db_path, mock_twitter_client
    ):
        """Test that referral amount is locked from the pool social map on insert."""
        mock_load_social_map.return_value = (
            {
                'accounts': {
                    'alice': {'followers_count': 25_000, 'score': 1_000.0},
                    'referrer': {'followers_count': 1_000, 'score': 1.0},
                }
            },
            '/tmp/social_map.json',
        )
        mock_pool_manager_cls.return_value.get_pool.return_value = {'max_referral_amount': 100.0}

        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['999']
        )
        self._patch_pools(scanner, {'tao': {'alice', 'referrer'}})

        referral_code = encode_referral_code('referrer')
        stats = scanner.process_tweet(
            {
                'tweet_id': '111',
                'author': 'alice',
                'text': f'Stitch3-abc123-{referral_code}',
            }
        )

        assert stats['new_connections'] == 1
        connection = scanner.database.get_connections_by_account('alice')[0]
        assert connection['referred_by'] == 'referrer'
        assert connection['referee_amount'] == 100.0
        assert connection['referrer_amount'] == 100.0

    def test_process_tweet_is_idempotent(self, temp_db_path, mock_twitter_client):
        """Re-processing the same tweet must not report a touched row.

        Registration tweets stay in the fast-track queue across polls. Without
        idempotent reporting, every poll would republish the same connection.
        """
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['999'],
        )
        self._patch_pools(scanner, {'tao': {'alice'}})

        tweet = {'tweet_id': '111', 'author': 'alice', 'text': 'Stitch3-abc123'}

        first = scanner.process_tweet(tweet)
        assert first['new_connections'] == 1
        assert first['updated_connections'] == 0
        assert first['unchanged'] == 0

        second = scanner.process_tweet(tweet)
        assert second['new_connections'] == 0
        assert second['updated_connections'] == 0
        assert second['unchanged'] == 1

    def test_process_tweet_reports_update_on_new_tag(self, temp_db_path, mock_twitter_client):
        """A genuinely changed tag/tweet_id should report a touched row."""
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['999'],
        )
        self._patch_pools(scanner, {'tao': {'alice'}})

        scanner.process_tweet({'tweet_id': '111', 'author': 'alice', 'text': 'Stitch3-aaaaaaaa'})

        updated = scanner.process_tweet(
            {'tweet_id': '222', 'author': 'alice', 'text': 'Stitch3-bbbbbbbb'}
        )
        assert updated['new_connections'] == 0
        assert updated['updated_connections'] == 1
        assert updated['unchanged'] == 0

        connection = scanner.database.get_connections_by_account('alice')[0]
        assert connection['tag'] == 'Stitch3-bbbbbbbb'
        assert str(connection['tweet_id']) == '222'

    @patch('bitcast.validator.account_connection.connection_scanner.PoolManager')
    @patch('bitcast.validator.account_connection.connection_scanner.load_latest_social_map')
    def test_locked_referral_uses_max_across_pools(
        self, mock_load_social_map, mock_pool_manager_cls, temp_db_path, mock_twitter_client
    ):
        """If a user is in multiple pools, the highest referral amount across pools wins."""
        social_maps = {
            'low': {
                'accounts': {
                    'alice': {'followers_count': 25_000, 'score': 1_000.0},
                    'referrer': {'followers_count': 1, 'score': 1.0},
                }
            },
            'high': {
                'accounts': {
                    'alice': {'followers_count': 25_000, 'score': 1_000.0},
                    'referrer': {'followers_count': 1, 'score': 1.0},
                }
            },
        }
        mock_load_social_map.side_effect = lambda pool: (social_maps[pool], f'/tmp/{pool}.json')
        pool_configs = {'low': {'max_referral_amount': 50.0}, 'high': {'max_referral_amount': 100.0}}
        mock_pool_manager_cls.return_value.get_pool.side_effect = lambda p: pool_configs.get(p)

        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['999']
        )
        self._patch_pools(scanner, {'low': {'alice', 'referrer'}, 'high': {'alice', 'referrer'}})

        amount = scanner._compute_locked_referral_amount('alice', 'referrer')
        # alice maxes out both formulas; expect the higher pool cap to win
        assert amount == 100.0

    def test_extract_connections_stitch3_tags(self, temp_db_path, mock_twitter_client):
        """Test extracting Stitch3 format tags from replies."""
        scanner = ConnectionScanner(
            db_path=temp_db_path,
            twitter_client=mock_twitter_client,
            tweet_ids=['999']
        )
        self._patch_pools(scanner, {'tao': {'alice', 'bob'}})

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

        connections = scanner._extract_connections_from_tweets(tweets, {'alice', 'bob'})

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
                tweet_id="123456789",
                tag="bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq",
                account_username="testuser",
            )

            is_new2 = scanner.database.upsert_connection(
                tweet_id="987654321",
                tag="bitcast-hk:5DNmDymxKQZ5rTVkN1BLgSv2rRuUuhCpB8UL9LGNmGSJnzQq",
                account_username="testuser",
            )

            assert is_new1 is True
            assert is_new2 is False
            assert scanner.database.get_connection_count() == 1
        finally:
            if db_path.exists():
                db_path.unlink()
