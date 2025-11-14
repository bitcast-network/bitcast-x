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

