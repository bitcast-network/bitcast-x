"""
Connection scanner for finding connection tags via search API.

Searches for tweets containing the connection search tag (e.g. '@bitcast_network'),
cross-references authors against social map accounts, and extracts connection tags.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any, Set
import bittensor as bt

from bitcast.validator.social_discovery.pool_manager import PoolManager
from bitcast.validator.clients.twitter_client import TwitterClient
from bitcast.validator.tweet_scoring.social_map_loader import load_latest_social_map
from bitcast.validator.utils.config import (
    ENABLE_DATA_PUBLISH, WALLET_NAME, HOTKEY_NAME, CONNECTION_SEARCH_TAG
)
from .connection_db import ConnectionDatabase
from .tag_parser import TagParser
from .connection_publisher import publish_account_connections


def get_social_map_accounts(pool_name: str) -> Set[str]:
    """
    Load latest social map for pool and return all account usernames.
    
    Args:
        pool_name: Name of the pool (e.g., "tao")
        
    Returns:
        Set of account usernames (lowercase)
        
    Raises:
        ValueError: If pool not found or no social map exists
    """
    try:
        social_map, _ = load_latest_social_map(pool_name)
    except FileNotFoundError as e:
        raise ValueError(str(e))
    
    accounts = {username.lower() for username in social_map.get('accounts', {})}
    bt.logging.info(f"Found {len(accounts)} accounts in pool '{pool_name}'")
    
    return accounts


class ConnectionScanner:
    """
    Scans for connection tags using search API.
    
    Searches for tweets containing the connection search tag (e.g. '@bitcast_network'),
    cross-references authors against the social map, and extracts connection tags.
    """
    
    def __init__(self, lookback_days: int = 7, db_path: Optional[Path] = None,
                 twitter_client: Optional[TwitterClient] = None):
        """
        Args:
            lookback_days: Number of days to look back for tweets (default: 7)
            db_path: Optional custom database path (for testing)
            twitter_client: Optional TwitterClient instance (for testing, creates default if None)
        """
        self.lookback_days = lookback_days
        self.twitter_client = twitter_client or TwitterClient()
        self.database = ConnectionDatabase(db_path=db_path)
        self.tag_parser = TagParser()
        self.search_tag = CONNECTION_SEARCH_TAG
        
        bt.logging.info(
            f"ConnectionScanner initialized: search_tag='{self.search_tag}', "
            f"lookback={self.lookback_days} days"
        )
    
    def _build_query(self) -> str:
        """Build search query with date range."""
        since_date = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        return f"{self.search_tag} since:{since_date.strftime('%Y-%m-%d')}"
    
    def _extract_connections_from_tweets(
        self,
        tweets: List[Dict],
        pool_accounts: Set[str]
    ) -> List[Dict]:
        """
        Extract connection tags from tweets by social map accounts.
        
        Args:
            tweets: List of tweet dicts from search API
            pool_accounts: Set of lowercase usernames in the social map
            
        Returns:
            List of connection dicts with keys: tweet_id, username, tag_type, tag
        """
        connections = []
        
        for tweet in tweets:
            author = tweet.get('author', '').lower()
            tweet_id = tweet.get('tweet_id')
            text = tweet.get('text', '')
            
            if not author or not tweet_id or not text:
                continue
            
            # Skip if author is not in the social map
            if author not in pool_accounts:
                continue
            
            # Skip retweets
            if tweet.get('retweeted_user'):
                continue
            
            # Extract tags
            tags = self.tag_parser.extract_tags(text)
            
            for tag_type, full_tag in tags:
                connections.append({
                    'tweet_id': tweet_id,
                    'username': author,
                    'tag_type': tag_type,
                    'tag': full_tag,
                })
        
        return connections
    
    async def scan_pool(self, pool_name: str, publish: bool = True) -> Dict[str, Any]:
        """
        Scan for connection tags in a pool using search API.
        
        Args:
            pool_name: Name of the pool to scan
            publish: Whether to publish connections after scanning
        
        Returns:
            Summary dict with statistics
        """
        pool_name = pool_name.lower()
        start_time = datetime.now(timezone.utc)
        
        bt.logging.info(f"Starting connection scan for pool: {pool_name}")
        
        # Load social map accounts for cross-referencing
        try:
            pool_accounts = get_social_map_accounts(pool_name)
        except ValueError as e:
            bt.logging.error(f"Failed to get pool accounts: {e}")
            raise
        
        # Search for connection tweets
        query = self._build_query()
        bt.logging.info(f"Searching for connection tweets: '{query}'")
        
        result = self.twitter_client.search_tweets(
            query=query,
            max_results=500,
            sort="latest"
        )
        
        all_tweets = []
        if result['api_succeeded']:
            all_tweets = [t for t in result['tweets'] if t.get('tweet_id')]
        else:
            bt.logging.warning("Search API failed for connection scan")
        
        bt.logging.info(f"Search returned {len(all_tweets)} tweets")
        
        # Extract connections from tweets by social map accounts
        found_connections = self._extract_connections_from_tweets(all_tweets, pool_accounts)
        
        bt.logging.info(
            f"Found {len(found_connections)} connection tags from "
            f"{len({c['username'] for c in found_connections})} accounts"
        )
        
        # Store connections
        stats = {
            'accounts_checked': len(pool_accounts),
            'tweets_scanned': len(all_tweets),
            'tags_found': len(found_connections),
            'new_connections': 0,
            'duplicates_skipped': 0,
            'errors': 0,
            'connections_data': [],
        }
        
        for conn in found_connections:
            try:
                is_new = self.database.upsert_connection(
                    pool_name=pool_name,
                    tweet_id=conn['tweet_id'],
                    tag=conn['tag'],
                    account_username=conn['username']
                )
                if is_new:
                    stats['new_connections'] += 1
                else:
                    stats['duplicates_skipped'] += 1
                
                stats['connections_data'].append({
                    'tweet_id': conn['tweet_id'],
                    'tag': conn['tag'],
                    'username': conn['username'],
                })
            except Exception as e:
                bt.logging.error(f"Error storing connection: {e}")
                stats['errors'] += 1
        
        stats['processing_time'] = (datetime.now(timezone.utc) - start_time).total_seconds()
        
        bt.logging.info(
            f"Scan complete: {stats['tweets_scanned']} tweets searched, "
            f"{stats['tags_found']} tags found, "
            f"{stats['new_connections']} new connections"
        )
        
        # Publish if enabled
        if publish and ENABLE_DATA_PUBLISH and stats['connections_data']:
            await self._publish_connections(stats['connections_data'])
        
        stats.pop('connections_data', None)
        return stats
    
    async def scan_all_pools(self) -> Dict[str, Any]:
        """
        Scan all available pools for connection tags.
        
        Returns:
            Summary dict with aggregated statistics
        """
        start_time = datetime.now(timezone.utc)
        
        pool_manager = PoolManager()
        available_pools = pool_manager.get_pools()
        
        bt.logging.info(f"Scanning {len(available_pools)} pools: {available_pools}")
        
        total_stats = {
            'pools_scanned': 0,
            'accounts_checked': 0,
            'tags_found': 0,
            'new_connections': 0,
            'duplicates_skipped': 0,
            'errors': 0,
        }
        
        for pool_name in available_pools:
            try:
                pool_stats = await self.scan_pool(pool_name, publish=False)
                
                total_stats['pools_scanned'] += 1
                total_stats['accounts_checked'] += pool_stats['accounts_checked']
                total_stats['tags_found'] += pool_stats['tags_found']
                total_stats['new_connections'] += pool_stats['new_connections']
                total_stats['duplicates_skipped'] += pool_stats['duplicates_skipped']
                total_stats['errors'] += pool_stats['errors']
                
                bt.logging.info(
                    f"Pool '{pool_name}': {pool_stats['tags_found']} tags, "
                    f"{pool_stats['new_connections']} new"
                )
            except Exception as e:
                bt.logging.error(f"Error scanning pool {pool_name}: {e}")
                total_stats['errors'] += 1
        
        total_stats['processing_time'] = (datetime.now(timezone.utc) - start_time).total_seconds()
        
        # Publish all connections from database
        if ENABLE_DATA_PUBLISH:
            all_db_connections = self.database.get_all_connections()
            connections_to_publish = [
                {
                    'tweet_id': conn['tweet_id'],
                    'tag': conn['tag'],
                    'username': conn['account_username'],
                }
                for conn in all_db_connections
            ]
            if connections_to_publish:
                await self._publish_connections(connections_to_publish)
        
        bt.logging.info(
            f"All pools scan complete: {total_stats['pools_scanned']} pools, "
            f"{total_stats['tags_found']} tags, {total_stats['new_connections']} new"
        )
        
        return total_stats
    
    async def _publish_connections(self, connections: List[Dict]) -> None:
        """Publish connections to data API."""
        try:
            run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            success = await publish_account_connections(
                connections=connections,
                run_id=run_id
            )
            if success:
                bt.logging.info(f"Published {len(connections)} connections")
            else:
                bt.logging.warning("Connection publishing failed")
        except RuntimeError as e:
            bt.logging.debug(f"Publishing skipped - no global publisher: {e}")
        except Exception as e:
            bt.logging.warning(f"Publishing failed: {e}")


if __name__ == "__main__":
    """Standalone account connection scanner."""
    import argparse
    import os
    from dotenv import load_dotenv
    
    try:
        parser = argparse.ArgumentParser(
            description="Scan for connection tags using search API"
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
        
        import sys
        args_list = sys.argv[1:]
        
        if not any('--logging' in arg for arg in args_list):
            args_list.insert(0, '--logging.debug')
        
        if WALLET_NAME and not any('--wallet.name' in arg for arg in args_list):
            args_list.extend(['--wallet.name', WALLET_NAME])
        if HOTKEY_NAME and not any('--wallet.hotkey' in arg for arg in args_list):
            args_list.extend(['--wallet.hotkey', HOTKEY_NAME])
        
        config = bt.config(parser, args=args_list)
        bt.logging.set_config(config=config.logging)
        
        env_path = Path(__file__).parents[1] / '.env'
        if env_path.exists():
            load_dotenv(dotenv_path=env_path)
            bt.logging.info(f"Loaded environment variables from {env_path}")
        
        if ENABLE_DATA_PUBLISH:
            from bitcast.validator.utils.data_publisher import initialize_global_publisher
            wallet = bt.wallet(config=config)
            initialize_global_publisher(wallet)
        
        scanner = ConnectionScanner(lookback_days=config.lookback_days)
        
        if config.pool_name.lower() == "all":
            bt.logging.info("Starting connection scan for ALL pools")
            summary = asyncio.run(scanner.scan_all_pools())
        else:
            bt.logging.info(f"Starting connection scan for pool: {config.pool_name}")
            summary = asyncio.run(scanner.scan_pool(config.pool_name))
        
        print(f"\n{'='*60}")
        print("Account Connection Scan Complete")
        print(f"{'='*60}")
        if 'pools_scanned' in summary:
            print(f"Pools scanned: {summary['pools_scanned']}")
        else:
            print(f"Pool: {config.pool_name}")
        print(f"Tweets searched: {summary.get('tweets_scanned', 'N/A')}")
        print(f"Tags found: {summary['tags_found']}")
        print(f"New connections: {summary['new_connections']}")
        print(f"Duplicates skipped: {summary['duplicates_skipped']}")
        print(f"Errors: {summary['errors']}")
        print(f"Processing time: {summary['processing_time']:.2f}s")
        print(f"{'='*60}\n")
        
    except Exception as e:
        bt.logging.error(f"Connection scan failed: {e}")
        import traceback
        bt.logging.debug(traceback.format_exc())
        exit(1)
