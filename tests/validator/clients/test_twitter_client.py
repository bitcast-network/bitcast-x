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
                            'views': {
                                'count': '12345',
                                'state': 'EnabledWithCount'
                            },
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
        assert tweet['view_count'] == 12345
    
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
        assert tweet['view_count'] == 0
    
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
        from datetime import datetime, timezone
        
        # Mock no cache
        mock_get_cache.return_value = None
        
        # Use recent date to pass cutoff filter (Bug #3 fix requires recent tweets)
        recent_date = datetime.now(timezone.utc).strftime('%a %b %d %H:%M:%S +0000 %Y')
        
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
                                                                'created_at': recent_date,
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
                                                                'created_at': recent_date,
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
        
        # Author validation is always enabled - only tweets from 'testuser' are returned
        result = client.fetch_user_tweets("testuser")
        
        # Should only include tweet from testuser
        assert len(result['tweets']) == 1
        assert result['tweets'][0]['text'] == 'Tweet from testuser'
        assert result['tweets'][0]['author'] == 'testuser'
    
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('requests.get')
    def test_author_validation_backward_compat(self, mock_get, mock_get_cache, mock_cache):
        """Test that tweets without author field get author set."""
        from datetime import datetime, timedelta, timezone
        
        # Use recent date to pass cutoff filter (Bug #3 fix requires recent tweets)
        recent_date = datetime.now(timezone.utc).strftime('%a %b %d %H:%M:%S +0000 %Y')
        
        # Mock cached data without author field (old cache format)
        # Cache is recent enough to be used
        mock_get_cache.return_value = {
            'user_info': {'username': 'testuser', 'followers_count': 100},
            'tweets': [
                {
                    'tweet_id': '1',
                    'text': 'Old cached tweet',
                    'created_at': recent_date
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
    
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('requests.get')
    def test_dual_endpoint_fetching(self, mock_get, mock_get_cache, mock_cache):
        """Test that dual endpoint fetching works and deduplicates correctly."""
        from datetime import datetime, timezone
        
        # Mock no cache
        mock_get_cache.return_value = None
        
        # Use recent date to pass cutoff filter
        recent_date = datetime.now(timezone.utc).strftime('%a %b %d %H:%M:%S +0000 %Y')
        
        # Mock responses from both endpoints
        # First endpoint (/tweetsandreplies) returns tweets 1 and 2
        mock_response_1 = mock.Mock()
        mock_response_1.status_code = 200
        mock_response_1.json.return_value = {
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
                                                                'created_at': recent_date,
                                                                'full_text': 'Tweet from tweetsandreplies',
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
                                                                            'screen_name': 'testuser'
                                                                        }
                                                                    }
                                                                }
                                                            },
                                                            'legacy': {
                                                                'created_at': recent_date,
                                                                'full_text': 'Shared tweet in both endpoints',
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
        
        # Second endpoint (/tweets) returns tweets 2 (duplicate) and 3 (unique)
        mock_response_2 = mock.Mock()
        mock_response_2.status_code = 200
        mock_response_2.json.return_value = {
            'data': {
                'user': {
                    'result': {
                        'timeline': {
                            'timeline': {
                                'instructions': [{
                                    'type': 'TimelineAddEntries',
                                    'entries': [
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
                                                                            'screen_name': 'testuser'
                                                                        }
                                                                    }
                                                                }
                                                            },
                                                            'legacy': {
                                                                'created_at': recent_date,
                                                                'full_text': 'Shared tweet in both endpoints',
                                                                'is_quote_status': False
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        },
                                        {
                                            'entryId': 'tweet-3',
                                            'content': {
                                                'itemContent': {
                                                    'tweet_results': {
                                                        'result': {
                                                            'rest_id': '3',
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
                                                                'created_at': recent_date,
                                                                'full_text': 'Tweet unique to tweets endpoint',
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
        
        # Return different responses based on URL
        def mock_get_side_effect(*args, **kwargs):
            url = args[0]
            if 'tweetsandreplies' in url:
                return mock_response_1
            elif 'tweets' in url:
                return mock_response_2
            return mock_response_1
        
        mock_get.side_effect = mock_get_side_effect
        
        # Explicitly request dual-endpoint mode for this test
        client = TwitterClient(api_key="test", posts_only=False)
        
        # Test dual endpoint fetching
        with mock.patch('time.sleep'):  # Skip rate limiting delay
            result = client.fetch_user_tweets("testuser")
        
        # Should have 3 unique tweets (1 from first endpoint, 2 shared, 3 from second)
        # Deduplication should remove the duplicate tweet 2
        assert len(result['tweets']) == 3
        
        # Verify all three unique tweets are present
        tweet_ids = {tweet['tweet_id'] for tweet in result['tweets']}
        assert tweet_ids == {'1', '2', '3'}
        
        # Verify both endpoints were called
        assert mock_get.call_count == 2
    
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('requests.get')
    def test_handles_none_author_gracefully(self, mock_get, mock_get_cache, mock_cache):
        """Test that tweets with None author don't cause crashes during validation."""
        from datetime import datetime, timezone
        
        # Mock no cache
        mock_get_cache.return_value = None
        
        # Use recent date to pass cutoff filter
        recent_date = datetime.now(timezone.utc).strftime('%a %b %d %H:%M:%S +0000 %Y')
        
        # Mock response with tweet that has no author info (None)
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
                                                            # Note: No 'core' field, so author extraction fails → author = None
                                                            'legacy': {
                                                                'created_at': recent_date,
                                                                'full_text': 'Tweet with no author info',
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
        
        # This should NOT crash with "'NoneType' object has no attribute 'lower'"
        with mock.patch('time.sleep'):
            result = client.fetch_user_tweets("testuser")
        
        # With dual endpoint mode, /tweets will default author to 'testuser'
        # So tweet should be included with defaulted author
        assert len(result['tweets']) == 1
        assert result['tweets'][0]['author'] == 'testuser'
    
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('requests.get')
    def test_tweets_endpoint_defaults_author_when_missing(self, mock_get, mock_get_cache, mock_cache):
        """Test that /tweets endpoint defaults author when core field is missing."""
        from datetime import datetime, timezone
        
        # Mock no cache
        mock_get_cache.return_value = None
        
        # Use recent date to pass cutoff filter
        recent_date = datetime.now(timezone.utc).strftime('%a %b %d %H:%M:%S +0000 %Y')
        
        # Mock responses - /tweets has no core, /tweetsandreplies has core
        tweets_response = mock.Mock()
        tweets_response.status_code = 200
        tweets_response.json.return_value = {
            'data': {
                'user': {
                    'result': {
                        'timeline': {
                            'timeline': {
                                'instructions': [{
                                    'type': 'TimelineAddEntries',
                                    'entries': [{
                                        'entryId': 'tweet-1',
                                        'content': {
                                            'itemContent': {
                                                'tweet_results': {
                                                    'result': {
                                                        'rest_id': '1',
                                                        # No 'core' field - simulates /tweets endpoint structure
                                                        'legacy': {
                                                            'created_at': recent_date,
                                                            'full_text': 'Tweet from /tweets endpoint',
                                                            'is_quote_status': False
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }]
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
        
        tweetsandreplies_response = mock.Mock()
        tweetsandreplies_response.status_code = 200
        tweetsandreplies_response.json.return_value = {
            'data': {
                'user': {
                    'result': {
                        'timeline': {
                            'timeline': {
                                'instructions': [{
                                    'type': 'TimelineAddEntries',
                                    'entries': []
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
        
        def mock_get_side_effect(*args, **kwargs):
            url = args[0]
            if '/user/tweets' in url and '/tweetsandreplies' not in url:
                return tweets_response
            else:
                return tweetsandreplies_response
        
        mock_get.side_effect = mock_get_side_effect
        
        # Explicitly request dual-endpoint mode for this test
        client = TwitterClient(api_key="test", posts_only=False)
        
        with mock.patch('time.sleep'):
            result = client.fetch_user_tweets("testuser")
        
        # Should have 1 tweet with author defaulted to 'testuser'
        assert len(result['tweets']) == 1
        assert result['tweets'][0]['author'] == 'testuser'
        assert result['tweets'][0]['text'] == 'Tweet from /tweets endpoint'
    
    @mock.patch('bitcast.validator.clients.twitter_client.cache_user_tweets')
    @mock.patch('bitcast.validator.clients.twitter_client.get_cached_user_tweets')
    @mock.patch('requests.get')
    def test_user_info_uses_requested_username_not_tweet_author(self, mock_get, mock_get_cache, mock_cache):
        """Test that user_info always uses requested username, not tweet author.
        
        Regression test for bug where profile-conversation entries starting with
        tweets from other users would cause wrong username to be cached.
        """
        from datetime import datetime, timezone
        
        # Mock no cache
        mock_get_cache.return_value = None
        
        recent_date = datetime.now(timezone.utc).strftime('%a %b %d %H:%M:%S +0000 %Y')
        
        # Mock tweetsandreplies endpoint with profile-conversation starting with reply FROM another user
        tweetsandreplies_response = mock.Mock()
        tweetsandreplies_response.status_code = 200
        tweetsandreplies_response.json.return_value = {
            'data': {
                'user': {
                    'result': {
                        'timeline': {
                            'timeline': {
                                'instructions': [{
                                    'type': 'TimelineAddEntries',
                                    'entries': [{
                                        'entryId': 'profile-conversation-123',
                                        'content': {
                                            'entryType': 'TimelineTimelineModule',
                                            'items': [
                                                # First tweet in conversation is FROM louisebeattie (not mogmachine)
                                                {
                                                    'entryId': 'profile-conversation-123-tweet-1',
                                                    'item': {
                                                        'itemContent': {
                                                            'tweet_results': {
                                                                'result': {
                                                                    'rest_id': '1',
                                                                    'core': {
                                                                        'user_results': {
                                                                            'result': {
                                                                                'legacy': {
                                                                                    'screen_name': 'louisebeattie',
                                                                                    'followers_count': 8000
                                                                                }
                                                                            }
                                                                        }
                                                                    },
                                                                    'legacy': {
                                                                        'created_at': recent_date,
                                                                        'full_text': 'Reply to mogmachine',
                                                                        'is_quote_status': False,
                                                                        'in_reply_to_screen_name': 'mogmachine'
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    }
                                                },
                                                # Second tweet is FROM mogmachine
                                                {
                                                    'entryId': 'profile-conversation-123-tweet-2',
                                                    'item': {
                                                        'itemContent': {
                                                            'tweet_results': {
                                                                'result': {
                                                                    'rest_id': '2',
                                                                    'core': {
                                                                        'user_results': {
                                                                            'result': {
                                                                                'legacy': {
                                                                                    'screen_name': 'mogmachine',
                                                                                    'followers_count': 5000
                                                                                }
                                                                            }
                                                                        }
                                                                    },
                                                                    'legacy': {
                                                                        'created_at': recent_date,
                                                                        'full_text': 'Response from mogmachine',
                                                                        'is_quote_status': False
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    }
                                                }
                                            ]
                                        }
                                    }]
                                }]
                            }
                        }
                    }
                }
            }
        }
        
        # Mock tweets endpoint (empty for this test)
        tweets_response = mock.Mock()
        tweets_response.status_code = 200
        tweets_response.json.return_value = {
            'data': {
                'user': {
                    'result': {
                        'timeline': {
                            'timeline': {
                                'instructions': [{
                                    'type': 'TimelineAddEntries',
                                    'entries': []
                                }]
                            }
                        }
                    }
                }
            }
        }
        
        def mock_get_side_effect(*args, **kwargs):
            url = args[0]
            if '/tweetsandreplies' in url:
                return tweetsandreplies_response
            else:
                return tweets_response
        
        mock_get.side_effect = mock_get_side_effect
        
        # Explicitly request dual-endpoint mode for this test
        client = TwitterClient(api_key="test", posts_only=False)
        
        with mock.patch('time.sleep'):
            result = client.fetch_user_tweets("mogmachine")
        
        # CRITICAL: user_info username must be 'mogmachine' (requested username)
        # NOT 'louisebeattie' (first tweet author in profile-conversation)
        assert result['user_info']['username'] == 'mogmachine', \
            "user_info username should match requested username, not tweet author"
        
        # Should only have mogmachine's tweet (louisebeattie's filtered out)
        assert len(result['tweets']) == 1
        assert result['tweets'][0]['author'] == 'mogmachine'
        assert result['tweets'][0]['text'] == 'Response from mogmachine'
        
        # Followers count is extracted when available from timeline owner's tweets
        # In profile-conversations it may not be easily accessible, which is acceptable
        assert result['user_info']['followers_count'] >= 0

