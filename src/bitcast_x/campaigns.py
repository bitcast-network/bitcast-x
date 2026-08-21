"""Versioned public campaign and digest-addressed ecosystem-map feed client."""

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from bitcast_x.campaign_urls import CAMPAIGN_FEED_URL, LEGACY_CAMPAIGN_FEED_URL
from bitcast_x.protocol import CampaignAccess


class SocialAccount(BaseModel):
    """Time-frozen influence entry used by v2-compatible engagement scoring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x_id: str = Field(pattern=r"^[0-9]+$")
    username: str = Field(min_length=1, max_length=15)
    influence: float = Field(ge=0)
    followers_count: int = Field(default=0, ge=0)


class RelationshipEdge(BaseModel):
    """Sparse cumulative interaction score for cabal protection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_username: str = Field(min_length=1, max_length=15)
    target_username: str = Field(min_length=1, max_length=15)
    score: float = Field(gt=0)


class EcosystemMap(BaseModel):
    """One public ecosystem eligibility map referenced by campaigns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ecosystem_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    eligible_creator_x_ids: tuple[str, ...]
    updated_at: datetime
    accounts: tuple[SocialAccount, ...] = ()
    relationships: tuple[RelationshipEdge, ...] = ()
    max_referral_amount: float = Field(default=100.0, ge=0)

    @field_validator("updated_at")
    @classmethod
    def normalize_updated_at(cls, value: datetime) -> datetime:
        """Require a timezone-aware map activation timestamp."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ecosystem updated_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("eligible_creator_x_ids")
    @classmethod
    def validate_eligible_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject mutable-handle identifiers and ambiguous duplicates."""

        if any(not item.isdigit() for item in value):
            raise ValueError("eligible creators must use immutable numeric X IDs")
        if len(set(value)) != len(value):
            raise ValueError("eligible creator X IDs must be unique")
        return value

    @model_validator(mode="after")
    def validate_graph_entries(self) -> "EcosystemMap":
        """Require unique accounts and relationship endpoints present in the map."""

        x_ids = [item.x_id for item in self.accounts]
        usernames = [item.username.lower() for item in self.accounts]
        if len(set(x_ids)) != len(x_ids) or len(set(usernames)) != len(usernames):
            raise ValueError("ecosystem accounts must have unique X IDs and usernames")
        known = set(usernames)
        if any(
            edge.source_username.lower() not in known or edge.target_username.lower() not in known
            for edge in self.relationships
        ):
            raise ValueError("relationship endpoints must exist in ecosystem accounts")
        return self


class CampaignRecord(BaseModel):
    """Public campaign opportunity and its consensus access fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    access: CampaignAccess
    display: str = Field(
        min_length=1,
        max_length=512,
        validation_alias=AliasChoices("display", "title"),
    )
    brief: str = Field(min_length=1, max_length=20_000)
    pools: tuple[str, ...] = Field(
        min_length=1,
        validation_alias=AliasChoices("pools", "ecosystem_id"),
    )
    opens_at: datetime
    closes_at: datetime
    reward_pool_usd: str
    required_terms: tuple[str, ...] = ()
    tag: str | None = Field(default=None, min_length=1, max_length=256)
    quoted_tweet_id: str | None = Field(default=None, pattern=r"^[0-9]+$")
    inclusion_keywords: tuple[str, ...] = ()
    prompt_version: Literal[1, 2, 5, 6] = 1
    max_tweets_per_creator: int | None = Field(default=None, ge=1)
    max_members: int | None = Field(default=None, ge=1)
    cap: float | None = Field(default=None, gt=0, le=1)
    emission_start_block: int | None = Field(default=None, ge=0)
    emission_end_block: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def reject_removed_language_filter(cls, value: Any) -> Any:
        """Reject language filters while tolerating old serialized null placeholders."""

        if not isinstance(value, dict) or "language" not in value:
            return value
        if value["language"] is not None:
            raise ValueError("campaign language filters are not supported")
        normalized = dict(value)
        normalized.pop("language")
        return normalized

    @field_validator("pools", mode="before")
    @classmethod
    def validate_pools(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            value = (value,)
        if not isinstance(value, (list, tuple)):
            raise ValueError("campaign pools must be a list")
        normalized = tuple(pool.strip() for pool in value)
        if any(not pool for pool in normalized):
            raise ValueError("campaign pools cannot be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("campaign pools must be unique")
        return normalized

    @property
    def primary_pool(self) -> str:
        """Return the first configured pool for legacy single-pool persistence."""
        return self.pools[0]

    @field_validator("opens_at", "closes_at")
    @classmethod
    def normalize_campaign_time(cls, value: datetime) -> datetime:
        """Require timezone-aware campaign windows and normalize them to UTC."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("campaign timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("reward_pool_usd")
    @classmethod
    def validate_reward_pool(cls, value: str) -> str:
        """Require a finite positive campaign floor without changing its wire string."""

        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("reward_pool_usd must be numeric") from exc
        if not amount.is_finite() or amount <= 0:
            raise ValueError("reward_pool_usd must be finite and positive")
        return value

    @field_validator("required_terms", "inclusion_keywords")
    @classmethod
    def validate_content_terms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank or duplicate campaign content terms."""

        stripped = tuple(item.strip() for item in value)
        if any(not item for item in stripped):
            raise ValueError("campaign content terms cannot be blank")
        if len({item.casefold() for item in stripped}) != len(stripped):
            raise ValueError("campaign content terms must be unique")
        return stripped

    @model_validator(mode="after")
    def validate_windows(self) -> "CampaignRecord":
        """Require coherent campaign dates and paired emission-block bounds."""

        if self.opens_at >= self.closes_at:
            raise ValueError("campaign opens_at must precede closes_at")
        if (self.emission_start_block is None) != (self.emission_end_block is None):
            raise ValueError("campaign emission block bounds must be supplied together")
        if (
            self.emission_start_block is not None
            and self.emission_end_block is not None
            and self.emission_start_block > self.emission_end_block
        ):
            raise ValueError("campaign emission_start_block cannot exceed emission_end_block")
        if (
            self.emission_start_block is not None
            and self.emission_start_block <= self.access.scoring_close_block
        ):
            raise ValueError("campaign emission must begin after scoring_close_block")
        return self


class CampaignFeed(BaseModel):
    """Immutable versioned snapshot shared by all miner platforms."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: int = Field(default=2, frozen=True)
    snapshot_id: str = Field(min_length=1, max_length=256)
    published_at: datetime
    campaigns: tuple[CampaignRecord, ...]
    ecosystem_maps: tuple[EcosystemMap, ...]

    @field_validator("published_at")
    @classmethod
    def normalize_published_at(cls, value: datetime) -> datetime:
        """Require a timezone-aware feed publication timestamp."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("feed published_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_snapshot_uniqueness(self) -> "CampaignFeed":
        """Reject ambiguous duplicate campaign IDs or map activation versions."""

        campaign_ids = [item.access.campaign_id for item in self.campaigns]
        if len(set(campaign_ids)) != len(campaign_ids):
            raise ValueError("campaign IDs must be unique within a feed snapshot")
        map_versions = [(item.ecosystem_id, item.updated_at) for item in self.ecosystem_maps]
        if len(set(map_versions)) != len(map_versions):
            raise ValueError("ecosystem map versions must be unique within a feed snapshot")
        return self


class EcosystemMapReference(BaseModel):
    """Immutable pointer to one separately downloadable ecosystem map."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ecosystem_id: str = Field(min_length=1, max_length=128)
    run_id: int = Field(ge=1)
    updated_at: datetime
    digest: str = Field(pattern=r"^sha256-[0-9a-f]{64}$")
    path: str = Field(pattern=r"^/api/v2/public/x/ecosystem-maps/")
    size_bytes: int = Field(ge=1)

    @field_validator("updated_at")
    @classmethod
    def normalize_updated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ecosystem map reference timestamp must be timezone-aware")
        return value.astimezone(UTC)


class CampaignManifest(BaseModel):
    """Small protocol-v3 index whose maps are fetched only when needed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[3, 4] = Field(default=3, frozen=True)
    snapshot_id: str = Field(min_length=1, max_length=256)
    published_at: datetime
    campaigns: tuple[CampaignRecord, ...]
    ecosystem_maps: tuple[EcosystemMapReference, ...]

    @field_validator("published_at")
    @classmethod
    def normalize_published_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("manifest published_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_uniqueness(self) -> "CampaignManifest":
        campaign_ids = [item.access.campaign_id for item in self.campaigns]
        if len(set(campaign_ids)) != len(campaign_ids):
            raise ValueError("campaign IDs must be unique within a manifest")
        identities = [(item.ecosystem_id, item.run_id) for item in self.ecosystem_maps]
        if len(set(identities)) != len(identities):
            raise ValueError("ecosystem map references must be unique")
        has_rank_cutoffs = tuple(item.max_members is not None for item in self.campaigns)
        if self.protocol_version == 4 and not all(has_rank_cutoffs):
            raise ValueError("manifest v4 campaigns must define max_members")
        if self.protocol_version == 3 and any(has_rank_cutoffs):
            raise ValueError("manifest v3 campaigns cannot define max_members")
        return self


def ecosystem_map_at(
    feed: CampaignFeed,
    ecosystem_id: str,
    timestamp: datetime,
) -> EcosystemMap | None:
    """Return the latest ecosystem map that was public at ``timestamp``."""

    candidates = (
        item
        for item in feed.ecosystem_maps
        if item.ecosystem_id == ecosystem_id and item.updated_at <= timestamp
    )
    return max(candidates, key=lambda item: item.updated_at, default=None)


def ecosystem_maps_for_campaign(
    feed: CampaignFeed,
    campaign: CampaignRecord,
) -> tuple[EcosystemMap, ...]:
    """Return every ecosystem map whose active interval overlaps the campaign."""

    relevant: list[EcosystemMap] = []
    for pool in campaign.pools:
        maps = sorted(
            (item for item in feed.ecosystem_maps if item.ecosystem_id == pool),
            key=lambda item: item.updated_at,
        )
        relevant.extend(
            item
            for index, item in enumerate(maps)
            if item.updated_at <= campaign.closes_at
            and (index + 1 == len(maps) or maps[index + 1].updated_at >= campaign.opens_at)
        )
    return tuple(relevant)


def eligible_creator_ids_in_map(
    ecosystem: EcosystemMap,
    max_members: int | None,
) -> frozenset[str]:
    """Return the explicit eligible set, optionally limited to deterministic top-N rank."""
    explicit = frozenset(ecosystem.eligible_creator_x_ids)
    if max_members is None:
        return explicit
    ranked = sorted(
        (account for account in ecosystem.accounts if account.x_id in explicit),
        key=lambda account: (-account.influence, int(account.x_id)),
    )
    return frozenset(account.x_id for account in ranked[:max_members])


def eligible_creator_ids_for_campaign(
    feed: CampaignFeed,
    campaign: CampaignRecord,
    *,
    ecosystem_id: str | None = None,
) -> frozenset[str]:
    """Union the top-ranked creator IDs from every map active during the campaign."""

    return frozenset(
        creator_x_id
        for ecosystem in ecosystem_maps_for_campaign(feed, campaign)
        if ecosystem_id is None or ecosystem.ecosystem_id == ecosystem_id
        for creator_x_id in eligible_creator_ids_in_map(ecosystem, campaign.max_members)
    )


def considered_accounts_for_campaign(
    feed: CampaignFeed,
    campaign: CampaignRecord,
    *,
    ecosystem_id: str | None = None,
    stale_decay: float = 0.5,
) -> tuple[dict[str, float], EcosystemMap]:
    """Reproduce v2's latest-full / dropped-account-decayed influence map."""

    selected_pool = ecosystem_id or campaign.primary_pool
    if selected_pool not in campaign.pools:
        raise ValueError(f"pool {selected_pool} is not configured for the campaign")
    maps = tuple(
        item
        for item in ecosystem_maps_for_campaign(feed, campaign)
        if item.ecosystem_id == selected_pool
    )
    if not maps:
        raise ValueError(f"campaign {campaign.access.campaign_id} has no ecosystem maps")
    latest = maps[-1]
    considered = {account.username.casefold(): account.influence for account in latest.accounts}
    older_scores: dict[str, float] = {}
    for ecosystem in maps[:-1]:
        for account in ecosystem.accounts:
            older_scores[account.username.casefold()] = account.influence
    for username, influence in older_scores.items():
        considered.setdefault(username, influence * stale_decay)
    return considered, latest


class CampaignFeedClient:
    """Fetch a bounded manifest and cache immutable maps by verified digest."""

    def __init__(
        self,
        url: str,
        *,
        cache_path: Path,
        timeout: float = 15.0,
        max_response_bytes: int = 2_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized_url = url.rstrip("/")
        self._document_urls: tuple[str, ...]
        if normalized_url == LEGACY_CAMPAIGN_FEED_URL:
            self.url = CAMPAIGN_FEED_URL
            self._document_urls = (CAMPAIGN_FEED_URL, LEGACY_CAMPAIGN_FEED_URL)
        elif normalized_url == CAMPAIGN_FEED_URL:
            self.url = CAMPAIGN_FEED_URL
            self._document_urls = (CAMPAIGN_FEED_URL, LEGACY_CAMPAIGN_FEED_URL)
        else:
            self.url = normalized_url
            self._document_urls = (normalized_url,)
        self.cache_path = cache_path
        self._max_response_bytes = max_response_bytes
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )

    async def close(self) -> None:
        """Close the underlying connection pool."""

        await self._client.aclose()

    async def fetch(self) -> CampaignFeed:
        """Return a full feed, downloading only maps absent from the local cache."""

        document = await self._fetch_document()
        if isinstance(document, CampaignFeed):
            return document
        maps = await asyncio.gather(*(self._resolve_map(item) for item in document.ecosystem_maps))
        return CampaignFeed(
            protocol_version=2,
            snapshot_id=document.snapshot_id,
            published_at=document.published_at,
            campaigns=document.campaigns,
            ecosystem_maps=tuple(maps),
        )

    async def fetch_campaigns(self) -> tuple[CampaignRecord, ...]:
        """Fetch campaign metadata without downloading referenced ecosystem maps."""

        document = await self._fetch_document()
        return document.campaigns

    def cached(self) -> CampaignFeed | None:
        """Return the last valid local snapshot without making a network request."""

        cached = self._read_cache()
        if cached is None or cached.get("url") not in self._document_urls:
            return None
        if "feed" in cached:
            return CampaignFeed.model_validate(cached["feed"])
        manifest = CampaignManifest.model_validate(cached["manifest"])
        maps: list[EcosystemMap] = []
        for reference in manifest.ecosystem_maps:
            ecosystem_map = self._read_map_cache(reference)
            if ecosystem_map is None:
                return None
            maps.append(ecosystem_map)
        return CampaignFeed(
            protocol_version=2,
            snapshot_id=manifest.snapshot_id,
            published_at=manifest.published_at,
            campaigns=manifest.campaigns,
            ecosystem_maps=tuple(maps),
        )

    async def _fetch_document(self) -> CampaignFeed | CampaignManifest:
        stored = self._read_cache()
        for index, document_url in enumerate(self._document_urls):
            # ETags are only meaningful for the resource that issued them.
            cached = stored if stored is not None and stored.get("url") == document_url else None
            headers = (
                {"if-none-match": str(cached["etag"])} if cached and cached.get("etag") else {}
            )
            try:
                payload, etag, not_modified = await self._get_bounded(document_url, headers=headers)
            except httpx.HTTPStatusError as exc:
                if index == 0 and exc.response.status_code == httpx.codes.NOT_FOUND:
                    continue
                raise
            if not_modified:
                if cached is None:
                    raise ValueError("campaign endpoint returned 304 without a local cache")
                if "manifest" in cached:
                    return CampaignManifest.model_validate(cached["manifest"])
                return CampaignFeed.model_validate(cached["feed"])

            raw = TypeAdapter(dict[str, Any]).validate_json(payload)
            if raw.get("protocol_version") in {3, 4}:
                manifest = CampaignManifest.model_validate(raw)
                self._write_document_cache(
                    "manifest",
                    manifest.model_dump(mode="json"),
                    etag,
                    source_url=document_url,
                )
                return manifest
            feed = CampaignFeed.model_validate(raw)
            self._write_document_cache(
                "feed",
                feed.model_dump(mode="json"),
                etag,
                source_url=document_url,
            )
            return feed
        raise ValueError("no supported campaign manifest endpoint is available")

    async def _resolve_map(self, reference: EcosystemMapReference) -> EcosystemMap:
        cached = self._read_map_cache(reference)
        if cached is not None:
            return cached
        map_url = urljoin(self.url, reference.path)
        if urlsplit(map_url).netloc != urlsplit(self.url).netloc:
            raise ValueError("ecosystem map reference must use the campaign endpoint origin")
        payload, _etag, not_modified = await self._get_bounded(map_url)
        if not_modified:
            raise ValueError("ecosystem map returned 304 without a local cache")
        ecosystem_map = EcosystemMap.model_validate_json(payload)
        self._validate_map(reference, ecosystem_map)
        self._write_map_cache(reference, ecosystem_map)
        return ecosystem_map

    async def _get_bounded(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> tuple[bytes, str | None, bool]:
        async with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code == httpx.codes.NOT_MODIFIED:
                return b"", response.headers.get("etag"), True
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self._max_response_bytes:
                    raise ValueError("campaign response exceeds configured byte limit")
                chunks.append(chunk)
            return b"".join(chunks), response.headers.get("etag"), False

    def _read_cache(self) -> dict[str, Any] | None:
        if not self.cache_path.exists():
            return None
        return TypeAdapter(dict[str, Any]).validate_json(self.cache_path.read_bytes())

    def _write_document_cache(
        self,
        kind: str,
        document: Any,
        etag: str | None,
        *,
        source_url: str,
    ) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        payload = TypeAdapter(dict[str, Any]).dump_json(
            {"url": source_url, "etag": etag, kind: document}
        )
        temporary.write_bytes(payload)
        os.replace(temporary, self.cache_path)

    @property
    def _map_cache_dir(self) -> Path:
        return self.cache_path.parent / f"{self.cache_path.name}.maps"

    def _map_cache_path(self, reference: EcosystemMapReference) -> Path:
        return self._map_cache_dir / f"{reference.digest}.json"

    def _read_map_cache(self, reference: EcosystemMapReference) -> EcosystemMap | None:
        path = self._map_cache_path(reference)
        if not path.exists():
            return None
        ecosystem_map = EcosystemMap.model_validate_json(path.read_bytes())
        self._validate_map(reference, ecosystem_map)
        return ecosystem_map

    def _write_map_cache(
        self, reference: EcosystemMapReference, ecosystem_map: EcosystemMap
    ) -> None:
        self._map_cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._map_cache_path(reference)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(ecosystem_map.model_dump_json().encode())
        os.replace(temporary, path)

    @staticmethod
    def _validate_map(reference: EcosystemMapReference, ecosystem_map: EcosystemMap) -> None:
        if ecosystem_map.ecosystem_id != reference.ecosystem_id:
            raise ValueError("ecosystem map identity does not match its manifest reference")
        if ecosystem_map.updated_at != reference.updated_at:
            raise ValueError("ecosystem map timestamp does not match its manifest reference")
        if _map_digest(ecosystem_map) != reference.digest:
            raise ValueError("ecosystem map digest does not match its manifest reference")


def _map_digest(ecosystem_map: EcosystemMap) -> str:
    payload = ecosystem_map.model_dump(mode="json")
    # This consumer-only compatibility default is not part of the API's v2 map
    # contract and therefore is deliberately excluded from the wire digest.
    payload.pop("max_referral_amount", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256-{hashlib.sha256(canonical.encode()).hexdigest()}"
