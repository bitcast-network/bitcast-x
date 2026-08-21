"""Hotkey-signed client for the central Bitcast miner read API."""

from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote, urlencode

import httpx

QueryParams = list[tuple[str, str]]


def canonical_query(params: QueryParams | None) -> str:
    """Return sorted RFC 3986 query parameters, preserving repeated keys."""

    return urlencode(sorted(params or []), doseq=True, quote_via=quote, safe="-._~")


def miner_auth_message(method: str, path: str, query: str, timestamp: str) -> bytes:
    """Build the stable central miner API authentication message."""

    target = f"{path}?{query}" if query else path
    return f"bitcast-x-miner-api-v1\n{method.upper()}\n{target}\n{timestamp}".encode()


class HotkeySigner(Protocol):
    """Minimal signing surface required from a Bittensor hotkey."""

    ss58_address: str

    def sign(self, data: bytes) -> bytes: ...


class MinerResultsClient:
    """Call central read endpoints without exposing the miner hotkey secret."""

    def __init__(self, base_url: str, signer: HotkeySigner, *, timeout: float = 15.0) -> None:
        self._signer = signer
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""

        await self._client.aclose()

    async def _get(
        self,
        path: str,
        *,
        params: QueryParams | None = None,
    ) -> dict[str, Any]:
        query = canonical_query(params)
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        signature = self._signer.sign(miner_auth_message("GET", path, query, timestamp)).hex()
        response = await self._client.get(
            path,
            params=tuple(params or ()),
            headers={
                "X-Bitcast-Hotkey": self._signer.ss58_address,
                "X-Bitcast-Timestamp": timestamp,
                "X-Bitcast-Signature": signature,
            },
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    @staticmethod
    def _filters(ecosystem_ids: tuple[str, ...]) -> QueryParams:
        return [("ecosystem_id", ecosystem_id) for ecosystem_id in ecosystem_ids]

    async def ecosystems(self) -> list[dict[str, Any]]:
        body = await self._get("/api/v2/miners/x/ecosystems")
        return list(body.get("items", []))

    async def campaigns(
        self,
        ecosystem_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        body = await self._get(
            "/api/v2/miners/x/campaigns",
            params=self._filters(ecosystem_ids),
        )
        return list(body.get("items", []))

    async def leaderboard(
        self,
        ecosystem_ids: tuple[str, ...] = (),
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        return await self._get(
            "/api/v2/miners/x/leaderboard",
            params=[*self._filters(ecosystem_ids), ("limit", str(limit))],
        )

    async def campaign(self, campaign_id: str) -> dict[str, Any]:
        return await self._get(f"/api/v2/miners/x/campaigns/{campaign_id}")

    async def eligibility(self, campaign_id: str, creator_x_id: str) -> dict[str, Any]:
        return await self._get(
            f"/api/v2/miners/x/campaigns/{campaign_id}/eligibility/{creator_x_id}"
        )

    async def campaign_tweets(
        self,
        campaign_id: str,
        ecosystem_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return await self._get(
            f"/api/v2/miners/x/campaigns/{campaign_id}/tweets",
            params=self._filters(ecosystem_ids),
        )

    async def submissions(
        self,
        *,
        campaign_id: str | None = None,
        tweet_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: QueryParams = []
        if campaign_id is not None:
            params.append(("campaign_id", campaign_id))
        if tweet_id is not None:
            params.append(("tweet_id", tweet_id))
        body = await self._get("/api/v2/miners/x/submissions", params=params)
        return list(body.get("items", []))

    async def submission(self, submission_id: str) -> dict[str, Any]:
        """Read the latest validator result for one miner-owned submission."""

        return await self._get(f"/api/v2/miners/x/submissions/{submission_id}")
