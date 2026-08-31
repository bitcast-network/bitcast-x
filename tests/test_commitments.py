"""Golden tests for the fixed on-chain commitment envelope."""

import pytest

from bitcast_x.errors import ProtocolError
from bitcast_x.protocol.commitments import (
    ENVELOPE_BYTES,
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
        (b"", "45 bytes"),
        (b"BAD" + bytes(42), "magic"),
    ],
)
def test_commitment_envelope_rejects_malformed_bytes(value: bytes, message: str) -> None:
    with pytest.raises(ProtocolError, match=message):
        CommitmentEnvelope.decode(value)


def test_resume_envelope_golden_vector_and_domain_separated_digest() -> None:
    envelope = ResumeEnvelope(next_sequence=4, nonce=bytes(range(32)))

    encoded = envelope.encode()

    assert len(encoded) == RESUME_ENVELOPE_BYTES == 43
    assert encoded.hex() == (
        "4458520000000000000004000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    )
    assert decode_envelope(encoded) == envelope
    assert (
        envelope.digest()
        != CommitmentEnvelope(sequence=4, event_count=1, batch_hash=envelope.nonce).batch_hash.hex()
    )
