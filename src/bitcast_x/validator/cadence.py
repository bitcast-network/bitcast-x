"""Cadences for optional validator work outside final consensus scoring."""

from time import monotonic

PREVIEW_INTERVAL_SECONDS = 20.0 * 60.0


class PreviewCadence:
    """Limit mutable pre-close previews to the outgoing validator cadence.

    Final reconciliation and score freezing are deliberately not gated by this
    cadence. They remain driven by the campaign scoring-close block.
    """

    def __init__(self, interval_seconds: float = PREVIEW_INTERVAL_SECONDS) -> None:
        if interval_seconds <= 0:
            raise ValueError("preview interval must be positive")
        self.interval_seconds = interval_seconds
        self._last_preview: float | None = None

    def due(self, now: float | None = None) -> bool:
        """Reserve one preview slot, suppressing retry storms after failures."""

        current = monotonic() if now is None else now
        if self._last_preview is not None and current - self._last_preview < self.interval_seconds:
            return False
        self._last_preview = current
        return True
