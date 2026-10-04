"""End-to-end attribution replay tests over verified validator history."""

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bitcast_x.campaigns import (
    EVIDENCE_GRACE_BLOCKS,
    CampaignFeed,
    CampaignRecord,
    EcosystemMap,
    SocialAccount,
)
from bitcast_x.chain import ChainCommitment
from bitcast_x.errors import ProtocolError
from bitcast_x.protocol import (
    CREATOR_BINDING_ACTIVATION_BLOCK,
    MAX_ACTIVE_CLAIMS,
    AttributionReason,
    AttributionResult,
    CampaignAccess,
    ClaimEvent,
    CommitmentEnvelope,
    CommittedBatch,
    DraftReveal,
    MiningProtocol,
    SubmissionEvent,
)
from bitcast_x.rewards import TweetReward
from bitcast_x.state import shadow_report
from bitcast_x.validator.preview import PreviewStore
from bitcast_x.validator.publishing import ShadowResultPublisher
from bitcast_x.validator.reconciliation import CampaignReconciler
from bitcast_x.validator.rewards import RewardCoordinator
from bitcast_x.validator.scoring import AttributionScorer, ScoredAttribution
from bitcast_x.validator.store import ValidatorStore, VerifiedBatchRecord
from bitcast_x.x_provider import EngagementFetch, Tweet, TweetFetch

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
OTHER_MINER = "5FHneW46xGXgs5mUiveU4sbTyGBzmst2jfFvCw9zThqAXhGK"
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class FakeQualification:
    def __init__(self, eligible: bool = True) -> None:
        self.value = eligible
        self.calls: list[int] = []

    async def eligible(self, _miner_hotkey: str, _block: int) -> bool:
        self.calls.append(_block)
        return self.value


class QualificationAtBlocks:
    def __init__(self, eligible_blocks: set[int]) -> None:
        self.eligible_blocks = eligible_blocks
        self.calls: list[int] = []

    async def eligible(self, _miner_hotkey: str, block: int) -> bool:
        self.calls.append(block)
        return block in self.eligible_blocks


class FakeX:
    def __init__(self, evidence: dict[str, TweetFetch]) -> None:
        self.evidence = evidence

    async def fetch_tweet_by_id(self, tweet_id: str) -> TweetFetch:
        return self.evidence[tweet_id]

    async def fetch_engagements(self, _tweet_id: str) -> EngagementFetch:
        return EngagementFetch(engagements={}, provider_available=True)

    async def close(self) -> None:
        pass


class MultiCampaignPublisher:
    def __init__(self) -> None:
        self.run_ids: list[str] = []
        self.payloads: list[dict[str, object]] = []

    async def publish(
        self,
        *,
        endpoint: str,
        payload_type: str,
        run_id: str,
        payload: dict[str, object],
    ) -> bool:
        assert endpoint == "https://ingestion.example/api/v1/brief-tweets"
        assert payload_type == "brief_tweets"
        self.run_ids.append(run_id)
        self.payloads.append(payload)
        return True


def campaign(
    *,
    exclusive: str | None = None,
    scoring_close_block: int = 20,
) -> CampaignRecord:
    return CampaignRecord(
        access=CampaignAccess(
            campaign_id="campaign",
            mechanism_id=1,
            mining_protocol=MiningProtocol.PRECLAIM_V2,
            scoring_close_block=scoring_close_block,
            exclusive_miner_hotkey=exclusive,
        ),
        display="Campaign",
        brief="Talk about the wallet in your own words",
        pools=("ecosystem",),
        opens_at=NOW,
        closes_at=NOW + timedelta(days=1),
        reward_pool_usd="1000.00",
        required_terms=("#Launch",),
    )


def feed(*records: CampaignRecord, influence: float | None = None) -> CampaignFeed:
    """Build a feed whose creator ``456`` scores engagement only when given ``influence``."""

    return CampaignFeed(
        snapshot_id="snapshot",
        published_at=NOW,
        campaigns=records,
        ecosystem_maps=(
            EcosystemMap(
                ecosystem_id="ecosystem",
                name="Ecosystem",
                eligible_creator_x_ids=("456",),
                updated_at=NOW,
                accounts=(
                    ()
                    if influence is None
                    else (SocialAccount(x_id="456", username="creator", influence=influence),)
                ),
            ),
        ),
    )


def tweet(
    tweet_id: str = "999",
    *,
    created_at: datetime | None = None,
    text: str | None = None,
    quoted_tweet_id: str | None = None,
) -> Tweet:
    return Tweet(
        tweet_id=tweet_id,
        author_x_id="456",
        created_at=created_at or NOW + timedelta(minutes=10),
        text=text
        or (
            "I spent a week testing the wallet. Fast confirmations help, but the recovery "
            "flow is what won me over. #Launch"
        ),
        author="creator",
        quoted_tweet_id=quoted_tweet_id,
    )


def persist_batch(
    store: ValidatorStore,
    batch: CommittedBatch,
    *,
    block: int,
    timestamp: datetime,
) -> None:
    anchor = ChainCommitment(
        hotkey=batch.miner_hotkey,
        block=block,
        extrinsic_index=2,
        timestamp=timestamp,
        envelope=CommitmentEnvelope(
            sequence=batch.sequence,
            event_count=len(batch.events),
            batch_hash=bytes.fromhex(batch.batch_hash),
            history_id=(bytes.fromhex(batch.history_id) if batch.history_id else None),
        ),
    )
    store.persist_verified(batch, anchor)


def open_history(
    path: Path,
    *,
    claim_timestamp: datetime = NOW + timedelta(minutes=1),
    draft: str | None = None,
    revealed_draft: str | None = None,
) -> ValidatorStore:
    store = ValidatorStore(path)
    claim_reveal = DraftReveal(
        claim_id="01" * 16,
        draft=draft
        or (
            "I spent a week testing the wallet — fast confirmations help, but the recovery "
            "flow is what won me over. #Launch"
        ),
        nonce="02" * 32,
    )
    submitted_reveal = (
        claim_reveal
        if revealed_draft is None
        else DraftReveal(
            claim_id=claim_reveal.claim_id,
            draft=revealed_draft,
            nonce=claim_reveal.nonce,
        )
    )
    claim = ClaimEvent(
        claim_id=claim_reveal.claim_id,
        campaign_id="campaign",
        creator_x_id="456",
        created_at=claim_timestamp,
        draft_commitment=claim_reveal.commitment(),
    )
    first = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(claim,),
    )
    submission = SubmissionEvent(
        submission_id="03" * 16,
        campaign_id="campaign",
        tweet_id="999",
        claim_id=claim.claim_id,
        miner_hotkey=MINER,
        creator_x_id="456",
    )
    second = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=2,
        previous_batch_hash=first.batch_hash,
        events=(submission,),
        reveals=(submitted_reveal,),
    )
    persist_batch(store, first, block=10, timestamp=claim_timestamp)
    persist_batch(store, second, block=11, timestamp=NOW + timedelta(minutes=20))
    return store


def miner_claim_history(
    *,
    hotkey: str,
    claim_id: str,
    draft: str,
    nonce: str,
    claim_block: int,
    submissions: tuple[tuple[int, str], ...],
    tweet_id: str = "999",
) -> list[tuple[int, CommittedBatch, datetime]]:
    """Build one miner's hash-linked claim and submission retry history."""

    reveal = DraftReveal(claim_id=claim_id, draft=draft, nonce=nonce)
    claim = ClaimEvent(
        claim_id=claim_id,
        campaign_id="campaign",
        creator_x_id="456",
        created_at=NOW + timedelta(minutes=1),
        draft_commitment=reveal.commitment(),
    )
    batch = CommittedBatch.create(
        miner_hotkey=hotkey,
        sequence=1,
        previous_batch_hash=None,
        events=(claim,),
    )
    history = [(claim_block, batch, NOW + timedelta(minutes=1))]
    for sequence, (block, submission_id) in enumerate(submissions, start=2):
        submission = SubmissionEvent(
            submission_id=submission_id,
            campaign_id="campaign",
            tweet_id=tweet_id,
            claim_id=claim_id,
            miner_hotkey=hotkey,
            creator_x_id="456",
        )
        next_batch = CommittedBatch.create(
            miner_hotkey=hotkey,
            sequence=sequence,
            previous_batch_hash=batch.batch_hash,
            events=(submission,),
            reveals=(reveal,),
        )
        history.append((block, next_batch, NOW + timedelta(minutes=20, seconds=sequence)))
        batch = next_batch
    return history


def submission_only_history(
    path: Path,
    *,
    block: int = 10,
    timestamp: datetime = NOW + timedelta(minutes=20),
    claim_id: str | None = None,
    creator_x_id: str | None = "456",
) -> ValidatorStore:
    """Commit MINER's single submission ``03…`` of tweet 999 with no preceding claim.

    A submission without ``creator_x_id`` is a legacy version-2 event.
    """

    store = ValidatorStore(path)
    submission = SubmissionEvent(
        version=3 if creator_x_id is not None else 2,
        submission_id="03" * 16,
        campaign_id="campaign",
        tweet_id="999",
        claim_id=claim_id,
        miner_hotkey=MINER,
        creator_x_id=creator_x_id,
    )
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(submission,),
    )
    persist_batch(store, batch, block=block, timestamp=timestamp)
    return store


def two_tweet_history(
    path: Path,
    campaign_ids: tuple[str, str] = ("campaign", "campaign"),
) -> ValidatorStore:
    """Commit MINER's direct submissions ``03…`` of tweet 998 and ``04…`` of tweet 999."""

    store = ValidatorStore(path)
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=tuple(
            SubmissionEvent(
                submission_id=submission_id,
                campaign_id=campaign_id,
                tweet_id=tweet_id,
                claim_id=None,
                miner_hotkey=MINER,
                creator_x_id="456",
            )
            for campaign_id, tweet_id, submission_id in zip(
                campaign_ids, ("998", "999"), ("03" * 16, "04" * 16), strict=True
            )
        ),
    )
    persist_batch(store, batch, block=10, timestamp=NOW + timedelta(minutes=20))
    return store


@dataclass(frozen=True)
class RewardRun:
    attributions: list[AttributionResult]
    coordinator: RewardCoordinator
    scored: list[ScoredAttribution]
    weights: dict[int, float]
    floors: list[TweetReward]


async def run_rewards(
    store: ValidatorStore,
    provider: FakeX,
    snapshot: CampaignFeed,
    *,
    block: int = 30,
    persist: bool = False,
) -> RewardRun:
    """Settle at ``block`` and compute the block-35 shadow vector."""

    attributions = await CampaignReconciler(
        store,
        provider,
        FakeQualification(),
    ).reconcile_feed(snapshot, finalized_block=block)
    coordinator = RewardCoordinator(store, AttributionScorer(provider), score_blend=0.0)
    scored = await coordinator.freeze_scores(snapshot, attributions, block=block)
    weights, floors = coordinator.shadow_weights(
        snapshot,
        scored,
        block=35,
        hotkey_to_uid={MINER: 7},
        uids=[0, 7],
        persist=persist,
    )
    return RewardRun(attributions, coordinator, scored, weights, floors)


async def test_feed_reconciliation_loads_verified_history_once(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    loads: list[int | None] = []
    load_history = store.verified_batches

    def counted(*, through_block: int | None = None) -> list[VerifiedBatchRecord]:
        loads.append(through_block)
        return load_history(through_block=through_block)

    store.verified_batches = counted  # type: ignore[method-assign]
    other = campaign().model_copy(
        update={"access": campaign().access.model_copy(update={"campaign_id": "other"})}
    )
    snapshot = feed(campaign(), other)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    results = await reconciler.reconcile_feed(snapshot, finalized_block=20)

    assert loads == [20]
    assert [item.campaign_id for item in results if item.accepted] == ["campaign"]
    assert reconciler.completed_campaign_ids == {"campaign", "other"}


async def test_open_campaign_attributes_independently_fetched_ordinary_edit(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    qualification = FakeQualification()
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        qualification,
    )

    results = await reconciler.reconcile_feed(feed(campaign()), finalized_block=20)

    assert len(results) == 1
    assert results[0].accepted is True
    assert results[0].miner_hotkey == MINER
    assert results[0].submission_id == "03" * 16
    assert results[0].claim_id == "01" * 16
    assert qualification.calls == [10, 20]


async def test_submission_cannot_reuse_a_claim_from_before_history_resume(
    tmp_path: Path,
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    reveal = DraftReveal(claim_id="01" * 16, draft=tweet().text, nonce="02" * 32)
    claim_batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(
            ClaimEvent(
                claim_id=reveal.claim_id,
                campaign_id="campaign",
                creator_x_id="456",
                created_at=NOW + timedelta(minutes=1),
                draft_commitment=reveal.commitment(),
            ),
        ),
    )
    persist_batch(store, claim_batch, block=10, timestamp=NOW + timedelta(minutes=1))
    history_id = "72" * 32
    submission_batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        history_id=history_id,
        events=(
            SubmissionEvent(
                submission_id="03" * 16,
                campaign_id="campaign",
                tweet_id="999",
                claim_id=reveal.claim_id,
                miner_hotkey=MINER,
                creator_x_id="456",
            ),
        ),
        reveals=(reveal,),
    )
    persist_batch(store, submission_batch, block=11, timestamp=NOW + timedelta(minutes=20))
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    results = await reconciler.reconcile_feed(feed(campaign()), finalized_block=20)

    assert results[0].accepted is False
    assert results[0].reason is AttributionReason.CLAIM_NOT_ACTIVE


@pytest.mark.parametrize(
    ("campaign_updates", "public_only_text", "quoted_tweet_id"),
    [
        (
            {"display": "Public launch phrase", "brief": "Unrelated", "required_terms": ()},
            "Public launch phrase",
            None,
        ),
        (
            {"brief": "Unrelated", "required_terms": (), "tag": "#Launch"},
            "#Launch",
            None,
        ),
        (
            {
                "brief": "Unrelated",
                "required_terms": (),
                "inclusion_keywords": ("public-keyword",),
            },
            "public-keyword",
            None,
        ),
        (
            {"brief": "Unrelated", "required_terms": (), "quoted_tweet_id": "123"},
            "https://t.co/public-quote",
            "123",
        ),
    ],
)
async def test_public_campaign_material_alone_does_not_prove_draft_access(
    tmp_path: Path,
    campaign_updates: dict[str, object],
    public_only_text: str,
    quoted_tweet_id: str | None,
) -> None:
    store = open_history(tmp_path / "validator.sqlite3", draft=public_only_text)
    record = campaign().model_copy(update=campaign_updates)
    published = tweet(text=public_only_text, quoted_tweet_id=quoted_tweet_id)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=published, provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(record, feed(record)))[0]

    assert result.accepted is False
    assert result.reason.value == "score_below_floor"
    assert result.winner_score == pytest.approx(0.55)


@pytest.mark.parametrize(
    ("eligible_blocks", "expected_calls"),
    [({20}, [10]), ({10}, [10, 20])],
    ids=["later_top_up_cannot_rescue_claim", "finally_rejected_at_scoring_close"],
)
async def test_open_claim_must_be_qualified_at_commitment_and_scoring_close(
    tmp_path: Path,
    eligible_blocks: set[int],
    expected_calls: list[int],
) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    qualification = QualificationAtBlocks(eligible_blocks)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        qualification,
    )

    result = (await reconciler.reconcile_campaign(record, feed(record), through_block=20))[0]

    assert result.accepted is False
    assert result.pending is False
    assert result.reason.value == "miner_not_qualified"
    assert qualification.calls == expected_calls


async def test_open_preview_can_recover_at_close_only_if_claim_was_initially_qualified(
    tmp_path: Path,
) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    qualification = QualificationAtBlocks({10, 20})
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        qualification,
    )

    pending = (await reconciler.reconcile_campaign(record, feed(record), through_block=15))[0]
    accepted = (await reconciler.reconcile_campaign(record, feed(record), through_block=20))[0]

    assert pending.accepted is False
    assert pending.pending is True
    assert pending.reason.value == "miner_not_qualified"
    assert pending.miner_hotkey == MINER
    assert pending.submission_id == "03" * 16
    assert pending.claim_id == "01" * 16
    assert accepted.accepted is True
    assert accepted.pending is False
    assert accepted.miner_hotkey == MINER
    assert qualification.calls == [10, 15, 20]


async def test_open_submission_without_a_committed_claim_is_rejected(tmp_path: Path) -> None:
    store = submission_only_history(tmp_path / "validator.sqlite3", claim_id="04" * 16)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(campaign(), feed(campaign())))[0]

    assert result.accepted is False
    assert result.reason.value == "claim_not_active"


async def test_eligible_tweet_author_must_match_the_open_claim(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    snapshot = feed(record).model_copy(
        update={
            "ecosystem_maps": (
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="Ecosystem",
                    eligible_creator_x_ids=("456", "789"),
                    updated_at=NOW,
                ),
            )
        }
    )
    other_author = tweet().model_copy(update={"author_x_id": "789", "author": "othercreator"})
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=other_author, provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(record, snapshot))[0]

    assert result.accepted is False
    assert result.reason.value == "author_mismatch"


async def test_exclusive_campaign_failure_preserves_submission_identity(tmp_path: Path) -> None:
    store = submission_only_history(tmp_path / "validator.sqlite3")
    record = campaign(exclusive=MINER)
    evidence = tweet(created_at=NOW - timedelta(days=1))
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=evidence, provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(record, feed(record)))[0]

    assert result.accepted is False
    assert result.reason is AttributionReason.POST_OUTSIDE_CAMPAIGN_WINDOW
    assert result.miner_hotkey == MINER
    assert result.submission_id == "03" * 16


async def test_late_submission_is_audited_without_fetching_x(tmp_path: Path) -> None:
    store = submission_only_history(
        tmp_path / "validator.sqlite3",
        block=21,
        timestamp=NOW + timedelta(days=2),
    )
    record = campaign(exclusive=MINER)
    reconciler = CampaignReconciler(store, FakeX({}), FakeQualification())

    result = (await reconciler.reconcile_campaign(record, feed(record), through_block=25))[0]

    assert result.accepted is False
    assert result.reason.value == "late_submission"
    assert result.miner_hotkey == MINER
    assert result.submission_id == "03" * 16


async def test_campaign_freezes_only_after_its_reconciliation_window(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign().model_copy(update={"emission_start_block": 30, "emission_end_block": 40})
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    before_emission = await reconciler.reconcile_feed(feed(record), finalized_block=29)
    at_emission = await reconciler.reconcile_feed(feed(record), finalized_block=30)

    assert before_emission == []
    assert len(at_emission) == 1
    assert at_emission[0].accepted is True


@pytest.mark.parametrize(
    (
        "exclusive",
        "creator_x_id",
        "block",
        "scoring_close_block",
        "reason",
        "identified",
        "qualification_calls",
    ),
    [
        pytest.param(
            MINER,
            "456",
            10,
            20,
            AttributionReason.ACCEPTED,
            True,
            [10, 20],
            id="skips_claim_and_matcher",
        ),
        pytest.param(
            MINER,
            None,
            CREATOR_BINDING_ACTIVATION_BLOCK - 1,
            CREATOR_BINDING_ACTIVATION_BLOCK + 1,
            AttributionReason.ACCEPTED,
            True,
            [CREATOR_BINDING_ACTIVATION_BLOCK - 1, CREATOR_BINDING_ACTIVATION_BLOCK + 1],
            id="accepts_legacy_submission_before_activation",
        ),
        pytest.param(
            MINER,
            None,
            CREATOR_BINDING_ACTIVATION_BLOCK,
            CREATOR_BINDING_ACTIVATION_BLOCK + 1,
            AttributionReason.AUTHOR_MISMATCH,
            True,
            [],
            id="rejects_missing_identity_at_activation",
        ),
        pytest.param(
            MINER,
            "789",
            CREATOR_BINDING_ACTIVATION_BLOCK,
            CREATOR_BINDING_ACTIVATION_BLOCK + 1,
            AttributionReason.AUTHOR_MISMATCH,
            True,
            [],
            id="rejects_wrong_identity_at_activation",
        ),
        pytest.param(
            OTHER_MINER,
            "456",
            10,
            20,
            AttributionReason.WRONG_EXCLUSIVE_MINER,
            False,
            [],
            id="rejects_a_different_miner",
        ),
    ],
)
async def test_exclusive_campaign_submitter_identity(
    tmp_path: Path,
    exclusive: str,
    creator_x_id: str | None,
    block: int,
    scoring_close_block: int,
    reason: AttributionReason,
    identified: bool,
    qualification_calls: list[int],
) -> None:
    store = submission_only_history(
        tmp_path / "validator.sqlite3",
        block=block,
        creator_x_id=creator_x_id,
    )
    record = campaign(exclusive=exclusive, scoring_close_block=scoring_close_block)
    qualification = FakeQualification()
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        qualification,
    )

    result = (await reconciler.reconcile_campaign(record, feed(record)))[0]

    assert result.accepted is (reason is AttributionReason.ACCEPTED)
    assert result.reason is reason
    assert result.claim_id is None
    assert result.miner_hotkey == (MINER if identified else None)
    assert result.submission_id == ("03" * 16 if identified else None)
    assert qualification.calls == qualification_calls


async def test_exclusive_submission_unqualified_at_commitment_cannot_be_rescued(
    tmp_path: Path,
) -> None:
    store = submission_only_history(tmp_path / "validator.sqlite3")
    record = campaign(exclusive=MINER)
    qualification = QualificationAtBlocks({20})
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        qualification,
    )

    result = (await reconciler.reconcile_campaign(record, feed(record), through_block=20))[0]

    assert result.accepted is False
    assert result.pending is False
    assert result.reason.value == "miner_not_qualified"
    assert result.miner_hotkey == MINER
    assert result.submission_id == "03" * 16
    assert qualification.calls == [10]


@pytest.mark.parametrize(
    ("victim_blocks", "attacker_blocks"),
    [
        ((10, 12, 14), (11, 13)),
        ((11, 13, 14), (10, 12)),
    ],
)
async def test_claim_ids_are_namespaced_by_miner_across_order_and_retry(
    tmp_path: Path,
    victim_blocks: tuple[int, int, int],
    attacker_blocks: tuple[int, int],
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    claim_id = "01" * 16
    history = miner_claim_history(
        hotkey=MINER,
        claim_id=claim_id,
        draft=tweet().text,
        nonce="02" * 32,
        claim_block=victim_blocks[0],
        submissions=(
            (victim_blocks[1], "03" * 16),
            (victim_blocks[2], "05" * 16),
        ),
    )
    history.extend(
        miner_claim_history(
            hotkey=OTHER_MINER,
            claim_id=claim_id,
            draft="Talk about the wallet. #Launch",
            nonce="04" * 32,
            claim_block=attacker_blocks[0],
            submissions=((attacker_blocks[1], "04" * 16),),
        )
    )
    for block, batch, timestamp in sorted(history, key=lambda item: item[0]):
        persist_batch(store, batch, block=block, timestamp=timestamp)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(campaign(), feed(campaign())))[0]

    assert result.accepted is True
    assert result.miner_hotkey == MINER
    assert result.claim_id == claim_id
    assert result.submission_id == "05" * 16


async def test_consuming_claim_id_for_one_miner_does_not_consume_another_miners_claim(
    tmp_path: Path,
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    claim_id = "01" * 16
    history = miner_claim_history(
        hotkey=MINER,
        claim_id=claim_id,
        draft=tweet("999").text,
        nonce="02" * 32,
        claim_block=10,
        submissions=((12, "03" * 16),),
        tweet_id="999",
    )
    history.extend(
        miner_claim_history(
            hotkey=OTHER_MINER,
            claim_id=claim_id,
            draft=tweet("998").text,
            nonce="04" * 32,
            claim_block=11,
            submissions=((13, "04" * 16),),
            tweet_id="998",
        )
    )
    for block, batch, timestamp in sorted(history, key=lambda item: item[0]):
        persist_batch(store, batch, block=block, timestamp=timestamp)
    reconciler = CampaignReconciler(
        store,
        FakeX(
            {
                "998": TweetFetch(tweet=tweet("998"), provider_available=True),
                "999": TweetFetch(tweet=tweet("999"), provider_available=True),
            }
        ),
        FakeQualification(),
    )

    results = await reconciler.reconcile_campaign(campaign(), feed(campaign()))

    assert {(item.tweet_id, item.miner_hotkey) for item in results} == {
        ("998", OTHER_MINER),
        ("999", MINER),
    }


def claim_event(reveal: DraftReveal) -> ClaimEvent:
    return ClaimEvent(
        claim_id=reveal.claim_id,
        campaign_id="campaign",
        creator_x_id="456",
        created_at=NOW + timedelta(minutes=1),
        draft_commitment=reveal.commitment(),
    )


@pytest.mark.parametrize(
    ("referenced_claim", "accepted"),
    [(0, False), (1, True)],
    ids=["evicted_oldest_claim", "oldest_claim_still_active"],
)
async def test_sixth_claim_evicts_the_oldest_from_the_active_set(
    tmp_path: Path,
    referenced_claim: int,
    accepted: bool,
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    # Claim IDs descend while chain positions ascend, so eviction must follow position.
    reveals = [
        DraftReveal(
            claim_id=f"{MAX_ACTIVE_CLAIMS + 1 - index:02x}" * 16,
            draft=tweet().text,
            nonce=f"{index + 1:02x}" * 32,
        )
        for index in range(MAX_ACTIVE_CLAIMS + 1)
    ]
    previous_hash = None
    for sequence, reveal in enumerate(reveals, start=1):
        claim_batch = CommittedBatch.create(
            miner_hotkey=MINER,
            sequence=sequence,
            previous_batch_hash=previous_hash,
            events=(claim_event(reveal),),
        )
        persist_batch(store, claim_batch, block=9 + sequence, timestamp=NOW + timedelta(minutes=1))
        previous_hash = claim_batch.batch_hash
    referenced = reveals[referenced_claim]
    submission_batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=len(reveals) + 1,
        previous_batch_hash=previous_hash,
        events=(
            SubmissionEvent(
                submission_id="aa" * 16,
                campaign_id="campaign",
                tweet_id="999",
                claim_id=referenced.claim_id,
                miner_hotkey=MINER,
                creator_x_id="456",
            ),
        ),
        reveals=(referenced,),
    )
    persist_batch(store, submission_batch, block=17, timestamp=NOW + timedelta(minutes=20))
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(campaign(), feed(campaign())))[0]

    # Whether an evicted claim becomes active again once a newer claim is consumed is an
    # open protocol decision, so it is deliberately not pinned here.
    assert result.accepted is accepted
    if accepted:
        assert result.claim_id == referenced.claim_id
        assert result.submission_id == "aa" * 16
    else:
        assert result.reason is AttributionReason.CLAIM_NOT_ACTIVE
        assert result.claim_id is None


@pytest.mark.parametrize("same_batch", [False, True], ids=["separate-batches", "one-batch"])
async def test_consumed_claim_cannot_win_a_second_tweet(tmp_path: Path, same_batch: bool) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    reveal = DraftReveal(claim_id="01" * 16, draft=tweet().text, nonce="02" * 32)
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(claim_event(reveal),),
    )
    persist_batch(store, batch, block=10, timestamp=NOW + timedelta(minutes=1))
    submissions = [
        SubmissionEvent(
            submission_id=submission_id,
            campaign_id="campaign",
            tweet_id=tweet_id,
            claim_id=reveal.claim_id,
            miner_hotkey=MINER,
            creator_x_id="456",
        )
        for tweet_id, submission_id in (("998", "03" * 16), ("999", "04" * 16))
    ]
    # Miners reveal a shared claim once per batch, so both submissions can
    # arrive together with a single reveal.
    groups = [tuple(submissions)] if same_batch else [(item,) for item in submissions]
    for sequence, events in enumerate(groups, start=2):
        batch = CommittedBatch.create(
            miner_hotkey=MINER,
            sequence=sequence,
            previous_batch_hash=batch.batch_hash,
            events=events,
            reveals=(reveal,),
        )
        persist_batch(store, batch, block=9 + sequence, timestamp=NOW + timedelta(minutes=20))
    reconciler = CampaignReconciler(
        store,
        FakeX(
            {
                "998": TweetFetch(
                    tweet=tweet("998", created_at=NOW + timedelta(minutes=10)),
                    provider_available=True,
                ),
                "999": TweetFetch(
                    tweet=tweet("999", created_at=NOW + timedelta(minutes=15)),
                    provider_available=True,
                ),
            }
        ),
        FakeQualification(),
    )

    results = await reconciler.reconcile_campaign(campaign(), feed(campaign()))

    by_tweet = {item.tweet_id: item for item in results}
    assert by_tweet["998"].accepted is True
    assert by_tweet["998"].claim_id == reveal.claim_id
    assert by_tweet["998"].submission_id == "03" * 16
    assert by_tweet["999"].accepted is False
    assert by_tweet["999"].reason is AttributionReason.CLAIM_NOT_ACTIVE


def test_legacy_null_language_placeholder_preserves_frozen_campaign_replay(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "validator.sqlite3"
    store = ValidatorStore(path)
    record = campaign()
    current_json = record.model_dump_json()
    legacy_payload = json.loads(current_json)
    legacy_payload["language"] = None
    legacy_json = json.dumps(legacy_payload, sort_keys=True)
    assert legacy_json != current_json
    accepted = AttributionResult(
        tweet_id="999",
        campaign_id="campaign",
        accepted=True,
        reason=AttributionReason.ACCEPTED,
        miner_hotkey=MINER,
        submission_id="03" * 16,
        claim_id="01" * 16,
    )
    reward = TweetReward(
        campaign_id="campaign",
        tweet_id="999",
        creator_x_id="456",
        miner_hotkey=MINER,
        score=1.0,
        daily_usd_floor=1.0,
    )
    # Freeze a positive allocation whose every stored contract is the legacy JSON.
    store.bind_campaign_protocols((record,))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE campaign_protocols SET campaign_contract_json = ? WHERE campaign_id = ?",
            (legacy_json, "campaign"),
        )
    store.persist_reconciliation(
        snapshot_id="snapshot",
        campaign_id="campaign",
        campaign_json=legacy_json,
        results=[accepted],
    )
    store.persist_campaign_rewards(
        snapshot_id="snapshot",
        campaign_id="campaign",
        campaign_json=legacy_json,
        rewards=[reward],
        decisions=[],
    )
    assert store.campaign_finalized("campaign") is True

    # Replaying the frozen state under the current serialization is a no-op, not a mutation.
    store.persist_reconciliation(
        snapshot_id="snapshot-2",
        campaign_id="campaign",
        campaign_json=current_json,
        results=[accepted],
    )
    store.persist_campaign_rewards(
        snapshot_id="snapshot-2",
        campaign_id="campaign",
        campaign_json=current_json,
        rewards=[reward],
        decisions=[],
    )
    assert store.reconciliation("snapshot-2", "campaign", current_json) == [accepted]
    assert store.campaign_rewards("campaign", current_json) == ([reward], [])
    assert store.reconciled_campaigns() == [record]
    with caplog.at_level(logging.ERROR, logger="bitcast_x.validator.store"):
        assert store.bind_campaign_protocols((record,)) == (record,)
    assert "rejected campaign mutation" not in caplog.text


async def test_provider_outage_is_pending_in_final_feed_after_the_grace_period(
    tmp_path: Path,
) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    snapshot = feed(record)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=None, provider_available=False)}),
        FakeQualification(),
    )

    results = await reconciler.reconcile_feed(
        snapshot, finalized_block=record.settlement_block + EVIDENCE_GRACE_BLOCKS
    )

    assert len(results) == 1
    result = results[0]
    assert result.pending is True
    assert result.reason is AttributionReason.EVIDENCE_UNAVAILABLE
    assert (
        store.reconciliation(
            snapshot.snapshot_id,
            record.access.campaign_id,
            record.model_dump_json(),
        )
        == results
    )


async def test_authoritative_tweet_absence_freezes_a_rejection(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    snapshot = feed(record)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=None, provider_available=True)}),
        FakeQualification(),
    )

    results = await reconciler.reconcile_feed(snapshot, finalized_block=20)

    assert len(results) == 1
    result = results[0]
    assert result.accepted is False
    assert result.reason.value == "tweet_not_found"
    assert (
        store.reconciliation(
            snapshot.snapshot_id,
            record.access.campaign_id,
            record.model_dump_json(),
        )
        == results
    )


def _exclusive_final_campaign(campaign_id: str) -> CampaignRecord:
    record = campaign(exclusive=MINER)
    return record.model_copy(
        update={
            "access": record.access.model_copy(update={"campaign_id": campaign_id}),
            "emission_start_block": 30,
            "emission_end_block": 40,
        }
    )


def _two_campaign_finalization(
    tmp_path: Path,
) -> tuple[ValidatorStore, CampaignFeed, CampaignRecord, CampaignRecord]:
    store = two_tweet_history(tmp_path / "validator.sqlite3", ("campaign-a", "campaign-b"))
    campaign_a = _exclusive_final_campaign("campaign-a")
    campaign_b = _exclusive_final_campaign("campaign-b")
    return store, feed(campaign_a, campaign_b, influence=10.0), campaign_a, campaign_b


# _exclusive_final_campaign settles from block 30.
AFTER_GRACE = 30 + EVIDENCE_GRACE_BLOCKS


def _unavailable_998_provider() -> FakeX:
    return FakeX(
        {
            "998": TweetFetch(tweet=None, provider_available=False),
            "999": TweetFetch(tweet=tweet("999"), provider_available=True),
        }
    )


class _SelectiveEngagementProvider(FakeX):
    async def fetch_engagements(self, tweet_id: str) -> EngagementFetch:
        return EngagementFetch(engagements={}, provider_available=tweet_id != "998")


def _unavailable_998_engagements() -> FakeX:
    return _SelectiveEngagementProvider(
        {
            "998": TweetFetch(tweet=tweet("998"), provider_available=True),
            "999": TweetFetch(tweet=tweet("999"), provider_available=True),
        }
    )


@pytest.mark.parametrize(
    "provider",
    [_unavailable_998_provider, _unavailable_998_engagements],
    ids=["tweet", "engagements"],
)
async def test_unavailable_evidence_holds_settlement_until_it_returns(
    tmp_path: Path, provider: Callable[[], FakeX]
) -> None:
    store = two_tweet_history(tmp_path / "validator.sqlite3")
    snapshot = feed(_exclusive_final_campaign("campaign"), influence=10.0)

    waiting = await run_rewards(store, provider(), snapshot, block=AFTER_GRACE - 1)

    # Within the grace period the campaign waits rather than freezing without the
    # tweet, so every validator settles on the same evidence once it returns.
    assert waiting.floors == []
    assert waiting.coordinator.pending_reward_campaign_ids(snapshot, block=35) == ("campaign",)
    healthy = FakeX(
        {
            "998": TweetFetch(tweet=tweet("998"), provider_available=True),
            "999": TweetFetch(tweet=tweet("999"), provider_available=True),
        }
    )
    settled = await run_rewards(store, healthy, snapshot, block=AFTER_GRACE - 1)
    assert sorted(item.tweet_id for item in settled.floors) == ["998", "999"]
    assert settled.coordinator.pending_reward_campaign_ids(snapshot, block=35) == ()


async def test_unavailable_tweet_does_not_block_its_campaign_rewards(tmp_path: Path) -> None:
    store = two_tweet_history(tmp_path / "validator.sqlite3")
    snapshot = feed(_exclusive_final_campaign("campaign"), influence=10.0)

    run = await run_rewards(store, _unavailable_998_provider(), snapshot, block=AFTER_GRACE)

    # Available evidence is ordered first; the unavailable tweet keeps its submission identity.
    assert [item.tweet_id for item in run.attributions] == ["999", "998"]
    results_by_tweet = {item.tweet_id: item for item in run.attributions}
    assert results_by_tweet["998"].pending is True
    assert results_by_tweet["998"].reason is AttributionReason.EVIDENCE_UNAVAILABLE
    assert results_by_tweet["998"].miner_hotkey == MINER
    assert results_by_tweet["998"].submission_id == "03" * 16
    assert results_by_tweet["999"].accepted is True
    assert results_by_tweet["999"].submission_id == "04" * 16
    assert [item.tweet_id for item in run.floors] == ["999"]
    assert run.floors[0].daily_usd_floor == pytest.approx(1000 / 7)
    assert run.weights == {0: 0.0, 7: 1.0}
    assert run.coordinator.pending_reward_campaign_ids(snapshot, block=35) == ()


async def test_finalization_isolates_an_unavailable_tweet(tmp_path: Path) -> None:
    store, snapshot, campaign_a, campaign_b = _two_campaign_finalization(tmp_path)

    run = await run_rewards(store, _unavailable_998_provider(), snapshot, block=AFTER_GRACE)
    publisher = MultiCampaignPublisher()
    published = await ShadowResultPublisher(
        store,
        publisher,  # type: ignore[arg-type]
        endpoint="https://ingestion.example/api/v1/brief-tweets",
        preview_store=PreviewStore(tmp_path / "preview.sqlite3"),
    ).publish(
        snapshot,
        run.scored,
        run.floors,
        block=35,
        hotkey_to_uid={MINER: 7},
        completed_campaign_ids=run.coordinator.completed_campaign_ids,
    )

    assert [item.campaign_id for item in run.attributions] == ["campaign-a", "campaign-b"]
    campaign_a_results = store.reconciliation(
        snapshot.snapshot_id,
        "campaign-a",
        campaign_a.model_dump_json(),
    )
    assert campaign_a_results is not None
    assert campaign_a_results[0].pending is True
    assert campaign_a_results[0].reason is AttributionReason.EVIDENCE_UNAVAILABLE
    assert campaign_a_results[0].miner_hotkey == MINER
    assert campaign_a_results[0].submission_id == "03" * 16
    assert (
        store.reconciliation(
            snapshot.snapshot_id,
            "campaign-b",
            campaign_b.model_dump_json(),
        )
        is not None
    )
    assert run.weights == {0: 0.0, 7: 1.0}
    assert run.coordinator.pending_reward_campaign_ids(snapshot, block=35) == ()
    assert store.campaign_rewards("campaign-a", campaign_a.model_dump_json()) is None
    assert store.campaign_rewards("campaign-b", campaign_b.model_dump_json()) is not None
    assert published == 1
    assert len(publisher.run_ids) == 2
    assert publisher.run_ids[0].startswith("v3-preview:snapshot:campaign-a:")
    assert publisher.run_ids[1] == "v3:snapshot:campaign-b"
    campaign_a_decision = publisher.payloads[0]["attribution_decisions"][0]  # type: ignore[index]
    assert campaign_a_decision["status"] == "pending"
    assert campaign_a_decision["reason"] == "evidence_unavailable"
    assert campaign_a_decision["reward_status"] == "pending"


async def test_final_scoring_isolates_an_unavailable_tweet(tmp_path: Path) -> None:
    store, snapshot, campaign_a, campaign_b = _two_campaign_finalization(tmp_path)

    run = await run_rewards(store, _unavailable_998_engagements(), snapshot, block=AFTER_GRACE)

    assert [item.attribution.campaign_id for item in run.scored] == ["campaign-b"]
    assert store.scored_reconciliation("campaign-a") == []
    assert store.scored_reconciliation("campaign-b") is not None
    assert run.coordinator.pending_reward_campaign_ids(snapshot, block=35) == ()
    assert store.campaign_rewards("campaign-a", campaign_a.model_dump_json()) is None
    assert store.campaign_rewards("campaign-b", campaign_b.model_dump_json()) is not None


async def test_eligibility_remains_after_creator_drops_below_rank_cutoff(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign().model_copy(update={"max_members": 1})
    snapshot = feed(record).model_copy(
        update={
            "ecosystem_maps": (
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="Initially eligible",
                    eligible_creator_x_ids=("123", "456"),
                    updated_at=NOW,
                    accounts=(
                        SocialAccount(x_id="456", username="creator", influence=2.0),
                        SocialAccount(x_id="123", username="leader", influence=1.0),
                    ),
                ),
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="Rank dropped",
                    eligible_creator_x_ids=("123", "456"),
                    updated_at=NOW + timedelta(hours=1),
                    accounts=(
                        SocialAccount(x_id="123", username="leader", influence=2.0),
                        SocialAccount(x_id="456", username="creator", influence=1.0),
                    ),
                ),
            )
        }
    )
    reconciler = CampaignReconciler(
        store,
        FakeX(
            {
                "999": TweetFetch(
                    tweet=tweet(created_at=NOW + timedelta(hours=2)),
                    provider_available=True,
                )
            }
        ),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(record, snapshot))[0]

    assert result.accepted is True


async def test_rank_cutoff_rejects_explicit_map_member_below_top_n(tmp_path: Path) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign().model_copy(update={"max_members": 1})
    snapshot = feed(record).model_copy(
        update={
            "ecosystem_maps": (
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="Active map",
                    eligible_creator_x_ids=("123", "456"),
                    updated_at=NOW,
                    accounts=(
                        SocialAccount(x_id="123", username="leader", influence=2.0),
                        SocialAccount(x_id="456", username="creator", influence=1.0),
                    ),
                ),
            )
        }
    )
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(record, snapshot))[0]

    assert result.accepted is False
    assert result.reason is AttributionReason.CREATOR_NOT_ELIGIBLE_FOR_CAMPAIGN


async def test_missing_historical_map_leaves_tweet_pending_in_completed_campaign(
    tmp_path: Path,
) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    snapshot = feed(record).model_copy(
        update={
            "ecosystem_maps": (
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="After campaign",
                    eligible_creator_x_ids=("456",),
                    updated_at=NOW + timedelta(days=2),
                ),
            )
        }
    )
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
        FakeQualification(),
    )

    results = await reconciler.reconcile_feed(
        snapshot, finalized_block=record.settlement_block + EVIDENCE_GRACE_BLOCKS
    )

    # After the grace period, the missing map defers only the tweet: it stays pending,
    # never rejected, while the campaign itself completes for this cycle.
    assert len(results) == 1
    assert results[0].tweet_id == "999"
    assert results[0].accepted is False
    assert results[0].pending is True
    assert results[0].reason is AttributionReason.EVIDENCE_UNAVAILABLE
    assert reconciler.completed_campaign_ids == {"campaign"}
    assert store.reconciliation(snapshot.snapshot_id, "campaign", record.model_dump_json()) == (
        results
    )


@pytest.mark.parametrize(
    ("history_update", "campaign_update", "tweet_update", "expected_reason"),
    [
        pytest.param(
            {},
            {"required_terms": ("required phrase",)},
            {},
            AttributionReason.REQUIRED_TERMS_MISSING,
            id="required_terms_missing",
        ),
        pytest.param(
            {},
            {},
            {"text": "RT @someone: #Launch wallet"},
            AttributionReason.RETWEET_NOT_ALLOWED,
            id="retweet",
        ),
        pytest.param(
            {},
            {},
            {"in_reply_to_status_id": "1"},
            AttributionReason.REPLY_NOT_ALLOWED,
            id="reply",
        ),
        pytest.param(
            {},
            {"tag": "@bitcast"},
            {},
            AttributionReason.CAMPAIGN_TAG_MISSING,
            id="campaign_tag_missing",
        ),
        pytest.param(
            {},
            {"quoted_tweet_id": "123"},
            {"quoted_tweet_id": "456"},
            AttributionReason.REQUIRED_QUOTE_MISSING_OR_INCORRECT,
            id="wrong_quote",
        ),
        pytest.param(
            {},
            {"inclusion_keywords": ("airdrop", "rewards")},
            {},
            AttributionReason.REQUIRED_CAMPAIGN_KEYWORD_MISSING,
            id="inclusion_keyword_missing",
        ),
        pytest.param(
            {"claim_timestamp": NOW + timedelta(minutes=10, seconds=1)},
            {},
            {"created_at": NOW + timedelta(minutes=10)},
            AttributionReason.CLAIM_AFTER_PUBLICATION,
            id="claim_finalized_after_publication",
        ),
        pytest.param(
            {"revealed_draft": "A different draft was revealed after publication. #Launch"},
            {},
            {},
            AttributionReason.DRAFT_REVEAL_MISMATCH,
            id="changed_reveal",
        ),
        pytest.param(
            {},
            {},
            {"created_at": NOW + timedelta(days=1, seconds=1)},
            AttributionReason.POST_OUTSIDE_CAMPAIGN_WINDOW,
            id="published_after_campaign_close",
        ),
    ],
)
async def test_v2_content_prefilters_reject_ineligible_submissions(
    tmp_path: Path,
    history_update: dict[str, Any],
    campaign_update: dict[str, object],
    tweet_update: dict[str, object],
    expected_reason: AttributionReason,
) -> None:
    store = open_history(tmp_path / "validator.sqlite3", **history_update)
    record = campaign().model_copy(update=campaign_update)
    evidence = tweet().model_copy(update=tweet_update)
    reconciler = CampaignReconciler(
        store,
        FakeX({"999": TweetFetch(tweet=evidence, provider_available=True)}),
        FakeQualification(),
    )

    result = (await reconciler.reconcile_campaign(record, feed(record)))[0]

    assert result.accepted is False
    assert result.reason is expected_reason


async def test_campaign_freeze_survives_feed_snapshot_rotation_and_rejects_mutation(
    tmp_path: Path,
) -> None:
    store = open_history(tmp_path / "validator.sqlite3")
    record = campaign()
    provider = FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)})
    reconciler = CampaignReconciler(store, provider, FakeQualification())

    first = await reconciler.reconcile_feed(feed(record), finalized_block=20)
    store.persist_campaign_rewards(
        snapshot_id="snapshot",
        campaign_id=record.access.campaign_id,
        campaign_json=record.model_dump_json(),
        rewards=[
            TweetReward(
                campaign_id=record.access.campaign_id,
                tweet_id="999",
                creator_x_id="456",
                miner_hotkey=MINER,
                score=1.0,
                daily_usd_floor=1.0,
            )
        ],
        decisions=[],
    )
    rotated = feed(record).model_copy(update={"snapshot_id": "snapshot-2"})
    replay = await reconciler.reconcile_feed(rotated, finalized_block=20)

    assert replay == first
    changed = record.model_copy(update={"display": "Mutated after close"})
    with pytest.raises(ProtocolError, match="changed after frozen reconciliation"):
        await reconciler.reconcile_feed(
            feed(changed).model_copy(update={"snapshot_id": "snapshot-3"}),
            finalized_block=20,
        )


async def test_independent_restarted_validators_produce_identical_full_shadow_reports(
    tmp_path: Path,
) -> None:
    reports: list[dict[str, object]] = []
    for operator in ("validator-a", "validator-b"):
        state_dir = tmp_path / operator
        path = state_dir / "validator.sqlite3"
        open_history(path)
        record = campaign().model_copy(
            update={"emission_start_block": 30, "emission_end_block": 40}
        )

        run = await run_rewards(
            ValidatorStore(path),
            FakeX({"999": TweetFetch(tweet=tweet(), provider_available=True)}),
            feed(record, influence=10.0),
            persist=True,
        )

        assert run.weights == {0: 0.0, 7: 1.0}
        reports.append(shadow_report(state_dir))

    assert reports[0] == reports[1]
    assert reports[0]["campaigns_frozen"] == 1
    assert reports[0]["shadow_blocks"] == 1
