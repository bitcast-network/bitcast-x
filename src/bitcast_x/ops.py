"""Liveness, readiness, and bounded-cardinality validator metrics."""

from dataclasses import dataclass
from time import monotonic

from fastapi import FastAPI, Response, status

from bitcast_x import __version__
from bitcast_x.release import source_revision


@dataclass(slots=True)
class RuntimeHealth:
    """Small in-memory operational state with no unbounded labels."""

    started_at: float
    ready: bool = False
    last_finalized_block: int | None = None
    successful_cycles: int = 0
    failed_cycles: int = 0

    @classmethod
    def create(cls) -> "RuntimeHealth":
        """Start a fresh process health record."""

        return cls(started_at=monotonic())

    def success(self, block: int) -> None:
        """Mark a complete validator cycle ready."""

        self.ready = True
        self.last_finalized_block = block
        self.successful_cycles += 1

    def failure(self) -> None:
        """Count a failed cycle without erasing the last good block."""

        self.failed_cycles += 1


def create_ops_app(health: RuntimeHealth) -> FastAPI:
    """Create an unauthenticated, non-sensitive operator endpoint."""

    app = FastAPI(title="Bitcast X operations", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def liveness() -> dict[str, object]:
        return {
            "status": "ok",
            "version": __version__,
            "source_revision": source_revision(),
            "uptime_seconds": round(monotonic() - health.started_at, 3),
        }

    @app.get("/ready")
    async def readiness(response: Response) -> dict[str, object]:
        if not health.ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if health.ready else "starting",
            "last_finalized_block": health.last_finalized_block,
        }

    @app.get("/metrics", response_class=Response)
    async def metrics() -> Response:
        lines = [
            "# TYPE bitcast_x_validator_ready gauge",
            f"bitcast_x_validator_ready {int(health.ready)}",
            "# TYPE bitcast_x_validator_cycles_total counter",
            f'bitcast_x_validator_cycles_total{{result="success"}} {health.successful_cycles}',
            f'bitcast_x_validator_cycles_total{{result="failure"}} {health.failed_cycles}',
            "# TYPE bitcast_x_validator_finalized_block gauge",
            f"bitcast_x_validator_finalized_block {health.last_finalized_block or 0}",
        ]
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    return app
