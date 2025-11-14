"""Essential tests for score aggregation service."""

import pytest
import numpy as np

from bitcast.validator.reward_engine.services.score_aggregation_service import ScoreAggregationService
from bitcast.validator.reward_engine.models.evaluation_result import (
    EvaluationResultCollection,
    EvaluationResult
)


@pytest.fixture
def service():
    """Create score aggregation service."""
    return ScoreAggregationService()


class TestAggregateScores:
    """Test score aggregation into matrix."""
    
    def test_aggregates_single_uid_single_brief(self, service):
        """Should create score matrix from evaluation results."""
        results = EvaluationResultCollection()
        results.results = {
            0: EvaluationResult(
                uid=0,
                platform='twitter',
                account_results={},
                aggregated_scores={'brief1': 100.0}
            )
        }
        
        briefs = [{'id': 'brief1', 'pool': 'tao'}]
        uids = [0]
        
        score_matrix = service.aggregate_scores(results, briefs, uids)
        
        # Should create 1x1 matrix
        assert score_matrix.matrix.shape == (1, 1)
        assert score_matrix.matrix[0, 0] == 100.0
    
    def test_aggregates_multiple_uids_multiple_briefs(self, service):
        """Should aggregate scores across UIDs and briefs."""
        results = EvaluationResultCollection()
        results.results = {
            0: EvaluationResult(
                uid=0,
                platform='twitter',
                account_results={},
                aggregated_scores={'brief1': 100.0, 'brief2': 50.0}
            ),
            1: EvaluationResult(
                uid=1,
                platform='twitter',
                account_results={},
                aggregated_scores={'brief1': 75.0, 'brief2': 25.0}
            )
        }
        
        briefs = [{'id': 'brief1', 'pool': 'tao'}, {'id': 'brief2', 'pool': 'tao'}]
        uids = [0, 1]
        
        score_matrix = service.aggregate_scores(results, briefs, uids)
        
        # Should create 2x2 matrix
        assert score_matrix.matrix.shape == (2, 2)
        # Check values
        assert score_matrix.matrix[0, 0] == 100.0  # UID 0, brief 1
        assert score_matrix.matrix[0, 1] == 50.0   # UID 0, brief 2
        assert score_matrix.matrix[1, 0] == 75.0   # UID 1, brief 1
        assert score_matrix.matrix[1, 1] == 25.0   # UID 1, brief 2
    
    def test_handles_missing_uid_in_results(self, service):
        """Should handle UIDs not present in results."""
        results = EvaluationResultCollection()
        results.results = {
            0: EvaluationResult(
                uid=0,
                platform='twitter',
                account_results={},
                aggregated_scores={'brief1': 100.0}
            )
        }
        
        briefs = [{'id': 'brief1', 'pool': 'tao'}]
        uids = [0, 1, 2]  # UIDs 1 and 2 not in results
        
        score_matrix = service.aggregate_scores(results, briefs, uids)
        
        # Should create 3x1 matrix
        assert score_matrix.matrix.shape == (3, 1)
        # UID 0 has score, others should be 0
        assert score_matrix.matrix[0, 0] == 100.0
        assert score_matrix.matrix[1, 0] == 0.0
        assert score_matrix.matrix[2, 0] == 0.0
    
    def test_handles_missing_brief_in_scores(self, service):
        """Should handle brief not present in UID's scores."""
        results = EvaluationResultCollection()
        results.results = {
            0: EvaluationResult(
                uid=0,
                platform='twitter',
                account_results={},
                aggregated_scores={'brief1': 100.0}  # Only has brief1
            )
        }
        
        briefs = [{'id': 'brief1', 'pool': 'tao'}, {'id': 'brief2', 'pool': 'tao'}]
        uids = [0]
        
        score_matrix = service.aggregate_scores(results, briefs, uids)
        
        # Should create 1x2 matrix
        assert score_matrix.matrix.shape == (1, 2)
        # Should have score for brief1, zero for brief2
        assert score_matrix.matrix[0, 0] == 100.0
        assert score_matrix.matrix[0, 1] == 0.0
    
    def test_handles_empty_results(self, service):
        """Should handle empty evaluation results."""
        results = EvaluationResultCollection()
        briefs = [{'id': 'brief1', 'pool': 'tao'}]
        uids = [0, 1]
        
        score_matrix = service.aggregate_scores(results, briefs, uids)
        
        # Should create 2x1 matrix of zeros
        assert score_matrix.matrix.shape == (2, 1)
        assert np.all(score_matrix.matrix == 0)

