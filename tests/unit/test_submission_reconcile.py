"""M2-713 acceptance: a live post the ledger refused to record is reconciled into agreement.

The acceptance criterion's own test is the first one below, and it is driven through the real
live path rather than a stand-in: ``test_tournament.py``'s ``case`` fixture runs the genuine
activation, research-replay and submission policy, so the ``forecast_intent`` a reconciliation
rests on is the one ``submission_policy`` really writes. The ledger refusal is also real -- a
second connection holds ``BEGIN IMMEDIATE`` from inside the POST, the way the six-hourly
resolutions unit can hold it against the poll, and the attempt write loses the busy timeout.

The second shape -- the process killed during the refetch, before the artifact -- is driven
with the fork-and-``os._exit`` child ``test_tournament.py`` already uses for "process death
after acceptance", which until this item asserted zero attempt rows as the recovered state.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from forecasting_tools.data_models.data_organizer import DataOrganizer

from tests.unit import test_tournament as tournament_suite
from tests.unit.test_tournament import Platform, _prepare_version, _process_submit, poll
from whiskeyjack_bot.forecast.store import read_forecast_record
from whiskeyjack_bot.lifecycle import current_status, read_history
from whiskeyjack_bot.resolution_ingest import ingest_resolutions
from whiskeyjack_bot.submission import (
    SubmissionError,
    attempt_for_key,
    key_is_reconciled,
    live_reservations_for_record,
    release_submission_key,
    require_key_unused,
    reserve_submission_key,
    submission_key_for_approved_record,
)
from whiskeyjack_bot.submission_live import LiveSubmissionError, post_approved_forecast
from whiskeyjack_bot.submission_payload import authorized_payload
from whiskeyjack_bot.submission_reconcile import (
    ReconciliationError,
    check_artifact_binds,
    find_unrecorded_post,
    read_intent,
    reconcile_unrecorded_post,
    unrecorded_posts,
)
from whiskeyjack_bot.tournament_state import utcnow

NOTE = "question page shows our 35% forecast, posted at the time of the refusal"


@pytest.fixture
def case(tmp_path: Path) -> Iterator[Any]:
    """``test_tournament.py``'s activated tournament ledger, config and fake platform."""
    yield from tournament_suite.case.__wrapped__(tmp_path)


class Counting(Platform):
    """The test platform, counting every call a reconciliation could make."""

    def __init__(self, raw: dict[str, Any]) -> None:
        super().__init__(raw)
        self.reads = 0
        self.identity_reads = 0
        self.values = [0.65, 0.35]
        self.fail_reads = False
        self.during_read: Any = None

    def get_current_user_id(self) -> int:
        self.identity_reads += 1
        return super().get_current_user_id()

    def get_question_by_post_id(self, post_id: int) -> Any:
        self.reads += 1
        if self.during_read is not None:
            self.during_read()
        if self.fail_reads:
            raise TimeoutError
        raw = json.loads(json.dumps(self.raw))
        if self.posts and not self.hide_forecast:
            raw["question"]["my_forecasts"] = {
                "history": [{"start_time": utcnow().timestamp(), "forecast_values": self.values}]
            }
        return DataOrganizer.get_question_from_post_json(raw)


def _payload(conn: sqlite3.Connection, config: Any, record_id: str) -> dict[str, object]:
    record = read_forecast_record(conn, record_id)
    return dict(authorized_payload(record, calibration=config.numeric_calibration).payload)


def _key(conn: sqlite3.Connection, config: Any, record_id: str) -> str:
    record = read_forecast_record(conn, record_id)
    digest = authorized_payload(record, calibration=config.numeric_calibration).sha256
    return submission_key_for_approved_record(conn, record_id, request_payload_sha256=digest)


def _refused_write(case: Any) -> tuple[str, Counting, LiveSubmissionError]:
    """Post for real, then make the ledger refuse the attempt row. Returns the L4 state."""
    conn, config, platform, *_ = case
    record_id = _prepare_version(case)
    held: dict[str, sqlite3.Connection] = {}

    class Blocking(Counting):
        def post_binary_question_prediction(self, question_id: int, prediction: float) -> None:
            super().post_binary_question_prediction(question_id, prediction)
            # After the server accepted: another process takes the write lock and keeps it
            # past the attempt write's busy timeout.
            blocker = sqlite3.connect(config.storage.sqlite_path, isolation_level=None)
            blocker.execute("BEGIN IMMEDIATE")
            held["blocker"] = blocker

    poster = Blocking(platform.raw)
    conn.execute("PRAGMA busy_timeout = 200")
    try:
        with pytest.raises(LiveSubmissionError) as refused:
            post_approved_forecast(
                conn,
                config=config,
                record_id=record_id,
                payload=_payload(conn, config, record_id),
                poster=poster,
                occurred_at=utcnow(),
            )
    finally:
        if "blocker" in held:
            held["blocker"].rollback()
            held["blocker"].close()
        conn.execute("PRAGMA busy_timeout = 5000")
    return record_id, poster, refused.value


def _killed_during_refetch(case: Any) -> str:
    """Post for real in a child that dies before the artifact or the row. Returns the record."""
    conn, config, platform, *_ = case
    record_id = _prepare_version(case)
    ctx = multiprocessing.get_context("fork")
    counter, start = ctx.Value("i", 0), ctx.Event()
    child = ctx.Process(
        target=_process_submit, args=(config, platform.raw, record_id, counter, start, True)
    )
    child.start()
    start.set()
    child.join(20)
    assert child.exitcode == 7 and counter.value == 1
    platform.posts = 1  # the platform state the child's accepted POST left behind
    return record_id


def _reconcile(case: Any, record_id: str, poster: Any, **kwargs: Any) -> Any:
    conn, config, *_ = case
    return reconcile_unrecorded_post(
        conn,
        config,
        record_id=kwargs.pop("record_id_override", record_id),
        observed_by=kwargs.pop("observed_by", "chris"),
        note=kwargs.pop("note", NOTE),
        poster=poster,
        occurred_at=utcnow(),
        sleep=lambda _: None,
        **kwargs,
    )


def _rows(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
    return tuple(  # type: ignore[return-value]
        conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "submission_reconciliations",
            "lifecycle_events",
            "submission_key_releases",
            "submission_attempts",
        )
    )


# --------------------------------------------------------------------------------------
# The acceptance criterion.
# --------------------------------------------------------------------------------------


def test_a_post_the_ledger_refused_to_record_is_reconciled_into_agreement_with_the_platform(
    case: Any,
) -> None:
    conn, config, *_ = case
    record_id, poster, refused = _refused_write(case)

    # The state M2-713 exists for, exactly as the backlog row describes it.
    assert "a live post was made and the ledger refused to record it" in str(refused)
    assert "reconcile-submission" in str(refused), "the refusal names the way out"
    assert poster.posts == 1
    assert current_status(conn, record_id) == "approved"
    key = _key(conn, config, record_id)
    assert attempt_for_key(conn, key) is None
    [standing] = live_reservations_for_record(conn, record_id)
    artifact = config.storage.artifact_root / "submissions" / "live"
    [artifact_file] = list(artifact.rglob("*.json"))

    before_reads = poster.reads
    event = _reconcile(case, record_id, poster)

    # The record agrees with the platform: submitted, by a confirmation, with no attempt row
    # invented for a response nobody captured.
    assert (event.event_type, event.from_status, event.to_status) == (
        "submission_confirmed",
        "approved",
        "submitted",
    )
    assert current_status(conn, record_id) == "submitted"
    assert read_history(conn, record_id)[-1] == event
    assert attempt_for_key(conn, key) is None
    assert conn.execute("SELECT count(*) FROM submission_attempts").fetchone()[0] == 0

    # The key agrees with the platform: spent, through every reader and both writers.
    assert key_is_reconciled(conn, key)
    assert live_reservations_for_record(conn, record_id) == ()
    with pytest.raises(SubmissionError, match="spent by a post recorded through reconciliation"):
        require_key_unused(conn, key)
    with pytest.raises(SubmissionError, match="spent by a post recorded through reconciliation"):
        reserve_submission_key(conn, record_id=record_id, idempotency_key=key, reserved_at=utcnow())
    with pytest.raises(SubmissionError, match="reconciled as a post that reached the platform"):
        release_submission_key(
            conn, standing, reason="operator_abandoned", released_at=utcnow(), released_by="x"
        )

    # The human and the program's evidence are both on the row; the artifact is pinned.
    row = conn.execute(
        "SELECT reservation_id, attempt_id, observed_by, note, artifact_path, artifact_sha256, "
        "refetched_forecast_snapshot FROM submission_reconciliations"
    ).fetchone()
    assert row[0] == standing.reservation_id
    assert row[2:4] == ("chris", NOTE)
    assert config.storage.artifact_root / row[4] == artifact_file
    assert row[5] == hashlib.sha256(artifact_file.read_bytes()).hexdigest()
    assert json.loads(row[6])["outcome"] == "confirmed"
    assert poster.reads == before_reads + 1 and poster.posts == 1

    # And nothing can post it again, nor does the worker.
    with pytest.raises(LiveSubmissionError):
        post_approved_forecast(
            conn,
            config=config,
            record_id=record_id,
            payload=_payload(conn, config, record_id),
            poster=poster,
            occurred_at=utcnow(),
        )
    assert poster.posts == 1
    ingested = ingest_resolutions(conn, lambda post_id: case[2].raw)
    assert record_id in {result.record_id for result in ingested}


def test_a_post_whose_process_died_before_its_receipt_is_reconciled(case: Any) -> None:
    conn, config, platform, *_ = case
    record_id = _killed_during_refetch(case)
    assert not list((config.storage.artifact_root / "submissions").rglob("*.json"))
    assert unrecorded_posts(conn) == (record_id,)

    event = _reconcile(case, record_id, platform)

    assert event.to_status == "submitted" == current_status(conn, record_id)
    row = conn.execute(
        "SELECT artifact_path, artifact_sha256 FROM submission_reconciliations"
    ).fetchone()
    assert tuple(row) == (None, None), "no receipt was captured, and none is claimed"
    assert key_is_reconciled(conn, _key_from_reservation(conn, record_id))
    assert unrecorded_posts(conn) == ()


def _key_from_reservation(conn: sqlite3.Connection, record_id: str) -> str:
    return str(
        conn.execute(
            "SELECT idempotency_key FROM submission_key_reservations WHERE forecast_record_id = ?",
            (record_id,),
        ).fetchone()[0]
    )


def test_the_worker_confirming_the_journal_first_does_not_block_reconciliation(case: Any) -> None:
    """The silent route: the poll confirms and comments; the lifecycle ledger still needs this."""
    conn, config, platform, *_ = case
    record_id = _killed_during_refetch(case)
    result = poll(case)
    assert result["forecast_confirmed"] == result["comment_completed"] == 1
    assert current_status(conn, record_id) == "approved"
    _reconcile(case, record_id, platform)
    assert current_status(conn, record_id) == "submitted"
    again = poll(case)
    assert platform.posts == 1 and platform.comment_posts == 1
    assert again["forecast_confirmed"] == 1


# --------------------------------------------------------------------------------------
# Refusals: every one writes nothing.
# --------------------------------------------------------------------------------------


def test_an_open_transaction_is_refused_before_anything_is_read(case: Any) -> None:
    conn, _, platform, *_ = case
    record_id, poster, _ = _refused_write(case)
    before, reads = _rows(conn), (poster.reads, poster.identity_reads)
    conn.execute("BEGIN")
    try:
        with pytest.raises(ReconciliationError, match="open transaction"):
            _reconcile(case, record_id, poster)
    finally:
        conn.execute("ROLLBACK")
    assert _rows(conn) == before and (poster.reads, poster.identity_reads) == reads


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("observed_by", ""),
        ("observed_by", " \t"),
        ("note", ""),
        ("note", "　"),
        ("observed_by", None),
    ],
)
def test_the_assertion_is_required_before_any_read(case: Any, field: str, value: object) -> None:
    conn, *_ = case
    record_id, poster, _ = _refused_write(case)
    before, reads = _rows(conn), (poster.reads, poster.identity_reads)
    with pytest.raises(ReconciliationError, match=f"{field} is required"):
        _reconcile(case, record_id, poster, **{field: value})
    assert _rows(conn) == before and (poster.reads, poster.identity_reads) == reads


def test_a_recorded_post_is_not_reconciled(case: Any) -> None:
    conn, _, platform, *_ = case
    poll(case)
    record_id = conn.execute("SELECT forecast_record_id FROM submission_attempts").fetchone()[0]
    before = _rows(conn)
    with pytest.raises(ReconciliationError, match="is submitted, not awaiting submission"):
        _reconcile(case, record_id, platform)
    assert _rows(conn) == before


def test_an_approved_record_that_was_never_claimed_is_not_reconciled(case: Any) -> None:
    conn, _, platform, *_ = case
    record_id = _prepare_version(case)
    with pytest.raises(ReconciliationError, match="no key reservation is standing"):
        _reconcile(case, record_id, platform)


def test_a_reservation_no_command_carried_to_the_post_is_not_reconciled(case: Any) -> None:
    """A claim with no intent behind it: the process died before ``before_post`` ran."""
    conn, config, platform, *_ = case
    record_id = _prepare_version(case)
    reserve_submission_key(
        conn,
        record_id=record_id,
        idempotency_key=_key(conn, config, record_id),
        reserved_at=utcnow(),
    )
    poster = Counting(platform.raw)
    before = _rows(conn)
    with pytest.raises(ReconciliationError, match="holds no durable submission intent"):
        _reconcile(case, record_id, poster)
    assert _rows(conn) == before and (poster.reads, poster.identity_reads) == (0, 0)


def test_a_wrongly_released_reservation_is_not_reconciled(case: Any) -> None:
    """Both answers to one question would be on the ledger; refused, and named a standing risk."""
    conn, *_ = case
    record_id, poster, _ = _refused_write(case)
    [standing] = live_reservations_for_record(conn, record_id)
    release_submission_key(
        conn, standing, reason="operator_abandoned", released_at=utcnow(), released_by="chris"
    )
    with pytest.raises(ReconciliationError, match="no key reservation is standing"):
        _reconcile(case, record_id, poster)


def test_a_different_account_is_refused_before_the_platform_is_read(case: Any) -> None:
    conn, *_ = case
    record_id, poster, _ = _refused_write(case)
    poster.account = 99
    before, reads = _rows(conn), poster.reads
    with pytest.raises(ReconciliationError, match="not the account that made this post"):
        _reconcile(case, record_id, poster)
    assert _rows(conn) == before and poster.reads == reads


def test_an_unreadable_identity_is_refused(case: Any) -> None:
    conn, *_ = case
    record_id, poster, _ = _refused_write(case)

    def broken() -> int:
        raise ConnectionError("https://www.metaculus.com/api/ token=secretish")

    poster.get_current_user_id = broken  # type: ignore[method-assign]
    with pytest.raises(ReconciliationError, match="could not be read") as refused:
        _reconcile(case, record_id, poster)
    assert "secretish" not in str(refused.value) and refused.value.__cause__ is None


@pytest.mark.parametrize(
    ("setup", "fragment"),
    [
        ("absent", "shows no forecast from this account"),
        ("mismatched", "not the payload this record's approval authorized"),
        ("unreadable", "could not be refetched"),
    ],
)
def test_a_refetch_that_does_not_confirm_is_refused(case: Any, setup: str, fragment: str) -> None:
    conn, *_ = case
    record_id, poster, _ = _refused_write(case)
    if setup == "absent":
        poster.hide_forecast = True
    elif setup == "mismatched":
        poster.values = [0.5, 0.5]
    else:
        poster.fail_reads = True
    before = _rows(conn)
    with pytest.raises(ReconciliationError, match=fragment):
        _reconcile(case, record_id, poster)
    assert _rows(conn) == before
    assert current_status(conn, record_id) == "approved"


def test_a_state_that_changes_during_the_refetch_is_refused(case: Any) -> None:
    conn, config, *_ = case
    record_id, poster, _ = _refused_write(case)
    [standing] = live_reservations_for_record(conn, record_id)

    def release_elsewhere() -> None:
        other = sqlite3.connect(config.storage.sqlite_path, isolation_level=None)
        try:
            other.execute(
                "INSERT INTO submission_key_releases (release_id, reservation_id, reason, "
                "released_by, released_at_utc, created_at_utc) "
                "VALUES ('wjrel-race', ?, 'operator_abandoned', 'someone', ?, ?)",
                (standing.reservation_id, "2099-01-01T00:00:00.000000+00:00", "x"),
            )
        finally:
            other.close()
        poster.during_read = None

    poster.during_read = release_elsewhere
    with pytest.raises(ReconciliationError, match="no key reservation is standing"):
        _reconcile(case, record_id, poster)
    assert conn.execute("SELECT count(*) FROM submission_reconciliations").fetchone()[0] == 0


def test_an_artifact_that_cannot_be_read_is_refused_rather_than_called_missing(case: Any) -> None:
    conn, config, *_ = case
    record_id, poster, _ = _refused_write(case)
    [artifact_file] = list((config.storage.artifact_root / "submissions").rglob("*.json"))
    artifact_file.chmod(0)
    try:
        if os.access(artifact_file, os.R_OK):  # pragma: no cover - running as root
            pytest.skip("permissions are not enforced for this user")
        with pytest.raises(ReconciliationError, match="cannot read the submission artifact"):
            _reconcile(case, record_id, poster)
    finally:
        artifact_file.chmod(0o600)
    assert conn.execute("SELECT count(*) FROM submission_reconciliations").fetchone()[0] == 0


def test_an_artifact_at_the_path_that_describes_another_post_is_refused(case: Any) -> None:
    conn, config, *_ = case
    record_id, poster, _ = _refused_write(case)
    [artifact_file] = list((config.storage.artifact_root / "submissions").rglob("*.json"))
    envelope = json.loads(artifact_file.read_text(encoding="utf-8"))
    envelope["receipt"]["forecast_record_id"] = "rec-somebody-else"
    artifact_file.chmod(0o600)
    artifact_file.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ReconciliationError, match="does not describe this post"):
        _reconcile(case, record_id, poster)


def test_the_evidence_is_assembled_without_touching_the_platform(case: Any) -> None:
    conn, config, *_ = case
    record_id, poster, _ = _refused_write(case)
    reads, identity = poster.reads, poster.identity_reads
    evidence = find_unrecorded_post(conn, config, record_id)
    assert (poster.reads, poster.identity_reads) == (reads, identity)
    assert evidence.idempotency_key == _key(conn, config, record_id)
    assert evidence.artifact_path is not None and evidence.account_id == 42


# --------------------------------------------------------------------------------------
# The pure checks, by example (the fuzzing is in tests/property).
# --------------------------------------------------------------------------------------

_INTENT = {
    "record_id": "rec-1",
    "account_id": 42,
    "project_id": "32977",
    "question_id": 7,
    "post_id": 8,
    "payload": {"question_type": "binary", "probability_yes": 0.35},
    "baseline": [],
}


def _intent_text(**changes: object) -> str:
    from whiskeyjack_bot.submission_gateway import payload_sha256

    data = {**_INTENT, "payload_sha256": payload_sha256(_INTENT["payload"])}  # type: ignore[arg-type]
    data.update(changes)
    return json.dumps(data)


def _read(text: object) -> Any:
    return read_intent(text, record_id="rec-1", question_id=7, post_id=8, tournament_id="32977")


def test_an_intent_that_describes_this_post_is_read() -> None:
    intent = _read(_intent_text())
    assert intent.account_id == 42 and intent.payload == _INTENT["payload"]


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"record_id": "rec-2"}, "does not describe this record's question"),
        ({"question_id": 8}, "does not describe this record's question"),
        ({"question_id": True}, "does not describe this record's question"),
        ({"post_id": 9}, "does not describe this record's question"),
        ({"project_id": 32977}, "does not describe this record's question"),
        ({"account_id": 0}, "names no account"),
        ({"account_id": "42"}, "names no account"),
        ({"baseline": [{"start_time": 1}]}, "records a baseline"),
        ({"baseline": None}, "records a baseline"),
        ({"payload_sha256": "A" * 64}, "names no payload digest"),
        ({"payload_sha256": "a" * 64}, "does not hash to the digest it records"),
        ({"payload": ["not", "an", "object"]}, "holds no payload"),
    ],
)
def test_an_intent_that_does_not_describe_this_post_is_refused(
    changes: dict[str, object], fragment: str
) -> None:
    with pytest.raises(ReconciliationError, match=fragment):
        _read(_intent_text(**changes))


@pytest.mark.parametrize("text", [None, b"{}", "not json", "[]", '{"a": NaN}'])
def test_an_intent_that_is_not_a_json_object_is_refused(text: object) -> None:
    with pytest.raises(ReconciliationError):
        _read(text)


def _envelope(**receipt_changes: object) -> dict[str, object]:
    from whiskeyjack_bot.submission_gateway import payload_sha256

    payload = {"question_type": "binary", "probability_yes": 0.35}
    receipt = {
        "mode": "live",
        "idempotency_key": "k",
        "attempt_id": "a",
        "forecast_record_id": "rec-1",
        "request_payload_sha256": payload_sha256(payload),
    }
    receipt.update(receipt_changes)
    return {"question_id": 7, "request_payload": payload, "receipt": receipt}


def _binds(envelope: dict[str, object]) -> None:
    from whiskeyjack_bot.submission_gateway import payload_sha256

    check_artifact_binds(
        envelope,
        record_id="rec-1",
        question_id=7,
        key="k",
        attempt_id="a",
        digest=payload_sha256({"question_type": "binary", "probability_yes": 0.35}),
    )


def test_an_artifact_that_describes_this_post_binds() -> None:
    _binds(_envelope())


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "dry_run"},
        {"idempotency_key": "other"},
        {"attempt_id": "other"},
        {"forecast_record_id": "rec-2"},
        {"request_payload_sha256": "f" * 64},
    ],
)
def test_an_artifact_whose_receipt_describes_another_post_is_refused(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ReconciliationError, match="does not describe this post"):
        _binds(_envelope(**changes))


def test_an_artifact_for_another_question_or_payload_is_refused() -> None:
    wrong_question = _envelope()
    wrong_question["question_id"] = 8
    with pytest.raises(ReconciliationError, match="does not describe this post"):
        _binds(wrong_question)
    wrong_payload = _envelope()
    wrong_payload["request_payload"] = {"question_type": "binary", "probability_yes": 0.4}
    with pytest.raises(ReconciliationError, match="not the one this record's approval authorized"):
        _binds(wrong_payload)


# --------------------------------------------------------------------------------------
# The commands.
# --------------------------------------------------------------------------------------


def _config_file(config: Any, tmp_path: Path) -> Path:
    path = tmp_path / "reconcile-config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    return path


def test_reconcile_submission_records_the_post_and_prints_its_evidence(
    case: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from whiskeyjack_bot.cli import main
    from whiskeyjack_bot.env_verify import EXIT_OK

    conn, config, *_ = case
    record_id, poster, _ = _refused_write(case)
    monkeypatch.setattr("whiskeyjack_bot.metaculus.client.build_poster", lambda _config: poster)
    path = _config_file(config, tmp_path)
    capsys.readouterr()
    code = main(
        [
            "reconcile-submission",
            "--config",
            str(path),
            "--record-id",
            record_id,
            "--observed-by",
            "chris",
            "--note",
            NOTE,
        ]
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    assert "artifact:    " in out and "sha256" in out
    assert "result:      submission_confirmed -> submitted" in out
    assert current_status(conn, record_id) == "submitted"


def test_reconcile_submission_refuses_locally_before_building_a_poster(
    case: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from whiskeyjack_bot.cli import EXIT_REFUSED, main

    conn, config, platform, *_ = case
    record_id = _prepare_version(case)

    def no_poster(_config: object) -> object:
        raise AssertionError("a local refusal must not build a poster")

    monkeypatch.setattr("whiskeyjack_bot.metaculus.client.build_poster", no_poster)
    code = main(
        [
            "reconcile-submission",
            "--config",
            str(_config_file(config, tmp_path)),
            "--record-id",
            record_id,
            "--observed-by",
            "chris",
            "--note",
            NOTE,
        ]
    )
    assert code == EXIT_REFUSED
    assert "refused: no key reservation is standing" in capsys.readouterr().out


@pytest.mark.parametrize("missing", ["--observed-by", "--note"])
def test_reconcile_submission_requires_the_assertion(missing: str) -> None:
    from whiskeyjack_bot.cli import main

    argv = ["reconcile-submission", "--record-id", "r", "--observed-by", "chris", "--note", "n"]
    index = argv.index(missing)
    del argv[index : index + 2]
    with pytest.raises(SystemExit) as exited:
        main(argv)
    assert exited.value.code == 2


def test_unrecorded_posts_lists_the_candidates_and_writes_nothing(
    case: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from whiskeyjack_bot.cli import main
    from whiskeyjack_bot.env_verify import EXIT_OK

    conn, config, *_ = case
    record_id, _, _ = _refused_write(case)
    before = _rows(conn)
    code = main(["unrecorded-posts", "--config", str(_config_file(config, tmp_path))])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert f"record: {record_id}" in out and "unrecorded-post candidates: 1" in out
    assert _rows(conn) == before


def test_a_refused_submit_no_longer_offers_to_release_a_spent_key(
    case: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Deviation, at the operator's surface: after an ordinary post the hint is silent."""
    from whiskeyjack_bot.cli import _print_standing_reservations

    conn, *_ = case
    poll(case)
    record_id = conn.execute("SELECT forecast_record_id FROM submission_attempts").fetchone()[0]
    capsys.readouterr()
    _print_standing_reservations(conn, record_id)
    assert capsys.readouterr().out == ""


def test_the_standing_reservation_hint_names_both_ways_out(
    case: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from whiskeyjack_bot.cli import _print_standing_reservations

    conn, *_ = case
    record_id, _, _ = _refused_write(case)
    capsys.readouterr()
    _print_standing_reservations(conn, record_id)
    out = capsys.readouterr().out
    assert "release-key" in out and "reconcile-submission" in out
