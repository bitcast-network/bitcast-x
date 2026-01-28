import time
from datetime import datetime, timezone, timedelta
import bittensor as bt

from bitcast.validator.reward_engine.orchestrator import RewardOrchestrator
from bitcast.validator.reward_engine.services.platform_registry import PlatformRegistry
from bitcast.validator.reward_engine.services.score_aggregation_service import ScoreAggregationService
from bitcast.validator.reward_engine.services.emission_calculation_service import EmissionCalculationService
from bitcast.validator.reward_engine.services.reward_distribution_service import RewardDistributionService
from bitcast.validator.reward_engine.twitter_evaluator import TwitterEvaluator
from bitcast.validator.account_connection.connection_scanner import ConnectionScanner
from bitcast.validator.social_discovery.social_discovery import run_discovery_for_stale_pools

from bitcast.utils.uids import get_all_uids
from bitcast.validator.utils.config import VALIDATOR_WAIT, ACCOUNT_CONNECTION_INTERVAL_HOURS, REWARDS_INTERVAL_HOURS
from bitcast.validator.utils.data_publisher import initialize_global_publisher, get_global_publisher

# Singleton for efficiency
_reward_orchestrator = None


def get_reward_orchestrator() -> RewardOrchestrator:
    """Get reward orchestrator singleton."""
    global _reward_orchestrator
    if _reward_orchestrator is None:
        platform_registry = PlatformRegistry()
        twitter_evaluator = TwitterEvaluator()
        platform_registry.register_evaluator(twitter_evaluator)
        bt.logging.info("Registered TwitterEvaluator")
        
        _reward_orchestrator = RewardOrchestrator(
            platform_registry=platform_registry,
            score_aggregator=ScoreAggregationService(),
            emission_calculator=EmissionCalculationService(),
            reward_distributor=RewardDistributionService()
        )
    
    return _reward_orchestrator


async def forward(self):
    """
    Forward function for standard and discovery modes.
    
    Performs full validation (account connection, tweet scoring, filtering, rewards).
    - Standard mode: Downloads social maps from reference validator
    - Discovery mode: Generates social maps via social discovery
    """
    # Run forward every hour on the hour (:00) and half hour (:30)
    if self.step % 30 != 0:
        time.sleep(VALIDATOR_WAIT)
        return

    from bitcast.validator.utils.config import VALIDATOR_MODE
    mode_label = "STANDARD" if VALIDATOR_MODE == "standard" else "DISCOVERY"
    bt.logging.info(f"🚀 Starting validation cycle - {mode_label} mode (step {self.step})")
    
    # Initialize global publisher if not already done
    try:
        get_global_publisher()
    except RuntimeError:
        initialize_global_publisher(self.wallet)
        bt.logging.debug("Global data publisher initialized")

    try:
        # Social map handling - mode-specific behavior
        if VALIDATOR_MODE == 'discovery':
            # Discovery mode: Generate social maps via discovery
            results = await run_discovery_for_stale_pools()
            if results:
                bt.logging.info(f"Social discovery complete for {len(results)} pool(s): {list(results.keys())}")
        else:
            # Standard mode: Download social maps from reference validator (every 12 hours at :00)
            if self.step % (12 * 60) == 0:
                from bitcast.validator.social_discovery.social_map_downloader import download_stale_social_maps
                bt.logging.info("🗺️ Checking for stale social maps...")
                downloaded = await download_stale_social_maps()
                if downloaded:
                    bt.logging.info(f"Downloaded {len(downloaded)} social map(s): {', '.join(downloaded)}")
        
        # Account connection scan (every 1 hour at :30)
        if self.step % (ACCOUNT_CONNECTION_INTERVAL_HOURS * 60) == 30:
            bt.logging.info("🔗 Starting account connection scan for all pools...")
            scanner = ConnectionScanner(lookback_days=7)
            summary = await scanner.scan_all_pools()
            bt.logging.info(
                f"Account connection scan complete: {summary['pools_scanned']} pools, "
                f"{summary['tags_found']} tags, {summary['new_connections']} new"
            )
        
        # Reward engine (every 1 hour at :00, staggered from account scan)
        if self.step % (REWARDS_INTERVAL_HOURS * 60) == 0:
            bt.logging.info("💰 Starting reward engine...")
            miner_uids = get_all_uids(self)
            orchestrator = get_reward_orchestrator()
            rewards, _ = await orchestrator.calculate_rewards(self, miner_uids)
            
            bt.logging.info("UID Rewards:")
            for uid, reward in zip(miner_uids, rewards):
                bt.logging.info(f"UID {uid}: {reward:.6f}")
            
            self.update_scores(rewards, miner_uids)
        
    except Exception as e:
        bt.logging.error(f"Error in validation cycle: {e}")

    time.sleep(VALIDATOR_WAIT)
