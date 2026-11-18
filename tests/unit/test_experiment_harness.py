"""M5-806: the replay experiment harness, end to end and criterion by criterion.

Every run here goes through the production ``PricedClient`` behind an ``httpx.MockTransport``
that counts what reached it, because spend is only measurable at the transport (M1-351: a fake
``Forecaster`` bypasses the priced client's reservation, cache and guard). The control ledger is
built by a real worker pass (``experiment_case.build_live``), so the fidelity gate reads exactly
what the worker writes.
"""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from experiment_case import ASTRA, LiveCase, OpenRouter, arm_spec, build_live, install

from tests.unit.test_pipeline_live import reply_for
from whiskeyjack_bot import cli
from whiskeyjack_bot.experiment import fidelity, runner
from whiskeyjack_bot.experiment.arm_store import (
    EXPERIMENT_STORE_SCHEMA_VERSION,
    open_store,
    read_arm_samples,
    store_lock,
)
from whiskeyjack_bot.experiment.errors import ExperimentError
from whiskeyjack_bot.experiment.fidelity import (
    check_record,
    journal_index,
    leakage_reason,
    prompt_archive,
)
from whiskeyjack_bot.experiment.runner import (
    RunReport,
    build_arm_client,
    check_arm,
    prepare_arm,
    run_arm,
    store_files_dir,
)
from whiskeyjack_bot.experiment.spec import LoadedArmSpec, load_arm_spec
from whiskeyjack_bot.forecast.inputs import ForecastInputError
from whiskeyjack_bot.forecast.priced import MODEL_IN_SERVICE
from whiskeyjack_bot.forecast.record import record_sha256
from whiskeyjack_bot.forecast.store import read_forecast_record
from whiskeyjack_bot.ledger import connect, connect_readonly
from whiskeyjack_bot.research.packet import packet_sha256
from whiskeyjack_bot.tournament_state import StorageFailure, append, check_storage, events

ROOT = Path(__file__).resolve().parents[2]


# --- fixtures -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> LiveCase:
    return build_live(tmp_path_factory.mktemp("experiment-template") / "live")


@pytest.fixture
def case(template: LiveCase, tmp_path: Path) -> LiveCase:
    return template.copy_to(tmp_path / "live")


@pytest.fixture
def live(case: LiveCase) -> Iterator[sqlite3.Connection]:
    conn = connect_readonly(case.ledger)
    yield conn
    conn.close()


def _store(tmp_path: Path) -> Path:
    return tmp_path / "experiments" / "arms.sqlite3"


def _loaded(case: LiveCase, tmp_path: Path, **overrides: Any) -> LoadedArmSpec:
    return load_arm_spec(arm_spec(case, tmp_path / "specs", **overrides))


def _run(
    case: LiveCase,
    live: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    endpoint: OpenRouter | None = None,
    ceiling_usd: int = 5,
    **overrides: Any,
) -> tuple[RunReport, Path, OpenRouter]:
    endpoint = endpoint or OpenRouter(reply_for(case.question))
    install(monkeypatch, endpoint, case.config.model.api_key_env)
    store = _store(tmp_path)
    prepared = prepare_arm(
        _loaded(case, tmp_path, **overrides), case.config, files_root=store_files_dir(store)
    )
    report = run_arm(
        prepared=prepared,
        client=build_arm_client(prepared),
        live=live,
        live_path=case.ledger,
        store_path=store,
        ceiling_usd=ceiling_usd,
        prompt_dirs=[case.prompt.parent],
    )
    return report, store, endpoint


def _samples(store: Path, arm: str) -> tuple[Any, ...]:
    conn = open_store(store)
    try:
        return read_arm_samples(conn, arm)
    finally:
        conn.close()


def _fingerprint(path: Path) -> dict[str, str]:
    """The ledger's content: the database and any WAL frames.

    An *empty* ``-wal`` is left out: ``connect_readonly`` documents that SQLite creates one (and
    a ``-shm``) beside a cleanly checkpointed ledger. It holds no frame, so it is lock state,
    not content; a WAL with frames in it is content and is compared byte for byte.
    """
    wal = path.with_name(path.name + "-wal")
    found = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
    if wal.exists() and wal.stat().st_size:
        found[wal.name] = hashlib.sha256(wal.read_bytes()).hexdigest()
    return found


# --- AC2: what every row carries -----------------------------------------------------------


def test_every_row_carries_the_arm_the_control_and_the_spend(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, store, endpoint = _run(case, live, tmp_path, monkeypatch, samples=2)
    assert report.stop_reason == "complete" and report.forecasts == 2 and endpoint.posts == 2

    control = read_forecast_record(live, case.record_id)
    rows = _samples(store, report.arm_sha256)
    assert [row.sample_index for row in rows] == [0, 1]
    for row in rows:
        assert row.arm_sha256 == report.arm_sha256
        assert row.control_record_id == case.record_id
        assert row.control_forecast_sha256 == record_sha256(control)
        assert row.outcome == "forecast" and row.record is not None
        assert row.record.record_id != case.record_id
        assert row.record.model_settings.name == ASTRA
        assert row.record.research_packet_sha256 == control.research_packet_sha256
        # The identity arm keeps every document, so the kept packet is the control's packet.
        assert row.kept_packet_sha256 == control.research_packet_sha256
        assert row.billed_calls == 1 and row.held_microusd == 0
        assert row.cost_microusd == round(endpoint.cost * 1_000_000)
        assert row.model_invocations == 1
        assert row.reported_models == (endpoint.model,)
        assert row.reasoning_effort_sent == "high"
        assert row.raw_output_path is not None
        assert (store_files_dir(store) / row.raw_output_path).is_file()
    # The request hash is the sha256 of exactly what was posted as the user message.
    posted = endpoint.bodies[0]["messages"][1]["content"]
    assert rows[0].request_sha256 == hashlib.sha256(posted.encode("ascii")).hexdigest()


def test_the_arm_runs_at_its_own_effort_and_model_not_the_controls(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, endpoint = _run(case, live, tmp_path, monkeypatch, reasoning_effort="low")
    (body,) = endpoint.bodies
    assert body["reasoning"] == {"effort": "low", "exclude": True}
    assert body["model"] == "openai/gpt-6-astra"


# --- AC5: spend ------------------------------------------------------------------------------


def test_distinct_samples_of_one_request_are_distinct_purchases(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, store, endpoint = _run(case, live, tmp_path, monkeypatch, samples=3)
    assert endpoint.posts == 3, "each sample bought, none served from another's cache"
    assert len({json.dumps(body, sort_keys=True) for body in endpoint.bodies}) == 1
    assert report.arm_spent_microusd == 3 * round(endpoint.cost * 1_000_000)


def test_a_rerun_skips_every_completed_sample_and_buys_nothing(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, store, endpoint = _run(case, live, tmp_path, monkeypatch, samples=3)
    second, _, again = _run(case, live, tmp_path, monkeypatch, samples=3)
    assert again.posts == 0
    assert second.skipped_existing == 3 and second.forecasts == 0
    assert len(_samples(store, first.arm_sha256)) == 3


def test_a_purchase_whose_row_was_lost_is_replayed_free(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the call and before the row: the next run writes the row, buying nothing."""
    original = runner.append_arm_sample

    def crash(*args: Any, **kwargs: Any) -> None:
        raise ExperimentError("simulated crash before the row")

    monkeypatch.setattr(runner, "append_arm_sample", crash)
    with pytest.raises(ExperimentError, match="simulated crash"):
        _run(case, live, tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "append_arm_sample", original)

    endpoint = OpenRouter(reply_for(case.question))
    report, store, _ = _run(case, live, tmp_path, monkeypatch, endpoint=endpoint)
    assert endpoint.posts == 0, "the bought reply is replayed from the journal"
    (row,) = _samples(store, report.arm_sha256)
    assert row.outcome == "forecast" and row.billed_calls == 1


def test_the_ceiling_stops_the_run_cleanly_and_is_cumulative_over_runs(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reply billed at USD 0.9 under a USD 1 ceiling: the first sample fits, the second's
    # reservation (held at its full estimate) cannot.
    pricey = OpenRouter(reply_for(case.question), cost=0.9)
    first, store, _ = _run(
        case, live, tmp_path, monkeypatch, endpoint=pricey, ceiling_usd=1, samples=3
    )
    assert first.stop_reason == "ceiling" and first.forecasts == 1 and pricey.posts == 1

    again = OpenRouter(reply_for(case.question), cost=0.9)
    second, _, _ = _run(case, live, tmp_path, monkeypatch, endpoint=again, ceiling_usd=1, samples=3)
    assert second.stop_reason == "ceiling" and again.posts == 0, "the arm's spend carries over"

    raised = OpenRouter(reply_for(case.question), cost=0.9)
    third, _, _ = _run(case, live, tmp_path, monkeypatch, endpoint=raised, ceiling_usd=3, samples=3)
    assert third.stop_reason == "complete" and raised.posts == 2, third
    assert len(_samples(store, first.arm_sha256)) == 3


def test_a_ceiling_above_the_code_maximum_is_refused(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ExperimentError, match="ceiling"):
        _run(case, live, tmp_path, monkeypatch, ceiling_usd=401)
    assert not _store(tmp_path).exists()


def test_a_concurrent_run_is_refused_by_the_store_lock(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with store_lock(_store(tmp_path)), pytest.raises(ExperimentError, match="lock"):
        _run(case, live, tmp_path, monkeypatch)


def test_a_missing_credential_exits_3_before_anything_is_written(
    case: LiveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(case.config.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(cli, "_load_verified_config", lambda _path: case.config)
    monkeypatch.delenv(case.config.model.api_key_env, raising=False)
    code = cli.main(
        [
            "experiment",
            "run",
            "--spec",
            str(arm_spec(case, tmp_path / "specs")),
            "--ledger",
            str(case.ledger),
            "--store",
            str(_store(tmp_path)),
            "--config",
            str(config_path),
            "--ceiling-usd",
            "5",
        ]
    )
    assert code == 3
    assert case.config.model.api_key_env in capsys.readouterr().out
    assert not _store(tmp_path).exists()


# --- AC1: the live ledger is read-only; the store is append-only -----------------------------


def test_the_live_ledger_is_never_opened_for_writing(
    case: LiveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    before = _fingerprint(case.ledger)
    opened: list[str] = []
    real_connect = sqlite3.connect

    def spy(target: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(target))
        return real_connect(target, *args, **kwargs)

    endpoint = OpenRouter(reply_for(case.question))
    install(monkeypatch, endpoint, case.config.model.api_key_env)
    monkeypatch.setattr(cli, "_load_verified_config", lambda _path: case.config)
    monkeypatch.setattr(sqlite3, "connect", spy)
    code = cli.main(
        [
            "experiment",
            "run",
            "--spec",
            str(arm_spec(case, tmp_path / "specs", samples=2)),
            "--ledger",
            str(case.ledger),
            "--store",
            str(_store(tmp_path)),
            "--config",
            str(tmp_path / "unused.yaml"),
            "--ceiling-usd",
            "5",
        ]
    )
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    assert code == 0, capsys.readouterr().out
    assert endpoint.posts == 2
    live_opens = [target for target in opened if case.ledger.name in target]
    assert live_opens, "the probe saw the live ledger opened"
    assert all(target.endswith("?mode=ro") for target in live_opens), live_opens
    assert _fingerprint(case.ledger) == before, "the live database and its WAL are byte-identical"

    # The live worker's own restore check still passes: no witness reached its guard directory.
    conn = connect(case.ledger)
    try:
        check_storage(conn, case.config.storage.artifact_root)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE arm_samples SET billed_calls = 9",
        "DELETE FROM arm_samples",
        "UPDATE arms SET created_at_utc = 'x'",
        "DELETE FROM arms",
        "UPDATE runs SET ceiling_microusd = 1",
        "DELETE FROM runs",
        "DELETE FROM run_reports",
        "DELETE FROM fidelity_outcomes",
        "UPDATE tournament_events SET data = '{}'",
        "DELETE FROM tournament_events",
        "DELETE FROM experiment_schema",
    ],
)
def test_the_store_refuses_update_and_delete(
    case: LiveCase,
    live: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    statement: str,
) -> None:
    _, store, _ = _run(case, live, tmp_path, monkeypatch)
    conn = open_store(store)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(statement)
    finally:
        conn.close()


def test_the_store_has_its_own_schema_version(tmp_path: Path) -> None:
    conn = open_store(_store(tmp_path))
    try:
        assert [tuple(row) for row in conn.execute("SELECT version FROM experiment_schema")] == [
            (EXPERIMENT_STORE_SCHEMA_VERSION,)
        ]
        assert not {"schema_migrations", "forecast_records"} & {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def test_a_ledger_is_refused_as_a_store_without_being_opened_for_writing(
    case: LiveCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused by a read-only look, before ``ledger.connect`` could set a pragma on it.

    The bytes alone cannot show this: a read-write open of a ledger already in WAL mode leaves
    the file unchanged. So every open of the path is recorded too, and each must be read-only.
    """
    before = _fingerprint(case.ledger)
    opened: list[str] = []
    real_connect = sqlite3.connect

    def spy(target: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    with pytest.raises(ExperimentError, match="is a ledger"):
        open_store(case.ledger)
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    assert opened and all(target.endswith("?mode=ro") for target in opened), opened
    assert _fingerprint(case.ledger) == before


@pytest.mark.parametrize("where", ["beside_ledger", "same_file", "artifact_root"])
def test_a_store_where_its_witnesses_would_reach_the_live_guard_is_refused(
    case: LiveCase,
    live: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    store = {
        "beside_ledger": case.ledger.with_name("arms.sqlite3"),
        "same_file": case.ledger,
        "artifact_root": case.config.storage.artifact_root / "arms.sqlite3",
    }[where]
    endpoint = OpenRouter(reply_for(case.question))
    install(monkeypatch, endpoint, case.config.model.api_key_env)
    prepared = prepare_arm(_loaded(case, tmp_path), case.config, files_root=store_files_dir(store))
    with pytest.raises(ExperimentError, match="must not"):
        run_arm(
            prepared=prepared,
            client=build_arm_client(prepared),
            live=live,
            live_path=case.ledger,
            store_path=store,
            ceiling_usd=5,
            prompt_dirs=[case.prompt.parent],
        )
    assert endpoint.posts == 0


def test_a_run_under_other_generation_settings_is_refused(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(case, live, tmp_path, monkeypatch)
    one_try = case.config.model.model_copy(update={"allowed_tries": 1})
    other = replace(case, config=case.config.model_copy(update={"model": one_try}))
    with pytest.raises(ExperimentError, match="settings differ"):
        _run(other, live, tmp_path, monkeypatch)


# --- AC3: the fidelity gate -------------------------------------------------------------------


def _verdict(case: LiveCase, live: sqlite3.Connection, **changes: Any) -> Any:
    arguments: dict[str, Any] = {
        "config": case.config,
        "prompts": prompt_archive([case.prompt.parent]),
        "journal": journal_index(live),
        "in_service_from": MODEL_IN_SERVICE[ASTRA],
    }
    arguments.update(changes)
    return check_record(live, case.record_id, **arguments)


def test_the_fixture_record_passes_the_gate_with_its_measured_effort(
    case: LiveCase, live: sqlite3.Connection
) -> None:
    verdict = _verdict(case, live)
    assert verdict.reason is None and verdict.control is not None
    assert verdict.control.control_effort == "high"
    record = read_forecast_record(live, case.record_id)
    assert packet_sha256(verdict.control.packet) == record.research_packet_sha256


def test_a_one_byte_change_to_the_stored_request_excludes_the_record(
    case: LiveCase, live: sqlite3.Connection
) -> None:
    (path,) = live.execute(
        "SELECT raw_output_path FROM forecast_records WHERE record_id = ?", (case.record_id,)
    ).fetchone()
    artifact = case.config.storage.artifact_root / path
    envelope = json.loads(artifact.read_text())
    request = envelope["request"]
    position = request.index('"question_text":"') + len('"question_text":"')
    envelope["request"] = request[:position] + "X" + request[position + 1 :]
    artifact.write_text(json.dumps(envelope))
    assert _verdict(case, live).reason == "request_mismatch"


def test_a_missing_artifact_excludes_the_record(case: LiveCase, live: sqlite3.Connection) -> None:
    (path,) = live.execute(
        "SELECT raw_output_path FROM forecast_records WHERE record_id = ?", (case.record_id,)
    ).fetchone()
    (case.config.storage.artifact_root / path).unlink()
    assert _verdict(case, live).reason == "artifact_unreadable"


def test_an_artifact_for_another_attempt_excludes_the_record(
    case: LiveCase, live: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = fidelity.read_raw_model_output
    monkeypatch.setattr(
        fidelity,
        "read_raw_model_output",
        lambda root, path: replace(real(root, path), attempt_id="another-attempt"),
    )
    assert _verdict(case, live).reason == "artifact_mismatch"


def test_another_freshness_window_cannot_reproduce_the_packet(
    case: LiveCase, live: sqlite3.Connection
) -> None:
    retrieval = case.config.retrieval.model_copy(update={"freshness_days_default": 0})
    config = case.config.model_copy(update={"retrieval": retrieval})
    assert _verdict(case, live, config=config).reason == "packet_hash_mismatch"


def test_a_record_with_no_checkpoint_is_excluded(
    case: LiveCase, live: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record made outside the tournament path has no research_checkpoint (simulated)."""
    real = fidelity.events
    monkeypatch.setattr(
        fidelity,
        "events",
        lambda conn, kind, scope: [] if kind == "research_checkpoint" else real(conn, kind, scope),
    )
    assert _verdict(case, live).reason == "no_checkpoint"


def test_two_checkpoints_that_both_reproduce_the_packet_are_ambiguous(
    case: LiveCase, live: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = fidelity._checkpoints
    monkeypatch.setattr(fidelity, "_checkpoints", lambda conn, record: real(conn, record) * 2)
    assert _verdict(case, live).reason == "checkpoint_ambiguous"


def test_a_prompt_the_archive_lacks_excludes_the_record(
    case: LiveCase, live: sqlite3.Connection
) -> None:
    assert _verdict(case, live, prompts={}).reason == "prompt_unavailable"


def test_a_call_absent_from_the_live_journal_is_not_reproduced(
    case: LiveCase, live: sqlite3.Connection
) -> None:
    assert _verdict(case, live, journal={}).reason == "call_not_reproduced"


def test_a_call_bought_under_another_project_is_not_reproduced(
    case: LiveCase, live: sqlite3.Connection
) -> None:
    moved = {key: tuple("42:99999" for _ in value) for key, value in journal_index(live).items()}
    assert _verdict(case, live, journal=moved).reason == "call_not_reproduced"


def test_an_effort_the_journal_contradicts_excludes_the_record(
    case: LiveCase, live: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fidelity, "_effort_recorded", lambda conn, scope: "low")
    assert _verdict(case, live).reason == "effort_disagrees"


def test_a_record_from_an_unpriced_model_is_excluded(
    case: LiveCase, live: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fidelity, "PRICED_MODELS", {})
    assert _verdict(case, live).reason == "model_unpriced"


def test_a_request_that_cannot_be_rebuilt_excludes_the_record(
    case: LiveCase, live: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(**kwargs: Any) -> Any:
        raise ForecastInputError("simulated")

    monkeypatch.setattr(fidelity, "build_model_input", refuse)
    assert _verdict(case, live).reason == "request_unbuildable"


def test_an_unknown_record_is_unreadable(case: LiveCase, live: sqlite3.Connection) -> None:
    verdict = check_record(
        live,
        "no-such-record",
        config=case.config,
        prompts={},
        journal={},
        in_service_from=MODEL_IN_SERVICE[ASTRA],
    )
    assert verdict.reason == "record_unreadable"


def test_the_run_reports_and_persists_its_exclusions(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fidelity, "PRICED_MODELS", {})
    report, store, endpoint = _run(case, live, tmp_path, monkeypatch)
    assert report.excluded == {"model_unpriced": 1} and report.passed_gate == 0
    assert endpoint.posts == 0
    conn = open_store(store)
    try:
        assert [
            tuple(row)
            for row in conn.execute("SELECT control_record_id, reason_code FROM fidelity_outcomes")
        ] == [(case.record_id, "model_unpriced")]
        (counts,) = conn.execute("SELECT counts_json FROM run_reports").fetchone()
        assert json.loads(counts)["excluded"] == {"model_unpriced": 1}
    finally:
        conn.close()


def test_a_dry_run_gates_without_a_store_or_a_client(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path
) -> None:
    prepared = prepare_arm(_loaded(case, tmp_path), case.config, files_root=None)
    report = check_arm(prepared=prepared, live=live, prompt_dirs=[case.prompt.parent])
    assert report.stop_reason == "dry_run" and report.selected == 1 and report.passed_gate == 1
    assert not _store(tmp_path).exists()


# --- AC6: leakage guards ----------------------------------------------------------------------


def test_a_question_that_opened_before_the_model_was_in_service_is_refused(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = read_forecast_record(live, case.record_id)
    assert record.question.open_time is not None
    later = record.question.open_time + timedelta(seconds=1)
    monkeypatch.setattr(runner, "MODEL_IN_SERVICE", {ASTRA: later})
    report, _, endpoint = _run(case, live, tmp_path, monkeypatch)
    assert report.excluded == {"model_not_in_service": 1} and endpoint.posts == 0
    # The boundary: opening exactly at the in-service instant is admitted.
    assert leakage_reason(record.question, record.question.open_time) is None


def test_a_question_with_no_open_time_is_refused(case: LiveCase, live: sqlite3.Connection) -> None:
    question = read_forecast_record(live, case.record_id).question
    assert leakage_reason(
        question.model_copy(update={"open_time": None}), MODEL_IN_SERVICE[ASTRA]
    ) == ("open_time_unknown")


def test_no_document_dated_after_the_controls_cutoff_reaches_the_model(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transform that admitted a later document (simulated) is refused before any spend."""
    real = runner.apply_transform

    def leaky(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        first = result.documents[0]
        late = first.rendered.model_copy(
            update={"retrieved_at": datetime.now(timezone.utc) + timedelta(days=30)}
        )
        return replace(result, documents=(replace(first, rendered=late), *result.documents[1:]))

    monkeypatch.setattr(runner, "apply_transform", leaky)
    report, _, endpoint = _run(case, live, tmp_path, monkeypatch)
    assert report.excluded == {"document_after_as_of": 1} and endpoint.posts == 0


# --- AC4 in the runner: transforms reach the model as specified -------------------------------


def test_no_research_sends_no_documents_and_keeps_the_controls_cutoff(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, store, endpoint = _run(
        case, live, tmp_path, monkeypatch, transform={"kind": "no_research"}
    )
    (body,) = endpoint.bodies
    user = json.loads(body["messages"][1]["content"])
    assert user["research_documents"] == []
    record = read_forecast_record(live, case.record_id)
    assert datetime.fromisoformat(user["as_of_utc"].replace("Z", "+00:00")) == (
        record.generated_at_utc
    )
    (row,) = _samples(store, report.arm_sha256)
    assert row.outcome == "forecast" and row.record is not None and row.record.sources == []
    assert row.kept_packet_sha256 != record.research_packet_sha256


def test_a_dropped_document_keeps_every_other_documents_source_id(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = _verdict(case, live).control
    full = [d.source_id for d in control.model_input.packet.research_documents]
    _, _, endpoint = _run(
        case,
        live,
        tmp_path,
        monkeypatch,
        transform={
            "kind": "random_subset",
            "seed": 7,
            "inclusion_probability": 0.5,
            "unit": "document",
        },
        samples=6,
    )
    shown = [
        [d["source_id"] for d in json.loads(body["messages"][1]["content"])["research_documents"]]
        for body in endpoint.bodies
    ]
    assert all(set(ids) <= set(full) for ids in shown)
    assert all(ids == [i for i in full if i in ids] for ids in shown), "numbering never shifts"
    assert len({tuple(ids) for ids in shown}) > 1, "the subsets differ across samples"


def test_a_spec_needing_an_unwired_labeller_is_refused_before_anything_runs(
    case: LiveCase, tmp_path: Path
) -> None:
    loaded = _loaded(
        case, tmp_path, transform={"kind": "collapse_syndication", "labeller": "m1-321"}
    )
    with pytest.raises(ExperimentError, match="M1-321"):
        prepare_arm(loaded, case.config, files_root=None)


# --- AC7: never submittable --------------------------------------------------------------------

POSTING = {
    "whiskeyjack_bot.approval",
    "whiskeyjack_bot.submission",
    "whiskeyjack_bot.submission_gateway",
    "whiskeyjack_bot.submission_live",
    "whiskeyjack_bot.submission_payload",
    "whiskeyjack_bot.submission_policy",
    "whiskeyjack_bot.submission_reconcile",
    "whiskeyjack_bot.metaculus.client",
    "whiskeyjack_bot.metaculus.poster",
    "whiskeyjack_bot.tournament",
    "whiskeyjack_bot.pipeline_live",
    "whiskeyjack_bot.restore",
    "whiskeyjack_bot.follow",
}

EXPERIMENT_MODULES = [
    "whiskeyjack_bot.experiment",
    "whiskeyjack_bot.experiment.errors",
    "whiskeyjack_bot.experiment.spec",
    "whiskeyjack_bot.experiment.transforms",
    "whiskeyjack_bot.experiment.fidelity",
    "whiskeyjack_bot.experiment.arm_store",
    "whiskeyjack_bot.experiment.runner",
]


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


@pytest.mark.parametrize("module", EXPERIMENT_MODULES)
def test_no_experiment_module_loads_anything_that_approves_or_posts(module: str) -> None:
    """Out of process, so what the suite has already imported cannot mask it."""
    loaded = _imported_by(f"import {module}")
    assert module in loaded
    assert not loaded & POSTING, sorted(loaded & POSTING)


def test_the_import_probe_sees_a_posting_module_when_one_is_imported() -> None:
    assert "whiskeyjack_bot.submission_policy" in _imported_by(
        "import whiskeyjack_bot.submission_policy"
    )


def test_the_store_holds_no_table_the_posting_path_reads(case: LiveCase, tmp_path: Path) -> None:
    conn = open_store(_store(tmp_path))
    live = connect_readonly(case.ledger)
    try:
        query = "SELECT name FROM sqlite_master WHERE type='table'"
        store_tables = {row[0] for row in conn.execute(query)}
        live_tables = {row[0] for row in live.execute(query)}
    finally:
        conn.close()
        live.close()
    # tournament_events is the priced client's spend journal (owner decision on AC7, 2026-11-17);
    # every other live table is absent.
    assert store_tables & live_tables == {"tournament_events"}


def test_the_store_journal_refuses_every_kind_but_spend_and_model_calls(tmp_path: Path) -> None:
    conn = open_store(_store(tmp_path))
    try:
        for kind in ("activation", "forecast_intent", "forecast_confirmed", "research_checkpoint"):
            with pytest.raises(StorageFailure):
                append(conn, kind, "scope", {})
        append(conn, "model_started", "scope", {"reservation_id": "r"})
        assert events(conn, "model_started", "scope") == [{"reservation_id": "r"}]
    finally:
        conn.close()


def test_the_store_journal_table_is_migration_012s(case: LiveCase, tmp_path: Path) -> None:
    conn = open_store(_store(tmp_path))
    live = connect_readonly(case.ledger)
    try:
        query = "SELECT sql FROM sqlite_master WHERE name = ?"
        for name in (
            "tournament_events",
            "tournament_events_scope",
            "tournament_events_no_update",
            "tournament_events_no_delete",
        ):
            mine = conn.execute(query, (name,)).fetchone()
            theirs = live.execute(query, (name,)).fetchone()
            assert mine is not None and theirs is not None and tuple(mine) == tuple(theirs), name
    finally:
        conn.close()
        live.close()


# --- AC8: a fake Forecaster opens no socket and spends nothing ----------------------------------


class _FakeForecaster:
    def __init__(self, reply: str) -> None:
        self.model = ASTRA
        self.reply = reply
        self.calls = 0

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        self.calls += 1
        user = json.loads(prompt[1]["content"])
        reply = json.loads(self.reply)
        reply["as_of_utc"] = user["as_of_utc"]
        return json.dumps(reply)


def test_a_fake_forecaster_run_opens_no_socket_and_spends_nothing(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connects: list[object] = []
    real_connect = socket.socket.connect

    def counting(self: socket.socket, address: Any) -> Any:
        connects.append(address)
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", counting)
    store = _store(tmp_path)
    prepared = prepare_arm(
        _loaded(case, tmp_path, samples=2), case.config, files_root=store_files_dir(store)
    )
    fake = _FakeForecaster(reply_for(case.question))
    report = run_arm(
        prepared=prepared,
        client=fake,
        live=live,
        live_path=case.ledger,
        store_path=store,
        ceiling_usd=5,
        prompt_dirs=[case.prompt.parent],
    )
    assert report.forecasts == 2 and fake.calls == 2
    assert connects == []
    conn = open_store(store)
    try:
        reserved = conn.execute(
            "SELECT count(*) FROM tournament_events WHERE kind = 'cost_reserved'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert reserved == 0 and report.arm_spent_microusd == 0
    assert all(row.billed_calls == 0 for row in _samples(store, report.arm_sha256))


def test_a_client_for_another_model_is_refused(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path
) -> None:
    store = _store(tmp_path)
    prepared = prepare_arm(_loaded(case, tmp_path), case.config, files_root=store_files_dir(store))
    fake = _FakeForecaster(reply_for(case.question))
    fake.model = "openrouter/openai/gpt-5.6-sol"
    with pytest.raises(ExperimentError, match="model"):
        run_arm(
            prepared=prepared,
            client=fake,
            live=live,
            live_path=case.ledger,
            store_path=store,
            ceiling_usd=5,
            prompt_dirs=[case.prompt.parent],
        )


# --- a generation failure and an unknown outcome are recorded, not retried ---------------------


def test_a_reply_that_fails_both_tries_is_a_generation_failed_row(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = OpenRouter("not json", first_reply="not json")
    report, store, _ = _run(case, live, tmp_path, monkeypatch, endpoint=endpoint)
    (row,) = _samples(store, report.arm_sha256)
    assert row.outcome == "generation_failed" and row.record is None
    assert row.failure_code == "malformed_response" and row.billed_calls == 2
    assert report.generation_failed == 1


def test_a_reply_repaired_on_the_second_try_is_one_forecast_of_two_calls(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = OpenRouter(reply_for(case.question), first_reply="not json")
    report, store, _ = _run(case, live, tmp_path, monkeypatch, endpoint=endpoint)
    (row,) = _samples(store, report.arm_sha256)
    assert row.outcome == "forecast" and row.billed_calls == 2 and endpoint.posts == 2


def test_an_unknown_outcome_is_recorded_once_and_never_bought_again(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(OpenRouter):
        def __call__(self, request: Any) -> Any:
            self.bodies.append({})
            raise RuntimeError("connection reset")

    broken = Broken(reply_for(case.question))
    report, store, _ = _run(case, live, tmp_path, monkeypatch, endpoint=broken)
    (row,) = _samples(store, report.arm_sha256)
    assert row.outcome == "outcome_unknown" and row.held_microusd > 0 and row.cost_microusd is None
    again = OpenRouter(reply_for(case.question))
    second, _, _ = _run(case, live, tmp_path, monkeypatch, endpoint=again)
    assert again.posts == 0 and second.skipped_existing == 1


# --- the store's writer and verified reader -----------------------------------------------------


def test_the_writer_refuses_a_control_hash_the_ledger_does_not_hold(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = runner.append_arm_sample

    def tampered(store: Any, live_conn: Any, sample: Any) -> None:
        real(store, live_conn, replace(sample, control_forecast_sha256="0" * 64))

    monkeypatch.setattr(runner, "append_arm_sample", tampered)
    with pytest.raises(ExperimentError, match="control forecast_sha256"):
        _run(case, live, tmp_path, monkeypatch)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("forecast_sha256", "0" * 64, "does not match its hash"),
        ("reported_models_json", '[ "x"]', "canonical form"),
        ("created_at_utc", "2026-11-17T00:00:00", "no offset"),
        ("billed_calls", -1, None),
    ],
)
def test_the_reader_refuses_a_row_that_does_not_attest_to_itself(
    case: LiveCase,
    live: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    value: object,
    message: str | None,
) -> None:
    """A row copied with one field changed (raw SQL, bypassing the writer) is refused on read."""
    report, store, _ = _run(case, live, tmp_path, monkeypatch)
    conn = open_store(store)
    try:
        row = dict(conn.execute("SELECT * FROM arm_samples").fetchone())
        row.update(sample_id="copy", attempt_id="copy-attempt", sample_index=1)
        row[column] = value
        columns = ", ".join(row)
        insert = f"INSERT INTO arm_samples ({columns}) VALUES ({', '.join('?' for _ in row)})"
        if message is None:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(insert, tuple(row.values()))
            return
        conn.execute(insert, tuple(row.values()))
        with pytest.raises(ExperimentError, match=message):
            read_arm_samples(conn, report.arm_sha256)
    finally:
        conn.close()


# --- the spec's own refusals before anything runs ------------------------------------------------


def test_an_arm_on_a_model_with_no_in_service_instant_is_refused(
    case: LiveCase, tmp_path: Path
) -> None:
    with pytest.raises(ExperimentError, match="in-service"):
        _loaded(case, tmp_path, model="openrouter/openai/gpt-5.6-sol")


def test_an_arm_whose_prompt_bytes_do_not_match_its_hash_is_refused(
    case: LiveCase, tmp_path: Path
) -> None:
    loaded = _loaded(case, tmp_path)
    spec = loaded.spec.model_copy(
        update={"prompt": loaded.spec.prompt.model_copy(update={"sha256": "0" * 64})}
    )
    with pytest.raises(ExperimentError, match="does not match the sha256"):
        prepare_arm(replace(loaded, spec=spec), case.config, files_root=None)


def test_only_checkpoints_led_by_the_records_own_run_are_candidates(
    case: LiveCase, live: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record stores its first run id; a checkpoint led by another run is not its own.

    Without this filter the hash check would still reject a checkpoint whose runs differ, so
    the filter is tested directly: it is the record's own claim about its first run, and it
    keeps the gate from loading runs the record never named.
    """
    record = read_forecast_record(live, case.record_id)
    saved = [
        {"run_ids": ["run-of-another-attempt", record.retrieval_run_id]},
        {"run_ids": [record.retrieval_run_id]},
        {"run_ids": [record.retrieval_run_id]},
        {"run_ids": []},
        {"run_ids": [7]},
        "not a checkpoint",
    ]
    monkeypatch.setattr(fidelity, "events", lambda conn, kind, scope: saved)
    assert fidelity._checkpoints(live, record) == [(record.retrieval_run_id,)]


def test_a_resumed_arm_uses_its_frozen_selection_not_the_population_now(
    case: LiveCase, live: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A question resolved after the arm started never enters it (SOURCE-ALPHA stopping rule)."""
    first, store, _ = _run(case, live, tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "select", lambda live_conn, loaded: ("a-newly-resolved-record",))
    second, _, endpoint = _run(case, live, tmp_path, monkeypatch)
    assert second.selected == 1 and second.skipped_existing == 1 and endpoint.posts == 0
    conn = open_store(store)
    try:
        (selection,) = conn.execute("SELECT selection_json FROM arms").fetchone()
    finally:
        conn.close()
    assert json.loads(selection) == [case.record_id]
