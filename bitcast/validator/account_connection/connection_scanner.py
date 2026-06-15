"""
Connection scanner for finding connection tags via reply-based scanning.

Fetches replies to designated connection tweets, cross-references authors
against the union of pool social maps, and extracts connection tags.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any, Set
import bittensor as bt

from bitcast.validator.social_discovery.pool_manager import PoolManager
from bitcast.validator.clients.twitter_client import TwitterClient
from bitcast.validator.tweet_scoring.social_map_loader import load_latest_social_map
from bitcast.validator.utils.config import (
    ENABLE_DATA_PUBLISH, WALLET_NAME, HOTKEY_NAME, CONNECTION_TWEET_IDS
)
from bitcast.validator.utils.referral_rewards import compute_referral_reward_from_account
from .connection_db import ConnectionDatabase
from .tag_parser import TagParser
from .connection_publisher import publish_account_connections


def get_social_map_accounts(pool_name: str) -> Set[str]:
    """
    Load latest social map for pool and return all account usernames (lowercase).
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
    Scans for connection tags by fetching replies to designated tweets.

    Cross-references authors against the union of every pool's social map.
    Each (user, tag) is stored as a single pool-agnostic row; the locked
    referral amount is the highest amount the user qualifies for across
    every pool they appear in.
    """

    def __init__(self, db_path: Optional[Path] = None,
                 twitter_client: Optional[TwitterClient] = None,
                 tweet_ids: Optional[List[str]] = None):
        self.twitter_client = twitter_client or TwitterClient()
        self.database = ConnectionDatabase(db_path=db_path)
        self.tag_parser = TagParser()
        self.tweet_ids = tweet_ids if tweet_ids is not None else CONNECTION_TWEET_IDS
        self._pool_accounts: Optional[Dict[str, Set[str]]] = None
        self._social_maps_by_pool: Dict[str, Dict[str, Any]] = {}

        bt.logging.info(
            f"ConnectionScanner initialized: {len(self.tweet_ids)} tweet(s) to scan"
        )

    def _get_pool_accounts_map(self) -> Dict[str, Set[str]]:
        """Return {pool_name: {usernames}} for every pool with a loadable social map."""
        if self._pool_accounts is not None:
            return self._pool_accounts

        result: Dict[str, Set[str]] = {}
        try:
            for pool_name in PoolManager().get_pools():
                try:
                    result[pool_name] = get_social_map_accounts(pool_name)
                except (ValueError, FileNotFoundError):
                    pass
        except Exception as e:
            bt.logging.warning(f"Could not load pool list: {e}")

        self._pool_accounts = result
        return result

    def _all_known_accounts(self) -> Set[str]:
        """Union of usernames across every pool's latest social map."""
        union: Set[str] = set()
        for accts in self._get_pool_accounts_map().values():
            union |= accts
        return union

    def _get_social_map(self, pool_name: str) -> Dict[str, Any]:
        pool_name = pool_name.lower()
        if pool_name not in self._social_maps_by_pool:
            social_map, _ = load_latest_social_map(pool_name)
            self._social_maps_by_pool[pool_name] = social_map
        return self._social_maps_by_pool[pool_name]

    def _compute_locked_referral_amount(self, username: str, referred_by: Optional[str]) -> float:
        """
        Compute the referral amount to lock for a user, as the maximum across
        every pool the user is a member of. Each pool contributes a candidate
        amount derived from its own max_referral_amount cap and the user's
        account data in that pool's social map.
        """
        if not referred_by:
            return 0.0

        username_key = username.lower()
        pool_accounts = self._get_pool_accounts_map()
        pool_manager: Optional[PoolManager] = None

        best = 0.0
        for pool_name, accts in pool_accounts.items():
            if username_key not in accts:
                continue

            try:
                if pool_manager is None:
                    pool_manager = PoolManager()
                pool_config = pool_manager.get_pool(pool_name) or {}
                max_amount = float(pool_config.get('max_referral_amount', 100.0))
            except Exception:
                max_amount = 100.0

            try:
                accounts = self._get_social_map(pool_name).get('accounts', {})
            except Exception as e:
                bt.logging.warning(
                    f"Could not load social map for referral amount lock "
                    f"({pool_name}/@{username}): {e}"
                )
                continue

            account_info = accounts.get(username) or accounts.get(username_key)
            if account_info is None:
                account_info = next(
                    (data for account, data in accounts.items() if account.lower() == username_key),
                    None,
                )
            if account_info is None:
                continue

            candidate = compute_referral_reward_from_account(account_info, max_amount=max_amount)
            if candidate > best:
                best = candidate

        return best

    def _extract_connections_from_tweets(
        self,
        tweets: List[Dict],
        eligible_accounts: Set[str],
    ) -> List[Dict]:
        """
        Extract connection tags from tweets whose author appears in
        eligible_accounts. eligible_accounts should be the union of accounts
        across every pool's social map.
        """
        connections = []
        all_known = self._all_known_accounts()

        for tweet in tweets:
            author = tweet.get('author', '').lower()
            tweet_id = tweet.get('tweet_id')
            text = tweet.get('text', '')

            if not author or not tweet_id or not text:
                continue

            if author not in eligible_accounts:
                continue

            if tweet.get('retweeted_user'):
                continue

            tags = self.tag_parser.extract_tags(text)

            for parsed in tags:
                referred_by = parsed.referred_by
                referral_code = parsed.referral_code

                if referred_by and referred_by.strip().lower().lstrip("@") == author:
                    bt.logging.info(
                        f"Ignoring self-referral for @{author} "
                        f"(referral_code='{referral_code}')"
                    )
                    referred_by = None
                    referral_code = None

                if referred_by and all_known and referred_by.lower() not in all_known:
                    bt.logging.info(
                        f"Ignoring referral code '{referral_code}' (decoded to "
                        f"'{referred_by}') — handle not found in any social map"
                    )
                    referred_by = None
                    referral_code = None

                connections.append({
                    'tweet_id': tweet_id,
                    'username': author,
                    'tag_type': parsed.tag_type,
                    'tag': parsed.full_tag,
                    'referral_code': referral_code,
                    'referred_by': referred_by,
                })

        return connections

    def _store_connection(self, conn: Dict[str, Any]) -> bool:
        """Compute locked referral amount and upsert. Returns True if newly inserted."""
        locked_amount = self._compute_locked_referral_amount(
            conn['username'], conn.get('referred_by')
        )
        return self.database.upsert_connection(
            tweet_id=conn['tweet_id'],
            tag=conn['tag'],
            account_username=conn['username'],
            referral_code=conn.get('referral_code'),
            referred_by=conn.get('referred_by'),
            referee_amount=locked_amount,
            referrer_amount=locked_amount,
        )

    def _published_fields_snapshot(self, username: str) -> Optional[tuple]:
        """
        Return a tuple of the connection fields that are sent downstream, or
        None if no row exists. Used to decide whether re-processing a tweet
        actually changed anything worth republishing.
        """
        rows = self.database.get_connections_by_account(username)
        if not rows:
            return None
        row = rows[0]
        return (
            str(row.get('tweet_id')),
            row.get('tag'),
            row.get('referred_by'),
            row.get('referee_amount'),
            row.get('referrer_amount'),
        )

    def process_tweet(self, tweet: Dict) -> Dict[str, Any]:
        """
        Process a single tweet for connection tags.

        Idempotent. Stores connections for any author whose handle appears in
        the union of all pool social maps. Referral amount is locked as the
        max across pools the user belongs to.

        Distinguishes three outcomes per connection so callers can avoid
        republishing unchanged rows:
        - ``new_connections``: a brand new row was inserted.
        - ``updated_connections``: an existing row's published fields changed.
        - ``unchanged``: the row already matched (no-op).
        """
        stats: Dict[str, Any] = {
            'tags_found': 0, 'new_connections': 0, 'updated_connections': 0,
            'unchanged': 0, 'errors': 0, 'pools_matched': [],
        }

        eligible = self._all_known_accounts()
        author = tweet.get('author', '').lower()
        if author and author in eligible:
            stats['pools_matched'] = [
                pool for pool, accts in self._get_pool_accounts_map().items()
                if author in accts
            ]

        found = self._extract_connections_from_tweets([tweet], eligible)
        stats['tags_found'] = len(found)

        for conn in found:
            try:
                before = self._published_fields_snapshot(conn['username'])
                is_new = self._store_connection(conn)
                if is_new:
                    stats['new_connections'] += 1
                    bt.logging.info(f"New connection: @{conn['username']} -> {conn['tag']}")
                elif before != self._published_fields_snapshot(conn['username']):
                    stats['updated_connections'] += 1
                    bt.logging.info(f"Updated connection: @{conn['username']} -> {conn['tag']}")
                else:
                    stats['unchanged'] += 1
            except Exception as e:
                bt.logging.error(f"Error storing connection for @{conn['username']}: {e}")
                stats['errors'] += 1

        return stats

    def _fetch_all_replies(self) -> List[Dict]:
        """Fetch replies from all configured connection tweets."""
        if not self.tweet_ids:
            bt.logging.warning("No CONNECTION_TWEET_IDS configured, skipping scan")
            return []

        tweet_id_set = set(self.tweet_ids)
        all_replies: List[Dict] = []
        for tid in self.tweet_ids:
            bt.logging.info(f"Fetching replies for connection tweet {tid}")
            result = self.twitter_client.fetch_post_replies(tid)
            if result['api_succeeded']:
                for t in result['tweets']:
                    if t.get('tweet_id') and t.get('in_reply_to_status_id') in tweet_id_set:
                        all_replies.append(t)
            else:
                bt.logging.warning(f"Failed to fetch replies for tweet {tid}")

        bt.logging.info(f"Fetched {len(all_replies)} direct replies across {len(self.tweet_ids)} tweet(s)")
        return all_replies

    async def scan_all_pools(self) -> Dict[str, Any]:
        """
        Scan replies once and upsert pool-agnostic connection rows for any
        author who appears in at least one pool's social map.
        """
        start_time = datetime.now(timezone.utc)

        pool_accounts = self._get_pool_accounts_map()
        bt.logging.info(f"Loaded social maps for {len(pool_accounts)} pools: {list(pool_accounts)}")

        eligible = self._all_known_accounts()
        all_replies = self._fetch_all_replies()

        total_stats: Dict[str, Any] = {
            'pools_scanned': len(pool_accounts),
            'accounts_checked': len(eligible),
            'tweets_scanned': len(all_replies),
            'tags_found': 0,
            'new_connections': 0,
            'duplicates_skipped': 0,
            'errors': 0,
        }

        if not all_replies:
            total_stats['processing_time'] = (datetime.now(timezone.utc) - start_time).total_seconds()
            return total_stats

        found_connections = self._extract_connections_from_tweets(all_replies, eligible)
        total_stats['tags_found'] = len(found_connections)

        bt.logging.info(
            f"Found {len(found_connections)} connection tags from "
            f"{len({c['username'] for c in found_connections})} accounts"
        )

        for conn in found_connections:
            try:
                is_new = self._store_connection(conn)
                if is_new:
                    total_stats['new_connections'] += 1
                    if conn.get('referred_by'):
                        bt.logging.info(
                            f"New connection with referral: {conn['username']} "
                            f"referred by @{conn['referred_by']}"
                        )
                else:
                    total_stats['duplicates_skipped'] += 1
            except Exception as e:
                bt.logging.error(f"Error storing connection: {e}")
                total_stats['errors'] += 1

        total_stats['processing_time'] = (datetime.now(timezone.utc) - start_time).total_seconds()

        if ENABLE_DATA_PUBLISH:
            all_db_connections = self.database.get_all_connections()
            connections_to_publish = [
                {
                    'tweet_id': conn['tweet_id'],
                    'tag': conn['tag'],
                    'username': conn['account_username'],
                    'referred_by': conn.get('referred_by'),
                    'referee_amount': conn.get('referee_amount'),
                    'referrer_amount': conn.get('referrer_amount'),
                }
                for conn in all_db_connections
            ]
            if connections_to_publish:
                await self._publish_connections(connections_to_publish)

        bt.logging.info(
            f"Scan complete: {total_stats['pools_scanned']} pools, "
            f"{total_stats['tags_found']} tags, "
            f"{total_stats['new_connections']} new"
        )
        return total_stats

    async def _publish_connections(self, connections: List[Dict]) -> None:
        """Publish connections to data API."""
        try:
            try:
                from ..utils.run_manager import get_run_manager
                hotkey = get_run_manager().wallet.hotkey.ss58_address
            except (ValueError, RuntimeError):
                hotkey = None
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
            if hotkey:
                run_id = f"vali_x_connection_{hotkey}_{timestamp}"
            else:
                run_id = f"vali_x_connection_{timestamp}"
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
            description="Scan for connection tags in replies to designated tweets"
        )
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)
        bt.subtensor.add_args(parser)

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

        scanner = ConnectionScanner()

        bt.logging.info("Starting connection scan across all pools")
        summary = asyncio.run(scanner.scan_all_pools())

        print(f"\n{'='*60}")
        print("Account Connection Scan Complete")
        print(f"{'='*60}")
        print(f"Pools scanned: {summary['pools_scanned']}")
        print(f"Replies checked: {summary.get('tweets_scanned', 'N/A')}")
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
