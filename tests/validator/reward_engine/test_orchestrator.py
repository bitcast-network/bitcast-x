"""Essential tests for reward orchestrator."""

import pytest
import numpy as np
from unittest.mock import Mock, AsyncMock, patch

from bitcast.validator.reward_engine.orchestrator import RewardOrchestrator


@pytest.fixture
def orchestrator():
    """Create orchestrator."""
    return RewardOrchestrator()


@pytest.fixture
def mock_validator():
    """Create mock validator instance."""
    validator = Mock()
    validator.wallet = Mock()
    validator.wallet.hotkey = Mock()
    validator.wallet.hotkey.ss58_address = "test_hotkey"
    validator.metagraph = Mock()
    validator.metagraph.n = 10
    return validator


class TestFallbackRewards:
    """Test fallback rewards when normal calculation cannot proceed."""
    
    def test_allocates_to_treasury_uid(self, orchestrator):
        """Should allocate rewards to treasury UID when using fallback."""
        # Mock treasury UID to be different from burn UID (0)
        with patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_UID', 106), \
             patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_PERCENTAGE', 1.0):
            uids = [0, 1, 2, 3, 106]
            
            rewards, stats = orchestrator._fallback_rewards(uids)
            
            # Treasury UID gets 1.0 (allocated from burn UID 0), others get 0
            assert len(rewards) == 5
            treasury_idx = uids.index(106)
            assert rewards[0] == 0.0  # Burn UID after treasury allocation
            assert rewards[1] == 0.0
            assert rewards[2] == 0.0
            assert rewards[3] == 0.0
            assert rewards[treasury_idx] == 1.0  # Treasury UID receives allocation
    
    def test_creates_stats_for_all_uids(self, orchestrator):
        """Should create stats structure for all UIDs."""
        uids = [5, 10]
        
        rewards, stats = orchestrator._fallback_rewards(uids)
        
        assert len(stats) == 2
        assert stats[0]['uid'] == 5
        assert stats[1]['uid'] == 10
        assert 'scores' in stats[0]


class TestCalculateRewards:
    """Test main reward calculation (basic smoke tests)."""
    
    @pytest.mark.asyncio
    async def test_handles_no_briefs(self, orchestrator, mock_validator):
        """Should allocate to treasury UID when no briefs."""
        with patch('bitcast.validator.reward_engine.orchestrator.get_briefs') as mock_get_briefs, \
             patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_UID', 106), \
             patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_PERCENTAGE', 1.0):
            mock_get_briefs.return_value = []
            
            uids = [0, 1, 106]
            rewards, stats = await orchestrator.calculate_rewards(mock_validator, uids)
            
            # Should allocate to treasury UID from burn UID
            assert len(rewards) == 3
            treasury_idx = uids.index(106)
            assert rewards[0] == 0.0  # Burn UID after allocation
            assert rewards[1] == 0.0
            assert rewards[treasury_idx] == 1.0  # Treasury UID receives allocation
    
    @pytest.mark.asyncio
    async def test_handles_brief_fetch_error(self, orchestrator, mock_validator):
        """Should handle errors when fetching briefs."""
        with patch('bitcast.validator.reward_engine.orchestrator.get_briefs') as mock_get_briefs, \
             patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_UID', 106), \
             patch('bitcast.validator.reward_engine.services.treasury_allocation.SUBNET_TREASURY_PERCENTAGE', 1.0):
            mock_get_briefs.side_effect = RuntimeError("API error")
            
            uids = [0, 1, 106]
            rewards, stats = await orchestrator.calculate_rewards(mock_validator, uids)
            
            # Should allocate to treasury UID, not crash
            assert len(rewards) == 3
            treasury_idx = uids.index(106)
            assert rewards[0] == 0.0  # Burn UID after allocation
            assert rewards[1] == 0.0
            assert rewards[treasury_idx] == 1.0  # Treasury UID receives allocation
