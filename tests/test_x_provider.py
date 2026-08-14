"""Tests for independent normalized X evidence fetching."""

from datetime import UTC, datetime

import httpx
import pytest

from bitcast_x.x_provider import DesearchProvider


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

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DesearchProvider("secret", client=client)
    try:
        result = await provider.fetch_tweet_by_id("123")
    finally:
        await client.aclose()

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

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await DesearchProvider("secret", client=client).fetch_tweet_by_id("123")
    finally:
        await client.aclose()

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

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await DesearchProvider("secret", client=client).fetch_tweet_by_id("123")
    finally:
        await client.aclose()

    assert result.provider_available is False
    assert result.tweet is None


@pytest.mark.asyncio
async def test_404_is_authoritative_absence_but_429_is_unavailable() -> None:
    statuses = iter([404, 429])

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DesearchProvider("secret", client=client, attempts=1)
    try:
        missing = await provider.fetch_tweet_by_id("123")
        unavailable = await provider.fetch_tweet_by_id("124")
    finally:
        await client.aclose()

    assert missing.provider_available is True and missing.tweet is None
    assert unavailable.provider_available is False and unavailable.tweet is None


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

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await DesearchProvider("secret", client=client).fetch_engagements("123")
    finally:
        await client.aclose()

    assert result.provider_available is True
    assert result.engagements == {"alice": "quote", "bob": "retweet"}


@pytest.mark.asyncio
async def test_replies_use_legacy_endpoint_and_normalize_results() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/twitter/replies/post"
        assert request.url.params["post_id"] == "123"
        assert request.url.params["count"] == "100"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "501",
                    "created_at": "2026-08-05T12:00:00Z",
                    "text": "Stitch3-builder",
                    "in_reply_to_status_id": "123",
                    "user": {"id": "1", "username": "Alice"},
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await DesearchProvider("secret", client=client).fetch_replies("123")
    finally:
        await client.aclose()

    assert result.provider_available is True
    assert result.tweets[0].in_reply_to_status_id == "123"
