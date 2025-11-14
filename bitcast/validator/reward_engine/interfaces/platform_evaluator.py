"""Platform evaluator interfaces - split by evaluation pattern."""

from abc import ABC, abstractmethod
from typing import Any, List, Dict


class PlatformEvaluator(ABC):
    """Base interface for all platform evaluators."""
    
    @abstractmethod
    def platform_name(self) -> str:
        """Return the platform identifier (e.g., 'twitter', 'youtube')."""
        pass


class QueryBasedEvaluator(PlatformEvaluator):
    """
    Evaluator for platforms that work via miner queries.
    
    Use this when:
    - Miners provide content URLs/IDs via query responses
    - Validator fetches and evaluates miner-provided content
    - Example: YouTube (miners submit video IDs, validator evaluates)
    """
    
    @abstractmethod
    def can_evaluate(self, miner_response: Any) -> bool:
        """Check if this evaluator can process the given miner response."""
        pass
    
    @abstractmethod
    async def evaluate_miner_response(
        self, 
        miner_response: Any, 
        briefs: List[Dict[str, Any]],
        metagraph_info: Dict[str, Any]
    ) -> "EvaluationResult":
        """
        Evaluate content from miner response.
        
        Args:
            miner_response: Response from miner query (platform-specific)
            briefs: Campaign briefs to evaluate against
            metagraph_info: Network information (UID, hotkey, etc.)
            
        Returns:
            EvaluationResult with scores per brief
        """
        pass


class ScanBasedEvaluator(PlatformEvaluator):
    """
    Evaluator for platforms that work via direct scanning.
    
    Use this when:
    - Validator directly scans platform (no miner queries)
    - UIDs mapped via connection database (tags, etc.)
    - Example: Twitter (validator scans tweets, maps via connection tags)
    """
    
    @abstractmethod
    async def evaluate_briefs(
        self,
        briefs: List[Dict[str, Any]],
        account_mappings: List[Dict[str, Any]],
        metagraph: Any,
        run_id: str = None
    ) -> "EvaluationResultCollection":
        """
        Evaluate briefs by directly scanning platform.
        
        Args:
            briefs: Campaign briefs to evaluate
            account_mappings: List of {account_username, uid} mappings
            metagraph: Bittensor metagraph for UID validation
            run_id: Optional run identifier for tracking
            
        Returns:
            EvaluationResultCollection with results per UID
        """
        pass