"""Binary envelopes committed through Bittensor's Commitments pallet."""

import hashlib
import struct
from dataclasses import dataclass

from bitcast_x.errors import ProtocolError

MAGIC = b"DX2"
RESUME_MAGIC = b"DXR"
BATCH_HASH_BYTES = 32
ENVELOPE_BYTES = len(MAGIC) + 8 + 2 + BATCH_HASH_BYTES
RESUME_ENVELOPE_BYTES = len(RESUME_MAGIC) + 8 + BATCH_HASH_BYTES
_ENVELOPE = struct.Struct(">3sQH32s")
_RESUME_ENVELOPE = struct.Struct(">3sQ32s")


@dataclass(frozen=True, slots=True)
class CommitmentEnvelope:
    """The fixed 45-byte on-chain pointer to one complete off-chain batch."""

    sequence: int
    event_count: int
    batch_hash: bytes

    def __post_init__(self) -> None:
        if not 0 <= self.sequence <= (2**64 - 1):
            raise ProtocolError("sequence must fit an unsigned 64-bit integer")
        if not 1 <= self.event_count <= (2**16 - 1):
            raise ProtocolError("event_count must be between 1 and 65535")
        if len(self.batch_hash) != BATCH_HASH_BYTES:
            raise ProtocolError("batch_hash must contain exactly 32 bytes")

    def encode(self) -> bytes:
        """Return the consensus-stable on-chain byte representation."""

        return _ENVELOPE.pack(MAGIC, self.sequence, self.event_count, self.batch_hash)

    @classmethod
    def decode(cls, value: bytes) -> "CommitmentEnvelope":
        """Validate and decode an on-chain commitment envelope."""

        if len(value) != ENVELOPE_BYTES:
            raise ProtocolError(f"commitment envelope must be {ENVELOPE_BYTES} bytes")
        magic, sequence, event_count, batch_hash = _ENVELOPE.unpack(value)
        if magic != MAGIC:
            raise ProtocolError("unsupported commitment envelope magic")
        return cls(sequence=sequence, event_count=event_count, batch_hash=batch_hash)


@dataclass(frozen=True, slots=True)
class ResumeEnvelope:
    """A signed, future-only boundary that abandons an unusable local history."""

    next_sequence: int
    nonce: bytes

    def __post_init__(self) -> None:
        if not 2 <= self.next_sequence <= (2**64 - 1):
            raise ProtocolError("resume next_sequence must be between 2 and 2^64-1")
        if len(self.nonce) != BATCH_HASH_BYTES:
            raise ProtocolError("resume nonce must contain exactly 32 bytes")

    def encode(self) -> bytes:
        """Return the consensus-stable signed marker bytes."""

        return _RESUME_ENVELOPE.pack(RESUME_MAGIC, self.next_sequence, self.nonce)

    def digest(self) -> str:
        """Return the link used as the first resumed batch's previous hash."""

        return hashlib.sha256(self.encode()).hexdigest()

    @classmethod
    def decode(cls, value: bytes) -> "ResumeEnvelope":
        if len(value) != RESUME_ENVELOPE_BYTES:
            raise ProtocolError(f"resume commitment envelope must be {RESUME_ENVELOPE_BYTES} bytes")
        magic, next_sequence, nonce = _RESUME_ENVELOPE.unpack(value)
        if magic != RESUME_MAGIC:
            raise ProtocolError("unsupported resume commitment envelope magic")
        return cls(next_sequence=next_sequence, nonce=nonce)


type OnChainEnvelope = CommitmentEnvelope | ResumeEnvelope


def decode_envelope(value: bytes) -> OnChainEnvelope:
    """Decode either an append-only batch pointer or a future-only resume marker."""

    if value.startswith(MAGIC):
        return CommitmentEnvelope.decode(value)
    if value.startswith(RESUME_MAGIC):
        return ResumeEnvelope.decode(value)
    raise ProtocolError("unsupported commitment envelope magic")
