"""
PageRank-based social discovery engine for X platform.

Replaces random scoring with real Twitter network analysis using PageRank algorithm.
Analyzes interactions (mentions, retweets, quotes) to discover social influence networks.
"""

import asyncio
import json
import os
import numpy as np
import networkx as nx
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
import bittensor as bt
import argparse
from dotenv import load_dotenv

# Initialize environment BEFORE importing custom modules when running standalone
if __name__ == "__main__":
    # Load environment variables from .env file
    env_path = Path(__file__).parents[1] / '.env'  # bitcast/validator/.env
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        print(f"Loaded environment variables from {env_path}")

# Now import custom modules that use bt.logging
from bitcast.validator.clients.twitter_client import TwitterClient
from bitcast.validator.tweet_scoring.social_map_loader import parse_social_map_filename
from .pool_manager import PoolManager
from .social_map_publisher import publish_social_map, republish_latest_social_map
from bitcast.validator.utils.config import (
    PAGERANK_MENTION_WEIGHT,
    PAGERANK_RETWEET_WEIGHT,
    PAGERANK_QUOTE_WEIGHT,
    PAGERANK_ALPHA,
    ENABLE_DATA_PUBLISH,
    WALLET_NAME,
    HOTKEY_NAME,
    SOCIAL_DISCOVERY_MAX_WORKERS
)
from bitcast.validator.utils.data_publisher import get_global_publisher, initialize_global_publisher


class TwitterNetworkAnalyzer:
    """
    Analyzes Twitter interaction networks and calculates PageRank scores.
    
    Consolidated class that handles the complete pipeline:
    - Tweet fetching and caching
    - Interaction network construction
    - PageRank calculation
    - Score normalization
    """
    
    def __init__(self, twitter_client: Optional[TwitterClient] = None, max_workers: Optional[int] = None, force_cache_refresh: bool = False):
        """
        Initialize analyzer with optional custom Twitter client.
        
        Args:
            twitter_client: Optional TwitterClient instance (typically for testing/mocking).
                          If provided, force_cache_refresh is ignored.
            max_workers: Number of concurrent workers (1=sequential, 2+=concurrent)
                        If None, uses SOCIAL_DISCOVERY_MAX_WORKERS config
            force_cache_refresh: If True, force Twitter API cache refresh. Only applied when
                               twitter_client is not provided.
        """
        self.twitter_client = twitter_client or TwitterClient(force_cache_refresh=force_cache_refresh)
        
        # PageRank weights
        self.tag_weight = PAGERANK_MENTION_WEIGHT
        self.retweet_weight = PAGERANK_RETWEET_WEIGHT
        self.quote_weight = PAGERANK_QUOTE_WEIGHT
        self.alpha = PAGERANK_ALPHA
        
        # Concurrency configuration (minimum 1 worker)
        self.max_workers = max(
            max_workers if max_workers is not None else SOCIAL_DISCOVERY_MAX_WORKERS,
            1
        )
        if self.max_workers > 1:
            bt.logging.info(f"Concurrent mode enabled with {self.max_workers} workers")
        else:
            bt.logging.info("Sequential mode (concurrency disabled)")
    
    def _fetch_tweets_safe(self, username: str) -> Tuple[str, List[Dict], Optional[str]]:
        """
        Fetch tweets for a user with error handling.
        
        Args:
            username: Twitter username
            
        Returns:
            Tuple of (username_lower, tweets_list, error_message)
        """
        try:
            result = self.twitter_client.fetch_user_tweets(username.lower())
            return username.lower(), result['tweets'], None
        except Exception as e:
            bt.logging.warning(f"Failed to fetch tweets for @{username}: {e}")
            return username.lower(), [], str(e)
    
    def _check_relevance_safe(self, username: str, keywords: List[str], min_followers: int, lang: Optional[str] = None) -> Tuple[str, bool]:
        """
        Check user relevance with error handling.
        
        Args:
            username: Twitter username
            keywords: Keywords to check
            min_followers: Minimum follower threshold
            lang: Optional language filter
            
        Returns:
            Tuple of (username, is_relevant)
        """
        try:
            return username, self.twitter_client.check_user_relevance(username, keywords, min_followers, lang)
        except Exception as e:
            bt.logging.warning(f"Relevance check failed for @{username}: {e}")
            return username, False
    
    def analyze_network(
        self, 
        seed_accounts: List[str], 
        keywords: List[str], 
        min_followers: int = 0,
        lang: Optional[str] = None
    ) -> Tuple[Dict[str, float], np.ndarray, List[str]]:
        """
        Analyze Twitter network and return PageRank scores.
        
        Args:
            seed_accounts: Initial accounts to analyze
            keywords: Keywords to filter accounts by
            min_followers: Minimum follower threshold
            lang: Optional language filter (e.g., 'en', 'zh')
            
        Returns:
            Tuple of (scores_dict, adjacency_matrix, usernames_list)
        """
        start_time = time.time()
        bt.logging.info(f"Analyzing network from {len(seed_accounts)} seed accounts")
        
        # Step 1: Fetch tweets for seed accounts
        fetch_start = time.time()
        all_tweets = {}
        failed_accounts = []
        
        if self.max_workers > 1:
            # Concurrent execution
            bt.logging.info(f"Fetching tweets concurrently ({self.max_workers} workers)...")
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_username = {
                    executor.submit(self._fetch_tweets_safe, username): username 
                    for username in seed_accounts
                }
                
                for future in as_completed(future_to_username):
                    username, tweets, error = future.result()
                    if error:
                        failed_accounts.append((username, error))
                    all_tweets[username] = tweets
        else:
            # Sequential execution
            for username in seed_accounts:
                username_lower = username.lower()
                result = self.twitter_client.fetch_user_tweets(username_lower)
                all_tweets[username_lower] = result['tweets']
        
        fetch_time = time.time() - fetch_start
        total_tweets = sum(len(tweets) for tweets in all_tweets.values())
        bt.logging.info(f"Fetched {total_tweets} tweets from {len(all_tweets)} accounts in {fetch_time:.1f}s")
        
        if failed_accounts:
            bt.logging.warning(f"Failed to fetch {len(failed_accounts)} accounts: {[acc for acc, _ in failed_accounts]}")
        
        # Step 2: Build interaction network
        interactions = {}  # (from_user, to_user) -> interaction_type
        discovered_users = set()
        
        # Track reply filtering for logging
        total_tweets_processed = 0
        reply_tweets_filtered = 0
        
        for from_user, tweets in all_tweets.items():
            for tweet in tweets:
                total_tweets_processed += 1
                
                # Skip reply tweets (consistent with rewards/scoring behavior)
                # Only analyze original tweets and quote tweets
                if tweet.get('in_reply_to_status_id'):
                    reply_tweets_filtered += 1
                    continue
                
                # Handle mentions
                for tagged_user in tweet.get('tagged_accounts', []):
                    if tagged_user != from_user:
                        interactions[(from_user, tagged_user)] = max(
                            interactions.get((from_user, tagged_user), 0), 
                            self.tag_weight
                        )
                        discovered_users.add(tagged_user)
                
                # Handle retweets
                if tweet.get('retweeted_user'):
                    retweeted_user = tweet['retweeted_user']
                    if retweeted_user != from_user:
                        interactions[(from_user, retweeted_user)] = max(
                            interactions.get((from_user, retweeted_user), 0),
                            self.retweet_weight
                        )
                        discovered_users.add(retweeted_user)
                
                # Handle quotes
                if tweet.get('quoted_user'):
                    quoted_user = tweet['quoted_user']
                    if quoted_user != from_user:
                        interactions[(from_user, quoted_user)] = max(
                            interactions.get((from_user, quoted_user), 0),
                            self.quote_weight
                        )
                        discovered_users.add(quoted_user)
        
        # Log reply filtering stats
        if reply_tweets_filtered > 0:
            bt.logging.info(
                f"Filtered {reply_tweets_filtered}/{total_tweets_processed} reply tweets "
                f"({reply_tweets_filtered/total_tweets_processed*100:.1f}%) - analyzing only original tweets and quotes"
            )
        
        # Step 3: Filter by keyword relevance
        if keywords:
            relevance_start = time.time()
            relevant_users = set()
            all_accounts_to_check = discovered_users | set(seed_accounts)
            
            if self.max_workers > 1:
                # Concurrent relevance checking
                bt.logging.info(f"Checking relevance concurrently ({self.max_workers} workers)...")
                with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {
                        executor.submit(self._check_relevance_safe, username, keywords, min_followers, lang): username
                        for username in all_accounts_to_check
                    }
                    
                    for future in as_completed(futures):
                        username, is_relevant = future.result()
                        if is_relevant:
                            relevant_users.add(username)
            else:
                # Sequential relevance checking
                for username in all_accounts_to_check:
                    if self.twitter_client.check_user_relevance(username, keywords, min_followers, lang):
                        relevant_users.add(username)
            
            relevance_time = time.time() - relevance_start
            bt.logging.info(f"Relevance check completed in {relevance_time:.1f}s: {len(relevant_users)}/{len(all_accounts_to_check)} relevant")
            
            # Filter interactions to only relevant users
            interactions = {
                (from_user, to_user): weight 
                for (from_user, to_user), weight in interactions.items()
                if from_user in relevant_users and to_user in relevant_users
            }
            
            all_users = relevant_users
        else:
            all_users = discovered_users | set(seed_accounts)
        
        bt.logging.info(f"Network: {len(all_users)} users, {len(interactions)} interactions")
        
        if not interactions:
            raise ValueError("No interactions found in network")
        
        # Step 4: Calculate PageRank
        G = nx.DiGraph()
        for (from_user, to_user), weight in interactions.items():
            G.add_edge(from_user, to_user, weight=weight)
        
        pagerank_scores = nx.pagerank(G, weight='weight', alpha=self.alpha, max_iter=1000)
        
        # Step 5: Normalize scores to sum to 1.0
        total_score = sum(pagerank_scores.values())
        normalized_scores = {user: score / total_score for user, score in pagerank_scores.items()}
        
        # Round and ensure exact sum of 1.0
        rounded_scores = {user: round(score, 6) for user, score in normalized_scores.items()}
        total_rounded = sum(rounded_scores.values())
        
        if abs(total_rounded - 1.0) > 1e-10:
            # Adjust highest scorer to make exact sum
            max_user = max(rounded_scores.keys(), key=lambda u: rounded_scores[u])
            rounded_scores[max_user] += 1.0 - total_rounded
            rounded_scores[max_user] = round(rounded_scores[max_user], 6)
        
        # Step 6: Create adjacency matrix
        usernames_sorted = sorted(list(all_users))
        n = len(usernames_sorted)
        adjacency_matrix = np.zeros((n, n))
        
        username_to_idx = {user: i for i, user in enumerate(usernames_sorted)}
        for (from_user, to_user), weight in interactions.items():
            if from_user in username_to_idx and to_user in username_to_idx:
                from_idx = username_to_idx[from_user]
                to_idx = username_to_idx[to_user]
                adjacency_matrix[from_idx, to_idx] = weight
        
        bt.logging.info(f"PageRank complete: {len(rounded_scores)} accounts mapped")
        
        # Final performance summary
        total_elapsed = time.time() - start_time
        mode = "concurrent" if self.max_workers > 1 else "sequential"
        bt.logging.info(
            f"✅ Network analysis completed in {total_elapsed:.1f}s "
            f"({mode} mode with {self.max_workers} worker{'s' if self.max_workers > 1 else ''})"
        )
        
        return rounded_scores, adjacency_matrix, usernames_sorted


def discover_social_network(
    pool_name: str = "tao", 
    run_id: Optional[str] = None,
    force_cache_refresh: bool = False
) -> str:
    """
    Discover social network using PageRank network analysis.
    
    Args:
        pool_name: Name of the pool to discover social network for
        run_id: Validation cycle identifier (auto-generated if not provided)
        force_cache_refresh: If True, force Twitter API cache refresh for all accounts
        
    Returns:
        Path to saved social map file
    """
    # Generate default run_id with validator hotkey if not provided
    if run_id is None:
        try:
            publisher = get_global_publisher()
            vali_hotkey = publisher.wallet.hotkey.ss58_address
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_id = f"vali_x_{vali_hotkey}_{timestamp}"
        except RuntimeError:
            # Global publisher not initialized - fallback to timestamp only
            run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    bt.logging.info(f"Discovering social network for pool: {pool_name} with run_id: {run_id}")
    
    try:
        # Load pool configuration
        pool_manager = PoolManager()
        pool_config = pool_manager.get_pool(pool_name)
        
        if not pool_config:
            raise Exception(f"Pool '{pool_name}' not found in configuration")
        
        # Get existing accounts or use initial accounts
        social_maps_dir = Path(__file__).parent / "social_maps"
        pool_dir = social_maps_dir / pool_name
        
        seed_accounts = []
        
        if pool_dir.exists():
            # Look for existing social map files
            social_map_files = [f for f in pool_dir.glob("*.json") 
                              if not f.name.endswith('_adjacency.json') 
                              and not f.name.endswith('_metadata.json')
                              and not f.name.startswith('recursive_summary_')]
            if social_map_files:
                # Use latest social map by filename timestamp
                latest_file = max(
                    social_map_files,
                    key=lambda f: parse_social_map_filename(f.name) or datetime.min.replace(tzinfo=timezone.utc)
                )
                with open(latest_file, 'r') as f:
                    existing_data = json.load(f)
                
                # Extract top accounts by score as seeds
                max_seed_accounts = pool_config.get('max_seed_accounts', 150)
                
                # Get all accounts sorted by score
                all_accounts = [
                    (acc, data.get('score', 0.0))
                    for acc, data in existing_data['accounts'].items()
                ]
                
                # Sort by score descending and take top N
                all_accounts.sort(key=lambda x: x[1], reverse=True)
                seed_accounts = [acc for acc, _ in all_accounts[:max_seed_accounts]]
                
                bt.logging.info(f"Using top {len(seed_accounts)} accounts (max: {max_seed_accounts}) from previous run as seeds")
        
        if not seed_accounts:
            seed_accounts = pool_config['initial_accounts']
            bt.logging.info(f"Using {len(seed_accounts)} initial accounts as seeds")
        
        # Analyze network
        analyzer = TwitterNetworkAnalyzer(force_cache_refresh=force_cache_refresh)
        scores, adjacency_matrix, usernames = analyzer.analyze_network(
            seed_accounts=seed_accounts,
            keywords=pool_config['keywords'],
            min_followers=0,
            lang=pool_config.get('lang')
        )
        
        # Create social map data structure
        # Sort accounts by score in descending order
        sorted_accounts = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        
        social_map_data = {
            'metadata': {
                'created_at': datetime.now().isoformat(),
                'pool_name': pool_name,
                'total_accounts': len(scores)
            },
            'accounts': {
                username: {
                    'score': score
                }
                for username, score in sorted_accounts
            }
        }
        
        # Save results
        pool_dir.mkdir(parents=True, exist_ok=True)
        timestamp_str = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
        
        # Save social map
        social_map_file = pool_dir / f"{timestamp_str}.json"
        with open(social_map_file, 'w') as f:
            json.dump(social_map_data, f, indent=2)
        
        # Save adjacency matrix
        matrix_file = pool_dir / f"{timestamp_str}_adjacency.json"
        matrix_data = {
            'usernames': usernames,
            'adjacency_matrix': adjacency_matrix.tolist(),
            'created_at': datetime.now().isoformat()
        }
        with open(matrix_file, 'w') as f:
            json.dump(matrix_data, f, indent=2)
        
        # Save metadata file
        metadata_file = pool_dir / f"{timestamp_str}_metadata.json"
        
        # Try to get validator hotkey, but don't fail if unavailable
        validator_hotkey = None
        try:
            publisher = get_global_publisher()
            validator_hotkey = publisher.wallet.hotkey.ss58_address
        except RuntimeError as e:
            bt.logging.debug(f"No global publisher available for metadata: {e}")
        except Exception as e:
            bt.logging.debug(f"Could not retrieve validator hotkey for metadata: {e}")
        
        metadata = {
            'run_id': run_id,
            'validator_hotkey': validator_hotkey,
            'created_at': datetime.now().isoformat(),
            'pool_name': pool_name
        }
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        bt.logging.info(f"✅ Social discovery completed successfully! Saved results to {pool_dir}")
        
        # Publish social map data if enabled (fire-and-forget pattern)
        if ENABLE_DATA_PUBLISH:
            try:
                success = asyncio.run(publish_social_map(
                    pool_name=pool_name,
                    social_map_data=social_map_data,
                    adjacency_matrix=adjacency_matrix,
                    usernames=usernames,
                    run_id=run_id
                ))
                if success:
                    bt.logging.info(f"🚀 Social map data published successfully for pool {pool_name}")
                else:
                    bt.logging.warning(f"⚠️ Social map data publishing failed for pool {pool_name} (local results saved)")
            except RuntimeError as e:
                # No global publisher initialized - log but don't fail
                bt.logging.debug(f"📴 Social map publishing skipped - no global publisher: {e}")
            except Exception as e:
                # Log but don't fail social discovery (fire-and-forget pattern)
                bt.logging.warning(f"⚠️ Social map publishing failed: {e} (local results saved)")
        else:
            bt.logging.debug("📴 Social map publishing disabled by config")
        
        return str(social_map_file)
        
    except Exception as e:
        bt.logging.error(f"❌ Social discovery core process failed: {e}")
        raise


def run_discovery_for_stale_pools() -> Dict[str, str]:
    """
    Run social discovery only for pools that need updating today.
    
    Checks each active pool's latest social map timestamp.
    Only runs discovery for pools without a map from today (UTC).
    Only runs every 2 weeks on Sundays.
    
    Always forces cache refresh to ensure fresh Twitter data for bi-weekly discovery.
    
    Returns:
        Dict mapping pool_name to social_map_path for pools that ran
    """
    from datetime import timezone, date, timedelta
    
    now = datetime.now(timezone.utc)
    
    # Only run on Sundays
    if now.weekday() != 6:
        bt.logging.debug("Not Sunday - skipping social discovery")
        return {}
    
    # Only run every 2 weeks (reference: November 09, 2025)
    reference_date = date(2025, 11, 9)
    today = now.date()
    days_since_reference = (today - reference_date).days
    if days_since_reference % 14 != 0:
        next_run_date = today + timedelta(days=14 - (days_since_reference % 14))
        bt.logging.debug(f"Skipping social discovery. Next run: {next_run_date}")
        return {}
    
    bt.logging.info("🔄 Bi-weekly discovery: forcing cache refresh for fresh Twitter data")
    
    pool_manager = PoolManager()
    results = {}
    
    for pool_name, config in pool_manager.pools.items():
        if not config.get('active', True):
            continue
        
        # Check if this specific pool needs update
        social_maps_dir = Path(__file__).parent / "social_maps" / pool_name
        needs_update = False
        
        if not social_maps_dir.exists():
            needs_update = True
        else:
            social_map_files = [
                f for f in social_maps_dir.glob("*.json")
                if not f.name.endswith(('_adjacency.json', '_metadata.json'))
                and not f.name.startswith('recursive_summary_')
            ]
            
            if not social_map_files:
                needs_update = True
            else:
                # Parse timestamp from filename
                latest_file = max(
                    social_map_files,
                    key=lambda f: parse_social_map_filename(f.name) or datetime.min.replace(tzinfo=timezone.utc)
                )
                latest_timestamp = parse_social_map_filename(latest_file.name)
                latest_date = latest_timestamp.date() if latest_timestamp else datetime.min.date()
                needs_update = (latest_date < today)
        
        # Only run if needed
        if needs_update:
            try:
                bt.logging.info(f"Running discovery for {pool_name} (no map from today)")
                social_map_path = discover_social_network(
                    pool_name=pool_name, 
                    force_cache_refresh=True
                )
                results[pool_name] = social_map_path
            except Exception as e:
                bt.logging.error(f"Discovery failed for {pool_name}: {e}")
    
    return results


if __name__ == "__main__":
    """Standalone social network discovery."""
    try:
        # Create argument parser with all options
        parser = argparse.ArgumentParser(
            description="Discover social network using PageRank analysis"
        )
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)
        bt.subtensor.add_args(parser)
        
        parser.add_argument(
            "--pool-name",
            type=str,
            default="tao",
            help="Name of the pool to discover (default: tao)"
        )
        
        # Build args list from environment variables for wallet config
        # Start with command-line args, then add environment-based defaults
        import sys
        args_list = sys.argv[1:]  # Get actual command-line arguments
        
        # Add wallet config from env if not already in CLI args
        if WALLET_NAME and '--wallet.name' not in args_list:
            args_list.extend(['--wallet.name', WALLET_NAME])
        if HOTKEY_NAME and '--wallet.hotkey' not in args_list:
            args_list.extend(['--wallet.hotkey', HOTKEY_NAME])
        
        # Add debug logging if no logging level specified
        if not any(arg.startswith('--logging.') for arg in args_list):
            args_list.insert(0, '--logging.debug')
        
        # Parse configuration with merged args
        config = bt.config(parser, args=args_list)
        bt.logging.set_config(config=config.logging)
        
        # Initialize global publisher with properly configured wallet
        wallet = bt.wallet(config=config)
        initialize_global_publisher(wallet)
        bt.logging.info("🌐 Global publisher initialized for standalone mode")
        
        # Auto-generate run_id with validator hotkey
        vali_hotkey = wallet.hotkey.ss58_address
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = f"vali_x_{vali_hotkey}_{timestamp}"
        
        saved_file = discover_social_network(
            pool_name=config.pool_name,
            run_id=run_id
        )
        print(f"✅ Social network discovered: {saved_file}")
    except Exception as e:
        print(f"❌ Error: {e}")
        exit(1)