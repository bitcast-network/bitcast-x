"""
Stability analysis for social discovery maps.

Provides windowed temporal stability metrics and parameter grid search.
Uses the production TwitterNetworkAnalyzer and TwitterClient directly.

Usage:
    python -m bitcast.validator.social_discovery.stability.cli --help
"""

from .analyzer import StabilityAnalyzer
from .grid_search import GridSearchRunner

__all__ = ["StabilityAnalyzer", "GridSearchRunner"]
