"""Tests for tweet_discovery module with accumulative TweetStore."""

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, patch, MagicMock

from bitcast.validator.tweet_scoring.tweet_discovery import (
    TweetDiscovery,
    build_search_query,
    refresh_connected_timelines,
)


class TestBuildSearchQuery:
    """Test build_search_query function."""
    
    def test_tag_only(self):
        query = build_search_query(tag="#bitcoin")
        assert query == "#bitcoin"
    
    def test_tag_with_dates(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, tzinfo=timezone.utc)
        query = build_search_query(tag="#bitcoin", since_date=start, until_date=end)
        # until_date gets +1 day because X search 'until:' is exclusive
        assert query == "#bitcoin since:2024-01-01 until:2024-01-16"
    
    def test_quoted_tweet_id_only(self):
        query = build_search_query(quoted_tweet_id="123456789")
        assert query == "quoted_tweet_id:123456789"
    
    def test_quoted_tweet_id_with_dates(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, tzinfo=timezone.utc)
        query = build_search_query(
            quoted_tweet_id="123456789",
            since_date=start,
            until_date=end
        )
        assert query == "quoted_tweet_id:123456789 since:2024-01-01 until:2024-01-16"
    
    def test_tag_and_quoted_tweet_id(self):
        query = build_search_query(tag="#bitcoin", quoted_tweet_id="123456789")
        assert query == "#bitcoin quoted_tweet_id:123456789"
    
    def test_empty_query(self):
        query = build_search_query()
        assert query == ""


class TestTweetDiscoveryInit:
    """Test TweetDiscovery initialization."""
    
    def test_init_with_active_accounts(self):
        mock_client = Mock()
        active_accounts = {"user1", "user2", "USER3"}
        
        discovery = TweetDiscovery(
            client=mock_client,
            active_accounts=active_accounts
        )
        
        assert discovery.active_accounts == {"user1", "user2", "user3"}
        assert "user1" in discovery.considered_accounts
        assert "user3" in discovery.considered_accounts
    
    def test_init_with_considered_accounts(self):
        mock_client = Mock()
        active_accounts = {"user1", "user2"}
        considered_accounts = {"user1": 0.5, "user3": 0.8, "USER4": 0.3}
        
        discovery = TweetDiscovery(
            client=mock_client,
            active_accounts=active_accounts,
            considered_accounts=considered_accounts
        )
        
        assert discovery.active_accounts == {"user1", "user2"}
        assert discovery.considered_accounts["user1"] == 0.5
        assert discovery.considered_accounts["user3"] == 0.8
        assert discovery.considered_accounts["user4"] == 0.3


class TestTweetDiscoveryDiscover:
    """Test TweetDiscovery discover methods using TweetStore."""
    
    def setup_method(self):
        self.mock_client = Mock()
        self.mock_store = MagicMock()
        self.active_accounts = {"alice", "bob", "charlie"}
        
        with patch('bitcast.validator.tweet_scoring.tweet_discovery.TweetStore') as mock_store_cls:
            mock_store_cls.get_instance.return_value = self.mock_store
            self.discovery = TweetDiscovery(
                client=self.mock_client,
                active_accounts=self.active_accounts
            )
    
    def test_discover_tweets_by_qrt(self):
        """Test discovering QRT tweets: API calls + store merge + store query."""
        # Mock single-sort API response (latest only)
        self.mock_client.search_tweets.return_value = {
            'tweets': [
                {'tweet_id': '1', 'author': 'alice', 'text': 'QRT1', 'quoted_tweet_id': '999'},
                {'tweet_id': '2', 'author': 'bob', 'text': 'QRT2', 'quoted_tweet_id': '999'},
                {'tweet_id': '3', 'author': 'charlie', 'text': 'QRT3', 'quoted_tweet_id': '999'},
            ],
            'api_succeeded': True
        }
        
        self.mock_store.store_tweets.return_value = {'new': 3, 'updated': 0}
        
        # Store query returns accumulated tweets (including previously found ones)
        self.mock_store.query_tweets.return_value = [
            {'tweet_id': '1', 'author': 'alice'},
            {'tweet_id': '2', 'author': 'bob'},
            {'tweet_id': '3', 'author': 'charlie'},
            {'tweet_id': '4', 'author': 'alice'},  # Previously found, not in this API call
        ]
        
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, tzinfo=timezone.utc)
        
        result = self.discovery.discover_tweets(
            tag=None, qrt='999', start_date=start, end_date=end
        )
        
        # Should return all tweets from store (including previously found)
        assert len(result) == 4
        
        # Should have called search_tweets once (latest sort only)
        assert self.mock_client.search_tweets.call_count == 1
        
        # Should have stored the 3 tweets
        self.mock_store.store_tweets.assert_called_once()
        stored_tweets = self.mock_store.store_tweets.call_args[0][0]
        assert len(stored_tweets) == 3
        
        # Should have queried store
        self.mock_store.query_tweets.assert_called_once()
    
    def test_discover_always_makes_api_calls(self):
        """Test that API calls are always made (no cache short-circuit)."""
        self.mock_client.search_tweets.return_value = {
            'tweets': [], 
            'api_succeeded': True
        }
        self.mock_store.store_tweets.return_value = {'new': 0, 'updated': 0}
        self.mock_store.query_tweets.return_value = [
            {'tweet_id': 'old1', 'author': 'alice'}  # Previously found
        ]
        
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, tzinfo=timezone.utc)
        
        result = self.discovery.discover_tweets(
            tag=None, qrt='999', start_date=start, end_date=end
        )
        
        # Even though API returned nothing, store has old data
        assert len(result) == 1
        assert result[0]['tweet_id'] == 'old1'
        
        # API was still called (always fresh calls, single sort)
        assert self.mock_client.search_tweets.call_count == 1
    
    def test_discover_tweets_requires_tag_or_qrt(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, tzinfo=timezone.utc)
        
        with pytest.raises(ValueError, match="At least one of 'tag' or 'qrt'"):
            self.discovery.discover_tweets(
                tag=None, qrt=None, start_date=start, end_date=end
            )


class TestTimelineDiscovery:
    """Test TweetDiscovery.discover_tweets_from_timelines() (cache-read mode)."""

    def setup_method(self):
        self.mock_client = Mock()
        self.mock_store = MagicMock()
        self.active_accounts = {"alice", "bob"}

        with patch('bitcast.validator.tweet_scoring.tweet_discovery.TweetStore') as mock_store_cls:
            mock_store_cls.get_instance.return_value = self.mock_store
            self.discovery = TweetDiscovery(
                client=self.mock_client,
                active_accounts=self.active_accounts
            )

    @patch('bitcast.validator.tweet_scoring.tweet_discovery.get_cached_user_tweets')
    def test_reads_from_cache_not_api(self, mock_get_cached):
        """Timeline discovery reads from DiscoveryCache, makes zero API calls."""
        mock_get_cached.side_effect = lambda u: {
            'alice': {'tweets': [
                {'tweet_id': '1', 'author': 'alice', 'text': 'Hello #test',
                 'created_at': 'Mon Jan 01 12:00:00 +0000 2024'},
            ]},
            'bob': {'tweets': [
                {'tweet_id': '2', 'author': 'bob', 'text': 'World #test',
                 'created_at': 'Mon Jan 01 13:00:00 +0000 2024'},
            ]},
        }.get(u)

        self.mock_store.store_tweets.return_value = {'new': 2, 'updated': 0}
        self.mock_store.query_tweets.return_value = [
            {'tweet_id': '1', 'author': 'alice'},
            {'tweet_id': '2', 'author': 'bob'},
        ]

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, tzinfo=timezone.utc)

        result = self.discovery.discover_tweets_from_timelines(
            tag='#test', qrt=None, start_date=start, end_date=end
        )

        assert mock_get_cached.call_count == 2
        self.mock_store.store_tweets.assert_called_once()
        self.mock_store.query_tweets.assert_called_once()
        assert len(result) == 2

    @patch('bitcast.validator.tweet_scoring.tweet_discovery.get_cached_user_tweets')
    def test_handles_empty_cache(self, mock_get_cached):
        """Timeline discovery handles accounts with no cached data."""
        mock_get_cached.return_value = None

        self.mock_store.query_tweets.return_value = []

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, tzinfo=timezone.utc)

        result = self.discovery.discover_tweets_from_timelines(
            tag='#test', qrt=None, start_date=start, end_date=end
        )

        self.mock_store.store_tweets.assert_not_called()
        assert len(result) == 0

    def test_requires_tag_or_qrt(self):
        """Timeline discovery requires at least one of tag or qrt."""
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, tzinfo=timezone.utc)

        with pytest.raises(ValueError, match="At least one of 'tag' or 'qrt'"):
            self.discovery.discover_tweets_from_timelines(
                tag=None, qrt=None, start_date=start, end_date=end
            )

    @patch('bitcast.validator.tweet_scoring.tweet_discovery.get_cached_user_tweets')
    def test_accumulates_with_search_results(self, mock_get_cached):
        """Timeline discovery results accumulate in TweetStore alongside search results."""
        mock_get_cached.side_effect = lambda u: {
            'alice': {'tweets': [
                {'tweet_id': '1', 'author': 'alice', 'text': 'timeline tweet',
                 'created_at': 'Mon Jan 01 12:00:00 +0000 2024'},
            ]},
            'bob': {'tweets': []},
        }.get(u)

        self.mock_store.store_tweets.return_value = {'new': 1, 'updated': 0}
        self.mock_store.query_tweets.return_value = [
            {'tweet_id': '1', 'author': 'alice'},
            {'tweet_id': '2', 'author': 'bob'},
        ]

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 15, tzinfo=timezone.utc)

        result = self.discovery.discover_tweets_from_timelines(
            tag='#test', qrt=None, start_date=start, end_date=end
        )

        assert len(result) == 2


class TestRefreshConnectedTimelines:
    """Test refresh_connected_timelines() standalone function."""

    @patch('bitcast.validator.tweet_scoring.tweet_discovery.TwitterClient')
    def test_refreshes_all_accounts(self, MockClient):
        """Refreshes timelines for all connected accounts."""
        mock_client = Mock()
        MockClient.return_value = mock_client

        mock_client.fetch_user_tweets.side_effect = [
            {'tweets': [{'tweet_id': '1'}], 'cache_info': {'cache_fresh': False, 'new_tweets': 3}},
            {'tweets': [{'tweet_id': '2'}], 'cache_info': {'cache_fresh': False, 'new_tweets': 1}},
        ]

        stats = refresh_connected_timelines({"alice", "bob"}, max_workers=1)

        MockClient.assert_called_once_with(posts_only=True)
        assert mock_client.fetch_user_tweets.call_count == 2
        # Check skip_if_cache_fresh=True was passed
        for call in mock_client.fetch_user_tweets.call_args_list:
            assert call[1].get('skip_if_cache_fresh') is True
        assert stats['accounts_total'] == 2
        assert stats['refreshed'] == 2
        assert stats['new_tweets'] == 4

    @patch('bitcast.validator.tweet_scoring.tweet_discovery.TwitterClient')
    def test_counts_cache_hits(self, MockClient):
        """Tracks cache hits separately from refreshes."""
        mock_client = Mock()
        MockClient.return_value = mock_client

        mock_client.fetch_user_tweets.side_effect = [
            {'tweets': [], 'cache_info': {'cache_fresh': True, 'new_tweets': 0}},
            {'tweets': [{'tweet_id': '1'}], 'cache_info': {'cache_fresh': False, 'new_tweets': 5}},
        ]

        stats = refresh_connected_timelines({"alice", "bob"}, max_workers=1)

        assert stats['cache_hits'] == 1
        assert stats['refreshed'] == 1
        assert stats['new_tweets'] == 5

    @patch('bitcast.validator.tweet_scoring.tweet_discovery.TwitterClient')
    def test_handles_failures_gracefully(self, MockClient):
        """Individual account failures are counted but don't stop processing."""
        mock_client = Mock()
        MockClient.return_value = mock_client

        mock_client.fetch_user_tweets.side_effect = [
            {'tweets': [], 'cache_info': {'cache_fresh': False, 'new_tweets': 0}},
            Exception("API error"),
        ]

        stats = refresh_connected_timelines({"alice", "bob"}, max_workers=1)

        assert stats['refreshed'] == 1
        assert stats['failed'] == 1

    @patch('bitcast.validator.tweet_scoring.tweet_discovery.TwitterClient')
    def test_empty_accounts_set(self, MockClient):
        """Handles empty set of accounts."""
        mock_client = Mock()
        MockClient.return_value = mock_client

        stats = refresh_connected_timelines(set(), max_workers=1)

        assert stats['accounts_total'] == 0
        mock_client.fetch_user_tweets.assert_not_called()


class TestTweetDiscoveryEngagements:
    """Test TweetDiscovery engagement retrieval with accumulative store."""
    
    def setup_method(self):
        self.mock_client = Mock()
        self.mock_store = MagicMock()
        self.active_accounts = {"alice", "bob"}
        self.considered_accounts = {"alice": 0.5, "bob": 0.4, "influencer": 0.9}
        
        with patch('bitcast.validator.tweet_scoring.tweet_discovery.TweetStore') as mock_store_cls:
            mock_store_cls.get_instance.return_value = self.mock_store
            self.discovery = TweetDiscovery(
                client=self.mock_client,
                active_accounts=self.active_accounts,
                considered_accounts=self.considered_accounts
            )
    
    def test_get_engagements_for_tweet(self):
        """Test engagement retrieval: fetch from API, store, return from store."""
        # Mock retweeters API
        self.mock_client.get_retweeters.return_value = {
            'retweeters': ['influencer', 'random_user'],
            'api_succeeded': True
        }
        
        # Mock QRT search
        self.mock_client.search_tweets.return_value = {
            'tweets': [
                {'tweet_id': 'qrt1', 'author': 'bob', 'text': 'Quote tweet'},
            ],
            'api_succeeded': True
        }
        
        # Mock store returns accumulated engagements
        self.mock_store.get_engagements.return_value = {
            'tweet_id': '123456',
            'retweeters': {
                'influencer': {'first_seen': '2024-01-01'},
                'random_user': {'first_seen': '2024-01-01'},
            },
            'quoters': {
                'bob': {'quote_tweet_id': 'qrt1', 'first_seen': '2024-01-01'},
            }
        }
        
        tweet = {'tweet_id': '123456', 'author': 'alice'}
        engagements = self.discovery.get_engagements_for_tweet(tweet)
        
        # Should have retweet from influencer (considered account)
        assert 'influencer' in engagements
        assert engagements['influencer'] == 'retweet'
        
        # Should have quote from bob (considered account, quote > retweet)
        assert 'bob' in engagements
        assert engagements['bob'] == 'quote'
        
        # random_user not in considered accounts
        assert 'random_user' not in engagements
        
        # Should have stored retweeters and quoters
        self.mock_store.store_retweeters.assert_called_once_with('123456', ['influencer', 'random_user'])
        self.mock_store.store_quoters.assert_called_once()
    
    def test_get_engagements_excludes_self(self):
        """Test that self-engagement is excluded."""
        self.mock_client.get_retweeters.return_value = {
            'retweeters': ['alice'], 'api_succeeded': True
        }
        self.mock_client.search_tweets.return_value = {
            'tweets': [], 'api_succeeded': True
        }
        
        self.mock_store.get_engagements.return_value = {
            'tweet_id': '123456',
            'retweeters': {'alice': {'first_seen': '2024-01-01'}},
            'quoters': {}
        }
        
        tweet = {'tweet_id': '123456', 'author': 'alice'}
        engagements = self.discovery.get_engagements_for_tweet(tweet)
        
        assert 'alice' not in engagements
    
    def test_get_engagements_excludes_specified_accounts(self):
        """Test that specified accounts are excluded."""
        self.mock_client.get_retweeters.return_value = {
            'retweeters': ['influencer', 'bob'], 'api_succeeded': True
        }
        self.mock_client.search_tweets.return_value = {
            'tweets': [], 'api_succeeded': True
        }
        
        self.mock_store.get_engagements.return_value = {
            'tweet_id': '123456',
            'retweeters': {
                'influencer': {'first_seen': '2024-01-01'},
                'bob': {'first_seen': '2024-01-01'}
            },
            'quoters': {}
        }
        
        tweet = {'tweet_id': '123456', 'author': 'alice'}
        engagements = self.discovery.get_engagements_for_tweet(
            tweet, excluded_engagers={'bob'}
        )
        
        assert 'influencer' in engagements
        assert 'bob' not in engagements
    
    def test_quote_takes_priority_over_retweet(self):
        """Test that quote engagement takes priority over retweet."""
        self.mock_client.get_retweeters.return_value = {
            'retweeters': ['influencer'], 'api_succeeded': True
        }
        self.mock_client.search_tweets.return_value = {
            'tweets': [{'tweet_id': 'qrt1', 'author': 'influencer'}],
            'api_succeeded': True
        }
        
        # Store has both RT and QRT from influencer
        self.mock_store.get_engagements.return_value = {
            'tweet_id': '123456',
            'retweeters': {'influencer': {'first_seen': '2024-01-01'}},
            'quoters': {'influencer': {'quote_tweet_id': 'qrt1', 'first_seen': '2024-01-01'}}
        }
        
        tweet = {'tweet_id': '123456', 'author': 'alice'}
        engagements = self.discovery.get_engagements_for_tweet(tweet)
        
        assert engagements['influencer'] == 'quote'
    
    def test_accumulative_engagements(self):
        """Test that store returns engagements found in previous runs."""
        # API returns nothing this run
        self.mock_client.get_retweeters.return_value = {
            'retweeters': [], 'api_succeeded': True
        }
        self.mock_client.search_tweets.return_value = {
            'tweets': [], 'api_succeeded': True
        }
        
        # But store has data from previous runs
        self.mock_store.get_engagements.return_value = {
            'tweet_id': '123456',
            'retweeters': {'influencer': {'first_seen': '2024-01-01'}},
            'quoters': {'bob': {'quote_tweet_id': 'qrt1', 'first_seen': '2024-01-01'}}
        }
        
        tweet = {'tweet_id': '123456', 'author': 'alice'}
        engagements = self.discovery.get_engagements_for_tweet(tweet)
        
        # Should still find engagements from previous runs
        assert 'influencer' in engagements
        assert engagements['influencer'] == 'retweet'
        assert 'bob' in engagements
        assert engagements['bob'] == 'quote'


class TestBriefValidation:
    """Test Brief model validation for tag/qrt requirement."""
    
    def test_brief_requires_tag_or_qrt(self):
        from bitcast.validator.reward_engine.models.brief import Brief
        
        with pytest.raises(ValueError, match="must specify either 'tag' or 'qrt'"):
            Brief(
                id="test-brief",
                pool="prediction_markets",
                budget=100.0,
                start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
                brief_text="Test brief",
                tag=None,
                qrt=None
            )
    
    def test_brief_accepts_tag_only(self):
        from bitcast.validator.reward_engine.models.brief import Brief
        
        brief = Brief(
            id="test-brief",
            pool="prediction_markets",
            budget=100.0,
            start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
            brief_text="Test brief",
            tag="#bitcoin"
        )
        assert brief.tag == "#bitcoin"
        assert brief.qrt is None
    
    def test_brief_accepts_qrt_only(self):
        from bitcast.validator.reward_engine.models.brief import Brief
        
        brief = Brief(
            id="test-brief",
            pool="prediction_markets",
            budget=100.0,
            start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
            brief_text="Test brief",
            qrt="123456789"
        )
        assert brief.tag is None
        assert brief.qrt == "123456789"
    
    def test_brief_accepts_both_tag_and_qrt(self):
        from bitcast.validator.reward_engine.models.brief import Brief
        
        brief = Brief(
            id="test-brief",
            pool="prediction_markets",
            budget=100.0,
            start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
            brief_text="Test brief",
            tag="#bitcoin",
            qrt="123456789"
        )
        assert brief.tag == "#bitcoin"
        assert brief.qrt == "123456789"
