"""Pricing snapshots that convert USD floors into mechanism-1 emission weight."""

import json
import logging
import math
from dataclasses import dataclass
from typing import Protocol

import httpx
from bittensor.result import BittensorError

from bitcast_x.errors import ChainOperationError, ResponseTooLargeError
from bitcast_x.http import read_bounded

LOGGER = logging.getLogger(__name__)

TAO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
TAO_PRICE_MAX_RESPONSE_BYTES = 65_536
# A failed TAO/USD request reuses the last quote fetched within this many blocks
# (a day). A day-old quote misvalues emission far less than the unpriced fallback,
# which splits all weight by floors alone.
TAO_PRICE_MAX_AGE_BLOCKS = 7_200


class EmissionPricingChain(Protocol):
    """Chain reads a pricing snapshot needs."""

    async def miner_emission_pricing(self, *, block: int) -> tuple[float, float]: ...


@dataclass(frozen=True, slots=True)
class PricingSnapshot:
    """The prices and emission fixed for one weight cycle."""

    block: int
    daily_miner_alpha: float
    alpha_price_tao: float
    tao_price_usd: float
    # The block whose cycle fetched the TAO/USD quote; earlier than ``block``
    # when a failed request reused it.
    tao_price_block: int

    @property
    def alpha_price_usd(self) -> float:
        return self.alpha_price_tao * self.tao_price_usd

    @property
    def emission_value_usd(self) -> float:
        """USD value of one day of this mechanism's miner emission."""

        return self.daily_miner_alpha * self.alpha_price_usd


class PricingService:
    """Read alpha's on-chain price and emission, and TAO/USD from CoinGecko.

    Alpha's TAO price and the emission come from finalized chain state, so they
    agree across validators; only the TAO/USD quote is external. A snapshot is
    fixed per block, so a block's weight vector never changes on a re-run.
    ``last_tao_quote`` is the newest recorded quote and the block it was
    fetched at, so a restart during a CoinGecko outage can still price weights.
    """

    def __init__(
        self,
        chain: EmissionPricingChain,
        *,
        timeout: float,
        client: httpx.AsyncClient | None = None,
        last_tao_quote: tuple[float, int] | None = None,
    ) -> None:
        self._chain = chain
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, trust_env=False)
        self._tao_quote = last_tao_quote
        self._by_block: tuple[int, PricingSnapshot | None] | None = None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def snapshot(self, block: int) -> PricingSnapshot | None:
        """Return the block's pricing snapshot, or None when it is unavailable."""

        if self._by_block is not None and self._by_block[0] == block:
            return self._by_block[1]
        snapshot: PricingSnapshot | None
        try:
            daily_miner_alpha, alpha_price_tao = await self._chain.miner_emission_pricing(
                block=block
            )
            tao_price_usd, tao_price_block = await self._tao_price_usd(block)
            snapshot = PricingSnapshot(
                block=block,
                daily_miner_alpha=daily_miner_alpha,
                alpha_price_tao=alpha_price_tao,
                tao_price_usd=tao_price_usd,
                tao_price_block=tao_price_block,
            )
        except (BittensorError, ChainOperationError, OSError, ValueError) as exc:
            LOGGER.warning("pricing snapshot unavailable block=%s error=%s", block, exc)
            snapshot = None
        self._by_block = (block, snapshot)
        return snapshot

    async def _tao_price_usd(self, block: int) -> tuple[float, int]:
        """Return a TAO/USD quote and the block it was fetched at."""

        try:
            price = await self._fetch_tao_price_usd()
        except (httpx.HTTPError, OSError, ResponseTooLargeError, ValueError) as exc:
            if self._tao_quote is not None and (
                0 <= block - self._tao_quote[1] <= TAO_PRICE_MAX_AGE_BLOCKS
            ):
                LOGGER.warning(
                    "TAO price request failed; reusing the quote from block %s error=%s",
                    self._tao_quote[1],
                    exc,
                )
                return self._tao_quote
            raise ValueError(f"TAO price is unavailable: {exc}") from exc
        self._tao_quote = (price, block)
        return self._tao_quote

    async def _fetch_tao_price_usd(self) -> float:
        async with self._client.stream(
            "GET", TAO_PRICE_URL, params={"ids": "bittensor", "vs_currencies": "usd"}
        ) as response:
            response.raise_for_status()
            body = await read_bounded(response, TAO_PRICE_MAX_RESPONSE_BYTES, source="TAO price")
        try:
            price = float(json.loads(body)["bittensor"]["usd"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("TAO price response is invalid") from exc
        if not math.isfinite(price) or price <= 0:
            raise ValueError("TAO price is not a positive finite number")
        return price
