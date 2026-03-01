"""
CLI entry point for social discovery module.

Usage:
    python -m bitcast.validator.social_discovery --pool-name tao

This delegates to recursive_discovery.two_stage_discovery which implements
the two-stage social discovery with personalized PageRank.
"""

from bitcast.validator.social_discovery.recursive_discovery import main

if __name__ == "__main__":
    main()
