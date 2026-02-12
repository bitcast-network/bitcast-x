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
        assert tweet['views_count'] == 0  # No views object provided
    
    def test_parse_tweet_views_count(self):
        """Test views_count extraction from tweet_result.views.count."""
        provider = RapidAPIProvider(api_key="test")
        
        tweet_result = {
            'rest_id': '111',
            'legacy': {
                'full_text': 'Tweet with views',
                'created_at': 'Mon Jan 15 12:00:00 +0000 2024',
                'favorite_count': 10,
                'retweet_count': 5,
                'reply_count': 2,
                'quote_count': 1,
                'bookmark_count': 0,
                'lang': 'en',
                'entities': {'user_mentions': []}
            },
            'core': {
                'user_results': {
                    'result': {
                        'legacy': {'screen_name': 'testuser'}
                    }
                }
            },
            'views': {'count': '98765', 'state': 'EnabledWithCount'}
        }
        
        tweet = provider._parse_tweet(tweet_result)
        assert tweet is not None
        assert tweet['views_count'] == 98765
        
        # Test views without count (state only)
        tweet_result['views'] = {'state': 'Enabled'}
        tweet = provider._parse_tweet(tweet_result)
        assert tweet['views_count'] == 0
    
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


class TestRapidAPIProviderSearchTweets:
    """Tests for search_tweets method."""
    
    @mock.patch('requests.get')
    def test_search_tweets_success(self, mock_get):
        """Test successful tweet search."""
        provider = RapidAPIProvider(api_key="test_key")
        
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'data': {
                'search_by_raw_query': {
                    'search_timeline': {
                        'timeline': {
                            'instructions': [
                                {
                                    'type': 'TimelineAddEntries',
                                    'entries': [
                                        {
                                            'entryId': 'tweet-123456',
                                            'content': {
                                                '__typename': 'TimelineItem',
                                                'itemContent': {
                                                    'itemType': 'TimelineTweet',
                                                    'tweet_results': {
                                                        'result': {
                                                            'rest_id': '123456',
                                                            'legacy': {
                                                                'full_text': 'Found #bitcoin tweet',
                                                                'created_at': 'Mon Jan 15 12:00:00 +0000 2024',
                                                                'favorite_count': 10,
                                                                'retweet_count': 5,
                                                                'entities': {'user_mentions': []}
                                                            },
                                                            'core': {
                                                                'user_results': {
                                                                    'result': {
                                                                        'legacy': {'screen_name': 'testuser'}
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
        mock_get.return_value = mock_response
        
        tweets, success = provider.search_tweets("#bitcoin", max_results=100)
        
        assert success is True
        assert len(tweets) == 1
        assert tweets[0]['tweet_id'] == '123456'
        assert tweets[0]['author'] == 'testuser'
    
    @mock.patch('requests.get')
    def test_search_tweets_empty(self, mock_get):
        """Test search with no results."""
        provider = RapidAPIProvider(api_key="test_key")
        
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'data': {
                'search_by_raw_query': {
                    'search_timeline': {
                        'timeline': {
                            'instructions': []
                        }
                    }
                }
            }
        }
        mock_get.return_value = mock_response
        
        tweets, success = provider.search_tweets("#nonexistent", max_results=100)
        
        assert success is True
        assert len(tweets) == 0
    
    @mock.patch('requests.get')
    def test_search_tweets_api_error(self, mock_get):
        """Test search with API error."""
        provider = RapidAPIProvider(api_key="test_key")
        
        mock_response = mock.Mock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response
        
        tweets, success = provider.search_tweets("#bitcoin", max_results=100)
        
        assert success is False
        assert len(tweets) == 0


class TestRapidAPIProviderGetRetweeters:
    """Tests for get_retweeters method."""
    
    @mock.patch('requests.get')
    def test_get_retweeters_success(self, mock_get):
        """Test successful retweeters retrieval."""
        provider = RapidAPIProvider(api_key="test_key")
        
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'data': {
                'retweeters_timeline': {
                    'timeline': {
                        'instructions': [
                            {
                                'type': 'TimelineAddEntries',
                                'entries': [
                                    {
                                        'entryId': 'user-1',
                                        'content': {
                                            'itemContent': {
                                                'user_results': {
                                                    'result': {
                                                        'legacy': {'screen_name': 'User1'}
                                                    }
                                                }
                                            }
                                        }
                                    },
                                    {
                                        'entryId': 'user-2',
                                        'content': {
                                            'itemContent': {
                                                'user_results': {
                                                    'result': {
                                                        'legacy': {'screen_name': 'User2'}
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
        mock_get.return_value = mock_response
        
        usernames, success = provider.get_retweeters("123456789")
        
        assert success is True
        assert len(usernames) == 2
        assert 'user1' in usernames  # Should be lowercased
        assert 'user2' in usernames
    
    @mock.patch('requests.get')
    def test_get_retweeters_empty(self, mock_get):
        """Test retweeters with no results."""
        provider = RapidAPIProvider(api_key="test_key")
        
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'data': {
                'retweeters_timeline': {
                    'timeline': {
                        'instructions': []
                    }
                }
            }
        }
        mock_get.return_value = mock_response
        
        usernames, success = provider.get_retweeters("123456789")
        
        assert success is True
        assert len(usernames) == 0
    
    @mock.patch('requests.get')
    def test_get_retweeters_api_error(self, mock_get):
        """Test retweeters with API error."""
        provider = RapidAPIProvider(api_key="test_key")
        
        mock_response = mock.Mock()
        mock_response.status_code = 429  # Rate limit
        mock_get.return_value = mock_response
        
        usernames, success = provider.get_retweeters("123456789")
        
        assert success is False
        assert len(usernames) == 0


class TestRapidAPIProviderNumericIDFiltering:
    """Test that numeric user IDs (from suspended/deleted accounts) are filtered during parsing."""
    
    def test_parse_tweet_filters_numeric_user_ids(self):
        """Test that numeric user IDs are filtered from tagged_accounts, retweeted_user, quoted_user, and in_reply_to_user."""
        provider = RapidAPIProvider(api_key="test_key")
        
        # Test 1: Regular tweet with tagged accounts (not a retweet)
        regular_tweet = {
            'rest_id': '123456789',
            'legacy': {
                'full_text': 'Hello @validuser and @911245230426525697 and @1098881129057112064',
                'created_at': 'Mon Jan 15 12:00:00 +0000 2024',
                'entities': {
                    'user_mentions': [
                        {'screen_name': 'validuser'},
                        {'screen_name': '911245230426525697'},  # Should be filtered
                        {'screen_name': '1098881129057112064'},  # Should be filtered
                    ]
                },
                'in_reply_to_screen_name': '555444333222111',  # Numeric ID - should be filtered
            },
            'core': {
                'user_results': {
                    'result': {
                        'legacy': {'screen_name': 'testuser'}
                    }
                }
            }
        }
        
        tweet = provider._parse_tweet(regular_tweet, 'testuser')
        
        assert tweet is not None
        # tagged_accounts should only contain valid usernames
        assert 'validuser' in tweet['tagged_accounts']
        assert '911245230426525697' not in tweet['tagged_accounts']
        assert '1098881129057112064' not in tweet['tagged_accounts']
        assert len(tweet['tagged_accounts']) == 1
        
        # in_reply_to_user should be None (numeric ID filtered)
        assert tweet['in_reply_to_user'] is None
        
        # Test 2: Retweet with numeric ID (should be filtered)
        retweet_data = {
            'rest_id': '987654321',
            'legacy': {
                'full_text': 'RT @911245230426525697: Test',  # Numeric ID in RT
                'created_at': 'Mon Jan 15 12:00:00 +0000 2024',
            },
            'core': {
                'user_results': {
                    'result': {
                        'legacy': {'screen_name': 'testuser'}
                    }
                }
            }
        }
        
        retweet = provider._parse_tweet(retweet_data, 'testuser')
        assert retweet is not None
        # retweeted_user should be None (numeric ID filtered from RT @xxx pattern)
        assert retweet['retweeted_user'] is None
    
    @mock.patch('requests.get')
    def test_get_retweeters_filters_numeric_ids(self, mock_get):
        """Test that numeric user IDs are filtered from retweeters list."""
        provider = RapidAPIProvider(api_key="test_key")
        
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'data': {
                'retweeters_timeline': {
                    'timeline': {
                        'instructions': [{
                            '__typename': 'TimelineAddEntries',
                            'entries': [
                                {
                                    'entry_id': 'user-validuser1',
                                    'content': {
                                        'itemContent': {
                                            'user_results': {
                                                'result': {
                                                    'legacy': {'screen_name': 'validuser1'}
                                                }
                                            }
                                        }
                                    }
                                },
                                {
                                    'entry_id': 'user-911245230426525697',
                                    'content': {
                                        'itemContent': {
                                            'user_results': {
                                                'result': {
                                                    'legacy': {'screen_name': '911245230426525697'}  # Should be filtered
                                                }
                                            }
                                        }
                                    }
                                },
                                {
                                    'entry_id': 'user-validuser2',
                                    'content': {
                                        'itemContent': {
                                            'user_results': {
                                                'result': {
                                                    'legacy': {'screen_name': 'validuser2'}
                                                }
                                            }
                                        }
                                    }
                                },
                            ]
                        }]
                    }
                }
            }
        }
        mock_get.return_value = mock_response
        
        usernames, success = provider.get_retweeters("123456789")
        
        assert success is True
        # Should only have 2 valid usernames (numeric ID filtered out)
        assert len(usernames) == 2
        assert 'validuser1' in usernames
        assert 'validuser2' in usernames
        assert '911245230426525697' not in usernames
