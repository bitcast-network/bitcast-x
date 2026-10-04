"""Persistent, rate-bounded X evidence for replaceable pre-close previews."""

import io
import json
import logging
import pickle
import sqlite3
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from bitcast_x.sqlite import apply_migrations, connect
from bitcast_x.x_provider import (
    EngagementFetch,
    Tweet,
    TweetFetch,
    XProvider,
)

LOGGER = logging.getLogger(__name__)

_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS preview_entries (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL
    );
    """,
)
# diskcache's storage mode for pickled values, which every legacy preview entry used.
_DISKCACHE_PICKLE_MODE = 4

_UNAVAILABLE_RETRY = timedelta(minutes=1)
_NEW_TWEET_REFRESH = timedelta(hours=1)
_RECENT_TWEET_REFRESH = timedelta(hours=4)
_OLD_TWEET_REFRESH = timedelta(hours=24)
_FEATURED_TWEET_ENGAGEMENT_REFRESH = timedelta(hours=1)
_COUNTERS = (
    "favorite_count",
    "retweet_count",
    "reply_count",
    "quote_count",
    "bookmark_count",
    "views_count",
)


@dataclass(frozen=True, slots=True)
class PreviewEvidence[T: (TweetFetch, EngagementFetch)]:
    """Durable mutable X evidence used only by pre-close previews."""

    result: T
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
    """Separate rollback-safe store for replaceable preview state."""

    def __init__(self, path: Path, *, legacy_directory: Path | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # One connection, as every caller runs on the validator's event loop thread.
        self._connection = connect(path)
        created = int(self._connection.execute("PRAGMA user_version").fetchone()[0]) == 0
        apply_migrations(self._connection, _MIGRATIONS)
        if created and legacy_directory is not None:
            self._import_legacy(legacy_directory)

    def close(self) -> None:
        """Close the preview store."""

        self._connection.close()

    def preview_tweet_evidence(self, tweet_id: str) -> PreviewEvidence[TweetFetch] | None:
        """Return the latest effective pre-close tweet evidence."""

        return self._load_evidence(f"tweet:{tweet_id}", TweetFetch)

    def record_preview_tweet_evidence(
        self,
        tweet_id: str,
        result: TweetFetch,
        *,
        attempted_at: datetime,
    ) -> PreviewEvidence[TweetFetch]:
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
        return self._save_evidence(
            f"tweet:{tweet_id}",
            PreviewEvidence(
                result=effective,
                refreshed_at=refreshed_at,
                attempted_at=attempted_at,
                last_attempt_available=result.provider_available,
            ),
        )

    def preview_engagement_evidence(self, tweet_id: str) -> PreviewEvidence[EngagementFetch] | None:
        """Return the latest cumulative pre-close engagement evidence."""

        return self._load_evidence(f"engagements:{tweet_id}", EngagementFetch)

    def record_preview_engagement_evidence(
        self,
        tweet_id: str,
        result: EngagementFetch,
        *,
        attempted_at: datetime,
    ) -> PreviewEvidence[EngagementFetch]:
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
        return self._save_evidence(
            f"engagements:{tweet_id}",
            PreviewEvidence(
                result=effective,
                refreshed_at=refreshed_at,
                attempted_at=attempted_at,
                last_attempt_available=result.provider_available,
            ),
        )

    def preview_publication(self, campaign_id: str) -> PreviewPublication | None:
        """Return the last replaceable preview attempt for one campaign."""

        value = self._get(f"publication:{campaign_id}")
        attempted_at = _timestamp(value.get("attempted_at")) if isinstance(value, dict) else None
        if attempted_at is None or not isinstance(value.get("payload"), dict):
            return None
        return PreviewPublication(
            payload_hash=str(value.get("payload_hash") or ""),
            run_id=str(value.get("run_id") or ""),
            payload=value["payload"],
            attempted_at=attempted_at,
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

        self._set(
            f"publication:{campaign_id}",
            {
                "payload_hash": payload_hash,
                "run_id": run_id,
                "payload": payload,
                "attempted_at": attempted_at.isoformat(),
                "succeeded": succeeded,
            },
        )

    def _load_evidence[T: (TweetFetch, EngagementFetch)](
        self, key: str, model: type[T]
    ) -> PreviewEvidence[T] | None:
        # Preview evidence is replaceable. An entry this release cannot read, for
        # example after a model change, is a miss and is fetched again.
        value = self._get(key)
        if not isinstance(value, dict):
            return None
        attempted_at = _timestamp(value.get("attempted_at"))
        try:
            result = model.model_validate(value.get("result"))
        except ValidationError:
            attempted_at = None
        if attempted_at is None:
            LOGGER.warning("discarding unreadable preview evidence key=%s", key)
            return None
        return PreviewEvidence(
            result=result,
            refreshed_at=_timestamp(value.get("refreshed_at")),
            attempted_at=attempted_at,
            last_attempt_available=bool(value.get("last_attempt_available")),
        )

    def _save_evidence[T: (TweetFetch, EngagementFetch)](
        self, key: str, evidence: PreviewEvidence[T]
    ) -> PreviewEvidence[T]:
        refreshed_at = evidence.refreshed_at
        self._set(
            key,
            {
                "result": evidence.result.model_dump(mode="json"),
                "refreshed_at": refreshed_at.isoformat() if refreshed_at is not None else None,
                "attempted_at": evidence.attempted_at.isoformat(),
                "last_attempt_available": evidence.last_attempt_available,
            },
        )
        return evidence

    def _get(self, key: str) -> Any:
        row = self._connection.execute(
            "SELECT value_json FROM preview_entries WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["value_json"]) if row is not None else None

    def _set(self, key: str, value: dict[str, object]) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO preview_entries (key, value_json) VALUES (?, ?)",
            (key, json.dumps(value)),
        )

    def _import_legacy(self, directory: Path) -> None:
        """Import the diskcache entries earlier releases kept, leaving them for rollback."""

        # Remove once no validator can still upgrade from a diskcache release.
        try:
            with self._connection as connection:
                connection.execute("BEGIN IMMEDIATE")
                imported = connection.executemany(
                    "INSERT OR REPLACE INTO preview_entries (key, value_json) VALUES (?, ?)",
                    _legacy_entries(directory),
                ).rowcount
        except sqlite3.Error as exc:
            LOGGER.warning("legacy preview cache not imported from %s: %s", directory, exc)
            return
        if imported:
            LOGGER.info("imported %s legacy preview entries from %s", imported, directory)


class _PlainUnpickler(pickle.Unpickler):
    """Rebuild only plain containers and scalars, so no stored callable can run."""

    def find_class(self, module_name: str, global_name: str, /) -> Any:
        raise pickle.UnpicklingError(f"refusing to load {module_name}.{global_name}")


def _legacy_entries(directory: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(key, value_json)`` for the readable entries of a diskcache directory."""

    database = (directory / "cache.db").resolve()
    if not database.is_file():
        return
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        rows = connection.execute(
            "SELECT key, filename, value FROM Cache WHERE raw = 1 AND mode = ?",
            (_DISKCACHE_PICKLE_MODE,),
        ).fetchall()
    finally:
        connection.close()
    for key, filename, value in rows:
        try:
            if filename is not None:
                path = (database.parent / filename).resolve()
                if not path.is_relative_to(database.parent):
                    raise ValueError("value file is outside the cache directory")
                value = path.read_bytes()
            entry = json.dumps(_PlainUnpickler(io.BytesIO(value)).load())
        except Exception as exc:
            # As in the live store, an unreadable entry is a miss and is fetched again.
            LOGGER.warning("skipping unreadable legacy preview entry key=%s: %s", key, exc)
            continue
        yield str(key), entry


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
        self._featured_tweet_ids: frozenset[str] = frozenset()

    def set_featured_tweet_ids(self, tweet_ids: Collection[str]) -> None:
        """Use the featured cadence for engagement evidence from these tweets."""

        self._featured_tweet_ids = frozenset(tweet_ids)

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
        refresh_interval = (
            _FEATURED_TWEET_ENGAGEMENT_REFRESH if tweet_id in self._featured_tweet_ids else None
        )
        if cached is not None and not _engagement_refresh_due(
            cached,
            tweet,
            now=current,
            refresh_interval=refresh_interval,
        ):
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

    async def close(self) -> None:
        """Leave lifecycle ownership with the final-evidence provider."""


def _tweet_refresh_due(record: PreviewEvidence[TweetFetch], *, now: datetime) -> bool:
    if not record.last_attempt_available and now - record.attempted_at < _UNAVAILABLE_RETRY:
        return False
    tweet = record.result.tweet
    if not record.result.provider_available or tweet is None or record.refreshed_at is None:
        return now - record.attempted_at >= _UNAVAILABLE_RETRY
    return now - record.refreshed_at >= _refresh_interval(tweet, now=now)


def _engagement_refresh_due(
    record: PreviewEvidence[EngagementFetch],
    tweet_record: PreviewEvidence[TweetFetch] | None,
    *,
    now: datetime,
    refresh_interval: timedelta | None = None,
) -> bool:
    if not record.last_attempt_available and now - record.attempted_at < _UNAVAILABLE_RETRY:
        return False
    if not record.result.provider_available or record.refreshed_at is None:
        return now - record.attempted_at >= _UNAVAILABLE_RETRY
    tweet = tweet_record.result.tweet if tweet_record is not None else None
    interval = refresh_interval
    if interval is None:
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
