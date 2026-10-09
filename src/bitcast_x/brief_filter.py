"""Version-aware JEV brief evaluation behind a durable verdict cache.

The JEV answers are combined with a code check for required tags and links. Thresholds are the
configuration validated offline with the request text in `bitcast_x.prompts`; change them only
with a fresh evaluation. Provider failure is never a content rejection.
"""

import asyncio
import hashlib
import json
import logging
import math
import re
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter

from bitcast_x.campaigns import CampaignRecord
from bitcast_x.errors import ReconciliationUnavailableError, ResponseTooLargeError
from bitcast_x.http import read_bounded
from bitcast_x.prompts import build_request
from bitcast_x.x_provider import Tweet

LOGGER = logging.getLogger(__name__)

JEV_API_URL = "https://openrouter.ai/api/v1/systemone"
# OpenRouter can return the canonical slug for the pinned Jev 1.13 model.
# https://openrouter.ai/docs/guides/community/typesafe-sdk
JEV_CANONICAL_MODEL = "typesafe/jev-1.13-20260917"
VERDICT_ACCEPT_MIN = 0.02
GATE_MIN = 0.6
IDENTITY_MISMATCH_MAX = 0.5


class BriefEvaluation(BaseModel):
    """Frozen outcome of one campaign/tweet content evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    meets_brief: bool
    reasoning: str
    detailed_breakdown: str | None = None
    checks_used: int


class BriefFilter(Protocol):
    """Campaign-specific semantic content evaluator."""

    async def evaluate(self, campaign: CampaignRecord, tweet: Tweet) -> BriefEvaluation: ...


class EvaluationCache(Protocol):
    """Durable request-verdict cache owned by validator state."""

    def llm_evaluation(self, prompt_hash: str) -> BriefEvaluation | None: ...

    def persist_llm_evaluation(
        self, prompt_hash: str, result: BriefEvaluation
    ) -> BriefEvaluation: ...


_HANDLE_INSTRUCTION = re.compile(
    r"\b(?:also\s+)?(?:tag|mention|tagging|mentioning)\s*:?\s*"
    r"((?:@\w+(?:\s*(?:,|/|&|\band\b)\s*)?)+)",
    re.IGNORECASE,
)
_LINK_INSTRUCTION = re.compile(r"\binclude\b[^.\n]*?\b(?:ref(?:erral)?\s+)?link\b", re.IGNORECASE)
_URL = re.compile(
    r"https?://\S+|\b[\w-]+\.(?:io|com|xyz|ai|so|fun|app|network|org|net|dev)(?:/\S*)?",
    re.IGNORECASE,
)


def missing_required_items(brief: str, post: str) -> list[str]:
    """Return handles or links the brief explicitly requires that the post lacks.

    Brand handles in a header are not requirements, and quote-post instructions are ignored
    because quote metadata is not captured. Any URL satisfies a required link because X
    shortens links and the destination is not captured.
    """

    text = post.lower()
    handles: list[str] = []
    for match in _HANDLE_INSTRUCTION.finditer(brief):
        for handle in re.findall(r"@\w+", match.group(1)):
            if handle.lower() not in handles:
                handles.append(handle.lower())
    missing = [handle for handle in handles if handle not in text]
    if _LINK_INSTRUCTION.search(brief) and not _URL.search(post):
        missing.append("link")
    return missing


def decide(answers: dict[str, Any], brief: str, post: str) -> BriefEvaluation:
    """Apply the validated thresholds to a JEV response and the code checks."""

    accept = float(answers["verdict"]["probabilities"]["ACCEPT"])
    mismatch = float(answers["identity"]["probabilities"]["MISMATCH"])
    gates = {
        name.removeprefix("gate_"): float(answer["noul"])
        for name, answer in answers.items()
        if name.startswith("gate_")
    }
    missing = missing_required_items(brief, post)
    failures: list[str] = []
    if mismatch >= IDENTITY_MISMATCH_MAX:
        failures.append(f"describes a different product (mismatch {mismatch:.2f})")
    if accept < VERDICT_ACCEPT_MIN:
        failures.append(f"verdict rejects (accept {accept:.2f})")
    failures.extend(
        f"{name} not met ({value:.2f})" for name, value in gates.items() if value < GATE_MIN
    )
    if missing:
        failures.append("missing required " + ", ".join(missing))
    breakdown = json.dumps(
        {
            "verdict_accept": accept,
            "identity_mismatch": mismatch,
            "gates": gates,
            "missing_required": missing,
        },
        sort_keys=True,
    )
    return BriefEvaluation(
        meets_brief=not failures,
        reasoning="Meets the brief" if not failures else "; ".join(failures),
        detailed_breakdown=breakdown,
        checks_used=1,
    )


def _validated_answers(payload: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    if payload.get("model") not in (request["model"], JEV_CANONICAL_MODEL):
        raise ValueError("unexpected JEV model")
    answers = payload["answers"]
    if not isinstance(answers, dict) or set(answers) != set(request["questions"]):
        raise ValueError("JEV answers do not match the request")
    values = [
        answers["verdict"]["probabilities"]["ACCEPT"],
        answers["identity"]["probabilities"]["MISMATCH"],
    ]
    values += [answers[name]["noul"] for name in answers if name.startswith("gate_")]
    if any(
        isinstance(v, bool)
        or not isinstance(v, int | float)
        or not math.isfinite(v)
        or not 0 <= v <= 1
        for v in values
    ):
        raise ValueError("invalid JEV probability")
    return answers


class JevBriefFilter:
    """Evaluate each tweet with one version-aware JEV request through OpenRouter."""

    def __init__(
        self,
        *,
        api_key: str,
        cache: EvaluationCache,
        api_url: str = JEV_API_URL,
        tweet_max_length: int = 10_000,
        max_response_bytes: int = 2_000_000,
        timeout: float = 30.0,
        attempts: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OpenRouter API key cannot be empty")
        if tweet_max_length <= 0 or max_response_bytes <= 0 or attempts <= 0:
            raise ValueError("JEV evaluation limits must be positive")
        self._api_url = api_url
        self._cache = cache
        self._tweet_max_length = tweet_max_length
        self._max_response_bytes = max_response_bytes
        self._timeout = timeout
        self._attempts = attempts
        self._headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
        }
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(trust_env=False)
        self._request_locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        """Close an internally owned HTTP pool."""

        if self._owns_client:
            await self._client.aclose()

    async def evaluate(self, campaign: CampaignRecord, tweet: Tweet) -> BriefEvaluation:
        """Return a cached or fresh verdict; provider failure is never a rejection."""

        text = tweet.text[: self._tweet_max_length]
        request = build_request(campaign.brief, campaign.prompt_version, text)
        request_hash = hashlib.sha256(
            ("jev:" + json.dumps(request, sort_keys=True)).encode()
        ).hexdigest()
        lock = self._request_locks.setdefault(request_hash, asyncio.Lock())
        async with lock:
            cached = self._cache.llm_evaluation(request_hash)
            if cached is not None:
                return cached
            try:
                answers = await self._request(request)
            except httpx.HTTPError as exc:
                LOGGER.warning(
                    "brief evaluation unavailable campaign=%s tweet=%s",
                    campaign.access.campaign_id,
                    tweet.tweet_id,
                )
                raise ReconciliationUnavailableError(
                    f"brief evaluation provider unavailable for tweet {tweet.tweet_id}"
                ) from exc
            result = decide(answers, campaign.brief, text)
            return self._cache.persist_llm_evaluation(request_hash, result)

    async def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        last_error: httpx.HTTPError | None = None
        for attempt in range(self._attempts):
            try:
                async with self._client.stream(
                    "POST",
                    self._api_url,
                    headers=self._headers,
                    json=request,
                    timeout=self._timeout,
                ) as response:
                    response.raise_for_status()
                    body = await read_bounded(response, self._max_response_bytes, source="JEV")
                payload = TypeAdapter(dict[str, Any]).validate_json(body)
                return _validated_answers(payload, request)
            except (
                httpx.HTTPError,
                ResponseTooLargeError,
                KeyError,
                IndexError,
                TypeError,
                ValueError,
            ) as exc:
                if isinstance(exc, httpx.HTTPError):
                    last_error = exc
                elif isinstance(exc, ResponseTooLargeError):
                    last_error = httpx.ProtocolError("JEV response exceeds byte limit")
                else:
                    last_error = httpx.ProtocolError("malformed JEV response")
                if attempt + 1 < self._attempts:
                    await asyncio.sleep(2 ** (attempt + 1))
        assert last_error is not None
        raise last_error
