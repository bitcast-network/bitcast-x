"""
Configuration for stability analysis.

Constants for windowed stability analysis and grid search definitions.
The validator runs in two-stage recursive mode only, so we define a
single grid covering core and extended stage parameters.
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
# Grid search definition (two-stage recursive)
#
# Pool-specific grids to account for different community sizes and characteristics.
# Values are lists of parameter values to sweep.
# ---------------------------------------------------------------------------

# Default grid for most pools (based on TAO optimization)
GRID = {
    "core": {
        "min_interaction_weight": [2, 4],
        "min_tweets": [5, 10],
        "max_seed_accounts": [100, 200],
    },
    "extended": {
        "min_interaction_weight": [1],
        "min_tweets": [1],
        "max_seed_accounts": [300, 500],
        "max_iterations": [3],
        "convergence_threshold": [0.9],
    },
}

# Prediction markets grid - larger community, keyword-heavy
# Key differences:
# - Higher max_seed_accounts to capture larger community
# - Higher min_interaction_weight to filter keyword-only accounts
# - Higher min_tweets for quality filtering
PREDICTION_MARKETS_GRID = {
    "core": {
        "min_interaction_weight": [2, 4],       # Test current vs stricter (keyword filtering)
        "min_tweets": [5, 10],                  # Higher thresholds for quality
        "max_seed_accounts": [200],              # Fixed larger seed pool
    },
    "extended": {
        "min_interaction_weight": [1, 2],        # Test looser vs stricter
        "min_tweets": [1],                       # Fixed at best practice
        "max_seed_accounts": [400, 600],         # Larger expansion for big community
        "max_iterations": [3],                   # Fixed at best practice
        "convergence_threshold": [0.9],          # Fixed at best practice
    },
}

# Map pool names to their specific grids
POOL_GRIDS = {
    "prediction_markets": PREDICTION_MARKETS_GRID,
    # Add more pool-specific grids here as needed
}
