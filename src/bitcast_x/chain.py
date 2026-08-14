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
from bitcast_x.protocol import CommitmentEnvelope, CommitmentPosition

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
    envelope: CommitmentEnvelope


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

        info = await self.block_info(block)
        if info is None:
            raise ChainOperationError(f"finalized block {block} is unavailable")
        candidates: dict[str, list[int]] = defaultdict(list)
        for index, extrinsic in enumerate(info.extrinsics):
            hotkey = _commitment_signer(extrinsic, self.netuid)
            if hotkey is not None:
                candidates[hotkey].append(index)
        ambiguous = sorted(hotkey for hotkey, indexes in candidates.items() if len(indexes) > 1)
        if ambiguous:
            raise ChainOperationError(
                f"multiple same-miner commitments in block {block}: {', '.join(ambiguous)}"
            )
        if not candidates:
            return []
        graph = await self.metagraph(block=block)
        observations: list[ChainCommitment] = []
        for hotkey, indexes in candidates.items():
            if graph is None or graph.by_hotkey(hotkey) is None:
                continue
            stored = await self.commitment(hotkey, block=block)
            if stored is None:
                raise ChainOperationError(
                    f"commitment storage missing for {hotkey} at block {block}"
                )
            raw = _raw_fields(stored.fields)
            observations.append(
                ChainCommitment(
                    hotkey=hotkey,
                    block=block,
                    extrinsic_index=indexes[0],
                    timestamp=info.timestamp,
                    envelope=CommitmentEnvelope.decode(raw),
                )
            )
        return observations

    async def commitment_at_position(
        self,
        hotkey: str,
        position: CommitmentPosition,
    ) -> ChainCommitment:
        """Verify and return one miner commitment at its claimed finalized position."""

        observations = await self.commitments_in_block(position.block)
        matches = [
            item
            for item in observations
            if item.hotkey == hotkey and item.extrinsic_index == position.extrinsic_index
        ]
        if len(matches) != 1:
            raise ProtocolError(
                "claimed position does not contain exactly one matching miner commitment"
            )
        return matches[0]

    async def latest_commitment_envelope(
        self,
        hotkey: str,
        *,
        block: int | None = None,
    ) -> CommitmentEnvelope | None:
        """Return a miner's latest protocol envelope at one finalized snapshot."""

        stored = await self.commitment(hotkey, block=block)
        if stored is None:
            return None
        return CommitmentEnvelope.decode(_raw_fields(stored.fields))

    async def miner_qualification_inputs(
        self,
        miner_hotkey: str,
        *,
        block: int | None = None,
    ) -> tuple[str | None, str | None, int]:
        """Read owner coldkey, lock target, and rolled conviction in alpha rao."""

        client = await self._client.at(block) if block is not None else self._client
        coldkey = await client.neurons.hotkey_owner(miner_hotkey)
        if coldkey is None:
            return None, None, 0
        lock = await client.locks.coldkey_lock(coldkey, self.netuid)
        if lock is None:
            return str(coldkey), None, 0
        target = str(lock["hotkey"])
        state = await client.runtime(
            runtime_api.StakeInfoRuntimeApi.get_coldkey_lock,
            [str(coldkey), self.netuid],
        )
        conviction = state.get("conviction", 0) if isinstance(state, Mapping) else 0
        if isinstance(conviction, Mapping):
            conviction_rao = int(conviction.get("bits", 0)) >> 64
        else:
            conviction_rao = int(conviction)
        return str(coldkey), target, conviction_rao

    async def legacy_daily_miner_alpha(self, *, block: int | None = None) -> float:
        """Return v2's mechanism-pinned daily miner emission amount in alpha."""

        client = await self._client.at(block) if block is not None else self._client
        dynamic = await client.runtime(
            runtime_api.SubnetInfoRuntimeApi.get_dynamic_info, [self.netuid]
        )
        split = await client.subnets.mechanism_emission_split(netuid=self.netuid, block=block)
        if not isinstance(dynamic, Mapping) or not isinstance(split, list):
            raise ChainOperationError("legacy emission inputs are unavailable")
        alpha_out_rao = int(dynamic.get("alpha_out_emission", 0))
        split_total = sum(int(item) for item in split)
        if alpha_out_rao <= 0 or split_total <= 0 or self.mechanism_id >= len(split):
            raise ChainOperationError("legacy emission inputs are invalid")
        ratio = int(split[self.mechanism_id]) / split_total
        return 7200.0 * (alpha_out_rao / 1_000_000_000) * 0.41 * ratio

    async def submit_commitment(self, wallet: Any, envelope: CommitmentEnvelope) -> Any:
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


def _commitment_signer(extrinsic: Any, netuid: int) -> str | None:
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
    if not isinstance(raw_netuid, (int, str)) or int(raw_netuid) != netuid:
        return None
    address = extrinsic.get("address")
    return str(address) if address else None


def _raw_fields(fields: list[Any]) -> bytes:
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
