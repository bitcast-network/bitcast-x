"""Tests for score calculator."""

import pytest
from bitcast.validator.tweet_scoring.score_calculator import ScoreCalculator


@pytest.fixture
def considered_accounts():
    """Sample considered accounts with influence scores."""
    return {
        'alice': 0.10,
        'bob': 0.08,
        'charlie': 0.06,
        'david': 0.04,
        'eve': 0.02
    }


@pytest.fixture
def calculator(considered_accounts):
    """Create calculator with default weights."""
    return ScoreCalculator(
        considered_accounts,
        retweet_weight=2.0,
        quote_weight=3.0
    )


class TestCalculateTweetScore:
    """Test weighted score calculation."""
    
    def test_calculates_retweet_score(self, calculator):
        """Should calculate score for retweets."""
        engagements = {
            'bob': 'retweet',  # 0.08 * 2.0 = 0.16
        }
        
        score, details = calculator.calculate_tweet_score(engagements, author_influence_score=0.05)
        
        # Score = baseline (0.05 * 2.0) + engagement (0.16) = 0.1 + 0.16 = 0.26
        assert score == 0.26
        assert len(details) == 1
        assert details[0]['username'] == 'bob'
        assert details[0]['influence_score'] == 0.08
        assert details[0]['engagement_type'] == 'retweet'
        assert details[0]['weighted_contribution'] == 0.16
    
    def test_calculates_quote_score(self, calculator):
        """Should calculate score for quotes."""
        engagements = {
            'charlie': 'quote',  # 0.06 * 3.0 = 0.18
        }
        
        score, details = calculator.calculate_tweet_score(engagements, author_influence_score=0.05)
        
        # Score = baseline (0.05 * 2.0) + engagement (0.18) = 0.1 + 0.18 = 0.28
        assert score == 0.28
        assert len(details) == 1
        assert details[0]['engagement_type'] == 'quote'
        assert details[0]['weighted_contribution'] == 0.18
    
    def test_sums_multiple_engagements(self, calculator):
        """Should sum weighted contributions from multiple accounts."""
        engagements = {
            'alice': 'retweet',  # 0.10 * 2.0 = 0.20
            'bob': 'quote',      # 0.08 * 3.0 = 0.24
            'charlie': 'retweet' # 0.06 * 2.0 = 0.12
        }
        
        score, details = calculator.calculate_tweet_score(engagements, author_influence_score=0.05)
        
        # Score = baseline (0.05 * 2.0) + engagements (0.20 + 0.24 + 0.12) = 0.1 + 0.56 = 0.66
        expected_score = 0.1 + 0.20 + 0.24 + 0.12
        assert score == pytest.approx(expected_score)
        assert len(details) == 3
    
    def test_quote_weight_higher_than_retweet(self, calculator):
        """Should weight quotes higher than retweets."""
        # Same influence score, different engagement types
        retweet_engagements = {'alice': 'retweet'}
        quote_engagements = {'alice': 'quote'}
        
        rt_score, _ = calculator.calculate_tweet_score(retweet_engagements, author_influence_score=0.05)
        quote_score, _ = calculator.calculate_tweet_score(quote_engagements, author_influence_score=0.05)
        
        assert quote_score > rt_score
        # RT score = baseline (0.1) + engagement (0.10 * 2.0) = 0.3
        assert rt_score == pytest.approx(0.3)
        # Quote score = baseline (0.1) + engagement (0.10 * 3.0) = 0.4
        assert quote_score == pytest.approx(0.4)
    
    def test_rounds_to_six_decimals(self, calculator):
        """Should round scores to 6 decimal places."""
        # Use score that would have many decimals
        engagements = {'alice': 'retweet'}  # 0.10 * 2.0
        
        score, details = calculator.calculate_tweet_score(engagements, author_influence_score=0.05)
        
        # Check rounding
        score_str = str(score)
        if '.' in score_str:
            decimals = len(score_str.split('.')[1])
            assert decimals <= 6
    
    def test_handles_unknown_account(self, calculator):
        """Should skip accounts not in considered_accounts."""
        engagements = {
            'bob': 'retweet',     # Valid: 0.16
            'stranger': 'quote'   # Not in considered_accounts
        }
        
        score, details = calculator.calculate_tweet_score(engagements, author_influence_score=0.05)
        
        # Score = baseline (0.1) + bob's engagement (0.16) = 0.26
        assert score == 0.26  # 0.1 + (0.08 * 2.0)
        assert len(details) == 1
        assert details[0]['username'] == 'bob'
    
    def test_handles_unknown_engagement_type(self, calculator):
        """Should skip unknown engagement types."""
        engagements = {
            'bob': 'retweet',
            'charlie': 'like'  # Unknown type
        }
        
        score, details = calculator.calculate_tweet_score(engagements, author_influence_score=0.05)
        
        # Score = baseline (0.1) + retweet (0.16) = 0.26
        assert score == 0.26  # 0.1 + (0.08 * 2.0)
        assert len(details) == 1
    
    def test_no_engagements(self, calculator):
        """Should return baseline score for no engagements."""
        engagements = {}
        author_influence_score = 0.05
        
        score, details = calculator.calculate_tweet_score(engagements, author_influence_score)
        
        # Score should be author_influence_score * BASELINE_TWEET_SCORE_FACTOR (2.0)
        assert score == 0.1  # 0.05 * 2.0
        assert details == []
    
    def test_custom_weights(self, considered_accounts):
        """Should use custom weights when provided."""
        custom_calculator = ScoreCalculator(
            considered_accounts,
            retweet_weight=5.0,
            quote_weight=10.0
        )
        
        engagements = {'alice': 'retweet'}
        score, _ = custom_calculator.calculate_tweet_score(engagements, author_influence_score=0.05)
        
        # Score = baseline (0.1) + engagement (0.10 * 5.0) = 0.6
        assert score == 0.6


class TestInitialization:
    """Test calculator initialization."""
    
    def test_uses_config_defaults(self, considered_accounts):
        """Should use config values when weights not provided."""
        calc = ScoreCalculator(considered_accounts)
        
        # Should use PAGERANK_RETWEET_WEIGHT and PAGERANK_QUOTE_WEIGHT
        assert calc.retweet_weight == 1.0
        assert calc.quote_weight == 3.0
    
    def test_accepts_custom_weights(self, considered_accounts):
        """Should accept custom weight values."""
        calc = ScoreCalculator(
            considered_accounts,
            retweet_weight=10.0,
            quote_weight=20.0
        )
        
        assert calc.retweet_weight == 10.0
        assert calc.quote_weight == 20.0
    
    def test_stores_considered_accounts(self, considered_accounts):
        """Should store considered accounts map."""
        calc = ScoreCalculator(considered_accounts)
        
        assert calc.considered_accounts == considered_accounts
        assert len(calc.considered_accounts) == 5
    
    def test_calculates_min_influence_score(self, considered_accounts):
        """Should calculate minimum influence score from considered accounts."""
        calc = ScoreCalculator(considered_accounts)
        
        # Eve has the lowest score (0.02)
        assert calc.min_influence_score == 0.02
    
    def test_min_influence_score_with_empty_accounts(self):
        """Should set min influence score to 0 when no accounts provided."""
        calc = ScoreCalculator({})
        
        assert calc.min_influence_score == 0.0



