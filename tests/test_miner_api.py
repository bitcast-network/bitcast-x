"""Offline HTTP tests for the generic authenticated miner API."""

import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from bitcast_x.campaigns import CampaignRecord
from bitcast_x.miner import BatchPolicy, FinalizedCommitment, MinerEngine, MinerSdk, MinerStore
from bitcast_x.miner.api import create_control_app
from bitcast_x.miner.control import MinerControlService
from bitcast_x.miner.engine import CapacityBudget
from bitcast_x.protocol import CommitmentEnvelope, CommitmentPosition
from bitcast_x.transport import BatchPageRequest, create_miner_app

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


class Feed:
    async def fetch_campaigns(self) -> tuple[CampaignRecord, ...]:
        return ()

    async def close(self) -> None:
        return None


class Results:
    """Central miner API double with one open preclaim campaign."""

    campaign_record = {
        "campaign_id": "campaign",
        "campaign_snapshot_id": "sha256-snapshot",
        "ecosystem_ids": ["tao", "hyperliquid"],
        "status": "open",
        "protocol": {"version": 2, "submission_mode": "preclaim"},
        "access": {"mode": "open", "exclusive_miner_hotkey": None},
        "capabilities": {
            "can_check_eligibility": True,
            "can_claim": True,
            "can_submit": True,
            "can_view_results": True,
            "requires_claim": True,
            "is_exclusive_to_this_miner": False,
        },
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

    async def campaign(self, campaign_id: str) -> dict[str, Any]:
        if campaign_id != "campaign":
            raise FakeNotFoundError
        return self.campaign_record

    async def eligibility(self, campaign_id: str, creator_x_id: str) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
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
        }

    async def campaign_tweets(
        self, campaign_id: str, ecosystem_ids: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        return {"campaign_id": campaign_id, "tweets": [], "ecosystems": ecosystem_ids}

    async def submission(self, submission_id: str) -> dict[str, Any]:
        return {"submission_id": submission_id, "status": "verification_pending"}

    async def submissions(self, **_filters: object) -> list[dict[str, Any]]:
        return []


class FakeNotFoundResponse:
    status_code = 404


class FakeNotFoundError(Exception):
    response = FakeNotFoundResponse()


def build_client(
    tmp_path: Path,
    *,
    submitter: Submitter | None = None,
    timeout: float = 5,
    enabled_ecosystems: tuple[str, ...] = ("tao", "hyperliquid"),
    qualified: bool = True,
    results_client: Results | None = None,
) -> TestClient:
    engine = MinerEngine(
        miner_hotkey=MINER,
        store=MinerStore(tmp_path / "miner.sqlite3"),
        submitter=submitter or Submitter(),
        policy=BatchPolicy(max_age_seconds=5),
    )

    async def qualification() -> dict[str, object]:
        return {
            "eligible": qualified,
            "reason": "eligible" if qualified else "conviction_below_minimum",
        }

    service = MinerControlService(
        MinerSdk(engine, qualification_provider=qualification),
        Feed(),
        timeout,
        results_client=results_client or Results(),  # type: ignore[arg-type]
        enabled_ecosystem_ids=enabled_ecosystems,
    )
    protocol = create_miner_app(
        miner_hotkey=MINER,
        provider=engine.batch_page,
        authorize_validator=lambda _hotkey: _authorized(),
    )
    return TestClient(
        create_control_app(lambda: service, protocol, INTERNAL_TOKEN),
        headers=AUTH_HEADERS,
    )


async def _authorized() -> bool:
    return True


def _claim(web: TestClient, *, key: str = "claim-key-0001") -> dict[str, Any]:
    response = web.post(
        "/api/v1/claims",
        headers={"Idempotency-Key": key},
        json={
            "campaign_id": "campaign",
            "creator_x_id": "123",
            "draft": "Exact draft",
            "external_id": "creator-claim-1",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_application_api_requires_internal_bearer_token(tmp_path: Path) -> None:
    web = build_client(tmp_path)
    del web.headers["Authorization"]

    response = web.get("/api/v1/campaigns")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_authentication"
    assert response.headers["www-authenticate"] == "Bearer"
    assert web.get("/health").status_code == 200


def test_campaigns_and_ecosystems_respect_configured_filter(tmp_path: Path) -> None:
    web = build_client(tmp_path, enabled_ecosystems=("tao",))

    campaigns = web.get("/api/v1/campaigns").json()["items"]
    ecosystems = web.get("/api/v1/ecosystems").json()["items"]

    assert campaigns[0]["campaign_id"] == "campaign"
    assert [item["ecosystem_id"] for item in ecosystems] == ["tao"]
    assert web.get("/api/v1/campaigns").headers["cache-control"] == "no-store"
    rejected = web.get("/api/v1/campaigns?ecosystem_id=hyperliquid")
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "ecosystem_not_enabled"


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

    claim = web.post(
        "/api/v1/claims",
        headers={"Idempotency-Key": "claim-key-0001"},
        json={"campaign_id": "campaign", "creator_x_id": "123", "draft": "Exact draft"},
    )
    assert claim.status_code == 400
    assert claim.json()["error"]["code"] == "creator_not_eligible"


def test_claim_and_submission_are_durable_and_recoverable(tmp_path: Path) -> None:
    web = build_client(tmp_path)
    claim = _claim(web)

    assert claim["usability"]["safe_to_post"] is True
    assert claim["commitment"]["block"] == 100
    assert web.get(f"/api/v1/claims/{claim['claim_id']}").json() == claim

    submission = web.post(
        "/api/v1/submissions",
        headers={"Idempotency-Key": "submission-key-0001"},
        json={
            "campaign_id": "campaign",
            "tweet_id": "999",
            "claim_id": claim["claim_id"],
            "creator_x_id": "123",
            "external_id": "creator-submission-1",
        },
    )

    assert submission.status_code == 200
    assert submission.json()["status"] == "tweet_received"
    assert web.get("/api/v1/claims").json()["items"][0]["claim_id"] == claim["claim_id"]
    assert web.get("/api/v1/submissions").json()["items"][0]["tweet_id"] == "999"


def test_idempotency_replays_same_claim_and_rejects_changed_input(tmp_path: Path) -> None:
    web = build_client(tmp_path)
    first = _claim(web)
    second = _claim(web)

    assert second["claim_id"] == first["claim_id"]
    conflict = web.post(
        "/api/v1/claims",
        headers={"Idempotency-Key": "claim-key-0001"},
        json={"campaign_id": "campaign", "creator_x_id": "123", "draft": "Changed"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_submission_requires_matching_safe_claim_and_creator(tmp_path: Path) -> None:
    web = build_client(tmp_path)
    claim = _claim(web)

    response = web.post(
        "/api/v1/submissions",
        headers={"Idempotency-Key": "submission-key-0001"},
        json={
            "campaign_id": "campaign",
            "tweet_id": "999",
            "claim_id": claim["claim_id"],
            "creator_x_id": "456",
        },
    )

    assert response.status_code == 400
    assert "creator" in response.json()["error"]["message"]


def test_claim_timeout_returns_durable_pending_resource(tmp_path: Path) -> None:
    web = build_client(tmp_path, submitter=SlowSubmitter(), timeout=0.05)
    claim = _claim(web)

    assert claim["commitment"]["status"] == "queued"
    assert claim["usability"]["status"] == "pending"
    assert claim["usability"]["safe_to_post"] is False


def test_unqualified_miner_cannot_create_operations(tmp_path: Path) -> None:
    web = build_client(tmp_path, qualified=False)

    response = web.post(
        "/api/v1/claims",
        headers={"Idempotency-Key": "claim-key-0001"},
        json={"campaign_id": "campaign", "creator_x_id": "123", "draft": "Exact draft"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "miner_not_qualified"
    assert web.get("/api/v1/claims").json()["items"] == []


def test_finalized_events_survive_restart_for_validator_fetch(tmp_path: Path) -> None:
    database = tmp_path / "miner.sqlite3"
    web = build_client(tmp_path)
    claim = _claim(web)
    submission = web.post(
        "/api/v1/submissions",
        headers={"Idempotency-Key": "submission-key-0001"},
        json={
            "campaign_id": "campaign",
            "tweet_id": "999",
            "claim_id": claim["claim_id"],
            "creator_x_id": "123",
        },
    ).json()

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
    consumed = restarted.store.receipt(claim["claim_id"])
    assert consumed is not None
    assert consumed["status"] == "consumed"
    assert consumed["consumed_by_submission_id"] == submission["submission_id"]
