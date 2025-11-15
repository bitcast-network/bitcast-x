"""
Simple read-only API for exposing validator weights and social maps.
Includes rate limiting for protection against abuse.
"""
from fastapi import FastAPI, HTTPException, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from pathlib import Path
import numpy as np
import json
from typing import Dict, List
import uvicorn

from bitcast.validator.utils.config import WALLET_NAME, HOTKEY_NAME, MECHID
import os


# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Reference Validator API", version="1.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def get_state_path() -> Path:
    """Get path to validator state file with mechanism ID."""
    # Get netuid from env, default to 93 (same as validator script)
    netuid = int(os.getenv('NETUID', '93'))
    # Path matches config.py: {logging_dir}/{wallet}/{hotkey}/netuid{netuid}/{neuron_name}
    # Include mechanism ID in filename to support multiple mechanisms
    state_file = Path.home() / ".bittensor" / "miners" / WALLET_NAME / HOTKEY_NAME / f"netuid{netuid}" / "validator" / f"state_mech_{MECHID}.npz"
    return state_file


def load_state() -> Dict:
    """Load validator state from disk."""
    state_path = get_state_path()
    
    if not state_path.exists():
        raise FileNotFoundError(f"State file not found at {state_path}")
    
    state = np.load(state_path, allow_pickle=True)
    return {
        "scores": state["scores"],
        "hotkeys": state["hotkeys"],
        "step": int(state["step"])
    }


def normalize_weights(scores: np.ndarray) -> np.ndarray:
    """Normalize scores to sum to 1."""
    norm = np.linalg.norm(scores, ord=1)
    if norm == 0 or np.isnan(norm):
        return np.zeros_like(scores)
    return scores / norm


def load_latest_social_map(pool_name: str) -> Dict:
    """
    Load the latest social map for a pool.
    
    Args:
        pool_name: Name of the pool
        
    Returns:
        Dict with social map data and metadata
        
    Raises:
        FileNotFoundError: If pool or social maps not found
        ValueError: If social map data is invalid
    """
    # Locate social maps directory relative to this file
    social_maps_dir = Path(__file__).parents[1] / "social_discovery" / "social_maps" / pool_name
    
    if not social_maps_dir.exists():
        raise FileNotFoundError(
            f"No social map directory found for pool '{pool_name}'. "
            f"Pool may not exist or social discovery has not been run."
        )
    
    # Find social map files (exclude adjacency, metadata, and recursive summary files)
    social_map_files = [
        f for f in social_maps_dir.glob("*.json")
        if not f.name.endswith('_adjacency.json')
        and not f.name.endswith('_metadata.json')
        and not f.name.startswith('recursive_summary_')
    ]
    
    if not social_map_files:
        raise FileNotFoundError(
            f"No social map files found for pool '{pool_name}'. "
            f"Run social discovery to generate maps."
        )
    
    # Get latest file by modification time
    latest_file = max(social_map_files, key=lambda f: f.stat().st_mtime)
    
    # Load and validate
    try:
        with open(latest_file, 'r') as f:
            social_map = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in social map file {latest_file}: {e}")
    
    # Validate structure
    if 'accounts' not in social_map:
        raise ValueError(f"Social map missing 'accounts' field: {latest_file}")
    
    return social_map


@app.get("/health")
@limiter.limit("60/minute")
async def health_check(request: Request):
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/weights")
@limiter.limit("10/minute")
async def get_weights(request: Request) -> Dict:
    """
    Get all validator weights from disk.
    Rate limit: 10 requests per minute per IP.
    """
    try:
        state = load_state()
        scores = state["scores"]
        hotkeys = state["hotkeys"]
        
        weights_data = [
            {
                "uid": int(uid),
                "hotkey": str(hotkey),
                "raw_weight": float(score)
            }
            for uid, (hotkey, score) in enumerate(zip(hotkeys, scores))
        ]
        
        normalized = normalize_weights(scores)
        
        return {
            "step": int(state["step"]),
            "total_miners": int(len(weights_data)),
            "weights": weights_data,
            "normalized_weights": [float(x) for x in normalized]
        }
        
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error loading weights: {str(e)}")


@app.get("/weights/{uid}")
@limiter.limit("20/minute")
async def get_weight_by_uid(uid: int, request: Request) -> Dict:
    """
    Get weight for a specific UID.
    Rate limit: 20 requests per minute per IP.
    """
    try:
        state = load_state()
        scores = state["scores"]
        hotkeys = state["hotkeys"]
        
        if uid >= len(scores) or uid < 0:
            raise HTTPException(status_code=404, detail=f"UID {uid} not found")
        
        normalized = normalize_weights(scores)
        
        return {
            "uid": int(uid),
            "hotkey": str(hotkeys[uid]),
            "raw_weight": float(scores[uid]),
            "normalized_weight": float(normalized[uid])
        }
        
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error loading weight: {str(e)}")


@app.get("/social-map/{pool_name}")
@limiter.limit("5/minute")
async def get_social_map(pool_name: str, request: Request) -> Dict:
    """
    Get latest social map for a pool.
    Rate limit: 5 requests per minute per IP.
    
    Args:
        pool_name: Name of the pool (e.g., 'tao')
        
    Returns:
        {
            "pool_name": "tao",
            "created_at": "2025-11-15T10:30:00",
            "total_accounts": 150,
            "social_map": {
                "metadata": {...},
                "accounts": {...}
            }
        }
    """
    try:
        social_map = load_latest_social_map(pool_name)
        
        # Extract metadata if present
        metadata = social_map.get('metadata', {})
        created_at = metadata.get('created_at', 'unknown')
        total_accounts = metadata.get('total_accounts', len(social_map.get('accounts', {})))
        
        return {
            "pool_name": pool_name,
            "created_at": created_at,
            "total_accounts": total_accounts,
            "social_map": social_map
        }
        
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error loading social map: {str(e)}")


def run_api(host: str = "0.0.0.0", port: int = 8094):
    """Run the reference validator API server."""
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_api()

