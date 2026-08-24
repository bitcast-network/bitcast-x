"""End-to-end offline tests for durable miner SDK batching and recovery."""

from dataclasses import replace
from pathlib import Path

import pytest

from bitcast_x.errors import ChainOperationError, ProtocolError
from bitcast_x.miner import (
    BatchPolicy,
    CapacityBudget,
    EventStatus,
    FinalizedCommitment,
    MinerEngine,
    MinerSdk,
    MinerStore,
)
from bitcast_x.protocol import ClaimEvent, CommitmentEnvelope, CommitmentPosition, DraftReveal
from bitcast_x.transport import BatchPageRequest

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"


class FakeSubmitter:
    def __init__(self) -> None:
        self.available = True
        self.latest_commitment: FinalizedCommitment | None = None
        self.submissions = 0

    async def capacity(self, envelope: CommitmentEnvelope) -> CapacityBudget:
        assert len(envelope.encode()) == 45
        return CapacityBudget(
            remaining_space=100 if self.available else 0,
            next_call_charge=100,
        )

    async def latest(self) -> FinalizedCommitment | None:
        return self.latest_commitment

    async def submit(self, envelope: CommitmentEnvelope) -> FinalizedCommitment:
        self.submissions += 1
        finalized = FinalizedCommitment(
            position=CommitmentPosition(block=100 + self.submissions, extrinsic_index=3),
            stored_envelope=envelope.encode(),
        )
        self.latest_commitment = finalized
        return finalized


def build_sdk(
    path: Path,
    submitter: FakeSubmitter,
    *,
    policy: BatchPolicy | None = None,
) -> MinerSdk:
    store = MinerStore(path)
    engine = MinerEngine(
        miner_hotkey=MINER,
        store=store,
        submitter=submitter,
        policy=policy or BatchPolicy(max_age_seconds=5, max_events=100, max_batch_bytes=100_000),
    )
    return MinerSdk(engine)


@pytest.mark.asyncio
async def test_claim_becomes_safe_only_after_finalized_batch(tmp_path: Path) -> None:
    submitter = FakeSubmitter()
    sdk = build_sdk(tmp_path / "miner.db", submitter)
    claim_id = sdk.create_claim(
        campaign_id="campaign",
        creator_x_id="123",
        draft="A private draft",
    )

    assert sdk.claim_status(claim_id) is EventStatus.WAITING_FOR_COMMITMENT
    assert await sdk.engine.commit_ready() is None

    batch = await sdk.engine.commit_ready(force=True)

    assert batch is not None
    assert sdk.claim_status(claim_id) is EventStatus.SAFE_TO_POST
    assert submitter.submissions == 1


@pytest.mark.asyncio
async def test_submission_batch_carries_required_reveal_and_is_pageable(tmp_path: Path) -> None:
    submitter = FakeSubmitter()
    sdk = build_sdk(tmp_path / "miner.db", submitter)
    claim_id = sdk.create_claim(
        campaign_id="campaign",
        creator_x_id="123",
        draft="A private draft",
    )
    await sdk.engine.commit_ready(force=True)
    submission_id = sdk.submit_tweet(
        campaign_id="campaign",
        tweet_id="999",
        claim_id=claim_id,
        creator_x_id="123",
    )

    batch = await sdk.engine.commit_ready(force=True)
    page = await sdk.engine.batch_page(
        BatchPageRequest(after_sequence=1, max_batches=10),
        caller_hotkey="validator",
    )

    assert batch is not None
    assert batch.sequence == 2
    assert [reveal.claim_id for reveal in batch.reveals] == [claim_id]
    assert sdk.submission_status(submission_id) is EventStatus.VERIFICATION_PENDING
    assert page.next_sequence == 2
    assert page.has_more is False
    assert page.batches[0].batch["batch_hash"] == batch.batch_hash
    assert page.batches[0].position.block == 102

    assert sdk.submissions() == [
        {
            "submission_id": submission_id,
            "campaign_id": "campaign",
            "tweet_id": "999",
            "claim_id": claim_id,
            "creator_x_id": "123",
            "status": "verification_pending",
            "created_ns": sdk.submissions()[0]["created_ns"],
        }
    ]
    sdk.record_submission_result(submission_id, EventStatus.ATTRIBUTED)
    assert sdk.submission_status(submission_id) is EventStatus.ATTRIBUTED
    sdk.record_submission_result(submission_id, EventStatus.ATTRIBUTED)
    with pytest.raises(ProtocolError, match="final submission result changed"):
        sdk.record_submission_result(submission_id, EventStatus.REJECTED)


@pytest.mark.asyncio
async def test_page_truncates_at_complete_batch_before_response_byte_limit(
    tmp_path: Path,
) -> None:
    sdk = build_sdk(tmp_path / "miner.db", FakeSubmitter())
    for creator in ("123", "456"):
        sdk.create_claim(campaign_id="campaign", creator_x_id=creator, draft="private draft")
        await sdk.engine.commit_ready(force=True)
    one_batch = await sdk.engine.batch_page(
        BatchPageRequest(after_sequence=0, max_batches=1),
        caller_hotkey="validator",
    )
    sdk.engine.policy = replace(
        sdk.engine.policy,
        max_page_bytes=len(one_batch.model_dump_json().encode()),
    )

    bounded = await sdk.engine.batch_page(
        BatchPageRequest(after_sequence=0, max_batches=10),
        caller_hotkey="validator",
    )

    assert [item.batch["sequence"] for item in bounded.batches] == [1]
    assert bounded.next_sequence == 1
    assert bounded.has_more is True
    assert len(bounded.model_dump_json().encode()) <= sdk.engine.policy.max_page_bytes


@pytest.mark.asyncio
async def test_page_is_pinned_to_validator_snapshot_sequence(tmp_path: Path) -> None:
    sdk = build_sdk(tmp_path / "miner.db", FakeSubmitter())
    for creator in ("123", "456"):
        sdk.create_claim(campaign_id="campaign", creator_x_id=creator, draft="private draft")
        await sdk.engine.commit_ready(force=True)

    page = await sdk.engine.batch_page(
        BatchPageRequest(after_sequence=0, through_sequence=1, max_batches=10),
        caller_hotkey="validator",
    )

    assert [item.batch["sequence"] for item in page.batches] == [1]
    assert page.next_sequence == 1
    assert page.has_more is False


@pytest.mark.asyncio
async def test_restart_recovers_prepared_batch_without_duplicate_commit(tmp_path: Path) -> None:
    database = tmp_path / "miner.db"
    submitter = FakeSubmitter()
    first_sdk = build_sdk(database, submitter)
    claim_id = first_sdk.create_claim(
        campaign_id="campaign",
        creator_x_id="123",
        draft="A private draft",
    )
    queued = first_sdk.engine.store.queued(limit=100)
    prepared = first_sdk.engine.store.prepare_batch(MINER, tuple(event for event, _ in queued))
    envelope = CommitmentEnvelope(
        sequence=prepared.sequence,
        event_count=len(prepared.events),
        batch_hash=bytes.fromhex(prepared.batch_hash),
    )
    submitter.latest_commitment = FinalizedCommitment(
        position=CommitmentPosition(block=101, extrinsic_index=3),
        stored_envelope=envelope.encode(),
    )

    restarted_sdk = build_sdk(database, submitter)
    recovered = await restarted_sdk.engine.commit_ready(force=True)

    assert recovered == prepared
    assert submitter.submissions == 0
    assert restarted_sdk.claim_status(claim_id) is EventStatus.SAFE_TO_POST


@pytest.mark.asyncio
async def test_capacity_exhaustion_preserves_prepared_batch(tmp_path: Path) -> None:
    submitter = FakeSubmitter()
    submitter.available = False
    sdk = build_sdk(tmp_path / "miner.db", submitter)
    claim_id = sdk.create_claim(
        campaign_id="campaign",
        creator_x_id="123",
        draft="A private draft",
    )

    with pytest.raises(ChainOperationError, match="capacity is exhausted"):
        await sdk.engine.commit_ready(force=True)

    assert sdk.engine.store.pending_batch() is not None
    assert sdk.claim_status(claim_id) is EventStatus.WAITING_FOR_COMMITMENT


def test_pending_queue_applies_backpressure_before_unbounded_growth(tmp_path: Path) -> None:
    sdk = build_sdk(
        tmp_path / "miner.db",
        FakeSubmitter(),
        policy=BatchPolicy(
            max_age_seconds=5,
            max_events=100,
            max_batch_bytes=100_000,
            max_pending_events=1,
            max_pending_bytes=100_000,
        ),
    )
    sdk.create_claim(campaign_id="campaign", creator_x_id="123", draft="first")

    with pytest.raises(ProtocolError, match="queue capacity is exhausted"):
        sdk.create_claim(campaign_id="campaign", creator_x_id="456", draft="second")


def test_duplicate_event_id_is_idempotent_but_conflicts_fail(tmp_path: Path) -> None:
    submitter = FakeSubmitter()
    sdk = build_sdk(tmp_path / "miner.db", submitter)
    reveal = DraftReveal(
        claim_id="01" * 16,
        draft="A private draft",
        nonce="02" * 32,
    )
    event = ClaimEvent(
        claim_id=reveal.claim_id,
        campaign_id="campaign",
        creator_x_id="123",
        created_at="2026-08-05T12:00:00Z",
        draft_commitment=reveal.commitment(),
    )

    sdk.engine.enqueue(event, reveal=reveal)
    sdk.engine.enqueue(event, reveal=reveal)

    assert sdk.claim_status(reveal.claim_id) is EventStatus.WAITING_FOR_COMMITMENT


def test_submission_rejects_claim_owned_by_another_miner(tmp_path: Path) -> None:
    sdk = build_sdk(tmp_path / "miner.db", FakeSubmitter())

    with pytest.raises(ProtocolError, match="does not belong"):
        sdk.submit_tweet(
            campaign_id="campaign",
            tweet_id="999",
            claim_id="01" * 16,
            creator_x_id="123",
        )


def test_submission_identity_is_idempotent_across_restart(tmp_path: Path) -> None:
    database = tmp_path / "miner.db"
    first = build_sdk(database, FakeSubmitter())
    submission_id = first.submit_tweet(
        campaign_id="campaign",
        tweet_id="999",
        claim_id=None,
        creator_x_id="123",
    )

    restarted = build_sdk(database, FakeSubmitter())
    repeated_id = restarted.submit_tweet(
        campaign_id="campaign",
        tweet_id="999",
        claim_id=None,
        creator_x_id="123",
    )

    assert repeated_id == submission_id
    assert len(restarted.submissions()) == 1


def test_submission_identity_includes_the_signed_creator(tmp_path: Path) -> None:
    sdk = build_sdk(tmp_path / "miner.db", FakeSubmitter())

    first_id = sdk.submit_tweet(
        campaign_id="campaign",
        tweet_id="999",
        claim_id=None,
        creator_x_id="123",
    )
    second_id = sdk.submit_tweet(
        campaign_id="campaign",
        tweet_id="999",
        claim_id=None,
        creator_x_id="456",
    )

    assert second_id != first_id
    assert {item["creator_x_id"] for item in sdk.submissions()} == {"123", "456"}


@pytest.mark.asyncio
async def test_batch_limit_covers_complete_payload_and_private_reveal(tmp_path: Path) -> None:
    store = MinerStore(tmp_path / "miner.db")
    engine = MinerEngine(
        miner_hotkey=MINER,
        store=store,
        submitter=FakeSubmitter(),
        policy=BatchPolicy(max_age_seconds=5, max_events=100, max_batch_bytes=10_000),
    )
    sdk = MinerSdk(engine)
    claim_id = sdk.create_claim(
        campaign_id="campaign",
        creator_x_id="123",
        draft="x" * 1_000,
    )
    await engine.commit_ready(force=True)
    engine.policy = BatchPolicy(max_age_seconds=5, max_events=100, max_batch_bytes=300)
    sdk.submit_tweet(
        campaign_id="campaign",
        tweet_id="999",
        claim_id=claim_id,
        creator_x_id="123",
    )

    with pytest.raises(ProtocolError, match="exceeds the maximum batch byte size"):
        await engine.commit_ready(force=True)


@pytest.mark.asyncio
async def test_sixth_finalized_claim_fifo_evicts_first(tmp_path: Path) -> None:
    sdk = build_sdk(tmp_path / "miner.db", FakeSubmitter())
    claim_ids: list[str] = []
    for index in range(6):
        claim_ids.append(
            sdk.create_claim(
                campaign_id="campaign",
                creator_x_id="123",
                draft=f"draft {index}",
            )
        )
        await sdk.engine.commit_ready(force=True)

    assert sdk.claim_status(claim_ids[0]) is EventStatus.EVICTED
    assert sdk.engine.store.active_claim_ids("campaign", "123") == claim_ids[1:]
