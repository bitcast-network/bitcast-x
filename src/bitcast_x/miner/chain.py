"""Concrete Bittensor v11 commitment adapter for the miner engine."""

from collections.abc import Mapping
from typing import Any

from bitcast_x.chain import BittensorChain
from bitcast_x.errors import ChainOperationError
from bitcast_x.miner.engine import CapacityBudget, FinalizedCommitment
from bitcast_x.protocol import CommitmentPosition, OnChainEnvelope

# Subtensor's Commitments pallet applies this floor to the sum of Data field lengths.
# It is pallet logic rather than a metadata constant; MaxSpace and UsedSpaceOf remain
# live runtime reads. Keep the calculation payload-aware so a future larger envelope
# is paced correctly without turning MaxSpace into a fixed calls-per-epoch assumption.
_PALLET_MINIMUM_RATE_LIMIT_SPACE = 100


class BittensorCommitmentSubmitter:
    """Commit and recover a miner's exact protocol envelope on Bittensor v11."""

    def __init__(self, chain: BittensorChain, wallet: Any) -> None:
        self._chain = chain
        self._wallet = wallet
        self.hotkey = str(wallet.hotkey.ss58_address)

    async def capacity(self, envelope: OnChainEnvelope) -> CapacityBudget:
        """Read the epoch-aware space budget enforced by the commitments pallet."""

        maximum, used, _epoch = await self._chain.commitment_capacity(self.hotkey)
        return CapacityBudget(
            remaining_space=max(0, maximum - used),
            next_call_charge=max(_PALLET_MINIMUM_RATE_LIMIT_SPACE, len(envelope.encode())),
        )

    async def latest(self) -> FinalizedCommitment | None:
        """Recover the latest stored envelope and its exact finalized ordering position."""

        commitment = await self._chain.commitment(self.hotkey)
        if commitment is None:
            return None
        block = int(commitment.block)
        stored = _raw_commitment_bytes(commitment.fields)
        extrinsic_index = await self._find_extrinsic_index(block, stored)
        return FinalizedCommitment(
            position=CommitmentPosition(block=block, extrinsic_index=extrinsic_index),
            stored_envelope=stored,
        )

    async def submit(self, envelope: OnChainEnvelope) -> FinalizedCommitment:
        """Submit, then independently read and verify the finalized on-chain bytes."""

        result = await self._chain.submit_commitment(self._wallet, envelope)
        extrinsic_id = getattr(result, "extrinsic_id", None)
        if not isinstance(extrinsic_id, str):
            raise ChainOperationError("finalized commitment did not return an extrinsic id")
        try:
            block_text, index_text = extrinsic_id.rsplit("-", 1)
            position = CommitmentPosition(
                block=int(block_text),
                extrinsic_index=int(index_text),
            )
        except (TypeError, ValueError) as exc:
            raise ChainOperationError(
                f"invalid finalized commitment extrinsic id: {extrinsic_id}"
            ) from exc
        stored = await self._chain.commitment(self.hotkey, block=position.block)
        if stored is None:
            raise ChainOperationError("finalized commitment is absent from chain storage")
        return FinalizedCommitment(
            position=position,
            stored_envelope=_raw_commitment_bytes(stored.fields),
        )

    async def _find_extrinsic_index(self, block: int, stored: bytes) -> int:
        matches = await self._chain.resolve_commitments_in_block(block, hotkey=self.hotkey)
        matches = [item for item in matches if item.payload == stored]
        if len(matches) != 1:
            raise ChainOperationError(
                f"expected one matching commitment extrinsic at block {block}, found {len(matches)}"
            )
        return matches[0].extrinsic_index


def _raw_commitment_bytes(fields: list[Any]) -> bytes:
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
