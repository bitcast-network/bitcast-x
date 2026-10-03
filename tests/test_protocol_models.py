"""Tests for protocol event, reveal, batch, and attribution contracts."""

from datetime import UTC, datetime
from typing import Any

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


def test_new_history_batch_is_domain_separated_and_self_identifying() -> None:
    legacy = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(make_claim(),),
    )
    resumed = CommittedBatch.create(
        miner_hotkey=MINER,
        history_id="04" * 32,
        sequence=1,
        previous_batch_hash=None,
        events=(make_claim(),),
    )

    assert resumed.version == 3
    assert resumed.history_id == "04" * 32
    assert resumed.batch_hash == "1b89e82dc7952dd16a12b5ce29cf5e4eed91fe9b346237865b0bc62b8aca67f3"
    assert resumed.batch_hash != legacy.batch_hash
    assert "history_id" not in legacy.model_dump()


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
        creator_x_id="456",
    )
    with pytest.raises(ValidationError, match="must match"):
        BatchContent(
            miner_hotkey=MINER,
            sequence=1,
            previous_batch_hash=None,
            events=(submission,),
        )


def test_historical_submission_without_creator_keeps_its_original_hash_shape() -> None:
    submission = SubmissionEvent(
        version=2,
        submission_id=SUBMISSION_ID,
        campaign_id="campaign",
        tweet_id="1234",
        claim_id=None,
        miner_hotkey=MINER,
    )
    batch = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(submission,),
    )

    assert "creator_x_id" not in submission.model_dump()
    assert CommittedBatch.model_validate_json(batch.model_dump_json()) == batch


def test_submission_versions_enforce_their_creator_binding_shape() -> None:
    common = {
        "submission_id": SUBMISSION_ID,
        "campaign_id": "campaign",
        "tweet_id": "1234",
        "claim_id": None,
        "miner_hotkey": MINER,
    }

    submission = SubmissionEvent(creator_x_id="456", **common)
    assert submission.version == 3
    assert submission.model_dump()["creator_x_id"] == "456"

    with pytest.raises(ValidationError, match="version 2 submissions cannot include"):
        SubmissionEvent(version=2, creator_x_id="456", **common)
    with pytest.raises(ValidationError, match="version 3 submissions require"):
        SubmissionEvent(version=3, **common)


@pytest.mark.parametrize(
    ("accepted", "reason", "pending", "error"),
    [
        pytest.param(True, AttributionReason.ACCEPTED, False, None, id="accepted"),
        pytest.param(
            False, AttributionReason.EVIDENCE_UNAVAILABLE, True, None, id="unavailable-pending"
        ),
        pytest.param(
            False, AttributionReason.MINER_NOT_QUALIFIED, True, None, id="unqualified-pending"
        ),
        pytest.param(
            True, AttributionReason.AMBIGUOUS_MATCH, False, "must agree", id="accepted-rejection"
        ),
        pytest.param(
            False, AttributionReason.ACCEPTED, False, "must agree", id="rejected-acceptance"
        ),
        pytest.param(
            False,
            AttributionReason.AMBIGUOUS_MATCH,
            True,
            "supported non-final reason",
            id="final-reason-pending",
        ),
        pytest.param(
            True,
            AttributionReason.ACCEPTED,
            True,
            "supported non-final reason",
            id="accepted-pending",
        ),
        pytest.param(
            False,
            AttributionReason.EVIDENCE_UNAVAILABLE,
            False,
            "must remain pending",
            id="unavailable-final",
        ),
    ],
)
def test_attribution_result_keeps_acceptance_reason_and_pending_consistent(
    accepted: bool,
    reason: AttributionReason,
    pending: bool,
    error: str | None,
) -> None:
    fields: dict[str, Any] = {
        "tweet_id": "1",
        "campaign_id": "campaign",
        "accepted": accepted,
        "reason": reason,
        "pending": pending,
    }

    if error is None:
        result = AttributionResult(**fields)
        assert (result.accepted, result.reason, result.pending) == (accepted, reason, pending)
    else:
        with pytest.raises(ValidationError, match=error):
            AttributionResult(**fields)
