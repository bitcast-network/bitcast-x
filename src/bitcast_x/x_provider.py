"""Normalized X data boundary and async Desearch implementation."""

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

_RETRYABLE = {408, 429, 500, 502, 503, 504}


class Tweet(BaseModel):
    """Stable normalized tweet evidence consumed by validation and legacy scoring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tweet_id: str = Field(pattern=r"^[0-9]+$")
    author_x_id: str = Field(pattern=r"^[0-9]+$")
    created_at: datetime
    text: str
    author: str
    tagged_accounts: tuple[str, ...] = ()
    retweeted_user: str | None = None
    retweeted_tweet_id: str | None = None
    quoted_user: str | None = None
    quoted_tweet_id: str | None = None
    lang: str = "und"
    favorite_count: int = Field(default=0, ge=0)
    retweet_count: int = Field(default=0, ge=0)
    reply_count: int = Field(default=0, ge=0)
    quote_count: int = Field(default=0, ge=0)
    bookmark_count: int = Field(default=0, ge=0)
    views_count: int = Field(default=0, ge=0)
    in_reply_to_status_id: str | None = None
    in_reply_to_user: str | None = None

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Require an aware provider timestamp and normalize it to UTC."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("tweet created_at must be timezone-aware")
        return value.astimezone(UTC)


class TweetFetch(BaseModel):
    """Distinguish authoritative absence from an unavailable provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tweet: Tweet | None
    provider_available: bool


class EngagementFetch(BaseModel):
    """Normalized max-one-contribution engagement map for v2 scoring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engagements: dict[str, str]
    provider_available: bool


class TweetSearchFetch(BaseModel):
    """Bounded normalized search results with explicit provider availability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tweets: tuple[Tweet, ...]
    provider_available: bool


class XProvider(Protocol):
    """Fetch operations only; validation never depends on provider wire shapes."""

    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch: ...

    async def fetch_engagements(self, tweet_id: str) -> EngagementFetch: ...

    async def search_tweets(self, query: str, *, count: int = 100) -> TweetSearchFetch: ...

    async def fetch_replies(self, tweet_id: str, *, count: int = 100) -> TweetSearchFetch: ...

    async def close(self) -> None: ...


class DesearchProvider:
    """Fetch exact posts from Desearch and normalize them onto immutable evidence."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.desearch.ai",
        timeout: float = 30.0,
        attempts: int = 3,
        retry_delay: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Desearch API key cannot be empty")
        if attempts <= 0 or retry_delay < 0:
            raise ValueError("Desearch retry settings are invalid")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._attempts = attempts
        self._retry_delay = retry_delay
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(trust_env=False)
        self._headers = {"Authorization": api_key.strip()}

    async def close(self) -> None:
        """Close an internally owned HTTP pool."""

        if self._owns_client:
            await self._client.aclose()

    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        """Fetch one exact post, retrying only transport and transient server failures."""

        for attempt in range(self._attempts):
            try:
                response = await self._client.get(
                    f"{self._base_url}/twitter/post",
                    params={"id": tweet_id},
                    headers=self._headers,
                    timeout=self._timeout,
                )
            except httpx.HTTPError:
                if attempt + 1 == self._attempts:
                    return TweetFetch(tweet=None, provider_available=False)
                await asyncio.sleep(self._retry_delay * (2**attempt))
                continue
            if response.status_code in _RETRYABLE:
                if attempt + 1 == self._attempts:
                    return TweetFetch(tweet=None, provider_available=False)
                await asyncio.sleep(self._retry_delay * (2**attempt))
                continue
            if response.status_code == 404:
                return TweetFetch(tweet=None, provider_available=True)
            try:
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                return TweetFetch(tweet=None, provider_available=False)
            if isinstance(payload, list):
                payload = payload[0] if payload else None
            if not isinstance(payload, dict) or not payload:
                return TweetFetch(tweet=None, provider_available=True)
            tweet = _parse_desearch_tweet(payload)
            return TweetFetch(tweet=tweet, provider_available=tweet is not None)
        return TweetFetch(tweet=None, provider_available=False)

    async def fetch_engagements(self, tweet_id: str) -> EngagementFetch:
        """Fetch retweeters and quotes concurrently; quotes override retweets as in v2."""

        retweeters_result, quotes_result = await asyncio.gather(
            self._request_json("/twitter/post/retweeters", {"id": tweet_id}),
            self._request_json(
                "/twitter",
                {"query": f"quoted_tweet_id:{tweet_id}", "sort": "Latest", "count": 100},
            ),
        )
        retweeters, retweeters_available = retweeters_result
        quotes, quotes_available = quotes_result
        if not retweeters_available or not quotes_available:
            return EngagementFetch(engagements={}, provider_available=False)
        engagements: dict[str, str] = {}
        users = retweeters.get("users", []) if isinstance(retweeters, dict) else []
        for user in users:
            if not isinstance(user, dict):
                continue
            username = str(user.get("username") or user.get("screen_name") or "").lower()
            if username:
                engagements[username] = "retweet"
        quote_items = (
            quotes
            if isinstance(quotes, list)
            else (quotes.get("tweets", []) if isinstance(quotes, dict) else [])
        )
        for payload in quote_items:
            if not isinstance(payload, dict):
                continue
            parsed = _parse_desearch_tweet(payload)
            if parsed is not None and parsed.quoted_tweet_id == tweet_id:
                engagements[parsed.author] = "quote"
        return EngagementFetch(engagements=engagements, provider_available=True)

    async def search_tweets(self, query: str, *, count: int = 100) -> TweetSearchFetch:
        """Search campaign posts using the frozen v2 query boundary."""

        if not query.strip() or not 1 <= count <= 100:
            raise ValueError("tweet search query/count is invalid")
        payload, available = await self._request_json(
            "/twitter", {"query": query, "sort": "Latest", "count": count}
        )
        if not available:
            return TweetSearchFetch(tweets=(), provider_available=False)
        items = (
            payload
            if isinstance(payload, list)
            else (payload.get("tweets", []) if isinstance(payload, dict) else [])
        )
        tweets = tuple(
            parsed
            for item in items
            if isinstance(item, dict) and (parsed := _parse_desearch_tweet(item)) is not None
        )
        return TweetSearchFetch(tweets=tweets, provider_available=True)

    async def fetch_replies(self, tweet_id: str, *, count: int = 100) -> TweetSearchFetch:
        """Fetch replies through the endpoint used by the legacy validator."""

        if not tweet_id.isdigit() or not 1 <= count <= 100:
            raise ValueError("reply tweet ID/count is invalid")
        payload, available = await self._request_json(
            "/twitter/replies/post", {"post_id": tweet_id, "count": count}
        )
        if not available:
            return TweetSearchFetch(tweets=(), provider_available=False)
        items = (
            payload
            if isinstance(payload, list)
            else (payload.get("tweets", []) if isinstance(payload, dict) else [])
        )
        tweets = tuple(
            parsed
            for item in items
            if isinstance(item, dict) and (parsed := _parse_desearch_tweet(item)) is not None
        )
        return TweetSearchFetch(tweets=tweets, provider_available=True)

    async def _request_json(
        self, path: str, params: dict[str, Any]
    ) -> tuple[dict[str, Any] | list[Any] | None, bool]:
        for attempt in range(self._attempts):
            try:
                response = await self._client.get(
                    f"{self._base_url}{path}",
                    params=params,
                    headers=self._headers,
                    timeout=self._timeout,
                )
            except httpx.HTTPError:
                response = None
            if response is not None and response.status_code not in _RETRYABLE:
                try:
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError):
                    return None, False
                return payload if isinstance(payload, (dict, list)) else None, True
            if attempt + 1 < self._attempts:
                await asyncio.sleep(self._retry_delay * (2**attempt))
        return None, False


def _parse_desearch_tweet(payload: dict[str, Any]) -> Tweet | None:
    """Map the proven v2 Desearch shape while requiring immutable author identity."""

    user = payload.get("user") or {}
    if not isinstance(user, dict):
        return None
    tweet_id = str(payload.get("id") or "")
    text = str(payload.get("text") or "")
    author_x_id = str(user.get("id") or user.get("rest_id") or "")
    author = str(user.get("username") or user.get("screen_name") or "").lower()
    created_at = _parse_timestamp(payload.get("created_at"))
    if (
        not tweet_id.isdigit()
        or not author_x_id.isdigit()
        or not text
        or not author
        or created_at is None
    ):
        return None
    retweet = payload.get("retweet") or {}
    quote = payload.get("quote") or {}
    entities = payload.get("entities") or {}
    mentions = entities.get("user_mentions") or [] if isinstance(entities, dict) else []
    return Tweet(
        tweet_id=tweet_id,
        author_x_id=author_x_id,
        created_at=created_at,
        text=text,
        author=author,
        tagged_accounts=tuple(
            str(mention.get("screen_name")).lower()
            for mention in mentions
            if isinstance(mention, dict) and mention.get("screen_name")
        ),
        retweeted_user=_nested_username(retweet),
        retweeted_tweet_id=_optional_numeric(retweet.get("id")) if retweet else None,
        quoted_user=_nested_username(quote),
        quoted_tweet_id=_optional_numeric(payload.get("quoted_status_id")),
        lang=str(payload.get("lang") or "und"),
        favorite_count=int(payload.get("like_count") or 0),
        retweet_count=int(payload.get("retweet_count") or 0),
        reply_count=int(payload.get("reply_count") or 0),
        quote_count=int(payload.get("quote_count") or 0),
        bookmark_count=int(payload.get("bookmark_count") or 0),
        views_count=int(payload.get("view_count") or payload.get("views_count") or 0),
        in_reply_to_status_id=_optional_numeric(payload.get("in_reply_to_status_id")),
        in_reply_to_user=(str(payload.get("in_reply_to_screen_name") or "").lower() or None),
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        if value.endswith("Z") or (
            len(value) > 10 and value[4] == "-" and value[7] == "-" and value[10] == "T"
        ):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").astimezone(UTC)
    except ValueError:
        return None


def _nested_username(value: Any) -> str | None:
    if not isinstance(value, dict) or not isinstance(value.get("user"), dict):
        return None
    user = value["user"]
    return str(user.get("username") or user.get("screen_name") or "").lower() or None


def _optional_numeric(value: Any) -> str | None:
    rendered = str(value or "")
    return rendered if rendered.isdigit() else None
