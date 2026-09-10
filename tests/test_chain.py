"""Unit tests for the narrow Bittensor v11 chain adapter."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import bittensor as bt
import pytest

from bitcast_x.chain import BittensorChain
from bitcast_x.errors import ChainOperationError, ProtocolError
from bitcast_x.protocol import CommitmentEnvelope, CommitmentPosition
from commitment_fixture import (
    INCIDENT_EXTRINSIC_INDEX,
    INCIDENT_HOTKEY,
    INCIDENT_PAYLOAD_HEX,
    FixtureClient,
    load_duplicate_commitment_fixture,
)


class FakeClient:
    """Capture generated calls without pretending to be a real chain."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.submitted: tuple[Any, Any, dict[str, Any]] | None = None
        self.executed: tuple[Any, Any, dict[str, Any]] | None = None
        self.queried: tuple[Any, list[int]] | None = None

    async def submit_call(self, call: Any, wallet: Any, **kwargs: Any) -> Any:
        self.submitted = (call, wallet, kwargs)
        return self.result

    async def execute(self, intent: Any, wallet: Any, **kwargs: Any) -> Any:
        self.executed = (intent, wallet, kwargs)
        return self.result

    async def query(self, storage: Any, params: list[int]) -> Any:
        self.queried = (storage, params)
        return self.result


@pytest.mark.asyncio
async def test_submit_commitment_uses_v11_raw_call_and_hotkey() -> None:
    client = FakeClient(SimpleNamespace(success=True))
    chain = BittensorChain(client, netuid=93)
    envelope = CommitmentEnvelope(sequence=7, event_count=3, batch_hash=b"x" * 32)
    wallet = object()

    await chain.submit_commitment(wallet, envelope)

    assert client.submitted is not None
    call, submitted_wallet, options = client.submitted
    assert submitted_wallet is wallet
    assert call.module == "Commitments"
    assert call.function == "set_commitment"
    assert call.params == {
        "netuid": 93,
        "info": {"fields": [{"Raw45": envelope.encode()}]},
    }
    assert options == {
        "signer": "hotkey",
        "wait_for_inclusion": True,
        "wait_for_finalization": True,
    }


@pytest.mark.asyncio
async def test_submit_commitment_exposes_structured_chain_failure() -> None:
    result = SimpleNamespace(
        success=False,
        message="not registered",
        error=SimpleNamespace(code="account_not_allowed_commit"),
    )
    chain = BittensorChain(FakeClient(result), netuid=93)
    envelope = CommitmentEnvelope(sequence=1, event_count=1, batch_hash=b"x" * 32)

    with pytest.raises(ChainOperationError, match="account_not_allowed_commit"):
        await chain.submit_commitment(object(), envelope)


@pytest.mark.asyncio
async def test_set_weights_uses_v11_conforming_intent_and_mechanism() -> None:
    client = FakeClient(SimpleNamespace(success=True))
    chain = BittensorChain(client, netuid=93, mechanism_id=1)
    wallet = object()

    await chain.set_weights(wallet, {0: 0.0, 7: 0.25, 9: 0.75}, version_key=3)

    assert client.executed is not None
    intent, submitted_wallet, options = client.executed
    assert submitted_wallet is wallet
    assert intent.netuid == 93
    assert intent.mechid == 1
    assert intent.version_key == 3
    assert intent.uids == [0, 7, 9]
    assert intent.weights == [0.0, 0.25, 0.75]
    assert options == {"wait_for_inclusion": True, "wait_for_finalization": True}


class SequencedExecuteClient(FakeClient):
    def __init__(self, results: list[Any]) -> None:
        super().__init__(results[-1])
        self.results = iter(results)
        self.execute_count = 0

    async def execute(self, intent: Any, wallet: Any, **kwargs: Any) -> Any:
        self.executed = (intent, wallet, kwargs)
        self.execute_count += 1
        return next(self.results)


def stale_nonce_result() -> SimpleNamespace:
    return SimpleNamespace(
        success=False,
        message=(
            "The extrinsic nonce is below the account's on-chain nonce "
            "(already used or superseded). Rebuild and resign with a fresh nonce."
        ),
        error=SimpleNamespace(code="EXPIRED"),
    )


@pytest.mark.asyncio
async def test_set_weights_rebuilds_after_stale_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("bitcast_x.chain.asyncio.sleep", sleep)
    client = SequencedExecuteClient([stale_nonce_result(), SimpleNamespace(success=True)])
    chain = BittensorChain(client, netuid=93, mechanism_id=1)

    result = await chain.set_weights(object(), {0: 0.25, 7: 0.75}, version_key=3)

    assert result.success is True
    assert client.execute_count == 2
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_weights_does_not_retry_other_expired_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("bitcast_x.chain.asyncio.sleep", sleep)
    result = SimpleNamespace(
        success=False,
        message="Transaction expired before inclusion",
        error=SimpleNamespace(code="EXPIRED"),
    )
    client = SequencedExecuteClient([result])
    chain = BittensorChain(client, netuid=93, mechanism_id=1)

    with pytest.raises(ChainOperationError, match="Transaction expired"):
        await chain.set_weights(object(), {0: 1.0}, version_key=3)

    assert client.execute_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_weights_stops_after_bounded_stale_nonce_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("bitcast_x.chain.asyncio.sleep", sleep)
    client = SequencedExecuteClient([stale_nonce_result() for _ in range(3)])
    chain = BittensorChain(client, netuid=93, mechanism_id=1)

    with pytest.raises(ChainOperationError, match="nonce is below"):
        await chain.set_weights(object(), {0: 1.0}, version_key=3)

    assert client.execute_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_last_weight_update_reads_mechanism_specific_storage() -> None:
    client = FakeClient([0, 0, 123])
    chain = BittensorChain(client, netuid=93, mechanism_id=1)
    assert await chain.last_weight_update(2) == 123
    assert client.queried is not None
    assert client.queried[1] == [4096 + 93]


@pytest.mark.asyncio
async def test_last_weight_update_fails_closed_on_missing_uid() -> None:
    chain = BittensorChain(FakeClient([0]), netuid=93, mechanism_id=1)
    with pytest.raises(ChainOperationError, match="last-update storage"):
        await chain.last_weight_update(2)


class BlockClient:
    def __init__(self, extrinsics: list[dict[str, Any]], *, registered: bool) -> None:
        self.extrinsics = extrinsics
        self.registered = registered
        self.subnets = self

    async def block_info(self, _block: int) -> Any:
        return SimpleNamespace(
            extrinsics=self.extrinsics,
            timestamp=datetime(2026, 8, 5, tzinfo=UTC),
        )

    async def at(self, _block: int) -> "BlockClient":
        return self

    async def metagraph(self, *, netuid: int) -> Any:
        del netuid
        return SimpleNamespace(
            by_hotkey=lambda _hotkey: object() if self.registered else None,
        )


class QualificationClient:
    """Expose one historical owner-lock and aggregate miner-hotkey stake snapshot."""

    def __init__(self, *, lock: dict[str, str] | None) -> None:
        self.neurons = self
        self.locks = self
        self.lock = lock
        self.at_block: int | None = None
        self.stake_query: tuple[Any, list[Any]] | None = None

    async def at(self, block: int) -> "QualificationClient":
        self.at_block = block
        return self

    async def hotkey_owner(self, _hotkey: str) -> str:
        return "coldkey"

    async def query(self, item: Any, params: list[Any]) -> int:
        self.stake_query = (item, params)
        return 15_000_000_000_000

    async def coldkey_lock(self, _coldkey: str, _netuid: int) -> dict[str, str] | None:
        return self.lock

    async def runtime(self, _api: Any, _params: list[Any]) -> dict[str, Any]:
        return {"conviction": {"bits": 15_000_000_000_000 << 64}}


@pytest.mark.asyncio
async def test_qualification_reads_aggregate_hotkey_stake_at_historical_block() -> None:
    client = QualificationClient(lock={"hotkey": "owner"})
    chain = BittensorChain(client, netuid=93)

    inputs = await chain.miner_qualification_inputs("miner", block=123, include_self_stake=True)

    assert inputs == ("coldkey", "owner", 15_000_000_000_000, 15_000_000_000_000)
    assert client.at_block == 123
    assert client.stake_query == (
        bt.storage.SubtensorModule.TotalHotkeyAlpha,
        ["miner", 93],
    )


@pytest.mark.asyncio
async def test_qualification_still_reads_hotkey_stake_without_a_lock() -> None:
    client = QualificationClient(lock=None)
    chain = BittensorChain(client, netuid=93)

    inputs = await chain.miner_qualification_inputs("miner", block=123, include_self_stake=True)

    assert inputs == ("coldkey", None, 0, 15_000_000_000_000)


@pytest.mark.asyncio
async def test_lock_only_qualification_skips_unused_hotkey_stake_read() -> None:
    client = QualificationClient(lock={"hotkey": "owner"})
    chain = BittensorChain(client, netuid=93)

    inputs = await chain.miner_qualification_inputs("miner", block=123)

    assert inputs == ("coldkey", "owner", 15_000_000_000_000, 0)
    assert client.stake_query is None


def commitment_extrinsic(hotkey: str) -> dict[str, Any]:
    return {
        "address": hotkey,
        "call": {
            "call_module": "Commitments",
            "call_function": "set_commitment",
            "call_args": [{"name": "netuid", "value": 93}],
        },
    }


@pytest.mark.asyncio
async def test_identical_successful_overwrite_resolves_last_public_block_position() -> None:
    fixture = load_duplicate_commitment_fixture()
    chain = BittensorChain(FixtureClient(fixture), netuid=fixture["netuid"])

    observations = await chain.commitments_in_block(fixture["block"])

    matching = [item for item in observations if item.hotkey == INCIDENT_HOTKEY]
    assert len(matching) == 1
    assert matching[0].extrinsic_index == INCIDENT_EXTRINSIC_INDEX
    assert matching[0].envelope.encode().hex() == INCIDENT_PAYLOAD_HEX
    positioned = await chain.commitment_at_position(
        INCIDENT_HOTKEY,
        CommitmentPosition(block=fixture["block"], extrinsic_index=INCIDENT_EXTRINSIC_INDEX),
    )
    assert positioned == matching[0]
    with pytest.raises(ProtocolError, match="claimed position"):
        await chain.commitment_at_position(
            INCIDENT_HOTKEY,
            CommitmentPosition(block=fixture["block"], extrinsic_index=10),
        )


def _system_event(fixture: dict[str, Any], index: int) -> dict[str, Any]:
    return next(
        event
        for event in fixture["events"]
        if event.get("extrinsic_idx") == index
        and event.get("module_id") == "System"
        and event.get("event_id") in {"ExtrinsicSuccess", "ExtrinsicFailed"}
    )


def _remove_system_event(fixture: dict[str, Any], index: int) -> None:
    fixture["events"] = [
        event
        for event in fixture["events"]
        if not (event.get("extrinsic_idx") == index and event.get("module_id") == "System")
    ]


@pytest.mark.asyncio
async def test_failed_duplicate_call_is_ignored_before_payload_comparison() -> None:
    fixture = load_duplicate_commitment_fixture()
    failed = _system_event(fixture, 10)
    failed["event_id"] = "ExtrinsicFailed"
    failed["event"]["event_id"] = "ExtrinsicFailed"
    fixture["extrinsics"][10]["call"]["call_args"][1]["value"]["fields"] = [{"Raw1": "0xff"}]
    chain = BittensorChain(FixtureClient(fixture), netuid=fixture["netuid"])

    observations = await chain.commitments_in_block(fixture["block"])

    assert [(item.hotkey, item.extrinsic_index) for item in observations] == [
        (INCIDENT_HOTKEY, INCIDENT_EXTRINSIC_INDEX)
    ]


@pytest.mark.asyncio
async def test_only_failed_commitment_call_is_not_observed() -> None:
    fixture = load_duplicate_commitment_fixture()
    fixture["extrinsics"][10] = {"call": {"call_module": "Timestamp", "call_function": "set"}}
    _remove_system_event(fixture, 10)
    failed = _system_event(fixture, 11)
    failed["event_id"] = "ExtrinsicFailed"
    failed["event"]["event_id"] = "ExtrinsicFailed"
    chain = BittensorChain(FixtureClient(fixture), netuid=fixture["netuid"])

    assert await chain.commitments_in_block(fixture["block"]) == []


@pytest.mark.asyncio
async def test_conflicting_successful_payloads_are_rejected() -> None:
    fixture = load_duplicate_commitment_fixture()
    fixture["extrinsics"][10]["call"]["call_args"][1]["value"]["fields"] = [{"Raw1": "0xff"}]
    chain = BittensorChain(FixtureClient(fixture), netuid=fixture["netuid"])

    with pytest.raises(ChainOperationError, match="successful commitments conflict"):
        await chain.commitments_in_block(fixture["block"])


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_missing_or_malformed_dispatch_evidence_is_rejected(malformed: bool) -> None:
    fixture = load_duplicate_commitment_fixture()
    event = _system_event(fixture, 10)
    if malformed:
        event["extrinsic_idx"] = "not-an-index"
    else:
        _remove_system_event(fixture, 10)
    chain = BittensorChain(FixtureClient(fixture), netuid=fixture["netuid"])

    with pytest.raises(ChainOperationError, match="dispatch outcome is unavailable"):
        await chain.commitments_in_block(fixture["block"])


@pytest.mark.asyncio
async def test_contradictory_dispatch_evidence_is_rejected() -> None:
    fixture = load_duplicate_commitment_fixture()
    fixture["extrinsics"][10] = {"call": {"call_module": "Timestamp", "call_function": "set"}}
    _remove_system_event(fixture, 10)
    failed = deepcopy(_system_event(fixture, 11))
    failed["event_id"] = "ExtrinsicFailed"
    failed["event"]["event_id"] = "ExtrinsicFailed"
    fixture["events"].append(failed)
    chain = BittensorChain(FixtureClient(fixture), netuid=fixture["netuid"])

    with pytest.raises(ChainOperationError, match="dispatch outcome is contradictory"):
        await chain.commitments_in_block(fixture["block"])


@pytest.mark.asyncio
async def test_contradictory_flat_and_nested_event_identity_is_rejected() -> None:
    fixture = load_duplicate_commitment_fixture()
    event = _system_event(fixture, 10)
    event["event"]["event_id"] = "ExtrinsicFailed"
    chain = BittensorChain(FixtureClient(fixture), netuid=fixture["netuid"])

    with pytest.raises(ChainOperationError, match="event identity is contradictory"):
        await chain.commitments_in_block(fixture["block"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["block", "bytes"])
async def test_pinned_storage_mismatch_is_rejected(mismatch: str) -> None:
    fixture = load_duplicate_commitment_fixture()
    stored = fixture["storage"]
    if mismatch == "block":
        stored["block"] = fixture["block"] - 1
    else:
        stored["fields"] = [{"Raw1": "0xff"}]
    chain = BittensorChain(FixtureClient(fixture), netuid=fixture["netuid"])

    with pytest.raises(ChainOperationError, match=f"storage {mismatch} mismatch"):
        await chain.commitments_in_block(fixture["block"])


@pytest.mark.asyncio
async def test_normal_single_success_is_preserved() -> None:
    fixture = load_duplicate_commitment_fixture()
    fixture["extrinsics"][10] = {"call": {"call_module": "Timestamp", "call_function": "set"}}
    _remove_system_event(fixture, 10)
    chain = BittensorChain(FixtureClient(fixture), netuid=fixture["netuid"])

    observations = await chain.commitments_in_block(fixture["block"])

    assert [(item.hotkey, item.extrinsic_index) for item in observations] == [
        (INCIDENT_HOTKEY, INCIDENT_EXTRINSIC_INDEX)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unrelated_hotkey", "unrelated_netuid"),
    [
        ("5GBjoKMB44CHcTeYhbpk2TNM9pG1G1rfs9TeSMCgxS6R9KUU", 93),
        (INCIDENT_HOTKEY, 123),
    ],
)
async def test_claimed_position_ignores_synthetic_unrelated_calls(
    unrelated_hotkey: str, unrelated_netuid: int
) -> None:
    fixture = load_duplicate_commitment_fixture()
    fixture["extrinsics"][9] = {
        "address": unrelated_hotkey,
        "call": {
            "call_module": "Commitments",
            "call_function": "set_commitment",
            "call_args": [
                {"name": "netuid", "value": unrelated_netuid},
                {"name": "info", "value": {"fields": [{"Raw1": "0xff"}]}},
            ],
        },
    }
    chain = BittensorChain(FixtureClient(fixture), netuid=fixture["netuid"])

    observation = await chain.commitment_at_position(
        INCIDENT_HOTKEY,
        CommitmentPosition(block=fixture["block"], extrinsic_index=INCIDENT_EXTRINSIC_INDEX),
    )

    assert observation.hotkey == INCIDENT_HOTKEY
    assert observation.extrinsic_index == INCIDENT_EXTRINSIC_INDEX


@pytest.mark.asyncio
async def test_unregistered_commitment_signer_is_ignored_at_finalized_block() -> None:
    hotkey = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
    chain = BittensorChain(
        BlockClient([commitment_extrinsic(hotkey)], registered=False),
        netuid=93,
    )

    assert await chain.commitments_in_block(10) == []
