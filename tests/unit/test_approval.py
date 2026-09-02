"""M2-701: approve/reject commands, the hash binding, and the approval readers.

The writer and the schema were reviewed under M1-603; what is under test here is the
layer above them -- what a decision binds to, what counts as an approval when it is read
back, and that a refused command writes nothing.
"""

from __future__ import annotations

import sqlite3
import sys
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
from whiskeyjack_bot.forecast.record import record_sha256
from whiskeyjack_bot.forecast.store import read_forecast_record
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import LifecycleError, current_status, record_validation
from whiskeyjack_bot.submission_payload import payload_sha256_for_record

from tests.unit.records import (
    CALIBRATION,
    GENERATED_AT_TEXT,
    binary_response,
    build_record,
    seed_record,
)

TS = "2026-08-19T00:00:00.000000+00:00"
OCCURRED = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
# The hash the default seeded record actually stores. It is `record_sha256` of the record
# `_seed_draft` writes, not a hand-picked constant: since M2-707 round 1 `approve` reads the
# record back and derives a payload from it, so the row has to be a real one and its hash is
# whatever its content says. `OTHER_SHA` stays arbitrary -- it exists to be *not* this value.
SHA = record_sha256(build_record(record_id="rec-1"))
OTHER_SHA = "c" * 64
# M2-707: the digest of the payload an approval authorizes. Shape-only here --
# `approval.py` never derives a payload, so any well-formed digest exercises the same
# path a real one does; `tests/unit/test_submission_payload.py` owns the derivation.
PAYLOAD_SHA = "d" * 64
# Planted where a stored value could be reprinted; see the reader test below.
PLANTED_SECRET = "privateFAKE123456"


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
    """Insert a real draft record under a fixed identifier.

    **It writes a genuine `ForecastRecord` now, and M2-707 round 1 is why.** `approve`
    derives the payload the decision authorizes from `record_json`, so the `'{}'`
    placeholder this seeder used to write refuses every approval -- correctly, since a
    record nothing can read is a record nothing can be approved to submit. The identifier
    is fixed rather than minted because these tests address the record by name.

    Returns the `record_id`, as it always did. The record's *hash* is no longer a constant
    this module picks -- it is derived from the record's content -- so `SHA` is computed
    from the same builder rather than declared, and a seeder under a non-default identity
    hashes to something else.
    """
    seed_record(
        conn,
        record_id=record_id,
        question_id=question_id,
        created_at_utc=TS,
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
        generated_at_utc=GENERATED_AT_TEXT,
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
    approved = approve(
        conn,
        record_id=record_id,
        actor="chris",
        occurred_at=OCCURRED,
        note="reviewed the packet",
        calibration=CALIBRATION,
    )
    recorded = approved.decision
    # M2-707 round 1: the digest on the row is the one `approve` derived from the record,
    # and `RecordedApproval` hands back that same derivation rather than leaving a caller to
    # rebuild it. Asserting they agree is asserting the two are one derivation, not two.
    assert recorded.payload_sha256 == approved.authorized.sha256
    assert recorded.decision == "approved"
    assert recorded.actor == "chris"
    assert recorded.note == "reviewed the packet"
    assert recorded.forecast_sha256 == SHA
    assert recorded.occurred_at_utc == "2026-08-19T12:00:00.000000+00:00"
    assert recorded.created_at_utc  # writer-owned; distinct field, always present
    assert current_status(conn, record_id) == "approved"


def test_the_digest_bound_is_the_records_own_and_not_a_callers(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    """M2-707 round 1: the finding, as the test that would have caught it.

    The first cut took a `payload_sha256` argument and argued a wrong value failed closed
    downstream. It did not: every gate compares a submitted payload against the *stored*
    digest, and nothing compared the stored digest against the record -- so a caller who
    passed another payload's digest and then submitted that same payload passed all of them.

    What closes it is that the value is derived from the record, and the two assertions
    below are the two halves of that. The first is the wiring: the digest on the row is the
    one the public builder derives from the record read back out of the ledger -- a mutant
    that stored anything else fails here. The second is that the claim has content: a
    different forecast derives a different digest, so "the record's own" is not a property
    every digest happens to satisfy.
    """
    conn, record_id = validated
    approved = approve(
        conn, record_id=record_id, actor="chris", occurred_at=OCCURRED, calibration=CALIBRATION
    )
    stored_record = read_forecast_record(conn, record_id)
    assert approved.decision.payload_sha256 == payload_sha256_for_record(
        stored_record, calibration=CALIBRATION
    )
    other_forecast = build_record(
        record_id=record_id,
        forecast=binary_response(100, final_prediction={"probability_yes": 0.75}),
    )
    assert (
        payload_sha256_for_record(other_forecast, calibration=CALIBRATION)
        != approved.decision.payload_sha256
    )


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
    approve(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED, calibration=CALIBRATION)

    history = approval_history(conn, record_id)
    assert [record.decision for record in history] == ["rejected", "rejected", "approved"]
    assert [record.event_seq for record in history] == [2, 3, 4]
    assert [record.note for record in history] == ["stale evidence", "still stale", None]


def test_a_record_holds_at_most_one_approval(validated: tuple[sqlite3.Connection, str]) -> None:
    """`approved` is reachable only from `validated`, and nothing returns there."""
    conn, record_id = validated
    approve(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED, calibration=CALIBRATION)
    with pytest.raises(ApprovalError) as excinfo:
        approve(
            conn,
            record_id=record_id,
            actor="chris",
            occurred_at=OCCURRED,
            calibration=CALIBRATION,
        )
    assert "not a legal transition" in str(excinfo.value)
    assert len(approval_history(conn, record_id)) == 1


def test_a_draft_cannot_be_approved(ledger: sqlite3.Connection) -> None:
    record_id = _seed_draft(ledger)
    with pytest.raises(ApprovalError):
        approve(
            ledger,
            record_id=record_id,
            actor="chris",
            occurred_at=OCCURRED,
            calibration=CALIBRATION,
        )
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
        approve(
            ledger,
            record_id=record_id,
            actor="chris",
            occurred_at=OCCURRED,
            calibration=CALIBRATION,
        )
    assert "stores no content hash" in str(excinfo.value)
    assert _counts(ledger) == (0, 0)


def test_an_unknown_record_is_refused_as_this_modules_error(ledger: sqlite3.Connection) -> None:
    with pytest.raises(ApprovalError):
        approve(
            ledger,
            record_id="no-such-record",
            actor="chris",
            occurred_at=OCCURRED,
            calibration=CALIBRATION,
        )


# --- the hash binding ---------------------------------------------------------------


def test_a_supplied_hash_that_matches_is_accepted(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = validated
    recorded = approve(
        conn,
        record_id=record_id,
        actor="chris",
        occurred_at=OCCURRED,
        expected_sha256=SHA,
        calibration=CALIBRATION,
    ).decision
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
            calibration=CALIBRATION,
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
            calibration=CALIBRATION,
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
    approve(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED, calibration=CALIBRATION)
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
        # `payload_sha256` because `011` refuses an approved row without one, which makes
        # this shape sharper rather than weaker: the row satisfies both hash-binding
        # clauses and still moves nothing.
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "note, created_at_utc, payload_sha256) VALUES (?, 'approved', 'usurper', ?, NULL, ?, ?)",
        (record_id, SHA, TS, PAYLOAD_SHA),
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
    approve(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED, calibration=CALIBRATION)
    conn.execute("DROP TRIGGER lifecycle_events_validate_on_insert")
    approval_id = conn.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "note, created_at_utc, payload_sha256) VALUES (?, 'approved', 'chris', ?, NULL, ?, ?)",
        (record_id, SHA, TS, PAYLOAD_SHA),
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


def test_a_stored_payload_digest_that_is_not_a_digest_is_refused_by_the_reader(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    """M2-707. What the reader hands out is compared against a payload hash by its caller.

    A stored value that is not a digest at all would make that comparison answer "not the
    approved payload" -- true, but for a reason no operator could act on, and
    indistinguishable from the ordinary case of submitting the wrong payload. So the shape
    is checked on the way out and the mismatch downstream means only what it says.

    The row is written by the real writer and then rewritten with the append-only block
    dropped, rather than inserted by hand: `011`'s bind trigger fires on INSERT only, so
    an UPDATE is the narrowest way to reach this state and it leaves every other column
    exactly as the writer left it. Values read back out of the ledger are untrusted
    (CLAUDE.md's threat boundary), and "the trigger would have caught it" is not a claim a
    reader gets to make about a row it did not write.

    The value is planted rather than arbitrary, because the second half of the assertion is
    that the refusal does not reprint it.
    """
    conn, record_id = validated
    approve(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED, calibration=CALIBRATION)
    conn.execute("DROP TRIGGER approval_events_block_update")
    conn.execute("UPDATE approval_events SET payload_sha256 = ?", (PLANTED_SECRET,))
    with pytest.raises(ApprovalError) as excinfo:
        effective_approval(conn, record_id)
    assert "stored payload_sha256 is not a 64-character lowercase hex digest" in str(excinfo.value)
    assert PLANTED_SECRET not in str(excinfo.value)


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
    approve(ledger, record_id=first, actor="chris", occurred_at=OCCURRED, calibration=CALIBRATION)

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
        approve(conn, **call, calibration=CALIBRATION)  # type: ignore[arg-type]
    with pytest.raises(ApprovalError):
        reject(conn, **call)  # type: ignore[arg-type]


def test_a_lifecycle_error_never_escapes_as_itself(
    validated: tuple[sqlite3.Connection, str],
) -> None:
    """The module-own-error rule, stated as the exception type a caller may see."""
    conn, record_id = validated
    approve(conn, record_id=record_id, actor="chris", occurred_at=OCCURRED, calibration=CALIBRATION)
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
            calibration=CALIBRATION,
        )
    assert "x" * 20 not in str(excinfo.value)


# --- M1-607: a blank record_id is a caller mistake, not a missing record -------------


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t\n", "\xa0"])
@pytest.mark.parametrize("reader", [read_forecast_summary, approval_history])
def test_a_blank_record_id_is_refused_by_name_rather_than_reported_as_unknown(
    ledger: sqlite3.Connection, blank: str, reader: object
) -> None:
    """M1-607. `_require_text` refuses `''` but `'\\t\\n'` is truthy and reached the query.

    The distinction matters for what the operator is told. These readers raise "no such
    record" for an id that names nothing, and that is the right answer for a real id that
    was never written -- but for a whitespace-only one it describes a caller mistake as a
    missing row, and `006_non_blank_identifiers.sql` guarantees the row it is describing
    can never exist. So the refusal names the field instead.
    """
    # `''` is matched loosely because `_require_text` already refuses it one layer up,
    # with its own message. What this asserts is the outcome both layers owe the caller:
    # a refusal that names `record_id`, never a report that the record is missing.
    with pytest.raises(ApprovalError, match="record_id must (not be blank|be a non-empty)"):
        reader(ledger, blank)  # type: ignore[operator]


def test_the_approval_readers_agree_with_the_schema_on_what_blank_means(
    ledger: sqlite3.Connection,
) -> None:
    """`approval.py` holds its own copy of `_require_identifier`, so it drifts on its own.

    It is duplicated rather than imported because each module owns its sanitized exception
    type -- but a second copy of a rule is a second thing that can be wrong, so the whole
    whitespace set is asserted here too rather than trusting the copy to match
    `lifecycle`'s. Same drift-guard reasoning as the migration-side test: `str.strip()`
    follows the running Python's Unicode data while 006's literal froze when it landed.
    """
    whitespace = [cp for cp in range(sys.maxunicode + 1) if chr(cp).isspace()]
    # A guard on the guard: an empty list would make the loop assert nothing at all.
    assert len(whitespace) == 29
    for cp in whitespace:
        with pytest.raises(ApprovalError, match="record_id must not be blank"):
            read_forecast_summary(ledger, chr(cp) * 3)

    # The other direction: U+200B is not whitespace to Python, so it is a real id that
    # simply names no row -- and must be reported as such, not as a malformed argument.
    with pytest.raises(ApprovalError, match="does not name a stored forecast record"):
        read_forecast_summary(ledger, "​")


def test_a_record_id_with_an_embedded_nul_is_refused(ledger: sqlite3.Connection) -> None:
    # 004's finding B1, on this module's copy: SQLite's `length()` stops counting at a NUL
    # while Python's `len()` does not, so the two layers must simply not accept one.
    with pytest.raises(ApprovalError, match="NUL character"):
        read_forecast_summary(ledger, "a\x00b")
