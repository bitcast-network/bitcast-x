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


def test_prompt_versions_have_frozen_hashes() -> None:
    expected = {
        1: "a0f1bd9de1e43a9bb1a2cfc91b9e78cc82304298b87bb9d4f80c53892e526e57",
        2: "f2d2d4c2cf16821be3decbf5ae2478ec5ff821abfb7cc289b96e106066efbcaf",
        5: "4a079a65ae1e2fdd5bddf3f42d334813d05056d749c3ae04178ecd414f4c5394",
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


def test_generic_prompt_only_checks_instructions_in_the_brief() -> None:
    prompt = generate_brief_evaluation_prompt(
        {"brief": "Explain the launch date and include #Example."},
        "Example launches Friday. #Example",
        1,
    )

    assert "follows all instructions in the brief" in prompt
    assert "Treat the brief as the complete source of requirements" in prompt
    assert "Do not add requirements that are not stated in the brief" in prompt
    assert "product or service" not in prompt
    assert "sponsor" not in prompt.lower()


@pytest.mark.parametrize("version", [3, 4])
def test_retired_prompt_versions_are_unavailable(version: int) -> None:
    with pytest.raises(ValueError, match=r"Available versions: \[1, 2, 5\]"):
        generate_brief_evaluation_prompt(
            {"brief": "Talk about Bitcast"},
            "A thoughtful Bitcast post",
            version,
        )


def test_honest_review_prompt_is_sentiment_neutral_and_requires_substance() -> None:
    prompt = generate_brief_evaluation_prompt(
        {"brief": "Review Example Cloud after trying its deployment workflow."},
        "Example Cloud was quick to deploy, but its logs were difficult to navigate.",
        5,
    )

    assert (
        "Positive, neutral, mixed, critical, and negative reviews are equally acceptable" in prompt
    )
    assert (
        "Generic praise, promotional slogans, or a passing mention do not constitute a review"
        in prompt
    )
    assert "Relevant comparisons with alternatives count as on-topic" in prompt
    assert "must not be negative or critical" not in prompt


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


def test_response_parser_preserves_v5_objective_requirements() -> None:
    result = parse_brief_evaluation(
        '## Objective Requirements\n- Req 1: Met — "quick to deploy"\n'
        "## Review Quality\n- Relevance: Met\n- Substance: Met\n"
        "## Verdict\nYES\n## Summary\nA specific mixed review.",
        checks_used=1,
    )

    assert result == BriefEvaluation(
        meets_brief=True,
        reasoning="A specific mixed review.",
        detailed_breakdown='- Req 1: Met — "quick to deploy"',
        checks_used=1,
    )
