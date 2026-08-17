"""V2-compatible optimistic LLM brief evaluation behind a durable cache seam."""

import asyncio
import hashlib
import logging
import re
from collections.abc import Mapping
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter

from bitcast_x.campaigns import CampaignRecord
from bitcast_x.errors import ReconciliationUnavailableError
from bitcast_x.prompts import generate_brief_evaluation_prompt
from bitcast_x.x_provider import Tweet

LOGGER = logging.getLogger(__name__)


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
    """Durable prompt-verdict cache owned by validator state."""

    def llm_evaluation(self, prompt_hash: str) -> BriefEvaluation | None: ...

    def persist_llm_evaluation(
        self, prompt_hash: str, result: BriefEvaluation
    ) -> BriefEvaluation: ...


class LlmBriefFilter:
    """Run v2's three optimistic checks using one configured chat-completion provider."""

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        model: str,
        cache: EvaluationCache,
        num_checks: int = 3,
        tweet_max_length: int = 10_000,
        max_response_bytes: int = 2_000_000,
        timeout: float = 60.0,
        attempts: int = 3,
        extra_headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("LLM API key cannot be empty")
        if num_checks <= 0 or tweet_max_length <= 0 or max_response_bytes <= 0 or attempts <= 0:
            raise ValueError("LLM evaluation limits must be positive")
        self._api_url = api_url
        self._model = model
        self._cache = cache
        self._num_checks = num_checks
        self._tweet_max_length = tweet_max_length
        self._max_response_bytes = max_response_bytes
        self._timeout = timeout
        self._attempts = attempts
        self._headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
            **dict(extra_headers or {}),
        }
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(trust_env=False)
        self._prompt_locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        """Close an internally owned HTTP pool."""

        if self._owns_client:
            await self._client.aclose()

    async def evaluate(self, campaign: CampaignRecord, tweet: Tweet) -> BriefEvaluation:
        """Pass if any available check passes; never turn total provider failure into rejection."""

        text = tweet.text[: self._tweet_max_length]
        last_result: BriefEvaluation | None = None
        unavailable = 0
        brief = {
            "id": campaign.access.campaign_id,
            "brief": campaign.brief,
            "prompt_version": campaign.prompt_version,
        }
        for check in range(1, self._num_checks + 1):
            prompt = generate_brief_evaluation_prompt(
                brief,
                f"{text} {check}",
                campaign.prompt_version,
            )
            prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
            lock = self._prompt_locks.setdefault(prompt_hash, asyncio.Lock())
            async with lock:
                result = self._cache.llm_evaluation(prompt_hash)
                if result is None:
                    try:
                        response_text = await self._request(prompt)
                    except httpx.HTTPError:
                        unavailable += 1
                        LOGGER.warning(
                            "brief evaluation unavailable campaign=%s tweet=%s check=%s",
                            campaign.access.campaign_id,
                            tweet.tweet_id,
                            check,
                        )
                        continue
                    proposed = parse_brief_evaluation(response_text, checks_used=check)
                    result = self._cache.persist_llm_evaluation(prompt_hash, proposed)
            last_result = result.model_copy(update={"checks_used": check})
            if result.meets_brief:
                return last_result
        if unavailable:
            raise ReconciliationUnavailableError(
                f"brief evaluation provider unavailable for tweet {tweet.tweet_id}"
            )
        return last_result or BriefEvaluation(
            meets_brief=False,
            reasoning="All checks failed",
            checks_used=self._num_checks,
        )

    async def _request(self, prompt: str) -> str:
        last_error: httpx.HTTPError | None = None
        for attempt in range(self._attempts):
            try:
                async with self._client.stream(
                    "POST",
                    self._api_url,
                    headers=self._headers,
                    json={
                        "model": self._model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0,
                        "max_tokens": 4096,
                    },
                    timeout=self._timeout,
                ) as response:
                    response.raise_for_status()
                    declared = int(response.headers.get("content-length", 0))
                    if declared > self._max_response_bytes:
                        raise httpx.ProtocolError("LLM response exceeds byte limit")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self._max_response_bytes:
                            raise httpx.ProtocolError("LLM response exceeds byte limit")
                        chunks.append(chunk)
                payload = TypeAdapter(dict[str, Any]).validate_json(b"".join(chunks))
                content = payload["choices"][0]["message"].get("content")
                if not isinstance(content, str) or not content:
                    raise httpx.ProtocolError("LLM response content is empty")
                return content
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                if isinstance(exc, httpx.HTTPError):
                    last_error = exc
                else:
                    last_error = httpx.ProtocolError("malformed LLM response")
                if attempt + 1 < self._attempts:
                    await asyncio.sleep(2 ** (attempt + 1))
        assert last_error is not None
        raise last_error


def parse_brief_evaluation(text: str, *, checks_used: int) -> BriefEvaluation:
    """Parse the versioned markdown verdict, breakdown, and summary fields."""

    verdict = re.search(r"## Verdict\s*\n\s*(YES|NO)", text, re.IGNORECASE)
    breakdown = re.search(
        r"## (?:Requirement-by-Requirement|Objective Requirements)[ \t]*\n"
        r"(.*?)(?:\n## Verdict|\n## |$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    summary = re.search(
        r"## Summary\s*\n\s*(.*?)(?:\n##|\n```|$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    return BriefEvaluation(
        meets_brief=bool(verdict and verdict.group(1).upper() == "YES"),
        reasoning=summary.group(1).strip() if summary else "Unable to parse response",
        detailed_breakdown=breakdown.group(1).strip() if breakdown else None,
        checks_used=checks_used,
    )
