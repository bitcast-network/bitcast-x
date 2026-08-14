"""Signed client for miner-owned campaign and submission results."""

from datetime import UTC, datetime
from typing import Any, Protocol

import httpx


def miner_auth_message(method: str, path: str, timestamp: str) -> bytes:
    """Build the stable hotkey-authentication message for miner result reads."""

    return f"bitcast-x-miner-results-v1\n{method.upper()}\n{path}\n{timestamp}".encode()


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

    async def submission(self, submission_id: str) -> dict[str, Any]:
        """Read the latest validator result for one miner-owned submission."""

        path = f"/api/v2/miners/x/submissions/{submission_id}"
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        signature = self._signer.sign(miner_auth_message("GET", path, timestamp)).hex()
        response = await self._client.get(
            path,
            headers={
                "X-Bitcast-Hotkey": self._signer.ss58_address,
                "X-Bitcast-Timestamp": timestamp,
                "X-Bitcast-Signature": signature,
            },
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    async def campaigns(self) -> list[dict[str, Any]]:
        """Return miner-facing campaigns enriched with public headline stats."""

        response = await self._client.get("/api/v2/public/x/campaigns")
        response.raise_for_status()
        payload = response.json()
        return [
            {**item["campaign"], "stats": item["stats"]} for item in payload.get("campaigns", [])
        ]
