"""Tests for brief evaluator."""

import pytest
from unittest.mock import patch, MagicMock
from bitcast.validator.tweet_filtering.brief_evaluator import BriefEvaluator


@pytest.fixture
def sample_brief():
    """Sample brief for testing."""
    return {
        'id': 'test_brief',
        'brief': 'Talk about BitCast and tag @bitcast_network',
        'prompt_version': 1
    }


@pytest.fixture
def sample_tweets():
    """Sample tweets for testing."""
    return [
        {
            'tweet_id': '123',
            'author': 'user1',
            'text': 'Check out @bitcast_network - great platform!',
            'score': 0.5
        },
        {
            'tweet_id': '456',
            'author': 'user2',
            'text': 'Random tweet about something else',
            'score': 0.3
        },
        {
            'tweet_id': '789',
            'author': 'user3',
            'text': '',  # Empty text
            'score': 0.2
        }
    ]


class TestBriefEvaluatorInit:
    """Test BriefEvaluator initialization."""
    
    def test_initializes_with_brief(self, sample_brief):
        """Should initialize with valid brief."""
        evaluator = BriefEvaluator(sample_brief)
        assert evaluator.brief == sample_brief
        assert evaluator.max_workers == 10  # Default
    
    def test_custom_max_workers(self, sample_brief):
        """Should accept custom max_workers."""
        evaluator = BriefEvaluator(sample_brief, max_workers=5)
        assert evaluator.max_workers == 5
    
    def test_raises_if_brief_text_missing(self):
        """Should raise if brief text is missing."""
        with pytest.raises(ValueError, match="must contain 'brief' text field"):
            BriefEvaluator({'id': 'test'})
    
    def test_raises_if_brief_id_missing(self):
        """Should raise if brief id is missing."""
        with pytest.raises(ValueError, match="must contain 'id' field"):
            BriefEvaluator({'brief': 'test'})


class TestEvaluateTweet:
    """Test single tweet evaluation."""
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_evaluates_tweet_with_text(self, mock_evaluate, sample_brief):
        """Should evaluate tweet and return enriched result."""
        mock_evaluate.return_value = (True, "Tweet mentions BitCast and tags correctly", "- Req 1: mentions BitCast — Met — \"@bitcast_network\"")
        
        evaluator = BriefEvaluator(sample_brief)
        tweet = {
            'tweet_id': '123',
            'author': 'user1',
            'text': 'Check out @bitcast_network!',
            'score': 0.5
        }
        
        result = evaluator.evaluate_tweet(tweet)
        
        # Should call LLM evaluation (now includes tweet_id and author parameters)
        mock_evaluate.assert_called_once_with(sample_brief, 'Check out @bitcast_network!', tweet_id='123', author='user1')
        
        # Should preserve original fields and add new ones
        assert result['tweet_id'] == '123'
        assert result['author'] == 'user1'
        assert result['text'] == 'Check out @bitcast_network!'
        assert result['score'] == 0.5
        assert result['meets_brief'] is True
        assert result['reasoning'] == "Tweet mentions BitCast and tags correctly"
        assert result['detailed_breakdown'] == "- Req 1: mentions BitCast — Met — \"@bitcast_network\""
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_handles_empty_text(self, mock_evaluate, sample_brief):
        """Should handle tweets with no text."""
        evaluator = BriefEvaluator(sample_brief)
        tweet = {
            'tweet_id': '123',
            'author': 'user1',
            'text': '',
            'score': 0.5
        }
        
        result = evaluator.evaluate_tweet(tweet)
        
        # Should not call LLM for empty text
        mock_evaluate.assert_not_called()
        
        # Should mark as failed
        assert result['meets_brief'] is False
        assert 'no text' in result['reasoning'].lower()
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_handles_llm_error(self, mock_evaluate, sample_brief):
        """Should handle LLM evaluation errors gracefully."""
        mock_evaluate.side_effect = Exception("API error")
        
        evaluator = BriefEvaluator(sample_brief)
        tweet = {
            'tweet_id': '123',
            'author': 'user1',
            'text': 'Some tweet',
            'score': 0.5
        }
        
        result = evaluator.evaluate_tweet(tweet)
        
        # Should mark as failed with error message
        assert result['meets_brief'] is False
        assert 'Evaluation failed' in result['reasoning']
        assert 'API error' in result['reasoning']


class TestEvaluateTweetsBatch:
    """Test batch tweet evaluation."""
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_evaluates_multiple_tweets(self, mock_evaluate, sample_brief, sample_tweets):
        """Should evaluate all tweets in batch."""
        # Mock different responses for different tweets
        mock_evaluate.side_effect = [
            (True, "Meets requirements", "- Req 1: mention BitCast — Met"),
            (False, "Does not mention BitCast", "- Req 1: mention BitCast — Not Met"),
            # Empty text tweet won't call LLM
        ]
        
        evaluator = BriefEvaluator(sample_brief, max_workers=2)
        results = evaluator.evaluate_tweets_batch(sample_tweets)
        
        # Should evaluate all tweets
        assert len(results) == 3
        
        # Check results by tweet_id (order not guaranteed with parallel execution)
        results_by_id = {r['tweet_id']: r for r in results}
        
        # First tweet should pass
        assert results_by_id['123']['meets_brief'] is True
        assert results_by_id['123']['reasoning'] == "Meets requirements"
        
        # Second tweet should fail
        assert results_by_id['456']['meets_brief'] is False
        assert results_by_id['456']['reasoning'] == "Does not mention BitCast"
        
        # Third tweet (empty text) should fail
        assert results_by_id['789']['meets_brief'] is False
        assert 'no text' in results_by_id['789']['reasoning'].lower()
        
        # Should have called LLM twice (not for empty tweet)
        assert mock_evaluate.call_count == 2
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_continues_on_individual_failures(self, mock_evaluate, sample_brief):
        """Should continue batch even if individual evaluations fail."""
        # First succeeds, second fails, third succeeds
        mock_evaluate.side_effect = [
            (True, "Pass", "- Req 1: Met"),
            Exception("API error"),
            (True, "Pass", "- Req 1: Met")
        ]
        
        tweets = [
            {'tweet_id': '1', 'author': 'u1', 'text': 'Tweet 1', 'score': 0.5},
            {'tweet_id': '2', 'author': 'u2', 'text': 'Tweet 2', 'score': 0.4},
            {'tweet_id': '3', 'author': 'u3', 'text': 'Tweet 3', 'score': 0.3},
        ]
        
        evaluator = BriefEvaluator(sample_brief)
        results = evaluator.evaluate_tweets_batch(tweets)
        
        # Should return all results
        assert len(results) == 3
        
        # Check results by tweet_id (order not guaranteed)
        results_by_id = {r['tweet_id']: r for r in results}
        
        # Check individual results
        assert results_by_id['1']['meets_brief'] is True
        assert results_by_id['2']['meets_brief'] is False  # Failed
        assert results_by_id['2']['reasoning'] == 'Evaluation failed: API error'
        assert results_by_id['3']['meets_brief'] is True
    
    def test_handles_empty_list(self, sample_brief):
        """Should handle empty tweet list."""
        evaluator = BriefEvaluator(sample_brief)
        results = evaluator.evaluate_tweets_batch([])
        
        assert results == []
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_preserves_all_original_fields(self, mock_evaluate, sample_brief):
        """Should preserve all original tweet fields."""
        mock_evaluate.return_value = (True, "Pass", "- Req 1: Met")
        
        tweet = {
            'tweet_id': '123',
            'author': 'user1',
            'text': 'Tweet text',
            'score': 0.5,
            'url': 'https://twitter.com/user1/status/123',
            'created_at': 'Mon Oct 30 12:00:00 +0000 2025',
            'retweets': ['user2'],
            'quotes': []
        }
        
        evaluator = BriefEvaluator(sample_brief)
        result = evaluator.evaluate_tweet(tweet)
        
        # All original fields should be preserved
        assert result['tweet_id'] == '123'
        assert result['author'] == 'user1'
        assert result['text'] == 'Tweet text'
        assert result['score'] == 0.5
        assert result['url'] == 'https://twitter.com/user1/status/123'
        assert result['created_at'] == 'Mon Oct 30 12:00:00 +0000 2025'
        assert result['retweets'] == ['user2']
        assert result['quotes'] == []
        
        # Plus new fields
        assert 'meets_brief' in result
        assert 'reasoning' in result

