"""Runnable reference miner node assembled from the reusable SDK pieces."""

import asyncio
import logging
from contextlib import suppress
from decimal import Decimal
from typing import Any

import bittensor as bt
import uvicorn

from bitcast_x.chain import BittensorChain
from bitcast_x.config import Settings
from bitcast_x.miner.chain import BittensorCommitmentSubmitter
from bitcast_x.miner.engine import BatchPolicy, MinerEngine, MinerSdk
from bitcast_x.miner.store import MinerStore
from bitcast_x.qualification import (
    QualificationConfig,
    QualificationReader,
    QualificationSchedule,
)
from bitcast_x.transport import create_miner_app

LOGGER = logging.getLogger(__name__)


def load_wallet(settings: Settings) -> Any:
    """Load the configured Bittensor wallet without creating or replacing keys."""

    return bt.Wallet(
        name=settings.wallet_name,
        hotkey=settings.wallet_hotkey,
        path=str(settings.wallet_path),
    )


async def build_sdk(
    settings: Settings,
) -> tuple[BittensorChain, MinerSdk]:
    """Build a chain-backed SDK over the miner's durable state database."""

    wallet = load_wallet(settings)
    chain = await BittensorChain.connect(
        settings.network,
        netuid=settings.netuid,
        mechanism_id=settings.mechanism_id,
    )
    store = MinerStore(settings.state_dir / "miner.sqlite3")
    engine = MinerEngine(
        miner_hotkey=str(wallet.hotkey.ss58_address),
        store=store,
        submitter=BittensorCommitmentSubmitter(chain, wallet),
        policy=BatchPolicy(
            max_age_seconds=settings.batch_max_age_seconds,
            max_events=settings.batch_max_events,
            max_batch_bytes=settings.batch_max_bytes,
            max_page_bytes=settings.max_response_bytes,
            max_pending_events=settings.pending_max_events,
            max_pending_bytes=settings.pending_max_bytes,
        ),
    )
    qualification_provider = None
    qualification_policy: QualificationConfig | QualificationSchedule | None
    if settings.qualification_schedule_json is not None:
        qualification_policy = QualificationSchedule.model_validate_json(
            settings.qualification_schedule_json
        )
    elif settings.qualification_owner_hotkey is not None:
        qualification_policy = QualificationConfig(
            owner_hotkey=settings.qualification_owner_hotkey,
            minimum_conviction_alpha=Decimal(settings.qualification_minimum_alpha),
            effective_block=settings.qualification_effective_block,
        )
    else:
        qualification_policy = None
    if qualification_policy is not None:
        reader = QualificationReader(
            chain,
            qualification_policy,
        )

        async def qualification_provider() -> dict[str, object]:
            block = await chain.current_block()
            status = await reader.read(str(wallet.hotkey.ss58_address), block=block)
            return status.model_dump(mode="json")

    return chain, MinerSdk(engine, qualification_provider=qualification_provider)


class ReferenceMiner:
    """Serve batches, advertise the endpoint, and continuously pace queued events."""

    def __init__(self, settings: Settings, chain: BittensorChain, sdk: MinerSdk) -> None:
        self.settings = settings
        self.chain = chain
        self.sdk = sdk
        self.wallet = load_wallet(settings)
        self._ready = False

    async def run(self) -> None:
        """Run until interrupted, preserving queued work and cleanly closing chain I/O."""

        app = create_miner_app(
            miner_hotkey=self.sdk.engine.miner_hotkey,
            provider=self.sdk.engine.batch_page,
            authorize_validator=self._authorize_validator,
            max_request_bytes=self.settings.max_request_bytes,
            auth_max_age=self.settings.auth_max_age_seconds,
            auth_allowed_skew=self.settings.auth_allowed_skew_seconds,
            requests_per_minute=self.settings.validator_requests_per_minute,
            readiness=self._is_ready,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=self.settings.host,
                port=self.settings.port,
                log_level="info",
            )
        )
        server_task = asyncio.create_task(server.serve())
        try:
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.05)
            if self.settings.public_ip is None:
                raise ValueError("BITCAST_X_PUBLIC_IP is required to advertise the miner endpoint")
            await self.chain.advertise_endpoint(
                self.wallet,
                ip=self.settings.public_ip,
                port=self.settings.port,
            )
            self._ready = True
            LOGGER.info(
                "reference miner ready hotkey=%s endpoint=%s:%s",
                self.sdk.engine.miner_hotkey,
                self.settings.public_ip,
                self.settings.port,
            )
            while not server.should_exit:
                try:
                    await self.sdk.engine.commit_ready()
                except Exception:
                    LOGGER.exception("queued batch commitment failed; durable state retained")
                await asyncio.sleep(min(0.5, self.settings.batch_max_age_seconds / 2))
        finally:
            self._ready = False
            server.should_exit = True
            with suppress(asyncio.CancelledError):
                await server_task
            await self.chain.close()

    async def _is_ready(self) -> bool:
        """Report readiness only after finalized endpoint advertisement."""

        return self._ready

    async def _authorize_validator(self, hotkey: str) -> bool:
        metagraph = await self.chain.metagraph()
        if metagraph is None:
            return False
        neuron = metagraph.by_hotkey(hotkey)
        return neuron is not None and bool(neuron.validator_permit)
