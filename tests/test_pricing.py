"""Tests for the pricing snapshots that convert USD floors into emission weight."""

import httpx
import pytest

from bitcast_x.errors import ChainOperationError
from bitcast_x.pricing import TAO_PRICE_MAX_AGE_BLOCKS, PricingService, PricingSnapshot


class FakeChain:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.fail = False

    async def miner_emission_pricing(self, *, block: int) -> tuple[float, float]:
        self.calls.append(block)
        if self.fail:
            raise ChainOperationError("subnet emission inputs are unavailable")
        return 2893.0, 0.02


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
        block=1,
        daily_miner_alpha=2893.0,
        alpha_price_tao=0.02,
        tao_price_usd=300.0,
        tao_price_block=1,
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

    assert first == again == PricingSnapshot(10, 2893.0, 0.02, 300.0, 10)
    assert later == PricingSnapshot(11, 2893.0, 0.02, 310.0, 11)
    assert chain.calls == [10, 11]
    assert responses == []


async def test_failed_tao_quote_reuses_the_last_one_only_while_fresh() -> None:
    responses: list[httpx.Response | Exception] = [
        quote(300.0),
        httpx.Response(429),
        httpx.ConnectError("offline"),
    ]
    async with tao_price_client(responses) as client:
        service = PricingService(FakeChain(), timeout=1, client=client)
        assert await service.snapshot(1) is not None

        reused = await service.snapshot(1 + TAO_PRICE_MAX_AGE_BLOCKS)
        expired = await service.snapshot(2 + TAO_PRICE_MAX_AGE_BLOCKS)

    assert reused == PricingSnapshot(1 + TAO_PRICE_MAX_AGE_BLOCKS, 2893.0, 0.02, 300.0, 1)
    assert expired is None


async def test_recorded_tao_quote_prices_a_restart_during_an_outage() -> None:
    responses: list[httpx.Response | Exception] = [
        httpx.ConnectError("offline"),
        httpx.ConnectError("offline"),
        quote(310.0),
    ]
    async with tao_price_client(responses) as client:
        service = PricingService(FakeChain(), timeout=1, client=client, last_tao_quote=(300.0, 5))

        reused = await service.snapshot(5 + TAO_PRICE_MAX_AGE_BLOCKS)
        expired = await service.snapshot(6 + TAO_PRICE_MAX_AGE_BLOCKS)
        fresh = await service.snapshot(7 + TAO_PRICE_MAX_AGE_BLOCKS)

    assert reused is not None and (reused.tao_price_usd, reused.tao_price_block) == (300.0, 5)
    assert expired is None
    assert fresh is not None and fresh.tao_price_usd == 310.0


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
        assert await service.snapshot(2) == PricingSnapshot(2, 2893.0, 0.02, 300.0, 2)
