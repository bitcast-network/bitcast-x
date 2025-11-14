"""Tests for score calculator."""

import pytest
from bitcast.validator.tweet_scoring.score_calculator import ScoreCalculator
from bitcast.validator.tweet_scoring.engagement_analyzer import EngagementAnalyzer


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


class TestScoreTweetsBatch:
    """Test batch tweet scoring."""
    
    def test_scores_multiple_tweets(self, calculator):
        """Should score multiple tweets."""
        tweets = [
            {
                'tweet_id': '001',
                'author': 'alice',
                'created_at': '2025-10-30T12:00:00',
                'text': 'Tweet 1',
                'lang': 'en'
            },
            {
                'tweet_id': '002',
                'author': 'bob',
                'created_at': '2025-10-30T13:00:00',
                'text': 'Tweet 2',
                'lang': 'en'
            }
        ]
        
        all_tweets = tweets + [
            {
                'tweet_id': '003',
                'author': 'charlie',
                'text': 'RT',
                'retweeted_user': 'alice',
                'quoted_user': None
            }
        ]
        
        analyzer = EngagementAnalyzer()
        scored = calculator.score_tweets_batch(tweets, all_tweets, analyzer)
        
        assert len(scored) == 2
        assert all('score' in tweet for tweet in scored)
        assert all('retweets' in tweet for tweet in scored)
        assert all('quotes' in tweet for tweet in scored)
    
    def test_includes_required_fields(self, calculator):
        """Should include required fields in output."""
        tweets = [
            {
                'tweet_id': '001',
                'author': 'alice',
                'created_at': '2025-10-30T12:00:00',
                'text': 'Original tweet',
                'lang': 'en'
            }
        ]
        
        analyzer = EngagementAnalyzer()
        scored = calculator.score_tweets_batch(tweets, tweets, analyzer)
        
        result = scored[0]
        assert result['tweet_id'] == '001'
        assert result['author'] == 'alice'
        assert result['url'] == 'https://twitter.com/alice/status/001'
        assert result['created_at'] == '2025-10-30T12:00:00'
        assert 'score' in result
        assert 'retweets' in result
        assert 'quotes' in result
        assert isinstance(result['retweets'], list)
        assert isinstance(result['quotes'], list)
    
    def test_separates_retweets_and_quotes(self, calculator):
        """Should separate retweets and quotes into different lists."""
        tweets = [
            {
                'tweet_id': '001',
                'author': 'alice',
                'text': 'Original',
                'created_at': '',
                'lang': 'en'
            }
        ]
        
        all_tweets = tweets + [
            {'tweet_id': '002', 'author': 'bob', 'retweeted_user': 'alice', 'quoted_user': None, 'text': '', 'retweeted_tweet_id': '001', 'quoted_tweet_id': None},
            {'tweet_id': '003', 'author': 'charlie', 'retweeted_user': None, 'quoted_user': 'alice', 'text': '', 'retweeted_tweet_id': None, 'quoted_tweet_id': '001'}
        ]
        
        analyzer = EngagementAnalyzer()
        scored = calculator.score_tweets_batch(tweets, all_tweets, analyzer)
        
        assert 'bob' in scored[0]['retweets']
        assert 'charlie' in scored[0]['quotes']
        assert len(scored[0]['retweets']) == 1
        assert len(scored[0]['quotes']) == 1
    
    def test_empty_tweet_list(self, calculator):
        """Should handle empty tweet list."""
        analyzer = EngagementAnalyzer()
        scored = calculator.score_tweets_batch([], [], analyzer)
        
        assert scored == []
    
    def test_sorts_by_score_descending(self, calculator):
        """Should sort tweets by score descending."""
        tweets = [
            {'tweet_id': '001', 'author': 'alice', 'text': 'A', 'created_at': '', 'lang': ''},
            {'tweet_id': '002', 'author': 'bob', 'text': 'B', 'created_at': '', 'lang': ''},
            {'tweet_id': '003', 'author': 'charlie', 'text': 'C', 'created_at': '', 'lang': ''}
        ]
        
        # Add engagements to give different scores
        all_tweets = tweets + [
            {'tweet_id': '004', 'author': 'david', 'retweeted_user': 'bob', 'quoted_user': None, 'text': ''},  # bob gets score
        ]
        
        analyzer = EngagementAnalyzer()
        scored = calculator.score_tweets_batch(tweets, all_tweets, analyzer)
        
        # Should be sorted by score descending
        scores = [t['score'] for t in scored]
        assert scores == sorted(scores, reverse=True)


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

