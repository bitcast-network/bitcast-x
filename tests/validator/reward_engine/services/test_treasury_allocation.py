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

