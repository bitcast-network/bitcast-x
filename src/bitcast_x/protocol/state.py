"""Deterministic batch-chain verification and five-claim FIFO state."""

from collections import defaultdict, deque
from dataclasses import dataclass

from bitcast_x.errors import ProtocolError
from bitcast_x.protocol.commitments import CommitmentEnvelope
from bitcast_x.protocol.models import ClaimEvent, CommitmentPosition, CommittedBatch

MAX_ACTIVE_CLAIMS = 5


class BatchChainVerifier:
    """Verify and advance one miner's append-only committed batch chain."""

    def __init__(self, miner_hotkey: str) -> None:
        self.miner_hotkey = miner_hotkey
        self.last_sequence = 0
        self.last_batch_hash: str | None = None

    def verify_and_advance(
        self,
        batch: CommittedBatch,
        envelope: CommitmentEnvelope,
    ) -> None:
        """Validate the next batch against chain bytes and advance atomically."""

        expected_sequence = self.last_sequence + 1
        if batch.miner_hotkey != self.miner_hotkey:
            raise ProtocolError("batch belongs to a different miner hotkey")
        if batch.sequence != expected_sequence or envelope.sequence != expected_sequence:
            raise ProtocolError(f"expected batch sequence {expected_sequence}")
        if batch.previous_batch_hash != self.last_batch_hash:
            raise ProtocolError("batch previous hash does not match verified history")
        if envelope.event_count != len(batch.events):
            raise ProtocolError("commitment event count does not match complete batch")
        if envelope.batch_hash.hex() != batch.batch_hash:
            raise ProtocolError("on-chain hash does not match complete batch")
        self.last_sequence = batch.sequence
        self.last_batch_hash = batch.batch_hash

    def resume_from(self, next_sequence: int, marker_hash: str) -> None:
        """Adopt a verified future-only boundary without altering accepted history."""

        if next_sequence <= self.last_sequence:
            raise ProtocolError("resume boundary must advance beyond verified history")
        if len(marker_hash) != 64:
            raise ProtocolError("resume marker hash must be a SHA-256 digest")
        self.last_sequence = next_sequence - 1
        self.last_batch_hash = marker_hash


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    """A claim and the chain coordinates that establish its ordering."""

    claim: ClaimEvent
    position: CommitmentPosition
    event_index: int

    @property
    def ordering_key(self) -> tuple[int, int, int]:
        """Return the protocol-defined total ordering key."""

        return (self.position.block, self.position.extrinsic_index, self.event_index)


class ClaimLedger:
    """Reconstruct active claims for one miner using the five-slot FIFO rule."""

    def __init__(self) -> None:
        self._active: dict[tuple[str, str], deque[ClaimRecord]] = defaultdict(deque)
        self._by_id: dict[str, ClaimRecord] = {}
        self._consumed: set[str] = set()
        self._evicted: set[str] = set()

    def add(self, record: ClaimRecord) -> str | None:
        """Add a unique claim and return the evicted claim id, if any."""

        claim_id = record.claim.claim_id
        if claim_id in self._by_id:
            existing = self._by_id[claim_id]
            if existing == record:
                return None
            raise ProtocolError("claim_id was reused with different content or ordering")
        key = (record.claim.campaign_id, record.claim.creator_x_id)
        claims = self._active[key]
        if claims and record.ordering_key <= claims[-1].ordering_key:
            raise ProtocolError("claims must be applied in canonical chain order")
        claims.append(record)
        self._by_id[claim_id] = record
        if len(claims) <= MAX_ACTIVE_CLAIMS:
            return None
        evicted = claims.popleft().claim.claim_id
        self._evicted.add(evicted)
        return evicted

    def consume(self, claim_id: str) -> None:
        """Consume one currently active winning claim."""

        record = self._by_id.get(claim_id)
        if record is None:
            raise ProtocolError("cannot consume an unknown claim")
        key = (record.claim.campaign_id, record.claim.creator_x_id)
        claims = self._active[key]
        for index, candidate in enumerate(claims):
            if candidate.claim.claim_id == claim_id:
                del claims[index]
                self._consumed.add(claim_id)
                return
        raise ProtocolError("cannot consume an inactive claim")

    def active(self, campaign_id: str, creator_x_id: str) -> tuple[ClaimRecord, ...]:
        """Return active claims in canonical FIFO order."""

        return tuple(self._active[(campaign_id, creator_x_id)])

    def status(self, claim_id: str) -> str:
        """Return a stable lifecycle label for a known or unknown claim id."""

        if claim_id in self._consumed:
            return "consumed"
        if claim_id in self._evicted:
            return "evicted"
        if claim_id in self._by_id:
            return "active"
        return "unknown"
