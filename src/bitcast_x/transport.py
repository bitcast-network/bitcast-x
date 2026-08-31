"""Bittensor v11 signed HTTP transport shared by miners and validators."""

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

import bittensor as bt
import httpx
from bittensor.http_auth import AuthError, Caller, InMemoryNonceStore
from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bitcast_x.errors import AuthenticationError, ResponseTooLargeError
from bitcast_x.protocol import CommitmentPosition, ResumeEnvelope

BATCHES_PATH = "/v3/batches"
LEGACY_BATCHES_PATH = "/v2/batches"
ReadinessProvider = Callable[[], Awaitable[bool]]


class HotkeyRateLimiter:
    """Bound signed requests per validator hotkey over a monotonic window."""

    def __init__(self, *, limit: int, window_seconds: float = 60.0) -> None:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("rate-limit settings must be positive")
        self._limit = limit
        self._window_seconds = window_seconds
        self._requests: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, hotkey: str) -> bool:
        """Consume one request slot if the caller remains inside its window limit."""

        now = time.monotonic()
        cutoff = now - self._window_seconds
        async with self._lock:
            for caller, history in tuple(self._requests.items()):
                while history and history[0] <= cutoff:
                    history.popleft()
                if not history:
                    del self._requests[caller]
            requests = self._requests[hotkey]
            if len(requests) >= self._limit:
                return False
            requests.append(now)
            return True


class BatchPageRequest(BaseModel):
    """Cursor request sent by a validator to one miner."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: int = Field(default=3, frozen=True)
    after_sequence: int = Field(ge=0)
    through_sequence: int | None = Field(default=None, ge=1)
    max_batches: int = Field(default=50, ge=1, le=50)


class PositionedBatch(BaseModel):
    """One complete batch and its miner-reported finalized chain coordinates."""

    model_config = ConfigDict(extra="forbid")

    batch: dict[str, Any]
    position: CommitmentPosition


class ResumeAnchor(BaseModel):
    """Exact finalized proof of the miner-signed future-only boundary."""

    model_config = ConfigDict(extra="forbid")

    history_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    position: CommitmentPosition

    def envelope(self) -> ResumeEnvelope:
        return ResumeEnvelope(history_id=bytes.fromhex(self.history_id))


class BatchPageResponse(BaseModel):
    """Positioned complete batches returned by one miner in sequence order."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: int = Field(default=3, frozen=True)
    miner_hotkey: str
    resume: ResumeAnchor | None = None
    batches: list[PositionedBatch]
    next_sequence: int = Field(ge=0)
    has_more: bool


class LegacyBatchPageRequest(BaseModel):
    """Read-only v2 overlap request retained for existing validators."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: int = Field(default=2, frozen=True)
    after_sequence: int = Field(ge=0)
    max_batches: int = Field(default=50, ge=1, le=50)


class LegacyBatchPageResponse(BaseModel):
    """Position-free v2 overlap response retained during protocol migration."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: int = Field(default=2, frozen=True)
    miner_hotkey: str
    batches: list[dict[str, Any]]
    next_sequence: int = Field(ge=0)
    has_more: bool


class BatchProvider(Protocol):
    """Application callback used by the miner HTTP boundary."""

    async def __call__(
        self, request: BatchPageRequest, caller_hotkey: str
    ) -> BatchPageResponse: ...


class RequestAuthenticator:
    """Verify receiver-bound requests and reject replayed nonces."""

    def __init__(
        self,
        self_hotkey_ss58: str,
        *,
        max_age: float = 10.0,
        allowed_skew: float = 2.0,
    ) -> None:
        self._self_hotkey_ss58 = self_hotkey_ss58
        self._max_age = max_age
        self._allowed_skew = allowed_skew
        self._nonce_store = InMemoryNonceStore(retention=max(60.0, max_age + allowed_skew))

    def verify(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        method: str,
        path: str,
    ) -> Caller:
        """Return the authenticated caller or raise a domain authentication error."""

        try:
            return bt.http_auth.verify(
                headers,
                body,
                method=method,
                path=path,
                self_hotkey_ss58=self._self_hotkey_ss58,
                max_age=self._max_age,
                allowed_skew=self._allowed_skew,
                require_receiver=True,
                nonce_store=self._nonce_store,
            )
        except AuthError as exc:
            raise AuthenticationError(str(exc)) from exc


def create_miner_app(
    *,
    miner_hotkey: str,
    provider: BatchProvider,
    authorize_validator: Callable[[str], Awaitable[bool]],
    max_request_bytes: int = 16_384,
    auth_max_age: float = 10.0,
    auth_allowed_skew: float = 2.0,
    requests_per_minute: int = 120,
    readiness: ReadinessProvider | None = None,
) -> FastAPI:
    """Create the bounded signed-HTTP miner application."""

    app = FastAPI(title="Bitcast X miner", docs_url=None, redoc_url=None)
    authenticator = RequestAuthenticator(
        miner_hotkey,
        max_age=auth_max_age,
        allowed_skew=auth_allowed_skew,
    )
    rate_limiter = HotkeyRateLimiter(limit=requests_per_minute)

    @app.get("/health")
    async def health() -> dict[str, str]:
        from bitcast_x import __version__

        return {"status": "ok", "protocol_version": "3", "version": __version__}

    @app.get("/ready")
    async def ready(response: Response) -> dict[str, str]:
        is_ready = True if readiness is None else await readiness()
        if not is_ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "ready" if is_ready else "starting"}

    async def authenticated_body(request: Request) -> tuple[bytes, str]:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid content-length") from exc
            if declared_length < 0:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid content-length")
            if declared_length > max_request_bytes:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "request is too large")
        body = await request.body()
        if len(body) > max_request_bytes:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "request is too large")
        try:
            caller = authenticator.verify(
                request.headers,
                body,
                method=request.method,
                path=request.url.path,
            )
        except AuthenticationError as exc:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "invalid Bittensor authentication"
            ) from exc
        if not await authorize_validator(caller.hotkey_ss58):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "validator is not authorized")
        if not await rate_limiter.allow(caller.hotkey_ss58):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "validator rate limit exceeded")
        return body, caller.hotkey_ss58

    @app.post(BATCHES_PATH, response_model=BatchPageResponse)
    async def batches(request: Request) -> Response | BatchPageResponse:
        body, caller_hotkey = await authenticated_body(request)
        try:
            page_request = BatchPageRequest.model_validate_json(body)
        except ValidationError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid page request"
            ) from exc
        return await provider(page_request, caller_hotkey)

    @app.post(LEGACY_BATCHES_PATH, response_model=LegacyBatchPageResponse)
    async def legacy_batches(request: Request) -> Response | LegacyBatchPageResponse:
        body, caller_hotkey = await authenticated_body(request)
        try:
            legacy_request = LegacyBatchPageRequest.model_validate_json(body)
        except ValidationError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid page request"
            ) from exc
        response = await provider(
            BatchPageRequest(
                after_sequence=legacy_request.after_sequence,
                max_batches=legacy_request.max_batches,
            ),
            caller_hotkey,
        )
        return LegacyBatchPageResponse(
            miner_hotkey=response.miner_hotkey,
            batches=[item.batch for item in response.batches],
            next_sequence=response.next_sequence,
            has_more=response.has_more,
        )

    return app


class SignedMinerClient:
    """Bounded validator client for one metagraph-discovered miner endpoint."""

    def __init__(
        self,
        wallet: Any,
        *,
        miner_hotkey: str,
        base_url: str,
        timeout: float = 15.0,
        max_response_bytes: int = 2_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._wallet = wallet
        self._miner_hotkey = miner_hotkey
        self._max_response_bytes = max_response_bytes
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""

        await self._client.aclose()

    async def fetch_batches(self, request: BatchPageRequest) -> BatchPageResponse:
        """Fetch and validate one signed page from the miner."""

        body = request.model_dump_json().encode()
        headers = bt.http_auth.sign(
            self._wallet,
            method="POST",
            path=BATCHES_PATH,
            body=body,
            receiver_ss58=self._miner_hotkey,
        )
        headers["content-type"] = "application/json"
        async with self._client.stream(
            "POST", BATCHES_PATH, headers=headers, content=body
        ) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self._max_response_bytes:
                    raise ResponseTooLargeError("miner response exceeds configured byte limit")
                chunks.append(chunk)
        return BatchPageResponse.model_validate_json(b"".join(chunks))
