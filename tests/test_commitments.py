"""Golden tests for the fixed on-chain commitment envelope."""

import pytest

from bitcast_x.errors import ProtocolError
from bitcast_x.protocol.commitments import (
    ENVELOPE_BYTES,
    HISTORY_ENVELOPE_BYTES,
    RESUME_ENVELOPE_BYTES,
    CommitmentEnvelope,
    ResumeEnvelope,
    decode_envelope,
)


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
        (b"", "45 or 77 bytes"),
        (b"BAD" + bytes(42), "magic"),
    ],
)
def test_commitment_envelope_rejects_malformed_bytes(value: bytes, message: str) -> None:
    with pytest.raises(ProtocolError, match=message):
        CommitmentEnvelope.decode(value)


def test_history_envelopes_have_stable_golden_vectors() -> None:
    history_id = bytes(range(32))
    envelope = ResumeEnvelope(history_id=history_id)

    encoded = envelope.encode()

    assert len(encoded) == RESUME_ENVELOPE_BYTES == 35
    assert encoded.hex() == "445852000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    assert decode_envelope(encoded) == envelope

    commitment = CommitmentEnvelope(
        sequence=1,
        event_count=2,
        batch_hash=b"b" * 32,
        history_id=history_id,
    )
    assert len(commitment.encode()) == HISTORY_ENVELOPE_BYTES == 77
    assert decode_envelope(commitment.encode()) == commitment
