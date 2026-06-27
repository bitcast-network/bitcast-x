"""Tests for account-level AI detection (social discovery v2)."""

import unittest.mock as mock
from datetime import date, datetime, timezone

import pytest

from bitcast.validator.social_discovery import ai_detection
from bitcast.validator.social_discovery.social_discovery import (
    DISCOVERY_REFERENCE_DATE,
    DISCOVERY_CYCLE_DAYS,
)


def _tweet(tid, text):
    return {"tweet_id": tid, "text": text}


class TestCycleBucket:
    """The sampling seed bucket aligns to the discovery cycle, not the UTC day."""

    def _bucket_on(self, d):
        fake = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        with mock.patch("bitcast.validator.social_discovery.ai_detection.datetime") as dt:
            dt.now.return_value = fake
            return ai_detection.current_date_bucket()

    def test_same_cycle_same_bucket_across_days(self):
        # Two different UTC days within one cycle must yield the same bucket
        # (this is what the daily bucket got wrong).
        d0 = DISCOVERY_REFERENCE_DATE
        d_mid = date.fromordinal(d0.toordinal() + DISCOVERY_CYCLE_DAYS - 1)
        assert self._bucket_on(d0) == self._bucket_on(d_mid)

    def test_adjacent_cycles_differ(self):
        d0 = DISCOVERY_REFERENCE_DATE
        d_next = date.fromordinal(d0.toordinal() + DISCOVERY_CYCLE_DAYS)
        assert self._bucket_on(d0) != self._bucket_on(d_next)

    def test_midnight_boundary_within_cycle_is_stable(self):
        # 23:59 on a day and 00:01 the next day, both mid-cycle -> same bucket.
        d0 = DISCOVERY_REFERENCE_DATE
        late = datetime(d0.year, d0.month, d0.day, 23, 59, tzinfo=timezone.utc)
        early = datetime(d0.year, d0.month, d0.day + 1, 0, 1, tzinfo=timezone.utc)
        with mock.patch("bitcast.validator.social_discovery.ai_detection.datetime") as dt:
            dt.now.side_effect = [late, early]
            b_late = ai_detection.current_date_bucket()
            b_early = ai_detection.current_date_bucket()
        assert b_late == b_early


class TestBucketize:
    def test_rounds_to_nearest_band(self):
        assert ai_detection.bucketize(0.83, 0.2) == 0.8
        assert ai_detection.bucketize(0.91, 0.2) == 1.0
        assert ai_detection.bucketize(0.07, 0.2) == 0.0
        assert ai_detection.bucketize(0.5, 0.2) == 0.6 or ai_detection.bucketize(0.5, 0.2) == 0.4

    def test_zero_bucket_is_identity(self):
        assert ai_detection.bucketize(0.37, 0) == 0.37


class TestSampleSelection:
    def test_excludes_short_tweets(self):
        tweets = [_tweet("1", "x" * 250), _tweet("2", "short"), _tweet("3", "y" * 300)]
        sample = ai_detection.select_sample_tweets("alice", tweets, "2026-06-27", sample_size=4, min_chars=200)
        assert {t["tweet_id"] for t in sample} == {"1", "3"}

    def test_deterministic_given_seed(self):
        tweets = [_tweet(str(i), "x" * 250) for i in range(20)]
        a = ai_detection.select_sample_tweets("alice", tweets, "2026-06-27", sample_size=4, min_chars=200)
        b = ai_detection.select_sample_tweets("alice", tweets, "2026-06-27", sample_size=4, min_chars=200)
        assert [t["tweet_id"] for t in a] == [t["tweet_id"] for t in b]
        assert len(a) == 4

    def test_seed_varies_by_account_and_bucket(self):
        tweets = [_tweet(str(i), "x" * 250) for i in range(20)]
        a = ai_detection.select_sample_tweets("alice", tweets, "2026-06-27", 4, 200)
        b = ai_detection.select_sample_tweets("bob", tweets, "2026-06-27", 4, 200)
        c = ai_detection.select_sample_tweets("alice", tweets, "2026-06-28", 4, 200)
        # Different account or different bucket should generally pick a different set.
        assert [t["tweet_id"] for t in a] != [t["tweet_id"] for t in b]
        assert [t["tweet_id"] for t in a] != [t["tweet_id"] for t in c]


class TestComputeAccountScore:
    @mock.patch("bitcast.validator.social_discovery.ai_detection.cache_tweet_ai_score")
    @mock.patch("bitcast.validator.social_discovery.ai_detection.get_cached_tweet_ai_score", return_value=None)
    def test_averages_and_buckets(self, _get, _set):
        client = mock.Mock()
        client.analyze_text.side_effect = [0.9, 0.7, 0.8, 1.0]  # mean 0.85 -> bucket 0.8
        tweets = [_tweet(str(i), "x" * 250) for i in range(4)]
        score = ai_detection.compute_account_ai_score("alice", tweets, "2026-06-27", client=client, concurrency=1)
        assert score == 0.8

    def test_no_eligible_tweets_returns_none(self):
        client = mock.Mock()
        tweets = [_tweet("1", "short")]
        score = ai_detection.compute_account_ai_score("alice", tweets, "2026-06-27", client=client, concurrency=1)
        assert score is None
        client.analyze_text.assert_not_called()

    @mock.patch("bitcast.validator.social_discovery.ai_detection.get_cached_tweet_ai_score", return_value=None)
    def test_all_samples_fail_returns_none(self, _get):
        client = mock.Mock()
        client.analyze_text.return_value = None  # every call skipped
        tweets = [_tweet(str(i), "x" * 250) for i in range(3)]
        score = ai_detection.compute_account_ai_score("alice", tweets, "2026-06-27", client=client, concurrency=1)
        assert score is None

    @mock.patch("bitcast.validator.social_discovery.ai_detection.cache_tweet_ai_score")
    def test_uses_per_tweet_cache(self, _set):
        client = mock.Mock()
        with mock.patch("bitcast.validator.social_discovery.ai_detection.get_cached_tweet_ai_score", return_value=0.2):
            tweets = [_tweet(str(i), "x" * 250) for i in range(3)]
            score = ai_detection.compute_account_ai_score("alice", tweets, "2026-06-27", client=client, concurrency=1)
        assert score == ai_detection.bucketize(0.2)
        client.analyze_text.assert_not_called()  # all served from cache


class TestComputeAiScores:
    @mock.patch("bitcast.validator.social_discovery.ai_detection.cache_ai_score")
    @mock.patch("bitcast.validator.social_discovery.ai_detection.get_cached_ai_score", return_value=None)
    @mock.patch("bitcast.validator.social_discovery.ai_detection.cache_tweet_ai_score")
    @mock.patch("bitcast.validator.social_discovery.ai_detection.get_cached_tweet_ai_score", return_value=None)
    def test_skips_accounts_with_no_tweets(self, *_):
        client = mock.Mock()
        client.analyze_text.return_value = 0.9
        tweets_map = {"alice": [_tweet(str(i), "x" * 250) for i in range(4)], "bob": []}
        scores = ai_detection.compute_ai_scores(
            ["alice", "bob"], date_bucket="2026-06-27",
            client=client, tweets_provider=lambda u: tweets_map.get(u),
        )
        assert "alice" in scores
        assert "bob" not in scores

    @mock.patch("bitcast.validator.social_discovery.ai_detection.get_cached_ai_score", return_value=0.4)
    def test_account_cache_short_circuits(self, _cache):
        client = mock.Mock()
        scores = ai_detection.compute_ai_scores(
            ["alice"], date_bucket="2026-06-27", client=client, tweets_provider=lambda u: None
        )
        assert scores["alice"] == 0.4
        client.analyze_text.assert_not_called()
