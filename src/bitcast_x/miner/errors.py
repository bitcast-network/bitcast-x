"""Typed refusals carrying the stable miner application API error codes."""

from enum import StrEnum

from bitcast_x.errors import ProtocolError


class ErrorCode(StrEnum):
    """Machine-readable reasons the application API reports for a refused operation."""

    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    MINER_NOT_QUALIFIED = "miner_not_qualified"
    CAMPAIGN_NOT_FOUND = "campaign_not_found"
    CLAIM_NOT_FOUND = "claim_not_found"
    SUBMISSION_NOT_FOUND = "submission_not_found"
    SUBMISSION_DEADLINE_PASSED = "submission_deadline_passed"
    SUBMISSION_COMMITMENT_PENDING = "submission_commitment_pending"
    CREATOR_NOT_ELIGIBLE = "creator_not_eligible"
    CLAIM_NOT_SAFE_TO_POST = "claim_not_safe_to_post"
    ECOSYSTEM_NOT_ENABLED = "ecosystem_not_enabled"
    QUEUE_CAPACITY_EXHAUSTED = "queue_capacity_exhausted"


class OperationError(ProtocolError):
    """A protocol refusal with a stable code for application API clients."""

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
