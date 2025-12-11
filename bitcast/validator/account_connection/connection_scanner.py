"""
Connection scanner for monitoring pool member tweets for connection tags.

Scans active members from social discovery pools and extracts connection tags
from their recent tweets.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any
import bittensor as bt

from bitcast.validator.social_discovery.pool_manager import PoolManager
from bitcast.validator.clients.twitter_client import TwitterClient
from bitcast.validator.tweet_scoring.social_map_loader import parse_social_map_filename
from bitcast.validator.utils.config import ENABLE_DATA_PUBLISH, WALLET_NAME, HOTKEY_NAME
from bitcast.validator.utils.data_publisher import initialize_global_publisher
from .connection_db import ConnectionDatabase
from .tag_parser import TagParser
from .connection_publisher import publish_account_connections


def get_active_pool_members(pool_name: str, scan_all: bool = False) -> List[str]:
    """
    Load latest social map for pool and return member usernames sorted by score.
    
    Args:
        pool_name: Name of the pool (e.g., "tao")
        scan_all: Deprecated - kept for backward compatibility but has no effect
        
    Returns:
        List of all account usernames (lowercase), sorted by score descending
        
    Raises:
        ValueError: If pool not found or no social map exists
    """
    pool_name = pool_name.lower()
    
    # Verify pool exists
    pool_manager = PoolManager()
    pool_config = pool_manager.get_pool(pool_name)
    
    if not pool_config:
        available_pools = pool_manager.get_pools()
        raise ValueError(
            f"Pool '{pool_name}' not found. Available pools: {', '.join(available_pools)}"
        )
    
    # Find latest social map file
    # Social maps are stored in: bitcast/validator/social_discovery/social_maps/{pool_name}/
    social_maps_dir = Path(__file__).parents[1] / "social_discovery" / "social_maps" / pool_name
    
    if not social_maps_dir.exists():
        raise ValueError(
            f"No social maps directory found for pool '{pool_name}'. "
            f"Run social_discovery first to generate pool data."
        )
    
    # Get all social map files (exclude adjacency and metadata files)
    social_map_files = [
        f for f in social_maps_dir.glob("*.json")
        if not f.name.endswith('_adjacency.json') 
        and not f.name.endswith('_metadata.json')
        and not f.name.startswith('recursive_summary_')
    ]
    
    if not social_map_files:
        raise ValueError(
            f"No social map files found for pool '{pool_name}'. "
            f"Run social_discovery first to generate pool data."
        )
    
    # Get latest file by filename timestamp
    latest_file = max(
        social_map_files,
        key=lambda f: parse_social_map_filename(f.name) or datetime.min.replace(tzinfo=timezone.utc)
    )
    
    bt.logging.info(f"Loading social map from: {latest_file.name}")
    
    # Load and parse social map
    try:
        with open(latest_file, 'r') as f:
            social_map_data = json.load(f)
    except Exception as e:
        raise ValueError(f"Failed to load social map file: {e}")
    
    # Extract members based on scan_all flag
    if 'accounts' not in social_map_data:
        raise ValueError(f"Invalid social map format: missing 'accounts' field")
    
    # Get all accounts with scores
    account_scores = [
        (username.lower(), account_data.get('score', 0.0))
        for username, account_data in social_map_data['accounts'].items()
    ]
    
    # Sort by score descending
    account_scores.sort(key=lambda x: x[1], reverse=True)
    
    # Return all accounts sorted by score
    members = [username for username, _ in account_scores]
    
    bt.logging.info(
        f"Found {len(members)} accounts in pool '{pool_name}' (sorted by score)"
    )
    
    return members


class ConnectionScanner:
    """
    Scans pool member tweets for connection tags.
    
    Fetches recent tweets from active pool members and extracts connection tags,
    storing them in the database.
    """
    
    def __init__(self, lookback_days: int = 7, db_path: Optional[Path] = None, force_refresh: bool = False, scan_all: bool = False):
        """
        Initialize scanner.
        
        Args:
            lookback_days: Number of days to look back for tweets (default: 7)
            db_path: Optional custom database path (for testing)
            force_refresh: If True, bypass cache and always fetch fresh tweets (default: False)
            scan_all: If True, scan all accounts in social map regardless of status (default: False)
        """
        self.lookback_days = lookback_days
        self.force_refresh = force_refresh
        self.scan_all = scan_all
        self.twitter_client = TwitterClient(posts_only=False)
        self.database = ConnectionDatabase(db_path=db_path)
        self.tag_parser = TagParser()
        
        cache_mode = "force refresh (no cache)" if force_refresh else "with caching"
        scan_mode = "all accounts" if scan_all else "active members only"
        bt.logging.info(
            f"ConnectionScanner initialized with {self.lookback_days} day lookback ({cache_mode}, {scan_mode})"
        )
    
    def filter_recent_tweets(self, tweets: List[Dict]) -> List[Dict]:
        """
        Filter tweets to only those within lookback_days.
        
        Args:
            tweets: List of tweet dictionaries
            
        Returns:
            Filtered list of recent tweets
        """
        if not tweets:
            return []
        
        # Calculate cutoff date (UTC)
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        
        recent_tweets = []
        for tweet in tweets:
            # Parse tweet timestamp (assume UTC)
            created_at = tweet.get('created_at')
            if not created_at:
                continue
            
            try:
                # Handle various datetime formats - all converted to UTC
                if isinstance(created_at, str):
                    # Try ISO format first
                    try:
                        tweet_date = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                        tweet_date = tweet_date.astimezone(timezone.utc)
                    except ValueError:
                        # Try Twitter API format: "Wed Oct 08 15:37:01 +0000 2025"
                        try:
                            tweet_date = datetime.strptime(created_at, '%a %b %d %H:%M:%S %z %Y')
                            tweet_date = tweet_date.astimezone(timezone.utc)
                        except ValueError:
                            # Skip if we can't parse the date
                            continue
                elif isinstance(created_at, datetime):
                    tweet_date = created_at
                    if tweet_date.tzinfo is None:
                        tweet_date = tweet_date.replace(tzinfo=timezone.utc)
                    else:
                        tweet_date = tweet_date.astimezone(timezone.utc)
                else:
                    continue
                
                # Check if within lookback period
                if tweet_date >= cutoff_date:
                    recent_tweets.append(tweet)
                    
            except Exception as e:
                bt.logging.debug(f"Error parsing tweet date: {e}")
                continue
        
        return recent_tweets
    
    def scan_account(self, username: str) -> List[tuple[int, str, str]]:
        """
        Scan a single account's recent tweets for connection tags.
        
        Args:
            username: Twitter username to scan
            
        Returns:
            List of tuples: [(tweet_id, tag_type, full_tag), ...]
        """
        username = username.lower()
        
        try:
            # Fetch tweets using TwitterClient (uses MAX_TWEETS_PER_FETCH from config)
            result = self.twitter_client.fetch_user_tweets(username, force_refresh=self.force_refresh)
            tweets = result.get('tweets', [])
            
            if not tweets:
                bt.logging.debug(f"No tweets found for @{username}")
                return []
            
            # Filter to recent tweets
            recent_tweets = self.filter_recent_tweets(tweets)
            
            if not recent_tweets:
                bt.logging.debug(
                    f"No recent tweets (within {self.lookback_days} days) for @{username}"
                )
                return []
            
            bt.logging.debug(
                f"Scanning {len(recent_tweets)} recent tweets from @{username} "
                f"(out of {len(tweets)} total)"
            )
            
            # Extract tags from recent tweets (excluding retweets)
            found_tags = []
            for tweet in recent_tweets:
                tweet_id = tweet.get('tweet_id')
                text = tweet.get('text', '')
                
                if not tweet_id or not text:
                    continue
                
                # Skip retweets - we only want original content
                if tweet.get('retweeted_user'):
                    continue
                
                # Defensive validation: Skip tweets from other authors
                tweet_author = tweet.get('author')
                if not tweet_author or tweet_author.lower() != username:
                    continue
                
                # Parse tags from tweet text
                tags = self.tag_parser.extract_tags(text)
                
                for tag_type, full_tag in tags:
                    found_tags.append((tweet_id, tag_type, full_tag))
                    bt.logging.debug(f"Found tag in tweet {tweet_id}: {full_tag}")
            
            if found_tags:
                bt.logging.info(
                    f"@{username}: Found {len(found_tags)} tag(s) in {len(recent_tweets)} recent tweets"
                )
            
            return found_tags
            
        except Exception as e:
            bt.logging.warning(f"Error scanning @{username}: {e}")
            return []
    
    def store_connection(self, pool_name: str, tweet_id: int, tag: str, account_username: str) -> bool:
        """
        Store connection in database.
        
        Args:
            pool_name: Name of the pool
            tweet_id: ID of the tweet containing the tag
            tag: The connection tag
            account_username: Twitter username
            
        Returns:
            True if new connection was inserted, False if existing was updated
        """
        try:
            is_new = self.database.upsert_connection(
                pool_name=pool_name,
                tweet_id=tweet_id,
                tag=tag,
                account_username=account_username
            )
            return is_new
        except Exception as e:
            bt.logging.error(f"Error storing connection: {e}")
            return False
    
    async def scan_pool(self, pool_name: str, publish: bool = True) -> Dict[str, Any]:
        """
        Scan pool members for connection tags.
        
        Args:
            pool_name: Name of the pool to scan
            publish: Whether to publish connections after scanning (default: True)
        
        Returns:
            Summary dictionary with statistics:
            - accounts_checked: Number of accounts scanned
            - tweets_scanned: Total tweets examined
            - tags_found: Total tags discovered
            - new_connections: New connections stored
            - duplicates_skipped: Existing connections updated
            - errors: Number of errors encountered
            - processing_time: Time taken in seconds
        """
        pool_name = pool_name.lower()
        start_time = datetime.now(timezone.utc)
        
        bt.logging.info(f"Starting connection scan for pool: {pool_name}")
        
        # Get pool members (active or all, based on scan_all flag)
        try:
            pool_members = get_active_pool_members(pool_name, scan_all=self.scan_all)
        except ValueError as e:
            bt.logging.error(f"Failed to get pool members: {e}")
            raise
        
        if not pool_members:
            bt.logging.warning(f"No members found in pool '{pool_name}'")
            return {
                'accounts_checked': 0,
                'tweets_scanned': 0,
                'tags_found': 0,
                'new_connections': 0,
                'duplicates_skipped': 0,
                'errors': 0,
                'processing_time': 0.0
            }
        
        # Scan each account
        stats = {
            'accounts_checked': 0,
            'tweets_scanned': 0,
            'tags_found': 0,
            'new_connections': 0,
            'duplicates_skipped': 0,
            'errors': 0,
            'connections_data': []  # Store connection data for publishing
        }
        
        for username in pool_members:
            try:
                # Scan account for tags
                found_tags = self.scan_account(username)
                stats['accounts_checked'] += 1
                stats['tags_found'] += len(found_tags)
                
                # Store each found tag
                for tweet_id, tag_type, full_tag in found_tags:
                    is_new = self.store_connection(pool_name, tweet_id, full_tag, username)
                    if is_new:
                        stats['new_connections'] += 1
                    else:
                        stats['duplicates_skipped'] += 1
                    
                    # Collect connection data for publishing
                    stats['connections_data'].append({
                        'tweet_id': tweet_id,
                        'tag': full_tag,
                        'username': username
                    })
                
            except Exception as e:
                bt.logging.error(f"Error processing @{username}: {e}")
                stats['errors'] += 1
                continue
        
        # Calculate processing time
        end_time = datetime.now(timezone.utc)
        processing_time = (end_time - start_time).total_seconds()
        stats['processing_time'] = processing_time
        
        bt.logging.info(
            f"Scan complete: {stats['accounts_checked']} accounts, "
            f"{stats['tags_found']} tags found, "
            f"{stats['new_connections']} new connections, "
            f"{stats['errors']} errors"
        )
        
        # Publish account connections if enabled and publish flag is True (fire-and-forget pattern)
        if publish and ENABLE_DATA_PUBLISH and stats['connections_data']:
            try:
                # Generate run_id
                run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                
                connections_to_publish = stats['connections_data']
                
                success = await publish_account_connections(
                    connections=connections_to_publish,
                    run_id=run_id
                )
                if success:
                    bt.logging.info(
                        f"🚀 Account connections published successfully for pool {pool_name}"
                    )
                else:
                    bt.logging.warning(
                        f"⚠️ Account connections publishing failed for pool {pool_name} "
                        f"(local results saved)"
                    )
            except RuntimeError as e:
                # No global publisher initialized - log but don't fail
                bt.logging.debug(
                    f"📴 Account connections publishing skipped - no global publisher: {e}"
                )
            except Exception as e:
                # Log but don't fail scanning (fire-and-forget pattern)
                bt.logging.warning(
                    f"⚠️ Account connections publishing failed: {e} (local results saved)"
                )
        elif publish and ENABLE_DATA_PUBLISH and not stats['connections_data']:
            bt.logging.debug(
                f"📴 No connections to publish for pool {pool_name}"
            )
        elif publish and not ENABLE_DATA_PUBLISH:
            bt.logging.debug("📴 Account connections publishing disabled by config")
        
        # Remove connections_data before returning stats
        stats.pop('connections_data', None)
        
        return stats
    
    async def scan_all_pools(self) -> Dict[str, Any]:
        """
        Scan all available pools and publish all connections together.
        
        Returns:
            Summary dictionary with aggregated statistics:
            - pools_scanned: Number of pools scanned
            - accounts_checked: Total accounts scanned across all pools
            - tags_found: Total tags discovered
            - new_connections: Total new connections stored
            - duplicates_skipped: Total existing connections updated
            - errors: Number of errors encountered
            - processing_time: Total time taken in seconds
        """
        start_time = datetime.now(timezone.utc)
        
        # Get all available pools
        pool_manager = PoolManager()
        available_pools = pool_manager.get_pools()
        
        bt.logging.info(f"Scanning {len(available_pools)} pools: {available_pools}")
        
        # Aggregate stats
        total_stats = {
            'pools_scanned': 0,
            'accounts_checked': 0,
            'tags_found': 0,
            'new_connections': 0,
            'duplicates_skipped': 0,
            'errors': 0,
            'connections_data': []
        }
        
        for pool_name in available_pools:
            bt.logging.info(f"━━━ Scanning pool: {pool_name} ━━━")
            try:
                # Don't publish per-pool, we'll publish all together at the end
                pool_stats = await self.scan_pool(pool_name, publish=False)
                
                # Aggregate
                total_stats['pools_scanned'] += 1
                total_stats['accounts_checked'] += pool_stats['accounts_checked']
                total_stats['tags_found'] += pool_stats['tags_found']
                total_stats['new_connections'] += pool_stats['new_connections']
                total_stats['duplicates_skipped'] += pool_stats['duplicates_skipped']
                total_stats['errors'] += pool_stats['errors']
                
                bt.logging.info(
                    f"Pool '{pool_name}' complete: {pool_stats['accounts_checked']} accounts, "
                    f"{pool_stats['tags_found']} tags, {pool_stats['new_connections']} new"
                )
                
            except Exception as e:
                bt.logging.error(f"Error scanning pool {pool_name}: {e}")
                total_stats['errors'] += 1
        
        # Calculate total time
        end_time = datetime.now(timezone.utc)
        total_stats['processing_time'] = (end_time - start_time).total_seconds()
        
        # Get all connections from database for publishing
        if ENABLE_DATA_PUBLISH:
            try:
                # Get all connections from database (across all pools)
                all_db_connections = self.database.get_all_connections()
                
                # Format for publishing (no pool_name in individual connections)
                connections_to_publish = [
                    {
                        'tweet_id': conn['tweet_id'],
                        'tag': conn['tag'],
                        'username': conn['account_username']
                    }
                    for conn in all_db_connections
                ]
                
                if connections_to_publish:
                    # Generate run_id
                    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                    
                    success = await publish_account_connections(
                        connections=connections_to_publish,
                        run_id=run_id
                    )
                    if success:
                        bt.logging.info(
                            f"🚀 Published {len(connections_to_publish)} connections from all pools"
                        )
                    else:
                        bt.logging.warning(
                            f"⚠️ Account connections publishing failed (local results saved)"
                        )
                else:
                    bt.logging.debug("📴 No connections to publish")
                    
            except RuntimeError as e:
                bt.logging.debug(
                    f"📴 Account connections publishing skipped - no global publisher: {e}"
                )
            except Exception as e:
                bt.logging.warning(
                    f"⚠️ Account connections publishing failed: {e} (local results saved)"
                )
        else:
            bt.logging.debug("📴 Account connections publishing disabled by config")
        
        bt.logging.info(
            f"All pools scan complete: {total_stats['pools_scanned']} pools, "
            f"{total_stats['accounts_checked']} accounts, "
            f"{total_stats['tags_found']} tags found, "
            f"{total_stats['new_connections']} new connections"
        )
        
        return total_stats


if __name__ == "__main__":
    """Standalone account connection scanner."""
    import argparse
    import os
    from dotenv import load_dotenv
    
    try:
        # Create argument parser
        parser = argparse.ArgumentParser(
            description="Scan pool member tweets for connection tags"
        )
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)
        bt.subtensor.add_args(parser)
        
        parser.add_argument(
            "--pool-name",
            type=str,
            default="all",
            help="Name of the pool to scan, or 'all' for all pools (default: all)"
        )
        
        parser.add_argument(
            "--lookback-days",
            type=int,
            default=7,
            help="Number of days to look back for tweets (default: 7)"
        )
        
        parser.add_argument(
            "--force-refresh",
            action="store_true",
            help="Bypass cache and always fetch fresh tweets from API"
        )
        
        parser.add_argument(
            "--scan-all-accounts",
            action="store_true",
            help="Deprecated flag (kept for compatibility) - all accounts are now scanned by default"
        )
        
        # Build args list from environment variables for wallet config
        # Start with actual command line args, then add wallet config if not provided
        import sys
        args_list = sys.argv[1:]  # Get actual command-line arguments
        
        # Add default logging if not specified
        if not any('--logging' in arg for arg in args_list):
            args_list.insert(0, '--logging.debug')
        
        # Add wallet config from environment if not provided on command line
        if WALLET_NAME and not any('--wallet.name' in arg for arg in args_list):
            args_list.extend(['--wallet.name', WALLET_NAME])
        if HOTKEY_NAME and not any('--wallet.hotkey' in arg for arg in args_list):
            args_list.extend(['--wallet.hotkey', HOTKEY_NAME])
        
        # Parse configuration with merged args
        config = bt.config(parser, args=args_list)
        bt.logging.set_config(config=config.logging)
        
        # Initialize environment
        env_path = Path(__file__).parents[1] / '.env'
        if env_path.exists():
            load_dotenv(dotenv_path=env_path)
            bt.logging.info(f"Loaded environment variables from {env_path}")
        
        # Initialize global publisher with properly configured wallet (for publishing)
        if ENABLE_DATA_PUBLISH:
            wallet = bt.wallet(config=config)
            initialize_global_publisher(wallet)
            bt.logging.info("🌐 Global publisher initialized for standalone mode")
        
        # Run scanner
        scanner = ConnectionScanner(
            lookback_days=config.lookback_days,
            force_refresh=config.force_refresh,
            scan_all=config.scan_all_accounts
        )
        
        if config.pool_name.lower() == "all":
            bt.logging.info("Starting connection scan for ALL pools")
            summary = asyncio.run(scanner.scan_all_pools())
        else:
            bt.logging.info(f"Starting connection scan for pool: {config.pool_name}")
            summary = asyncio.run(scanner.scan_pool(config.pool_name))
        
        # Display results
        print("\n" + "="*60)
        print("Account Connection Scan Complete")
        print("="*60)
        if 'pools_scanned' in summary:
            print(f"Pools scanned: {summary['pools_scanned']}")
        else:
            print(f"Pool: {config.pool_name}")
        print(f"Accounts checked: {summary['accounts_checked']}")
        print(f"Tags found: {summary['tags_found']}")
        print(f"New connections: {summary['new_connections']}")
        print(f"Duplicates skipped: {summary['duplicates_skipped']}")
        print(f"Errors: {summary['errors']}")
        print(f"Processing time: {summary['processing_time']:.2f}s")
        print("="*60 + "\n")
        
        if summary['errors'] > 0:
            bt.logging.warning(f"⚠️  Completed with {summary['errors']} error(s)")
        else:
            bt.logging.info("✅ Scan completed successfully!")
        
    except Exception as e:
        bt.logging.error(f"❌ Connection scan failed: {e}")
        import traceback
        bt.logging.debug(traceback.format_exc())
        exit(1)

