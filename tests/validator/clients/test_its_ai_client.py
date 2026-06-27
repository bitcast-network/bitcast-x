"""Tests for the it's-AI text detection client."""

import pytest
import unittest.mock as mock

from bitcast.validator.clients import its_ai_client
from bitcast.validator.clients.its_ai_client import analyze_text, ItsAiConfigError


def _resp(status_code, json_body=None, text=""):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = json_body if json_body is not None else {}
    r.text = text
    return r


class TestAnalyzeText:
    def test_success_returns_score(self):
        session = mock.Mock()
        session.post.return_value = _resp(200, {"score": 0.42, "ai_percentage": 0.4})
        score = analyze_text("x" * 300, api_key="k", session=session)
        assert score == pytest.approx(0.42)
        # Auth header carries the key directly.
        _, kwargs = session.post.call_args
        assert kwargs["headers"]["Authorization"] == "k"
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
