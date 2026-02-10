"""
Configuration for stability analysis.

Constants for windowed stability analysis and per-pool grid search definitions.
"""

from pathlib import Path


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
OUTPUT_DIR = Path(__file__).parent / "output"

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
DISCOVERY_WINDOW_DAYS = 30          # Days of data used for social map discovery

# ---------------------------------------------------------------------------
# Windowed stability analysis
# ---------------------------------------------------------------------------
EXTENDED_FETCH_DAYS = 60            # Total days of history to fetch for top accounts
NUM_WINDOWS = 4                     # Number of non-overlapping analysis windows
WINDOW_DAYS = 15                    # Days per window (60 / 4 = 15)
TOP_N_ACCOUNTS = 250                # Top accounts to track across windows

# ---------------------------------------------------------------------------
# Recursive discovery defaults
# ---------------------------------------------------------------------------
MAX_CORE_ITERATIONS = 10            # Max core-stage iterations
CORE_CONVERGENCE_THRESHOLD = 0.95   # Jaccard threshold for core convergence

# ---------------------------------------------------------------------------
# Per-pool grid search definitions
#
# Each pool has "core", "extended", and "extended_recursive" grids.
# Values are lists of parameter values to sweep.
# ---------------------------------------------------------------------------
GRID_DEFINITIONS = {
    "bittensor": {
        "core": {
            "min_interaction_weight": [2],
            "min_tweets": [5],
            "max_seed_accounts": [100],
        },
        "extended": {
            "min_interaction_weight": [1],
            "min_tweets": [1],
            "max_seed_accounts": [300],
            "recursive": [False],
        },
        "extended_recursive": {
            "min_interaction_weight": [1],
            "min_tweets": [1],
            "max_seed_accounts": [300],
            "recursive": [True],
            "max_iterations": [3],
            "convergence_threshold": [0.95],
        },
    },
    "prediction_markets": {
        "core": {
            "min_interaction_weight": [2, 3],
            "min_tweets": [5, 7],
            "max_seed_accounts": [100],
        },
        "extended": {
            "min_interaction_weight": [1],
            "min_tweets": [1, 2],
            "max_seed_accounts": [250, 300],
            "recursive": [False],
        },
        "extended_recursive": {
            "min_interaction_weight": [1],
            "min_tweets": [1, 2],
            "max_seed_accounts": [250, 300],
            "recursive": [True],
            "max_iterations": [3],
            "convergence_threshold": [0.90],
        },
    },
    "kalshi_prediction": {
        "core": {
            "min_interaction_weight": [1, 2],
            "min_tweets": [3, 5, 7],
            "max_seed_accounts": [100],
        },
        "extended": {
            "min_interaction_weight": [1],
            "min_tweets": [1],
            "max_seed_accounts": [150, 200, 250, 300],
            "recursive": [False],
        },
        "extended_recursive": {
            "min_interaction_weight": [1],
            "min_tweets": [1],
            "max_seed_accounts": [150, 200, 250, 300],
            "recursive": [True],
            "max_iterations": [3],
            "convergence_threshold": [0.90],
        },
    },
}
# Aliases — "tao" is the common CLI shorthand for "bittensor"
GRID_DEFINITIONS["tao"] = GRID_DEFINITIONS["bittensor"]

# ---------------------------------------------------------------------------
# Single-stage grid (rarely used, kept for completeness)
# ---------------------------------------------------------------------------
SINGLE_STAGE_GRID = {
    "min_interaction_weight": [1, 2, 3, 5, 8],
    "min_tweets": [1, 2, 3, 5],
    "max_seed_accounts": [100, 150, 200],
}
