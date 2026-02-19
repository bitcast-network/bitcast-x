"""
Tweet discovery module for tweet retrieval with accumulative caching.

Two discovery modes:
- Lightweight (search-based): Fast API search by tag/QRT, runs every 15 min
- Thorough (timeline-based): Reads from DiscoveryCache (pre-refreshed), runs every 8 hours

Thorough mode is split into two phases:
1. refresh_connected_timelines() - called once per cycle, incrementally fetches
   new tweets for all connected accounts into DiscoveryCache
2. discover_tweets_from_timelines() - called per brief, reads from DiscoveryCache
   with zero API calls

Both modes store results in TweetStore and query it for final output, ensuring
that once a tweet or engagement is discovered it is never lost.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set
import bittensor as bt

from bitcast.validator.clients import TwitterClient
from bitcast.validator.utils.config import (
    ENGAGEMENT_FETCH_INTERVAL_NEW,
    ENGAGEMENT_FETCH_INTERVAL_RECENT,
    ENGAGEMENT_FETCH_INTERVAL_OLD,
)
from bitcast.validator.utils.twitter_cache import get_cached_user_tweets
from .tweet_store import TweetStore

# Max concurrent API calls for engagement retrieval
ENGAGEMENT_MAX_WORKERS = 5


def refresh_connected_timelines(
    connected_accounts: Set[str],
    max_workers: int = 5,
) -> Dict[str, int]:
    """
    Incrementally refresh timelines for all connected accounts.

    Called once per thorough cycle before brief processing. Uses
    DiscoveryCache to skip fresh accounts and only fetch tweets
    newer than the last cache entry.

    Args:
        connected_accounts: Set of usernames to refresh
        max_workers: Concurrent timeline fetches

    Returns:
        Summary stats: accounts_total, cache_hits, refreshed, new_tweets, failed
    """
    client = TwitterClient(posts_only=True)
    accounts = list(connected_accounts)
    stats = {
        'accounts_total': len(accounts),
        'cache_hits': 0,
        'refreshed': 0,
        'new_tweets': 0,
        'failed': 0,
    }

    bt.logging.info(
        f"Refreshing timelines for {len(accounts)} connected accounts "
        f"({max_workers} workers)"
    )

    def fetch_one(username: str) -> Dict:
        return client.fetch_user_tweets(username, skip_if_cache_fresh=True)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_one, u): u for u in accounts
        }
        for future in as_completed(futures):
            username = futures[future]
            try:
                result = future.result()
                info = result.get('cache_info', {})
                if info.get('cache_fresh'):
                    stats['cache_hits'] += 1
                else:
                    stats['refreshed'] += 1
                    stats['new_tweets'] += info.get('new_tweets', 0)
            except Exception as e:
                stats['failed'] += 1
                bt.logging.warning(f"Failed to refresh timeline for @{username}: {e}")

    bt.logging.info(
        f"Timeline refresh complete: {stats['refreshed']} refreshed, "
        f"{stats['cache_hits']} cache hits, {stats['new_tweets']} new tweets, "
        f"{stats['failed']} failed"
    )
    return stats


def build_search_query(
    tag: Optional[str] = None,
    quoted_tweet_id: Optional[str] = None,
    since_date: Optional[datetime] = None,
    until_date: Optional[datetime] = None
) -> str:
    """
    Build X-style search query string.
    
    Args:
        tag: Optional tag/hashtag/keyword to search for
        quoted_tweet_id: Optional tweet ID to find quotes of
        since_date: Optional start date filter (inclusive)
        until_date: Optional end date filter (inclusive - tweets up to end of this day)
    
    Returns:
        X-style query string
        
    Note:
        X search's 'until:' parameter is exclusive, meaning 'until:2026-01-15' 
        returns tweets BEFORE Jan 15th starts. To include all of Jan 15th,
        we add 1 day to until_date when formatting the query.
    """
    parts = []
    
    if tag:
        parts.append(tag)
    
    if quoted_tweet_id:
        parts.append(f"quoted_tweet_id:{quoted_tweet_id}")
    
    if since_date:
        parts.append(f"since:{since_date.strftime('%Y-%m-%d')}")
    
    if until_date:
        next_day = until_date + timedelta(days=1)
        parts.append(f"until:{next_day.strftime('%Y-%m-%d')}")
    
    return " ".join(parts)


class TweetDiscovery:
    """
    Discovers tweets for a brief with accumulative caching via TweetStore.
    
    Two discovery modes:
    - discover_tweets(): Lightweight search API queries (fast, may miss tweets)
    - discover_tweets_from_timelines(): Reads from pre-refreshed DiscoveryCache (thorough, no API calls)
    
    Both store results in TweetStore and query it for final output.
    """
    
    def __init__(
        self,
        client: TwitterClient,
        active_accounts: Set[str],
        considered_accounts: Optional[Dict[str, float]] = None,
    ):
        """
        Args:
            client: TwitterClient instance for API access
            active_accounts: Set of usernames whose tweets can be scored
            considered_accounts: Dict of username -> influence_score for engagements
        """
        self.client = client
        self.active_accounts = {a.lower() for a in active_accounts}
        self.considered_accounts = (
            {k.lower(): v for k, v in considered_accounts.items()}
            if considered_accounts
            else {a: 1.0 for a in self.active_accounts}
        )
        self.store = TweetStore.get_instance()
        
        bt.logging.info(
            f"TweetDiscovery initialized: {len(self.active_accounts)} active accounts, "
            f"{len(self.considered_accounts)} considered accounts"
        )
    
    def _search_and_store(self, query: str, max_results: int = 200) -> List[Dict]:
        """
        Search API and store results.
        
        Always makes fresh API calls. Merges results into the accumulative store.
        
        Args:
            query: X-style search query
            max_results: Max results
            
        Returns:
            List of tweets found in this API call (for logging only)
        """
        result = self.client.search_tweets(
            query=query,
            max_results=max_results,
            sort="latest"
        )
        
        all_tweets = []
        if result['api_succeeded']:
            all_tweets = [t for t in result['tweets'] if t.get('tweet_id')]
        else:
            bt.logging.warning(f"Search API failed for query '{query[:50]}...'")
        
        # Store all discovered tweets (accumulative merge)
        if all_tweets:
            stats = self.store.store_tweets(all_tweets)
            bt.logging.info(
                f"Search returned {len(all_tweets)} tweets for query: {query[:50]}... "
                f"(store: {stats['new']} new, {stats['updated']} updated)"
            )
        else:
            bt.logging.info(f"Search returned 0 tweets for query: {query[:50]}...")
        
        return all_tweets
    
    def discover_tweets(
        self,
        tag: Optional[str],
        qrt: Optional[str],
        start_date: datetime,
        end_date: datetime,
        max_results: int = 200
    ) -> List[Dict]:
        """
        Discover tweets for a brief: search APIs, store results, query store.
        
        At least one of tag or qrt must be provided.
        
        Args:
            tag: Optional tag/hashtag to search for
            qrt: Optional tweet ID that must be quoted
            start_date: Start date for tweet window
            end_date: End date for tweet window
            max_results: Max results per API call
        
        Returns:
            List of matching tweets from active accounts (from store)
        """
        if not tag and not qrt:
            raise ValueError("At least one of 'tag' or 'qrt' must be provided")
        
        # Step 1: Make fresh API calls and store results
        if qrt:
            query = build_search_query(
                quoted_tweet_id=qrt,
                since_date=start_date,
                until_date=end_date
            )
            bt.logging.info(f"Searching QRTs with query: {query}")
            self._search_and_store(query, max_results)
        
        if tag:
            query = build_search_query(
                tag=tag,
                since_date=start_date,
                until_date=end_date
            )
            bt.logging.info(f"Searching tweets with query: {query}")
            self._search_and_store(query, max_results)
        
        # Step 2: Query store for all matching tweets from active accounts
        store_tweets = self.store.query_tweets(
            authors=self.active_accounts,
            quoted_tweet_id=qrt,
            tag=tag if not qrt else None,  # Don't filter by tag if qrt is the primary filter
            start_date=start_date,
            end_date=end_date,
        )
        
        bt.logging.info(
            f"Store query returned {len(store_tweets)} tweets from active accounts"
        )
        
        return store_tweets
    
    def discover_tweets_from_timelines(
        self,
        tag: Optional[str],
        qrt: Optional[str],
        start_date: datetime,
        end_date: datetime,
    ) -> List[Dict]:
        """
        Discover tweets from pre-refreshed DiscoveryCache (no API calls).

        Reads cached timelines for active accounts, filters by brief date range,
        stores matches in TweetStore, then queries TweetStore for results.

        Timelines must be refreshed beforehand via refresh_connected_timelines().

        Args:
            tag: Optional tag/hashtag to filter for
            qrt: Optional tweet ID that must be quoted
            start_date: Start date for tweet window
            end_date: End date for tweet window

        Returns:
            List of matching tweets from active accounts (from store)
        """
        if not tag and not qrt:
            raise ValueError("At least one of 'tag' or 'qrt' must be provided")

        accounts = list(self.active_accounts)
        bt.logging.info(
            f"Thorough discovery: reading cached timelines for {len(accounts)} accounts"
        )

        all_tweets = []
        cache_hits = 0

        for username in accounts:
            cached = get_cached_user_tweets(username)
            if cached and cached.get('tweets'):
                cache_hits += 1
                all_tweets.extend(cached['tweets'])

        # Filter to brief date range before storing
        date_filtered = []
        for tweet in all_tweets:
            created_at = tweet.get('created_at', '')
            if not created_at:
                continue
            try:
                tweet_date = datetime.strptime(created_at, '%a %b %d %H:%M:%S %z %Y')
                tweet_utc = tweet_date.astimezone(timezone.utc)
                if start_date <= tweet_utc <= end_date:
                    date_filtered.append(tweet)
            except (ValueError, AttributeError):
                date_filtered.append(tweet)

        bt.logging.info(
            f"Read {len(all_tweets)} cached tweets from {cache_hits}/{len(accounts)} "
            f"accounts, {len(date_filtered)} within brief dates"
        )

        if date_filtered:
            stats = self.store.store_tweets(date_filtered)
            bt.logging.info(
                f"Timeline tweets stored: {stats['new']} new, {stats['updated']} updated"
            )

        store_tweets = self.store.query_tweets(
            authors=self.active_accounts,
            quoted_tweet_id=qrt,
            tag=tag if not qrt else None,
            start_date=start_date,
            end_date=end_date,
        )

        bt.logging.info(
            f"Store query returned {len(store_tweets)} tweets from active accounts"
        )

        return store_tweets
    
    def _fetch_engagements(self, tweet_id: str) -> None:
        """
        Fetch RT and QRT engagements from API and store them.
        
        Runs RT and QRT fetches concurrently using threads.
        """
        def fetch_retweeters():
            rt_result = self.client.get_retweeters(tweet_id)
            if rt_result['api_succeeded']:
                self.store.store_retweeters(tweet_id, rt_result['retweeters'])
        
        def fetch_quoters():
            qrt_query = build_search_query(quoted_tweet_id=tweet_id)
            qrt_result = self.client.search_tweets(query=qrt_query, max_results=100)
            if qrt_result['api_succeeded']:
                self.store.store_tweets(qrt_result['tweets'])
                self.store.store_quoters(tweet_id, qrt_result['tweets'])
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(fetch_retweeters), executor.submit(fetch_quoters)]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    bt.logging.warning(f"Error fetching engagements for {tweet_id}: {e}")
    
    def _build_engagements_from_store(
        self,
        tweet_id: str,
        author: str,
        excluded: Set[str]
    ) -> Dict[str, str]:
        """Build filtered engagement map from store data."""
        stored = self.store.get_engagements(tweet_id)
        engagements = {}
        
        for username in stored.get('retweeters', {}):
            if username == author or username in excluded:
                continue
            if username in self.considered_accounts:
                engagements[username] = "retweet"
        
        # Quote takes priority over retweet
        for username in stored.get('quoters', {}):
            if username == author or username in excluded:
                continue
            if username in self.considered_accounts:
                engagements[username] = "quote"
        
        return engagements
    
    def get_engagements_for_tweet(
        self,
        tweet: Dict,
        excluded_engagers: Optional[Set[str]] = None
    ) -> Dict[str, str]:
        """
        Fetch fresh RT/QRT engagements, merge into store, return all known.
        
        Args:
            tweet: Tweet dict with 'tweet_id' and 'author'
            excluded_engagers: Usernames to exclude (e.g., brief participants)
        
        Returns:
            Dict mapping username -> engagement_type ("retweet" or "quote")
        """
        tweet_id = tweet.get('tweet_id', '')
        author = tweet.get('author', '').lower()
        
        if not tweet_id:
            return {}
        
        excluded = {e.lower() for e in (excluded_engagers or set())}
        
        self._fetch_engagements(tweet_id)
        engagements = self._build_engagements_from_store(tweet_id, author, excluded)
        
        bt.logging.debug(
            f"Tweet {tweet_id[:10]}... by @{author}: "
            f"{len(engagements)} engagements from considered accounts"
        )
        
        return engagements
    
    def _should_fetch_engagements(self, tweet: Dict) -> bool:
        """
        Determine if we should fetch engagements for a tweet based on tiered intervals.
        
        Uses tweet age to determine fetch frequency:
        - New tweets (< 1 hour): Fetch every hour
        - Recent tweets (1-24 hours): Fetch every 4 hours
        - Old tweets (> 24 hours): Fetch every 8 hours
        
        Args:
            tweet: Tweet dict with 'tweet_id' and 'created_at'
            
        Returns:
            True if engagements should be fetched, False otherwise
        """
        tweet_id = tweet.get('tweet_id')
        if not tweet_id:
            return False
        
        # Get last fetch time
        last_fetch = self.store.get_last_engagement_fetch(tweet_id)
        
        # Calculate tweet age
        created_at = tweet.get('created_at', '')
        if not created_at:
            # If no created_at, fetch to be safe
            return True
        
        try:
            tweet_date = datetime.strptime(created_at, '%a %b %d %H:%M:%S %z %Y')
            tweet_date_utc = tweet_date.astimezone(timezone.utc)
            now = datetime.now(timezone.utc)
            age_hours = (now - tweet_date_utc).total_seconds() / 3600
        except (ValueError, AttributeError):
            # If we can't parse the date, fetch to be safe
            return True
        
        # Determine required interval based on age
        if age_hours < 1:
            required_interval = ENGAGEMENT_FETCH_INTERVAL_NEW  # 1 hour
        elif age_hours < 24:
            required_interval = ENGAGEMENT_FETCH_INTERVAL_RECENT  # 4 hours
        else:
            required_interval = ENGAGEMENT_FETCH_INTERVAL_OLD  # 8 hours
        
        # Check if enough time has passed since last fetch
        if last_fetch is None:
            return True  # Never fetched, fetch now
        
        hours_since_fetch = (now - last_fetch).total_seconds() / 3600
        return hours_since_fetch >= required_interval
    
    def get_engagements_batch(
        self,
        tweets: List[Dict],
        excluded_engagers: Optional[Set[str]] = None
    ) -> Dict[str, Dict[str, str]]:
        """
        Get engagements for multiple tweets concurrently with tiered fetching.
        
        Uses smart tiered fetching based on tweet age to reduce API calls:
        - New tweets (< 1 hour): Fetch every hour
        - Recent tweets (1-24 hours): Fetch every 4 hours
        - Old tweets (> 24 hours): Fetch every 8 hours
        
        Args:
            tweets: List of tweet dicts
            excluded_engagers: Usernames to exclude
        
        Returns:
            Dict mapping tweet_id -> {username -> engagement_type}
        """
        excluded = {e.lower() for e in (excluded_engagers or set())}
        valid_tweets = [t for t in tweets if t.get('tweet_id')]
        
        # Filter tweets that actually need engagement fetching
        tweets_needing_fetch = [t for t in valid_tweets if self._should_fetch_engagements(t)]
        skipped_count = len(valid_tweets) - len(tweets_needing_fetch)
        
        # Fetch engagements only for tweets that need updating
        if tweets_needing_fetch:
            bt.logging.info(
                f"Fetching engagements for {len(tweets_needing_fetch)}/{len(valid_tweets)} tweets "
                f"({skipped_count} skipped - cached) ({ENGAGEMENT_MAX_WORKERS} workers)"
            )
            
            with ThreadPoolExecutor(max_workers=ENGAGEMENT_MAX_WORKERS) as executor:
                futures = {
                    executor.submit(self._fetch_engagements, t['tweet_id']): t
                    for t in tweets_needing_fetch
                }
                
                # Track successful fetches for timestamp updates
                successful_fetches = []
                failed_fetches = []
                
                for future in as_completed(futures):
                    tweet = futures[future]
                    try:
                        future.result()
                        successful_fetches.append(tweet)
                    except Exception as e:
                        failed_fetches.append(tweet)
                        bt.logging.warning(
                            f"Error fetching engagements for {tweet.get('tweet_id')}: {e}"
                        )
            
            # Update last fetch timestamp only for successfully fetched tweets
            now = datetime.now(timezone.utc)
            for tweet in successful_fetches:
                self.store.set_last_engagement_fetch(tweet['tweet_id'], now)
            
            if failed_fetches:
                bt.logging.warning(
                    f"Failed to fetch engagements for {len(failed_fetches)} tweets - "
                    f"will retry on next cycle"
                )
        else:
            bt.logging.info(
                f"Using cached engagements for all {len(valid_tweets)} tweets "
                f"(no API calls needed)"
            )
        
        # Build engagement maps from store (includes cached data)
        all_engagements = {}
        for tweet in valid_tweets:
            tweet_id = tweet['tweet_id']
            author = tweet.get('author', '').lower()
            all_engagements[tweet_id] = self._build_engagements_from_store(
                tweet_id, author, excluded
            )
        
        total_engagements = sum(len(e) for e in all_engagements.values())
        bt.logging.info(
            f"Retrieved engagements for {len(valid_tweets)} tweets: "
            f"{total_engagements} total engagements"
        )
        
        return all_engagements
