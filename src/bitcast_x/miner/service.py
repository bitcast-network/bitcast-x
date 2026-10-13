"""Runnable reference miner node assembled from the reusable SDK pieces."""

import asyncio
import ipaddress
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import partial
from typing import Any

import bittensor as bt
import uvicorn
from fastapi import FastAPI

from bitcast_x.chain import BittensorChain
from bitcast_x.config import Settings
from bitcast_x.errors import ChainOperationError
from bitcast_x.miner.chain import BittensorCommitmentSubmitter
from bitcast_x.miner.engine import BatchPolicy, MinerEngine, MinerSdk
from bitcast_x.miner.store import MinerStore
from bitcast_x.qualification import (
    QualificationReader,
)
from bitcast_x.transport import BatchProvider, ReadinessProvider, create_miner_app

LOGGER = logging.getLogger(__name__)
SURPLUS_BENEFICIARY_RETRY_SECONDS = 300.0


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
    qualification_policy = settings.qualification_policy
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


def create_protocol_app(
    settings: Settings,
    *,
    miner_hotkey: str,
    provider: BatchProvider,
    authorize_validator: Callable[[str], Awaitable[bool]],
    readiness: ReadinessProvider,
) -> FastAPI:
    """Create the signed validator protocol app with the configured request bounds."""

    return create_miner_app(
        miner_hotkey=miner_hotkey,
        provider=provider,
        authorize_validator=authorize_validator,
        max_request_bytes=settings.max_request_bytes,
        auth_max_age=settings.auth_max_age_seconds,
        auth_allowed_skew=settings.auth_allowed_skew_seconds,
        requests_per_minute=settings.validator_requests_per_minute,
        readiness=readiness,
    )


async def is_permitted_validator(chain: BittensorChain, hotkey: str) -> bool:
    """Return whether ``hotkey`` currently holds a validator permit on the subnet."""

    metagraph = await chain.metagraph()
    if metagraph is None:
        return False
    neuron = metagraph.by_hotkey(hotkey)
    return neuron is not None and bool(neuron.validator_permit)


async def advertise_endpoint(chain: BittensorChain, wallet: Any, *, ip: str, port: int) -> None:
    """Advertise this miner's endpoint, accepting the identical one already on chain.

    The chain rate-limits serve calls (50 blocks on SN93), so a restart soon after
    the last advertisement cannot re-advertise. That is harmless when validators
    already read this endpoint; otherwise the miner would be unreachable, so the
    failure is raised.
    """

    try:
        await chain.advertise_endpoint(wallet, ip=ip, port=port)
    except ChainOperationError:
        metagraph = await chain.metagraph()
        neuron = (
            metagraph.by_hotkey(str(wallet.hotkey.ss58_address)) if metagraph is not None else None
        )
        if neuron is None or neuron.axon != _endpoint(ip, port):
            raise
        LOGGER.warning("endpoint advertisement failed; chain already advertises %s", neuron.axon)


def _endpoint(ip: str, port: int) -> str:
    """Format an endpoint the way the metagraph reports a served axon."""

    address = ipaddress.ip_address(ip)
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"{host}:{port}"


async def commit_until(
    engine: MinerEngine,
    settings: Settings,
    stopped: Callable[[], bool],
) -> None:
    """Commit due queued batches until ``stopped``, keeping durable state on failure."""

    while not stopped():
        try:
            await engine.commit_ready()
        except Exception:
            LOGGER.exception("queued batch commitment failed; durable state retained")
        await asyncio.sleep(min(0.5, settings.batch_max_age_seconds / 2))


def check_surplus_beneficiary(settings: Settings, miner_hotkey: str) -> None:
    """Reject a configured surplus beneficiary that is the miner itself."""

    if settings.miner_surplus_beneficiary == miner_hotkey:
        raise ValueError("BITCAST_X_MINER_SURPLUS_BENEFICIARY cannot be the miner's own hotkey")


async def sync_surplus_beneficiary(
    engine: MinerEngine,
    beneficiary_hotkey: str | None,
    *,
    retry_seconds: float = SURPLUS_BENEFICIARY_RETRY_SECONDS,
) -> None:
    """Queue the configured surplus beneficiary when it changes the latest one.

    An unset beneficiary clears an earlier designation. The batching loop then
    commits the queued event like any other.
    """

    if beneficiary_hotkey is None and engine.store.latest_surplus_beneficiary() is None:
        return
    while True:
        try:
            if engine.designate_surplus_beneficiary(beneficiary_hotkey):
                # The hotkey is public, but CodeQL treats beneficiary values as
                # private data, so the configured value is not repeated here.
                LOGGER.info(
                    "queued surplus beneficiary designation action=%s",
                    "clear" if beneficiary_hotkey is None else "set",
                )
            return
        except Exception:
            LOGGER.exception("surplus beneficiary designation failed; retrying")
        await asyncio.sleep(retry_seconds)


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

        check_surplus_beneficiary(self.settings, self.sdk.engine.miner_hotkey)
        app = create_protocol_app(
            self.settings,
            miner_hotkey=self.sdk.engine.miner_hotkey,
            provider=self.sdk.engine.batch_page,
            authorize_validator=partial(is_permitted_validator, self.chain),
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
        beneficiary_task: asyncio.Task[None] | None = None
        try:
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.05)
            if self.settings.public_ip is None:
                raise ValueError("BITCAST_X_PUBLIC_IP is required to advertise the miner endpoint")
            await advertise_endpoint(
                self.chain,
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
            beneficiary_task = asyncio.create_task(
                sync_surplus_beneficiary(self.sdk.engine, self.settings.miner_surplus_beneficiary)
            )
            await commit_until(self.sdk.engine, self.settings, lambda: server.should_exit)
        finally:
            self._ready = False
            if beneficiary_task is not None:
                beneficiary_task.cancel()
                with suppress(asyncio.CancelledError):
                    await beneficiary_task
            server.should_exit = True
            with suppress(asyncio.CancelledError):
                await server_task
            await self.chain.close()

    async def _is_ready(self) -> bool:
        """Report readiness only after finalized endpoint advertisement."""

        return self._ready
