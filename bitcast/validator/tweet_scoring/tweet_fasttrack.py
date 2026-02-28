"""
Tweet fasttrack module for manually injecting tweets into the pipeline.

Fetches a single tweet by ID from the desearch API and stores it in the
TweetStore. Automatically processes connection tags if the tweet contains
them and the author is in any pool's social map.

Idempotent: re-fasttracking a tweet already in the store simply refreshes
engagement stats. Re-processing connections is safe due to the UNIQUE
constraint on (pool_name, account_username, tag) in the connection DB.
"""

from typing import Any, Dict
import bittensor as bt

from bitcast.validator.clients.twitter_client import TwitterClient
from bitcast.validator.tweet_scoring.tweet_store import TweetStore
from bitcast.validator.account_connection.tag_parser import TagParser


def _process_connections_all_pools(tweet: Dict) -> Dict[str, Any]:
    """
    Check all pools and store connection tags where the author is in the social map.

    Delegates to ConnectionScanner.process_tweet() for the actual extraction
    and storage -- keeps all connection logic in the account_connection module.
    """
    from bitcast.validator.social_discovery.pool_manager import PoolManager
    from bitcast.validator.account_connection.connection_scanner import (
        ConnectionScanner, get_social_map_accounts,
    )

    result: Dict[str, Any] = {
        'tags_found': 0, 'new_connections': 0, 'duplicates': 0,
        'errors': 0, 'pools_matched': [],
    }

    text = tweet.get('text', '')
    if not text or not TagParser.extract_tags(text):
        bt.logging.debug(f"No connection tags in tweet {tweet.get('tweet_id')}")
        return result

    try:
        pool_manager = PoolManager()
        pools = pool_manager.get_pools()
    except Exception as e:
        bt.logging.error(f"Failed to load pools: {e}")
        result['errors'] += 1
        return result

    scanner = ConnectionScanner()
    author = tweet.get('author', '').lower()

    for pool_name in pools:
        try:
            pool_accounts = get_social_map_accounts(pool_name)
        except Exception as e:
            bt.logging.debug(f"Could not load social map for pool '{pool_name}': {e}")
            continue

        if author not in pool_accounts:
            continue

        result['pools_matched'].append(pool_name)
        pool_stats = scanner.process_tweet(tweet, pool_name, pool_accounts)

        result['tags_found'] = max(result['tags_found'], pool_stats['tags_found'])
        result['new_connections'] += pool_stats['new_connections']
        result['duplicates'] += pool_stats['duplicates']
        result['errors'] += pool_stats['errors']

    if not result['pools_matched']:
        bt.logging.info(f"Author @{author} not in any pool's social map, skipping connections")

    return result


def fasttrack_tweet(tweet_id: str) -> Dict[str, Any]:
    """
    Fasttrack a tweet into the store and automatically process connections.

    1. Checks the TweetStore -- returns immediately if already present.
    2. Fetches the tweet by ID from the desearch API.
    3. Stores it in the TweetStore.
    4. If the tweet contains connection tags (bitcast-hk: / bitcast-x) and
       the author is in any pool's social map, stores the connections.

    Idempotent -- safe to call multiple times for the same tweet_id.

    Args:
        tweet_id: Twitter status ID to fasttrack.

    Returns:
        Dict with keys:
            status: "fetched_and_stored" | "already_in_store" | "api_failed" | "not_found"
            tweet: The normalised tweet dict (or existing store record)
            is_new: Whether the tweet was newly added to the store
            connection: Connection processing result (always included)
    """
    tweet_id = str(tweet_id).strip()
    store = TweetStore()

    existing = store.get_tweet(tweet_id)
    if existing is not None:
        bt.logging.info(f"Tweet {tweet_id} already in store (by @{existing.get('author', '?')})")
        return {
            'status': 'already_in_store',
            'tweet': existing,
            'is_new': False,
            'connection': _process_connections_all_pools(existing),
        }

    client = TwitterClient()
    api_result = client.fetch_tweet_by_id(tweet_id)

    if not api_result['api_succeeded']:
        bt.logging.warning(f"API call failed for tweet {tweet_id}")
        return {'status': 'api_failed', 'tweet': None, 'is_new': False, 'connection': None}

    tweet = api_result.get('tweet')
    if tweet is None:
        bt.logging.warning(f"Tweet {tweet_id} not found via API")
        return {'status': 'not_found', 'tweet': None, 'is_new': False, 'connection': None}

    is_new = store.store_tweet(tweet)
    bt.logging.info(
        f"{'Stored new' if is_new else 'Updated'} tweet {tweet_id} "
        f"by @{tweet.get('author', '?')} in tweet store"
    )

    return {
        'status': 'fetched_and_stored',
        'tweet': tweet,
        'is_new': is_new,
        'connection': _process_connections_all_pools(tweet),
    }


if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(
        description="Fasttrack a tweet into the tweet store"
    )
    bt.logging.add_args(parser)

    parser.add_argument(
        "--tweet-id",
        type=str,
        default=None,
        help="Tweet ID to fasttrack",
    )

    args_list = sys.argv[1:]

    if not any('--logging' in arg for arg in args_list):
        args_list.insert(0, '--logging.debug')

    config = bt.config(parser, args=args_list)
    bt.logging.set_config(config=config.logging)

    if not config.tweet_id:
        print("ERROR: --tweet-id is required")
        print("Usage: python3 -m bitcast.validator.tweet_scoring.tweet_fasttrack --tweet-id <ID>")
        sys.exit(1)

    env_path = Path(__file__).parents[1] / '.env'
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        bt.logging.info(f"Loaded environment variables from {env_path}")

    result = fasttrack_tweet(tweet_id=config.tweet_id)

    print(f"\n{'='*60}")
    print("Tweet Fasttrack Result")
    print(f"{'='*60}")
    print(f"Status: {result['status']}")
    print(f"New:    {result['is_new']}")

    tweet = result.get('tweet')
    if tweet:
        print(f"Author: @{tweet.get('author', '?')}")
        print(f"Text:   {tweet.get('text', '')[:120]}")
        print(f"Date:   {tweet.get('created_at', '?')}")
        print(f"Likes:  {tweet.get('favorite_count', 0)}  "
              f"RTs: {tweet.get('retweet_count', 0)}  "
              f"Views: {tweet.get('views_count', 0)}")

    conn = result.get('connection')
    if conn:
        print(f"\nConnection processing:")
        print(f"  Tags found:       {conn['tags_found']}")
        print(f"  Pools matched:    {', '.join(conn['pools_matched']) or 'none'}")
        print(f"  New connections:   {conn['new_connections']}")
        print(f"  Duplicates:       {conn['duplicates']}")
        print(f"  Errors:           {conn['errors']}")

    print(f"{'='*60}\n")
