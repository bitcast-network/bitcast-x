"""Capacity-aware batching engine and platform-facing miner SDK."""

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from bitcast_x.errors import ChainOperationError, ProtocolError
from bitcast_x.miner.store import EventStatus, MinerStore, OperationMetadata
from bitcast_x.protocol import (
    ClaimEvent,
    CommitmentEnvelope,
    CommitmentPosition,
    CommittedBatch,
    DraftReveal,
    OnChainEnvelope,
    ProtocolEvent,
    SubmissionEvent,
)
from bitcast_x.protocol.canonical import canonical_json
from bitcast_x.transport import (
    BatchPageRequest,
    BatchPageResponse,
    PositionedBatch,
)


@dataclass(frozen=True, slots=True)
class BatchPolicy:
    """Protocol-safe batching bounds controlled by the SDK."""

    max_age_seconds: float = 5.0
    max_events: int = 100
    max_batch_bytes: int = 512_000
    max_page_bytes: int = 2_000_000
    max_pending_events: int = 10_000
    max_pending_bytes: int = 50_000_000

    def __post_init__(self) -> None:
        if (
            self.max_age_seconds <= 0
            or self.max_events <= 0
            or self.max_batch_bytes <= 0
            or self.max_page_bytes <= 0
            or self.max_pending_events <= 0
            or self.max_pending_bytes <= 0
        ):
            raise ValueError("batch policy limits must be positive")


@dataclass(frozen=True, slots=True)
class CapacityBudget:
    """Live commitment allowance available to one miner in the current epoch."""

    remaining_space: int
    next_call_charge: int

    @property
    def can_commit(self) -> bool:
        """Whether one more commitment fits in the current epoch."""

        return self.remaining_space >= self.next_call_charge


@dataclass(frozen=True, slots=True)
class FinalizedCommitment:
    """Verified final chain position returned by a commitment submitter."""

    position: CommitmentPosition
    stored_envelope: bytes


class CommitmentSubmitter(Protocol):
    """Chain operations required by the miner batching engine."""

    async def capacity(self, envelope: OnChainEnvelope) -> CapacityBudget: ...

    async def latest(self) -> FinalizedCommitment | None: ...

    async def submit(self, envelope: OnChainEnvelope) -> FinalizedCommitment: ...


class MinerEngine:
    """Prepare, commit, finalize, and serve batches without concurrent writes."""

    def __init__(
        self,
        *,
        miner_hotkey: str,
        store: MinerStore,
        submitter: CommitmentSubmitter,
        policy: BatchPolicy | None = None,
    ) -> None:
        self.miner_hotkey = miner_hotkey
        self.store = store
        self.submitter = submitter
        self.policy = policy or BatchPolicy()
        self._commit_lock = asyncio.Lock()

    def enqueue(
        self,
        event: ProtocolEvent,
        *,
        reveal: DraftReveal | None = None,
        metadata: OperationMetadata | None = None,
    ) -> str:
        """Durably queue a protocol event."""

        return self.store.enqueue(
            event,
            reveal=reveal,
            metadata=metadata,
            max_pending_events=self.policy.max_pending_events,
            max_pending_bytes=self.policy.max_pending_bytes,
        )

    async def resume_history(self) -> str:
        """Atomically abandon pending work and select a fresh local history."""

        async with self._commit_lock:
            existing = self.store.current_history_id()
            if existing is not None and not self.store.history_has_batches(existing):
                return existing
            return self.store.start_history(secrets.token_hex(32))

    async def commit_ready(self, *, force: bool = False) -> CommittedBatch | None:
        """Finalize one due batch, recovering a prepared batch after restart."""

        async with self._commit_lock:
            batch = self.store.pending_batch()
            if batch is None:
                queued = self.store.queued(limit=self.policy.max_events)
                if not queued:
                    return None
                oldest_ns = queued[0][1]
                due = (time.time_ns() - oldest_ns) / 1e9 >= self.policy.max_age_seconds
                if not force and len(queued) < self.policy.max_events and not due:
                    return None
                selected = self._select_events([event for event, _created in queued])
                batch = self.store.prepare_batch(self.miner_hotkey, tuple(selected))
            envelope = CommitmentEnvelope(
                sequence=batch.sequence,
                event_count=len(batch.events),
                batch_hash=bytes.fromhex(batch.batch_hash),
                history_id=(
                    bytes.fromhex(batch.history_id) if batch.history_id is not None else None
                ),
            )
            recovered = await self.submitter.latest()
            if recovered is not None and recovered.stored_envelope == envelope.encode():
                finalized = recovered
            else:
                budget = await self.submitter.capacity(envelope)
                if not budget.can_commit:
                    raise ChainOperationError("commitment epoch capacity is exhausted")
                finalized = await self.submitter.submit(envelope)
            if finalized.stored_envelope != envelope.encode():
                raise ChainOperationError("finalized chain bytes differ from the prepared batch")
            self.store.mark_finalized(batch, finalized.position)
            return batch

    def _select_events(self, queued: list[ProtocolEvent]) -> list[ProtocolEvent]:
        selected: list[ProtocolEvent] = []
        for event in queued:
            candidate = [*selected, event]
            preview = self.store.preview_batch(self.miner_hotkey, tuple(candidate))
            if len(canonical_json(preview)) > self.policy.max_batch_bytes:
                if not selected:
                    raise ProtocolError("one queued event exceeds the maximum batch byte size")
                break
            selected.append(event)
        return selected

    async def batch_page(self, request: BatchPageRequest, caller_hotkey: str) -> BatchPageResponse:
        """Serve a bounded page of finalized complete batches."""

        del caller_hotkey  # authorization happens before this callback
        batches, has_more = self.store.finalized_batches(
            after_sequence=request.after_sequence,
            through_sequence=request.through_sequence,
            limit=request.max_batches,
        )
        selected: list[PositionedBatch] = []
        for batch, position in batches:
            candidate = [
                *selected,
                PositionedBatch(batch=batch.model_dump(mode="json"), position=position),
            ]
            response = BatchPageResponse(
                miner_hotkey=self.miner_hotkey,
                batches=candidate,
                next_sequence=batch.sequence,
                has_more=has_more or len(candidate) < len(batches),
            )
            if len(response.model_dump_json().encode()) > self.policy.max_page_bytes:
                if not selected:
                    raise ProtocolError("one finalized batch exceeds the maximum page byte size")
                break
            selected = candidate
        next_sequence = (
            batches[len(selected) - 1][0].sequence if selected else request.after_sequence
        )
        return BatchPageResponse(
            miner_hotkey=self.miner_hotkey,
            batches=selected,
            next_sequence=next_sequence,
            has_more=has_more or len(selected) < len(batches),
        )


class QualificationProvider(Protocol):
    """Explanatory miner qualification read used by the platform SDK."""

    async def __call__(self) -> dict[str, object]: ...


class MinerSdk:
    """Small platform-facing API that hides commitment protocol machinery."""

    def __init__(
        self,
        engine: MinerEngine,
        *,
        qualification_provider: QualificationProvider | None = None,
    ) -> None:
        self.engine = engine
        self._qualification_provider = qualification_provider

    def create_claim(
        self,
        *,
        campaign_id: str,
        creator_x_id: str,
        draft: str,
        metadata: OperationMetadata | None = None,
    ) -> str:
        """Queue a private draft claim and return its random claim id."""

        claim_id = secrets.token_hex(16)
        reveal = DraftReveal(
            claim_id=claim_id,
            draft=draft,
            nonce=secrets.token_hex(32),
        )
        claim = ClaimEvent(
            claim_id=claim_id,
            campaign_id=campaign_id,
            creator_x_id=creator_x_id,
            created_at=datetime.now(UTC),
            draft_commitment=reveal.commitment(),
        )
        return self.engine.enqueue(claim, reveal=reveal, metadata=metadata)

    def claim_status(self, claim_id: str) -> EventStatus | None:
        """Return the current creator-facing claim status."""

        return self.engine.store.status(claim_id)

    def submit_tweet(
        self,
        *,
        campaign_id: str,
        tweet_id: str,
        claim_id: str | None,
        creator_x_id: str,
        metadata: OperationMetadata | None = None,
    ) -> str:
        """Queue a completed tweet mapping and return its submission id."""

        if claim_id is not None and not self.engine.store.has_claim(claim_id):
            raise ProtocolError("submission claim_id does not belong to this miner")

        existing = self.engine.store.submission_id(
            campaign_id=campaign_id,
            tweet_id=tweet_id,
            claim_id=claim_id,
            creator_x_id=creator_x_id,
        )
        if existing is not None:
            return existing
        identity = "\0".join(
            (
                self.engine.miner_hotkey,
                campaign_id,
                tweet_id,
                claim_id or "",
                creator_x_id,
            )
        ).encode()
        submission_id = hashlib.sha256(identity).hexdigest()[:32]
        submission = SubmissionEvent(
            submission_id=submission_id,
            campaign_id=campaign_id,
            tweet_id=tweet_id,
            claim_id=claim_id,
            miner_hotkey=self.engine.miner_hotkey,
            creator_x_id=creator_x_id,
        )
        return self.engine.enqueue(submission, metadata=metadata)

    def submission_status(self, submission_id: str) -> EventStatus | None:
        """Return the current platform-facing submission status."""

        return self.engine.store.status(submission_id)

    def submissions(self) -> list[dict[str, object]]:
        """Return all durable submissions for a platform status view."""

        return self.engine.store.submissions()

    def record_submission_result(self, submission_id: str, status: EventStatus) -> None:
        """Persist a final result obtained from the authenticated results API."""

        self.engine.store.record_submission_result(submission_id, status)

    async def qualification_status(self) -> dict[str, object]:
        """Return an explanatory chain qualification snapshot when configured."""

        if self._qualification_provider is None:
            return {"eligible": False, "reason": "qualification_provider_not_configured"}
        return await self._qualification_provider()
