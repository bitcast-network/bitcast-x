"""Central miner API signing and endpoint client tests."""

import json

import httpx

from bitcast_x.miner.results import MinerResultsClient, canonical_query


class Signer:
    ss58_address = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"

    def __init__(self) -> None:
        self.messages: list[bytes] = []

    def sign(self, data: bytes) -> bytes:
        self.messages.append(data)
        return b"signature"


async def test_repeated_ecosystem_filters_are_canonical_and_signed() -> None:
    signer = Signer()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/miners/x/campaigns"
        assert request.url.params.multi_items() == [
            ("ecosystem_id", "ai agents"),
            ("ecosystem_id", "tao"),
        ]
        assert request.headers["X-Bitcast-Hotkey"] == signer.ss58_address
        assert request.headers["X-Bitcast-Signature"] == b"signature".hex()
        return httpx.Response(200, json={"items": [{"campaign_id": "campaign"}]})

    client = MinerResultsClient("https://example.test", signer)
    await client._client.aclose()  # noqa: SLF001 - replace transport in a focused unit test
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        campaigns = await client.campaigns(("ai agents", "tao"))
    finally:
        await client.close()

    assert campaigns == [{"campaign_id": "campaign"}]
    signed = signer.messages[0].decode().splitlines()
    assert signed[:3] == [
        "bitcast-x-miner-api-v1",
        "GET",
        "/api/v2/miners/x/campaigns?ecosystem_id=ai%20agents&ecosystem_id=tao",
    ]


async def test_submission_collection_uses_owner_endpoint() -> None:
    signer = Signer()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps({"items": [{"submission_id": "a" * 32}]}),
            headers={"content-type": "application/json"},
        )

    client = MinerResultsClient("https://example.test", signer)
    await client._client.aclose()  # noqa: SLF001
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        submissions = await client.submissions(campaign_id="campaign", tweet_id="123")
    finally:
        await client.close()

    assert submissions == [{"submission_id": "a" * 32}]
    assert (
        canonical_query([("tweet_id", "123"), ("campaign_id", "campaign")])
        == "campaign_id=campaign&tweet_id=123"
    )
