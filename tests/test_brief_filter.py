"""Golden request, decision and availability tests for JEV brief evaluation."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from bitcast_x.brief_filter import (
    BriefEvaluation,
    JevBriefFilter,
    decide,
    missing_required_items,
)
from bitcast_x.campaigns import CampaignRecord
from bitcast_x.errors import ReconciliationUnavailableError
from bitcast_x.prompts import JEV_MODEL, VERSION_GATES, build_request
from bitcast_x.protocol import CampaignAccess, MiningProtocol
from bitcast_x.validator.store import ValidatorStore
from bitcast_x.x_provider import Tweet

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
BRIEF = "Talk about Bitcast and tag @bitcast_network"


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, BriefEvaluation] = {}

    def llm_evaluation(self, prompt_hash: str) -> BriefEvaluation | None:
        return self.values.get(prompt_hash)

    def persist_llm_evaluation(self, prompt_hash: str, result: BriefEvaluation) -> BriefEvaluation:
        return self.values.setdefault(prompt_hash, result)


def campaign(*, prompt_version: int = 2, brief: str = BRIEF) -> CampaignRecord:
    return CampaignRecord(
        access=CampaignAccess(
            campaign_id="campaign",
            mechanism_id=1,
            mining_protocol=MiningProtocol.PRECLAIM_V2,
            scoring_close_block=20,
        ),
        display="Campaign",
        brief=brief,
        pools=("ecosystem",),
        opens_at=NOW,
        closes_at=NOW + timedelta(days=1),
        reward_pool_usd="1000",
        prompt_version=prompt_version,
    )


def tweet(text: str = "@bitcast_network pays creators for verified X engagement") -> Tweet:
    return Tweet(tweet_id="123", author_x_id="456", created_at=NOW, text=text, author="creator")


def answers(
    prompt_version: int = 2, *, accept: float = 0.9, mismatch: float = 0.01, gate: float = 0.9
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "verdict": {"type": "choice", "probabilities": {"ACCEPT": accept, "REJECT": 1 - accept}},
        "identity": {
            "type": "choice",
            "probabilities": {"MATCH": 1 - mismatch, "MISMATCH": mismatch, "UNESTABLISHED": 0.0},
        },
    }
    for name in VERSION_GATES[prompt_version]:
        result[f"gate_{name}"] = {"type": "noul", "noul": gate}
    return result


def response(prompt_version: int = 2, **kwargs: float) -> dict[str, Any]:
    return {
        "id": "gen-dec-test",
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "answers": answers(prompt_version, **kwargs),
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
    }


def test_request_versions_have_frozen_hashes() -> None:
    expected = {
        1: "83bcab0bb404081a674e42d81aa16d4be96fe72fcb285399c240ea3975a80782",
        2: "1e7c78cb3f67b2496a60cc3354b9ff1c1f5260658761de06c5bb1404119b5480",
        5: "fc833ffa5635013f488e6fb4334badb64daf4125c38cb65b3780e8622157987e",
        6: "5c4e7ea96b4ec92cc79337ff3a3d515cf9ba512ae292a6d550dda1968f740751",
    }

    actual = {
        version: hashlib.sha256(
            json.dumps(build_request(BRIEF, version, "post"), sort_keys=True).encode()
        ).hexdigest()
        for version in expected
    }

    assert actual == expected


@pytest.mark.parametrize(
    ("version", "gates"),
    [
        (1, {"gate_nonnegative"}),
        (2, {"gate_nonnegative", "gate_focus80"}),
        (5, {"gate_primary", "gate_substance"}),
        (6, set()),
    ],
)
def test_request_carries_version_policy_and_its_gates(version: int, gates: set[str]) -> None:
    request = build_request(BRIEF, version, "post")

    assert request["model"] == JEV_MODEL
    assert request["state"]["prompt_version"] == version
    assert request["state"]["version_policy"].startswith(f"X v{version} ")
    assert set(request["questions"]) == {"verdict", "identity"} | gates
    assert all(BRIEF in request["questions"][gate]["instructions"] for gate in gates)


@pytest.mark.parametrize("version", [3, 4, 7])
def test_unsupported_prompt_versions_are_refused(version: int) -> None:
    with pytest.raises(ValueError, match="Unsupported prompt version"):
        build_request(BRIEF, version, "post")


def test_decision_accepts_only_when_every_check_passes() -> None:
    post = "@bitcast_network pays creators"

    assert decide(answers(), BRIEF, post).meets_brief
    focus = decide(answers(gate=0.59), BRIEF, post)
    assert not focus.meets_brief and "focus80 not met" in focus.reasoning
    assert not decide(answers(accept=0.01), BRIEF, post).meets_brief
    assert not decide(answers(mismatch=0.5), BRIEF, post).meets_brief
    missing = decide(answers(), BRIEF, "Bitcast pays creators")
    assert not missing.meets_brief and "@bitcast_network" in missing.reasoning
    assert json.loads(missing.detailed_breakdown or "{}")["missing_required"] == [
        "@bitcast_network"
    ]


def test_required_items_come_only_from_explicit_instructions() -> None:
    header = "Brand: Bitcast (@bitcast_network). Product: creator rewards."
    assert missing_required_items(header, "Bitcast pays creators") == []
    assert missing_required_items("Also tag @A and @B.", "thanks @a") == ["@b"]
    quote = "Talk about Bitcast. Quote https://x.com/bitcast_network/status/1"
    assert missing_required_items(quote, "Bitcast pays creators") == []
    ref = "Talk about BUD + include ref link: 'budsignal.io/?ref='"
    assert missing_required_items(ref, "try it") == ["link"]
    assert missing_required_items(ref, "try it https://t.co/abc") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["typesafe/jev-1.13", "typesafe/jev-1.13-20260917"])
async def test_one_openrouter_request_is_sent_and_replayed_from_cache(model: str) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://openrouter.ai/api/v1/systemone"
        assert request.headers["Authorization"] == "Bearer existing-openrouter-key"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={**response(), "model": model})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    evaluator = JevBriefFilter(
        api_key="existing-openrouter-key", cache=MemoryCache(), client=client
    )

    first = await evaluator.evaluate(campaign(), tweet())
    second = await evaluator.evaluate(campaign(), tweet())

    assert first.meets_brief and second == first
    assert len(sent) == 1
    assert sent[0]["model"] == "typesafe/jev-1.13"
    assert sent[0] == build_request(BRIEF, 2, tweet().text)
    await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_identical_requests_make_one_provider_request() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=response())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    evaluator = JevBriefFilter(api_key="secret", cache=MemoryCache(), attempts=1, client=client)

    first, second = await asyncio.gather(
        evaluator.evaluate(campaign(), tweet()),
        evaluator.evaluate(campaign(), tweet()),
    )

    assert first == second
    assert requests == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_failure_keeps_campaign_unreconciled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = MemoryCache()
    evaluator = JevBriefFilter(api_key="secret", cache=cache, attempts=1, client=client)

    with pytest.raises(ReconciliationUnavailableError, match="provider unavailable"):
        await evaluator.evaluate(campaign(), tweet())
    assert cache.values == {}
    await client.aclose()


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
async def test_oversized_response_is_unavailable_and_can_be_retried(
    headers: dict[str, str],
) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(200, content=b" " * 1025, headers=headers)
        return httpx.Response(200, json=response())

    cache = MemoryCache()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evaluator = JevBriefFilter(
            api_key="secret", cache=cache, attempts=1, max_response_bytes=1024, client=client
        )
        with pytest.raises(ReconciliationUnavailableError):
            await evaluator.evaluate(campaign(), tweet())
        assert cache.values == {}
        assert (await evaluator.evaluate(campaign(), tweet())).meets_brief
        assert requests == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"model": "other", "answers": answers()},
        {"model": "typesafe/jev-1.14", "answers": answers()},
        {"model": "typesafe/jev-1.13-20261001", "answers": answers()},
        {"model": "~typesafe/jev-latest", "answers": answers()},
        {"model": "jev-1.13.0", "answers": answers()},
        {"model": JEV_MODEL, "answers": answers(5)},
        {"model": JEV_MODEL, "answers": answers(accept=1.5)},
        {"model": JEV_MODEL},
    ],
)
async def test_malformed_responses_are_unavailable_not_rejections(payload: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = MemoryCache()
    evaluator = JevBriefFilter(api_key="secret", cache=cache, attempts=1, client=client)

    with pytest.raises(ReconciliationUnavailableError):
        await evaluator.evaluate(campaign(), tweet())
    assert cache.values == {}
    await client.aclose()


def test_validator_store_freezes_llm_cache_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "validator.sqlite3"
    result = BriefEvaluation(meets_brief=True, reasoning="pass", checks_used=1)
    ValidatorStore(path).persist_llm_evaluation("ab" * 32, result)

    assert ValidatorStore(path).llm_evaluation("ab" * 32) == result


def test_validator_store_keeps_first_llm_verdict(tmp_path: Path) -> None:
    store = ValidatorStore(tmp_path / "validator.sqlite3")
    first = BriefEvaluation(meets_brief=True, reasoning="first", checks_used=1)
    later = BriefEvaluation(meets_brief=False, reasoning="later", checks_used=1)

    assert store.persist_llm_evaluation("ab" * 32, first) == first
    assert store.persist_llm_evaluation("ab" * 32, later) == first
    assert store.llm_evaluation("ab" * 32) == first
