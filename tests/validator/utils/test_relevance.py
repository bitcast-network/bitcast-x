"""Tests for the relevance gradient helpers (social discovery v2)."""

import pytest

from bitcast.validator.utils.relevance import (
    beta_params,
    smoothed_relevance_ratio,
    passes_relevance_gate,
)


class TestSmoothedRatio:
    def test_beta_params_from_mean_strength(self):
        alpha, beta = beta_params(0.02, 15)
        assert alpha == pytest.approx(0.3)
        assert beta == pytest.approx(14.7)

    def test_high_volume_off_topic_account_stays_low(self):
        # 1000 tweets, 30 on-topic -> ~3%, below tao's 5% gate.
        ratio = smoothed_relevance_ratio(30, 1000, 0.02, 15)
        assert ratio == pytest.approx(30.3 / 1015, rel=1e-6)
        assert ratio < 0.05

    def test_small_sample_is_shrunk_toward_prior(self):
        # 2 of 3 on-topic looks like 67% raw, but smoothing pulls it far down.
        ratio = smoothed_relevance_ratio(2, 3, 0.02, 15)
        assert ratio < 0.20
        assert ratio == pytest.approx(2.3 / 18, rel=1e-6)

    def test_zero_tweets_returns_prior_mean(self):
        ratio = smoothed_relevance_ratio(0, 0, 0.02, 15)
        assert ratio == pytest.approx(0.02, rel=1e-6)

    def test_ratio_always_positive(self):
        # Safe to use directly as a personalization weight.
        assert smoothed_relevance_ratio(0, 5000, 0.02, 15) > 0


class TestRelevanceGate:
    def test_tao_excludes_others_includes(self):
        ratio = smoothed_relevance_ratio(30, 1000, 0.02, 15)  # ~2.98%
        assert passes_relevance_gate(30, ratio, 0.05, 1) is False   # tao
        assert passes_relevance_gate(30, ratio, 0.02, 1) is True    # broader pool

    def test_count_floor_blocks_tiny_samples(self):
        # 1 relevant tweet clears ratio but min_relevant_tweets=2 blocks it.
        ratio = smoothed_relevance_ratio(1, 1, 0.02, 15)
        assert passes_relevance_gate(1, ratio, 0.02, 2) is False
        assert passes_relevance_gate(1, ratio, 0.02, 1) is True
