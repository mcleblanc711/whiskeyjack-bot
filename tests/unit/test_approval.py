"""M2-701: approve/reject commands, the hash binding, and the approval readers.

The writer and the schema were reviewed under M1-603; what is under test here is the
layer above them -- what a decision binds to, what counts as an approval when it is read
back, and that a refused command writes nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from whiskeyjack_bot.approval import (
    ApprovalError,
    ForecastSummary,
    approval_history,
    approve,
    effective_approval,
    read_forecast_summary,
    reject,
)
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import LifecycleError, current_status, record_validation

TS = "2026-08-19T00:00:00.000000+00:00"
OCCURRED = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
SHA = "b" * 64
OTHER_SHA = "c" * 64


def _seed_run(conn: sqlite3.Connection, run_id: str = "run-1") -> None:
    conn.execute(
        "INSERT INTO research_runs (retrieval_run_id, provider, question_id, started_at_utc, "
        "created_at_utc) VALUES (?, 'asknews', 100, ?, ?)",
        (run_id, TS, TS),
    )


def _seed_draft(
    conn: sqlite3.Connection,
    *,
    record_id: str = "rec-1",
    question_id: int = 100,
) -> str:
    """Insert a draft directly: M1-602's record writer does not exist yet.

    The same shape `tests/unit/test_lifecycle.py` uses, including the per-record
    `attempt_id` migration 004 indexes UNIQUE.
    """
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
        "forecast_sha256, attempt_id) "
        "VALUES (?, ?, 'minibench', 1, 'binary', 'draft', 'anthropic', 'claude', 'v1', 'abc', "
        "'run-1', ?, '{}', '{}', ?, ?, ?)",
        (record_id, question_id, TS, TS, SHA, f"att-{record_id}"),
    )
    return record_id


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        _seed_run(conn)
        yield conn
    finally:
        conn.close()


@pytest.fixture
def validated(ledger: sqlite3.Connection) -> tuple[sqlite3.Connection, str]:
    """A record sitting at `validated` -- the only state a decision is legal from."""
    record_id = _seed_draft(ledger)
    record_validation(ledger, record_id=record_id, occurred_at=OCCURRED)
    return ledger, record_id


def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
    approvals = conn.execute("SELECT count(*) FROM approval_events").fetchone()[0]
    events = conn.execute("SELECT count(*) FROM lifecycle_events").fetchone()[0]
    return int(approvals), int(events)


# --- what a decision binds to -------------------------------------------------------


def test_summary_reports_the_derived_status_not_the_stored_one(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = validated
    summary = read_forecast_summary(conn, record_id)
    assert summary == ForecastSummary(
        record_id=record_id,
        question_id=100,
        tournament_id="minibench",
        forecast_version=1,
        question_type="binary",
        status="validated",
        forecast_sha256=SHA,
        generated_at_utc=TS,
    )
    # `forecast_records.status` is status-at-creation and is pinned to 'draft' by 003, so
    # a summary reading that column would report 'draft' for the record above.
    stored = conn.execute(
        "SELECT status FROM forecast_records WHERE record_id = ?", (record_id,)
    ).fetchone()[0]
    assert stored == "draft"


def test_summary_is_replay_stable_through_the_persisted_form(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    import json

    conn, record_id = validated
    summary = read_forecast_summary(conn, record_id)
    encoded = json.dumps(asdict(summary), ensure_ascii=True, sort_keys=True)
    assert json.loads(encoded) == asdict(summary)


def test_summary_of_an_unknown_record_raises(ledger: sqlite3.Connection) -> None:
    with pytest.raises(ApprovalError) as excinfo:
        read_forecast_summary(ledger, "no-such-record")
    assert "does not name a stored forecast record" in str(excinfo.value)


# --- approving and rejecting --------------------------------------------------------


def test_approval_moves_the_record_and_retains_actor_timestamp_and_note(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = validated
    recorded = approve(
        conn, record_id=record_id, actor="chris", occurred_at=OCCURRED, note="reviewed the packet"
    )
    assert recorded.decision == "approved"
    assert recorded.actor == "chris"
    assert recorded.note == "reviewed the packet"
    assert recorded.forecast_sha256 == SHA
    assert recorded.occurred_at_utc == "2026-08-19T12:00:00.000000+00:00"
    assert recorded.created_at_utc  # writer-owned; distinct field, always present
    assert current_status(conn, record_id) == "approved"


def test_rejection_records_a_decision_without_moving_the_record(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = validated
    recorded = reject(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED)
    assert recorded.decision == "rejected"
    assert recorded.note is None
    assert current_status(conn, record_id) == "validated"


def test_a_record_may_be_rejected_repeatedly_and_then_approved(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = validated
    reject(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED, note="stale evidence")
    reject(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED, note="still stale")
    approve(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED)

    history = approval_history(conn, record_id)
    assert [record.decision for record in history] == ["rejected", "rejected", "approved"]
    assert [record.event_seq for record in history] == [2, 3, 4]
    assert [record.note for record in history] == ["stale evidence", "still stale", None]


def test_a_record_holds_at_most_one_approval(validated: tuple[sqlite3.Connection, str]) -> None:
    """`approved` is reachable only from `validated`, and nothing returns there."""
    conn, record_id = validated
    approve(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED)
    with pytest.raises(ApprovalError) as excinfo:
        approve(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED)
    assert "not a legal transition" in str(excinfo.value)
    assert len(approval_history(conn, record_id)) == 1


def test_a_draft_cannot_be_approved(ledger: sqlite3.Connection) -> None:
    record_id = _seed_draft(ledger)
    with pytest.raises(ApprovalError):
        approve(ledger, record_id=record_id, actor="chris", occurred_at=OCCURRED)
    assert _counts(ledger) == (0, 0)


def test_a_record_with_no_stored_hash_cannot_be_decided_on(ledger: sqlite3.Connection) -> None:
    """The pre-003 population: an honest NULL hash, readable and unapprovable."""
    record_id = _seed_draft(ledger)
    # 003 refuses a NULL hash on a new row and refuses every UPDATE, so this population
    # cannot be created through the schema at all -- which is the point: it is the set of
    # rows that predate the migration. Dropping the update block is how the test reaches
    # it, and it is the only way to.
    ledger.execute("DROP TRIGGER forecast_records_block_update")
    ledger.execute(
        "UPDATE forecast_records SET forecast_sha256 = NULL WHERE record_id = ?", (record_id,)
    )
    with pytest.raises(ApprovalError) as excinfo:
        approve(ledger, record_id=record_id, actor="chris", occurred_at=OCCURRED)
    assert "stores no content hash" in str(excinfo.value)
    assert _counts(ledger) == (0, 0)


def test_an_unknown_record_is_refused_as_this_modules_error(ledger: sqlite3.Connection) -> None:
    with pytest.raises(ApprovalError):
        approve(ledger, record_id="no-such-record", actor="chris", occurred_at=OCCURRED)


# --- the hash binding ---------------------------------------------------------------


def test_a_supplied_hash_that_matches_is_accepted(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = validated
    recorded = approve(
        conn, record_id=record_id, actor="chris", occurred_at=OCCURRED, expected_sha256=SHA
    )
    assert recorded.forecast_sha256 == SHA


def test_a_supplied_hash_that_does_not_match_writes_nothing(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = validated
    with pytest.raises(ApprovalError) as excinfo:
        approve(
            conn,
            record_id=record_id,
            actor="chris",
            occurred_at=OCCURRED,
            expected_sha256=OTHER_SHA,
        )
    assert "the forecast changed and any prior approval no longer binds" in str(excinfo.value)
    assert _counts(conn) == (0, 1)  # the `validated` event only
    assert current_status(conn, record_id) == "validated"


def test_a_mismatch_message_prints_neither_hash(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = validated
    with pytest.raises(ApprovalError) as excinfo:
        approve(
            conn,
            record_id=record_id,
            actor="chris",
            occurred_at=OCCURRED,
            expected_sha256=OTHER_SHA,
        )
    message = str(excinfo.value)
    assert SHA not in message
    assert OTHER_SHA not in message


def test_a_malformed_supplied_hash_is_told_apart_from_a_changed_forecast(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = validated
    with pytest.raises(ApprovalError) as excinfo:
        reject(
            conn,
            record_id=record_id,
            actor="chris",
            occurred_at=OCCURRED,
            expected_sha256="not-a-digest",
        )
    assert "64 lowercase hexadecimal characters" in str(excinfo.value)
    assert "not-a-digest" not in str(excinfo.value)


def test_a_changed_forecast_is_a_new_record_with_no_approval(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    """The structural half of "changed forecast invalidates prior approval"."""
    conn, record_id = validated
    approve(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED)
    assert effective_approval(conn, record_id) is not None

    # A changed forecast is a new version, which is a new record with its own hash.
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, parent_record_id, "
        "question_type, status, model_provider, model_name, prompt_version, prompt_sha256, "
        "retrieval_run_id, generated_at_utc, final_prediction_json, record_json, "
        "created_at_utc, forecast_sha256, attempt_id) "
        "VALUES ('rec-2', 100, 'minibench', 2, ?, 'binary', 'draft', 'anthropic', 'claude', "
        "'v1', 'abc', 'run-1', ?, '{}', '{}', ?, ?, 'att-rec-2')",
        (record_id, TS, TS, OTHER_SHA),
    )
    assert effective_approval(conn, "rec-2") is None
    assert approval_history(conn, "rec-2") == ()
    # ... and the approval of v1 is untouched by v2 existing.
    assert effective_approval(conn, record_id) is not None


# --- what counts as an approval when it is read back --------------------------------


def test_an_approval_row_no_lifecycle_event_cites_is_not_an_approval(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    """A decision the ledger never acted on must not be credited as one.

    `tests/unit/test_lifecycle.py` proves such a row can be written by raw SQL: it passes
    003's hash-binding trigger and still moves nothing, because status is derived from
    `lifecycle_events`.
    """
    conn, record_id = validated
    conn.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "note, created_at_utc) VALUES (?, 'approved', 'usurper', ?, NULL, ?)",
        (record_id, SHA, TS),
    )
    assert effective_approval(conn, record_id) is None
    assert approval_history(conn, record_id) == ()
    assert current_status(conn, record_id) == "validated"


def test_effective_approval_ignores_a_rejection(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = validated
    reject(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED)
    assert effective_approval(conn, record_id) is None
    assert len(approval_history(conn, record_id)) == 1


def test_effective_approval_refuses_an_inconsistent_history(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    """Two approvals cannot be written through the schema; a corrupt ledger says so.

    Values read back out of the ledger are untrusted (CLAUDE.md's threat boundary), so the
    reader must not silently answer with one of two contradicting rows. Reached by
    dropping the validating trigger, which is the only way this state exists at all.
    """
    conn, record_id = validated
    approve(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED)
    conn.execute("DROP TRIGGER lifecycle_events_validate_on_insert")
    approval_id = conn.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "note, created_at_utc) VALUES (?, 'approved', 'chris', ?, NULL, ?)",
        (record_id, SHA, TS),
    ).lastrowid
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, from_status, "
        "to_status, approval_event_id, occurred_at_utc, created_at_utc) "
        "VALUES (?, 3, 'approved', 'validated', 'approved', ?, ?, ?)",
        (record_id, approval_id, TS, TS),
    )
    with pytest.raises(ApprovalError) as excinfo:
        effective_approval(conn, record_id)
    assert "more than one approval event" in str(excinfo.value)


def test_history_of_an_unknown_record_raises(ledger: sqlite3.Connection) -> None:
    """The same answer `current_status` and `read_history` give, for their reason."""
    with pytest.raises(ApprovalError):
        approval_history(ledger, "no-such-record")
    with pytest.raises(ApprovalError):
        effective_approval(ledger, "no-such-record")


def test_history_ignores_another_records_decisions(ledger: sqlite3.Connection) -> None:
    first = _seed_draft(ledger, record_id="rec-1", question_id=100)
    second = _seed_draft(ledger, record_id="rec-2", question_id=101)
    record_validation(ledger, record_id=first, occurred_at=OCCURRED)
    record_validation(ledger, record_id=second, occurred_at=OCCURRED)
    approve(ledger, record_id=first, actor="chris", occurred_at=OCCURRED)

    assert [record.forecast_record_id for record in approval_history(ledger, first)] == [first]
    assert approval_history(ledger, second) == ()


# --- error hygiene ------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"record_id": ""},
        {"record_id": "a" * 201},
        {"record_id": "rec-\ud800"},
        {"record_id": 42},
        {"actor": ""},
        {"actor": "\ud800"},
        {"actor": 42},
        {"note": 42},
        {"note": "\ud800"},
        {"occurred_at": "2026-08-19"},
        {"occurred_at": datetime(2026, 8, 19, 12, 0)},
        {"expected_sha256": ""},
        {"expected_sha256": "B" * 64},
        {"expected_sha256": 42},
    ],
    ids=str,
)
def test_every_malformed_shape_arrives_as_an_approval_error(
    validated: tuple[sqlite3.Connection, str], kwargs: dict[str, object]
) -> None:
    """No `LifecycleError`, no raw sqlite3/Unicode/Type error escapes either command."""
    conn, record_id = validated
    call: dict[str, object] = {
        "record_id": record_id,
        "actor": "chris",
        "occurred_at": OCCURRED,
        **kwargs,
    }
    with pytest.raises(ApprovalError):
        approve(conn, **call)  # type: ignore[arg-type]
    with pytest.raises(ApprovalError):
        reject(conn, **call)  # type: ignore[arg-type]


def test_a_lifecycle_error_never_escapes_as_itself(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    """The module-own-error rule, stated as the exception type a caller may see."""
    conn, record_id = validated
    approve(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED)
    with pytest.raises(ApprovalError) as excinfo:
        reject(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED)
    assert not isinstance(excinfo.value, LifecycleError)
    # ... and the underlying message survives, because it is what makes it actionable.
    assert "rejected" in str(excinfo.value)


def test_a_note_value_is_never_echoed_by_a_refusal(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = validated
    with pytest.raises(ApprovalError) as excinfo:
        approve(
            conn,
            record_id=record_id,
            actor="chris",
            occurred_at=OCCURRED,
            note="x" * 4001,
            expected_sha256=OTHER_SHA,
        )
    assert "x" * 20 not in str(excinfo.value)
