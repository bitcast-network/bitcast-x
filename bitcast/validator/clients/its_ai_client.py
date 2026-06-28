"""
Client for the it's-AI text detection API (https://docs.its-ai.org/).

Two endpoints, both synchronous (block until analysis completes, up to ~5 min):

  POST https://api.its-ai.org/api/v2/text  (analyze_text)
    - Body: {"text": "<>=200 chars, English>"}.
    - Response 200: {"score": 0.0..1.0, ...}.

  POST https://api.its-ai.org/api/v2/batch (analyze_texts)
    - Body: {"texts": [...]} (<= plan batch limit per request).
    - Response 200: {"results": [{"score": 0.0..1.0, ...} | {"error": {...}}, ...]}
      one per submitted text, in the same order. Invalid items carry a per-item
      ``error`` instead of failing the whole request.

Auth for both: ``Authorization: <api_key>`` header. We use ``score`` directly as
the per-tweet AI score (0.0 human .. 1.0 AI).

Transient failures (network errors, 429, 5xx) are retried with exponential
backoff (ITS_AI_MAX_RETRIES / ITS_AI_RETRY_BACKOFF), honoring Retry-After on 429.

Failure policy is fail-open: validation errors (text too short / non-English),
and transient failures that survive all retries, map to ``None`` so the caller
simply skips that sample rather than dampening an account because of an API
hiccup. Only genuine misconfiguration (no API key, or a 401) raises.
"""

import time
from typing import List, Optional

import requests
import bittensor as bt

from bitcast.validator.utils.config import (
    ITS_AI_API_URL,
    ITS_AI_BATCH_API_URL,
    ITS_AI_API_KEY,
    ITS_AI_TIMEOUT,
    ITS_AI_MAX_RETRIES,
    ITS_AI_RETRY_BACKOFF,
)

# Statuses worth retrying: rate limit and transient server/gateway errors
# (its-ai's server:server / server:service_unavailable surface as 500). 401/403/404
# are deterministic — retrying them just wastes time, so they fall through.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ItsAiConfigError(Exception):
    """Raised when the client is misconfigured (e.g. missing API key)."""


def _retry_after_seconds(resp, default: float) -> float:
    """Honor a numeric Retry-After header (seconds); fall back to ``default``."""
    raw = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
    if raw:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    return default


def _post_with_retries(http, endpoint, payload, key, timeout, label):
    """POST with bounded retries on network errors, 429 and 5xx.

    Returns the final ``requests.Response`` — which may still carry a retryable
    error status if every attempt was exhausted, leaving the caller to fail open.
    Re-raises ``requests.RequestException`` only after the last attempt fails at
    the network level. Backoff is exponential (ITS_AI_RETRY_BACKOFF * 2**attempt),
    overridden by Retry-After on 429.
    """
    headers = {
        "Authorization": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    attempts = ITS_AI_MAX_RETRIES + 1
    for attempt in range(attempts):
        last = attempt + 1 >= attempts
        try:
            resp = http.post(endpoint, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            if last:
                raise
            delay = ITS_AI_RETRY_BACKOFF * (2 ** attempt)
            bt.logging.warning(
                f"its-ai {label} network error (attempt {attempt + 1}/{attempts}), "
                f"retrying in {delay:.1f}s: {e}"
            )
            time.sleep(delay)
            continue

        if resp.status_code in _RETRYABLE_STATUS and not last:
            backoff = ITS_AI_RETRY_BACKOFF * (2 ** attempt)
            delay = _retry_after_seconds(resp, backoff) if resp.status_code == 429 else backoff
            bt.logging.warning(
                f"its-ai {label} {resp.status_code} (attempt {attempt + 1}/{attempts}), "
                f"retrying in {delay:.1f}s"
            )
            time.sleep(delay)
            continue

        return resp


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
        resp = _post_with_retries(
            http, endpoint, {"text": text}, key, timeout or ITS_AI_TIMEOUT, "request"
        )
    except requests.RequestException as e:
        bt.logging.warning(f"its-ai request failed (network, retries exhausted): {e}")
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


def _parse_item_score(item) -> Optional[float]:
    """Extract a per-item score from a /v2/batch result entry, or None to skip."""
    if not isinstance(item, dict):
        return None
    if item.get("error"):  # per-item validation/processing failure -> skip
        return None
    score = item.get("score")
    if score is None:
        return None
    try:
        return float(score)
    except (TypeError, ValueError):
        bt.logging.warning(f"its-ai returned non-numeric batch score: {score!r}")
        return None


def analyze_texts(
    texts: List[str],
    api_key: Optional[str] = None,
    url: Optional[str] = None,
    timeout: Optional[int] = None,
    session: Optional[requests.Session] = None,
) -> List[Optional[float]]:
    """Batch-analyze ``texts`` via /v2/batch; return scores aligned to the input.

    The returned list always has the same length and order as ``texts``. Each
    element is the AI score (0.0 human .. 1.0 AI) or None for any
    skippable/recoverable condition: per-item validation error, or a whole-batch
    failure (quota, batch-limit, rate limit, server, network, malformed body) in
    which case every element of the batch is None. Raises ItsAiConfigError only
    on missing key or 401, since that affects every request.
    """
    if not texts:
        return []

    key = api_key or ITS_AI_API_KEY
    if not key:
        raise ItsAiConfigError("ITS_AI_API_KEY is not configured")

    endpoint = url or ITS_AI_BATCH_API_URL
    http = session or requests
    fail_open = [None] * len(texts)

    try:
        resp = _post_with_retries(
            http, endpoint, {"texts": texts}, key, timeout or ITS_AI_TIMEOUT, "batch"
        )
    except requests.RequestException as e:
        bt.logging.warning(f"its-ai batch request failed (network, retries exhausted): {e}")
        return fail_open

    if resp.status_code == 200:
        try:
            results = resp.json().get("results")
        except ValueError:
            bt.logging.warning("its-ai batch returned 200 with non-JSON body")
            return fail_open
        if not isinstance(results, list):
            bt.logging.warning("its-ai batch 200 body missing 'results' list")
            return fail_open
        scores = [_parse_item_score(item) for item in results]
        if len(scores) != len(texts):
            # Spec guarantees one result per input in order; if that's violated we
            # can't trust the alignment, so skip the whole batch rather than
            # misattribute scores to the wrong tweets.
            bt.logging.warning(
                f"its-ai batch returned {len(scores)} results for {len(texts)} texts; skipping"
            )
            return fail_open
        return scores

    if resp.status_code == 401:
        raise ItsAiConfigError(f"its-ai authentication failed (401): {resp.text[:200]}")

    # 403 (quota), 404 (batch limit), 429 (rate limit), 500 (server) — skippable.
    # 404 batch_limit means ITS_AI_BATCH_SIZE exceeds the plan limit: surface it
    # clearly (still fail open) so the misconfig is visible in logs.
    if resp.status_code == 404:
        bt.logging.warning(
            f"its-ai batch 404 ({resp.text[:200]}); is ITS_AI_BATCH_SIZE within the plan limit?"
        )
    else:
        bt.logging.debug(f"its-ai batch non-200 ({resp.status_code}), skipping: {resp.text[:200]}")
    return fail_open
