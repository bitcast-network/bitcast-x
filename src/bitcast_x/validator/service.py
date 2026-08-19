"""Runnable validator ingestion and weight-calculation loop."""

import asyncio
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import uvicorn
from bittensor.result import BittensorError

from bitcast_x import __version__
from bitcast_x.brief_filter import LlmBriefFilter
from bitcast_x.campaigns import CampaignFeedClient
from bitcast_x.chain import BittensorChain
from bitcast_x.config import Settings
from bitcast_x.errors import (
    ChainOperationError,
    ProtocolError,
    ReconciliationUnavailableError,
    ResponseTooLargeError,
)
from bitcast_x.legacy import (
    ConnectionStore,
    LegacyAttributionEngine,
    LegacyCadence,
    LegacyConnectionCollector,
    LegacyPricingService,
    LegacyResultPublisher,
    LegacyRewardCoordinator,
    LegacySnapshotStore,
    LegacyTweetStore,
)
from bitcast_x.logging import configure_loki_logging, shutdown_loki_logging
from bitcast_x.miner.service import load_wallet
from bitcast_x.ops import RuntimeHealth, create_ops_app
from bitcast_x.publishing import DataPublisher
from bitcast_x.qualification import (
    HistoricalQualificationChecker,
    QualificationConfig,
    QualificationReader,
    QualificationSchedule,
)
from bitcast_x.release import source_revision
from bitcast_x.validator.ingestion import (
    ValidatorIngestor,
    signed_client_factory,
)
from bitcast_x.validator.legacy import (
    combine_weights,
    has_legacy_campaigns,
    preclaim_feed,
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
    missing: list[str] = []
    if settings.campaign_feed_url is None:
        missing.append("BITCAST_X_CAMPAIGN_FEED_URL")
    if not settings.desearch_api_key:
        missing.append("BITCAST_X_DESEARCH_API_KEY")
    if not settings.llm_api_key:
        missing.append(
            "BITCAST_X_CHUTES_API_KEY"
            if settings.llm_provider == "chutes"
            else "BITCAST_X_OPENROUTER_API_KEY"
        )
    if settings.qualification_schedule_json is None and settings.qualification_owner_hotkey is None:
        missing.append("BITCAST_X_QUALIFICATION_OWNER_HOTKEY")
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
        campaign_client: CampaignFeedClient | None = None
        x_provider: DesearchProvider | None = None
        data_publisher: DataPublisher | None = None
        brief_filter: LlmBriefFilter | None = None
        preview_store: PreviewStore | None = None
        legacy_pricing: LegacyPricingService | None = None
        legacy_collector: LegacyConnectionCollector | None = None
        legacy_tweet_store: LegacyTweetStore | None = None
        ops_server: uvicorn.Server | None = None
        ops_task: asyncio.Task[None] | None = None
        health = RuntimeHealth.create()
        try:
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
            store = ValidatorStore(
                self.settings.state_dir / "validator.sqlite3",
            )
            ops_server = uvicorn.Server(
                uvicorn.Config(
                    create_ops_app(health),
                    host=self.settings.ops_host,
                    port=self.settings.ops_port,
                    log_level=self.settings.log_level.lower(),
                )
            )
            ops_task = asyncio.create_task(ops_server.serve())
            while not ops_server.started:
                if ops_task.done():
                    await ops_task
                await asyncio.sleep(0.05)
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
            reconciler: CampaignReconciler | None = None
            preview_reconciler: CampaignReconciler | None = None
            reward_coordinator: RewardCoordinator | None = None
            preview_reward_coordinator: RewardCoordinator | None = None
            legacy_engine: LegacyAttributionEngine | None = None
            legacy_rewards: LegacyRewardCoordinator | None = None
            legacy_cadence = LegacyCadence()
            cached_legacy_weights: dict[int, float] | None = None
            result_publisher: ShadowResultPublisher | None = None
            legacy_result_publisher: LegacyResultPublisher | None = None
            qualification_schedule: QualificationSchedule | None = None
            if (
                self.settings.campaign_feed_url is not None
                and self.settings.desearch_api_key is not None
                and (
                    self.settings.qualification_schedule_json is not None
                    or self.settings.qualification_owner_hotkey is not None
                )
                and self.settings.llm_api_key is not None
            ):
                campaign_client = CampaignFeedClient(
                    self.settings.campaign_feed_url,
                    cache_path=self.settings.state_dir / "campaign-feed.json",
                    timeout=self.settings.request_timeout_seconds,
                    max_response_bytes=self.settings.campaign_feed_max_response_bytes,
                )
                x_provider = DesearchProvider(
                    self.settings.desearch_api_key,
                    timeout=self.settings.request_timeout_seconds,
                )
                if self.settings.qualification_schedule_json is not None:
                    qualification_policy: QualificationConfig | QualificationSchedule = (
                        QualificationSchedule.model_validate_json(
                            self.settings.qualification_schedule_json
                        )
                    )
                else:
                    owner_hotkey = self.settings.qualification_owner_hotkey
                    if owner_hotkey is None:
                        raise ValueError("qualification owner hotkey is required")
                    qualification_policy = QualificationConfig(
                        owner_hotkey=owner_hotkey,
                        minimum_conviction_alpha=Decimal(self.settings.qualification_minimum_alpha),
                        minimum_self_stake_alpha=(
                            Decimal(self.settings.qualification_minimum_self_stake_alpha)
                            if self.settings.qualification_minimum_self_stake_alpha is not None
                            else None
                        ),
                        effective_block=self.settings.qualification_effective_block,
                    )
                qualification_reader = QualificationReader(chain, qualification_policy)
                qualification_schedule = qualification_reader.schedule
                qualification = HistoricalQualificationChecker(qualification_reader)
                reconciler = CampaignReconciler(store, x_provider, qualification)
                preview_store = PreviewStore(self.settings.state_dir / "preview-cache")
                preview_provider = PreviewXProvider(x_provider, preview_store)
                preview_reconciler = CampaignReconciler(store, preview_provider, qualification)
                if self.settings.llm_provider == "chutes":
                    llm_url = "https://llm.chutes.ai/v1/chat/completions"
                    llm_model = "Qwen/Qwen3-32B"
                    llm_headers: dict[str, str] = {}
                    llm_timeout = 60.0
                else:
                    llm_url = "https://openrouter.ai/api/v1/chat/completions"
                    llm_model = "qwen/qwen3-32b:nitro"
                    llm_headers = {
                        "HTTP-Referer": "https://bitcast.ai",
                        "X-Title": "Bitcast Validator",
                    }
                    llm_timeout = 90.0
                brief_filter = LlmBriefFilter(
                    api_url=llm_url,
                    api_key=self.settings.llm_api_key,
                    model=llm_model,
                    cache=store,
                    num_checks=self.settings.llm_num_checks,
                    tweet_max_length=self.settings.llm_tweet_max_length,
                    max_response_bytes=self.settings.max_response_bytes,
                    timeout=llm_timeout,
                    extra_headers=llm_headers,
                )
                scorer = AttributionScorer(
                    x_provider,
                    brief_filter=brief_filter,
                    max_concurrency=self.settings.validator_max_concurrency,
                )
                reward_coordinator = RewardCoordinator(store, scorer)
                preview_reward_coordinator = RewardCoordinator(
                    store,
                    AttributionScorer(
                        preview_provider,
                        brief_filter=brief_filter,
                        max_concurrency=self.settings.validator_max_concurrency,
                    ),
                )
                connection_path = (
                    self.settings.legacy_connections_path
                    or self.settings.state_dir / "connections.db"
                )
                snapshot_path = (
                    self.settings.legacy_snapshots_path
                    or self.settings.state_dir / "reward_snapshots"
                )
                tweet_store_path = (
                    self.settings.legacy_tweet_store_path
                    or self.settings.state_dir / "legacy_tweet_store"
                )
                legacy_connections = ConnectionStore(connection_path)
                legacy_tweet_store = LegacyTweetStore(tweet_store_path)
                legacy_scorer = AttributionScorer(
                    x_provider,
                    brief_filter=brief_filter,
                    max_concurrency=self.settings.validator_max_concurrency,
                    engagement_merger=legacy_tweet_store.merge_engagements,
                )
                legacy_engine = LegacyAttributionEngine(
                    legacy_connections,
                    x_provider,
                    legacy_scorer,
                    legacy_tweet_store,
                    nocode_uid=self.settings.legacy_nocode_uid,
                )
                legacy_collector = LegacyConnectionCollector(
                    legacy_connections,
                    x_provider,
                    connection_tweet_ids=tuple(
                        item.strip()
                        for item in self.settings.legacy_connection_tweet_ids.split(",")
                        if item.strip()
                    ),
                    fasttrack_url=self.settings.legacy_fasttrack_url,
                    tweet_merger=legacy_tweet_store.merge,
                    timeout=self.settings.request_timeout_seconds,
                )
                legacy_snapshot_store = LegacySnapshotStore(snapshot_path)
                legacy_rewards = LegacyRewardCoordinator(legacy_snapshot_store)
                legacy_pricing = LegacyPricingService(chain)
                if self.settings.enable_data_publish:
                    data_publisher = DataPublisher(
                        wallet,
                        timeout=self.settings.request_timeout_seconds,
                    )
                    result_publisher = ShadowResultPublisher(
                        store,
                        data_publisher,
                        endpoint=(
                            f"{self.settings.data_client_url.rstrip('/')}/api/v1/brief-tweets"
                        ),
                        preview_store=preview_store,
                    )
                    legacy_result_publisher = LegacyResultPublisher(
                        legacy_connections,
                        data_publisher,
                        data_client_url=self.settings.data_client_url,
                        snapshots=legacy_snapshot_store,
                        nocode_uid=self.settings.legacy_nocode_uid,
                    )
            else:
                LOGGER.warning(
                    "campaign URL, Desearch key, LLM key, or qualification owner is missing; "
                    "validator will ingest but not reconcile"
                )
            while not ops_server.should_exit:
                cycle_started = time.monotonic()
                try:
                    submission_weights: dict[int, float] | None = None
                    finalized_block = await chain.current_block()
                    endpoints = await ingestor.discover(block=finalized_block)
                    outcomes = await ingestor.reconcile_all(endpoints, block=finalized_block)
                    attributions = []
                    if campaign_client is not None and reconciler is not None:
                        complete_feed = await campaign_client.fetch()
                        bound_campaigns = store.bind_campaign_protocols(complete_feed.campaigns)
                        complete_feed = complete_feed.model_copy(
                            update={"campaigns": bound_campaigns}
                        )
                        legacy_active = has_legacy_campaigns(complete_feed)
                        feed = preclaim_feed(complete_feed)
                        if qualification_schedule is None:
                            raise ProtocolError("qualification schedule is unavailable")
                        ensure_preclaim_economics_qualified(
                            qualification_schedule,
                            block=finalized_block,
                            preclaim_active=bool(feed.campaigns),
                            data_publish_enabled=self.settings.enable_data_publish,
                            weight_submission_enabled=self.settings.enable_weight_submission,
                        )
                        attributions = await reconciler.reconcile_feed(
                            feed,
                            finalized_block=finalized_block,
                        )
                        if reward_coordinator is not None:
                            scored = await reward_coordinator.freeze_scores(feed, attributions)
                            graph = await chain.metagraph(block=finalized_block)
                            if graph is None:
                                raise ChainOperationError("finalized metagraph is unavailable")
                            uids = [int(neuron.uid) for neuron in graph.neurons]
                            hotkey_to_uid = {
                                str(neuron.hotkey): int(neuron.uid) for neuron in graph.neurons
                            }
                            if (
                                result_publisher is not None
                                and preview_reconciler is not None
                                and preview_reward_coordinator is not None
                            ):
                                for campaign in feed.campaigns:
                                    if finalized_block >= campaign.access.scoring_close_block:
                                        continue
                                    try:
                                        preview_attributions = (
                                            await preview_reconciler.reconcile_campaign(
                                                campaign,
                                                feed,
                                                through_block=finalized_block,
                                                defer_unavailable_tweets=True,
                                            )
                                        )
                                        preview_scores = (
                                            await preview_reward_coordinator.preview_scores(
                                                feed,
                                                preview_attributions,
                                            )
                                        )
                                        if preview_attributions:
                                            await result_publisher.publish_preview(
                                                feed,
                                                campaign,
                                                preview_scores,
                                                preview_attributions,
                                                block=finalized_block,
                                                hotkey_to_uid=hotkey_to_uid,
                                            )
                                    except ReconciliationUnavailableError as exc:
                                        LOGGER.warning(
                                            "preview unavailable campaign=%s block=%s error=%s",
                                            campaign.access.campaign_id,
                                            finalized_block,
                                            exc,
                                        )
                            productive_weights, floors = reward_coordinator.shadow_weights(
                                feed,
                                scored,
                                block=finalized_block,
                                hotkey_to_uid=hotkey_to_uid,
                                uids=uids,
                                persist=False,
                            )
                            pending_reward_campaigns = (
                                reward_coordinator.pending_reward_campaign_ids(
                                    feed,
                                    block=finalized_block,
                                )
                            )
                            preclaim_weights_complete = not pending_reward_campaigns
                            if pending_reward_campaigns:
                                LOGGER.warning(
                                    "weight update deferred; final campaign economics "
                                    "are incomplete campaigns=%s",
                                    ",".join(pending_reward_campaigns),
                                )
                            else:
                                submission_weights = productive_weights
                                if not legacy_active:
                                    store.persist_shadow_weights(
                                        finalized_block,
                                        feed.snapshot_id,
                                        productive_weights,
                                    )
                            if legacy_active:
                                if (
                                    legacy_engine is None
                                    or legacy_rewards is None
                                    or legacy_pricing is None
                                ):
                                    raise ProtocolError("local legacy engine is unavailable")
                                legacy_engine.validate_state()
                                if legacy_collector is None:
                                    raise ProtocolError(
                                        "legacy connection collector is unavailable"
                                    )
                                if legacy_cadence.fast_track_due():
                                    changed = await legacy_collector.collect_fasttrack(
                                        complete_feed
                                    )
                                    LOGGER.info("legacy fast-track intake changed=%s", changed)
                                if legacy_cadence.scoring_due():
                                    legacy_scoring_feed = legacy_rewards.scoring_feed(complete_feed)
                                    skipped_frozen = len(complete_feed.campaigns) - len(
                                        legacy_scoring_feed.campaigns
                                    )
                                    if skipped_frozen:
                                        LOGGER.info(
                                            "skipping frozen legacy campaign scoring count=%s",
                                            skipped_frozen,
                                        )
                                    legacy_scored = await legacy_engine.score_feed(
                                        legacy_scoring_feed,
                                        block=finalized_block,
                                        hotkey_to_uid=hotkey_to_uid,
                                    )
                                    pricing = await legacy_pricing.fetch(block=finalized_block)
                                    legacy_weights, legacy_floors = legacy_rewards.calculate(
                                        complete_feed,
                                        legacy_scored,
                                        block=finalized_block,
                                        hotkey_to_uid=hotkey_to_uid,
                                        uids=uids,
                                        pricing=pricing,
                                    )
                                    legacy_connections.activate_referrals(
                                        {
                                            item.tweet.author
                                            for item in legacy_scored
                                            if any(
                                                reward.tweet_id == item.tweet.tweet_id
                                                for reward in legacy_floors
                                            )
                                        }
                                    )
                                    account_to_uid = legacy_connections.resolve_uids(
                                        hotkey_to_uid,
                                        nocode_uid=self.settings.legacy_nocode_uid,
                                    )
                                    cached_legacy_weights = legacy_rewards.apply_referrals(
                                        legacy_weights,
                                        legacy_connections.due_referrals(datetime.now(UTC).date()),
                                        account_to_uid,
                                        daily_usd=pricing.daily_miner_usd,
                                    )
                                    if legacy_result_publisher is not None:
                                        await legacy_result_publisher.publish(
                                            complete_feed,
                                            legacy_scored,
                                            legacy_floors,
                                            block=finalized_block,
                                            hotkey_to_uid=hotkey_to_uid,
                                            pricing=pricing,
                                        )
                                combined_weights = combine_weights(
                                    cached_legacy_weights or {0: 1.0},
                                    productive_weights,
                                    uids=uids,
                                )
                                LOGGER.info(
                                    "combined legacy and preclaim weights "
                                    "block=%s legacy_burn=%s productive_miners=%s",
                                    finalized_block,
                                    (cached_legacy_weights or {0: 1.0}).get(0, 0.0),
                                    sum(
                                        uid != 0 and weight > 0
                                        for uid, weight in productive_weights.items()
                                    ),
                                )
                                if preclaim_weights_complete:
                                    store.persist_shadow_weights(
                                        finalized_block,
                                        complete_feed.snapshot_id,
                                        combined_weights,
                                    )
                                    submission_weights = combined_weights
                            if result_publisher is not None:
                                await result_publisher.publish(
                                    feed,
                                    scored,
                                    floors,
                                    block=finalized_block,
                                    hotkey_to_uid=hotkey_to_uid,
                                )
                            if (
                                self.settings.enable_weight_submission
                                and submission_weights is not None
                            ):
                                await submit_weights_if_due(
                                    chain,
                                    wallet,
                                    graph,
                                    submission_weights,
                                    block=finalized_block,
                                    epoch_blocks=self.settings.weight_epoch_blocks,
                                    version_key=self.settings.weight_version_key,
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
                if not ops_server.should_exit:
                    await asyncio.sleep(self.settings.validator_poll_seconds)
        finally:
            health.ready = False
            if ops_server is not None:
                ops_server.should_exit = True
            if ops_task is not None:
                with suppress(asyncio.CancelledError):
                    await ops_task
            if campaign_client is not None:
                await campaign_client.close()
            if x_provider is not None:
                await x_provider.close()
            if data_publisher is not None:
                await data_publisher.close()
            if brief_filter is not None:
                await brief_filter.close()
            if preview_store is not None:
                preview_store.close()
            if legacy_pricing is not None:
                await legacy_pricing.close()
            if legacy_collector is not None:
                await legacy_collector.close()
            if legacy_tweet_store is not None:
                legacy_tweet_store.close()
            await chain.close()
            await shutdown_loki_logging()
