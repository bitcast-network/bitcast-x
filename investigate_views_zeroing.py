"""
Investigation script for views_count zeroing issue.

This script helps investigate why views_count becomes 0 in the database
after a brief's end date (or end date + 1).

The hypothesis is that either:
1. Twitter's API stops providing view counts for tweets after a certain age
2. Cached tweets retain their old (0) view counts
3. There's a bug in how views are being propagated through the system
"""

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, List
import bittensor as bt

def analyze_scored_tweets_views(pool_name: str = "tao"):
    """
    Analyze view counts in scored tweets files to identify patterns.
    
    Args:
        pool_name: Pool to analyze (default: 'tao')
    """
    scored_tweets_dir = Path(__file__).parent / "bitcast" / "validator" / "tweet_scoring" / "scored_tweets" / pool_name
    
    if not scored_tweets_dir.exists():
        print(f"Scored tweets directory not found: {scored_tweets_dir}")
        return
    
    # Get all scored tweet files
    scored_files = sorted(scored_tweets_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    
    if not scored_files:
        print(f"No scored tweet files found in {scored_tweets_dir}")
        return
    
    print(f"Found {len(scored_files)} scored tweet files")
    print("-" * 80)
    
    for scored_file in scored_files[:10]:  # Analyze last 10 files
        try:
            with open(scored_file, 'r') as f:
                data = json.load(f)
            
            metadata = data.get('metadata', {})
            scored_tweets = data.get('scored_tweets', [])
            
            brief_id = metadata.get('brief_id', 'unknown')
            created_at = metadata.get('created_at', 'unknown')
            start_date_str = metadata.get('start_date', 'unknown')
            end_date_str = metadata.get('end_date', 'unknown')
            
            # Analyze view counts
            total_tweets = len(scored_tweets)
            tweets_with_views = sum(1 for t in scored_tweets if t.get('views_count', 0) > 0)
            tweets_with_zero_views = total_tweets - tweets_with_views
            
            # Calculate tweet ages relative to scoring time
            if created_at != 'unknown':
                scoring_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                
                tweet_ages = []
                for tweet in scored_tweets:
                    created_at_str = tweet.get('created_at', '')
                    if created_at_str:
                        try:
                            tweet_date = datetime.strptime(created_at_str, '%a %b %d %H:%M:%S %z %Y')
                            age_days = (scoring_time - tweet_date.replace(tzinfo=timezone.utc)).days
                            tweet_ages.append((age_days, tweet.get('views_count', 0)))
                        except:
                            pass
                
                # Analyze by age
                if tweet_ages:
                    avg_age = sum(age for age, _ in tweet_ages) / len(tweet_ages)
                    zero_view_ages = [age for age, views in tweet_ages if views == 0]
                    nonzero_view_ages = [age for age, views in tweet_ages if views > 0]
                    
                    print(f"\nBrief: {brief_id}")
                    print(f"File: {scored_file.name}")
                    print(f"Scoring time: {scoring_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"Brief dates: {start_date_str} to {end_date_str}")
                    print(f"Total tweets: {total_tweets}")
                    print(f"  - With views > 0: {tweets_with_views} ({tweets_with_views/total_tweets*100:.1f}%)")
                    print(f"  - With views = 0: {tweets_with_zero_views} ({tweets_with_zero_views/total_tweets*100:.1f}%)")
                    print(f"Average tweet age: {avg_age:.1f} days")
                    if zero_view_ages:
                        print(f"Tweets with 0 views - avg age: {sum(zero_view_ages)/len(zero_view_ages):.1f} days")
                    if nonzero_view_ages:
                        print(f"Tweets with >0 views - avg age: {sum(nonzero_view_ages)/len(nonzero_view_ages):.1f} days")
                    
        except Exception as e:
            print(f"Error analyzing {scored_file.name}: {e}")
    
    print("\n" + "=" * 80)


def analyze_published_tweets_views(endpoint_logs_dir: Path = None):
    """
    Analyze view counts in published tweet payloads to see what's being sent to the database.
    
    This would require access to logs or published data.
    """
    print("\nTo investigate views in published data:")
    print("1. Check the database directly for tweet view counts over time")
    print("2. Add logging in brief_tweet_publisher.py to track views_count being published")
    print("3. Compare views_count at different stages of the pipeline:")
    print("   - TwitterClient fetch")
    print("   - Scored tweets file")
    print("   - Filtered tweets")
    print("   - Published payload")


def check_twitter_cache_views():
    """
    Check Twitter cache for view count patterns.
    """
    from bitcast.validator.utils.config import CACHE_DIRS
    from diskcache import Cache
    
    cache_dir = CACHE_DIRS.get("twitter")
    if not cache_dir or not Path(cache_dir).exists():
        print(f"Twitter cache directory not found: {cache_dir}")
        return
    
    print(f"\nTwitter cache location: {cache_dir}")
    print("Cache contains user tweet data with views_count")
    print("To investigate cache behavior:")
    print("1. Check cache timestamps vs tweet ages")
    print("2. Monitor cache refresh patterns")
    print("3. Track views_count changes over time for same tweet_id")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Investigate views_count zeroing issue")
    parser.add_argument("--pool", type=str, default="tao", help="Pool name to analyze")
    args = parser.parse_args()
    
    print("=" * 80)
    print("VIEWS_COUNT ZEROING INVESTIGATION")
    print("=" * 80)
    
    analyze_scored_tweets_views(args.pool)
    analyze_published_tweets_views()
    check_twitter_cache_views()
    
    print("\n" + "=" * 80)
    print("RECOMMENDATIONS:")
    print("=" * 80)
    print("1. Add logging to track views_count at each stage of the pipeline")
    print("2. Compare Twitter API responses for same tweets at different times")
    print("3. Check if Twitter's API behavior changes for tweets older than N days")
    print("4. Consider forcing cache refresh during emission phase to get latest views")
    print("5. Check if the issue correlates with specific brief end dates")
    print("=" * 80)
