"""M1-349: retry a failed provider before blocking, then an evidence-poor base-rate forecast.

Driven through ``run_once`` with the launch harness from ``test_tournament``: the real SDK
parsing, the real orchestration, the real approval policy and a fake platform. Every count
of money is a count of BILLED calls (``News.calls``, the Exa transport's requests) or of
``cost_reserved`` rows -- never of ``research_runs`` rows, which are wrong in both
directions (M1-315, M1-326).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from asknews_sdk.dto.news import SearchResponse

from whiskeyjack_bot import pipeline_live
from whiskeyjack_bot import tournament as whiskeyjack_tournament
from whiskeyjack_bot import tournament_state
from whiskeyjack_bot.forecast.record import record_sha256
from whiskeyjack_bot.forecast.store import read_forecast_record
from whiskeyjack_bot.tournament import (
    EVIDENCE_POOR_NOTICE,
    MAX_TRANSIENT_ATTEMPTS,
    comment_text,
)
from whiskeyjack_bot.tournament_state import TournamentError, append, enable, utcnow
from tests.unit.test_tournament import case, poll  # noqa: F401 - `case` is a fixture

__all__ = ["case"]


class _EmptyExa:
    """An Exa endpoint that answers, successfully, with nothing."""

    def __init__(self) -> None:
        self.requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return httpx.Response(
            200, json={"requestId": "r", "results": [], "costDollars": {"total": 0.01}}
        )


def _exa_client(exchange: _EmptyExa) -> httpx.Client:
    return httpx.Client(base_url="https://api.exa.ai", transport=httpx.MockTransport(exchange))


def _poll(case: Any, web_client: Any) -> dict[str, Any]:
    conn, config, platform, news, model = case
    return whiskeyjack_tournament.run_once(
        conn,
        config,
        client=platform,
        poster=platform,
        news_client=news,
        web_client=web_client,
        forecaster=model,
    )


def _clock(case: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    _conn, _config, platform, _news, _model = case
    real_now = utcnow()
    platform.raw["question"]["scheduled_close_time"] = (real_now + timedelta(hours=8)).isoformat()
    clock = {"now": real_now}
    monkeypatch.setattr(tournament_state, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(whiskeyjack_tournament, "utcnow", lambda: clock["now"])
    return clock


def _outage(news: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def search_news(**kwargs: Any) -> SearchResponse:
        news.calls += 1
        raise RuntimeError("provider outage")

    monkeypatch.setattr(news, "search_news", search_news)


def _nothing_found(news: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def search_news(**kwargs: Any) -> SearchResponse:
        news.calls += 1
        return SearchResponse.model_construct(as_dicts=[])

    monkeypatch.setattr(news, "search_news", search_news)


def _rows(conn: Any, kind: str) -> list[dict[str, Any]]:
    return [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT data FROM tournament_events WHERE kind=? ORDER BY seq", (kind,)
        )
    ]


def _asknews_reservations(conn: Any) -> int:
    return sum(1 for row in _rows(conn, "cost_reserved") if row.get("provider") == "asknews")


def _failure_codes(conn: Any) -> list[tuple[str, str]]:
    return [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT event_type, detail_code FROM pipeline_failure_events ORDER BY rowid"
        )
    ]


# --- the retry ---------------------------------------------------------------------------


def test_a_failed_primary_with_an_empty_fallback_is_retried_then_forecast_evidence_poor(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole criterion on the branch it is about: AskNews FAILS, Exa answers EMPTY.

    Before M1-349 this was `no_evidence` -- the last run succeeded, so the packet read as
    "nothing exists" -- and the first poll blocked the question for good.
    """
    conn, _config, platform, news, model = case
    clock = _clock(case, monkeypatch)
    _outage(news, monkeypatch)
    exa = _EmptyExa()
    web = _exa_client(exa)

    for attempt in range(1, MAX_TRANSIENT_ATTEMPTS):
        result = _poll(case, web)
        assert result["heartbeat"]["failures"] == 1
        assert _failure_codes(conn)[-1] == ("research_failed", "provider_error"), (
            "a failed primary with an empty fallback is a provider failure, not no_evidence"
        )
        assert not _rows(conn, "question_blocked"), f"attempt {attempt} must not block"
        # Two: the fallback pass sends both of its queries (`_fallback_pass`).
        assert exa.requests == 2 * attempt, "the fallback really ran and really answered empty"
        clock["now"] += timedelta(minutes=31)

    assert model.calls == platform.posts == 0

    final = _poll(case, web)
    assert final["heartbeat"]["failures"] == 0
    assert platform.posts == 1 and platform.comment_posts == 1
    assert model.requests[-1]["research_documents"] == []

    (record_id,) = [row[0] for row in conn.execute("SELECT record_id FROM forecast_records")]
    record = read_forecast_record(conn, record_id)
    assert record.sources == []
    markers = [
        row
        for row in conn.execute(
            "SELECT scope, data FROM tournament_events WHERE kind='evidence_gap'"
        )
    ]
    assert len(markers) == 1
    scope, data = markers[0][0], json.loads(markers[0][1])
    assert scope == record_id
    assert data["code"] == "evidence_poor"
    assert data["reason"] == "provider_failed_exhausted"
    assert data["forecast_sha256"] == record_sha256(record)

    approval = conn.execute("SELECT actor FROM approval_events").fetchone()[0]
    assert approval.startswith("policy:launch-v1:"), "the ordinary approval policy, no bypass"
    assert EVIDENCE_POOR_NOTICE in platform.comments[0]["text"]
    assert "Sources:\n(none)" in platform.comments[0]["text"]


def test_one_retry_costs_at_most_one_more_asknews_reservation(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paid-call count (M1-315): polls INSIDE a checkpoint window buy nothing.

    Before M1-349 a provider failure inside the 30-minute window was re-bought on every
    5-minute poll without counting as an attempt: six purchases per counted attempt.
    """
    conn, _config, _platform, news, _model = case
    clock = _clock(case, monkeypatch)
    _outage(news, monkeypatch)
    exa = _EmptyExa()
    web = _exa_client(exa)

    _poll(case, web)
    first = (_asknews_reservations(conn), news.calls, exa.requests)
    assert first[0] == 1, "one retrieval is one AskNews reservation (measured)"

    for _ in range(5):
        clock["now"] += timedelta(minutes=5)
        waited = _poll(case, web)
        assert waited["heartbeat"]["retry_wait"] == 1
        assert waited["heartbeat"]["failures"] == 0, "waiting is not failing"
    assert (_asknews_reservations(conn), news.calls, exa.requests) == first, (
        "a poll inside the window must not re-buy the research"
    )

    clock["now"] += timedelta(minutes=6)  # 31 minutes after the first attempt
    _poll(case, web)
    assert _asknews_reservations(conn) == 2, "one retry is exactly one more AskNews reservation"
    assert news.calls == 2 * first[1] and exa.requests == 2 * first[2]


# --- the base-rate fallback --------------------------------------------------------------


def _live_gate(case: Any) -> None:
    """Switch the harness to the LIVE sufficiency setting, `fail_on_stale_research: true`.

    The harness inherits the committed default (false, flag only), under which a missed
    stand-down of the sufficiency gate merely logs -- the mutation pass found that mutant
    surviving. Re-enabled afterwards because any config change retires the activation, which
    is the operator's real sequence too.
    """
    conn, config, _platform, _news, _model = case
    config.forecast.fail_on_stale_research = True
    enable(
        conn,
        config,
        account_id=42,
        project_id=32977,
        starts=utcnow() - timedelta(minutes=1),
        ends=utcnow() + timedelta(days=1),
    )


@pytest.mark.parametrize("live_gate", [False, True], ids=["default-gate", "live-gate"])
def test_every_provider_finding_nothing_forecasts_evidence_poor_at_once(
    case: Any, monkeypatch: pytest.MonkeyPatch, live_gate: bool
) -> None:
    conn, _config, platform, news, model = case
    if live_gate:
        _live_gate(case)
    _clock(case, monkeypatch)
    _nothing_found(news, monkeypatch)
    exa = _EmptyExa()

    result = _poll(case, _exa_client(exa))
    assert result["heartbeat"]["failures"] == 0
    assert exa.requests == 2, "the fallback pass sent both queries and they answered empty"
    assert platform.posts == 1 and model.calls == 1
    assert _failure_codes(conn) == []
    assert not _rows(conn, "question_blocked")
    (marker,) = _rows(conn, "evidence_gap")
    assert (marker["code"], marker["reason"]) == ("evidence_poor", "no_documents")
    assert EVIDENCE_POOR_NOTICE in platform.comments[0]["text"]


def test_a_forecast_with_evidence_carries_no_marker_and_no_notice(case: Any) -> None:
    conn, _config, platform, _news, _model = case
    poll(case)
    assert platform.posts == 1
    assert not [row for row in _rows(conn, "evidence_gap") if row["code"] == "evidence_poor"]
    assert "Evidence: none retrieved" not in platform.comments[0]["text"]


@pytest.mark.parametrize("field", ["future", "stale"])
def test_documents_that_are_all_unusable_still_block(case: Any, field: str) -> None:
    """Evidence-poor mode is for an EMPTY retrieval. Documents that exist but are stale or
    from the future are a deterministic verdict, exactly as before."""
    conn, _config, platform, news, model = case
    setattr(news, field, True)
    poll(case)
    assert _failure_codes(conn) == [("research_failed", "stale_evidence")]
    assert [row["reason"] for row in _rows(conn, "question_blocked")] == ["deterministic_verdict"]
    assert model.calls == platform.posts == 0
    assert not _rows(conn, "evidence_gap")


def test_a_schema_invalid_evidence_poor_reply_still_blocks(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evidence-poor forecast passes the ORDINARY validation: a reply citing documents
    it was never shown is refused, and `schema_invalid` stays deterministic."""
    conn, _config, platform, news, model = case
    _clock(case, monkeypatch)
    _nothing_found(news, monkeypatch)
    original = model.invoke

    async def cites_anyway(prompt: Any, system_prompt: str | None = None) -> str:
        reply = json.loads(await original(prompt, system_prompt))
        reply["base_rate"]["source_ids"] = ["src-001"]
        return json.dumps(reply)

    monkeypatch.setattr(model, "invoke", cites_anyway)
    _poll(case, _exa_client(_EmptyExa()))
    assert platform.posts == 0
    assert ("generation_failed", "schema_invalid") in _failure_codes(conn)
    assert [row["detail_code"] for row in _rows(conn, "question_blocked")] == ["schema_invalid"]


# --- the marker is bound to the hash -----------------------------------------------------


def test_a_marker_for_other_content_is_refused_and_nothing_is_posted(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound like an approval: a marker whose hash is not the record's stops the question
    BEFORE approval, so no forecast is posted under a claim about different content."""
    conn, _config, platform, news, _model = case
    _clock(case, monkeypatch)
    _nothing_found(news, monkeypatch)
    monkeypatch.setattr(pipeline_live, "record_sha256", lambda record: "0" * 64)

    result = _poll(case, _exa_client(_EmptyExa()))
    assert result["heartbeat"]["failures"] == 1
    assert platform.posts == platform.comment_posts == 0
    assert not conn.execute("SELECT 1 FROM approval_events").fetchall()


def test_the_comment_refuses_a_marker_that_does_not_match(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _config, _platform, news, _model = case
    _clock(case, monkeypatch)
    _nothing_found(news, monkeypatch)
    _poll(case, _exa_client(_EmptyExa()))
    (record_id,) = [row[0] for row in conn.execute("SELECT record_id FROM forecast_records")]
    assert EVIDENCE_POOR_NOTICE in comment_text(conn, record_id)

    planted = "f" * 64
    append(conn, "evidence_gap", record_id, {"code": "evidence_poor", "forecast_sha256": planted})
    with pytest.raises(TournamentError) as raised:
        comment_text(conn, record_id)
    assert planted not in str(raised.value)
