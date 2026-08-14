"""Frozen v1/v2 cumulative tweet-store cutover tests."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from diskcache import Cache

from bitcast_x.campaigns import CampaignFeed, CampaignRecord, EcosystemMap, SocialAccount
from bitcast_x.errors import ProtocolError
from bitcast_x.legacy import LegacyTweetStore
from bitcast_x.protocol import CampaignAccess
from bitcast_x.x_provider import EngagementFetch, Tweet

NOW = datetime(2026, 8, 5, tzinfo=UTC)


def _campaign() -> CampaignRecord:
    return CampaignRecord(
        access=CampaignAccess(
            campaign_id="legacy",
            mechanism_id=1,
            mining_protocol="legacy_connection",
            scoring_close_block=1,
        ),
        title="legacy",
        brief="brief",
        ecosystem_id="tao",
        opens_at=NOW,
        closes_at=datetime(2026, 8, 7, tzinfo=UTC),
        reward_pool_usd="700",
        tag="#legacy",
    )


def _filtered_campaign(**updates: object) -> CampaignRecord:
    return _campaign().model_copy(update=updates)


def _feed() -> CampaignFeed:
    return CampaignFeed(
        snapshot_id="s",
        published_at=NOW,
        campaigns=(_campaign(),),
        ecosystem_maps=(
            EcosystemMap(
                ecosystem_id="tao",
                name="TAO",
                eligible_creator_x_ids=("99",),
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                accounts=(SocialAccount(x_id="99", username="alice", influence=1),),
            ),
        ),
    )


def test_imports_old_record_without_x_id_and_accumulates_metrics(tmp_path: Path) -> None:
    cache_path = tmp_path / "tweet_store"
    cache = Cache(cache_path)
    cache.set(
        "tweet:1",
        {
            "tweet_id": "1",
            "author": "alice",
            "text": "old #legacy",
            "created_at": "Wed Aug 05 12:00:00 +0000 2026",
            "favorite_count": 20,
        },
    )
    cache.close()
    store = LegacyTweetStore(cache_path)
    store.validate()
    store.merge(
        (
            Tweet(
                tweet_id="1",
                author_x_id="99",
                created_at=NOW,
                text="old #legacy",
                author="alice",
                favorite_count=10,
                views_count=50,
            ),
        )
    )
    tweets = store.campaign_tweets(_feed(), _campaign())
    store.close()
    assert len(tweets) == 1
    assert tweets[0].author_x_id == "99"
    assert tweets[0].favorite_count == 20
    assert tweets[0].views_count == 50


@pytest.mark.parametrize(
    ("tweet_id", "text", "in_reply_to_status_id"),
    (
        ("2", "@brand reply #legacy", "1"),
        ("3", "RT @brand: original #legacy", None),
    ),
)
def test_campaign_tweets_exclude_replies_and_pure_retweets(
    tmp_path: Path,
    tweet_id: str,
    text: str,
    in_reply_to_status_id: str | None,
) -> None:
    cache_path = tmp_path / "tweet_store"
    with Cache(cache_path) as cache:
        cache.set(
            f"tweet:{tweet_id}",
            {
                "tweet_id": tweet_id,
                "author": "alice",
                "text": text,
                "created_at": "Wed Aug 05 12:00:00 +0000 2026",
                "in_reply_to_status_id": in_reply_to_status_id,
            },
        )

    store = LegacyTweetStore(cache_path)
    store.validate()
    assert store.campaign_tweets(_feed(), _campaign()) == ()
    store.close()


@pytest.mark.parametrize(
    ("campaign_updates", "tweet_updates"),
    (
        ({"inclusion_keywords": ("wallet",)}, {"text": "plain #legacy"}),
        (
            {"quoted_tweet_id": "99"},
            {"text": "quote without required tag", "quoted_tweet_id": "99"},
        ),
        ({"quoted_tweet_id": "99"}, {"quoted_tweet_id": "98"}),
    ),
)
def test_campaign_tweets_apply_every_v1_content_filter(
    tmp_path: Path,
    campaign_updates: dict[str, object],
    tweet_updates: dict[str, object],
) -> None:
    campaign = _filtered_campaign(**campaign_updates)
    tweet = {
        "tweet_id": "10",
        "author": "alice",
        "text": "wallet #legacy",
        "created_at": "Wed Aug 05 12:00:00 +0000 2026",
        "lang": "en",
        "quoted_tweet_id": "99",
    } | tweet_updates
    cache_path = tmp_path / "tweet_store"
    with Cache(cache_path) as cache:
        cache.set("tweet:10", tweet)

    store = LegacyTweetStore(cache_path)
    store.validate()
    assert store.campaign_tweets(_feed(), campaign) == ()
    store.close()


def test_missing_import_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ProtocolError, match="tweet store is missing"):
        LegacyTweetStore(tmp_path / "missing").validate()


def test_engagements_are_cumulative_and_survive_provider_failure(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    cache.set(
        "engagements:1",
        {
            "tweet_id": "1",
            "retweeters": {"Alice": {"first_seen": "old"}},
            "quoters": {"Bob": {"quote_tweet_id": "2", "first_seen": "old"}},
        },
    )
    cache.close()
    store = LegacyTweetStore(tmp_path)
    store.validate()

    merged = store.merge_engagements(
        "1",
        EngagementFetch(
            engagements={"alice": "quote", "carol": "retweet"},
            provider_available=True,
        ),
    )
    assert merged.engagements == {
        "alice": "quote",
        "bob": "quote",
        "carol": "retweet",
    }
    cached = store.merge_engagements("1", EngagementFetch(engagements={}, provider_available=False))
    assert cached == merged
    with Cache(tmp_path) as cache:
        persisted = cache.get("engagements:1")
    assert set(persisted["retweeters"]) == {"Alice", "carol"}
    assert set(persisted["quoters"]) == {"Bob", "alice"}
    store.close()
