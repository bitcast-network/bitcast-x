"""
Main tweet scorer orchestrator.

Coordinates the complete tweet scoring pipeline from social map loading
through targeted search-based tweet discovery, engagement analysis, and score calculation.

Uses TweetDiscovery for efficient search-based tweet retrieval instead of
fetching all tweets from all accounts.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import bittensor as bt

from bitcast.validator.clients import TwitterClient
from bitcast.validator.utils.config import (
    PAGERANK_RETWEET_WEIGHT,
    PAGERANK_QUOTE_WEIGHT,
    BASELINE_TWEET_SCORE_FACTOR
)
from bitcast.validator.utils.data_publisher import get_global_publisher
from bitcast.validator.utils.date_utils import parse_brief_date
from bitcast.validator.social_discovery import PoolManager

from .social_map_loader import (
    load_latest_social_map,
    get_active_members,
    get_considered_accounts,
    load_relationship_scores
)
from .tweet_filter import TweetFilter
from .tweet_discovery import TweetDiscovery
from .score_calculator import ScoreCalculator


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
    max_members: Optional[int] = None,
    considered_accounts_limit: Optional[int] = None,
    thorough: bool = False,
) -> List[Dict]:
    """
    Score tweets for a pool based on RT/QRT engagement.
    
    Two discovery modes:
    - Lightweight (thorough=False): Uses search API for fast, targeted discovery
    - Thorough (thorough=True): Fetches connected accounts' timelines for complete coverage
    
    Steps:
    1. Loads pool configuration and social map
    2. Discovers tweets (via search or timeline, based on mode)
    3. Filters to active/connected accounts
    4. Gets engagement (RTs/QRTs) via direct API calls
    5. Calculates weighted scores
    6. Saves results to disk
    
    Args:
        pool_name: Name of pool to score tweets for
        brief_id: Brief identifier (used for naming the output file)
        connected_accounts: Optional set of connected account usernames to filter scoring
             If provided and non-empty, only tweets from accounts in this set will be scored
             If None or empty, all active members from the social map will be scored
        run_id: Optional run identifier (auto-generated if not provided)
        tag: Tag/string to filter tweets by (e.g., '#bitcast', '@elon')
             At least one of tag or qrt is REQUIRED
        qrt: Quoted tweet ID to filter by (e.g., '1983210945288569177')
             At least one of tag or qrt is REQUIRED
        start_date: Start date for brief window (inclusive, REQUIRED)
        end_date: End date for brief window (inclusive, REQUIRED)
        max_members: Optional limit on active members
        considered_accounts_limit: Optional limit on considered accounts
        thorough: If True, use timeline-based discovery instead of search API
        
    Returns:
        List of dicts with keys: author, tweet_id, score
        Complete results are also saved to file
        
    Raises:
        ValueError: If pool not found, social map doesn't exist, or neither tag nor qrt provided
    """
    start_time = time.time()
    
    bt.logging.info(f"Starting tweet scoring: pool={pool_name}, brief={brief_id}")
    
    # Validate that at least one of tag or qrt is provided
    if not tag and not qrt:
        raise ValueError(
            f"Brief '{brief_id}' must specify either 'tag' or 'qrt' field. "
            "Search-based scoring requires at least one filter."
        )
    
    # Generate run_id if not provided
    if run_id is None:
        try:
            publisher = get_global_publisher()
            vali_hotkey = publisher.wallet.hotkey.ss58_address
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_id = f"tweet_scoring_vali_x_{vali_hotkey}_{timestamp}"
        except RuntimeError:
            run_id = f"tweet_scoring_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    bt.logging.debug(f"Run ID: {run_id}")
    
    # Step 1: Load pool configuration
    bt.logging.debug("Loading pool configuration")
    
    pool_manager = PoolManager()
    pool_config = pool_manager.get_pool(pool_name)
    
    if not pool_config:
        raise ValueError(f"Pool '{pool_name}' not found in configuration")
    
    bt.logging.debug(f"Pool config: lang={pool_config.get('lang', 'any')}")
    
    # Validate dates
    if not start_date or not end_date:
        raise ValueError(f"Brief '{brief_id}' must specify both start_date and end_date")
    
    bt.logging.debug(f"Brief window: {start_date.date()} to {end_date.date()}")
    bt.logging.debug(f"Filters: tag={tag}, qrt={qrt}")
    
    # Step 2: Load social map and determine active members
    bt.logging.debug("Loading social map")
    
    if start_date and end_date and max_members:
        from .social_map_loader import get_active_members_for_brief
        active_members = get_active_members_for_brief(
            pool_name=pool_name,
            start_date=start_date,
            end_date=end_date,
            max_members=max_members
        )
        social_map, map_file = load_latest_social_map(pool_name)
    else:
        social_map, map_file = load_latest_social_map(pool_name)
        active_members = get_active_members(social_map, limit=max_members)
    
    # Get considered accounts
    DEFAULT_CONSIDERED = 300
    considered_limit = considered_accounts_limit or DEFAULT_CONSIDERED
    considered_accounts_list = get_considered_accounts(social_map, considered_limit)
    considered_accounts_dict = dict(considered_accounts_list)
    
    # Load relationship scores for cabal protection
    relationship_scores, scores_usernames, scores_username_to_idx = load_relationship_scores(pool_name)
    
    bt.logging.debug(f"Social map: {map_file}")
    bt.logging.info(
        f"  -> {len(active_members)} active members"
        f"{' (limited: ' + str(max_members) + ')' if max_members else ''}, "
        f"{len(considered_accounts_dict)} considered accounts"
    )
    
    # Filter to connected accounts if provided
    if connected_accounts:
        original_count = len(active_members)
        active_members = [m for m in active_members if m in connected_accounts]
        
        bt.logging.info(
            f"  -> Filtered to {len(active_members)} connected accounts "
            f"(excluded {original_count - len(active_members)} non-connected)"
        )
        
        if not active_members:
            bt.logging.warning(f"No connected accounts found in social map for pool {pool_name}")
            return []
    
    # Step 3: Discover tweets
    mode_label = "thorough (timeline)" if thorough else "lightweight (search)"
    bt.logging.info(f"Discovering tweets via {mode_label} mode")
    
    # Search mode needs a client for API queries; thorough mode creates its own
    twitter_client = TwitterClient(posts_only=False) if not thorough else TwitterClient()
    
    discovery = TweetDiscovery(
        client=twitter_client,
        active_accounts=set(active_members),
        considered_accounts=considered_accounts_dict,
    )
    
    discover_start = time.time()
    
    if thorough:
        discovered_tweets = discovery.discover_tweets_from_timelines(
            tag=tag,
            qrt=qrt,
            start_date=start_date,
            end_date=end_date,
        )
    else:
        discovered_tweets = discovery.discover_tweets(
            tag=tag,
            qrt=qrt,
            start_date=start_date,
            end_date=end_date,
            max_results=500,
        )
    
    discover_time = time.time() - discover_start
    bt.logging.info(f"  -> Discovered {len(discovered_tweets)} tweets ({discover_time:.1f}s)")
    
    if not discovered_tweets:
        bt.logging.warning(f"No tweets found matching criteria for brief {brief_id}")
        # Still save empty results for audit
        metadata = {
            'run_id': run_id,
            'brief_id': brief_id,
            'created_at': datetime.now().isoformat(),
            'pool_name': pool_name,
            'tag_filter': tag,
            'qrt_filter': qrt,
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat(),
            'total_tweets_scored': 0,
            'execution_time_seconds': round(time.time() - start_time, 2)
        }
        save_scored_tweets(pool_name, brief_id, [], metadata)
        return []
    
    # Step 4: Filter by content (language, type)
    tweet_filter = TweetFilter(language=pool_config.get('lang'), tag=tag, qrt=qrt)
    filtered_tweets = tweet_filter.filter_tweets(discovered_tweets)
    
    bt.logging.debug(f"After content filter: {len(filtered_tweets)} tweets")
    
    # Step 5: Get engagements and score tweets
    bt.logging.info("Retrieving engagements and scoring tweets")
    
    # Exclude brief participants from contributing to each other's scores
    excluded_engagers = {m.lower() for m in active_members}
    
    # Initialize score calculator
    calculator = ScoreCalculator(
        considered_accounts=considered_accounts_dict,
        relationship_scores=relationship_scores,
        scores_username_to_idx=scores_username_to_idx
    )
    
    score_start = time.time()
    
    # Fetch all engagements concurrently
    all_engagements = discovery.get_engagements_batch(
        tweets=filtered_tweets,
        excluded_engagers=excluded_engagers
    )
    
    # Score each tweet using pre-fetched engagements
    scored_tweets = []
    for tweet in filtered_tweets:
        tweet_id = tweet.get('tweet_id', '')
        author = tweet.get('author', '').lower()
        engagements = all_engagements.get(tweet_id, {})
        
        author_influence = considered_accounts_dict.get(author, calculator.min_influence_score)
        
        score, details = calculator.calculate_tweet_score(
            engagements=engagements,
            author_influence_score=author_influence,
            author=author
        )
        
        retweets = [d['username'] for d in details if d['engagement_type'] == 'retweet']
        quotes = [d['username'] for d in details if d['engagement_type'] == 'quote']
        
        scored_tweet = {
            'tweet_id': tweet_id,
            'author': author,
            'text': tweet.get('text', ''),
            'url': f"https://twitter.com/{author}/status/{tweet_id}",
            'created_at': tweet.get('created_at', ''),
            'lang': tweet.get('lang', 'und'),
            'score': score,
            'retweets': retweets,
            'quotes': quotes,
            'favorite_count': tweet.get('favorite_count', 0),
            'retweet_count': tweet.get('retweet_count', 0),
            'reply_count': tweet.get('reply_count', 0),
            'quote_count': tweet.get('quote_count', 0),
            'bookmark_count': tweet.get('bookmark_count', 0),
            'views_count': tweet.get('views_count', 0)
        }
        
        if tweet.get('quoted_tweet_id'):
            scored_tweet['quoted_tweet_id'] = tweet['quoted_tweet_id']
        
        scored_tweets.append(scored_tweet)
    
    score_time = time.time() - score_start
    bt.logging.debug(f"Scoring completed in {score_time:.1f}s")
    
    # Sort by score descending
    scored_tweets.sort(key=lambda t: t['score'], reverse=True)
    
    # Filter out zero-score tweets
    total_before = len(scored_tweets)
    scored_tweets = [t for t in scored_tweets if t['score'] > 0]
    
    bt.logging.debug(
        f"Filtered out {total_before - len(scored_tweets)} zero-score tweets, "
        f"keeping {len(scored_tweets)} with engagement"
    )
    
    # Calculate statistics
    total_score = sum(t['score'] for t in scored_tweets)
    
    if scored_tweets:
        bt.logging.info(
            f"  -> Scored {len(scored_tweets)} tweets (total: {total_score:.6f})"
        )
        bt.logging.debug(
            f"  Avg: {total_score / len(scored_tweets):.6f}, "
            f"Highest: {scored_tweets[0]['score']:.6f} (@{scored_tweets[0]['author']})"
        )
    
    # Step 6: Save results
    bt.logging.debug("Saving results")
    
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
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'date_range_days': (end_date - start_date).days,
        'total_tweets_scored': len(scored_tweets),
        'tweets_with_engagement': len(scored_tweets),
        'active_members_count': len(active_members),
        'max_members_limit': max_members,
        'considered_accounts_count': len(considered_accounts_dict),
        'considered_accounts_limit': considered_limit,
        'pool_language': pool_config.get('lang'),
        'social_map_file': map_file,
        'scoring_method': 'search_based',
        'weights': {
            'retweet_weight': PAGERANK_RETWEET_WEIGHT,
            'quote_weight': PAGERANK_QUOTE_WEIGHT,
            'BASELINE_TWEET_SCORE_FACTOR': BASELINE_TWEET_SCORE_FACTOR
        },
        'execution_time_seconds': round(time.time() - start_time, 2)
    }
    
    output_file = save_scored_tweets(pool_name, brief_id, scored_tweets, metadata)
    
    # Final summary
    total_time = time.time() - start_time
    bt.logging.info(f"Tweet scoring complete: {len(scored_tweets)} tweets scored ({total_time:.1f}s)")
    bt.logging.debug(f"Output: {output_file}")
    
    # Return simplified data structure
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
    from bitcast.validator.account_connection import ConnectionDatabase
    
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
            "--thorough",
            action="store_true",
            default=False,
            help="Use thorough (timeline-based) discovery instead of search API"
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
        start_date = parse_brief_date(brief_data.get('start_date'))
        end_date = parse_brief_date(brief_data.get('end_date'), end_of_day=True)
        
        # Extract brief-level configuration
        brief_max_members = brief_data.get('max_members')
        brief_max_considered = brief_data.get('max_considered')
        
        # Generate run_id for CLI execution
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = f"tweet_scoring_cli_{timestamp}"
        
        # Load connected accounts from database (matching production behavior)
        bt.logging.info(f"Loading connected accounts from database for pool '{pool_name}'...")
        db = ConnectionDatabase()
        
        # Get all connections and extract usernames
        # Note: CLI mode doesn't resolve UIDs (no metagraph), just checks for connection tags
        all_connections = db.get_all_connections(pool_name=pool_name)
        connected_accounts = {conn['account_username'].lower() for conn in all_connections}
        
        if connected_accounts:
            bt.logging.info(f"  → Found {len(connected_accounts)} connected accounts in database")
        else:
            bt.logging.warning(
                f"  → No connected accounts found in database for pool '{pool_name}'\n"
                f"     No tweets will be scored. Run connection scanner first:\n"
                f"     python -m bitcast.validator.account_connection.connection_scanner --pool-name {pool_name}"
            )
        
        # Run tweet scoring (with connected accounts filter - matching production)
        results = score_tweets_for_pool(
            pool_name=pool_name,
            brief_id=config.brief_id,
            connected_accounts=connected_accounts,
            run_id=run_id,
            tag=tag,
            qrt=qrt,
            start_date=start_date,
            end_date=end_date,
            max_members=brief_max_members,
            considered_accounts_limit=brief_max_considered,
            thorough=config.thorough
        )
        
        # Print summary
        print(f"\n✅ Tweet scoring complete: {len(results)} tweets scored")
        
        if results:
            print(f"\n📊 Scored Tweets (sorted by score):")
            print(f"{'Rank':<6} {'Score':<12} {'Author':<20} {'Tweet ID'}")
            print("-" * 80)
            
            for idx, tweet in enumerate(results[:100], 1):  # Show top 100
                author = tweet['author']
                tweet_id = tweet['tweet_id']
                score = tweet['score']
                print(f"{idx:<6} {score:<12.6f} @{author:<19} {tweet_id}")
            
            if len(results) > 100:
                print(f"\n... and {len(results) - 100} more tweets")
        else:
            print("\n⚠️  No tweets were scored (no tweets matched the brief criteria)")
        
    except KeyboardInterrupt:
        print("\n\n❌ Cancelled by user")
        exit(1)
    except Exception as e:
        bt.logging.error(f"Tweet scoring failed: {e}", exc_info=True)
        print(f"❌ Error: {e}")
        exit(1)

