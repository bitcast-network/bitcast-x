"""Cycle-pinned legacy alpha/USD pricing and emission conversion."""

from dataclasses import dataclass

import httpx

from bitcast_x.chain import BittensorChain
from bitcast_x.errors import ChainOperationError

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"


@dataclass(frozen=True, slots=True)
class LegacyPricingSnapshot:
    """The one price/emission pair used for an entire legacy cycle."""

    alpha_price_usd: float
    daily_miner_alpha: float

    @property
    def daily_miner_usd(self) -> float:
        return self.alpha_price_usd * self.daily_miner_alpha


class LegacyPricingService:
    """Fetch the same external price and chain emission inputs as v2."""

    def __init__(
        self,
        chain: BittensorChain,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._chain = chain
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=10, trust_env=False)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, *, block: int) -> LegacyPricingSnapshot:
        """Pin price and emissions once, failing closed on missing evidence."""

        response = await self._client.get(
            COINGECKO_URL,
            params={"ids": "bitcast", "vs_currencies": "usd"},
        )
        response.raise_for_status()
        payload = response.json()
        try:
            price = float(payload["bitcast"]["usd"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ChainOperationError("legacy alpha price is invalid") from exc
        emissions = await self._chain.legacy_daily_miner_alpha(block=block)
        if price <= 0 or emissions <= 0:
            raise ChainOperationError("legacy pricing snapshot is non-positive")
        return LegacyPricingSnapshot(price, emissions)
