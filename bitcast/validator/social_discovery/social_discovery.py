"""
PageRank-based social discovery engine for X platform.

Provides TwitterNetworkAnalyzer for PageRank-based social network analysis.
Analyzes interactions (mentions, retweets, quotes) to discover social influence networks.
"""

import json
import numpy as np
import networkx as nx
import time
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
import bittensor as bt

from bitcast.validator.clients.twitter_client import TwitterClient
from bitcast.validator.tweet_scoring.social_map_loader import parse_social_map_filename
from .pool_manager import PoolManager
from bitcast.validator.utils.config import (
    PAGERANK_MENTION_WEIGHT,
    PAGERANK_RETWEET_WEIGHT,
    PAGERANK_QUOTE_WEIGHT,
    PAGERANK_ALPHA,
    SOCIAL_DISCOVERY_MAX_WORKERS,
    SOCIAL_DISCOVERY_LOOKBACK,
    SOCIAL_DISCOVERY_FETCH_DAYS
)
from bitcast.validator.utils.twitter_cache import get_cached_user_tweets
from bitcast.validator.utils.twitter_validators import is_valid_twitter_username

# Reference date for social discovery scheduling
DISCOVERY_REFERENCE_DATE = date(2025, 11, 9)


class TwitterNetworkAnalyzer:
    """
    Analyzes Twitter interaction networks and calculates PageRank scores.
    
    Consolidated class that handles the complete pipeline:
    - Tweet fetching and caching
    - Interaction network construction
    - PageRank calculation
    - Score normalization
    """
    
    def __init__(
        self,
        twitter_client: Optional[TwitterClient] = None,
        max_workers: Optional[int] = None,
        fetch_days: Optional[int] = None,
        posts_only: bool = True,
        max_data_age_days: Optional[int] = None,
        skip_if_cache_fresh: bool = False,
    ):
        """
        Initialize analyzer with optional custom Twitter client.

        Args:
            twitter_client: Optional TwitterClient instance (typically for testing/mocking).
                          If provided, posts_only is ignored.
            max_workers: Number of concurrent workers (1=sequential, 2+=concurrent)
                        If None, uses SOCIAL_DISCOVERY_MAX_WORKERS config
            fetch_days: Number of days of tweet history to fetch per account.
                       If None, uses SOCIAL_DISCOVERY_FETCH_DAYS default (30 days).
            posts_only: If True, use only /user/tweets endpoint (faster, saves quota).
                       Default: True for social discovery. Only applied when twitter_client is not provided.
            max_data_age_days: Maximum age of cached tweets to use in analysis (in days).
                              If None, uses all cached data. Default: None
            skip_if_cache_fresh: If True, skip API calls if cache was updated within freshness window.
                                Default: False for full fetches, useful for extended iterations.
        """
        self.twitter_client = twitter_client or TwitterClient(posts_only=posts_only)
        self.fetch_days = fetch_days or SOCIAL_DISCOVERY_FETCH_DAYS
        self.max_data_age_days = max_data_age_days
        self.skip_if_cache_fresh = skip_if_cache_fresh
        
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
    
    def _fetch_tweets_safe(self, username: str, skip_if_cache_fresh: Optional[bool] = None) -> Tuple[str, List[Dict], Dict, Optional[str]]:
        """
        Fetch tweets for a user with error handling.

        Args:
            username: Twitter username
            skip_if_cache_fresh: If provided, overrides instance's skip_if_cache_fresh setting

        Returns:
            Tuple of (username_lower, tweets_list, user_info, error_message)
        """
        try:
            skip_fresh = skip_if_cache_fresh if skip_if_cache_fresh is not None else self.skip_if_cache_fresh
            result = self.twitter_client.fetch_user_tweets(
                username.lower(),
                fetch_days=self.fetch_days,
                skip_if_cache_fresh=skip_fresh,
            )
            return username.lower(), result['tweets'], result['user_info'], None
        except Exception as e:
            bt.logging.warning(f"Failed to fetch tweets for @{username}: {e}")
            return username.lower(), [], {'username': username.lower(), 'followers_count': 0}, str(e)
    
    def _check_relevance_safe(self, username: str, keywords: List[str], min_followers: int, lang: Optional[str] = None, min_tweets: int = 1, skip_if_cache_fresh: bool = False) -> Tuple[str, bool]:
        """
        Check user relevance with error handling.
        
        Args:
            username: Twitter username
            keywords: Keywords to check
            min_followers: Minimum follower threshold
            lang: Optional language filter
            min_tweets: Minimum number of tweets containing keywords
            skip_if_cache_fresh: If True, skip API call if cache was updated within freshness window
            
        Returns:
            Tuple of (username, is_relevant)
        """
        try:
            return username, self.twitter_client.check_user_relevance(
                username, keywords, min_followers, lang, min_tweets,
                skip_if_cache_fresh=skip_if_cache_fresh,
            )
        except Exception as e:
            bt.logging.warning(f"Relevance check failed for @{username}: {e}")
            return username, False
    
    def analyze_network(
        self,
        seed_accounts: List[str],
        keywords: List[str],
        min_followers: int = 0,
        lang: Optional[str] = None,
        min_tweets: int = 1,
        min_interaction_weight: float = 0,
        core_accounts: Optional[Set[str]] = None,
        use_personalized_pagerank: bool = False,
        skip_if_cache_fresh: Optional[bool] = None
    ) -> Tuple[Dict[str, float], np.ndarray, np.ndarray, List[str], Dict[str, Dict], int]:
        """
        Analyze Twitter network and return absolute influence scores and relationship matrices.

        Scores are calculated as: PageRank × (total_pool_followers / 1000)
        This gives "absolute influence" that can be compared across pools with different
        difficulty levels (pools with more followers = higher difficulty).
        The division by 1000 keeps scores at a reasonable scale for UIs.

        Args:
            seed_accounts: Initial accounts to analyze
            keywords: Keywords to filter accounts by
            min_followers: Minimum follower threshold
            lang: Optional language filter (e.g., 'en', 'zh')
            min_tweets: Minimum number of tweets containing keywords for relevance
            min_interaction_weight: Minimum total incoming interaction weight for quality filtering.
                                   Accounts with incoming weight below threshold are filtered out.
                                   Seed accounts are always preserved.
            core_accounts: Optional set of "core" accounts for personalized PageRank.
                          When provided with use_personalized_pagerank=True, the random walk
                          restart distribution is biased toward these accounts.
            use_personalized_pagerank: If True (and core_accounts provided), use personalized
                                      PageRank biased toward core accounts instead of standard PageRank.
            skip_if_cache_fresh: If provided, overrides the instance's skip_if_cache_fresh setting
                                for this analyze_network call only.
            
        Returns:
            Tuple of (scores_dict, adjacency_matrix, relationship_matrix, usernames_list, user_info_map, total_pool_followers)
            - scores_dict: Absolute influence scores (PageRank × pool_difficulty / 1000)
            - adjacency_matrix: Max interaction weights for influence (PageRank)
            - relationship_matrix: Cumulative weighted interactions for cabal protection
            - total_pool_followers: Pool difficulty metric (sum of all members' followers)
        """
        start_time = time.time()
        bt.logging.info(f"Analyzing network from {len(seed_accounts)} seed accounts")

        # Relax params when seed count is low to bootstrap discovery
        if len(seed_accounts) < 20:
            if min_interaction_weight > 1 or min_tweets > 1:
                bt.logging.info(
                    f"Low seed count ({len(seed_accounts)} < 20): relaxing "
                    f"min_interaction_weight {min_interaction_weight}→1, min_tweets {min_tweets}→1"
                )
                min_interaction_weight = 1
                min_tweets = 1

        # Determine if we should skip API calls based on cache freshness
        # Use parameter override if provided, otherwise use instance setting
        skip_if_fresh = skip_if_cache_fresh if skip_if_cache_fresh is not None else self.skip_if_cache_fresh
        if skip_if_fresh:
            bt.logging.info("Cache freshness check enabled (fresh if < 24h old)")

        # Step 1: Fetch tweets for seed accounts
        fetch_start = time.time()
        all_tweets = {}
        user_info_map = {}
        failed_accounts = []

        if self.max_workers > 1:
            # Concurrent execution
            bt.logging.info(f"Fetching tweets concurrently ({self.max_workers} workers)...")
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_username = {
                    executor.submit(self._fetch_tweets_safe, username, skip_if_fresh): username
                    for username in seed_accounts
                }

                for future in as_completed(future_to_username):
                    username, tweets, user_info, error = future.result()
                    if error:
                        failed_accounts.append((username, error))
                    all_tweets[username] = tweets
                    user_info_map[username] = user_info
        else:
            # Sequential execution
            for username in seed_accounts:
                username_lower = username.lower()
                result = self.twitter_client.fetch_user_tweets(
                    username_lower,
                    fetch_days=self.fetch_days,
                    skip_if_cache_fresh=skip_if_fresh,
                )
                all_tweets[username_lower] = result['tweets']
                user_info_map[username_lower] = result['user_info']
        
        fetch_time = time.time() - fetch_start
        total_tweets = sum(len(tweets) for tweets in all_tweets.values())
        bt.logging.info(f"Fetched {total_tweets} tweets from {len(all_tweets)} accounts in {fetch_time:.1f}s")

        if failed_accounts:
            bt.logging.warning(f"Failed to fetch {len(failed_accounts)} accounts: {[acc for acc, _ in failed_accounts]}")

        # Step 2: Filter tweets by age if max_data_age_days is specified
        if self.max_data_age_days:
            cutoff_date = datetime.now() - timedelta(days=self.max_data_age_days)
            filtered_tweets = {}
            total_before = sum(len(tweets) for tweets in all_tweets.values())

            for username, tweets in all_tweets.items():
                filtered = []
                for tweet in tweets:
                    try:
                        if tweet.get('created_at'):
                            tweet_date = datetime.strptime(tweet['created_at'], '%a %b %d %H:%M:%S %z %Y')
                            # Make cutoff timezone-aware
                            cutoff_with_tz = cutoff_date.replace(tzinfo=tweet_date.tzinfo)
                            if tweet_date >= cutoff_with_tz:
                                filtered.append(tweet)
                    except Exception as e:
                        bt.logging.debug(f"Error parsing tweet date for @{username}: {e}")
                        # Include tweet if date parsing fails (safer than excluding)
                        filtered.append(tweet)
                filtered_tweets[username] = filtered

            all_tweets = filtered_tweets
            total_after = sum(len(tweets) for tweets in all_tweets.values())
            bt.logging.info(
                f"Filtered tweets by age ({self.max_data_age_days} days max): "
                f"{total_after}/{total_before} tweets remain ({total_before - total_after} filtered out)"
            )

        # Step 3: Build interaction network
        interaction_weights = {}  # (from_user, to_user) -> max weight for influence (PageRank)
        relationship_scores = {}  # (from_user, to_user) -> sum of weighted interactions for cabal protection
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
                    # Skip invalid usernames (numeric IDs from suspended/deleted accounts)
                    if not is_valid_twitter_username(tagged_user):
                        continue
                    if tagged_user != from_user:
                        key = (from_user, tagged_user)
                        # For influence score: max weight
                        interaction_weights[key] = max(
                            interaction_weights.get(key, 0), 
                            self.tag_weight
                        )
                        # For relationship score: cumulative sum
                        relationship_scores[key] = relationship_scores.get(key, 0.0) + self.tag_weight
                        discovered_users.add(tagged_user)
                
                # Handle retweets
                if tweet.get('retweeted_user'):
                    retweeted_user = tweet['retweeted_user']
                    # Skip invalid usernames (numeric IDs from suspended/deleted accounts)
                    if not is_valid_twitter_username(retweeted_user):
                        continue
                    if retweeted_user != from_user:
                        key = (from_user, retweeted_user)
                        # For influence score: max weight
                        interaction_weights[key] = max(
                            interaction_weights.get(key, 0),
                            self.retweet_weight
                        )
                        # For relationship score: cumulative sum
                        relationship_scores[key] = relationship_scores.get(key, 0.0) + self.retweet_weight
                        discovered_users.add(retweeted_user)
                
                # Handle quotes
                if tweet.get('quoted_user'):
                    quoted_user = tweet['quoted_user']
                    # Skip invalid usernames (numeric IDs from suspended/deleted accounts)
                    if not is_valid_twitter_username(quoted_user):
                        continue
                    if quoted_user != from_user:
                        key = (from_user, quoted_user)
                        # For influence score: max weight
                        interaction_weights[key] = max(
                            interaction_weights.get(key, 0),
                            self.quote_weight
                        )
                        # For relationship score: cumulative sum
                        relationship_scores[key] = relationship_scores.get(key, 0.0) + self.quote_weight
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
                        executor.submit(self._check_relevance_safe, username, keywords, min_followers, lang, min_tweets, skip_if_fresh): username
                        for username in all_accounts_to_check
                    }

                    for future in as_completed(futures):
                        username, is_relevant = future.result()
                        if is_relevant:
                            relevant_users.add(username)
            else:
                # Sequential relevance checking
                for username in all_accounts_to_check:
                    if self.twitter_client.check_user_relevance(username, keywords, min_followers, lang, min_tweets, skip_if_cache_fresh=skip_if_fresh):
                        relevant_users.add(username)
            
            relevance_time = time.time() - relevance_start
            bt.logging.info(f"Relevance check completed in {relevance_time:.1f}s: {len(relevant_users)}/{len(all_accounts_to_check)} relevant")
            
            # Populate user_info_map from cache for relevant users
            # (relevance checking fetched their tweets, which cached their user info)
            users_without_info = relevant_users - set(user_info_map.keys())
            cached_count = 0
            for username in users_without_info:
                cached_tweets = get_cached_user_tweets(username)
                if cached_tweets and 'user_info' in cached_tweets:
                    user_info_map[username] = cached_tweets['user_info']
                    cached_count += 1
            
            if cached_count > 0:
                bt.logging.info(f"Populated user info from cache for {cached_count}/{len(users_without_info)} discovered accounts")
            
            # Filter interactions to only relevant users
            interaction_weights = {
                (from_user, to_user): weight 
                for (from_user, to_user), weight in interaction_weights.items()
                if from_user in relevant_users and to_user in relevant_users
            }
            relationship_scores = {
                (from_user, to_user): score
                for (from_user, to_user), score in relationship_scores.items()
                if from_user in relevant_users and to_user in relevant_users
            }
            
            all_users = relevant_users
        else:
            all_users = discovered_users | set(seed_accounts)
        
        # Step 4: Filter by minimum interaction weight (quality check)
        if min_interaction_weight > 0:
            # Calculate total incoming weight for each account
            incoming_weights = {}
            for (from_user, to_user), weight in interaction_weights.items():
                incoming_weights[to_user] = incoming_weights.get(to_user, 0) + weight
            
            # Filter to accounts meeting threshold
            accounts_before = len(all_users)
            qualified_accounts = {
                user for user, total_weight in incoming_weights.items()
                if total_weight >= min_interaction_weight
            }
            
            # Include seed accounts that may have outgoing but no incoming interactions
            # They are important network sources even without incoming weight
            qualified_accounts |= (set(seed_accounts) & all_users)
            
            # Re-filter interactions to only include edges between qualified accounts
            interaction_weights = {
                (from_user, to_user): weight
                for (from_user, to_user), weight in interaction_weights.items()
                if from_user in qualified_accounts and to_user in qualified_accounts
            }
            relationship_scores = {
                (from_user, to_user): score
                for (from_user, to_user), score in relationship_scores.items()
                if from_user in qualified_accounts and to_user in qualified_accounts
            }
            
            all_users = qualified_accounts
            bt.logging.info(
                f"Filtered by min_interaction_weight ({min_interaction_weight}): "
                f"{len(all_users)}/{accounts_before} accounts remain"
            )
        
        bt.logging.info(f"Network: {len(all_users)} users, {len(interaction_weights)} interactions")
        
        if not interaction_weights:
            raise ValueError("No interactions found in network")
        
        # Step 5: Calculate PageRank (using max interaction weights for influence scores)
        G = nx.DiGraph()
        for (from_user, to_user), weight in interaction_weights.items():
            G.add_edge(from_user, to_user, weight=weight)
        
        if use_personalized_pagerank and core_accounts:
            # Personalized PageRank biased toward core accounts
            personalization = {
                node: 1.0 if node in core_accounts else 0.0
                for node in G.nodes()
            }
            if sum(personalization.values()) > 0:
                bt.logging.info(f"Using personalized PageRank biased toward {int(sum(personalization.values()))} core accounts")
                pagerank_scores = nx.pagerank(
                    G, weight='weight', alpha=self.alpha,
                    personalization=personalization, max_iter=1000
                )
            else:
                bt.logging.warning("No core accounts found in graph, falling back to standard PageRank")
                pagerank_scores = nx.pagerank(G, weight='weight', alpha=self.alpha, max_iter=1000)
        else:
            pagerank_scores = nx.pagerank(G, weight='weight', alpha=self.alpha, max_iter=1000)
        
        # Step 5: Calculate pool difficulty and absolute influence scores
        # Pool difficulty = total followers across all pool members
        # This allows comparing influence across pools with different difficulty levels
        total_pool_followers = sum(
            user_info_map.get(user, {}).get('followers_count', 0)
            for user in all_users
        )
        
        # Normalize PageRank to sum to 1.0, then scale by pool difficulty
        total_score = sum(pagerank_scores.values())
        normalized_scores = {user: score / total_score for user, score in pagerank_scores.items()}
        
        # Multiply by pool difficulty (divided by 1000) to get absolute influence scores
        # Score represents "effective follower reach through network position"
        # Using sum(followers)/1000 to keep scores at a reasonable scale for UIs
        absolute_scores = {
            user: round(score * (total_pool_followers / 1000), 2)
            for user, score in normalized_scores.items()
        }
        
        scaled_pool_difficulty = total_pool_followers / 1000
        bt.logging.info(
            f"Pool difficulty: {total_pool_followers:,} total followers (scaled: {scaled_pool_difficulty:.2f}), "
            f"scores range: {min(absolute_scores.values()):.2f} - {max(absolute_scores.values()):.2f}"
        )
        
        # Step 6: Create adjacency matrices
        usernames_sorted = sorted(list(all_users))
        n = len(usernames_sorted)
        
        # Adjacency matrix: max interaction weights (for influence scores/PageRank)
        adjacency_matrix = np.zeros((n, n))
        # Relationship scores matrix: cumulative weighted interactions (for cabal protection)
        relationship_matrix = np.zeros((n, n))
        
        username_to_idx = {user: i for i, user in enumerate(usernames_sorted)}
        
        # Populate adjacency matrix with max weights
        for (from_user, to_user), weight in interaction_weights.items():
            if from_user in username_to_idx and to_user in username_to_idx:
                from_idx = username_to_idx[from_user]
                to_idx = username_to_idx[to_user]
                adjacency_matrix[from_idx, to_idx] = weight
        
        # Populate relationship scores matrix with cumulative scores
        for (from_user, to_user), score in relationship_scores.items():
            if from_user in username_to_idx and to_user in username_to_idx:
                from_idx = username_to_idx[from_user]
                to_idx = username_to_idx[to_user]
                relationship_matrix[from_idx, to_idx] = score
        
        bt.logging.info(f"PageRank complete: {len(absolute_scores)} accounts mapped")
        
        # Final performance summary
        total_elapsed = time.time() - start_time
        mode = "concurrent" if self.max_workers > 1 else "sequential"
        bt.logging.info(
            f"✅ Network analysis completed in {total_elapsed:.1f}s "
            f"({mode} mode with {self.max_workers} worker{'s' if self.max_workers > 1 else ''})"
        )
        
        return absolute_scores, adjacency_matrix, relationship_matrix, usernames_sorted, user_info_map, total_pool_followers


def should_run_discovery_today(date_offset: int = 0, reference_date: date = DISCOVERY_REFERENCE_DATE) -> bool:
    """
    Check if discovery should run today based on the date offset.
    
    Args:
        date_offset: Number of days offset from reference date (0-13 for bi-weekly cycle)
        reference_date: Reference date for calculating cycles
        
    Returns:
        True if discovery should run today, False otherwise
    """
    from datetime import timezone
    
    today = datetime.now(timezone.utc).date()
    days_since_reference = (today - reference_date).days
    
    # Apply offset and check if it's a discovery day (every 14 days)
    adjusted_days = days_since_reference - date_offset
    return adjusted_days >= 0 and adjusted_days % 14 == 0


async def run_discovery_for_stale_pools() -> Dict[str, str]:
    """
    Run social discovery for pools that are scheduled to update today.
    
    Each pool can have a 'date_offset' (0-13) that determines which day in the 
    14-day cycle it runs. This allows distributing discovery across different days.
    
    Checks each active pool's:
    - date_offset configuration to see if today is its scheduled day
    - latest social map timestamp to avoid redundant runs
    
    Always forces cache refresh to ensure fresh Twitter data for bi-weekly discovery.
    
    Returns:
        Dict mapping pool_name to social_map_path for pools that ran
    """
    from datetime import timezone
    
    now = datetime.now(timezone.utc)
    today = now.date()
    
    pool_manager = PoolManager()
    results = {}
    pools_scheduled_today = []
    
    for pool_name, config in pool_manager.pools.items():
        if not config.get('active', True):
            continue
        
        # Check if this pool is scheduled to run today
        date_offset = config.get('date_offset', 0)
        if not should_run_discovery_today(date_offset, DISCOVERY_REFERENCE_DATE):
            continue
        
        pools_scheduled_today.append(pool_name)
        
        # Check if this specific pool needs update (hasn't run today)
        social_maps_dir = Path(__file__).parent / "social_maps" / pool_name
        needs_update = False
        
        if not social_maps_dir.exists():
            needs_update = True
        else:
            social_map_files = [
                f for f in social_maps_dir.glob("*.json")
                if not f.name.endswith(('_adjacency.json', '_metadata.json'))
                and not f.name.startswith(('recursive_summary_', 'two_stage_summary_'))
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
                from .recursive_discovery import two_stage_discovery
                
                bt.logging.info(
                    f"Running two-stage discovery for {pool_name} "
                    f"(offset={date_offset}, no map from today)"
                )
                social_map_path, _ = await two_stage_discovery(
                    pool_name=pool_name,
                    posts_only=True,
                )
                results[pool_name] = social_map_path
            except Exception as e:
                bt.logging.error(f"Discovery failed for {pool_name}: {e}")
        else:
            bt.logging.debug(f"Pool {pool_name} already has map from today, skipping")
    
    if pools_scheduled_today:
        bt.logging.info(
            f"🔄 Bi-weekly discovery check: {len(pools_scheduled_today)} pool(s) scheduled today: "
            f"{', '.join(pools_scheduled_today)}"
        )
    else:
        bt.logging.debug("No pools scheduled for discovery today")
    
    return results


# NOTE: CLI entry point moved to __main__.py
# Use: python -m bitcast.validator.social_discovery --pool-name tao
# (Running this file directly causes a RuntimeWarning due to module naming conflict)