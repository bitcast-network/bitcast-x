"""Offline tests for the read-only finalized commitment replay command."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from bitcast_x import replay_commitment_block as replay_module
from bitcast_x.chain import BittensorChain, ChainCommitment
from bitcast_x.config import Settings
from bitcast_x.protocol import CommitmentEnvelope
from bitcast_x.replay_commitment_block import replay_commitments


@pytest.mark.asyncio
async def test_replay_uses_settings_chain_and_closes_without_wallet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = CommitmentEnvelope(sequence=490, event_count=1, batch_hash=b"a" * 32)
    observed = ChainCommitment(
        hotkey="miner",
        block=9_031_452,
        extrinsic_index=11,
        timestamp=datetime(2026, 9, 9, 17, 30, tzinfo=UTC),
        envelope=envelope,
    )
    fake = SimpleNamespace(closed=False)

    async def commitments_in_block(block: int) -> list[ChainCommitment]:
        assert block == 9_031_452
        return [observed]

    async def close() -> None:
        fake.closed = True

    fake.commitments_in_block = commitments_in_block
    fake.close = close

    async def connect(
        network: str,
        *,
        netuid: int,
        mechanism_id: int,
    ) -> Any:
        assert (network, netuid, mechanism_id) == ("test", 93, 1)
        return fake

    monkeypatch.setattr(BittensorChain, "connect", connect)
    settings = Settings(network="test", netuid=93, mechanism_id=1)

    replay = await replay_commitments(settings, block=9_031_452, hotkey="miner")

    assert replay == {
        "network": "test",
        "netuid": 93,
        "block": 9_031_452,
        "commitments": [
            {
                "hotkey": "miner",
                "block": 9_031_452,
                "extrinsic_index": 11,
                "timestamp": "2026-09-09T17:30:00+00:00",
                "sequence": 490,
                "event_count": 1,
                "batch_hash": "sha256-" + (b"a" * 32).hex(),
                "history_id": None,
            }
        ],
    }
    assert fake.closed is True


def test_cli_prints_machine_readable_public_replay(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def replay(
        _settings: Settings,
        *,
        block: int,
        hotkey: str | None = None,
    ) -> dict[str, Any]:
        assert (block, hotkey) == (9_031_452, "miner")
        return {"block": block, "commitments": [{"hotkey": hotkey, "extrinsic_index": 11}]}

    monkeypatch.setattr(replay_module, "replay_commitments", replay)

    replay_module.main(["--block", "9031452", "--hotkey", "miner"])

    assert json.loads(capsys.readouterr().out) == {
        "block": 9_031_452,
        "commitments": [{"hotkey": "miner", "extrinsic_index": 11}],
    }
