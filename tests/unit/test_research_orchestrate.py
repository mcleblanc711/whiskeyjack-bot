"""M1-315: one question's paid retrieval, sequenced and recorded (research/orchestrate.py).

The module this exercises is the one M1-306 deferred -- "the cross-provider orchestration
... is a follow-up row, not this branch" -- so nothing here has a predecessor suite to
extend. What it does have is a boundary worth being exact about: **every call into
``retrieve_for_question`` spends money**, so the tests are organized by which side of the
first billable call each behaviour sits on.

Before the spend, a caller mistake is refused and ``client.news.calls == []`` proves it.
After the spend, nothing refuses: a provider that fails, an artifact that cannot be written
and a fallback whose credential is missing are each recorded and reported, and the assertion
is that the run row survived.

The suite runs under three independent network guards (pytest-socket, the DNS refusal in
``tests/conftest.py``, the ``socket.connect`` refusal in ``tests/unit/conftest.py``), so
"no network was reached" is enforced rather than asserted.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from asknews_sdk.dto.base import Author, Entities
from asknews_sdk.dto.news import SearchResponse, SearchResponseDictItem
from pydantic import AnyUrl

from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.metaculus.snapshots import load_snapshot
from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion, CanonicalQuestion
from whiskeyjack_bot.questions.normalize import normalize_questions
from whiskeyjack_bot.research.model import validate_run
from whiskeyjack_bot.research.orchestrate import (
    OrchestrationError,
    ProviderRun,
    RetrievalOutcome,
    derive_queries,
    retrieve_for_question,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO_ROOT / "tests" / "fixtures" / "snapshots" / "minibench_sample_snapshot.json"
QUESTION_ID = 91001
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
# Low-entropy on purpose: CI scans every branch with gitleaks, so a realistic secret shape
# here fails unrelated PRs (docs/LESSONS.md).
SECRET = "privateFAKE123456"


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    data = copy.deepcopy(yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text("utf-8")))
    data["model"]["name"] = "openrouter/test-model"
    data["storage"]["sqlite_path"] = str(tmp_path / "ledger.sqlite3")
    data["storage"]["artifact_root"] = str(tmp_path / "artifacts")
    data["storage"]["export_root"] = str(tmp_path / "exports")
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    data["forecast"]["prompt_path"] = str(REPO_ROOT / "prompts" / "forecaster.md")
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


def question(question_id: int = QUESTION_ID) -> CanonicalQuestion:
    _, loaded = load_snapshot(SNAPSHOT)
    result = normalize_questions(loaded)
    return next(q for q in result.questions if q.question_id == question_id)


def _article(url: str = "https://example.org/payrolls", **overrides: Any) -> SearchResponseDictItem:
    data: dict[str, Any] = {
        "article_url": AnyUrl(url),
        "article_id": uuid.UUID(int=abs(hash(url)) % (2**120)),
        "classification": ["Business"],
        "country": "US",
        "source_id": "Example Wire",
        "page_rank": 3,
        "domain_url": "example.org",
        "eng_title": "Payrolls beat expectations",
        "entities": Entities(),
        "keywords": ["payrolls"],
        "language": "en",
        "pub_date": datetime(2026, 8, 29, 9, 30, tzinfo=timezone.utc),
        "summary": "Nonfarm payrolls rose more than forecast.",
        "title": "Payrolls beat expectations",
        "sentiment": 0,
        "as_string_key": "k1",
        "crawl_date": datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc),
        "full_text": "Nonfarm payrolls rose by 250,000.",
        "authors": [Author(name="A. Reporter", email=None, url=None)],
    }
    data.update(overrides)
    return SearchResponseDictItem.model_construct(**data)


class _NewsAPI:
    """A stand-in for ``AskNewsSDK.news`` that can also look at the ledger mid-call."""

    def __init__(
        self,
        articles: list[SearchResponseDictItem] | None = None,
        *,
        raises: BaseException | None = None,
        observer: Any = None,
    ) -> None:
        self._articles = articles if articles is not None else [_article()]
        self._raises = raises
        self._observer = observer
        self.calls: list[dict[str, Any]] = []

    def search_news(self, **kwargs: Any) -> SearchResponse:
        self.calls.append(kwargs)
        if self._observer is not None:
            self._observer(len(self.calls))
        if self._raises is not None:
            raise self._raises
        return SearchResponse.model_construct(as_dicts=self._articles)


class _SDK:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.news = _NewsAPI(*args, **kwargs)


def _exa_body(url: str = "https://official.example/report") -> dict[str, Any]:
    return {
        "requestId": "abc123",
        "results": [
            {
                "title": "Official report",
                "url": url,
                "publishedDate": "2026-08-29",
                "text": "The agency published its figures.",
            }
        ],
        "costDollars": {"total": 0.01},
    }


class _Exchange:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self._response = response
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        return (
            self._response if self._response is not None else httpx.Response(200, json=_exa_body())
        )


def _web_client(exchange: _Exchange) -> httpx.Client:
    return httpx.Client(base_url="https://api.exa.ai", transport=httpx.MockTransport(exchange))


def _rows(conn: sqlite3.Connection, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in conn.execute(sql, params).fetchall()]


# --- derive_queries: pure, and refuses before anything can be paid for ----------------


def test_a_plain_question_yields_its_title() -> None:
    assert derive_queries(question()) == (question().title,)


def test_a_group_sibling_leads_with_the_parent_title() -> None:
    """M1-202's unpacking leaves sibling titles whose meaning lives in the parent.

    "Democratic" is not a search query. The combined form goes first because it is the more
    complete question; the bare title stays because it is the more precise search term.
    """
    sibling = question().model_copy(update={"group_parent_title": "Who wins the 2028 election?"})
    assert derive_queries(sibling) == (
        f"Who wins the 2028 election? {sibling.title}",
        sibling.title,
    )


def test_a_parent_title_equal_to_the_title_is_not_emitted_twice() -> None:
    same = question().model_copy(update={"group_parent_title": None})
    assert len(derive_queries(same)) == len(set(derive_queries(same)))


@pytest.mark.parametrize("value", ["not a question", None, 42, object()])
def test_a_non_question_is_refused_as_this_modules_error(value: object) -> None:
    with pytest.raises(OrchestrationError, match="canonical question"):
        derive_queries(value)  # type: ignore[arg-type]


def test_a_whitespace_only_title_is_refused() -> None:
    """``min_length=1`` accepts a lone space, and a lone space is not a query."""
    blank = question().model_copy(update={"title": "   "})
    with pytest.raises(OrchestrationError, match="no title to search on"):
        derive_queries(blank)


def test_a_title_carrying_a_lone_surrogate_is_refused_before_any_call() -> None:
    """The totality half of the contract, and **not** a reachable condition.

    The obvious story is that a lone surrogate reaches a title from a snapshot file, since
    ``"\\ud800"`` is valid JSON and decodes to one. That story is wrong and the test below
    says so: pydantic's ``str`` refuses a lone surrogate outright, so a *validated*
    ``CanonicalQuestion`` cannot carry one. This constructs the state a validator would have
    refused, which is why it uses ``model_copy`` -- and it is asserting the public promise
    that only ``OrchestrationError`` escapes, not claiming the input is one a snapshot could
    produce. See ``_require_storable``'s docstring.
    """
    broken = question().model_copy(update={"title": "payrolls \ud800 report"})
    with pytest.raises(OrchestrationError) as caught:
        derive_queries(broken)
    assert "\ud800" not in str(caught.value)
    assert "payrolls" not in str(caught.value)


def test_the_surrogate_guard_is_not_vacuous() -> None:
    """The condition it names really does break the binding layer it protects."""
    with pytest.raises(UnicodeEncodeError):
        sqlite3.connect(":memory:").execute("SELECT ?", ("payrolls \ud800 report",))


def test_the_validated_model_already_refuses_a_lone_surrogate_title() -> None:
    """Why the guard above is a backstop rather than a defence, pinned so it stays true.

    If a future pydantic or a future field type ever accepts one, this fails and the
    reachability question reopens -- which is the only thing that would make the guard
    load-bearing.
    """
    with pytest.raises(Exception) as caught:
        CanonicalBinaryQuestion(question_id=1, post_id=1, title="payrolls \ud800 report")
    assert "string_unicode" in str(caught.value)


# --- before the spend: refusals, with zero provider calls ------------------------------


def test_a_config_shaped_object_that_is_not_one_is_refused(ledger: Any) -> None:
    """M1-316's finding, closed in this module rather than copied into it.

    ``pipeline.py``'s guards read an attribute and check the *value's* type while printing
    "config must be an AppConfig", so a ``SimpleNamespace`` satisfies them. This one checks
    the object.
    """
    from types import SimpleNamespace

    stand_in = SimpleNamespace(
        retrieval=SimpleNamespace(primary=SimpleNamespace(provider="asknews"))
    )
    with pytest.raises(OrchestrationError, match="config must be an AppConfig"):
        retrieve_for_question(ledger, stand_in, question=question(), now=NOW)  # type: ignore[arg-type]


def test_a_primary_provider_that_is_not_asknews_is_refused_with_no_calls(
    config: AppConfig, ledger: Any
) -> None:
    """Exa is structurally the fallback: it refuses to run without a reason it was reached."""
    config.retrieval.primary.provider = "exa"
    sdk = _SDK()
    with pytest.raises(OrchestrationError, match="only 'asknews' is supported"):
        retrieve_for_question(ledger, config, question=question(), now=NOW, news_client=sdk)
    assert sdk.news.calls == []


def test_a_naive_now_is_refused_before_any_call(config: AppConfig, ledger: Any) -> None:
    sdk = _SDK()
    with pytest.raises(OrchestrationError):
        retrieve_for_question(
            ledger, config, question=question(), now=datetime(2026, 8, 30), news_client=sdk
        )
    assert sdk.news.calls == []
    assert _rows(ledger, "SELECT retrieval_run_id FROM research_runs") == []


def test_a_missing_credential_is_refused_before_any_call(
    config: AppConfig, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(config.retrieval.primary.api_key_env, raising=False)
    with pytest.raises(OrchestrationError, match=config.retrieval.primary.api_key_env):
        retrieve_for_question(ledger, config, question=question(), now=NOW)
    assert _rows(ledger, "SELECT retrieval_run_id FROM research_runs") == []


# --- the ordering claim, proved from inside the call -----------------------------------


def test_the_run_row_is_open_in_the_ledger_before_the_first_billable_call(
    config: AppConfig, ledger: Any
) -> None:
    """The two-phase shape's whole purpose, asserted at the only moment it is observable.

    ``store.open_run`` exists so that a process dying mid-call still leaves the ledger
    saying money was spent on this question. Asserting it *after* the call would prove
    nothing -- the row is completed by then. So the fake provider reads the ledger from
    inside ``search_news``, on its own connection, and records what it saw.
    """
    seen: list[tuple[int, Any]] = []

    def observe(call_number: int) -> None:
        with sqlite3.connect(config.storage.sqlite_path) as probe:
            rows = probe.execute(
                "SELECT retrieval_run_id, completed_at_utc FROM research_runs"
            ).fetchall()
        seen.append((call_number, [tuple(row) for row in rows]))

    sdk = _SDK(observer=observe)
    retrieve_for_question(ledger, config, question=question(), now=NOW, news_client=sdk)

    assert seen, "the fake provider was never called; the test asserts nothing"
    first_call, rows_at_first_call = seen[0]
    assert first_call == 1
    assert len(rows_at_first_call) == 1, rows_at_first_call
    assert rows_at_first_call[0][1] is None, "the row must still be open during the call"


def test_a_provider_that_raises_still_leaves_the_run_recorded(
    config: AppConfig, ledger: Any
) -> None:
    """The spend is on the books even though every call failed. Nothing raises."""
    sdk = _SDK(raises=RuntimeError(f"upstream said no; auth header Bearer {SECRET}"))
    outcome = retrieve_for_question(
        ledger, config, question=question(), now=NOW, news_client=sdk, web_client=None
    )
    assert outcome.packet is None
    assert outcome.document_count == 0
    assert outcome.runs[0].provider_failed is True
    completed = _rows(ledger, "SELECT completed_at_utc, error_summary FROM research_runs")
    assert completed and completed[0][0] is not None
    assert SECRET not in json.dumps(completed)


# --- the fallback: authorized only by decide_fallback ----------------------------------


def test_the_fallback_runs_when_the_primary_failed_and_records_why(
    config: AppConfig, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(config.retrieval.fallback.api_key_env, "fakeEXAkey1234567")
    exchange = _Exchange()
    outcome = retrieve_for_question(
        ledger,
        config,
        question=question(),
        now=NOW,
        news_client=_SDK(raises=RuntimeError("nope")),
        web_client=_web_client(exchange),
    )
    assert [run.provider for run in outcome.runs] == ["asknews", "exa"]
    assert "primary_provider_failed" in outcome.runs[1].fallback_reasons
    stored = _rows(
        ledger, "SELECT provider, provider_config_json FROM research_runs ORDER BY provider"
    )
    exa_config = json.loads([row[1] for row in stored if row[0] == "exa"][0])
    assert "primary_provider_failed" in exa_config["fallback_reasons"]
    assert outcome.document_count == 1


def test_a_primary_that_succeeded_with_nothing_does_not_authorize_a_paid_fallback(
    config: AppConfig, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D18, enforced by ``decide_fallback`` and not re-decided here.

    A provider that answered with zero documents has not *failed*. This module adds no
    trigger of its own, so the Exa transport must never be touched.
    """
    monkeypatch.setenv(config.retrieval.fallback.api_key_env, "fakeEXAkey1234567")
    exchange = _Exchange()
    outcome = retrieve_for_question(
        ledger,
        config,
        question=question(),
        now=NOW,
        news_client=_SDK(articles=[]),
        web_client=_web_client(exchange),
    )
    assert exchange.requests == []
    assert [run.provider for run in outcome.runs] == ["asknews"]
    assert outcome.packet is None


def test_an_unavailable_fallback_keeps_the_primary_run_instead_of_raising(
    config: AppConfig, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary is already billed. Losing its row to a missing optional key is the
    outcome M1-312's rule exists to prevent, one layer up."""
    monkeypatch.delenv(config.retrieval.fallback.api_key_env, raising=False)
    outcome = retrieve_for_question(
        ledger,
        config,
        question=question(),
        now=NOW,
        news_client=_SDK(raises=RuntimeError("nope")),
    )
    assert [run.provider for run in outcome.runs] == ["asknews"]
    assert len(_rows(ledger, "SELECT 1 FROM research_runs")) == 1


# --- after the spend: the artifact may be lost, the run may not ------------------------


def test_an_artifact_that_cannot_be_written_still_records_the_run(
    config: AppConfig, ledger: Any
) -> None:
    """M1-312's rule reached through this module: the destination is a file, so the
    directory the artifact needs cannot be created."""
    (config.storage.artifact_root).parent.mkdir(parents=True, exist_ok=True)
    config.storage.artifact_root.write_text("not a directory", encoding="utf-8")
    outcome = retrieve_for_question(
        ledger, config, question=question(), now=NOW, news_client=_SDK()
    )
    assert outcome.runs[0].artifact_outcome == "failed"
    assert outcome.runs[0].artifact_error
    stored = _rows(ledger, "SELECT raw_response_path FROM research_runs")
    assert stored == [(None,)]
    assert outcome.document_count == 1
    assert outcome.packet is not None


def test_retention_switched_off_is_reported_as_itself_not_as_a_failure(
    config: AppConfig, ledger: Any
) -> None:
    config.storage.retain_raw_research = False
    outcome = retrieve_for_question(
        ledger, config, question=question(), now=NOW, news_client=_SDK()
    )
    assert outcome.runs[0].artifact_outcome == "retention_disabled"
    assert outcome.runs[0].artifact_error is None


# --- the clock, and what actually protects it ------------------------------------------


def test_a_now_spelled_in_another_zone_still_completes_the_row_it_opened(
    config: AppConfig, ledger: Any
) -> None:
    """``complete_run`` matches an opened row on ``(question_id, provider, started_at_utc)``.

    This passes for a reason the module's first docstring got wrong, so the test below pins
    the real one rather than letting this stand as evidence for a claim it cannot support.
    """
    elsewhere = NOW.astimezone(timezone(timedelta(hours=9, minutes=30)))
    assert elsewhere.utcoffset() != timedelta(0)
    outcome = retrieve_for_question(
        ledger, config, question=question(), now=elsewhere, news_client=_SDK()
    )
    stored = _rows(ledger, "SELECT completed_at_utc, started_at_utc FROM research_runs")
    assert len(stored) == 1
    assert stored[0][0] is not None, "the opened row was never completed"
    assert outcome.packet is not None


def test_the_run_model_is_what_makes_the_two_timestamps_agree() -> None:
    """The dependency this module rests on, pinned in the module that would break it.

    The tempting story is that the orchestrator must normalize ``now`` exactly once, because
    normalizing it here and again in the adapter would store two different texts and the
    completion would match no row -- after the calls were billed. **It is false.**
    ``ResearchRun.started_at_utc`` is ``UtcDatetime`` (``AwareDatetime`` +
    ``AfterValidator(_to_utc)``), so every spelling of one instant validates to the same UTC
    value and the match holds however many conversions happened on the way.

    A mutation handing the adapter the caller's raw ``now`` survived the whole suite, which
    is what exposed the claim. So the honest guard is this one: if ``UtcDatetime`` ever stops
    normalizing, the ordering question reopens and this fails first.
    """
    aware = NOW.astimezone(timezone(timedelta(hours=-5)))
    run = validate_run(
        {
            "retrieval_run_id": "run-1",
            "question_id": 1,
            "provider": "asknews",
            "started_at_utc": aware,
        }
    )
    assert run.started_at_utc == NOW
    assert run.started_at_utc.utcoffset() == timedelta(0)
    # And the two spellings really are different objects, or the assertion above is trivial.
    assert aware.utcoffset() != timedelta(0)
    assert aware.isoformat() != NOW.isoformat()


# --- the result object cannot misreport what happened ----------------------------------


def test_an_outcome_naming_a_run_it_did_not_perform_is_refused() -> None:
    with pytest.raises(OrchestrationError, match="named in retrieval_run_ids"):
        RetrievalOutcome(
            question_id=1,
            packet=None,
            retrieval_run_ids=("run-a",),
            runs=(),
            document_count=0,
            cost_usd=None,
            unpriced_runs=0,
        )


def test_a_packet_without_documents_is_refused() -> None:
    with pytest.raises(OrchestrationError, match="exactly when a document was retained"):
        RetrievalOutcome(
            question_id=1,
            packet=None,
            retrieval_run_ids=(),
            runs=(),
            document_count=3,
            cost_usd=None,
            unpriced_runs=0,
        )


def test_a_provider_run_claiming_a_failure_without_naming_it_is_refused() -> None:
    with pytest.raises(OrchestrationError, match="reports its error"):
        ProviderRun(
            retrieval_run_id="run-a",
            provider="asknews",
            documents_retained=0,
            provider_failed=True,
            artifact_outcome="failed",
            artifact_error=None,
            cost_usd=None,
            fallback_reasons=(),
        )
