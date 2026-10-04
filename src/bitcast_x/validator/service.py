"""Runnable validator ingestion and weight-calculation loop."""

import asyncio
import logging
import time
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn
from bittensor.result import BittensorError

from bitcast_x import __version__
from bitcast_x.brief_filter import LlmBriefFilter
from bitcast_x.campaigns import CampaignFeed, CampaignFeedClient
from bitcast_x.chain import BittensorChain
from bitcast_x.config import Settings
from bitcast_x.errors import (
    ChainOperationError,
    ProtocolError,
    ReconciliationUnavailableError,
    ResponseTooLargeError,
)
from bitcast_x.logging import configure_loki_logging, shutdown_loki_logging
from bitcast_x.miner.service import load_wallet
from bitcast_x.ops import RuntimeHealth, create_ops_app
from bitcast_x.protocol import AttributionResult
from bitcast_x.publishing import DataPublisher
from bitcast_x.qualification import (
    QualificationReader,
    QualificationSchedule,
)
from bitcast_x.release import source_revision
from bitcast_x.validator.ingestion import (
    ValidatorIngestor,
    signed_client_factory,
)
from bitcast_x.validator.preview import PreviewStore, PreviewXProvider
from bitcast_x.validator.publishing import ShadowResultPublisher
from bitcast_x.validator.reconciliation import CampaignReconciler
from bitcast_x.validator.rewards import RewardCoordinator
from bitcast_x.validator.scoring import AttributionScorer
from bitcast_x.validator.store import ValidatorStore
from bitcast_x.x_provider import DesearchProvider

LOGGER = logging.getLogger(__name__)


def ensure_production_outputs_configured(settings: Settings) -> None:
    """Reject enabled production outputs that cannot produce a complete vector."""

    if not (settings.enable_data_publish or settings.enable_weight_submission):
        return
    missing = settings.missing_validator_settings()
    if missing:
        raise ValueError("production validator outputs require: " + ", ".join(missing))


def ensure_preclaim_economics_qualified(
    schedule: QualificationSchedule,
    *,
    block: int,
    preclaim_active: bool,
    data_publish_enabled: bool,
    weight_submission_enabled: bool,
) -> None:
    """Fail closed when preclaim economics could run with qualification disabled."""

    economics_enabled = data_publish_enabled or weight_submission_enabled
    if preclaim_active and economics_enabled and not schedule.at(block).financial_barrier_enabled:
        raise ProtocolError(
            "preclaim publication or weight submission requires a non-zero qualification threshold"
        )


async def submit_weights_if_due(
    chain: BittensorChain,
    wallet: Any,
    graph: Any,
    weights: dict[int, float],
    *,
    block: int,
    epoch_blocks: int,
    version_key: int,
) -> bool:
    """Submit once the mechanism-specific chain cadence is due."""

    validator = graph.by_hotkey(str(wallet.hotkey.ss58_address))
    if validator is None:
        raise ChainOperationError("validator hotkey is not registered on the subnet")
    last_update = await chain.last_weight_update(int(validator.uid))
    if block - last_update <= epoch_blocks:
        return False
    await chain.set_weights(wallet, weights, version_key=version_key)
    LOGGER.info(
        "submitted mechanism weights block=%s prior_update=%s",
        block,
        last_update,
    )
    return True


@dataclass(frozen=True, slots=True)
class _Economics:
    """Everything needed to reconcile, reward and publish campaigns each cycle."""

    feed: CampaignFeedClient
    schedule: QualificationSchedule
    reconciler: CampaignReconciler
    rewards: RewardCoordinator
    preview_provider: PreviewXProvider
    preview_reconciler: CampaignReconciler
    preview_scorer: AttributionScorer
    publisher: ShadowResultPublisher | None


class ValidatorService:
    """Continuously verify miner-reported history and reconcile campaign state."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self) -> None:
        """Run finalized ingestion with independently activated production outputs."""

        ensure_production_outputs_configured(self.settings)
        wallet = load_wallet(self.settings)
        chain = await BittensorChain.connect(
            self.settings.network,
            netuid=self.settings.netuid,
            mechanism_id=self.settings.mechanism_id,
        )
        health = RuntimeHealth.create()
        async with AsyncExitStack() as stack:
            stack.push_async_callback(shutdown_loki_logging)
            stack.push_async_callback(chain.close)
            configure_loki_logging(
                self.settings,
                labels={
                    "service": "bitcast-x",
                    "neuron": "validator",
                    "netuid": str(self.settings.netuid),
                    "mechanism_id": str(self.settings.mechanism_id),
                    "hotkey": str(wallet.hotkey.ss58_address),
                    "version": __version__,
                    "source_revision": source_revision(),
                },
            )
            LOGGER.info(
                "validator runtime identity version=%s source_revision=%s",
                __version__,
                source_revision(),
            )
            store = ValidatorStore(self.settings.state_dir / "validator.sqlite3")
            ingestor = ValidatorIngestor(
                chain,
                store,
                client_factory=signed_client_factory(
                    wallet,
                    timeout=self.settings.request_timeout_seconds,
                    max_response_bytes=self.settings.max_response_bytes,
                ),
                max_concurrency=self.settings.validator_max_concurrency,
                page_size=self.settings.max_batches_per_page,
            )
            economics = await self._open_economics(chain, store, wallet, stack)
            # Registered last so shutdown marks the node unready before closing anything.
            ops_server = await self._serve_ops(health, stack)
            if economics is None:
                LOGGER.warning(
                    "campaign URL, Desearch key, LLM key, or qualification owner is missing; "
                    "validator will ingest but not reconcile"
                )
            while not ops_server.should_exit:
                await self._cycle(chain, wallet, store, ingestor, economics, health)
                if not ops_server.should_exit:
                    await asyncio.sleep(self.settings.validator_poll_seconds)

    async def _serve_ops(self, health: RuntimeHealth, stack: AsyncExitStack) -> uvicorn.Server:
        """Start the ops endpoint and register its shutdown, which marks the node unready."""

        server = uvicorn.Server(
            uvicorn.Config(
                create_ops_app(health),
                host=self.settings.ops_host,
                port=self.settings.ops_port,
                log_level=self.settings.log_level.lower(),
            )
        )
        task = asyncio.create_task(server.serve())

        async def stop() -> None:
            health.ready = False
            server.should_exit = True
            with suppress(asyncio.CancelledError):
                await task

        stack.push_async_callback(stop)
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.05)
        return server

    async def _open_economics(
        self,
        chain: BittensorChain,
        store: ValidatorStore,
        wallet: Any,
        stack: AsyncExitStack,
    ) -> _Economics | None:
        """Build the economics stack, or return None when its settings are incomplete."""

        settings = self.settings
        qualification_policy = settings.qualification_policy
        if (
            settings.missing_validator_settings()
            or qualification_policy is None
            or settings.desearch_api_key is None
            or settings.llm_api_key is None
        ):
            return None
        feed = CampaignFeedClient.from_settings(settings)
        stack.push_async_callback(feed.close)
        x_provider = DesearchProvider(
            settings.desearch_api_key,
            timeout=settings.request_timeout_seconds,
        )
        stack.push_async_callback(x_provider.close)
        qualification = QualificationReader(chain, qualification_policy)
        preview_store = PreviewStore(
            settings.state_dir / "preview.sqlite3",
            legacy_directory=settings.state_dir / "preview-cache",
        )
        stack.callback(preview_store.close)
        preview_provider = PreviewXProvider(x_provider, preview_store)
        endpoint = settings.llm_endpoint
        brief_filter = LlmBriefFilter(
            api_url=endpoint.url,
            api_key=settings.llm_api_key,
            model=endpoint.model,
            cache=store,
            num_checks=settings.llm_num_checks,
            tweet_max_length=settings.llm_tweet_max_length,
            max_response_bytes=settings.max_response_bytes,
            timeout=endpoint.timeout,
            extra_headers=endpoint.headers,
        )
        stack.push_async_callback(brief_filter.close)
        publisher: ShadowResultPublisher | None = None
        if settings.enable_data_publish:
            data_publisher = DataPublisher(wallet, timeout=settings.request_timeout_seconds)
            stack.push_async_callback(data_publisher.close)
            publisher = ShadowResultPublisher(
                store,
                data_publisher,
                endpoint=f"{settings.data_client_url.rstrip('/')}/api/v1/brief-tweets",
                preview_store=preview_store,
            )
        return _Economics(
            feed=feed,
            schedule=qualification.schedule,
            reconciler=CampaignReconciler(store, x_provider, qualification),
            rewards=RewardCoordinator(
                store,
                AttributionScorer(
                    x_provider,
                    brief_filter=brief_filter,
                    max_concurrency=settings.validator_max_concurrency,
                ),
                score_blend=settings.weight_score_blend,
            ),
            preview_provider=preview_provider,
            preview_reconciler=CampaignReconciler(store, preview_provider, qualification),
            preview_scorer=AttributionScorer(
                preview_provider,
                brief_filter=brief_filter,
                max_concurrency=settings.validator_preview_max_concurrency,
            ),
            publisher=publisher,
        )

    async def _cycle(
        self,
        chain: BittensorChain,
        wallet: Any,
        store: ValidatorStore,
        ingestor: ValidatorIngestor,
        economics: _Economics | None,
        health: RuntimeHealth,
    ) -> None:
        """Ingest miner history, then settle and publish campaigns; never raise."""

        cycle_started = time.monotonic()
        try:
            finalized_block = await chain.current_block()
            endpoints = await ingestor.discover(block=finalized_block)
            outcomes = await ingestor.reconcile_all(endpoints, block=finalized_block)
            attributions = (
                await self._settle(chain, wallet, store, economics, finalized_block)
                if economics is not None
                else []
            )
            LOGGER.info(
                "validator reconciliation block=%s miners=%s successful=%s "
                "empty=%s verified_batches=%s attributions=%s unavailable=%s "
                "quarantined=%s errors=%s duration_seconds=%.3f",
                finalized_block,
                len(outcomes),
                sum(not item.error for item in outcomes),
                sum(not item.error and item.batches_verified == 0 for item in outcomes),
                sum(item.batches_verified for item in outcomes),
                len(attributions),
                sum(not item.available for item in outcomes),
                sum(item.quarantined for item in outcomes),
                sum(bool(item.error) for item in outcomes),
                time.monotonic() - cycle_started,
            )
            health.success(finalized_block)
        except ProtocolError:
            health.failure()
            LOGGER.exception(
                "validator consensus violation; prior durable validator state retained"
            )
        except (
            BittensorError,
            ChainOperationError,
            ReconciliationUnavailableError,
            ResponseTooLargeError,
            httpx.HTTPError,
            OSError,
        ):
            health.failure()
            LOGGER.exception("validator evidence unavailable; durable state retained")

    async def _settle(
        self,
        chain: BittensorChain,
        wallet: Any,
        store: ValidatorStore,
        economics: _Economics,
        block: int,
    ) -> list[AttributionResult]:
        """Reconcile and score the feed, submit weights when due, then publish results."""

        try:
            feed = await economics.feed.fetch()
        except ValueError as exc:
            # A malformed or unsupported feed, such as one carrying a retired
            # campaign mode, fails this cycle closed.
            raise ProtocolError(f"campaign feed rejected: {exc}") from exc
        feed = feed.model_copy(update={"campaigns": store.bind_campaign_protocols(feed.campaigns)})
        ensure_preclaim_economics_qualified(
            economics.schedule,
            block=block,
            preclaim_active=bool(feed.campaigns),
            data_publish_enabled=self.settings.enable_data_publish,
            weight_submission_enabled=self.settings.enable_weight_submission,
        )
        attributions = await economics.reconciler.reconcile_feed(feed, finalized_block=block)
        scored = await economics.rewards.freeze_scores(
            feed,
            attributions,
            block=block,
            reconciled_campaign_ids=economics.reconciler.completed_campaign_ids,
        )
        graph = await chain.metagraph(block=block)
        if graph is None:
            raise ChainOperationError("finalized metagraph is unavailable")
        uids = [int(neuron.uid) for neuron in graph.neurons]
        hotkey_to_uid = {str(neuron.hotkey): int(neuron.uid) for neuron in graph.neurons}
        if economics.publisher is not None:
            await self._publish_previews(
                store, economics, economics.publisher, feed, block, hotkey_to_uid
            )
        weights, floors = economics.rewards.shadow_weights(
            feed,
            scored,
            block=block,
            hotkey_to_uid=hotkey_to_uid,
            uids=uids,
            persist=False,
        )
        pending = economics.rewards.pending_reward_campaign_ids(feed, block=block)
        if pending:
            LOGGER.warning(
                "weight update deferred; final campaign economics are incomplete campaigns=%s",
                ",".join(pending),
            )
        else:
            store.persist_shadow_weights(block, feed.snapshot_id, weights)
        if self.settings.enable_weight_submission and not pending:
            await submit_weights_if_due(
                chain,
                wallet,
                graph,
                weights,
                block=block,
                epoch_blocks=self.settings.weight_epoch_blocks,
                version_key=self.settings.weight_version_key,
            )
        # Publish only after weights, so a campaign whose results cannot be
        # published never holds back consensus output for every campaign.
        if economics.publisher is not None:
            await economics.publisher.publish(
                feed,
                scored,
                floors,
                block=block,
                hotkey_to_uid=hotkey_to_uid,
                completed_campaign_ids=economics.rewards.completed_campaign_ids,
            )
        return attributions

    async def _publish_previews(
        self,
        store: ValidatorStore,
        economics: _Economics,
        publisher: ShadowResultPublisher,
        feed: CampaignFeed,
        block: int,
        hotkey_to_uid: dict[str, int],
    ) -> None:
        """Publish replaceable previews for campaigns whose scoring has not closed."""

        campaigns = [
            campaign for campaign in feed.campaigns if block < campaign.access.scoring_close_block
        ]
        featured_tweet_ids: set[str] = set()
        for campaign in campaigns:
            selection = store.featured_tweet_selection(campaign.access.campaign_id)
            if selection is not None:
                featured_tweet_ids.add(selection.tweet_id)
        economics.preview_provider.set_featured_tweet_ids(featured_tweet_ids)
        if not campaigns:
            return
        events = economics.preview_reconciler.verified_events(block)
        for campaign in campaigns:
            try:
                attributions = await economics.preview_reconciler.reconcile_campaign(
                    campaign,
                    feed,
                    through_block=block,
                    events=events,
                    defer_unavailable_tweets=True,
                )
                scores = await economics.preview_scorer.score(
                    feed,
                    attributions,
                    defer_unavailable_tweets=True,
                )
                if attributions:
                    await publisher.publish_preview(
                        feed,
                        campaign,
                        scores,
                        attributions,
                        block=block,
                        hotkey_to_uid=hotkey_to_uid,
                    )
            except ReconciliationUnavailableError as exc:
                LOGGER.warning(
                    "preview unavailable campaign=%s block=%s error=%s",
                    campaign.access.campaign_id,
                    block,
                    exc,
                )
