"""End-to-end offline tests for durable miner SDK batching and recovery."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

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
from bitcast_x.protocol import (
    ClaimEvent,
    CommitmentPosition,
    CommittedBatch,
    DraftReveal,
    OnChainEnvelope,
    ProtocolEvent,
    SubmissionEvent,
)
from bitcast_x.protocol.canonical import canonical_json
from bitcast_x.transport import BatchPageRequest

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"


class FakeSubmitter:
    def __init__(self) -> None:
        self.available = True
        self.latest_commitment: FinalizedCommitment | None = None
        self.submissions = 0

    async def capacity(self, envelope: OnChainEnvelope) -> CapacityBudget:
        assert len(envelope.encode()) in {35, 45, 77}
        return CapacityBudget(
            remaining_space=100 if self.available else 0,
            next_call_charge=100,
        )

    async def latest(self) -> FinalizedCommitment | None:
        return self.latest_commitment

    async def submit(self, envelope: OnChainEnvelope) -> FinalizedCommitment:
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


async def test_history_resume_abandons_old_pending_work_and_links_future_batch(
    tmp_path: Path,
) -> None:
    submitter = FakeSubmitter()
    sdk = build_sdk(tmp_path / "miner.db", submitter)
    abandoned_claim = sdk.create_claim(
        campaign_id="campaign", creator_x_id="123", draft="old draft"
    )
    old_batch = await sdk.engine.commit_ready(force=True)
    assert old_batch is not None and old_batch.sequence == 1

    anchor = sdk.engine.store.resume_history()
    repeated_anchor = sdk.engine.store.resume_history()
    new_claim = sdk.create_claim(campaign_id="campaign", creator_x_id="123", draft="new draft")
    new_batch = await sdk.engine.commit_ready(force=True)
    page = await sdk.engine.batch_page(
        BatchPageRequest(after_sequence=0, max_batches=10), caller_hotkey="validator"
    )

    assert sdk.claim_status(abandoned_claim) is EventStatus.REJECTED
    assert sdk.claim_status(new_claim) is EventStatus.SAFE_TO_POST
    assert repeated_anchor == anchor
    assert submitter.submissions == 2  # old batch and first batch in the new history
    assert new_batch is not None and new_batch.sequence == 1
    assert new_batch.previous_batch_hash is None
    assert new_batch.history_id == anchor
    assert [item.batch["sequence"] for item in page.batches] == [1]


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


def test_claim_too_long_once_normalized_is_refused_before_it_is_queued(tmp_path: Path) -> None:
    sdk = build_sdk(tmp_path / "miner.db", FakeSubmitter())

    # Each U+FDFA ligature normalizes to 18 characters, so 1,200 become 21,600: a stored
    # reveal that long could never be read back to build a batch.
    with pytest.raises(ValidationError, match="draft"):
        sdk.create_claim(campaign_id="campaign", creator_x_id="123", draft="\ufdfa" * 1_200)

    assert sdk.engine.store.queued(limit=10) == []


async def test_submissions_sharing_a_claim_commit_with_one_reveal(tmp_path: Path) -> None:
    """Two tweets may cite one claim before it is consumed; validators let at most one win it.

    The batch must reveal the claim once: a duplicate reveal made every commit
    attempt fail validation and stalled the miner's queue.
    """

    submitter = FakeSubmitter()
    sdk = build_sdk(tmp_path / "miner.db", submitter)
    claim_id = sdk.create_claim(campaign_id="campaign", creator_x_id="123", draft="A private draft")
    await sdk.engine.commit_ready(force=True)
    submission_ids = [
        sdk.submit_tweet(
            campaign_id="campaign", tweet_id=tweet_id, claim_id=claim_id, creator_x_id="123"
        )
        for tweet_id in ("998", "999")
    ]

    batch = await sdk.engine.commit_ready(force=True)
    page = await sdk.engine.batch_page(
        BatchPageRequest(after_sequence=1, max_batches=10),
        caller_hotkey="validator",
    )

    assert batch is not None
    assert [event.tweet_id for event in batch.events if isinstance(event, SubmissionEvent)] == [
        "998",
        "999",
    ]
    assert [reveal.claim_id for reveal in batch.reveals] == [claim_id]
    assert CommittedBatch.model_validate(page.batches[0].batch) == batch
    assert [sdk.submission_status(item) for item in submission_ids] == [
        EventStatus.VERIFICATION_PENDING
    ] * 2


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


class LostResponseSubmitter(FakeSubmitter):
    """Finalize every commitment on chain, then lose the response to the caller."""

    async def submit(self, envelope: OnChainEnvelope) -> FinalizedCommitment:
        await super().submit(envelope)
        raise ChainOperationError("connection lost after finalization")


async def test_restart_recovers_prepared_batch_without_duplicate_commit(tmp_path: Path) -> None:
    database = tmp_path / "miner.db"
    submitter = LostResponseSubmitter()
    first_sdk = build_sdk(database, submitter)
    claim_id = first_sdk.create_claim(
        campaign_id="campaign",
        creator_x_id="123",
        draft="A private draft",
    )
    with pytest.raises(ChainOperationError, match="connection lost"):
        await first_sdk.engine.commit_ready(force=True)
    assert first_sdk.claim_status(claim_id) is EventStatus.WAITING_FOR_COMMITMENT

    restarted_sdk = build_sdk(database, submitter)
    recovered = await restarted_sdk.engine.commit_ready(force=True)
    page = await restarted_sdk.engine.batch_page(
        BatchPageRequest(after_sequence=0, max_batches=10), caller_hotkey="validator"
    )

    assert recovered is not None
    assert submitter.submissions == 1
    assert restarted_sdk.claim_status(claim_id) is EventStatus.SAFE_TO_POST
    assert [(item.batch["batch_hash"], item.position.block) for item in page.batches] == [
        (recovered.batch_hash, 101)
    ]


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


def test_pending_queue_byte_bound_counts_payload_and_private_reveal(tmp_path: Path) -> None:
    store = MinerStore(tmp_path / "miner.db")
    first, first_reveal = _fixed_claim(1, draft_length=100)
    second, second_reveal = _fixed_claim(2, draft_length=100)
    first_bytes = len(first.model_dump_json().encode()) + len(
        first_reveal.model_dump_json().encode()
    )

    store.enqueue(
        first,
        max_pending_events=10,
        max_pending_bytes=2 * first_bytes - 1,
        reveal=first_reveal,
    )

    with pytest.raises(ProtocolError, match="queue capacity is exhausted"):
        store.enqueue(
            second,
            max_pending_events=10,
            max_pending_bytes=2 * first_bytes - 1,
            reveal=second_reveal,
        )


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
    with pytest.raises(ProtocolError, match="event id was reused with different content"):
        sdk.engine.enqueue(event.model_copy(update={"creator_x_id": "456"}), reveal=reveal)
    other_draft = DraftReveal(claim_id=reveal.claim_id, draft="Another draft", nonce=reveal.nonce)
    with pytest.raises(ProtocolError, match="event id was reused with different content"):
        sdk.engine.enqueue(event, reveal=other_draft)
    assert len(sdk.engine.store.queued(limit=100)) == 1


def test_submission_rejects_claim_owned_by_another_miner(tmp_path: Path) -> None:
    sdk = build_sdk(tmp_path / "miner.db", FakeSubmitter())

    with pytest.raises(ProtocolError, match="does not belong"):
        sdk.submit_tweet(
            campaign_id="campaign",
            tweet_id="999",
            claim_id="01" * 16,
            creator_x_id="123",
        )


def test_submission_identity_is_idempotent_across_restart_and_includes_the_creator(
    tmp_path: Path,
) -> None:
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

    other_creator_id = restarted.submit_tweet(
        campaign_id="campaign",
        tweet_id="999",
        claim_id=None,
        creator_x_id="456",
    )

    assert other_creator_id != submission_id
    assert {item["creator_x_id"] for item in restarted.submissions()} == {"123", "456"}


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


def _fixed_claim(index: int, draft_length: int) -> tuple[ClaimEvent, DraftReveal]:
    reveal = DraftReveal(
        claim_id=f"{index:032x}",
        draft="d" * draft_length,
        nonce=f"{index:064x}",
    )
    claim = ClaimEvent(
        claim_id=reveal.claim_id,
        campaign_id="campaign",
        creator_x_id="123",
        created_at="2026-08-05T12:00:00Z",
        draft_commitment=reveal.commitment(),
    )
    return claim, reveal


def _fixed_submission(index: int, claim_index: int | None) -> SubmissionEvent:
    return SubmissionEvent(
        submission_id=f"{0xFF00 + index:032x}",
        campaign_id="campaign",
        tweet_id=str(900 + index),
        claim_id=None if claim_index is None else f"{claim_index:032x}",
        miner_hotkey=MINER,
        creator_x_id="123",
    )


async def _engine_with_mixed_queue(path: Path) -> MinerEngine:
    """Queue claims and submissions whose reveals come from earlier and same batches."""

    engine = MinerEngine(
        miner_hotkey=MINER,
        store=MinerStore(path),
        submitter=FakeSubmitter(),
        policy=BatchPolicy(max_batch_bytes=100_000),
    )
    for index, draft_length in ((1, 10), (2, 200)):
        claim, reveal = _fixed_claim(index, draft_length)
        engine.enqueue(claim, reveal=reveal)
    await engine.commit_ready(force=True)
    claim, reveal = _fixed_claim(3, 50)
    engine.enqueue(_fixed_submission(1, claim_index=1))
    engine.enqueue(claim, reveal=reveal)
    engine.enqueue(_fixed_submission(2, claim_index=None))
    engine.enqueue(_fixed_submission(3, claim_index=3))
    engine.enqueue(_fixed_submission(4, claim_index=2))
    return engine


def _linear_selection(engine: MinerEngine, queued: list[ProtocolEvent]) -> list[ProtocolEvent]:
    """Reference: grow the batch one queued event at a time until it overflows."""

    draft = engine.store.batch_draft(tuple(queued))
    selected: list[ProtocolEvent] = []
    for event in queued:
        batch = draft.build(engine.miner_hotkey, (*selected, event))
        if len(canonical_json(batch)) > engine.policy.max_batch_bytes:
            if not selected:
                raise ProtocolError("one queued event exceeds the maximum batch byte size")
            break
        selected.append(event)
    return selected


async def test_batch_selection_matches_a_linear_scan_at_every_byte_boundary(
    tmp_path: Path,
) -> None:
    engine = await _engine_with_mixed_queue(tmp_path / "miner.db")
    queued = [event for event, _created in engine.store.queued(limit=100)]
    draft = engine.store.batch_draft(tuple(queued))
    sizes = [
        len(canonical_json(draft.build(MINER, tuple(queued[:count]))))
        for count in range(1, len(queued) + 1)
    ]

    for limit in sorted({size + delta for size in sizes for delta in (-1, 0, 1)}):
        engine.policy = BatchPolicy(max_batch_bytes=limit)
        if limit < sizes[0]:
            with pytest.raises(ProtocolError, match="one queued event exceeds"):
                _linear_selection(engine, queued)
            with pytest.raises(ProtocolError, match="one queued event exceeds"):
                engine._select_events(queued)
        else:
            assert engine._select_events(queued) == _linear_selection(engine, queued)


async def test_batch_selection_fails_only_on_an_unbuildable_event_it_reaches(
    tmp_path: Path,
) -> None:
    engine = await _engine_with_mixed_queue(tmp_path / "miner.db")
    engine.enqueue(_fixed_submission(5, claim_index=99))  # no local reveal for claim 99
    queued = [event for event, _created in engine.store.queued(limit=100)]
    five_events = len(
        canonical_json(engine.store.batch_draft(tuple(queued)).build(MINER, tuple(queued[:5])))
    )

    engine.policy = BatchPolicy(max_batch_bytes=five_events - 1)
    assert engine._select_events(queued) == queued[:4]
    engine.policy = BatchPolicy(max_batch_bytes=five_events)
    with pytest.raises(ProtocolError, match="claim without a local reveal"):
        engine._select_events(queued)


async def test_prepared_batches_keep_their_pinned_bytes(tmp_path: Path) -> None:
    engine = await _engine_with_mixed_queue(tmp_path / "miner.db")
    engine.policy = BatchPolicy(max_batch_bytes=1_161)

    batches = [await engine.commit_ready(force=True) for _ in range(3)]

    assert [
        (batch.sequence, len(batch.events), hashlib.sha256(canonical_json(batch)).hexdigest())
        for batch in batches
        if batch is not None
    ] == [
        (2, 3, "ae03889770b017a3bc8fd7c76d50e96b1165448f3674963ac4274e68edfa6f24"),
        (3, 1, "928c8f16c17514670e5f611ccb5d1f746352dcdc7643624f34a25187feec3884"),
        (4, 1, "89958f6f240aa75a7f4acd48483c7f5c60c218d5f1b15e99701c814d9e2c0e46"),
    ]
