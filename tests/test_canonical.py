"""Golden and rejection tests for consensus canonical JSON."""

from datetime import UTC, datetime

import pytest

from bitcast_x.errors import ProtocolError
from bitcast_x.protocol.canonical import canonical_json, hash_hex


def test_canonical_json_golden_vector() -> None:
    value = {
        "z": "Ａ",
        "a": [datetime(2026, 8, 5, 12, 0, tzinfo=UTC), {"é": "e\u0301"}],
    }

    assert canonical_json(value) == '{"a":["2026-08-05T12:00:00Z",{"é":"é"}],"z":"A"}'.encode()
    assert (
        hash_hex("dx2/test", value)
        == "90e9a28bc08394b2c0c9cbb6f21a71561c214da4d1e39731308f5c17d6150a7d"
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), b"bytes"])
def test_canonical_json_rejects_unsupported_values(value: object) -> None:
    with pytest.raises(ProtocolError):
        canonical_json(value)


def test_canonical_json_rejects_nfkc_key_collision() -> None:
    with pytest.raises(ProtocolError, match="collide"):
        canonical_json({"Ａ": 1, "A": 2})
