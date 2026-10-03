"""Tests for independent normalized X evidence fetching."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from bitcast_x import x_provider
from bitcast_x.x_provider import DesearchProvider, TweetFetch

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


@asynccontextmanager
async def desearch(handler: Handler, **options: Any) -> AsyncIterator[DesearchProvider]:
    """Yield a provider whose HTTP traffic is served by ``handler``."""

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield DesearchProvider("secret", client=client, **options)


@pytest.mark.asyncio
async def test_desearch_maps_immutable_author_and_v2_scoring_fields() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["id"] == "123"
        assert request.headers["authorization"] == "secret"
        return httpx.Response(
            200,
            json={
                "id": "123",
                "created_at": "2026-08-05T12:00:00Z",
                "text": "Hello @Bitcast",
                "user": {"id": "456", "username": "Creator"},
                "entities": {"user_mentions": [{"screen_name": "Bitcast"}]},
                "like_count": 10,
                "retweet_count": 2,
                "reply_count": 3,
                "quote_count": 4,
                "bookmark_count": 5,
                "view_count": 100,
            },
        )

    async with desearch(handler) as provider:
        result = await provider.fetch_tweet_by_id("123")

    assert result.provider_available is True
    assert result.tweet is not None
    assert result.tweet.author_x_id == "456"
    assert result.tweet.author == "creator"
    assert result.tweet.created_at == datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    assert result.tweet.tagged_accounts == ("bitcast",)
    assert result.tweet.views_count == 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        ("Thu Aug 06 18:47:13 +0000 2026", datetime(2026, 8, 6, 18, 47, 13, tzinfo=UTC)),
        ("Tue Aug 04 01:02:03 +0000 2026", datetime(2026, 8, 4, 1, 2, 3, tzinfo=UTC)),
    ],
)
async def test_desearch_parses_twitter_timestamps_starting_with_t(
    created_at: str, expected: datetime
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "123",
                "created_at": created_at,
                "text": "Hello",
                "user": {"id": "456", "username": "Creator"},
            },
        )

    async with desearch(handler) as provider:
        result = await provider.fetch_tweet_by_id("123")

    assert result.provider_available is True
    assert result.tweet is not None
    assert result.tweet.created_at == expected


@pytest.mark.asyncio
async def test_missing_author_id_is_not_accepted_as_evidence() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "123",
                "created_at": "2026-08-05T12:00:00Z",
                "text": "Hello",
                "user": {"username": "creator"},
            },
        )

    async with desearch(handler) as provider:
        result = await provider.fetch_tweet_by_id("123")

    assert result.provider_available is False
    assert result.tweet is None


@pytest.mark.asyncio
async def test_404_is_authoritative_absence_but_429_is_unavailable() -> None:
    statuses = iter([404, 429])

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses))

    async with desearch(handler, attempts=1) as provider:
        missing = await provider.fetch_tweet_by_id("123")
        unavailable = await provider.fetch_tweet_by_id("124")

    assert missing.provider_available is True and missing.tweet is None
    assert unavailable.provider_available is False and unavailable.tweet is None


@pytest.mark.asyncio
async def test_exhausted_failure_is_cached_for_ttl_and_success_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(x_provider, "time", SimpleNamespace(monotonic=lambda: clock.now))
    statuses = [500, 500, 500]
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(statuses.pop(0)) if statuses else httpx.Response(200, json={})

    async with desearch(handler, attempts=3, retry_delay=0) as provider:
        first = await provider.fetch_tweet_by_id("777")  # one full retry cycle -> cached
        clock.now += x_provider._NEGATIVE_TTL_SECONDS - 1
        second = await provider.fetch_tweet_by_id("777")  # still inside the TTL
        assert requests == 3
        clock.now += 1
        third = await provider.fetch_tweet_by_id("777")  # expired -> one real fetch
        fourth = await provider.fetch_tweet_by_id("777")  # absence is not negative-cached

    assert first == second == TweetFetch(tweet=None, provider_available=False)
    assert third == fourth == TweetFetch(tweet=None, provider_available=True)
    assert requests == 5


@pytest.mark.asyncio
async def test_negative_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(x_provider, "_NEGATIVE_CACHE_MAX", 8)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with desearch(handler, attempts=1) as provider:
        for index in range(12):
            await provider.fetch_tweet_by_id(str(index))

    assert list(provider._negative) == [str(index) for index in range(4, 12)]


@pytest.mark.asyncio
async def test_quotes_override_retweets_and_false_quote_search_hits_are_ignored() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/retweeters"):
            return httpx.Response(
                200,
                json={"users": [{"username": "Alice"}, {"username": "Bob"}]},
            )
        return httpx.Response(
            200,
            json={
                "tweets": [
                    {
                        "id": "501",
                        "created_at": "2026-08-05T12:00:00Z",
                        "text": "A real quote",
                        "quoted_status_id": "123",
                        "user": {"id": "1", "username": "Alice"},
                    },
                    {
                        "id": "502",
                        "created_at": "2026-08-05T12:00:00Z",
                        "text": "Search false positive",
                        "quoted_status_id": "999",
                        "user": {"id": "2", "username": "Mallory"},
                    },
                ]
            },
        )

    async with desearch(handler) as provider:
        result = await provider.fetch_engagements("123")

    assert result.provider_available is True
    assert result.engagements == {"alice": "quote", "bob": "retweet"}
