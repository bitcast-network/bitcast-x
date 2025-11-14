"""
Simple read-only API for exposing validator weights.
Includes rate limiting for protection against abuse.
"""
from fastapi import FastAPI, HTTPException, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from pathlib import Path
import numpy as np
from typing import Dict, List
import uvicorn

from bitcast.validator.utils.config import WALLET_NAME, HOTKEY_NAME, MECHID
import os


# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Validator Weights API", version="1.0.0")
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


def run_api(host: str = "0.0.0.0", port: int = 8094):
    """Run the weights API server."""
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_api()

