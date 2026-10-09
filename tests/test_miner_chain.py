"""Unit tests for the Bittensor v11 miner commitment adapter."""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from bitcast_x.chain import BittensorChain
from bitcast_x.errors import ChainOperationError
from bitcast_x.miner import BittensorCommitmentSubmitter
from bitcast_x.protocol import CommitmentEnvelope, CommitmentPosition
from commitment_fixture import (
    INCIDENT_EXTRINSIC_INDEX,
    INCIDENT_HOTKEY,
    INCIDENT_PAYLOAD_HEX,
    FixtureClient,
    load_duplicate_commitment_fixture,
)

HOTKEY = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
OTHER_ENVELOPE = CommitmentEnvelope(sequence=2, event_count=1, batch_hash=b"b" * 32)


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
class FakeResult:
    extrinsic_id: str | None


class FakeChain:
    def __init__(self, envelope: CommitmentEnvelope) -> None:
        self.envelope = envelope
        self.maximum = 3_100
        self.used = 100
        self.epoch = 7
        self.commitment_value: FakeCommitment | None = FakeCommitment(
            block=42,
            fields=[{"Raw45": "0x" + envelope.encode().hex()}],
        )
        self.commitment_reads: list[int | None] = []
        self.resolved: list[Any] = []
        self.result = FakeResult("42-0002")

    async def commitment_capacity(self, hotkey: str) -> tuple[int, int, int]:
        assert hotkey == HOTKEY
        return self.maximum, self.used, self.epoch

    async def commitment(self, hotkey: str, *, block: int | None = None) -> FakeCommitment | None:
        assert hotkey == HOTKEY
        self.commitment_reads.append(block)
        return self.commitment_value

    async def submit_commitment(
        self, wallet: FakeWallet, envelope: CommitmentEnvelope
    ) -> FakeResult:
        assert wallet.hotkey.ss58_address == HOTKEY
        assert envelope == self.envelope
        return self.result

    async def resolve_commitments_in_block(
        self,
        block: int,
        *,
        hotkey: str | None = None,
    ) -> list[Any]:
        assert block == 42
        assert hotkey == HOTKEY
        return self.resolved


def make_submitter() -> tuple[BittensorCommitmentSubmitter, FakeChain, CommitmentEnvelope]:
    envelope = CommitmentEnvelope(sequence=1, event_count=1, batch_hash=b"a" * 32)
    chain = FakeChain(envelope)
    submitter = BittensorCommitmentSubmitter(
        chain,  # type: ignore[arg-type]
        FakeWallet(hotkey=FakeHotkey()),
    )
    return submitter, chain, envelope


async def test_reads_live_capacity_budget() -> None:
    submitter, _chain, envelope = make_submitter()

    budget = await submitter.capacity(envelope)

    assert budget.remaining_space == 3_000
    assert budget.next_call_charge == 100
    assert budget.can_commit is True


async def test_latest_uses_shared_duplicate_commitment_resolution() -> None:
    fixture = load_duplicate_commitment_fixture()
    chain = BittensorChain(FixtureClient(fixture), netuid=fixture["netuid"])
    submitter = BittensorCommitmentSubmitter(
        chain,
        FakeWallet(hotkey=FakeHotkey(ss58_address=INCIDENT_HOTKEY)),
    )

    latest = await submitter.latest()

    assert latest is not None
    assert latest.position == CommitmentPosition(
        block=fixture["block"],
        extrinsic_index=INCIDENT_EXTRINSIC_INDEX,
    )
    assert latest.stored_envelope.hex() == INCIDENT_PAYLOAD_HEX


async def test_submit_rereads_finalized_storage() -> None:
    submitter, chain, envelope = make_submitter()
    # Storage holding other bytes tells a re-read from an echo of the submitted
    # envelope; the engine, not the submitter, rejects the mismatch.
    stored = OTHER_ENVELOPE.encode()
    chain.commitment_value = FakeCommitment(block=42, fields=[{"Raw45": "0x" + stored.hex()}])

    finalized = await submitter.submit(envelope)

    assert finalized.position == CommitmentPosition(block=42, extrinsic_index=2)
    assert chain.commitment_reads == [42]
    assert finalized.stored_envelope == stored


@pytest.mark.parametrize(
    "resolved",
    [
        pytest.param([], id="no-commitment-extrinsic"),
        pytest.param(
            [SimpleNamespace(payload=OTHER_ENVELOPE.encode(), extrinsic_index=2)],
            id="only-another-payload",
        ),
    ],
)
async def test_recovery_rejects_missing_matching_extrinsic(resolved: list[Any]) -> None:
    submitter, chain, _envelope = make_submitter()
    chain.resolved = resolved

    with pytest.raises(ChainOperationError, match="found 0"):
        await submitter.latest()
