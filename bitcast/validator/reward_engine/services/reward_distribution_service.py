"""Handles final reward distribution - extracted from reward.py normalization and allocation logic."""

from typing import List, Dict, Any
import numpy as np
import bittensor as bt
from ..models.emission_target import EmissionTarget
from .treasury_allocation import allocate_subnet_treasury


class RewardDistributionService:
    """Handles final reward calculation and distribution."""
    
    def calculate_distribution(
        self,
        emission_targets: List[EmissionTarget],
        briefs: List[Dict[str, Any]],
        uids: List[int]
    ) -> np.ndarray:
        """Calculate final reward distribution from emission targets."""
        try:
            raw_weights_matrix = self._extract_raw_weights_matrix(emission_targets, len(uids))
            rewards = self._normalize_weights(raw_weights_matrix, briefs, uids)
            return allocate_subnet_treasury(rewards, uids)
            
        except Exception as e:
            bt.logging.error(f"Error in reward distribution: {e}")
            return self._error_fallback(uids)
    
    def _extract_raw_weights_matrix(
        self, 
        emission_targets: List[EmissionTarget], 
        num_uids: int
    ) -> np.ndarray:
        """Extract raw weights matrix from emission targets."""
        bt.logging.debug("Extracting raw weights matrix")
        
        if not emission_targets:
            bt.logging.warning("No emission targets - returning empty matrix")
            return np.zeros((num_uids, 0))
        
        num_briefs = len(emission_targets)
        matrix = np.zeros((num_uids, num_briefs), dtype=np.float64)
        
        for brief_idx, target in enumerate(emission_targets):
            weights = target.allocation_details.get("per_uid_weights", [])
            brief_id = target.brief_id
            brief_total = 0.0
            non_zero_count = 0
            
            for uid_idx, weight in enumerate(weights):
                if uid_idx < num_uids and weight != 0:
                    matrix[uid_idx, brief_idx] = weight
                    brief_total += weight
                    non_zero_count += 1
            
            # Only log briefs with significant activity at DEBUG level
            if brief_total > 0.001 or non_zero_count > 0:
                bt.logging.debug(f"Brief {brief_id}: {non_zero_count} UIDs, ${target.usd_target:.2f}")
        
        return matrix
    
    def _normalize_weights(
        self, 
        weights_matrix: np.ndarray, 
        briefs: List[Dict[str, Any]], 
        uids: List[int]
    ) -> np.ndarray:
        """Normalize weights into final reward distribution."""
        if weights_matrix.size == 0:
            bt.logging.warning("Empty weights matrix - returning zero rewards")
            return np.zeros(len(uids))
        
        normalized = self._apply_emission_constraints(weights_matrix, briefs)
        rewards = self._sum_to_final_rewards(normalized, uids)
        
        final_total = float(np.sum(rewards))
        final_non_zero = np.count_nonzero(rewards)
        max_reward = float(np.max(rewards)) if len(rewards) > 0 else 0.0
        bt.logging.debug(f"Normalized rewards: {final_non_zero}/{len(uids)} miners, total={final_total:.4f}, max={max_reward:.6f}")
        
        return rewards
    
    def _apply_emission_constraints(
        self, 
        scores_matrix: np.ndarray, 
        briefs: List[Dict[str, Any]]
    ) -> np.ndarray:
        """Apply brief caps and global constraints to ensure proper emission allocation."""
        if scores_matrix.size == 0:
            return scores_matrix
        
        result = scores_matrix.copy()
        
        # Apply individual brief caps
        for brief_idx, brief in enumerate(briefs):
            brief_cap = brief.get("cap", 1.0)
            brief_sum = result[:, brief_idx].sum()
            
            if brief_sum > brief_cap:
                scale_factor = brief_cap / brief_sum
                result[:, brief_idx] *= scale_factor
                # Log when brief exceeds cap
                bt.logging.debug(f"Brief '{brief.get('id', 'unknown')}' exceeded cap {brief_cap:.4f}, scaled by {scale_factor:.4f}")
        
        # Apply global maximum scaling if total > 1.0
        total_sum = result.sum()
        if total_sum > 1.0:
            global_scale_factor = 1.0 / total_sum
            result = result / total_sum
            bt.logging.debug(f"Applied global scaling {global_scale_factor:.4f} (total was {total_sum:.4f})")
        
        # Log emission percentages per brief at DEBUG
        for brief_idx, brief in enumerate(briefs):
            brief_percentage = result[:, brief_idx].sum()
            bt.logging.debug(f"Brief '{brief.get('id', 'unknown')}' claiming {brief_percentage * 100:.2f}% emissions")
        
        return result
    
    def _sum_to_final_rewards(self, scores_matrix: np.ndarray, uids: List[int]) -> np.ndarray:
        """Sum normalized scores to final rewards, ensuring total = 1 and UID 0 is not negative."""
        if scores_matrix.size == 0:
            return np.zeros(len(uids))
        
        # Sum each miner's scores across briefs
        rewards = scores_matrix.sum(axis=1)
        
        # Ensure total rewards sum to 1 by adjusting UID 0, but never negative
        uid_0_idx = next((i for i, uid in enumerate(uids) if uid == 0), None)
        if uid_0_idx is not None:
            other_sum = sum(rewards[i] for i in range(len(rewards)) if i != uid_0_idx)
            rewards[uid_0_idx] = max(1.0 - other_sum, 0.0)
        
        return rewards
    
    def _error_fallback(self, uids: List[int]) -> np.ndarray:
        """Error fallback that gives all rewards to UID 0."""
        return np.array([1.0 if uid == 0 else 0.0 for uid in uids])