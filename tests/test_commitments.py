"""Golden tests for the fixed on-chain commitment envelope."""

import pytest

from bitcast_x.errors import ProtocolError
from bitcast_x.protocol.commitments import (
    ENVELOPE_BYTES,
    HISTORY_ENVELOPE_BYTES,
    CommitmentEnvelope,
    decode_envelope,
)
from commitment_fixture import load_duplicate_commitment_fixture


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
    commitment = CommitmentEnvelope(
        sequence=1,
        event_count=2,
        batch_hash=b"b" * 32,
        history_id=history_id,
    )

    encoded = commitment.encode()

    assert len(encoded) == HISTORY_ENVELOPE_BYTES == 77
    # "DX3" | history_id | sequence u64 BE | event_count u16 BE | batch_hash
    assert encoded.hex() == (
        "445833"
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
        "0000000000000001"
        "0002"
        "6262626262626262626262626262626262626262626262626262626262626262"
    )
    assert decode_envelope(encoded) == commitment


def test_finalized_incident_raw77_envelope_decodes_to_its_fields() -> None:
    (field,) = load_duplicate_commitment_fixture()["storage"]["fields"]
    encoded = bytes.fromhex(field["Raw77"].removeprefix("0x"))

    envelope = decode_envelope(encoded)

    assert envelope == CommitmentEnvelope(
        sequence=490,
        event_count=1,
        batch_hash=bytes.fromhex(
            "8374cc4cbcd9595639d1d86e651f88dff18e815706e03a2639bfa5618bab61c4"
        ),
        history_id=bytes.fromhex(
            "216d8d27631d366c36f72033c7342b2764e3a3660ae9a6133b10939d101913bf"
        ),
    )
    assert envelope.encode() == encoded
