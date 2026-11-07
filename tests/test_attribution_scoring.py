"""Tests for time-pinned v2 engagement scoring after attribution."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from bitcast_x.brief_filter import BriefEvaluation, JevBriefFilter
from bitcast_x.campaigns import (
    X_ID_SCORING_CUTOVER,
    CampaignFeed,
    CampaignRecord,
    EcosystemMap,
    RelationshipEdge,
    SocialAccount,
    considered_account_ids_for_campaign,
    uses_x_id_scoring,
)
from bitcast_x.errors import ReconciliationUnavailableError
from bitcast_x.protocol import AttributionReason, AttributionResult, CampaignAccess, MiningProtocol
from bitcast_x.scoring import EngagementContribution
from bitcast_x.validator.rewards import RewardCoordinator
from bitcast_x.validator.scoring import AttributionScorer
from bitcast_x.validator.store import ValidatorStore
from bitcast_x.x_provider import EngagementFetch, EngagementIdentity, Tweet, TweetFetch

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class FakeX:
    def __init__(self) -> None:
        self.tweet_fetches = 0
        self.engagement_fetches = 0
        self.quote_counts: dict[str, int | None] = {}

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

    async def fetch_engagements(
        self, tweet_id: str, *, quote_count: int | None = None
    ) -> EngagementFetch:
        self.engagement_fetches += 1
        self.quote_counts[tweet_id] = quote_count
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


class PartialEngagementX(FakeX):
    """Answers the retweeters lookup for 998 but not its quote search; 997 is gone."""

    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        if tweet_id == "997":
            return TweetFetch(tweet=None, provider_available=False)
        return await super().fetch_tweet_by_id(tweet_id)

    async def fetch_engagements(
        self, tweet_id: str, *, quote_count: int | None = None
    ) -> EngagementFetch:
        if tweet_id == "998":
            return EngagementFetch(engagements={"bob": "retweet"}, provider_available=False)
        return await super().fetch_engagements(tweet_id, quote_count=quote_count)


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


def identity_feed() -> CampaignFeed:
    snapshot = feed()
    shift = X_ID_SCORING_CUTOVER - NOW
    return snapshot.model_copy(
        update={
            "campaigns": tuple(
                campaign.model_copy(
                    update={
                        "opens_at": campaign.opens_at + shift,
                        "closes_at": campaign.closes_at + shift,
                    }
                )
                for campaign in snapshot.campaigns
            ),
            "ecosystem_maps": tuple(
                ecosystem.model_copy(update={"updated_at": ecosystem.updated_at + shift})
                for ecosystem in snapshot.ecosystem_maps
            ),
        }
    )


@pytest.mark.parametrize("offset,expected", [(-1, False), (0, True), (1, True)])
def test_identity_boundary_uses_campaign_open_time(offset: int, expected: bool) -> None:
    campaign = (
        feed()
        .campaigns[0]
        .model_copy(update={"opens_at": X_ID_SCORING_CUTOVER + timedelta(seconds=offset)})
    )
    assert uses_x_id_scoring(campaign) is expected


def test_identity_influence_rename_and_reused_handle() -> None:
    snapshot = identity_feed()
    old, latest = snapshot.ecosystem_maps
    latest = latest.model_copy(
        update={
            "accounts": (
                SocialAccount(x_id="1", username="alice", influence=10),
                SocialAccount(x_id="2", username="renamed", influence=7),
                SocialAccount(x_id="4", username="bob", influence=1),
            )
        }
    )
    snapshot = snapshot.model_copy(update={"ecosystem_maps": (old, latest)})
    considered, _ = considered_account_ids_for_campaign(
        snapshot, snapshot.campaigns[0], ecosystem_id="eco"
    )
    assert considered == {"1": 10, "2": 7, "4": 1, "3": 1}


class IdentityX(FakeX):
    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        result = await super().fetch_tweet_by_id(tweet_id)
        assert result.tweet is not None
        return result.model_copy(
            update={
                "tweet": result.tweet.model_copy(
                    update={
                        "created_at": X_ID_SCORING_CUTOVER + timedelta(minutes=10),
                        "author": "alice_now",
                    }
                )
            }
        )

    async def fetch_engagements(
        self, tweet_id: str, *, quote_count: int | None = None
    ) -> EngagementFetch:
        raise AssertionError("identity scorer must not fetch legacy evidence")

    async def fetch_engagements_by_id(
        self, tweet_id: str, *, quote_count: int | None = None
    ) -> EngagementFetch:
        return EngagementFetch(
            engagements={"carol": "quote"},
            identities=(
                EngagementIdentity(x_id="1", username="old_alice", kind="quote"),
                EngagementIdentity(x_id="2", username="bob_now", kind="retweet"),
                EngagementIdentity(x_id="9", username="carol", kind="quote"),
            ),
            provider_available=True,
        )


async def test_identity_scoring_matches_renamed_engager_and_excludes_self_by_id() -> None:
    snapshot = identity_feed()
    snapshot = snapshot.model_copy(update={"ecosystem_maps": snapshot.ecosystem_maps[:1]})
    result = (await AttributionScorer(IdentityX()).score(snapshot, [accepted("999")]))[0]
    assert result.scoring_by_x_id
    assert result.score == 21.1
    assert [(item.x_id, item.username) for item in result.details] == [("2", "bob_now")]
    assert result.details[0].relationship_score == 9


async def test_identity_participant_exclusion_uses_id_not_handle() -> None:
    from bitcast_x.validator.scoring import _exclude_participant_engagement

    snapshot = identity_feed()
    snapshot = snapshot.model_copy(update={"ecosystem_maps": snapshot.ecosystem_maps[:1]})
    first = (await AttributionScorer(IdentityX()).score(snapshot, [accepted("999")]))[0]
    second = first.model_copy(
        update={"tweet": first.tweet.model_copy(update={"author_x_id": "2", "author": "bob_later"})}
    )
    result = _exclude_participant_engagement([first, second])
    assert result[0].score == 20
    assert result[0].details == ()


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


async def test_preview_scoring_defers_only_the_tweet_with_unavailable_brief_check() -> None:
    attributions = [accepted("998"), accepted("999")]
    scorer = AttributionScorer(FakeX(), brief_filter=SelectivelyUnavailableBriefFilter())

    with pytest.raises(ReconciliationUnavailableError, match="provider unavailable"):
        await scorer.score(feed(), attributions)

    results = await scorer.score(feed(), attributions, defer_unavailable_tweets=True)

    assert [item.attribution.tweet_id for item in results] == ["999"]


async def test_incomplete_engagement_waits_then_scores_the_confirmed_tweet() -> None:
    attributions = [accepted("997"), accepted("998"), accepted("999")]
    scorer = AttributionScorer(PartialEngagementX())

    # Within the settlement grace period, incomplete evidence keeps the campaign waiting.
    with pytest.raises(ReconciliationUnavailableError, match="scoring evidence unavailable"):
        await scorer.score(feed(), attributions)

    results = {
        item.attribution.tweet_id: item
        for item in await scorer.score(feed(), attributions, defer_unavailable_tweets=True)
    }

    # Only the tweet that cannot be fetched is left out; a missing quote search
    # costs 998 its quote engagers, not its baseline or its retweets.
    assert sorted(results) == ["998", "999"]
    partial = results["998"]
    assert partial.baseline_score == results["999"].baseline_score
    assert [detail.username for detail in partial.details] == ["bob"]
    assert partial.score == pytest.approx(
        partial.baseline_score + partial.details[0].weighted_contribution
    )
    assert partial.baseline_score < partial.score < results["999"].score


async def test_engagement_lookup_receives_the_fetched_posts_quote_count() -> None:
    provider = PartialEngagementX()

    await AttributionScorer(provider).score(
        feed(), [accepted("997"), accepted("999")], defer_unavailable_tweets=True
    )

    # 999's post reports no quotes; 997's post could not be fetched, so its count is unknown.
    assert provider.quote_counts == {"997": None, "999": 0}


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
        coordinator = RewardCoordinator(store, AttributionScorer(FakeX(), brief_filter=evaluator))
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
        restarted = RewardCoordinator(store, AttributionScorer(FakeX(), brief_filter=evaluator))
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


async def test_pre_cutover_rebuild_selects_handle_scorer_after_cutover(tmp_path: Path) -> None:
    # A rebuilt store has no finalization marker. Feed opening time still pins legacy scoring.
    snapshot = feed()
    store = ValidatorStore(tmp_path / "rebuilt.sqlite3")
    provider = FakeX()
    coordinator = RewardCoordinator(store, AttributionScorer(provider))
    try:
        store.persist_reconciliation(
            snapshot_id=snapshot.snapshot_id,
            campaign_id="campaign",
            campaign_json=snapshot.campaigns[0].model_dump_json(),
            results=[accepted("999")],
        )
        scores = await coordinator.freeze_scores(
            snapshot,
            [accepted("999")],
            block=100_000_000,
            reconciled_campaign_ids={"campaign"},
        )
        assert len(scores) == 1
        assert not scores[0].scoring_by_x_id
        assert scores[0].score == 2003.75
        assert provider.engagement_fetches == 1
    finally:
        store.close()


class LegacyScoredAttribution(BaseModel):
    """Pre-ID release schema: unknown fields make a rollback unreadable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribution: AttributionResult
    tweet: Tweet
    score: float
    author_influence: float
    baseline_score: float
    details: tuple[EngagementContribution, ...]
    meets_brief: bool = True
    brief_reasoning: str = ""
    brief_detailed_breakdown: str | None = None
    llm_checks_used: int = 0
    engagements: tuple[str, ...] = ()
    author_followers_count: int = 0


async def test_pre_cutover_score_serialization_is_rollback_compatible() -> None:
    score = (await AttributionScorer(FakeX()).score(feed(), [accepted("999")]))[0]
    legacy = LegacyScoredAttribution.model_validate(score.model_dump(exclude={"scoring_by_x_id"}))
    serialized = score.model_dump_json()
    assert "scoring_by_x_id" not in score.model_dump()
    assert serialized == legacy.model_dump_json()
    assert LegacyScoredAttribution.model_validate_json(serialized) == legacy

    # New-campaign evidence must still retain its identity semantics on reload.
    identity_score = score.model_copy(update={"scoring_by_x_id": True})
    assert json.loads(identity_score.model_dump_json())["scoring_by_x_id"] is True
    assert type(score).model_validate_json(identity_score.model_dump_json()).scoring_by_x_id
