"""Golden tests for the fixed on-chain commitment envelope."""

import pytest

from bitcast_x.errors import ProtocolError
from bitcast_x.protocol.commitments import ENVELOPE_BYTES, CommitmentEnvelope


def test_commitment_envelope_golden_vector() -> None:
    envelope = CommitmentEnvelope(sequence=1, event_count=2, batch_hash=bytes(range(32)))

    encoded = envelope.encode()

    assert len(encoded) == ENVELOPE_BYTES == 45
    assert encoded.hex() == (
        "44583200000000000000010002000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    )
    assert CommitmentEnvelope.decode(encoded) == envelope


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (b"", "45 bytes"),
        (b"BAD" + bytes(42), "magic"),
    ],
)
def test_commitment_envelope_rejects_malformed_bytes(value: bytes, message: str) -> None:
    with pytest.raises(ProtocolError, match=message):
        CommitmentEnvelope.decode(value)
