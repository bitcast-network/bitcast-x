"""Read-only finalized commitment replay command."""

import argparse
import asyncio
import json
from collections.abc import Sequence
from typing import Any

from bitcast_x.chain import BittensorChain
from bitcast_x.config import Settings


async def replay_commitments(
    settings: Settings,
    *,
    block: int,
    hotkey: str | None = None,
) -> dict[str, Any]:
    """Read one public finalized block without loading a wallet or miner engine."""

    chain = await BittensorChain.connect(
        settings.network,
        netuid=settings.netuid,
        mechanism_id=settings.mechanism_id,
    )
    try:
        observations = await chain.commitments_in_block(block)
    finally:
        await chain.close()
    if hotkey is not None:
        observations = [item for item in observations if item.hotkey == hotkey]
    return {
        "network": settings.network,
        "netuid": settings.netuid,
        "block": block,
        "commitments": [
            {
                "hotkey": item.hotkey,
                "block": item.block,
                "extrinsic_index": item.extrinsic_index,
                "timestamp": item.timestamp.isoformat(),
                "sequence": item.envelope.sequence,
                "event_count": item.envelope.event_count,
                "batch_hash": f"sha256-{item.envelope.batch_hash.hex()}",
                "history_id": (
                    f"sha256-{item.envelope.history_id.hex()}"
                    if item.envelope.history_id is not None
                    else None
                ),
            }
            for item in observations
        ],
    }


def main(argv: Sequence[str] | None = None) -> None:
    """Replay one public finalized block and print a machine-readable summary."""

    parser = argparse.ArgumentParser(
        prog="python -m bitcast_x.replay_commitment_block",
        description="Read finalized commitments without loading a wallet or miner engine.",
    )
    parser.add_argument("--block", required=True, type=int)
    parser.add_argument("--hotkey")
    arguments = parser.parse_args(argv)
    result = asyncio.run(
        replay_commitments(
            Settings(),
            block=arguments.block,
            hotkey=arguments.hotkey,
        )
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))  # noqa: T201


if __name__ == "__main__":
    main()
