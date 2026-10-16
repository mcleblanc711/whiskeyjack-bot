"""M1-358: the local-source lens in the tournament worker -- collect-only, and never fatal.

Driven through the real ``run_once`` with ``test_tournament``'s fake platform, news and model,
on a question about the Strait of Hormuz, so the lens plans an Exa call for Iran and an
AskNews call for the UAE. What is asserted is the acceptance criterion's contract:

- disabled (the shipped default), the lens makes no call and writes nothing;
- enabled, it runs after the question's forecast and post, writes lens runs and one
  ``local_lens_checkpoint``, and never enters ``research_checkpoint`` or the forecast's inputs;
- it is bought once per research checkpoint;
- a missing registry, a refused AskNews reservation (the series sub-cap), a provider failure
  and its own time bound are each recorded, and the question still succeeds.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_pipeline_live import config as base_config
from test_tournament import Model, News, Platform

from whiskeyjack_bot import tournament as whiskeyjack_tournament
from whiskeyjack_bot.config import validate_config_data
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.research.store import load_locales, load_packet
from whiskeyjack_bot.tournament import run_once
from whiskeyjack_bot.tournament_state import (
    AskNewsSubcapReached,
    Budget,
    enable,
    events,
    utcnow,
)

ROOT = Path(__file__).resolve().parents[2]
TITLE = "[SYNTHETIC FIXTURE] Will Iran close the Strait of Hormuz before 2026-11-01?"


class RecordingNews(News):
    """``test_tournament.News`` that also keeps every request it was sent."""

    def __init__(self, raw: dict[str, Any]) -> None:
        super().__init__(raw)
        self.requests: list[dict[str, Any]] = []

    def search_news(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return super().search_news(**kwargs)


class ExaTransport:
    def __init__(self, *, status: int = 200, delay: float = 0.0) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.status = status
        self.delay = delay

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        if self.delay:
            time.sleep(self.delay)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "unavailable"})
        return httpx.Response(
            200,
            json={
                "requestId": "r1",
                "results": [
                    {
                        "title": "تنگه هرمز باز است یا بسته؟",
                        "url": "https://www.mehrnews.com/news/6974299",
                        "publishedDate": (utcnow() - timedelta(days=1)).isoformat(),
                        "text": "متن خبر درباره تنگه هرمز",
                    }
                ],
                "costDollars": {"total": 0.007},
            },
        )


def _build(tmp_path: Path, *, enabled: bool, registry: Path | None = None) -> Any:
    config = base_config.__wrapped__(tmp_path)
    data = config.model_dump(mode="json")
    prompt = tmp_path / "forecaster.md"
    prompt.write_bytes(config.forecast.prompt_path.read_bytes())
    data["forecast"]["prompt_path"] = str(prompt)
    data["metaculus"]["tournament"].update(id=32977, use_sdk_current_id=False)
    data["model"].update(
        name=Model.model, max_output_tokens=6000, timeout_seconds=120, temperature=None
    )
    data["submission"].update(
        enabled=True, dry_run=False, no_submit=False, post_private_reasoning_comment=True
    )
    data["retrieval"]["primary"]["retries"] = 0
    data["retrieval"]["fallback"]["retries"] = 0
    data["retrieval"]["local_lens"] = {
        "enabled": enabled,
        "registry_path": str(registry or ROOT / "config" / "locales.yaml"),
        "max_calls_per_question": 2,
    }
    config = validate_config_data(data)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    raw = json.loads((ROOT / "tests/fixtures/api_posts/binary_post.json").read_text())
    raw["projects"]["default_project"]["id"] = 32977
    raw["question"]["title"] = TITLE
    raw["question"]["scheduled_close_time"] = (utcnow() + timedelta(hours=2)).isoformat()
    enable(
        conn,
        config,
        account_id=42,
        project_id=32977,
        starts=utcnow() - timedelta(minutes=1),
        ends=utcnow() + timedelta(days=1),
    )
    return conn, config, Platform(raw), RecordingNews(raw), Model(raw)


@pytest.fixture
def enabled_case(tmp_path: Path) -> Iterator[Any]:
    case = _build(tmp_path, enabled=True)
    yield case
    case[0].close()


@pytest.fixture
def disabled_case(tmp_path: Path) -> Iterator[Any]:
    case = _build(tmp_path, enabled=False)
    yield case
    case[0].close()


def _poll(case: Any, transport: ExaTransport) -> dict[str, Any]:
    conn, config, platform, news, model = case
    web = httpx.Client(base_url="https://api.exa.ai", transport=httpx.MockTransport(transport))
    return run_once(
        conn,
        config,
        client=platform,
        poster=platform,
        news_client=news,
        web_client=web,
        forecaster=model,
    )


def _lens_events(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT DISTINCT scope FROM tournament_events WHERE kind = 'local_lens_checkpoint'"
    ).fetchall()
    return [
        event for (subject,) in rows for event in events(conn, "local_lens_checkpoint", subject)
    ]


def _research_run_ids(conn: Any) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT scope FROM tournament_events WHERE kind = 'research_checkpoint'"
    ).fetchall()
    return {
        run_id
        for (subject,) in rows
        for event in events(conn, "research_checkpoint", subject)
        for run_id in event["run_ids"]
    }


def test_disabled_the_lens_makes_no_call_and_writes_nothing(disabled_case: Any) -> None:
    transport = ExaTransport()
    result = _poll(disabled_case, transport)
    conn, _, _, news, _ = disabled_case
    assert result["forecast_confirmed"] == 1 and result["heartbeat"]["failures"] == 0
    assert _lens_events(conn) == []
    assert transport.bodies == []
    assert [set(request) for request in news.requests] == [
        {"query", "strategy", "n_articles", "return_type", "hours_back", "historical"}
    ]
    assert conn.execute("SELECT count(*) FROM research_document_locales").fetchone()[0] == 0


def test_enabled_the_lens_runs_after_the_forecast_and_never_reaches_it(enabled_case: Any) -> None:
    transport = ExaTransport()
    result = _poll(enabled_case, transport)
    conn, _, platform, news, model = enabled_case
    assert result["forecast_confirmed"] == 1 and result["heartbeat"]["failures"] == 0
    assert platform.posts == 1

    (event,) = _lens_events(conn)
    assert "error_type" not in event
    assert [(call["locale"], call["provider"]) for call in event["calls"]] == [
        ("IR", "exa"),
        ("AE", "asknews"),
    ]
    lens_ids = set(event["lens_run_ids"])
    assert len(lens_ids) == 2
    # Collect-only: the research checkpoint, which the forecast and the approval policy read,
    # names only the baseline runs, and the model was shown none of the lens's documents.
    assert not lens_ids & _research_run_ids(conn)
    assert set(event["baseline_run_ids"]) == _research_run_ids(conn)
    shown = {doc["url"] for request in model.requests for doc in request["research_documents"]}
    assert "https://mehrnews.com/news/6974299" not in shown
    # The lens bought what it planned: one Exa search, one AskNews search for local domains.
    assert len(transport.bodies) == 1 and "mehrnews.com" in transport.bodies[0]["includeDomains"]
    assert [("domain_url" in request) for request in news.requests] == [False, True]
    # And recorded locale rows the 1.1.0 packet carries.
    exa_run = next(c["retrieval_run_id"] for c in event["calls"] if c["provider"] == "exa")
    (row,) = load_locales(conn, exa_run)
    assert (row.locale_code, row.publisher_origin) == ("IR", "local")
    assert load_packet(conn, question_id=row_question(conn), retrieval_run_ids=[exa_run]).locales


def row_question(conn: Any) -> int:
    return int(conn.execute("SELECT question_id FROM forecast_records LIMIT 1").fetchone()[0])


def test_the_lens_is_bought_once_per_research_checkpoint(enabled_case: Any) -> None:
    transport = ExaTransport()
    _poll(enabled_case, transport)
    conn, _, _, news, _ = enabled_case
    calls = (len(news.requests), len(transport.bodies))
    _poll(enabled_case, transport)
    assert (len(news.requests), len(transport.bodies)) == calls
    assert len(_lens_events(conn)) == 1


def test_a_missing_registry_is_recorded_and_the_question_still_succeeds(tmp_path: Path) -> None:
    case = _build(tmp_path, enabled=True, registry=tmp_path / "absent.yaml")
    try:
        result = _poll(case, ExaTransport())
        assert result["forecast_confirmed"] == 1 and result["heartbeat"]["failures"] == 0
        (event,) = _lens_events(case[0])
        assert event["error_type"] == "LocaleError"
    finally:
        case[0].close()


def test_a_provider_failure_is_recorded_on_the_lens_run_only(enabled_case: Any) -> None:
    result = _poll(enabled_case, ExaTransport(status=500))
    conn = enabled_case[0]
    assert result["forecast_confirmed"] == 1 and result["heartbeat"]["failures"] == 0
    (event,) = _lens_events(conn)
    exa_call = next(call for call in event["calls"] if call["provider"] == "exa")
    assert exa_call["provider_failed"] is True


def test_a_refused_asknews_reservation_stops_only_the_lens_call(
    enabled_case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The series AskNews sub-cap (M1-355) reached between the baseline and the lens.

    Simulated at `Budget.reserve`, which is where the real refusal is raised; only a request
    carrying the lens's `domain_url` is refused, so the baseline call still buys.
    """
    reserve = Budget.reserve

    def capped(self: Budget, provider: str, estimate: float, request: Any) -> str:
        if provider == "asknews" and isinstance(request, dict) and "domain_url" in request:
            raise AskNewsSubcapReached("AskNews sub-cap reached")
        return reserve(self, provider, estimate, request)

    monkeypatch.setattr(Budget, "reserve", capped)
    result = _poll(enabled_case, ExaTransport())
    conn, _, _, news, _ = enabled_case
    assert result["forecast_confirmed"] == 1 and result["heartbeat"]["failures"] == 0
    (event,) = _lens_events(conn)
    asknews_call = next(call for call in event["calls"] if call["provider"] == "asknews")
    assert asknews_call["requests"] == 0 and asknews_call["documents"] == 0
    assert asknews_call["provider_failed"] is False
    assert [("domain_url" in request) for request in news.requests] == [False]


def test_the_lens_time_bound_is_recorded_and_the_question_still_succeeds(
    enabled_case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(whiskeyjack_tournament, "LENS_TIMEOUT_SECONDS", 0.2)
    result = _poll(enabled_case, ExaTransport(delay=2.0))
    conn = enabled_case[0]
    assert result["forecast_confirmed"] == 1 and result["heartbeat"]["failures"] == 0
    (event,) = _lens_events(conn)
    assert event["error_type"] == "TournamentError"
