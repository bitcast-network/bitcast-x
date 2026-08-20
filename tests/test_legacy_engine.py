"""Local legacy discovery and connection attribution tests."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from diskcache import Cache

from bitcast_x.campaigns import CampaignFeed, CampaignRecord, EcosystemMap, SocialAccount
from bitcast_x.legacy import (
    ConnectionStore,
    LegacyAttributionEngine,
    LegacyTweetStore,
    legacy_search_queries,
)
from bitcast_x.protocol import CampaignAccess, MiningProtocol
from bitcast_x.validator.scoring import AttributionScorer, ScoredAttribution
from bitcast_x.x_provider import EngagementFetch, Tweet, TweetFetch, TweetSearchFetch

HOTKEY = "5FLSigC9H8sTAQG4q4FUFz3FK8t9vM7uU5KZJf5LrG1xVJdC"


def _feed() -> CampaignFeed:
    campaign = CampaignRecord(
        access=CampaignAccess(
            campaign_id="legacy",
            mechanism_id=1,
            mining_protocol="legacy_connection",
            scoring_close_block=10,
        ),
        title="Legacy",
        brief="Brief",
        ecosystem_id="tao",
        opens_at=datetime(2026, 8, 1, tzinfo=UTC),
        closes_at=datetime(2026, 8, 7, tzinfo=UTC),
        reward_pool_usd="700",
        tag="#legacy",
        emission_start_block=11,
        emission_end_block=100,
    )
    return CampaignFeed(
        snapshot_id="s",
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
        campaigns=(campaign,),
        ecosystem_maps=(
            EcosystemMap(
                ecosystem_id="tao",
                name="TAO",
                eligible_creator_x_ids=("1",),
                updated_at=datetime(2026, 7, 1, tzinfo=UTC),
                accounts=(SocialAccount(x_id="1", username="alice", influence=1),),
            ),
        ),
    )


class Provider:
    def __init__(self) -> None:
        self.tweet_fetches = 0
        self.engagement_fetches = 0

    async def search_tweets(self, query: str, *, count: int = 100) -> TweetSearchFetch:
        assert query == "#legacy since:2026-08-01 until:2026-08-08"
        assert count == 100
        return TweetSearchFetch(
            provider_available=True,
            tweets=(
                Tweet(
                    tweet_id="123",
                    author_x_id="1",
                    created_at=datetime(2026, 8, 2, tzinfo=UTC),
                    text="#legacy",
                    author="alice",
                ),
            ),
        )

    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        self.tweet_fetches += 1
        return TweetFetch(
            provider_available=True,
            tweet=Tweet(
                tweet_id=tweet_id,
                author_x_id="1",
                created_at=datetime(2026, 8, 2, tzinfo=UTC),
                text="#legacy",
                author="alice",
            ),
        )

    async def fetch_engagements(self, tweet_id: str) -> EngagementFetch:
        self.engagement_fetches += 1
        return EngagementFetch(engagements={}, provider_available=True)


class QuoteSearchProvider:
    """Return a quote through campaign search while engagement search misses it."""

    @staticmethod
    def original() -> Tweet:
        return Tweet(
            tweet_id="123",
            author_x_id="1",
            created_at=datetime(2026, 8, 2, tzinfo=UTC),
            text="#legacy",
            author="alice",
        )

    async def search_tweets(self, query: str, *, count: int = 100) -> TweetSearchFetch:
        assert query == "#legacy since:2026-08-01 until:2026-08-08"
        assert count == 100
        return TweetSearchFetch(
            provider_available=True,
            tweets=(
                self.original(),
                Tweet(
                    tweet_id="456",
                    author_x_id="2",
                    created_at=datetime(2026, 8, 3, tzinfo=UTC),
                    text="quote #legacy",
                    author="bob",
                    quoted_tweet_id="123",
                ),
            ),
        )

    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        assert tweet_id == "123"
        return TweetFetch(tweet=self.original(), provider_available=True)

    async def fetch_engagements(self, tweet_id: str) -> EngagementFetch:
        assert tweet_id == "123"
        return EngagementFetch(engagements={}, provider_available=True)


class UnavailableProvider:
    async def search_tweets(self, query: str, *, count: int = 100) -> TweetSearchFetch:
        assert query == "#legacy since:2026-08-01 until:2026-08-08"
        assert count == 100
        return TweetSearchFetch(provider_available=False, tweets=())


class Scorer:
    async def score(
        self,
        feed: CampaignFeed,
        attributions: list[object],
        *,
        tweet_evidence: dict[str, Tweet] | None = None,
        cached_evidence: dict[str, tuple[TweetFetch, EngagementFetch]] | None = None,
    ) -> list[ScoredAttribution]:
        self.feed = feed
        self.attributions = attributions
        self.tweet_evidence = tweet_evidence
        self.cached_evidence = cached_evidence
        return []


def _connections(path: Path) -> ConnectionStore:
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE connections (
                connection_id INTEGER PRIMARY KEY, tweet_id BIGINT NOT NULL,
                tag TEXT NOT NULL, account_username TEXT NOT NULL UNIQUE,
                added TEXT NOT NULL, updated TEXT NOT NULL, referral_code TEXT,
                referred_by TEXT, referee_amount REAL, referrer_amount REAL,
                payout_date DATE
            )
            """
        )
        db.execute("PRAGMA user_version = 2")
        db.execute(
            "INSERT INTO connections VALUES (1, 1, ?, 'alice', 'a', 'b', NULL, NULL, 0, 0, NULL)",
            (f"Stitch-hk:{HOTKEY}",),
        )
    return ConnectionStore(path)


async def test_connected_eligible_tweet_is_attributed_locally(tmp_path: Path) -> None:
    scorer = Scorer()
    store_path = tmp_path / "tweets"
    Cache(store_path).close()
    engine = LegacyAttributionEngine(
        _connections(tmp_path / "c.db"),
        Provider(),  # type: ignore[arg-type]
        scorer,  # type: ignore[arg-type]
        LegacyTweetStore(store_path),
    )
    await engine.score_feed(_feed(), block=1, hotkey_to_uid={HOTKEY: 7})
    attribution = scorer.attributions[0]
    assert attribution.miner_hotkey == HOTKEY  # type: ignore[attr-defined]
    assert scorer.tweet_evidence["123"].author == "alice"
    assert scorer.feed.campaigns[0].access.mining_protocol is MiningProtocol.LEGACY_CONNECTION


async def test_observed_quote_scores_when_provider_engagement_search_misses_it(
    tmp_path: Path,
) -> None:
    snapshot = _feed()
    snapshot = snapshot.model_copy(
        update={
            "ecosystem_maps": (
                snapshot.ecosystem_maps[0].model_copy(
                    update={
                        "accounts": (
                            SocialAccount(x_id="1", username="alice", influence=2),
                            SocialAccount(x_id="2", username="bob", influence=3),
                        ),
                    }
                ),
            )
        }
    )
    store_path = tmp_path / "tweets"
    Cache(store_path).close()
    provider = QuoteSearchProvider()
    tweet_store = LegacyTweetStore(store_path)
    scorer = AttributionScorer(
        provider,  # type: ignore[arg-type]
        engagement_merger=tweet_store.merge_engagements,
    )
    engine = LegacyAttributionEngine(
        _connections(tmp_path / "c.db"),
        provider,  # type: ignore[arg-type]
        scorer,
        tweet_store,
    )

    result = await engine.score_feed(snapshot, block=1, hotkey_to_uid={HOTKEY: 7})

    assert len(result) == 1
    assert result[0].tweet.tweet_id == "123"
    assert result[0].score == 13
    assert result[0].details[0].username == "bob"
    assert result[0].details[0].engagement_type == "quote"


def test_legacy_attribution_keeps_creator_eligible_after_rank_drop() -> None:
    snapshot = _feed()
    campaign = snapshot.campaigns[0].model_copy(update={"max_members": 1})
    old_map = snapshot.ecosystem_maps[0].model_copy(
        update={
            "eligible_creator_x_ids": ("1", "2"),
            "accounts": (
                SocialAccount(x_id="1", username="alice", influence=2),
                SocialAccount(x_id="2", username="bob", influence=1),
            ),
        }
    )
    new_map = old_map.model_copy(
        update={
            "updated_at": datetime(2026, 8, 3, tzinfo=UTC),
            "accounts": (
                SocialAccount(x_id="2", username="bob", influence=2),
                SocialAccount(x_id="1", username="alice", influence=1),
            ),
        }
    )
    tweet = Tweet(
        tweet_id="123",
        author_x_id="1",
        created_at=datetime(2026, 8, 4, tzinfo=UTC),
        text="#legacy",
        author="alice",
    )

    assert (
        LegacyAttributionEngine._eligible(
            snapshot.model_copy(
                update={"campaigns": (campaign,), "ecosystem_maps": (old_map, new_map)}
            ),
            campaign,
            tweet,
        )
        is True
    )


async def test_unavailable_search_reuses_cumulative_tweet_store(tmp_path: Path) -> None:
    scorer = Scorer()
    store_path = tmp_path / "tweets"
    Cache(store_path).close()
    connections = _connections(tmp_path / "c.db")
    tweet_store = LegacyTweetStore(store_path)

    await LegacyAttributionEngine(
        connections,
        Provider(),  # type: ignore[arg-type]
        scorer,  # type: ignore[arg-type]
        tweet_store,
    ).score_feed(_feed(), block=1, hotkey_to_uid={HOTKEY: 7})

    scorer = Scorer()
    await LegacyAttributionEngine(
        connections,
        UnavailableProvider(),  # type: ignore[arg-type]
        scorer,  # type: ignore[arg-type]
        tweet_store,
    ).score_feed(_feed(), block=1, hotkey_to_uid={HOTKEY: 7})

    assert scorer.attributions[0].tweet_id == "123"  # type: ignore[attr-defined]


def test_search_query_preserves_v2_exclusive_until_rule() -> None:
    assert legacy_search_queries(_feed().campaigns[0])[0].endswith("until:2026-08-08")


def test_search_queries_are_separate_when_tag_and_quote_are_both_configured() -> None:
    """v2 ran one search per selector and unioned them; ANDing them loses tweets."""

    campaign = _feed().campaigns[0].model_copy(update={"quoted_tweet_id": "99"})

    queries = legacy_search_queries(campaign)

    assert len(queries) == 2
    assert queries[0].startswith("quoted_tweet_id:99 since:")
    assert queries[1].startswith("#legacy since:")
    assert not any("#legacy" in query and "quoted_tweet_id" in query for query in queries)


def test_search_queries_cover_a_tag_only_campaign() -> None:
    assert legacy_search_queries(_feed().campaigns[0]) == (
        "#legacy since:2026-08-01 until:2026-08-08",
    )


class RecordingProvider:
    """Capture every query the engine issues for one campaign."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search_tweets(self, query: str, *, count: int = 100) -> TweetSearchFetch:
        self.queries.append(query)
        return TweetSearchFetch(provider_available=True, tweets=())


async def test_qrt_campaign_issues_both_v2_searches(tmp_path: Path) -> None:
    """A QRT campaign must search the quote and the tag separately, as v2 did."""

    store_path = tmp_path / "tweets"
    Cache(store_path).close()
    provider = RecordingProvider()
    feed = _feed()
    feed = feed.model_copy(
        update={
            "campaigns": (feed.campaigns[0].model_copy(update={"quoted_tweet_id": "99"}),),
        }
    )

    await LegacyAttributionEngine(
        _connections(tmp_path / "c.db"),
        provider,  # type: ignore[arg-type]
        Scorer(),  # type: ignore[arg-type]
        LegacyTweetStore(store_path),
    ).score_feed(feed, block=1, hotkey_to_uid={HOTKEY: 7})

    assert provider.queries == [
        "quoted_tweet_id:99 since:2026-08-01 until:2026-08-08",
        "#legacy since:2026-08-01 until:2026-08-08",
    ]


async def test_legacy_scoring_reuses_cache_but_refreshes_at_close(tmp_path: Path) -> None:
    store_path = tmp_path / "tweets"
    Cache(store_path).close()
    provider = Provider()
    tweet_store = LegacyTweetStore(store_path)
    scorer = AttributionScorer(
        provider,  # type: ignore[arg-type]
        engagement_merger=tweet_store.merge_engagements,
    )
    engine = LegacyAttributionEngine(
        _connections(tmp_path / "c.db"),
        provider,  # type: ignore[arg-type]
        scorer,
        tweet_store,
    )

    await engine.score_feed(_feed(), block=1, hotkey_to_uid={HOTKEY: 7})
    await engine.score_feed(_feed(), block=2, hotkey_to_uid={HOTKEY: 7})
    assert provider.tweet_fetches == 1
    assert provider.engagement_fetches == 1

    await engine.score_feed(_feed(), block=10, hotkey_to_uid={HOTKEY: 7})
    assert provider.tweet_fetches == 2
    assert provider.engagement_fetches == 2
