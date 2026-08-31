"""Binary envelopes committed through Bittensor's Commitments pallet."""

import struct
from dataclasses import dataclass

from bitcast_x.errors import ProtocolError

MAGIC = b"DX2"
HISTORY_MAGIC = b"DX3"
RESUME_MAGIC = b"DXR"
BATCH_HASH_BYTES = 32
ENVELOPE_BYTES = len(MAGIC) + 8 + 2 + BATCH_HASH_BYTES
HISTORY_ENVELOPE_BYTES = len(HISTORY_MAGIC) + BATCH_HASH_BYTES + 8 + 2 + BATCH_HASH_BYTES
RESUME_ENVELOPE_BYTES = len(RESUME_MAGIC) + BATCH_HASH_BYTES
_ENVELOPE = struct.Struct(">3sQH32s")
_HISTORY_ENVELOPE = struct.Struct(">3s32sQH32s")
_RESUME_ENVELOPE = struct.Struct(">3s32s")


@dataclass(frozen=True, slots=True)
class CommitmentEnvelope:
    """The fixed 45-byte on-chain pointer to one complete off-chain batch."""

    sequence: int
    event_count: int
    batch_hash: bytes
    history_id: bytes | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.sequence <= (2**64 - 1):
            raise ProtocolError("sequence must fit an unsigned 64-bit integer")
        if not 1 <= self.event_count <= (2**16 - 1):
            raise ProtocolError("event_count must be between 1 and 65535")
        if len(self.batch_hash) != BATCH_HASH_BYTES:
            raise ProtocolError("batch_hash must contain exactly 32 bytes")
        if self.history_id is not None and len(self.history_id) != BATCH_HASH_BYTES:
            raise ProtocolError("history_id must contain exactly 32 bytes")

    def encode(self) -> bytes:
        """Return the consensus-stable on-chain byte representation."""

        if self.history_id is None:
            return _ENVELOPE.pack(MAGIC, self.sequence, self.event_count, self.batch_hash)
        return _HISTORY_ENVELOPE.pack(
            HISTORY_MAGIC,
            self.history_id,
            self.sequence,
            self.event_count,
            self.batch_hash,
        )

    @classmethod
    def decode(cls, value: bytes) -> "CommitmentEnvelope":
        """Validate and decode an on-chain commitment envelope."""

        if len(value) not in {ENVELOPE_BYTES, HISTORY_ENVELOPE_BYTES}:
            raise ProtocolError(
                f"commitment envelope must be {ENVELOPE_BYTES} or {HISTORY_ENVELOPE_BYTES} bytes"
            )
        if value.startswith(MAGIC) and len(value) == ENVELOPE_BYTES:
            _magic, sequence, event_count, batch_hash = _ENVELOPE.unpack(value)
            return cls(sequence=sequence, event_count=event_count, batch_hash=batch_hash)
        if value.startswith(HISTORY_MAGIC) and len(value) == HISTORY_ENVELOPE_BYTES:
            _magic, history_id, sequence, event_count, batch_hash = _HISTORY_ENVELOPE.unpack(value)
            return cls(
                sequence=sequence,
                event_count=event_count,
                batch_hash=batch_hash,
                history_id=history_id,
            )
        raise ProtocolError("unsupported commitment envelope magic")


@dataclass(frozen=True, slots=True)
class ResumeEnvelope:
    """A signed, future-only boundary that abandons an unusable local history."""

    history_id: bytes

    def __post_init__(self) -> None:
        if len(self.history_id) != BATCH_HASH_BYTES:
            raise ProtocolError("history_id must contain exactly 32 bytes")

    def encode(self) -> bytes:
        """Return the consensus-stable signed marker bytes."""

        return _RESUME_ENVELOPE.pack(RESUME_MAGIC, self.history_id)

    @classmethod
    def decode(cls, value: bytes) -> "ResumeEnvelope":
        if len(value) != RESUME_ENVELOPE_BYTES:
            raise ProtocolError(f"resume commitment envelope must be {RESUME_ENVELOPE_BYTES} bytes")
        magic, history_id = _RESUME_ENVELOPE.unpack(value)
        if magic != RESUME_MAGIC:
            raise ProtocolError("unsupported resume commitment envelope magic")
        return cls(history_id=history_id)


type OnChainEnvelope = CommitmentEnvelope | ResumeEnvelope


def decode_envelope(value: bytes) -> OnChainEnvelope:
    """Decode either an append-only batch pointer or a future-only resume marker."""

    if value.startswith((MAGIC, HISTORY_MAGIC)):
        return CommitmentEnvelope.decode(value)
    if value.startswith(RESUME_MAGIC):
        return ResumeEnvelope.decode(value)
    raise ProtocolError("unsupported commitment envelope magic")
