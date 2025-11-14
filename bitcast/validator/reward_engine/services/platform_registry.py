"""Registry for managing platform evaluators."""

from typing import Dict, List, Optional
import bittensor as bt
from ..interfaces.platform_evaluator import (
    PlatformEvaluator,
    QueryBasedEvaluator,
    ScanBasedEvaluator
)


class PlatformRegistry:
    """Registry for managing and selecting platform evaluators."""
    
    def __init__(self):
        self._evaluators: Dict[str, PlatformEvaluator] = {}
    
    def register_evaluator(self, evaluator: PlatformEvaluator):
        """Register a platform evaluator (either query-based or scan-based)."""
        platform_name = evaluator.platform_name()
        
        # Validate evaluator implements the right interface
        if not isinstance(evaluator, (QueryBasedEvaluator, ScanBasedEvaluator)):
            raise ValueError(
                f"Evaluator must implement QueryBasedEvaluator or ScanBasedEvaluator, "
                f"got {type(evaluator)}"
            )
        
        self._evaluators[platform_name] = evaluator
        
        evaluator_type = "scan-based" if isinstance(evaluator, ScanBasedEvaluator) else "query-based"
        bt.logging.info(f"Registered {evaluator_type} evaluator for platform: {platform_name}")
    
    def get_evaluator(self, platform_name: str) -> Optional[PlatformEvaluator]:
        """Get an evaluator for a specific platform."""
        return self._evaluators.get(platform_name)
    
    def get_scan_evaluator(self, platform_name: str) -> Optional[ScanBasedEvaluator]:
        """Get a scan-based evaluator (returns None if query-based)."""
        evaluator = self._evaluators.get(platform_name)
        return evaluator if isinstance(evaluator, ScanBasedEvaluator) else None
    
    def get_query_evaluator(self, platform_name: str) -> Optional[QueryBasedEvaluator]:
        """Get a query-based evaluator (returns None if scan-based)."""
        evaluator = self._evaluators.get(platform_name)
        return evaluator if isinstance(evaluator, QueryBasedEvaluator) else None
    
    def get_available_platforms(self) -> List[str]:
        """Get list of available platform names."""
        return list(self._evaluators.keys())
    
    def __len__(self) -> int:
        """Number of registered evaluators."""
        return len(self._evaluators)
    
    def __repr__(self) -> str:
        """String representation for debugging."""
        platforms = ", ".join(self._evaluators.keys())
        return f"PlatformRegistry({platforms})" 