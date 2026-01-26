"""
Tests for DesearchProvider.
"""

import pytest
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

from bitcast.validator.clients.desearch_provider import DesearchProvider


class TestDesearchProvider:
    """Tests for Desearch.ai provider implementation."""
    
    def test_init_basic(self):
        """Test provider initializes with API key."""
        provider = DesearchProvider(api_key="dt_$test_key_123")
        assert provider.api_key == "dt_$test_key_123"
        assert provider.base_url == "https://api.desearch.ai"
        assert "Authorization" in provider.headers
        assert provider.headers["Authorization"] == "dt_$test_key_123"
    
    def test_init_strips_whitespace(self):
        """Test provider strips whitespace from API key."""
        provider = DesearchProvider(api_key="  dt_$test_key_123  ")
        assert provider.api_key == "dt_$test_key_123"
    
    def test_init_with_config(self):
        """Test provider accepts configuration parameters."""
        provider = DesearchProvider(
            api_key="dt_$test",
            max_retries=5,
            retry_delay=3.0,
            rate_limit_delay=2.0
        )
        assert provider.max_retries == 5
        assert provider.retry_delay == 3.0
        assert provider.rate_limit_delay == 2.0
    
    def test_validate_api_key_valid(self):
        """Test API key validation with valid key."""
        provider = DesearchProvider(api_key="dt_$test123")
        assert provider.validate_api_key() is True
    
    def test_validate_api_key_invalid(self):
        """Test API key validation with invalid keys."""
        # Missing prefix
        provider = DesearchProvider(api_key="test123")
        assert provider.validate_api_key() is False
        
        # Partial prefix
        provider = DesearchProvider(api_key="$test123")
        assert provider.validate_api_key() is False
        
        # Empty key
        provider = DesearchProvider(api_key="")
        assert provider.validate_api_key() is False
    
    @mock.patch('requests.get')
    def test_make_api_request_success(self, mock_get):
        """Test successful API request."""
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'tweets': [], 'user': {}}
        mock_get.return_value = mock_response
        
        provider = DesearchProvider(api_key="dt_$test")
        data, error = provider._make_api_request("http://test", {})
        
        assert error is None
        assert data == {'tweets': [], 'user': {}}
    
    @mock.patch('requests.get')
    def test_make_api_request_retry_logic(self, mock_get):
        """Test API retry logic works."""
        # Mock rate limit then success
        mock_429 = mock.Mock()
        mock_429.status_code = 429
        
        mock_200 = mock.Mock()
        mock_200.status_code = 200
        mock_200.json.return_value = {'tweets': [], 'user': {}}
        
        mock_get.side_effect = [mock_429, mock_200]
        
        provider = DesearchProvider(api_key="dt_$test")
        
        with mock.patch('time.sleep'):
            data, error = provider._make_api_request("http://test", {})
        
        assert error is None
        assert mock_get.call_count == 2
    
    @mock.patch('requests.get')
    def test_make_api_request_timeout(self, mock_get):
        """Test API request timeout handling."""
        import requests
        mock_get.side_effect = requests.exceptions.Timeout()
        
        provider = DesearchProvider(api_key="dt_$test", max_retries=1)
        
        with mock.patch('time.sleep'):
            data, error = provider._make_api_request("http://test", {})
        
        assert data is None
        assert "timeout" in error.lower()
    
    @mock.patch('requests.get')
    def test_make_api_request_handles_response_formats(self, mock_get):
        """Test API request handles different response formats."""
        provider = DesearchProvider(api_key="dt_$test")
        
        # Format 1: {"tweets": [...], "user": {...}}
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'tweets': [], 'user': {}}
        mock_get.return_value = mock_response
        data, error = provider._make_api_request("http://test", {})
        assert error is None
        assert 'tweets' in data
        
        # Format 2: list of tweets (legacy)
        mock_response.json.return_value = []
        data, error = provider._make_api_request("http://test", {})
        assert error is None
        assert isinstance(data, list)
        
        # Format 3: {"data": [...]}
        mock_response.json.return_value = {'data': []}
        data, error = provider._make_api_request("http://test", {})
        assert error is None
        assert isinstance(data, list)
    
    def test_parse_tweet_basic(self):
        """Test basic Desearch.ai tweet parsing."""
        provider = DesearchProvider(api_key="dt_$test")
        
        # Mock Desearch.ai tweet response
        desearch_tweet = {
            'id': '1234567890',
            'text': 'Hello @user1 and @user2',
            'created_at': '2024-01-15T12:00:00Z',
            'like_count': 42,
            'retweet_count': 15,
            'reply_count': 8,
            'quote_count': 3,
            'bookmark_count': 5,
            'entities': {
                'user_mentions': [
                    {'screen_name': 'user1'},
                    {'screen_name': 'user2'}
                ]
            }
        }
        
        tweet = provider._parse_tweet(desearch_tweet, "testuser")
        
        assert tweet is not None
        assert tweet['tweet_id'] == '1234567890'
        assert tweet['text'] == 'Hello @user1 and @user2'
        assert tweet['author'] == 'testuser'
        assert tweet['tagged_accounts'] == ['user1', 'user2']
        assert tweet['favorite_count'] == 42
        assert tweet['retweet_count'] == 15
        assert tweet['reply_count'] == 8
        assert tweet['quote_count'] == 3
        assert tweet['bookmark_count'] == 5
    
    def test_parse_tweet_engagement_defaults(self):
        """Test engagement metrics default to 0 when missing."""
        provider = DesearchProvider(api_key="dt_$test")
        
        desearch_tweet = {
            'id': '1234567890',
            'text': 'Hello world',
            'created_at': '2024-01-15T12:00:00Z'
        }
        
        tweet = provider._parse_tweet(desearch_tweet, "testuser")
        
        assert tweet is not None
        assert tweet['favorite_count'] == 0
        assert tweet['retweet_count'] == 0
        assert tweet['reply_count'] == 0
        assert tweet['quote_count'] == 0
        assert tweet['bookmark_count'] == 0
    
    def test_parse_tweet_retweet(self):
        """Test parsing retweet information."""
        provider = DesearchProvider(api_key="dt_$test")
        
        desearch_tweet = {
            'id': '1234567890',
            'text': 'RT @original_user: Hello world',
            'created_at': '2024-01-15T12:00:00Z',
            'is_retweet': True,
            'retweet': {
                'id': '987654321',
                'user': {
                    'username': 'original_user'
                }
            }
        }
        
        tweet = provider._parse_tweet(desearch_tweet, "testuser")
        
        assert tweet is not None
        assert tweet['retweeted_user'] == 'original_user'
        assert tweet['retweeted_tweet_id'] == '987654321'
    
    def test_parse_tweet_quote(self):
        """Test parsing quote tweet information."""
        provider = DesearchProvider(api_key="dt_$test")
        
        desearch_tweet = {
            'id': '1234567890',
            'text': 'Great point! https://twitter.com/user/status/987654321',
            'created_at': '2024-01-15T12:00:00Z',
            'is_quote_tweet': True,
            'quoted_status_id': '987654321',
            'quote': {
                'user': {
                    'username': 'quoted_user'
                }
            }
        }
        
        tweet = provider._parse_tweet(desearch_tweet, "testuser")
        
        assert tweet is not None
        assert tweet['quoted_user'] == 'quoted_user'
        assert tweet['quoted_tweet_id'] == '987654321'
    
    def test_parse_tweet_reply(self):
        """Test parsing reply information."""
        provider = DesearchProvider(api_key="dt_$test")
        
        desearch_tweet = {
            'id': '1234567890',
            'text': '@other_user Good point!',
            'created_at': '2024-01-15T12:00:00Z',
            'in_reply_to_status_id': '987654321',
            'in_reply_to_screen_name': 'other_user'
        }
        
        tweet = provider._parse_tweet(desearch_tweet, "testuser")
        
        assert tweet is not None
        assert tweet['in_reply_to_status_id'] == '987654321'
        assert tweet['in_reply_to_user'] == 'other_user'
    
    def test_parse_tweet_invalid(self):
        """Test parsing invalid tweet data."""
        provider = DesearchProvider(api_key="dt_$test")
        
        # Missing tweet_id
        assert provider._parse_tweet({'text': 'Hello'}, "testuser") is None
        
        # Missing text
        assert provider._parse_tweet({'id': '123'}, "testuser") is None
        
        # Empty dict
        assert provider._parse_tweet({}, "testuser") is None
    
    def test_convert_iso_to_twitter_date(self):
        """Test ISO date conversion to Twitter format."""
        provider = DesearchProvider(api_key="dt_$test")
        
        # Test with Z suffix
        iso_date = "2024-01-15T12:30:45Z"
        twitter_date = provider._convert_iso_to_twitter_date(iso_date)
        assert "Mon Jan 15" in twitter_date
        assert "12:30:45" in twitter_date
        assert "2024" in twitter_date
        
        # Test with timezone offset
        iso_date = "2024-01-15T12:30:45+00:00"
        twitter_date = provider._convert_iso_to_twitter_date(iso_date)
        assert "Mon Jan 15" in twitter_date
        
        # Test invalid date returns original
        invalid_date = "invalid-date"
        result = provider._convert_iso_to_twitter_date(invalid_date)
        assert result == invalid_date
    
    @mock.patch.object(DesearchProvider, '_make_api_request')
    def test_fetch_from_endpoint_basic(self, mock_api_request):
        """Test basic endpoint fetching."""
        provider = DesearchProvider(api_key="dt_$test")
        
        # Use recent date that won't be filtered by cutoff
        recent_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        
        # Mock API response with complete tweet data
        mock_api_request.return_value = (
            {
                'tweets': [
                    {
                        'id': 123,  # Desearch returns int, will be converted to str
                        'text': 'Hello',
                        'created_at': recent_date,
                        'like_count': 10,
                        'retweet_count': 5,
                        'reply_count': 2
                    }
                ],
                'user': {
                    'username': 'testuser',
                    'followers_count': 1000
                }
            },
            None
        )
        
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        tweets, user_info, success = provider._fetch_from_endpoint(
            "/twitter/user/posts",
            "testuser",
            100,
            cutoff,
            "username"
        )
        
        assert success is True
        assert len(tweets) == 1
        assert tweets[0]['tweet_id'] == '123'
        assert user_info['username'] == 'testuser'
        assert user_info['followers_count'] == 1000
    
    @mock.patch.object(DesearchProvider, '_make_api_request')
    def test_fetch_from_endpoint_pagination(self, mock_api_request):
        """Test endpoint pagination."""
        provider = DesearchProvider(api_key="dt_$test", rate_limit_delay=0.01)
        
        # Use recent dates that won't be filtered by cutoff
        recent_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        
        # Mock paginated responses with complete tweet data
        page1_tweets = [
            {
                'id': i,  # Desearch returns int
                'text': f'Tweet {i}',
                'created_at': recent_date,
                'like_count': 1,
                'retweet_count': 0,
                'reply_count': 0
            }
            for i in range(100)
        ]
        page2_tweets = [
            {
                'id': i,
                'text': f'Tweet {i}',
                'created_at': recent_date,
                'like_count': 1,
                'retweet_count': 0,
                'reply_count': 0
            }
            for i in range(100, 150)
        ]
        
        mock_api_request.side_effect = [
            ({'tweets': page1_tweets, 'user': {'followers_count': 1000}}, None),
            ({'tweets': page2_tweets, 'user': {}}, None)
        ]
        
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        tweets, user_info, success = provider._fetch_from_endpoint(
            "/twitter/user/posts",
            "testuser",
            200,
            cutoff,
            "username"
        )
        
        assert success is True
        assert len(tweets) == 150
        assert mock_api_request.call_count == 2
    
    @mock.patch.object(DesearchProvider, '_make_api_request')
    def test_fetch_from_endpoint_date_cutoff(self, mock_api_request):
        """Test endpoint stops at date cutoff."""
        provider = DesearchProvider(api_key="dt_$test")
        
        # Mock tweets with different dates
        recent_date = datetime.now(timezone.utc).replace(microsecond=0)
        old_date = recent_date - timedelta(days=10)
        
        tweets = [
            {
                'id': '1',
                'text': 'Recent tweet',
                'created_at': recent_date.strftime('%Y-%m-%dT%H:%M:%SZ')
            },
            {
                'id': '2',
                'text': 'Old tweet',
                'created_at': old_date.strftime('%Y-%m-%dT%H:%M:%SZ')
            }
        ]
        
        mock_api_request.return_value = ({'tweets': tweets, 'user': {}}, None)
        
        cutoff = datetime.now(timezone.utc) - timedelta(days=5)
        result_tweets, user_info, success = provider._fetch_from_endpoint(
            "/twitter/user/posts",
            "testuser",
            100,
            cutoff,
            "username"
        )
        
        assert success is True
        # Should only get the recent tweet, old tweet hits cutoff
        assert len(result_tweets) == 1
        assert result_tweets[0]['tweet_id'] == '1'
    
    @mock.patch.object(DesearchProvider, '_make_api_request')
    def test_fetch_from_endpoint_api_error(self, mock_api_request):
        """Test endpoint handles API errors."""
        provider = DesearchProvider(api_key="dt_$test")
        
        # Mock API error
        mock_api_request.return_value = (None, "API error")
        
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        tweets, user_info, success = provider._fetch_from_endpoint(
            "/twitter/user/posts",
            "testuser",
            100,
            cutoff,
            "username"
        )
        
        assert success is False
        assert len(tweets) == 0
    
    @mock.patch.object(DesearchProvider, '_fetch_from_endpoint')
    def test_fetch_user_tweets_posts_only(self, mock_fetch_endpoint):
        """Test fetching user tweets in posts-only mode."""
        provider = DesearchProvider(api_key="dt_$test")
        
        mock_fetch_endpoint.return_value = (
            [{'tweet_id': '123', 'text': 'Hello'}],
            {'username': 'testuser', 'followers_count': 1000},
            True
        )
        
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        tweets, user_info, success = provider.fetch_user_tweets(
            "testuser",
            cutoff,
            400,
            posts_only=True
        )
        
        assert success is True
        assert len(tweets) == 1
        assert mock_fetch_endpoint.call_count == 1
        # Check it only called posts endpoint
        call_args = mock_fetch_endpoint.call_args[0]
        assert "/twitter/user/posts" in call_args[0]
    
    @mock.patch.object(DesearchProvider, '_fetch_from_endpoint')
    def test_fetch_user_tweets_dual_endpoint(self, mock_fetch_endpoint):
        """Test fetching user tweets in dual-endpoint mode."""
        provider = DesearchProvider(api_key="dt_$test")
        
        # Mock both endpoints returning tweets
        mock_fetch_endpoint.side_effect = [
            (
                [{'tweet_id': '123', 'text': 'Reply'}],
                {'username': 'testuser', 'followers_count': 1000},
                True
            ),
            (
                [{'tweet_id': '456', 'text': 'Post'}],
                {'username': 'testuser', 'followers_count': 1000},
                True
            )
        ]
        
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        tweets, user_info, success = provider.fetch_user_tweets(
            "testuser",
            cutoff,
            200,
            posts_only=False
        )
        
        assert success is True
        assert len(tweets) == 2
        assert mock_fetch_endpoint.call_count == 2
    
    @mock.patch.object(DesearchProvider, '_fetch_from_endpoint')
    def test_fetch_user_tweets_deduplication(self, mock_fetch_endpoint):
        """Test that duplicate tweets are deduplicated."""
        provider = DesearchProvider(api_key="dt_$test")
        
        # Mock both endpoints returning same tweet (e.g., pinned)
        duplicate_tweet = {'tweet_id': '123', 'text': 'Pinned tweet'}
        
        mock_fetch_endpoint.side_effect = [
            ([duplicate_tweet], {'username': 'testuser', 'followers_count': 1000}, True),
            ([duplicate_tweet, {'tweet_id': '456', 'text': 'Other'}], {'username': 'testuser', 'followers_count': 1000}, True)
        ]
        
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        tweets, user_info, success = provider.fetch_user_tweets(
            "testuser",
            cutoff,
            200,
            posts_only=False
        )
        
        assert success is True
        # Should only have 2 unique tweets, not 3
        assert len(tweets) == 2
        tweet_ids = [t['tweet_id'] for t in tweets]
        assert '123' in tweet_ids
        assert '456' in tweet_ids


class TestDesearchProviderIntegration:
    """Integration tests for DesearchProvider (may require real API key for full testing)."""
    
    @mock.patch('requests.get')
    def test_full_fetch_flow(self, mock_get):
        """Test complete tweet fetching flow."""
        provider = DesearchProvider(api_key="dt_$test")
        
        # Use recent date that won't be filtered by cutoff
        recent_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        
        # Mock successful API response
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'tweets': [
                {
                    'id': 1234567890,  # Desearch returns int
                    'text': 'Test tweet',
                    'created_at': recent_date,
                    'like_count': 10,
                    'retweet_count': 5,
                    'reply_count': 2,
                    'quote_count': 1,
                    'bookmark_count': 3
                }
            ],
            'user': {
                'username': 'testuser',
                'followers_count': 1000
            }
        }
        mock_get.return_value = mock_response
        
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        tweets, user_info, success = provider.fetch_user_tweets(
            "testuser",
            cutoff,
            400,
            posts_only=True
        )
        
        assert success is True
        assert len(tweets) == 1
        assert tweets[0]['tweet_id'] == '1234567890'
        assert tweets[0]['text'] == 'Test tweet'
        assert user_info['username'] == 'testuser'
        assert user_info['followers_count'] == 1000
