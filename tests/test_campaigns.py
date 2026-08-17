"""Tests for the public campaign snapshot client."""

import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from bitcast_x.campaigns import CampaignFeed, CampaignFeedClient, EcosystemMap, _map_digest

FEED = {
    "protocol_version": 2,
    "snapshot_id": "snapshot-1",
    "published_at": "2026-08-05T12:00:00Z",
    "campaigns": [
        {
            "access": {
                "campaign_id": "campaign-1",
                "mechanism_id": 1,
                "mining_protocol": "preclaim_v2",
                "scoring_close_block": 12345,
                "exclusive_miner_hotkey": None,
            },
            "display": "Example campaign",
            "brief": "Explain the product in your own words.",
            "pools": ["example"],
            "opens_at": "2026-08-05T12:00:00Z",
            "closes_at": "2026-08-12T12:00:00Z",
            "reward_pool_usd": "1000.00",
            "required_terms": ["#example"],
        }
    ],
    "ecosystem_maps": [
        {
            "ecosystem_id": "example",
            "name": "Example",
            "eligible_creator_x_ids": ["123"],
            "updated_at": "2026-08-05T12:00:00Z",
        }
    ],
}


def _manifest() -> dict[str, object]:
    ecosystem_map = EcosystemMap.model_validate(FEED["ecosystem_maps"][0])  # type: ignore[index]
    digest = _map_digest(ecosystem_map)
    return {
        "protocol_version": 3,
        "snapshot_id": "snapshot-1",
        "published_at": FEED["published_at"],
        "campaigns": FEED["campaigns"],
        "ecosystem_maps": [
            {
                "ecosystem_id": "example",
                "run_id": 7,
                "updated_at": "2026-08-05T12:00:00Z",
                "digest": digest,
                "path": f"/api/v2/public/x/ecosystem-maps/example/7/{digest}",
                "size_bytes": 100,
            }
        ],
    }


def test_campaign_contract_rejects_removed_language_filter() -> None:
    payload = deepcopy(FEED)
    payload["campaigns"][0]["language"] = "en"  # type: ignore[index]

    with pytest.raises(ValidationError, match="language filters are not supported"):
        CampaignFeed.model_validate(payload)


def test_campaign_contract_accepts_legacy_null_language_as_noop() -> None:
    payload = deepcopy(FEED)
    payload["campaigns"][0]["language"] = None  # type: ignore[index]

    parsed = CampaignFeed.model_validate(payload)

    assert "language" not in parsed.campaigns[0].model_dump()


def test_campaign_contract_accepts_prompt_v5_and_rejects_unknown_versions() -> None:
    payload = deepcopy(FEED)
    payload["campaigns"][0]["prompt_version"] = 5  # type: ignore[index]

    parsed = CampaignFeed.model_validate(payload)

    assert parsed.campaigns[0].prompt_version == 5

    payload["campaigns"][0]["prompt_version"] = 6  # type: ignore[index]
    with pytest.raises(ValidationError, match="less than or equal to 5"):
        CampaignFeed.model_validate(payload)


@pytest.mark.asyncio
async def test_fetches_valid_snapshot_and_reuses_etag_cache(tmp_path: Path) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=FEED, headers={"etag": '"v1"'})
        assert request.headers["if-none-match"] == '"v1"'
        return httpx.Response(304)

    client = CampaignFeedClient(
        "https://feed.example/v2/snapshot",
        cache_path=tmp_path / "feed.json",
        transport=httpx.MockTransport(handler),
    )
    try:
        first = await client.fetch()
        second = await client.fetch()
    finally:
        await client.close()

    assert first == second
    assert second.campaigns[0].access.campaign_id == "campaign-1"
    assert second.campaigns[0].display == "Example campaign"
    assert second.campaigns[0].pools == ("example",)
    assert json.loads((tmp_path / "feed.json").read_text())["etag"] == '"v1"'


@pytest.mark.asyncio
async def test_rejects_oversized_snapshot_without_replacing_cache(tmp_path: Path) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 101)

    path = tmp_path / "feed.json"
    client = CampaignFeedClient(
        "https://feed.example/v2/snapshot",
        cache_path=path,
        max_response_bytes=100,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError, match="exceeds"):
            await client.fetch()
    finally:
        await client.close()

    assert path.exists() is False


@pytest.mark.asyncio
async def test_campaign_listing_does_not_download_manifest_maps(tmp_path: Path) -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json=_manifest(), headers={"etag": '"manifest-1"'})

    client = CampaignFeedClient(
        "https://feed.example/api/v2/public/x/campaign-manifest",
        cache_path=tmp_path / "feed.json",
        transport=httpx.MockTransport(handler),
    )
    try:
        campaigns = await client.fetch_campaigns()
    finally:
        await client.close()

    assert campaigns[0].access.campaign_id == "campaign-1"
    assert requests == ["/api/v2/public/x/campaign-manifest"]


@pytest.mark.asyncio
async def test_split_feed_downloads_each_map_once_then_uses_digest_cache(tmp_path: Path) -> None:
    manifest = _manifest()
    calls = {"manifest": 0, "map": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("campaign-manifest"):
            calls["manifest"] += 1
            if calls["manifest"] > 1:
                return httpx.Response(304)
            return httpx.Response(200, json=manifest, headers={"etag": '"manifest-1"'})
        calls["map"] += 1
        return httpx.Response(200, json=FEED["ecosystem_maps"][0])  # type: ignore[index]

    client = CampaignFeedClient(
        "https://feed.example/api/v2/public/x/campaign-manifest",
        cache_path=tmp_path / "feed.json",
        transport=httpx.MockTransport(handler),
    )
    try:
        first = await client.fetch()
        second = await client.fetch()
    finally:
        await client.close()

    assert first == second
    assert calls == {"manifest": 2, "map": 1}
    assert client.cached() == second


@pytest.mark.asyncio
async def test_split_feed_rejects_map_whose_content_does_not_match_digest(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("campaign-manifest"):
            return httpx.Response(200, json=_manifest())
        wrong_map = deepcopy(FEED["ecosystem_maps"][0])  # type: ignore[index]
        wrong_map["name"] = "Tampered"
        return httpx.Response(200, json=wrong_map)

    client = CampaignFeedClient(
        "https://feed.example/api/v2/public/x/campaign-manifest",
        cache_path=tmp_path / "feed.json",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError, match="digest"):
            await client.fetch()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_does_not_reuse_an_etag_cached_for_a_different_feed_url(tmp_path: Path) -> None:
    path = tmp_path / "feed.json"
    path.write_text(json.dumps({"etag": '"snapshot-1"', "feed": FEED}))

    async def handler(request: httpx.Request) -> httpx.Response:
        assert "if-none-match" not in request.headers
        return httpx.Response(200, json=_manifest(), headers={"etag": '"snapshot-1"'})

    client = CampaignFeedClient(
        "https://feed.example/api/v2/public/x/campaign-manifest",
        cache_path=path,
        transport=httpx.MockTransport(handler),
    )
    try:
        campaigns = await client.fetch_campaigns()
    finally:
        await client.close()

    assert campaigns[0].access.campaign_id == "campaign-1"
    assert json.loads(path.read_text())["url"] == str(client.url)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["campaigns"].append(deepcopy(value["campaigns"][0])), "unique"),
        (
            lambda value: value["campaigns"][0].update({"reward_pool_usd": "NaN"}),
            "finite and positive",
        ),
        (
            lambda value: value["ecosystem_maps"][0].update(
                {"eligible_creator_x_ids": ["mutable-handle"]}
            ),
            "immutable numeric X IDs",
        ),
    ],
)
def test_rejects_ambiguous_consensus_feed_values(mutation: object, message: str) -> None:
    payload = deepcopy(FEED)
    mutation(payload)  # type: ignore[operator]

    with pytest.raises(ValidationError, match=message):
        CampaignFeed.model_validate(payload)
