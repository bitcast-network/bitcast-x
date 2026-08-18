"""Optional previews retain the outgoing validator's 20-minute cadence."""

import pytest

from bitcast_x.validator.cadence import PREVIEW_INTERVAL_SECONDS, PreviewCadence


def test_preview_cadence_defaults_to_twenty_minutes() -> None:
    cadence = PreviewCadence()

    assert PREVIEW_INTERVAL_SECONDS == 1_200
    assert cadence.due(100)
    assert not cadence.due(1_299)
    assert cadence.due(1_300)


def test_preview_cadence_reserves_slot_before_work_starts() -> None:
    cadence = PreviewCadence(interval_seconds=20)

    assert cadence.due(100)
    assert not cadence.due(100)
    assert not cadence.due(119)
    assert cadence.due(120)


def test_preview_cadence_rejects_non_positive_intervals() -> None:
    with pytest.raises(ValueError, match="preview interval must be positive"):
        PreviewCadence(interval_seconds=0)
