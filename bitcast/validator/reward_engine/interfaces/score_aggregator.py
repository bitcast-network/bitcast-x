"""Abstract interface for score aggregation strategies."""

from abc import ABC, abstractmethod
from typing import List, Dict, Any


class ScoreAggregator(ABC):
    """Abstract interface for score aggregation strategies."""
    
    @abstractmethod
    def aggregate_scores(
        self, 
        evaluation_results: "EvaluationResultCollection",
        briefs: List[Dict[str, Any]],
        uids: List[int]
    ) -> "ScoreMatrix":
        """Aggregate evaluation results into a score matrix.
        
        Args:
            evaluation_results: Collection of evaluation results by UID
            briefs: List of brief configurations
            uids: Ordered list of UIDs from metagraph (for correct matrix indexing)
        """
        pass 