"""Tests for the public campaign snapshot client."""

import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from bitcast_x.campaign_urls import CAMPAIGN_FEED_URL, LEGACY_CAMPAIGN_FEED_URL
from bitcast_x.campaigns import (
    CampaignFeed,
    CampaignFeedClient,
    CampaignManifest,
    EcosystemMap,
    SocialAccount,
    _map_digest,
    eligible_creator_ids_for_campaign,
    eligible_creator_ids_in_map,
)
from bitcast_x.errors import ProtocolError, ResponseTooLargeError

FEED = {
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
            "accounts": [
                {
                    "x_id": "123",
                    "username": "example",
                    "influence": 1.0,
                    "followers_count": 10,
                }
            ],
        }
    ],
}


def _manifest() -> dict[str, object]:
    ecosystem_map = EcosystemMap.model_validate(FEED["ecosystem_maps"][0])  # type: ignore[index]
    digest = _map_digest(ecosystem_map)
    campaigns = deepcopy(FEED["campaigns"])
    campaigns[0]["max_members"] = 1  # type: ignore[index]
    return {
        "protocol_version": 4,
        "snapshot_id": "snapshot-1",
        "published_at": FEED["published_at"],
        "campaigns": campaigns,
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


def test_campaign_contract_only_accepts_supported_prompt_versions() -> None:
    payload = deepcopy(FEED)
    for supported in (1, 2, 5, 6):
        payload["campaigns"][0]["prompt_version"] = supported  # type: ignore[index]
        parsed = CampaignFeed.model_validate(payload)
        assert parsed.campaigns[0].prompt_version == supported

    for retired_or_unknown in (3, 4, 7):
        payload["campaigns"][0]["prompt_version"] = retired_or_unknown  # type: ignore[index]
        with pytest.raises(ValidationError, match="Input should be 1, 2, 5 or 6"):
            CampaignFeed.model_validate(payload)


def test_manifest_v4_requires_rank_cutoff_on_every_campaign() -> None:
    ranked = CampaignManifest.model_validate(_manifest())

    assert ranked.campaigns[0].max_members == 1

    missing = _manifest()
    missing["campaigns"][0].pop("max_members")  # type: ignore[index]
    with pytest.raises(ValidationError, match="must define max_members"):
        CampaignManifest.model_validate(missing)


def test_retired_manifest_versions_are_rejected() -> None:
    for retired in (2, 3):
        payload = _manifest()
        payload["protocol_version"] = retired
        with pytest.raises(ValidationError, match="protocol_version"):
            CampaignManifest.model_validate(payload)


def test_rank_cutoff_uses_influence_then_immutable_id_for_ties() -> None:
    ecosystem = EcosystemMap(
        ecosystem_id="example",
        name="Example",
        eligible_creator_x_ids=("30", "20", "10"),
        updated_at="2026-08-05T12:00:00Z",
        accounts=(
            SocialAccount(x_id="30", username="third", influence=1.0),
            SocialAccount(x_id="20", username="second", influence=2.0),
            SocialAccount(x_id="10", username="first", influence=2.0),
        ),
    )

    assert eligible_creator_ids_in_map(ecosystem, 2) == frozenset({"10", "20"})


def test_campaign_rank_eligibility_unions_top_n_across_overlapping_maps() -> None:
    campaign = CampaignFeed.model_validate(FEED).campaigns[0].model_copy(update={"max_members": 1})
    old_map = EcosystemMap(
        ecosystem_id="example",
        name="Old map",
        eligible_creator_x_ids=("10", "20"),
        updated_at="2026-08-01T12:00:00Z",
        accounts=(
            SocialAccount(x_id="10", username="incumbent", influence=2.0),
            SocialAccount(x_id="20", username="challenger", influence=1.0),
        ),
    )
    new_map = EcosystemMap(
        ecosystem_id="example",
        name="New map",
        eligible_creator_x_ids=("10", "20"),
        updated_at="2026-08-08T12:00:00Z",
        accounts=(
            SocialAccount(x_id="20", username="challenger", influence=3.0),
            SocialAccount(x_id="10", username="incumbent", influence=1.0),
        ),
    )
    after_campaign = EcosystemMap(
        ecosystem_id="example",
        name="After campaign",
        eligible_creator_x_ids=("30",),
        updated_at=new_map.updated_at + timedelta(days=5),
        accounts=(SocialAccount(x_id="30", username="latecomer", influence=4.0),),
    )
    snapshot = CampaignFeed.model_validate(FEED).model_copy(
        update={
            "campaigns": (campaign,),
            "ecosystem_maps": (old_map, new_map, after_campaign),
        }
    )

    assert eligible_creator_ids_for_campaign(snapshot, campaign) == frozenset({"10", "20"})


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
        with pytest.raises(ResponseTooLargeError, match="exceeds"):
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
async def test_retired_v3_feed_url_reads_the_v4_manifest(tmp_path: Path) -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json=_manifest())

    client = CampaignFeedClient(
        LEGACY_CAMPAIGN_FEED_URL,
        cache_path=tmp_path / "feed.json",
        transport=httpx.MockTransport(handler),
    )
    try:
        campaigns = await client.fetch_campaigns()
    finally:
        await client.close()

    assert client.url == CAMPAIGN_FEED_URL
    assert campaigns[0].max_members == 1
    assert requests == ["/api/v2/public/x/campaign-manifest-v4"]


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
    assert second.campaigns[0].display == "Example campaign"
    assert second.campaigns[0].pools == ("example",)
    assert json.loads((tmp_path / "feed.json").read_text())["etag"] == '"manifest-1"'
    bindings = json.loads((tmp_path / "feed.json.map-bindings.json").read_text())
    assert bindings == {
        "version": 1,
        "maps": [
            {
                "ecosystem_id": "example",
                "run_id": 7,
                "digest": _manifest()["ecosystem_maps"][0]["digest"],  # type: ignore[index]
            }
        ],
    }


@pytest.mark.asyncio
async def test_rejects_changed_digest_for_an_accepted_ecosystem_run(tmp_path: Path) -> None:
    original_manifest = _manifest()
    changed_map = deepcopy(FEED["ecosystem_maps"][0])  # type: ignore[index]
    changed_map["name"] = "Mutated after publication"
    changed_digest = _map_digest(EcosystemMap.model_validate(changed_map))
    changed_manifest = deepcopy(original_manifest)
    changed_manifest["ecosystem_maps"][0]["digest"] = changed_digest  # type: ignore[index]
    changed_manifest["ecosystem_maps"][0]["path"] = (  # type: ignore[index]
        f"/api/v2/public/x/ecosystem-maps/example/7/{changed_digest}"
    )
    manifest_calls = 0
    map_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal manifest_calls, map_calls
        if request.url.path.endswith("campaign-manifest"):
            manifest_calls += 1
            manifest = original_manifest if manifest_calls == 1 else changed_manifest
            return httpx.Response(200, json=manifest, headers={"etag": f'"v{manifest_calls}"'})
        map_calls += 1
        return httpx.Response(200, json=FEED["ecosystem_maps"][0])  # type: ignore[index]

    path = tmp_path / "feed.json"
    client = CampaignFeedClient(
        "https://feed.example/api/v2/public/x/campaign-manifest",
        cache_path=path,
        transport=httpx.MockTransport(handler),
    )
    try:
        first = await client.fetch()
        with pytest.raises(ProtocolError, match="changed the digest"):
            await client.fetch()
    finally:
        await client.close()

    assert first.ecosystem_maps[0].name == "Example"
    assert map_calls == 1
    cached = json.loads(path.read_text())
    assert (
        cached["manifest"]["ecosystem_maps"][0]["digest"]
        == (  # type: ignore[index]
            original_manifest["ecosystem_maps"][0]["digest"]  # type: ignore[index]
        )
    )


@pytest.mark.asyncio
async def test_upgrade_imports_verified_cached_map_before_accepting_new_digest(
    tmp_path: Path,
) -> None:
    original_manifest = _manifest()
    changed_map = deepcopy(FEED["ecosystem_maps"][0])  # type: ignore[index]
    changed_map["name"] = "Mutated after the validator cached the run"
    changed_digest = _map_digest(EcosystemMap.model_validate(changed_map))
    changed_manifest = deepcopy(original_manifest)
    changed_manifest["ecosystem_maps"][0]["digest"] = changed_digest  # type: ignore[index]
    changed_manifest["ecosystem_maps"][0]["path"] = (  # type: ignore[index]
        f"/api/v2/public/x/ecosystem-maps/example/7/{changed_digest}"
    )
    manifest_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal manifest_calls
        if request.url.path.endswith("campaign-manifest"):
            manifest_calls += 1
            manifest = original_manifest if manifest_calls == 1 else changed_manifest
            return httpx.Response(200, json=manifest)
        return httpx.Response(200, json=FEED["ecosystem_maps"][0])  # type: ignore[index]

    path = tmp_path / "feed.json"
    client = CampaignFeedClient(
        "https://feed.example/api/v2/public/x/campaign-manifest",
        cache_path=path,
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.fetch()
        bindings_path = tmp_path / "feed.json.map-bindings.json"
        bindings_path.unlink()  # Simulate a cache written by the previous release.

        with pytest.raises(ProtocolError, match="changed the digest"):
            await client.fetch()
    finally:
        await client.close()

    imported = json.loads(bindings_path.read_text())
    assert imported["maps"] == [
        {
            "ecosystem_id": "example",
            "run_id": 7,
            "digest": original_manifest["ecosystem_maps"][0]["digest"],  # type: ignore[index]
        }
    ]


@pytest.mark.asyncio
async def test_accepts_and_records_a_new_ecosystem_run(tmp_path: Path) -> None:
    first_manifest = _manifest()
    next_manifest = deepcopy(first_manifest)
    next_manifest["ecosystem_maps"][0]["run_id"] = 8  # type: ignore[index]
    next_manifest["ecosystem_maps"][0]["path"] = (  # type: ignore[index]
        f"/api/v2/public/x/ecosystem-maps/example/8/"
        f"{next_manifest['ecosystem_maps'][0]['digest']}"  # type: ignore[index]
    )
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("campaign-manifest"):
            calls += 1
            manifest = first_manifest if calls == 1 else next_manifest
            return httpx.Response(200, json=manifest)
        return httpx.Response(200, json=FEED["ecosystem_maps"][0])  # type: ignore[index]

    client = CampaignFeedClient(
        "https://feed.example/api/v2/public/x/campaign-manifest",
        cache_path=tmp_path / "feed.json",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.fetch()
        await client.fetch()
    finally:
        await client.close()

    bindings = json.loads((tmp_path / "feed.json.map-bindings.json").read_text())
    assert [(item["ecosystem_id"], item["run_id"]) for item in bindings["maps"]] == [
        ("example", 7),
        ("example", 8),
    ]


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

    assert (tmp_path / "feed.json.map-bindings.json").exists() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale",
    [
        {"url": "https://other.example/campaign-manifest", "manifest": "replaced below"},
        {"url": "https://feed.example/api/v2/public/x/campaign-manifest", "feed": FEED},
    ],
    ids=["different-url", "retired-v2-feed"],
)
async def test_does_not_reuse_an_etag_from_a_stale_cache(
    tmp_path: Path, stale: dict[str, object]
) -> None:
    path = tmp_path / "feed.json"
    cache = {**stale, "etag": '"snapshot-1"'}
    if "manifest" in cache:
        cache["manifest"] = _manifest()
    path.write_text(json.dumps(cache))

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


@pytest.mark.asyncio
async def test_unreadable_map_cache_is_downloaded_again(tmp_path: Path) -> None:
    manifest = _manifest()
    digest = manifest["ecosystem_maps"][0]["digest"]  # type: ignore[index]
    cached_map = tmp_path / "feed.json.maps" / f"{digest}.json"
    cached_map.parent.mkdir()
    # Written by releases that still carried the retired referral field.
    stale = {**FEED["ecosystem_maps"][0], "max_referral_amount": 100.0}  # type: ignore[dict-item]
    cached_map.write_text(json.dumps(stale))
    map_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal map_calls
        if request.url.path.endswith("campaign-manifest"):
            return httpx.Response(200, json=manifest)
        map_calls += 1
        return httpx.Response(200, json=FEED["ecosystem_maps"][0])  # type: ignore[index]

    client = CampaignFeedClient(
        "https://feed.example/api/v2/public/x/campaign-manifest",
        cache_path=tmp_path / "feed.json",
        transport=httpx.MockTransport(handler),
    )
    try:
        feed = await client.fetch()
    finally:
        await client.close()

    assert map_calls == 1
    assert feed.ecosystem_maps[0].ecosystem_id == "example"
    assert "max_referral_amount" not in json.loads(cached_map.read_text())


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


@pytest.mark.asyncio
async def test_campaigns_command_reads_maps_larger_than_the_protocol_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Published ecosystem maps reach several MB, well past the protocol page
    # limit (max_response_bytes); the CLI must bound them by the feed limit.
    import argparse
    import functools

    from bitcast_x import main
    from bitcast_x.config import Settings

    manifest = _manifest()
    ecosystem_map = FEED["ecosystem_maps"][0]  # type: ignore[index]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("campaign-manifest"):
            return httpx.Response(200, json=manifest)
        return httpx.Response(200, json=ecosystem_map)

    monkeypatch.setattr(
        main,
        "CampaignFeedClient",
        functools.partial(CampaignFeedClient, transport=httpx.MockTransport(handler)),
    )
    settings = Settings(
        state_dir=tmp_path,
        campaign_feed_url="https://feed.example/api/v2/public/x/campaign-manifest",
        max_response_bytes=len(json.dumps(ecosystem_map)) - 1,
    )

    feed = await main.run_command(argparse.Namespace(command="campaigns"), settings)

    assert feed is not None
    assert [c["access"]["campaign_id"] for c in feed["campaigns"]] == ["campaign-1"]
