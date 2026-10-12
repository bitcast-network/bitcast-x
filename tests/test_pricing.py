"""Tests for the pricing snapshots that convert USD floors into emission weight."""

import httpx
import pytest

from bitcast_x.errors import ChainOperationError
from bitcast_x.pricing import TAO_PRICE_MAX_AGE_SECONDS, PricingService, PricingSnapshot


class FakeChain:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.fail = False

    async def miner_emission_pricing(self, *, block: int) -> tuple[float, float]:
        self.calls.append(block)
        if self.fail:
            raise ChainOperationError("subnet emission inputs are unavailable")
        return 2893.0, 0.02


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def tao_price_client(responses: list[httpx.Response | Exception]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ids"] == "bittensor"
        assert request.url.params["vs_currencies"] == "usd"
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def quote(usd: object) -> httpx.Response:
    return httpx.Response(200, json={"bittensor": {"usd": usd}})


def test_snapshot_values_one_day_of_miner_emission_in_usd() -> None:
    snapshot = PricingSnapshot(
        block=1, daily_miner_alpha=2893.0, alpha_price_tao=0.02, tao_price_usd=300.0
    )

    assert snapshot.alpha_price_usd == pytest.approx(6.0)
    assert snapshot.emission_value_usd == pytest.approx(17_358.0)


async def test_snapshot_is_fixed_per_block() -> None:
    chain = FakeChain()
    responses: list[httpx.Response | Exception] = [quote(300.0), quote(310.0)]
    async with tao_price_client(responses) as client:
        service = PricingService(chain, timeout=1, client=client)

        first = await service.snapshot(10)
        again = await service.snapshot(10)
        later = await service.snapshot(11)

    assert first == again == PricingSnapshot(10, 2893.0, 0.02, 300.0)
    assert later == PricingSnapshot(11, 2893.0, 0.02, 310.0)
    assert chain.calls == [10, 11]
    assert responses == []


async def test_failed_tao_quote_reuses_the_last_one_only_while_fresh() -> None:
    clock = Clock()
    responses: list[httpx.Response | Exception] = [
        quote(300.0),
        httpx.Response(429),
        httpx.ConnectError("offline"),
    ]
    async with tao_price_client(responses) as client:
        service = PricingService(FakeChain(), timeout=1, client=client, clock=clock)
        assert await service.snapshot(1) is not None

        clock.now = TAO_PRICE_MAX_AGE_SECONDS
        reused = await service.snapshot(2)
        clock.now = TAO_PRICE_MAX_AGE_SECONDS + 1
        expired = await service.snapshot(3)

    assert reused is not None and reused.tao_price_usd == 300.0
    assert expired is None


@pytest.mark.parametrize(
    "response",
    [
        quote(0),
        quote(-1.0),
        quote("not a number"),
        httpx.Response(200, json={"bitcast": {"usd": 5.0}}),
        httpx.Response(200, content=b"x" * 70_000),
    ],
)
async def test_unusable_tao_quote_leaves_the_block_unpriced(response: httpx.Response) -> None:
    async with tao_price_client([response]) as client:
        service = PricingService(FakeChain(), timeout=1, client=client)

        assert await service.snapshot(1) is None


async def test_chain_failure_leaves_the_block_unpriced_until_the_next_block() -> None:
    chain = FakeChain()
    chain.fail = True
    responses: list[httpx.Response | Exception] = [quote(300.0)]
    async with tao_price_client(responses) as client:
        service = PricingService(chain, timeout=1, client=client)

        assert await service.snapshot(1) is None
        chain.fail = False
        # A block's answer never changes, so a retry at the same block stays unpriced.
        assert await service.snapshot(1) is None
        assert await service.snapshot(2) == PricingSnapshot(2, 2893.0, 0.02, 300.0)
