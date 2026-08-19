"""Persistent, rate-bounded X evidence for replaceable pre-close previews."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from diskcache import Cache  # type: ignore[import-untyped]

from bitcast_x.errors import ProtocolError
from bitcast_x.x_provider import (
    EngagementFetch,
    Tweet,
    TweetFetch,
    TweetSearchFetch,
    XProvider,
)

LOGGER = logging.getLogger(__name__)

_UNAVAILABLE_RETRY = timedelta(minutes=1)
_NEW_TWEET_REFRESH = timedelta(hours=1)
_RECENT_TWEET_REFRESH = timedelta(hours=4)
_OLD_TWEET_REFRESH = timedelta(hours=24)
_COUNTERS = (
    "favorite_count",
    "retweet_count",
    "reply_count",
    "quote_count",
    "bookmark_count",
    "views_count",
)


@dataclass(frozen=True, slots=True)
class PreviewTweetEvidence:
    """Durable mutable tweet evidence used only by pre-close previews."""

    result: TweetFetch
    refreshed_at: datetime | None
    attempted_at: datetime
    last_attempt_available: bool


@dataclass(frozen=True, slots=True)
class PreviewEngagementEvidence:
    """Durable cumulative engagement evidence used only by pre-close previews."""

    result: EngagementFetch
    refreshed_at: datetime | None
    attempted_at: datetime
    last_attempt_available: bool


@dataclass(frozen=True, slots=True)
class PreviewPublication:
    """Last replaceable preview publication attempt for one campaign."""

    payload_hash: str
    run_id: str
    payload: dict[str, object]
    attempted_at: datetime
    succeeded: bool


class PreviewStore:
    """Separate rollback-safe cache for replaceable preview state."""

    def __init__(self, path: Path) -> None:
        self._cache = Cache(
            directory=str(path),
            sqlite_journal_mode="truncate",
            sqlite_mmap_size=0,
            disk_pickle_protocol=4,
        )

    def close(self) -> None:
        """Flush and close the preview cache."""

        self._cache.close()

    def preview_tweet_evidence(self, tweet_id: str) -> PreviewTweetEvidence | None:
        """Return the latest effective pre-close tweet evidence."""

        value = self._cache.get(f"tweet:{tweet_id}")
        if not isinstance(value, dict):
            return None
        return PreviewTweetEvidence(
            result=TweetFetch.model_validate(value.get("result")),
            refreshed_at=_timestamp(value.get("refreshed_at")),
            attempted_at=_required_timestamp(value.get("attempted_at")),
            last_attempt_available=bool(value.get("last_attempt_available")),
        )

    def record_preview_tweet_evidence(
        self,
        tweet_id: str,
        result: TweetFetch,
        *,
        attempted_at: datetime,
    ) -> PreviewTweetEvidence:
        """Persist a preview fetch while retaining prior evidence across provider outages."""

        existing = self.preview_tweet_evidence(tweet_id)
        refreshed_at: datetime | None
        if result.provider_available:
            effective = result
            refreshed_at = attempted_at
        elif existing is not None and existing.result.provider_available:
            effective = existing.result
            refreshed_at = existing.refreshed_at
        else:
            effective = result
            refreshed_at = None
        self._cache.set(
            f"tweet:{tweet_id}",
            {
                "result": effective.model_dump(mode="json"),
                "refreshed_at": refreshed_at.isoformat() if refreshed_at is not None else None,
                "attempted_at": attempted_at.isoformat(),
                "last_attempt_available": result.provider_available,
            },
        )
        return PreviewTweetEvidence(
            result=effective,
            refreshed_at=refreshed_at,
            attempted_at=attempted_at,
            last_attempt_available=result.provider_available,
        )

    def preview_engagement_evidence(self, tweet_id: str) -> PreviewEngagementEvidence | None:
        """Return the latest cumulative pre-close engagement evidence."""

        value = self._cache.get(f"engagements:{tweet_id}")
        if not isinstance(value, dict):
            return None
        return PreviewEngagementEvidence(
            result=EngagementFetch.model_validate(value.get("result")),
            refreshed_at=_timestamp(value.get("refreshed_at")),
            attempted_at=_required_timestamp(value.get("attempted_at")),
            last_attempt_available=bool(value.get("last_attempt_available")),
        )

    def record_preview_engagement_evidence(
        self,
        tweet_id: str,
        result: EngagementFetch,
        *,
        attempted_at: datetime,
    ) -> PreviewEngagementEvidence:
        """Merge cumulative preview engagements and retain them across provider outages."""

        existing = self.preview_engagement_evidence(tweet_id)
        refreshed_at: datetime | None
        if result.provider_available:
            engagements = (
                dict(existing.result.engagements)
                if existing is not None and existing.result.provider_available
                else {}
            )
            engagements.update(
                {username.casefold(): kind for username, kind in result.engagements.items()}
            )
            effective = EngagementFetch(engagements=engagements, provider_available=True)
            refreshed_at = attempted_at
        elif existing is not None and existing.result.provider_available:
            effective = existing.result
            refreshed_at = existing.refreshed_at
        else:
            effective = result
            refreshed_at = None
        self._cache.set(
            f"engagements:{tweet_id}",
            {
                "result": effective.model_dump(mode="json"),
                "refreshed_at": refreshed_at.isoformat() if refreshed_at is not None else None,
                "attempted_at": attempted_at.isoformat(),
                "last_attempt_available": result.provider_available,
            },
        )
        return PreviewEngagementEvidence(
            result=effective,
            refreshed_at=refreshed_at,
            attempted_at=attempted_at,
            last_attempt_available=result.provider_available,
        )

    def preview_publication(self, campaign_id: str) -> PreviewPublication | None:
        """Return the last replaceable preview attempt for one campaign."""

        value = self._cache.get(f"publication:{campaign_id}")
        if not isinstance(value, dict) or not isinstance(value.get("payload"), dict):
            return None
        return PreviewPublication(
            payload_hash=str(value.get("payload_hash") or ""),
            run_id=str(value.get("run_id") or ""),
            payload=value["payload"],
            attempted_at=_required_timestamp(value.get("attempted_at")),
            succeeded=bool(value.get("succeeded")),
        )

    def record_preview_publication(
        self,
        campaign_id: str,
        *,
        payload_hash: str,
        run_id: str,
        payload: dict[str, object],
        attempted_at: datetime,
        succeeded: bool,
    ) -> None:
        """Record the latest replaceable preview attempt."""

        self._cache.set(
            f"publication:{campaign_id}",
            {
                "payload_hash": payload_hash,
                "run_id": run_id,
                "payload": payload,
                "attempted_at": attempted_at.isoformat(),
                "succeeded": succeeded,
            },
        )


class PreviewXProvider:
    """Cache mutable preview evidence without affecting mandatory final fetches."""

    def __init__(
        self,
        upstream: XProvider,
        store: PreviewStore,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._upstream = upstream
        self._store = store
        self._now = now or (lambda: datetime.now(UTC))

    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        """Fetch a new/due tweet, otherwise return its durable preview evidence."""

        current = self._now()
        cached = self._store.preview_tweet_evidence(tweet_id)
        if cached is not None and not _tweet_refresh_due(cached, now=current):
            return cached.result
        fresh = await self._upstream.fetch_tweet_by_id(tweet_id)
        if fresh.provider_available and fresh.tweet is not None and cached is not None:
            fresh = TweetFetch(
                tweet=_merge_tweet(cached.result.tweet, fresh.tweet),
                provider_available=True,
            )
        effective = self._store.record_preview_tweet_evidence(
            tweet_id,
            fresh,
            attempted_at=current,
        )
        LOGGER.info(
            "preview tweet evidence refreshed tweet_id=%s available=%s found=%s",
            tweet_id,
            fresh.provider_available,
            fresh.tweet is not None,
        )
        return effective.result

    async def fetch_engagements(self, tweet_id: str) -> EngagementFetch:
        """Fetch new/due engagement identities, preserving the cumulative set."""

        current = self._now()
        cached = self._store.preview_engagement_evidence(tweet_id)
        tweet = self._store.preview_tweet_evidence(tweet_id)
        if cached is not None and not _engagement_refresh_due(cached, tweet, now=current):
            return cached.result
        fresh = await self._upstream.fetch_engagements(tweet_id)
        effective = self._store.record_preview_engagement_evidence(
            tweet_id,
            fresh,
            attempted_at=current,
        )
        LOGGER.info(
            "preview engagement evidence refreshed tweet_id=%s available=%s engagements=%s",
            tweet_id,
            fresh.provider_available,
            len(effective.result.engagements),
        )
        return effective.result

    async def search_tweets(self, query: str, *, count: int = 100) -> TweetSearchFetch:
        """Delegate searches; preclaim preview reconciliation does not call this path."""

        return await self._upstream.search_tweets(query, count=count)

    async def fetch_replies(self, tweet_id: str, *, count: int = 100) -> TweetSearchFetch:
        """Delegate replies; preclaim preview reconciliation does not call this path."""

        return await self._upstream.fetch_replies(tweet_id, count=count)

    async def close(self) -> None:
        """Leave lifecycle ownership with the final-evidence provider."""


def _tweet_refresh_due(record: PreviewTweetEvidence, *, now: datetime) -> bool:
    if not record.last_attempt_available and now - record.attempted_at < _UNAVAILABLE_RETRY:
        return False
    tweet = record.result.tweet
    if not record.result.provider_available or tweet is None or record.refreshed_at is None:
        return now - record.attempted_at >= _UNAVAILABLE_RETRY
    return now - record.refreshed_at >= _refresh_interval(tweet, now=now)


def _engagement_refresh_due(
    record: PreviewEngagementEvidence,
    tweet_record: PreviewTweetEvidence | None,
    *,
    now: datetime,
) -> bool:
    if not record.last_attempt_available and now - record.attempted_at < _UNAVAILABLE_RETRY:
        return False
    if not record.result.provider_available or record.refreshed_at is None:
        return now - record.attempted_at >= _UNAVAILABLE_RETRY
    tweet = tweet_record.result.tweet if tweet_record is not None else None
    interval = _refresh_interval(tweet, now=now) if tweet is not None else _NEW_TWEET_REFRESH
    return now - record.refreshed_at >= interval


def _refresh_interval(tweet: Tweet, *, now: datetime) -> timedelta:
    age = max(now - tweet.created_at, timedelta())
    if age < timedelta(hours=1):
        return _NEW_TWEET_REFRESH
    if age < timedelta(hours=24):
        return _RECENT_TWEET_REFRESH
    return _OLD_TWEET_REFRESH


def _merge_tweet(existing: Tweet | None, fresh: Tweet) -> Tweet:
    if existing is None:
        return fresh
    return fresh.model_copy(
        update={field: max(getattr(existing, field), getattr(fresh, field)) for field in _COUNTERS}
    )


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _required_timestamp(value: Any) -> datetime:
    parsed = _timestamp(value)
    if parsed is None:
        raise ProtocolError("stored preview timestamp is invalid")
    return parsed
