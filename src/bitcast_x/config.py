"""Typed runtime configuration for Bitcast X v3."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from bitcast_x.campaign_urls import CAMPAIGN_FEED_URL
from bitcast_x.legacy.constants import LEGACY_NOCODE_UID

LEGACY_CONNECTION_TWEET_IDS = "2031383975088836738"
QUALIFICATION_OWNER_HOTKEY = "5DAoDtMxVqtMu2Nd5E7QhPEGXDMgrySvE1b3rRT5ARDhfNNK"


class Settings(BaseSettings):
    """Environment-driven settings shared by miner and validator processes."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BITCAST_X_",
        extra="ignore",
        frozen=True,
    )

    network: str = "finney"
    netuid: int = 93
    mechanism_id: int = 1
    wallet_name: str = "default"
    wallet_hotkey: str = "default"
    wallet_path: Path = Path.home() / ".bittensor" / "wallets"
    state_dir: Path = Path.home() / ".bitcast-x"
    host: str = "0.0.0.0"  # noqa: S104 - miner service must be externally reachable
    port: int = Field(default=8095, ge=1, le=65535)
    public_ip: str | None = None
    auth_max_age_seconds: float = Field(default=10.0, gt=0)
    auth_allowed_skew_seconds: float = Field(default=2.0, ge=0)
    validator_requests_per_minute: int = Field(default=120, ge=1, le=10_000)
    request_timeout_seconds: float = Field(default=15.0, gt=0)
    max_request_bytes: int = Field(default=16_384, ge=1)
    max_response_bytes: int = Field(default=2_000_000, ge=1)
    campaign_feed_max_response_bytes: int = Field(default=16_000_000, ge=1)
    max_batches_per_page: int = Field(default=50, ge=1, le=50)
    campaign_feed_url: str | None = CAMPAIGN_FEED_URL
    miner_api_token: SecretStr | None = Field(default=None, repr=False)
    miner_api_commit_timeout_seconds: float = Field(default=90.0, gt=0, le=300)
    miner_results_api_url: str = "https://bitcast-api.bitcast.network"
    miner_results_poll_seconds: float = Field(default=30.0, ge=5.0, le=300.0)
    legacy_connections_path: Path | None = None
    legacy_snapshots_path: Path | None = None
    legacy_tweet_store_path: Path | None = None
    legacy_nocode_uid: int = Field(default=LEGACY_NOCODE_UID, ge=0)
    legacy_connection_tweet_ids: str = LEGACY_CONNECTION_TWEET_IDS
    legacy_fasttrack_url: str = "https://www.stitch3.ai/api/fast-track"
    qualification_owner_hotkey: str | None = QUALIFICATION_OWNER_HOTKEY
    qualification_minimum_alpha: str = "15000"
    qualification_minimum_self_stake_alpha: str | None = None
    qualification_effective_block: int = Field(default=0, ge=0)
    qualification_schedule_json: str | None = Field(default=None, repr=False)
    batch_max_age_seconds: float = Field(default=5.0, gt=0)
    batch_max_events: int = Field(default=100, ge=1, le=65_535)
    batch_max_bytes: int = Field(default=512_000, ge=1)
    pending_max_events: int = Field(default=10_000, ge=1)
    pending_max_bytes: int = Field(default=50_000_000, ge=1)
    validator_poll_seconds: float = Field(default=12.0, gt=0)
    validator_max_concurrency: int = Field(default=16, ge=1, le=256)
    desearch_api_key: str | None = Field(default=None, repr=False)
    llm_provider: Literal["chutes", "openrouter"] = "chutes"
    chutes_api_key: str | None = Field(default=None, repr=False)
    openrouter_api_key: str | None = Field(default=None, repr=False)
    llm_num_checks: int = Field(default=3, ge=1, le=10)
    llm_tweet_max_length: int = Field(default=10_000, ge=1, le=100_000)
    enable_data_publish: bool = True
    enable_weight_submission: bool = True
    weight_epoch_blocks: int = Field(default=100, ge=1)
    weight_version_key: int = Field(default=0, ge=0)
    data_client_url: str = "https://ingestion.bitcast.network:443"
    ops_host: str = "0.0.0.0"  # noqa: S104 - container health endpoint
    ops_port: int = Field(default=8096, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    # Shared write-only Loki credentials keep decentralized operators zero-config.
    # Override any value through BITCAST_X_LOKI_* or set the URL empty to disable.
    loki_url: str | None = "https://logs-prod-042.grafana.net"
    loki_username: str | None = "1693344"
    loki_token: SecretStr | None = Field(
        default=SecretStr("REPLACE_WITH_PUBLIC_WRITE_ONLY_LOKI_TOKEN"),
        repr=False,
    )
    auto_update: bool = False
    auto_update_ref: str = "origin/main"
    auto_update_interval_seconds: float = Field(default=900.0, ge=60.0)
    auto_update_startup_grace_seconds: float = Field(default=30.0, ge=5.0)
    auto_update_shutdown_timeout_seconds: float = Field(default=3600.0, ge=30.0)
    auto_update_dir: Path = Path.home() / ".cache" / "bitcast-x" / "updates"

    @property
    def llm_api_key(self) -> str | None:
        """Return the credential for the selected v2-compatible LLM provider."""

        return self.chutes_api_key if self.llm_provider == "chutes" else self.openrouter_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide immutable settings object."""

    return Settings()
