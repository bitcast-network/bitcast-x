"""Narrow asynchronous boundary around the Bittensor v11 SDK."""

import asyncio
import logging
import secrets
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import bittensor as bt
from bittensor._generated import runtime_apis as runtime_api

from bitcast_x.errors import ChainOperationError, ProtocolError
from bitcast_x.protocol import (
    CommitmentPosition,
    OnChainEnvelope,
    decode_envelope,
)

_GLOBAL_MAX_SUBNET_COUNT = 4096
_WEIGHT_NONCE_ATTEMPTS = 3
_WEIGHT_NONCE_RETRY_MIN_MS = 250
_WEIGHT_NONCE_RETRY_JITTER_MS = 750
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChainCommitment:
    """One registered miner commitment observed at a finalized chain position."""

    hotkey: str
    block: int
    extrinsic_index: int
    timestamp: datetime
    envelope: OnChainEnvelope


@dataclass(frozen=True, slots=True)
class ResolvedCommitment:
    """One successful direct commitment reconciled with finalized storage."""

    hotkey: str
    block: int
    extrinsic_index: int
    timestamp: datetime
    payload: bytes


class BittensorChain:
    """Own all generated Bittensor reads and calls used by the application."""

    def __init__(self, client: Any, *, netuid: int, mechanism_id: int = 1) -> None:
        self._client = client
        self.netuid = netuid
        self.mechanism_id = mechanism_id

    @classmethod
    async def connect(cls, network: str, *, netuid: int, mechanism_id: int = 1) -> "BittensorChain":
        """Connect to a Bittensor network and return its async application adapter."""

        client = await bt.Subtensor(network)
        return cls(client, netuid=netuid, mechanism_id=mechanism_id)

    async def close(self) -> None:
        """Close the underlying RPC client."""

        await self._client.close()

    async def metagraph(self, *, block: int | None = None) -> Any:
        """Read the subnet metagraph, optionally pinned to a finalized block."""

        client = await self._client.at(block) if block is not None else self._client
        return await client.subnets.metagraph(netuid=self.netuid)

    async def commitment(self, hotkey_ss58: str, *, block: int | None = None) -> Any:
        """Read one hotkey's commitment, optionally at a historical block."""

        client = await self._client.at(block) if block is not None else self._client
        return await client.identity.commitment(
            netuid=self.netuid,
            hotkey_ss58=hotkey_ss58,
        )

    async def commitment_capacity(self, hotkey_ss58: str) -> tuple[int, int, int]:
        """Return ``(maximum, used, epoch)`` for the hotkey's live commitment quota."""

        maximum = int(await self._client.query(bt.storage.Commitments.MaxSpace))
        epoch = int(
            await self._client.query(
                bt.storage.SubtensorModule.SubnetEpochIndex,
                [self.netuid],
            )
        )
        usage = await self._client.query(
            bt.storage.Commitments.UsedSpaceOf,
            [self.netuid, hotkey_ss58],
        )
        used = 0
        if isinstance(usage, Mapping) and int(usage.get("last_epoch", -1)) == epoch:
            used = int(usage.get("used_space", 0))
        return maximum, used, epoch

    async def block_info(self, block: int) -> Any:
        """Return fully decoded block data for exact extrinsic recovery."""

        return await self._client.block_info(block)

    async def current_block(self) -> int:
        """Return the latest finalized block exposed by the connected client."""

        return int(await self._client.block())

    async def commitments_in_block(self, block: int) -> list[ChainCommitment]:
        """Read protocol commitments from one finalized block and verify storage bytes."""

        return [
            ChainCommitment(
                hotkey=item.hotkey,
                block=item.block,
                extrinsic_index=item.extrinsic_index,
                timestamp=item.timestamp,
                envelope=decode_envelope(item.payload),
            )
            for item in await self.resolve_commitments_in_block(block, require_registered=True)
        ]

    async def resolve_commitments_in_block(
        self,
        block: int,
        *,
        hotkey: str | None = None,
        require_registered: bool = False,
    ) -> list[ResolvedCommitment]:
        """Resolve successful direct calls against events and storage at one block."""

        info = await self.block_info(block)
        extrinsics = getattr(info, "extrinsics", None)
        timestamp = getattr(info, "timestamp", None)
        if info is None or not isinstance(extrinsics, list) or not isinstance(timestamp, datetime):
            raise ChainOperationError(f"finalized block {block} is unavailable")

        candidates: dict[str, list[tuple[int, Any]]] = defaultdict(list)
        for index, extrinsic in enumerate(extrinsics):
            candidate = _commitment_call_fields(extrinsic, self.netuid)
            if candidate is None:
                continue
            signer, fields = candidate
            if hotkey is None or signer == hotkey:
                candidates[signer].append((index, fields))
        if not candidates:
            return []

        client = await self._client.at(block)
        if require_registered:
            graph = await client.subnets.metagraph(netuid=self.netuid)
            if graph is None:
                raise ChainOperationError(f"finalized metagraph {block} is unavailable")
            candidates = {
                signer: calls
                for signer, calls in candidates.items()
                if graph.by_hotkey(signer) is not None
            }
            if not candidates:
                return []

        relevant_indexes = {index for calls in candidates.values() for index, _fields in calls}
        events = await client.query(bt.storage.System.Events)
        outcomes = _dispatch_outcomes(events, relevant_indexes)
        resolved: list[ResolvedCommitment] = []
        for signer, calls in candidates.items():
            successful: list[tuple[int, bytes]] = []
            for index, fields in calls:
                outcome = outcomes.get(index, set())
                if not outcome:
                    raise ChainOperationError(
                        f"commitment dispatch outcome is unavailable at block {block} index {index}"
                    )
                if len(outcome) != 1:
                    raise ChainOperationError(
                        "commitment dispatch outcome is contradictory "
                        f"at block {block} index {index}"
                    )
                if "success" in outcome:
                    successful.append((index, _raw_fields(fields)))
            if not successful:
                continue

            payloads = {payload for _index, payload in successful}
            if len(payloads) != 1:
                raise ChainOperationError(
                    f"successful commitments conflict for {signer} at block {block}"
                )
            stored = await client.identity.commitment(
                netuid=self.netuid,
                hotkey_ss58=signer,
            )
            if stored is None:
                raise ChainOperationError(
                    f"commitment storage missing for {signer} at block {block}"
                )
            try:
                stored_block = int(stored.block)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ChainOperationError(
                    f"commitment storage block mismatch for {signer} at block {block}"
                ) from exc
            if stored_block != block:
                raise ChainOperationError(
                    f"commitment storage block mismatch for {signer} at block {block}"
                )
            payload = payloads.pop()
            if _raw_fields(getattr(stored, "fields", None)) != payload:
                raise ChainOperationError(
                    f"commitment storage bytes mismatch for {signer} at block {block}"
                )
            resolved.append(
                ResolvedCommitment(
                    hotkey=signer,
                    block=block,
                    extrinsic_index=successful[-1][0],
                    timestamp=timestamp,
                    payload=payload,
                )
            )
        return resolved

    async def commitment_at_position(
        self,
        hotkey: str,
        position: CommitmentPosition,
    ) -> ChainCommitment:
        """Verify and return one miner commitment at its claimed finalized position."""

        resolved = await self.resolve_commitments_in_block(
            position.block,
            hotkey=hotkey,
            require_registered=True,
        )
        matches = [item for item in resolved if item.extrinsic_index == position.extrinsic_index]
        if len(matches) != 1:
            raise ProtocolError(
                "claimed position does not contain exactly one matching miner commitment"
            )
        item = matches[0]
        return ChainCommitment(
            hotkey=item.hotkey,
            block=item.block,
            extrinsic_index=item.extrinsic_index,
            timestamp=item.timestamp,
            envelope=decode_envelope(item.payload),
        )

    async def latest_commitment_envelope(
        self,
        hotkey: str,
        *,
        block: int | None = None,
    ) -> OnChainEnvelope | None:
        """Return a miner's latest protocol envelope at one finalized snapshot."""

        stored = await self.commitment(hotkey, block=block)
        if stored is None:
            return None
        return decode_envelope(_raw_fields(stored.fields))

    async def miner_qualification_inputs(
        self,
        miner_hotkey: str,
        *,
        block: int | None = None,
        include_self_stake: bool = False,
    ) -> tuple[str | None, str | None, int, int]:
        """Read owner, lock conviction, and total miner-hotkey stake in alpha rao."""

        client = await self._client.at(block) if block is not None else self._client
        coldkey = await client.neurons.hotkey_owner(miner_hotkey)
        if coldkey is None:
            return None, None, 0, 0
        coldkey_ss58 = str(coldkey)
        self_stake_rao = 0
        if include_self_stake:
            self_stake_rao = int(
                await client.query(
                    bt.storage.SubtensorModule.TotalHotkeyAlpha,
                    [miner_hotkey, self.netuid],
                )
                or 0
            )
        lock = await client.locks.coldkey_lock(coldkey, self.netuid)
        if lock is None:
            return coldkey_ss58, None, 0, self_stake_rao
        target = str(lock["hotkey"])
        state = await client.runtime(
            runtime_api.StakeInfoRuntimeApi.get_coldkey_lock,
            [coldkey_ss58, self.netuid],
        )
        conviction = state.get("conviction", 0) if isinstance(state, Mapping) else 0
        if isinstance(conviction, Mapping):
            conviction_rao = int(conviction.get("bits", 0)) >> 64
        else:
            conviction_rao = int(conviction)
        return coldkey_ss58, target, conviction_rao, self_stake_rao

    async def submit_commitment(self, wallet: Any, envelope: OnChainEnvelope) -> Any:
        """Submit one finalized raw commitment signed by the miner hotkey."""

        encoded = envelope.encode()
        call = bt.calls.Commitments.set_commitment(
            netuid=self.netuid,
            # The runtime metadata exposes one SCALE variant per Raw length
            # (Raw0..Raw128), even though the Rust type is Data::Raw.
            info={"fields": [{f"Raw{len(encoded)}": encoded}]},
        )
        result = await self._client.submit_call(
            call,
            wallet,
            signer="hotkey",
            wait_for_inclusion=True,
            wait_for_finalization=True,
        )
        self._raise_for_failure(result, "commitment submission")
        return result

    async def advertise_endpoint(
        self,
        wallet: Any,
        *,
        ip: str,
        port: int,
        protocol: int = 4,
        version: int = 1,
    ) -> Any:
        """Publish the miner's HTTP endpoint in its on-chain axon record."""

        result = await self._client.execute(
            bt.ServeAxon(
                netuid=self.netuid,
                ip=ip,
                port=port,
                protocol=protocol,
                version=version,
            ),
            wallet,
            wait_for_inclusion=True,
            wait_for_finalization=True,
        )
        self._raise_for_failure(result, "endpoint advertisement")
        return result

    async def set_weights(
        self,
        wallet: Any,
        weights: Mapping[int, float],
        *,
        version_key: int,
    ) -> Any:
        """Submit normalized mechanism weights after application-level validation."""

        for attempt in range(1, _WEIGHT_NONCE_ATTEMPTS + 1):
            result = await self._client.execute(
                bt.SetWeights(
                    netuid=self.netuid,
                    uids=list(weights),
                    weights=list(weights.values()),
                    mechid=self.mechanism_id,
                    version_key=version_key,
                ),
                wallet,
                wait_for_inclusion=True,
                wait_for_finalization=True,
            )
            if bool(getattr(result, "success", False)):
                return result
            if attempt == _WEIGHT_NONCE_ATTEMPTS or not _is_stale_nonce_failure(result):
                self._raise_for_failure(result, "weight submission")
            delay_ms = _WEIGHT_NONCE_RETRY_MIN_MS + secrets.randbelow(
                _WEIGHT_NONCE_RETRY_JITTER_MS + 1
            )
            LOGGER.warning(
                "weight submission used a stale nonce; rebuilding attempt=%s/%s delay_ms=%s",
                attempt + 1,
                _WEIGHT_NONCE_ATTEMPTS,
                delay_ms,
            )
            await asyncio.sleep(delay_ms / 1000)
        raise AssertionError("weight submission retry loop exhausted")

    async def last_weight_update(self, uid: int) -> int:
        """Read this mechanism's authoritative last-update block for a validator UID."""

        if uid < 0:
            raise ValueError("validator UID must be non-negative")
        index = self.mechanism_id * _GLOBAL_MAX_SUBNET_COUNT + self.netuid
        values = await self._client.query(bt.storage.SubtensorModule.LastUpdate, [index])
        if not isinstance(values, (list, tuple)) or uid >= len(values):
            raise ChainOperationError("mechanism weight last-update storage is unavailable")
        return int(values[uid])

    @staticmethod
    def _raise_for_failure(result: Any, operation: str) -> None:
        if bool(getattr(result, "success", False)):
            return
        error = getattr(result, "error", None)
        code = getattr(error, "code", None) or getattr(error, "name", None)
        message = getattr(result, "message", None) or str(error or "unknown chain error")
        suffix = f" [{code}]" if code else ""
        raise ChainOperationError(f"{operation} failed{suffix}: {message}")


def _is_stale_nonce_failure(result: Any) -> bool:
    """Identify only the explicit already-used nonce failure that is safe to rebuild."""

    error = getattr(result, "error", None)
    code = getattr(error, "code", None) or getattr(error, "name", None)
    code_name = str(getattr(code, "name", code)).casefold()
    message = str(getattr(result, "message", None) or error or "").casefold()
    return (
        code_name == "expired"
        and "nonce" in message
        and any(marker in message for marker in ("below", "already used", "superseded"))
    )


def _commitment_call_fields(extrinsic: Any, netuid: int) -> tuple[str, Any] | None:
    if not isinstance(extrinsic, Mapping):
        return None
    call = extrinsic.get("call")
    if not isinstance(call, Mapping):
        return None
    if call.get("call_module") != "Commitments" or call.get("call_function") != "set_commitment":
        return None
    arguments = call.get("call_args")
    if not isinstance(arguments, list):
        return None
    by_name = {
        str(argument.get("name")): argument.get("value")
        for argument in arguments
        if isinstance(argument, Mapping)
    }
    raw_netuid = by_name.get("netuid")
    if not isinstance(raw_netuid, (int, str)):
        return None
    try:
        if int(raw_netuid) != netuid:
            return None
    except ValueError:
        return None
    address = extrinsic.get("address")
    if not address:
        return None
    info = by_name.get("info")
    fields = info.get("fields") if isinstance(info, Mapping) else None
    return str(address), fields


def _dispatch_outcomes(events: Any, relevant_indexes: set[int]) -> dict[int, set[str]]:
    if not isinstance(events, (list, tuple)):
        raise ChainOperationError("finalized block events are unavailable")
    outcomes: dict[int, set[str]] = defaultdict(set)
    for record in events:
        if not isinstance(record, Mapping) or record.get("phase") != "ApplyExtrinsic":
            continue
        raw_index = record.get("extrinsic_idx")
        if not isinstance(raw_index, (int, str)):
            continue
        try:
            index = int(raw_index)
        except ValueError:
            continue
        if index not in relevant_indexes:
            continue

        event = record.get("event")
        if not isinstance(event, Mapping):
            continue
        flat_identity = (record.get("module_id"), record.get("event_id"))
        nested_identity = (event.get("module_id"), event.get("event_id"))
        if flat_identity != nested_identity:
            raise ChainOperationError(
                f"commitment event identity is contradictory at extrinsic index {index}"
            )
        module_id, event_id = flat_identity
        if module_id != "System":
            continue
        if event_id == "ExtrinsicSuccess":
            outcomes[index].add("success")
        elif event_id == "ExtrinsicFailed":
            outcomes[index].add("failure")
    return outcomes


def _raw_fields(fields: Any) -> bytes:
    if not isinstance(fields, list):
        raise ChainOperationError("commitment does not contain raw protocol bytes")
    raw = bytearray()
    for field in fields:
        if not isinstance(field, Mapping):
            continue
        for variant, value in field.items():
            if str(variant).startswith("Raw") and isinstance(value, str):
                try:
                    raw.extend(bytes.fromhex(value.removeprefix("0x")))
                except ValueError as exc:
                    raise ChainOperationError("commitment contains malformed raw bytes") from exc
    if not raw:
        raise ChainOperationError("commitment does not contain raw protocol bytes")
    return bytes(raw)
