"""Typed wire and consensus models for pre-claim protocol version 2."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

from bitcast_x.protocol.canonical import hash_hex, normalize_text

ProtocolId = Literal[2]
BatchProtocolId = Literal[2, 3]
SubmissionProtocolId = Literal[2, 3]
# Direct submissions committed before this block use the legacy creator-resolution
# rule. At and after it, validators require the version-3 creator binding.
CREATOR_BINDING_ACTIVATION_BLOCK: Final = 8_920_000
Hex128 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Hash256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NumericId = Annotated[str, StringConstraints(pattern=r"^[0-9]+$")]
Hotkey = Annotated[str, StringConstraints(min_length=40, max_length=64)]


class ProtocolModel(BaseModel):
    """Strict immutable base for data crossing a consensus boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MiningProtocol(StrEnum):
    """Application behavior selected by a campaign feed record."""

    LEGACY_CONNECTION = "legacy_connection"
    PRECLAIM_V2 = "preclaim_v2"


class CampaignAccess(ProtocolModel):
    """Campaign routing and miner-access fields consumed by the protocol."""

    campaign_id: str = Field(min_length=1, max_length=128)
    mechanism_id: int = Field(ge=0)
    mining_protocol: MiningProtocol
    scoring_close_block: int = Field(ge=0)
    exclusive_miner_hotkey: Hotkey | None = None

    @field_validator("campaign_id")
    @classmethod
    def normalize_campaign_id(cls, value: str) -> str:
        """Normalize and reject empty campaign identifiers."""

        normalized = normalize_text(value)
        if not normalized.strip():
            raise ValueError("campaign_id cannot be blank")
        return normalized


class ClaimEvent(ProtocolModel):
    """Public metadata committed before an open-campaign tweet exists."""

    version: ProtocolId = 2
    kind: Literal["claim"] = "claim"
    claim_id: Hex128
    campaign_id: str = Field(min_length=1, max_length=128)
    creator_x_id: NumericId
    created_at: datetime
    draft_commitment: Hash256

    @field_validator("campaign_id")
    @classmethod
    def normalize_campaign_id(cls, value: str) -> str:
        return normalize_text(value)

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        """Require an aware timestamp and normalize it to UTC."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)


class SubmissionEvent(ProtocolModel):
    """A miner's committed mapping from an X tweet to an optional claim."""

    version: SubmissionProtocolId = 3
    kind: Literal["submission"] = "submission"
    submission_id: Hex128
    campaign_id: str = Field(min_length=1, max_length=128)
    tweet_id: NumericId
    claim_id: Hex128 | None
    miner_hotkey: Hotkey
    # Optional only for replaying version-2 submissions committed before creator
    # binding was introduced. Version-3 submissions always populate this field.
    creator_x_id: NumericId | None = None

    @field_validator("campaign_id")
    @classmethod
    def normalize_campaign_id(cls, value: str) -> str:
        return normalize_text(value)

    @model_validator(mode="after")
    def require_versioned_creator_binding(self) -> "SubmissionEvent":
        """Keep the historical and creator-bound event schemas unambiguous."""

        if self.version == 2 and self.creator_x_id is not None:
            raise ValueError("version 2 submissions cannot include creator_x_id")
        if self.version == 3 and self.creator_x_id is None:
            raise ValueError("version 3 submissions require creator_x_id")
        return self

    @model_serializer(mode="wrap")
    def omit_legacy_creator_binding(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, Any]:
        """Preserve hashes for historical submissions that predate this field."""

        data: dict[str, Any] = handler(self)
        if self.creator_x_id is None:
            data.pop("creator_x_id", None)
        return data


ProtocolEvent = Annotated[ClaimEvent | SubmissionEvent, Field(discriminator="kind")]


class DraftReveal(ProtocolModel):
    """Private draft material revealed only with a completed submission."""

    claim_id: Hex128
    draft: str = Field(min_length=1, max_length=20_000)
    nonce: Hash256

    @field_validator("draft")
    @classmethod
    def normalize_draft(cls, value: str) -> str:
        return normalize_text(value)

    def commitment(self) -> str:
        """Recompute the claim's public draft commitment."""

        return hash_hex(
            "dx2/draft",
            {"claim_id": self.claim_id, "draft_nfkc": self.draft, "nonce": self.nonce},
        )


class BatchContent(ProtocolModel):
    """The exact fields covered by a batch hash."""

    version: BatchProtocolId = 2
    miner_hotkey: Hotkey
    history_id: Hash256 | None = None
    sequence: int = Field(ge=1)
    previous_batch_hash: Hash256 | None
    events: tuple[ProtocolEvent, ...] = Field(min_length=1, max_length=65_535)

    @model_validator(mode="after")
    def require_genesis_link_rule(self) -> "BatchContent":
        """Require only sequence one to have an empty previous-hash link."""

        if (self.sequence == 1) != (self.previous_batch_hash is None):
            raise ValueError("only sequence 1 may omit previous_batch_hash")
        if (self.version == 2) != (self.history_id is None):
            raise ValueError("only version 3 batches may include history_id")
        for event in self.events:
            if isinstance(event, SubmissionEvent) and event.miner_hotkey != self.miner_hotkey:
                raise ValueError("submission miner_hotkey must match its batch")
        return self

    def hash(self) -> str:
        """Return the protocol-defined complete batch hash."""

        return hash_hex("dx2/batch" if self.version == 2 else "dx3/batch", self)

    @model_serializer(mode="wrap")
    def omit_legacy_history_id(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, Any]:
        """Preserve hashes for version-2 batches that predate history epochs."""

        data: dict[str, Any] = handler(self)
        if self.history_id is None:
            data.pop("history_id", None)
        return data


class CommittedBatch(BatchContent):
    """A complete off-chain batch carrying its recomputable digest and reveals."""

    batch_hash: Hash256
    reveals: tuple[DraftReveal, ...] = ()

    @model_validator(mode="after")
    def verify_batch_and_reveals(self) -> "CommittedBatch":
        """Reject altered content and unrelated or conflicting draft reveals."""

        content = BatchContent.model_validate(
            self.model_dump(exclude={"batch_hash", "reveals"}, mode="python")
        )
        if self.batch_hash != content.hash():
            raise ValueError("batch_hash does not match canonical batch content")
        claims = {event.claim_id: event for event in self.events if isinstance(event, ClaimEvent)}
        seen: set[str] = set()
        for reveal in self.reveals:
            if reveal.claim_id in seen:
                raise ValueError("a claim may be revealed at most once per batch")
            seen.add(reveal.claim_id)
            claim = claims.get(reveal.claim_id)
            if claim is not None and claim.draft_commitment != reveal.commitment():
                raise ValueError("draft reveal does not match its claim commitment")
        return self

    @classmethod
    def create(
        cls,
        *,
        miner_hotkey: str,
        sequence: int,
        previous_batch_hash: str | None,
        events: tuple[ProtocolEvent, ...],
        reveals: tuple[DraftReveal, ...] = (),
        history_id: str | None = None,
    ) -> "CommittedBatch":
        """Create a committed batch without allowing a caller-supplied digest."""

        content = BatchContent(
            version=3 if history_id is not None else 2,
            miner_hotkey=miner_hotkey,
            history_id=history_id,
            sequence=sequence,
            previous_batch_hash=previous_batch_hash,
            events=events,
        )
        return cls(**content.model_dump(mode="python"), batch_hash=content.hash(), reveals=reveals)


class CommitmentPosition(ProtocolModel):
    """Finalized ordering coordinates assigned by the chain."""

    block: int = Field(ge=0)
    extrinsic_index: int = Field(ge=0)


class AttributionReason(StrEnum):
    """Stable machine-readable attribution outcomes."""

    ACCEPTED = "accepted"
    MINER_NOT_QUALIFIED = "miner_not_qualified"
    INVALID_COMMITMENT = "invalid_commitment"
    MANIFEST_GAP = "manifest_gap"
    LATE_SUBMISSION = "late_submission"
    TWEET_NOT_FOUND = "tweet_not_found"
    AUTHOR_MISMATCH = "author_mismatch"
    CAMPAIGN_INELIGIBLE = "campaign_ineligible"
    POST_OUTSIDE_CAMPAIGN_WINDOW = "post_outside_campaign_window"
    CREATOR_NOT_ELIGIBLE_FOR_CAMPAIGN = "creator_not_eligible_for_campaign"
    REQUIRED_TERMS_MISSING = "required_terms_missing"
    RETWEET_NOT_ALLOWED = "retweet_not_allowed"
    REPLY_NOT_ALLOWED = "reply_not_allowed"
    CAMPAIGN_TAG_MISSING = "campaign_tag_missing"
    REQUIRED_QUOTE_MISSING_OR_INCORRECT = "required_quote_missing_or_incorrect"
    REQUIRED_CAMPAIGN_KEYWORD_MISSING = "required_campaign_keyword_missing"
    CLAIM_NOT_ACTIVE = "claim_not_active"
    CLAIM_AFTER_PUBLICATION = "claim_after_publication"
    DRAFT_REVEAL_MISMATCH = "draft_reveal_mismatch"
    SCORE_BELOW_FLOOR = "score_below_floor"
    AMBIGUOUS_MATCH = "ambiguous_match"
    DUPLICATE_TWEET = "duplicate_tweet"
    WRONG_EXCLUSIVE_MINER = "wrong_exclusive_miner"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"


class AttributionResult(ProtocolModel):
    """Replayable deterministic outcome for one submitted tweet."""

    tweet_id: NumericId
    campaign_id: str
    accepted: bool
    reason: AttributionReason
    pending: bool = False
    miner_hotkey: Hotkey | None = None
    submission_id: Hex128 | None = None
    claim_id: Hex128 | None = None
    winner_score: float | None = Field(default=None, ge=0, le=1)
    runner_up_score: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def keep_acceptance_consistent(self) -> "AttributionResult":
        if self.accepted != (self.reason is AttributionReason.ACCEPTED):
            raise ValueError("accepted must agree with the attribution reason")
        pending_reasons = {
            AttributionReason.EVIDENCE_UNAVAILABLE,
            AttributionReason.MINER_NOT_QUALIFIED,
        }
        if self.pending and (self.accepted or self.reason not in pending_reasons):
            raise ValueError("pending results require a supported non-final reason")
        if self.reason is AttributionReason.EVIDENCE_UNAVAILABLE and not self.pending:
            raise ValueError("unavailable evidence must remain pending")
        return self
