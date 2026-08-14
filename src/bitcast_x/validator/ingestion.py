"""Finalized commitment scanning and deterministic miner reconciliation."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from bitcast_x.chain import BittensorChain
from bitcast_x.errors import (
    ChainOperationError,
    ProtocolError,
    ResponseTooLargeError,
)
from bitcast_x.protocol import BatchChainVerifier, CommittedBatch
from bitcast_x.transport import BatchPageRequest, SignedMinerClient
from bitcast_x.validator.store import ValidatorStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MinerEndpoint:
    """Registered miner hotkey and its finalized metagraph HTTP endpoint."""

    hotkey: str
    base_url: str


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Operational outcome for one miner; availability is not attribution."""

    hotkey: str
    batches_verified: int
    cursor: int
    available: bool
    quarantined: bool
    error: str | None = None


class MinerClient(Protocol):
    """Bounded signed miner client used by reconciliation."""

    async def fetch_batches(self, request: BatchPageRequest) -> Any: ...

    async def close(self) -> None: ...


class MinerClientFactory(Protocol):
    """Construct one recipient-bound client for a discovered endpoint."""

    def __call__(self, endpoint: MinerEndpoint) -> MinerClient: ...


class ValidatorIngestor:
    """Persist finalized anchors, discover miners, and advance independent cursors."""

    def __init__(
        self,
        chain: BittensorChain,
        store: ValidatorStore,
        *,
        client_factory: MinerClientFactory,
        max_concurrency: int = 16,
        max_pages_per_run: int = 100,
        page_size: int = 50,
    ) -> None:
        if max_concurrency <= 0 or max_pages_per_run <= 0 or not 1 <= page_size <= 50:
            raise ValueError("validator ingestion limits must be positive and bounded")
        self.chain = chain
        self.store = store
        self._client_factory = client_factory
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_pages = max_pages_per_run
        self._page_size = page_size

    async def discover(self, *, block: int | None = None) -> list[MinerEndpoint]:
        """Discover registered, serving miner endpoints from one metagraph snapshot."""

        graph = await self.chain.metagraph(block=block)
        if graph is None:
            return []
        endpoints: list[MinerEndpoint] = []
        for neuron in graph.neurons:
            axon = str(neuron.axon or "")
            if not axon or axon.endswith(":0"):
                continue
            endpoints.append(MinerEndpoint(hotkey=str(neuron.hotkey), base_url=f"http://{axon}"))
        return sorted(endpoints, key=lambda endpoint: endpoint.hotkey)

    async def reconcile_all(
        self, endpoints: list[MinerEndpoint], *, block: int | None = None
    ) -> list[ReconciliationResult]:
        """Query miners concurrently while isolating each cursor and failure."""

        return list(
            await asyncio.gather(
                *(self._bounded_reconcile(item, block=block) for item in endpoints)
            )
        )

    async def _bounded_reconcile(
        self, endpoint: MinerEndpoint, *, block: int | None
    ) -> ReconciliationResult:
        async with self._semaphore:
            return await self.reconcile(endpoint, block=block)

    async def reconcile(
        self, endpoint: MinerEndpoint, *, block: int | None = None
    ) -> ReconciliationResult:
        """Verify contiguous pages for one miner and persist each batch before cursor advance."""

        client = self._client_factory(endpoint)
        start_sequence, start_hash = self.store.cursor(endpoint.hotkey)
        verifier = BatchChainVerifier(endpoint.hotkey)
        verifier.last_sequence = start_sequence
        verifier.last_batch_hash = start_hash
        verified = 0
        try:
            expected = await self.chain.latest_commitment_envelope(endpoint.hotkey, block=block)
            if expected is None:
                if start_sequence != 0:
                    raise ProtocolError("verified history has no latest on-chain commitment")
                return ReconciliationResult(
                    hotkey=endpoint.hotkey,
                    batches_verified=0,
                    cursor=0,
                    available=True,
                    quarantined=False,
                )
            if expected.sequence < start_sequence:
                raise ProtocolError("latest on-chain commitment regressed behind verified history")
            if expected.sequence == start_sequence:
                if expected.batch_hash.hex() != start_hash:
                    raise ProtocolError("latest on-chain commitment changed verified history")
                return ReconciliationResult(
                    hotkey=endpoint.hotkey,
                    batches_verified=0,
                    cursor=start_sequence,
                    available=True,
                    quarantined=False,
                )
            after = start_sequence
            for _page_number in range(self._max_pages):
                response = await client.fetch_batches(
                    BatchPageRequest(
                        after_sequence=after,
                        through_sequence=expected.sequence,
                        max_batches=self._page_size,
                    )
                )
                if response.miner_hotkey != endpoint.hotkey:
                    raise ProtocolError("response belongs to a different miner hotkey")
                if not response.batches and response.has_more:
                    raise ProtocolError("empty batch page cannot declare more results")
                page_batches: list[CommittedBatch] = []
                for item in response.batches:
                    batch = CommittedBatch.model_validate(item.batch)
                    observation = await self.chain.commitment_at_position(
                        endpoint.hotkey,
                        item.position,
                    )
                    verifier.verify_and_advance(batch, observation.envelope)
                    self.store.persist_verified(batch, observation)
                    page_batches.append(batch)
                    verified += 1
                expected_next = page_batches[-1].sequence if page_batches else after
                if response.next_sequence != expected_next:
                    raise ProtocolError("response next_sequence does not match page content")
                after = expected_next
                if not response.has_more:
                    if expected.sequence != after:
                        raise ProtocolError(
                            f"manifest gap: miner returned through sequence {after}, "
                            f"chain reports {expected.sequence}"
                        )
                    if expected.batch_hash.hex() != verifier.last_batch_hash:
                        raise ProtocolError("latest on-chain commitment changed verified history")
                    result = ReconciliationResult(
                        hotkey=endpoint.hotkey,
                        batches_verified=verified,
                        cursor=after,
                        available=True,
                        quarantined=False,
                    )
                    if verified:
                        LOGGER.info(
                            "miner reconciliation complete hotkey=%s endpoint=%s "
                            "batches_verified=%s cursor=%s",
                            endpoint.hotkey,
                            endpoint.base_url,
                            verified,
                            after,
                        )
                    return result
            raise ProtocolError("miner pagination exceeded the per-run page limit")
        except (ProtocolError, ValidationError) as exc:
            self.store.record_error(endpoint.hotkey, str(exc))
            cursor, _hash = self.store.cursor(endpoint.hotkey)
            LOGGER.warning(
                "miner reconciliation quarantined hotkey=%s endpoint=%s cursor=%s "
                "batches_verified=%s error=%s",
                endpoint.hotkey,
                endpoint.base_url,
                cursor,
                verified,
                exc,
            )
            return ReconciliationResult(
                hotkey=endpoint.hotkey,
                batches_verified=verified,
                cursor=cursor,
                available=True,
                quarantined=True,
                error=str(exc),
            )
        except (ChainOperationError, httpx.HTTPError, OSError, ResponseTooLargeError) as exc:
            self.store.record_error(endpoint.hotkey, f"unavailable: {exc}")
            cursor, _hash = self.store.cursor(endpoint.hotkey)
            LOGGER.warning(
                "miner reconciliation unavailable hotkey=%s endpoint=%s cursor=%s "
                "batches_verified=%s error=%s",
                endpoint.hotkey,
                endpoint.base_url,
                cursor,
                verified,
                exc,
            )
            return ReconciliationResult(
                hotkey=endpoint.hotkey,
                batches_verified=verified,
                cursor=cursor,
                available=False,
                quarantined=False,
                error=str(exc),
            )
        finally:
            await client.close()


def signed_client_factory(
    wallet: Any,
    *,
    timeout: float,
    max_response_bytes: int,
) -> MinerClientFactory:
    """Build the production recipient-bound signed HTTP client factory."""

    def create(endpoint: MinerEndpoint) -> SignedMinerClient:
        return SignedMinerClient(
            wallet,
            miner_hotkey=endpoint.hotkey,
            base_url=endpoint.base_url,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )

    return create
