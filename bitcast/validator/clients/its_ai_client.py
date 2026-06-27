"""
Client for the it's-AI text detection API (https://docs.its-ai.org/).

Single endpoint: POST https://api.its-ai.org/api/v2/text
  - Auth: ``Authorization: <api_key>`` header.
  - Body: {"text": "<>=200 chars, English>"}.
  - Synchronous: blocks until analysis completes (up to ~5 min).
  - Response 200: {"score": 0.0..1.0, ...} where score is 0.0 (human) .. 1.0 (AI).

We use ``score`` directly as the per-tweet AI score.

Failure policy is fail-open: validation errors (text too short / non-English),
rate limits, and server errors return ``None`` so the caller simply skips that
sample rather than dampening an account because of an API hiccup. Only genuine
misconfiguration (no API key) raises.
"""

from typing import Optional

import requests
import bittensor as bt

from bitcast.validator.utils.config import (
    ITS_AI_API_URL,
    ITS_AI_API_KEY,
    ITS_AI_TIMEOUT,
)


class ItsAiConfigError(Exception):
    """Raised when the client is misconfigured (e.g. missing API key)."""


def analyze_text(
    text: str,
    api_key: Optional[str] = None,
    url: Optional[str] = None,
    timeout: Optional[int] = None,
    session: Optional[requests.Session] = None,
) -> Optional[float]:
    """Return the AI score (0.0 human .. 1.0 AI) for ``text``, or None to skip.

    Returns None for any recoverable/skippable condition (text shorter than the
    API minimum, unsupported language, rate limit, server error, network error).
    Raises ItsAiConfigError only when no API key is available.
    """
    key = api_key or ITS_AI_API_KEY
    if not key:
        raise ItsAiConfigError("ITS_AI_API_KEY is not configured")

    endpoint = url or ITS_AI_API_URL
    http = session or requests

    try:
        resp = http.post(
            endpoint,
            json={"text": text},
            headers={
                "Authorization": key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout or ITS_AI_TIMEOUT,
        )
    except requests.RequestException as e:
        bt.logging.warning(f"its-ai request failed (network): {e}")
        return None

    if resp.status_code == 200:
        try:
            score = resp.json().get("score")
        except ValueError:
            bt.logging.warning("its-ai returned 200 with non-JSON body")
            return None
        if score is None:
            return None
        try:
            return float(score)
        except (TypeError, ValueError):
            bt.logging.warning(f"its-ai returned non-numeric score: {score!r}")
            return None

    if resp.status_code == 401:
        # Misconfigured key — this will affect every call, so surface loudly.
        raise ItsAiConfigError(f"its-ai authentication failed (401): {resp.text[:200]}")

    # 403 (quota), 404 (validation: too short / non-English), 429 (rate limit),
    # 500 (server) — all skippable. Fail open.
    bt.logging.debug(f"its-ai non-200 ({resp.status_code}), skipping sample: {resp.text[:200]}")
    return None
