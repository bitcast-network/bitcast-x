"""
Client for downloading account connections from reference validator.
Handles API communication, database operations, and error handling.
"""
import httpx
import bittensor as bt
from typing import Optional

from bitcast.validator.utils.config import REFERENCE_VALIDATOR_ENDPOINT
from .connection_db import ConnectionDatabase


class ConnectionClient:
    """Client for downloading account connections from reference validator."""
    
    def __init__(self, timeout: float = 30.0):
        self.base_url = REFERENCE_VALIDATOR_ENDPOINT
        self.timeout = timeout
    
    async def download_and_store_connections(
        self,
        pool_name: Optional[str] = None
    ) -> bool:
        """
        Download connections from reference validator and store in local database.
        
        Args:
            pool_name: Optional pool name to filter by
            
        Returns:
            True if successful, False if failed
        """
        try:
            # Build URL with optional pool filter
            url = f"{self.base_url}/account-connections"
            params = {}
            if pool_name:
                params["pool_name"] = pool_name
            
            # Fetch connections from API
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                
                data = response.json()
                
                # Extract connections list
                connections = data.get("connections", [])
                if not connections:
                    bt.logging.info(
                        f"📭 No connections found on reference validator"
                        f"{f' for pool {pool_name}' if pool_name else ''}"
                    )
                    return True  # Empty list is not an error
                
                # Validate response structure
                if not isinstance(connections, list):
                    bt.logging.error("Invalid response format: 'connections' is not a list")
                    return False
                
                # Store connections in database
                db = ConnectionDatabase()
                stored_count = 0
                error_count = 0
                
                for conn in connections:
                    try:
                        # Validate required fields
                        required_fields = ["pool_name", "tweet_id", "tag", "account_username"]
                        if not all(field in conn for field in required_fields):
                            bt.logging.warning(f"Skipping connection with missing fields: {conn}")
                            error_count += 1
                            continue
                        
                        # Store connection (upsert handles duplicates)
                        db.upsert_connection(
                            pool_name=conn["pool_name"],
                            tweet_id=conn["tweet_id"],
                            tag=conn["tag"],
                            account_username=conn["account_username"]
                        )
                        stored_count += 1
                        
                    except Exception as e:
                        bt.logging.warning(f"Error storing connection {conn.get('account_username')}: {e}")
                        error_count += 1
                        continue
                
                # Log summary
                filter_msg = f" for pool '{pool_name}'" if pool_name else ""
                bt.logging.info(
                    f"✅ Downloaded and stored {stored_count} account connections{filter_msg}"
                )
                
                if error_count > 0:
                    bt.logging.warning(
                        f"⚠️ Failed to store {error_count} connections (see warnings above)"
                    )
                
                return stored_count > 0 or len(connections) == 0
                
        except httpx.TimeoutException:
            bt.logging.warning(
                f"⚠️ Timeout connecting to reference validator at {self.base_url} "
                f"while downloading account connections"
            )
            return False
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                bt.logging.info(
                    f"📭 Account connections endpoint not found on reference validator. "
                    f"Reference validator may not have this feature yet."
                )
            else:
                bt.logging.warning(
                    f"⚠️ HTTP error {e.response.status_code} downloading account connections"
                )
            return False
        except Exception as e:
            bt.logging.error(f"❌ Unexpected error downloading account connections: {e}")
            return False

