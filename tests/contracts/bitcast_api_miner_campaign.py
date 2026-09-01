"""Pinned Bitcast API models consumed by the miner campaign client.

Vendored from ``bitcast-network/bitcast-api`` at commit
76b16558ae12096ba69d2f4ecfae155b9be4fd92 (2026-09-01). Keep these models
source-identical to ``app/schemas/miner_results.py`` and re-vendor them when
that producer contract changes.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class PublicCampaignStats(BaseModel):
    matched_tweets: int = Field(ge=0)
    total_views: int = Field(ge=0)
    total_engagements: int = Field(ge=0)
    engagement_rate: float = Field(ge=0)
    data_updated_at: datetime | None = None


class MinerCampaignCapabilities(BaseModel):
    can_check_eligibility: bool
    can_claim: bool
    can_submit: bool
    can_view_results: bool
    requires_claim: bool
    is_exclusive_to_this_miner: bool


class MinerCampaign(BaseModel):
    campaign_id: str
    campaign_snapshot_id: str
    ecosystem_ids: list[str]
    status: str
    protocol: dict[str, Any]
    access: dict[str, Any]
    opens_at: datetime
    closes_at: datetime
    scoring_close_block: int
    brief: str
    prompt_version: int = Field(ge=1, le=5)
    x_brief: dict[str, Any]
    required_terms: list[str] = Field(default_factory=list)
    language: str | None = None
    tag: str | None = None
    quoted_tweet_id: str | None = None
    inclusion_keywords: list[str] = Field(default_factory=list)
    reward_pool_usd: Decimal
    max_tweets_per_creator: int | None = None
    ecosystem_rules: list[dict[str, Any]] = Field(default_factory=list)
    presentation: dict[str, Any]
    capabilities: MinerCampaignCapabilities
    stats: PublicCampaignStats
    updated_at: datetime


class MinerEcosystemEligibility(BaseModel):
    ecosystem_id: str
    eligible: bool
    rank: int | None = None
    cutoff: int
    map_run_id: int | None = None
    map_digest: str | None = None
    map_effective_at: datetime | None = None


class MinerCampaignEligibility(BaseModel):
    campaign_id: str
    campaign_snapshot_id: str
    creator_x_id: str
    eligible: bool
    claim_eligible: bool
    eligible_if_published_now: bool
    eligible_ecosystems: list[MinerEcosystemEligibility]
    badges: list[dict[str, str]]
    reason: str
    checked_at: datetime
