"""Composition root for the generic authenticated miner API process."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI

from bitcast_x.campaigns import CampaignFeedClient
from bitcast_x.config import Settings
from bitcast_x.errors import ChainOperationError
from bitcast_x.miner.api import create_control_app
from bitcast_x.miner.control import MinerControlService
from bitcast_x.miner.results import MinerResultsClient
from bitcast_x.miner.service import build_sdk, load_wallet
from bitcast_x.transport import create_miner_app

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MinerApps:
    """What run-miner-api serves.

    ``protocol`` is None when both share ``settings.port``: ``api`` then carries
    the validator protocol as well. Otherwise ``protocol`` alone is served on
    ``settings.port`` and ``api`` on ``settings.miner_api_port``.
    """

    api: FastAPI
    protocol: FastAPI | None


def build_miner_api(settings: Settings) -> MinerApps:
    """Build a chain-backed API with a managed single-writer lifecycle."""

    campaign_feed_url = settings.campaign_feed_url
    public_ip = settings.public_ip
    if campaign_feed_url is None:
        raise ValueError("BITCAST_X_CAMPAIGN_FEED_URL is required")
    if public_ip is None:
        raise ValueError("BITCAST_X_PUBLIC_IP is required")
    if settings.miner_api_token is None:
        raise ValueError("BITCAST_X_MINER_API_TOKEN is required")
    split = settings.miner_api_port is not None
    if settings.miner_api_port == settings.port:
        raise ValueError("BITCAST_X_MINER_API_PORT must differ from BITCAST_X_PORT")

    wallet = load_wallet(settings)
    runtime: dict[str, Any] = {"ready": False}

    async def is_ready() -> bool:
        return bool(runtime["ready"])

    async def authorize_validator(hotkey: str) -> bool:
        chain = runtime.get("chain")
        if chain is None:
            return False
        metagraph = await chain.metagraph()
        if metagraph is None:
            return False
        neuron = metagraph.by_hotkey(hotkey)
        return neuron is not None and bool(neuron.validator_permit)

    def get_service() -> MinerControlService:
        service = runtime.get("service")
        if not isinstance(service, MinerControlService):
            raise RuntimeError("miner control service is not ready")
        return service

    protocol_app = create_miner_app(
        miner_hotkey=str(wallet.hotkey.ss58_address),
        provider=lambda request, caller: get_service().sdk.engine.batch_page(request, caller),
        authorize_validator=authorize_validator,
        max_request_bytes=settings.max_request_bytes,
        auth_max_age=settings.auth_max_age_seconds,
        auth_allowed_skew=settings.auth_allowed_skew_seconds,
        requests_per_minute=settings.validator_requests_per_minute,
        readiness=is_ready,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        chain, sdk = await build_sdk(settings)
        campaign_source = CampaignFeedClient(
            campaign_feed_url,
            cache_path=settings.state_dir / "campaign-feed.json",
            timeout=settings.request_timeout_seconds,
            max_response_bytes=settings.campaign_feed_max_response_bytes,
        )
        results_client = MinerResultsClient(
            settings.miner_results_api_url,
            wallet.hotkey,
            timeout=settings.request_timeout_seconds,
        )
        runtime["chain"] = chain
        runtime["service"] = MinerControlService(
            sdk=sdk,
            campaign_source=campaign_source,
            commit_timeout_seconds=settings.miner_api_commit_timeout_seconds,
            results_client=results_client,
            enabled_ecosystem_ids=settings.miner_enabled_ecosystem_ids,
        )

        async def commit_loop() -> None:
            while True:
                try:
                    await sdk.engine.commit_ready()
                except Exception:
                    LOGGER.exception("queued commitment failed; durable state retained")
                await asyncio.sleep(min(0.5, settings.batch_max_age_seconds / 2))

        async def results_loop() -> None:
            while True:
                try:
                    await get_service().sync_submission_results()
                except Exception:
                    LOGGER.exception("submission result poll failed; local state retained")
                await asyncio.sleep(settings.miner_results_poll_seconds)

        commit_task: asyncio.Task[None] | None = None
        results_task: asyncio.Task[None] | None = None
        try:
            try:
                await chain.advertise_endpoint(wallet, ip=public_ip, port=settings.port)
            except ChainOperationError:
                LOGGER.exception(
                    "endpoint advertisement failed; continuing with existing on-chain endpoint"
                )
            commit_task = asyncio.create_task(commit_loop())
            results_task = asyncio.create_task(results_loop())
            runtime["ready"] = True
            LOGGER.info("miner API ready hotkey=%s", sdk.engine.miner_hotkey)
            yield
        finally:
            runtime["ready"] = False
            for task in (commit_task, results_task):
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            await results_client.close()
            await campaign_source.close()
            await chain.close()

    app = create_control_app(
        get_service,
        None if split else protocol_app,
        settings.miner_api_token.get_secret_value(),
    )
    # The lifecycle lives on the API app in both layouts; when split, the
    # protocol listener reports not-ready until it has run.
    app.router.lifespan_context = lifespan
    return MinerApps(api=app, protocol=protocol_app if split else None)


async def run_miner_api(settings: Settings) -> None:
    """Run the generic miner API until interrupted."""

    apps = build_miner_api(settings)
    if apps.protocol is None or settings.miner_api_port is None:
        await uvicorn.Server(
            uvicorn.Config(apps.api, host=settings.host, port=settings.port)
        ).serve()
        return

    servers = [
        uvicorn.Server(
            uvicorn.Config(apps.api, host=settings.miner_api_host, port=settings.miner_api_port)
        ),
        uvicorn.Server(uvicorn.Config(apps.protocol, host=settings.host, port=settings.port)),
    ]
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        # One listener stopping (signal, bind failure, crash) stops the other:
        # a miner serving validators without its lifecycle, or the reverse, is
        # never a state worth keeping.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for server in servers:
            server.should_exit = True
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
