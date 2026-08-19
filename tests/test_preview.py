"""Tests for persistent, tiered pre-close preview evidence."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bitcast_x.validator.preview import PreviewStore, PreviewXProvider, _refresh_interval
from bitcast_x.x_provider import EngagementFetch, Tweet, TweetFetch, TweetSearchFetch

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


class Provider:
    def __init__(self) -> None:
        self.tweet_fetches = 0
        self.engagement_fetches = 0
        self.available = True

    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        self.tweet_fetches += 1
        if not self.available:
            return TweetFetch(tweet=None, provider_available=False)
        return TweetFetch(
            tweet=Tweet(
                tweet_id=tweet_id,
                author_x_id="1",
                created_at=NOW - timedelta(hours=12),
                text="post",
                author="alice",
                views_count=self.tweet_fetches,
            ),
            provider_available=True,
        )

    async def fetch_engagements(self, _tweet_id: str) -> EngagementFetch:
        self.engagement_fetches += 1
        if not self.available:
            return EngagementFetch(engagements={}, provider_available=False)
        username = "bob" if self.engagement_fetches > 1 else "alice"
        return EngagementFetch(
            engagements={username: "quote"},
            provider_available=True,
        )

    async def search_tweets(self, _query: str, *, count: int = 100) -> TweetSearchFetch:
        return TweetSearchFetch(tweets=(), provider_available=True)

    async def fetch_replies(self, _tweet_id: str, *, count: int = 100) -> TweetSearchFetch:
        return TweetSearchFetch(tweets=(), provider_available=True)

    async def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("age", "expected"),
    (
        (timedelta(minutes=30), timedelta(hours=1)),
        (timedelta(hours=12), timedelta(hours=4)),
        (timedelta(days=2), timedelta(hours=24)),
    ),
)
def test_preview_refresh_interval_uses_age_tiers(age: timedelta, expected: timedelta) -> None:
    tweet = Tweet(
        tweet_id="1",
        author_x_id="1",
        created_at=NOW - age,
        text="post",
        author="alice",
    )

    assert _refresh_interval(tweet, now=NOW) == expected


@pytest.mark.asyncio
async def test_preview_evidence_is_cached_then_refreshed_and_merged(tmp_path: Path) -> None:
    current = [NOW]
    upstream = Provider()
    provider = PreviewXProvider(
        upstream,
        PreviewStore(tmp_path / "preview-cache"),
        now=lambda: current[0],
    )

    first_tweet = await provider.fetch_tweet_by_id("123")
    first_engagements = await provider.fetch_engagements("123")
    cached_tweet = await provider.fetch_tweet_by_id("123")
    cached_engagements = await provider.fetch_engagements("123")

    assert upstream.tweet_fetches == 1
    assert upstream.engagement_fetches == 1
    assert cached_tweet == first_tweet
    assert cached_engagements == first_engagements

    current[0] += timedelta(hours=4)
    refreshed_tweet = await provider.fetch_tweet_by_id("123")
    refreshed_engagements = await provider.fetch_engagements("123")

    assert upstream.tweet_fetches == 2
    assert upstream.engagement_fetches == 2
    assert refreshed_tweet.tweet is not None and refreshed_tweet.tweet.views_count == 2
    assert refreshed_engagements.engagements == {"alice": "quote", "bob": "quote"}


@pytest.mark.asyncio
async def test_preview_outage_reuses_evidence_and_retries_once_per_minute(tmp_path: Path) -> None:
    current = [NOW]
    upstream = Provider()
    provider = PreviewXProvider(
        upstream,
        PreviewStore(tmp_path / "preview-cache"),
        now=lambda: current[0],
    )
    original = await provider.fetch_tweet_by_id("123")
    upstream.available = False
    current[0] += timedelta(hours=4)

    fallback = await provider.fetch_tweet_by_id("123")
    immediate_retry = await provider.fetch_tweet_by_id("123")

    assert fallback == original
    assert immediate_retry == original
    assert upstream.tweet_fetches == 2

    current[0] += timedelta(minutes=1)
    await provider.fetch_tweet_by_id("123")

    assert upstream.tweet_fetches == 3
