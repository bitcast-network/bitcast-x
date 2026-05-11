"""Tests for dynamic referral bonus computation."""

from datetime import date
from pathlib import Path
import tempfile

import pytest

from bitcast.validator.account_connection.connection_db import ConnectionDatabase
from bitcast.validator.reward_engine.services.referral_bonus_service import compute_referral_reward
from bitcast.validator.reward_engine.services.referral_bonus_service import ReferralBonusService


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


class TestReferralBonusService:

    @pytest.fixture
    def temp_db(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = Path(f.name)

        yield db_path

        if db_path.exists():
            db_path.unlink()

    def test_get_referral_bonuses_uses_locked_amounts(self, temp_db):
        db = ConnectionDatabase(db_path=temp_db)
        db.upsert_connection(
            tweet_id=123,
            tag="Stitch3-abc-refcode",
            account_username="referee",
            referral_code="refcode",
            referred_by="referrer",
            referee_amount=12.5,
            referrer_amount=7.25,
        )

        referral = db.get_all_connections_with_referrals()[0]
        payout_date = date(2026, 5, 12)
        assert db.set_payout_date(referral["connection_id"], payout_date)

        service = ReferralBonusService(db)
        result = service.get_referral_bonuses(
            payout_date=payout_date,
            account_to_uid={"referee": 1, "referrer": 2},
            account_data={
                "referee": {"followers_count": 100_000, "score": 5_000.0},
            },
        )

        assert result.bonuses == {1: 12.5, 2: 7.25}
        assert result.referrals[0]["computed_referee_amount"] == 12.5
        assert result.referrals[0]["computed_referrer_amount"] == 7.25

    def test_one_referee_one_row_after_retag(self, temp_db):
        """A referee re-tagging collapses into a single row keyed by the referee."""
        db = ConnectionDatabase(db_path=temp_db)
        db.upsert_connection(
            tweet_id=123,
            tag="Stitch3-low-refcode",
            account_username="referee",
            referral_code="refcode",
            referred_by="referrer",
            referee_amount=10.0,
            referrer_amount=10.0,
        )
        db.upsert_connection(
            tweet_id=456,
            tag="Stitch3-high-refcode",
            account_username="referee",
            referral_code="refcode",
            referred_by="referrer",
            referee_amount=30.0,
            referrer_amount=30.0,
        )

        service = ReferralBonusService(db)
        assert service.check_and_activate_referrals({"referee"}) == 1

        connections = db.get_all_connections_with_referrals()
        assert len(connections) == 1
        # Higher locked amount wins, tag/tweet_id refresh to most recent
        assert connections[0]["referee_amount"] == 30.0
        assert connections[0]["tag"] == "Stitch3-high-refcode"
        assert connections[0]["payout_date"] is not None

    def test_retag_with_different_referrer_takes_higher_amount(self, temp_db):
        """When the same referee re-tags with a different referrer at a higher
        amount, the higher-amount referrer wins (per cross-pool max policy)."""
        db = ConnectionDatabase(db_path=temp_db)
        db.upsert_connection(
            tweet_id=100,
            tag="Stitch3-abc123",
            account_username="referee",
            referral_code="alice_code",
            referred_by="alice",
            referee_amount=80.0,
            referrer_amount=80.0,
        )
        db.upsert_connection(
            tweet_id=101,
            tag="Stitch3-def456",
            account_username="referee",
            referral_code="bob_code",
            referred_by="bob",
            referee_amount=90.0,
            referrer_amount=90.0,
        )

        service = ReferralBonusService(db)
        assert service.check_and_activate_referrals({"referee"}) == 1

        activated = [
            c for c in db.get_all_connections_with_referrals()
            if c.get("payout_date") is not None
        ]
        assert len(activated) == 1
        assert activated[0]["referred_by"] == "bob"  # higher amount wins
