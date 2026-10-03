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


@pytest.mark.parametrize(
    ("updates", "missing"),
    [
        pytest.param(
            {},
            "BITCAST_X_DESEARCH_API_KEY, BITCAST_X_CHUTES_API_KEY",
            id="enabled_outputs_require_reconciliation_providers",
        ),
        pytest.param(
            {"enable_data_publish": False, "enable_weight_submission": False},
            None,
            id="disabled_outputs_allow_ingestion_only_diagnostic_run",
        ),
        pytest.param(
            {"desearch_api_key": "desearch", "chutes_api_key": "chutes"},
            None,
            id="complete_provider_configuration",
        ),
    ],
)
def test_production_outputs_require_reconciliation_providers(
    updates: dict[str, object],
    missing: str | None,
) -> None:
    settings = Settings(_env_file=None).model_copy(update=updates)

    if missing is None:
        ensure_production_outputs_configured(settings)
    else:
        with pytest.raises(ValueError, match=missing):
            ensure_production_outputs_configured(settings)


ZERO = _qualification_schedule((0, "0"))
ZERO_FROM_200 = _qualification_schedule((0, "100"), (200, "0"))
SELF_STAKE_ONLY = QualificationSchedule(
    configurations=(
        QualificationConfig(
            owner_hotkey=OWNER,
            minimum_conviction_alpha=Decimal("0"),
            minimum_self_stake_alpha=Decimal("15000"),
            effective_block=0,
        ),
    )
)


@pytest.mark.parametrize(
    ("schedule", "block", "preclaim", "publish", "weights", "fails_closed"),
    [
        pytest.param(ZERO, 100, True, True, False, True, id="zero_with_publication"),
        pytest.param(ZERO, 100, True, False, True, True, id="zero_with_weights"),
        pytest.param(ZERO, 100, True, True, True, True, id="zero_with_both_outputs"),
        pytest.param(ZERO, 100, True, False, False, False, id="zero_for_shadow_cycle"),
        pytest.param(ZERO, 100, False, True, True, False, id="zero_without_preclaim"),
        pytest.param(SELF_STAKE_ONLY, 100, True, True, True, False, id="self_stake_threshold"),
        pytest.param(ZERO_FROM_200, 199, True, False, True, False, id="nonzero_before_change"),
        # The threshold version effective at the finalized block decides.
        pytest.param(ZERO_FROM_200, 200, True, False, True, True, id="zero_effective_at_block"),
    ],
)
def test_preclaim_economics_fail_closed_without_an_effective_threshold(
    schedule: QualificationSchedule,
    block: int,
    preclaim: bool,
    publish: bool,
    weights: bool,
    fails_closed: bool,
) -> None:
    def check() -> None:
        ensure_preclaim_economics_qualified(
            schedule,
            block=block,
            preclaim_active=preclaim,
            data_publish_enabled=publish,
            weight_submission_enabled=weights,
        )

    if fails_closed:
        with pytest.raises(ProtocolError, match="non-zero qualification threshold"):
            check()
    else:
        check()


async def test_submits_exact_vector_once_chain_cadence_is_due() -> None:
    chain = Chain(last_update=100)
    wallet = _wallet()
    weights = {0: 0.25, 7: 0.75}
    submitted = await submit_weights_if_due(  # type: ignore[arg-type]
        chain, wallet, _graph(), weights, block=201, epoch_blocks=100, version_key=3
    )
    assert submitted is True
    assert chain.submissions == [(wallet, weights, 3)]


async def test_skips_until_chain_cadence_is_strictly_due() -> None:
    chain = Chain(last_update=100)
    submitted = await submit_weights_if_due(  # type: ignore[arg-type]
        chain, _wallet(), _graph(), {0: 1.0}, block=200, epoch_blocks=100, version_key=0
    )
    assert submitted is False
    assert chain.submissions == []


async def test_enabled_submission_fails_closed_for_unregistered_validator() -> None:
    with pytest.raises(ChainOperationError, match="not registered"):
        await submit_weights_if_due(  # type: ignore[arg-type]
            Chain(0), _wallet(), _graph(False), {0: 1.0}, block=201, epoch_blocks=100, version_key=0
        )
