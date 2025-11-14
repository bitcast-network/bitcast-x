"""
Publisher for account connection data.

Publishes discovered account-tag connections to the data client API.
"""

from datetime import datetime
from typing import Dict, List, Any
import bittensor as bt

from bitcast.validator.utils.config import X_ACCOUNT_CONNECTIONS_ENDPOINT
from bitcast.validator.utils.data_publisher import get_global_publisher


async def publish_account_connections(
    connections: List[Dict[str, Any]],
    run_id: str
) -> bool:
    """
    Publish account connection data using unified API format.
    
    Args:
        connections: List of connection dictionaries with keys:
                    - tweet_id: ID of tweet containing tag
                    - tag: Connection tag string
                    - username: Twitter username
        run_id: Validation cycle identifier
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        bt.logging.info(
            f"🔗 Publishing {len(connections)} account connections "
            f"to {X_ACCOUNT_CONNECTIONS_ENDPOINT}"
        )
        
        # Create payload structure
        payload_data = {
            "connections": connections,
            "timestamp": datetime.now().isoformat()
        }
        
        # Use global publisher
        publisher = get_global_publisher()
        success = await publisher.publish_unified_payload(
            payload_type="x_account_connections",
            run_id=run_id,
            payload_data=payload_data,
            endpoint=X_ACCOUNT_CONNECTIONS_ENDPOINT
        )
        
        if success:
            bt.logging.info(
                f"✅ Account connections published for run {run_id} "
                f"({len(connections)} connections)"
            )
        else:
            bt.logging.warning(
                f"⚠️ Account connections publishing failed for run {run_id}"
            )
            
        return success
            
    except Exception as e:
        bt.logging.error(f"Critical account connections publishing error: {e}")
        return False

