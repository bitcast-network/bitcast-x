"""Credential-free core tweet journeys through the real miner and validator code."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import bittensor as bt
import httpx
import pytest
from fixture_x import FixtureXProvider

from bitcast_x.brief_filter import BriefEvaluation
from bitcast_x.campaigns import CampaignFeed, CampaignRecord, EcosystemMap, SocialAccount
from bitcast_x.chain import ChainCommitment
from bitcast_x.errors import ProtocolError
from bitcast_x.miner import BatchPolicy, FinalizedCommitment, MinerEngine, MinerSdk, MinerStore
from bitcast_x.miner.api import create_control_app
from bitcast_x.miner.control import MinerControlService
from bitcast_x.miner.engine import CapacityBudget
from bitcast_x.protocol import (
    CampaignAccess,
    CommitmentEnvelope,
    CommitmentPosition,
    MiningProtocol,
)
from bitcast_x.qualification import (
    HistoricalQualificationChecker,
    QualificationConfig,
    QualificationReader,
)
from bitcast_x.transport import SignedMinerClient, create_miner_app
from bitcast_x.validator.ingestion import MinerEndpoint, ValidatorIngestor
from bitcast_x.validator.publishing import ShadowResultPublisher
from bitcast_x.validator.reconciliation import CampaignReconciler
from bitcast_x.validator.rewards import RewardCoordinator
from bitcast_x.validator.scoring import AttributionScorer
from bitcast_x.validator.store import ValidatorStore
from bitcast_x.x_provider import EngagementFetch, Tweet, TweetFetch

CAMPAIGN_ID = "fixture-campaign"
CREATOR_X_ID = "456"
TWEET_ID = "999"
INTERNAL_TOKEN = "a" * 64
DRAFT = (
    "I spent a week testing the wallet. Fast confirmations help, but the recovery flow "
    "is what won me over. #Launch"
)
EXCLUSIVE_TWEET = "The wallet launch is live, and the recovery flow stands out. #Launch"


class InMemoryChain:
    """Record deterministic finalized commitments for miner and validator adapters."""

    def __init__(self, miner_hotkey: str, *, first_timestamp: datetime) -> None:
        self._miner_hotkey = miner_hotkey
        self._first_timestamp = first_timestamp
        self._observations: list[ChainCommitment] = []

    async def capacity(self, _envelope: CommitmentEnvelope) -> CapacityBudget:
        return CapacityBudget(remaining_space=100, next_call_charge=1)

    async def latest(self) -> FinalizedCommitment | None:
        if not self._observations:
            return None
        latest = self._observations[-1]
        return FinalizedCommitment(
            position=CommitmentPosition(
                block=latest.block,
                extrinsic_index=latest.extrinsic_index,
            ),
            stored_envelope=latest.envelope.encode(),
        )

    async def submit(self, envelope: CommitmentEnvelope) -> FinalizedCommitment:
        sequence_offset = len(self._observations)
        position = CommitmentPosition(block=10 + sequence_offset, extrinsic_index=2)
        self._observations.append(
            ChainCommitment(
                hotkey=self._miner_hotkey,
                block=position.block,
                extrinsic_index=position.extrinsic_index,
                timestamp=self._first_timestamp + timedelta(minutes=2 * sequence_offset),
                envelope=envelope,
            )
        )
        return FinalizedCommitment(position=position, stored_envelope=envelope.encode())

    async def latest_commitment_envelope(
        self,
        hotkey: str,
        *,
        block: int | None = None,
    ) -> CommitmentEnvelope | None:
        candidates = [
            item
            for item in self._observations
            if item.hotkey == hotkey and (block is None or item.block <= block)
        ]
        return candidates[-1].envelope if candidates else None

    async def commitment_at_position(
        self,
        hotkey: str,
        position: CommitmentPosition,
    ) -> ChainCommitment:
        matches = [
            item
            for item in self._observations
            if item.hotkey == hotkey
            and item.block == position.block
            and item.extrinsic_index == position.extrinsic_index
        ]
        if len(matches) != 1:
            raise ProtocolError("claimed position does not contain matching commitment")
        return matches[0]

    async def miner_qualification_inputs(
        self,
        _miner_hotkey: str,
        *,
        block: int | None = None,
        include_self_stake: bool = False,
    ) -> tuple[str | None, str | None, int, int]:
        del block, include_self_stake
        return None, None, 0, 0

    async def metagraph(self, *, block: int | None = None) -> SimpleNamespace:
        del block
        return SimpleNamespace(
            neurons=[
                SimpleNamespace(
                    hotkey=self._miner_hotkey,
                    axon="miner.test:80",
                )
            ]
        )


class CampaignSource:
    def __init__(self, campaign: CampaignRecord) -> None:
        self._campaign = campaign

    async def fetch_campaigns(self) -> tuple[CampaignRecord, ...]:
        return (self._campaign,)

    async def close(self) -> None:
        return None


class CentralResults:
    """Central miner API double backed by the same immutable fixture campaign."""

    def __init__(self, campaign: CampaignRecord) -> None:
        self.campaign_source = campaign

    def _campaign(self) -> dict[str, object]:
        exclusive = self.campaign_source.access.exclusive_miner_hotkey is not None
        return {
            "campaign_id": self.campaign_source.access.campaign_id,
            "campaign_snapshot_id": "sha256-fixture-snapshot",
            "ecosystem_ids": list(self.campaign_source.pools),
            "status": "open",
            "capabilities": {
                "can_claim": not exclusive,
                "can_submit": True,
                "requires_claim": not exclusive,
            },
        }

    async def campaign(self, _campaign_id: str) -> dict[str, object]:
        return self._campaign()

    async def campaigns(self, _ecosystems=()) -> list[dict[str, object]]:
        return [self._campaign()]

    async def eligibility(self, campaign_id: str, creator_x_id: str) -> dict[str, object]:
        return {
            "campaign_id": campaign_id,
            "creator_x_id": creator_x_id,
            "eligible": True,
            "claim_eligible": True,
        }

    async def submission(self, submission_id: str) -> dict[str, object]:
        return {"submission_id": submission_id, "status": "verification_pending"}

    async def submissions(self, **_filters) -> list[dict[str, object]]:
        return []


class CapturingPublisher:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def publish(
        self,
        *,
        endpoint: str,
        payload_type: str,
        run_id: str,
        payload: dict[str, object],
    ) -> bool:
        assert endpoint == "https://ingestion.example/api/v1/brief-tweets"
        assert payload_type == "brief_tweets"
        assert run_id == f"v3:fixture-snapshot:{CAMPAIGN_ID}"
        self.payloads.append(payload)
        return True


class PassingBriefFilter:
    """Stand in for the external LLM while exercising the production scoring seam."""

    async def evaluate(
        self,
        _campaign: CampaignRecord,
        _tweet: Tweet,
    ) -> BriefEvaluation:
        return BriefEvaluation(
            meets_brief=True,
            reasoning="Fixture satisfies the campaign brief",
            checks_used=1,
        )


def create_wallet(path: Path, name: str) -> bt.Wallet:
    wallet = bt.Wallet(name=name, hotkey="default", path=str(path))
    wallet.create_new_coldkey(use_password=False, suppress=True)
    wallet.create_new_hotkey(use_password=False, suppress=True)
    return wallet


def campaign_feed(now: datetime, *, exclusive_miner_hotkey: str | None = None) -> CampaignFeed:
    campaign = CampaignRecord(
        access=CampaignAccess(
            campaign_id=CAMPAIGN_ID,
            mechanism_id=1,
            mining_protocol=MiningProtocol.PRECLAIM_V2,
            scoring_close_block=20,
            exclusive_miner_hotkey=exclusive_miner_hotkey,
        ),
        display="Fixture launch",
        brief="Describe your experience with the wallet in your own words.",
        pools=("ecosystem",),
        opens_at=now - timedelta(hours=1),
        closes_at=now + timedelta(hours=1),
        reward_pool_usd="1000.00",
        required_terms=("#Launch",),
        emission_start_block=30,
        emission_end_block=40,
    )
    return CampaignFeed(
        snapshot_id="fixture-snapshot",
        published_at=now - timedelta(hours=1),
        campaigns=(campaign,),
        ecosystem_maps=(
            EcosystemMap(
                ecosystem_id="ecosystem",
                name="Fixture ecosystem",
                eligible_creator_x_ids=(CREATOR_X_ID,),
                updated_at=now - timedelta(hours=1),
                accounts=(
                    SocialAccount(
                        x_id=CREATOR_X_ID,
                        username="creator",
                        influence=10.0,
                    ),
                ),
            ),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("exclusive", [False, True], ids=["open", "exclusive"])
async def test_tweet_flows_from_miner_api_to_published_reward(
    tmp_path: Path,
    *,
    exclusive: bool,
) -> None:
    now = datetime(2026, 1, 15, 12, tzinfo=UTC)
    miner_wallet = create_wallet(tmp_path, "miner")
    validator_wallet = create_wallet(tmp_path, "validator")
    miner_hotkey = miner_wallet.hotkey.ss58_address
    validator_hotkey = validator_wallet.hotkey.ss58_address
    feed = campaign_feed(
        now,
        exclusive_miner_hotkey=miner_hotkey if exclusive else None,
    )
    campaign = feed.campaigns[0]
    chain = InMemoryChain(miner_hotkey, first_timestamp=now + timedelta(minutes=1))
    miner_store = MinerStore(tmp_path / "miner.sqlite3")
    engine = MinerEngine(
        miner_hotkey=miner_hotkey,
        store=miner_store,
        submitter=chain,
        policy=BatchPolicy(max_age_seconds=5),
    )

    async def qualification() -> dict[str, object]:
        return {"eligible": True, "reason": "eligible"}

    service = MinerControlService(
        MinerSdk(engine, qualification_provider=qualification),
        CampaignSource(campaign),  # type: ignore[arg-type]
        commit_timeout_seconds=5,
        results_client=CentralResults(campaign),  # type: ignore[arg-type]
    )

    async def authorize_validator(hotkey: str) -> bool:
        return hotkey == validator_hotkey

    protocol_app = create_miner_app(
        miner_hotkey=miner_hotkey,
        provider=engine.batch_page,
        authorize_validator=authorize_validator,
    )
    app = create_control_app(lambda: service, protocol_app, INTERNAL_TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://miner.test",
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
    ) as control_client:
        claim_id = None
        if not exclusive:
            claim_response = await control_client.post(
                "/api/v1/claims",
                headers={"Idempotency-Key": "fixture-claim-0001"},
                json={
                    "campaign_id": CAMPAIGN_ID,
                    "creator_x_id": CREATOR_X_ID,
                    "draft": DRAFT,
                },
            )
            assert claim_response.status_code == 200
            claim = claim_response.json()
            assert claim["usability"]["safe_to_post"] is True
            claim_id = claim["claim_id"]

        submission_response = await control_client.post(
            "/api/v1/submissions",
            headers={"Idempotency-Key": "fixture-submission-0001"},
            json={
                "campaign_id": CAMPAIGN_ID,
                "tweet_id": TWEET_ID,
                "claim_id": claim_id,
                "creator_x_id": CREATOR_X_ID,
            },
        )
        assert submission_response.status_code == 200
        submission = submission_response.json()
        assert submission["status"] == "tweet_received"

        await engine.commit_ready(force=True)
        status_response = await control_client.get(
            f"/api/v1/submissions/{submission['submission_id']}"
        )
        assert status_response.json()["status"] == "verification_pending"

    validator_store = ValidatorStore(tmp_path / "validator.sqlite3", start_block=10)

    def client_factory(endpoint: MinerEndpoint) -> SignedMinerClient:
        return SignedMinerClient(
            validator_wallet,
            miner_hotkey=endpoint.hotkey,
            base_url=endpoint.base_url,
            transport=httpx.ASGITransport(app=app),
        )

    validator = ValidatorIngestor(
        chain,  # type: ignore[arg-type]
        validator_store,
        client_factory=client_factory,
    )
    endpoints = await validator.discover(block=30)
    assert endpoints == [MinerEndpoint(miner_hotkey, "http://miner.test:80")]
    ingestion = await validator.reconcile(endpoints[0], block=30)

    expected_batches = 1 if exclusive else 2
    assert ingestion.batches_verified == expected_batches
    assert ingestion.cursor == expected_batches
    assert ingestion.quarantined is False

    evidence = FixtureXProvider(
        tweets={
            TWEET_ID: TweetFetch(
                tweet=Tweet(
                    tweet_id=TWEET_ID,
                    author_x_id=CREATOR_X_ID,
                    created_at=now + timedelta(minutes=2),
                    text=EXCLUSIVE_TWEET if exclusive else DRAFT,
                    author="creator",
                ),
                provider_available=True,
            )
        },
        engagements={
            TWEET_ID: EngagementFetch(engagements={}, provider_available=True),
        },
    )
    qualification = HistoricalQualificationChecker(
        QualificationReader(
            chain,
            QualificationConfig(
                owner_hotkey=validator_hotkey,
                minimum_conviction_alpha=Decimal("0"),
                effective_block=0,
            ),
        )
    )
    attributions = await CampaignReconciler(
        validator_store,
        evidence,
        qualification,
    ).reconcile_feed(feed, finalized_block=30)
    coordinator = RewardCoordinator(
        validator_store,
        AttributionScorer(evidence, brief_filter=PassingBriefFilter()),
    )
    scored = await coordinator.freeze_scores(feed, attributions)
    weights, rewards = coordinator.shadow_weights(
        feed,
        scored,
        block=35,
        hotkey_to_uid={miner_hotkey: 7},
        uids=[0, 7],
    )
    publisher = CapturingPublisher()
    published = await ShadowResultPublisher(
        validator_store,
        publisher,  # type: ignore[arg-type]
        endpoint="https://ingestion.example/api/v1/brief-tweets",
    ).publish(feed, scored, rewards, block=35, hotkey_to_uid={miner_hotkey: 7})

    assert len(attributions) == 1
    assert attributions[0].accepted is True
    assert attributions[0].claim_id == claim_id
    assert attributions[0].submission_id == submission["submission_id"]
    assert attributions[0].miner_hotkey == miner_hotkey
    assert len(scored) == 1
    assert scored[0].score == 20.0
    assert scored[0].meets_brief is True
    assert scored[0].llm_checks_used == 1
    assert weights == {0: 0.0, 7: 1.0}
    assert len(rewards) == 1
    assert published == 1
    tweets = publisher.payloads[0]["tweets"]
    assert isinstance(tweets, list)
    assert tweets[0]["meets_brief"] is True
    assert tweets[0]["reasoning"] == "Fixture satisfies the campaign brief"
    decisions = publisher.payloads[0]["attribution_decisions"]
    assert isinstance(decisions, list)
    assert decisions[0]["reward_status"] == "rewarded"
    assert decisions[0]["reward_reason"] == "accepted"
