"""M1-603 acceptance: lifecycle events are recorded atomically, and an approved or
submitted state cannot exist without its event record.

The criterion has two halves and they are tested differently. That a *state* cannot
exist without its event is a property of the schema -- there is nowhere else for the
state to be written -- so it is tested against the database, with raw SQL, bypassing
:mod:`whiskeyjack_bot.lifecycle` entirely. That a detail row and its lifecycle row
cannot be observed apart is a property of the writers, so it is tested by making the
second write fail, both with an injected exception and (better, because nothing is
mocked) by asking for a transition the machine does not allow after the detail row has
already gone in.
"""

import hashlib
import json
import sqlite3
import traceback
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from typing import get_args

import pytest

from whiskeyjack_bot import lifecycle
from whiskeyjack_bot.ledger import LEDGER_SCHEMA_VERSION, connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    LifecycleEvent,
    LifecycleEventType,
    LifecycleStatus,
    SubmissionAttempt,
    current_status,
    read_history,
    record_approval,
    record_failure,
    record_submission_attempt,
    record_validation,
    transaction,
)


def _checksum_of(name: str) -> str:
    """The checksum ledger.py records, computed the same way it computes it."""
    return hashlib.sha256(
        files("whiskeyjack_bot.migrations").joinpath(name).read_bytes()
    ).hexdigest()


TS = "2026-07-27T00:00:00+00:00"
WHEN = datetime(2026, 7, 27, tzinfo=timezone.utc)
SHA = "b" * 64
OTHER_SHA = "c" * 64
PAYLOAD_SHA = "d" * 64

# Low-entropy by convention: a realistic-looking secret in a tracked file fails the
# gitleaks full-history scan on every branch, not just this one.
PLANTED_SECRET = "privateFAKE123456"

STATUSES: tuple[str, ...] = get_args(LifecycleStatus)
EVENT_TYPES: tuple[str, ...] = get_args(LifecycleEventType)

# Tables migration 003 closes to UPDATE and DELETE alike, with a row of each already
# present so a FOR EACH ROW trigger has something to fire on -- `DELETE FROM t` against
# an empty table succeeds, and would read as a passing test of a trigger that never ran.
APPEND_ONLY_TABLES = (
    "forecast_records",
    "lifecycle_events",
    "approval_events",
    "submission_attempts",
    "resolution_events",
    "score_events",
)

# One existing, nullable column per table, so the UPDATE probe below is a well-formed
# statement that only the append-only trigger can be refusing.
UPDATABLE_COLUMN = {
    "forecast_records": "record_json",
    "lifecycle_events": "occurred_at_utc",
    "approval_events": "note",
    "submission_attempts": "response_body",
    "resolution_events": "outcome",
    "score_events": "comparison_baseline",
}


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
    forecast_sha256: str | None = SHA,
) -> str:
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, forecast_sha256) "
        "VALUES (?, ?, 'minibench', 1, 'binary', 'draft', 'anthropic', 'claude', 'v1', 'abc', "
        "'run-1', ?, '{}', '{}', ?, ?)",
        (record_id, question_id, TS, TS, forecast_sha256),
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
def draft(ledger: sqlite3.Connection) -> tuple[sqlite3.Connection, str]:
    return ledger, _seed_draft(ledger)


def _attempt(
    *,
    attempt_id: str = "att-1",
    key: str = "idem-1",
    success: bool = True,
    verified: bool = True,
    **extra: object,
) -> SubmissionAttempt:
    return SubmissionAttempt(
        attempt_id=attempt_id,
        idempotency_key=key,
        requested_at_utc=WHEN,
        request_payload_sha256=PAYLOAD_SHA,
        success=success,
        verified_by_refetch=verified,
        **extra,  # type: ignore[arg-type]
    )


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in APPEND_ONLY_TABLES
    }


# --------------------------------------------------------------------------------------
# The state cannot exist without the event: schema-level, no writers involved.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["validated", "approved", "submitted", "resolved", "scored"])
def test_a_new_record_cannot_be_created_in_a_later_state(
    ledger: sqlite3.Connection, status: str
) -> None:
    # This is the acceptance criterion at its sharpest. Without the draft-only trigger a
    # writer could INSERT a row already claiming `approved`, satisfy every other
    # constraint in the schema, and produce an approved state with no approval event.
    with pytest.raises(sqlite3.IntegrityError):
        ledger.execute(
            "INSERT INTO forecast_records ("
            "record_id, question_id, tournament_id, forecast_version, question_type, status, "
            "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
            "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
            "forecast_sha256) "
            "VALUES ('rec-x', 100, 'minibench', 1, 'binary', ?, 'anthropic', 'claude', 'v1', "
            "'abc', 'run-1', ?, '{}', '{}', ?, ?)",
            (status, TS, TS, SHA),
        )


@pytest.mark.parametrize("bad_hash", [None, "", "ZZ" * 32, "b" * 63, "B" * 64])
def test_a_new_record_must_carry_a_content_hash(
    ledger: sqlite3.Connection, bad_hash: str | None
) -> None:
    # Approval binds to an exact forecast hash. A record with no hash, or a hash that is
    # not a sha256 digest, has nothing for an approval to bind to.
    with pytest.raises(sqlite3.IntegrityError):
        _seed_draft(ledger, record_id="rec-bad", forecast_sha256=bad_hash)


def test_an_approval_row_alone_does_not_move_the_record(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # Writing straight to approval_events -- bypassing this module entirely -- records a
    # decision but cannot produce an approved *state*, because the state lives in the
    # event log and nowhere else.
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    conn.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "created_at_utc) VALUES (?, 'approved', 'chris', ?, ?)",
        (record_id, SHA, TS),
    )
    assert current_status(conn, record_id) == "validated"


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_ledger_rows_can_be_neither_updated_nor_deleted(
    ledger: sqlite3.Connection, table: str
) -> None:
    conn = ledger
    record_id = _seed_draft(conn)
    _walk_to(conn, record_id, "scored")
    assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE {table} SET {UPDATABLE_COLUMN[table]} = ?", ("changed",))


def test_evidence_rows_may_be_completed_but_never_deleted(ledger: sqlite3.Connection) -> None:
    # The deliberate asymmetry: D25 names forecast versions and lifecycle events, and a
    # research run is a row M1-306 starts and later finishes. Completable, not erasable.
    ledger.execute(
        "INSERT INTO research_documents (document_id, retrieval_run_id, original_url, "
        "canonical_url, retrieved_at_utc, source_type, provenance, content_sha256) "
        "VALUES ('doc-1', 'run-1', 'https://example.test/a', 'https://example.test/a', ?, "
        "'news', 'direct_api', 'hash-1')",
        (TS,),
    )
    ledger.execute("UPDATE research_runs SET completed_at_utc = ?", (TS,))
    with pytest.raises(sqlite3.IntegrityError):
        ledger.execute("DELETE FROM research_runs")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.execute("DELETE FROM research_documents")


# --------------------------------------------------------------------------------------
# The transition table, driven through the database.
# --------------------------------------------------------------------------------------


def _detail_rows(conn: sqlite3.Connection, record_id: str, suffix: str) -> dict[str, object]:
    """Create one valid detail row of every kind, so only the transition can be at fault."""
    approved = conn.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "created_at_utc) VALUES (?, 'approved', 'chris', ?, ?)",
        (record_id, SHA, TS),
    ).lastrowid
    rejected = conn.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "created_at_utc) VALUES (?, 'rejected', 'chris', ?, ?)",
        (record_id, SHA, TS),
    ).lastrowid
    for attempt_id, verified in ((f"att-ok-{suffix}", 1), (f"att-bad-{suffix}", 0)):
        conn.execute(
            "INSERT INTO submission_attempts (attempt_id, forecast_record_id, idempotency_key, "
            "requested_at_utc, request_payload_sha256, success, verified_by_refetch, "
            "created_at_utc) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (attempt_id, record_id, f"idem-{attempt_id}", TS, PAYLOAD_SHA, verified, TS),
        )
    resolution = conn.execute(
        "INSERT INTO resolution_events (question_id, forecast_record_id, ingested_at_utc) "
        "VALUES (100, ?, ?)",
        (record_id, TS),
    ).lastrowid
    score = conn.execute(
        "INSERT INTO score_events (forecast_record_id, metric, value, implementation_version, "
        "computed_at_utc) VALUES (?, 'brier', 0.25, 'v1', ?)",
        (record_id, TS),
    ).lastrowid
    return {
        "approved": approved,
        "rejected": rejected,
        "att_ok": f"att-ok-{suffix}",
        "att_bad": f"att-bad-{suffix}",
        "resolved": resolution,
        "scored": score,
    }


def _insert_event(
    conn: sqlite3.Connection,
    record_id: str,
    event_type: str,
    from_status: str,
    to_status: str,
    detail: dict[str, object],
) -> None:
    """Insert one lifecycle row with a valid detail link for its event type."""
    seq = conn.execute(
        "SELECT coalesce(max(event_seq), 0) + 1 FROM lifecycle_events WHERE forecast_record_id = ?",
        (record_id,),
    ).fetchone()[0]
    links: dict[str, object] = {
        "approval_event_id": None,
        "submission_attempt_id": None,
        "resolution_event_id": None,
        "score_event_id": None,
    }
    if event_type in ("approved", "rejected"):
        links["approval_event_id"] = detail[event_type]
    elif event_type == "submitted":
        links["submission_attempt_id"] = detail["att_ok"]
    elif event_type == "submission_failed":
        links["submission_attempt_id"] = detail["att_bad"]
    elif event_type == "resolved":
        links["resolution_event_id"] = detail["resolved"]
    elif event_type == "scored":
        links["score_event_id"] = detail["scored"]
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, from_status, "
        "to_status, detail_code, approval_event_id, submission_attempt_id, resolution_event_id, "
        "score_event_id, occurred_at_utc, created_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record_id,
            seq,
            event_type,
            from_status,
            to_status,
            "internal_error" if to_status == "failed" else None,
            links["approval_event_id"],
            links["submission_attempt_id"],
            links["resolution_event_id"],
            links["score_event_id"],
            TS,
            TS,
        ),
    )


# The legitimate route to each of the seven states, walked with raw SQL so the walk
# itself does not depend on the module under test.
_ROUTES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "draft": (),
    "validated": (("validated", "draft", "validated"),),
    "failed": (("validation_failed", "draft", "failed"),),
    "approved": (("validated", "draft", "validated"), ("approved", "validated", "approved")),
    "submitted": (
        ("validated", "draft", "validated"),
        ("approved", "validated", "approved"),
        ("submitted", "approved", "submitted"),
    ),
    "resolved": (
        ("validated", "draft", "validated"),
        ("approved", "validated", "approved"),
        ("submitted", "approved", "submitted"),
        ("resolved", "submitted", "resolved"),
    ),
    "scored": (
        ("validated", "draft", "validated"),
        ("approved", "validated", "approved"),
        ("submitted", "approved", "submitted"),
        ("resolved", "submitted", "resolved"),
        ("scored", "resolved", "scored"),
    ),
}


def _walk_to(conn: sqlite3.Connection, record_id: str, status: str) -> dict[str, object]:
    detail = _detail_rows(conn, record_id, record_id)
    for event_type, from_status, to_status in _ROUTES[status]:
        _insert_event(conn, record_id, event_type, from_status, to_status, detail)
    return detail


def test_every_state_is_reachable(ledger: sqlite3.Connection) -> None:
    # A precondition of the exhaustive test below: if a state were unreachable, the
    # triples starting from it would silently never be exercised.
    for index, status in enumerate(STATUSES):
        record_id = f"rec-{status}"
        _seed_draft(ledger, record_id=record_id, question_id=200 + index)
        _walk_to(ledger, record_id, status)
        assert current_status(ledger, record_id) == status


def test_database_accepts_exactly_the_legal_transitions(ledger: sqlite3.Connection) -> None:
    """The migration's trigger and ``_LEGAL_TRANSITIONS`` must describe the same machine.

    Every (event_type, from_status, to_status) triple is attempted against a record
    actually sitting in ``from_status``, inside a savepoint that is rolled back so the
    attempt cannot disturb the next one. What the database accepts is then compared with
    the Python table as a set. The duplication between the two is deliberate -- the
    database is the enforcement, the Python table is the writer -- and this is what stops
    them drifting apart, which is a class of bug no amount of example-based testing over
    the happy path would catch.
    """
    detail: dict[str, dict[str, object]] = {}
    for index, status in enumerate(STATUSES):
        record_id = f"rec-{status}"
        _seed_draft(ledger, record_id=record_id, question_id=300 + index)
        detail[status] = _walk_to(ledger, record_id, status)

    accepted: set[tuple[str, str, str]] = set()
    for event_type in EVENT_TYPES:
        for from_status in STATUSES:
            for to_status in STATUSES:
                ledger.execute("SAVEPOINT probe")
                try:
                    _insert_event(
                        ledger,
                        f"rec-{from_status}",
                        event_type,
                        from_status,
                        to_status,
                        detail[from_status],
                    )
                except sqlite3.IntegrityError:
                    pass
                else:
                    accepted.add((event_type, from_status, to_status))
                finally:
                    ledger.execute("ROLLBACK TO probe")
                    ledger.execute("RELEASE probe")

    assert accepted == set(lifecycle._LEGAL_TRANSITIONS)


def test_destination_table_is_single_valued() -> None:
    # _DESTINATIONS is derived from _LEGAL_TRANSITIONS by a dict comprehension, which
    # would silently drop a duplicate key -- i.e. one event type meaning two different
    # destinations from the same state.
    assert len(lifecycle._DESTINATIONS) == len(lifecycle._LEGAL_TRANSITIONS)


def test_failed_is_terminal(draft: tuple[sqlite3.Connection, str]) -> None:
    # A retry is a new forecast version (M1-602), not a resurrected record.
    conn, record_id = draft
    record_failure(
        conn,
        record_id=record_id,
        event_type="research_failed",
        detail_code="provider_error",
        occurred_at=WHEN,
    )
    assert current_status(conn, record_id) == "failed"
    for event_type in EVENT_TYPES:
        with pytest.raises(LifecycleError):
            lifecycle._append_event(
                conn,
                record_id=record_id,
                event_type=event_type,  # type: ignore[arg-type]
                occurred_at_utc=TS,
            )


# --------------------------------------------------------------------------------------
# Sequence integrity.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("seq", [0, -1, 2, 99, "abc", 1.5])
def test_event_seq_must_be_the_records_next_integer(
    draft: tuple[sqlite3.Connection, str], seq: object
) -> None:
    # 0/-1/2/99 fail the contiguity probe; 'abc' and 1.5 fail the typeof() probe.
    #
    # '1' and 1.0 are deliberately *not* here, and their absence is the point. INTEGER is
    # affinity, not a type: SQLite converts a well-formed integer literal and a lossless
    # REAL to an integer before any trigger sees NEW, so typeof() reports 'integer' and
    # the row is correct rather than rejected. Only values that cannot convert stay in
    # their original type, and those are the ones the probe has to catch -- the same
    # affinity trap 002 documents for posts_dropped_no_url and cost_usd.
    conn, record_id = draft
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
            "from_status, to_status, occurred_at_utc, created_at_utc) "
            "VALUES (?, ?, 'validated', 'draft', 'validated', ?, ?)",
            (record_id, seq, TS, TS),
        )


def test_duplicate_event_seq_is_rejected(draft: tuple[sqlite3.Connection, str]) -> None:
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
            "from_status, to_status, occurred_at_utc, created_at_utc) "
            "VALUES (?, 1, 'validated', 'draft', 'validated', ?, ?)",
            (record_id, TS, TS),
        )


def test_from_status_must_match_the_records_current_status(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # Without this the caller could assert its own starting point and skip a state --
    # claiming draft -> validated on a record that is already approved, say.
    conn, record_id = draft
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
            "from_status, to_status, occurred_at_utc, created_at_utc) "
            "VALUES (?, 1, 'validation_failed', 'validated', 'failed', ?, ?)",
            (record_id, TS, TS),
        )


def test_a_failure_event_must_say_why(draft: tuple[sqlite3.Connection, str]) -> None:
    conn, record_id = draft
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
            "from_status, to_status, occurred_at_utc, created_at_utc) "
            "VALUES (?, 1, 'validation_failed', 'draft', 'failed', ?, ?)",
            (record_id, TS, TS),
        )


# --------------------------------------------------------------------------------------
# The writers: the happy path, then atomicity.
# --------------------------------------------------------------------------------------


def test_the_pipeline_walks_draft_to_submitted(draft: tuple[sqlite3.Connection, str]) -> None:
    conn, record_id = draft
    assert current_status(conn, record_id) == "draft"

    validated = record_validation(conn, record_id=record_id, occurred_at=WHEN)
    assert (validated.event_seq, validated.from_status, validated.to_status) == (
        1,
        "draft",
        "validated",
    )
    assert current_status(conn, record_id) == "validated"

    approved = record_approval(
        conn,
        record_id=record_id,
        decision="approved",
        actor="chris",
        forecast_sha256=SHA,
        occurred_at=WHEN + timedelta(minutes=1),
        note="looks right",
    )
    assert approved.approval_event_id is not None
    assert current_status(conn, record_id) == "approved"

    submitted = record_submission_attempt(
        conn, record_id=record_id, attempt=_attempt(), occurred_at=WHEN + timedelta(minutes=2)
    )
    assert submitted.event_type == "submitted"
    assert current_status(conn, record_id) == "submitted"

    history = read_history(conn, record_id)
    assert [event.event_seq for event in history] == [1, 2, 3]
    assert [event.event_type for event in history] == ["validated", "approved", "submitted"]


def test_a_rejection_records_a_decision_without_moving_the_record(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    rejected = record_approval(
        conn,
        record_id=record_id,
        decision="rejected",
        actor="chris",
        forecast_sha256=SHA,
        occurred_at=WHEN,
        note="needs more evidence",
    )
    assert (rejected.from_status, rejected.to_status) == ("validated", "validated")
    assert current_status(conn, record_id) == "validated"
    # And the record can still be approved afterwards: the last valid record is intact.
    record_approval(
        conn,
        record_id=record_id,
        decision="approved",
        actor="chris",
        forecast_sha256=SHA,
        occurred_at=WHEN,
    )
    assert current_status(conn, record_id) == "approved"


def test_approval_binds_to_the_exact_forecast_hash(draft: tuple[sqlite3.Connection, str]) -> None:
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    with pytest.raises(LifecycleError):
        record_approval(
            conn,
            record_id=record_id,
            decision="approved",
            actor="chris",
            forecast_sha256=OTHER_SHA,
            occurred_at=WHEN,
        )
    assert current_status(conn, record_id) == "validated"
    assert _counts(conn)["approval_events"] == 0


def test_rows_written_before_migration_003_survive_it_but_cannot_be_approved(
    tmp_path: Path,
) -> None:
    """A pre-003 record keeps its honest NULL hash, stays readable, and is unapprovable.

    The same shape 002 settled for ``provenance``: defaulting the column would stamp an
    unearned content claim onto a row nobody hashed, and rejecting the row would make the
    migration undeployable. So the row survives -- and because approval binds to an exact
    hash, a record with none has nothing to bind to. Unapprovable, not
    approvable-by-any-hash.
    """
    db = tmp_path / "ledger.sqlite3"
    legacy = "rec-legacy"
    conn = connect(db)
    try:
        for name in ("001_initial.sql", "002_research_document_fields.sql"):
            conn.executescript(files("whiskeyjack_bot.migrations").joinpath(name).read_text())
        _seed_run(conn)
        conn.execute(
            "INSERT INTO forecast_records ("
            "record_id, question_id, tournament_id, forecast_version, question_type, status, "
            "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
            "generated_at_utc, final_prediction_json, record_json, created_at_utc) "
            "VALUES (?, 101, 'minibench', 1, 'binary', 'draft', 'anthropic', 'claude', 'v1', "
            "'abc', 'run-1', ?, '{}', '{}', ?)",
            (legacy, TS, TS),
        )
        for version, name in ((1, "001_initial.sql"), (2, "002_research_document_fields.sql")):
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at_utc, checksum) "
                "VALUES (?, ?, ?)",
                (version, TS, _checksum_of(name)),
            )
    finally:
        conn.close()

    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION

    conn = connect(db)
    try:
        assert (
            conn.execute(
                "SELECT forecast_sha256 FROM forecast_records WHERE record_id = ?", (legacy,)
            ).fetchone()[0]
            is None
        )
        record_validation(conn, record_id=legacy, occurred_at=WHEN)
        with pytest.raises(LifecycleError):
            record_approval(
                conn,
                record_id=legacy,
                decision="approved",
                actor="chris",
                forecast_sha256=SHA,
                occurred_at=WHEN,
            )
        assert current_status(conn, legacy) == "validated"
        assert _counts(conn)["approval_events"] == 0
    finally:
        conn.close()


def test_an_unverified_post_is_a_failure_not_a_submission(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # M2-704's "success requires refetch confirmation". A post that went through but was
    # not confirmed is the uncertain case, and it must still produce an event -- which is
    # why `submitted` and `submission_failed` are exact complements rather than
    # success=1/success=0.
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    record_approval(
        conn,
        record_id=record_id,
        decision="approved",
        actor="chris",
        forecast_sha256=SHA,
        occurred_at=WHEN,
    )
    event = record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_attempt(success=True, verified=False),
        occurred_at=WHEN,
        detail_code="refetch_missing",
    )
    assert (event.event_type, event.detail_code) == ("submission_failed", "refetch_missing")
    assert current_status(conn, record_id) == "failed"


def test_an_unverified_attempt_requires_a_detail_code(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    record_approval(
        conn,
        record_id=record_id,
        decision="approved",
        actor="chris",
        forecast_sha256=SHA,
        occurred_at=WHEN,
    )
    with pytest.raises(LifecycleError):
        record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=_attempt(success=False, verified=False),
            occurred_at=WHEN,
        )
    assert current_status(conn, record_id) == "approved"
    assert _counts(conn)["submission_attempts"] == 0


def test_a_verified_submission_rejects_a_detail_code(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    record_approval(
        conn,
        record_id=record_id,
        decision="approved",
        actor="chris",
        forecast_sha256=SHA,
        occurred_at=WHEN,
    )
    with pytest.raises(LifecycleError):
        record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=_attempt(),
            occurred_at=WHEN,
            detail_code="timeout",
        )


def test_a_submitted_event_cannot_cite_an_unverified_attempt(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # The database half of the same rule: even a writer that bypasses this module cannot
    # record a submitted state against an attempt no refetch confirmed.
    conn, record_id = draft
    detail = _walk_to(conn, record_id, "approved")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_event(
            conn,
            record_id,
            "submitted",
            "approved",
            "submitted",
            {**detail, "att_ok": detail["att_bad"]},
        )


# --------------------------------------------------------------------------------------
# Atomicity: the detail row and its lifecycle event are never observable apart.
# --------------------------------------------------------------------------------------


def test_a_second_approval_rolls_back_its_own_approval_row(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """A real, unmocked partial write: the detail row lands, then the event is refused.

    Approving an already-approved record inserts a valid ``approval_events`` row and only
    then discovers there is no legal transition out of ``approved``. Without one
    transaction around both, the ledger would keep an approval decision that never became
    an approval -- exactly the orphaned half M1-603 exists to prevent.
    """
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    record_approval(
        conn,
        record_id=record_id,
        decision="approved",
        actor="chris",
        forecast_sha256=SHA,
        occurred_at=WHEN,
    )
    before = _counts(conn)
    with pytest.raises(LifecycleError):
        record_approval(
            conn,
            record_id=record_id,
            decision="approved",
            actor="chris",
            forecast_sha256=SHA,
            occurred_at=WHEN,
        )
    assert _counts(conn) == before
    assert current_status(conn, record_id) == "approved"


def test_a_second_submission_rolls_back_its_own_attempt_row(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    record_approval(
        conn,
        record_id=record_id,
        decision="approved",
        actor="chris",
        forecast_sha256=SHA,
        occurred_at=WHEN,
    )
    record_submission_attempt(conn, record_id=record_id, attempt=_attempt(), occurred_at=WHEN)
    before = _counts(conn)
    with pytest.raises(LifecycleError):
        record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=_attempt(attempt_id="att-2", key="idem-2"),
            occurred_at=WHEN,
        )
    assert _counts(conn) == before
    assert current_status(conn, record_id) == "submitted"


@pytest.mark.parametrize("target", ["approval", "submission"])
def test_an_injected_failure_leaves_no_half_written_state(
    draft: tuple[sqlite3.Connection, str], monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """The acceptance criterion, with the failure injected rather than provoked.

    The detail row is written, then the lifecycle insert raises something this module
    does not model at all. Nothing may survive: no orphan approval or attempt, and no
    change of state.
    """
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    if target == "submission":
        record_approval(
            conn,
            record_id=record_id,
            decision="approved",
            actor="chris",
            forecast_sha256=SHA,
            occurred_at=WHEN,
        )
    before = _counts(conn)
    expected_status = current_status(conn, record_id)

    real_insert = lifecycle._insert

    def failing_insert(conn_: sqlite3.Connection, sql: str, parameters: tuple[object, ...]) -> int:
        if "INTO lifecycle_events" in sql:
            raise RuntimeError("injected failure between the detail row and its event")
        return real_insert(conn_, sql, parameters)

    monkeypatch.setattr(lifecycle, "_insert", failing_insert)

    with pytest.raises(RuntimeError):
        if target == "approval":
            record_approval(
                conn,
                record_id=record_id,
                decision="approved",
                actor="chris",
                forecast_sha256=SHA,
                occurred_at=WHEN,
            )
        else:
            record_submission_attempt(
                conn, record_id=record_id, attempt=_attempt(), occurred_at=WHEN
            )

    monkeypatch.undo()
    assert _counts(conn) == before
    assert current_status(conn, record_id) == expected_status
    assert not conn.in_transaction


def test_a_writer_composes_inside_a_callers_transaction(
    ledger: sqlite3.Connection,
) -> None:
    # M1-602's shape: create the record and its first event as one unit. The writer must
    # neither commit early nor roll back work it does not own.
    conn = ledger
    with transaction(conn):
        record_id = _seed_draft(conn, record_id="rec-composed")
        record_validation(conn, record_id=record_id, occurred_at=WHEN)
    assert current_status(conn, "rec-composed") == "validated"

    with pytest.raises(RuntimeError):
        with transaction(conn):
            _seed_draft(conn, record_id="rec-abandoned", question_id=102)
            record_validation(conn, record_id="rec-abandoned", occurred_at=WHEN)
            raise RuntimeError("the caller changed its mind")
    assert (
        conn.execute(
            "SELECT count(*) FROM forecast_records WHERE record_id = 'rec-abandoned'"
        ).fetchone()[0]
        == 0
    )


def test_an_inner_failure_does_not_discard_the_outer_transaction(
    ledger: sqlite3.Connection,
) -> None:
    # The savepoint half of the nesting contract: a rejected inner write unwinds itself
    # and leaves the caller's earlier work in place to commit.
    conn = ledger
    with transaction(conn):
        record_id = _seed_draft(conn, record_id="rec-nested")
        with pytest.raises(LifecycleError):
            record_approval(
                conn,
                record_id=record_id,
                decision="approved",
                actor="chris",
                forecast_sha256=SHA,
                occurred_at=WHEN,
            )
    assert current_status(conn, "rec-nested") == "draft"
    assert _counts(conn)["approval_events"] == 0


class _FailingCommit(sqlite3.Connection):
    """A real connection whose COMMIT fails, so no production code is mocked.

    The failure a `COMMIT` actually has -- a busy timeout, a full disk -- cannot be
    provoked deterministically offline, and monkeypatching `transaction()` would test the
    patch. Overriding one statement on a genuine connection leaves every other path,
    including the rollback that has to follow, exactly as it ships.
    """

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        if sql == "COMMIT":
            raise sqlite3.OperationalError("database is locked")
        return super().execute(sql, parameters)  # type: ignore[arg-type]


def test_a_failing_commit_raises_lifecycle_error_and_closes_the_transaction(
    tmp_path: Path,
) -> None:
    # A raw sqlite3.Error escaping is a hygiene failure on its own -- callers handle only
    # this module's type -- but the transaction is the worse half: a COMMIT that failed
    # without a rollback leaves the connection inside a transaction the caller believes
    # is finished, stranding every later write on it.
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = sqlite3.connect(db, factory=_FailingCommit)
    try:
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        _seed_run(conn)
        with pytest.raises(LifecycleError):
            with transaction(conn):
                _seed_draft(conn, record_id="rec-uncommitted")
        assert not conn.in_transaction
        assert (
            conn.execute(
                "SELECT count(*) FROM forecast_records WHERE record_id = 'rec-uncommitted'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_transaction_refuses_an_implicit_transaction_connection(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = sqlite3.connect(db)  # default isolation_level: sqlite3 manages transactions
    try:
        with pytest.raises(LifecycleError):
            with transaction(conn):
                pass
    finally:
        conn.close()


# --------------------------------------------------------------------------------------
# Value objects and error hygiene.
# --------------------------------------------------------------------------------------


def test_events_round_trip_through_the_persisted_json_form(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # The M1-305 rule: replay comparison keys on the persisted form, so the value object
    # has to survive json.dumps(..., ensure_ascii=True, sort_keys=True) unchanged.
    conn, record_id = draft
    event = record_validation(conn, record_id=record_id, occurred_at=WHEN)
    encoded = json.dumps(asdict(event), ensure_ascii=True, sort_keys=True)
    assert json.loads(encoded) == asdict(event)


def test_read_history_is_empty_for_a_record_with_no_events(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = draft
    assert read_history(conn, record_id) == ()


def test_read_history_rejects_an_unknown_record(ledger: sqlite3.Connection) -> None:
    # Not (): a caller that cannot tell "no events yet" from "no such record" would report
    # the first while looking at the second. Both readers answer an unknown record the
    # same way, which is what makes them a usable read seam for M1-604 and `show`.
    with pytest.raises(LifecycleError):
        read_history(ledger, "no-such-record")


def test_unknown_record_is_a_lifecycle_error(ledger: sqlite3.Connection) -> None:
    with pytest.raises(LifecycleError):
        current_status(ledger, "no-such-record")


@pytest.mark.parametrize("occurred_at", [WHEN.replace(tzinfo=None), "2026-07-27", None, 0])
def test_occurred_at_must_be_an_aware_datetime(
    draft: tuple[sqlite3.Connection, str], occurred_at: object
) -> None:
    conn, record_id = draft
    with pytest.raises(LifecycleError):
        record_validation(conn, record_id=record_id, occurred_at=occurred_at)  # type: ignore[arg-type]


def test_a_lone_surrogate_raises_lifecycle_error_without_echoing_it(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # Lone surrogates reach this layer from provider JSON. sqlite3 encodes parameters as
    # UTF-8, so without the boundary check a writer raises a raw UnicodeEncodeError that
    # quotes the offending character -- both a leak and an error type callers do not
    # handle. (The same defect is still open against research/hashing.py.)
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    with pytest.raises(LifecycleError) as excinfo:
        record_approval(
            conn,
            record_id=record_id,
            decision="approved",
            actor="\ud800",
            forecast_sha256=SHA,
            occurred_at=WHEN,
        )
    assert "\ud800" not in str(excinfo.value)
    assert not isinstance(excinfo.value.__cause__, UnicodeEncodeError)


def _leak_cases(
    conn: sqlite3.Connection, record_id: str
) -> dict[str, Callable[[], LifecycleEvent]]:
    """Every caller-supplied text field, planted with a secret, on a path that must fail.

    Each case fails for a different reason -- unknown record, over the length cap,
    illegal transition -- because a no-leak guarantee that only holds on one code path is
    not a guarantee. The failures are what matters; the field values must never surface.
    """

    def approve(**overrides: object) -> LifecycleEvent:
        kwargs: dict[str, object] = {
            "record_id": record_id,
            "decision": "approved",
            "actor": "chris",
            "forecast_sha256": SHA,
            "occurred_at": WHEN,
        }
        kwargs.update(overrides)
        return record_approval(conn, **kwargs)  # type: ignore[arg-type]

    def submit(**overrides: object) -> LifecycleEvent:
        # The record is only `validated`, so the attempt row goes in and the event is
        # then refused -- which also exercises the rollback path with a secret in flight.
        return record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=_attempt(**overrides),  # type: ignore[arg-type]
            occurred_at=WHEN,
        )

    return {
        "record_id": lambda: approve(record_id=PLANTED_SECRET),
        "actor_over_cap": lambda: approve(actor=PLANTED_SECRET * 40),
        "note_over_cap": lambda: approve(note=PLANTED_SECRET * 400),
        "forecast_sha256": lambda: approve(forecast_sha256=PLANTED_SECRET),
        "attempt_id": lambda: submit(attempt_id=PLANTED_SECRET),
        "idempotency_key": lambda: submit(key=PLANTED_SECRET),
        "response_body_over_cap": lambda: submit(response_body=PLANTED_SECRET * 6000),
        "error_message": lambda: submit(error_message=PLANTED_SECRET * 6000),
    }


@pytest.mark.parametrize(
    "case",
    [
        "record_id",
        "actor_over_cap",
        "note_over_cap",
        "forecast_sha256",
        "attempt_id",
        "idempotency_key",
        "response_body_over_cap",
        "error_message",
    ],
)
def test_no_planted_secret_reaches_an_error_message_or_traceback(
    draft: tuple[sqlite3.Connection, str], case: str
) -> None:
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    with pytest.raises(LifecycleError) as excinfo:
        _leak_cases(conn, record_id)[case]()
    assert PLANTED_SECRET not in str(excinfo.value)
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert PLANTED_SECRET not in rendered
