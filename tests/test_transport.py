"""End-to-end tests for Bittensor v11 signed HTTP authentication."""

import asyncio
from pathlib import Path

import bittensor as bt
import httpx
import pytest

from bitcast_x import __version__
from bitcast_x.errors import ResponseTooLargeError
from bitcast_x.protocol import CommitmentPosition
from bitcast_x.transport import (
    BATCHES_PATH,
    LEGACY_BATCHES_PATH,
    BatchPageRequest,
    BatchPageResponse,
    LegacyBatchPageRequest,
    PositionedBatch,
    SignedMinerClient,
    create_miner_app,
)


def create_wallet(path: Path, name: str) -> bt.Wallet:
    """Create an unencrypted temporary wallet for authentication tests."""

    wallet = bt.Wallet(name=name, hotkey="default", path=str(path))
    wallet.create_new_coldkey(use_password=False, suppress=True)
    wallet.create_new_hotkey(use_password=False, suppress=True)
    return wallet


@pytest.mark.asyncio
async def test_signed_batch_page_round_trip(tmp_path: Path) -> None:
    miner = create_wallet(tmp_path, "miner")
    validator = create_wallet(tmp_path, "validator")

    async def authorize(hotkey: str) -> bool:
        return hotkey == validator.hotkey.ss58_address

    async def provide(request: BatchPageRequest, caller_hotkey: str) -> BatchPageResponse:
        assert caller_hotkey == validator.hotkey.ss58_address
        assert request.after_sequence == 0
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

    app = create_miner_app(
        miner_hotkey=miner.hotkey.ss58_address,
        provider=provide,
        authorize_validator=authorize,
    )
    client = SignedMinerClient(
        validator,
        miner_hotkey=miner.hotkey.ss58_address,
        base_url="http://miner.test",
        transport=httpx.ASGITransport(app=app),
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

    async def provide(_request: BatchPageRequest, _caller: str) -> BatchPageResponse:
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

    app = create_miner_app(
        miner_hotkey=miner.hotkey.ss58_address,
        provider=provide,
        authorize_validator=lambda _hotkey: asyncio.sleep(0, result=True),
    )
    body = LegacyBatchPageRequest(after_sequence=0).model_dump_json().encode()
    headers = bt.http_auth.sign(
        validator,
        method="POST",
        path=LEGACY_BATCHES_PATH,
        body=body,
        receiver_ss58=miner.hotkey.ss58_address,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://miner.test"
    ) as client:
        response = await client.post(LEGACY_BATCHES_PATH, headers=headers, content=body)

    assert response.status_code == 200
    assert response.json()["protocol_version"] == 2
    assert response.json()["batches"] == [{"sequence": 1}]


@pytest.mark.asyncio
async def test_replayed_signed_request_is_rejected(tmp_path: Path) -> None:
    miner = create_wallet(tmp_path, "miner")
    validator = create_wallet(tmp_path, "validator")

    async def authorize(_hotkey: str) -> bool:
        return True

    async def provide(_request: BatchPageRequest, _caller: str) -> BatchPageResponse:
        return BatchPageResponse(
            miner_hotkey=miner.hotkey.ss58_address,
            batches=[],
            next_sequence=0,
            has_more=False,
        )

    app = create_miner_app(
        miner_hotkey=miner.hotkey.ss58_address,
        provider=provide,
        authorize_validator=authorize,
    )
    body = BatchPageRequest(after_sequence=0).model_dump_json().encode()
    headers = bt.http_auth.sign(
        validator,
        method="POST",
        path=BATCHES_PATH,
        body=body,
        receiver_ss58=miner.hotkey.ss58_address,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://miner.test") as client:
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

    async def authorize(_hotkey: str) -> bool:
        return True

    async def provide(_request: BatchPageRequest, _caller: str) -> BatchPageResponse:
        raise AssertionError("provider must not run for unauthenticated traffic")

    app = create_miner_app(
        miner_hotkey=miner.hotkey.ss58_address,
        provider=provide,
        authorize_validator=authorize,
    )
    body = BatchPageRequest(after_sequence=0).model_dump_json().encode()
    headers = bt.http_auth.sign(
        validator,
        method="POST",
        path=BATCHES_PATH,
        body=body,
        receiver_ss58=other_miner.hotkey.ss58_address,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://miner.test"
    ) as client:
        response = await client.post(BATCHES_PATH, headers=headers, content=body)

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

    async def provide(_request: BatchPageRequest, _caller: str) -> BatchPageResponse:
        raise AssertionError("provider must not run for unauthorized validators")

    app = create_miner_app(
        miner_hotkey=miner.hotkey.ss58_address,
        provider=provide,
        authorize_validator=authorize,
    )
    body = BatchPageRequest(after_sequence=0).model_dump_json().encode()
    headers = bt.http_auth.sign(
        validator,
        method="POST",
        path=BATCHES_PATH,
        body=body,
        receiver_ss58=miner.hotkey.ss58_address,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://miner.test"
    ) as client:
        response = await client.post(BATCHES_PATH, headers=headers, content=body)

    assert response.status_code == 403
    assert response.json() == {"detail": "validator is not authorized"}
    assert checked == [validator.hotkey.ss58_address]


@pytest.mark.asyncio
async def test_malformed_content_length_is_rejected_without_server_error(tmp_path: Path) -> None:
    miner = create_wallet(tmp_path, "miner")

    async def authorize(_hotkey: str) -> bool:
        return True

    async def provide(_request: BatchPageRequest, _caller: str) -> BatchPageResponse:
        raise AssertionError("provider must not run")

    app = create_miner_app(
        miner_hotkey=miner.hotkey.ss58_address,
        provider=provide,
        authorize_validator=authorize,
    )
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
    miner = create_wallet(tmp_path, "miner")
    advertised = False

    async def authorize(_hotkey: str) -> bool:
        return True

    async def provide(_request: BatchPageRequest, _caller: str) -> BatchPageResponse:
        raise AssertionError("batch provider is not used")

    async def readiness() -> bool:
        return advertised

    app = create_miner_app(
        miner_hotkey=miner.hotkey.ss58_address,
        provider=provide,
        authorize_validator=authorize,
        readiness=readiness,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://miner.test"
    ) as client:
        starting = await client.get("/ready")
        advertised = True
        ready = await client.get("/ready")
        health = await client.get("/health")

    assert starting.status_code == 503
    assert ready.status_code == 200
    assert health.json()["version"] == __version__


@pytest.mark.asyncio
async def test_oversized_request_is_rejected_before_authentication(tmp_path: Path) -> None:
    miner = create_wallet(tmp_path, "miner")

    async def authorize(_hotkey: str) -> bool:
        raise AssertionError("authorization must not run")

    async def provide(_request: BatchPageRequest, _caller: str) -> BatchPageResponse:
        raise AssertionError("provider must not run")

    app = create_miner_app(
        miner_hotkey=miner.hotkey.ss58_address,
        provider=provide,
        authorize_validator=authorize,
        max_request_bytes=10,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://miner.test"
    ) as client:
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
        base_url="http://miner.test",
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

    async def authorize(hotkey: str) -> bool:
        return hotkey in {validator.hotkey.ss58_address, other_validator.hotkey.ss58_address}

    async def provide(request: BatchPageRequest, caller: str) -> BatchPageResponse:
        served.append(caller)
        await asyncio.sleep(0)
        return BatchPageResponse(
            miner_hotkey=miner.hotkey.ss58_address,
            batches=[],
            next_sequence=request.after_sequence,
            has_more=False,
        )

    app = create_miner_app(
        miner_hotkey=miner.hotkey.ss58_address,
        provider=provide,
        authorize_validator=authorize,
        requests_per_minute=limit,
    )
    client, other_client = (
        SignedMinerClient(
            wallet,
            miner_hotkey=miner.hotkey.ss58_address,
            base_url="http://miner.test",
            transport=httpx.ASGITransport(app=app),
        )
        for wallet in (validator, other_validator)
    )
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
