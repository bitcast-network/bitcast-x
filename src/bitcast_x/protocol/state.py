"""Deterministic batch-chain verification and the active-claim limit."""

from bitcast_x.errors import ProtocolError
from bitcast_x.protocol.commitments import CommitmentEnvelope
from bitcast_x.protocol.models import CommittedBatch

# Unconsumed claims active per (miner hotkey, campaign, creator X ID).
MAX_ACTIVE_CLAIMS = 5


class BatchChainVerifier:
    """Verify and advance one miner's append-only committed batch chain."""

    def __init__(self, miner_hotkey: str) -> None:
        self.miner_hotkey = miner_hotkey
        self.history_id: str | None = None
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
        if batch.history_id != self.history_id:
            raise ProtocolError("batch belongs to a different miner history")
        envelope_history_id = envelope.history_id.hex() if envelope.history_id is not None else None
        if envelope_history_id != self.history_id:
            raise ProtocolError("on-chain envelope belongs to a different miner history")
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

    def start_history(self, history_id: str) -> None:
        """Start verification of a new history-scoped batch chain."""

        if len(history_id) != 64:
            raise ProtocolError("history_id must be a 32-byte hexadecimal value")
        if history_id == self.history_id:
            raise ProtocolError("history boundary must select a new miner history")
        self.history_id = history_id
        self.last_sequence = 0
        self.last_batch_hash = None
