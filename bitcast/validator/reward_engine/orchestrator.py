"""Main reward calculation orchestrator - replaces monolithic reward.py functions."""

from typing import List, Tuple, Dict, Any
import asyncio
import numpy as np
import bittensor as bt
from bitcast.validator.reward_engine.utils import get_briefs
from ..utils.run_manager import generate_current_run_id

from .services.platform_registry import PlatformRegistry
from .services.score_aggregation_service import ScoreAggregationService
from .services.emission_calculation_service import EmissionCalculationService
from .services.reward_distribution_service import RewardDistributionService
from .services.treasury_allocation import allocate_subnet_treasury
from .models.evaluation_result import EvaluationResultCollection, EvaluationResult
from ..account_connection.connection_db import ConnectionDatabase


class RewardOrchestrator:
    """Coordinates the complete reward calculation workflow."""
    
    def __init__(
        self,
        platform_registry: PlatformRegistry = None,
        score_aggregator: ScoreAggregationService = None,
        emission_calculator: EmissionCalculationService = None,
        reward_distributor: RewardDistributionService = None
    ):
        self.platforms = platform_registry or PlatformRegistry()
        self.score_aggregator = score_aggregator or ScoreAggregationService()
        self.emission_calculator = emission_calculator or EmissionCalculationService()
        self.reward_distributor = reward_distributor or RewardDistributionService()
    
    async def calculate_rewards(
        self, 
        validator_self, 
        uids: List[int]
    ) -> Tuple[np.ndarray, List[dict]]:
        """Main entry point for reward calculation workflow."""
        try:
            # 1. Get all content briefs (unfiltered)
            try:
                briefs = get_briefs()
            except RuntimeError as e:
                bt.logging.error(f"Failed to fetch content briefs: {e}")
                return self._fallback_rewards(uids)
                
            if not briefs:
                bt.logging.warning("No briefs available - using fallback rewards")
                return self._fallback_rewards(uids)
            
            # 2. Separate briefs by phase
            scoring_briefs = [b for b in briefs if b['state'] == 'scoring']
            emission_briefs = [b for b in briefs if b['state'] == 'emission']
            
            bt.logging.info(
                f"Brief phases: {len(scoring_briefs)} scoring, {len(emission_briefs)} emission"
            )
            
            # 3. Load connections early for all briefs (both scoring and emission)
            db = ConnectionDatabase()
            all_pools = {brief.get('pool', 'tao') for brief in briefs}
            
            # Load and merge connections from all pools (deduplicate by username)
            all_accounts = {
                m['account_username']: m
                for pool in all_pools
                for m in db.get_accounts_with_uids(pool, validator_self.metagraph)
            }
            
            valid_mappings = [m for m in all_accounts.values() if m['uid'] is not None]
            connected_usernames = set(all_accounts.keys())
            bt.logging.info(f"📡 {len(connected_usernames)} connected accounts across {len(all_pools)} pools")
            
            # 4. Generate run ID for brief tweet publishing
            run_id = generate_current_run_id(validator_self.wallet)
            bt.logging.debug(f"Run ID: {run_id}")
            
            # 5. Get Twitter evaluator once
            twitter_evaluator = self.platforms.get_evaluator("twitter")
            if not twitter_evaluator:
                bt.logging.error("TwitterEvaluator not registered")
                return self._fallback_rewards(uids)
            
            # 6a. Process scoring-phase briefs (monitoring only)
            if scoring_briefs:
                bt.logging.info(f"📊 Phase 1: Monitoring {len(scoring_briefs)} briefs")
                await twitter_evaluator.score_briefs_for_monitoring(
                    briefs=scoring_briefs,
                    connected_accounts=connected_usernames,
                    run_id=run_id
                )
            
            # 6b. Process emission-phase briefs (rewards)
            if not emission_briefs:
                bt.logging.warning("No briefs in emission phase - using fallback")
                return self._fallback_rewards(uids)
            
            bt.logging.info(f"🎯 Phase 2: Processing {len(emission_briefs)} briefs for rewards")
            
            if not valid_mappings:
                bt.logging.warning("No valid UID-account mappings found - using fallback")
                return self._fallback_rewards(uids)
            
            evaluation_results = await twitter_evaluator.evaluate_briefs(
                briefs=emission_briefs,
                uid_account_mappings=valid_mappings,
                connected_accounts=connected_usernames,
                metagraph=validator_self.metagraph,
                run_id=run_id
            )
                
            # 7. Aggregate scores across platforms
            bt.logging.debug("Aggregating tweet scores into score matrix")
            score_matrix = self.score_aggregator.aggregate_scores(evaluation_results, emission_briefs, uids)
            bt.logging.debug(f"Created {score_matrix.matrix.shape} score matrix")
                                    
            # 8. Calculate emission targets
            bt.logging.debug("Converting scores to USD emission targets")
            emission_targets = self.emission_calculator.calculate_targets(score_matrix, emission_briefs)
            
            # 9. Distribute final rewards
            bt.logging.debug("Calculating final reward distribution")
            rewards, stats_list = self.reward_distributor.calculate_distribution(
                emission_targets, evaluation_results, emission_briefs, uids
            )
            
            total_rewards = float(np.sum(rewards))
            non_zero_uids = np.count_nonzero(rewards)
            bt.logging.info(f"✅ Rewards calculated: {non_zero_uids}/{len(uids)} UIDs rewarded ({total_rewards:.6f} total)")
            
            return rewards, stats_list
            
        except Exception as e:
            bt.logging.error(f"Sequential reward calculation failed: {e}")
            return self._fallback_rewards(uids)
    
    def _fallback_rewards(self, uids: List[int]) -> Tuple[np.ndarray, List[dict]]:
        """
        Return fallback rewards when normal calculation cannot proceed.
        Allocates all rewards to burn UID, then transfers to treasury via allocation service.
        """
        rewards = np.array([1.0 if uid == 0 else 0.0 for uid in uids])
        final_rewards = allocate_subnet_treasury(rewards, uids)
        stats_list = [{"scores": {}, "uid": uid} for uid in uids]
        return final_rewards, stats_list