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
    
    def test_validate_tweet_authors(self):
        """Test tweet author validation."""
        with mock.patch('bitcast.validator.clients.twitter_client.DESEARCH_API_KEY', 'dt_$test'):
            client = TwitterClient()
        
        tweets = [
            {'tweet_id': '1', 'text': 'My tweet', 'author': 'testuser'},
            {'tweet_id': '2', 'text': 'Other tweet', 'author': 'otheruser'},
            {'tweet_id': '3', 'text': 'No author', 'author': None}
        ]
        
        validated = client._validate_tweet_authors(tweets, 'testuser')
        
        # Should keep testuser's tweet and tweet with no author (gets set)
        assert len(validated) == 2
        assert validated[0]['tweet_id'] == '1'
        assert validated[1]['tweet_id'] == '3'
        assert validated[1]['author'] == 'testuser'  # Author set for no-author tweet


class TestTwitterClientCaching:
    """Tests for TwitterClient caching behavior."""
    
    @mock.patch('bitcast.validator.clients.twitter_client.DESEARCH_API_KEY', 'dt_$test')
    @mock.patch('bitcast.validator.utils.twitter_cache.get_cached_user_tweets')
    @mock.patch('bitcast.validator.utils.twitter_cache.cache_user_tweets')
    def test_fetch_uses_cache_when_fresh(self, mock_cache_set, mock_cache_get):
        """Test that fresh cache is used without calling provider."""
        recent_time = datetime.now() - timedelta(hours=1)
        mock_cache_get.return_value = {
            'tweets': [{'tweet_id': '123', 'text': 'Cached', 'author': 'testuser', 'created_at': 'Mon Jan 15 12:00:00 +0000 2024'}],
            'user_info': {'username': 'testuser', 'followers_count': 1000},
            'last_updated': recent_time
        }
        
        client = TwitterClient()
        result = client.fetch_user_tweets('testuser')
        
        # Verify cache was used
        assert result['cache_info']['cache_hit'] is True
        assert result['cache_info']['provider_used'] == 'cache'
        assert len(result['tweets']) == 1
