"""
Tests for RapidAPIProvider.
"""

import pytest
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

from bitcast.validator.clients.rapidapi_provider import RapidAPIProvider


class TestRapidAPIProvider:
    """Tests for RapidAPI provider implementation."""
    
    def test_init_basic(self):
        """Test provider initializes with API key."""
        provider = RapidAPIProvider(api_key="test_key_123")
        assert provider.api_key == "test_key_123"
        assert provider.base_url == "https://twitter-v24.p.rapidapi.com"
        assert "x-rapidapi-key" in provider.headers
        assert provider.headers["x-rapidapi-key"] == "test_key_123"
    
    def test_init_strips_whitespace(self):
        """Test provider strips whitespace from API key."""
        provider = RapidAPIProvider(api_key="  test_key_123  ")
        assert provider.api_key == "test_key_123"
    
    def test_init_with_config(self):
        """Test provider accepts configuration parameters."""
        provider = RapidAPIProvider(
            api_key="test123",
            max_retries=5,
            retry_delay=3.0,
            rate_limit_delay=2.0
        )
        assert provider.max_retries == 5
        assert provider.retry_delay == 3.0
        assert provider.rate_limit_delay == 2.0
    
    def test_validate_api_key_valid(self):
        """Test API key validation with valid key."""
        provider = RapidAPIProvider(api_key="test123")
        assert provider.validate_api_key() is True
    
    def test_validate_api_key_invalid(self):
        """Test API key validation with invalid keys."""
        # Empty key
        provider = RapidAPIProvider(api_key="")
        assert provider.validate_api_key() is False
    
    @mock.patch('requests.get')
    def test_make_api_request_success(self, mock_get):
        """Test successful API request."""
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'data': {'user': {'result': {}}}}
        mock_get.return_value = mock_response
        
        provider = RapidAPIProvider(api_key="test")
        data, error = provider._make_api_request("http://test", {})
        
        assert error is None
        assert 'data' in data
    
    @mock.patch('requests.get')
    def test_make_api_request_retry_logic(self, mock_get):
        """Test API retry logic works."""
        # Mock rate limit then success
        mock_429 = mock.Mock()
        mock_429.status_code = 429
        
        mock_200 = mock.Mock()
        mock_200.status_code = 200
        mock_200.json.return_value = {'data': {'user': {}}}
        
        mock_get.side_effect = [mock_429, mock_200]
        
        provider = RapidAPIProvider(api_key="test")
        
        with mock.patch('time.sleep'):
            data, error = provider._make_api_request("http://test", {})
        
        assert error is None
        assert mock_get.call_count == 2
    
    @mock.patch('requests.get')
    def test_make_api_request_handles_response_formats(self, mock_get):
        """Test API request handles different response formats."""
        provider = RapidAPIProvider(api_key="test")
        
        # Format 1: {"data": {"user": {...}}}
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'data': {'user': {}}}
        mock_get.return_value = mock_response
        data, error = provider._make_api_request("http://test", {})
        assert error is None
        assert 'data' in data
        
        # Format 2: {"user": {...}} (gets wrapped)
        mock_response.json.return_value = {'user': {}}
        data, error = provider._make_api_request("http://test", {})
        assert error is None
        assert 'data' in data
        assert 'user' in data['data']
    
    def test_parse_tweet_basic(self):
        """Test basic RapidAPI tweet parsing."""
        provider = RapidAPIProvider(api_key="test")
        
        # Mock RapidAPI tweet response
        tweet_result = {
            'rest_id': '1234567890',
            'legacy': {
                'full_text': 'Hello @user1 and @user2',
                'created_at': 'Mon Jan 15 12:00:00 +0000 2024',
                'favorite_count': 42,
                'retweet_count': 15,
                'reply_count': 8,
                'quote_count': 3,
                'bookmark_count': 5,
                'lang': 'en',
                'entities': {
                    'user_mentions': [
                        {'screen_name': 'user1'},
                        {'screen_name': 'user2'}
                    ]
                }
            },
            'core': {
                'user_results': {
                    'result': {
                        'legacy': {
                            'screen_name': 'testuser'
                        }
                    }
                }
            }
        }
        
        tweet = provider._parse_tweet(tweet_result)
        
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
    
    def test_parse_tweet_retweet(self):
        """Test parsing retweet information."""
        provider = RapidAPIProvider(api_key="test")
        
        tweet_result = {
            'rest_id': '1234567890',
            'legacy': {
                'full_text': 'RT @original_user: Hello world',
                'created_at': 'Mon Jan 15 12:00:00 +0000 2024',
                'retweeted_status_result': {
                    'result': {
                        'rest_id': '987654321'
                    }
                },
                'entities': {
                    'user_mentions': []
                }
            },
            'core': {
                'user_results': {
                    'result': {
                        'legacy': {
                            'screen_name': 'testuser'
                        }
                    }
                }
            }
        }
        
        tweet = provider._parse_tweet(tweet_result)
        
        assert tweet is not None
        assert tweet['retweeted_user'] == 'original_user'
        assert tweet['retweeted_tweet_id'] == '987654321'
    
    def test_parse_tweet_quote(self):
        """Test parsing quote tweet information."""
        provider = RapidAPIProvider(api_key="test")
        
        tweet_result = {
            'rest_id': '1234567890',
            'legacy': {
                'full_text': 'Great point! https://twitter.com/user/status/987654321',
                'created_at': 'Mon Jan 15 12:00:00 +0000 2024',
                'is_quote_status': True,
                'quoted_status_id_str': '987654321',
                'quoted_status_result': {
                    'result': {
                        'core': {
                            'user_results': {
                                'result': {
                                    'legacy': {
                                        'screen_name': 'quoted_user'
                                    }
                                }
                            }
                        }
                    }
                },
                'entities': {
                    'user_mentions': []
                }
            },
            'core': {
                'user_results': {
                    'result': {
                        'legacy': {
                            'screen_name': 'testuser'
                        }
                    }
                }
            }
        }
        
        tweet = provider._parse_tweet(tweet_result)
        
        assert tweet is not None
        assert tweet['quoted_user'] == 'quoted_user'
        assert tweet['quoted_tweet_id'] == '987654321'
    
    def test_parse_tweet_reply(self):
        """Test parsing reply information."""
        provider = RapidAPIProvider(api_key="test")
        
        tweet_result = {
            'rest_id': '1234567890',
            'legacy': {
                'full_text': '@other_user Good point!',
                'created_at': 'Mon Jan 15 12:00:00 +0000 2024',
                'in_reply_to_status_id_str': '987654321',
                'in_reply_to_screen_name': 'other_user',
                'entities': {
                    'user_mentions': []
                }
            },
            'core': {
                'user_results': {
                    'result': {
                        'legacy': {
                            'screen_name': 'testuser'
                        }
                    }
                }
            }
        }
        
        tweet = provider._parse_tweet(tweet_result)
        
        assert tweet is not None
        assert tweet['in_reply_to_status_id'] == '987654321'
        assert tweet['in_reply_to_user'] == 'other_user'
    
    def test_parse_tweet_invalid(self):
        """Test parsing invalid tweet data."""
        provider = RapidAPIProvider(api_key="test")
        
        # Missing full_text
        assert provider._parse_tweet({'legacy': {}}) is None
        
        # Empty dict
        assert provider._parse_tweet({}) is None
    
    @mock.patch.object(RapidAPIProvider, '_make_api_request')
    def test_fetch_from_endpoint_basic(self, mock_api_request):
        """Test basic endpoint fetching."""
        provider = RapidAPIProvider(api_key="test")
        
        # Use recent date that won't be filtered by cutoff
        recent_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%a %b %d %H:%M:%S %z %Y')
        
        # Mock RapidAPI response
        mock_api_request.return_value = (
            {
                'data': {
                    'user': {
                        'result': {
                            'timeline': {
                                'timeline': {
                                    'instructions': [
                                        {
                                            'type': 'TimelineAddEntries',
                                            'entries': [
                                                {
                                                    'entryId': 'tweet-123',
                                                    'content': {
                                                        'itemContent': {
                                                            'tweet_results': {
                                                                'result': {
                                                                    'rest_id': '123',
                                                                    'legacy': {
                                                                        'full_text': 'Hello',
                                                                        'created_at': recent_date,
                                                                        'favorite_count': 10,
                                                                        'entities': {'user_mentions': []}
                                                                    },
                                                                    'core': {
                                                                        'user_results': {
                                                                            'result': {
                                                                                'legacy': {
                                                                                    'screen_name': 'testuser',
                                                                                    'followers_count': 1000
                                                                                }
                                                                            }
                                                                        }
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    }
                                                }
                                            ]
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            },
            None
        )
        
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        tweets, user_info, success = provider._fetch_from_endpoint(
            "/user/tweets",
            "testuser",
            100,
            cutoff
        )
        
        assert success is True
        assert len(tweets) == 1
        assert tweets[0]['tweet_id'] == '123'
        assert user_info['followers_count'] == 1000
    
    @mock.patch.object(RapidAPIProvider, '_fetch_from_endpoint')
    def test_fetch_user_tweets_posts_only(self, mock_fetch_endpoint):
        """Test fetching user tweets in posts-only mode."""
        provider = RapidAPIProvider(api_key="test")
        
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
        # Check it only called tweets endpoint
        call_args = mock_fetch_endpoint.call_args[0]
        assert "/user/tweets" in call_args[0]
    
    @mock.patch.object(RapidAPIProvider, '_fetch_from_endpoint')
    def test_fetch_user_tweets_dual_endpoint(self, mock_fetch_endpoint):
        """Test fetching user tweets in dual-endpoint mode."""
        provider = RapidAPIProvider(api_key="test")
        
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
    
    @mock.patch.object(RapidAPIProvider, '_fetch_from_endpoint')
    def test_fetch_user_tweets_deduplication(self, mock_fetch_endpoint):
        """Test that duplicate tweets are deduplicated."""
        provider = RapidAPIProvider(api_key="test")
        
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


class TestRapidAPIProviderIntegration:
    """Integration tests for RapidAPIProvider."""
    
    @mock.patch('requests.get')
    def test_full_fetch_flow(self, mock_get):
        """Test complete tweet fetching flow."""
        provider = RapidAPIProvider(api_key="test")
        
        # Use recent date that won't be filtered by cutoff
        recent_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%a %b %d %H:%M:%S %z %Y')
        
        # Mock successful API response
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'data': {
                'user': {
                    'result': {
                        'timeline': {
                            'timeline': {
                                'instructions': [
                                    {
                                        'type': 'TimelineAddEntries',
                                        'entries': [
                                            {
                                                'entryId': 'tweet-1234567890',
                                                'content': {
                                                    'itemContent': {
                                                        'tweet_results': {
                                                            'result': {
                                                                'rest_id': '1234567890',
                                                                'legacy': {
                                                                    'full_text': 'Test tweet',
                                                                    'created_at': recent_date,
                                                                    'favorite_count': 10,
                                                                    'retweet_count': 5,
                                                                    'reply_count': 2,
                                                                    'quote_count': 1,
                                                                    'bookmark_count': 3,
                                                                    'entities': {'user_mentions': []}
                                                                },
                                                                'core': {
                                                                    'user_results': {
                                                                        'result': {
                                                                            'legacy': {
                                                                                'screen_name': 'testuser',
                                                                                'followers_count': 1000
                                                                            }
                                                                        }
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                }
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
