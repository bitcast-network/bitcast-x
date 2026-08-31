"""Tests for crash-safe finalized validator reconciliation."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from bitcast_x.chain import ChainCommitment
from bitcast_x.errors import ProtocolError
from bitcast_x.protocol import (
    ClaimEvent,
    CommitmentEnvelope,
    CommitmentPosition,
    CommittedBatch,
    OnChainEnvelope,
)
from bitcast_x.transport import BatchPageResponse, PositionedBatch
from bitcast_x.validator.ingestion import (
    MinerEndpoint,
    ValidatorIngestor,
)
from bitcast_x.validator.store import ValidatorStore

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
OLD_MINER = "5FhG3BhBVmqugRW8m39Q9WQgJ3PjJDgMvnP89SDo9Rsz7rAF"


def batches() -> tuple[CommittedBatch, CommittedBatch]:
    first_event = ClaimEvent(
        claim_id="01" * 16,
        campaign_id="campaign",
        creator_x_id="123",
        created_at="2026-08-05T12:00:00Z",
        draft_commitment="02" * 32,
    )
    second_event = ClaimEvent(
        claim_id="03" * 16,
        campaign_id="campaign",
        creator_x_id="123",
        created_at="2026-08-05T12:01:00Z",
        draft_commitment="04" * 32,
    )
    first = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        events=(first_event,),
    )
    second = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=2,
        previous_batch_hash=first.batch_hash,
        events=(second_event,),
    )
    return first, second


def observation(batch: CommittedBatch, block: int) -> ChainCommitment:
    return ChainCommitment(
        hotkey=MINER,
        block=block,
        extrinsic_index=2,
        timestamp=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
        envelope=CommitmentEnvelope(
            sequence=batch.sequence,
            event_count=len(batch.events),
            batch_hash=bytes.fromhex(batch.batch_hash),
            history_id=(bytes.fromhex(batch.history_id) if batch.history_id is not None else None),
        ),
    )


class FakeClient:
    def __init__(
        self,
        available: list[CommittedBatch],
        *,
        fail: bool = False,
        timeout: bool = False,
        block_offset: int = 9,
    ) -> None:
        self.available = available
        self.fail = fail
        self.timeout = timeout
        self.block_offset = block_offset
        self.closed = False

    async def fetch_batches(self, request: Any) -> BatchPageResponse:
        if self.fail:
            raise httpx.ConnectError("offline")
        if self.timeout:
            raise httpx.ReadTimeout("slow miner")
        selected = [batch for batch in self.available if batch.sequence > request.after_sequence]
        if request.through_sequence is not None:
            selected = [batch for batch in selected if batch.sequence <= request.through_sequence]
        selected = selected[: request.max_batches]
        return BatchPageResponse(
            miner_hotkey=MINER,
            batches=[
                PositionedBatch(
                    batch=batch.model_dump(mode="json"),
                    position=CommitmentPosition(
                        block=self.block_offset + batch.sequence, extrinsic_index=2
                    ),
                )
                for batch in selected
            ],
            next_sequence=selected[-1].sequence if selected else request.after_sequence,
            has_more=len(self.available) > request.after_sequence + len(selected),
        )

    async def close(self) -> None:
        self.closed = True


class FakeChain:
    def __init__(
        self,
        by_block: dict[int, list[ChainCommitment]] | None = None,
        *,
        fail_at: int | None = None,
        latest: OnChainEnvelope | None = None,
    ) -> None:
        self.by_block = by_block or {}
        self.fail_at = fail_at
        self.latest = latest
        self.read_blocks: list[int] = []

    async def commitments_in_block(self, block: int) -> list[ChainCommitment]:
        self.read_blocks.append(block)
        if block == self.fail_at:
            raise OSError(f"archive RPC unavailable at block {block}")
        return self.by_block.get(block, [])

    async def commitment_at_position(
        self, hotkey: str, position: CommitmentPosition
    ) -> ChainCommitment:
        matches = [
            item
            for item in await self.commitments_in_block(position.block)
            if item.hotkey == hotkey and item.extrinsic_index == position.extrinsic_index
        ]
        if len(matches) != 1:
            raise ProtocolError("claimed position does not contain matching commitment")
        return matches[0]

    async def latest_commitment_envelope(
        self, hotkey: str, *, block: int | None = None
    ) -> OnChainEnvelope | None:
        del hotkey, block
        return self.latest


def ingestor(
    store: ValidatorStore, client: FakeClient, chain: FakeChain | None = None
) -> ValidatorIngestor:
    return ValidatorIngestor(
        chain or FakeChain(),  # type: ignore[arg-type]
        store,
        client_factory=lambda _endpoint: client,
        page_size=1,
    )


@pytest.mark.asyncio
async def test_reconciles_pages_and_recovers_cursor_after_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    first, second = batches()
    path = tmp_path / "validator.sqlite3"
    store = ValidatorStore(path, start_block=10)
    client = FakeClient([first, second])
    chain = FakeChain(
        {10: [observation(first, 10)], 11: [observation(second, 11)]},
        latest=observation(second, 11).envelope,
    )

    with caplog.at_level(logging.INFO):
        result = await ingestor(store, client, chain).reconcile(
            MinerEndpoint(MINER, "http://miner")
        )
    restarted = ValidatorStore(path, start_block=999)

    assert result.quarantined is False
    assert result.batches_verified == 2
    assert restarted.cursor(MINER) == (2, second.batch_hash)
    assert chain.read_blocks == [10, 11]
    assert client.closed is True
    assert (
        f"miner reconciliation complete hotkey={MINER} endpoint=http://miner "
        "batches_verified=2 cursor=2"
    ) in caplog.messages


@pytest.mark.asyncio
async def test_manifest_gap_quarantines_run_after_last_valid_batch(tmp_path: Path) -> None:
    first, second = batches()
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    gap_observation = observation(second, 11)
    gap_observation = ChainCommitment(
        hotkey=MINER,
        block=11,
        extrinsic_index=2,
        timestamp=gap_observation.timestamp,
        envelope=CommitmentEnvelope(
            sequence=3,
            event_count=gap_observation.envelope.event_count,
            batch_hash=gap_observation.envelope.batch_hash,
        ),
    )
    chain = FakeChain(
        {10: [observation(first, 10)]},
        latest=gap_observation.envelope,
    )

    result = await ingestor(store, FakeClient([first]), chain).reconcile(
        MinerEndpoint(MINER, "http://miner")
    )

    assert result.quarantined is True
    assert "manifest gap" in str(result.error)
    assert store.cursor(MINER) == (1, first.batch_hash)


@pytest.mark.asyncio
async def test_latest_recommitment_cannot_hide_changed_history(tmp_path: Path) -> None:
    first, _second = batches()
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    latest = ChainCommitment(
        hotkey=MINER,
        block=11,
        extrinsic_index=3,
        timestamp=datetime(2026, 8, 5, 12, 2, tzinfo=UTC),
        envelope=CommitmentEnvelope(
            sequence=1,
            event_count=len(first.events),
            batch_hash=bytes.fromhex("ff" * 32),
        ),
    )
    chain = FakeChain(
        {10: [observation(first, 10)], 11: [latest]},
        latest=latest.envelope,
    )

    result = await ingestor(store, FakeClient([first]), chain).reconcile(
        MinerEndpoint(MINER, "http://miner")
    )

    assert result.quarantined is True
    assert result.batches_verified == 1
    assert store.cursor(MINER) == (1, first.batch_hash)


@pytest.mark.asyncio
async def test_first_history_batch_atomically_preserves_old_batches_and_starts_future(
    tmp_path: Path,
) -> None:
    first, second = batches()
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    store.persist_verified(first, observation(first, 10))
    store.persist_verified(second, observation(second, 11))
    history_id = "72" * 32
    resumed = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        history_id=history_id,
        events=(
            ClaimEvent(
                claim_id="05" * 16,
                campaign_id="campaign",
                creator_x_id="123",
                created_at="2026-08-05T12:03:00Z",
                draft_commitment="06" * 32,
            ),
        ),
    )
    resumed_observation = observation(resumed, 13)
    chain = FakeChain({13: [resumed_observation]}, latest=resumed_observation.envelope)

    result = await ingestor(store, FakeClient([resumed], block_offset=12), chain).reconcile(
        MinerEndpoint(MINER, "http://miner")
    )

    assert result.quarantined is False
    assert result.cursor == 1
    assert [record.batch.sequence for record in store.verified_batches()] == [1, 2, 1]
    assert store.verified_batches()[-1].history_start == CommitmentPosition(
        block=13, extrinsic_index=2
    )


@pytest.mark.asyncio
async def test_history_boundary_converges_validators_with_different_old_prefixes(
    tmp_path: Path,
) -> None:
    first, second = batches()
    ahead = ValidatorStore(tmp_path / "ahead.sqlite3", start_block=10)
    behind = ValidatorStore(tmp_path / "behind.sqlite3", start_block=10)
    ahead.persist_verified(first, observation(first, 10))
    ahead.persist_verified(second, observation(second, 11))
    behind.persist_verified(first, observation(first, 10))
    resumed = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        history_id="72" * 32,
        events=(first.events[0].model_copy(update={"claim_id": "05" * 16}),),
    )
    resumed_observation = observation(resumed, 13)
    chain = FakeChain({13: [resumed_observation]}, latest=resumed_observation.envelope)

    ahead_result = await ingestor(ahead, FakeClient([resumed], block_offset=12), chain).reconcile(
        MinerEndpoint(MINER, "http://miner")
    )
    behind_result = await ingestor(behind, FakeClient([resumed], block_offset=12), chain).reconcile(
        MinerEndpoint(MINER, "http://miner")
    )

    assert ahead_result.quarantined is False
    assert behind_result.quarantined is False
    assert ahead.history_cursor(MINER) == behind.history_cursor(MINER)


def test_closed_history_cannot_be_reactivated(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    legacy, _ = batches()
    first_history = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        history_id="71" * 32,
        events=(legacy.events[0],),
    )
    second_history = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        history_id="72" * 32,
        events=(legacy.events[0].model_copy(update={"claim_id": "05" * 16}),),
    )
    reused = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        history_id="71" * 32,
        events=(legacy.events[0].model_copy(update={"claim_id": "07" * 16}),),
    )
    store.persist_verified(first_history, observation(first_history, 10))
    store.persist_verified(second_history, observation(second_history, 11))

    with pytest.raises(ProtocolError, match="history ID was already used"):
        store.persist_verified(reused, observation(reused, 12))


def test_observed_unverified_history_id_cannot_be_reused(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    legacy, _ = batches()
    observed = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        history_id="71" * 32,
        events=(legacy.events[0],),
    )
    active = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        history_id="72" * 32,
        events=(legacy.events[0].model_copy(update={"claim_id": "05" * 16}),),
    )
    reused = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        history_id="71" * 32,
        events=(legacy.events[0].model_copy(update={"claim_id": "07" * 16}),),
    )
    store.persist_block(10, [observation(observed, 10)])
    store.persist_verified(active, observation(active, 11))

    with pytest.raises(ProtocolError, match="history ID was already used"):
        store.persist_verified(reused, observation(reused, 12))


@pytest.mark.asyncio
async def test_new_history_rejects_boundary_before_verified_history(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    old, _ = batches()
    store.persist_verified(old, observation(old, 20))
    resumed = CommittedBatch.create(
        miner_hotkey=MINER,
        sequence=1,
        previous_batch_hash=None,
        history_id="72" * 32,
        events=(batches()[0].events[0],),
    )
    batch_observation = observation(resumed, 13)
    chain = FakeChain({13: [batch_observation]}, latest=batch_observation.envelope)

    result = await ingestor(
        store,
        FakeClient([resumed], block_offset=12),
        chain,
    ).reconcile(MinerEndpoint(MINER, "http://miner"))

    assert result.quarantined is True
    assert result.error == "new history must begin after verified history"


@pytest.mark.asyncio
async def test_recommitted_sequence_without_exact_batch_proof_is_quarantined(
    tmp_path: Path,
) -> None:
    first, _second = batches()
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    mismatched = observation(first, 10)
    mismatched = ChainCommitment(
        hotkey=mismatched.hotkey,
        block=mismatched.block,
        extrinsic_index=mismatched.extrinsic_index,
        timestamp=mismatched.timestamp,
        envelope=CommitmentEnvelope(
            sequence=1,
            event_count=len(first.events),
            batch_hash=bytes.fromhex("ff" * 32),
        ),
    )
    chain = FakeChain(
        {10: [mismatched]},
        latest=mismatched.envelope,
    )

    result = await ingestor(store, FakeClient([first]), chain).reconcile(
        MinerEndpoint(MINER, "http://miner")
    )

    assert result.quarantined is True
    assert result.batches_verified == 0
    assert result.error == "on-chain hash does not match complete batch"
    assert store.cursor(MINER) == (0, None)


@pytest.mark.asyncio
async def test_unreachable_miner_is_availability_failure_without_cursor_change(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    first, _second = batches()
    chain = FakeChain(latest=observation(first, 10).envelope)

    result = await ingestor(store, FakeClient([], fail=True), chain).reconcile(
        MinerEndpoint(MINER, "http://miner")
    )

    assert result.available is False
    assert result.quarantined is False
    assert store.cursor(MINER) == (0, None)
    assert any(
        f"miner reconciliation unavailable hotkey={MINER} endpoint=http://miner" in message
        and "error=offline" in message
        for message in caplog.messages
    )


@pytest.mark.asyncio
async def test_slow_miner_is_isolated_as_availability_failure(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)
    first, _second = batches()
    chain = FakeChain(latest=observation(first, 10).envelope)

    result = await ingestor(store, FakeClient([], timeout=True), chain).reconcile(
        MinerEndpoint(MINER, "http://miner")
    )

    assert result.available is False
    assert result.quarantined is False
    assert "slow miner" in str(result.error)


@pytest.mark.asyncio
async def test_sparse_reconciliation_reads_only_reported_commitment_blocks(tmp_path: Path) -> None:
    first, second = batches()
    chain = FakeChain(
        {10: [observation(first, 10)], 11: [observation(second, 11)]},
        latest=observation(second, 11).envelope,
    )
    store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)

    result = await ingestor(store, FakeClient([first, second]), chain).reconcile(
        MinerEndpoint(MINER, "http://miner")
    )

    assert result.batches_verified == 2
    assert chain.read_blocks == [10, 11]


def test_verified_positions_must_increase_with_batch_sequence(tmp_path: Path) -> None:
    first, second = batches()
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    store.persist_verified(first, observation(first, 10))

    with pytest.raises(ProtocolError, match="positions must increase"):
        store.persist_verified(second, observation(second, 9))

    assert store.cursor(MINER) == (1, first.batch_hash)


@pytest.mark.asyncio
async def test_temporarily_offline_miner_heals_on_later_poll(
    tmp_path: Path,
) -> None:
    first, second = batches()
    path = tmp_path / "validator.sqlite3"
    chain = FakeChain(
        {10: [observation(first, 10)], 11: [observation(second, 11)]},
        latest=observation(second, 11).envelope,
    )
    store = ValidatorStore(path, start_block=10)

    unavailable = await ingestor(store, FakeClient([], fail=True), chain).reconcile(
        MinerEndpoint(MINER, "http://miner")
    )
    assert unavailable.available is False
    assert store.cursor(MINER) == (0, None)

    restarted = ValidatorStore(path, start_block=999)
    recovered = await ingestor(restarted, FakeClient([first, second]), chain).reconcile(
        MinerEndpoint(MINER, "http://miner")
    )

    assert recovered.available is True
    assert recovered.batches_verified == 2
    assert restarted.cursor(MINER) == (2, second.batch_hash)


@pytest.mark.asyncio
async def test_one_incompatible_miner_isolated_from_other_histories(tmp_path: Path) -> None:
    first, _second = batches()

    class MixedChain(FakeChain):
        async def latest_commitment_envelope(
            self, hotkey: str, *, block: int | None = None
        ) -> CommitmentEnvelope | None:
            del block
            return observation(first, 10).envelope if hotkey == OLD_MINER else None

    clients = {
        MINER: FakeClient([]),
        OLD_MINER: FakeClient([], fail=True),
    }
    validator = ValidatorIngestor(
        MixedChain(),  # type: ignore[arg-type]
        ValidatorStore(tmp_path / "validator.sqlite3"),
        client_factory=lambda endpoint: clients[endpoint.hotkey],
    )

    outcomes = await validator.reconcile_all(
        [
            MinerEndpoint(MINER, "http://current-miner"),
            MinerEndpoint(OLD_MINER, "http://old-miner"),
        ],
        block=20,
    )

    assert [(item.hotkey, item.available, item.error) for item in outcomes] == [
        (MINER, True, None),
        (OLD_MINER, False, "offline"),
    ]
