"""Tests for best-effort Grafana Loki forwarding."""

import logging
from argparse import Namespace
from unittest.mock import AsyncMock, patch

import pytest

import bitcast_x.logging as logging_module
from bitcast_x.config import Settings
from bitcast_x.logging import LokiHandler, configure_loki_logging, shutdown_loki_logging
from bitcast_x.main import run_command


@pytest.fixture(autouse=True)
async def reset_loki() -> None:
    """Keep module-level logging state isolated between tests."""

    yield
    await shutdown_loki_logging()


def test_loki_is_disabled_without_complete_configuration() -> None:
    settings = Settings(
        loki_url="https://example.test",
        loki_username="tenant",
        loki_token=None,
    )

    assert configure_loki_logging(settings, labels={"neuron": "validator"}) is False
    assert logging_module._loki_handler is None


def test_loki_has_zero_configuration_write_defaults() -> None:
    settings = Settings()

    assert settings.loki_url == "https://logs-prod-042.grafana.net"
    assert settings.loki_username == "1693344"
    assert settings.loki_token is not None
    assert settings.loki_token.get_secret_value()


@pytest.mark.asyncio
async def test_loki_batches_labels_and_pushes_records() -> None:
    handler = LokiHandler(
        url="https://example.test/",
        username="tenant",
        token="write-token",  # noqa: S106 - inert test credential
        labels={"service": "bitcast x", "hotkey": "5abc"},
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="miner unavailable",
        args=(),
        exc_info=None,
    )
    response = AsyncMock()
    client = AsyncMock()
    client.is_closed = False
    client.post.return_value = response
    handler._client = client

    handler.emit(record)
    await handler.push()

    request = client.post.await_args
    assert request.args[0] == "https://example.test/loki/api/v1/push"
    stream = request.kwargs["json"]["streams"][0]
    assert stream["stream"] == {
        "hotkey": "5abc",
        "level": "warning",
        "service": "bitcast_x",
    }
    assert stream["values"][0][1] == "miner unavailable"


@pytest.mark.asyncio
async def test_loki_network_failure_never_escapes() -> None:
    handler = LokiHandler(
        url="https://example.test",
        username="tenant",
        token="write-token",  # noqa: S106 - inert test credential
        labels={},
    )
    handler._client = AsyncMock()
    handler._client.is_closed = False
    handler._client.post.side_effect = RuntimeError("offline")
    handler.emit(logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None))

    await handler.push()


def test_loki_does_not_buffer_its_own_http_push_log() -> None:
    handler = LokiHandler(
        url="https://example.test",
        username="tenant",
        token="write-token",  # noqa: S106 - inert test credential
        labels={},
    )
    handler.emit(
        logging.LogRecord(
            "httpx",
            logging.INFO,
            __file__,
            1,
            'HTTP Request: POST https://example.test/loki/api/v1/push "HTTP/1.1 204"',
            (),
            None,
        )
    )

    assert not handler._buffer


@pytest.mark.asyncio
async def test_configure_and_shutdown_attach_to_root_logger() -> None:
    settings = Settings(
        loki_url="https://example.test",
        loki_username="tenant",
        loki_token="write-token",  # noqa: S106 - inert test credential
    )
    with patch.object(LokiHandler, "push", new=AsyncMock()):
        assert configure_loki_logging(settings, labels={"neuron": "validator"}) is True
        handler = logging_module._loki_handler
        assert handler is not None
        assert handler in logging.getLogger().handlers

        await shutdown_loki_logging()

        assert handler not in logging.getLogger().handlers
        assert logging_module._loki_handler is None


@pytest.mark.asyncio
async def test_run_miner_does_not_enable_loki() -> None:
    miner = AsyncMock()
    with (
        patch("bitcast_x.main.build_sdk", new=AsyncMock(return_value=(object(), object()))),
        patch("bitcast_x.main.ReferenceMiner", return_value=miner),
        patch("bitcast_x.validator.service.configure_loki_logging") as configure_loki,
    ):
        await run_command(Namespace(command="run-miner"), Settings())

    miner.run.assert_awaited_once()
    configure_loki.assert_not_called()
