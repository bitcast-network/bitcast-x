"""Opt-in conformance test against a real Bittensor v11 localnet."""

import asyncio
import hashlib
import json
import os
import socket
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import bittensor as bt
import httpx
import pytest
import uvicorn
from bittensor.keyfiles import Keypair
from bittensor.result import ChainError, ErrorCode
from fixture_x import FixtureXProvider

from bitcast_x.campaigns import CampaignFeed, CampaignRecord, EcosystemMap
from bitcast_x.chain import BittensorChain
from bitcast_x.miner import (
    BatchPolicy,
    BittensorCommitmentSubmitter,
    EventStatus,
    MinerEngine,
    MinerSdk,
    MinerStore,
)
from bitcast_x.protocol import CampaignAccess, CommitmentEnvelope, MiningProtocol
from bitcast_x.qualification import (
    HistoricalQualificationChecker,
    QualificationConfig,
    QualificationReader,
)
from bitcast_x.transport import (
    BatchPageRequest,
    BatchPageResponse,
    PositionedBatch,
    SignedMinerClient,
    create_miner_app,
)
from bitcast_x.validator.ingestion import MinerEndpoint, ValidatorIngestor
from bitcast_x.validator.reconciliation import CampaignReconciler
from bitcast_x.validator.store import ValidatorStore
from bitcast_x.x_provider import Tweet, TweetFetch

pytestmark = pytest.mark.skipif(
    os.getenv("BITCAST_X_RUN_LOCALNET") != "1",
    reason="set BITCAST_X_RUN_LOCALNET=1 with a local chain on port 9944",
)

CAMPAIGN_ID = "localnet-campaign"
CREATOR_X_ID = "456"
TWEET_ID = "999"
DRAFT = (
    "I spent a week testing the wallet. Fast confirmations help, but the recovery flow "
    "is what won me over. #Launch"
)


def canonical_json(value: Any) -> bytes:
    """Encode the test batch deterministically for the round-trip assertion."""

    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def create_wallet(path: Path, name: str) -> bt.Wallet:
    """Create one unencrypted wallet scoped to the disposable local chain."""

    wallet = bt.Wallet(name=name, hotkey="default", path=str(path))
    wallet.create_new_coldkey(use_password=False, suppress=True)
    wallet.create_new_hotkey(use_password=False, suppress=True)
    return wallet


def unused_tcp_port() -> int:
    """Ask the kernel for an available local test port."""

    with socket.socket() as listener:
        listener.bind(("", 0))
        return int(listener.getsockname()[1])


async def fund_and_register(client: Any, wallet: bt.Wallet, netuid: int) -> None:
    """Fund and register a disposable localnet wallet without the MEV relay."""

    alice = Keypair.create_from_uri("//Alice")
    transfer = await client.execute(
        bt.Transfer(dest_ss58=wallet.coldkeypub.ss58_address, amount_tao=10),
        alice,
        wait_for_inclusion=True,
        wait_for_finalization=True,
    )
    transfer.raise_for_failure()
    call = bt.calls.SubtensorModule.burned_register(
        netuid=netuid,
        hotkey=wallet.hotkey.ss58_address,
    )
    registration = await client.submit_call(
        call,
        wallet,
        signer="coldkey",
        wait_for_inclusion=True,
        wait_for_finalization=True,
    )
    registration.raise_for_failure()


async def set_localnet_weights_after_rate_limit(
    chain: BittensorChain,
    wallet: bt.Wallet,
    uid: int,
) -> None:
    """Exercise the real v11 call once the shared local subnet rate limit permits it."""

    for _attempt in range(45):
        try:
            await chain.set_weights(wallet, {uid: 1.0}, version_key=0)
            return
        except ChainError as exc:
            if exc.code is not ErrorCode.RATE_LIMITED:
                raise
            await asyncio.sleep(1)
    raise AssertionError("localnet weight rate limit did not clear within 45 seconds")


@pytest.mark.asyncio
async def test_real_v11_commitment_and_signed_http_round_trip(tmp_path: Path) -> None:
    miner = create_wallet(tmp_path, "miner")
    validator = create_wallet(tmp_path, "validator")
    netuid = 1
    port = unused_tcp_port()
    host_ip = socket.gethostbyname(socket.gethostname())
    batch = {
        "version": 2,
        "miner_hotkey": miner.hotkey.ss58_address,
        "sequence": 1,
        "previous_batch_hash": None,
        "events": [{"kind": "submission", "tweet_id": "1"}],
    }
    batch_hash = hashlib.sha256(canonical_json(batch)).digest()

    async def authorize(hotkey: str) -> bool:
        return hotkey == validator.hotkey.ss58_address

    async def provide(_request: BatchPageRequest, _caller: str) -> BatchPageResponse:
        return BatchPageResponse(
            miner_hotkey=miner.hotkey.ss58_address,
            batches=[PositionedBatch(batch=batch, position=finalized.position)],
            next_sequence=1,
            has_more=False,
        )

    app = create_miner_app(
        miner_hotkey=miner.hotkey.ss58_address,
        provider=provide,
        authorize_validator=authorize,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")  # noqa: S104
    )
    server_task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - uvicorn exposes a flag, not an awaitable event
        await asyncio.sleep(0.01)

    try:
        async with bt.Subtensor("local") as client:
            await fund_and_register(client, miner, netuid)
            await fund_and_register(client, validator, netuid)
            chain = BittensorChain(client, netuid=netuid)
            (
                coldkey,
                lock_target,
                conviction_rao,
                self_stake_rao,
            ) = await chain.miner_qualification_inputs(
                miner.hotkey.ss58_address,
                include_self_stake=True,
            )
            assert coldkey == miner.coldkeypub.ss58_address
            assert lock_target is None
            assert conviction_rao == 0
            assert self_stake_rao == 0
            await chain.advertise_endpoint(miner, ip=host_ip, port=port)
            envelope = CommitmentEnvelope(sequence=1, event_count=1, batch_hash=batch_hash)
            submitter = BittensorCommitmentSubmitter(chain, miner)
            budget = await submitter.capacity(envelope)
            finalized = await submitter.submit(envelope)
            recovered = await submitter.latest()
            observations = await chain.commitments_in_block(finalized.position.block)

            assert budget.can_commit is True
            assert finalized.stored_envelope == envelope.encode()
            assert recovered == finalized
            assert len(observations) == 1
            assert observations[0].hotkey == miner.hotkey.ss58_address
            assert observations[0].envelope == envelope

            stored = await chain.commitment(miner.hotkey.ss58_address)
            raw_hex = stored.fields[0]["Raw45"]
            assert CommitmentEnvelope.decode(bytes.fromhex(raw_hex.removeprefix("0x"))) == envelope

            metagraph = await chain.metagraph()
            neuron = metagraph.by_hotkey(miner.hotkey.ss58_address)
            assert neuron.axon == f"{host_ip}:{port}"

            # The disposable local subnet exposes only mechanism 0. The unit suite
            # separately proves mechanism 1 is retained on the v11 intent.
            local_weight_chain = BittensorChain(client, netuid=netuid, mechanism_id=0)
            await set_localnet_weights_after_rate_limit(local_weight_chain, miner, int(neuron.uid))

            miner_client = SignedMinerClient(
                validator,
                miner_hotkey=miner.hotkey.ss58_address,
                base_url=f"http://{neuron.axon}",
            )
            try:
                page = await miner_client.fetch_batches(BatchPageRequest(after_sequence=0))
            finally:
                await miner_client.close()

        assert hashlib.sha256(canonical_json(page.batches[0].batch)).digest() == envelope.batch_hash
    finally:
        server.should_exit = True
        await server_task


@pytest.mark.asyncio
async def test_real_creator_journey_survives_restart_and_reaches_attribution(
    tmp_path: Path,
) -> None:
    miner = create_wallet(tmp_path, "journey-miner")
    validator_wallet = create_wallet(tmp_path, "journey-validator")
    netuid = 1
    database = tmp_path / "miner.sqlite3"

    async with bt.Subtensor("local") as client:
        await fund_and_register(client, miner, netuid)
        await fund_and_register(client, validator_wallet, netuid)
        chain = BittensorChain(client, netuid=netuid)
        policy = BatchPolicy(max_age_seconds=5, max_events=100, max_batch_bytes=100_000)
        first_engine = MinerEngine(
            miner_hotkey=miner.hotkey.ss58_address,
            store=MinerStore(database),
            submitter=BittensorCommitmentSubmitter(chain, miner),
            policy=policy,
        )
        first_sdk = MinerSdk(first_engine)
        claim_id = first_sdk.create_claim(
            campaign_id=CAMPAIGN_ID,
            creator_x_id=CREATOR_X_ID,
            draft=DRAFT,
        )
        claim_batch = await first_engine.commit_ready(force=True)
        assert claim_batch is not None
        assert first_sdk.claim_status(claim_id) is EventStatus.SAFE_TO_POST

        # Rebuild every in-memory miner object over the same durable database.
        restarted_engine = MinerEngine(
            miner_hotkey=miner.hotkey.ss58_address,
            store=MinerStore(database),
            submitter=BittensorCommitmentSubmitter(chain, miner),
            policy=policy,
        )
        restarted_sdk = MinerSdk(restarted_engine)
        submission_id = restarted_sdk.submit_tweet(
            campaign_id=CAMPAIGN_ID,
            tweet_id=TWEET_ID,
            claim_id=claim_id,
            creator_x_id=CREATOR_X_ID,
        )
        submission_batch = await restarted_engine.commit_ready(force=True)
        assert submission_batch is not None
        page = await restarted_engine.batch_page(
            BatchPageRequest(after_sequence=0, max_batches=50),
            caller_hotkey="validator",
        )
        assert restarted_sdk.submission_status(submission_id) is EventStatus.VERIFICATION_PENDING
        assert [batch.batch["sequence"] for batch in page.batches] == [1, 2]
        assert page.batches[1].batch["reveals"][0]["claim_id"] == claim_id

        async def authorize(hotkey: str) -> bool:
            return hotkey == validator_wallet.hotkey.ss58_address

        app = create_miner_app(
            miner_hotkey=miner.hotkey.ss58_address,
            provider=restarted_engine.batch_page,
            authorize_validator=authorize,
        )

        def client_factory(endpoint: MinerEndpoint) -> SignedMinerClient:
            return SignedMinerClient(
                validator_wallet,
                miner_hotkey=endpoint.hotkey,
                base_url=endpoint.base_url,
                transport=httpx.ASGITransport(app=app),
            )

        validator_store = ValidatorStore(
            tmp_path / "validator.sqlite3",
            start_block=page.batches[0].position.block,
        )
        ingestion = await ValidatorIngestor(
            chain,
            validator_store,
            client_factory=client_factory,
        ).reconcile(MinerEndpoint(miner.hotkey.ss58_address, "http://miner.test"))
        assert ingestion.batches_verified == 2
        assert ingestion.cursor == 2
        assert ingestion.quarantined is False

        claim_anchor = await chain.commitment_at_position(
            miner.hotkey.ss58_address,
            page.batches[0].position,
        )
        scoring_close_block = page.batches[1].position.block
        tweet_created_at = claim_anchor.timestamp + timedelta(microseconds=1)
        campaign = CampaignRecord(
            access=CampaignAccess(
                campaign_id=CAMPAIGN_ID,
                mechanism_id=1,
                mining_protocol=MiningProtocol.PRECLAIM_V2,
                scoring_close_block=scoring_close_block,
            ),
            display="Localnet launch",
            brief="Describe your experience with the wallet in your own words.",
            pools=("ecosystem",),
            opens_at=claim_anchor.timestamp - timedelta(hours=1),
            closes_at=claim_anchor.timestamp + timedelta(hours=1),
            reward_pool_usd="1000.00",
            required_terms=("#Launch",),
        )
        feed = CampaignFeed(
            snapshot_id="localnet-snapshot",
            published_at=claim_anchor.timestamp - timedelta(hours=1),
            campaigns=(campaign,),
            ecosystem_maps=(
                EcosystemMap(
                    ecosystem_id="ecosystem",
                    name="Localnet ecosystem",
                    eligible_creator_x_ids=(CREATOR_X_ID,),
                    updated_at=claim_anchor.timestamp - timedelta(hours=1),
                ),
            ),
        )
        evidence = FixtureXProvider(
            tweets={
                TWEET_ID: TweetFetch(
                    tweet=Tweet(
                        tweet_id=TWEET_ID,
                        author_x_id=CREATOR_X_ID,
                        created_at=tweet_created_at,
                        text=DRAFT,
                        author="creator",
                    ),
                    provider_available=True,
                )
            },
        )
        qualification = HistoricalQualificationChecker(
            QualificationReader(
                chain,
                QualificationConfig(
                    owner_hotkey=validator_wallet.hotkey.ss58_address,
                    minimum_conviction_alpha=Decimal("0"),
                    effective_block=0,
                ),
            )
        )
        attributions = await CampaignReconciler(
            validator_store,
            evidence,
            qualification,
        ).reconcile_feed(feed, finalized_block=scoring_close_block)

    assert len(attributions) == 1
    assert attributions[0].accepted is True
    assert attributions[0].claim_id == claim_id
    assert attributions[0].submission_id == submission_id
    assert attributions[0].miner_hotkey == miner.hotkey.ss58_address
