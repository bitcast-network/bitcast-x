"""Tests for persistent, tiered pre-close preview evidence."""

import pickle
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bitcast_x.validator.preview import (
    PreviewStore,
    PreviewXProvider,
    _refresh_interval,
    import_legacy_preview_cache,
)
from bitcast_x.x_provider import EngagementFetch, Tweet, TweetFetch

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


class Provider:
    def __init__(self, *, tweet_age: timedelta = timedelta(hours=12)) -> None:
        self.tweet_fetches = 0
        self.engagement_fetches = 0
        self.engagement_tweet_ids: list[str] = []
        self.available = True
        self.tweet_age = tweet_age

    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        self.tweet_fetches += 1
        if not self.available:
            return TweetFetch(tweet=None, provider_available=False)
        return TweetFetch(
            tweet=Tweet(
                tweet_id=tweet_id,
                author_x_id="1",
                created_at=NOW - self.tweet_age,
                text="post",
                author="alice",
                views_count=self.tweet_fetches,
            ),
            provider_available=True,
        )

    async def fetch_engagements(self, tweet_id: str) -> EngagementFetch:
        self.engagement_fetches += 1
        self.engagement_tweet_ids.append(tweet_id)
        if not self.available:
            return EngagementFetch(engagements={}, provider_available=False)
        username = "bob" if self.engagement_fetches > 1 else "alice"
        return EngagementFetch(
            engagements={username: "quote"},
            provider_available=True,
        )

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


async def test_preview_evidence_is_cached_then_refreshed_and_merged(tmp_path: Path) -> None:
    current = [NOW]
    upstream = Provider()
    provider = PreviewXProvider(
        upstream,
        PreviewStore(tmp_path / "preview.sqlite3"),
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


async def test_featured_tweet_engagements_refresh_hourly_without_refreshing_other_old_tweets(
    tmp_path: Path,
) -> None:
    current = [NOW]
    upstream = Provider(tweet_age=timedelta(days=2))
    provider = PreviewXProvider(
        upstream,
        PreviewStore(tmp_path / "preview.sqlite3"),
        now=lambda: current[0],
    )

    await provider.fetch_tweet_by_id("101")
    await provider.fetch_engagements("101")
    await provider.fetch_tweet_by_id("202")
    await provider.fetch_engagements("202")
    provider.set_featured_tweet_ids({"101"})

    current[0] += timedelta(hours=1)
    await provider.fetch_engagements("101")
    await provider.fetch_engagements("202")

    assert upstream.engagement_tweet_ids == ["101", "202", "101"]

    provider.set_featured_tweet_ids(set())
    current[0] += timedelta(hours=1)
    await provider.fetch_engagements("101")

    assert upstream.engagement_tweet_ids == ["101", "202", "101"]


async def test_preview_outage_reuses_evidence_and_retries_once_per_minute(tmp_path: Path) -> None:
    current = [NOW]
    upstream = Provider()
    provider = PreviewXProvider(
        upstream,
        PreviewStore(tmp_path / "preview.sqlite3"),
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


async def test_unreadable_preview_entries_are_fetched_again(tmp_path: Path) -> None:
    store = PreviewStore(tmp_path / "preview.sqlite3")
    # Entries written by another release: a model field this release does not
    # know, and a timestamp it cannot parse.
    store._set(
        "tweet:1",
        {
            "result": {"tweet": None, "provider_available": True, "retired_field": 1},
            "refreshed_at": NOW.isoformat(),
            "attempted_at": NOW.isoformat(),
            "last_attempt_available": True,
        },
    )
    store._set(
        "engagements:1",
        {
            "result": {"engagements": {}, "provider_available": True},
            "refreshed_at": None,
            "attempted_at": "not-a-timestamp",
            "last_attempt_available": True,
        },
    )
    store._set("publication:campaign", {"payload": {}, "attempted_at": None})
    upstream = Provider()
    provider = PreviewXProvider(upstream, store, now=lambda: NOW)

    tweet = await provider.fetch_tweet_by_id("1")
    engagements = await provider.fetch_engagements("1")

    assert tweet.tweet is not None
    assert engagements.engagements == {"alice": "quote"}
    assert (upstream.tweet_fetches, upstream.engagement_fetches) == (1, 1)
    assert store.preview_publication("campaign") is None


def _write_legacy_cache(directory: Path, rows: list[tuple[str, str | None, bytes | None]]) -> None:
    """Write ``(key, value file, inline value)`` rows as earlier releases' diskcache did."""

    directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(directory / "cache.db")) as database, database:
        database.execute(
            "CREATE TABLE Cache (rowid INTEGER PRIMARY KEY, key BLOB, raw INTEGER, "
            "store_time REAL, expire_time REAL, access_time REAL, access_count INTEGER "
            "DEFAULT 0, tag BLOB, size INTEGER DEFAULT 0, mode INTEGER DEFAULT 0, "
            "filename TEXT, value BLOB)"
        )
        database.executemany(
            "INSERT INTO Cache (key, raw, mode, filename, value) VALUES (?, 1, 4, ?, ?)", rows
        )


def _pickled_evidence(result: dict[str, object]) -> bytes:
    """Return preview evidence as earlier releases pickled it, refreshed at NOW."""

    return pickle.dumps(
        {
            "result": result,
            "refreshed_at": NOW.isoformat(),
            "attempted_at": NOW.isoformat(),
            "last_attempt_available": True,
        },
        protocol=4,
    )


TWEET = Tweet(
    tweet_id="1",
    author_x_id="1",
    created_at=NOW - timedelta(hours=12),
    text="post",
    author="alice",
    views_count=7,
)
TWEET_EVIDENCE = _pickled_evidence(
    TweetFetch(tweet=TWEET, provider_available=True).model_dump(mode="json")
)


async def test_existing_preview_cache_is_imported_without_running_stored_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    legacy = tmp_path / "preview-cache"
    (legacy / "ab").mkdir(parents=True)
    # Large values sit in a separate file; neither stored code nor a file outside
    # the cache directory may ever be loaded.
    (legacy / "ab" / "1.val").write_bytes(
        _pickled_evidence({"engagements": {"bob": "quote"}, "provider_available": True})
    )
    (tmp_path / "outside.val").write_bytes(TWEET_EVIDENCE)
    publication = {
        "payload_hash": "h",
        "run_id": "r",
        "payload": {},
        "attempted_at": NOW.isoformat(),
    }
    _write_legacy_cache(
        legacy,
        [
            ("tweet:1", None, TWEET_EVIDENCE),
            ("engagements:1", "ab/1.val", None),
            ("publication:campaign", None, pickle.dumps(publication, protocol=4)),
            ("tweet:2", None, b"cbuiltins\nprint\n(Vpwned\ntR."),
            ("tweet:3", "../outside.val", None),
        ],
    )
    legacy_bytes = (legacy / "cache.db").read_bytes()
    path = tmp_path / "preview.sqlite3"

    import_legacy_preview_cache(path, legacy)
    store = PreviewStore(path)
    upstream = Provider()
    provider = PreviewXProvider(upstream, store, now=lambda: NOW)

    assert (await provider.fetch_tweet_by_id("1")).tweet == TWEET
    assert (await provider.fetch_engagements("1")).engagements == {"bob": "quote"}
    assert (upstream.tweet_fetches, upstream.engagement_fetches) == (0, 0)
    assert store.preview_publication("campaign") is not None
    assert store.preview_tweet_evidence("2") is None
    assert store.preview_tweet_evidence("3") is None
    assert "pwned" not in capsys.readouterr().out
    # The old cache stays intact for a rollback, and a completed import never reads it again.
    assert (legacy / "cache.db").read_bytes() == legacy_bytes
    (legacy / "cache.db").write_bytes(b"replaced after the import")
    caplog.clear()
    import_legacy_preview_cache(path, legacy)
    assert "not imported" not in caplog.text
    assert store.preview_tweet_evidence("1") is not None


def test_failed_legacy_import_runs_again_without_replacing_newer_entries(tmp_path: Path) -> None:
    legacy = tmp_path / "preview-cache"
    legacy.mkdir()
    (legacy / "cache.db").write_bytes(b"not a database")
    path = tmp_path / "preview.sqlite3"

    import_legacy_preview_cache(path, legacy)
    store = PreviewStore(path)
    store.record_preview_tweet_evidence(
        "1", TweetFetch(tweet=None, provider_available=True), attempted_at=NOW
    )
    (legacy / "cache.db").unlink()
    _write_legacy_cache(
        legacy,
        [("tweet:1", None, TWEET_EVIDENCE), ("tweet:2", None, TWEET_EVIDENCE)],
    )
    import_legacy_preview_cache(path, legacy)

    assert store.preview_tweet_evidence("2") is not None
    newer = store.preview_tweet_evidence("1")
    assert newer is not None and newer.result.tweet is None


@pytest.mark.parametrize("state", ["missing", "unreadable-directory"])
def test_unusable_legacy_preview_cache_starts_empty(tmp_path: Path, state: str) -> None:
    legacy = tmp_path / "preview-cache"
    if state == "unreadable-directory":
        _write_legacy_cache(legacy, [("tweet:1", None, TWEET_EVIDENCE)])
        legacy.chmod(0)
    path = tmp_path / "preview.sqlite3"

    try:
        import_legacy_preview_cache(path, legacy)
    finally:
        if legacy.exists():
            legacy.chmod(0o700)

    assert PreviewStore(path).preview_tweet_evidence("1") is None


def test_entries_unwritten_for_the_retention_period_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "preview.sqlite3"
    import_legacy_preview_cache(path, tmp_path / "preview-cache")
    first = PreviewStore(path)
    first.record_preview_tweet_evidence(
        "closed", TweetFetch(tweet=None, provider_available=True), attempted_at=NOW
    )
    first.close()
    # Everything written so far, the import marker included, ages past the retention.
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("UPDATE preview_entries SET updated_ns = 0")

    store = PreviewStore(path)
    store.record_preview_tweet_evidence(
        "active", TweetFetch(tweet=None, provider_available=True), attempted_at=NOW
    )

    assert store.preview_tweet_evidence("closed") is None
    assert store.preview_tweet_evidence("active") is not None
    assert store._get("legacy-import") is not None
