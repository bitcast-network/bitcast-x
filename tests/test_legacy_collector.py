"""Legacy registration intake and referral-lock parity tests."""

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from bitcast_x.campaigns import CampaignFeed, EcosystemMap, SocialAccount
from bitcast_x.legacy import ConnectionStore, LegacyConnectionCollector, referral_reward
from bitcast_x.x_provider import Tweet, TweetFetch, TweetSearchFetch

HOTKEY = "5FLSigC9H8sTAQG4q4FUFz3FK8t9vM7uU5KZJf5LrG1xVJdC"


def _store(path: Path) -> ConnectionStore:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE connections (
                connection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tweet_id BIGINT NOT NULL, tag TEXT NOT NULL,
                account_username TEXT NOT NULL UNIQUE, added TEXT NOT NULL,
                updated TEXT NOT NULL, referral_code TEXT, referred_by TEXT,
                referee_amount REAL, referrer_amount REAL, payout_date DATE
            )
            """
        )
        connection.execute("PRAGMA user_version = 2")
    return ConnectionStore(path)


def _feed() -> CampaignFeed:
    return CampaignFeed(
        snapshot_id="s",
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
        campaigns=(),
        ecosystem_maps=(
            EcosystemMap(
                ecosystem_id="tao",
                name="TAO",
                eligible_creator_x_ids=("1", "2"),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
                max_referral_amount=80,
                accounts=(
                    SocialAccount(
                        x_id="1", username="alice", influence=100, followers_count=25_000
                    ),
                    SocialAccount(x_id="2", username="bob", influence=1),
                ),
            ),
        ),
    )


class Provider:
    async def fetch_replies(self, tweet_id: str, *, count: int = 100) -> TweetSearchFetch:
        assert tweet_id == "99"
        assert count == 100
        return TweetSearchFetch(
            provider_available=True,
            tweets=(
                Tweet(
                    tweet_id="100",
                    author_x_id="1",
                    created_at=datetime(2026, 8, 2, tzinfo=UTC),
                    text=f"Stitch-hk:{HOTKEY}-Ym9i",
                    author="alice",
                    in_reply_to_status_id="99",
                ),
                Tweet(
                    tweet_id="101",
                    author_x_id="1",
                    created_at=datetime(2026, 8, 2, tzinfo=UTC),
                    text="Stitch3-not-direct",
                    author="alice",
                    in_reply_to_status_id="98",
                ),
            ),
        )

    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        assert tweet_id == "102"
        return TweetFetch(
            provider_available=True,
            tweet=Tweet(
                tweet_id="102",
                author_x_id="1",
                created_at=datetime(2026, 8, 3, tzinfo=UTC),
                text="Stitch3-builder-Ym9i",
                author="alice",
            ),
        )


async def test_reply_and_fasttrack_preserve_latest_tag_and_referral_lock(tmp_path: Path) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["102"]})

    store = _store(tmp_path / "connections.db")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = LegacyConnectionCollector(
        store,
        Provider(),  # type: ignore[arg-type]
        connection_tweet_ids=("99",),
        fasttrack_url="https://fast-track.test",
        client=client,
    )
    try:
        assert await collector.collect(_feed()) == 2
    finally:
        await client.aclose()

    connection = store.all()[0]
    assert connection.tweet_id == "102"
    assert connection.tag == "Stitch3-builder-Ym9i"
    assert connection.referred_by == "bob"
    assert connection.referee_amount == referral_reward(25_000, 100, 80)

    assert store.activate_referrals({"alice"}, today=date(2026, 8, 8)) == 1
    assert store.activate_referrals({"alice"}, today=date(2026, 8, 8)) == 0
    assert store.all()[0].payout_date == date(2026, 8, 9)

    store.upsert(
        tweet_id="103",
        tag=f"Stitch-hk:{HOTKEY}",
        account_username="alice",
        referral_code=None,
        referred_by=None,
        referee_amount=0,
        referrer_amount=0,
    )
    locked = store.all()[0]
    assert locked.tweet_id == "103"
    assert locked.referred_by == "bob"
    assert locked.referee_amount == referral_reward(25_000, 100, 80)


async def test_fasttrack_cadence_does_not_refetch_processed_ids(tmp_path: Path) -> None:
    requests = 0

    class CountingProvider(Provider):
        async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
            nonlocal requests
            requests += 1
            return await super().fetch_tweet_by_id(tweet_id)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["102"]})

    store = _store(tmp_path / "connections.db")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = LegacyConnectionCollector(
        store,
        CountingProvider(),  # type: ignore[arg-type]
        connection_tweet_ids=(),
        fasttrack_url="https://fast-track.test",
        client=client,
    )
    try:
        assert await collector.collect_fasttrack(_feed()) == 1
        assert await collector.collect_fasttrack(_feed()) == 0
    finally:
        await client.aclose()
    assert requests == 1


async def test_missing_fasttrack_tweet_remains_retryable(tmp_path: Path) -> None:
    requests = 0

    class EventuallyAvailableProvider(Provider):
        async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
            nonlocal requests
            requests += 1
            if requests == 1:
                return TweetFetch(tweet=None, provider_available=True)
            return await super().fetch_tweet_by_id(tweet_id)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["102"]})

    merged: list[Tweet] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = LegacyConnectionCollector(
        _store(tmp_path / "connections.db"),
        EventuallyAvailableProvider(),  # type: ignore[arg-type]
        connection_tweet_ids=(),
        fasttrack_url="https://fast-track.test",
        tweet_merger=lambda tweets: merged.extend(tweets),
        client=client,
    )
    try:
        assert await collector.collect_fasttrack(_feed()) == 0
        assert await collector.collect_fasttrack(_feed()) == 1
    finally:
        await client.aclose()

    assert requests == 2
    assert [tweet.tweet_id for tweet in merged] == ["102"]


async def test_unavailable_fasttrack_tweet_does_not_abort_other_intake(tmp_path: Path) -> None:
    class PartiallyUnavailableProvider(Provider):
        async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
            if tweet_id == "102":
                return TweetFetch(tweet=None, provider_available=False)
            assert tweet_id == "103"
            return TweetFetch(
                provider_available=True,
                tweet=Tweet(
                    tweet_id="103",
                    author_x_id="1",
                    created_at=datetime(2026, 8, 3, tzinfo=UTC),
                    text="Stitch3-builder-Ym9i",
                    author="alice",
                ),
            )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["102", "103"]})

    store = _store(tmp_path / "connections.db")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = LegacyConnectionCollector(
        store,
        PartiallyUnavailableProvider(),  # type: ignore[arg-type]
        connection_tweet_ids=("99",),
        fasttrack_url="https://fast-track.test",
        client=client,
    )
    try:
        assert await collector.collect(_feed()) == 2
    finally:
        await client.aclose()

    assert store.all()[0].tweet_id == "103"


async def test_fasttrack_injects_tweets_into_the_campaign_store(tmp_path: Path) -> None:
    """A creator submission must reach the candidate store even with no tag.

    v2's fast track called ``store.store_tweet`` on every fast-tracked ID, which
    is the only route into scoring for tweets Desearch's search never returns.
    """

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["102"]})

    merged: list[Tweet] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = LegacyConnectionCollector(
        _store(tmp_path / "connections.db"),
        Provider(),  # type: ignore[arg-type]
        connection_tweet_ids=("99",),
        fasttrack_url="https://fast-track.test",
        tweet_merger=lambda tweets: merged.extend(tweets),
        client=client,
    )
    try:
        await collector.collect_fasttrack(_feed())
    finally:
        await client.aclose()

    assert [tweet.tweet_id for tweet in merged] == ["102"]


async def test_fasttrack_without_a_merger_still_collects_connections(tmp_path: Path) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["102"]})

    store = _store(tmp_path / "connections.db")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = LegacyConnectionCollector(
        store,
        Provider(),  # type: ignore[arg-type]
        connection_tweet_ids=("99",),
        fasttrack_url="https://fast-track.test",
        client=client,
    )
    try:
        assert await collector.collect_fasttrack(_feed()) == 1
    finally:
        await client.aclose()

    assert store.all()[0].tweet_id == "102"
