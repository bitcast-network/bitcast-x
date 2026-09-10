"""Public finalized-chain fixture support for commitment resolver tests."""

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "block_9031452_duplicate_commitment.json"
INCIDENT_HOTKEY = "5HVydkt4MPHgMZW2tqew646Nxxo1RvrC4aUzAsMmmyKvVPYD"
INCIDENT_EXTRINSIC_INDEX = 11
INCIDENT_PAYLOAD_HEX = (
    "445833216d8d27631d366c36f72033c7342b2764e3a3660ae9a6133b10939d101913bf"
    "00000000000001ea00018374cc4cbcd9595639d1d86e651f88dff18e815706e03a2639bfa"
    "5618bab61c4"
)


def load_duplicate_commitment_fixture() -> dict[str, Any]:
    """Load the byte-exact public block 9031452 incident capture."""

    return json.loads(FIXTURE_PATH.read_text())


class FixtureClient:
    """Serve the public fixture through the Bittensor client methods the resolver uses."""

    def __init__(self, fixture: dict[str, Any]) -> None:
        self.fixture = deepcopy(fixture)
        self.identity = self
        self.subnets = self

    async def block_info(self, block: int) -> Any:
        assert block == self.fixture["block"]
        return SimpleNamespace(
            extrinsics=deepcopy(self.fixture["extrinsics"]),
            timestamp=datetime.fromisoformat(self.fixture["timestamp"]),
        )

    async def at(self, block: int) -> "FixtureClient":
        assert block == self.fixture["block"]
        return self

    async def query(self, _storage: Any) -> list[dict[str, Any]]:
        return deepcopy(self.fixture["events"])

    async def commitment(self, *, netuid: int, hotkey_ss58: str) -> Any:
        assert netuid == self.fixture["netuid"]
        if hotkey_ss58 != self.fixture["hotkey"]:
            return None
        return SimpleNamespace(**deepcopy(self.fixture["storage"]))

    async def metagraph(self, *, netuid: int) -> Any:
        assert netuid == self.fixture["netuid"]
        return SimpleNamespace(
            by_hotkey=lambda hotkey: object() if hotkey == self.fixture["hotkey"] else None,
        )
