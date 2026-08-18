"""Deterministic owner-lock or self-stake miner qualification."""

from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

_RAO_PER_ALPHA = Decimal(1_000_000_000)


class QualificationConfig(BaseModel):
    """Versioned consensus parameters governing miner eligibility."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1)
    owner_hotkey: str = Field(min_length=40, max_length=64)
    minimum_conviction_alpha: Decimal = Field(ge=0)
    minimum_self_stake_alpha: Decimal | None = Field(default=None, ge=0)
    effective_block: int = Field(ge=0)

    @property
    def minimum_conviction_rao(self) -> int:
        """Return the exact integer chain-unit threshold."""

        return int(self.minimum_conviction_alpha * _RAO_PER_ALPHA)

    @property
    def minimum_self_stake_rao(self) -> int | None:
        """Return the exact owner-to-miner stake threshold when that path is configured."""

        if self.minimum_self_stake_alpha is None:
            return None
        return int(self.minimum_self_stake_alpha * _RAO_PER_ALPHA)

    @property
    def financial_barrier_enabled(self) -> bool:
        """Return whether at least one positive qualification path is active."""

        return self.minimum_conviction_rao > 0 or (self.minimum_self_stake_rao or 0) > 0


class QualificationSchedule(BaseModel):
    """Complete published threshold history ordered by effective block."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    configurations: tuple[QualificationConfig, ...]

    @model_validator(mode="after")
    def validate_history(self) -> "QualificationSchedule":
        """Require a non-empty, strictly ordered and uniquely versioned history."""

        if not self.configurations:
            raise ValueError("qualification schedule cannot be empty")
        blocks = [item.effective_block for item in self.configurations]
        versions = [item.version for item in self.configurations]
        if blocks != sorted(blocks) or len(set(blocks)) != len(blocks):
            raise ValueError("qualification effective blocks must be strictly increasing")
        if versions != sorted(versions) or len(set(versions)) != len(versions):
            raise ValueError("qualification versions must be strictly increasing")
        return self

    def at(self, block: int | None) -> QualificationConfig:
        """Select the immutable rule effective at a block, or the latest for a live read."""

        if block is None:
            return self.configurations[-1]
        applicable = [item for item in self.configurations if item.effective_block <= block]
        return applicable[-1] if applicable else self.configurations[0]


class QualificationStatus(BaseModel):
    """Explanatory result from one deterministic qualification read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    miner_hotkey: str
    config_version: int
    controlling_coldkey: str | None
    configured_owner_hotkey: str
    lock_target_hotkey: str | None
    conviction_alpha: Decimal
    required_conviction_alpha: Decimal
    self_stake_alpha: Decimal
    required_self_stake_alpha: Decimal | None
    qualified_via: Literal["owner_lock", "self_stake"] | None
    effective_block: int
    checked_block: int | None
    eligible: bool
    reason: str


class QualificationChain(Protocol):
    """Historical chain reads needed by qualification consensus."""

    async def miner_qualification_inputs(
        self,
        miner_hotkey: str,
        *,
        block: int | None = None,
        include_self_stake: bool = False,
    ) -> tuple[str | None, str | None, int, int]: ...


class QualificationReader:
    """Evaluate the configured financial barrier at a chosen finalized block."""

    def __init__(
        self,
        chain: QualificationChain,
        config: QualificationConfig | QualificationSchedule,
    ) -> None:
        self._chain = chain
        self.schedule = (
            config
            if isinstance(config, QualificationSchedule)
            else QualificationSchedule(configurations=(config,))
        )

    async def read(
        self,
        miner_hotkey: str,
        *,
        block: int | None = None,
    ) -> QualificationStatus:
        """Return eligibility and enough evidence for a platform to explain it."""

        config = self.schedule.at(block)
        (
            coldkey,
            target,
            conviction_rao,
            self_stake_rao,
        ) = await self._chain.miner_qualification_inputs(
            miner_hotkey,
            block=block,
            include_self_stake=(config.minimum_self_stake_rao or 0) > 0,
        )
        minimum_self_stake_rao = config.minimum_self_stake_rao
        lock_path_enabled = config.minimum_conviction_rao > 0
        self_stake_path_enabled = (minimum_self_stake_rao or 0) > 0
        lock_eligible = (
            lock_path_enabled
            and target == config.owner_hotkey
            and conviction_rao >= config.minimum_conviction_rao
        )
        self_stake_eligible = self_stake_path_enabled and self_stake_rao >= (
            minimum_self_stake_rao or 0
        )
        qualified_via: Literal["owner_lock", "self_stake"] | None = None
        if block is not None and block < config.effective_block:
            eligible = False
            reason = "qualification_not_effective"
        elif not config.financial_barrier_enabled:
            eligible = True
            reason = "qualification_disabled"
        elif coldkey is None:
            eligible = False
            reason = "hotkey_has_no_owner"
        elif lock_eligible:
            eligible = True
            reason = "eligible"
            qualified_via = "owner_lock"
        elif self_stake_eligible:
            eligible = True
            reason = "eligible"
            qualified_via = "self_stake"
        elif lock_path_enabled and self_stake_path_enabled:
            eligible = False
            reason = "neither_qualification_path_met"
        elif self_stake_path_enabled:
            eligible = False
            reason = "self_stake_below_minimum"
        elif target != config.owner_hotkey:
            eligible = False
            reason = "lock_targets_different_hotkey"
        else:
            eligible = False
            reason = "conviction_below_minimum"
        return QualificationStatus(
            miner_hotkey=miner_hotkey,
            config_version=config.version,
            controlling_coldkey=coldkey,
            configured_owner_hotkey=config.owner_hotkey,
            lock_target_hotkey=target,
            conviction_alpha=Decimal(conviction_rao) / _RAO_PER_ALPHA,
            required_conviction_alpha=config.minimum_conviction_alpha,
            self_stake_alpha=Decimal(self_stake_rao) / _RAO_PER_ALPHA,
            required_self_stake_alpha=config.minimum_self_stake_alpha,
            qualified_via=qualified_via,
            effective_block=config.effective_block,
            checked_block=block,
            eligible=eligible,
            reason=reason,
        )


class HistoricalQualificationChecker:
    """Boolean adapter used by consensus replay while retaining explanatory reads elsewhere."""

    def __init__(self, reader: QualificationReader) -> None:
        self._reader = reader

    async def eligible(self, miner_hotkey: str, block: int) -> bool:
        """Return eligibility at an exact historical finalized block."""

        return (await self._reader.read(miner_hotkey, block=block)).eligible
