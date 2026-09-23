"""M1-351: a transient generation failure waits out its checkpoint window.

Driven through ``run_once`` with the launch harness, and -- the point of this file -- with the
**production** ``PricedClient`` behind an httpx MockTransport. The harness's fake ``Model``
bypasses the priced client's durable guard (``model_started`` without a completion refuses the
same request), and that is how M1-349 measured "six model calls per counted attempt": on the
fake. Through the real client an in-window re-attempt was already free. Every money count here
is a count of POSTs that reached the transport, never of recorded rows (M1-315).

What pacing changes is the rest of the re-attempt: a ``question_failure`` row, a duplicate
``generation_failed`` row and a failed poll (``heartbeat.failures`` -> exit 1 -> ``OnFailure``)
every five minutes.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, get_args

import httpx
import pytest
from forecasting_tools.data_models.data_organizer import DataOrganizer
from hypothesis import given, strategies as st

from test_evidence_poor import (  # type: ignore[import-not-found]
    _EmptyExa,
    _asknews_reservations,
    _clock,
    _exa_client,
    _failure_codes,
    _nothing_found,
    _rows,
)
from test_pipeline_live import reply_for  # type: ignore[import-not-found]
from test_tournament import case  # type: ignore[import-not-found]  # noqa: F401
from whiskeyjack_bot import tournament as whiskeyjack_tournament
from whiskeyjack_bot.forecast.priced import PricedClient
from whiskeyjack_bot.lifecycle import PreForecastFailureCode
from whiskeyjack_bot.pipeline_live import QuestionStatus
from whiskeyjack_bot.questions.normalize import normalize_questions
from whiskeyjack_bot.tournament import MAX_TRANSIENT_ATTEMPTS, _paces_retry

__all__ = ["case"]

POLLS_IN_WINDOW = 5  # after the first attempt: +5 .. +25 minutes, all inside 1800 s


class _OpenRouter:
    """A fake OpenRouter endpoint; ``posts`` counts what reached the transport (billed)."""

    def __init__(self, respond: Any) -> None:
        self.respond = respond
        self.posts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.posts += 1
        response: httpx.Response = self.respond(json.loads(request.content))
        return response


def _install(monkeypatch: pytest.MonkeyPatch, config: Any, endpoint: _OpenRouter) -> None:
    original = httpx.AsyncClient
    monkeypatch.setenv(config.model.api_key_env, "test-secret")
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(endpoint), **kw)
    )


def _unavailable(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(503, json={"error": {"message": "no provider"}})


def _not_json(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "not json at all"}}]})


def _poll(case: Any, forecaster: Any, web: Any) -> dict[str, Any]:
    conn, config, platform, news, _model = case
    result: dict[str, Any] = whiskeyjack_tournament.run_once(
        conn,
        config,
        client=platform,
        poster=platform,
        news_client=news,
        web_client=web,
        forecaster=forecaster,
    )
    return result


def _count(conn: Any, kind: str) -> int:
    return len(_rows(conn, kind))


def _run_window(
    case: Any, monkeypatch: pytest.MonkeyPatch, forecaster: Any, web: Any, billed: Any
) -> list[tuple[int, int]]:
    """One attempt, POLLS_IN_WINDOW polls inside its window, then the retry after it.

    Returns ``billed()`` after the attempt and after the retry. Asserts, on every in-window
    poll, that the question is declined for free: no new purchase, a wait and not a failure,
    and nothing further written to either failure table.
    """
    conn = case[0]
    clock = _clock(case, monkeypatch)
    first = _poll(case, forecaster, web)
    assert first["heartbeat"]["failures"] == 1, "the attempt itself still fails"
    after_attempt = billed()
    rows_after_attempt = (_count(conn, "question_failure"), len(_failure_codes(conn)))
    assert _count(conn, "retry_wait") == 1

    for _ in range(POLLS_IN_WINDOW):
        clock["now"] += timedelta(minutes=5)
        waited = _poll(case, forecaster, web)
        assert waited["heartbeat"]["retry_wait"] == 1
        assert waited["heartbeat"]["failures"] == 0, "waiting is not failing"
        assert billed() == after_attempt, "a poll inside the window must buy nothing"
    assert (_count(conn, "question_failure"), len(_failure_codes(conn))) == rows_after_attempt
    assert _count(conn, "question_started") == 1, "a wait is not an attempt"

    clock["now"] += timedelta(minutes=6)  # 31 minutes after the first attempt
    retried = _poll(case, forecaster, web)
    assert retried.get("heartbeat", {}).get("retry_wait", 0) == 0
    assert _count(conn, "question_started") == 2, "the retry is one counted attempt"
    return [after_attempt, billed()]


def test_a_failed_model_call_is_paid_once_per_counted_attempt(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario A, the common live shape: OpenRouter refuses. One POST per counted attempt."""
    conn, config, _platform, news, _model = case
    endpoint = _OpenRouter(_unavailable)
    _install(monkeypatch, config, endpoint)

    counts = _run_window(case, monkeypatch, PricedClient(config), object(), lambda: endpoint.posts)
    assert counts == [1, 2], "exactly one model call per counted attempt"
    reasons = [row["reason"] for row in _rows(conn, "question_failure")]
    assert reasons == ["priced model request failed or was unavailable at the authorized price"] * 2
    assert [row["error_type"] for row in _rows(conn, "question_failure")] == [
        "ModelOutcomeUnknown"
    ] * 2


def test_an_unusable_reply_is_one_generation_per_counted_attempt(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario B: the reply is not JSON, so the one bounded repair is bought (M1-402).

    One generation is the call plus its repair: two POSTs per counted attempt, never more,
    and exactly one ``generation_failed`` row per attempt rather than one per poll.
    """
    conn, config, _platform, _news, _model = case
    endpoint = _OpenRouter(_not_json)
    _install(monkeypatch, config, endpoint)

    counts = _run_window(case, monkeypatch, PricedClient(config), object(), lambda: endpoint.posts)
    assert counts == [2, 4]
    assert _failure_codes(conn) == [("generation_failed", "malformed_response")] * 2
    assert [row["reason"] for row in _rows(conn, "question_failure")] == [
        "question generation_failed (malformed_response)"
    ] * 2


def test_an_evidence_poor_generation_failure_buys_no_retrieval_inside_the_window(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario C, the criterion's second path: an empty packet, then a failed model call.

    Inside the window neither the model call nor the retrieval is bought again. After it,
    the retry is one counted attempt: one retrieval (as for any packet -- a checkpoint is
    reused only within 1800 s) and one model call.
    """
    conn, config, _platform, news, _model = case
    endpoint = _OpenRouter(_unavailable)
    _install(monkeypatch, config, endpoint)
    _nothing_found(news, monkeypatch)
    exa = _EmptyExa()

    counts = _run_window(
        case,
        monkeypatch,
        PricedClient(config),
        _exa_client(exa),
        lambda: (endpoint.posts, _asknews_reservations(conn), news.calls, exa.requests),
    )
    (posts, reservations, asknews_calls, exa_calls), retried = counts
    assert (posts, reservations) == (1, 1)
    assert retried == (2, 2, 2 * asknews_calls, 2 * exa_calls)


def test_the_fake_model_is_invoked_once_per_counted_attempt(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario D, M1-349's own measurement: a client with no durable guard of its own.

    On HEAD before M1-351 this was six invocations per counted attempt. It is the one shape
    where pacing changes a call count, and it is what `run_once` does with any forecaster
    that is not the priced client.
    """
    _conn, _config, _platform, _news, model = case
    calls = [0]

    async def raises(prompt: Any, system_prompt: str | None = None) -> str:
        calls[0] += 1
        raise RuntimeError("provider outage")

    monkeypatch.setattr(model, "invoke", raises)
    counts = _run_window(case, monkeypatch, model, object(), lambda: calls[0])
    assert counts == [1, 2]


def test_a_deterministic_generation_verdict_still_blocks_and_never_waits(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``schema_invalid`` is a verdict about the reply, so it blocks exactly as before."""
    conn, config, platform, _news, _model = case
    q = normalize_questions([DataOrganizer.get_question_from_post_json(platform.raw)]).questions[0]
    # A well-formed reply with the wrong cutoff: a verdict about the reply, `schema_invalid`,
    # and the repair turn gets the same wrong answer back.
    reply = dict(json.loads(reply_for(q)), as_of_utc="2000-01-01T00:00:00Z")

    def schema_invalid(body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    endpoint = _OpenRouter(schema_invalid)
    _install(monkeypatch, config, endpoint)
    clock = _clock(case, monkeypatch)
    _poll(case, PricedClient(config), object())
    assert _failure_codes(conn) == [("generation_failed", "schema_invalid")]
    assert _count(conn, "retry_wait") == 0
    blocked = _rows(conn, "question_blocked")
    assert [(row["reason"], row["detail_code"]) for row in blocked] == [
        ("deterministic_verdict", "schema_invalid")
    ]
    clock["now"] += timedelta(minutes=31)
    assert _poll(case, PricedClient(config), object())["heartbeat"]["blocked"] == 1


def test_the_final_attempt_still_exhausts_after_paced_generation_failures(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pacing adds no attempt and removes none: the cap is reached at the same attempt.

    The gate order is unchanged -- exhausted is checked before the wait -- so the first poll
    after the last counted attempt blocks the question, inside its window or not.
    """
    conn, config, _platform, _news, _model = case
    endpoint = _OpenRouter(_unavailable)
    _install(monkeypatch, config, endpoint)
    clock = _clock(case, monkeypatch)
    for _ in range(MAX_TRANSIENT_ATTEMPTS - 1):
        _poll(case, PricedClient(config), object())
        clock["now"] += timedelta(minutes=10)
        assert _poll(case, PricedClient(config), object())["heartbeat"]["retry_wait"] == 1
        clock["now"] += timedelta(minutes=21)
    _poll(case, PricedClient(config), object())
    assert endpoint.posts == MAX_TRANSIENT_ATTEMPTS
    clock["now"] += timedelta(minutes=5)
    final = _poll(case, PricedClient(config), object())
    assert final["heartbeat"]["exhausted"] == 1
    assert [row["reason"] for row in _rows(conn, "question_blocked")] == [
        "transient_attempts_exhausted"
    ]
    assert endpoint.posts == MAX_TRANSIENT_ATTEMPTS


# --- the rule itself ---------------------------------------------------------------------

# Written from the criterion, not read back from the code: research paces on exactly
# `provider_error` (M1-349, unchanged); generation paces on every code that is not a
# deterministic verdict; nothing else paces.
_DETERMINISTIC = {"no_evidence", "stale_evidence", "schema_invalid", "calibration_invalid"}
_CODES: list[str | None] = [*get_args(PreForecastFailureCode), None]


@pytest.mark.parametrize("status", get_args(QuestionStatus))
@pytest.mark.parametrize("code", _CODES)
def test_which_outcomes_pace(status: str, code: str | None) -> None:
    if status == "research_failed":
        expected = code == "provider_error"
    elif status == "generation_failed":
        expected = code not in _DETERMINISTIC
    else:
        expected = False
    assert _paces_retry(status, code) is expected


def test_the_generation_codes_that_pace_are_the_transient_six() -> None:
    pacing = {code for code in _CODES if _paces_retry("generation_failed", code)}
    assert pacing == {
        "internal_error",
        "timeout",
        "provider_error",
        "provider_unavailable",
        "malformed_response",
        "http_error",
        None,
    }


@given(status=st.text(), code=st.none() | st.text())
def test_the_rule_is_total(status: str, code: str | None) -> None:
    assert type(_paces_retry(status, code)) is bool
