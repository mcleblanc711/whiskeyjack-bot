"""The reasoning effort sent, and the effort recorded for it (M1-367, D59); providers are fake.

The witness is outside the program: the effort is read out of the JSON body the client
actually posted to the (mock) OpenRouter transport, and out of the ``model_response`` the
client journaled for that call. The expected value is the literal ``"high"``, not
``priced.REASONING_EFFORT``: a test comparing the constant with itself pins nothing.
"""

from __future__ import annotations

import asyncio
import math
import json
from typing import Any

import httpx
import pytest

from test_tournament import case as tournament_case  # type: ignore[import-not-found]
from whiskeyjack_bot.config import validate_config_data
from whiskeyjack_bot.forecast.priced import PRICED_MODELS, PricedClient, build_request
from whiskeyjack_bot.tournament_state import Budget, budget_context, digest, events

SCOPE = "42:32977"
PROMPT = [{"role": "user", "content": "the one prompt"}]
USAGE = {
    "completion_tokens": 1138,
    "completion_tokens_details": {"reasoning_tokens": 312},
    "cost": 0.05,
    "is_byok": False,
    "prompt_tokens": 10067,
    "total_tokens": 11205,
}


@pytest.fixture
def case(tmp_path: Any) -> Any:
    yield from tournament_case.__wrapped__(tmp_path)


def _with_model(config: Any, name: str) -> Any:
    data = config.model_dump(mode="json")
    data["model"]["name"] = name
    return validate_config_data(data)


def _capture_posts(monkeypatch: Any, config: Any) -> list[dict[str, Any]]:
    posted: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "model": "fake",
                "usage": USAGE,
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setenv(config.model.api_key_env, "test-secret")
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    return posted


@pytest.mark.parametrize("model", sorted(PRICED_MODELS))
def test_the_effort_posted_is_high_and_is_what_the_journal_records(
    case: Any, monkeypatch: Any, model: str
) -> None:
    conn, config, *_ = case
    config = _with_model(config, model)
    posted = _capture_posts(monkeypatch, config)
    budget = Budget(conn, config.storage.artifact_root, SCOPE, 10_000_000)
    with budget_context(budget):
        assert asyncio.run(PricedClient(config).invoke(PROMPT)) == "ok"

    (body,) = posted
    assert body["reasoning"] == {"effort": "high", "exclude": True}

    scope = f"{SCOPE}:{digest(build_request(PROMPT, PRICED_MODELS[model]))}"
    (started,) = events(conn, "model_started", scope)
    (response,) = events(conn, "model_response", started["reservation_id"])
    assert response["reasoning_effort"] == body["reasoning"]["effort"] == "high"
    assert response["schema_version"] == "1.1.0"
    # The rest of the stored response is unchanged by M1-367.
    assert response["usage"] == USAGE and response["finish_reason"] == "stop"
    assert response["content"] == "ok" and response["model"] == "fake"


def test_a_replayed_call_posts_nothing_and_writes_no_second_response(
    case: Any, monkeypatch: Any
) -> None:
    """The cache scope is keyed on the high-effort request, so a repeat is a replay."""
    conn, config, *_ = case
    config = _with_model(config, "openrouter/openai/gpt-6-astra")
    posted = _capture_posts(monkeypatch, config)
    budget = Budget(conn, config.storage.artifact_root, SCOPE, 10_000_000)
    with budget_context(budget):
        asyncio.run(PricedClient(config).invoke(PROMPT))
        asyncio.run(PricedClient(config).invoke(PROMPT))
    assert len(posted) == 1
    scope = f"{SCOPE}:{digest(build_request(PROMPT, PRICED_MODELS[config.model.name]))}"
    (started,) = events(conn, "model_started", scope)
    assert len(events(conn, "model_response", started["reservation_id"])) == 1


def test_a_pre_m1_367_response_without_the_field_still_settles_on_replay(
    case: Any, monkeypatch: Any
) -> None:
    """Readers of ``model_response`` use ``.get``; a 1.0.0 event with no effort still reads."""
    from whiskeyjack_bot.tournament_state import append, spending

    conn, config, *_ = case
    config = _with_model(config, "openrouter/openai/gpt-6-astra")
    posted = _capture_posts(monkeypatch, config)
    budget = Budget(conn, config.storage.artifact_root, SCOPE, 10_000_000)
    request = build_request(PROMPT, PRICED_MODELS[config.model.name])
    reservation = budget.reserve("openrouter", 1.0, request)
    scope = f"{SCOPE}:{digest(request)}"
    append(conn, "model_started", scope, {"reservation_id": reservation})
    append(conn, "model_completed", scope, {"content": "ok", "cost": 0.05})
    append(
        conn,
        "model_response",
        reservation,
        {"schema_version": "1.0.0", "usage": USAGE, "content": "ok"},
    )
    with budget_context(budget):
        assert asyncio.run(PricedClient(config).invoke(PROMPT)) == "ok"
    assert posted == []
    assert spending(conn, SCOPE) == (50_000, 0)


@pytest.mark.parametrize("model", sorted(PRICED_MODELS))
def test_the_estimate_moves_only_by_the_effort_strings_bytes(model: str) -> None:
    """AC6 as corrected after review round 1 (B1).

    The estimator bounds input by the request's UTF-8 bytes, so "high" (2 bytes shorter
    than "medium") reserves exactly two bytes of input less, and the output term does not
    move. The expected figures are literals, not recomputed from the estimator.
    """
    from copy import deepcopy

    from whiskeyjack_bot.forecast.priced import reservation_estimate_usd

    priced = PRICED_MODELS[model]
    high = build_request(PROMPT, priced)
    medium = deepcopy(high)
    medium["reasoning"]["effort"] = "medium"
    micro = {
        effort: math.ceil(reservation_estimate_usd(request, priced) * 1_000_000)
        for effort, request in (("medium", medium), ("high", high))
    }
    expected = {
        "openrouter/openai/gpt-5.6-sol": {"medium": 68_702, "high": 68_698},
        "openrouter/openai/gpt-6-astra": {"medium": 343_520, "high": 343_500},
    }[model]
    assert micro == expected
    # Two bytes of input at the model's prompt price, and nothing from the output term.
    assert micro["medium"] - micro["high"] == 2 * priced.prompt_usd_per_mtok
