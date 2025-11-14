"""
Data publishing utilities for Twitter/X platform data.

This module provides abstract base classes and implementations for publishing
Twitter/X platform data to external endpoints with message signing and error handling.
"""

import asyncio
import json
import aiohttp
import bittensor as bt
import numpy as np
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Any, Optional, Union, List
import time


def convert_numpy_types(obj):
    """
    Recursively convert NumPy types to Python native types for JSON serialization.
    
    Args:
        obj: The object to convert
        
    Returns:
        The object with NumPy types converted to Python native types
    """
    if isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, dict):
        return {key: convert_numpy_types(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_numpy_types(item) for item in obj]
    else:
        return obj


class DataPublisher(ABC):
    """Abstract base class for data publishing with message signing."""
    
    def __init__(self, wallet: bt.wallet, timeout_seconds: int = 10):
        """
        Initialize DataPublisher with validator wallet.
        
        Args:
            wallet: Bittensor wallet for message signing
            timeout_seconds: HTTP request timeout
        """
        self.wallet = wallet
        self.timeout_seconds = timeout_seconds
    
    @abstractmethod
    async def publish_data(self, data: Dict[str, Any], endpoint: str) -> bool:
        """
        Publish data to specified endpoint.
        
        Args:
            data: Data payload to publish
            endpoint: Target endpoint URL
            
        Returns:
            bool: True if successful, False otherwise
        """
        pass
    
    def _get_expected_status_code(self) -> int:
        """
        Get the expected HTTP status code for successful requests.
        Override in subclasses for different endpoint behaviors.
        
        Returns:
            Expected HTTP status code (default: 200)
        """
        return 200
    
    def _sign_message(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sign message data for API publishing.
        
        Args:
            data: Full payload including metadata and core data
            
        Returns:
            Dict containing signed payload with signature and signer
        """
        # Get hotkey for signing
        keypair = self.wallet.hotkey
        signer = keypair.ss58_address
        
        # Extract core data to sign - supports both payload formats
        core_data_to_sign = self._extract_signable_data(data)
        
        # Generate timestamp for BOTH signing and payload (must be identical!)
        timestamp = datetime.utcnow().isoformat()
        
        # Create message to sign (format: signer:timestamp:core_data)
        message = f"{signer}:{timestamp}:{json.dumps(core_data_to_sign, sort_keys=True)}"
        
        # Sign the message
        signature = keypair.sign(data=message)
        
        # Create final payload with SAME timestamp used for signing
        converted_payload = convert_numpy_types(data)
        signed_payload = {
            **converted_payload,  # Include all metadata
            "time": timestamp,  # Use same timestamp as signature
            "signature": signature.hex(),
            "signer": signer,
            "vali_hotkey": signer  # Required for unified API format
        }
        
        return signed_payload
    
    def _extract_signable_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract the core data that should be signed from the payload.
        Supports different payload structures: wrapped ('payload' field) or direct ('account_data' field).
        
        Args:
            data: Full payload data
            
        Returns:
            Core data to include in signature
        """
        # Wrapped format uses 'payload' field
        if 'payload' in data:
            return convert_numpy_types(data.get('payload', {}))
        
        # Direct format uses 'account_data' field
        account_data = data.get('account_data', {})
        return convert_numpy_types(account_data)
    
    def _log_success(self, endpoint: str, data_type: str = "data") -> None:
        """Log successful publication."""
        bt.logging.info(f"Successfully published {data_type}")
    
    def _log_error(self, endpoint: str, error: Exception, data_type: str = "data") -> None:
        """Log publication error."""
        bt.logging.error(f"Failed to publish {data_type} to {endpoint}: {error}")


class UnifiedDataPublisher(DataPublisher):
    """Unified publisher for Twitter/X platform data using async API format."""
    
    def __init__(self, wallet: bt.wallet, timeout_seconds: int = 60):
        """
        Initialize UnifiedDataPublisher with timeout for async processing.
        
        Args:
            wallet: Bittensor wallet for message signing
            timeout_seconds: HTTP request timeout for async processing (default: 60s)
        """
        super().__init__(wallet, timeout_seconds)
    
    def _get_expected_status_code(self) -> int:
        """Return 202 Accepted for async processing."""
        return 202
    

    
    async def publish_unified_payload(
        self,
        payload_type: str,
        run_id: str,
        payload_data: Any,
        endpoint: str,
        miner_uid: Optional[int] = None
    ) -> bool:
        """
        Publish data using unified API format.
        
        Args:
            payload_type: Type of data being published (e.g., "brief_tweets", "social_map")
            run_id: Validation cycle identifier
            payload_data: The actual data (format depends on payload_type)
            endpoint: Target endpoint URL
            miner_uid: Optional miner UID
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Create unified payload structure
            payload = {
                "payload_type": payload_type,
                "run_id": run_id,
                "payload": payload_data
            }
            
            # Add optional miner_uid if provided
            if miner_uid is not None:
                payload["miner_uid"] = miner_uid
            
            # Publish using unified format
            return await self.publish_data(payload, endpoint)
            
        except Exception as e:
            self._log_error(endpoint, e, f"{payload_type} data")
            return False
    
    async def publish_data(self, data: Dict[str, Any], endpoint: str) -> bool:
        """
        Publish data to specified endpoint with unified API format handling.
        
        Args:
            data: Data payload to publish
            endpoint: Target endpoint URL
            
        Returns:
            bool: True if successful, False otherwise
        """
        start_time = time.time()
        try:
            # Sign the message using corrected format
            signed_payload = self._sign_message(data)
            
            # Make async HTTP request with longer timeout
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    endpoint, 
                    json=signed_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json"
                    }
                ) as response:
                    response_time = time.time() - start_time
                    if response.status == 202:  # Expect 202 Accepted for async processing
                        try:
                            response_data = await response.json()
                            # Check for success status in response (accept both "success" and "accepted")
                            if response_data.get("status") in ["success", "accepted"]:
                                payload_type = signed_payload.get("payload_type", "unknown")
                                bt.logging.info(f"✅ Successfully published {payload_type} data (%.2fs)", response_time)
                                return True
                            else:
                                bt.logging.error(f"Server returned error: {response_data} (%.2fs)", response_time)
                                return False
                        except Exception as json_error:
                            bt.logging.error(f"Failed to parse response JSON: {json_error} (%.2fs)", response_time)
                            return False
                    elif response.status == 400:
                        error_text = await response.text()
                        bt.logging.error(f"400 Bad Request - Payload validation failed: {error_text} (%.2fs)", response_time)
                        return False
                    elif response.status == 401:
                        bt.logging.error(f"401 Unauthorized - Invalid signature/authentication (%.2fs)", response_time)
                        return False
                    elif response.status == 403:
                        bt.logging.error(f"403 Forbidden - Validator not authorized (%.2fs)", response_time)
                        return False
                    else:
                        error_text = await response.text()
                        bt.logging.error(f"HTTP {response.status} error from {endpoint}: {error_text} (%.2fs)", response_time)
                        return False
                        
        except asyncio.TimeoutError:
            response_time = time.time() - start_time
            bt.logging.warning(f"Request timed out after %.2fs - server queue may be processing", response_time)
            return False
        except Exception as e:
            response_time = time.time() - start_time
            bt.logging.error(f"Failed to publish unified data to {endpoint}: {e} (%.2fs)", response_time)
            return False





# Global publisher instance
_global_publisher: Optional[UnifiedDataPublisher] = None


def initialize_global_publisher(wallet: bt.wallet) -> None:
    """
    Initialize the global publisher with a validator wallet.
    Should be called once during validator startup.
    
    Args:
        wallet: Validator's bittensor wallet
    """
    global _global_publisher
    _global_publisher = UnifiedDataPublisher(wallet)
    bt.logging.info("🌐 Global data publisher initialized")


def get_global_publisher() -> UnifiedDataPublisher:
    """
    Get the global publisher instance.
    
    Returns:
        UnifiedDataPublisher instance
        
    Raises:
        RuntimeError: If publisher not initialized
    """
    if _global_publisher is None:
        raise RuntimeError("Global publisher not initialized. Call initialize_global_publisher() first.")
    return _global_publisher


# Convenience functions for easy usage


async def publish_unified_data(
    payload_type: str,
    run_id: str, 
    payload_data: Union[Dict[str, Any], List[Dict[str, Any]]],
    endpoint: str,
    miner_uid: Optional[int] = None
) -> bool:
    """
    Convenience function to publish data using unified API format.
    
    Args:
        payload_type: Type of data being published (e.g., "brief_tweets", "social_map")  
        run_id: Validation cycle identifier
        payload_data: The actual data payload
        endpoint: Target endpoint URL
        miner_uid: Optional miner UID
        
    Returns:
        bool: True if successful, False otherwise
    """
    publisher = get_global_publisher()
    return await publisher.publish_unified_payload(
        payload_type, run_id, payload_data, endpoint, miner_uid
    )

