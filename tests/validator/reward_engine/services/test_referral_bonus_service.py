"""Tests for dynamic referral bonus computation."""

import pytest

from bitcast.validator.reward_engine.services.referral_bonus_service import compute_referral_reward


class TestComputeReferralReward:

    @pytest.mark.parametrize("followers,influence,expected", [
        (0, 0.0, 0.0),
        (999, 0.5, 0.0),
        (25_000, 1_000, 100.0),
        (100_000, 5_000, 100.0),
    ])
    def test_boundaries(self, followers, influence, expected):
        assert compute_referral_reward(followers, influence) == expected

    def test_mid_range_is_between_bounds(self):
        result = compute_referral_reward(5_000, 50)
        assert 0 < result < 100
