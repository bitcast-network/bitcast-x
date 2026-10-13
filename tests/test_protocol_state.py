"""Tests for deterministic batch-chain and claim FIFO reconstruction."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

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


HISTORY = "04" * 32


def envelope_for(batch: CommittedBatch) -> CommitmentEnvelope:
    return CommitmentEnvelope(
        sequence=batch.sequence,
        event_count=len(batch.events),
        batch_hash=bytes.fromhex(batch.batch_hash),
        history_id=bytes.fromhex(batch.history_id) if batch.history_id is not None else None,
    )


@pytest.mark.parametrize(
    ("batch_changes", "envelope_changes", "message"),
    [
        pytest.param(
            {"miner_hotkey": "5F" + "x" * 46},
            {},
            "batch belongs to a different miner hotkey",
            id="wrong-miner-hotkey",
        ),
        pytest.param(
            {"history_id": "05" * 32},
            {},
            "batch belongs to a different miner history",
            id="wrong-batch-history",
        ),
        pytest.param(
            {"history_id": None},
            {},
            "batch belongs to a different miner history",
            id="legacy-batch-in-history",
        ),
        pytest.param(
            {},
            {"history_id": bytes.fromhex("05" * 32)},
            "on-chain envelope belongs to a different miner history",
            id="wrong-envelope-history",
        ),
        pytest.param(
            {"sequence": 3},
            {"sequence": 2},
            "expected batch sequence 2",
            id="batch-sequence-gap",
        ),
        pytest.param(
            {},
            {"sequence": 3},
            "expected batch sequence 2",
            id="envelope-sequence-gap",
        ),
        pytest.param(
            {"previous_batch_hash": "ff" * 32},
            {},
            "batch previous hash does not match verified history",
            id="previous-hash-mismatch",
        ),
        pytest.param(
            {},
            {"event_count": 2},
            "commitment event count does not match complete batch",
            id="event-count-mismatch",
        ),
        pytest.param(
            {},
            {"batch_hash": bytes(32)},
            "on-chain hash does not match complete batch",
            id="batch-hash-mismatch",
        ),
    ],
)
def test_batch_chain_rejects_any_mismatched_next_commitment_without_advancing(
    batch_changes: dict[str, Any],
    envelope_changes: dict[str, Any],
    message: str,
) -> None:
    first = CommittedBatch.create(
        miner_hotkey=MINER,
        history_id=HISTORY,
        sequence=1,
        previous_batch_hash=None,
        events=(claim(1),),
    )
    verifier = BatchChainVerifier(MINER)
    verifier.start_history(HISTORY)
    verifier.verify_and_advance(first, envelope_for(first))
    batch = CommittedBatch.create(
        **{
            "miner_hotkey": MINER,
            "history_id": HISTORY,
            "sequence": 2,
            "previous_batch_hash": first.batch_hash,
            "events": (claim(2),),
            **batch_changes,
        }
    )

    with pytest.raises(ProtocolError, match=f"^{message}$"):
        verifier.verify_and_advance(batch, replace(envelope_for(batch), **envelope_changes))

    assert (verifier.history_id, verifier.last_sequence, verifier.last_batch_hash) == (
        HISTORY,
        1,
        first.batch_hash,
    )
