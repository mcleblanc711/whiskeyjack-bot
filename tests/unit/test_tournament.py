"""Launch release contracts: real SDK parsing, durable retries, and policy isolation."""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from forecasting_tools.data_models.data_organizer import DataOrganizer
from asknews_sdk.dto.news import SearchResponse

from whiskeyjack_bot.config import validate_config_data
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.questions.normalize import normalize_questions
from whiskeyjack_bot import tournament as whiskeyjack_tournament
from whiskeyjack_bot import tournament_state
from whiskeyjack_bot.tournament import MAX_TRANSIENT_ATTEMPTS, run_once
from whiskeyjack_bot.tournament import status
from whiskeyjack_bot.tournament_state import (
    Budget,
    StorageFailure,
    TournamentError,
    append,
    check_storage,
    digest,
    disable,
    enable,
    require_activation,
    spending,
    utcnow,
)
from tests.unit.test_pipeline_live import config as base_config, _article, reply_for

ROOT = Path(__file__).resolve().parents[2]


class Platform:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.posts = 0
        self.comment_posts = 0
        self.comments: list[dict[str, Any]] = []
        self.hide_forecast = False
        self.lose_forecast_response = False
        self.lose_comment_response = False
        self.hide_comments = False
        self.account = 42
        self.empty = False
        self.before_post_fetch: Any = None

    def get_current_user_id(self) -> int:
        return self.account

    def get_all_open_questions_from_tournament(self, project: int, **kwargs: Any) -> list[Any]:
        assert project == 32977
        return (
            []
            if self.empty
            else [DataOrganizer.get_question_from_post_json(copy.deepcopy(self.raw))]
        )

    def get_question_by_post_id(self, post_id: int) -> Any:
        raw = copy.deepcopy(self.raw)
        if self.before_post_fetch:
            self.before_post_fetch(raw)
        if self.posts and not self.hide_forecast:
            raw["question"]["my_forecasts"] = {
                "history": [{"start_time": utcnow().timestamp(), "forecast_values": [0.65, 0.35]}]
            }
        return DataOrganizer.get_question_from_post_json(raw)

    def post_binary_question_prediction(self, question_id: int, prediction: float) -> None:
        assert question_id == self.raw["question"]["id"]
        assert prediction == 0.35
        self.posts += 1
        if self.lose_forecast_response:
            raise TimeoutError

    def create_private_comment(self, post_id: int, text: str) -> object:
        self.comment_posts += 1
        comment = {
            "id": 1,
            "on_post": post_id,
            "text": text,
            "author": {"id": self.account},
            "is_private": True,
        }
        self.comments.append(comment)
        if self.lose_comment_response:
            raise TimeoutError
        return comment

    def list_my_comments(self, post_id: int, account_id: int) -> list[dict[str, Any]]:
        return [] if self.hide_comments else self.comments


class News:
    def __init__(self, raw: dict[str, Any]):
        self.news = self
        self.raw = raw
        self.calls = 0
        self.future = False
        self.stale = False

    def search_news(self, **kwargs: Any) -> SearchResponse:
        self.calls += 1
        docs = [_article("https://example.org/first"), _article("https://example.org/second")]
        now = utcnow() - timedelta(seconds=2)
        if self.future:
            now += timedelta(days=2)
        if self.stale:
            now -= timedelta(days=365)
        for doc in docs:
            doc.pub_date = now
            doc.crawl_date = now
            doc.eng_title = self.raw["question"]["title"]
            doc.title = doc.eng_title
            doc.summary = "The example agency July data release is scheduled for publication."
        return SearchResponse.model_construct(as_dicts=docs)


class Model:
    model = "openrouter/openai/gpt-5.6-sol"

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.calls = 0
        self.wrong_time = False

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        self.calls += 1
        request = json.loads(prompt[1]["content"])
        q = normalize_questions([DataOrganizer.get_question_from_post_json(self.raw)]).questions[0]
        reply = json.loads(reply_for(q))
        reply["as_of_utc"] = "2000-01-01T00:00:00Z" if self.wrong_time else request["as_of_utc"]
        return json.dumps(reply)


@pytest.fixture
def case(tmp_path: Path) -> Any:
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
    config = validate_config_data(data)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    raw = json.loads((ROOT / "tests/fixtures/api_posts/binary_post.json").read_text())
    raw["projects"]["default_project"]["id"] = 32977
    raw["question"]["scheduled_close_time"] = (utcnow() + timedelta(hours=2)).isoformat()
    platform, news, model = Platform(raw), News(raw), Model(raw)
    enable(
        conn,
        config,
        account_id=42,
        project_id=32977,
        starts=utcnow() - timedelta(minutes=1),
        ends=utcnow() + timedelta(days=1),
    )
    yield conn, config, platform, news, model
    conn.close()


def poll(case: Any) -> dict[str, Any]:
    conn, config, platform, news, model = case
    return run_once(
        conn,
        config,
        client=platform,
        poster=platform,
        news_client=news,
        web_client=object(),
        forecaster=model,
    )


def test_one_command_confirms_forecast_and_private_comment_and_repeat_is_idle(case: Any) -> None:
    result = poll(case)
    assert result["forecast_confirmed"] == result["comment_completed"] == 1
    assert result["unresolved"] == 0
    assert result["heartbeat"]["failures"] == 0
    repeated = poll(case)
    assert repeated["comment_completed"] == 1
    assert (case[2].posts, case[2].comment_posts, case[3].calls, case[4].calls) == (1, 1, 2, 1)
    approval = case[0].execute("SELECT actor FROM approval_events").fetchone()[0]
    assert approval.startswith("policy:launch-v1:")
    assert result["reserved_cost_usd"] == 0.15  # AskNews charges are estimates, never free.


def test_empty_tournament_is_successful_idle_poll(case: Any) -> None:
    case[2].empty = True
    result = poll(case)
    assert result["heartbeat"]["complete"] and result["heartbeat"]["failures"] == 0
    assert case[3].calls == case[4].calls == 0


@pytest.mark.parametrize("field", ["future", "stale"])
def test_unusable_evidence_never_purchases_a_forecast(case: Any, field: str) -> None:
    setattr(case[3], field, True)
    assert poll(case)["heartbeat"]["failures"] == 1
    assert case[4].calls == case[2].posts == 0


@pytest.mark.parametrize(
    "field,expected_code", [("future", "stale_evidence"), ("stale", "stale_evidence")]
)
def test_deterministic_refusal_is_recorded_and_never_re_purchased(
    case: Any, field: str, expected_code: str
) -> None:
    """M1-326: the second poll declines for free.

    Asserted on ``News.calls`` -- billed provider calls -- and never on ``research_runs``
    rows, which are wrong in both directions: ``started_at_utc`` is the *pinned* ``now``
    rather than wall clock, so distinct timestamps undercount retrievals, while the row
    count overcounts billed calls because a within-window repeat is served from
    ``durable.py``'s dedup without billing. Live, question 45452 spent 33 billed AskNews
    calls re-deriving one unchanging verdict.
    """
    conn, _config, platform, news, model = case
    setattr(news, field, True)

    assert poll(case)["heartbeat"]["failures"] == 1
    first = news.calls
    assert first == 2, "one retrieval is two AskNews calls, never one"

    row = conn.execute("SELECT event_type, detail_code FROM pipeline_failure_events").fetchone()
    assert tuple(row) == ("research_failed", expected_code), (
        "the verdict must reach pipeline_failure_events; before M1-326 it was raised "
        "straight past the recorder and left no row at all"
    )

    second = poll(case)
    assert news.calls == first, "a deterministic verdict must never be re-purchased"
    assert model.calls == platform.posts == 0
    assert second["heartbeat"]["blocked"] == 1
    assert second["heartbeat"]["failures"] == 0, "declining is not failing"


def test_editing_the_question_re_qualifies_a_blocked_question(case: Any) -> None:
    """The block is keyed on the fingerprint, so a changed question is a new question."""
    conn, _config, _platform, news, _model = case
    news.stale = True
    poll(case)
    blocked = conn.execute(
        "SELECT COUNT(*) FROM tournament_events WHERE kind='question_blocked'"
    ).fetchone()[0]
    assert blocked == 1 and news.calls == 2

    poll(case)
    assert news.calls == 2, "unchanged question: still blocked"

    news.raw["question"]["title"] += " (revised)"
    poll(case)
    assert news.calls == 4, "an edited question must be retrieved again"


def test_transient_exhaustion_records_the_blocked_question(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-1 finding B1: exhaustion must name the question, not just count it.

    A provider outage is transient, so the question is retried -- bounded. When the bound is
    reached the skip has to append a fingerprint-bound `question_blocked` row; before the fix
    it left only an aggregate ``heartbeat["exhausted"]``, so the exhausted question was
    unidentifiable in the ledger.

    The clock advances 31 minutes between polls because attempts are counted in
    `question_started` events, and those are appended only once the 1800s checkpoint has
    expired -- inside one window `now` is pinned and four polls consume a single attempt.
    That is the checkpoint-round semantics the cap is deliberately built on.
    """
    conn, _config, platform, news, _model = case
    real_now = utcnow()
    platform.raw["question"]["scheduled_close_time"] = (real_now + timedelta(hours=8)).isoformat()

    clock = {"now": real_now}
    monkeypatch.setattr(tournament_state, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(whiskeyjack_tournament, "utcnow", lambda: clock["now"])

    def outage(**kwargs: Any) -> SearchResponse:
        news.calls += 1
        raise RuntimeError("provider outage")

    monkeypatch.setattr(news, "search_news", outage)

    for _ in range(MAX_TRANSIENT_ATTEMPTS):
        assert poll(case)["heartbeat"]["failures"] == 1
        clock["now"] += timedelta(minutes=31)

    assert (
        conn.execute(
            "SELECT COUNT(*) FROM tournament_events WHERE kind='question_blocked'"
        ).fetchone()[0]
        == 0
    ), "a transient failure must not block before the bound"

    exhausted = poll(case)
    assert exhausted["heartbeat"]["exhausted"] == 1
    rows = [
        json.loads(r[0])
        for r in conn.execute("SELECT data FROM tournament_events WHERE kind='question_blocked'")
    ]
    assert len(rows) == 1, "exhaustion must record exactly one block, at the transition"
    assert rows[0]["reason"] == "transient_attempts_exhausted"
    assert rows[0]["attempts"] == MAX_TRANSIENT_ATTEMPTS
    assert rows[0]["fingerprint"], "the block must be fingerprint-bound"

    billed = news.calls
    clock["now"] += timedelta(minutes=31)
    again = poll(case)
    assert again["heartbeat"]["blocked"] == 1 and news.calls == billed
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM tournament_events WHERE kind='question_blocked'"
        ).fetchone()[0]
        == 1
    ), "the block is appended at the transition, not on every later poll"


def test_wrong_timestamp_is_repaired_at_most_once_and_never_posted(case: Any) -> None:
    case[4].wrong_time = True
    assert poll(case)["heartbeat"]["failures"] == 1
    assert case[4].calls == 2 and case[2].posts == 0


def test_lost_forecast_response_reconciles_without_another_post(case: Any) -> None:
    case[2].lose_forecast_response = True
    case[2].hide_forecast = True
    assert poll(case)["unresolved"] == 1
    assert poll(case)["unresolved"] == 1
    assert case[2].posts == 1
    case[2].hide_forecast = False
    result = poll(case)
    assert result["unresolved"] == 0 and result["comment_completed"] == 1
    assert case[2].posts == case[4].calls == 1


def test_comment_response_loss_resumes_only_comment_verification(case: Any) -> None:
    case[2].lose_comment_response = True
    case[2].hide_comments = True
    first = poll(case)
    assert first["forecast_confirmed"] == 1 and first["comment_completed"] == 0
    assert poll(case)["unresolved"] == 1
    case[2].hide_comments = False
    assert poll(case)["comment_completed"] == 1
    assert case[2].posts == case[2].comment_posts == case[4].calls == 1


@pytest.mark.parametrize("change", ["bounds", "state", "deadline", "destination"])
def test_changed_or_unreadable_question_refuses_before_post(case: Any, change: str) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        if change == "bounds":
            raw["question"]["resolution_criteria"] = "Changed resolution rule"
        elif change == "state":
            raw["question"]["status"] = "closed"
        elif change == "deadline":
            raw["question"]["scheduled_close_time"] = "2020-01-01T00:00:00Z"
        else:
            raw["projects"]["default_project"]["id"] = 33122

    case[2].before_post_fetch = mutate
    assert poll(case)["heartbeat"]["failures"] == 1
    assert case[2].posts == 0


def test_activation_disable_account_and_config_bindings(case: Any) -> None:
    conn, config, platform, _, _ = case
    with pytest.raises(TournamentError):
        require_activation(conn, config, account_id=43, project_id="32977")
    with pytest.raises(TournamentError):
        require_activation(conn, config, account_id=42, project_id="33122")
    config.forecast.prompt_version = "9.9.9"
    with pytest.raises(TournamentError):
        require_activation(conn, config, account_id=42, project_id="32977")
    disable(conn)
    with pytest.raises(TournamentError):
        poll(case)
    assert platform.posts == 0


def test_testing_profile_cannot_activate_production(case: Any) -> None:
    conn, config, _, _, _ = case
    config.metaculus.tournament.id = 33122
    with pytest.raises(TournamentError, match="testing profiles"):
        enable(
            conn,
            config,
            account_id=42,
            project_id=33122,
            starts=utcnow(),
            ends=utcnow() + timedelta(days=1),
        )


def test_unknown_budget_reservations_survive_restart_and_reactivation(case: Any) -> None:
    conn, config, _, _, _ = case
    budget = Budget(conn, config.storage.artifact_root, "42:32977", 1_000_000)
    reservation = budget.reserve("provider", 0.75, {"query": "test"})
    budget.settle(reservation, None)
    with connect(config.storage.sqlite_path) as second:
        restarted = Budget(second, config.storage.artifact_root, "42:32977", 1_000_000)
        with pytest.raises(TournamentError, match="budget exhausted"):
            restarted.reserve("provider", 0.26, {})
        assert spending(second, "42:32977") == (0, 750_000)
    budget.settle(reservation, 0.25)
    assert spending(conn, "42:32977") == (250_000, 0)


def test_restored_database_and_missing_witnesses_fail_closed(case: Any, tmp_path: Path) -> None:
    conn, config, _, _, _ = case
    backup = tmp_path / "backup.sqlite"
    copied = sqlite3.connect(backup)
    conn.backup(copied)
    copied.close()
    Budget(conn, config.storage.artifact_root, "42:32977", 1_000_000).reserve("provider", 0.1, {})
    old = connect(backup)
    with pytest.raises(StorageFailure, match="restore"):
        check_storage(old, config.storage.artifact_root)
    old.close()
    next((config.storage.artifact_root / "operations").glob("*.json")).unlink()
    with pytest.raises(StorageFailure, match="missing"):
        check_storage(conn, config.storage.artifact_root)


def _prepare_version(case: Any) -> str:
    from whiskeyjack_bot.pipeline_live import _attempt_question
    from whiskeyjack_bot.prompt import load_prompt
    from whiskeyjack_bot.approval import approve

    conn, config, platform, news, model = case
    q = normalize_questions(platform.get_all_open_questions_from_tournament(32977)).questions[0]
    outcome = _attempt_question(
        conn,
        config,
        question=q,
        tournament_id="32977",
        prompt=load_prompt(config.forecast.prompt_path, config.forecast.prompt_version),
        now=utcnow(),
        refresh=False,
        news_client=news,
        web_client=object(),
        forecaster=model,
    )
    assert outcome.record_id
    approve(
        conn,
        record_id=outcome.record_id,
        actor="policy:test",
        occurred_at=utcnow(),
        calibration=config.numeric_calibration,
    )
    return outcome.record_id


def _process_submit(
    config: Any, raw: dict[str, Any], record_id: str, counter: Any, start: Any, crash: bool = False
) -> None:
    import os
    from whiskeyjack_bot.forecast.store import read_forecast_record
    from whiskeyjack_bot.submission_payload import authorized_payload
    from whiskeyjack_bot.submission_live import post_approved_forecast

    class CountedPlatform(Platform):
        def post_binary_question_prediction(self, question_id: int, prediction: float) -> None:
            with counter.get_lock():
                counter.value += 1
            super().post_binary_question_prediction(question_id, prediction)
            if crash:
                os._exit(7)  # Server accepted; receipt never reaches SQLite.

    conn = connect(config.storage.sqlite_path)
    record = read_forecast_record(conn, record_id)
    payload = authorized_payload(record, calibration=config.numeric_calibration).payload
    start.wait(10)
    try:
        post_approved_forecast(
            conn,
            config=config,
            record_id=record_id,
            payload=payload,
            poster=CountedPlatform(raw),
            occurred_at=utcnow(),
        )
    except Exception:
        pass  # The parent measures POSTs and the durable reservation, not exception text.
    finally:
        conn.close()


def test_two_processes_and_two_versions_allow_at_most_one_post(case: Any) -> None:
    import multiprocessing

    first, second = _prepare_version(case), _prepare_version(case)
    ctx = multiprocessing.get_context("fork")
    counter, start = ctx.Value("i", 0), ctx.Event()
    children = [
        ctx.Process(target=_process_submit, args=(case[1], case[2].raw, rid, counter, start))
        for rid in [first, second]
    ]
    for child in children:
        child.start()
    start.set()
    for child in children:
        child.join(20)
        if child.is_alive():
            child.kill()
            pytest.fail("submission process exceeded its bound")
    assert counter.value == 1
    assert case[0].execute("SELECT count(*) FROM submission_key_reservations").fetchone()[0] == 1


def test_process_death_after_acceptance_recovers_without_post_or_paid_work(case: Any) -> None:
    import multiprocessing

    record_id = _prepare_version(case)
    ctx = multiprocessing.get_context("fork")
    counter, start = ctx.Value("i", 0), ctx.Event()
    child = ctx.Process(
        target=_process_submit, args=(case[1], case[2].raw, record_id, counter, start, True)
    )
    child.start()
    start.set()
    child.join(20)
    assert child.exitcode == 7 and counter.value == 1
    assert case[0].execute("SELECT count(*) FROM submission_attempts").fetchone()[0] == 0
    case[2].posts = 1  # Read-only platform state survived the process.
    result = poll(case)
    assert result["forecast_confirmed"] == result["comment_completed"] == 1
    assert case[2].posts == case[2].comment_posts == case[4].calls == 1


def test_missing_model_artifact_refuses_even_manual_submission(case: Any) -> None:
    from whiskeyjack_bot.forecast.store import read_forecast_record, read_model_call
    from whiskeyjack_bot.submission_payload import authorized_payload
    from whiskeyjack_bot.submission_live import LiveSubmissionError, post_approved_forecast

    record_id = _prepare_version(case)
    conn, config, platform, _, _ = case
    call = read_model_call(conn, record_id)
    (config.storage.artifact_root / call.raw_output_path).unlink()
    record = read_forecast_record(conn, record_id)
    with pytest.raises(LiveSubmissionError):
        post_approved_forecast(
            conn,
            config=config,
            record_id=record_id,
            poster=platform,
            payload=authorized_payload(record, calibration=config.numeric_calibration).payload,
        )
    assert platform.posts == 0


def test_manual_submit_requires_activation(case: Any) -> None:
    from whiskeyjack_bot.forecast.store import read_forecast_record
    from whiskeyjack_bot.submission_payload import authorized_payload
    from whiskeyjack_bot.submission_live import LiveSubmissionError, post_approved_forecast

    record_id = _prepare_version(case)
    conn, config, platform, _, _ = case
    disable(conn)
    with pytest.raises(LiveSubmissionError, match="disabled"):
        post_approved_forecast(
            conn,
            config=config,
            record_id=record_id,
            poster=platform,
            payload=authorized_payload(
                read_forecast_record(conn, record_id), calibration=config.numeric_calibration
            ).payload,
        )
    assert platform.posts == 0


def test_real_sdk_group_refetch_selects_exact_subquestion(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests
    from forecasting_tools.helpers.metaculus_client import MetaculusClient
    from whiskeyjack_bot.metaculus.client import SingleAttemptPoster
    from whiskeyjack_bot.submission_live import select_subquestion

    raw = json.loads((ROOT / "tests/fixtures/api_posts/group/minibench_group.json").read_text())
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(raw).encode()
    monkeypatch.setattr(requests, "get", lambda *a, **kw: response)
    client = MetaculusClient(
        token="synthetic", sleep_seconds_between_requests=0, sleep_jitter_seconds=0
    )
    result = SingleAttemptPoster(client).get_question_by_post_id(raw["id"])
    assert isinstance(result, list) and len(result) > 1
    target = result[-1].id_of_question
    selected = select_subquestion(result, target)
    assert selected.id_of_question == target
    assert normalize_questions([selected]).questions[0].question_id == target


def test_missing_research_artifact_blocks_post(case: Any) -> None:
    from whiskeyjack_bot.forecast.store import read_forecast_record
    from whiskeyjack_bot.submission_policy import require_research_artifacts

    rid = _prepare_version(case)
    record = read_forecast_record(case[0], rid)
    run = (
        case[0]
        .execute(
            "SELECT raw_response_path FROM research_runs WHERE retrieval_run_id=?",
            (record.retrieval_run_id,),
        )
        .fetchone()
    )
    (case[1].storage.artifact_root / run[0]).unlink()
    with pytest.raises(StorageFailure, match="artifact missing"):
        require_research_artifacts(case[0], case[1], record)


def test_consistent_backup_rollback_still_requires_live_reconciliation(
    case: Any, tmp_path: Path
) -> None:
    import shutil
    from whiskeyjack_bot.restore import reconcile_restored

    conn, config, platform, _, _ = case
    backup = tmp_path / "saved.sqlite"
    copy_conn = sqlite3.connect(backup)
    conn.backup(copy_conn)
    copy_conn.close()
    budget = Budget(conn, config.storage.artifact_root, "42:32977", 1_000_000)
    budget.reserve("provider", 0.2, {})
    # Roll back BOTH the database and its artifacts; the host guard is outside that backup.
    for path in (config.storage.artifact_root / "operations").glob("*.json"):
        path.unlink()
    restored_path = tmp_path / "restored.sqlite"
    shutil.copyfile(backup, restored_path)
    restored = connect(restored_path)
    with pytest.raises(StorageFailure, match="restore"):
        check_storage(restored, config.storage.artifact_root)
    result = reconcile_restored(restored, config, platform)
    assert result["imported_witnesses"] == 1
    assert spending(restored, "42:32977") == (0, 200_000)
    check_storage(restored, config.storage.artifact_root)
    restored.close()


def test_completed_retrieval_call_survives_restart_and_unknown_call_is_held(case: Any) -> None:
    from whiskeyjack_bot.research.durable import begin_call, complete_call
    from whiskeyjack_bot.tournament_state import budget_context

    conn, config, *_ = case
    request = {"query": "contemporary evidence", "strategy": "current"}
    budget = Budget(conn, config.storage.artifact_root, "42:32977", 200_000)
    with budget_context(budget):
        scope, cached = begin_call("asknews", 0.025, request, 1, "cutoff")
        assert cached is None
        complete_call(scope, {"articles": ["saved evidence"]})
    with connect(config.storage.sqlite_path) as restored:
        with budget_context(Budget(restored, budget.root, budget.scope, budget.ceiling)):
            _, cached = begin_call("asknews", 0.025, request, 1, "cutoff")
            assert cached == {"articles": ["saved evidence"]}
            begin_call("asknews", 0.125, {"query": "archive"}, 1, "cutoff")
            with pytest.raises(TournamentError, match="unknown"):
                begin_call("asknews", 0.125, {"query": "archive"}, 1, "cutoff")
            assert spending(restored, budget.scope) == (0, 150_000)


def test_sol_price_parameters_response_recovery_and_unknown_cost(
    case: Any, monkeypatch: Any
) -> None:
    import asyncio
    import httpx
    from whiskeyjack_bot.forecast.sol import SolClient
    from whiskeyjack_bot.tournament_state import budget_context

    conn, config, *_ = case
    calls = []

    def request(req: Any) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        assert body["reasoning"]["effort"] == "medium"
        assert body["max_tokens"] == 6000 and "temperature" not in body
        assert body["provider"]["max_price"] == {"prompt": 2, "completion": 10}
        return httpx.Response(200, json={"choices": [{"message": {"content": "saved output"}}]})

    original = httpx.AsyncClient
    monkeypatch.setenv(config.model.api_key_env, "test-secret")
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(request), **kw)
    )
    budget = Budget(conn, config.storage.artifact_root, "42:32977", 1_000_000)
    with budget_context(budget):
        prompt = [{"role": "user", "content": "test"}]
        assert asyncio.run(SolClient(config).invoke(prompt)) == "saved output"
        assert asyncio.run(SolClient(config).invoke(prompt)) == "saved output"
    assert len(calls) == 1
    assert spending(conn, budget.scope)[0] == 0
    assert spending(conn, budget.scope)[1] > 60_000


def test_rehearsal_filter_does_not_purchase_another_question(case: Any) -> None:
    conn, config, platform, news, model = case
    result = run_once(
        conn,
        config,
        client=platform,
        poster=platform,
        news_client=news,
        web_client=object(),
        forecaster=model,
        question_id=999999,
    )
    assert result["heartbeat"]["processed"] == 0
    assert news.calls == model.calls == platform.posts == 0


def test_uncertain_version_blocks_a_new_version_before_live_post(case: Any) -> None:
    from whiskeyjack_bot.forecast.store import read_forecast_record
    from whiskeyjack_bot.submission_live import post_approved_forecast, LiveSubmissionError
    from whiskeyjack_bot.submission_payload import authorized_payload

    first, second = _prepare_version(case), _prepare_version(case)
    conn, config, platform, *_ = case
    platform.lose_forecast_response = platform.hide_forecast = True

    def submit(rid: str) -> Any:
        record = read_forecast_record(conn, rid)
        return post_approved_forecast(
            conn,
            config=config,
            record_id=rid,
            poster=platform,
            payload=authorized_payload(record, calibration=config.numeric_calibration).payload,
        )

    submit(first)
    with pytest.raises(LiveSubmissionError, match="durable submission intent"):
        submit(second)
    assert platform.posts == 1


@pytest.mark.parametrize(
    "fixture,field,value",
    [
        ("numeric_post.json", "scaling", {"range_min": 0, "range_max": 900, "zero_point": None}),
        ("multiple_choice_post.json", "options", ["Changed A", "Changed B"]),
    ],
)
def test_submission_policy_revalidates_numeric_bounds_and_mc_options(
    case: Any, fixture: str, field: str, value: Any
) -> None:
    from whiskeyjack_bot.submission_policy import prepare_live_policy
    from whiskeyjack_bot.submission_live import LiveSubmissionError, ForecastHistory
    from whiskeyjack_bot.forecast.store import read_forecast_record
    from whiskeyjack_bot.submission_payload import authorized_payload

    conn, config, platform, news, model = case
    raw = json.loads((ROOT / "tests/fixtures/api_posts" / fixture).read_text())
    raw["projects"]["default_project"]["id"] = 32977
    raw["question"]["scheduled_close_time"] = (utcnow() + timedelta(hours=2)).isoformat()
    platform.raw = news.raw = model.raw = raw
    rid = _prepare_version(case)
    record = read_forecast_record(conn, rid)
    payload = authorized_payload(record, calibration=config.numeric_calibration)
    callback = prepare_live_policy(conn, config, platform, record, payload.payload, payload.sha256)
    raw = copy.deepcopy(raw)
    if field == "scaling":
        raw["question"][field].update(value)
    else:
        raw["question"][field] = value
    changed = DataOrganizer.get_question_from_post_json(raw)
    with pytest.raises(LiveSubmissionError, match="resolution inputs changed"):
        callback(changed, ForecastHistory(()))
    assert platform.posts == 0


def test_archived_resolution_url_names_the_original_publisher(case: Any) -> None:
    from whiskeyjack_bot.research.quality import source_domains

    q = normalize_questions(case[2].get_all_open_questions_from_tournament(32977)).questions[0]
    q = q.model_copy(
        update={
            "resolution_criteria": "Use https://web.archive.org/web/20240112150444/https://www.forbes.com/profile/example/",
            "fine_print": None,
        }
    )
    assert source_domains(q) == ("www.forbes.com",)


def test_budget_transaction_failure_is_fatal_before_provider_invocation(case: Any) -> None:
    conn, config, *_ = case
    closed = connect(config.storage.sqlite_path)
    closed.close()
    with pytest.raises(StorageFailure, match="spending transaction"):
        Budget(closed, config.storage.artifact_root, "42:32977", 1_000_000).reserve(
            "provider", 0.1, {}
        )


def test_a_named_resolution_source_no_longer_refuses_the_forecast(case: Any) -> None:
    """M1-327: the forecast is made, and the ledger says what it was made without.

    The Cup's question 45452 in miniature. It names `https://results.cik.bg/`, the
    Bulgarian election commission's results portal for an election on 2026-10-25; AskNews
    and Exa are news retrievers, so no document they return can carry that host before the
    event it exists to report. Thirteen usable documents were discarded on each of 17
    retrievals and the question was never forecast. `News` here returns `example.org`
    documents and never the named host, which is the same situation exactly.
    """
    conn, _config, platform, news, model = case
    platform.raw["question"]["resolution_criteria"] = (
        "Resolves to the count published at https://results.example.gov/2026/ ."
    )

    result = poll(case)

    assert result["heartbeat"]["failures"] == 0, "a missing resolution source is not a failure"
    assert platform.posts == 1 and model.calls == 1, "the forecast must actually be made"
    assert news.calls == 2, "one retrieval, two AskNews calls"

    rows = [
        (scope, json.loads(data))
        for scope, data in conn.execute(
            "SELECT scope, data FROM tournament_events WHERE kind='evidence_gap'"
        )
    ]
    assert len(rows) == 1, "one forecast, one recorded gap"
    scope, gap = rows[0]
    record_id, forecast_sha = conn.execute(
        "SELECT record_id, forecast_sha256 FROM forecast_records"
    ).fetchone()
    assert scope == record_id, "the gap is scoped to the attribution record it describes"
    assert gap["code"] == "named_source_absent"
    assert gap["domains"] == ["results.example.gov"]
    assert gap["forecast_sha256"] == forecast_sha
    assert conn.execute("SELECT COUNT(*) FROM pipeline_failure_events").fetchone()[0] == 0, (
        "a recorded gap is not a recorded failure"
    )
    assert status(conn, _config)["evidence_gaps"] == 1


def test_a_forecast_from_the_named_source_records_no_gap(case: Any) -> None:
    """The other half: naming a source the retrieval *did* reach records nothing.

    Without this the test above passes on code that records a gap unconditionally, which
    would be a claim about every forecast rather than about this one.
    """
    conn, _config, platform, news, _model = case
    platform.raw["question"]["resolution_criteria"] = "Resolves per https://www.example.org/x ."

    assert poll(case)["heartbeat"]["failures"] == 0
    assert platform.posts == 1 and news.calls == 2
    assert (
        conn.execute("SELECT COUNT(*) FROM tournament_events WHERE kind='evidence_gap'").fetchone()[
            0
        ]
        == 0
    ), "a document from the named host is exactly what the gate asked for"


def test_pre_activation_attempts_do_not_retire_a_question(case: Any) -> None:
    """M1-327: `question_started` rows from before M1-326 are not attempts.

    Question 45452's ledger holds 17 of them under one fingerprint against a cap of 3,
    written by the launch-readiness checkpoint months before M1-326 gave that event a
    second meaning. Reading them as attempts retires the question permanently, before the
    gate this branch fixes is ever reached -- so the seeded rows here carry the exact
    pre-M1-326 shape: a fingerprint and an `at`, and no `activation_id`.
    """
    conn, config, platform, news, _model = case
    question = normalize_questions(
        platform.get_all_open_questions_from_tournament(32977)
    ).questions[0]
    fingerprint = digest(question.model_dump(mode="json"))
    for _ in range(MAX_TRANSIENT_ATTEMPTS + 2):
        append(
            conn,
            "question_started",
            f"32977:{question.question_id}",
            {"at": utcnow().isoformat(), "fingerprint": fingerprint},
        )

    result = poll(case)

    assert news.calls == 2, "legacy checkpoints must not read as exhausted attempts"
    assert platform.posts == 1 and result["heartbeat"]["failures"] == 0
    assert result["heartbeat"].get("exhausted", 0) == 0


def test_re_enabling_the_tournament_re_arms_a_blocked_question(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verdict expires with the activation that produced it (M1-327).

    A deterministic block claims the answer cannot change, which is only ever true of a
    fixed question *and* fixed code. `tournament enable` is the operator's existing,
    recorded "go", so it is what retires the verdicts a code change invalidated -- the
    situation this whole branch is: 45452's recorded refusal is wrong now, and nothing
    else could clear it without editing an append-only journal.

    The clock advances 31 minutes so the re-attempt is a real one. Inside the 1800s
    window `now` is pinned and the research checkpoint serves the same packet back, which
    would re-derive the same verdict off cached evidence and prove nothing about the gate.
    """
    conn, config, platform, news, _model = case
    real_now = utcnow()
    platform.raw["question"]["scheduled_close_time"] = (real_now + timedelta(hours=8)).isoformat()
    clock = {"now": real_now}
    monkeypatch.setattr(tournament_state, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(whiskeyjack_tournament, "utcnow", lambda: clock["now"])

    news.stale = True
    assert poll(case)["heartbeat"]["failures"] == 1
    billed = news.calls
    clock["now"] += timedelta(minutes=31)
    assert poll(case)["heartbeat"]["blocked"] == 1 and news.calls == billed

    enable(
        conn,
        config,
        account_id=42,
        project_id=32977,
        starts=clock["now"] - timedelta(minutes=1),
        ends=clock["now"] + timedelta(days=1),
    )
    news.stale = False
    clock["now"] += timedelta(minutes=31)

    result = poll(case)
    assert result["heartbeat"].get("blocked", 0) == 0, "the block belonged to the old activation"
    assert news.calls > billed, "a fresh activation re-arms the question"
    assert platform.posts == 1 and result["heartbeat"]["failures"] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM tournament_events WHERE kind='question_blocked'"
        ).fetchone()[0]
        == 1
    ), "the stale block stays in the journal; it is superseded, never deleted"
