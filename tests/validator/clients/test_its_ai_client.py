"""Tests for the it's-AI text detection client."""

import pytest
import unittest.mock as mock

from bitcast.validator.clients import its_ai_client
from bitcast.validator.clients.its_ai_client import (
    analyze_text,
    analyze_texts,
    ItsAiConfigError,
)


def _resp(status_code, json_body=None, text="", headers=None):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = json_body if json_body is not None else {}
    r.text = text
    r.headers = headers if headers is not None else {}
    return r


@pytest.fixture(autouse=True)
def _no_sleep():
    """Don't actually sleep during retry backoff in tests."""
    with mock.patch.object(its_ai_client.time, "sleep") as s:
        yield s


class TestAnalyzeText:
    def test_success_returns_score(self):
        session = mock.Mock()
        session.post.return_value = _resp(200, {"score": 0.42, "ai_percentage": 0.4})
        score = analyze_text("x" * 300, api_key="k", session=session)
        assert score == pytest.approx(0.42)
        # Auth header carries the key with the APIKey scheme.
        _, kwargs = session.post.call_args
        assert kwargs["headers"]["Authorization"] == "APIKey k"
        assert kwargs["json"] == {"text": "x" * 300}

    def test_validation_error_returns_none(self):
        # 404 low_words / non-english -> skip this sample (fail open).
        session = mock.Mock()
        session.post.return_value = _resp(404, {"status": "error"}, text="low_words")
        assert analyze_text("short", api_key="k", session=session) is None

    def test_rate_limit_returns_none(self):
        session = mock.Mock()
        session.post.return_value = _resp(429, text="rate limited")
        assert analyze_text("x" * 300, api_key="k", session=session) is None

    def test_auth_failure_raises(self):
        session = mock.Mock()
        session.post.return_value = _resp(401, text="bad key")
        with pytest.raises(ItsAiConfigError):
            analyze_text("x" * 300, api_key="k", session=session)

    def test_missing_key_raises(self):
        with mock.patch.object(its_ai_client, "ITS_AI_API_KEY", None):
            with pytest.raises(ItsAiConfigError):
                analyze_text("x" * 300, api_key=None)

    def test_network_error_returns_none(self):
        import requests
        session = mock.Mock()
        session.post.side_effect = requests.RequestException("boom")
        assert analyze_text("x" * 300, api_key="k", session=session) is None

    def test_null_score_returns_none(self):
        session = mock.Mock()
        session.post.return_value = _resp(200, {"score": None})
        assert analyze_text("x" * 300, api_key="k", session=session) is None


class TestAnalyzeTexts:
    def test_empty_input_short_circuits(self):
        session = mock.Mock()
        assert analyze_texts([], api_key="k", session=session) == []
        session.post.assert_not_called()

    def test_success_aligned_scores(self):
        session = mock.Mock()
        session.post.return_value = _resp(200, {"results": [{"score": 0.1}, {"score": 0.9}]})
        out = analyze_texts(["a" * 250, "b" * 250], api_key="k", session=session)
        assert out == [pytest.approx(0.1), pytest.approx(0.9)]
        _, kwargs = session.post.call_args
        assert kwargs["headers"]["Authorization"] == "APIKey k"
        assert kwargs["json"] == {"texts": ["a" * 250, "b" * 250]}

    def test_per_item_error_and_null_become_none(self):
        session = mock.Mock()
        session.post.return_value = _resp(200, {"results": [
            {"score": 0.5},
            {"error": {"code": "validation:low_words", "message": "too short"}},
            {"score": None},
        ]})
        out = analyze_texts(["a" * 250, "short", "c" * 250], api_key="k", session=session)
        assert out == [pytest.approx(0.5), None, None]

    def test_length_mismatch_fails_open(self):
        session = mock.Mock()
        session.post.return_value = _resp(200, {"results": [{"score": 0.5}]})
        out = analyze_texts(["a" * 250, "b" * 250], api_key="k", session=session)
        assert out == [None, None]

    def test_batch_limit_404_fails_open(self):
        session = mock.Mock()
        session.post.return_value = _resp(404, {"status": "error"}, text="validation:batch_limit")
        out = analyze_texts(["a" * 250, "b" * 250], api_key="k", session=session)
        assert out == [None, None]

    def test_rate_limit_fails_open(self):
        session = mock.Mock()
        session.post.return_value = _resp(429, text="rate limited")
        assert analyze_texts(["a" * 250], api_key="k", session=session) == [None]

    def test_auth_failure_raises(self):
        session = mock.Mock()
        session.post.return_value = _resp(401, text="bad key")
        with pytest.raises(ItsAiConfigError):
            analyze_texts(["a" * 250], api_key="k", session=session)

    def test_missing_key_raises(self):
        with mock.patch.object(its_ai_client, "ITS_AI_API_KEY", None):
            with pytest.raises(ItsAiConfigError):
                analyze_texts(["a" * 250], api_key=None)

    def test_network_error_fails_open(self):
        import requests
        session = mock.Mock()
        session.post.side_effect = requests.RequestException("boom")
        assert analyze_texts(["a" * 250, "b" * 250], api_key="k", session=session) == [None, None]


class TestRetries:
    def test_retries_on_429_then_succeeds(self):
        session = mock.Mock()
        session.post.side_effect = [
            _resp(429, text="slow down"),
            _resp(200, {"results": [{"score": 0.6}]}),
        ]
        out = analyze_texts(["a" * 250], api_key="k", session=session)
        assert out == [pytest.approx(0.6)]
        assert session.post.call_count == 2

    def test_retries_on_5xx_then_succeeds(self):
        session = mock.Mock()
        session.post.side_effect = [
            _resp(500, text="server:service_unavailable"),
            _resp(200, {"score": 0.3}),
        ]
        assert analyze_text("a" * 250, api_key="k", session=session) == pytest.approx(0.3)
        assert session.post.call_count == 2

    def test_retries_on_network_error_then_succeeds(self):
        import requests
        session = mock.Mock()
        session.post.side_effect = [
            requests.RequestException("boom"),
            _resp(200, {"results": [{"score": 0.9}]}),
        ]
        out = analyze_texts(["a" * 250], api_key="k", session=session)
        assert out == [pytest.approx(0.9)]
        assert session.post.call_count == 2

    def test_exhausts_retries_then_fails_open(self):
        session = mock.Mock()
        session.post.return_value = _resp(503, text="unavailable")
        with mock.patch.object(its_ai_client, "ITS_AI_MAX_RETRIES", 2):
            out = analyze_texts(["a" * 250], api_key="k", session=session)
        assert out == [None]
        assert session.post.call_count == 3  # 1 initial + 2 retries

    def test_does_not_retry_deterministic_4xx(self):
        session = mock.Mock()
        session.post.return_value = _resp(404, text="validation:batch_limit")
        out = analyze_texts(["a" * 250], api_key="k", session=session)
        assert out == [None]
        assert session.post.call_count == 1  # 404 is not retried

    def test_honors_retry_after_header(self):
        session = mock.Mock()
        session.post.side_effect = [
            _resp(429, text="slow down", headers={"Retry-After": "7"}),
            _resp(200, {"results": [{"score": 0.5}]}),
        ]
        with mock.patch.object(its_ai_client.time, "sleep") as sleep:
            out = analyze_texts(["a" * 250], api_key="k", session=session)
        assert out == [pytest.approx(0.5)]
        sleep.assert_called_once_with(7.0)
