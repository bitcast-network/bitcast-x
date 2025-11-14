"""Tests for tweet filter orchestrator."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from bitcast.validator.tweet_filtering.tweet_filter import (
    save_filtered_tweets,
    filter_tweets_for_brief,
    apply_max_tweets_filter
)


@pytest.fixture
def sample_scored_data():
    """Sample scored tweets data."""
    return {
        'metadata': {
            'run_id': 'scoring_run_123',
            'brief_id': 'test_brief',
            'created_at': '2025-10-30T12:00:00',
            'pool_name': 'tao'
        },
        'scored_tweets': [
            {
                'tweet_id': '123',
                'author': 'user1',
                'text': 'Great tweet about @bitcast_network',
                'score': 0.5
            },
            {
                'tweet_id': '456',
                'author': 'user2',
                'text': 'Another relevant tweet',
                'score': 0.3
            }
        ]
    }


@pytest.fixture
def sample_evaluated_tweets():
    """Sample evaluated tweets."""
    return [
        {
            'tweet_id': '123',
            'author': 'user1',
            'text': 'Great tweet about @bitcast_network',
            'score': 0.5,
            'meets_brief': True,
            'reasoning': 'Mentions BitCast correctly'
        },
        {
            'tweet_id': '456',
            'author': 'user2',
            'text': 'Another relevant tweet',
            'score': 0.3,
            'meets_brief': False,
            'reasoning': 'Does not mention required tag'
        }
    ]


class TestSaveFilteredTweets:
    """Test saving filtered tweets to disk."""
    
    def test_saves_to_correct_location(self, tmp_path, sample_evaluated_tweets, monkeypatch):
        """Should save filtered tweets to correct directory."""
        # Mock __file__ to use temp directory
        mock_file = tmp_path / "tweet_filtering" / "tweet_filter.py"
        mock_file.parent.mkdir(parents=True)
        mock_file.touch()
        
        from bitcast.validator.tweet_filtering import tweet_filter
        monkeypatch.setattr(tweet_filter, '__file__', str(mock_file))
        
        passed = [t for t in sample_evaluated_tweets if t['meets_brief']]
        failed = [t for t in sample_evaluated_tweets if not t['meets_brief']]
        
        metadata = {
            'run_id': 'test_run',
            'brief_id': 'test_brief',
            'total_evaluated': 2,
            'passed_count': 1,
            'failed_count': 1
        }
        
        output_path = save_filtered_tweets(
            'test_brief',
            sample_evaluated_tweets,
            passed,
            failed,
            metadata
        )
        
        # Should create file
        assert Path(output_path).exists()
        
        # Should be in filtered_tweets directory
        assert 'filtered_tweets' in output_path
        assert 'test_brief' in output_path
        
        # Should contain all data
        with open(output_path) as f:
            data = json.load(f)
        
        assert data['metadata'] == metadata
        assert len(data['filtered_tweets']) == 2
        assert len(data['passed_tweets']) == 1
        assert len(data['failed_tweets']) == 1
    
    def test_creates_directory_if_missing(self, tmp_path, monkeypatch):
        """Should create output directory if it doesn't exist."""
        mock_file = tmp_path / "tweet_filtering" / "tweet_filter.py"
        mock_file.parent.mkdir(parents=True)
        mock_file.touch()
        
        from bitcast.validator.tweet_filtering import tweet_filter
        monkeypatch.setattr(tweet_filter, '__file__', str(mock_file))
        
        output_path = save_filtered_tweets('test', [], [], [], {})
        
        assert Path(output_path).parent.exists()


class TestFilterTweetsForBrief:
    """Test main filtering orchestrator."""
    
    @patch('bitcast.validator.tweet_filtering.tweet_filter.get_global_publisher')
    @patch('bitcast.validator.tweet_filtering.tweet_filter.load_latest_scored_tweets')
    @patch('bitcast.validator.tweet_filtering.tweet_filter.BriefEvaluator')
    @patch('bitcast.validator.tweet_filtering.tweet_filter.save_filtered_tweets')
    def test_end_to_end_filtering(
        self,
        mock_save,
        mock_evaluator_class,
        mock_load,
        mock_publisher,
        sample_scored_data,
        sample_evaluated_tweets
    ):
        """Should run complete filtering pipeline."""
        # Setup mocks
        mock_load.return_value = (sample_scored_data, '/path/to/scored.json')
        
        mock_evaluator = MagicMock()
        mock_evaluator.evaluate_tweets_batch.return_value = sample_evaluated_tweets
        mock_evaluator_class.return_value = mock_evaluator
        
        mock_save.return_value = '/path/to/filtered.json'
        
        # Mock publisher (may not be initialized)
        mock_publisher.side_effect = RuntimeError("Not initialized")
        
        # Run filtering
        results = filter_tweets_for_brief(
            brief_id='test_brief',
            brief_text='Talk about BitCast and tag @bitcast_network',
            prompt_version=1
        )
        
        # Should load scored tweets
        mock_load.assert_called_once_with('test_brief')
        
        # Should create evaluator with correct brief
        mock_evaluator_class.assert_called_once()
        brief_arg = mock_evaluator_class.call_args[0][0]
        assert brief_arg['id'] == 'test_brief'
        assert brief_arg['brief'] == 'Talk about BitCast and tag @bitcast_network'
        assert brief_arg['prompt_version'] == 1
        
        # Should evaluate tweets
        mock_evaluator.evaluate_tweets_batch.assert_called_once()
        
        # Should save results
        mock_save.assert_called_once()
        
        # Should return full tweet results (not simplified)
        assert len(results) == 2
        # Results now include all tweet fields, not just author/tweet_id/meets_brief
        assert results[0]['author'] == 'user1'
        assert results[0]['tweet_id'] == '123'
        assert results[0]['meets_brief'] is True
        assert results[1]['author'] == 'user2'
        assert results[1]['tweet_id'] == '456'
        assert results[1]['meets_brief'] is False
    
    @patch('bitcast.validator.tweet_filtering.tweet_filter.get_global_publisher')
    @patch('bitcast.validator.tweet_filtering.tweet_filter.load_latest_scored_tweets')
    @patch('bitcast.validator.tweet_filtering.tweet_filter.save_filtered_tweets')
    def test_handles_empty_scored_tweets(
        self,
        mock_save,
        mock_load,
        mock_publisher
    ):
        """Should handle case where scored tweets list is empty."""
        # Setup mocks
        empty_data = {
            'metadata': {'run_id': 'test', 'brief_id': 'test', 'created_at': 'test'},
            'scored_tweets': []
        }
        mock_load.return_value = (empty_data, '/path/to/scored.json')
        mock_save.return_value = '/path/to/filtered.json'
        mock_publisher.side_effect = RuntimeError("Not initialized")
        
        # Run filtering
        results = filter_tweets_for_brief(
            brief_id='test_brief',
            brief_text='Test brief'
        )
        
        # Should return empty results
        assert results == []
        
        # Should still save metadata
        mock_save.assert_called_once()
        save_args = mock_save.call_args
        assert save_args[0][1] == []  # filtered_tweets
        assert save_args[0][2] == []  # passed_tweets
        assert save_args[0][3] == []  # failed_tweets
    
    @patch('bitcast.validator.tweet_filtering.tweet_filter.get_global_publisher')
    @patch('bitcast.validator.tweet_filtering.tweet_filter.load_latest_scored_tweets')
    def test_raises_if_scored_tweets_not_found(
        self,
        mock_load,
        mock_publisher
    ):
        """Should raise if scored tweets don't exist."""
        mock_load.side_effect = FileNotFoundError("No scored tweets found")
        mock_publisher.side_effect = RuntimeError("Not initialized")
        
        with pytest.raises(FileNotFoundError, match="No scored tweets found"):
            filter_tweets_for_brief(
                brief_id='nonexistent_brief',
                brief_text='Test'
            )
    
    @patch('bitcast.validator.tweet_filtering.tweet_filter.get_global_publisher')
    @patch('bitcast.validator.tweet_filtering.tweet_filter.load_latest_scored_tweets')
    @patch('bitcast.validator.tweet_filtering.tweet_filter.BriefEvaluator')
    @patch('bitcast.validator.tweet_filtering.tweet_filter.save_filtered_tweets')
    def test_includes_metadata_in_output(
        self,
        mock_save,
        mock_evaluator_class,
        mock_load,
        mock_publisher,
        sample_scored_data,
        sample_evaluated_tweets
    ):
        """Should include comprehensive metadata in saved output."""
        # Setup mocks
        mock_load.return_value = (sample_scored_data, '/path/to/scored.json')
        
        mock_evaluator = MagicMock()
        mock_evaluator.evaluate_tweets_batch.return_value = sample_evaluated_tweets
        mock_evaluator_class.return_value = mock_evaluator
        
        mock_save.return_value = '/path/to/filtered.json'
        mock_publisher.side_effect = RuntimeError("Not initialized")
        
        # Run filtering
        filter_tweets_for_brief(
            brief_id='test_brief',
            brief_text='Test brief text',
            prompt_version=2
        )
        
        # Check metadata passed to save
        save_args = mock_save.call_args
        metadata = save_args[0][4]  # 5th argument is metadata
        
        assert metadata['brief_id'] == 'test_brief'
        assert metadata['brief_text'] == 'Test brief text'
        assert metadata['prompt_version'] == 2
        assert metadata['total_evaluated'] == 2
        assert metadata['passed_count'] == 1
        assert metadata['failed_count'] == 1
        assert metadata['pass_rate'] == 0.5
        assert 'run_id' in metadata
        assert 'created_at' in metadata
        assert 'execution_time_seconds' in metadata


class TestApplyMaxTweetsFilter:
    """Test apply_max_tweets_filter function."""
    
    def test_limits_accounts_with_too_many_tweets(self):
        """Test apply_max_tweets_filter limits accounts with too many tweets."""
        tweets = [
            # user1: 5 tweets with different scores
            {'author': 'user1', 'tweet_id': '1', 'score': 0.5, 'created_at': '2024-01-01'},
            {'author': 'user1', 'tweet_id': '2', 'score': 0.8, 'created_at': '2024-01-02'},
            {'author': 'user1', 'tweet_id': '3', 'score': 0.3, 'created_at': '2024-01-03'},
            {'author': 'user1', 'tweet_id': '4', 'score': 0.9, 'created_at': '2024-01-04'},
            {'author': 'user1', 'tweet_id': '5', 'score': 0.2, 'created_at': '2024-01-05'},
            # user2: 2 tweets (under limit)
            {'author': 'user2', 'tweet_id': '6', 'score': 0.6, 'created_at': '2024-01-01'},
            {'author': 'user2', 'tweet_id': '7', 'score': 0.4, 'created_at': '2024-01-02'},
        ]
        
        result = apply_max_tweets_filter(tweets, max_tweets=3, brief_id='test_brief')
        
        # Should have 5 tweets total: 3 from user1 + 2 from user2
        assert len(result) == 5
        
        # Get user1 tweets
        user1_tweets = [t for t in result if t['author'] == 'user1']
        assert len(user1_tweets) == 3
        
        # Should keep top 3 by score: 0.9, 0.8, 0.5 (not 0.3, 0.2)
        user1_scores = sorted([t['score'] for t in user1_tweets], reverse=True)
        assert user1_scores == [0.9, 0.8, 0.5]
        
        # user2 should have all tweets (under limit)
        user2_tweets = [t for t in result if t['author'] == 'user2']
        assert len(user2_tweets) == 2
    
    def test_no_limit_when_none_or_zero(self):
        """Test apply_max_tweets_filter with no limit (None or 0)."""
        tweets = [
            {'author': 'user1', 'tweet_id': '1', 'score': 0.5, 'created_at': '2024-01-01'},
            {'author': 'user1', 'tweet_id': '2', 'score': 0.8, 'created_at': '2024-01-02'},
            {'author': 'user1', 'tweet_id': '3', 'score': 0.3, 'created_at': '2024-01-03'},
        ]
        
        # Test with None
        result_none = apply_max_tweets_filter(tweets, max_tweets=None, brief_id='test_brief')
        assert len(result_none) == 3  # All tweets kept
        
        # Test with 0
        result_zero = apply_max_tweets_filter(tweets, max_tweets=0, brief_id='test_brief')
        assert len(result_zero) == 3  # All tweets kept
    
    def test_keeps_top_scores(self):
        """Test apply_max_tweets_filter keeps highest scoring tweets."""
        tweets = [
            {'author': 'user1', 'tweet_id': '1', 'score': 0.1, 'created_at': '2024-01-01'},
            {'author': 'user1', 'tweet_id': '2', 'score': 0.9, 'created_at': '2024-01-02'},
            {'author': 'user1', 'tweet_id': '3', 'score': 0.5, 'created_at': '2024-01-03'},
            {'author': 'user1', 'tweet_id': '4', 'score': 0.7, 'created_at': '2024-01-04'},
        ]
        
        result = apply_max_tweets_filter(tweets, max_tweets=2, brief_id='test_brief')
        
        # Should keep top 2: score 0.9 and 0.7
        assert len(result) == 2
        result_scores = sorted([t['score'] for t in result], reverse=True)
        assert result_scores == [0.9, 0.7]
        
        # Verify tweet IDs
        result_ids = sorted([t['tweet_id'] for t in result])
        assert result_ids == ['2', '4']
    
    def test_timestamp_tiebreaker(self):
        """Test apply_max_tweets_filter uses timestamp as tiebreaker for same scores."""
        tweets = [
            {'author': 'user1', 'tweet_id': '1', 'score': 0.8, 'created_at': '2024-01-03'},
            {'author': 'user1', 'tweet_id': '2', 'score': 0.8, 'created_at': '2024-01-01'},  # Earliest
            {'author': 'user1', 'tweet_id': '3', 'score': 0.8, 'created_at': '2024-01-02'},
        ]
        
        result = apply_max_tweets_filter(tweets, max_tweets=2, brief_id='test_brief')
        
        # Should keep 2 tweets with same score, earliest timestamps win
        assert len(result) == 2
        result_ids = sorted([t['tweet_id'] for t in result])
        assert result_ids == ['2', '3']  # Created 01-01 and 01-02, not 01-03
    
    def test_handles_empty_tweets(self):
        """Test apply_max_tweets_filter handles empty tweet list."""
        result = apply_max_tweets_filter([], max_tweets=3, brief_id='test_brief')
        assert result == []
    
    def test_exact_limit(self):
        """Test apply_max_tweets_filter when account has exactly max_tweets."""
        tweets = [
            {'author': 'user1', 'tweet_id': '1', 'score': 0.5, 'created_at': '2024-01-01'},
            {'author': 'user1', 'tweet_id': '2', 'score': 0.8, 'created_at': '2024-01-02'},
            {'author': 'user1', 'tweet_id': '3', 'score': 0.3, 'created_at': '2024-01-03'},
        ]
        
        result = apply_max_tweets_filter(tweets, max_tweets=3, brief_id='test_brief')
        
        # Should keep all 3 tweets
        assert len(result) == 3

