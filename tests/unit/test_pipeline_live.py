"""M1-315: the live paid batch -- one record per question, and one failure per failure.

The criterion this suite is written against: *"One command forecasts one or more snapshot
questions through live retrieval and a live model call, writing one validated record per
question; a per-question failure records its pipeline event and does not abort the batch;
retrying a later phase repeats no paid call unless asked."*

Three things are asserted by **counting provider calls**, not by reading a log line: that a
refusal happened before any spend, that a reused research phase costs nothing, and that
``--refresh-research`` really does pay again. A count is falsifiable in a way a message is
not.

Both fake clients are recording doubles, and the suite runs under three independent network
guards, so a fake that was silently not used would fail the run rather than pass it quietly.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from asknews_sdk.dto.base import Author, Entities
from asknews_sdk.dto.news import SearchResponse, SearchResponseDictItem
from pydantic import AnyUrl

from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.metaculus.snapshots import load_snapshot
from whiskeyjack_bot.pipeline_live import (
    BatchRun,
    LiveRunError,
    QuestionOutcome,
    run_live,
)
from whiskeyjack_bot.questions.model import CanonicalQuestion
from whiskeyjack_bot.questions.normalize import normalize_questions

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO_ROOT / "tests" / "fixtures" / "snapshots" / "minibench_sample_snapshot.json"
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")
BINARY, MULTIPLE_CHOICE, NUMERIC = 91001, 91003, 91002
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
MODEL_NAME = "openrouter/test-model"


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    data = copy.deepcopy(yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text("utf-8")))
    data["model"]["name"] = MODEL_NAME
    data["storage"]["sqlite_path"] = str(tmp_path / "ledger.sqlite3")
    data["storage"]["artifact_root"] = str(tmp_path / "artifacts")
    data["storage"]["export_root"] = str(tmp_path / "exports")
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    data["forecast"]["prompt_path"] = str(REPO_ROOT / "prompts" / "forecaster.md")
    # The committed default is 1, which is the safe value and the wrong one for a suite
    # about batching. Raised deliberately, which is exactly what an operator must do.
    data["run_limits"]["max_questions"] = 3
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


def questions() -> dict[int, CanonicalQuestion]:
    _, loaded = load_snapshot(SNAPSHOT)
    return {q.question_id: q for q in normalize_questions(loaded).questions}


# --- replies, built from the prompt the model is actually shown ------------------------


def _block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def reply_for(question: CanonicalQuestion) -> str:
    """A valid reply for one question, assembled from ``prompts/forecaster.md``.

    The shared fields come from the prompt's own example -- the ``scenario.py`` trick, so a
    prompt edit that invalidates the example surfaces here rather than in a fixture that
    quietly disagrees with the file the model is shown. The ``final_prediction`` is derived
    from *the question* instead, because the prompt's example predicts a question that does
    not exist: its options and its numeric range are its own.

    This is also T-903's "no numeric or multiple-choice acceptance scenario" deferral paid
    off -- a batch that only ever forecasts binary questions would not exercise the two
    conversion paths M1-404 and M1-405 added.
    """
    payload: dict[str, Any] = json.loads(_block("Shared fields"))
    payload["question_id"] = question.question_id
    payload["question_type"] = question.qtype
    if question.qtype != "binary":
        # The prompt's shared example is written for a binary question, and
        # `forecast/schema.py` requires both scalar priors to be null for the other two --
        # a single probability is not a prior over a set of options or a range.
        payload["base_rate"]["prior_probability"] = None
        payload["model_prior"] = None
    if question.qtype == "binary":
        payload["final_prediction"] = {"probability_yes": 0.35}
    elif question.qtype == "multiple_choice":
        options = list(question.options)
        share = round(1.0 / len(options), 6)
        entries = [{"option": name, "probability": share} for name in options]
        entries[-1]["probability"] = round(1.0 - share * (len(options) - 1), 6)
        payload["final_prediction"] = {"options": entries}
    else:
        low, high = question.lower_bound, question.upper_bound
        levels = (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)
        payload["final_prediction"] = {
            "percentiles": [
                {"percentile": level, "value": round(low + (high - low) * level, 4)}
                for level in levels
            ]
        }
    return json.dumps(payload)


class _Forecaster:
    """A recording stand-in for ``GeneralLlm``, answering whichever question it is shown.

    ``model`` is part of the protocol because ``generate_forecast`` checks it against config,
    so a double that skipped it would be refused -- which is the point: a client carries no
    memory of which config built it.
    """

    def __init__(
        self, *, replies: dict[int, str] | None = None, raises: BaseException | None = None
    ) -> None:
        self.model = MODEL_NAME
        self._replies = replies or {q: reply_for(v) for q, v in questions().items()}
        self._raises = raises
        self.calls: list[int] = []

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        # The **first** user message, not the last. `generate_forecast`'s repair turn
        # appends the model's own reply and a plain-text instruction, so reading the last
        # one parses the repair prompt as JSON and raises -- which the adapter reports as
        # `provider_error`, and the failure then looks like a product defect rather than a
        # broken double. It did, once, before this comment existed.
        request = json.loads(next(m for m in prompt if m["role"] == "user")["content"])
        question_id = int(request["question_id"])
        self.calls.append(question_id)
        if self._raises is not None:
            raise self._raises
        return self._replies.get(question_id, "not json at all")


# --- retrieval doubles ------------------------------------------------------------------


def _article(url: str) -> SearchResponseDictItem:
    return SearchResponseDictItem.model_construct(
        article_url=AnyUrl(url),
        article_id=uuid.uuid5(uuid.NAMESPACE_URL, url),
        classification=["Business"],
        country="US",
        source_id="Example Wire",
        page_rank=3,
        domain_url="example.org",
        eng_title="Example headline",
        entities=Entities(),
        keywords=["example"],
        language="en",
        pub_date=datetime(2026, 8, 29, 9, 30, tzinfo=timezone.utc),
        summary="An example summary.",
        title="Example headline",
        sentiment=0,
        as_string_key="k1",
        crawl_date=datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc),
        full_text="Example body text.",
        authors=[Author(name="A. Reporter", email=None, url=None)],
    )


class _NewsAPI:
    def __init__(
        self, *, fail_for: set[int] | None = None, empty_for: set[int] | None = None
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_for = fail_for or set()
        self._empty_for = empty_for or set()
        self.current_question: int | None = None

    def search_news(self, **kwargs: Any) -> SearchResponse:
        self.calls.append(kwargs)
        if self.current_question in self._fail_for:
            raise RuntimeError("upstream said no")
        if self.current_question in self._empty_for:
            return SearchResponse.model_construct(as_dicts=[])
        # Two documents, because the prompt's example cites src-001 and src-002 and the
        # attribution checks resolve every citation against the packet's real source ids.
        return SearchResponse.model_construct(
            as_dicts=[
                _article("https://example.org/first"),
                _article("https://example.org/second"),
            ]
        )


class _SDK:
    """Knows which question is being retrieved, so a per-question failure can be staged."""

    def __init__(self, **kwargs: Any) -> None:
        self.news = _NewsAPI(**kwargs)

    def expect(self, question_id: int) -> None:
        self.news.current_question = question_id


class _TrackingSDK(_SDK):
    """Sets ``current_question`` from the query the adapter actually sends."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        titles = {q.title: qid for qid, q in questions().items()}
        original = self.news.search_news

        def search_news(**call: Any) -> SearchResponse:
            for title, qid in titles.items():
                if title in call.get("query", ""):
                    self.news.current_question = qid
            return original(**call)

        self.news.search_news = search_news  # type: ignore[method-assign]


def live(ledger: Any, config: AppConfig, **overrides: Any) -> BatchRun:
    call: dict[str, Any] = {
        "snapshot": SNAPSHOT,
        "now": NOW,
        "news_client": _TrackingSDK(),
        "web_client": object(),
        "forecaster": _Forecaster(),
    }
    call.update(overrides)
    return run_live(ledger, config, **call)


def rows(conn: sqlite3.Connection, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in conn.execute(sql).fetchall()]


# --- refusals, all before the first billable call ---------------------------------------


def test_a_config_shaped_object_that_is_not_one_is_refused(ledger: Any) -> None:
    """M1-316's finding closed here rather than copied in: the check tests the object."""
    stand_in = SimpleNamespace(
        retrieval=SimpleNamespace(replay_saved_research=False),
        forecast=SimpleNamespace(replay_saved_model_output=False),
        run_limits=SimpleNamespace(max_parallel_questions=1, max_questions=1),
    )
    with pytest.raises(LiveRunError, match="config must be an AppConfig"):
        run_live(ledger, stand_in, snapshot=SNAPSHOT, now=NOW)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("section", "field"),
    [("retrieval", "replay_saved_research"), ("forecast", "replay_saved_model_output")],
)
def test_a_replay_configuration_refuses_the_live_command(
    config: AppConfig, ledger: Any, section: str, field: str
) -> None:
    """A live run under a replay configuration is a contradiction, not a preference.

    The switch says "this run must not spend" and every path below it does. Refusing is also
    what keeps the two commands legible: a config edit cannot silently turn one into the
    other.
    """
    setattr(getattr(config, section), field, True)
    sdk = _TrackingSDK()
    with pytest.raises(LiveRunError, match="use `run-replay`"):
        live(ledger, config, news_client=sdk)
    assert sdk.news.calls == []


def test_configured_parallelism_is_refused_rather_than_quietly_ignored(
    config: AppConfig, ledger: Any
) -> None:
    """A configuration asking for something that silently does not happen is a claim the
    operator has no way to check."""
    config.run_limits.max_parallel_questions = 4
    sdk = _TrackingSDK()
    with pytest.raises(LiveRunError, match="max_parallel_questions must be 1"):
        live(ledger, config, news_client=sdk)
    assert sdk.news.calls == []


def test_limit_may_lower_the_configured_ceiling_but_never_raise_it(
    config: AppConfig, ledger: Any
) -> None:
    config.run_limits.max_questions = 1
    sdk = _TrackingSDK()
    with pytest.raises(LiveRunError, match="exceeds run_limits.max_questions"):
        live(ledger, config, limit=2, news_client=sdk)
    assert sdk.news.calls == []


def test_a_question_id_and_a_limit_together_are_refused(config: AppConfig, ledger: Any) -> None:
    with pytest.raises(LiveRunError, match="not both"):
        live(ledger, config, question_id=BINARY, limit=2)


def test_a_naive_now_is_refused(config: AppConfig, ledger: Any) -> None:
    with pytest.raises(LiveRunError, match="timezone-aware"):
        live(ledger, config, now=datetime(2026, 8, 30))


def test_an_unknown_question_id_is_refused_without_naming_it(
    config: AppConfig, ledger: Any
) -> None:
    with pytest.raises(LiveRunError, match="offending value withheld") as caught:
        live(ledger, config, question_id=424242)
    assert "424242" not in str(caught.value)


def test_a_missing_model_credential_refuses_before_any_retrieval(
    config: AppConfig, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Built once, before the loop, so a missing key fails at question zero rather than
    after two questions' retrieval has already been paid for."""
    monkeypatch.delenv(config.model.api_key_env, raising=False)
    sdk = _TrackingSDK()
    with pytest.raises(LiveRunError, match=config.model.api_key_env):
        live(ledger, config, news_client=sdk, forecaster=None)
    assert sdk.news.calls == []


# --- the criterion: one validated record per question ----------------------------------


def test_a_batch_writes_one_validated_record_for_every_question(
    config: AppConfig, ledger: Any
) -> None:
    """All three supported types in one run, which is also T-903's deferral paid off."""
    batch = live(ledger, config, limit=3)

    assert batch.stop_reason == "completed"
    assert batch.records_written == 3
    assert batch.failures == 0
    assert {outcome.question_id for outcome in batch.outcomes} == {BINARY, MULTIPLE_CHOICE, NUMERIC}
    assert len(rows(ledger, "SELECT record_id FROM forecast_records")) == 3
    assert len(rows(ledger, "SELECT 1 FROM lifecycle_events WHERE event_type = 'validated'")) == 3
    assert rows(ledger, "SELECT 1 FROM pipeline_failure_events") == []
    for outcome in batch.outcomes:
        assert outcome.status == "recorded"
        assert outcome.forecast_sha256 and outcome.record_id
        assert outcome.raw_output_path
        assert outcome.document_count == 2


def test_every_record_is_stamped_with_the_research_it_was_given(
    config: AppConfig, ledger: Any
) -> None:
    batch = live(ledger, config, limit=3)
    stored = {
        row[0]: row[1]
        for row in rows(ledger, "SELECT record_id, retrieval_run_id FROM forecast_records")
    }
    for outcome in batch.outcomes:
        assert outcome.record_id is not None
        assert stored[outcome.record_id] == outcome.retrieval_run_ids[0]


# --- per-question failure isolation ------------------------------------------------------


def test_a_failed_provider_records_research_failed_and_the_batch_continues(
    config: AppConfig, ledger: Any
) -> None:
    """``research_failed``'s first production writer. ``pipeline.py`` writes only the other
    member of the vocabulary, so nothing had ever exercised this branch."""
    sdk = _TrackingSDK(fail_for={MULTIPLE_CHOICE})
    batch = live(ledger, config, limit=3, news_client=sdk)

    assert batch.records_written == 2
    failed = [o for o in batch.outcomes if o.status != "recorded"]
    assert [o.question_id for o in failed] == [MULTIPLE_CHOICE]
    assert failed[0].status == "research_failed"
    assert failed[0].detail_code == "provider_error"
    events = rows(
        ledger, "SELECT event_type, detail_code, question_id FROM pipeline_failure_events"
    )
    assert events == [("research_failed", "provider_error", MULTIPLE_CHOICE)]
    assert len(rows(ledger, "SELECT 1 FROM forecast_records")) == 2


def test_a_provider_that_found_nothing_is_no_evidence_not_provider_error(
    config: AppConfig, ledger: Any
) -> None:
    """A provider that answered with zero documents has not failed, and the ledger must not
    say it did -- the same distinction ``decide_fallback`` refuses to blur."""
    sdk = _TrackingSDK(empty_for={NUMERIC})
    batch = live(ledger, config, limit=3, news_client=sdk)
    failed = [o for o in batch.outcomes if o.status != "recorded"]
    assert [(o.question_id, o.detail_code) for o in failed] == [(NUMERIC, "no_evidence")]
    assert batch.records_written == 2


def test_an_unusable_reply_records_generation_failed_and_keeps_the_text_it_paid_for(
    config: AppConfig, ledger: Any
) -> None:
    """The money bought that text, so it reaches disk before the failure is recorded."""
    replies = {q: reply_for(v) for q, v in questions().items()}
    replies[NUMERIC] = "this is not json"
    batch = live(ledger, config, limit=3, forecaster=_Forecaster(replies=replies))

    failed = [o for o in batch.outcomes if o.status != "recorded"]
    assert [o.question_id for o in failed] == [NUMERIC]
    assert failed[0].status == "generation_failed"
    assert failed[0].detail_code == "malformed_response"
    assert failed[0].artifact_outcome == "written"
    assert failed[0].raw_output_path
    assert (config.storage.artifact_root / failed[0].raw_output_path).is_file()
    events = rows(ledger, "SELECT event_type, detail_code FROM pipeline_failure_events")
    assert events == [("generation_failed", "malformed_response")]
    assert batch.records_written == 2


def test_a_refusal_raised_mid_batch_is_isolated_like_any_other_failure(
    config: AppConfig, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third failure shape, and the one no test reached until a mutation asked.

    ``retrieve_for_question`` raises ``OrchestrationError`` for a caller mistake *before* any
    provider call -- a ledger that refuses ``open_run`` is the reachable instance, since
    ``StoreError`` arrives as one. Every other failure in this suite comes back as a *value*
    on the outcome, so nothing exercised the one path that arrives as an exception, and a
    mutation that let it escape ``_attempt_question`` survived the whole file.

    The monkeypatch simulates a reachable condition rather than inventing one: it stands in
    for the ledger refusal, which needs no hostile operator. What is asserted is that the
    batch survives it, that the failure is recorded as ``research_failed`` with **no**
    ``retrieval_run_id`` -- the shape ``record_pre_forecast_failure`` documents for a failure
    that precedes any ``research_runs`` row -- and that the message reaches the operator
    without a ledger event having to carry it.
    """
    from whiskeyjack_bot import pipeline_live
    from whiskeyjack_bot.research.orchestrate import OrchestrationError

    real = pipeline_live.retrieve_for_question

    def refusing(conn: Any, cfg: Any, *, question: Any, **kwargs: Any) -> Any:
        if question.question_id == MULTIPLE_CHOICE:
            raise OrchestrationError("the ledger refused to open the run")
        return real(conn, cfg, question=question, **kwargs)

    monkeypatch.setattr(pipeline_live, "retrieve_for_question", refusing)
    batch = live(ledger, config, limit=3)

    assert batch.records_written == 2
    failed = [o for o in batch.outcomes if o.status != "recorded"]
    assert [o.question_id for o in failed] == [MULTIPLE_CHOICE]
    assert failed[0].status == "research_failed"
    assert failed[0].detail_code == "internal_error"
    assert failed[0].retrieval_run_ids == ()
    assert failed[0].problems == ("the ledger refused to open the run",)
    events = rows(
        ledger,
        "SELECT event_type, detail_code, retrieval_run_id FROM pipeline_failure_events",
    )
    assert events == [("research_failed", "internal_error", None)]


def test_a_failed_question_does_not_consume_another_questions_attempt_id(
    config: AppConfig, ledger: Any
) -> None:
    """004's trigger refuses a failure whose attempt id already produced a record, so a
    batch that reused one id across questions would fail on its second question."""
    sdk = _TrackingSDK(fail_for={BINARY})
    batch = live(ledger, config, limit=3, news_client=sdk)
    ids = [outcome.attempt_id for outcome in batch.outcomes]
    assert len(set(ids)) == len(ids)


# --- retrying a later phase repeats no paid call ----------------------------------------


def test_a_second_run_reuses_the_research_it_already_paid_for(
    config: AppConfig, ledger: Any
) -> None:
    """``CODEX_HANDOFF.md``: *retrying a later phase must not repeat an earlier paid call
    unless explicitly requested.* Asserted by call count, which a log line could not do."""
    first = _TrackingSDK()
    live(ledger, config, question_id=BINARY, news_client=first)
    assert first.news.calls, "the first run must actually retrieve"

    second = _TrackingSDK()
    batch = live(ledger, config, question_id=BINARY, news_client=second)
    assert second.news.calls == []
    assert batch.outcomes[0].research_reused is True
    assert batch.outcomes[0].status == "recorded"
    # The reused packet is the same evidence, so the second record cites the same runs.
    assert batch.outcomes[0].retrieval_run_ids == (
        rows(ledger, "SELECT retrieval_run_id FROM research_runs")[0][0],
    )


def test_refresh_research_pays_again_and_says_so(config: AppConfig, ledger: Any) -> None:
    first = _TrackingSDK()
    live(ledger, config, question_id=BINARY, news_client=first)
    second = _TrackingSDK()
    batch = live(ledger, config, question_id=BINARY, news_client=second, refresh_research=True)
    assert second.news.calls, "--refresh-research must retrieve again"
    assert batch.outcomes[0].research_reused is False
    assert len(rows(ledger, "SELECT 1 FROM research_runs")) == 2


def test_reuse_does_not_require_the_replay_switch(config: AppConfig, ledger: Any) -> None:
    """Reuse is not replay. ``replay_research``'s gate means "this run never spends", which
    a live run has already contradicted, so reuse goes through the ungated reader instead."""
    assert config.retrieval.replay_saved_research is False
    live(ledger, config, question_id=BINARY)
    batch = live(ledger, config, question_id=BINARY)
    assert batch.outcomes[0].research_reused is True


# --- the caps ---------------------------------------------------------------------------


def test_the_configured_question_ceiling_stops_the_batch_and_says_why(
    config: AppConfig, ledger: Any
) -> None:
    config.run_limits.max_questions = 2
    batch = live(ledger, config)
    assert len(batch.outcomes) == 2
    assert batch.stop_reason == "question_limit"


def test_a_batch_that_runs_out_of_questions_is_completed_not_capped(
    config: AppConfig, ledger: Any
) -> None:
    """Anti-vacuity for the test above: the stop reason must distinguish the two."""
    batch = live(ledger, config, limit=3)
    assert len(batch.outcomes) == 3
    assert batch.stop_reason == "completed"


def test_known_spend_reaching_the_budget_stops_the_batch(config: AppConfig, ledger: Any) -> None:
    """The cap is checked before each question after the first: nothing is known about cost
    until something has been bought."""
    config.run_limits.max_cost_usd = 0.01
    batch = live(ledger, config, limit=3, forecaster=_PricedForecaster(0.5))
    assert batch.stop_reason == "cost_limit"
    assert len(batch.outcomes) == 1
    assert batch.known_cost_usd >= 0.01


def test_a_budget_the_run_never_reaches_does_not_stop_it(config: AppConfig, ledger: Any) -> None:
    config.run_limits.max_cost_usd = 1000.0
    batch = live(ledger, config, limit=3, forecaster=_PricedForecaster(0.5))
    assert batch.stop_reason == "completed"
    assert len(batch.outcomes) == 3


def test_unpriced_calls_are_counted_rather_than_added_as_zero(
    config: AppConfig, ledger: Any
) -> None:
    """``cost_usd is None`` means unknown, never free (M1-303 round 3).

    AskNews prices none of its calls -- "AskNews reports usage in credits, not currency, and
    no credit->USD rate is configured" -- so an unpriced call is the ordinary case, not an
    anomaly. It is counted and reported; it is never summed as zero, and it is deliberately
    not a reason to stop, because a rule that stopped on it would stop every batch after its
    first question. ``max_cost_usd`` bounds known spend and cannot bound unknown spend.
    """
    batch = live(ledger, config, limit=3)
    assert batch.known_cost_usd == 0.0
    assert batch.unpriced_calls >= 3
    assert batch.stop_reason == "completed"
    assert batch.records_written == 3


class _PricedForecaster(_Forecaster):
    """A forecaster whose calls report a cost, so the budget has something to bite on."""

    def __init__(self, price: float) -> None:
        super().__init__()
        self._price = price

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        from forecasting_tools import MonetaryCostManager

        reply = await super().invoke(prompt, system_prompt)
        # The pinned SDK's own seam: `generate.py` reads `manager.current_usage` inside the
        # same coroutine as the call, and this is how a real provider's usage arrives there.
        # Driving it through the SDK rather than monkeypatching `_invoke` keeps the cost
        # path under test the one that runs in production.
        for manager in MonetaryCostManager.get_active_cost_managers():
            manager.increase_current_usage_in_parent_managers(self._price)
        return reply


# --- the paid path writes the row even when the evidence copy is lost -------------------


def test_a_lost_artifact_still_appends_the_record(config: AppConfig, ledger: Any) -> None:
    """**The one behaviour that differs from ``run_replay``, and it is M1-312's rule.**

    ``run_replay`` refuses a record whose artifact was not written, because it spends nothing
    and can hold itself to that bar. For a paid attempt the cost and the invocation count are
    facts whether or not the evidence survived, so the row is written regardless --
    ``pipeline._require_retained_output``'s own docstring names this caller as the one that
    needs it. Rolling back here would trade a lost artifact for a lost record of a real spend.
    """
    config.storage.retain_raw_model_output = False
    batch = live(ledger, config, question_id=BINARY)
    outcome = batch.outcomes[0]
    assert outcome.status == "recorded"
    assert outcome.artifact_outcome == "retention_disabled"
    assert outcome.raw_output_path is None
    assert rows(ledger, "SELECT raw_output_path FROM forecast_records") == [(None,)]


# --- the result object cannot misreport what happened ------------------------------------


def test_a_recorded_question_without_a_record_id_is_refused() -> None:
    with pytest.raises(LiveRunError, match="carries a record id"):
        QuestionOutcome(
            question_id=1,
            status="recorded",
            attempt_id="a",
            retrieval_run_ids=(),
            document_count=0,
            research_reused=False,
        )


def test_a_recorded_failure_without_a_detail_code_is_refused() -> None:
    with pytest.raises(LiveRunError, match="detail code"):
        QuestionOutcome(
            question_id=1,
            status="research_failed",
            attempt_id="a",
            retrieval_run_ids=(),
            document_count=0,
            research_reused=False,
        )


def test_a_question_that_never_reached_the_model_reports_no_artifact() -> None:
    with pytest.raises(LiveRunError, match="reached the model"):
        QuestionOutcome(
            question_id=1,
            status="research_failed",
            attempt_id="a",
            retrieval_run_ids=(),
            document_count=0,
            research_reused=False,
            detail_code="no_evidence",
            artifact_outcome="written",
        )
