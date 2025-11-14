"""Handles emission calculation - extracted from reward.py calculate_emission_targets and calculate_raw_weights."""

from typing import List, Dict, Any
import numpy as np
import bittensor as bt
from ..interfaces.emission_calculator import EmissionCalculator
from ..models.score_matrix import ScoreMatrix
from ..models.emission_target import EmissionTarget

from ...utils.token_pricing import get_bitcast_alpha_price, get_total_miner_emissions


class EmissionCalculationService(EmissionCalculator):
    """Optimized emission calculation with reduced memory overhead."""
    
    def calculate_targets(
        self, 
        score_matrix: ScoreMatrix,
        briefs: List[Dict[str, Any]]
    ) -> List[EmissionTarget]:
        """
        Calculate emission targets from score matrix.
        """
        bt.logging.debug(f"Emission calculation: {score_matrix.matrix.shape[0]} miners, {len(briefs)} briefs")
        
        # Scores matrix already contains all scaling factors applied at per-content level
        if score_matrix.matrix.size == 0:
            bt.logging.warning("Empty score matrix - returning empty array")
            emission_targets_matrix = np.array([])
        else:
            emission_targets_matrix = score_matrix.matrix.astype(np.float64, copy=True)
        
        # Convert USD targets to raw weights using alpha price and total emissions
        raw_weights_matrix = self._calculate_raw_weights(emission_targets_matrix)
        
        # Create EmissionTarget objects for each brief
        targets = []
        total_usd_targets = 0.0
        total_weights = 0.0
        
        for brief_idx, brief in enumerate(briefs):
            # Extract weights for this brief
            per_uid_weights = raw_weights_matrix[:, brief_idx] if brief_idx < raw_weights_matrix.shape[1] else []
            
            # Calculate USD target for this brief
            usd_target = float(np.sum(emission_targets_matrix[:, brief_idx])) if brief_idx < emission_targets_matrix.shape[1] else 0.0
            
            brief_weight_sum = float(np.sum(per_uid_weights)) if len(per_uid_weights) > 0 else 0.0
            
            # Only log at DEBUG if there are significant targets
            if usd_target > 0.01:
                bt.logging.debug(f"Brief {brief.get('id', f'brief_{brief_idx}')}: ${usd_target:.2f}, weight={brief_weight_sum:.4f}")
            
            # Store brief metadata for downstream processes
            brief_format = brief.get("format", "dedicated")
            scaling_factors = {
                "boost_factor": brief.get("boost", 1.0)
            }
            
            target = EmissionTarget(
                brief_id=brief["id"],
                usd_target=usd_target,
                allocation_details={
                    "per_uid_weights": per_uid_weights.tolist(),
                    "brief_format": brief_format
                },
                scaling_factors=scaling_factors
            )
            targets.append(target)
            total_usd_targets += usd_target
            total_weights += brief_weight_sum
        
        bt.logging.info(f"💰 Emission targets: ${total_usd_targets:.2f} USD, weight={total_weights:.6f}")
        return targets
    
    def _calculate_raw_weights(self, emission_targets_matrix: np.ndarray) -> np.ndarray:
        """
        Convert USD emission targets to raw weights.
        Optimized for memory efficiency.
        """
        if emission_targets_matrix.size == 0:
            bt.logging.warning("Empty emission targets matrix - returning empty array")
            return np.array([])
        
        try:
            alpha_price_usd = get_bitcast_alpha_price()
            total_daily_alpha = get_total_miner_emissions()
            
            # Calculate conversion factor once
            total_daily_usd = alpha_price_usd * total_daily_alpha
            conversion_factor = 1.0 / total_daily_usd
            
            bt.logging.debug(f"Daily miner emission: ${total_daily_usd:.6f} USD")

            # Convert USD targets to raw weights in-place
            raw_weights = emission_targets_matrix * conversion_factor
            
            return raw_weights
            
        except Exception as e:
            bt.logging.error(f"Error calculating raw weights: {e}")
            return np.zeros_like(emission_targets_matrix)
    
 