"""Legacy compatibility work retains v1-like independent cadences."""

from bitcast_x.legacy.cadence import LegacyCadence


def test_fast_track_and_scoring_have_separate_cadences() -> None:
    cadence = LegacyCadence()

    assert cadence.fast_track_due(100)
    assert cadence.scoring_due(100)
    assert not cadence.fast_track_due(119)
    assert cadence.fast_track_due(120)
    assert not cadence.scoring_due(1_299)
    assert cadence.scoring_due(1_300)
