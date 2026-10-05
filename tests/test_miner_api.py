"""Offline HTTP tests for the generic authenticated miner API."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from bitcast_x.config import Settings
from bitcast_x.errors import ChainOperationError
from bitcast_x.miner import (
    BatchPolicy,
    EventStatus,
    FinalizedCommitment,
    MinerEngine,
    MinerSdk,
    MinerStore,
)
from bitcast_x.miner.api import _ERROR_STATUS, create_control_app
from bitcast_x.miner.control import MinerControlService
from bitcast_x.miner.engine import CapacityBudget
from bitcast_x.miner.errors import ErrorCode
from bitcast_x.miner.web import build_miner_api
from bitcast_x.protocol import CommitmentEnvelope, CommitmentPosition, CommittedBatch
from bitcast_x.transport import BatchPageRequest, create_miner_app
from contracts.bitcast_api_miner_campaign import (
    MinerCampaign as BitcastApiMinerCampaign,
)
from contracts.bitcast_api_miner_campaign import (
    MinerCampaignEligibility as BitcastApiMinerCampaignEligibility,
)

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
INTERNAL_TOKEN = "a" * 64
AUTH_HEADERS = {"Authorization": f"Bearer {INTERNAL_TOKEN}"}


class Submitter:
    async def capacity(self, _envelope: CommitmentEnvelope) -> CapacityBudget:
        return CapacityBudget(remaining_space=100, next_call_charge=100)

    async def latest(self) -> None:
        return None

    async def submit(self, envelope: CommitmentEnvelope) -> FinalizedCommitment:
        return FinalizedCommitment(
            position=CommitmentPosition(block=100, extrinsic_index=1),
            stored_envelope=envelope.encode(),
        )


class SlowSubmitter(Submitter):
    async def submit(self, envelope: CommitmentEnvelope) -> FinalizedCommitment:
        await asyncio.sleep(1)
        return await super().submit(envelope)


class FailingSubmitter(Submitter):
    async def submit(self, _envelope: CommitmentEnvelope) -> FinalizedCommitment:
        raise ChainOperationError("finalized commitment outcome is unavailable")


class LateSubmitter(Submitter):
    async def submit(self, envelope: CommitmentEnvelope) -> FinalizedCommitment:
        return FinalizedCommitment(
            position=CommitmentPosition(block=101, extrinsic_index=1),
            stored_envelope=envelope.encode(),
        )


def _central_error(
    status_code: int, path: str, headers: dict[str, str] | None = None
) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://central.test{path}")
    response = httpx.Response(status_code, request=request, headers=headers)
    return httpx.HTTPStatusError("central error", request=request, response=response)


class Results:
    """Central miner API double with one open preclaim campaign."""

    campaign_record = {
        "campaign_id": "campaign",
        "campaign_snapshot_id": "sha256-snapshot",
        "ecosystem_ids": ["tao", "hyperliquid"],
        "status": "open",
        "protocol": {"version": 2, "submission_mode": "preclaim"},
        "access": {"mode": "open", "exclusive_miner_hotkey": None},
        "opens_at": "2026-08-28T00:00:00Z",
        "closes_at": "2026-08-31T23:59:59Z",
        "scoring_close_block": 100,
        "brief": "Explain the campaign in your own words.",
        "prompt_version": 2,
        "x_brief": {"brief_id": "campaign"},
        "required_terms": [],
        "language": "en",
        "tag": "@campaign",
        "quoted_tweet_id": None,
        "inclusion_keywords": [],
        "reward_pool_usd": "1000.00",
        "max_tweets_per_creator": 1,
        "ecosystem_rules": [
            {"ecosystem_id": "tao", "max_members": 100},
            {"ecosystem_id": "hyperliquid", "max_members": 100},
        ],
        "presentation": {
            "name": "Campaign",
            "description": None,
            "image_url": None,
        },
        "capabilities": {
            "can_check_eligibility": True,
            "can_claim": True,
            "can_submit": True,
            "can_view_results": True,
            "requires_claim": True,
            "is_exclusive_to_this_miner": False,
        },
        "stats": {
            "matched_tweets": 0,
            "total_views": 0,
            "total_engagements": 0,
            "engagement_rate": 0,
            "data_updated_at": "2026-09-01T12:00:00Z",
        },
        "updated_at": "2026-09-01T12:00:00Z",
    }

    async def ecosystems(self) -> list[dict[str, Any]]:
        return [
            {"ecosystem_id": "tao", "name": "TAO", "status": "active"},
            {"ecosystem_id": "hyperliquid", "name": "Hyperliquid", "status": "active"},
        ]

    async def campaigns(self, ecosystem_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        if ecosystem_ids and not set(ecosystem_ids).intersection(
            self.campaign_record["ecosystem_ids"]
        ):
            return []
        return [self.campaign_record]

    async def leaderboard(
        self,
        ecosystem_ids: tuple[str, ...] = (),
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        del limit, offset
        return {
            "ecosystem_ids": list(ecosystem_ids),
            "accounts": [
                {
                    "rank": 1,
                    "username": "creator",
                    "score": 0.9,
                    "scores": {"tao": 0.9, "hyperliquid": 0.8},
                }
            ],
            "total_count": 1,
        }

    async def campaign(self, campaign_id: str) -> dict[str, Any]:
        if campaign_id != "campaign":
            raise _central_error(404, f"/api/v2/miners/x/campaigns/{campaign_id}")
        return self.campaign_record

    async def eligibility(self, campaign_id: str, creator_x_id: str) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "campaign_snapshot_id": "sha256-snapshot",
            "creator_x_id": creator_x_id,
            "eligible": True,
            "claim_eligible": True,
            "eligible_if_published_now": True,
            "eligible_ecosystems": [
                {"ecosystem_id": "tao", "eligible": True, "rank": 7, "cutoff": 100},
                {
                    "ecosystem_id": "hyperliquid",
                    "eligible": True,
                    "rank": 11,
                    "cutoff": 100,
                },
            ],
            "badges": [
                {"ecosystem_id": "tao", "label": "TAO"},
                {"ecosystem_id": "hyperliquid", "label": "Hyperliquid"},
            ],
            "reason": "eligible",
            "checked_at": "2026-09-01T12:00:00Z",
        }

    async def submission(self, submission_id: str) -> dict[str, Any]:
        return {"submission_id": submission_id, "status": "verification_pending"}

    async def submissions(self, **_filters: object) -> list[dict[str, Any]]:
        return []


class DirectResults(Results):
    """Central double for an exclusive protocol-v2 direct campaign."""

    campaign_record = {
        **Results.campaign_record,
        "protocol": {"version": 2, "submission_mode": "direct"},
        "access": {"mode": "exclusive", "exclusive_miner_hotkey": MINER},
        "capabilities": {
            **Results.campaign_record["capabilities"],
            "can_claim": False,
            "requires_claim": False,
            "is_exclusive_to_this_miner": True,
        },
    }

    async def eligibility(self, campaign_id: str, creator_x_id: str) -> dict[str, Any]:
        result = await super().eligibility(campaign_id, creator_x_id)
        result["claim_eligible"] = False
        return result


class EvaluatingDirectResults(DirectResults):
    """Exclusive campaign accepting existing posts during submission grace."""

    campaign_record = {**DirectResults.campaign_record, "status": "evaluating"}

    async def eligibility(self, campaign_id: str, creator_x_id: str) -> dict[str, Any]:
        result = await super().eligibility(campaign_id, creator_x_id)
        result.update(eligible_if_published_now=False, reason="campaign_not_open")
        return result


class IneligibleDirectResults(DirectResults):
    """Direct campaign whose creator is eligible in no ecosystem."""

    async def eligibility(self, campaign_id: str, creator_x_id: str) -> dict[str, Any]:
        result = await super().eligibility(campaign_id, creator_x_id)
        result.update(
            eligible=False,
            eligible_if_published_now=False,
            eligible_ecosystems=[],
            badges=[],
            reason="creator_not_eligible",
        )
        return result


class UnavailableCampaignResults(Results):
    """Central answers a single-campaign lookup with 503 rather than 404."""

    async def campaign(self, campaign_id: str) -> dict[str, Any]:
        raise _central_error(503, f"/api/v2/miners/x/campaigns/{campaign_id}")


@pytest.mark.parametrize(
    ("results", "status", "eligible_if_published_now"),
    [
        pytest.param(Results(), "open", True, id="preclaim"),
        pytest.param(DirectResults(), "open", True, id="direct"),
        pytest.param(EvaluatingDirectResults(), "evaluating", False, id="evaluating-direct"),
    ],
)
def test_results_doubles_match_pinned_bitcast_api_contract(
    results: Results, status: str, *, eligible_if_published_now: bool
) -> None:
    eligibility_record = asyncio.run(results.eligibility("campaign", "123"))

    campaign = BitcastApiMinerCampaign.model_validate(results.campaign_record)
    eligibility = BitcastApiMinerCampaignEligibility.model_validate(eligibility_record)

    assert set(results.campaign_record) <= set(BitcastApiMinerCampaign.model_fields)
    assert set(eligibility_record) <= set(BitcastApiMinerCampaignEligibility.model_fields)
    assert campaign.status == status
    assert campaign.scoring_close_block == 100
    assert campaign.capabilities.can_submit is True
    assert eligibility.eligible is True
    assert eligibility.eligible_if_published_now is eligible_if_published_now


def build_client(
    tmp_path: Path,
    *,
    submitter: Submitter | None = None,
    timeout: float = 5,
    enabled_ecosystems: tuple[str, ...] = ("tao", "hyperliquid"),
    qualified: bool = True,
    results_client: Results | None = None,
    split_protocol: bool = False,
    policy: BatchPolicy | None = None,
) -> TestClient:
    engine = MinerEngine(
        miner_hotkey=MINER,
        store=MinerStore(tmp_path / "miner.sqlite3"),
        submitter=submitter or Submitter(),
        policy=policy or BatchPolicy(max_age_seconds=5),
    )

    async def qualification() -> dict[str, object]:
        return {
            "eligible": qualified,
            "reason": "eligible" if qualified else "conviction_below_minimum",
        }

    service = MinerControlService(
        MinerSdk(engine, qualification_provider=qualification),
        results_client=results_client or Results(),  # type: ignore[arg-type]
        commit_timeout_seconds=timeout,
        enabled_ecosystem_ids=enabled_ecosystems,
    )
    protocol = create_miner_app(
        miner_hotkey=MINER,
        provider=engine.batch_page,
        authorize_validator=lambda _hotkey: _authorized(),
    )
    return TestClient(
        create_control_app(lambda: service, None if split_protocol else protocol, INTERNAL_TOKEN),
        headers=AUTH_HEADERS,
    )


async def _authorized() -> bool:
    return True


def _post_claim(
    web: TestClient,
    *,
    key: str = "claim-key-0001",
    draft: str = "Exact draft",
    external_id: str | None = "creator-claim-1",
) -> httpx.Response:
    return web.post(
        "/api/v1/claims",
        headers={"Idempotency-Key": key},
        json={
            "campaign_id": "campaign",
            "creator_x_id": "123",
            "draft": draft,
            "external_id": external_id,
        },
    )


def _claim(web: TestClient, **request: Any) -> dict[str, Any]:
    """Create a claim and return the accepted claim resource."""

    response = _post_claim(web, **request)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def _submit(
    web: TestClient,
    *,
    claim_id: str | None = None,
    creator_x_id: str = "123",
    key: str = "submission-key-0001",
    external_id: str | None = None,
    campaign_id: str = "campaign",
) -> httpx.Response:
    return web.post(
        "/api/v1/submissions",
        headers={"Idempotency-Key": key},
        json={
            "campaign_id": campaign_id,
            "tweet_id": "999",
            "claim_id": claim_id,
            "creator_x_id": creator_x_id,
            "external_id": external_id,
        },
    )


def _refusal(code: str, message: str, *, retryable: bool = False) -> dict[str, object]:
    return {"code": code, "message": message, "retryable": retryable}


def _error_envelope(response: httpx.Response) -> tuple[int, dict[str, object]]:
    """Return the status and error of a response whose body is only the error envelope."""

    body = response.json()
    assert list(body) == ["error"], body
    return response.status_code, body["error"]


def test_miner_api_does_not_require_the_campaign_feed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "bitcast_x.miner.web.load_wallet",
        lambda _settings: SimpleNamespace(hotkey=SimpleNamespace(ss58_address=MINER)),
    )

    apps = build_miner_api(
        Settings(
            _env_file=None,
            state_dir=tmp_path,
            public_ip="203.0.113.10",
            miner_api_token=INTERNAL_TOKEN,
            campaign_feed_url=None,
        )
    )

    assert apps.protocol is None


def test_miner_api_refuses_to_start_when_validators_cannot_reach_its_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rejected advertisement while the chain points elsewhere must not report ready."""

    class Chain:
        closed = False

        async def advertise_endpoint(self, _wallet: object, *, ip: str, port: int) -> None:
            raise ChainOperationError("endpoint advertisement failed: ServingRateLimitExceeded")

        async def metagraph(self) -> SimpleNamespace:
            return SimpleNamespace(by_hotkey={MINER: SimpleNamespace(axon="198.51.100.7:8093")}.get)

        async def close(self) -> None:
            self.closed = True

    chain = Chain()
    monkeypatch.setattr(
        "bitcast_x.miner.web.load_wallet",
        lambda _settings: SimpleNamespace(hotkey=SimpleNamespace(ss58_address=MINER)),
    )
    monkeypatch.setattr(
        "bitcast_x.miner.web.build_sdk", AsyncMock(return_value=(chain, SimpleNamespace()))
    )
    apps = build_miner_api(
        Settings(
            _env_file=None,
            state_dir=tmp_path,
            public_ip="203.0.113.10",
            miner_api_token=INTERNAL_TOKEN,
        )
    )

    with pytest.raises(ChainOperationError, match="ServingRateLimitExceeded"), TestClient(apps.api):
        pass
    assert chain.closed is True


def test_miner_env_example_states_the_enforced_token_length() -> None:
    template = Path("config/miner.env.example").read_text()

    assert "at least 64 characters" in template
    create_control_app(lambda: None, None, "a" * 64)  # type: ignore[arg-type,return-value]
    with pytest.raises(ValueError, match="at least 256 bits"):
        create_control_app(lambda: None, None, "a" * 63)  # type: ignore[arg-type,return-value]


def test_application_api_requires_internal_bearer_token(tmp_path: Path) -> None:
    web = build_client(tmp_path)
    del web.headers["Authorization"]

    response = web.get("/api/v1/campaigns")

    assert _error_envelope(response) == (
        401,
        _refusal("invalid_authentication", "Invalid or missing credentials."),
    )
    assert response.headers["www-authenticate"] == "Bearer"
    assert web.get("/health").status_code == 200


def test_split_listeners_keep_the_token_api_off_the_protocol_port(tmp_path: Path) -> None:
    api = build_client(tmp_path, split_protocol=True)
    protocol = TestClient(
        create_miner_app(
            miner_hotkey=MINER,
            provider=lambda _request, _caller: None,  # type: ignore[arg-type,return-value]
            authorize_validator=lambda _hotkey: _authorized(),
        ),
        headers=AUTH_HEADERS,
    )

    assert api.get("/api/v1/campaigns").status_code == 200
    assert api.get("/health").status_code == 404
    assert protocol.get("/health").status_code == 200
    assert protocol.get("/api/v1/campaigns").status_code == 404


def test_openapi_pins_the_public_v1_route_and_auth_contract(tmp_path: Path) -> None:
    web = build_client(tmp_path)

    schema = web.get("/api/v1/openapi.json").json()
    methods = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
    }

    assert methods == {
        ("GET", "/api/v1/qualification"),
        ("GET", "/api/v1/ecosystems"),
        ("GET", "/api/v1/leaderboard"),
        ("GET", "/api/v1/campaigns"),
        ("GET", "/api/v1/campaigns/{campaign_id}"),
        ("GET", "/api/v1/campaigns/{campaign_id}/eligibility/{creator_x_id}"),
        ("GET", "/api/v1/campaigns/{campaign_id}/tweets"),
        ("GET", "/api/v1/claims"),
        ("POST", "/api/v1/claims"),
        ("GET", "/api/v1/claims/{claim_id}"),
        ("GET", "/api/v1/submissions"),
        ("POST", "/api/v1/submissions"),
        ("GET", "/api/v1/submissions/{submission_id}"),
    }
    assert schema["security"] == [{"BearerAuth": []}]
    assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
    for path in ("/api/v1/claims", "/api/v1/submissions"):
        parameters = schema["paths"][path]["post"]["parameters"]
        idempotency = next(item for item in parameters if item["name"] == "Idempotency-Key")
        assert idempotency["in"] == "header"
        assert idempotency["required"] is True


def test_campaigns_and_ecosystems_respect_configured_filter(tmp_path: Path) -> None:
    web = build_client(tmp_path, enabled_ecosystems=("tao",))

    campaigns = web.get("/api/v1/campaigns").json()["items"]
    ecosystems = web.get("/api/v1/ecosystems").json()["items"]

    assert campaigns[0]["campaign_id"] == "campaign"
    assert [item["ecosystem_id"] for item in ecosystems] == ["tao"]
    assert web.get("/api/v1/campaigns").headers["cache-control"] == "no-store"


def test_leaderboard_is_limited_to_enabled_ecosystems(tmp_path: Path) -> None:
    web = build_client(tmp_path, enabled_ecosystems=("tao",))

    response = web.get("/api/v1/leaderboard?ecosystem_id=tao&limit=25&offset=50")

    assert response.status_code == 200
    assert response.json()["ecosystem_ids"] == ["tao"]
    assert response.json()["accounts"] == [
        {
            "rank": 1,
            "username": "creator",
            "score": 0.9,
            "scores": {"tao": 0.9},
        }
    ]


def test_eligibility_cannot_expand_beyond_enabled_ecosystems(tmp_path: Path) -> None:
    class HyperliquidOnlyEligibility(Results):
        async def eligibility(self, campaign_id: str, creator_x_id: str) -> dict[str, Any]:
            result = await super().eligibility(campaign_id, creator_x_id)
            result["eligible_ecosystems"][0]["eligible"] = False
            result["badges"] = [
                {"ecosystem_id": "hyperliquid", "label": "Hyperliquid"},
            ]
            return result

    web = build_client(
        tmp_path,
        enabled_ecosystems=("tao",),
        results_client=HyperliquidOnlyEligibility(),
    )

    eligibility = web.get("/api/v1/campaigns/campaign/eligibility/123")

    assert eligibility.status_code == 200
    assert eligibility.json()["eligible"] is False
    assert eligibility.json()["claim_eligible"] is False
    assert eligibility.json()["eligible_if_published_now"] is False
    assert eligibility.json()["eligible_ecosystems"] == [
        {"ecosystem_id": "tao", "eligible": False, "rank": 7, "cutoff": 100}
    ]
    assert eligibility.json()["badges"] == []
    assert eligibility.json()["reason"] == "creator_not_eligible"

    assert _error_envelope(_post_claim(web)) == (
        400,
        _refusal("creator_not_eligible", "creator is not eligible to claim this campaign"),
    )


def test_claim_and_submission_are_durable_and_recoverable(tmp_path: Path) -> None:
    web = build_client(tmp_path)
    claim = _claim(web)

    assert claim["usability"]["safe_to_post"] is True
    assert claim["commitment"]["block"] == 100
    assert web.get(f"/api/v1/claims/{claim['claim_id']}").json() == claim

    submission = _submit(web, claim_id=claim["claim_id"], external_id="creator-submission-1")

    assert submission.status_code == 200
    assert submission.json()["status"] == "tweet_received"
    assert submission.json()["claim_commitment"]["status"] == "finalized"
    assert submission.json()["submission_commitment"]["status"] == "queued"
    assert (
        submission.json()["claim_commitment"]["batch_hash"]
        != submission.json()["submission_commitment"]["batch_hash"]
    )
    assert web.get("/api/v1/claims").json()["items"][0]["claim_id"] == claim["claim_id"]
    assert web.get("/api/v1/submissions").json()["items"][0]["tweet_id"] == "999"


def test_preclaim_submission_remains_pinned_to_claim_snapshot(tmp_path: Path) -> None:
    results = Results()
    web = build_client(tmp_path, results_client=results)
    claim = _claim(web)
    results.campaign_record = {
        **results.campaign_record,
        "campaign_snapshot_id": "sha256-new-snapshot",
    }

    submission = _submit(web, claim_id=claim["claim_id"])

    assert submission.status_code == 200
    assert submission.json()["campaign_snapshot_id"] == "sha256-snapshot"


def test_direct_submission_accepts_existing_post_during_evaluation_grace(
    tmp_path: Path,
) -> None:
    web = build_client(tmp_path, results_client=EvaluatingDirectResults())

    response = _submit(web)

    assert response.status_code == 200
    assert response.json()["claim_id"] is None
    assert response.json()["status"] == "verification_pending"
    assert response.json()["submission_commitment"]["status"] == "finalized"
    assert response.json()["submission_commitment"]["block"] == 100


def test_repeated_submission_mapping_returns_the_existing_receipt(tmp_path: Path) -> None:
    """A repeated tweet mapping resolves to its receipt; a reused key must repeat its input."""

    web = build_client(tmp_path, results_client=DirectResults())

    first = _submit(web)
    replay = _submit(web)
    new_key = _submit(web, key="submission-key-0002")
    changed_input = _submit(web, external_id="changed")

    assert [first.status_code, replay.status_code, new_key.status_code] == [200] * 3
    assert replay.json()["submission_id"] == first.json()["submission_id"]
    assert new_key.json()["submission_id"] == first.json()["submission_id"]
    assert _error_envelope(changed_input) == (
        409,
        _refusal("idempotency_conflict", "idempotency key was reused with different input"),
    )
    assert len(web.get("/api/v1/submissions").json()["items"]) == 1


def test_result_sync_records_final_results_only_for_pending_submissions(
    tmp_path: Path,
) -> None:
    class FinalResults(Results):
        def __init__(self) -> None:
            self.reads = 0
            self.final: dict[str, str] = {}

        async def submissions(self, **_filters: object) -> list[dict[str, Any]]:
            self.reads += 1
            return [
                {"submission_id": submission_id, "status": status}
                for submission_id, status in self.final.items()
            ]

    results = FinalResults()
    engine = MinerEngine(
        miner_hotkey=MINER,
        store=MinerStore(tmp_path / "miner.sqlite3"),
        submitter=Submitter(),
    )
    sdk = MinerSdk(engine)
    service = MinerControlService(
        sdk,
        results_client=results,  # type: ignore[arg-type]
        commit_timeout_seconds=5,
    )

    asyncio.run(service.sync_submission_results())
    assert results.reads == 0

    attributed, rejected, unresolved = [
        sdk.submit_tweet(campaign_id="campaign", tweet_id=tweet_id, claim_id=None, creator_x_id="1")
        for tweet_id in ("1", "2", "3")
    ]
    asyncio.run(engine.commit_ready(force=True))
    queued = sdk.submit_tweet(campaign_id="campaign", tweet_id="4", claim_id=None, creator_x_id="1")
    results.final = {
        attributed: "attributed",
        rejected: "rejected",
        unresolved: "verification_pending",
        queued: "attributed",
    }

    asyncio.run(service.sync_submission_results())

    assert results.reads == 1
    assert sdk.submission_status(attributed) is EventStatus.ATTRIBUTED
    assert sdk.submission_status(rejected) is EventStatus.REJECTED
    assert sdk.submission_status(unresolved) is EventStatus.VERIFICATION_PENDING
    assert sdk.submission_status(queued) is EventStatus.TWEET_RECEIVED


def test_submission_listing_reads_claims_in_one_query_and_each_batch_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = MinerEngine(
        miner_hotkey=MINER,
        store=MinerStore(tmp_path / "miner.sqlite3"),
        submitter=Submitter(),
    )
    sdk = MinerSdk(engine)
    service = MinerControlService(
        sdk,
        results_client=Results(),  # type: ignore[arg-type]
        commit_timeout_seconds=5,
    )
    claim_ids = [
        sdk.create_claim(campaign_id="campaign", creator_x_id="123", draft=f"draft {index}")
        for index in range(3)
    ]
    asyncio.run(engine.commit_ready(force=True))
    for index, claim_id in enumerate(claim_ids):
        sdk.submit_tweet(
            campaign_id="campaign",
            tweet_id=str(900 + index),
            claim_id=claim_id,
            creator_x_id="123",
        )
    asyncio.run(engine.commit_ready(force=True))
    expected = {
        claim_id: receipt["commitment"]
        for claim_id in claim_ids
        if (receipt := engine.store.receipt(claim_id)) is not None
    }
    calls = {"receipts": 0, "batch_parses": 0}
    receipts = MinerStore.receipts
    parse = CommittedBatch.model_validate_json

    def counting_receipts(store: MinerStore, **filters: Any) -> list[dict[str, object]]:
        calls["receipts"] += 1
        return receipts(store, **filters)

    def counting_parse(*args: Any, **kwargs: Any) -> CommittedBatch:
        calls["batch_parses"] += 1
        return parse(*args, **kwargs)

    monkeypatch.setattr(MinerStore, "receipts", counting_receipts)
    monkeypatch.setattr(CommittedBatch, "model_validate_json", counting_parse)

    submissions = asyncio.run(service.submissions())

    assert calls == {"receipts": 2, "batch_parses": 2}
    assert {item["claim_id"]: item["claim_commitment"] for item in submissions} == expected
    assert sorted(commitment["event_index"] for commitment in expected.values()) == [0, 1, 2]


def test_idempotency_replays_same_claim_and_rejects_changed_input(tmp_path: Path) -> None:
    web = build_client(tmp_path)
    first = _claim(web)
    second = _claim(web)

    assert second["claim_id"] == first["claim_id"]
    assert _error_envelope(_post_claim(web, draft="Changed")) == (
        409,
        _refusal("idempotency_conflict", "idempotency key was reused with different input"),
    )


def test_submission_requires_matching_safe_claim_and_creator(tmp_path: Path) -> None:
    """Refuse a claim made for another creator or campaign.

    The unsafe-claim half is ``test_unsafe_claim_submission_is_refused_with_its_code``.
    """

    class TwoCampaignResults(Results):
        async def campaign(self, campaign_id: str) -> dict[str, Any]:
            if campaign_id == "other-campaign":
                return {**self.campaign_record, "campaign_id": campaign_id}
            return await super().campaign(campaign_id)

    web = build_client(tmp_path, results_client=TwoCampaignResults())
    claim = _claim(web)

    other_creator = _submit(web, claim_id=claim["claim_id"], creator_x_id="456")
    other_campaign = _submit(
        web,
        claim_id=claim["claim_id"],
        key="submission-key-0002",
        campaign_id="other-campaign",
    )

    assert _error_envelope(other_creator) == (
        400,
        _refusal("invalid_request", "claim creator does not match submission creator"),
    )
    assert _error_envelope(other_campaign) == (
        400,
        _refusal("invalid_request", "claim campaign does not match submission campaign"),
    )
    assert web.get("/api/v1/submissions").json()["items"] == []


def test_validation_errors_use_stable_envelope_without_echoing_input(tmp_path: Path) -> None:
    web = build_client(tmp_path)

    validation = web.post(
        "/api/v1/claims",
        json={"campaign_id": "campaign", "creator_x_id": "123", "draft": "private"},
    )

    assert _error_envelope(validation) == (
        422,
        _refusal("invalid_request", "Request validation failed."),
    )
    assert "private" not in validation.text


def test_claim_too_long_once_normalized_is_a_validation_error(tmp_path: Path) -> None:
    web = build_client(tmp_path)

    # 1,200 characters, but 21,600 once normalized as the claim's reveal stores it.
    response = _post_claim(web, draft="\ufdfa" * 1_200)

    assert _error_envelope(response) == (
        422,
        _refusal("invalid_request", "Request validation failed."),
    )
    assert web.get("/api/v1/claims").json()["items"] == []


def test_every_operation_error_code_has_an_http_status() -> None:
    assert set(_ERROR_STATUS) == set(ErrorCode)


def _get(path: str) -> Callable[[TestClient], httpx.Response]:
    return lambda web: web.get(path)


@pytest.mark.parametrize(
    ("options", "send", "status", "error"),
    [
        pytest.param(
            {},
            _get("/api/v1/campaigns/missing"),
            404,
            _refusal("campaign_not_found", "campaign not found"),
            id="campaign-not-found",
        ),
        pytest.param(
            {},
            _get("/api/v1/campaigns/missing/eligibility/123"),
            404,
            _refusal("campaign_not_found", "campaign is not available to this miner"),
            id="eligibility-for-missing-campaign",
        ),
        pytest.param(
            {"results_client": UnavailableCampaignResults()},
            _get("/api/v1/campaigns/campaign"),
            503,
            _refusal(
                "central_api_unavailable",
                "The central miner API cannot currently verify this request.",
                retryable=True,
            ),
            id="campaign-lookup-only-treats-central-404-as-not-found",
        ),
        pytest.param(
            {},
            _get("/api/v1/claims/does-not-exist"),
            404,
            _refusal("claim_not_found", "claim not found"),
            id="claim-not-found",
        ),
        pytest.param(
            {},
            _get("/api/v1/submissions/does-not-exist"),
            404,
            _refusal("submission_not_found", "submission not found"),
            id="submission-not-found",
        ),
        pytest.param(
            {},
            lambda web: _submit(web, claim_id="ab" * 16),
            404,
            _refusal("claim_not_found", "submission claim_id does not belong to this miner"),
            id="submission-with-foreign-claim",
        ),
        *(
            pytest.param(
                {"enabled_ecosystems": ("tao",)},
                _get(f"{route}?ecosystem_id=hyperliquid"),
                400,
                _refusal(
                    "ecosystem_not_enabled", "requested ecosystem is not enabled by this miner"
                ),
                id=f"{route.removeprefix('/api/v1/')}-outside-enabled-ecosystems",
            )
            for route in (
                "/api/v1/campaigns",
                "/api/v1/leaderboard",
                "/api/v1/claims",
                "/api/v1/submissions",
            )
        ),
        pytest.param(
            {"results_client": DirectResults()},
            _post_claim,
            400,
            _refusal("invalid_request", "campaign does not accept claims"),
            id="uncoded-refusal-is-invalid-request",
        ),
        pytest.param(
            {"results_client": IneligibleDirectResults()},
            _submit,
            400,
            _refusal("creator_not_eligible", "creator is not eligible to submit to this campaign"),
            id="direct-submission-by-ineligible-creator",
        ),
        pytest.param(
            {"submitter": LateSubmitter(), "results_client": EvaluatingDirectResults()},
            _submit,
            409,
            _refusal(
                "submission_deadline_passed",
                "submission deadline passed before on-chain commitment",
            ),
            id="grace-commit-after-scoring-close",
        ),
        pytest.param(
            {
                "submitter": SlowSubmitter(),
                "timeout": 0.01,
                "results_client": EvaluatingDirectResults(),
            },
            _submit,
            503,
            _refusal(
                "submission_commitment_pending",
                "submission commitment was not confirmed before request timeout",
                retryable=True,
            ),
            id="grace-commit-unconfirmed-before-timeout",
        ),
    ],
)
def test_single_request_refusals_keep_their_codes_statuses_and_messages(
    tmp_path: Path,
    options: dict[str, Any],
    send: Callable[[TestClient], httpx.Response],
    status: int,
    error: dict[str, object],
) -> None:
    web = build_client(tmp_path, **options)

    assert _error_envelope(send(web)) == (status, error)


def test_unsafe_claim_submission_is_refused_with_its_code(tmp_path: Path) -> None:
    web = build_client(tmp_path, submitter=SlowSubmitter(), timeout=0.05)
    claim = _claim(web)

    response = _submit(web, claim_id=claim["claim_id"])

    assert _error_envelope(response) == (
        400,
        _refusal("claim_not_safe_to_post", "claim is not safe to post"),
    )


def test_full_pending_queue_is_refused_with_its_code(tmp_path: Path) -> None:
    web = build_client(
        tmp_path,
        submitter=FailingSubmitter(),
        policy=BatchPolicy(max_age_seconds=5, max_pending_events=1),
    )
    first = _post_claim(web, draft="First")

    second = _post_claim(web, key="claim-key-0002", draft="Second")

    assert first.status_code == 503
    assert _error_envelope(second) == (
        400,
        _refusal("queue_capacity_exhausted", "miner pending queue capacity is exhausted"),
    )


@pytest.mark.parametrize(
    ("failure", "status", "error", "retry_after"),
    [
        pytest.param(
            _central_error(403, "/api/v2/miners/x/campaigns"),
            403,
            _refusal(
                "miner_not_registered",
                "The miner hotkey is not currently registered on subnet 93.",
            ),
            None,
            id="registration-403",
        ),
        pytest.param(
            _central_error(429, "/api/v2/miners/x/campaigns", {"Retry-After": "30"}),
            429,
            _refusal(
                "central_api_rate_limited",
                "The central miner API rate limit was reached.",
                retryable=True,
            ),
            "30",
            id="rate-limited-429",
        ),
        pytest.param(
            _central_error(503, "/api/v2/miners/x/campaigns", {"Retry-After": "5"}),
            503,
            _refusal(
                "central_api_unavailable",
                "The central miner API cannot currently verify this request.",
                retryable=True,
            ),
            "5",
            id="unavailable-503",
        ),
        pytest.param(
            _central_error(500, "/api/v2/miners/x/campaigns"),
            502,
            _refusal(
                "central_api_error",
                "The central miner API returned an unexpected response.",
                retryable=True,
            ),
            None,
            id="server-error-500",
        ),
        pytest.param(
            _central_error(400, "/api/v2/miners/x/campaigns"),
            502,
            _refusal(
                "central_api_error",
                "The central miner API returned an unexpected response.",
            ),
            None,
            id="client-error-400",
        ),
        pytest.param(
            httpx.ConnectError(
                "unreachable",
                request=httpx.Request("GET", "https://central.test/api/v2/miners/x/campaigns"),
            ),
            503,
            _refusal(
                "central_api_unavailable",
                "The central miner API is temporarily unreachable.",
                retryable=True,
            ),
            None,
            id="unreachable",
        ),
    ],
)
def test_central_failures_keep_a_stable_application_envelope(
    tmp_path: Path,
    failure: Exception,
    status: int,
    error: dict[str, object],
    retry_after: str | None,
) -> None:
    class FailingResults(Results):
        async def campaigns(self, ecosystem_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]:
            raise failure

    response = build_client(tmp_path, results_client=FailingResults()).get("/api/v1/campaigns")

    assert _error_envelope(response) == (status, error)
    assert response.headers.get("retry-after") == retry_after


def test_claim_timeout_returns_durable_pending_resource(tmp_path: Path) -> None:
    web = build_client(tmp_path, submitter=SlowSubmitter(), timeout=0.05)
    claim = _claim(web)

    assert claim["commitment"]["status"] == "queued"
    assert claim["usability"]["status"] == "pending"
    assert claim["usability"]["safe_to_post"] is False


def test_chain_failure_after_claim_persistence_is_retryable_and_deduplicated(
    tmp_path: Path,
) -> None:
    web = build_client(tmp_path, submitter=FailingSubmitter())
    request = {"key": "claim-chain-failure", "external_id": "creator-claim-chain-failure"}

    first = _post_claim(web, **request)

    assert first.status_code == 503
    assert first.json() == {
        "error": {
            "code": "chain_operation_unavailable",
            "message": "Chain operation outcome is unavailable.",
            "retryable": True,
        }
    }
    persisted = web.get(
        "/api/v1/claims?campaign_id=campaign&creator_x_id=123"
        "&external_id=creator-claim-chain-failure"
    ).json()["items"]
    assert len(persisted) == 1
    claim_id = persisted[0]["claim_id"]

    replay = _post_claim(web, **request)

    assert replay.status_code == 503
    after_replay = web.get(
        "/api/v1/claims?campaign_id=campaign&creator_x_id=123"
        "&external_id=creator-claim-chain-failure"
    ).json()["items"]
    assert [item["claim_id"] for item in after_replay] == [claim_id]


def test_unqualified_miner_cannot_create_operations(tmp_path: Path) -> None:
    web = build_client(tmp_path, qualified=False)

    response = _post_claim(web)

    assert _error_envelope(response) == (
        403,
        _refusal("miner_not_qualified", "miner is not qualified: conviction_below_minimum"),
    )
    assert web.get("/api/v1/claims").json()["items"] == []


def test_finalized_events_survive_restart_for_validator_fetch(tmp_path: Path) -> None:
    database = tmp_path / "miner.sqlite3"
    web = build_client(tmp_path)
    claim = _claim(web)
    submission = _submit(web, claim_id=claim["claim_id"]).json()

    restarted = MinerEngine(
        miner_hotkey=MINER,
        store=MinerStore(database),
        submitter=Submitter(),
        policy=BatchPolicy(max_age_seconds=5),
    )
    asyncio.run(restarted.commit_ready(force=True))
    page = asyncio.run(
        restarted.batch_page(BatchPageRequest(after_sequence=0, max_batches=50), "validator")
    )

    assert page.next_sequence == 2
    assert page.batches[0].batch["events"][0]["claim_id"] == claim["claim_id"]
    assert page.batches[1].batch["events"][0]["submission_id"] == submission["submission_id"]
    assert page.batches[1].batch["events"][0]["creator_x_id"] == "123"
    consumed = restarted.store.receipt(claim["claim_id"])
    assert consumed is not None
    assert consumed["status"] == "consumed"
    assert consumed["consumed_by_submission_id"] == submission["submission_id"]


class CountingResults(Results):
    """Count central campaign and eligibility reads made by one operation."""

    def __init__(self) -> None:
        self.campaign_calls = 0
        self.eligibility_calls = 0

    async def campaign(self, campaign_id: str) -> dict[str, Any]:
        self.campaign_calls += 1
        return await super().campaign(campaign_id)

    async def eligibility(self, campaign_id: str, creator_x_id: str) -> dict[str, Any]:
        self.eligibility_calls += 1
        return await super().eligibility(campaign_id, creator_x_id)


class CountingDirectResults(CountingResults, DirectResults):
    """Counting double for an exclusive direct campaign."""


def test_claim_fetches_campaign_once_and_fresh_eligibility(tmp_path: Path) -> None:
    results = CountingResults()
    web = build_client(tmp_path, results_client=results)

    claim = _claim(web)

    assert results.campaign_calls == 1
    assert results.eligibility_calls == 1
    assert claim["usability"]["safe_to_post"] is True


def test_direct_submission_needs_no_claim_and_reads_campaign_and_eligibility_once(
    tmp_path: Path,
) -> None:
    results = CountingDirectResults()
    web = build_client(tmp_path, results_client=results)

    response = _submit(web)

    assert response.status_code == 200
    assert response.json()["claim_id"] is None
    assert response.json()["submission_commitment"]["status"] == "queued"
    assert results.campaign_calls == 1
    assert results.eligibility_calls == 1
