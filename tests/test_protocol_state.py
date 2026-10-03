"""Tests for deterministic batch-chain and claim FIFO reconstruction."""

from datetime import UTC, datetime, timedelta

import pytest

from bitcast_x.errors import ProtocolError
from bitcast_x.protocol import (
    BatchChainVerifier,
    ClaimEvent,
    CommitmentEnvelope,
    CommittedBatch,
)

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"


def claim(number: int) -> ClaimEvent:
    return ClaimEvent(
        claim_id=f"{number:032x}",
        campaign_id="campaign",
        creator_x_id="123",
        created_at=datetime(2026, 8, 5, tzinfo=UTC) + timedelta(seconds=number),
        draft_commitment=f"{number:064x}",
    )


def test_batch_chain_advances_only_for_exact_next_commitment() -> None:
    first = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(claim(1),),
    )
    second = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=2,
        previous_batch_hash=first.batch_hash,
        events=(claim(2),),
    )
    verifier = BatchChainVerifier(MINER)

    verifier.verify_and_advance(
        first,
        CommitmentEnvelope(
            sequence=1,
            event_count=1,
            batch_hash=bytes.fromhex(first.batch_hash),
        ),
    )
    verifier.verify_and_advance(
        second,
        CommitmentEnvelope(
            sequence=2,
            event_count=1,
            batch_hash=bytes.fromhex(second.batch_hash),
        ),
    )

    assert verifier.last_sequence == 2
    assert verifier.last_batch_hash == second.batch_hash


def test_batch_chain_does_not_advance_on_gap() -> None:
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=2,
        previous_batch_hash="00" * 32,
        events=(claim(2),),
    )
    verifier = BatchChainVerifier(MINER)

    with pytest.raises(ProtocolError, match="expected batch sequence 1"):
        verifier.verify_and_advance(
            batch,
            CommitmentEnvelope(
                sequence=2,
                event_count=1,
                batch_hash=bytes.fromhex(batch.batch_hash),
            ),
        )

    assert verifier.last_sequence == 0
    assert verifier.last_batch_hash is None
