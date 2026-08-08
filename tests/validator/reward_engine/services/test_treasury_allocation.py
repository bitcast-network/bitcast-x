"""Essential tests for treasury allocation."""

import pytest
import numpy as np
from unittest.mock import patch

from bitcast.validator.reward_engine.services.treasury_allocation import allocate_subnet_treasury


class TestTreasuryAllocation:
    """Test subnet treasury allocation."""
    
    def test_allocates_treasury_percentage(self):
        """Should allocate from burn UID (0) to treasury UID."""
        with patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_PERCENTAGE', 0.1):
            with patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_UID', 106):
                # UID 0 (burn) has 0.5, we'll take 0.1 and give to treasury
                rewards = np.array([0.5, 0.3, 0.2, 0.0])  # Sum = 1.0
                uids = [0, 1, 2, 106]  # UID 0 is burn, 106 is treasury
                
                final_rewards = allocate_subnet_treasury(rewards, uids)
                
                # Burn UID (0) should have 0.1 removed
                assert abs(final_rewards[0] - 0.4) < 1e-6  # Was 0.5, now 0.4
                
                # Treasury (106) should receive 0.1
                treasury_idx = uids.index(106)
                assert abs(final_rewards[treasury_idx] - 0.1) < 1e-6
                
                # Others unchanged
                assert abs(final_rewards[1] - 0.3) < 1e-6
                assert abs(final_rewards[2] - 0.2) < 1e-6
                
                # Total should still sum to 1.0
                assert abs(np.sum(final_rewards) - 1.0) < 1e-6
    
    def test_handles_treasury_uid_not_in_list(self):
        """Should handle case where treasury UID is not in the list."""
        with patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_PERCENTAGE', 10.0):
            with patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_UID', 999):
                rewards = np.array([0.5, 0.5])
                uids = [0, 1]  # Treasury UID 999 not present
                
                final_rewards = allocate_subnet_treasury(rewards, uids)
                
                # Should return unchanged (can't allocate to missing UID)
                assert np.allclose(final_rewards, rewards)
    
    def test_handles_zero_percentage(self):
        """Should handle 0% treasury allocation."""
        with patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_PERCENTAGE', 0.0):
            with patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_UID', 106):
                rewards = np.array([0.5, 0.5, 0.0])
                uids = [0, 1, 106]
                
                final_rewards = allocate_subnet_treasury(rewards, uids)
                
                # With 0% allocation, rewards should be unchanged
                assert np.allclose(final_rewards, rewards)
    
    def test_handles_empty_rewards(self):
        """Should handle empty rewards array."""
        with patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_PERCENTAGE', 10.0):
            with patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_UID', 106):
                rewards = np.array([])
                uids = []
                
                final_rewards = allocate_subnet_treasury(rewards, uids)
                
                # Should return empty array
                assert len(final_rewards) == 0



class TestShippedTreasuryConstants:
    """Pins the constants this validator actually ships.

    Every test above patches SUBNET_TREASURY_PERCENTAGE/UID, so none of them
    notice a change to the deployed values. Both are consensus-critical: they
    change the emitted weight vector, so a validator running different numbers
    disagrees with the network. This repo is the UID 0 reference validator for
    mechid 1, so a divergence here moves the whole subnet.
    """

    def test_shipped_constants(self):
        from bitcast.validator.utils import config

        assert config.SUBNET_TREASURY_UID == 155
        assert config.SUBNET_TREASURY_PERCENTAGE == 1.0

    def test_full_residual_diverted_off_burn_uid(self):
        from bitcast.validator.utils import config

        rewards = np.array([0.97, 0.03, 0.0])
        uids = [0, 7, config.SUBNET_TREASURY_UID]

        final_rewards = allocate_subnet_treasury(rewards, uids)

        assert final_rewards[0] == 0.0
        assert final_rewards[1] == 0.03
        assert final_rewards[2] == 0.97
        assert final_rewards.sum() == 1.0

    def test_allocation_capped_by_what_burn_uid_holds(self):
        """Miners earning most of the emission leaves little to divert."""
        from bitcast.validator.utils import config

        rewards = np.array([0.1, 0.9, 0.0])
        uids = [0, 7, config.SUBNET_TREASURY_UID]

        final_rewards = allocate_subnet_treasury(rewards, uids)

        assert final_rewards[0] == 0.0
        assert final_rewards[2] == 0.1
        assert final_rewards.sum() == 1.0
