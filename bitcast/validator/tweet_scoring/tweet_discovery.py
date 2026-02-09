"""
Tweet discovery module for tweet retrieval with accumulative caching.

Two discovery modes:
- Lightweight (search-based): Fast API search by tag/QRT, runs every 45 min
- Thorough (timeline-based): Fetches connected accounts' profiles, runs every 8 hours

Both modes:
1. Make fresh API calls
2. Merge results into ScoringStore (accumulative - never loses data)
3. Query ScoringStore for all tweets matching brief criteria
4. Fetch fresh engagement data (RTs/QRTs) from API
5. Merge engagements into ScoringStore
6. Query ScoringStore for all known engagements

This ensures that once a tweet or engagement is discovered, it is never lost
even if the search API stops returning it in subsequent calls.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set
import bittensor as bt

from bitcast.validator.clients import TwitterClient
from bitcast.validator.utils.config import TWEET_SCORING_FETCH_DAYS
from .tweet_store import ScoringStore

# Max concurrent API calls for engagement retrieval
ENGAGEMENT_MAX_WORKERS = 5


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
    Discovers tweets for a brief with accumulative caching via ScoringStore.
    
    Two discovery modes:
    - discover_tweets(): Lightweight search API queries (fast, may miss tweets)
    - discover_tweets_from_timelines(): Fetches connected accounts' profiles (thorough)
    
    Both store results in ScoringStore and query it for final output.
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
        self.store = ScoringStore.get_instance()
        
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
        max_workers: int = 5,
    ) -> List[Dict]:
        """
        Discover tweets by fetching connected accounts' timelines.
        
        Thorough mode: fetches each active account's recent tweets via the
        timeline API, stores all tweets in ScoringStore, then queries for matches.
        Fetches TWEET_SCORING_FETCH_DAYS (1 day) of history per account.
        
        Args:
            tag: Optional tag/hashtag to filter for
            qrt: Optional tweet ID that must be quoted
            start_date: Start date for tweet window
            end_date: End date for tweet window
            max_workers: Concurrent timeline fetches
        
        Returns:
            List of matching tweets from active accounts (from store)
        """
        if not tag and not qrt:
            raise ValueError("At least one of 'tag' or 'qrt' must be provided")
        
        # Use a separate client for timeline fetching (posts_only=True for speed)
        timeline_client = TwitterClient(posts_only=True)
        
        accounts = list(self.active_accounts)
        bt.logging.info(
            f"Thorough discovery: fetching timelines for {len(accounts)} accounts "
            f"({max_workers} workers)"
        )
        
        # Fetch timelines concurrently
        all_tweets = []
        failed = 0
        
        def fetch_timeline(username: str) -> List[Dict]:
            result = timeline_client.fetch_user_tweets(username, fetch_days=TWEET_SCORING_FETCH_DAYS)
            return result.get('tweets', [])
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(fetch_timeline, username): username
                for username in accounts
            }
            for future in as_completed(futures):
                username = futures[future]
                try:
                    tweets = future.result()
                    all_tweets.extend(tweets)
                except Exception as e:
                    failed += 1
                    bt.logging.warning(f"Failed to fetch timeline for @{username}: {e}")
        
        # Filter to brief date range before storing (timelines return all cached tweets)
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
                date_filtered.append(tweet)  # Include unparseable dates (permissive)
        
        bt.logging.info(
            f"Fetched {len(all_tweets)} total tweets from {len(accounts)} accounts "
            f"({failed} failed), {len(date_filtered)} within brief dates"
        )
        
        # Store only date-relevant tweets in ScoringStore
        if date_filtered:
            stats = self.store.store_tweets(date_filtered)
            bt.logging.info(
                f"Timeline tweets stored: {stats['new']} new, {stats['updated']} updated"
            )
        
        # Query store for matching tweets (same as search-based flow)
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
    
    def get_engagements_batch(
        self,
        tweets: List[Dict],
        excluded_engagers: Optional[Set[str]] = None
    ) -> Dict[str, Dict[str, str]]:
        """
        Get engagements for multiple tweets concurrently.
        
        Fetches RT/QRT data for multiple tweets in parallel, then builds
        engagement maps from the store.
        
        Args:
            tweets: List of tweet dicts
            excluded_engagers: Usernames to exclude
        
        Returns:
            Dict mapping tweet_id -> {username -> engagement_type}
        """
        excluded = {e.lower() for e in (excluded_engagers or set())}
        valid_tweets = [t for t in tweets if t.get('tweet_id')]
        
        # Fetch all engagements concurrently
        bt.logging.info(
            f"Fetching engagements for {len(valid_tweets)} tweets "
            f"({ENGAGEMENT_MAX_WORKERS} workers)"
        )
        
        with ThreadPoolExecutor(max_workers=ENGAGEMENT_MAX_WORKERS) as executor:
            futures = {
                executor.submit(self._fetch_engagements, t['tweet_id']): t
                for t in valid_tweets
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    tweet = futures[future]
                    bt.logging.warning(
                        f"Error fetching engagements for {tweet.get('tweet_id')}: {e}"
                    )
        
        # Build engagement maps from store (fast, no API calls)
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
