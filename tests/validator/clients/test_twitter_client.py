"""
Tests for TwitterClient with provider support.
"""

import pytest
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

from bitcast.validator.clients import TwitterClient, DesearchProvider, RapidAPIProvider


class TestTwitterClientProviderSelection:
    """Tests for provider selection and initialization."""
    
    @mock.patch('bitcast.validator.clients.twitter_client.TWITTER_API_PROVIDER', 'desearch')
    @mock.patch('bitcast.validator.clients.twitter_client.DESEARCH_API_KEY', 'dt_$test')
    def test_init_with_desearch_provider(self):
        """Test initialization with Desearch provider."""
        client = TwitterClient()
        assert client.provider_name == 'desearch'
        assert isinstance(client.provider, DesearchProvider)
    
    @mock.patch('bitcast.validator.clients.twitter_client.TWITTER_API_PROVIDER', 'rapidapi')
    @mock.patch('bitcast.validator.clients.twitter_client.RAPID_API_KEY', 'test123')
    def test_init_with_rapidapi_provider(self):
        """Test initialization with RapidAPI provider."""
        client = TwitterClient()
        assert client.provider_name == 'rapidapi'
        assert isinstance(client.provider, RapidAPIProvider)
    
    def test_provider_override(self):
        """Test manual provider override."""
        with mock.patch('bitcast.validator.clients.twitter_client.RAPID_API_KEY', 'test123'):
            client = TwitterClient(provider='rapidapi')
            assert client.provider_name == 'rapidapi'
            assert isinstance(client.provider, RapidAPIProvider)
    
    def test_provider_selection_from_config(self):
        """Test provider is selected correctly from config."""
        with mock.patch('bitcast.validator.clients.twitter_client.TWITTER_API_PROVIDER', 'desearch'):
            with mock.patch('bitcast.validator.clients.twitter_client.DESEARCH_API_KEY', 'dt_$test'):
                client = TwitterClient()
                assert client.provider_name == 'desearch'
                assert isinstance(client.provider, DesearchProvider)
    
    def test_invalid_provider_raises_error(self):
        """Test unknown provider raises error."""
        with pytest.raises(ValueError, match="Unknown provider"):
            TwitterClient(provider='invalid')
    
    def test_missing_desearch_key_raises_error(self):
        """Test missing Desearch key raises error."""
        with mock.patch('bitcast.validator.clients.twitter_client.DESEARCH_API_KEY', None):
            with pytest.raises(ValueError, match="DESEARCH_API_KEY not configured"):
                TwitterClient(provider='desearch')
    
    def test_missing_rapidapi_key_raises_error(self):
        """Test missing RapidAPI key raises error."""
        with mock.patch('bitcast.validator.clients.twitter_client.RAPID_API_KEY', None):
            with pytest.raises(ValueError, match="RAPID_API_KEY not configured"):
                TwitterClient(provider='rapidapi')
    
    def test_invalid_desearch_key_format_raises_error(self):
        """Test invalid Desearch key format raises error."""
        with pytest.raises(ValueError, match="Invalid DESEARCH_API_KEY format"):
            TwitterClient(api_key="test123", provider='desearch')


class TestTwitterClientHelperMethods:
    """Tests for TwitterClient helper methods (caching, validation, etc.)."""
    
    @mock.patch('bitcast.validator.clients.twitter_client.TWITTER_API_PROVIDER', 'desearch')
    @mock.patch('bitcast.validator.clients.twitter_client.DESEARCH_API_KEY', 'dt_$test')
    def test_validate_tweet_authors_strict(self):
        """Test strict tweet author validation (rejects None and wrong authors)."""
        client = TwitterClient()
        
        tweets = [
            {'tweet_id': '1', 'text': 'My tweet', 'author': 'testuser'},
            {'tweet_id': '2', 'text': 'Other tweet', 'author': 'otheruser'},
            {'tweet_id': '3', 'text': 'No author', 'author': None}
        ]
        
        validated = client._validate_tweet_authors(tweets, 'testuser')
        
        # Strict validation: only keep tweets from testuser, reject None and other users
        assert len(validated) == 1
        assert validated[0]['tweet_id'] == '1'
        assert validated[0]['author'] == 'testuser'


class TestTwitterClientCaching:
    """Tests for TwitterClient caching behavior."""
    
    @mock.patch('bitcast.validator.clients.twitter_client.TWITTER_API_PROVIDER', 'desearch')
    @mock.patch('bitcast.validator.clients.twitter_client.DESEARCH_API_KEY', 'dt_$test')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    def test_fetch_merges_with_cache(self, mock_cache_set, mock_cache_get):
        """Test that fetch always calls provider and merges with cache."""
        recent_time = datetime.now() - timedelta(hours=1)
        mock_cache_get.return_value = {
            'tweets': [{'tweet_id': '123', 'text': 'Cached', 'author': 'testuser', 'created_at': 'Mon Jan 15 12:00:00 +0000 2024'}],
            'user_info': {'username': 'testuser', 'followers_count': 1000},
            'last_updated': recent_time
        }
        
        client = TwitterClient()
        # Mock the provider to return new tweets
        client.provider.fetch_user_tweets = mock.Mock(return_value=(
            [{'tweet_id': '456', 'text': 'New', 'author': 'testuser', 'created_at': 'Mon Feb 01 12:00:00 +0000 2026'}],
            {'username': 'testuser', 'followers_count': 1000},
            True
        ))
        
        result = client.fetch_user_tweets('testuser')
        
        # Always fetches from provider and merges with cache
        assert result['cache_info']['cache_hit'] is True
        assert result['cache_info']['provider_used'] == 'desearch'
        assert len(result['tweets']) == 2  # 1 new + 1 cached
    
    @mock.patch('bitcast.validator.clients.twitter_client.TWITTER_API_PROVIDER', 'desearch')
    @mock.patch('bitcast.validator.clients.twitter_client.DESEARCH_API_KEY', 'dt_$test')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    def test_uses_cache_timestamp_as_cutoff(self, mock_cache_set, mock_cache_get):
        """When cache exists, incremental_cutoff is derived from cache_timestamp."""
        cache_time = datetime.now() - timedelta(hours=6)
        mock_cache_get.return_value = {
            'tweets': [{'tweet_id': '123', 'text': 'Cached', 'author': 'testuser',
                        'created_at': 'Mon Jan 15 12:00:00 +0000 2024'}],
            'user_info': {'username': 'testuser', 'followers_count': 1000},
            'cache_timestamp': cache_time.isoformat(),
        }
        
        client = TwitterClient()
        client.provider.fetch_user_tweets = mock.Mock(return_value=(
            [], {'username': 'testuser', 'followers_count': 1000}, True
        ))
        
        client.fetch_user_tweets('testuser')
        
        # Provider should have been called with cutoff ~1h before cache_timestamp
        call_args = client.provider.fetch_user_tweets.call_args
        cutoff = call_args[1]['incremental_cutoff']
        expected = cache_time - timedelta(hours=1)
        assert abs((cutoff - expected).total_seconds()) < 2
    
    @mock.patch('bitcast.validator.clients.twitter_client.TWITTER_API_PROVIDER', 'desearch')
    @mock.patch('bitcast.validator.clients.twitter_client.DESEARCH_API_KEY', 'dt_$test')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    def test_falls_back_to_fetch_days_without_cache(self, mock_cache_set, mock_cache_get):
        """Without cache, falls back to fetch_days for cutoff."""
        mock_cache_get.return_value = None
        
        client = TwitterClient()
        client.provider.fetch_user_tweets = mock.Mock(return_value=(
            [], {'username': 'testuser', 'followers_count': 0}, True
        ))
        
        client.fetch_user_tweets('testuser', fetch_days=7)
        
        call_args = client.provider.fetch_user_tweets.call_args
        cutoff = call_args[1]['incremental_cutoff']
        expected = datetime.now() - timedelta(days=7)
        assert abs((cutoff - expected).total_seconds()) < 5


class TestTwitterClientUsernameValidation:
    """Test username validation at TwitterClient entry point."""
    
    def test_fetch_user_tweets_rejects_numeric_username(self):
        """Test that fetch_user_tweets rejects numeric user IDs (suspended/deleted accounts)."""
        client = TwitterClient()
        
        result = client.fetch_user_tweets('911245230426525697')
        
        assert result['tweets'] == []
        assert result['user_info']['username'] == '911245230426525697'
        assert result['cache_info']['provider_used'] == 'none'
    
    def test_fetch_user_tweets_accepts_valid_username(self):
        """Test that fetch_user_tweets accepts valid usernames."""
        from bitcast.validator.utils.twitter_validators import is_valid_twitter_username
        
        # Validation function should distinguish between valid usernames and numeric IDs
        assert is_valid_twitter_username('elonmusk')
        assert is_valid_twitter_username('jack')
        assert is_valid_twitter_username('user123')  # Has letters - valid
        assert not is_valid_twitter_username('911245230426525697')  # Purely numeric - invalid
