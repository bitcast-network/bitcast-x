"""Tests for deterministic miner conviction qualification."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from bitcast_x.qualification import (
    PUBLIC_FINNEY_QUALIFICATION_SCHEDULE,
    QualificationConfig,
    QualificationReader,
    QualificationSchedule,
    resolve_qualification_policy,
)

MINER = "5E2FKe891uQ7Y1xQ1PLjU7WAouhkxbdJhmovEapJ2cUQv5oA"
OWNER = "5FHneW46xGXgs5mUiveU4sbTyGBzmst2jfFvCw9zThqAXhGK"
COLDKEY = "5DAAnrj7VHTz5f4tY8cnLXbd3P4H8R3fP8y4oPmEoQWmWZJv"


class FakeChain:
    def __init__(
        self,
        target: str | None,
        conviction_rao: int,
        coldkey: str | None = COLDKEY,
        self_stake_rao: int = 0,
    ):
        self.inputs = (coldkey, target, conviction_rao, self_stake_rao)

    async def miner_qualification_inputs(
        self,
        miner_hotkey: str,
        *,
        block: int | None = None,
        include_self_stake: bool = False,
    ) -> tuple[str | None, str | None, int, int]:
        assert miner_hotkey == MINER
        assert block is not None
        if self.inputs[3] > 0:
            assert include_self_stake is True
        return self.inputs


def config() -> QualificationConfig:
    return QualificationConfig(
        owner_hotkey=OWNER,
        minimum_conviction_alpha=Decimal("250.5"),
        effective_block=90,
    )


def test_public_finney_schedule_ships_with_both_qualification_paths() -> None:
    before = PUBLIC_FINNEY_QUALIFICATION_SCHEDULE.at(8_873_999)
    active = PUBLIC_FINNEY_QUALIFICATION_SCHEDULE.at(8_874_000)

    assert before.version == 1
    assert before.minimum_self_stake_alpha is None
    assert active.version == 2
    assert active.minimum_conviction_alpha == Decimal("15000")
    assert active.minimum_self_stake_alpha == Decimal("15000")


def test_finney_ignores_stale_environment_qualification_policy() -> None:
    stale_schedule = QualificationSchedule(
        configurations=(
            QualificationConfig(
                version=1,
                owner_hotkey=OWNER,
                minimum_conviction_alpha=Decimal("250"),
                effective_block=0,
            ),
        )
    ).model_dump_json()

    policy = resolve_qualification_policy(
        network="finney",
        netuid=93,
        schedule_json=stale_schedule,
        owner_hotkey=OWNER,
        minimum_conviction_alpha="250",
        minimum_self_stake_alpha=None,
        effective_block=0,
    )

    assert policy is PUBLIC_FINNEY_QUALIFICATION_SCHEDULE


def test_non_finney_network_retains_explicit_qualification_schedule() -> None:
    explicit = QualificationSchedule(
        configurations=(
            QualificationConfig(
                owner_hotkey=OWNER,
                minimum_conviction_alpha=Decimal("250"),
                effective_block=0,
            ),
        )
    )

    policy = resolve_qualification_policy(
        network="local",
        netuid=93,
        schedule_json=explicit.model_dump_json(),
        owner_hotkey=None,
        minimum_conviction_alpha="0",
        minimum_self_stake_alpha=None,
        effective_block=0,
    )

    assert policy == explicit


@pytest.mark.asyncio
async def test_qualifies_exact_threshold_without_float_rounding() -> None:
    result = await QualificationReader(FakeChain(OWNER, 250_500_000_000), config()).read(
        MINER, block=100
    )

    assert result.eligible is True
    assert result.reason == "eligible"
    assert result.qualified_via == "owner_lock"
    assert result.config_version == 1
    assert result.conviction_alpha == Decimal("250.5")
    assert result.self_stake_alpha == Decimal("0")
    assert result.required_self_stake_alpha is None


@pytest.mark.asyncio
async def test_self_stake_to_owned_miner_hotkey_is_an_alternative_path() -> None:
    policy = config().model_copy(update={"minimum_self_stake_alpha": Decimal("250.5")})

    result = await QualificationReader(
        FakeChain(MINER, 0, self_stake_rao=250_500_000_000), policy
    ).read(MINER, block=100)

    assert result.eligible is True
    assert result.reason == "eligible"
    assert result.qualified_via == "self_stake"
    assert result.self_stake_alpha == Decimal("250.5")
    assert result.required_self_stake_alpha == Decimal("250.5")


@pytest.mark.asyncio
async def test_third_party_stake_is_not_part_of_self_stake_input() -> None:
    policy = config().model_copy(update={"minimum_self_stake_alpha": Decimal("250.5")})

    result = await QualificationReader(FakeChain(MINER, 0), policy).read(MINER, block=100)

    assert result.eligible is False
    assert result.reason == "neither_qualification_path_met"
    assert result.qualified_via is None


@pytest.mark.asyncio
async def test_self_stake_only_policy_qualifies_without_an_owner_lock() -> None:
    policy = QualificationConfig(
        owner_hotkey=OWNER,
        minimum_conviction_alpha=Decimal("0"),
        minimum_self_stake_alpha=Decimal("250.5"),
        effective_block=90,
    )

    result = await QualificationReader(
        FakeChain(None, 0, self_stake_rao=250_500_000_000), policy
    ).read(MINER, block=100)

    assert result.eligible is True
    assert result.qualified_via == "self_stake"


@pytest.mark.asyncio
async def test_self_stake_only_policy_reports_below_minimum() -> None:
    policy = QualificationConfig(
        owner_hotkey=OWNER,
        minimum_conviction_alpha=Decimal("0"),
        minimum_self_stake_alpha=Decimal("250.5"),
        effective_block=90,
    )

    result = await QualificationReader(FakeChain(None, 0), policy).read(MINER, block=100)

    assert result.eligible is False
    assert result.reason == "self_stake_below_minimum"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chain", "reason"),
    [
        (FakeChain(OWNER, 250_499_999_999), "conviction_below_minimum"),
        (FakeChain(MINER, 999_000_000_000), "lock_targets_different_hotkey"),
        (FakeChain(None, 0, None), "hotkey_has_no_owner"),
    ],
)
async def test_rejects_each_failed_qualification_condition(chain: FakeChain, reason: str) -> None:
    result = await QualificationReader(chain, config()).read(MINER, block=100)

    assert result.eligible is False
    assert result.reason == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chain",
    [
        FakeChain(MINER, 0),
        FakeChain(None, 0, None),
    ],
)
async def test_zero_threshold_disables_lock_target_and_owner_checks(chain: FakeChain) -> None:
    disabled = QualificationConfig(
        owner_hotkey=OWNER,
        minimum_conviction_alpha=Decimal("0"),
        effective_block=0,
    )

    result = await QualificationReader(chain, disabled).read(MINER, block=100)

    assert result.eligible is True
    assert result.reason == "qualification_disabled"
    assert result.qualified_via is None


@pytest.mark.asyncio
async def test_historical_block_selects_immutable_threshold_version() -> None:
    schedule = QualificationSchedule(
        configurations=(
            QualificationConfig(
                version=1,
                owner_hotkey=OWNER,
                minimum_conviction_alpha=Decimal("300"),
                effective_block=0,
            ),
            QualificationConfig(
                version=2,
                owner_hotkey=OWNER,
                minimum_conviction_alpha=Decimal("200"),
                minimum_self_stake_alpha=Decimal("200"),
                effective_block=100,
            ),
        )
    )
    reader = QualificationReader(FakeChain(OWNER, 250_000_000_000), schedule)

    before_reduction = await reader.read(MINER, block=99)
    after_reduction = await reader.read(MINER, block=100)

    assert (before_reduction.config_version, before_reduction.eligible) == (1, False)
    assert (after_reduction.config_version, after_reduction.eligible) == (2, True)
    assert before_reduction.required_self_stake_alpha is None
    assert after_reduction.required_self_stake_alpha == Decimal("200")


def test_schedule_rejects_reordered_or_duplicate_history() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        QualificationSchedule(
            configurations=(
                QualificationConfig(
                    version=2,
                    owner_hotkey=OWNER,
                    minimum_conviction_alpha=Decimal("200"),
                    effective_block=100,
                ),
                QualificationConfig(
                    version=1,
                    owner_hotkey=OWNER,
                    minimum_conviction_alpha=Decimal("300"),
                    effective_block=0,
                ),
            )
        )
