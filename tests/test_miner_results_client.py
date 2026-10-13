"""Central miner API signing and endpoint client tests."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from bitcast_x.miner.results import MinerResultsClient, canonical_query

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


class Signer:
    ss58_address = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"

    def __init__(self) -> None:
        self.messages: list[bytes] = []

    def sign(self, data: bytes) -> bytes:
        self.messages.append(data)
        return b"signature"


@asynccontextmanager
async def _client(handler: Handler, signer: Signer) -> AsyncIterator[MinerResultsClient]:
    """Yield a results client whose HTTP transport is the given handler."""

    client = MinerResultsClient("https://example.test", signer)
    await client._client.aclose()  # noqa: SLF001 - replace transport in a focused unit test
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("call", "path", "params", "signed_target", "body", "expected"),
    [
        pytest.param(
            lambda client: client.campaigns(("ai agents", "tao")),
            "/api/v2/miners/x/campaigns",
            [("ecosystem_id", "ai agents"), ("ecosystem_id", "tao")],
            "/api/v2/miners/x/campaigns?ecosystem_id=ai%20agents&ecosystem_id=tao",
            {"items": [{"campaign_id": "campaign"}]},
            [{"campaign_id": "campaign"}],
            id="campaigns-repeated-ecosystem-filters",
        ),
        pytest.param(
            lambda client: client.leaderboard(("tao",), limit=25, offset=50),
            "/api/v2/miners/x/leaderboard",
            [("ecosystem_id", "tao"), ("limit", "25"), ("offset", "50")],
            "/api/v2/miners/x/leaderboard?ecosystem_id=tao&limit=25&offset=50",
            {"ecosystem_ids": ["tao"], "accounts": []},
            {"ecosystem_ids": ["tao"], "accounts": []},
            id="leaderboard-filters-and-page",
        ),
        pytest.param(
            lambda client: client.submissions(campaign_id="campaign", tweet_id="123"),
            "/api/v2/miners/x/submissions",
            [("campaign_id", "campaign"), ("tweet_id", "123")],
            "/api/v2/miners/x/submissions?campaign_id=campaign&tweet_id=123",
            {"items": [{"submission_id": "a" * 32}]},
            [{"submission_id": "a" * 32}],
            id="submissions-owner-endpoint",
        ),
    ],
)
async def test_reads_call_their_endpoint_with_signed_canonical_queries(
    call: Callable[[MinerResultsClient], Awaitable[Any]],
    path: str,
    params: list[tuple[str, str]],
    signed_target: str,
    body: dict[str, Any],
    expected: object,
) -> None:
    signer = Signer()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=body)

    async with _client(handler, signer) as client:
        result = await call(client)

    assert result == expected
    [request] = requests
    assert request.method == "GET"
    assert request.url.path == path
    assert request.url.params.multi_items() == params
    [message] = signer.messages
    assert message.decode().splitlines() == [
        "bitcast-x-miner-api-v1",
        "GET",
        signed_target,
        request.headers["X-Bitcast-Timestamp"],
    ]
    assert request.headers["X-Bitcast-Hotkey"] == signer.ss58_address
    assert request.headers["X-Bitcast-Signature"] == b"signature".hex()


def test_canonical_query_sorts_out_of_order_parameters() -> None:
    assert (
        canonical_query([("tweet_id", "123"), ("campaign_id", "campaign")])
        == "campaign_id=campaign&tweet_id=123"
    )
