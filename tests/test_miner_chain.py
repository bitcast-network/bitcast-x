"""Unit tests for the Bittensor v11 miner commitment adapter."""

from dataclasses import dataclass
from typing import Any

import pytest

from bitcast_x.errors import ChainOperationError
from bitcast_x.miner import BittensorCommitmentSubmitter
from bitcast_x.protocol import CommitmentEnvelope

HOTKEY = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"


@dataclass
class FakeHotkey:
    ss58_address: str = HOTKEY


@dataclass
class FakeWallet:
    hotkey: FakeHotkey


@dataclass
class FakeCommitment:
    block: int
    fields: list[dict[str, str]]


@dataclass
class FakeBlock:
    extrinsics: list[dict[str, Any]]


@dataclass
class FakeResult:
    extrinsic_id: str | None


class FakeChain:
    netuid = 93

    def __init__(self, envelope: CommitmentEnvelope) -> None:
        self.envelope = envelope
        self.maximum = 3_100
        self.used = 100
        self.epoch = 7
        self.commitment_value: FakeCommitment | None = FakeCommitment(
            block=42,
            fields=[{"Raw45": "0x" + envelope.encode().hex()}],
        )
        self.result = FakeResult("42-0002")

    async def commitment_capacity(self, hotkey: str) -> tuple[int, int, int]:
        assert hotkey == HOTKEY
        return self.maximum, self.used, self.epoch

    async def commitment(self, hotkey: str, *, block: int | None = None) -> FakeCommitment | None:
        assert hotkey == HOTKEY
        assert block is None or block == 42
        return self.commitment_value

    async def submit_commitment(
        self, wallet: FakeWallet, envelope: CommitmentEnvelope
    ) -> FakeResult:
        assert wallet.hotkey.ss58_address == HOTKEY
        assert envelope == self.envelope
        return self.result

    async def block_info(self, block: int) -> FakeBlock:
        assert block == 42
        return FakeBlock(
            extrinsics=[
                {"call": {"call_module": "Timestamp", "call_function": "set"}},
                {"address": "another-hotkey", "call": {}},
                commitment_extrinsic(self.envelope),
            ]
        )


def commitment_extrinsic(envelope: CommitmentEnvelope) -> dict[str, Any]:
    return {
        "address": HOTKEY,
        "call": {
            "call_module": "Commitments",
            "call_function": "set_commitment",
            "call_args": [
                {"name": "netuid", "value": 93},
                {
                    "name": "info",
                    "value": {"fields": [{"Raw45": "0x" + envelope.encode().hex()}]},
                },
            ],
        },
    }


def make_submitter() -> tuple[BittensorCommitmentSubmitter, FakeChain, CommitmentEnvelope]:
    envelope = CommitmentEnvelope(sequence=1, event_count=1, batch_hash=b"a" * 32)
    chain = FakeChain(envelope)
    submitter = BittensorCommitmentSubmitter(
        chain,  # type: ignore[arg-type]
        FakeWallet(hotkey=FakeHotkey()),
    )
    return submitter, chain, envelope


@pytest.mark.asyncio
async def test_reads_live_capacity_budget() -> None:
    submitter, _chain, envelope = make_submitter()

    budget = await submitter.capacity(envelope)

    assert budget.remaining_space == 3_000
    assert budget.next_call_charge == 100
    assert budget.can_commit is True


@pytest.mark.asyncio
async def test_recovers_exact_commitment_position_from_finalized_block() -> None:
    submitter, _chain, envelope = make_submitter()

    latest = await submitter.latest()

    assert latest is not None
    assert latest.position.block == 42
    assert latest.position.extrinsic_index == 2
    assert latest.stored_envelope == envelope.encode()


@pytest.mark.asyncio
async def test_submit_rereads_finalized_storage() -> None:
    submitter, _chain, envelope = make_submitter()

    finalized = await submitter.submit(envelope)

    assert finalized.position.block == 42
    assert finalized.position.extrinsic_index == 2
    assert finalized.stored_envelope == envelope.encode()


@pytest.mark.asyncio
async def test_recovery_rejects_missing_matching_extrinsic() -> None:
    submitter, chain, _envelope = make_submitter()

    async def empty_block(_block: int) -> FakeBlock:
        return FakeBlock(extrinsics=[])

    chain.block_info = empty_block  # type: ignore[method-assign]

    with pytest.raises(ChainOperationError, match="found 0"):
        await submitter.latest()
