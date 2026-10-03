"""End-to-end tests for Bittensor v11 signed HTTP authentication."""

import asyncio
from pathlib import Path
from typing import Any

import bittensor as bt
import httpx
import pytest
from fastapi import FastAPI

from bitcast_x import __version__
from bitcast_x.errors import ResponseTooLargeError
from bitcast_x.protocol import CommitmentPosition
from bitcast_x.transport import (
    BATCHES_PATH,
    LEGACY_BATCHES_PATH,
    BatchPageRequest,
    BatchPageResponse,
    BatchProvider,
    LegacyBatchPageRequest,
    PositionedBatch,
    SignedMinerClient,
    create_miner_app,
)

MINER_URL = "http://miner.test"


def create_wallet(path: Path, name: str) -> bt.Wallet:
    """Create an unencrypted temporary wallet for authentication tests."""

    wallet = bt.Wallet(name=name, hotkey="default", path=str(path))
    wallet.create_new_coldkey(use_password=False, suppress=True)
    wallet.create_new_hotkey(use_password=False, suppress=True)
    return wallet


async def allow_all(_hotkey: str) -> bool:
    return True


async def refuse(request: BatchPageRequest, caller_hotkey: str) -> BatchPageResponse:
    raise AssertionError("provider must not run")


def empty_page(miner: bt.Wallet, served: list[str] | None = None) -> BatchProvider:
    """Serve empty pages that echo the cursor, optionally recording each caller."""

    async def provide(request: BatchPageRequest, caller_hotkey: str) -> BatchPageResponse:
        if served is not None:
            served.append(caller_hotkey)
        return BatchPageResponse(
            miner_hotkey=miner.hotkey.ss58_address,
            batches=[],
            next_sequence=request.after_sequence,
            has_more=False,
        )

    return provide


def one_batch_page(miner: bt.Wallet) -> BatchPageResponse:
    return BatchPageResponse(
        miner_hotkey=miner.hotkey.ss58_address,
        batches=[
            PositionedBatch(
                batch={"sequence": 1},
                position=CommitmentPosition(block=10, extrinsic_index=2),
            )
        ],
        next_sequence=1,
        has_more=False,
    )


def make_app(miner: bt.Wallet, **overrides: Any) -> FastAPI:
    """Build a miner app that authorizes everyone and must not reach its provider."""

    options: dict[str, Any] = {"provider": refuse, "authorize_validator": allow_all}
    return create_miner_app(miner_hotkey=miner.hotkey.ss58_address, **(options | overrides))


def asgi_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=MINER_URL)


def signed_client(wallet: bt.Wallet, miner: bt.Wallet, app: FastAPI) -> SignedMinerClient:
    return SignedMinerClient(
        wallet,
        miner_hotkey=miner.hotkey.ss58_address,
        base_url=MINER_URL,
        transport=httpx.ASGITransport(app=app),
    )


def sign(wallet: bt.Wallet, receiver: bt.Wallet, path: str, body: bytes) -> dict[str, str]:
    headers: dict[str, str] = bt.http_auth.sign(
        wallet,
        method="POST",
        path=path,
        body=body,
        receiver_ss58=receiver.hotkey.ss58_address,
    )
    return headers


async def signed_post(
    app: FastAPI, wallet: bt.Wallet, receiver: bt.Wallet, path: str, body: bytes
) -> httpx.Response:
    async with asgi_client(app) as client:
        return await client.post(path, headers=sign(wallet, receiver, path, body), content=body)


def first_page_body() -> bytes:
    return BatchPageRequest(after_sequence=0).model_dump_json().encode()


@pytest.mark.asyncio
async def test_signed_batch_page_round_trip(tmp_path: Path) -> None:
    miner = create_wallet(tmp_path, "miner")
    validator = create_wallet(tmp_path, "validator")

    async def authorize(hotkey: str) -> bool:
        return hotkey == validator.hotkey.ss58_address

    async def provide(request: BatchPageRequest, caller_hotkey: str) -> BatchPageResponse:
        assert caller_hotkey == validator.hotkey.ss58_address
        assert request.after_sequence == 0
        return one_batch_page(miner)

    client = signed_client(
        validator, miner, make_app(miner, provider=provide, authorize_validator=authorize)
    )
    try:
        response = await client.fetch_batches(BatchPageRequest(after_sequence=0, max_batches=10))
    finally:
        await client.close()

    assert response.next_sequence == 1
    assert response.batches[0].batch == {"sequence": 1}
    assert response.batches[0].position.block == 10


@pytest.mark.asyncio
async def test_v2_overlap_endpoint_strips_positions(tmp_path: Path) -> None:
    miner = create_wallet(tmp_path, "miner")
    validator = create_wallet(tmp_path, "validator")

    async def provide(request: BatchPageRequest, caller_hotkey: str) -> BatchPageResponse:
        return one_batch_page(miner)

    response = await signed_post(
        make_app(miner, provider=provide),
        validator,
        miner,
        LEGACY_BATCHES_PATH,
        LegacyBatchPageRequest(after_sequence=0).model_dump_json().encode(),
    )

    assert response.status_code == 200
    assert response.json()["protocol_version"] == 2
    assert response.json()["batches"] == [{"sequence": 1}]


@pytest.mark.asyncio
async def test_replayed_signed_request_is_rejected(tmp_path: Path) -> None:
    miner = create_wallet(tmp_path, "miner")
    validator = create_wallet(tmp_path, "validator")
    app = make_app(miner, provider=empty_page(miner))
    body = first_page_body()
    headers = sign(validator, miner, BATCHES_PATH, body)

    async with asgi_client(app) as client:
        first = await client.post(BATCHES_PATH, headers=headers, content=body)
        replay = await client.post(BATCHES_PATH, headers=headers, content=body)

    assert first.status_code == 200
    assert replay.status_code == 401
    assert replay.json() == {"detail": "invalid Bittensor authentication"}


@pytest.mark.asyncio
async def test_wrong_receiver_is_rejected(tmp_path: Path) -> None:
    miner = create_wallet(tmp_path, "miner")
    other_miner = create_wallet(tmp_path, "other-miner")
    validator = create_wallet(tmp_path, "validator")

    response = await signed_post(
        make_app(miner), validator, other_miner, BATCHES_PATH, first_page_body()
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_authenticated_validator_without_authorization_is_forbidden(
    tmp_path: Path,
) -> None:
    miner = create_wallet(tmp_path, "miner")
    validator = create_wallet(tmp_path, "validator")
    checked: list[str] = []

    async def authorize(hotkey: str) -> bool:
        checked.append(hotkey)
        return False

    response = await signed_post(
        make_app(miner, authorize_validator=authorize),
        validator,
        miner,
        BATCHES_PATH,
        first_page_body(),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "validator is not authorized"}
    assert checked == [validator.hotkey.ss58_address]


@pytest.mark.asyncio
async def test_malformed_content_length_is_rejected_without_server_error(tmp_path: Path) -> None:
    app = make_app(create_wallet(tmp_path, "miner"))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": BATCHES_PATH,
        "raw_path": BATCHES_PATH.encode(),
        "query_string": b"",
        "headers": [(b"host", b"miner.test"), (b"content-length", b"not-a-number")],
        "client": ("127.0.0.1", 1),
        "server": ("miner.test", 80),
    }
    messages: list[dict[str, object]] = []
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    await app(scope, receive, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    assert start["status"] == 400


@pytest.mark.asyncio
async def test_miner_readiness_waits_for_endpoint_advertisement(tmp_path: Path) -> None:
    advertised = False

    async def readiness() -> bool:
        return advertised

    app = make_app(create_wallet(tmp_path, "miner"), readiness=readiness)
    async with asgi_client(app) as client:
        starting = await client.get("/ready")
        advertised = True
        ready = await client.get("/ready")
        health = await client.get("/health")

    assert starting.status_code == 503
    assert ready.status_code == 200
    assert health.json()["version"] == __version__


@pytest.mark.asyncio
async def test_oversized_request_is_rejected_before_authentication(tmp_path: Path) -> None:
    async def authorize(_hotkey: str) -> bool:
        raise AssertionError("authorization must not run")

    app = make_app(
        create_wallet(tmp_path, "miner"),
        authorize_validator=authorize,
        max_request_bytes=10,
    )
    async with asgi_client(app) as client:
        response = await client.post(BATCHES_PATH, content=b"x" * 11)

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_oversized_miner_response_is_rejected_without_parsing(tmp_path: Path) -> None:
    miner = create_wallet(tmp_path, "miner")
    validator = create_wallet(tmp_path, "validator")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 101)

    client = SignedMinerClient(
        validator,
        miner_hotkey=miner.hotkey.ss58_address,
        base_url=MINER_URL,
        max_response_bytes=100,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ResponseTooLargeError, match="exceeds"):
            await client.fetch_batches(BatchPageRequest(after_sequence=0))
    finally:
        await client.close()


def status_of(result: BatchPageResponse | BaseException) -> int:
    """Map one gathered fetch outcome to the HTTP status the miner returned."""

    if isinstance(result, httpx.HTTPStatusError):
        return result.response.status_code
    if isinstance(result, BaseException):
        raise result
    return 200


@pytest.mark.asyncio
async def test_concurrent_signed_traffic_is_rate_limited_per_validator_hotkey(
    tmp_path: Path,
) -> None:
    miner = create_wallet(tmp_path, "miner")
    validator = create_wallet(tmp_path, "validator")
    other_validator = create_wallet(tmp_path, "other-validator")
    limit, excess = 5, 3
    served: list[str] = []
    app = make_app(miner, provider=empty_page(miner, served), requests_per_minute=limit)
    client = signed_client(validator, miner, app)
    other_client = signed_client(other_validator, miner, app)
    try:
        results = await asyncio.gather(
            *(
                client.fetch_batches(BatchPageRequest(after_sequence=index))
                for index in range(limit + excess)
            ),
            return_exceptions=True,
        )
        other_page = await other_client.fetch_batches(BatchPageRequest(after_sequence=0))
    finally:
        await client.close()
        await other_client.close()

    assert sorted(status_of(result) for result in results) == [200] * limit + [429] * excess
    assert all(
        result.next_sequence == index
        for index, result in enumerate(results)
        if isinstance(result, BatchPageResponse)
    )
    assert served == [validator.hotkey.ss58_address] * limit + [other_validator.hotkey.ss58_address]
    assert other_page.next_sequence == 0
