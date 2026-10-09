"""Tests for time-pinned v2 engagement scoring after attribution."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from bitcast_x.brief_filter import BriefEvaluation, JevBriefFilter
from bitcast_x.campaigns import (
    CampaignFeed,
    CampaignRecord,
    EcosystemMap,
    RelationshipEdge,
    SocialAccount,
)
from bitcast_x.errors import ReconciliationUnavailableError
from bitcast_x.protocol import AttributionReason, AttributionResult, CampaignAccess, MiningProtocol
from bitcast_x.validator.rewards import RewardCoordinator
from bitcast_x.validator.scoring import AttributionScorer
from bitcast_x.validator.store import ValidatorStore
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


class PassingBriefFilter:
    async def evaluate(self, _campaign: CampaignRecord, _tweet: Tweet) -> BriefEvaluation:
        return BriefEvaluation(
            meets_brief=True,
            reasoning="pass",
            checks_used=1,
        )


class SelectivelyUnavailableEngagementX(FakeX):
    async def fetch_engagements(self, tweet_id: str) -> EngagementFetch:
        if tweet_id == "998":
            return EngagementFetch(engagements={}, provider_available=False)
        return await super().fetch_engagements(tweet_id)


class SelectivelyUnavailableBriefFilter(PassingBriefFilter):
    async def evaluate(self, campaign: CampaignRecord, tweet: Tweet) -> BriefEvaluation:
        if tweet.tweet_id == "998":
            raise ReconciliationUnavailableError("provider unavailable")
        return await super().evaluate(campaign, tweet)


def accepted(tweet_id: str, campaign_id: str = "campaign") -> AttributionResult:
    return AttributionResult(
        tweet_id=tweet_id,
        campaign_id=campaign_id,
        accepted=True,
        reason=AttributionReason.ACCEPTED,
        miner_hotkey=MINER,
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
                display="Campaign",
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


async def test_uses_max_tweet_time_and_current_influence_and_excludes_self() -> None:
    result = (await AttributionScorer(FakeX()).score(feed(), [accepted("999")]))[0]

    assert result.author_influence == 999.0
    assert result.score == 2003.75
    assert [detail.username for detail in result.details] == ["bob", "carol"]


async def test_same_tweet_across_campaigns_uses_one_frozen_provider_observation() -> None:
    snapshot = feed()
    second = snapshot.campaigns[0].model_copy(
        update={"access": snapshot.campaigns[0].access.model_copy(update={"campaign_id": "second"})}
    )
    snapshot = snapshot.model_copy(update={"campaigns": (*snapshot.campaigns, second)})
    attributions = [accepted("999"), accepted("999", campaign_id="second")]
    provider = FakeX()

    results = await AttributionScorer(provider).score(snapshot, attributions)

    assert len(results) == 2
    assert provider.tweet_fetches == 1
    assert provider.engagement_fetches == 1


async def test_passing_campaign_participants_cannot_boost_one_another() -> None:
    snapshot = feed()
    old_map = snapshot.ecosystem_maps[0].model_copy(update={"eligible_creator_x_ids": ("1", "2")})
    snapshot = snapshot.model_copy(update={"ecosystem_maps": (old_map, snapshot.ecosystem_maps[1])})
    attributions = [accepted("999"), accepted("998")]

    results = await AttributionScorer(
        ParticipantX(),
        brief_filter=PassingBriefFilter(),
    ).score(snapshot, attributions)
    alice = next(item for item in results if item.tweet.author == "alice")

    assert alice.meets_brief is True
    assert alice.score == 2001.0
    assert [detail.username for detail in alice.details] == ["carol"]


@pytest.mark.parametrize(
    ("provider", "brief_filter", "error"),
    [
        pytest.param(
            SelectivelyUnavailableEngagementX(),
            None,
            "scoring evidence unavailable",
            id="engagements-unavailable",
        ),
        pytest.param(
            FakeX(),
            SelectivelyUnavailableBriefFilter(),
            "provider unavailable",
            id="brief-check-unavailable",
        ),
    ],
)
async def test_preview_scoring_defers_only_the_tweet_with_unavailable_evidence(
    provider: FakeX,
    brief_filter: PassingBriefFilter | None,
    error: str,
) -> None:
    attributions = [accepted("998"), accepted("999")]
    scorer = AttributionScorer(provider, brief_filter=brief_filter)

    with pytest.raises(ReconciliationUnavailableError, match=error):
        await scorer.score(feed(), attributions)

    results = await scorer.score(feed(), attributions, defer_unavailable_tweets=True)

    assert [item.attribution.tweet_id for item in results] == ["999"]


async def test_jev_outage_grace_recovery_and_frozen_rewards_survive_restart(tmp_path: Path) -> None:
    snapshot = feed()
    campaign = snapshot.campaigns[0].model_copy(
        update={"prompt_version": 6, "emission_start_block": 30, "emission_end_block": 2000}
    )
    ready = campaign.model_copy(
        update={
            "access": campaign.access.model_copy(update={"campaign_id": "ready"}),
            "brief": "Ready campaign",
        }
    )
    snapshot = snapshot.model_copy(update={"campaigns": (campaign, ready)})
    attributions = [accepted("999"), accepted("998", campaign_id="ready")]
    path = tmp_path / "validator.sqlite3"
    store = ValidatorStore(path)
    for record, attribution in zip(snapshot.campaigns, attributions, strict=True):
        store.persist_reconciliation(
            snapshot_id=snapshot.snapshot_id,
            campaign_id=record.access.campaign_id,
            campaign_json=record.model_dump_json(),
            results=[attribution],
        )
    pin = store.pin_featured_tweet_selection(
        campaign_id="campaign",
        campaign_json=campaign.model_dump_json(),
        tweet_id="999",
        selection_pool=("999",),
        selected_block=19,
        selected_at=NOW,
    )
    unavailable = True
    requests: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        if unavailable and json.loads(request.content)["state"]["campaign_brief"] == campaign.brief:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "model": "typesafe/jev-1.13-20260917",
                "answers": {
                    "verdict": {"type": "choice", "probabilities": {"ACCEPT": 0.9, "REJECT": 0.1}},
                    "identity": {
                        "type": "choice",
                        "probabilities": {"MATCH": 0.99, "MISMATCH": 0.01, "UNESTABLISHED": 0.0},
                    },
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evaluator = JevBriefFilter(api_key="existing-key", cache=store, client=client, attempts=1)
        coordinator = RewardCoordinator(
            store, AttributionScorer(FakeX(), brief_filter=evaluator), score_blend=1.0
        )
        for block in (30, 930):
            scores = await coordinator.freeze_scores(snapshot, attributions, block=block)
            assert [item.attribution.campaign_id for item in scores] == ["ready"]
            weights, rewards = coordinator.shadow_weights(
                snapshot, scores, block=block, hotkey_to_uid={MINER: 1}, uids=[0, 1]
            )
            assert weights == {0: 0.0, 1: 1.0}
            assert [item.campaign_id for item in rewards] == ["ready"]
            assert not store.campaign_finalized("campaign")
            assert coordinator.pending_reward_campaign_ids(snapshot, block=block) == (
                ("campaign",) if block == 30 else ()
            )
        unavailable = False
        scores = await coordinator.freeze_scores(snapshot, attributions, block=931)
        weights, rewards = coordinator.shadow_weights(
            snapshot, scores, block=931, hotkey_to_uid={MINER: 1}, uids=[0, 1]
        )
        assert len(rewards) == 2
        assert store.campaign_finalized("campaign")
        assert store.featured_tweet_selection("campaign") == pin
        assert next(item for item in rewards if item.campaign_id == "campaign").featured_tweet_bonus
        request_count = len(requests)
        assert request_count == 4
        store.close()

        # Reopened state must reuse both the JEV verdict and frozen positive economics.
        unavailable = True
        store = ValidatorStore(path)
        evaluator = JevBriefFilter(api_key="existing-key", cache=store, client=client, attempts=1)
        assert (await evaluator.evaluate(campaign, scores[0].tweet)).meets_brief
        restarted = RewardCoordinator(
            store, AttributionScorer(FakeX(), brief_filter=evaluator), score_blend=1.0
        )
        replay = await restarted.freeze_scores(snapshot, attributions, block=932)
        assert replay == scores
        replay_weights, replay_rewards = restarted.shadow_weights(
            snapshot, replay, block=932, hotkey_to_uid={MINER: 1}, uids=[0, 1]
        )
        assert replay_weights == weights
        assert {item.campaign_id: item for item in replay_rewards} == {
            item.campaign_id: item for item in rewards
        }
        assert len(requests) == request_count
        store.close()
