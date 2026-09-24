"""M4-802: ``whiskeyjack-bot score`` and the orchestration beneath it.

The suite blocks sockets; on top of that the command is run with ``build_client`` and the
``requests`` verbs replaced by refusals, so "no network, no paid call" is a measurement of what
the command reached, not a reading of its imports.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml

from resolution_rows import insert_resolution_row, post_payload, seed_record, seed_submitted
from score_rows import (
    PAST_OBSERVATION,
    resolve,
    seed_forecast,
    seed_resolved,
    walk_to_submitted,
)
from whiskeyjack_bot.cli import EXIT_REFUSED, main
from whiskeyjack_bot.env_verify import EXIT_OK
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    current_status,
    read_local_scores,
    read_platform_scores,
    record_resolution_observation,
)
from whiskeyjack_bot.resolution import canonical_json, sha256_text
from whiskeyjack_bot.score_records import ScoreRecordsError, score_records

REPO_ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)


def _clock() -> Iterator[datetime]:
    moment = T0
    while True:
        yield moment
        moment += timedelta(minutes=1)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[Any]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    connection = connect(db)
    try:
        yield connection
    finally:
        connection.close()


def _population(conn: Any) -> None:
    """One record for every status the run can report, and one it must not see."""
    seed_resolved(conn, "rec-yes", question_id=45747, post_id=45556, probability_yes=0.7)
    seed_resolved(conn, "rec-mc", question_id=45748, post_id=45557, question_type="multiple_choice")
    seed_resolved(conn, "rec-annulled", question_id=45749, post_id=45558, resolution="annulled")
    # Numeric: placeholder record, resolved through the production writer.
    seed_submitted(conn, "rec-num", question_id=45750, post_id=45559, question_type="numeric")
    resolve(conn, "rec-num")
    # A binary record that does not read back as a forecast: a per-record failure.
    seed_submitted(conn, "rec-broken", question_id=45751, post_id=45560)
    resolve(conn, "rec-broken")
    # Posted, never resolved: not a candidate at all.
    digest = seed_forecast(conn, "rec-open", question_id=45752, post_id=45561)
    walk_to_submitted(conn, "rec-open", digest)
    # Never posted.
    seed_record(conn, "rec-draft", question_id=45753, post_id=45562)


# ── orchestration ────────────────────────────────────────────────────────────


def test_every_record_with_a_resolution_gets_exactly_one_verdict(conn: Any) -> None:
    _population(conn)
    clock = _clock()
    results = score_records(conn, clock=lambda: next(clock))
    by_record = {
        r.record_id: (
            r.status,
            r.platform_status,
            r.rows_appended,
            r.platform_rows_appended,
            r.moved_to_scored,
        )
        for r in results
    }
    assert by_record == {
        "rec-yes": ("appended", "appended", 2, 4, True),
        "rec-mc": ("appended", "appended", 2, 4, True),
        "rec-annulled": ("not_scorable", "not_scorable", 0, 0, False),
        # M4-803: never scored locally (D30), and the platform writer is what moves it.
        "rec-num": ("out_of_scope", "appended", 0, 4, True),
        # The local writer cannot read the forecast back; the platform's scores need only the
        # observation, so they are still recorded, and the platform row takes the event.
        "rec-broken": ("failed", "appended", 0, 4, True),
    }
    broken = next(r for r in results if r.record_id == "rec-broken")
    assert broken.failed
    assert broken.detail is not None and "cannot be read back" in broken.detail
    assert current_status(conn, "rec-yes") == "scored"
    assert current_status(conn, "rec-num") == "scored"
    assert len(read_local_scores(conn, "rec-mc")) == 2
    assert len(read_platform_scores(conn, "rec-num")) == 4
    assert read_local_scores(conn, "rec-num") == ()

    again = {
        r.record_id: (r.status, r.platform_status)
        for r in score_records(conn, clock=lambda: next(clock))
    }
    assert again["rec-yes"] == again["rec-mc"] == ("unchanged", "unchanged")
    assert again["rec-num"] == ("out_of_scope", "unchanged")
    assert again["rec-broken"] == ("failed", "unchanged")
    assert conn.execute("SELECT count(*) FROM score_events").fetchone()[0] == 4 + 4 * 4


def test_one_named_record_is_scored_alone(conn: Any) -> None:
    _population(conn)
    results = score_records(conn, record_id="rec-yes", clock=lambda: T0)
    assert [(r.record_id, r.status) for r in results] == [("rec-yes", "appended")]
    assert current_status(conn, "rec-mc") == "resolved"
    named_open = score_records(conn, record_id="rec-open", clock=lambda: T0)
    assert [(r.record_id, r.status) for r in named_open] == [("rec-open", "not_scorable")]


@pytest.mark.parametrize("record_id", ["", 7, "rec-nowhere-SENTINEL"])
def test_a_malformed_or_unknown_record_id_is_refused_without_echo(
    conn: Any, record_id: object
) -> None:
    with pytest.raises(ScoreRecordsError) as excinfo:
        score_records(conn, record_id=record_id)  # type: ignore[arg-type]
    assert "SENTINEL" not in str(excinfo.value)


def test_a_malformed_stored_question_type_is_refused_without_echo(conn: Any) -> None:
    seed_submitted(conn, "rec-typo", question_id=45760, post_id=45570)
    insert_resolution_row(conn, "rec-typo")
    conn.execute("DROP TRIGGER forecast_records_block_update")
    conn.execute("UPDATE forecast_records SET question_type = 'SENTINEL-type'")
    with pytest.raises(ScoreRecordsError, match="malformed") as excinfo:
        score_records(conn)
    assert "SENTINEL" not in str(excinfo.value)


# ── the command ──────────────────────────────────────────────────────────────


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    data["storage"]["sqlite_path"] = str(tmp_path / "data" / "bot.sqlite3")
    data["storage"]["artifact_root"] = str(tmp_path / "data" / "artifacts")
    data["storage"]["export_root"] = str(tmp_path / "data" / "exports")
    data["logging"]["file"] = str(tmp_path / "data" / "logs" / "bot.jsonl")
    data["forecast"]["prompt_path"] = str(REPO_ROOT / "prompts" / "forecaster.md")
    data["retrieval"]["social"]["account_allowlist_path"] = str(
        REPO_ROOT / "config" / "x_accounts.yaml"
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _ledger(config_file: Path) -> Any:
    database = Path(
        yaml.safe_load(config_file.read_text(encoding="utf-8"))["storage"]["sqlite_path"]
    )
    database.parent.mkdir(parents=True, exist_ok=True)
    initialize_ledger(database)
    return connect(database)


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reached: list[str] = []

    def refuse(name: str) -> Any:
        def call(*args: object, **kwargs: object) -> Any:
            reached.append(name)
            raise AssertionError(f"score reached {name}")

        return call

    import whiskeyjack_bot.metaculus.client as client_module

    monkeypatch.setattr(client_module, "build_client", refuse("build_client"))
    for verb in ("get", "post", "put", "request"):
        monkeypatch.setattr(requests, verb, refuse(f"requests.{verb}"))
    monkeypatch.setattr(requests.Session, "request", refuse("requests.Session.request"))
    return reached


def test_the_command_scores_prints_and_is_idempotent(
    config_file: Path, offline: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    connection = _ledger(config_file)
    try:
        seed_resolved(
            connection, "rec-yes", question_id=45747, post_id=45556, observed_at=PAST_OBSERVATION
        )
        seed_resolved(
            connection,
            "rec-no",
            question_id=45748,
            post_id=45557,
            resolution="annulled",
            observed_at=PAST_OBSERVATION,
        )
    finally:
        connection.close()

    assert main(["score", "--config", str(config_file)]) == EXIT_OK
    out = capsys.readouterr().out
    assert (
        "question 45747  record rec-yes  binary  appended  rows 2  "
        "platform appended  rows 4  -> scored"
    ) in out
    assert (
        "question 45748  record rec-no  binary  not_scorable  rows 0  platform not_scorable  rows 0"
    ) in out
    assert "records: 2  failed: 0" in out

    assert main(["score", "--config", str(config_file)]) == EXIT_OK
    assert (
        "record rec-yes  binary  unchanged  rows 0  platform unchanged  rows 0"
        in capsys.readouterr().out
    )
    assert offline == []


def test_the_command_exits_refused_when_any_record_failed(
    config_file: Path, offline: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    connection = _ledger(config_file)
    try:
        seed_resolved(
            connection, "rec-yes", question_id=45747, post_id=45556, observed_at=PAST_OBSERVATION
        )
        seed_submitted(connection, "rec-broken", question_id=45751, post_id=45560)
        resolve(connection, "rec-broken", observed_at=PAST_OBSERVATION)
    finally:
        connection.close()
    assert main(["score", "--config", str(config_file)]) == EXIT_REFUSED
    out = capsys.readouterr().out
    assert (
        "record rec-broken  binary  failed  rows 0  platform appended  rows 4  "
        "failed: the forecast record cannot be read back"
    ) in out
    assert "record rec-yes  binary  appended" in out, "the good record still landed"
    assert "records: 2  failed: 1" in out and offline == []


def test_an_unknown_record_id_is_refused_without_echo(
    config_file: Path, offline: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _ledger(config_file).close()
    assert main(["score", "--config", str(config_file), "--record-id", "SENTINEL-x"]) == (
        EXIT_REFUSED
    )
    out = capsys.readouterr().out
    assert "refused: record_id does not name a stored forecast record" in out
    assert "SENTINEL" not in out and offline == []


def test_a_missing_ledger_is_refused_rather_than_created(
    config_file: Path, offline: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["score", "--config", str(config_file)]) == EXIT_REFUSED
    assert "no ledger database" in capsys.readouterr().out
    database = Path(
        yaml.safe_load(config_file.read_text(encoding="utf-8"))["storage"]["sqlite_path"]
    )
    assert not database.exists()


def test_a_resolved_observation_without_platform_scores_fails_the_command(
    config_file: Path, offline: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """M4-803, owner decision: a definite resolution with no `score_data` is an attribution gap,
    so it fails the record and the command (the schedule's OnFailure page) -- including for a
    record whose local scores landed."""
    connection = _ledger(config_file)
    try:
        seed_resolved(
            connection, "rec-yes", question_id=45747, post_id=45556, observed_at=PAST_OBSERVATION
        )
        seed_submitted(
            connection, "rec-bare", question_id=45750, post_id=45559, question_type="numeric"
        )
        record_resolution_observation(
            connection,
            record_id="rec-bare",
            source_response=post_payload(
                "numeric", post_id=45559, question_id=45750, score_data={}
            ),
            observed_at=PAST_OBSERVATION,
        )
        results = {r.record_id: r for r in score_records(connection)}
    finally:
        connection.close()
    bare = results["rec-bare"]
    assert (bare.status, bare.platform_status, bare.failed) == ("out_of_scope", "failed", True)
    assert bare.detail == (
        "the platform scores cannot be recorded: the observation carries no platform scores"
    )
    assert not results["rec-yes"].failed

    connection = _ledger(config_file)
    try:
        seed_resolved(
            connection, "rec-b2", question_id=45760, post_id=45570, observed_at=PAST_OBSERVATION
        )
        # A binary record whose local scores land but whose observation has no platform scores.
        connection.execute("DROP TRIGGER resolution_events_block_update")
        stripped = post_payload("binary", post_id=45570, question_id=45760, score_data={})
        text = canonical_json(stripped)
        connection.execute(
            "UPDATE resolution_events SET source_response = ?, source_response_sha256 = ? "
            "WHERE forecast_record_id = 'rec-b2'",
            (text, sha256_text(text)),
        )
        connection.commit()
    finally:
        connection.close()
    assert main(["score", "--config", str(config_file)]) == EXIT_REFUSED
    out = capsys.readouterr().out
    assert (
        "record rec-bare  numeric  out_of_scope  rows 0  platform failed  rows 0  failed: the "
        "platform scores cannot be recorded: the observation carries no platform scores"
    ) in out
    assert (
        "record rec-b2  binary  appended  rows 2  platform failed  rows 0  failed: the "
        "platform scores cannot be recorded: the observation carries no platform scores"
    ) in out
    assert "records: 3  failed: 2" in out and offline == []
