"""Golden and availability tests for v2-compatible brief evaluation."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from bitcast_x.brief_filter import BriefEvaluation, LlmBriefFilter, parse_brief_evaluation
from bitcast_x.campaigns import CampaignRecord
from bitcast_x.errors import ReconciliationUnavailableError
from bitcast_x.prompts import generate_brief_evaluation_prompt
from bitcast_x.protocol import CampaignAccess, MiningProtocol
from bitcast_x.validator.store import ValidatorStore
from bitcast_x.x_provider import Tweet

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, BriefEvaluation] = {}

    def llm_evaluation(self, prompt_hash: str) -> BriefEvaluation | None:
        return self.values.get(prompt_hash)

    def persist_llm_evaluation(self, prompt_hash: str, result: BriefEvaluation) -> BriefEvaluation:
        existing = self.values.get(prompt_hash)
        if existing is not None:
            return existing
        self.values[prompt_hash] = result
        return result


def campaign(*, prompt_version: int = 1) -> CampaignRecord:
    return CampaignRecord(
        access=CampaignAccess(
            campaign_id="campaign",
            mechanism_id=1,
            mining_protocol=MiningProtocol.PRECLAIM_V2,
            scoring_close_block=20,
        ),
        title="Campaign",
        brief="Talk about Bitcast and tag @bitcast_network",
        ecosystem_id="ecosystem",
        opens_at=NOW,
        closes_at=NOW + timedelta(days=1),
        reward_pool_usd="1000",
        prompt_version=prompt_version,
    )


def tweet() -> Tweet:
    return Tweet(
        tweet_id="123",
        author_x_id="456",
        created_at=NOW,
        text="A thoughtful Bitcast post",
        author="creator",
    )


def completion(verdict: str, summary: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": (
                        "## Requirement-by-Requirement\n- Req 1: Met\n"
                        f"## Verdict\n{verdict}\n## Summary\n{summary}"
                    )
                }
            }
        ]
    }


def test_prompt_versions_are_byte_identical_to_v2_oracle() -> None:
    expected = {
        1: "193ca82cc622774a2cb142bb724378b33fbdbf8ec113cc16778a1153297849a0",
        2: "f2d2d4c2cf16821be3decbf5ae2478ec5ff821abfb7cc289b96e106066efbcaf",
        3: "2cd4cd1a4009c26a7c89900dfaaddee845c56206b6880bbd71fe5ae727c10f5a",
        4: "78e7381236ca3c3e815105a721360f1cb76d9275518b33e53cd54b7d9ae8343b",
    }
    brief = {"brief": "Talk about Bitcast and tag @bitcast_network"}

    actual = {
        version: hashlib.sha256(
            generate_brief_evaluation_prompt(
                brief,
                "A thoughtful Bitcast post",
                version,
            ).encode()
        ).hexdigest()
        for version in expected
    }

    assert actual == expected


@pytest.mark.asyncio
async def test_optimistic_checks_short_circuit_and_replay_from_cache() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        payload = completion("NO", "first failed") if requests == 1 else completion("YES", "pass")
        return httpx.Response(200, json=payload)

    cache = MemoryCache()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    evaluator = LlmBriefFilter(
        api_url="https://llm.test/chat",
        api_key="secret",
        model="model",
        cache=cache,
        attempts=1,
        client=client,
    )

    first = await evaluator.evaluate(campaign(), tweet())
    replay = await evaluator.evaluate(campaign(), tweet())

    assert first == replay
    assert first.meets_brief is True
    assert first.checks_used == 2
    assert requests == 2
    assert len(cache.values) == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_identical_prompts_make_one_provider_request() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=completion("YES", "pass"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    evaluator = LlmBriefFilter(
        api_url="https://llm.test/chat",
        api_key="secret",
        model="model",
        cache=MemoryCache(),
        attempts=1,
        client=client,
    )

    first, second = await asyncio.gather(
        evaluator.evaluate(campaign(), tweet()),
        evaluator.evaluate(campaign(), tweet()),
    )

    assert first == second
    assert requests == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_total_provider_failure_keeps_campaign_unreconciled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    evaluator = LlmBriefFilter(
        api_url="https://llm.test/chat",
        api_key="secret",
        model="model",
        cache=MemoryCache(),
        attempts=1,
        client=client,
    )

    with pytest.raises(ReconciliationUnavailableError, match="provider unavailable"):
        await evaluator.evaluate(campaign(), tweet())
    await client.aclose()


@pytest.mark.asyncio
async def test_one_missing_optimistic_check_cannot_be_frozen_as_a_rejection() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ConnectError("transient", request=request)
        return httpx.Response(200, json=completion("NO", "failed"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    evaluator = LlmBriefFilter(
        api_url="https://llm.test/chat",
        api_key="secret",
        model="model",
        cache=MemoryCache(),
        attempts=1,
        client=client,
    )

    with pytest.raises(ReconciliationUnavailableError, match="provider unavailable"):
        await evaluator.evaluate(campaign(), tweet())
    await client.aclose()


def test_validator_store_freezes_llm_cache_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "validator.sqlite3"
    result = BriefEvaluation(meets_brief=True, reasoning="pass", checks_used=2)
    ValidatorStore(path).persist_llm_evaluation("ab" * 32, result)

    assert ValidatorStore(path).llm_evaluation("ab" * 32) == result


def test_validator_store_keeps_first_llm_verdict(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    first = BriefEvaluation(meets_brief=True, reasoning="first", checks_used=1)
    later = BriefEvaluation(meets_brief=True, reasoning="different markdown", checks_used=1)

    assert store.persist_llm_evaluation("ab" * 32, first) == first
    assert store.persist_llm_evaluation("ab" * 32, later) == first
    assert store.llm_evaluation("ab" * 32) == first


def test_response_parser_preserves_v2_fields() -> None:
    result = parse_brief_evaluation(
        "## Requirement-by-Requirement\n- Req 1: Met\n"
        "## Verdict\nYES\n## Summary\nAll requirements met.",
        checks_used=1,
    )

    assert result == BriefEvaluation(
        meets_brief=True,
        reasoning="All requirements met.",
        detailed_breakdown="- Req 1: Met",
        checks_used=1,
    )
