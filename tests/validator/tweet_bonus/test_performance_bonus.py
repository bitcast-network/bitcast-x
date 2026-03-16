"""Tests for performance bonus calculator."""

import pytest
from unittest.mock import patch

from bitcast.validator.tweet_bonus.performance_bonus import (
    calculate_performance_bonus,
    _compute_metrics,
    _compute_bonuses,
)


def _make_tweet(author="alice", score=1.0, views=1000, favorites=50,
                retweets=10, replies=5, quotes=3, bookmarks=2):
    return {
        'tweet_id': f'tweet_{author}',
        'author': author,
        'score': score,
        'views_count': views,
        'favorite_count': favorites,
        'retweet_count': retweets,
        'reply_count': replies,
        'quote_count': quotes,
        'bookmark_count': bookmarks,
    }


class TestComputeMetrics:

    def test_basic_metrics(self):
        tweets = [_make_tweet(views=1000, favorites=50, retweets=10, replies=5, quotes=3, bookmarks=2)]
        follower_counts = {'alice': 500}

        metrics = _compute_metrics(tweets, follower_counts)

        assert len(metrics) == 1
        assert metrics[0]['views'] == 1000
        assert metrics[0]['views_per_follower'] == 2.0  # 1000 / 500
        assert metrics[0]['total_engagements'] == 70  # 50+10+5+3+2
        assert metrics[0]['engagement_per_view'] == 0.07  # 70 / 1000

    def test_zero_views(self):
        tweets = [_make_tweet(views=0)]
        metrics = _compute_metrics(tweets, {'alice': 500})

        assert metrics[0]['views'] == 0
        assert metrics[0]['views_per_follower'] == 0.0
        assert metrics[0]['engagement_per_view'] == 0.0

    def test_zero_followers(self):
        tweets = [_make_tweet(views=1000)]
        metrics = _compute_metrics(tweets, {'alice': 0})

        assert metrics[0]['views_per_follower'] == 0.0

    def test_missing_followers(self):
        tweets = [_make_tweet(views=1000)]
        metrics = _compute_metrics(tweets, {})  # no entry for alice

        assert metrics[0]['views_per_follower'] == 0.0


class TestComputeBonuses:

    def test_single_tweet_gets_full_bonus(self):
        metrics = [{
            'views': 1000,
            'views_per_follower': 2.0,
            'total_engagements': 70,
            'engagement_per_view': 0.07,
        }]

        bonuses = _compute_bonuses(metrics)

        # Single tweet is max in all metrics: 4 * 2.5% = 10%
        assert len(bonuses) == 1
        assert bonuses[0] == pytest.approx(0.10)

    def test_two_tweets_proportional(self):
        metrics = [
            {'views': 1000, 'views_per_follower': 2.0, 'total_engagements': 100, 'engagement_per_view': 0.1},
            {'views': 500, 'views_per_follower': 1.0, 'total_engagements': 50, 'engagement_per_view': 0.1},
        ]

        bonuses = _compute_bonuses(metrics)

        # Tweet 1 is max in all metrics except engagement_per_view (tied)
        assert bonuses[0] == pytest.approx(0.10)
        # Tweet 2: views=500/1000*2.5% + vpf=1/2*2.5% + eng=50/100*2.5% + epv=0.1/0.1*2.5%
        # = 1.25% + 1.25% + 1.25% + 2.5% = 6.25%
        assert bonuses[1] == pytest.approx(0.0625)

    def test_all_identical_tweets_get_full_bonus(self):
        metrics = [
            {'views': 500, 'views_per_follower': 1.0, 'total_engagements': 50, 'engagement_per_view': 0.1},
            {'views': 500, 'views_per_follower': 1.0, 'total_engagements': 50, 'engagement_per_view': 0.1},
            {'views': 500, 'views_per_follower': 1.0, 'total_engagements': 50, 'engagement_per_view': 0.1},
        ]

        bonuses = _compute_bonuses(metrics)

        for bonus in bonuses:
            assert bonus == pytest.approx(0.10)

    def test_all_zero_metrics(self):
        metrics = [
            {'views': 0, 'views_per_follower': 0.0, 'total_engagements': 0, 'engagement_per_view': 0.0},
            {'views': 0, 'views_per_follower': 0.0, 'total_engagements': 0, 'engagement_per_view': 0.0},
        ]

        bonuses = _compute_bonuses(metrics)

        for bonus in bonuses:
            assert bonus == 0.0


class TestCalculatePerformanceBonus:

    @patch('bitcast.validator.tweet_bonus.performance_bonus._save_bonus_results')
    def test_score_is_multiplied(self, mock_save):
        tweets = [_make_tweet(score=2.0, views=1000, favorites=50, retweets=10,
                              replies=5, quotes=3, bookmarks=2)]
        follower_counts = {'alice': 500}

        result = calculate_performance_bonus(tweets, follower_counts, 'test_pool', 'brief_1')

        # Single tweet: full 10% bonus, score = 2.0 * 1.10 = 2.2
        assert len(result) == 1
        assert result[0]['score'] == pytest.approx(2.2)
        assert result[0]['performance_bonus_pct'] == 10.0

    @patch('bitcast.validator.tweet_bonus.performance_bonus._save_bonus_results')
    def test_bonus_pct_field_added(self, mock_save):
        tweets = [
            _make_tweet(author='alice', score=1.0, views=1000),
            _make_tweet(author='bob', score=1.0, views=500),
        ]
        follower_counts = {'alice': 500, 'bob': 500}

        result = calculate_performance_bonus(tweets, follower_counts, 'test_pool', 'brief_1')

        assert 'performance_bonus_pct' in result[0]
        assert 'performance_bonus_pct' in result[1]
        # Alice has higher views, should have higher bonus
        assert result[0]['performance_bonus_pct'] >= result[1]['performance_bonus_pct']

    @patch('bitcast.validator.tweet_bonus.performance_bonus._save_bonus_results')
    def test_empty_tweets_returns_empty(self, mock_save):
        result = calculate_performance_bonus([], {}, 'test_pool', 'brief_1')
        assert result == []
        mock_save.assert_not_called()

    @patch('bitcast.validator.tweet_bonus.performance_bonus._save_bonus_results')
    def test_zero_views_edge_case(self, mock_save):
        tweets = [_make_tweet(score=1.0, views=0, favorites=0, retweets=0,
                              replies=0, quotes=0, bookmarks=0)]

        result = calculate_performance_bonus(tweets, {'alice': 100}, 'test_pool', 'brief_1')

        # All metrics zero except views_per_follower (0/100=0) → 0% bonus
        assert result[0]['performance_bonus_pct'] == 0.0
        assert result[0]['score'] == pytest.approx(1.0)

    @patch('bitcast.validator.tweet_bonus.performance_bonus._save_bonus_results')
    def test_zero_followers_edge_case(self, mock_save):
        tweets = [_make_tweet(score=1.0, views=1000)]

        result = calculate_performance_bonus(tweets, {'alice': 0}, 'test_pool', 'brief_1')

        # Single tweet still gets full bonus for non-zero metrics
        # views=1000 (max, 2.5%), vpf=0 (0/0, single tweet with 0 gets full 2.5%),
        # Actually vpf=0 but max is also 0 so 0% for that metric
        # engagements=70 (max, 2.5%), epv=70/1000 (max, 2.5%) = 7.5%
        assert result[0]['performance_bonus_pct'] == 7.5
