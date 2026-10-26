"""M1-359: the shadow experiment arm -- a never-submittable forecast from baseline plus lens.

Driven through the real ``run_once`` with the M1-358 lens harness (``test_local_lens_worker``):
a question about the Strait of Hormuz, so the lens buys an Exa call for Iran and an AskNews call
for the UAE. What is asserted is the acceptance criterion, clause by clause:

- **its own append-only table**, bound to the control forecast, the combined packet hash and its
  own prompt hash -- each recomputed here from the ledger and the prompt file, never read back
  from the row it is checking;
- **no code path can approve, reserve or submit it** -- by the functions that do those things,
  by the schema's foreign keys and 020's trigger, and by what the module imports, out of process;
- **its model cost reserves and settles under the active budget** -- through the production
  ``PricedClient`` behind an httpx transport, counting what reached the transport (M1-351: the
  harness's fake model bypasses the priced client, so a count taken on it proves nothing);
- **a shadow failure never affects the control** -- each failure the worker can meet leaves the
  control recorded and posted, and is written down.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from forecasting_tools.data_models.data_organizer import DataOrganizer
from test_local_lens_worker import (  # type: ignore[import-not-found]
    ExaTransport,
    _build,
    _lens_events,
    _observed,
    _poll,
)
from test_pipeline_live import reply_for  # type: ignore[import-not-found]
from test_tournament import Model  # type: ignore[import-not-found]

from whiskeyjack_bot import tournament as whiskeyjack_tournament
from whiskeyjack_bot.approval import ApprovalError, approve
from whiskeyjack_bot.config import validate_config_data
from whiskeyjack_bot.forecast.priced import PricedClient
from whiskeyjack_bot.forecast.record import canonical_record_json, record_sha256
from whiskeyjack_bot.forecast.shadow import (
    SHADOW_CHECKPOINT_KIND,
    ShadowForecastError,
    lens_run_ids,
    read_shadow_forecasts,
    run_shadow_forecast,
)
from whiskeyjack_bot.questions.normalize import normalize_questions
from whiskeyjack_bot.research.packet import PACKET_LOCALES_SCHEMA_VERSION, packet_sha256
from whiskeyjack_bot.research.quality import usable_packet
from whiskeyjack_bot.research.store import load_packet
from whiskeyjack_bot.submission import SubmissionError, submission_key_for_record
from whiskeyjack_bot.tournament_state import TournamentError, events

ROOT = Path(__file__).resolve().parents[2]
LENS_PROMPT = ROOT / "prompts" / "forecaster-tournament-lens.md"
SHADOW_ON = {"enabled": True, "prompt_path": str(LENS_PROMPT), "prompt_version": "1.1.0"}


def _is_shadow(request: dict[str, Any]) -> bool:
    """The shadow's request is the one whose documents carry ``locale`` (``LensModelInput``)."""
    return any("locale" in document for document in request["research_documents"])


class FailingShadowModel(Model):
    """Answers the control; raises on the shadow's call, as a provider failure would."""

    def __init__(self, raw: dict[str, Any]) -> None:
        super().__init__(raw)
        self.shadow_calls = 0

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        if _is_shadow(json.loads(prompt[1]["content"])):
            self.shadow_calls += 1
            raise RuntimeError("provider unavailable")
        return await super().invoke(prompt, system_prompt)


class SlowShadowModel(Model):
    """Answers the control at once and stalls on the shadow's call."""

    delay = 2.0

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        if _is_shadow(json.loads(prompt[1]["content"])):
            time.sleep(self.delay)
        return await super().invoke(prompt, system_prompt)


@pytest.fixture
def shadow_case(tmp_path: Path) -> Iterator[Any]:
    case = _build(tmp_path, enabled=True, shadow=SHADOW_ON)
    yield case
    case[0].close()


def _shadow_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT DISTINCT scope FROM tournament_events WHERE kind = ?", (SHADOW_CHECKPOINT_KIND,)
    ).fetchall()
    return [event for (scope,) in rows for event in events(conn, SHADOW_CHECKPOINT_KIND, scope)]


def _lens_event_id(conn: sqlite3.Connection) -> str:
    (event_id,) = conn.execute(
        "SELECT event_id FROM tournament_events WHERE kind = 'local_lens_checkpoint'"
    ).fetchone()
    return str(event_id)


def _control(conn: sqlite3.Connection) -> tuple[str, str]:
    (row,) = conn.execute("SELECT record_id, record_json FROM forecast_records").fetchall()
    return str(row[0]), str(row[1])


def _shadow_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM shadow_forecasts").fetchone()[0])


def _question(case: Any) -> Any:
    platform = case[2]
    return normalize_questions([DataOrganizer.get_question_from_post_json(platform.raw)]).questions[
        0
    ]


# --- the arm, end to end ---------------------------------------------------------------------


def test_disabled_the_shadow_makes_no_call_and_writes_nothing(tmp_path: Path) -> None:
    """The lens on and the shadow off -- the shipped combination once the lens is enabled."""
    case = _build(tmp_path, enabled=True)
    try:
        result = _poll(case, ExaTransport())
        conn, _, platform, _, model = case
        assert result["forecast_confirmed"] == 1 and platform.posts == 1
        assert len(_lens_events(conn)) == 1, "positive control: the lens ran"
        assert model.calls == 1
        assert _shadow_rows(conn) == 0 and _shadow_events(conn) == []
    finally:
        case[0].close()


def test_a_shadow_without_the_lens_is_refused_at_load(tmp_path: Path) -> None:
    from test_pipeline_live import config as base_config  # type: ignore[import-not-found]

    data = base_config.__wrapped__(tmp_path).model_dump(mode="json")
    data["retrieval"]["local_lens"] = {"enabled": False, "shadow": {"enabled": True}}
    with pytest.raises(Exception, match="shadow_without_lens"):
        validate_config_data(data)


def test_enabled_one_shadow_is_written_bound_to_the_control_packet_and_prompt(
    shadow_case: Any,
) -> None:
    """The criterion's first clause, every binding recomputed from outside the row."""
    result = _poll(shadow_case, ExaTransport())
    conn, config, platform, _, model = shadow_case
    assert result["forecast_confirmed"] == 1 and result["heartbeat"]["failures"] == 0
    assert platform.posts == 1, "the control was posted, and only the control"
    assert model.calls == 2

    (shadow,) = read_shadow_forecasts(conn)
    control_id, _ = _control(conn)
    (lens,) = _lens_events(conn)
    assert shadow.control_record_id == control_id
    assert shadow.local_lens_checkpoint_id == _lens_event_id(conn)

    # The combined packet, rebuilt here: the control's baseline runs plus the lens runs that
    # retained a document, through the control's freshness filter.
    lens_ids = [call["retrieval_run_id"] for call in lens["calls"] if call["documents"] > 0]
    assert lens_ids, "positive control: the lens retained documents"
    runs = [*lens["baseline_run_ids"], *lens_ids]
    assert list(shadow.retrieval_run_ids) == runs
    question = _question(shadow_case)
    combined = usable_packet(
        load_packet(conn, question_id=question.question_id, retrieval_run_ids=runs),
        question,
        shadow.record.generated_at_utc,
        config.retrieval.freshness_days_default,
    )
    assert combined.locales, "lens documents carry locale rows"
    assert shadow.research_packet_sha256 == packet_sha256(combined)
    assert shadow.packet_schema_version == PACKET_LOCALES_SCHEMA_VERSION
    # The control's own packet is a different one: the baseline alone.
    assert shadow.research_packet_sha256 != json.loads(_control(conn)[1])["research_packet_sha256"]

    # Its own prompt, by the file's bytes.
    digest = hashlib.sha256(LENS_PROMPT.read_bytes()).hexdigest()
    assert shadow.prompt_sha256 == digest == shadow.record.model_settings.prompt_sha256
    assert shadow.prompt_version == "1.1.0"
    # The control's prompt is the activation's, not the lens prompt.
    assert json.loads(_control(conn)[1])["model_settings"]["prompt_sha256"] != digest

    # The record is the control's canonical form, and the stored digest is of it.
    assert shadow.record.record_id == shadow.shadow_forecast_id != control_id
    assert (
        canonical_record_json(shadow.record)
        == conn.execute("SELECT record_json FROM shadow_forecasts").fetchone()[0]
    )
    assert shadow.record_sha256 == record_sha256(shadow.record)

    # The shadow model saw the lens's UAE documents with their locale; the control saw no
    # locale at all, and the shadow's baseline documents say ordinary retrieval (null).
    control_request, shadow_request = model.requests
    assert not _is_shadow(control_request) and _is_shadow(shadow_request)
    locales = [document["locale"] for document in shadow_request["research_documents"]]
    lens_locale = next(locale for locale in locales if locale is not None)
    assert lens_locale["locale_code"] == "AE"
    assert set(lens_locale) == {
        "locale_code",
        "publisher_origin",
        "language",
        "native_title",
        "translation_source",
    }
    assert None in locales, "baseline documents are rendered as ordinary retrieval"
    assert len(shadow_request["research_documents"]) > len(control_request["research_documents"])
    # Known limit, pinned so its fix is visible (M1-361): the Iranian page is Persian, and the
    # control's freshness/relevance filter reads English terms only, so it never reaches the
    # shadow's model although the lens stored it.
    unfiltered = load_packet(conn, question_id=question.question_id, retrieval_run_ids=runs)
    persian = "https://www.mehrnews.com/news/6974299"
    assert persian in {document.canonical_url for document in unfiltered.documents}
    assert persian not in {document["url"] for document in shadow_request["research_documents"]}

    (checkpoint,) = _shadow_events(conn)
    assert checkpoint["outcome"] == "written"
    assert checkpoint["shadow_forecast_id"] == shadow.shadow_forecast_id
    assert checkpoint["control_record_id"] == control_id


def test_the_control_is_the_same_run_with_the_shadow_on_or_off(tmp_path: Path) -> None:
    """The control's research checkpoint, packet and record, up to ids and clock, are what the
    lens-only run produces -- and the positive control is that the shadow really ran."""
    observed = []
    shadows = []
    for name, shadow in (("off", None), ("on", SHADOW_ON)):
        (tmp_path / name).mkdir()
        case = _build(tmp_path / name, enabled=True, shadow=shadow)
        try:
            assert _poll(case, ExaTransport())["forecast_confirmed"] == 1
            observed.append(_observed(case[0]))
            shadows.append(_shadow_rows(case[0]))
            if shadow is not None:
                control_request = case[4].requests[0]
        finally:
            case[0].close()
    assert observed[0] == observed[1]
    assert shadows == [0, 1]
    assert not _is_shadow(control_request)


def test_the_shadow_is_bought_once_per_lens_checkpoint(shadow_case: Any) -> None:
    _poll(shadow_case, ExaTransport())
    conn, _, _, _, model = shadow_case
    calls = model.calls
    _poll(shadow_case, ExaTransport())
    assert model.calls == calls
    assert _shadow_rows(conn) == 1 and len(_shadow_events(conn)) == 1


# --- a shadow failure never affects the control ----------------------------------------------


def _assert_control_unaffected(case: Any, result: dict[str, Any]) -> dict[str, Any]:
    conn, _, platform, _, _ = case
    assert result["forecast_confirmed"] == 1 and result["heartbeat"]["failures"] == 0
    assert platform.posts == 1
    assert conn.execute("SELECT count(*) FROM forecast_records").fetchone()[0] == 1
    assert _shadow_rows(conn) == 0
    (checkpoint,) = _shadow_events(conn)
    return checkpoint


def test_a_failed_shadow_model_call_is_recorded_and_the_control_stands(tmp_path: Path) -> None:
    case = _build(tmp_path, enabled=True, shadow=SHADOW_ON, model=FailingShadowModel)
    try:
        checkpoint = _assert_control_unaffected(case, _poll(case, ExaTransport()))
        assert case[4].shadow_calls >= 1, "positive control: the shadow call was made"
        assert checkpoint.get("error_type") or checkpoint["outcome"] == "generation_failed"
    finally:
        case[0].close()


def test_a_missing_shadow_prompt_is_recorded_and_the_control_stands(tmp_path: Path) -> None:
    shadow = dict(SHADOW_ON, prompt_path=str(tmp_path / "absent.md"))
    case = _build(tmp_path, enabled=True, shadow=shadow)
    try:
        checkpoint = _assert_control_unaffected(case, _poll(case, ExaTransport()))
        assert checkpoint["error_type"] == "ShadowForecastError"
        assert case[4].calls == 1, "nothing was bought for a shadow that could not start"
    finally:
        case[0].close()


def test_the_shadow_time_bound_is_recorded_and_the_control_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(whiskeyjack_tournament, "SHADOW_TIMEOUT_SECONDS", 0.5)
    case = _build(tmp_path, enabled=True, shadow=SHADOW_ON, model=SlowShadowModel)
    try:
        checkpoint = _assert_control_unaffected(case, _poll(case, ExaTransport()))
        assert checkpoint["error_type"] == "TournamentError"
    finally:
        case[0].close()


def test_a_refused_shadow_reservation_is_recorded_and_the_control_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget saying no -- the round or series ceiling reached by the shadow's estimate."""
    from whiskeyjack_bot.tournament_state import Budget

    real = Budget.reserve
    reserved: list[str] = []

    def reserve(self: Budget, provider: str, estimate: float, request: Any) -> str:
        # The shadow's request carries the lens prompt as its system message.
        if provider == "openrouter" and "local-source lens arm" in json.dumps(request):
            raise TournamentError("round budget exhausted; no provider call made")
        reserved.append(provider)
        return real(self, provider, estimate, request)

    monkeypatch.setattr(Budget, "reserve", reserve)
    case = _build(tmp_path, enabled=True, shadow=SHADOW_ON)
    try:
        conn, config, platform, news, _ = case
        web = httpx.Client(
            base_url="https://api.exa.ai", transport=httpx.MockTransport(ExaTransport())
        )
        _install_openrouter(monkeypatch, config, _OpenRouter(reply_for(_question(case))))
        result = whiskeyjack_tournament.run_once(
            conn,
            config,
            client=platform,
            poster=platform,
            news_client=news,
            web_client=web,
            forecaster=PricedClient(config),
        )
        checkpoint = _assert_control_unaffected(case, result)
        assert checkpoint["error_type"] == "TournamentError"
        assert "openrouter" in reserved, "positive control: the control's call was reserved"
    finally:
        case[0].close()


def test_a_ledger_refusal_of_the_row_is_recorded_and_the_control_stands(
    shadow_case: Any,
) -> None:
    """The ledger refusing the insert, as a full disk or a lock would: simulated by a trigger,
    so the refusal is SQLite's own and arrives the way a real one does."""
    conn = shadow_case[0]
    conn.execute(
        "CREATE TEMP TRIGGER refuse_shadow BEFORE INSERT ON shadow_forecasts "
        "BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )
    checkpoint = _assert_control_unaffected(shadow_case, _poll(shadow_case, ExaTransport()))
    assert checkpoint["error_type"] == "ShadowForecastError"
    assert shadow_case[4].calls == 2, "positive control: the shadow call was made"


# --- its cost reserves and settles under the active budget -----------------------------------


CONTROL_COST, SHADOW_COST = 0.0123, 0.0321


class _OpenRouter:
    """A fake OpenRouter: a valid reply for whichever arm asked, priced per arm."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.posts: list[bool] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user = json.loads(body["messages"][-1]["content"])
        shadow = _is_shadow(user)
        self.posts.append(shadow)
        reply = json.loads(self.reply)
        reply["as_of_utc"] = user["as_of_utc"]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(reply)}}],
                "usage": {"cost": SHADOW_COST if shadow else CONTROL_COST},
            },
        )


def _install_openrouter(
    monkeypatch: pytest.MonkeyPatch, config: Any, endpoint: _OpenRouter
) -> None:
    original = httpx.AsyncClient
    monkeypatch.setenv(config.model.api_key_env, "test-secret")
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(endpoint), **kw)
    )


def test_the_shadow_call_reserves_and_settles_under_the_active_budget(
    shadow_case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, config, platform, news, _ = shadow_case
    endpoint = _OpenRouter(reply_for(_question(shadow_case)))
    _install_openrouter(monkeypatch, config, endpoint)
    web = httpx.Client(base_url="https://api.exa.ai", transport=httpx.MockTransport(ExaTransport()))
    result = whiskeyjack_tournament.run_once(
        conn,
        config,
        client=platform,
        poster=platform,
        news_client=news,
        web_client=web,
        forecaster=PricedClient(config),
    )
    assert result["forecast_confirmed"] == 1 and platform.posts == 1
    assert endpoint.posts == [False, True], "one billed call per arm, the control's first"

    (shadow,) = read_shadow_forecasts(conn)
    assert shadow.cost_usd == SHADOW_COST and shadow.model_invocations == 1
    scope = conn.execute(
        "SELECT DISTINCT scope FROM tournament_events WHERE kind = 'cost_reserved'"
    ).fetchall()
    reserved = [e for (s,) in scope for e in events(conn, "cost_reserved", s)]
    settled = [e for (s,) in scope for e in events(conn, "cost_settled", s)]
    model_reservations = {e["reservation_id"] for e in reserved if e["provider"] == "openrouter"}
    assert len(model_reservations) == 2
    actual = {
        e["reservation_id"]: e["actual_microusd"]
        for e in settled
        if e["reservation_id"] in model_reservations
    }
    assert sorted(actual.values()) == [
        round(CONTROL_COST * 1_000_000),
        round(SHADOW_COST * 1_000_000),
    ], "both arms settled at what they billed, under the activation's budget scope"


# --- no code path can approve, reserve or submit it ------------------------------------------


@pytest.fixture
def written(shadow_case: Any) -> tuple[sqlite3.Connection, str, str, Any]:
    _poll(shadow_case, ExaTransport())
    conn, config = shadow_case[0], shadow_case[1]
    (shadow,) = read_shadow_forecasts(conn)
    return conn, shadow.shadow_forecast_id, shadow.control_record_id, config


def test_approval_cannot_name_a_shadow_forecast(written: Any) -> None:
    conn, shadow_id, _, config = written
    with pytest.raises(ApprovalError):
        approve(
            conn,
            record_id=shadow_id,
            actor="policy:test",
            occurred_at=_now(),
            calibration=config.numeric_calibration,
        )


def test_no_idempotency_key_can_be_derived_for_a_shadow_forecast(written: Any) -> None:
    conn, shadow_id, control_id, _ = written
    assert submission_key_for_record(conn, control_id, request_payload_sha256="a" * 64)
    with pytest.raises(SubmissionError):
        submission_key_for_record(conn, shadow_id, request_payload_sha256="a" * 64)


@pytest.mark.parametrize(
    ("table", "statement"),
    [
        (
            "approval_events",
            "INSERT INTO approval_events (forecast_record_id, decision, actor, "
            "forecast_sha256, created_at_utc, payload_sha256) VALUES (?, 'approved', 'x', ?, "
            "'2026-01-01T00:00:00+00:00', ?)",
        ),
        (
            "lifecycle_events",
            "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
            "from_status, to_status, occurred_at_utc, created_at_utc) VALUES (?, 99, "
            "'validated', 'draft', 'validated', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00') -- ? ?",
        ),
        (
            "submission_key_reservations",
            "INSERT INTO submission_key_reservations (reservation_id, forecast_record_id, "
            "idempotency_key, reservation_seq, reserved_at_utc, created_at_utc) VALUES "
            "('wjres-shadow', ?, ?, 1, '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00') -- ?",
        ),
    ],
)
def test_the_schema_refuses_every_downstream_row_for_a_shadow_id(
    written: Any, table: str, statement: str
) -> None:
    conn, shadow_id, _, _ = written
    sql = statement.split(" -- ")[0]
    params = (shadow_id, "b" * 64, "c" * 64)[: sql.count("?")]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)


def test_a_shadow_id_can_never_become_a_forecast_record(written: Any) -> None:
    """020's ``forecast_records_refuse_shadow_id``, against an insert that is otherwise valid:
    the same row under a fresh id is accepted, so the trigger is the only thing refusing."""
    conn, shadow_id, control_id, _ = written
    columns = [row[1] for row in conn.execute("PRAGMA table_info(forecast_records)")]
    (row,) = conn.execute(
        "SELECT * FROM forecast_records WHERE record_id = ?", (control_id,)
    ).fetchall()
    values = dict(zip(columns, row, strict=True))
    values.update(
        status="draft", forecast_version=values["forecast_version"] + 1, parent_record_id=control_id
    )
    insert = (
        f"INSERT INTO forecast_records ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})"
    )
    with pytest.raises(sqlite3.IntegrityError, match="shadow forecast id"):
        conn.execute(insert, [shadow_id if c == "record_id" else values[c] for c in columns])
    values["record_id"] = "fresh-record-id"
    values["attempt_id"] = "att-fresh-record-id"
    conn.execute(insert, [values[c] for c in columns])


def test_the_shadow_module_loads_nothing_that_approves_or_posts() -> None:
    """Out of process, so what the test suite has already imported cannot mask it."""
    posting = {
        "whiskeyjack_bot.approval",
        "whiskeyjack_bot.submission",
        "whiskeyjack_bot.submission_gateway",
        "whiskeyjack_bot.submission_live",
        "whiskeyjack_bot.submission_payload",
        "whiskeyjack_bot.submission_policy",
        "whiskeyjack_bot.metaculus.client",
    }
    shadow = _imported_by("import whiskeyjack_bot.forecast.shadow")
    assert "whiskeyjack_bot.forecast.shadow" in shadow
    assert not shadow & posting
    # Anti-vacuity: the same probe sees a posting module when one is imported.
    assert "whiskeyjack_bot.approval" in _imported_by("import whiskeyjack_bot.approval")


def _imported_by(statement: str) -> set[str]:
    code = (
        "import sys, json\n"
        "before = set(sys.modules)\n"
        f"{statement}\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT
    )
    return set(json.loads(completed.stdout.strip().splitlines()[-1]))


# --- the table is append-only and checks the pairing it claims -------------------------------


def test_a_shadow_row_can_be_neither_updated_nor_deleted(written: Any) -> None:
    conn, shadow_id, _, _ = written
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE shadow_forecasts SET raw_output_path = 'x' WHERE shadow_forecast_id = ?",
            (shadow_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM shadow_forecasts WHERE shadow_forecast_id = ?", (shadow_id,))


def _row(conn: sqlite3.Connection, shadow_id: str) -> dict[str, Any]:
    columns = [row[1] for row in conn.execute("PRAGMA table_info(shadow_forecasts)")]
    (row,) = conn.execute(
        "SELECT * FROM shadow_forecasts WHERE shadow_forecast_id = ?", (shadow_id,)
    ).fetchall()
    return dict(zip(columns, row, strict=True))


def _insert(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    columns = list(values)
    conn.execute(
        f"INSERT INTO shadow_forecasts ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        [values[c] for c in columns],
    )


def _reidentified(values: dict[str, Any], new_id: str, checkpoint: str) -> dict[str, Any]:
    """``values`` as a second, well-formed row: a new id (inside record_json too) and a new
    lens checkpoint, so only the clause a test breaks can refuse it."""
    record = json.loads(values["record_json"])
    record["record_id"] = new_id
    return dict(
        values,
        shadow_forecast_id=new_id,
        local_lens_checkpoint_id=checkpoint,
        record_json=json.dumps(record),
    )


def _second_checkpoint(conn: sqlite3.Connection) -> str:
    conn.execute(
        "INSERT INTO tournament_events (event_id, kind, scope, data, created_at_utc) "
        "VALUES ('lens-2', 'local_lens_checkpoint', 'fp', '{}', '2026-01-01T00:00:00+00:00')"
    )
    return "lens-2"


def _break(values: dict[str, Any], clause: str, conn: sqlite3.Connection) -> dict[str, Any]:
    record = json.loads(values["record_json"])
    if clause == "control_missing":
        return dict(values, control_record_id="no-such-record")
    if clause == "control_other_question":
        # The record agrees with the row, so only the pairing clause can refuse it.
        record["question_id"] = values["question_id"] + 1
        return dict(values, question_id=values["question_id"] + 1, record_json=json.dumps(record))
    if clause == "checkpoint_not_lens":
        conn.execute(
            "INSERT INTO tournament_events (event_id, kind, scope, data, created_at_utc) "
            "VALUES ('hb-1', 'heartbeat', 'worker', '{}', '2026-01-01T00:00:00+00:00')"
        )
        return dict(values, local_lens_checkpoint_id="hb-1")
    if clause == "record_id_mismatch":
        record["record_id"] = "someone-else"
    elif clause == "record_question_mismatch":
        record["question_id"] = values["question_id"] + 1
    elif clause == "record_tournament_mismatch":
        record["tournament_id"] = "another-tournament"
    elif clause == "record_not_json":
        return dict(values, record_json="{not json")
    elif clause == "runs_not_array":
        return dict(values, retrieval_run_ids_json='{"run": 1}')
    elif clause == "packet_mismatch":
        record["research_packet_sha256"] = "d" * 64
    elif clause == "prompt_mismatch":
        record["model_settings"]["prompt_sha256"] = "e" * 64
    elif clause == "runs_empty":
        return dict(values, retrieval_run_ids_json="[]")
    elif clause == "invocations_zero":
        return dict(values, model_invocations=0)
    elif clause == "id_is_a_record":
        return dict(values, shadow_forecast_id=values["control_record_id"])
    return dict(values, record_json=json.dumps(record))


@pytest.mark.parametrize(
    "clause",
    [
        "control_missing",
        "control_other_question",
        "checkpoint_not_lens",
        "record_id_mismatch",
        "record_question_mismatch",
        "record_tournament_mismatch",
        "record_not_json",
        "runs_not_array",
        "packet_mismatch",
        "prompt_mismatch",
        "runs_empty",
        "invocations_zero",
        "id_is_a_record",
    ],
)
def test_the_insert_trigger_refuses_a_row_that_misdescribes_its_pairing(
    written: Any, clause: str
) -> None:
    conn, shadow_id, _, _ = written
    good = _reidentified(_row(conn, shadow_id), "shadow-2", _second_checkpoint(conn))
    bad = _break(good, clause, conn)
    if clause == "id_is_a_record":
        record = json.loads(bad["record_json"])
        record["record_id"] = bad["shadow_forecast_id"]
        bad["record_json"] = json.dumps(record)
    with pytest.raises(sqlite3.IntegrityError, match="shadow_forecasts"):
        _insert(conn, bad)
    _insert(conn, good)  # positive control: the well-formed twin is accepted


def test_one_shadow_per_lens_checkpoint(written: Any) -> None:
    conn, shadow_id, _, _ = written
    values = _row(conn, shadow_id)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, _reidentified(values, "shadow-3", values["local_lens_checkpoint_id"]))


# --- run_shadow_forecast's own decisions -----------------------------------------------------


def test_a_lens_checkpoint_with_no_retained_document_buys_nothing(shadow_case: Any) -> None:
    conn, config, _, _, model = shadow_case
    _poll(shadow_case, ExaTransport())
    (lens,) = _lens_events(conn)
    calls = model.calls
    empty = dict(lens, calls=[dict(call, documents=0) for call in lens["calls"]])
    control_id, _ = _control(conn)
    result = run_shadow_forecast(
        conn,
        config,
        question=_question(shadow_case),
        tournament_id="32977",
        now=_now(),
        control_record_id=control_id,
        lens_checkpoint_id=_lens_event_id(conn),
        lens_checkpoint=empty,
        forecaster=model,
    )
    assert result.outcome == "no_lens_evidence"
    assert model.calls == calls


@pytest.mark.parametrize(
    ("call", "kept"),
    [
        ({"retrieval_run_id": "r", "documents": 1, "interrupted": False, "skipped": None}, True),
        ({"retrieval_run_id": "r", "documents": 0, "interrupted": False, "skipped": None}, False),
        ({"retrieval_run_id": "r", "documents": 1, "interrupted": True, "skipped": None}, False),
        (
            {
                "retrieval_run_id": "r",
                "documents": 1,
                "interrupted": False,
                "skipped": "no_web_client",
            },
            False,
        ),
        ({"retrieval_run_id": None, "documents": 1, "interrupted": False, "skipped": None}, False),
        (
            {"retrieval_run_id": "r", "documents": True, "interrupted": False, "skipped": None},
            False,
        ),
        ({"retrieval_run_id": "r", "documents": 1}, False),
        ("not a call", False),
    ],
)
def test_only_completed_lens_runs_that_retained_a_document_are_read(call: Any, kept: bool) -> None:
    assert lens_run_ids({"calls": [call]}) == (("r",) if kept else ())


@pytest.mark.parametrize("baseline", [None, [], ["ok", 3], "run"])
def test_a_checkpoint_naming_no_baseline_is_this_module_s_error(
    shadow_case: Any, baseline: Any
) -> None:
    conn, config, _, _, model = shadow_case
    with pytest.raises(ShadowForecastError):
        run_shadow_forecast(
            conn,
            config,
            question=_question(shadow_case),
            tournament_id="32977",
            now=_now(),
            control_record_id="x",
            lens_checkpoint_id="y",
            lens_checkpoint={"baseline_run_ids": baseline, "calls": []},
            forecaster=model,
        )


def _now() -> Any:
    from whiskeyjack_bot.tournament_state import utcnow

    return utcnow()
