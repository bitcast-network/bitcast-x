"""Live v2-compatible connection reply and fast-track collection."""

import logging
from collections.abc import Callable
from math import log10
from typing import Any

import httpx

from bitcast_x.campaigns import CampaignFeed, EcosystemMap
from bitcast_x.errors import ReconciliationUnavailableError
from bitcast_x.legacy.connections import ConnectionStore
from bitcast_x.legacy.tags import extract_tags, sanitize_referral
from bitcast_x.x_provider import Tweet, XProvider

FAST_TRACK_MAX_IDS = 1000
LOGGER = logging.getLogger(__name__)


def referral_reward(followers: int, influence: float, max_amount: float) -> float:
    """Freeze v2's 80% follower / 20% influence log-scaled reward."""

    follower_raw = max_amount * log10(max(followers, 1000) / 1000) / log10(25_000 / 1000)
    influence_raw = max_amount * log10(max(influence, 1)) / log10(1000)
    return round(
        0.8 * max(0.0, min(follower_raw, max_amount))
        + 0.2 * max(0.0, min(influence_raw, max_amount)),
        2,
    )


class LegacyConnectionCollector:
    """Continuously ingest registrations into the imported frozen database."""

    def __init__(
        self,
        store: ConnectionStore,
        x_provider: XProvider,
        *,
        connection_tweet_ids: tuple[str, ...],
        fasttrack_url: str,
        tweet_merger: Callable[[tuple[Tweet, ...]], None] | None = None,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = store
        self._x = x_provider
        self._tweet_ids = connection_tweet_ids
        self._fasttrack_url = fasttrack_url
        self._tweet_merger = tweet_merger
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(trust_env=False)
        self._processed_fasttrack_ids = {item.tweet_id for item in store.all()}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def collect(self, feed: CampaignFeed) -> int:
        """Process direct registration replies and bounded fast-track IDs."""

        tweets: dict[str, Tweet] = {}
        parent_ids = set(self._tweet_ids)
        for parent_id in self._tweet_ids:
            search = await self._x.fetch_replies(parent_id, count=100)
            if not search.provider_available:
                raise ReconciliationUnavailableError(
                    f"legacy connection reply search unavailable for {parent_id}"
                )
            tweets.update(
                (tweet.tweet_id, tweet)
                for tweet in search.tweets
                if tweet.in_reply_to_status_id in parent_ids
            )
        for tweet_id in await self._fasttrack_ids():
            fetched = await self._x.fetch_tweet_by_id(tweet_id)
            if not fetched.provider_available:
                LOGGER.warning(
                    "legacy fast-track tweet unavailable; deferring tweet_id=%s",
                    tweet_id,
                )
                continue
            if fetched.tweet is not None:
                self._merge_tweet(fetched.tweet)
                tweets[fetched.tweet.tweet_id] = fetched.tweet
        return sum(
            self._process(tweet, feed)
            for tweet in sorted(
                tweets.values(), key=lambda item: (item.created_at, int(item.tweet_id))
            )
        )

    async def collect_fasttrack(self, feed: CampaignFeed) -> int:
        """Process only new miner-submitted IDs; unavailable IDs remain retryable."""

        changed = 0
        for tweet_id in await self._fasttrack_ids():
            if tweet_id in self._processed_fasttrack_ids:
                continue
            fetched = await self._x.fetch_tweet_by_id(tweet_id)
            if not fetched.provider_available:
                LOGGER.warning(
                    "legacy fast-track tweet unavailable; deferring tweet_id=%s",
                    tweet_id,
                )
                continue
            if fetched.tweet is None:
                LOGGER.warning(
                    "legacy fast-track tweet missing; deferring tweet_id=%s",
                    tweet_id,
                )
                continue
            self._merge_tweet(fetched.tweet)
            changed += self._process(fetched.tweet, feed)
            self._processed_fasttrack_ids.add(tweet_id)
        return changed

    def _merge_tweet(self, tweet: Tweet) -> None:
        """Inject a fast-tracked tweet into the cumulative campaign candidate store.

        v2's ``scoring/fasttrack.py::fasttrack_tweet`` called
        ``store.store_tweet(tweet)`` for every fast-tracked ID, so a creator
        submission entered the campaign candidate set even when the campaign tag
        search never returned it. Desearch's search index is not exhaustive —
        tweet 2087483233998889282 mentions ``@ProblyHQ`` and is fetchable by ID,
        but is returned by no ``@ProblyHQ`` search — so this is the only route
        those tweets have.
        """

        if self._tweet_merger is None:
            return
        self._tweet_merger((tweet,))

    async def _fasttrack_ids(self) -> tuple[str, ...]:
        try:
            response = await self._client.get(self._fasttrack_url, timeout=self._timeout)
            response.raise_for_status()
            body: Any = response.json()
        except (httpx.HTTPError, ValueError):
            return ()
        data = body.get("data", []) if isinstance(body, dict) else []
        return tuple(str(item) for item in data[:FAST_TRACK_MAX_IDS] if str(item).isdigit())

    def _process(self, tweet: Tweet, feed: CampaignFeed) -> int:
        if tweet.retweeted_user is not None:
            return 0
        maps = self._author_maps(tweet.author, feed)
        if not maps:
            return 0
        latest_maps = self._latest_maps(feed)
        all_handles = {
            account.username.casefold()
            for ecosystem in latest_maps
            for account in ecosystem.accounts
        }
        changed = 0
        for parsed in extract_tags(tweet.text):
            referred_by, referral_code = sanitize_referral(
                tweet.author, parsed.referred_by, parsed.referral_code
            )
            if referred_by is not None and referred_by not in all_handles:
                referred_by, referral_code = None, None
            amount = (
                max(self._reward_for_map(tweet.author, ecosystem) for ecosystem in maps)
                if referred_by
                else 0.0
            )
            self._store.upsert(
                tweet_id=tweet.tweet_id,
                tag=parsed.full_tag,
                account_username=tweet.author,
                referral_code=referral_code,
                referred_by=referred_by,
                referee_amount=amount,
                referrer_amount=amount,
            )
            changed += 1
        return changed

    @staticmethod
    def _author_maps(author: str, feed: CampaignFeed) -> tuple[EcosystemMap, ...]:
        normalized = author.casefold()
        return tuple(
            ecosystem
            for ecosystem in LegacyConnectionCollector._latest_maps(feed)
            if any(account.username.casefold() == normalized for account in ecosystem.accounts)
        )

    @staticmethod
    def _latest_maps(feed: CampaignFeed) -> tuple[EcosystemMap, ...]:
        latest: dict[str, EcosystemMap] = {}
        for ecosystem in feed.ecosystem_maps:
            existing = latest.get(ecosystem.ecosystem_id)
            if existing is None or ecosystem.updated_at > existing.updated_at:
                latest[ecosystem.ecosystem_id] = ecosystem
        return tuple(latest[key] for key in sorted(latest))

    @staticmethod
    def _reward_for_map(author: str, ecosystem: EcosystemMap) -> float:
        account = next(
            item for item in ecosystem.accounts if item.username.casefold() == author.casefold()
        )
        return referral_reward(
            account.followers_count, account.influence, ecosystem.max_referral_amount
        )
