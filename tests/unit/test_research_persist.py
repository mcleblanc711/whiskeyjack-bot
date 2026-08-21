"""M1-312 acceptance: a paid run stays recorded even when its raw artifact is lost.

The criterion in one sentence -- *one persistence API attempts artifact storage and still
commits the run and its documents with ``raw_response_path`` NULL after an ordinary
artifact I/O failure, and reports the audit loss to its caller rather than swallowing it*
-- is asserted by the three ``does_not_lose_the_paid_run`` tests below, each driven by a
**real** failure rather than a monkeypatched one: a destination that already exists, an
unwritable directory, and a run id the artifact layout refuses.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from whiskeyjack_bot.config import AppConfig, load_config
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.research.artifacts import artifact_relative_path, read_raw_responses
from whiskeyjack_bot.research.model import ResearchDocument, ResearchRun, validate_document
from whiskeyjack_bot.research.persist import (
    PaidRunPersistence,
    persist_paid_run,
)
from whiskeyjack_bot.research.store import StoreError, load_documents, load_run, open_run

WHEN = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 18, 12, 5, tzinfo=timezone.utc)
QUESTION = 42
BODIES: list[dict[str, Any]] = [{"articles": [{"title": "one"}]}, {"articles": []}]

# Low-entropy on purpose: a realistic-looking key would trip the repository's gitleaks
# full-history scan on every unrelated PR (docs/LESSONS.md).
PLANTED = "privateFAKE123456"

_ROOT_ONLY = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="an unwritable directory does not stop root",
)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    root.mkdir()
    return root


def _config(tmp_path: Path, artifact_root: Path, **overrides: object) -> AppConfig:
    """Build a real AppConfig off config.example.yaml, rooted in the test's tmp_path.

    Overrides are addressed as ``section__key`` so a test can turn one retention flag off
    without restating either section.
    """
    data = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    data["model"]["name"] = "openai/gpt-4o"
    data["retrieval"]["social"]["agent_model"] = "grok-3"
    data["storage"]["artifact_root"] = str(artifact_root)
    for name, value in overrides.items():
        section, _, key = name.partition("__")
        data[section][key] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path)


def _run(**overrides: Any) -> ResearchRun:
    payload: dict[str, Any] = {
        "retrieval_run_id": "run-1",
        "question_id": QUESTION,
        "provider": "asknews",
        "provider_config": {"hours_back": 720, "strategy": "news knowledge"},
        "queries": ["inflation", "cpi release"],
        "started_at_utc": WHEN,
        "completed_at_utc": LATER,
        "cost_usd": 0.5,
    }
    payload.update(overrides)
    return ResearchRun.model_validate(payload)


def _document(index: int = 0, **overrides: Any) -> ResearchDocument:
    payload: dict[str, Any] = {
        "retrieval_run_id": "run-1",
        "original_url": f"https://example.org/a{index}",
        "canonical_url": f"https://example.org/a{index}",
        "title": f"Article {index}",
        "retrieved_at_utc": WHEN,
        "source_type": "news",
        "provenance": "direct_api",
        "content_sha256": f"{index:064x}",
    }
    payload.update(overrides)
    return validate_document(payload)


def _stored_path(conn: sqlite3.Connection, run_id: str = "run-1") -> object:
    row = conn.execute(
        "SELECT raw_response_path FROM research_runs WHERE retrieval_run_id = ?", (run_id,)
    ).fetchone()
    assert row is not None, "the paid run was not recorded at all"
    return row["raw_response_path"]


# --- the artifact was written ------------------------------------------------


def test_the_artifact_is_written_and_its_path_recorded(
    ledger: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    result = persist_paid_run(
        ledger,
        _config(tmp_path, artifacts),
        _run(),
        [_document(0), _document(1)],
        raw_responses=BODIES,
        written_at_utc=WHEN,
    )

    relative = artifact_relative_path(question_id=QUESTION, retrieval_run_id="run-1")
    assert result.artifact_outcome == "written"
    assert result.raw_response_path == relative
    assert result.artifact_error is None
    assert len(result.document_ids) == 2
    # The path is on the row, and the file it names really holds the bodies.
    assert _stored_path(ledger) == relative
    assert read_raw_responses(artifacts, relative) == tuple(BODIES)
    assert load_run(ledger, "run-1").raw_response_path == relative
    assert len(load_documents(ledger, "run-1")) == 2


def test_the_artifact_identity_comes_from_the_run_not_the_caller(
    ledger: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """So the envelope and the row it is filed under cannot disagree."""
    persist_paid_run(
        ledger,
        _config(tmp_path, artifacts),
        _run(retrieval_run_id="run-9", question_id=7, provider="exa"),
        [_document(0, retrieval_run_id="run-9")],
        raw_responses=BODIES,
        written_at_utc=WHEN,
    )
    envelope = (artifacts / "research" / "7" / "run-9.json").read_text(encoding="utf-8")
    assert '"retrieval_run_id": "run-9"' in envelope
    assert '"question_id": 7' in envelope
    assert '"provider": "exa"' in envelope


# --- the acceptance criterion: an artifact failure never loses the run -------


def test_an_existing_artifact_does_not_lose_the_paid_run(
    ledger: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """An artifact is never overwritten, so a re-used run id fails the write.

    The point of the assertion is what happens *next*: the run and its documents commit
    anyway, with the path NULL, and the caller is told.
    """
    relative = artifact_relative_path(question_id=QUESTION, retrieval_run_id="run-1")
    destination = artifacts / relative
    destination.parent.mkdir(parents=True)
    destination.write_text("{}", encoding="utf-8")

    result = persist_paid_run(
        ledger,
        _config(tmp_path, artifacts),
        _run(),
        [_document(0)],
        raw_responses=BODIES,
        written_at_utc=WHEN,
    )

    assert result.artifact_outcome == "failed"
    assert result.raw_response_path is None
    assert result.artifact_error is not None
    assert str(destination) in result.artifact_error
    assert result.document_ids != ()
    assert _stored_path(ledger) is None
    assert len(load_documents(ledger, "run-1")) == 1
    # The pre-existing file is untouched: an artifact is evidence, never overwritten.
    assert destination.read_text(encoding="utf-8") == "{}"


@_ROOT_ONLY
def test_an_unwritable_artifact_directory_does_not_lose_the_paid_run(
    ledger: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    artifacts.chmod(0o500)
    try:
        result = persist_paid_run(
            ledger,
            _config(tmp_path, artifacts),
            _run(),
            [_document(0)],
            raw_responses=BODIES,
            written_at_utc=WHEN,
        )
    finally:
        artifacts.chmod(0o700)

    assert result.artifact_outcome == "failed"
    assert result.raw_response_path is None
    assert result.artifact_error is not None
    assert _stored_path(ledger) is None
    assert len(load_documents(ledger, "run-1")) == 1


def test_a_run_id_the_artifact_layout_refuses_does_not_lose_the_paid_run(
    ledger: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """A caller mistake degrades like an I/O failure, because the calls are already paid.

    ``run/1`` is a non-blank, NUL-free identifier, so the model and the ledger's own
    identifier guard both accept it; the artifact layout refuses it because a run id
    becomes a path component. Refusing here would trade a lost artifact for a lost run.
    """
    result = persist_paid_run(
        ledger,
        _config(tmp_path, artifacts),
        _run(retrieval_run_id="run/1"),
        [_document(0, retrieval_run_id="run/1")],
        raw_responses=BODIES,
        written_at_utc=WHEN,
    )

    assert result.artifact_outcome == "failed"
    assert result.artifact_error is not None
    assert _stored_path(ledger, "run/1") is None
    assert len(load_documents(ledger, "run/1")) == 1
    assert not list(artifacts.rglob("*.json"))


def test_a_body_that_is_not_json_does_not_leak_through_the_report_or_the_logs(
    ledger: sqlite3.Connection,
    tmp_path: Path,
    artifacts: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``json.dumps`` names the offending value, so the report is a leak channel too.

    Both channels are asserted: ``caplog`` does not see what logging's own ``%s``
    interpolation writes to stderr (M1-303), so ``capsys`` is checked as well.
    """
    caplog.set_level("WARNING")
    unrenderable: list[dict[str, Any]] = [{"query": PLANTED, "handle": object()}]

    result = persist_paid_run(
        ledger,
        _config(tmp_path, artifacts),
        _run(),
        [_document(0)],
        raw_responses=unrenderable,
        written_at_utc=WHEN,
    )

    assert result.artifact_outcome == "failed"
    assert result.artifact_error is not None
    assert PLANTED not in result.artifact_error
    assert PLANTED not in caplog.text
    captured = capsys.readouterr()
    assert PLANTED not in captured.out + captured.err
    # And the run is still recorded.
    assert _stored_path(ledger) is None
    assert len(load_documents(ledger, "run-1")) == 1


def test_the_audit_loss_is_reported_to_the_logs_as_well_as_the_caller(
    ledger: sqlite3.Connection,
    tmp_path: Path,
    artifacts: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING")
    destination = artifacts / artifact_relative_path(question_id=QUESTION, retrieval_run_id="run-1")
    destination.parent.mkdir(parents=True)
    destination.write_text("{}", encoding="utf-8")

    persist_paid_run(
        ledger,
        _config(tmp_path, artifacts),
        _run(),
        [],
        raw_responses=BODIES,
        written_at_utc=WHEN,
    )
    assert "recording the run without it" in caplog.text


# --- retention is off: not a failure -----------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"retrieval__retain_raw_responses": False},
        {"storage__retain_raw_research": False},
        {"retrieval__retain_raw_responses": False, "storage__retain_raw_research": False},
    ],
    ids=["retrieval-flag-off", "storage-flag-off", "both-off"],
)
def test_either_retention_flag_off_writes_nothing_and_is_not_a_failure(
    ledger: sqlite3.Connection, tmp_path: Path, artifacts: Path, overrides: dict[str, object]
) -> None:
    """The two flags are combined with ``and``: either one off means keep nothing."""
    result = persist_paid_run(
        ledger,
        _config(tmp_path, artifacts, **overrides),
        _run(),
        [_document(0)],
        raw_responses=BODIES,
        written_at_utc=WHEN,
    )

    assert result.artifact_outcome == "retention_disabled"
    assert result.raw_response_path is None
    assert result.artifact_error is None
    assert not list(artifacts.rglob("*"))
    assert _stored_path(ledger) is None
    assert len(load_documents(ledger, "run-1")) == 1


# --- the two-phase path ------------------------------------------------------


def test_a_run_opened_before_the_calls_is_completed_with_its_artifact(
    ledger: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    open_run(ledger, _run(completed_at_utc=None))

    result = persist_paid_run(
        ledger,
        _config(tmp_path, artifacts),
        _run(),
        [_document(0)],
        raw_responses=BODIES,
        written_at_utc=WHEN,
        run_opened=True,
    )

    assert result.artifact_outcome == "written"
    rows = ledger.execute("SELECT COUNT(*) AS n FROM research_runs").fetchone()
    assert rows["n"] == 1, "the opened row was updated, not duplicated"
    assert _stored_path(ledger) == result.raw_response_path
    assert load_run(ledger, "run-1").completed_at_utc is not None


def test_a_run_opened_before_the_calls_is_completed_even_without_its_artifact(
    ledger: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """The criterion holds on the two-phase path too, not only the one-shot one."""
    open_run(ledger, _run(completed_at_utc=None))
    destination = artifacts / artifact_relative_path(question_id=QUESTION, retrieval_run_id="run-1")
    destination.parent.mkdir(parents=True)
    destination.write_text("{}", encoding="utf-8")

    result = persist_paid_run(
        ledger,
        _config(tmp_path, artifacts),
        _run(),
        [_document(0)],
        raw_responses=BODIES,
        written_at_utc=WHEN,
        run_opened=True,
    )

    assert result.artifact_outcome == "failed"
    assert _stored_path(ledger) is None
    assert load_run(ledger, "run-1").completed_at_utc is not None
    assert len(load_documents(ledger, "run-1")) == 1


def test_run_opened_is_checked_as_a_bool_not_for_truthiness(
    ledger: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """M1-306 round 2's ``completed_only=None`` defect, one argument over.

    ``run_opened`` selects which ledger write happens, so a truthy non-bool taking the
    false branch would insert a duplicate rather than complete the opened row.
    """
    for value in ("yes", 1, None):
        with pytest.raises(StoreError, match="run_opened must be a bool"):
            persist_paid_run(
                ledger,
                _config(tmp_path, artifacts),
                _run(),
                [_document(0)],
                raw_responses=BODIES,
                written_at_utc=WHEN,
                run_opened=value,  # type: ignore[arg-type]
            )
    assert ledger.execute("SELECT COUNT(*) AS n FROM research_runs").fetchone()["n"] == 0
    assert not list(artifacts.rglob("*"))


# --- refusals, all before any I/O --------------------------------------------


def test_a_run_already_carrying_a_path_is_refused_before_anything_is_written(
    ledger: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """The store writes the argument and ignores the model's field, so accepting one
    would silently discard the caller's claim about where its evidence lives."""
    with pytest.raises(StoreError, match="mints raw_response_path"):
        persist_paid_run(
            ledger,
            _config(tmp_path, artifacts),
            _run(raw_response_path="research/42/somewhere-else.json"),
            [_document(0)],
            raw_responses=BODIES,
            written_at_utc=WHEN,
        )
    assert ledger.execute("SELECT COUNT(*) AS n FROM research_runs").fetchone()["n"] == 0
    assert not list(artifacts.rglob("*"))


def test_a_run_that_has_not_completed_is_refused(
    ledger: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    with pytest.raises(StoreError, match="completed_at_utc"):
        persist_paid_run(
            ledger,
            _config(tmp_path, artifacts),
            _run(completed_at_utc=None),
            [],
            raw_responses=BODIES,
            written_at_utc=WHEN,
        )
    assert not list(artifacts.rglob("*"))


def test_a_run_that_is_not_a_research_run_is_refused(
    ledger: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    with pytest.raises(StoreError, match="run must be a ResearchRun"):
        persist_paid_run(
            ledger,
            _config(tmp_path, artifacts),
            {"retrieval_run_id": "run-1"},  # type: ignore[arg-type]
            [],
            raw_responses=BODIES,
            written_at_utc=WHEN,
        )


@pytest.mark.parametrize("config", [object(), None], ids=["object", "none"])
def test_a_config_that_is_not_an_appconfig_arrives_as_this_modules_error(
    ledger: sqlite3.Connection, config: object
) -> None:
    with pytest.raises(StoreError, match="config must be an AppConfig"):
        persist_paid_run(
            ledger,
            config,  # type: ignore[arg-type]
            _run(),
            [],
            raw_responses=BODIES,
            written_at_utc=WHEN,
        )


# --- the ledger half is the failure, not an audit loss -----------------------


def test_a_ledger_failure_after_a_written_artifact_raises_and_keeps_the_artifact(
    ledger: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """A ledger failure is not reported, it is raised: there is no record to report it on.

    The artifact it already wrote stays. A file with no row is inert, the next run mints a
    new id, and an artifact is never deleted to tidy one up.
    """
    disabled = _config(tmp_path, artifacts, retrieval__retain_raw_responses=False)
    persist_paid_run(ledger, disabled, _run(), [], raw_responses=BODIES, written_at_utc=WHEN)

    with pytest.raises(StoreError):
        persist_paid_run(
            ledger,
            _config(tmp_path, artifacts),
            _run(),
            [_document(0)],
            raw_responses=BODIES,
            written_at_utc=WHEN,
        )

    relative = artifact_relative_path(question_id=QUESTION, retrieval_run_id="run-1")
    assert (artifacts / relative).exists()
    assert _stored_path(ledger) is None


# --- the report cannot misdescribe what happened -----------------------------


@pytest.mark.parametrize(
    ("path", "outcome", "error"),
    [
        (None, "written", None),
        ("research/42/run-1.json", "failed", "boom"),
        ("research/42/run-1.json", "retention_disabled", None),
        (None, "failed", None),
        (None, "retention_disabled", "boom"),
        (None, "invented", None),
    ],
    ids=[
        "written-no-path",
        "failed-with-path",
        "disabled-with-path",
        "failed-no-error",
        "disabled-with-error",
        "unknown-outcome",
    ],
)
def test_the_result_refuses_to_misreport_what_happened(
    path: str | None, outcome: str, error: str | None
) -> None:
    with pytest.raises(StoreError):
        PaidRunPersistence(
            document_ids=(),
            raw_response_path=path,
            artifact_outcome=outcome,  # type: ignore[arg-type]
            artifact_error=error,
        )
