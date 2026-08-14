"""Offline HTTP tests for the generic authenticated miner API."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from bitcast_x.campaigns import CampaignRecord
from bitcast_x.miner import BatchPolicy, FinalizedCommitment, MinerEngine, MinerSdk, MinerStore
from bitcast_x.miner.api import create_control_app
from bitcast_x.miner.control import MinerControlService
from bitcast_x.miner.engine import CapacityBudget
from bitcast_x.protocol import CommitmentEnvelope, CommitmentPosition
from bitcast_x.transport import BatchPageRequest, create_miner_app

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
INTERNAL_TOKEN = "test-internal-token-that-is-at-least-32-chars"  # noqa: S105


class Submitter:
    """Finalizing in-memory chain adapter for HTTP tests."""

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
    """Chain adapter that exceeds the control API timeout."""

    async def submit(self, envelope: CommitmentEnvelope) -> FinalizedCommitment:
        await asyncio.sleep(1)
        return await super().submit(envelope)


class Feed:
    """Minimal campaign source for control API tests."""

    async def fetch_campaigns(self) -> tuple[CampaignRecord, ...]:
        return (
            CampaignRecord.model_validate(
                {
                    "access": {
                        "campaign_id": "campaign",
                        "mechanism_id": 1,
                        "mining_protocol": "preclaim_v2",
                        "scoring_close_block": 100,
                    },
                    "display": "Campaign",
                    "brief": "Write an original post.",
                    "pools": ["tao", "hyperliquid"],
                    "opens_at": "2026-08-01T00:00:00Z",
                    "closes_at": "2026-08-10T00:00:00Z",
                    "reward_pool_usd": "1000",
                }
            ),
        )

    async def close(self) -> None:
        return None


def client(
    tmp_path: Path,
    *,
    submitter: Submitter | None = None,
    timeout: float = 5,
) -> TestClient:
    """Create an app using real durable miner state and a fake chain."""

    engine = MinerEngine(
        miner_hotkey=MINER,
        store=MinerStore(tmp_path / "miner.sqlite3"),
        submitter=submitter or Submitter(),
        policy=BatchPolicy(max_age_seconds=5),
    )
    service = MinerControlService(MinerSdk(engine), Feed(), timeout)  # type: ignore[arg-type]
    protocol = create_miner_app(
        miner_hotkey=MINER,
        provider=engine.batch_page,
        authorize_validator=lambda _hotkey: _authorized(),
    )
    return TestClient(
        create_control_app(lambda: service, protocol, INTERNAL_TOKEN),
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
    )


async def _authorized() -> bool:
    return True


def test_control_api_requires_internal_bearer_token(tmp_path: Path) -> None:
    web = client(tmp_path)
    del web.headers["Authorization"]

    response = web.get("/api/campaigns")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_validator_health_does_not_use_internal_bearer_token(tmp_path: Path) -> None:
    web = client(tmp_path)
    del web.headers["Authorization"]

    assert web.get("/health").status_code == 200


def test_campaign_listing_uses_public_metadata_source(tmp_path: Path) -> None:
    response = client(tmp_path).get("/api/campaigns")

    assert response.status_code == 200
    assert response.json()[0]["access"]["campaign_id"] == "campaign"
    assert response.json()[0]["pools"] == ["tao", "hyperliquid"]


def test_claim_is_finalized_and_submission_is_acknowledged_durably(tmp_path: Path) -> None:
    web = client(tmp_path)
    claim = web.post(
        "/api/claims",
        json={"campaign_id": "campaign", "creator_x_id": "123", "draft": "Exact draft"},
    )
    assert claim.status_code == 200
    assert claim.json()["status"] == "safe_to_post"

    submission = web.post(
        "/api/submissions",
        json={
            "campaign_id": "campaign",
            "tweet_id": "999",
            "claim_id": claim.json()["claim_id"],
        },
    )
    assert submission.status_code == 200
    assert submission.json()["status"] == "tweet_received"
    pending = web.get("/api/submissions")
    assert pending.status_code == 200
    assert pending.json()[0]["submission_id"] == submission.json()["submission_id"]


def test_finalized_events_survive_restart_for_validator_fetch(tmp_path: Path) -> None:
    database = tmp_path / "miner.sqlite3"
    web = client(tmp_path)
    claim = web.post(
        "/api/claims",
        json={"campaign_id": "campaign", "creator_x_id": "123", "draft": "Exact draft"},
    ).json()
    submission = web.post(
        "/api/submissions",
        json={
            "campaign_id": "campaign",
            "tweet_id": "999",
            "claim_id": claim["claim_id"],
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


def test_repeated_submission_returns_same_durable_id(tmp_path: Path) -> None:
    web = client(tmp_path)
    payload = {"campaign_id": "campaign", "tweet_id": "999", "claim_id": None}

    first = web.post("/api/submissions", json=payload)
    second = web.post("/api/submissions", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(web.get("/api/submissions").json()) == 1


async def test_submission_list_retains_local_rows_when_result_lookup_fails(
    tmp_path: Path,
) -> None:
    engine = MinerEngine(
        miner_hotkey=MINER,
        store=MinerStore(tmp_path / "miner.sqlite3"),
        submitter=Submitter(),
        policy=BatchPolicy(max_age_seconds=5),
    )
    sdk = MinerSdk(engine)
    submission_id = sdk.submit_tweet(
        campaign_id="campaign",
        tweet_id="999",
        claim_id=None,
    )
    results_client = AsyncMock()
    results_client.submission.side_effect = RuntimeError("central result unavailable")
    service = MinerControlService(
        sdk,
        Feed(),  # type: ignore[arg-type]
        5,
        results_client=results_client,
    )

    submissions = await service.submissions()

    assert submissions == [
        {
            "submission_id": submission_id,
            "campaign_id": "campaign",
            "tweet_id": "999",
            "claim_id": None,
            "status": "tweet_received",
            "created_ns": submissions[0]["created_ns"],
        }
    ]


def test_claim_timeout_returns_durable_waiting_status(tmp_path: Path) -> None:
    web = client(tmp_path, submitter=SlowSubmitter(), timeout=0.05)
    response = web.post(
        "/api/claims",
        json={"campaign_id": "campaign", "creator_x_id": "123", "draft": "Exact draft"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "waiting_for_commitment"


def test_submission_rejects_campaign_mismatch(tmp_path: Path) -> None:
    web = client(tmp_path)
    claim = web.post(
        "/api/claims",
        json={"campaign_id": "campaign", "creator_x_id": "123", "draft": "Exact draft"},
    ).json()

    response = web.post(
        "/api/submissions",
        json={
            "campaign_id": "other-campaign",
            "tweet_id": "999",
            "claim_id": claim["claim_id"],
        },
    )

    assert response.status_code == 400
    assert "does not match claim campaign" in response.json()["detail"]
