"""Tests for brief evaluator."""

import pytest
from unittest.mock import patch, call
from bitcast.validator.tweet_filtering.brief_evaluator import (
    BriefEvaluator,
    NUM_LLM_CHECKS,
)


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
    """Test single tweet evaluation with optimistic multi-check."""
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_passes_on_first_check(self, mock_evaluate, sample_brief):
        """Should short-circuit and return pass after first successful check."""
        mock_evaluate.return_value = (True, "Tweet mentions BitCast and tags correctly", "- Req 1: Met")
        
        evaluator = BriefEvaluator(sample_brief)
        tweet = {
            'tweet_id': '123',
            'author': 'user1',
            'text': 'Check out @bitcast_network!',
            'score': 0.5
        }
        
        result = evaluator.evaluate_tweet(tweet)
        
        assert mock_evaluate.call_count == 1
        mock_evaluate.assert_called_once_with(
            sample_brief, 'Check out @bitcast_network! 1',
            tweet_id='123', author='user1',
        )
        assert result['meets_brief'] is True
        assert result['reasoning'] == "Tweet mentions BitCast and tags correctly"
        assert result['detailed_breakdown'] == "- Req 1: Met"
        assert result['llm_checks_used'] == 1
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_passes_on_second_check_optimistic_bias(self, mock_evaluate, sample_brief):
        """Should pass if second check succeeds (optimistic bias)."""
        mock_evaluate.side_effect = [
            (False, "Does not meet requirements", "- Req 1: Not Met"),
            (True, "Actually meets requirements", "- Req 1: Met"),
        ]
        
        evaluator = BriefEvaluator(sample_brief)
        tweet = {'tweet_id': '123', 'author': 'user1', 'text': 'Some tweet', 'score': 0.5}
        
        result = evaluator.evaluate_tweet(tweet)
        
        assert mock_evaluate.call_count == 2
        assert result['meets_brief'] is True
        assert result['reasoning'] == "Actually meets requirements"
        assert result['llm_checks_used'] == 2
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_fails_when_all_checks_fail(self, mock_evaluate, sample_brief):
        """Should fail only when all NUM_LLM_CHECKS checks fail."""
        mock_evaluate.side_effect = [
            (False, "Fail 1", "- Req 1: Not Met"),
            (False, "Fail 2", "- Req 1: Not Met"),
            (False, "Fail 3", "- Req 1: Not Met — final"),
        ]
        
        evaluator = BriefEvaluator(sample_brief)
        tweet = {'tweet_id': '456', 'author': 'user2', 'text': 'Random tweet', 'score': 0.3}
        
        result = evaluator.evaluate_tweet(tweet)
        
        assert mock_evaluate.call_count == NUM_LLM_CHECKS
        assert result['meets_brief'] is False
        assert result['reasoning'] == "Fail 3"
        assert result['detailed_breakdown'] == "- Req 1: Not Met — final"
        assert result['llm_checks_used'] == NUM_LLM_CHECKS
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_handles_empty_text(self, mock_evaluate, sample_brief):
        """Should handle tweets with no text without calling LLM."""
        evaluator = BriefEvaluator(sample_brief)
        tweet = {
            'tweet_id': '123',
            'author': 'user1',
            'text': '',
            'score': 0.5
        }
        
        result = evaluator.evaluate_tweet(tweet)
        
        mock_evaluate.assert_not_called()
        assert result['meets_brief'] is False
        assert 'no text' in result['reasoning'].lower()
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_handles_llm_error_continues_checking(self, mock_evaluate, sample_brief):
        """Should continue to next check if one errors, and pass if a later one succeeds."""
        mock_evaluate.side_effect = [
            Exception("API error"),
            (True, "Pass after error", None),
        ]
        
        evaluator = BriefEvaluator(sample_brief)
        tweet = {'tweet_id': '123', 'author': 'user1', 'text': 'Some tweet', 'score': 0.5}
        
        result = evaluator.evaluate_tweet(tweet)
        
        assert mock_evaluate.call_count == 2
        assert result['meets_brief'] is True
        assert result['reasoning'] == "Pass after error"
        assert result['llm_checks_used'] == 2
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_all_checks_error(self, mock_evaluate, sample_brief):
        """Should mark as failed if all checks raise errors."""
        mock_evaluate.side_effect = [
            Exception("API error 1"),
            Exception("API error 2"),
            Exception("API error 3"),
        ]
        
        evaluator = BriefEvaluator(sample_brief)
        tweet = {'tweet_id': '123', 'author': 'user1', 'text': 'Some tweet', 'score': 0.5}
        
        result = evaluator.evaluate_tweet(tweet)
        
        assert mock_evaluate.call_count == NUM_LLM_CHECKS
        assert result['meets_brief'] is False
        assert 'Evaluation failed' in result['reasoning']
        assert 'API error 3' in result['reasoning']
        assert result['llm_checks_used'] == NUM_LLM_CHECKS
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_appends_check_digit_to_each_call(self, mock_evaluate, sample_brief):
        """Should append a different digit to tweet text for each check."""
        mock_evaluate.side_effect = [
            (False, "Fail", None),
            (False, "Fail", None),
            (False, "Fail", None),
        ]
        
        evaluator = BriefEvaluator(sample_brief)
        tweet = {'tweet_id': '123', 'author': 'user1', 'text': 'Some tweet', 'score': 0.5}
        
        evaluator.evaluate_tweet(tweet)
        
        assert mock_evaluate.call_count == NUM_LLM_CHECKS
        calls = mock_evaluate.call_args_list
        for i, c in enumerate(calls, 1):
            assert c == call(
                sample_brief, f'Some tweet {i}',
                tweet_id='123', author='user1',
            )


class TestEvaluateTweetsBatch:
    """Test batch tweet evaluation."""
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_evaluates_multiple_tweets(self, mock_evaluate, sample_brief, sample_tweets):
        """Should evaluate all tweets in batch."""
        mock_evaluate.side_effect = [
            # Tweet 123: passes on first check
            (True, "Meets requirements", "- Req 1: mention BitCast — Met"),
            # Tweet 456: fails all 3 checks
            (False, "Does not mention BitCast", "- Req 1: mention BitCast — Not Met"),
            (False, "Does not mention BitCast", "- Req 1: mention BitCast — Not Met"),
            (False, "Does not mention BitCast", "- Req 1: mention BitCast — Not Met"),
            # Tweet 789: empty text, no LLM calls
        ]
        
        # max_workers=1 to ensure deterministic side_effect consumption order
        evaluator = BriefEvaluator(sample_brief, max_workers=1)
        results = evaluator.evaluate_tweets_batch(sample_tweets)
        
        assert len(results) == 3
        
        results_by_id = {r['tweet_id']: r for r in results}
        
        assert results_by_id['123']['meets_brief'] is True
        assert results_by_id['123']['reasoning'] == "Meets requirements"
        
        assert results_by_id['456']['meets_brief'] is False
        assert results_by_id['456']['reasoning'] == "Does not mention BitCast"
        
        assert results_by_id['789']['meets_brief'] is False
        assert 'no text' in results_by_id['789']['reasoning'].lower()
        
        # 1 call for tweet 123 (passes first check) + 3 for tweet 456 (fails all)
        assert mock_evaluate.call_count == 4
    
    @patch('bitcast.validator.tweet_filtering.brief_evaluator.evaluate_content_against_brief')
    def test_continues_on_individual_failures(self, mock_evaluate, sample_brief):
        """Should continue batch even if individual evaluations fail."""
        mock_evaluate.side_effect = [
            # Tweet 1: passes first check
            (True, "Pass", "- Req 1: Met"),
            # Tweet 2: all checks error
            Exception("API error"),
            Exception("API error"),
            Exception("API error"),
            # Tweet 3: passes first check
            (True, "Pass", "- Req 1: Met")
        ]
        
        tweets = [
            {'tweet_id': '1', 'author': 'u1', 'text': 'Tweet 1', 'score': 0.5},
            {'tweet_id': '2', 'author': 'u2', 'text': 'Tweet 2', 'score': 0.4},
            {'tweet_id': '3', 'author': 'u3', 'text': 'Tweet 3', 'score': 0.3},
        ]
        
        # max_workers=1 to ensure deterministic side_effect consumption order
        evaluator = BriefEvaluator(sample_brief, max_workers=1)
        results = evaluator.evaluate_tweets_batch(tweets)
        
        assert len(results) == 3
        
        results_by_id = {r['tweet_id']: r for r in results}
        
        assert results_by_id['1']['meets_brief'] is True
        assert results_by_id['2']['meets_brief'] is False
        assert 'Evaluation failed' in results_by_id['2']['reasoning']
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
        
        assert result['tweet_id'] == '123'
        assert result['author'] == 'user1'
        assert result['text'] == 'Tweet text'
        assert result['score'] == 0.5
        assert result['url'] == 'https://twitter.com/user1/status/123'
        assert result['created_at'] == 'Mon Oct 30 12:00:00 +0000 2025'
        assert result['retweets'] == ['user2']
        assert result['quotes'] == []
        
        assert 'meets_brief' in result
        assert 'reasoning' in result
        assert 'llm_checks_used' in result
