"""Bounded structured application logging without protocol payload leakage."""

import asyncio
import json
import logging
import re
import threading
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, datetime

import httpx

from bitcast_x.config import Settings

_FLUSH_INTERVAL_SECONDS = 5.0
_REQUEST_TIMEOUT_SECONDS = 10.0
_LOKI_LABEL = re.compile(r"[^a-zA-Z0-9_:.-]")
_loki_handler: "LokiHandler | None" = None
_loki_flush_task: asyncio.Task[None] | None = None


class JsonFormatter(logging.Formatter):
    """Render one stable JSON object from standard safe log record fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure_logging(*, level: str, json_output: bool) -> None:
    """Configure the process root logger once from typed settings."""

    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter() if json_output else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


class LokiHandler(logging.Handler):
    """Buffer and asynchronously push log records without blocking the validator."""

    def __init__(
        self,
        *,
        url: str,
        username: str,
        token: str,
        labels: dict[str, str],
    ) -> None:
        super().__init__()
        self._push_url = f"{url.rstrip('/')}/loki/api/v1/push"
        self._auth = httpx.BasicAuth(username, token)
        self._labels = {
            str(key): _LOKI_LABEL.sub("_", str(value))[:1_024] for key, value in labels.items()
        }
        self._buffer: dict[tuple[tuple[str, str], ...], list[tuple[str, str]]] = defaultdict(list)
        self._lock = threading.Lock()
        self._client: httpx.AsyncClient | None = None

    def emit(self, record: logging.LogRecord) -> None:
        """Add one record to the in-memory batch; logging must never raise."""

        try:
            if record.name == "httpx" and self._push_url in record.getMessage():
                return
            labels = {**self._labels, "level": record.levelname.lower()}
            key = tuple(sorted(labels.items()))
            timestamp = str(int(record.created * 1_000_000_000))
            with self._lock:
                self._buffer[key].append((timestamp, self.format(record)))
        except Exception:
            return

    async def push(self) -> None:
        """Push and discard the current batch on a best-effort basis."""

        with self._lock:
            if not self._buffer:
                return
            batch = dict(self._buffer)
            self._buffer.clear()
        payload = {
            "streams": [
                {
                    "stream": dict(labels),
                    "values": sorted(values, key=lambda item: item[0]),
                }
                for labels, values in batch.items()
            ]
        }
        try:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient()
            await self._client.post(
                self._push_url,
                auth=self._auth,
                json=payload,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception:
            return

    async def close_client(self) -> None:
        """Close the reusable HTTP client."""

        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


async def _push_loki_forever(handler: LokiHandler) -> None:
    while True:
        await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
        await handler.push()


def configure_loki_logging(settings: Settings, *, labels: dict[str, str]) -> bool:
    """Attach Loki when all settings are present; otherwise remain a safe no-op."""

    global _loki_flush_task, _loki_handler
    if _loki_handler is not None:
        return True
    if not settings.loki_url or not settings.loki_username or settings.loki_token is None:
        return False
    handler = LokiHandler(
        url=settings.loki_url,
        username=settings.loki_username,
        token=settings.loki_token.get_secret_value(),
        labels=labels,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
    _loki_handler = handler
    _loki_flush_task = asyncio.create_task(_push_loki_forever(handler), name="loki-push-loop")
    logging.getLogger(__name__).info("Loki logging enabled")
    return True


async def shutdown_loki_logging() -> None:
    """Flush buffered records and detach Loki during graceful shutdown."""

    global _loki_flush_task, _loki_handler
    if _loki_flush_task is not None:
        _loki_flush_task.cancel()
        with suppress(asyncio.CancelledError):
            await _loki_flush_task
        _loki_flush_task = None
    if _loki_handler is not None:
        logging.getLogger().removeHandler(_loki_handler)
        await _loki_handler.push()
        await _loki_handler.close_client()
        _loki_handler = None
