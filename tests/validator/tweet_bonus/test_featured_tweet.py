"""Tests for featured tweet selection and bonus logic."""

import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from bitcast.validator.tweet_bonus.featured_tweet import (
    select_featured_tweet,
    apply_featured_tweet_bonus,
    FEATURED_DIR,
)


def _make_tweet(author="alice", tweet_id=None, score=1.0, views=1000):
    return {
        'tweet_id': tweet_id or f'tweet_{author}',
        'author': author,
        'score': score,
        'views_count': views,
        'favorite_count': 10,
        'retweet_count': 5,
        'reply_count': 2,
        'quote_count': 1,
        'bookmark_count': 0,
    }


def _make_brief(brief_id="brief_1", end_date=None):
    if end_date is None:
        # Default to yesterday so selection triggers
        end_date = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime('%Y-%m-%d')
    return {'id': brief_id, 'end_date': end_date}


class TestSelectFeaturedTweet:

    def test_selection_too_early(self):
        """Returns None when more than 1 day before end_date."""
        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime('%Y-%m-%d')
        brief = _make_brief(end_date=future)
        tweets = [_make_tweet()]

        result = select_featured_tweet(tweets, brief, 'test_pool')

        assert result is None

    def test_selection_triggers(self, tmp_path, monkeypatch):
        """Selects a tweet when within 1 day of end_date."""
        monkeypatch.setattr(
            'bitcast.validator.tweet_bonus.featured_tweet.FEATURED_DIR', tmp_path
        )
        tweets = [
            _make_tweet(author='alice', tweet_id='t1', views=5000),
            _make_tweet(author='bob', tweet_id='t2', views=3000),
            _make_tweet(author='carol', tweet_id='t3', views=1000),
        ]
        brief = _make_brief()

        result = select_featured_tweet(tweets, brief, 'test_pool')

        assert result is not None
        assert result['tweet_id'] in ['t1', 't2', 't3']
        assert result['brief_id'] == 'brief_1'
        assert result['selection_method'] == 'sha256_mod'
        assert len(result['selection_pool']) == 3

    def test_selection_sticky(self, tmp_path, monkeypatch):
        """Returns same selection on second call (reads from disk)."""
        monkeypatch.setattr(
            'bitcast.validator.tweet_bonus.featured_tweet.FEATURED_DIR', tmp_path
        )
        tweets = [
            _make_tweet(author='alice', tweet_id='t1', views=5000),
            _make_tweet(author='bob', tweet_id='t2', views=3000),
        ]
        brief = _make_brief()

        first = select_featured_tweet(tweets, brief, 'test_pool')
        second = select_featured_tweet(tweets, brief, 'test_pool')

        assert first == second

    def test_selection_deterministic(self, tmp_path, monkeypatch):
        """Same top-5 always produces same pick."""
        monkeypatch.setattr(
            'bitcast.validator.tweet_bonus.featured_tweet.FEATURED_DIR', tmp_path
        )
        tweets = [
            _make_tweet(author='a', tweet_id='t1', views=5000),
            _make_tweet(author='b', tweet_id='t2', views=4000),
            _make_tweet(author='c', tweet_id='t3', views=3000),
            _make_tweet(author='d', tweet_id='t4', views=2000),
            _make_tweet(author='e', tweet_id='t5', views=1000),
        ]
        brief = _make_brief(brief_id='det_test')

        result1 = select_featured_tweet(tweets, brief, 'pool_a')

        # Re-run with fresh dir (different pool path) but same tweets
        result2 = select_featured_tweet(tweets, brief, 'pool_b')

        assert result1['tweet_id'] == result2['tweet_id']

    def test_fewer_than_5_tweets(self, tmp_path, monkeypatch):
        """Works correctly with fewer than 5 tweets."""
        monkeypatch.setattr(
            'bitcast.validator.tweet_bonus.featured_tweet.FEATURED_DIR', tmp_path
        )
        tweets = [
            _make_tweet(author='alice', tweet_id='t1', views=5000),
            _make_tweet(author='bob', tweet_id='t2', views=3000),
            _make_tweet(author='carol', tweet_id='t3', views=1000),
        ]
        brief = _make_brief()

        result = select_featured_tweet(tweets, brief, 'test_pool')

        assert result is not None
        assert len(result['selection_pool']) == 3


class TestApplyFeaturedTweetBonus:

    def _make_discovery_mock(self, engagements=None):
        mock = MagicMock()
        mock.get_engagements_for_tweet.return_value = engagements or {}
        return mock

    @patch('bitcast.validator.tweet_bonus.featured_tweet._save_featured_bonus_results')
    def test_bonus_applied_to_retweeters(self, mock_save):
        """Retweeters of the featured tweet get 5% bonus."""
        tweets = [
            _make_tweet(author='alice', score=1.0),
            _make_tweet(author='bob', score=2.0),
        ]
        selection = {'tweet_id': 'featured_1', 'author': 'carol'}
        discovery = self._make_discovery_mock({'alice': 'retweet'})

        result = apply_featured_tweet_bonus(
            tweets, selection, discovery, 'test_pool', 'brief_1'
        )

        # alice retweeted → bonus
        assert result[0]['score'] == pytest.approx(1.0 * 1.05)
        assert result[0]['featured_tweet_bonus'] is True
        # bob did not retweet → no bonus
        assert result[1]['score'] == pytest.approx(2.0)
        assert result[1]['featured_tweet_bonus'] is False

    @patch('bitcast.validator.tweet_bonus.featured_tweet._save_featured_bonus_results')
    def test_bonus_applied_to_featured_author(self, mock_save):
        """Featured tweet author gets the 5% bonus."""
        tweets = [
            _make_tweet(author='carol', score=3.0),
            _make_tweet(author='dave', score=1.0),
        ]
        selection = {'tweet_id': 'featured_1', 'author': 'carol'}
        discovery = self._make_discovery_mock({})

        result = apply_featured_tweet_bonus(
            tweets, selection, discovery, 'test_pool', 'brief_1'
        )

        assert result[0]['score'] == pytest.approx(3.0 * 1.05)
        assert result[0]['featured_tweet_bonus'] is True
        assert result[1]['featured_tweet_bonus'] is False

    @patch('bitcast.validator.tweet_bonus.featured_tweet._save_featured_bonus_results')
    def test_bonus_not_applied_to_non_retweeters(self, mock_save):
        """Non-retweeters are unchanged."""
        tweets = [
            _make_tweet(author='dave', score=2.0),
            _make_tweet(author='eve', score=4.0),
        ]
        selection = {'tweet_id': 'featured_1', 'author': 'carol'}
        discovery = self._make_discovery_mock({'alice': 'retweet'})

        result = apply_featured_tweet_bonus(
            tweets, selection, discovery, 'test_pool', 'brief_1'
        )

        assert result[0]['score'] == pytest.approx(2.0)
        assert result[0]['featured_tweet_bonus'] is False
        assert result[1]['score'] == pytest.approx(4.0)
        assert result[1]['featured_tweet_bonus'] is False

    @patch('bitcast.validator.tweet_bonus.featured_tweet._save_featured_bonus_results')
    def test_bonus_stacks_with_performance(self, mock_save):
        """Featured bonus stacks multiplicatively on top of existing score."""
        # Simulate a tweet that already had performance bonus applied (score=2.2)
        tweets = [_make_tweet(author='alice', score=2.2)]
        selection = {'tweet_id': 'featured_1', 'author': 'carol'}
        discovery = self._make_discovery_mock({'alice': 'retweet'})

        result = apply_featured_tweet_bonus(
            tweets, selection, discovery, 'test_pool', 'brief_1'
        )

        assert result[0]['score'] == pytest.approx(2.2 * 1.05)

    @patch('bitcast.validator.tweet_bonus.featured_tweet._save_featured_bonus_results')
    def test_featured_flag_in_output(self, mock_save):
        """featured_tweet_bonus flag is set correctly on all tweets."""
        tweets = [
            _make_tweet(author='alice', score=1.0),
            _make_tweet(author='bob', score=1.0),
            _make_tweet(author='carol', score=1.0),
        ]
        selection = {'tweet_id': 'featured_1', 'author': 'carol'}
        discovery = self._make_discovery_mock({'alice': 'retweet'})

        result = apply_featured_tweet_bonus(
            tweets, selection, discovery, 'test_pool', 'brief_1'
        )

        assert result[0]['featured_tweet_bonus'] is True   # alice retweeted
        assert result[1]['featured_tweet_bonus'] is False   # bob did nothing
        assert result[2]['featured_tweet_bonus'] is True    # carol is author

    def test_none_selection_returns_unchanged(self):
        """When featured_selection is None, tweets are returned unchanged."""
        tweets = [_make_tweet(author='alice', score=5.0)]

        result = apply_featured_tweet_bonus(
            tweets, None, MagicMock(), 'test_pool', 'brief_1'
        )

        assert result[0]['score'] == 5.0
        assert 'featured_tweet_bonus' not in result[0]
