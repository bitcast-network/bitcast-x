"""Tests for time-pinned v2 engagement scoring after attribution."""

from datetime import UTC, datetime, timedelta

import pytest

from bitcast_x.brief_filter import BriefEvaluation
from bitcast_x.campaigns import (
    CampaignFeed,
    CampaignRecord,
    EcosystemMap,
    RelationshipEdge,
    SocialAccount,
)
from bitcast_x.protocol import AttributionReason, AttributionResult, CampaignAccess, MiningProtocol
from bitcast_x.validator.scoring import AttributionScorer
from bitcast_x.x_provider import EngagementFetch, Tweet, TweetFetch

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class FakeX:
    def __init__(self) -> None:
        self.tweet_fetches = 0
        self.engagement_fetches = 0

    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        self.tweet_fetches += 1
        return TweetFetch(
            tweet=Tweet(
                tweet_id=tweet_id,
                author_x_id="1",
                created_at=NOW + timedelta(minutes=10),
                text="post",
                author="alice",
            ),
            provider_available=True,
        )

    async def fetch_engagements(self, _tweet_id: str) -> EngagementFetch:
        self.engagement_fetches += 1
        return EngagementFetch(
            engagements={"alice": "retweet", "bob": "retweet", "carol": "quote"},
            provider_available=True,
        )

    async def close(self) -> None:
        pass


class ParticipantX(FakeX):
    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        author = "alice" if tweet_id == "999" else "bob"
        author_x_id = "1" if author == "alice" else "2"
        return TweetFetch(
            tweet=Tweet(
                tweet_id=tweet_id,
                author_x_id=author_x_id,
                created_at=NOW + timedelta(minutes=10),
                text="post",
                author=author,
            ),
            provider_available=True,
        )


class UnavailableTweetX(FakeX):
    async def fetch_tweet_by_id(self, _tweet_id: str) -> TweetFetch:
        return TweetFetch(tweet=None, provider_available=False)


class PassingBriefFilter:
    async def evaluate(self, _campaign: CampaignRecord, _tweet: Tweet) -> BriefEvaluation:
        return BriefEvaluation(
            meets_brief=True,
            reasoning="pass",
            checks_used=1,
        )


def feed() -> CampaignFeed:
    return CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=(
            CampaignRecord(
                access=CampaignAccess(
                    campaign_id="campaign",
                    mechanism_id=1,
                    mining_protocol=MiningProtocol.PRECLAIM_V2,
                    scoring_close_block=20,
                ),
                title="Campaign",
                brief="Brief",
                pools=("unrelated", "eco"),
                opens_at=NOW,
                closes_at=NOW + timedelta(days=1),
                reward_pool_usd="1000",
            ),
        ),
        ecosystem_maps=(
            EcosystemMap(
                ecosystem_id="eco",
                name="Old map",
                eligible_creator_x_ids=("1",),
                updated_at=NOW - timedelta(days=1),
                accounts=(
                    SocialAccount(x_id="1", username="alice", influence=10.0),
                    SocialAccount(x_id="2", username="bob", influence=5.5),
                    SocialAccount(x_id="3", username="carol", influence=2.0),
                ),
                relationships=(
                    RelationshipEdge(source_username="bob", target_username="alice", score=9.0),
                    RelationshipEdge(source_username="carol", target_username="alice", score=4.0),
                ),
            ),
            EcosystemMap(
                ecosystem_id="eco",
                name="Future map",
                eligible_creator_x_ids=("1",),
                updated_at=NOW + timedelta(hours=1),
                accounts=(SocialAccount(x_id="1", username="alice", influence=999.0),),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_uses_max_tweet_time_and_current_influence_and_excludes_self() -> None:
    attribution = AttributionResult(
        tweet_id="999",
        campaign_id="campaign",
        accepted=True,
        reason=AttributionReason.ACCEPTED,
        miner_hotkey=MINER,
    )

    result = (await AttributionScorer(FakeX()).score(feed(), [attribution]))[0]

    assert result.author_influence == 999.0
    assert result.score == 2003.75
    assert [detail.username for detail in result.details] == ["bob", "carol"]


@pytest.mark.asyncio
async def test_same_tweet_across_campaigns_uses_one_frozen_provider_observation() -> None:
    snapshot = feed()
    second = snapshot.campaigns[0].model_copy(
        update={"access": snapshot.campaigns[0].access.model_copy(update={"campaign_id": "second"})}
    )
    snapshot = snapshot.model_copy(update={"campaigns": (*snapshot.campaigns, second)})
    attributions = [
        AttributionResult(
            tweet_id="999",
            campaign_id=campaign_id,
            accepted=True,
            reason=AttributionReason.ACCEPTED,
            miner_hotkey=MINER,
        )
        for campaign_id in ("campaign", "second")
    ]
    provider = FakeX()

    results = await AttributionScorer(provider).score(snapshot, attributions)

    assert len(results) == 2
    assert provider.tweet_fetches == 1
    assert provider.engagement_fetches == 1


@pytest.mark.asyncio
async def test_legacy_cached_tweet_is_used_when_refresh_is_unavailable() -> None:
    attribution = AttributionResult(
        tweet_id="999",
        campaign_id="campaign",
        accepted=True,
        reason=AttributionReason.ACCEPTED,
        miner_hotkey=MINER,
    )
    cached = Tweet(
        tweet_id="999",
        author_x_id="1",
        created_at=NOW + timedelta(minutes=10),
        text="post",
        author="alice",
    )

    result = (
        await AttributionScorer(UnavailableTweetX()).score(
            feed(),
            [attribution],
            tweet_evidence={"999": cached},
        )
    )[0]

    assert result.tweet == cached


@pytest.mark.asyncio
async def test_passing_campaign_participants_cannot_boost_one_another() -> None:
    snapshot = feed()
    old_map = snapshot.ecosystem_maps[0].model_copy(update={"eligible_creator_x_ids": ("1", "2")})
    snapshot = snapshot.model_copy(update={"ecosystem_maps": (old_map, snapshot.ecosystem_maps[1])})
    attributions = [
        AttributionResult(
            tweet_id=tweet_id,
            campaign_id="campaign",
            accepted=True,
            reason=AttributionReason.ACCEPTED,
            miner_hotkey=MINER,
        )
        for tweet_id in ("999", "998")
    ]

    results = await AttributionScorer(
        ParticipantX(),
        brief_filter=PassingBriefFilter(),
    ).score(snapshot, attributions)
    alice = next(item for item in results if item.tweet.author == "alice")

    assert alice.meets_brief is True
    assert alice.score == 2001.0
    assert [detail.username for detail in alice.details] == ["carol"]
