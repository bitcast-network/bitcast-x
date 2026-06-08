"""
Simplified forward logic for weight copy mode validators.
Replaces complex validation logic with API weight fetching.
"""
import asyncio
import bittensor as bt

from bitcast.validator.weight_copy.wc_client import WeightCopyClient
from bitcast.validator.utils.config import VALIDATOR_WAIT


async def forward_weight_copy(self):
    """
    Weight copy mode forward function.
    
    Instead of running complex validation logic, fetches weights from 
    the reference validator and updates local scores.
    
    Behavior on API failure: Continues with existing scores (no update).
    """
    # Only run every 360 steps (360 × 10s = 60 minutes)
    if self.step % 360 != 0:
        await asyncio.sleep(VALIDATOR_WAIT)
        return
    
    bt.logging.info(f"🔄 Weight copy: Fetching weights from reference validator (step {self.step})")
    
    try:
        # Initialize client if not already done
        if not hasattr(self, '_wc_client'):
            self._wc_client = WeightCopyClient()
        
        # Fetch weights from primary validator
        result = await self._wc_client.fetch_weights()
        
        if result is None:
            # API fetch failed - continue with existing weights
            bt.logging.info("Continuing with existing weights until next fetch attempt")
            await asyncio.sleep(VALIDATOR_WAIT)
            return
        
        scores, hotkeys, primary_step = result
        
        # Validate array size matches our metagraph
        if len(scores) != len(self.scores):
            bt.logging.warning(
                f"Score array size mismatch: primary has {len(scores)}, "
                f"we have {len(self.scores)} - keeping existing weights"
            )
            await asyncio.sleep(VALIDATOR_WAIT)
            return
        
        # Validate hotkey count for informational purposes
        if len(hotkeys) != len(self.metagraph.hotkeys):
            bt.logging.warning(
                f"Hotkey count mismatch: primary has {len(hotkeys)}, "
                f"we have {len(self.metagraph.hotkeys)}"
            )
        
        # Update our scores with fetched weights
        self.scores = scores.copy()
        bt.logging.info(
            f"✅ Updated scores from reference validator "
            f"(ref_step={primary_step}, our_step={self.step})"
        )
        
    except Exception as e:
        bt.logging.error(f"Error in weight copy forward: {e} - continuing with existing weights")
    
    await asyncio.sleep(VALIDATOR_WAIT)

