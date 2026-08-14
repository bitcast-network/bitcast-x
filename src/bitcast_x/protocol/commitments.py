"""Binary envelope committed through Bittensor's Commitments pallet."""

import struct
from dataclasses import dataclass

from bitcast_x.errors import ProtocolError

MAGIC = b"DX2"
BATCH_HASH_BYTES = 32
ENVELOPE_BYTES = len(MAGIC) + 8 + 2 + BATCH_HASH_BYTES
_ENVELOPE = struct.Struct(">3sQH32s")


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
