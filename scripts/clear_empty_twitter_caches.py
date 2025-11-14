#!/usr/bin/env python3
"""
Utility script to clear empty tweet caches.

This removes cache entries for accounts that have no tweets,
which can happen due to API errors or legitimately empty accounts.

Usage:
    python scripts/clear_empty_caches.py
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from bitcast.validator.utils.twitter_cache import clear_empty_tweet_caches
import bittensor as bt


def main():
    """Run cache cleanup."""
    print("=" * 80)
    print("Twitter Cache Cleanup - Removing Empty Entries")
    print("=" * 80)
    print()
    
    # Run cleanup
    stats = clear_empty_tweet_caches()
    
    # Display results
    print()
    print("=" * 80)
    print("Cleanup Results:")
    print("=" * 80)
    print(f"  Entries checked:   {stats['checked']}")
    print(f"  Empty entries removed: {stats['removed']}")
    print(f"  Entries preserved: {stats['preserved']}")
    print("=" * 80)
    
    if stats['removed'] > 0:
        print(f"\n✅ Successfully removed {stats['removed']} empty cache entries")
        print("   These accounts will be re-fetched on next discovery run.")
    else:
        print("\n✅ No empty cache entries found - cache is clean!")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())


