"""Frozen hotkey-signed transport for the existing DEEBLY ingestion API."""

import gzip
import json
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
import numpy as np

BRIEF_TWEETS_PAYLOAD_TYPE = "brief_tweets"


class SigningHotkey(Protocol):
    """Narrow hotkey surface required by the legacy ingestion signature."""

    ss58_address: str

    def sign(self, data: str, /) -> bytes: ...


class SigningWallet(Protocol):
    """Narrow wallet surface required by the publisher."""

    hotkey: SigningHotkey


def json_native(value: Any) -> Any:
    """Recursively convert NumPy values while preserving the v2 wire shape."""

    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {key: json_native(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_native(item) for item in value]
    return value


class DataPublisher:
    """Publish the byte-compatible v2 envelope through one reusable HTTP client."""

    def __init__(
        self,
        wallet: SigningWallet,
        *,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.wallet = wallet
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def close(self) -> None:
        """Close an internally owned connection pool."""

        if self._owns_client:
            await self._client.aclose()

    def signed_payload(
        self,
        data: dict[str, Any],
        *,
        timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        """Create the exact signer/time/signature envelope verified by ingestion."""

        signer = self.wallet.hotkey.ss58_address
        now = timestamp or datetime.now(UTC)
        wire_time = now.astimezone(UTC).replace(tzinfo=None).isoformat()
        native = json_native(data)
        signable = native.get("payload", native.get("account_data", {}))
        message = f"{signer}:{wire_time}:{json.dumps(signable, sort_keys=True)}"
        signature = self.wallet.hotkey.sign(message)
        return {
            **native,
            "time": wire_time,
            "signature": signature.hex(),
            "signer": signer,
            "vali_hotkey": signer,
        }

    async def publish(
        self,
        *,
        endpoint: str,
        payload_type: str,
        run_id: str,
        payload: Any,
        miner_uid: int | None = None,
    ) -> bool:
        """Sign, optionally gzip, and POST; return true only for an accepted 202."""

        envelope: dict[str, Any] = {
            "payload_type": payload_type,
            "run_id": run_id,
            "payload": payload,
        }
        if miner_uid is not None:
            envelope["miner_uid"] = miner_uid
        body = json.dumps(self.signed_payload(envelope)).encode()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if len(body) > 1_000_000:
            body = gzip.compress(body, compresslevel=6)
            headers["Content-Encoding"] = "gzip"
        try:
            response = await self._client.post(endpoint, content=body, headers=headers)
            if response.status_code != httpx.codes.ACCEPTED:
                return False
            result = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        return isinstance(result, dict) and result.get("status") in {"success", "accepted"}
