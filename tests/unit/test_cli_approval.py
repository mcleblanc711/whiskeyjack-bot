"""M2-701: the `approve` and `reject` commands at the CLI boundary.

What is under test here is the command layer's own behaviour -- required arguments, the
summary printed before anything is written, the exit code a refusal produces, and that a
mistyped `--config` cannot silently mint an empty ledger. The decision semantics
themselves are `tests/unit/test_approval.py`.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from whiskeyjack_bot.cli import EXIT_REFUSED, main
from whiskeyjack_bot.env_verify import EXIT_OK
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import current_status, record_validation

from tests.unit.test_submission_live import (  # noqa: F401 - helpers reused deliberately
    BINARY_PAYLOAD,
    QUESTION_ID,
    RUN_ID,
    TIMESTAMP,
    _draft,
    _generation,
    _response,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

TS = "2026-08-19T00:00:00.000000+00:00"
OCCURRED = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
# A hash no seeded record stores, used only as the `--forecast-sha256` an operator got
# wrong. Every hash a record actually holds is now derived from the record (see
# `_seed_ledger`), because M2-707 made `approve` read `record_json` rather than only the
# `forecast_sha256` column -- a `'{}'` placeholder no longer reaches a decision.
OTHER_SHA = "c" * 64
RECORD_ID = "rec-1"


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """A valid config whose data paths live under tmp_path (the test_env_verify shape)."""
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


def _ledger_path(config_file: Path) -> Path:
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    return Path(data["storage"]["sqlite_path"])


def _seed_ledger(
    path: Path, *, attempt_id: str = "attempt-1", probability: float | None = None
) -> str:
    """A ledger holding one real record at `validated`, under `RECORD_ID`. Returns its hash.

    **The row is a genuine `ForecastRecord` now, and M2-707 is why.** `approve` derives the
    submission payload the decision authorizes from `record_json`, so the `'{}'` placeholder
    this seeder used to write refuses the whole command -- correctly, since a record nothing
    can read is a record nothing can be approved to submit.

    Built by hand rather than by `store.append_forecast_version` because these tests address
    the record by a fixed identifier on the command line and the writer mints a UUID. The
    column values come from `store._projection`, imported rather than transcribed, so a
    column added to `forecast_records` cannot leave this seeder writing a row
    `read_forecast_record` then refuses -- which is the failure it exists to avoid.

    `attempt_id` is the knob for producing a *different* record: it is part of the hashed
    content, so two ledgers seeded with different values hold two distinguishable hashes.

    `probability` is the knob for a record whose payload cannot be *built*. The response
    schema admits `[0.0, 1.0]` and Metaculus accepts `[0.001, 0.999]`, so a probability in
    the gap is a record the pipeline can legitimately hold and no approval can bind to --
    see `test_a_record_with_no_buildable_payload_cannot_be_approved_but_can_be_rejected`.
    """
    from whiskeyjack_bot.forecast.record import ForecastRecord
    from whiskeyjack_bot.forecast.store import _projection

    if probability is None:
        draft = _draft(attempt_id)
    else:
        draft = _draft(
            attempt_id,
            generation=_generation(
                forecast=_response(final_prediction={"probability_yes": probability})
            ),
        )
    record = ForecastRecord(
        **draft.model_dump(), record_id=RECORD_ID, forecast_version=1, parent_record_id=None
    )
    projected = _projection(record)
    initialize_ledger(path)
    conn = connect(path)
    try:
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, question_id, "
            "started_at_utc, created_at_utc) VALUES (?, 'exa', ?, ?, ?)",
            (RUN_ID, QUESTION_ID, TIMESTAMP, TIMESTAMP),
        )
        columns = ", ".join((*projected, "status", "created_at_utc"))
        placeholders = ", ".join("?" for _ in range(len(projected) + 2))
        conn.execute(
            f"INSERT INTO forecast_records ({columns}) VALUES ({placeholders})",
            (*projected.values(), "draft", TS),
        )
        record_validation(conn, record_id=RECORD_ID, occurred_at=OCCURRED)
    finally:
        conn.close()
    return str(projected["forecast_sha256"])


def _isolated_approval_count(path: Path) -> int:
    """Approvals in the database file itself, with no sidecar able to replay into it.

    Rotating a live SQLite database leaves its WAL behind under the old pathname, and
    SQLite will replay a foreign WAL beside a database it was not written for. Reading a
    byte copy is what tells "this database was written" apart from "another database's
    WAL is sitting next to it".
    """
    copy_path = path.parent / f"isolated-{path.name}"
    copy_path.write_bytes(path.read_bytes())
    conn = sqlite3.connect(copy_path)
    try:
        return int(conn.execute("SELECT count(*) FROM approval_events").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture()
def seeded(config_file: Path) -> Path:
    """A ledger at the configured path holding one record at `validated`."""
    _seed_ledger(_ledger_path(config_file))
    return config_file


def _stored_hash(config_file: Path) -> str:
    """The hash the seeded record actually stores -- what a decision binds to.

    Read back from the ledger rather than kept as a module constant, because it is now
    derived from the record's content and a literal would be a second spelling of it.
    """
    conn = sqlite3.connect(_ledger_path(config_file))
    try:
        row = conn.execute(
            "SELECT forecast_sha256 FROM forecast_records WHERE record_id = ?", (RECORD_ID,)
        ).fetchone()
    finally:
        conn.close()
    return str(row[0])


def _status(config_file: Path) -> str:
    conn = connect(_ledger_path(config_file))
    try:
        return current_status(conn, RECORD_ID)
    finally:
        conn.close()


def _approval_count(config_file: Path) -> int:
    conn = sqlite3.connect(_ledger_path(config_file))
    try:
        return int(conn.execute("SELECT count(*) FROM approval_events").fetchone()[0])
    finally:
        conn.close()


def test_approve_records_the_decision_and_prints_what_it_bound_to(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "approve",
            "--config",
            str(seeded),
            "--record-id",
            RECORD_ID,
            "--actor",
            "chris",
            "--note",
            "reviewed",
        ]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    # The summary is printed *before* the write, so the operator can see what a decision
    # is binding to rather than taking the record_id on trust.
    assert RECORD_ID in out
    assert "minibench" in out
    assert "status:    validated" in out
    assert _stored_hash(seeded) in out
    assert "approved rec-1" in out
    assert _status(seeded) == "approved"
    # M2-707: what the decision authorized, printed in full. It comes *after* the write
    # since round 1 -- `approve` derives the payload inside the transaction that records the
    # decision, so this line is the derivation that was stored rather than a second run of
    # the same function. The digest alone would say two payloads differ; the JSON says how.
    assert "payload:   sha256 " in out
    assert json.dumps(BINARY_PAYLOAD, ensure_ascii=True, sort_keys=True, separators=(",", ":")) in (
        out
    )


def test_a_record_with_no_buildable_payload_cannot_be_approved_but_can_be_rejected(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The asymmetry `011` encodes, driven from the command line (M2-707).

    An approval authorizes one payload, so a record that derives none cannot be approved --
    and no decision may survive the refusal. Since round 1 the derivation runs inside
    `approve`'s own transaction rather than in this command, so what makes that true is the
    rollback, and the approval count below is what asserts it. A rejection authorizes
    nothing, so it must still be available: a record nobody can submit is exactly the record
    an operator most needs to be able to reject, and requiring a payload hash for both
    decisions would have made it permanently undecidable.

    The record is a real one: the response schema admits a probability of 0.0 and Metaculus
    does not, so this is a forecast the pipeline can legitimately store and no approval can
    ever bind to.
    """
    _seed_ledger(_ledger_path(config_file), probability=0.0)
    code = main(
        ["approve", "--config", str(config_file), "--record-id", RECORD_ID, "--actor", "chris"]
    )
    out = capsys.readouterr().out
    assert code == EXIT_REFUSED
    assert "refused:" in out
    assert "Metaculus would accept" in out
    assert _status(config_file) == "validated"
    assert _isolated_approval_count(_ledger_path(config_file)) == 0

    code = main(
        ["reject", "--config", str(config_file), "--record-id", RECORD_ID, "--actor", "chris"]
    )
    assert code == EXIT_OK
    assert "rejected rec-1" in capsys.readouterr().out
    assert _status(config_file) == "validated"


def test_reject_leaves_the_record_validated(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["reject", "--config", str(seeded), "--record-id", RECORD_ID, "--actor", "chris"])
        == EXIT_OK
    )
    assert "rejected rec-1" in capsys.readouterr().out
    assert _status(seeded) == "validated"


def test_actor_is_required(seeded: Path) -> None:
    """Nothing is inferred from the OS login; an approval names a person or fails."""
    with pytest.raises(SystemExit) as excinfo:
        main(["approve", "--config", str(seeded), "--record-id", RECORD_ID])
    assert excinfo.value.code == 2


def test_a_supplied_hash_that_does_not_match_refuses_and_writes_nothing(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "approve",
            "--config",
            str(seeded),
            "--record-id",
            RECORD_ID,
            "--actor",
            "chris",
            "--forecast-sha256",
            OTHER_SHA,
        ]
    )
    assert code == EXIT_REFUSED
    out = capsys.readouterr().out
    assert "refused:" in out
    assert "the forecast changed and any prior approval no longer binds" in out
    assert _approval_count(seeded) == 0
    assert _status(seeded) == "validated"


def test_a_supplied_hash_that_matches_is_accepted(seeded: Path) -> None:
    code = main(
        [
            "approve",
            "--config",
            str(seeded),
            "--record-id",
            RECORD_ID,
            "--actor",
            "chris",
            "--forecast-sha256",
            _stored_hash(seeded),
        ]
    )
    assert code == EXIT_OK
    assert _status(seeded) == "approved"


def test_an_unknown_record_is_refused(seeded: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["approve", "--config", str(seeded), "--record-id", "no-such", "--actor", "chris"])
    assert code == EXIT_REFUSED
    assert "does not name a stored forecast record" in capsys.readouterr().out


def test_a_missing_ledger_is_refused_rather_than_created(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mistyped --config must not mint an empty ledger and report "no such record".

    That failure is a true statement about the wrong database, which is worse than an
    error: it looks like an answer.
    """
    path = _ledger_path(config_file)
    assert not path.exists()
    code = main(
        ["approve", "--config", str(config_file), "--record-id", RECORD_ID, "--actor", "chris"]
    )
    assert code == EXIT_REFUSED
    assert "no ledger database at" in capsys.readouterr().out
    assert not path.exists()


def test_a_ledger_that_disappears_after_the_check_is_refused_not_recreated(
    seeded: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The check-then-open race, made deterministic (review round 1, finding 1).

    `_open_existing_ledger` checks that the file exists and then opens it. Both opens
    used to be creation-capable, so an ordinary deletion or rotation in between -- a
    backup, a rotation, an operator clearing a path -- minted a fresh empty ledger and
    the command reported "does not name a stored forecast record" against it. That is
    the same wrong-ledger answer the existence check exists to prevent, reached from the
    other side, and it is worse than an error because it looks like an answer.

    The monkeypatch only makes the timing deterministic; the simulated condition is an
    ordinary file disappearance, which CLAUDE.md's threat boundary keeps in scope.
    """
    path = _ledger_path(seeded)
    real_is_file = Path.is_file

    def vanishing_is_file(self: Path) -> bool:
        result = real_is_file(self)
        if self == path and result:
            # The window: the check has already answered truthfully.
            for sidecar in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                sidecar.unlink(missing_ok=True)
        return result

    monkeypatch.setattr(Path, "is_file", vanishing_is_file)

    code = main(["approve", "--config", str(seeded), "--record-id", RECORD_ID, "--actor", "chris"])

    assert code == EXIT_REFUSED
    # Nothing was created to stand in for what vanished -- not the database, and not
    # the parent directory the create path would have made.
    assert not path.exists()
    out = capsys.readouterr().out
    assert "cannot open ledger database at" in out
    # The assertion the finding turns on: on the pre-fix code the command got a live
    # connection to a brand-new empty ledger and printed this instead.
    assert "does not name a stored forecast record" not in out


def test_a_ledger_rotated_mid_command_is_not_the_one_that_gets_approved(
    seeded: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verification and the decision must land on one database (round 2, finding 1).

    The command used to verify the ledger through one connection and then reopen the
    pathname for the decision. Refusing to create catches nothing here -- both files
    exist -- so an ordinary atomic rotation in between (a backup, a restore, a rename)
    meant the schema that was checked and the database that was written were different
    files. The command exited 0, printed the *replacement's* hash, and appended an
    immutable approval to a ledger it had never read.

    Opening once and holding the connection makes the rotation harmless: the descriptor
    still refers to the database that was verified and shown to the operator.
    """
    from whiskeyjack_bot import ledger as ledger_module

    path = _ledger_path(seeded)
    verified_hash = _stored_hash(seeded)
    replacement = path.parent / "replacement.sqlite3"
    # A different `attempt_id` makes a different record and therefore a different hash --
    # the two ledgers have to be distinguishable by what the command prints.
    replacement_hash = _seed_ledger(replacement, attempt_id="attempt-2")
    assert replacement_hash != verified_hash
    archive = path.parent / "rotated.sqlite3"

    real_connect = ledger_module.connect
    opened = 0

    def rotating_connect(target: Path, *, create: bool = True) -> sqlite3.Connection:
        nonlocal opened
        conn = real_connect(target, create=create)
        opened += 1
        if opened == 1 and Path(target) == path:
            # The window: the ledger is verified through this connection, and the
            # pathname is handed to another database before anything is read from it.
            path.replace(archive)
            replacement.replace(path)
        return conn

    monkeypatch.setattr(ledger_module, "connect", rotating_connect)

    code = main(["approve", "--config", str(seeded), "--record-id", RECORD_ID, "--actor", "chris"])

    assert code == EXIT_OK
    out = capsys.readouterr().out
    # The hash the operator was shown is the verified ledger's, not the replacement's.
    assert verified_hash in out
    assert replacement_hash not in out
    # And the replacement -- read in isolation, so the verified ledger's stranded WAL
    # cannot replay into it -- never received the decision.
    assert _isolated_approval_count(path) == 0


def test_an_invalid_config_is_told_apart_from_a_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID

    bad = tmp_path / "config.yaml"
    bad.write_text("environment: development\n", encoding="utf-8")
    code = main(["approve", "--config", str(bad), "--record-id", RECORD_ID, "--actor", "chris"])
    assert code == EXIT_CONFIG_INVALID
    assert capsys.readouterr().out


def test_neither_command_reaches_the_network(seeded: Path) -> None:
    """Approval is a ledger write. The submission gateway is M2-703/M2-704.

    `tests/unit/conftest.py` blocks socket.connect for every test in this directory, so a
    command that tried would fail here rather than at a review.
    """
    assert (
        main(["approve", "--config", str(seeded), "--record-id", RECORD_ID, "--actor", "chris"])
        == EXIT_OK
    )
