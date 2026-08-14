"""Economic activation cadence and fail-closed tests."""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from bitcast_x.config import Settings
from bitcast_x.errors import ChainOperationError, ProtocolError
from bitcast_x.qualification import QualificationConfig, QualificationSchedule
from bitcast_x.validator.service import (
    ensure_preclaim_economics_qualified,
    ensure_production_outputs_configured,
    submit_weights_if_due,
)

OWNER = "5FHneW46xGXgs5mUiveU4sbTyGBzmst2jfFvCw9zThqAXhGK"


class Chain:
    def __init__(self, last_update: int) -> None:
        self.last_update = last_update
        self.submissions: list[tuple[Any, dict[int, float], int]] = []

    async def last_weight_update(self, uid: int) -> int:
        assert uid == 7
        return self.last_update

    async def set_weights(
        self, wallet: Any, weights: dict[int, float], *, version_key: int
    ) -> None:
        self.submissions.append((wallet, weights, version_key))


def _wallet() -> Any:
    return SimpleNamespace(hotkey=SimpleNamespace(ss58_address="validator"))


def _graph(registered: bool = True) -> Any:
    return SimpleNamespace(
        by_hotkey=lambda hotkey: (
            SimpleNamespace(uid=7) if registered and hotkey == "validator" else None
        )
    )


def _qualification_schedule(*thresholds: tuple[int, str]) -> QualificationSchedule:
    return QualificationSchedule(
        configurations=tuple(
            QualificationConfig(
                version=index,
                owner_hotkey=OWNER,
                minimum_conviction_alpha=Decimal(threshold),
                effective_block=block,
            )
            for index, (block, threshold) in enumerate(thresholds, start=1)
        )
    )


def test_enabled_production_outputs_require_reconciliation_providers() -> None:
    with pytest.raises(
        ValueError,
        match="BITCAST_X_DESEARCH_API_KEY, BITCAST_X_CHUTES_API_KEY",
    ):
        ensure_production_outputs_configured(Settings(_env_file=None))


def test_disabled_outputs_allow_an_ingestion_only_diagnostic_run() -> None:
    settings = Settings(_env_file=None).model_copy(
        update={"enable_data_publish": False, "enable_weight_submission": False}
    )

    ensure_production_outputs_configured(settings)


def test_production_outputs_accept_complete_provider_configuration() -> None:
    settings = Settings(_env_file=None).model_copy(
        update={"desearch_api_key": "desearch", "chutes_api_key": "chutes"}
    )

    ensure_production_outputs_configured(settings)


@pytest.mark.parametrize(
    ("data_publish_enabled", "weight_submission_enabled"),
    [(True, False), (False, True), (True, True)],
)
def test_preclaim_economics_fail_closed_when_effective_threshold_is_zero(
    data_publish_enabled: bool,
    weight_submission_enabled: bool,
) -> None:
    with pytest.raises(ProtocolError, match="non-zero qualification threshold"):
        ensure_preclaim_economics_qualified(
            _qualification_schedule((0, "0")),
            block=100,
            preclaim_active=True,
            data_publish_enabled=data_publish_enabled,
            weight_submission_enabled=weight_submission_enabled,
        )


def test_zero_threshold_remains_available_for_shadow_and_legacy_only_cycles() -> None:
    schedule = _qualification_schedule((0, "0"))

    ensure_preclaim_economics_qualified(
        schedule,
        block=100,
        preclaim_active=True,
        data_publish_enabled=False,
        weight_submission_enabled=False,
    )
    ensure_preclaim_economics_qualified(
        schedule,
        block=100,
        preclaim_active=False,
        data_publish_enabled=True,
        weight_submission_enabled=True,
    )


def test_preclaim_guard_uses_threshold_version_effective_at_finalized_block() -> None:
    schedule = _qualification_schedule((0, "100"), (200, "0"))

    ensure_preclaim_economics_qualified(
        schedule,
        block=199,
        preclaim_active=True,
        data_publish_enabled=False,
        weight_submission_enabled=True,
    )
    with pytest.raises(ProtocolError, match="non-zero qualification threshold"):
        ensure_preclaim_economics_qualified(
            schedule,
            block=200,
            preclaim_active=True,
            data_publish_enabled=False,
            weight_submission_enabled=True,
        )


@pytest.mark.asyncio
async def test_submits_exact_vector_once_chain_cadence_is_due() -> None:
    chain = Chain(last_update=100)
    wallet = _wallet()
    weights = {0: 0.25, 7: 0.75}
    submitted = await submit_weights_if_due(  # type: ignore[arg-type]
        chain, wallet, _graph(), weights, block=201, epoch_blocks=100, version_key=3
    )
    assert submitted is True
    assert chain.submissions == [(wallet, weights, 3)]


@pytest.mark.asyncio
async def test_skips_until_chain_cadence_is_strictly_due() -> None:
    chain = Chain(last_update=100)
    submitted = await submit_weights_if_due(  # type: ignore[arg-type]
        chain, _wallet(), _graph(), {0: 1.0}, block=200, epoch_blocks=100, version_key=0
    )
    assert submitted is False
    assert chain.submissions == []


@pytest.mark.asyncio
async def test_enabled_submission_fails_closed_for_unregistered_validator() -> None:
    with pytest.raises(ChainOperationError, match="not registered"):
        await submit_weights_if_due(  # type: ignore[arg-type]
            Chain(0), _wallet(), _graph(False), {0: 1.0}, block=201, epoch_blocks=100, version_key=0
        )
