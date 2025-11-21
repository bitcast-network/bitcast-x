"""Tests for Twitter evaluator."""

import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime

from bitcast.validator.reward_engine.twitter_evaluator import TwitterEvaluator
from bitcast.validator.reward_engine.models.evaluation_result import (
    EvaluationResult,
    EvaluationResultCollection,
    AccountResult
)
from bitcast.validator.utils.config import EMISSIONS_PERIOD


@pytest.fixture
def mock_alpha_price():
    """Mock get_bitcast_alpha_price to avoid API calls in tests."""
    with patch('bitcast.validator.utils.token_pricing.get_bitcast_alpha_price', return_value=0.01):
        yield


class TestTwitterEvaluator:
    """Test TwitterEvaluator class."""
    
    @pytest.mark.asyncio
    async def test_evaluate_briefs_empty_briefs(self):
        """Test evaluate_briefs with no briefs returns empty collection."""
        evaluator = TwitterEvaluator()
        result = await evaluator.evaluate_briefs(
            briefs=[],
            uid_account_mappings=[],
            connected_accounts=set(),
            metagraph=Mock(),
            run_id="test_run"
        )
        assert isinstance(result, EvaluationResultCollection)
        assert len(result.results) == 0
    
    @pytest.mark.asyncio
    async def test_evaluate_briefs_no_mappings(self):
        """Test evaluate_briefs with no UID mappings returns empty collection."""
        evaluator = TwitterEvaluator()
        briefs = [{'id': 'test_brief', 'pool': 'tao', 'budget': 1000}]
        result = await evaluator.evaluate_briefs(
            briefs=briefs,
            uid_account_mappings=[],
            connected_accounts=set(),
            metagraph=Mock(),
            run_id="test_run"
        )
        assert isinstance(result, EvaluationResultCollection)
        assert len(result.results) == 0
    
    @pytest.mark.asyncio
    async def test_evaluate_briefs_single_brief_single_uid(self, mock_alpha_price):
        """Test evaluate_briefs with single brief and single UID."""
        evaluator = TwitterEvaluator()
        
        briefs = [
            {
                'id': 'test_brief_001',
                'pool': 'tao',
                'budget': 7000,
                'brief': 'Test brief text',
                'prompt_version': 1
            }
        ]
        
        uid_mappings = [
            {'account_username': 'test_user', 'uid': 42}
        ]
        
        # Mock the internal methods
        scored_tweets = [
            {'author': 'test_user', 'tweet_id': '123', 'score': 0.5}
        ]
        
        filtered_tweets = [
            {'author': 'test_user', 'tweet_id': '123', 'score': 0.5, 'meets_brief': True}
        ]
        
        with patch.object(evaluator, '_score_tweets_for_brief', return_value=scored_tweets):
            with patch.object(evaluator, '_filter_tweets_for_brief', return_value=filtered_tweets):
                with patch('bitcast.validator.reward_engine.twitter_evaluator.save_reward_snapshot'):
                    with patch('bitcast.validator.reward_engine.twitter_evaluator.load_reward_snapshot', side_effect=FileNotFoundError()):
                        result = await evaluator.evaluate_briefs(
                            briefs=briefs,
                            uid_account_mappings=uid_mappings,
                            connected_accounts={'test_user'},
                            metagraph=Mock(),
                            run_id="test_run"
                        )
        
        # Verify results
        assert len(result.results) == 1
        assert 42 in result.results
        
        uid_result = result.results[42]
        assert uid_result.uid == 42
        assert uid_result.platform == "twitter"
        assert 'test_brief_001' in uid_result.aggregated_scores
        
        # Check USD amount - with power law smoothing, single UID still gets 100%
        daily_budget = 7000 / EMISSIONS_PERIOD
        expected_usd = daily_budget  # 100% of budget since only UID
        assert abs(uid_result.aggregated_scores['test_brief_001'] - expected_usd) < 0.01
    
    @pytest.mark.asyncio
    async def test_evaluate_briefs_multiple_uids(self, mock_alpha_price):
        """Test evaluate_briefs with multiple UIDs sharing budget."""
        evaluator = TwitterEvaluator()
        
        briefs = [
            {
                'id': 'test_brief_002',
                'pool': 'tao',
                'budget': 7000,
                'brief': 'Test brief text',
                'prompt_version': 1
            }
        ]
        
        uid_mappings = [
            {'account_username': 'user1', 'uid': 10},
            {'account_username': 'user2', 'uid': 20}
        ]
        
        # Mock tweets with different scores
        scored_tweets = [
            {'author': 'user1', 'tweet_id': '123', 'score': 0.6},
            {'author': 'user2', 'tweet_id': '456', 'score': 0.4}
        ]
        
        filtered_tweets = [
            {'author': 'user1', 'tweet_id': '123', 'score': 0.6, 'meets_brief': True},
            {'author': 'user2', 'tweet_id': '456', 'score': 0.4, 'meets_brief': True}
        ]
        
        with patch.object(evaluator, '_score_tweets_for_brief', return_value=scored_tweets):
            with patch.object(evaluator, '_filter_tweets_for_brief', return_value=filtered_tweets):
                with patch('bitcast.validator.reward_engine.twitter_evaluator.save_reward_snapshot'):
                    with patch('bitcast.validator.reward_engine.twitter_evaluator.load_reward_snapshot', side_effect=FileNotFoundError()):
                        result = await evaluator.evaluate_briefs(
                            briefs=briefs,
                            uid_account_mappings=uid_mappings,
                            connected_accounts={'user1', 'user2'},
                            metagraph=Mock(),
                            run_id="test_run"
                        )
        
        # Verify results
        assert len(result.results) == 2
        assert 10 in result.results
        assert 20 in result.results
        
        # Check proportional distribution with power law smoothing (α=0.65)
        # Scores: 0.6 and 0.4
        # Smoothed: 0.6^0.65 = 0.7175, 0.4^0.65 = 0.5512, total = 1.2687
        # Proportions: 56.55% and 43.45%
        daily_budget = 7000 / EMISSIONS_PERIOD
        uid1_expected = daily_budget * 0.5655  # ~56.55% of budget
        uid2_expected = daily_budget * 0.4345  # ~43.45% of budget
        
        assert abs(result.results[10].aggregated_scores['test_brief_002'] - uid1_expected) < 1.0
        assert abs(result.results[20].aggregated_scores['test_brief_002'] - uid2_expected) < 1.0
        
        # Verify total equals daily budget (critical constraint)
        total = (result.results[10].aggregated_scores['test_brief_002'] + 
                result.results[20].aggregated_scores['test_brief_002'])
        assert abs(total - daily_budget) < 0.01
    
    @pytest.mark.asyncio
    async def test_evaluate_briefs_unmapped_account(self, mock_alpha_price):
        """Test that unmapped accounts are logged but don't crash."""
        evaluator = TwitterEvaluator()
        
        briefs = [
            {
                'id': 'test_brief_004',
                'pool': 'tao',
                'budget': 7000,
                'brief': 'Test brief text',
                'prompt_version': 1
            }
        ]
        
        uid_mappings = [
            {'account_username': 'mapped_user', 'uid': 42}
        ]
        
        # Mock tweets including one from unmapped account
        scored_tweets = [
            {'author': 'mapped_user', 'tweet_id': '123', 'score': 0.5},
            {'author': 'unmapped_user', 'tweet_id': '456', 'score': 0.3}
        ]
        
        filtered_tweets = [
            {'author': 'mapped_user', 'tweet_id': '123', 'score': 0.5, 'meets_brief': True},
            {'author': 'unmapped_user', 'tweet_id': '456', 'score': 0.3, 'meets_brief': True}
        ]
        
        with patch.object(evaluator, '_score_tweets_for_brief', return_value=scored_tweets):
            with patch.object(evaluator, '_filter_tweets_for_brief', return_value=filtered_tweets):
                with patch('bitcast.validator.reward_engine.twitter_evaluator.save_reward_snapshot'):
                    with patch('bitcast.validator.reward_engine.twitter_evaluator.load_reward_snapshot', side_effect=FileNotFoundError()):
                        result = await evaluator.evaluate_briefs(
                            briefs=briefs,
                            uid_account_mappings=uid_mappings,
                            connected_accounts={'mapped_user', 'unmapped_user'},
                            metagraph=Mock(),
                            run_id="test_run"
                        )
        
        # Only mapped accounts get rewards in results (unmapped tweet processed but not assigned to UID)
        assert len(result.results) == 1
        assert 42 in result.results
        
        # Budget distributed with power law smoothing (α=0.65)
        # Scores: 0.5 (mapped) and 0.3 (unmapped)
        # Smoothed: 0.5^0.65 = 0.6373, 0.3^0.65 = 0.4572, total = 1.0945
        # Mapped user gets: 58.23% of budget (unmapped user's 41.77% goes unassigned)
        daily_budget = 7000 / EMISSIONS_PERIOD
        assert abs(result.results[42].aggregated_scores['test_brief_004'] - daily_budget * 0.5823) < 1.0
    
    @pytest.mark.asyncio
    async def test_evaluate_briefs_error_in_brief_continues(self, mock_alpha_price):
        """Test that error in one brief doesn't stop processing others."""
        evaluator = TwitterEvaluator()
        
        briefs = [
            {'id': 'error_brief', 'pool': 'tao', 'budget': 1000, 'brief': 'Test', 'prompt_version': 1},
            {'id': 'good_brief', 'pool': 'tao', 'budget': 2000, 'brief': 'Test', 'prompt_version': 1}
        ]
        
        uid_mappings = [
            {'account_username': 'test_user', 'uid': 42}
        ]
        
        # Mock scoring to fail for first brief, succeed for second
        def mock_score(pool_name, brief_id, connected_accounts, tag, qrt, run_id, start_date, end_date):
            if brief_id == 'error_brief':
                raise ValueError("Scoring failed")
            return [{'author': 'test_user', 'tweet_id': '123', 'score': 0.5}]
        
        filtered_tweets = [
            {'author': 'test_user', 'tweet_id': '123', 'score': 0.5, 'meets_brief': True}
        ]
        
        with patch.object(evaluator, '_score_tweets_for_brief', side_effect=mock_score):
            with patch.object(evaluator, '_filter_tweets_for_brief', return_value=filtered_tweets):
                with patch('bitcast.validator.reward_engine.twitter_evaluator.save_reward_snapshot'):
                    with patch('bitcast.validator.reward_engine.twitter_evaluator.load_reward_snapshot', side_effect=FileNotFoundError()):
                        result = await evaluator.evaluate_briefs(
                            briefs=briefs,
                            uid_account_mappings=uid_mappings,
                            connected_accounts={'test_user'},
                            metagraph=Mock(),
                            run_id="test_run"
                        )
        
        # Should have result only for good brief
        assert len(result.results) == 1
        assert 'good_brief' in result.results[42].aggregated_scores
        assert 'error_brief' not in result.results[42].aggregated_scores
    
    def test_calculate_tweet_targets(self, mock_alpha_price):
        """Test _calculate_tweet_targets method with power law smoothing."""
        evaluator = TwitterEvaluator()
        
        tweets = [
            {'author': 'user1', 'tweet_id': '1', 'score': 0.6},
            {'author': 'user2', 'tweet_id': '2', 'score': 0.4}
        ]
        
        daily_budget = 1000.0
        
        result = evaluator._calculate_tweet_targets(tweets, daily_budget, 'test_brief')
        
        # With power law smoothing (α=0.65):
        # 0.6^0.65 = 0.7175, 0.4^0.65 = 0.5512, total = 1.2687
        # Proportions: 56.55% and 43.45%
        assert abs(result[0]['usd_target'] - 565.51) < 0.1  # ~56.55% of budget
        assert abs(result[1]['usd_target'] - 434.49) < 0.1  # ~43.45% of budget
        assert 'alpha_target' in result[0]
        assert 'alpha_target' in result[1]
        assert 'total_usd_target' in result[0]
        assert 'total_usd_target' in result[1]
        
        # Verify total equals budget (critical constraint)
        total = sum(t['usd_target'] for t in result)
        assert abs(total - daily_budget) < 0.01
    
    def test_aggregate_targets_to_uids(self):
        """Test _aggregate_targets_to_uids method."""
        evaluator = TwitterEvaluator()
        
        tweets = [
            {'author': 'user1', 'usd_target': 300.0},
            {'author': 'user1', 'usd_target': 300.0},  # Same UID, should sum
            {'author': 'user2', 'usd_target': 400.0}
        ]
        
        account_to_uid = {
            'user1': 10,
            'user2': 20
        }
        
        result = evaluator._aggregate_targets_to_uids(tweets, account_to_uid)
        
        # Verify aggregation
        assert len(result) == 2
        assert result[10] == 600.0  # user1 tweets summed
        assert result[20] == 400.0  # user2 tweet
    
    @pytest.mark.asyncio
    async def test_score_briefs_for_monitoring_empty_briefs(self):
        """Test score_briefs_for_monitoring with empty briefs list."""
        evaluator = TwitterEvaluator()
        
        # Should handle empty list without error
        result = await evaluator.score_briefs_for_monitoring([], connected_accounts=set(), run_id='test_run')
        
        # Returns None (fire and forget)
        assert result is None
    
    @pytest.mark.asyncio
    async def test_score_briefs_for_monitoring_no_rewards_calculated(self, mock_alpha_price):
        """Test that score_briefs_for_monitoring does NOT calculate rewards."""
        evaluator = TwitterEvaluator()
        
        briefs = [
            {
                'id': 'test_brief',
                'pool': 'test_pool',
                'tag': None,
                'qrt': None,
                'budget': 1000,
                'state': 'scoring'
            }
        ]
        
        # Mock the internal methods to prevent actual API calls
        with patch.object(evaluator, '_score_tweets_for_brief', return_value=[]):
            with patch.object(evaluator, '_filter_tweets_for_brief', return_value=[]):
                with patch.object(evaluator, '_publish_brief_tweets', new=AsyncMock()):
                    # Should not raise error and not calculate rewards
                    result = await evaluator.score_briefs_for_monitoring(briefs, connected_accounts=set(), run_id='test_run')
                    assert result is None
    
    @pytest.mark.asyncio  
    async def test_score_tweets_for_brief_always_fresh(self, mock_alpha_price):
        """Test that _score_tweets_for_brief always scores fresh (no snapshot loading)."""
        evaluator = TwitterEvaluator()
        
        # Mock score_tweets_for_pool to return test data
        with patch('bitcast.validator.reward_engine.twitter_evaluator.score_tweets_for_pool') as mock_score:
            mock_score.return_value = [
                {'author': 'user1', 'tweet_id': '1', 'score': 0.8}
            ]
            
            result = evaluator._score_tweets_for_brief(
                pool_name='test_pool',
                brief_id='test_brief',
                connected_accounts={'user1'},
                tag=None,
                qrt=None,
                run_id='test_run',
                start_date=None,
                end_date=None
            )
            
            # Should call score_tweets_for_pool (not load snapshot)
            assert mock_score.called
            assert len(result) == 1
            assert result[0]['author'] == 'user1'
    
    @pytest.mark.asyncio
    async def test_score_tweets_for_brief_passes_dates(self, mock_alpha_price):
        """Test that _score_tweets_for_brief passes start_date and end_date to score_tweets_for_pool."""
        evaluator = TwitterEvaluator()
        
        start_date = datetime(2024, 1, 1)
        end_date = datetime(2024, 1, 7)
        
        # Mock score_tweets_for_pool to return test data
        with patch('bitcast.validator.reward_engine.twitter_evaluator.score_tweets_for_pool') as mock_score:
            mock_score.return_value = [
                {'author': 'user1', 'tweet_id': '1', 'score': 0.8}
            ]
            
            result = evaluator._score_tweets_for_brief(
                pool_name='test_pool',
                brief_id='test_brief',
                connected_accounts={'user1'},
                tag='#test',
                qrt='123456',
                run_id='test_run',
                start_date=start_date,
                end_date=end_date
            )
            
            # Verify score_tweets_for_pool was called with dates
            assert mock_score.called
            call_args = mock_score.call_args
            assert call_args[1]['start_date'] == start_date
            assert call_args[1]['end_date'] == end_date
            assert call_args[1]['tag'] == '#test'
            assert call_args[1]['qrt'] == '123456'
            
            # Verify result
            assert len(result) == 1
            assert result[0]['author'] == 'user1'
    
    def test_parse_brief_date_valid_formats(self):
        """Test _parse_brief_date with valid date formats."""
        evaluator = TwitterEvaluator()
        
        # ISO format with timezone
        result = evaluator._parse_brief_date('2024-01-15T00:00:00Z')
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
        
        # Simple YYYY-MM-DD format
        result = evaluator._parse_brief_date('2024-01-15')
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
    
    def test_parse_brief_date_invalid_inputs(self):
        """Test _parse_brief_date with invalid inputs returns None."""
        evaluator = TwitterEvaluator()
        
        assert evaluator._parse_brief_date(None) is None
        assert evaluator._parse_brief_date('') is None
        assert evaluator._parse_brief_date('not-a-date') is None
    
    def test_convert_snapshot_to_tweets_with_targets_preserves_engagement(self):
        """Test that _convert_snapshot_to_tweets_with_targets preserves engagement metrics."""
        evaluator = TwitterEvaluator()
        
        tweet_rewards = [{
            'tweet_id': '123', 'author': 'user1', 'uid': 1, 'score': 0.85, 'total_usd': 700.0,
            'favorite_count': 10, 'retweet_count': 5, 'reply_count': 3, 'quote_count': 2,
            'bookmark_count': 1, 'retweets': ['acc1', 'acc2'], 'quotes': ['acc3'],
            'created_at': '2025-11-01T10:00:00Z', 'lang': 'en'
        }]
        
        with patch('bitcast.validator.reward_engine.twitter_evaluator.get_bitcast_alpha_price', return_value=0.01):
            result = evaluator._convert_snapshot_to_tweets_with_targets(tweet_rewards)
            
            assert len(result) == 1
            tweet = result[0]
            
            # Financial targets calculated correctly
            expected_daily = 700.0 / EMISSIONS_PERIOD
            assert abs(tweet['usd_target'] - expected_daily) < 0.01
            assert tweet['total_usd_target'] == 700.0
            
            # Engagement metrics preserved
            assert tweet['favorite_count'] == 10
            assert tweet['retweet_count'] == 5
            assert tweet['retweets'] == ['acc1', 'acc2']
            assert tweet['quotes'] == ['acc3']

