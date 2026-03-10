"""Main reward calculation orchestrator - replaces monolithic reward.py functions."""

from datetime import date, datetime, timezone
from typing import List, Dict, Any
import numpy as np
import bittensor as bt
from bitcast.validator.reward_engine.utils import get_briefs
from ..utils.run_manager import generate_current_run_id
from ..utils.config import ENABLE_DATA_PUBLISH, TWEETS_SUBMIT_ENDPOINT

from .services.platform_registry import PlatformRegistry
from .services.score_aggregation_service import ScoreAggregationService
from .services.emission_calculation_service import EmissionCalculationService
from .services.reward_distribution_service import RewardDistributionService
from .services.treasury_allocation import allocate_subnet_treasury
from .services.referral_bonus_service import ReferralBonusService
from .models.evaluation_result import EvaluationResultCollection
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
        uids: List[int],
        thorough: bool = False,
    ) -> np.ndarray:
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
            account_to_uid = {m['account_username']: m['uid'] for m in valid_mappings}
            connected_usernames = set(all_accounts.keys())
            bt.logging.info(f"📡 {len(connected_usernames)} connected accounts across {len(all_pools)} pools")
            
            # 3b. On thorough cycles, refresh all connected account timelines once
            if thorough and connected_usernames:
                from bitcast.validator.tweet_scoring import refresh_connected_timelines
                refresh_connected_timelines(connected_usernames)
            
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
                    run_id=run_id,
                    thorough=thorough,
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
                run_id=run_id,
                thorough=thorough,
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
            rewards = self.reward_distributor.calculate_distribution(
                emission_targets, emission_briefs, uids
            )
            
            # 10. Check and activate new referrals, then apply today's bonuses
            try:
                referral_service = ReferralBonusService(connection_db=db)
                
                participating_accounts = set()
                for uid, result in evaluation_results.results.items():
                    participating_accounts.update(result.account_results.keys())
                
                activated = referral_service.check_and_activate_referrals(
                    participating_accounts=participating_accounts
                )
                if activated > 0:
                    bt.logging.info(f"Activated {activated} new referral bonuses")
                
                # Build merged account_data from social maps for dynamic bonus calc
                # Keys are lowercased to match connection DB convention
                from bitcast.validator.tweet_scoring.social_map_loader import load_latest_social_map
                account_data: Dict[str, Dict] = {}
                for pool in all_pools:
                    try:
                        social_map, _ = load_latest_social_map(pool)
                        for username, data in social_map.get('accounts', {}).items():
                            key = username.lower()
                            if key not in account_data:
                                account_data[key] = data
                    except FileNotFoundError:
                        bt.logging.warning(f"No social map for pool '{pool}', skipping for referral calc")
                
                today = date.today()
                result = referral_service.get_referral_bonuses(
                    payout_date=today,
                    account_to_uid=account_to_uid,
                    account_data=account_data,
                )
                
                if result.bonuses:
                    from bitcast.validator.utils.token_pricing import get_bitcast_alpha_price, get_total_miner_emissions
                    alpha_price = get_bitcast_alpha_price()
                    daily_alpha = get_total_miner_emissions()
                    daily_emission_usd = alpha_price * daily_alpha
                    
                    uid_to_idx = {uid: i for i, uid in enumerate(uids)}
                    bonus_total_usd = 0.0
                    for uid, bonus_usd in result.bonuses.items():
                        idx = uid_to_idx.get(uid)
                        if idx is not None:
                            bonus_weight = bonus_usd / daily_emission_usd
                            rewards[idx] += bonus_weight
                            bonus_total_usd += bonus_usd
                            bt.logging.info(f"Referral bonus: ${bonus_usd:.2f} (weight {bonus_weight:.6f}) to UID {uid}")
                    
                    bt.logging.info(f"Applied ${bonus_total_usd:.2f} in referral bonuses to {len(result.bonuses)} UIDs")
                    
                    await self._publish_referral_bonuses(
                        referrals=result.referrals,
                        account_to_uid=account_to_uid,
                        activated=activated,
                        payout_date=today,
                        run_id=run_id
                    )
            except Exception as e:
                bt.logging.error(f"Referral bonus calculation failed (rewards unaffected): {e}")
            
            total_rewards = float(np.sum(rewards))
            non_zero_uids = np.count_nonzero(rewards)
            bt.logging.info(f"✅ Rewards calculated: {non_zero_uids}/{len(uids)} UIDs rewarded ({total_rewards:.6f} total)")
            
            return rewards
            
        except Exception as e:
            bt.logging.error(f"Sequential reward calculation failed: {e}")
            return self._fallback_rewards(uids)
    
    async def _publish_referral_bonuses(
        self,
        referrals: List[Dict[str, Any]],
        account_to_uid: Dict[str, int],
        activated: int,
        payout_date: date,
        run_id: str,
    ) -> None:
        """Publish referral bonus data. Fire-and-forget -- failures don't break rewards."""
        if not ENABLE_DATA_PUBLISH:
            return
        
        try:
            from ..utils.data_publisher import get_global_publisher
            
            bonuses = []
            total_usd = 0.0
            for ref in referrals:
                referee = ref['account_username']
                referrer = ref.get('referred_by')
                amount = ref.get('computed_amount', 0.0)
                bonuses.append({
                    "referee": referee,
                    "referrer": referrer,
                    "referee_uid": account_to_uid.get(referee),
                    "referrer_uid": account_to_uid.get(referrer) if referrer else None,
                    "referee_amount_usd": amount,
                    "referrer_amount_usd": amount,
                })
                total_usd += amount * (2 if referrer else 1)
            
            payload = {
                "payout_date": payout_date.isoformat(),
                "bonuses": bonuses,
                "total_usd": total_usd,
                "activated": activated,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            
            publisher = get_global_publisher()
            success = await publisher.publish_unified_payload(
                payload_type="referral_bonuses",
                run_id=run_id,
                payload_data=payload,
                endpoint=TWEETS_SUBMIT_ENDPOINT
            )
            
            if success:
                bt.logging.info(f"Published {len(bonuses)} referral bonuses")
            else:
                bt.logging.debug("Referral bonus publishing failed (continuing...)")
                
        except Exception as e:
            bt.logging.error(f"Exception publishing referral bonuses: {e}")
    
    def _fallback_rewards(self, uids: List[int]) -> np.ndarray:
        """
        Return fallback rewards when normal calculation cannot proceed.
        Allocates all rewards to burn UID, then transfers to treasury via allocation service.
        """
        rewards = np.array([1.0 if uid == 0 else 0.0 for uid in uids])
        return allocate_subnet_treasury(rewards, uids)