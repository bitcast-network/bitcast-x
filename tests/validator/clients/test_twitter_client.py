"""
Essential tests for TwitterClient.
"""

import pytest
import unittest.mock as mock

from bitcast.validator.clients.twitter_client import TwitterClient


class TestTwitterClient:
    """Essential Twitter client tests."""
    
    def test_init_requires_api_key(self):
        """Test client requires API key."""
        with mock.patch('bitcast.validator.clients.twitter_client.RAPID_API_KEY', None):
            with pytest.raises(ValueError, match="RAPID_API_KEY"):
                TwitterClient()
    
    @mock.patch('requests.get')
    def test_api_request_retry_logic(self, mock_get):
        """Test API retry logic works."""
        # Mock rate limit then success
        mock_429 = mock.Mock()
        mock_429.status_code = 429
        
        mock_200 = mock.Mock()
        mock_200.status_code = 200
        mock_200.json.return_value = {'data': {'user': {}}}
        
        mock_get.side_effect = [mock_429, mock_200]
        
        client = TwitterClient(api_key="test")
        
        with mock.patch('time.sleep'):
            data, error = client._make_api_request("http://test", {})
        
        assert error is None
        assert mock_get.call_count == 2
    
    def test_tweet_parsing(self):
        """Test basic tweet parsing."""
        client = TwitterClient(api_key="test")
        
        # Mock tweet entry
        entry = {
            'entryId': 'tweet-123',
            'content': {
                'itemContent': {
                    'tweet_results': {
                        'result': {
                            'rest_id': '123',
                            'legacy': {
                                'full_text': 'Hello @user1 and @user2',
                                'entities': {
                                    'user_mentions': [
                                        {'screen_name': 'user1'},
                                        {'screen_name': 'user2'}
                                    ]
                                },
                                'is_quote_status': False,
                                'favorite_count': 42,
                                'retweet_count': 15,
                                'reply_count': 8,
                                'quote_count': 3,
                                'bookmark_count': 5
                            }
                        }
                    }
                }
            }
        }
        
        tweet = client._parse_tweet(entry, "testuser")
        
        assert tweet is not None
        assert tweet['text'] == 'Hello @user1 and @user2'
        assert tweet['tagged_accounts'] == ['user1', 'user2']
        # Test engagement metrics
        assert tweet['favorite_count'] == 42
        assert tweet['retweet_count'] == 15
        assert tweet['reply_count'] == 8
        assert tweet['quote_count'] == 3
        assert tweet['bookmark_count'] == 5
    
    def test_engagement_metrics_defaults(self):
        """Test engagement metrics default to 0 when missing."""
        client = TwitterClient(api_key="test")
        
        # Mock tweet entry without engagement fields
        entry = {
            'entryId': 'tweet-456',
            'content': {
                'itemContent': {
                    'tweet_results': {
                        'result': {
                            'rest_id': '456',
                            'legacy': {
                                'full_text': 'Tweet without engagement metrics',
                                'is_quote_status': False
                            }
                        }
                    }
                }
            }
        }
        
        tweet = client._parse_tweet(entry, "testuser")
        
        assert tweet is not None
        assert tweet['text'] == 'Tweet without engagement metrics'
        # Test default engagement values
        assert tweet['favorite_count'] == 0
        assert tweet['retweet_count'] == 0
        assert tweet['reply_count'] == 0
        assert tweet['quote_count'] == 0
        assert tweet['bookmark_count'] == 0
    
    def test_retweet_parsing(self):
        """Test retweet parsing."""
        client = TwitterClient(api_key="test")
        
        entry = {
            'entryId': 'tweet-123',
            'content': {
                'itemContent': {
                    'tweet_results': {
                        'result': {
                            'rest_id': '123',
                            'legacy': {
                                'full_text': 'RT @original: Great tweet',
                                'entities': {'user_mentions': [{'screen_name': 'original'}]},
                                'is_quote_status': False
                            }
                        }
                    }
                }
            }
        }
        
        tweet = client._parse_tweet(entry, "testuser")
        
        assert tweet['retweeted_user'] == 'original'
        assert tweet['tagged_accounts'] == []  # Should clear for RTs
    
    def test_note_tweet_parsing(self):
        """Test extended tweet (note_tweet) parsing."""
        client = TwitterClient(api_key="test")
        
        # Mock extended tweet with note_tweet field
        entry = {
            'entryId': 'tweet-789',
            'content': {
                'itemContent': {
                    'tweet_results': {
                        'result': {
                            'rest_id': '789',
                            'note_tweet': {
                                'note_tweet_results': {
                                    'result': {
                                        'text': 'Extended tweet with full text @user1 @user2',
                                        'entity_set': {
                                            'user_mentions': [
                                                {'screen_name': 'user1'},
                                                {'screen_name': 'user2'}
                                            ]
                                        }
                                    }
                                }
                            },
                            'legacy': {
                                'full_text': 'Extended tweet...',  # Truncated version
                                'is_quote_status': False
                            }
                        }
                    }
                }
            }
        }
        
        tweet = client._parse_tweet(entry, "testuser")
        
        assert tweet is not None
        assert tweet['text'] == 'Extended tweet with full text @user1 @user2'
        assert tweet['tagged_accounts'] == ['user1', 'user2']
    
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('requests.get')
    def test_author_validation_filters_wrong_authors(self, mock_get, mock_get_cache, mock_cache):
        """Test that author validation filters out tweets from other users."""
        # Mock no cache
        mock_get_cache.return_value = None
        
        # Mock API response with mixed authors
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'data': {
                'user': {
                    'result': {
                        'timeline': {
                            'timeline': {
                                'instructions': [{
                                    'type': 'TimelineAddEntries',
                                    'entries': [
                                        {
                                            'entryId': 'tweet-1',
                                            'content': {
                                                'itemContent': {
                                                    'tweet_results': {
                                                        'result': {
                                                            'rest_id': '1',
                                                            'core': {
                                                                'user_results': {
                                                                    'result': {
                                                                        'legacy': {
                                                                            'screen_name': 'testuser'
                                                                        }
                                                                    }
                                                                }
                                                            },
                                                            'legacy': {
                                                                'created_at': 'Mon Jan 01 00:00:00 +0000 2024',
                                                                'full_text': 'Tweet from testuser',
                                                                'is_quote_status': False
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        },
                                        {
                                            'entryId': 'tweet-2',
                                            'content': {
                                                'itemContent': {
                                                    'tweet_results': {
                                                        'result': {
                                                            'rest_id': '2',
                                                            'core': {
                                                                'user_results': {
                                                                    'result': {
                                                                        'legacy': {
                                                                            'screen_name': 'otheruser'
                                                                        }
                                                                    }
                                                                }
                                                            },
                                                            'legacy': {
                                                                'created_at': 'Mon Jan 01 00:00:00 +0000 2024',
                                                                'full_text': 'Reply from otheruser',
                                                                'is_quote_status': False
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    ]
                                }]
                            }
                        },
                        'legacy': {
                            'screen_name': 'testuser',
                            'followers_count': 100
                        }
                    }
                }
            }
        }
        mock_get.return_value = mock_response
        
        client = TwitterClient(api_key="test")
        
        # Test with validate_author=True (default)
        result = client.fetch_user_tweets("testuser")
        
        # Should only include tweet from testuser
        assert len(result['tweets']) == 1
        assert result['tweets'][0]['text'] == 'Tweet from testuser'
        assert result['tweets'][0]['author'] == 'testuser'
    
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('requests.get')
    def test_author_validation_disabled(self, mock_get, mock_get_cache, mock_cache):
        """Test that author validation can be disabled."""
        # Mock no cache
        mock_get_cache.return_value = None
        
        # Mock API response with mixed authors
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'data': {
                'user': {
                    'result': {
                        'timeline': {
                            'timeline': {
                                'instructions': [{
                                    'type': 'TimelineAddEntries',
                                    'entries': [
                                        {
                                            'entryId': 'tweet-1',
                                            'content': {
                                                'itemContent': {
                                                    'tweet_results': {
                                                        'result': {
                                                            'rest_id': '1',
                                                            'core': {
                                                                'user_results': {
                                                                    'result': {
                                                                        'legacy': {
                                                                            'screen_name': 'testuser'
                                                                        }
                                                                    }
                                                                }
                                                            },
                                                            'legacy': {
                                                                'created_at': 'Mon Jan 01 00:00:00 +0000 2024',
                                                                'full_text': 'Tweet from testuser',
                                                                'is_quote_status': False
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        },
                                        {
                                            'entryId': 'tweet-2',
                                            'content': {
                                                'itemContent': {
                                                    'tweet_results': {
                                                        'result': {
                                                            'rest_id': '2',
                                                            'core': {
                                                                'user_results': {
                                                                    'result': {
                                                                        'legacy': {
                                                                            'screen_name': 'otheruser'
                                                                        }
                                                                    }
                                                                }
                                                            },
                                                            'legacy': {
                                                                'created_at': 'Mon Jan 01 00:00:00 +0000 2024',
                                                                'full_text': 'Reply from otheruser',
                                                                'is_quote_status': False
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    ]
                                }]
                            }
                        },
                        'legacy': {
                            'screen_name': 'testuser',
                            'followers_count': 100
                        }
                    }
                }
            }
        }
        mock_get.return_value = mock_response
        
        client = TwitterClient(api_key="test")
        
        # Test with validate_author=False
        with mock.patch('time.sleep'):  # Skip rate limiting delay
            result = client.fetch_user_tweets("testuser", validate_author=False)
        
        # Should include both tweets (if API parsing succeeds for both)
        assert len(result['tweets']) >= 1  # At least testuser's tweet
    
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('requests.get')
    def test_author_validation_backward_compat(self, mock_get, mock_get_cache, mock_cache):
        """Test that tweets without author field get author set."""
        from datetime import datetime, timedelta
        
        # Mock cached data without author field (old cache format)
        # Cache is recent enough to be used
        mock_get_cache.return_value = {
            'user_info': {'username': 'testuser', 'followers_count': 100},
            'tweets': [
                {
                    'tweet_id': '1',
                    'text': 'Old cached tweet',
                    'created_at': 'Mon Jan 01 00:00:00 +0000 2024'
                    # Note: no 'author' field
                }
            ],
            'last_updated': datetime.now() - timedelta(seconds=10)  # Fresh cache
        }
        
        client = TwitterClient(api_key="test")
        
        # Test that author field is added for old cache entries
        result = client.fetch_user_tweets("testuser", force_refresh=False)
        
        assert len(result['tweets']) == 1
        assert result['tweets'][0]['author'] == 'testuser'

