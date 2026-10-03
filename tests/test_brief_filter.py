"""Golden and availability tests for v2-compatible brief evaluation."""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
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
        display="Campaign",
        brief="Talk about Bitcast and tag @bitcast_network",
        pools=("ecosystem",),
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


@asynccontextmanager
async def evaluator(
    handler: Callable[[httpx.Request], Any],
    cache: MemoryCache | None = None,
) -> AsyncIterator[LlmBriefFilter]:
    """Yield a single-attempt filter whose provider calls go to ``handler``."""

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield LlmBriefFilter(
            api_url="https://llm.test/chat",
            api_key="secret",
            model="model",
            cache=cache if cache is not None else MemoryCache(),
            attempts=1,
            client=client,
        )


def test_prompt_versions_have_frozen_hashes() -> None:
    # The templates are static apart from the brief and post, so these hashes
    # pin every phrase. Key wording: v6 checks only "instructions in the brief"
    # ("Do not add requirements that are not stated in the brief"; no product
    # or sponsor framing). v5 is sentiment-neutral ("Positive, neutral, mixed,
    # critical, and negative reviews are equally acceptable") yet requires
    # substance ("Generic praise ... do not constitute a review").
    expected = {
        1: "193ca82cc622774a2cb142bb724378b33fbdbf8ec113cc16778a1153297849a0",
        2: "f2d2d4c2cf16821be3decbf5ae2478ec5ff821abfb7cc289b96e106066efbcaf",
        5: "4a079a65ae1e2fdd5bddf3f42d334813d05056d749c3ae04178ecd414f4c5394",
        6: "a0f1bd9de1e43a9bb1a2cfc91b9e78cc82304298b87bb9d4f80c53892e526e57",
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


@pytest.mark.parametrize("version", [3, 4])
def test_retired_prompt_versions_are_unavailable(version: int) -> None:
    with pytest.raises(ValueError, match=r"Available versions: \[1, 2, 5, 6\]"):
        generate_brief_evaluation_prompt(
            {"brief": "Talk about Bitcast"},
            "A thoughtful Bitcast post",
            version,
        )


@pytest.mark.asyncio
async def test_optimistic_checks_short_circuit_and_replay_from_cache() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        payload = completion("NO", "first failed") if requests == 1 else completion("YES", "pass")
        return httpx.Response(200, json=payload)

    cache = MemoryCache()

    async with evaluator(handler, cache) as brief_filter:
        first = await brief_filter.evaluate(campaign(), tweet())
        replay = await brief_filter.evaluate(campaign(), tweet())

    assert first == replay
    assert first.meets_brief is True
    assert first.checks_used == 2
    assert requests == 2
    assert len(cache.values) == 2


@pytest.mark.asyncio
async def test_concurrent_identical_prompts_make_one_provider_request() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=completion("YES", "pass"))

    async with evaluator(handler) as brief_filter:
        first, second = await asyncio.gather(
            brief_filter.evaluate(campaign(), tweet()),
            brief_filter.evaluate(campaign(), tweet()),
        )

    assert first == second
    assert requests == 1


@pytest.mark.parametrize(
    "failed_requests",
    [
        pytest.param(1, id="one-check-unavailable-others-reject"),
        pytest.param(3, id="every-check-unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_unavailable_checks_keep_campaign_unreconciled(failed_requests: int) -> None:
    # One missing optimistic check might have passed, so the remaining NO
    # verdicts cannot be frozen as a rejection; total failure is never one either.
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests <= failed_requests:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json=completion("NO", "failed"))

    async with evaluator(handler) as brief_filter:
        with pytest.raises(ReconciliationUnavailableError, match="provider unavailable"):
            await brief_filter.evaluate(campaign(), tweet())

    # An unavailable check never ends evaluation early: any later check could pass.
    assert requests == 3


def test_validator_store_keeps_first_llm_verdict_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "validator.sqlite3"
    store = ValidatorStore(path)
    first = BriefEvaluation(meets_brief=True, reasoning="first", checks_used=1)
    later = BriefEvaluation(meets_brief=True, reasoning="different markdown", checks_used=1)

    assert store.persist_llm_evaluation("ab" * 32, first) == first
    assert store.persist_llm_evaluation("ab" * 32, later) == first
    assert store.llm_evaluation("ab" * 32) == first
    assert ValidatorStore(path).llm_evaluation("ab" * 32) == first


@pytest.mark.parametrize(
    ("response", "summary", "breakdown"),
    [
        pytest.param(
            "## Requirement-by-Requirement\n- Req 1: Met\n"
            "## Verdict\nYES\n## Summary\nAll requirements met.",
            "All requirements met.",
            "- Req 1: Met",
            id="v2-requirements",
        ),
        pytest.param(
            '## Objective Requirements\n- Req 1: Met — "quick to deploy"\n'
            "## Review Quality\n- Relevance: Met\n- Substance: Met\n"
            "## Verdict\nYES\n## Summary\nA specific mixed review.",
            "A specific mixed review.",
            '- Req 1: Met — "quick to deploy"',
            id="v5-objective-requirements",
        ),
        pytest.param(
            '## Instruction-by-Instruction\n- Instruction 1: Met — "launches Friday"\n'
            "## Verdict\nYES\n## Summary\nEvery stated instruction was met.",
            "Every stated instruction was met.",
            '- Instruction 1: Met — "launches Friday"',
            id="v6-instruction-breakdown",
        ),
    ],
)
def test_response_parser_preserves_versioned_fields(
    response: str, summary: str, breakdown: str
) -> None:
    result = parse_brief_evaluation(response, checks_used=1)

    assert result == BriefEvaluation(
        meets_brief=True,
        reasoning=summary,
        detailed_breakdown=breakdown,
        checks_used=1,
    )
