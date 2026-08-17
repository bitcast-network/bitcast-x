"""Tests for protocol event, reveal, batch, and attribution contracts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from bitcast_x.protocol import (
    AttributionReason,
    AttributionResult,
    BatchContent,
    ClaimEvent,
    CommittedBatch,
    DraftReveal,
    SubmissionEvent,
)

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
CLAIM_ID = "01" * 16
SUBMISSION_ID = "02" * 16
NONCE = "03" * 32


def make_reveal() -> DraftReveal:
    return DraftReveal(claim_id=CLAIM_ID, draft="Ａ real draft", nonce=NONCE)


def make_claim() -> ClaimEvent:
    reveal = make_reveal()
    return ClaimEvent(
        claim_id=CLAIM_ID,
        campaign_id="campaign",
        creator_x_id="123",
        created_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
        draft_commitment=reveal.commitment(),
    )


def test_create_batch_golden_hash_and_reveal() -> None:
    claim = make_claim()
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(claim,),
        reveals=(make_reveal(),),
    )

    assert batch.batch_hash == "773512aa802c34c715aa11ce0d78d58169635e4cd7bc499d6800db0431671370"
    assert (
        BatchContent.model_validate(batch.model_dump(exclude={"batch_hash", "reveals"})).hash()
        == batch.batch_hash
    )


def test_batch_rejects_wrong_hash() -> None:
    claim = make_claim()
    with pytest.raises(ValidationError, match="batch_hash"):
        CommittedBatch(
            miner_hotkey=MINER,
            sequence=1,
            previous_batch_hash=None,
            events=(claim,),
            batch_hash="00" * 32,
        )


def test_batch_rejects_broken_sequence_link() -> None:
    with pytest.raises(ValidationError, match="sequence 1"):
        BatchContent(
            miner_hotkey=MINER,
            sequence=2,
            previous_batch_hash=None,
            events=(make_claim(),),
        )


def test_batch_rejects_submission_for_other_miner() -> None:
    submission = SubmissionEvent(
        submission_id=SUBMISSION_ID,
        campaign_id="campaign",
        tweet_id="1234",
        claim_id=CLAIM_ID,
        miner_hotkey="5F" + "x" * 46,
    )
    with pytest.raises(ValidationError, match="must match"):
        BatchContent(
            miner_hotkey=MINER,
            sequence=1,
            previous_batch_hash=None,
            events=(submission,),
        )


def test_attribution_acceptance_must_match_reason() -> None:
    with pytest.raises(ValidationError, match="must agree"):
        AttributionResult(
            tweet_id="1",
            campaign_id="campaign",
            accepted=True,
            reason=AttributionReason.AMBIGUOUS_MATCH,
        )


def test_evidence_unavailable_can_remain_pending() -> None:
    result = AttributionResult(
        tweet_id="1",
        campaign_id="campaign",
        accepted=False,
        reason=AttributionReason.EVIDENCE_UNAVAILABLE,
        pending=True,
    )

    assert result.pending is True


def test_unsupported_reason_cannot_be_pending() -> None:
    with pytest.raises(ValidationError, match="supported non-final reason"):
        AttributionResult(
            tweet_id="1",
            campaign_id="campaign",
            accepted=False,
            reason=AttributionReason.AMBIGUOUS_MATCH,
            pending=True,
        )


def test_evidence_unavailable_cannot_be_rejected() -> None:
    with pytest.raises(ValidationError, match="must remain pending"):
        AttributionResult(
            tweet_id="1",
            campaign_id="campaign",
            accepted=False,
            reason=AttributionReason.EVIDENCE_UNAVAILABLE,
        )
