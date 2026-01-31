"""
Tests for views_count preservation through the reward snapshot cycle.

This test verifies that views_count is properly saved in reward snapshots
and restored when loading snapshots for subsequent emission runs.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch
from bitcast.validator.reward_engine.twitter_evaluator import TwitterEvaluator
from bitcast.validator.reward_engine.utils import save_reward_snapshot, load_reward_snapshot
from pathlib import Path
import json
import tempfile
import shutil


class TestViewsCountPreservation:
    """Test suite for views_count preservation bug fix."""
    
    def test_snapshot_includes_views_count(self):
        """Test that reward snapshot includes views_count field."""
        # Create test snapshot data
        brief_id = "test_brief_001"
        pool_name = "test_pool"
        
        snapshot_data = {
            'brief_id': brief_id,
            'pool_name': pool_name,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'tweet_rewards': [
                {
                    'tweet_id': '1234567890',
                    'author': 'test_user',
                    'uid': 1,
                    'score': 0.5,
                    'total_usd': 10.0,
                    'text': 'Test tweet',
                    'favorite_count': 5,
                    'retweet_count': 2,
                    'reply_count': 1,
                    'quote_count': 0,
                    'bookmark_count': 3,
                    'views_count': 1234,  # This should be preserved
                    'retweets': [],
                    'quotes': [],
                    'created_at': 'Wed Jan 01 12:00:00 +0000 2026',
                    'lang': 'en'
                },
                {
                    'tweet_id': '0987654321',
                    'author': 'another_user',
                    'uid': 2,
                    'score': 0.3,
                    'total_usd': 5.0,
                    'text': 'Another tweet',
                    'favorite_count': 10,
                    'retweet_count': 4,
                    'reply_count': 2,
                    'quote_count': 1,
                    'bookmark_count': 0,
                    'views_count': 5678,  # This should be preserved
                    'retweets': [],
                    'quotes': [],
                    'created_at': 'Wed Jan 02 12:00:00 +0000 2026',
                    'lang': 'en'
                }
            ]
        }
        
        # Save and load snapshot
        snapshot_file = save_reward_snapshot(brief_id, pool_name, snapshot_data)
        
        try:
            loaded_data, loaded_file = load_reward_snapshot(brief_id, pool_name)
            
            # Verify views_count is preserved
            assert 'tweet_rewards' in loaded_data
            assert len(loaded_data['tweet_rewards']) == 2
            
            # Check first tweet
            tweet1 = loaded_data['tweet_rewards'][0]
            assert 'views_count' in tweet1, "views_count field missing from snapshot"
            assert tweet1['views_count'] == 1234, f"Expected views_count=1234, got {tweet1.get('views_count')}"
            
            # Check second tweet
            tweet2 = loaded_data['tweet_rewards'][1]
            assert 'views_count' in tweet2, "views_count field missing from snapshot"
            assert tweet2['views_count'] == 5678, f"Expected views_count=5678, got {tweet2.get('views_count')}"
            
        finally:
            # Cleanup
            if Path(snapshot_file).exists():
                Path(snapshot_file).unlink()
                # Also try to remove parent directory if empty
                parent_dir = Path(snapshot_file).parent
                try:
                    parent_dir.rmdir()
                except OSError:
                    pass  # Directory not empty, that's fine
    
    @patch('bitcast.validator.reward_engine.twitter_evaluator.get_bitcast_alpha_price', return_value=1.0)
    def test_convert_snapshot_to_tweets_with_targets_preserves_views(self, mock_price):
        """Test that _convert_snapshot_to_tweets_with_targets preserves views_count."""
        evaluator = TwitterEvaluator()
        
        tweet_rewards = [
            {
                'tweet_id': '1111111111',
                'author': 'user1',
                'uid': 1,
                'score': 0.8,
                'total_usd': 20.0,
                'text': 'Test tweet 1',
                'favorite_count': 15,
                'retweet_count': 5,
                'reply_count': 3,
                'quote_count': 2,
                'bookmark_count': 7,
                'views_count': 9999,  # Should be preserved
                'retweets': [],
                'quotes': [],
                'created_at': 'Thu Jan 03 12:00:00 +0000 2026',
                'lang': 'en'
            },
            {
                'tweet_id': '2222222222',
                'author': 'user2',
                'uid': 2,
                'score': 0.4,
                'total_usd': 8.0,
                'text': 'Test tweet 2',
                'favorite_count': 8,
                'retweet_count': 2,
                'reply_count': 1,
                'quote_count': 0,
                'bookmark_count': 4,
                'views_count': 3333,  # Should be preserved
                'retweets': [],
                'quotes': [],
                'created_at': 'Thu Jan 04 12:00:00 +0000 2026',
                'lang': 'en'
            }
        ]
        
        # Convert snapshot to tweets_with_targets format
        tweets_with_targets = evaluator._convert_snapshot_to_tweets_with_targets(tweet_rewards)
        
        # Verify views_count is preserved in conversion
        assert len(tweets_with_targets) == 2
        
        tweet1 = tweets_with_targets[0]
        assert 'views_count' in tweet1, "views_count missing after snapshot conversion"
        assert tweet1['views_count'] == 9999, f"Expected views_count=9999, got {tweet1.get('views_count')}"
        
        tweet2 = tweets_with_targets[1]
        assert 'views_count' in tweet2, "views_count missing after snapshot conversion"
        assert tweet2['views_count'] == 3333, f"Expected views_count=3333, got {tweet2.get('views_count')}"
    
