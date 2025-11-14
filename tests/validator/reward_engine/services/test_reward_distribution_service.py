"""Essential tests for reward distribution service."""

import pytest
import numpy as np

from bitcast.validator.reward_engine.services.reward_distribution_service import RewardDistributionService
from bitcast.validator.reward_engine.models.emission_target import EmissionTarget
from bitcast.validator.reward_engine.models.evaluation_result import (
    EvaluationResultCollection,
    EvaluationResult
)


@pytest.fixture
def service():
    """Create reward distribution service."""
    return RewardDistributionService()


class TestExtractRawWeightsMatrix:
    """Test extraction of weights from emission targets."""
    
    def test_extracts_weights_from_targets(self, service):
        """Should extract raw weights matrix from emission targets."""
        targets = [
            EmissionTarget(
                brief_id='brief1',
                usd_target=100.0,
                allocation_details={'per_uid_weights': [10.0, 5.0]}
            ),
            EmissionTarget(
                brief_id='brief2',
                usd_target=50.0,
                allocation_details={'per_uid_weights': [3.0, 7.0]}
            )
        ]
        
        matrix = service._extract_raw_weights_matrix(targets, num_uids=2)
        
        # Should create 2x2 matrix
        assert matrix.shape == (2, 2)
        # Should have correct values
        assert matrix[0, 0] == 10.0  # UID 0, brief 1
        assert matrix[1, 0] == 5.0   # UID 1, brief 1
        assert matrix[0, 1] == 3.0   # UID 0, brief 2
        assert matrix[1, 1] == 7.0   # UID 1, brief 2
    
    def test_handles_empty_targets(self, service):
        """Should handle empty targets list."""
        matrix = service._extract_raw_weights_matrix([], num_uids=2)
        
        # Should return empty matrix
        assert matrix.shape[0] == 2
        assert matrix.shape[1] == 0


class TestNormalizeWeights:
    """Test weight normalization."""
    
    def test_normalizes_weights_to_sum_to_one(self, service):
        """Should normalize weights to sum to 1.0."""
        raw_weights = np.array([
            [10.0, 5.0],
            [5.0, 10.0]
        ])
        briefs = [
            {'id': 'brief1', 'budget': 100.0},
            {'id': 'brief2', 'budget': 100.0}
        ]
        uids = [0, 1]
        
        rewards, rewards_matrix, percentages = service._normalize_weights(raw_weights, briefs, uids)
        
        # Rewards should sum to 1.0
        assert abs(np.sum(rewards) - 1.0) < 1e-6
    
    def test_handles_empty_weights(self, service):
        """Should handle empty weights matrix."""
        raw_weights = np.array([]).reshape(2, 0)
        briefs = []
        uids = [0, 1]
        
        rewards, rewards_matrix, percentages = service._normalize_weights(raw_weights, briefs, uids)
        
        # Should return zeros with UID 0 getting fallback
        assert len(rewards) == 2


class TestErrorFallback:
    """Test error fallback behavior."""
    
    def test_returns_rewards_to_burn_uid(self, service):
        """Should give all rewards to burn UID (0) on error."""
        uids = [0, 1, 2]
        
        rewards, stats = service._error_fallback(uids)
        
        # UID 0 should get 1.0, others 0.0
        assert rewards[0] == 1.0
        assert rewards[1] == 0.0
        assert rewards[2] == 0.0
        # Should create stats for all UIDs
        assert len(stats) == 3
