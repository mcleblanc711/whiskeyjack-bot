"""M1-358: the local-source lens -- planning, locale rows, and one question's lens calls.

The worker hook (when the lens runs, and that it never changes the forecast) is
``tests/unit/test_local_lens_worker.py``; the offline end-to-end slice is
``tests/unit/test_local_lens_slice.py``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from test_research_orchestrate import _SDK, _article

from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion
from whiskeyjack_bot.research import asknews, exa
from whiskeyjack_bot.research.lens import (
    ASKNEWS_LANGUAGES,
    LensCall,
    LensError,
    locale_rows,
    plan,
    run_lens,
)
from whiskeyjack_bot.research.locales import LocaleRegistry, load_registry
from whiskeyjack_bot.research.model import validate_document
from whiskeyjack_bot.research.store import load_locales, load_packet, load_run

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
HORMUZ = "Will Iran close the Strait of Hormuz before November 1, 2026?"


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    data = yaml.safe_load((REPO / "config.example.yaml").read_text("utf-8"))
    data["model"]["name"] = "openrouter/test-model"
    data["storage"]["sqlite_path"] = str(tmp_path / "ledger.sqlite3")
    data["storage"]["artifact_root"] = str(tmp_path / "artifacts")
    data["storage"]["export_root"] = str(tmp_path / "exports")
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    data["forecast"]["prompt_path"] = str(REPO / "prompts" / "forecaster.md")
    data["retrieval"]["primary"]["retries"] = 0
    data["retrieval"]["fallback"]["retries"] = 0
    return validate_config_data(data)


@pytest.fixture()
def ledger(config: AppConfig) -> Any:
    config.storage.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    try:
        yield conn
    finally:
        conn.close()


def _registry() -> LocaleRegistry:
    return load_registry(REPO / "config" / "locales.yaml")


def _question(title: str = HORMUZ, criteria: str | None = None) -> CanonicalBinaryQuestion:
    return CanonicalBinaryQuestion(
        question_id=4242, post_id=1, title=title, resolution_criteria=criteria
    )


# --- plan --------------------------------------------------------------------------------


def test_a_hormuz_question_plans_exa_for_iran_and_asknews_for_the_arab_littoral() -> None:
    calls = plan(_question(), _registry(), max_calls=4)
    by_locale = {call.locale_code: call for call in calls}
    assert [call.locale_code for call in calls] == ["IR", "AE", "OM"]
    iran = by_locale["IR"]
    # Persian is not an AskNews language (probe: HTTP 422), so Iran goes to Exa with the
    # registry's native terms for what the question named -- not a translation of it.
    assert iran.provider == "exa" and iran.query_source == "native_terms"
    # "Hormuz" is also a trigger, but its native term is a word of "Strait of Hormuz"'s.
    assert iran.query == "ایران تنگه هرمز"
    assert "mehrnews.com" in iran.domains and all("/" not in d for d in iran.domains)
    assert by_locale["OM"].provider == "asknews" and by_locale["OM"].query_source == "question"
    assert "timesofoman.com" in by_locale["OM"].domains


def test_the_cap_keeps_the_first_locales_in_question_order() -> None:
    assert [call.locale_code for call in plan(_question(), _registry(), max_calls=2)] == [
        "IR",
        "AE",
    ]


def test_a_question_about_no_registry_place_plans_nothing() -> None:
    assert plan(_question("Will US CPI exceed 3% in October?"), _registry(), max_calls=4) == ()


def test_a_locale_without_local_domains_plans_nothing(tmp_path: Path) -> None:
    path = tmp_path / "locales.yaml"
    path.write_text(
        "schema_version: 1\nlocales:\n"
        "  - {code: IR, triggers: [Iran], languages: [fa], cctlds: [ir],"
        " external_domains: [iranintl.com]}\n",
        encoding="utf-8",
    )
    assert plan(_question(), load_registry(path), max_calls=4) == ()


def test_an_exa_locale_without_native_terms_queries_the_title(tmp_path: Path) -> None:
    path = tmp_path / "locales.yaml"
    path.write_text(
        "schema_version: 1\nlocales:\n"
        "  - {code: IR, triggers: [Iran], languages: [fa], local_domains: [irna.ir]}\n",
        encoding="utf-8",
    )
    (call,) = plan(_question(), load_registry(path), max_calls=4)
    assert (call.provider, call.query_source, call.query) == ("exa", "question_title", HORMUZ)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_plan_refuses_a_bad_cap(bad: object) -> None:
    with pytest.raises(LensError):
        plan(_question(), _registry(), max_calls=bad)  # type: ignore[arg-type]


def test_persian_is_not_an_asknews_language_and_arabic_is() -> None:
    assert "fa" not in ASKNEWS_LANGUAGES and "ar" in ASKNEWS_LANGUAGES


# --- locale rows -----------------------------------------------------------------------


def _document(url: str, title: str | None, run: str = "run-lens") -> Any:
    return validate_document(
        {
            "retrieval_run_id": run,
            "original_url": url,
            "canonical_url": url,
            "title": title,
            "retrieved_at_utc": NOW,
            "source_type": "news",
            "provenance": "direct_api",
            "content_sha256": url.encode().hex().ljust(64, "0")[:64],
        }
    )


OMAN = LensCall("OM", "asknews", "q", "question", ("timesofoman.com",))


def test_asknews_rows_carry_provider_facts_verbatim_and_classify_the_publisher() -> None:
    entry = _registry().get("OM")
    assert entry is not None
    local = _document("https://timesofoman.com/a", "Oman reopens port")
    other = _document("https://www.almasryalyoum.com/b", "Egypt view")
    raw = [
        {
            "as_dicts": [
                {
                    "article_url": "https://timesofoman.com/a",
                    "language": "en-US",
                    "country": "OM",
                    "title": "Oman reopens Musandam port",
                    "eng_title": "Oman reopens port",
                    "crawl_date": "2026-10-15T09:00:00Z",
                    "reporting_voice": "Objective",
                    "provocative": "low",
                },
                {
                    "article_url": "https://www.almasryalyoum.com/b",
                    "language": "ar",
                    "country": "IR",  # the STORY's country, never the publisher's
                    "title": "هل تراجع الاهتمام بهرمز؟",
                    "eng_title": None,
                    "crawl_date": "not a date",
                    "reporting_voice": 7,
                },
            ]
        }
    ]
    first, second = locale_rows(OMAN, entry, [local, other], raw)
    assert (first.publisher_origin, first.language, first.translation_source) == (
        "local",
        "en-US",
        "provider:asknews",
    )
    assert first.provider_first_seen_at_utc == datetime(2026, 10, 15, 9, tzinfo=timezone.utc)
    assert first.provider_labels == {"reporting_voice": "Objective", "provocative": "low"}
    # Not local to Oman, whatever AskNews tagged the story; no translation; bad values dropped.
    assert (second.publisher_origin, second.provider_story_country) == ("unclassified", "IR")
    assert (second.translation_source, second.english_title) == ("none", None)
    assert second.provider_first_seen_at_utc is None and second.provider_labels is None


def test_a_value_the_ledger_could_not_read_back_is_dropped_not_kept() -> None:
    entry = _registry().get("OM")
    assert entry is not None
    lone = chr(0xD800)
    document = _document("https://timesofoman.com/a", "x")
    raw = [{"as_dicts": [{"article_url": "https://timesofoman.com/a", "title": "a" + lone}]}]
    (row,) = locale_rows(OMAN, entry, [document], raw)
    assert row.native_title is None


def test_exa_rows_keep_the_page_title_as_native_and_claim_no_translation() -> None:
    entry = _registry().get("IR")
    assert entry is not None
    call = LensCall("IR", "exa", "ایران", "native_terms", ("mehrnews.com",))
    documents = [
        _document("https://www.mehrnews.com/news/1", "تنگه هرمز باز است یا بسته؟"),
        _document("https://www.bbc.com/persian/articles/x", "BBC Persian"),
    ]
    mehr, bbc = locale_rows(call, entry, documents, [{"results": []}])
    assert (mehr.publisher_origin, mehr.native_title, mehr.english_title) == (
        "local",
        "تنگه هرمز باز است یا بسته؟",
        None,
    )
    assert mehr.language is None  # Exa reports none, and the query language is not the page's
    assert bbc.publisher_origin == "external"


# --- run_lens ---------------------------------------------------------------------------


class _Exa:
    """An Exa transport that records each request body and answers with local results."""

    def __init__(self, status: int = 200) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        return httpx.Response(
            200,
            json={
                "requestId": "r1",
                "results": [
                    {
                        "title": "تنگه هرمز باز است یا بسته؟",
                        "url": "https://www.mehrnews.com/news/6974299",
                        "publishedDate": (NOW - timedelta(days=5)).isoformat(),
                        "text": "متن خبر",
                    }
                ],
                "costDollars": {"total": 0.007},
            },
        )


def _exa_client(transport: _Exa) -> httpx.Client:
    return httpx.Client(base_url="https://api.exa.ai", transport=httpx.MockTransport(transport))


def _news() -> Any:
    return _SDK(
        [
            _article(
                "https://timesofoman.com/a",
                eng_title="Oman says Strait of Hormuz shipping continues",
                title="Oman says Strait of Hormuz shipping continues",
                pub_date=NOW - timedelta(hours=3),
                crawl_date=NOW - timedelta(hours=2),
            )
        ]
    )


def test_run_lens_buys_each_planned_call_and_records_runs_and_locale_rows(
    config: AppConfig, ledger: sqlite3.Connection
) -> None:
    config = config.model_copy(
        update={
            "retrieval": config.retrieval.model_copy(
                update={
                    "local_lens": config.retrieval.local_lens.model_copy(
                        update={"enabled": True, "max_calls_per_question": 2}
                    )
                }
            )
        }
    )
    news, transport = _news(), _Exa()
    outcome = run_lens(
        ledger,
        config,
        question=_question(criteria="per Oman's maritime authority"),
        now=NOW,
        registry=_registry(),
        baseline_run_ids=["run-baseline"],
        news_client=news,
        web_client=_exa_client(transport),
    )
    assert [(run.locale_code, run.provider) for run in outcome.runs] == [
        ("IR", "exa"),
        ("AE", "asknews"),
    ]
    # Exa: the native query and the locale's publishers.
    (body,) = transport.bodies
    assert body["query"] == "ایران تنگه هرمز"
    assert "mehrnews.com" in body["includeDomains"]
    # AskNews: the publishers, and no language filter.
    (request,) = news.news.calls
    assert "wam.ae" in request["domain_url"] and "languages" not in request
    iran_run = load_run(ledger, outcome.runs[0].retrieval_run_id or "")
    assert iran_run.provider_config is not None
    assert iran_run.provider_config["fallback_reasons"] == ["local_source_lens"]
    assert iran_run.provider_config["lens"] == {
        "locale": "IR",
        "registry_sha256": _registry().sha256,
        "query_source": "native_terms",
        "baseline_run_ids": ["run-baseline"],
    }
    (row,) = load_locales(ledger, outcome.runs[0].retrieval_run_id or "")
    assert (row.locale_code, row.publisher_origin) == ("IR", "local")
    packet = load_packet(
        ledger, question_id=4242, retrieval_run_ids=list(outcome.retrieval_run_ids)
    )
    assert packet.locales  # the lens packet hashes under 1.1.0


def test_a_planned_call_without_a_client_is_recorded_as_skipped(
    config: AppConfig, ledger: sqlite3.Connection
) -> None:
    outcome = run_lens(
        ledger,
        config,
        question=_question(),
        now=NOW,
        registry=_registry(),
        baseline_run_ids=[],
        news_client=_news(),
        web_client=None,
    )
    assert outcome.runs[0].skipped == "no_client" and outcome.runs[0].retrieval_run_id is None


def test_a_provider_failure_is_recorded_on_its_run_not_raised(
    config: AppConfig, ledger: sqlite3.Connection
) -> None:
    outcome = run_lens(
        ledger,
        config,
        question=_question(),
        now=NOW,
        registry=_registry(),
        baseline_run_ids=[],
        news_client=_SDK(raises=RuntimeError("boom")),
        web_client=_exa_client(_Exa(status=500)),
    )
    assert all(run.provider_failed for run in outcome.runs)
    for run in outcome.runs:
        stored = load_run(ledger, run.retrieval_run_id or "")
        assert stored.completed_at_utc is not None and stored.error_summary


# --- the baseline path is untouched -------------------------------------------------------


def test_a_baseline_asknews_call_sends_and_records_what_it_did_before_the_lens(
    config: AppConfig,
) -> None:
    news = _news()
    result = asknews.retrieve_news(
        news, config, question_id=1, queries=["q"], retrieval_run_id="run-1", now=NOW
    )
    (request,) = news.news.calls
    assert set(request) == {
        "query",
        "strategy",
        "n_articles",
        "return_type",
        "hours_back",
        "historical",
    }
    assert result.run.provider_config is not None
    assert set(result.run.provider_config) == {
        "strategies",
        "n_articles",
        "hours_back",
        "return_type",
    }


def test_a_baseline_exa_call_records_no_lens(config: AppConfig) -> None:
    transport = _Exa()
    result = exa.retrieve_web(
        _exa_client(transport),
        config,
        question_id=1,
        queries=["q"],
        retrieval_run_id="run-1",
        now=NOW,
        fallback_reasons=["primary_returned_no_documents", "official_source_required"],
    )
    assert result.run.provider_config is not None
    assert "lens" not in result.run.provider_config


def test_decide_fallback_never_emits_the_lens_reason() -> None:
    for failed in (True, False):
        for documents in (0, 3):
            for official in (True, False):
                decision = exa.decide_fallback(
                    primary_failed=failed,
                    primary_documents=documents,
                    official_source_required=official,
                )
                assert "local_source_lens" not in decision.reasons
