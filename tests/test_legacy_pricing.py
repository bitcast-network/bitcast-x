"""Legacy cycle pricing parity tests."""

import httpx

from bitcast_x.chain import BittensorChain
from bitcast_x.legacy import LegacyPricingService


class Client:
    async def at(self, block: int) -> "Client":
        return self

    async def runtime(self, method: object, args: list[int]) -> dict[str, int]:
        return {"alpha_out_emission": 1_000_000_000}

    class Subnets:
        async def mechanism_emission_split(self, **kwargs: object) -> list[int]:
            return [39_321, 26_214]

    subnets = Subnets()


async def test_v2_daily_emission_formula_and_price_are_pinned() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bitcast": {"usd": 2.0}})

    chain = BittensorChain(Client(), netuid=93, mechanism_id=1)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = LegacyPricingService(chain, client=client)
    snapshot = await service.fetch(block=10)
    assert snapshot.daily_miner_alpha == 1180.8
    assert snapshot.daily_miner_usd == 2361.6
    await client.aclose()
