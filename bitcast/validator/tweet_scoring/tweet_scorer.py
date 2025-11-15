"""
Main tweet scorer orchestrator.

Coordinates the complete tweet scoring pipeline from social map loading
through tweet fetching, filtering, engagement analysis, and score calculation.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import bittensor as bt

from bitcast.validator.clients import TwitterClient
from bitcast.validator.utils.config import (
    TWITTER_DEFAULT_LOOKBACK_DAYS,
    PAGERANK_RETWEET_WEIGHT,
    PAGERANK_QUOTE_WEIGHT,
    BASELINE_TWEET_SCORE_FACTOR,
    SOCIAL_DISCOVERY_MAX_WORKERS
)
from bitcast.validator.utils.data_publisher import get_global_publisher
from bitcast.validator.social_discovery import PoolManager

from .social_map_loader import (
    load_latest_social_map,
    get_active_members,
    get_considered_accounts
)
from .tweet_filter import TweetFilter
from .engagement_analyzer import EngagementAnalyzer
from .score_calculator import ScoreCalculator


def fetch_user_tweets_safe(
    client: TwitterClient,
    username: str
) -> Tuple[List[Dict], Optional[str]]:
    """
    Safely fetch tweets for a user with error handling.
    
    Args:
        client: TwitterClient instance
        username: Username to fetch tweets for
        
    Returns:
        Tuple of (tweets_list, error_message)
    """
    try:
        result = client.fetch_user_tweets(username, tweet_limit=100)
        tweets = result.get('tweets', [])
        
        # Add author field to each tweet (TwitterClient doesn't include it)
        for tweet in tweets:
            tweet['author'] = username.lower()
        
        return tweets, None
    except Exception as e:
        bt.logging.warning(f"Failed to fetch tweets for @{username}: {e}")
        return [], str(e)


def filter_tweets_by_date(tweets: List[Dict], cutoff_start: datetime, cutoff_end: Optional[datetime] = None) -> List[Dict]:
    """
    Filter tweets to only those within date range. All comparisons in UTC.
    
    Args:
        tweets: List of tweet dicts with 'created_at' field (Twitter UTC format)
        cutoff_start: Start datetime in UTC (tweets before this are excluded)
        cutoff_end: Optional end datetime in UTC (tweets after this are excluded)
        
    Returns:
        Filtered list of tweets within the date range
    """
    filtered = []
    for tweet in tweets:
        created_at = tweet.get('created_at', '')
        if not created_at:
            continue
        
        try:
            # Parse Twitter date format (UTC): "Wed Oct 30 12:00:00 +0000 2025"
            tweet_date = datetime.strptime(created_at, '%a %b %d %H:%M:%S %z %Y')
            tweet_date_utc = tweet_date.astimezone(timezone.utc)
            
            if tweet_date_utc >= cutoff_start:
                if cutoff_end is None or tweet_date_utc <= cutoff_end:
                    filtered.append(tweet)
        except ValueError as e:
            bt.logging.debug(f"Failed to parse date '{created_at}': {e}")
            # Include tweets with unparseable dates (permissive)
            filtered.append(tweet)
    
    return filtered


def save_scored_tweets(
    pool_name: str,
    brief_id: str,
    scored_tweets: List[Dict],
    metadata: Dict
) -> str:
    """
    Save scored tweets and metadata to single file.
    
    Args:
        pool_name: Name of the pool (used for directory structure)
        brief_id: Brief identifier (used in filename)
        scored_tweets: List of scored tweet dicts (already sorted by score desc)
        metadata: Metadata dict
        
    Returns:
        Path to saved scored tweets file
    """
    # Create output directory
    output_dir = Path(__file__).parent / "scored_tweets" / pool_name
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate timestamp
    timestamp_str = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    
    # Save scored tweets with metadata in single file
    output_file = output_dir / f"{brief_id}_{timestamp_str}.json"
    output_data = {
        'metadata': metadata,
        'scored_tweets': scored_tweets
    }
    
    with open(output_file, 'w') as f:
        json.dump(output_data, f, indent=2)
    
    bt.logging.debug(f"Saved {len(scored_tweets)} scored tweets to {output_file}")
    
    return str(output_file)


def score_tweets_for_pool(
    pool_name: str,
    brief_id: str,
    connected_accounts: Optional[set] = None,
    run_id: Optional[str] = None,
    tag: Optional[str] = None,
    qrt: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    force_cache_refresh: Optional[bool] = None
) -> List[Dict]:
    """
    Score tweets for a pool based on RT/QRT engagement.
    
    This is the main entry point for tweet scoring. It:
    1. Loads pool configuration and social map
    2. Fetches tweets from connected active members
    3. Filters tweets by date, type, language, optional tag, and optional QRT
    4. Analyzes engagement patterns
    5. Calculates weighted scores
    6. Saves results to disk
    
    Args:
        pool_name: Name of pool to score tweets for
        brief_id: Brief identifier (used for naming the output file)
        connected_accounts: Optional set of connected account usernames to filter scoring
             If provided and non-empty, only tweets from accounts in this set will be scored
             If None or empty, all active members from the social map will be scored
        run_id: Optional run identifier (auto-generated if not provided)
        tag: Optional tag/string to filter tweets by (e.g., '#bitcast', '@elon')
             Only tweets containing this tag will be scored
        qrt: Optional quoted tweet ID to filter by (e.g., '1983210945288569177')
             Only tweets that quote this specific tweet ID will be scored
        start_date: Optional start date for brief window (inclusive)
             Only tweets posted on or after this date will be scored
             If None, uses TWITTER_DEFAULT_LOOKBACK_DAYS
        end_date: Optional end date for brief window (inclusive)
             Only tweets posted on or before this date will be scored
             If None, uses current date
        force_cache_refresh: If True, force cache refresh (overrides FORCE_CACHE_REFRESH config)
             If None, uses FORCE_CACHE_REFRESH config variable
        
    Returns:
        List of dicts with keys: author, tweet_id, score
        Complete results are also saved to file
        
    Raises:
        ValueError: If pool not found or social map doesn't exist
    """
    start_time = time.time()
    
    bt.logging.info(f"🔍 Starting tweet scoring: pool={pool_name}, brief={brief_id}")
    
    # Generate run_id if not provided
    if run_id is None:
        try:
            publisher = get_global_publisher()
            vali_hotkey = publisher.wallet.hotkey.ss58_address
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_id = f"tweet_scoring_vali_x_{vali_hotkey}_{timestamp}"
        except RuntimeError:
            # Global publisher not initialized - fallback to timestamp
            run_id = f"tweet_scoring_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    bt.logging.debug(f"Run ID: {run_id}")
    
    # Step 1: Load pool configuration
    bt.logging.debug("Loading pool configuration")
    
    pool_manager = PoolManager()
    pool_config = pool_manager.get_pool(pool_name)
    
    if not pool_config:
        raise ValueError(f"Pool '{pool_name}' not found in configuration")
    
    bt.logging.debug(f"Pool config: lang={pool_config.get('lang', 'any')}, max_members={pool_config['max_members']}, "
                    f"considered_accounts={pool_config.get('considered_accounts', 256)}")
    
    # Set up date filtering (all dates in UTC)
    if start_date and end_date:
        cutoff_start = start_date
        cutoff_end = end_date
        bt.logging.debug(f"Brief window: {start_date.date()} to {end_date.date()}")
    else:
        # Fallback to lookback period
        cutoff_start = datetime.now(timezone.utc) - timedelta(days=TWITTER_DEFAULT_LOOKBACK_DAYS)
        cutoff_end = datetime.now(timezone.utc)
        bt.logging.debug(f"Using lookback period: {TWITTER_DEFAULT_LOOKBACK_DAYS} days")
    
    if tag or qrt:
        bt.logging.debug(f"Filters: tag={tag}, qrt={qrt}")
    
    # Step 2: Load latest social map
    bt.logging.debug("Loading social map")
    
    social_map, map_file = load_latest_social_map(pool_name)
    active_members = get_active_members(social_map)
    considered_accounts = get_considered_accounts(
        social_map,
        pool_config.get('considered_accounts', 256)
    )
    
    bt.logging.debug(f"Social map: {map_file}")
    bt.logging.info(f"  → {len(active_members)} active members, {len(considered_accounts)} considered accounts")
    
    # Filter to only connected accounts if provided
    if connected_accounts:
        original_count = len(active_members)
        active_members = [m for m in active_members if m in connected_accounts]
        filtered_count = len(active_members)
        
        bt.logging.info(
            f"  → Filtered to {filtered_count} connected accounts "
            f"(excluded {original_count - filtered_count} non-connected)"
        )
        
        if not active_members:
            bt.logging.warning(f"No connected accounts found in social map for pool {pool_name}")
            return []
    else:
        bt.logging.info("  → No connected accounts filter applied (scoring all active members)")
    
    # Step 3: Fetch tweets from connected active members
    bt.logging.debug("Fetching tweets from active members")
    
    twitter_client = TwitterClient(force_cache_refresh=force_cache_refresh)
    member_tweets = []
    failed_members = []
    
    fetch_start = time.time()
    
    # Use ThreadPoolExecutor for parallel fetching (like social_discovery)
    with ThreadPoolExecutor(max_workers=SOCIAL_DISCOVERY_MAX_WORKERS) as executor:
        future_to_member = {
            executor.submit(fetch_user_tweets_safe, twitter_client, member): member
            for member in active_members
        }
        
        for future in as_completed(future_to_member):
            member = future_to_member[future]
            tweets, error = future.result()
            
            if error:
                failed_members.append((member, error))
            else:
                member_tweets.extend(tweets)
    
    fetch_time = time.time() - fetch_start
    bt.logging.info(
        f"  → Fetched {len(member_tweets)} tweets from "
        f"{len(active_members) - len(failed_members)}/{len(active_members)} members "
        f"({fetch_time:.1f}s)"
    )
    
    if failed_members:
        bt.logging.debug(
            f"Failed to fetch from {len(failed_members)} members: "
            f"{[m for m, _ in failed_members[:5]]}"
            f"{' and more...' if len(failed_members) > 5 else ''}"
        )
    
    # Step 4: Fetch tweets from considered accounts
    bt.logging.debug("Fetching tweets from considered accounts")
    
    considered_tweets = []
    considered_usernames = [username for username, _ in considered_accounts]
    
    # Deduplicate with active members (many will overlap)
    unique_considered = set(considered_usernames) - set(active_members)
    bt.logging.debug(
        f"Fetching from {len(unique_considered)} additional considered accounts "
        f"(total {len(considered_accounts)}, {len(active_members)} already fetched)"
    )
    
    with ThreadPoolExecutor(max_workers=SOCIAL_DISCOVERY_MAX_WORKERS) as executor:
        future_to_account = {
            executor.submit(fetch_user_tweets_safe, twitter_client, account): account
            for account in unique_considered
        }
        
        for future in as_completed(future_to_account):
            tweets, error = future.result()
            if not error:
                considered_tweets.extend(tweets)
    
    # Combine all tweets for engagement detection
    all_tweets = member_tweets + considered_tweets
    bt.logging.debug(f"Total tweets for engagement analysis: {len(all_tweets)}")
    
    # Step 5: Filter member tweets by date window (only tweets in brief window get scored)
    # Note: considered_tweets are NOT filtered - they can contribute engagement from any time
    bt.logging.debug("Filtering member tweets by brief window")
    
    # Filter by date - only score tweets within the brief window
    date_filtered = filter_tweets_by_date(member_tweets, cutoff_start, cutoff_end)
    
    if start_date and end_date:
        bt.logging.debug(
            f"Date filter: {len(member_tweets)} → {len(date_filtered)} "
            f"(brief window: {start_date.date()} to {end_date.date()})"
        )
    else:
        bt.logging.debug(
            f"Date filter: {len(member_tweets)} → {len(date_filtered)} "
            f"(past {TWITTER_DEFAULT_LOOKBACK_DAYS} days)"
        )
    
    # Filter by content (language, optional tag, and optional QRT)
    tweet_filter = TweetFilter(language=pool_config.get('lang'), tag=tag, qrt=qrt)
    content_filtered = tweet_filter.filter_tweets(date_filtered)
    
    # Step 6: Score tweets
    bt.logging.debug("Calculating weighted scores")
    
    analyzer = EngagementAnalyzer()
    calculator = ScoreCalculator(dict(considered_accounts))
    
    scored_tweets = calculator.score_tweets_batch(
        content_filtered,
        all_tweets,
        analyzer
    )
    
    # Filter out tweets with 0 score
    total_tweets_before_filter = len(scored_tweets)
    scored_tweets = [t for t in scored_tweets if t['score'] > 0]
    
    bt.logging.debug(
        f"Filtered out {total_tweets_before_filter - len(scored_tweets)} tweets with 0 score, "
        f"keeping {len(scored_tweets)} tweets with engagement"
    )
    
    # Calculate statistics
    total_score = sum(t['score'] for t in scored_tweets)
    tweets_with_engagement = len(scored_tweets)  # All remaining tweets have engagement
    
    bt.logging.debug(f"  → Scored {len(scored_tweets)} tweets (total score: {total_score:.6f})")
    if scored_tweets:
        bt.logging.debug(f"  Avg score: {total_score / len(scored_tweets):.6f}, "
                        f"Highest: {scored_tweets[0]['score']:.6f} (@{scored_tweets[0]['author']})")
    
    # Step 7: Build metadata
    bt.logging.debug("Saving results")
    
    # Try to get validator hotkey
    validator_hotkey = None
    try:
        publisher = get_global_publisher()
        validator_hotkey = publisher.wallet.hotkey.ss58_address
    except RuntimeError:
        pass
    
    metadata = {
        'run_id': run_id,
        'brief_id': brief_id,
        'validator_hotkey': validator_hotkey,
        'created_at': datetime.now().isoformat(),
        'pool_name': pool_name,
        'tag_filter': tag,
        'qrt_filter': qrt,
        'start_date': start_date.isoformat() if start_date else None,
        'end_date': end_date.isoformat() if end_date else None,
        'lookback_days': TWITTER_DEFAULT_LOOKBACK_DAYS if not (start_date and end_date) else None,
        'total_tweets_scored': len(scored_tweets),
        'tweets_with_engagement': tweets_with_engagement,
        'active_members_count': len(active_members),
        'considered_accounts_count': len(considered_accounts),
        'pool_language': pool_config.get('lang'),
        'social_map_file': map_file,
        'weights': {
            'retweet_weight': PAGERANK_RETWEET_WEIGHT,
            'quote_weight': PAGERANK_QUOTE_WEIGHT,
            'BASELINE_TWEET_SCORE_FACTOR': BASELINE_TWEET_SCORE_FACTOR
        },
        'execution_time_seconds': round(time.time() - start_time, 2)
    }
    
    # Save results
    output_file = save_scored_tweets(pool_name, brief_id, scored_tweets, metadata)
    
    # Final summary
    total_time = time.time() - start_time
    bt.logging.debug(f"✅ Tweet scoring complete: {len(scored_tweets)} tweets scored ({total_time:.1f}s)")
    bt.logging.debug(f"Output: {output_file}")
    
    # Return simplified data structure for programmatic use
    result = [
        {
            'author': tweet['author'],
            'tweet_id': tweet['tweet_id'],
            'score': tweet['score']
        }
        for tweet in scored_tweets
    ]
    
    return result


# CLI interface for standalone execution
if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    from bitcast.validator.reward_engine.utils import get_briefs
    
    # Load environment variables
    env_path = Path(__file__).parents[1] / '.env'
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        print(f"Loaded environment variables from {env_path}")
    
    try:
        # Create argument parser
        parser = argparse.ArgumentParser(
            description="Score tweets for a brief - fetches brief details from server"
        )
        bt.logging.add_args(parser)
        
        parser.add_argument(
            "--brief-id",
            type=str,
            default=None,
            help="Brief identifier (fetches pool, dates, filters from brief server) (required)"
        )
        
        parser.add_argument(
            "--force-cache-refresh",
            action="store_true",
            help="Force cache refresh - ignores freshness check (overrides config)"
        )
        
        # Build args list from command line
        import sys
        args_list = sys.argv[1:]
        
        # Add info logging if no logging level specified
        if not any(arg.startswith('--logging.') for arg in args_list):
            args_list.insert(0, '--logging.info')
        
        # Parse configuration
        config = bt.config(parser, args=args_list)
        bt.logging.set_config(config=config.logging)
        
        # Validate required arguments
        if not config.brief_id:
            raise ValueError("--brief-id is required")
        
        # Fetch brief from server
        bt.logging.info(f"Fetching brief '{config.brief_id}' from brief server...")
        briefs = get_briefs()
        brief_data = next((b for b in briefs if b['id'] == config.brief_id), None)
        
        if not brief_data:
            raise ValueError(f"Brief ID '{config.brief_id}' not found on brief server")
        
        # Extract brief parameters
        pool_name = brief_data.get('pool', 'tao')
        tag = brief_data.get('tag')
        qrt = brief_data.get('qrt')
        
        bt.logging.info(f"  → Brief: pool={pool_name}, "
                       f"dates={brief_data.get('start_date')} to {brief_data.get('end_date')}, "
                       f"tag={tag or 'none'}, qrt={qrt or 'none'}")
        
        # Parse dates from brief
        start_date_str = brief_data.get('start_date')
        if start_date_str:
            try:
                start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
                start_date = start_date.astimezone(timezone.utc)
            except (ValueError, AttributeError):
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
                start_date = start_date.replace(tzinfo=timezone.utc)
        else:
            start_date = None
        
        end_date_str = brief_data.get('end_date')
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                end_date = end_date.astimezone(timezone.utc)
            except (ValueError, AttributeError):
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
                end_date = end_date.replace(tzinfo=timezone.utc)
        else:
            end_date = None
        
        # Generate run_id for CLI execution
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = f"tweet_scoring_cli_{timestamp}"
        
        # Determine force_cache_refresh (CLI flag overrides config)
        force_cache_refresh = config.force_cache_refresh if hasattr(config, 'force_cache_refresh') and config.force_cache_refresh else None
        
        if force_cache_refresh:
            bt.logging.info("Force cache refresh enabled - ignoring cache freshness check")
        
        # Run tweet scoring (without connected accounts filter)
        results = score_tweets_for_pool(
            pool_name=pool_name,
            brief_id=config.brief_id,
            connected_accounts=None,
            run_id=run_id,
            tag=tag,
            qrt=qrt,
            start_date=start_date,
            end_date=end_date,
            force_cache_refresh=force_cache_refresh
        )
        
        # Print summary
        print(f"\n✅ Tweet scoring complete: {len(results)} tweets scored")
        
        if results:
            print(f"\n📊 Scored Tweets (sorted by score):")
            print(f"{'Rank':<6} {'Score':<12} {'Author':<20} {'Tweet ID'}")
            print("-" * 80)
            
            for idx, tweet in enumerate(results[:20], 1):  # Show top 20
                author = tweet['author']
                tweet_id = tweet['tweet_id']
                score = tweet['score']
                print(f"{idx:<6} {score:<12.6f} @{author:<19} {tweet_id}")
            
            if len(results) > 20:
                print(f"\n... and {len(results) - 20} more tweets")
        else:
            print("\n⚠️  No tweets were scored (no tweets matched the brief criteria)")
        
    except KeyboardInterrupt:
        print("\n\n❌ Cancelled by user")
        exit(1)
    except Exception as e:
        bt.logging.error(f"Tweet scoring failed: {e}", exc_info=True)
        print(f"❌ Error: {e}")
        exit(1)

