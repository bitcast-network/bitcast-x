"""Offline tests for the node lifecycle pieces shared by run-miner and run-miner-api."""

import ipaddress
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from bittensor.metagraph import _axon_endpoint
from fastapi.testclient import TestClient

from bitcast_x.config import Settings
from bitcast_x.errors import ChainOperationError
from bitcast_x.miner.service import (
    advertise_endpoint,
    commit_until,
    create_protocol_app,
    is_permitted_validator,
)
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


def served(ip: str, port: int) -> str:
    """Format an endpoint exactly as the SDK metagraph reports a served axon."""

    address = ipaddress.ip_address(ip)
    endpoint = _axon_endpoint({"ip": int(address), "port": port, "ip_type": address.version})
    assert endpoint is not None
    return endpoint


class AdvertisingChain:
    def __init__(self, *, rejected: bool, on_chain: dict[str, SimpleNamespace]) -> None:
        self.rejected = rejected
        self.on_chain = on_chain
        self.metagraph_reads = 0

    async def advertise_endpoint(self, _wallet: object, *, ip: str, port: int) -> None:
        if self.rejected:
            raise ChainOperationError("endpoint advertisement failed: ServingRateLimitExceeded")

    async def metagraph(self) -> SimpleNamespace:
        self.metagraph_reads += 1
        return SimpleNamespace(by_hotkey=self.on_chain.get)


WALLET = SimpleNamespace(hotkey=SimpleNamespace(ss58_address=MINER))


@pytest.mark.parametrize(
    ("ip", "on_chain_axon"),
    [
        ("203.0.113.10", served("203.0.113.10", 8093)),
        ("2001:0db8:0:0:0:0:0:1", served("2001:db8::1", 8093)),
    ],
    ids=["ipv4", "ipv6"],
)
async def test_rejected_advertisement_is_accepted_when_chain_already_points_here(
    ip: str, on_chain_axon: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A restart inside the serving rate limit keeps the endpoint validators already read."""

    chain: Any = AdvertisingChain(
        rejected=True, on_chain={MINER: SimpleNamespace(axon=on_chain_axon)}
    )

    with caplog.at_level(logging.WARNING):
        await advertise_endpoint(chain, WALLET, ip=ip, port=8093)

    assert f"chain already advertises {on_chain_axon}" in caplog.text


@pytest.mark.parametrize(
    "on_chain",
    [
        {MINER: SimpleNamespace(axon=served("198.51.100.7", 8093))},
        {MINER: SimpleNamespace(axon=served("203.0.113.10", 9000))},
        {MINER: SimpleNamespace(axon=None)},
        {},
    ],
    ids=["other-ip", "other-port", "nothing-served", "unregistered"],
)
async def test_rejected_advertisement_fails_when_validators_cannot_reach_this_miner(
    on_chain: dict[str, SimpleNamespace],
) -> None:
    chain: Any = AdvertisingChain(rejected=True, on_chain=on_chain)

    with pytest.raises(ChainOperationError, match="ServingRateLimitExceeded"):
        await advertise_endpoint(chain, WALLET, ip="203.0.113.10", port=8093)


async def test_successful_advertisement_does_not_read_the_metagraph() -> None:
    chain = AdvertisingChain(rejected=False, on_chain={})

    await advertise_endpoint(chain, WALLET, ip="203.0.113.10", port=8093)  # type: ignore[arg-type]

    assert chain.metagraph_reads == 0
