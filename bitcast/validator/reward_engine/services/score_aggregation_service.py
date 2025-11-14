"""Handles score aggregation across platforms and accounts."""

from typing import List, Dict, Any
import numpy as np
from ..interfaces.score_aggregator import ScoreAggregator
from ..models.score_matrix import ScoreMatrix
from ..models.evaluation_result import EvaluationResultCollection


class ScoreAggregationService(ScoreAggregator):
    """Default implementation of score aggregation."""
    
    def aggregate_scores(
        self, 
        evaluation_results: EvaluationResultCollection,
        briefs: List[Dict[str, Any]],
        uids: List[int]
    ) -> ScoreMatrix:
        """Aggregate scores into a matrix aligned with metagraph UID order."""
        uid_to_index = {uid: idx for idx, uid in enumerate(uids)}
        score_matrix = ScoreMatrix.create_empty(len(uids), len(briefs))
        
        for uid, result in evaluation_results.results.items():
            if (uid_idx := uid_to_index.get(uid)) is not None:
                for brief_idx, brief in enumerate(briefs):
                    score = result.aggregated_scores.get(brief["id"], 0.0)
                    score_matrix.set_score(uid_idx, brief_idx, score)
        
        return score_matrix