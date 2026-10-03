"""Offline tests for the node lifecycle pieces shared by run-miner and run-miner-api."""

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bitcast_x.config import Settings
from bitcast_x.miner.service import commit_until, create_protocol_app, is_permitted_validator
from bitcast_x.transport import BATCHES_PATH

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
VALIDATOR = "5FHneW46xGXgs5mUiveU4sbTyGBzmst2jfFvCw9zThqAXhGK"


class Metagraph:
    def __init__(self, neurons: dict[str, bool]) -> None:
        self._neurons = neurons

    def by_hotkey(self, hotkey: str) -> SimpleNamespace | None:
        if hotkey not in self._neurons:
            return None
        return SimpleNamespace(validator_permit=self._neurons[hotkey])


class Chain:
    def __init__(self, metagraph: Metagraph | None) -> None:
        self._metagraph = metagraph

    async def metagraph(self) -> Metagraph | None:
        return self._metagraph


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metagraph", "permitted"),
    [
        (None, False),
        (Metagraph({}), False),
        (Metagraph({VALIDATOR: False}), False),
        (Metagraph({VALIDATOR: True}), True),
    ],
)
async def test_only_permitted_validators_are_authorized(
    metagraph: Metagraph | None, *, permitted: bool
) -> None:
    chain: Any = Chain(metagraph)

    assert await is_permitted_validator(chain, VALIDATOR) is permitted


@pytest.mark.asyncio
async def test_commit_loop_retries_after_failure_until_stopped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Engine:
        calls = 0

        async def commit_ready(self) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("chain unavailable")

    engine: Any = Engine()

    with caplog.at_level(logging.ERROR):
        await commit_until(
            engine,
            Settings(_env_file=None, batch_max_age_seconds=0.002),
            lambda: engine.calls >= 3,
        )

    assert engine.calls == 3
    assert [record.getMessage() for record in caplog.records] == [
        "queued batch commitment failed; durable state retained"
    ]


def test_protocol_app_applies_configured_bounds_and_readiness() -> None:
    async def not_ready() -> bool:
        return False

    async def authorize(_hotkey: str) -> bool:
        return True

    async def provider(*_args: object) -> Any:
        raise AssertionError("an oversized request must not reach the provider")

    client = TestClient(
        create_protocol_app(
            Settings(_env_file=None, max_request_bytes=8),
            miner_hotkey=MINER,
            provider=provider,
            authorize_validator=authorize,
            readiness=not_ready,
        )
    )

    assert client.get("/ready").status_code == 503
    assert client.post(BATCHES_PATH, content=b"x" * 9).status_code == 413
