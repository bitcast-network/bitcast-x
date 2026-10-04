"""Tests for persistent, tiered pre-close preview evidence."""

import pickle
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bitcast_x.validator.preview import PreviewStore, PreviewXProvider, _refresh_interval
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


async def test_existing_preview_cache_is_imported_without_running_stored_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The diskcache layout earlier releases left: pickled values inline, or in a
    # separate file when large.
    legacy = tmp_path / "preview-cache"
    (legacy / "ab").mkdir(parents=True)
    tweet = Tweet(
        tweet_id="1",
        author_x_id="1",
        created_at=NOW - timedelta(hours=12),
        text="post",
        author="alice",
        views_count=7,
    )
    fresh = {
        "refreshed_at": NOW.isoformat(),
        "attempted_at": NOW.isoformat(),
        "last_attempt_available": True,
    }
    tweet_value = pickle.dumps(
        {
            **fresh,
            "result": TweetFetch(tweet=tweet, provider_available=True).model_dump(mode="json"),
        },
        protocol=4,
    )
    (legacy / "ab" / "1.val").write_bytes(
        pickle.dumps(
            {**fresh, "result": {"engagements": {"bob": "quote"}, "provider_available": True}},
            protocol=4,
        )
    )
    (tmp_path / "outside.val").write_bytes(tweet_value)
    publication = {
        "payload_hash": "h",
        "run_id": "r",
        "payload": {},
        "attempted_at": NOW.isoformat(),
    }
    database = sqlite3.connect(legacy / "cache.db")
    database.execute(
        "CREATE TABLE Cache (rowid INTEGER PRIMARY KEY, key BLOB, raw INTEGER, store_time REAL, "
        "expire_time REAL, access_time REAL, access_count INTEGER DEFAULT 0, tag BLOB, "
        "size INTEGER DEFAULT 0, mode INTEGER DEFAULT 0, filename TEXT, value BLOB)"
    )
    database.executemany(
        "INSERT INTO Cache (key, raw, mode, filename, value) VALUES (?, 1, 4, ?, ?)",
        [
            ("tweet:1", None, tweet_value),
            ("engagements:1", "ab/1.val", None),
            ("publication:campaign", None, pickle.dumps(publication, protocol=4)),
            # Neither stored code nor a file outside the cache is ever loaded.
            ("tweet:2", None, b"cbuiltins\nprint\n(Vpwned\ntR."),
            ("tweet:3", "../outside.val", None),
        ],
    )
    database.commit()
    database.close()
    legacy_bytes = (legacy / "cache.db").read_bytes()

    store = PreviewStore(tmp_path / "preview.sqlite3", legacy_directory=legacy)
    upstream = Provider()
    provider = PreviewXProvider(upstream, store, now=lambda: NOW)

    assert (await provider.fetch_tweet_by_id("1")).tweet == tweet
    assert (await provider.fetch_engagements("1")).engagements == {"bob": "quote"}
    assert (upstream.tweet_fetches, upstream.engagement_fetches) == (0, 0)
    assert store.preview_publication("campaign") is not None
    assert store.preview_tweet_evidence("2") is None
    assert store.preview_tweet_evidence("3") is None
    assert "pwned" not in capsys.readouterr().out
    # The old cache stays intact for a rollback and is imported only once.
    assert (legacy / "cache.db").read_bytes() == legacy_bytes
    store.record_preview_tweet_evidence(
        "1", TweetFetch(tweet=None, provider_available=True), attempted_at=NOW
    )
    reopened = PreviewStore(tmp_path / "preview.sqlite3", legacy_directory=legacy)
    assert reopened.preview_tweet_evidence("1") == store.preview_tweet_evidence("1")
    assert store.preview_tweet_evidence("1").result.tweet is None


@pytest.mark.parametrize("cache_db", [None, b"not a database"], ids=["missing", "corrupt"])
def test_unusable_legacy_preview_cache_starts_empty(tmp_path: Path, cache_db: bytes | None) -> None:
    legacy = tmp_path / "preview-cache"
    if cache_db is not None:
        legacy.mkdir()
        (legacy / "cache.db").write_bytes(cache_db)

    store = PreviewStore(tmp_path / "preview.sqlite3", legacy_directory=legacy)

    assert store.preview_tweet_evidence("1") is None
