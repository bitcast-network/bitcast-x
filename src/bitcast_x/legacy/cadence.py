"""Self-contained scheduling policy for removable legacy compatibility work."""

from time import monotonic

FAST_TRACK_INTERVAL_SECONDS = 20.0
SCORING_INTERVAL_SECONDS = 20.0 * 60.0


class LegacyCadence:
    """Gate legacy intake and scoring without adding full-discovery refreshes."""

    def __init__(self) -> None:
        self._last_fast_track: float | None = None
        self._last_scoring: float | None = None

    def fast_track_due(self, now: float | None = None) -> bool:
        current = monotonic() if now is None else now
        if (
            self._last_fast_track is not None
            and current - self._last_fast_track < FAST_TRACK_INTERVAL_SECONDS
        ):
            return False
        self._last_fast_track = current
        return True

    def scoring_due(self, now: float | None = None) -> bool:
        current = monotonic() if now is None else now
        if (
            self._last_scoring is not None
            and current - self._last_scoring < SCORING_INTERVAL_SECONDS
        ):
            return False
        self._last_scoring = current
        return True
