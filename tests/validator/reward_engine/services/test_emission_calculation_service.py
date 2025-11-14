"""Essential tests for emission calculation service."""

import pytest
import numpy as np
from unittest.mock import patch

from bitcast.validator.reward_engine.services.emission_calculation_service import EmissionCalculationService
from bitcast.validator.reward_engine.models.score_matrix import ScoreMatrix


@pytest.fixture
def service():
    """Create emission calculation service."""
    return EmissionCalculationService()


@pytest.fixture
def mock_pricing():
    """Mock token pricing to avoid API calls."""
    with patch('bitcast.validator.reward_engine.services.emission_calculation_service.get_bitcast_alpha_price', return_value=0.10):
        with patch('bitcast.validator.reward_engine.services.emission_calculation_service.get_total_miner_emissions', return_value=1000.0):
            yield


class TestCalculateTargets:
    """Test emission target calculation."""
    
    def test_calculates_targets_for_single_brief(self, service, mock_pricing):
        """Should calculate emission targets from USD scores."""
        # 2 miners, 1 brief with USD targets
        matrix = np.array([[100.0], [50.0]])
        score_matrix = ScoreMatrix(matrix=matrix)
        briefs = [{'id': 'brief1', 'budget': 150.0}]
        
        targets = service.calculate_targets(score_matrix, briefs)
        
        assert len(targets) == 1
        assert targets[0].brief_id == 'brief1'
        assert targets[0].usd_target == 150.0
        # Should have allocation details
        assert 'per_uid_weights' in targets[0].allocation_details
    
    def test_calculates_targets_for_multiple_briefs(self, service, mock_pricing):
        """Should calculate targets for multiple briefs."""
        # 2 miners, 2 briefs
        matrix = np.array([
            [100.0, 50.0],  # UID 0: brief1=$100, brief2=$50
            [50.0, 100.0]   # UID 1: brief1=$50, brief2=$100
        ])
        score_matrix = ScoreMatrix(matrix=matrix)
        briefs = [
            {'id': 'brief1', 'budget': 150.0},
            {'id': 'brief2', 'budget': 150.0}
        ]
        
        targets = service.calculate_targets(score_matrix, briefs)
        
        assert len(targets) == 2
        assert targets[0].brief_id == 'brief1'
        assert targets[1].brief_id == 'brief2'
    
    def test_handles_empty_matrix(self, service, mock_pricing):
        """Should handle empty score matrix gracefully."""
        matrix = np.array([]).reshape(0, 0)
        score_matrix = ScoreMatrix(matrix=matrix)
        briefs = []
        
        targets = service.calculate_targets(score_matrix, briefs)
        
        assert targets == []
    
    def test_handles_zero_scores(self, service, mock_pricing):
        """Should handle miners with zero scores."""
        matrix = np.array([[0.0], [0.0]])
        score_matrix = ScoreMatrix(matrix=matrix)
        briefs = [{'id': 'brief1', 'budget': 100.0}]
        
        targets = service.calculate_targets(score_matrix, briefs)
        
        assert len(targets) == 1
        # Should still create target even with zero scores
        assert targets[0].usd_target == 0.0


class TestCalculateRawWeights:
    """Test USD to weight conversion."""
    
    def test_converts_usd_to_weights(self, service, mock_pricing):
        """Should convert USD targets to raw weights using alpha price."""
        # With alpha price = 0.10 and total emissions = 1000.0
        # USD $100 = 1000 alpha = weight proportional to pool
        usd_matrix = np.array([[100.0], [50.0]])
        
        weights = service._calculate_raw_weights(usd_matrix)
        
        # Should return weights (not zeros)
        assert weights.shape == usd_matrix.shape
        assert np.sum(weights) > 0
        # Higher USD should give higher weight
        assert weights[0, 0] > weights[1, 0]
    
    def test_handles_zero_emissions(self, service):
        """Should handle zero emissions gracefully."""
        with patch('bitcast.validator.reward_engine.services.emission_calculation_service.get_bitcast_alpha_price', return_value=0.10):
            with patch('bitcast.validator.reward_engine.services.emission_calculation_service.get_total_miner_emissions', return_value=0.0):
                usd_matrix = np.array([[100.0]])
                
                weights = service._calculate_raw_weights(usd_matrix)
                
                # Should return zeros when no emissions available
                assert np.all(weights == 0)

