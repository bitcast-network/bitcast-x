import asyncio
import bittensor as bt

from bitcast.validator.reward_engine.orchestrator import RewardOrchestrator
from bitcast.validator.reward_engine.services.platform_registry import PlatformRegistry
from bitcast.validator.reward_engine.services.score_aggregation_service import ScoreAggregationService
from bitcast.validator.reward_engine.services.emission_calculation_service import EmissionCalculationService
from bitcast.validator.reward_engine.services.reward_distribution_service import RewardDistributionService
from bitcast.validator.reward_engine.twitter_evaluator import TwitterEvaluator
from bitcast.validator.account_connection.connection_scanner import ConnectionScanner
from bitcast.validator.social_discovery.discovery_manager import DiscoveryManager

from bitcast.validator.tweet_scoring.tweet_fasttrack import poll_fast_track

from bitcast.utils.uids import get_all_uids
from bitcast.validator.utils.config import (
    VALIDATOR_WAIT, SCORING_INTERVAL_STEPS, THOROUGH_SCORING_INTERVAL_STEPS,
    SOCIAL_MAP_DOWNLOAD_INTERVAL
)
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
    
    Runs every 2 steps (~20s):
      - poll_fast_track: fetches tweets from stitch3 fast-track endpoint
    
    Runs every SCORING_INTERVAL_STEPS steps (~20 min):
      - Account connection scan, reward engine, weight updates
    """
    # Initialize global publisher if not already done.
    # Do this before fast-track so immediate connection publishing can happen
    # even on non-scoring ticks.
    try:
        get_global_publisher()
    except RuntimeError:
        initialize_global_publisher(self.wallet)
        bt.logging.debug("Global data publisher initialized")

    # Fast-track runs every 2 steps (~20s)
    if self.step % 2 == 0:
        if not hasattr(self, "_fasttrack_poll_lock"):
            self._fasttrack_poll_lock = asyncio.Lock()

        if self._fasttrack_poll_lock.locked():
            bt.logging.debug("Fast-track poll skipped: previous poll still running")
        else:
            try:
                async with self._fasttrack_poll_lock:
                    poll_fast_track()
            except Exception as e:
                bt.logging.warning(f"Fast-track poll error: {e}")

    # Scoring only runs every SCORING_INTERVAL_STEPS steps
    if self.step % SCORING_INTERVAL_STEPS != 0:
        await asyncio.sleep(VALIDATOR_WAIT)
        return

    from bitcast.validator.utils.config import VALIDATOR_MODE
    mode_label = "STANDARD" if VALIDATOR_MODE == "standard" else "DISCOVERY"
    bt.logging.info(f"Starting validation cycle - {mode_label} mode (step {self.step})")
    
    try:
        # Social map handling - mode-specific behavior
        if VALIDATOR_MODE == 'discovery':
            DiscoveryManager.get_instance().maybe_start()
        else:
            # Standard mode: Download social maps from reference validator (every 12 hours)
            if self.step % SOCIAL_MAP_DOWNLOAD_INTERVAL == 0:
                from bitcast.validator.social_discovery.social_map_downloader import download_stale_social_maps
                bt.logging.info("Checking for stale social maps...")
                downloaded = await download_stale_social_maps()
                if downloaded:
                    bt.logging.info(f"Downloaded {len(downloaded)} social map(s): {', '.join(downloaded)}")
        
        # Account connection scan
        bt.logging.info("Starting account connection scan...")

        scanner = ConnectionScanner()
        summary = await scanner.scan_all_pools()
        bt.logging.info(
            f"Connection scan complete: {summary['pools_scanned']} pools, "
            f"{summary['tags_found']} tags, {summary['new_connections']} new"
        )
        
        # Reward engine
        is_thorough = self.step % THOROUGH_SCORING_INTERVAL_STEPS == 0
        mode = "thorough (timeline)" if is_thorough else "lightweight (search)"
        bt.logging.info(f"Starting reward engine ({mode} discovery)...")
        miner_uids = get_all_uids(self)
        orchestrator = get_reward_orchestrator()
        rewards = await orchestrator.calculate_rewards(self, miner_uids, thorough=is_thorough)
        
        bt.logging.info("UID Rewards:")
        for uid, reward in zip(miner_uids, rewards):
            bt.logging.info(f"UID {uid}: {reward:.6f}")
        
        self.update_scores(rewards, miner_uids)
        
    except Exception as e:
        bt.logging.error(f"Error in validation cycle: {e}")

    await asyncio.sleep(VALIDATOR_WAIT)
