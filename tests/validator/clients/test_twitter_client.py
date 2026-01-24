"""
Tests for TwitterClient with Desearch.ai API.
"""

import pytest
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

from bitcast.validator.clients.twitter_client import TwitterClient


class TestTwitterClient:
    """TwitterClient tests for Desearch.ai implementation."""
    
    def test_init_requires_api_key(self):
        """Test client requires Desearch.ai API key."""
        with mock.patch('bitcast.validator.clients.twitter_client.DESEARCH_API_KEY', None):
            with pytest.raises(ValueError, match="DESEARCH_API_KEY"):
                TwitterClient()
    
    def test_init_with_api_key(self):
        """Test client initializes with API key."""
        client = TwitterClient(api_key="test_key_123")
        assert client.api_key == "test_key_123"
        assert client.base_url == "https://api.desearch.ai"
        assert "Authorization" in client.headers
    
    def test_api_key_prefix_handling(self):
        """Test API key prefix is added correctly."""
        # Test without prefix
        client = TwitterClient(api_key="test123")
        assert client.headers["Authorization"] == "dt_$test123"
        
        # Test with $ prefix
        client2 = TwitterClient(api_key="$test123")
        assert client2.headers["Authorization"] == "dt_$test123"
        
        # Test with full prefix
        client3 = TwitterClient(api_key="dt_$test123")
        assert client3.headers["Authorization"] == "dt_$test123"
    
    @mock.patch('requests.get')
    def test_api_request_retry_logic(self, mock_get):
        """Test API retry logic works."""
        # Mock rate limit then success
        mock_429 = mock.Mock()
        mock_429.status_code = 429
        
        mock_200 = mock.Mock()
        mock_200.status_code = 200
        mock_200.json.return_value = {'tweets': [], 'user': {}}
        
        mock_get.side_effect = [mock_429, mock_200]
        
        client = TwitterClient(api_key="test")
        
        with mock.patch('time.sleep'):
            data, error = client._make_api_request("http://test", {})
        
        assert error is None
        assert mock_get.call_count == 2
    
    def test_parse_desearch_tweet_basic(self):
        """Test basic Desearch.ai tweet parsing."""
        client = TwitterClient(api_key="test")
        
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
        
        tweet = client._parse_desearch_tweet(desearch_tweet, "testuser")
        
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
    
    def test_parse_desearch_tweet_engagement_defaults(self):
        """Test engagement metrics default to 0 when missing."""
        client = TwitterClient(api_key="test")
        
        # Minimal tweet without engagement metrics
        desearch_tweet = {
            'id': '9876543210',
            'text': 'Tweet without engagement metrics',
            'created_at': '2024-01-15T12:00:00Z'
        }
        
        tweet = client._parse_desearch_tweet(desearch_tweet, "testuser")
        
        assert tweet is not None
        assert tweet['favorite_count'] == 0
        assert tweet['retweet_count'] == 0
        assert tweet['reply_count'] == 0
        assert tweet['quote_count'] == 0
        assert tweet['bookmark_count'] == 0
    
    def test_parse_desearch_tweet_retweet(self):
        """Test parsing retweet from Desearch.ai."""
        client = TwitterClient(api_key="test")
        
        desearch_tweet = {
            'id': '111222333',
            'text': 'RT @original: Great tweet',
            'created_at': '2024-01-15T12:00:00Z',
            'is_retweet': True,
            'retweet': {
                'id': '999888777',
                'user': {
                    'username': 'original'
                }
            }
        }
        
        tweet = client._parse_desearch_tweet(desearch_tweet, "testuser")
        
        assert tweet is not None
        assert tweet['retweeted_user'] == 'original'
        assert tweet['retweeted_tweet_id'] == '999888777'
    
    def test_parse_desearch_tweet_quote(self):
        """Test parsing quote tweet from Desearch.ai."""
        client = TwitterClient(api_key="test")
        
        desearch_tweet = {
            'id': '555666777',
            'text': 'Adding context to this tweet',
            'created_at': '2024-01-15T12:00:00Z',
            'is_quote_tweet': True,
            'quoted_status_id': '444555666',
            'quote': {
                'user': {
                    'username': 'quoteduser'
                }
            }
        }
        
        tweet = client._parse_desearch_tweet(desearch_tweet, "testuser")
        
        assert tweet is not None
        assert tweet['quoted_tweet_id'] == '444555666'
        assert tweet['quoted_user'] == 'quoteduser'
    
    def test_parse_desearch_tweet_invalid(self):
        """Test parsing returns None for invalid tweet."""
        client = TwitterClient(api_key="test")
        
        # Missing required fields
        invalid_tweet = {'id': '123'}  # No text
        assert client._parse_desearch_tweet(invalid_tweet, "testuser") is None
        
        invalid_tweet2 = {'text': 'Hello'}  # No ID
        assert client._parse_desearch_tweet(invalid_tweet2, "testuser") is None
    
    def test_convert_iso_to_twitter_date(self):
        """Test ISO date conversion to Twitter format."""
        client = TwitterClient(api_key="test")
        
        # Test with Z suffix
        iso_date = "2024-01-15T12:30:45Z"
        twitter_date = client._convert_iso_to_twitter_date(iso_date)
        assert "2024" in twitter_date
        assert "12:30:45" in twitter_date
        
        # Test with timezone offset
        iso_date2 = "2024-01-15T12:30:45+00:00"
        twitter_date2 = client._convert_iso_to_twitter_date(iso_date2)
        assert "2024" in twitter_date2
    
    def test_validate_tweet_authors(self):
        """Test author validation filters correctly."""
        client = TwitterClient(api_key="test")
        
        tweets = [
            {'tweet_id': '1', 'author': 'testuser', 'text': 'My tweet'},
            {'tweet_id': '2', 'author': 'otheruser', 'text': 'Other tweet'},
            {'tweet_id': '3', 'author': 'testuser', 'text': 'Another tweet'},
            {'tweet_id': '4', 'text': 'No author field'}  # Missing author
        ]
        
        validated = client._validate_tweet_authors(tweets, 'testuser')
        
        # Should keep testuser tweets and add author to missing one
        assert len(validated) == 3
        assert validated[0]['tweet_id'] == '1'
        assert validated[1]['tweet_id'] == '3'
        assert validated[2]['tweet_id'] == '4'
        assert validated[2]['author'] == 'testuser'  # Author was added
    
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('requests.get')
    def test_fetch_user_tweets_with_desearch(self, mock_get, mock_get_cache, mock_cache):
        """Test fetching tweets from Desearch.ai API."""
        # Mock no cache
        mock_get_cache.return_value = None
        
        # Use recent dates (within last 30 days) to pass cutoff filter
        now = datetime.now(timezone.utc)
        recent_date1 = (now - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        recent_date2 = (now - timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        
        # Mock Desearch.ai API response
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'user': {
                'username': 'testuser',
                'followers_count': 1000
            },
            'tweets': [
                {
                    'id': '123',
                    'text': 'Test tweet 1',
                    'created_at': recent_date1,
                    'like_count': 10
                },
                {
                    'id': '456',
                    'text': 'Test tweet 2',
                    'created_at': recent_date2,
                    'like_count': 20
                }
            ]
        }
        mock_get.return_value = mock_response
        
        client = TwitterClient(api_key="test", posts_only=True)
        
        with mock.patch('time.sleep'):
            result = client.fetch_user_tweets("testuser")
        
        # Verify API was called
        assert mock_get.called
        
        # Verify results
        assert result['user_info']['username'] == 'testuser'
        assert result['user_info']['followers_count'] == 1000
        assert len(result['tweets']) == 2
        assert result['tweets'][0]['tweet_id'] == '123'
        assert result['tweets'][1]['tweet_id'] == '456'
    
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('requests.get')
    def test_fetch_user_tweets_with_pagination(self, mock_get, mock_get_cache, mock_cache):
        """Test Desearch.ai pagination fetches multiple pages."""
        # Mock no cache
        mock_get_cache.return_value = None
        
        # Use recent dates within last 30 days
        now = datetime.now(timezone.utc)
        date_page1 = (now - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        date_page2 = (now - timedelta(days=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        
        # Mock two pages of responses
        mock_response_page1 = mock.Mock()
        mock_response_page1.status_code = 200
        mock_response_page1.json.return_value = {
            'user': {'username': 'testuser', 'followers_count': 1000},
            'tweets': [
                {'id': str(i), 'text': f'Tweet {i}', 'created_at': date_page1}
                for i in range(100)
            ]
        }
        
        mock_response_page2 = mock.Mock()
        mock_response_page2.status_code = 200
        mock_response_page2.json.return_value = {
            'user': {'username': 'testuser', 'followers_count': 1000},
            'tweets': [
                {'id': str(100 + i), 'text': f'Tweet {100 + i}', 'created_at': date_page2}
                for i in range(50)
            ]
        }
        
        mock_get.side_effect = [mock_response_page1, mock_response_page2]
        
        client = TwitterClient(api_key="test", posts_only=True)
        
        with mock.patch('time.sleep'):
            result = client.fetch_user_tweets("testuser")
        
        # Should fetch multiple pages
        assert mock_get.call_count == 2
        
        # Should have tweets from both pages
        assert len(result['tweets']) == 150
    
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    def test_fetch_user_tweets_uses_cache(self, mock_get_cache, mock_cache):
        """Test that fresh cache is used without API call."""
        now = datetime.now()
        
        # Mock fresh cache (within freshness window)
        mock_get_cache.return_value = {
            'user_info': {'username': 'testuser', 'followers_count': 500},
            'tweets': [
                {
                    'tweet_id': '111',
                    'author': 'testuser',
                    'text': 'Cached tweet',
                    'created_at': 'Mon Jan 15 12:00:00 +0000 2024',
                    'missing_count': 0
                }
            ],
            'last_updated': now - timedelta(hours=1)  # Fresh cache
        }
        
        client = TwitterClient(api_key="test")
        
        result = client.fetch_user_tweets("testuser")
        
        # Should use cache, not call API
        assert result['user_info']['username'] == 'testuser'
        assert len(result['tweets']) == 1
        assert result['cache_info']['cache_hit'] is True
    
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('requests.get')
    def test_dual_endpoint_fetching(self, mock_get, mock_get_cache, mock_cache):
        """Test dual-endpoint mode fetches from both endpoints."""
        # Mock no cache
        mock_get_cache.return_value = None
        
        # Use recent dates
        now = datetime.now(timezone.utc)
        recent_date = (now - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        
        # Mock responses for both endpoints
        mock_response_replies = mock.Mock()
        mock_response_replies.status_code = 200
        mock_response_replies.json.return_value = {
            'user': {'username': 'testuser', 'followers_count': 1000},
            'tweets': [
                {'id': '1', 'text': 'Reply tweet', 'created_at': recent_date}
            ]
        }
        
        mock_response_posts = mock.Mock()
        mock_response_posts.status_code = 200
        mock_response_posts.json.return_value = {
            'user': {'username': 'testuser', 'followers_count': 1000},
            'tweets': [
                {'id': '2', 'text': 'Post tweet', 'created_at': recent_date},
                {'id': '1', 'text': 'Reply tweet', 'created_at': recent_date}  # Duplicate
            ]
        }
        
        mock_get.side_effect = [mock_response_replies, mock_response_posts]
        
        # Enable dual-endpoint mode
        client = TwitterClient(api_key="test", posts_only=False)
        
        with mock.patch('time.sleep'):
            result = client.fetch_user_tweets("testuser")
        
        # Should call both endpoints
        assert mock_get.call_count == 2
        
        # Should deduplicate tweets
        assert len(result['tweets']) == 2  # Not 3 (duplicate removed)
        tweet_ids = {t['tweet_id'] for t in result['tweets']}
        assert tweet_ids == {'1', '2'}


class TestTwitterClientIntegration:
    """Integration tests requiring more complex scenarios."""
    
    def test_check_user_relevance(self):
        """Test user relevance checking based on keywords."""
        # This method still exists and uses the main fetch_user_tweets
        # We'll just test it returns False without API key to avoid actual calls
        with mock.patch('bitcast.validator.clients.twitter_client.DESEARCH_API_KEY', 'test'):
            with mock.patch.object(TwitterClient, 'fetch_user_tweets') as mock_fetch:
                mock_fetch.return_value = {
                    'user_info': {'followers_count': 1000},
                    'tweets': [
                        {'text': 'I love Python programming'},
                        {'text': 'Machine learning is great'}
                    ]
                }
                
                client = TwitterClient(api_key="test")
                
                # Should find Python keyword
                assert client.check_user_relevance("testuser", ["Python"], min_followers=500)
                
                # Should not find Java keyword
                assert not client.check_user_relevance("testuser", ["Java"], min_followers=500)
                
                # Should fail follower threshold
                assert not client.check_user_relevance("testuser", ["Python"], min_followers=5000)
