"""
Tweet fasttrack module for manually injecting tweets into the pipeline.

Fetches a single tweet by ID from the desearch API and stores it in the
TweetStore. Automatically processes connection tags if the tweet contains
them and the author is in any pool's social map.

Idempotent: re-fasttracking a tweet already in the store simply refreshes
engagement stats. Re-processing connections is safe -- ConnectionDatabase
uses a single row per account_username.
"""

import asyncio
from typing import Any, Dict, List
import bittensor as bt
import requests

from bitcast.validator.clients.twitter_client import TwitterClient
from bitcast.validator.tweet_scoring.tweet_store import TweetStore
from bitcast.validator.account_connection.tag_parser import TagParser


def _process_connections_all_pools(tweet: Dict) -> Dict[str, Any]:
    """
    Store connection tags if the tweet's author appears in any pool's social map.

    Delegates to ConnectionScanner.process_tweet() so all connection logic
    stays in the account_connection module.
    """
    from bitcast.validator.account_connection.connection_scanner import ConnectionScanner

    if not tweet.get('text') or not TagParser.extract_tags(tweet.get('text', '')):
        bt.logging.debug(f"No connection tags in tweet {tweet.get('tweet_id')}")
        return {
            'tags_found': 0, 'new_connections': 0, 'updated_connections': 0,
            'unchanged': 0, 'errors': 0, 'pools_matched': [],
        }

    scanner = ConnectionScanner()
    result = scanner.process_tweet(tweet)

    if not result['pools_matched']:
        author = tweet.get('author', '').lower()
        bt.logging.info(f"Author @{author} not in any pool's social map, skipping connections")

    return result


def fasttrack_tweet(tweet_id: str) -> Dict[str, Any]:
    """
    Fasttrack a tweet into the store and automatically process connections.

    1. Checks the TweetStore -- returns immediately if already present.
    2. Fetches the tweet by ID from the desearch API.
    3. Stores it in the TweetStore.
    4. If the tweet contains connection tags (Stitch-hk: / Stitch3- or
       legacy bitcast-hk: / bitcast-x) and the author is in any pool's
       social map, stores the connections.

    Idempotent -- safe to call multiple times for the same tweet_id.

    Args:
        tweet_id: Twitter status ID to fasttrack.

    Returns:
        Dict with keys:
            status: "fetched_and_stored" | "already_in_store" | "api_failed" | "not_found"
            tweet: The normalised tweet dict (or existing store record)
            is_new: Whether the tweet was newly added to the store
            connection: Connection processing result (included when processed)
    """
    tweet_id = str(tweet_id).strip()
    store = TweetStore()

    existing = store.get_tweet(tweet_id)
    if existing is not None:
        # The tweet may have been cached by an unrelated brief search before
        # fast-track saw it. Still process connections so registration tags are
        # not silently dropped. Connection processing is idempotent and only
        # reports a touched row when the DB content actually changes.
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


FAST_TRACK_URL = "https://www.stitch3.ai/api/fast-track"
FAST_TRACK_MAX_IDS = 1000


def _publish_connections_from_db(usernames: List[str]) -> int:
    """
    Publish only selected connection rows using the shared publisher path.

    Returns:
        Number of connection rows included in the publish payload.
    """
    from datetime import datetime, timezone
    from bitcast.validator.account_connection.connection_db import ConnectionDatabase
    from bitcast.validator.account_connection.connection_publisher import publish_account_connections

    if not usernames:
        return 0

    db = ConnectionDatabase()
    unique_usernames = {u.lower() for u in usernames if u}
    selected_rows: List[Dict[str, Any]] = []
    for username in unique_usernames:
        selected_rows.extend(db.get_connections_by_account(username))

    connections_to_publish = [
        {
            'tweet_id': conn['tweet_id'],
            'tag': conn['tag'],
            'username': conn['account_username'],
            'referred_by': conn.get('referred_by'),
            'referee_amount': conn.get('referee_amount'),
            'referrer_amount': conn.get('referrer_amount'),
        }
        for conn in selected_rows
    ]
    if not connections_to_publish:
        return 0

    async def _publish_selected_connections() -> bool:
        try:
            try:
                from bitcast.validator.utils.run_manager import get_run_manager
                hotkey = get_run_manager().wallet.hotkey.ss58_address
            except (ValueError, RuntimeError):
                hotkey = None

            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
            run_id = (
                f"vali_x_connection_{hotkey}_{timestamp}"
                if hotkey else
                f"vali_x_connection_{timestamp}"
            )
            return await publish_account_connections(
                connections=connections_to_publish,
                run_id=run_id,
            )
        except Exception as e:
            bt.logging.warning(f"Fast-track publish task failed: {e}")
            return False

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_publish_selected_connections())
        bt.logging.debug(
            f"Fast-track queued publish for {len(connections_to_publish)} connection(s)"
        )
    except RuntimeError:
        success = asyncio.run(_publish_selected_connections())
        if success:
            bt.logging.debug(
                f"Fast-track published {len(connections_to_publish)} connection(s)"
            )
        else:
            bt.logging.warning("Fast-track publish failed for selected connection rows")
    return len(connections_to_publish)


def poll_fast_track() -> Dict[str, Any]:
    """
    Poll the stitch3 fast-track endpoint and fasttrack all returned tweet IDs.

    Fetches tweet IDs from the stitch3 API and passes each to
    fasttrack_tweet(). Capped at FAST_TRACK_MAX_IDS to prevent runaway
    batches.

    Returns:
        Dict with summary: polled, fast_tracked, already_in_store, failed, total
    """
    try:
        resp = requests.get(FAST_TRACK_URL, timeout=10)
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        bt.logging.warning(f"Fast-track poll failed: {e}")
        return {"polled": 0, "error": str(e)}

    tweet_ids: List[str] = body.get("data", [])
    if not tweet_ids:
        bt.logging.debug("Fast-track: no tweets to process")
        return {"polled": 0, "total": 0}

    if len(tweet_ids) > FAST_TRACK_MAX_IDS:
        bt.logging.warning(
            f"Fast-track: received {len(tweet_ids)} IDs, capping to {FAST_TRACK_MAX_IDS}"
        )
        tweet_ids = tweet_ids[:FAST_TRACK_MAX_IDS]

    stats = {
        "polled": len(tweet_ids),
        "fast_tracked": 0,
        "already_in_store": 0,
        "failed": 0,
        "new_connections": 0,
        "updated_connections": 0,
        "connection_rows_touched": 0,
        "published_rows": 0,
        "publish_queued": False,
    }
    touched_usernames: List[str] = []

    for tid in tweet_ids:
        result = fasttrack_tweet(tid)
        status = result.get("status")

        if status == "fetched_and_stored":
            stats["fast_tracked"] += 1
        elif status == "already_in_store":
            stats["already_in_store"] += 1
        else:
            stats["failed"] += 1
            bt.logging.debug(f"Fast-track tweet {tid}: {status}")
            continue

        # Connections are processed for both freshly stored and already-cached
        # tweets. Only genuine inserts/updates count as touched, so unchanged
        # rows are never republished on subsequent polls.
        conn = result.get("connection") or {}
        new_connections = int(conn.get("new_connections", 0))
        updated_connections = int(conn.get("updated_connections", 0))
        rows_touched = new_connections + updated_connections
        stats["new_connections"] += new_connections
        stats["updated_connections"] += updated_connections
        stats["connection_rows_touched"] += rows_touched
        if rows_touched > 0:
            tweet = result.get("tweet") or {}
            author = (tweet.get("author") or "").strip().lower()
            if author:
                touched_usernames.append(author)

    # Publish when any connection row was inserted or updated.
    if stats["connection_rows_touched"] > 0:
        from bitcast.validator.utils.config import ENABLE_DATA_PUBLISH
        if ENABLE_DATA_PUBLISH:
            try:
                stats["published_rows"] = _publish_connections_from_db(touched_usernames)
                stats["publish_queued"] = stats["published_rows"] > 0
            except Exception as e:
                bt.logging.warning(f"Fast-track connection publish failed: {e}")

    bt.logging.info(
        f"Fast-track poll: {stats['polled']} polled, "
        f"{stats['fast_tracked']} new, "
        f"{stats['already_in_store']} cached, "
        f"{stats['failed']} failed, "
        f"{stats['new_connections']} new connections, "
        f"{stats['updated_connections']} updated connections, "
        f"{stats['connection_rows_touched']} connection rows touched, "
        f"{stats['published_rows']} rows published, "
        f"publish_queued={stats['publish_queued']}"
    )
    return stats


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

    config = bt.Config(parser, args=args_list)
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
        print(f"  New connections:    {conn['new_connections']}")
        print(f"  Updated connections:{conn['updated_connections']}")
        print(f"  Unchanged:          {conn['unchanged']}")
        print(f"  Errors:             {conn['errors']}")

    print(f"{'='*60}\n")
