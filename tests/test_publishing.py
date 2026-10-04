"""Golden tests for the existing ingestion wire contract and durable publication."""

import gzip
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from bitcast_x.campaigns import CampaignFeed, CampaignRecord
from bitcast_x.errors import ProtocolError
from bitcast_x.protocol import (
    AttributionReason,
    AttributionResult,
    CampaignAccess,
    MiningProtocol,
)
from bitcast_x.publishing import BRIEF_TWEETS_PAYLOAD_TYPE, DataPublisher
from bitcast_x.rewards import RewardDecision, TweetReward
from bitcast_x.scoring import EngagementContribution
from bitcast_x.validator.preview import PreviewStore
from bitcast_x.validator.publishing import ShadowResultPublisher, create_brief_tweets_payload
from bitcast_x.validator.scoring import ScoredAttribution
from bitcast_x.validator.store import ValidatorStore
from bitcast_x.x_provider import Tweet

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
BEFORE_FEATURED_SELECTION = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
SUBMISSION_ID = "01" * 16


def campaign() -> CampaignRecord:
    return CampaignRecord(
        access=CampaignAccess(
            campaign_id="campaign",
            mechanism_id=1,
            mining_protocol=MiningProtocol.PRECLAIM_V2,
            scoring_close_block=100,
        ),
        display="Campaign",
        brief="Write about the campaign",
        pools=("tao",),
        opens_at=datetime(2026, 8, 1, tzinfo=UTC),
        closes_at=datetime(2026, 8, 5, tzinfo=UTC),
        reward_pool_usd="700",
        emission_start_block=101,
        emission_end_block=200,
    )


class FakeHotkey:
    ss58_address = MINER

    def __init__(self) -> None:
        self.message = ""

    def sign(self, data: str) -> bytes:
        self.message = data
        return b"signed"


class CapturingDataPublisher:
    def __init__(self, outcomes: list[bool] | None = None) -> None:
        self.payloads: list[dict[str, object]] = []
        self.run_ids: list[str] = []
        self._outcomes = list(outcomes or [])

    async def publish(
        self,
        *,
        endpoint: str,
        payload_type: str,
        run_id: str,
        payload: dict[str, object],
    ) -> bool:
        assert endpoint == "https://ingestion.example/api/v1/brief-tweets"
        assert payload_type == BRIEF_TWEETS_PAYLOAD_TYPE
        assert run_id.startswith("v3-preview:snapshot:campaign:")
        self.run_ids.append(run_id)
        self.payloads.append(payload)
        return self._outcomes.pop(0) if self._outcomes else True


def scored() -> ScoredAttribution:
    return ScoredAttribution(
        attribution=AttributionResult(
            tweet_id="123",
            campaign_id="campaign",
            accepted=True,
            reason=AttributionReason.ACCEPTED,
            miner_hotkey=MINER,
            submission_id=SUBMISSION_ID,
        ),
        tweet=Tweet(
            tweet_id="123",
            author_x_id="456",
            created_at=NOW,
            text="hello",
            author="alice",
            favorite_count=4,
            retweet_count=2,
            views_count=100,
        ),
        score=23.05,
        author_influence=10.0,
        baseline_score=20.0,
        author_followers_count=10,
        details=(
            EngagementContribution(
                username="bob",
                influence_score=5.5,
                engagement_type="retweet",
                relationship_score=9.0,
                scale_factor=0.2,
                weighted_contribution=1.1,
            ),
        ),
    )


SNAPSHOT = CampaignFeed(
    snapshot_id="snapshot",
    published_at=NOW,
    campaigns=(campaign(),),
    ecosystem_maps=(),
)


def preview_publisher(
    tmp_path: Path,
    *,
    outcomes: list[bool] | None = None,
    now: Callable[[], datetime] = lambda: BEFORE_FEATURED_SELECTION,
) -> tuple[ShadowResultPublisher, CapturingDataPublisher]:
    """Return a preview publisher over the state in ``tmp_path`` and its captured posts."""

    data_publisher = CapturingDataPublisher(outcomes)
    publisher = ShadowResultPublisher(
        ValidatorStore(tmp_path / "validator.sqlite3"),
        data_publisher,  # type: ignore[arg-type]
        endpoint="https://ingestion.example/api/v1/brief-tweets",
        preview_store=PreviewStore(tmp_path / "preview.sqlite3"),
        now=now,
    )
    return publisher, data_publisher


async def publish_preview(
    publisher: ShadowResultPublisher,
    items: list[ScoredAttribution],
    attributions: list[AttributionResult] | None = None,
    *,
    block: int = 50,
) -> bool:
    """Publish a preview of ``items``; ``attributions`` defaults to the items' own."""

    return await publisher.publish_preview(
        SNAPSHOT,
        campaign(),
        items,
        [item.attribution for item in items] if attributions is None else attributions,
        block=block,
        hotkey_to_uid={MINER: 7},
    )


def test_signed_envelope_matches_the_message_ingestion_rebuilds() -> None:
    hotkey = FakeHotkey()
    publisher = DataPublisher(SimpleNamespace(hotkey=hotkey))
    data = {
        "payload_type": BRIEF_TWEETS_PAYLOAD_TYPE,
        "run_id": "run",
        # UID keys sort as 45 < 114 here but "114" < "45" once parsed from JSON.
        "payload": {"brief_id": "campaign", "tweets": [], "uid_usd_targets": {45: 1.0, 114: 2.0}},
    }

    signed = publisher.signed_payload(data, timestamp=NOW)

    received = json.loads(json.dumps(signed))
    expected_core = json.dumps(received["payload"], sort_keys=True)
    assert hotkey.message == f"{MINER}:2026-08-05T12:00:00:{expected_core}"
    assert signed["signature"] == b"signed".hex()
    assert signed["signer"] == signed["vali_hotkey"] == MINER


async def test_publisher_gzips_large_payload_and_requires_accepted_status() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["body"] = request.content
        return httpx.Response(202, json={"status": "accepted"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = DataPublisher(SimpleNamespace(hotkey=FakeHotkey()), client=client)

    success = await publisher.publish(
        endpoint="https://ingestion.example/api/v1/brief-tweets",
        payload_type=BRIEF_TWEETS_PAYLOAD_TYPE,
        run_id="run",
        payload={"brief_id": "campaign", "tweets": [], "padding": "x" * 1_000_000},
    )

    assert success is True
    assert captured["headers"]["content-encoding"] == "gzip"
    assert json.loads(gzip.decompress(captured["body"]))["payload_type"] == "brief_tweets"
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(500, b'{"status": "accepted"}', id="server-error"),
        pytest.param(200, b'{"status": "accepted"}', id="ok-but-not-202"),
        pytest.param(202, b'{"status": "rejected"}', id="202-rejected-status"),
        pytest.param(202, b'["accepted"]', id="202-non-object-body"),
        pytest.param(202, b"accepted", id="202-malformed-json"),
    ],
)
async def test_publisher_reports_failure_unless_ingestion_accepts(status: int, body: bytes) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = DataPublisher(SimpleNamespace(hotkey=FakeHotkey()), client=client)

    success = await publisher.publish(
        endpoint="https://ingestion.example/api/v1/brief-tweets",
        payload_type=BRIEF_TWEETS_PAYLOAD_TYPE,
        run_id="run",
        payload={"brief_id": "campaign", "tweets": []},
    )

    assert success is False
    await client.aclose()


def test_brief_tweets_payload_preserves_v2_shape_and_v3_miner_target() -> None:
    item = scored()
    reward = TweetReward(
        campaign_id="campaign",
        tweet_id="123",
        creator_x_id="456",
        miner_hotkey=MINER,
        score=23.05,
        daily_usd_floor=100.0,
    )
    reward_decision = RewardDecision(
        campaign_id="campaign",
        tweet_id="123",
        miner_hotkey=MINER,
        accepted=True,
        reason=AttributionReason.ACCEPTED,
        daily_usd_floor=100.0,
    )

    payload = create_brief_tweets_payload(
        campaign(),
        [reward],
        {("campaign", "123"): item},
        {MINER: 7},
        reward_decisions=[reward_decision],
        timestamp=NOW,
    )

    assert payload["brief_id"] == "campaign"
    assert payload["summary"] == {
        "total_tweets": 1,
        "total_usd_target": 100.0,
        "unique_creators": 1,
        "uid_usd_targets": {7: 100.0},
        "attribution_accepted": 1,
        "attribution_pending": 0,
        "attribution_rejected": 0,
    }
    tweet = payload["tweets"][0]  # type: ignore[index]
    assert tweet["usd_target"] == 100.0
    assert tweet["total_usd_target"] == 700.0
    assert tweet["alpha_target"] is None
    assert tweet["weight"] is None
    assert tweet["score_breakdown"] == [{"u": "bob", "t": "rt", "i": 5.5, "s": 0.2, "c": 1.1}]
    assert tweet["attribution"] == {
        "status": "accepted",
        "reason": "accepted",
        "miner_hotkey": MINER,
        "miner_uid": 7,
        "submission_id": SUBMISSION_ID,
        "claim_id": None,
        "mining_protocol": "preclaim_v2",
        "mechanism_id": 1,
        "winner_score": None,
        "runner_up_score": None,
    }
    assert payload["attribution_decisions"] == [
        {
            "tweet_id": "123",
            "campaign_id": "campaign",
            "status": "accepted",
            "reason": "accepted",
            "miner_hotkey": MINER,
            "miner_uid": 7,
            "submission_id": SUBMISSION_ID,
            "claim_id": None,
            "mining_protocol": "preclaim_v2",
            "mechanism_id": 1,
            "winner_score": None,
            "runner_up_score": None,
            "reward_status": "rewarded",
            "reward_reason": "accepted",
            "daily_usd_floor": 100.0,
        }
    ]


def test_payload_publishes_protocol_rejections_without_fabricating_tweet_rows() -> None:
    accepted = scored()
    rejected = AttributionResult(
        tweet_id="124",
        campaign_id="campaign",
        accepted=False,
        reason=AttributionReason.AMBIGUOUS_MATCH,
        submission_id="02" * 16,
        winner_score=0.8,
        runner_up_score=0.75,
    )

    payload = create_brief_tweets_payload(
        campaign(),
        [],
        {("campaign", "123"): accepted},
        {MINER: 7},
        attributions=[rejected, accepted.attribution],
        timestamp=NOW,
    )

    assert [item["tweet_id"] for item in payload["tweets"]] == ["123"]  # type: ignore[index]
    assert payload["summary"]["attribution_accepted"] == 1  # type: ignore[index]
    assert payload["summary"]["attribution_pending"] == 0  # type: ignore[index]
    assert payload["summary"]["attribution_rejected"] == 1  # type: ignore[index]
    decisions = payload["attribution_decisions"]
    assert isinstance(decisions, list)
    assert decisions[1] == {
        "tweet_id": "124",
        "campaign_id": "campaign",
        "status": "rejected",
        "reason": "ambiguous_match",
        "miner_hotkey": None,
        "miner_uid": None,
        "submission_id": "02" * 16,
        "claim_id": None,
        "mining_protocol": "preclaim_v2",
        "mechanism_id": 1,
        "winner_score": 0.8,
        "runner_up_score": 0.75,
        "reward_status": "pending",
        "reward_reason": None,
        "daily_usd_floor": None,
    }


async def test_preview_publishes_zero_dollar_bonuses_and_omits_unscored_acceptances(
    tmp_path: Path,
) -> None:
    available = scored()
    unavailable = available.attribution.model_copy(
        update={"tweet_id": "124", "submission_id": "02" * 16}
    )
    rejected = AttributionResult(
        tweet_id="125",
        campaign_id="campaign",
        accepted=False,
        reason=AttributionReason.AMBIGUOUS_MATCH,
        submission_id="03" * 16,
    )
    publisher, data_publisher = preview_publisher(tmp_path)

    published = await publish_preview(
        publisher, [available], [available.attribution, unavailable, rejected]
    )

    assert published is True
    payload = data_publisher.payloads[0]
    # The accepted tweet without scoring evidence (124) is withheld; the rejection is not.
    decisions = payload["attribution_decisions"]
    assert isinstance(decisions, list)
    assert [item["tweet_id"] for item in decisions] == ["123", "125"]
    tweet = payload["tweets"][0]  # type: ignore[index]
    assert tweet["performance_bonus_pct"] == 20.0
    assert tweet["performance_bonus_breakdown"] == {
        "views": 5.0,
        "views_per_follower": 5.0,
        "total_engagements": 5.0,
        "engagement_per_view": 5.0,
    }
    assert tweet["score"] == pytest.approx(27.66)
    assert tweet["usd_target"] == 0.0
    assert tweet["total_usd_target"] == 0.0
    assert payload["summary"]["total_usd_target"] == 0.0  # type: ignore[index]
    assert payload["summary"]["uid_usd_targets"] == {}  # type: ignore[index]


async def test_preview_is_not_republished_until_its_semantic_payload_changes(
    tmp_path: Path,
) -> None:
    publisher, data_publisher = preview_publisher(tmp_path)

    first = await publish_preview(publisher, [scored()])
    duplicate = await publish_preview(publisher, [scored()], block=51)
    changed_score = scored().model_copy(update={"score": scored().score + 1})
    changed = await publish_preview(publisher, [changed_score], block=52)

    assert first is True
    assert duplicate is False
    assert changed is True
    assert len(data_publisher.payloads) == 2
    assert data_publisher.run_ids[0] != data_publisher.run_ids[1]


async def test_preview_pins_featured_tweet_and_never_replaces_it(
    tmp_path: Path,
) -> None:
    selected_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    first_publisher, first_data_publisher = preview_publisher(tmp_path, now=lambda: selected_at)

    assert await publish_preview(first_publisher, [scored()], block=90)
    selection = first_publisher.store.featured_tweet_selection("campaign")
    assert selection is not None
    assert selection.tweet_id == "123"
    assert selection.selected_block == 90
    first_payload = first_data_publisher.payloads[0]
    assert first_payload["featured_tweet"] == {
        "brief_id": "campaign",
        "tweet_id": "123",
        "author": "alice",
        "views_count": 100,
        "selected_at": selected_at.isoformat(),
        "selection_pool": ["123"],
        "selection_method": "sha256_mod",
    }
    assert first_payload["tweets"][0]["featured_tweet_bonus"] is True  # type: ignore[index]

    # A fresh publisher over the same state directory is a validator restart.
    restarted_publisher, restarted_data_publisher = preview_publisher(
        tmp_path, now=lambda: selected_at + timedelta(minutes=5)
    )
    rejected = AttributionResult(
        tweet_id="999",
        campaign_id="campaign",
        accepted=False,
        reason=AttributionReason.AMBIGUOUS_MATCH,
    )

    assert await publish_preview(restarted_publisher, [], [rejected], block=91)
    assert restarted_data_publisher.payloads[0]["featured_tweet"] is None

    recovered = scored().model_copy(update={"score": scored().score + 1})
    assert await publish_preview(restarted_publisher, [recovered], block=92)
    assert restarted_publisher.store.featured_tweet_selection("campaign") == selection
    assert restarted_data_publisher.payloads[1]["featured_tweet"] == first_payload["featured_tweet"]


async def test_failed_preview_publication_retries_same_payload_after_one_minute(
    tmp_path: Path,
) -> None:
    clock = [BEFORE_FEATURED_SELECTION]
    publisher, data_publisher = preview_publisher(
        tmp_path, outcomes=[False, True], now=lambda: clock[0]
    )

    assert not await publish_preview(publisher, [scored()])
    clock[0] += timedelta(seconds=30)
    assert not await publish_preview(publisher, [scored()], block=51)
    assert len(data_publisher.payloads) == 1

    clock[0] += timedelta(seconds=31)
    assert await publish_preview(publisher, [scored()], block=52)
    assert data_publisher.run_ids == [data_publisher.run_ids[0]] * 2
    assert data_publisher.payloads[0] == data_publisher.payloads[1]


@pytest.mark.parametrize(
    ("attribution", "reward_decisions", "status", "reward_reason"),
    [
        pytest.param(
            AttributionResult(
                tweet_id="124",
                campaign_id="campaign",
                accepted=False,
                reason=AttributionReason.MINER_NOT_QUALIFIED,
                pending=True,
            ),
            None,
            "pending",
            "miner_not_qualified",
            id="preview-unqualified-miner",
        ),
        pytest.param(
            AttributionResult(
                tweet_id="124",
                campaign_id="campaign",
                accepted=False,
                reason=AttributionReason.EVIDENCE_UNAVAILABLE,
                pending=True,
                miner_hotkey=MINER,
                submission_id="02" * 16,
            ),
            [],
            "pending",
            "evidence_unavailable",
            id="final-unavailable-evidence",
        ),
        pytest.param(
            scored().attribution,
            [],
            "accepted",
            "evidence_unavailable",
            id="final-unavailable-scoring",
        ),
    ],
)
def test_payload_keeps_unsettled_rewards_pending(
    attribution: AttributionResult,
    reward_decisions: list[RewardDecision] | None,
    status: str,
    reward_reason: str,
) -> None:
    payload = create_brief_tweets_payload(
        campaign(),
        [],
        {},
        {MINER: 7},
        attributions=[attribution],
        reward_decisions=reward_decisions,
        timestamp=NOW,
    )

    assert payload["summary"][f"attribution_{status}"] == 1  # type: ignore[index]
    assert payload["summary"]["attribution_rejected"] == 0  # type: ignore[index]
    decision = payload["attribution_decisions"][0]  # type: ignore[index]
    assert decision["status"] == status
    assert decision["reason"] == attribution.reason.value
    assert decision["reward_status"] == "pending"
    assert decision["reward_reason"] == reward_reason
    assert decision["daily_usd_floor"] is None


def test_final_payload_distinguishes_attribution_from_duplicate_reward_rejection() -> None:
    accepted = scored()
    duplicate = RewardDecision(
        campaign_id="campaign",
        tweet_id="123",
        miner_hotkey=MINER,
        accepted=False,
        reason=AttributionReason.DUPLICATE_TWEET,
    )

    payload = create_brief_tweets_payload(
        campaign(),
        [],
        {("campaign", "123"): accepted},
        {MINER: 7},
        reward_decisions=[duplicate],
        timestamp=NOW,
    )

    tweet = payload["tweets"][0]  # type: ignore[index]
    decision = payload["attribution_decisions"][0]  # type: ignore[index]
    assert tweet["attribution"]["status"] == "accepted"
    assert tweet["usd_target"] == 0.0
    assert decision["status"] == "accepted"
    assert decision["reward_status"] == "not_rewarded"
    assert decision["reward_reason"] == "duplicate_tweet"
    assert decision["daily_usd_floor"] == 0.0


def test_zero_value_publication_is_replaceable(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    payload = {"brief_id": "campaign", "tweets": []}

    store.record_publication("snapshot", "campaign", run_id="run", payload=payload, succeeded=True)
    changed = {"brief_id": "campaign", "tweets": [{"usd_target": 0.0}]}
    store.record_publication(
        "snapshot-2",
        "campaign",
        run_id="run-2",
        payload=changed,
        succeeded=True,
    )

    assert store.publication_succeeded("campaign") is False


def test_positive_publication_success_is_durable_and_changed_replay_fails(
    tmp_path: Path,
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    payload = {"brief_id": "campaign", "tweets": [{"usd_target": 1.0}]}

    store.record_publication("snapshot", "campaign", run_id="run", payload=payload, succeeded=True)
    with pytest.raises(ProtocolError, match="successful publication changed"):
        store.record_publication(
            "snapshot",
            "campaign",
            run_id="run",
            payload={"brief_id": "campaign", "tweets": [{"usd_target": 2.0}]},
            succeeded=True,
        )

    assert store.publication_succeeded("campaign") is True
    store.record_publication("snapshot", "campaign", run_id="run", payload=payload, succeeded=True)


def test_payload_includes_bonus_metadata_and_unrewarded_filter_results() -> None:
    accepted = scored()
    failed = accepted.model_copy(
        update={
            "attribution": accepted.attribution.model_copy(update={"tweet_id": "124"}),
            "tweet": accepted.tweet.model_copy(update={"tweet_id": "124", "author_x_id": "457"}),
            "meets_brief": False,
            "brief_reasoning": "Missing a required point",
        }
    )
    reward = TweetReward(
        campaign_id="campaign",
        tweet_id="123",
        creator_x_id="456",
        miner_hotkey=MINER,
        score=27.0,
        daily_usd_floor=100.0,
        performance_bonus_pct=12.5,
        performance_bonus_breakdown={"views": 5.0},
        featured_tweet_bonus=True,
        featured_tweet_id="123",
    )
    reward_decision = RewardDecision(
        campaign_id="campaign",
        tweet_id="123",
        miner_hotkey=MINER,
        accepted=True,
        reason=AttributionReason.ACCEPTED,
        daily_usd_floor=100.0,
    )

    payload = create_brief_tweets_payload(
        campaign(),
        [reward],
        {("campaign", "123"): accepted, ("campaign", "124"): failed},
        {MINER: 7},
        reward_decisions=[reward_decision],
        timestamp=NOW,
    )

    tweets = payload["tweets"]
    assert isinstance(tweets, list)
    assert tweets[0]["score"] == 27.0
    assert tweets[0]["performance_bonus_pct"] == 12.5
    assert tweets[0]["featured_tweet_bonus"] is True
    assert tweets[1]["meets_brief"] is False
    assert tweets[1]["reasoning"] == "Missing a required point"
    assert tweets[1]["usd_target"] == 0.0
    decisions = payload["attribution_decisions"]
    assert isinstance(decisions, list)
    assert decisions[0]["reward_status"] == "rewarded"
    assert decisions[1]["reward_status"] == "not_rewarded"
    assert decisions[1]["reward_reason"] == "brief_filter_rejected"
    assert decisions[1]["daily_usd_floor"] == 0.0
    featured = payload["featured_tweet"]
    assert isinstance(featured, dict)
    assert featured["tweet_id"] == "123"


def test_rewarded_tweet_requires_matching_registered_miner() -> None:
    item = scored()
    reward = TweetReward(
        campaign_id="campaign",
        tweet_id="123",
        creator_x_id="456",
        miner_hotkey=MINER,
        score=23.05,
        daily_usd_floor=100.0,
    )

    with pytest.raises(ProtocolError, match="no unambiguous registered miner"):
        create_brief_tweets_payload(
            campaign(),
            [reward],
            {("campaign", "123"): item},
            {},
            timestamp=NOW,
        )


def test_unpinned_featured_pool_is_published_in_settlement_rank_order() -> None:
    views = {"1": 10, "2": 30, "3": 20}
    scored_by_key: dict[tuple[str, str], ScoredAttribution] = {}
    rewards: list[TweetReward] = []
    for tweet_id, count in views.items():
        scored_by_key[("campaign", tweet_id)] = scored().model_copy(
            update={
                "attribution": scored().attribution.model_copy(update={"tweet_id": tweet_id}),
                "tweet": scored().tweet.model_copy(
                    update={"tweet_id": tweet_id, "views_count": count}
                ),
            }
        )
        rewards.append(
            TweetReward(
                campaign_id="campaign",
                tweet_id=tweet_id,
                creator_x_id="1",
                miner_hotkey=MINER,
                score=1.0,
                daily_usd_floor=1.0,
                featured_tweet_bonus=tweet_id == "2",
                featured_tweet_id="2",
            )
        )

    payload = create_brief_tweets_payload(
        campaign(), rewards, scored_by_key, {MINER: 7}, timestamp=NOW
    )

    featured = payload["featured_tweet"]
    assert isinstance(featured, dict)
    assert featured["selection_pool"] == ["2", "3", "1"]
