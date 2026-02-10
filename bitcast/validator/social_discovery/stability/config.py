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
# One universal grid — pool name is only used for output file naming.
# Values are lists of parameter values to sweep.
# ---------------------------------------------------------------------------
GRID = {
    "core": {
        "min_interaction_weight": [2],
        "min_tweets": [5],
        "max_seed_accounts": [100],
    },
    "extended": {
        "min_interaction_weight": [1],
        "min_tweets": [1],
        "max_seed_accounts": [300],
        "max_iterations": [3],
        "convergence_threshold": [0.95],
    },
}
