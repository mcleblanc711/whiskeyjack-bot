"""M2-701: the `approve` and `reject` commands at the CLI boundary.

What is under test here is the command layer's own behaviour -- required arguments, the
summary printed before anything is written, the exit code a refusal produces, and that a
mistyped `--config` cannot silently mint an empty ledger. The decision semantics
themselves are `tests/unit/test_approval.py`.
"""

from __future__ import annotations

import copy
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from whiskeyjack_bot.cli import EXIT_REFUSED, main
from whiskeyjack_bot.env_verify import EXIT_OK
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import current_status, record_validation

REPO_ROOT = Path(__file__).resolve().parents[2]

TS = "2026-08-19T00:00:00.000000+00:00"
OCCURRED = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
SHA = "b" * 64
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


def _seed_ledger(path: Path, forecast_sha256: str) -> None:
    """A ledger holding one record at `validated`, bound to `forecast_sha256`."""
    initialize_ledger(path)
    conn = connect(path)
    try:
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, question_id, "
            "started_at_utc, created_at_utc) VALUES ('run-1', 'asknews', 100, ?, ?)",
            (TS, TS),
        )
        conn.execute(
            "INSERT INTO forecast_records ("
            "record_id, question_id, tournament_id, forecast_version, question_type, status, "
            "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
            "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
            "forecast_sha256, attempt_id) "
            "VALUES (?, 100, 'minibench', 1, 'binary', 'draft', 'anthropic', 'claude', 'v1', "
            "'abc', 'run-1', ?, '{}', '{}', ?, ?, 'att-rec-1')",
            (RECORD_ID, TS, TS, forecast_sha256),
        )
        record_validation(conn, record_id=RECORD_ID, occurred_at=OCCURRED)
    finally:
        conn.close()


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
    _seed_ledger(_ledger_path(config_file), SHA)
    return config_file


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
    assert SHA in out
    assert "approved rec-1" in out
    assert _status(seeded) == "approved"


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
            SHA,
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
    replacement = path.parent / "replacement.sqlite3"
    _seed_ledger(replacement, OTHER_SHA)
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
    assert SHA in out
    assert OTHER_SHA not in out
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
