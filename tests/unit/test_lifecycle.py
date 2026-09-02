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
import itertools
import json
import sqlite3
import sys
import traceback
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import datetime, timedelta, timezone, tzinfo
from importlib.resources import files
from pathlib import Path
from typing import get_args

import pytest

from whiskeyjack_bot import lifecycle
from whiskeyjack_bot.ledger import (
    LEDGER_SCHEMA_VERSION,
    LedgerError,
    connect,
    initialize_ledger,
)
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    LifecycleEvent,
    LifecycleEventType,
    LifecycleStatus,
    PreForecastEventType,
    PreForecastFailure,
    PreForecastFailureCode,
    RefetchOutcome,
    SubmissionAttempt,
    SubmissionVerification,
    current_status,
    read_history,
    read_pipeline_failure_events,
    record_approval,
    record_failure,
    record_pre_forecast_failure,
    record_submission_attempt,
    record_submission_verification,
    record_validation,
    transaction,
    unresolved_uncertainties,
)


def _checksum_of(name: str) -> str:
    """The checksum ledger.py records, computed the same way it computes it."""
    return hashlib.sha256(
        files("whiskeyjack_bot.migrations").joinpath(name).read_bytes()
    ).hexdigest()


# The canonical form `lifecycle._require_utc` renders and 003 pins on the three columns it
# orders: fixed width 32, always UTC, microseconds always present. Raw-SQL fixtures write
# it too, so they exercise the same shape the writers store rather than a second one the
# schema would have to tolerate.
TS = "2026-07-27T00:00:00.000000+00:00"
WHEN = datetime(2026, 7, 27, tzinfo=timezone.utc)
SHA = "b" * 64
OTHER_SHA = "c" * 64
PAYLOAD_SHA = "d" * 64
# What a confirming refetch saw. Required for a `confirmed` outcome: a confirmation
# with nothing stored is what carries a record to `submitted` on no evidence.
SNAPSHOT = '{"probability": 0.6}'

# Low-entropy by convention: a realistic-looking secret in a tracked file fails the
# gitleaks full-history scan on every branch, not just this one.
PLANTED_SECRET = "privateFAKE123456"

STATUSES: tuple[str, ...] = get_args(LifecycleStatus)
EVENT_TYPES: tuple[str, ...] = get_args(LifecycleEventType)

# Tables migrations 003 and 004 close to UPDATE and DELETE alike, with a row of each
# already present so a FOR EACH ROW trigger has something to fire on -- `DELETE FROM t`
# against an empty table succeeds, and would read as a passing test of a trigger that
# never ran. `pipeline_failure_events` (004) is in the same list rather than in a probe
# of its own: its block pair is the same shape as 003's, so what it needs is the same
# coverage, not a second one written from scratch.
APPEND_ONLY_TABLES = (
    "forecast_records",
    "lifecycle_events",
    "approval_events",
    "submission_attempts",
    "submission_verifications",
    "resolution_events",
    "score_events",
    "pipeline_failure_events",
    # M2-708's 010. Both are append-only for D25's reason and by the same pair of block
    # triggers, so what they need is this coverage rather than a second probe written from
    # scratch. They are also the pair that makes the release table a *record* instead of a
    # way to rewind a claim: a reservation cannot be edited into a different key, and a
    # release cannot be deleted to make an abandoned key look spent.
    "submission_key_reservations",
    "submission_key_releases",
)

# One existing, nullable column per table, so the UPDATE probe below is a well-formed
# statement that only the append-only trigger can be refusing.
UPDATABLE_COLUMN = {
    "forecast_records": "record_json",
    "lifecycle_events": "occurred_at_utc",
    "approval_events": "note",
    "submission_attempts": "response_body",
    "submission_verifications": "refetched_forecast_snapshot",
    "resolution_events": "outcome",
    "score_events": "comparison_baseline",
    "pipeline_failure_events": "retrieval_run_id",
    # No nullable column on the reservation table -- every one of its columns is NOT NULL
    # -- so this names a NOT NULL one instead. What the probe needs is a *well-formed*
    # UPDATE that only the block trigger can be refusing, and `created_at_utc` accepts the
    # text the probe writes; nullability was never the requirement, only well-formedness.
    "submission_key_reservations": "created_at_utc",
    "submission_key_releases": "note",
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
    attempt_id: str | None = None,
) -> str:
    # attempt_id defaults to one derived from record_id rather than a constant: migration
    # 004 requires it of every new row and indexes it UNIQUE (where not null), so a shared
    # default would make a second seeded record fail on the attempt_id index instead of
    # whatever the test meant to exercise. Callers that are *about* the attempt_id -- two
    # records claiming one attempt, a failure and a success sharing one -- pass it
    # explicitly.
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
        "forecast_sha256, attempt_id) "
        "VALUES (?, ?, 'minibench', 1, 'binary', 'draft', 'anthropic', 'claude', 'v1', 'abc', "
        "'run-1', ?, '{}', '{}', ?, ?, ?)",
        (record_id, question_id, TS, TS, forecast_sha256, attempt_id or f"att-{record_id}"),
    )
    return record_id


# The attempt a failure is recorded under by default. Deliberately not `_seed_draft`'s
# `att-rec-1`: 004 refuses a failure whose attempt_id already produced a forecast record,
# so a fixture that shared one would fail on that probe in every test that seeds both.
FAILED_ATTEMPT = "att-failed"


def _seed_failure(
    conn: sqlite3.Connection,
    *,
    attempt_id: str = FAILED_ATTEMPT,
    event_seq: object = 1,
    question_id: object = 100,
    tournament_id: object = "minibench",
    event_type: str = "research_failed",
    detail_code: str = "provider_unavailable",
    retrieval_run_id: str | None = None,
    occurred_at: object = TS,
) -> None:
    """Write a `pipeline_failure_events` row directly, bypassing the writer's validation.

    Every field is a parameter and several are typed `object`: what these tests are for
    is the schema's own guards, and a helper that could only produce well-formed values
    could not reach them. `_seed_run`/`_insert_attempt` take the same shape for the same
    reason.
    """
    conn.execute(
        "INSERT INTO pipeline_failure_events (attempt_id, event_seq, question_id, "
        "tournament_id, event_type, detail_code, retrieval_run_id, occurred_at_utc, "
        "created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            event_seq,
            question_id,
            tournament_id,
            event_type,
            detail_code,
            retrieval_run_id,
            occurred_at,
            TS,
        ),
    )


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
    refetch: RefetchOutcome = "confirmed",
    **extra: object,
) -> SubmissionAttempt:
    return SubmissionAttempt(
        attempt_id=attempt_id,
        idempotency_key=key,
        requested_at_utc=extra.pop("requested_at_utc", WHEN),  # type: ignore[arg-type]
        completed_at_utc=extra.pop("completed_at_utc", WHEN),  # type: ignore[arg-type]
        request_payload_sha256=PAYLOAD_SHA,
        success=success,
        refetch_outcome=refetch,
        **extra,  # type: ignore[arg-type]
    )


# Distinguishes "the caller did not supply one" from an explicit `None`, which is itself a
# value these tests need to push at the schema (M1-607).
_UNSET = object()


def _insert_attempt(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    attempt_id: object = "att-raw",
    key: object = _UNSET,
    requested: object = TS,
    completed: object = TS,
) -> None:
    """Write a `submission_attempts` row directly, bypassing the writer's validation.

    `attempt_id` and `key` are typed `object` and passed through untouched, for M1-607's
    guards on this table: `f"idem-{attempt_id}"` would substitute a well-formed key for
    every malformed one, which is the same trap `_insert_record_raw` documents.
    """
    conn.execute(
        "INSERT INTO submission_attempts (attempt_id, forecast_record_id, idempotency_key, "
        "requested_at_utc, completed_at_utc, request_payload_sha256, success, "
        "verified_by_refetch, refetch_outcome, created_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 1, 'confirmed', ?)",
        (
            attempt_id,
            record_id,
            f"idem-{attempt_id}" if key is _UNSET else key,
            requested,
            completed,
            PAYLOAD_SHA,
            TS,
        ),
    )


class HostileTimezone(tzinfo):
    """A `tzinfo` whose `utcoffset` raises, planting a secret in the message.

    `tzinfo` is an abstract base class, so a `datetime` that passes every type gate can
    still run caller-supplied code during the UTC conversion. Without a guard that code's
    exception -- and its text -- propagates straight out of the writer.
    """

    def utcoffset(self, dt: datetime | None) -> timedelta:
        raise ValueError(PLANTED_SECRET)

    def tzname(self, dt: datetime | None) -> str:
        return "hostile"

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


class HostileAttempt(SubmissionAttempt):
    """A `SubmissionAttempt` subclass that makes every attribute read hostile.

    Shadowing a field with a property fails at construction on a frozen dataclass, so the
    realistic shape is `__getattribute__`: it survives `__init__` and turns each
    `attempt.<field>` read in the writer into a call the writer did not know it made.
    """

    def __getattribute__(self, name: str) -> object:
        if name == "idempotency_key":
            raise RuntimeError(PLANTED_SECRET)
        return object.__getattribute__(self, name)


class HostileVerification(SubmissionVerification):
    """The same trick against the verification writer, which has the same two writes."""

    def __getattribute__(self, name: str) -> object:
        if name == "outcome":
            raise RuntimeError(PLANTED_SECRET)
        return object.__getattribute__(self, name)


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
    #
    # attempt_id is supplied even though it is irrelevant to the state under test:
    # migration 004's extended trigger requires it, so omitting it would raise
    # IntegrityError for *that* reason and leave this test green while proving nothing
    # about the draft-only rule it is named for.
    with pytest.raises(sqlite3.IntegrityError):
        ledger.execute(
            "INSERT INTO forecast_records ("
            "record_id, question_id, tournament_id, forecast_version, question_type, status, "
            "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
            "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
            "forecast_sha256, attempt_id) "
            "VALUES ('rec-x', 100, 'minibench', 1, 'binary', ?, 'anthropic', 'claude', 'v1', "
            "'abc', 'run-1', ?, '{}', '{}', ?, ?, 'att-rec-x')",
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
    # A walk populates the seven 003 tables but never pipeline_failure_events -- nothing
    # in a successful lifecycle writes one. Without this the count assertion below would
    # be the only thing standing between an empty table and a DELETE that "passes".
    _seed_failure(conn)
    # And nothing in a successful lifecycle writes 010's pair either: a reservation is
    # spent by the attempt row, never released, so the release table stays empty on the
    # happy path.
    _seed_reservations(conn, record_id)
    assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE {table} SET {UPDATABLE_COLUMN[table]} = ?", ("changed",))


def test_one_approval_decision_cannot_back_two_lifecycle_events(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """Round 3, finding 2, at its most reachable point.

    The link probes checked that a detail row belonged to this record and recorded this
    decision, and never that it had not already been cited. `rejected` is
    validated -> validated, so the second event satisfies every other constraint: the
    history then shows two rejections on the evidence of one decision, immutably.
    """
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    approval_id = conn.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "created_at_utc) VALUES (?, 'rejected', 'chris', ?, ?)",
        (record_id, SHA, TS),
    ).lastrowid

    def cite(seq: int) -> None:
        conn.execute(
            "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
            "from_status, to_status, approval_event_id, occurred_at_utc, created_at_utc) "
            "VALUES (?, ?, 'rejected', 'validated', 'validated', ?, ?, ?)",
            (record_id, seq, approval_id, TS, TS),
        )

    cite(2)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        cite(3)
    assert len(read_history(conn, record_id)) == 2


def test_one_attempt_cannot_back_two_lifecycle_events(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """The second reachable case, and it only became reachable again in round 4.

    `submission_uncertain` is a self-transition, so a record can hold two of them -- and
    with round 3's retry probe withdrawn, nothing but this index stops both citing the same
    receipt. That would be one post recorded as two, in an append-only log.
    """
    conn, record_id = draft
    _approve(conn, record_id)
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_attempt(success=True, refetch="absent"),
        occurred_at=WHEN,
        detail_code="refetch_missing",
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        conn.execute(
            "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
            "from_status, to_status, detail_code, submission_attempt_id, occurred_at_utc, "
            "created_at_utc) VALUES (?, 4, 'submission_uncertain', 'approved', 'approved', "
            "'refetch_missing', 'att-1', ?, ?)",
            (record_id, TS, TS),
        )
    assert len(read_history(conn, record_id)) == 3


@pytest.mark.parametrize(
    "link",
    [
        "approval_event_id",
        "submission_attempt_id",
        "submission_verification_id",
        "resolution_event_id",
        "score_event_id",
    ],
)
def test_every_link_column_is_unique(ledger: sqlite3.Connection, link: str) -> None:
    """The same rule for all five, two of which can be reproduced through it.

    `rejected` and `submission_uncertain` are self-transitions, so both of those are
    reachable and have their own reproductions above and below. The other three move the
    record somewhere no second event of that type is legal from, which makes them defence
    in depth -- and defence in depth is exactly what "not reachable today" warrants: that
    is a fact about the current transition table, which M4-802 and M5-803 have yet to add
    to, and not a property of this table.

    So the assertion here is on the constraint rather than on a contrived path to it:
    asserting a refusal that some *other* guard produces would be a test that passes
    whether or not the index exists.
    """
    indexes = {
        row["name"]: row
        for row in ledger.execute("PRAGMA index_list('lifecycle_events')")
        if row["unique"]
    }
    covering = {
        name: [
            column["name"]
            for column in ledger.execute(f"PRAGMA index_info('{name}')")
            if column["name"] is not None
        ]
        for name in indexes
    }
    assert [link] in covering.values(), f"no unique index on {link} alone"
    name = next(key for key, columns in covering.items() if columns == [link])
    # Partial: an unlinked event is the normal case, and there are far more of those than
    # linked ones.
    assert indexes[name]["partial"] == 1


def test_an_event_must_name_a_forecast_record(draft: tuple[sqlite3.Connection, str]) -> None:
    """``forecast_record_id`` is nullable in the DDL and mandatory in the trigger.

    The column is declared nullable only so M1-606 can add attempt-scoped events without
    rebuilding an append-only table (SQLite cannot relax NOT NULL in place). The
    constraint actually in force must still be NOT NULL, or the forward-compatibility
    gesture has quietly weakened the schema it was meant to leave alone.
    """
    conn, _ = draft
    with pytest.raises(sqlite3.IntegrityError, match="forecast_record_id is required"):
        conn.execute(
            "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
            "from_status, to_status, occurred_at_utc, created_at_utc) "
            "VALUES (NULL, 1, 'validated', 'draft', 'validated', ?, ?)",
            (TS, TS),
        )


RESERVATION_KEY = "idem-reservation-probe"
RELEASED_KEY = "idem-reservation-probe-released"


def _seed_reservations(conn: sqlite3.Connection, record_id: str) -> None:
    """Put a row in each of `010`'s two tables, for the append-only probes to fire on.

    Two reservations under *different* keys, one of them released. Different keys because
    `010` refuses a second live reservation for one key, and because a walk to `scored`
    has already written a `submission_attempts` row -- so reusing that attempt's key
    would be refused as spent, and the probe would be seeding nothing while reading as a
    passing test of a trigger that never ran.

    The released one is first by rowid, which is what `_replace_row`'s `LIMIT 1` picks up:
    a release row is exactly the thing that must not be deletable, since deleting it would
    make an abandoned key look spent.
    """
    conn.execute(
        "INSERT INTO submission_key_reservations (reservation_id, idempotency_key, "
        "forecast_record_id, reservation_seq, reserved_at_utc, created_at_utc) "
        "VALUES ('wjres-released', ?, ?, 1, ?, ?)",
        (RELEASED_KEY, record_id, TS, TS),
    )
    conn.execute(
        "INSERT INTO submission_key_releases (release_id, reservation_id, reason, "
        "released_by, note, released_at_utc, created_at_utc) "
        "VALUES ('wjrel-1', 'wjres-released', 'operator_abandoned', 'chris', NULL, ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO submission_key_reservations (reservation_id, idempotency_key, "
        "forecast_record_id, reservation_seq, reserved_at_utc, created_at_utc) "
        "VALUES ('wjres-live', ?, ?, 1, ?, ?)",
        (RESERVATION_KEY, record_id, TS, TS),
    )


def _replace_row(conn: sqlite3.Connection, table: str, **changes: object) -> None:
    """Re-insert an existing row through ``INSERT OR REPLACE``, optionally altered.

    Rebuilt from ``SELECT *`` so the statement is well-formed for any of the six tables
    and the only thing that can refuse it is the append-only trigger.
    """
    stored = dict(conn.execute(f"SELECT * FROM {table} LIMIT 1").fetchone())
    stored.update(changes)
    columns = ", ".join(stored)
    placeholders = ", ".join("?" for _ in stored)
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(stored.values()),
    )


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_replace_cannot_overwrite_a_row_through_its_primary_key(
    ledger: sqlite3.Connection, table: str
) -> None:
    """``INSERT OR REPLACE`` is a DELETE the block triggers must still see.

    SQLite resolves a REPLACE conflict by deleting the row in the way, and with
    ``PRAGMA recursive_triggers`` off -- its default -- those deletes fire no BEFORE
    DELETE trigger at all, which made every append-only trigger in migration 003
    bypassable by one statement. ``ledger.connect()`` turns the pragma on and verifies
    the readback; this is the statement-level half of that guarantee. (GPT review round
    1, finding 1, reproduced against all three tables it named.)

    Asserted as "refused, and nothing was erased" rather than on the message: for most
    of these tables the append-only delete trigger is what fires, but on
    ``lifecycle_events`` the state-machine trigger gets there first and on
    ``pipeline_failure_events`` the sequence probe does, and which guard catches it
    matters less than that the stored rows survive. The two named scenarios below pin
    the specific guards.
    """
    conn = ledger
    record_id = _seed_draft(conn)
    _walk_to(conn, record_id, "scored")
    _seed_failure(conn)  # see the sibling test: a walk writes no failure row
    _seed_reservations(conn, record_id)  # 010's pair, for the same reason
    before = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    assert before
    with pytest.raises(sqlite3.IntegrityError):
        _replace_row(conn, table)
    after = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_replace_cannot_overwrite_a_row_through_a_secondary_unique_key(
    ledger: sqlite3.Connection,
) -> None:
    """The primary key is not the only way into REPLACE's delete path.

    A conflict on *any* uniqueness constraint triggers the same replacement delete, so
    the two secondary UNIQUE keys need their own probe: a new attempt_id reusing a
    stored idempotency_key, and a new record_id reusing a stored
    (question_id, tournament_id, forecast_version).
    """
    conn = ledger
    record_id = _seed_draft(conn)
    _walk_to(conn, record_id, "submitted")

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _replace_row(conn, "submission_attempts", attempt_id="att-usurper")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _replace_row(conn, "forecast_records", record_id="rec-usurper")


def test_replace_cannot_erase_an_event_through_its_detail_link(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """Each link index is a new conflict target, and so a new route into REPLACE's delete.

    Unlike the sequence key, this one is reachable: a second `rejected` event citing the
    same approval row passes every trigger probe -- next sequence number, matching
    from_status, legal transition, right detail row -- and only then collides, on the index
    added in round 3. The append-only delete trigger is what has to catch it, which it can
    only do with `PRAGMA recursive_triggers` on.
    """
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    approval_id = conn.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "created_at_utc) VALUES (?, 'rejected', 'chris', ?, ?)",
        (record_id, SHA, TS),
    ).lastrowid
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, from_status, "
        "to_status, approval_event_id, occurred_at_utc, created_at_utc) "
        "VALUES (?, 2, 'rejected', 'validated', 'validated', ?, ?, ?)",
        (record_id, approval_id, TS, TS),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "INSERT OR REPLACE INTO lifecycle_events (forecast_record_id, event_seq, "
            "event_type, from_status, to_status, approval_event_id, occurred_at_utc, "
            "created_at_utc) VALUES (?, 3, 'rejected', 'validated', 'validated', ?, ?, ?)",
            (record_id, approval_id, TS, TS),
        )
    assert [event.event_seq for event in read_history(conn, record_id)] == [1, 2]


def test_replace_cannot_renumber_an_event_through_its_sequence_key(
    ledger: sqlite3.Connection,
) -> None:
    """``lifecycle_events``' UNIQUE key is guarded twice, and the first guard wins.

    A REPLACE aimed at (forecast_record_id, event_seq) never reaches conflict resolution:
    the BEFORE INSERT state-machine trigger runs first and requires the record's *next*
    sequence number, which by definition does not collide with a stored one. The
    append-only delete trigger is the second line, exercised by the primary-key test
    above -- so both routes to erasing an event are closed, by different guards.
    """
    conn = ledger
    record_id = _seed_draft(conn)
    _walk_to(conn, record_id, "validated")
    with pytest.raises(sqlite3.IntegrityError, match="next sequence number"):
        _replace_row(conn, "lifecycle_events", event_id=None)


def test_replace_cannot_flip_a_stored_approval_decision(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """GPT round 1, scenario 1: an approved event left pointing at a rejected row.

    The exact-hash approval binding is only as good as the immutability of the row it
    binds to.
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
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _replace_row(conn, "approval_events", decision="rejected", actor="usurper")
    assert conn.execute("SELECT decision FROM approval_events").fetchone()[0] == "approved"


def test_replace_cannot_downgrade_a_verified_submission(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """GPT round 1, scenario 2: a submitted event left pointing at success=0.

    ``submitted`` means "posted and confirmed by refetch" only because the attempt row
    it points at says so; rewriting that row underneath it is an unearned claim.
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
    record_submission_attempt(conn, record_id=record_id, attempt=_attempt(), occurred_at=WHEN)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _replace_row(
            conn,
            "submission_attempts",
            success=0,
            verified_by_refetch=0,
            refetch_outcome="absent",
        )
    stored = conn.execute(
        "SELECT success, verified_by_refetch, refetch_outcome FROM submission_attempts"
    ).fetchone()
    assert tuple(stored) == (1, 1, "confirmed")
    assert current_status(conn, record_id) == "submitted"


def test_replace_cannot_erase_the_first_event_of_a_history(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """GPT round 1, scenario 3: a history left as ``[(1, 2, 'approved')]``.

    Event contiguity from 1 is what makes a *missing* event detectable, and REPLACE
    could delete seq 1 while inserting seq 2 under the same event_id -- passing the
    state-machine trigger, which sees the old row still in place when it runs.
    """
    conn, record_id = draft
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    first = conn.execute("SELECT event_id FROM lifecycle_events WHERE event_seq = 1").fetchone()[0]
    approval_id = conn.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "created_at_utc) VALUES (?, 'approved', 'usurper', ?, ?)",
        (record_id, SHA, TS),
    ).lastrowid
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "INSERT OR REPLACE INTO lifecycle_events (event_id, forecast_record_id, event_seq, "
            "event_type, from_status, to_status, approval_event_id, occurred_at_utc, "
            "created_at_utc) VALUES (?, ?, 2, 'approved', 'validated', 'approved', ?, ?, ?)",
            (first, record_id, approval_id, TS, TS),
        )
    history = [(event.event_seq, event.event_type) for event in read_history(conn, record_id)]
    assert history == [(1, "validated")]


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_update_or_replace_is_refused_before_it_can_delete(
    ledger: sqlite3.Connection, table: str
) -> None:
    # UPDATE OR REPLACE also deletes conflicting rows, but the BEFORE UPDATE block fires
    # first, so this one never reaches conflict resolution. Pinned so the ordering is not
    # something a later migration can quietly change.
    conn = ledger
    record_id = _seed_draft(conn)
    _walk_to(conn, record_id, "scored")
    _seed_failure(conn)  # a walk writes no failure row; see the DELETE probe above
    _seed_reservations(conn, record_id)
    # An UPDATE against an empty table changes nothing and raises nothing, so an unseeded
    # table would fail here rather than pass -- which is the right way round, but only
    # because the seed above exists.
    assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"UPDATE OR REPLACE {table} SET {UPDATABLE_COLUMN[table]} = ?", ("changed",))


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
    ledger.execute("UPDATE research_runs SET error_summary = 'provider timed out'")
    ledger.execute("UPDATE research_documents SET title = 'a better title'")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.execute("DELETE FROM research_runs")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.execute("DELETE FROM research_documents")


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("research_runs", "retrieval_run_id", "run-usurper"),
        ("research_runs", "provider", "somewhere-else"),
        ("research_runs", "started_at_utc", "2020-01-01T00:00:00+00:00"),
        ("research_runs", "created_at_utc", "2020-01-01T00:00:00+00:00"),
        # Required at insert by 002, so established at creation like the rest -- nullable
        # only because ADD COLUMN cannot retrofit NOT NULL. Leaving them open let a run be
        # reassigned to another question and a document retrieved from a provider API be
        # rewritten as an agent's claim (round 2, finding 4).
        ("research_runs", "question_id", 999),
        ("research_documents", "document_id", "doc-usurper"),
        ("research_documents", "retrieval_run_id", "run-2"),
        ("research_documents", "canonical_url", "https://example.test/elsewhere"),
        ("research_documents", "original_url", "https://example.test/elsewhere"),
        ("research_documents", "content_sha256", "hash-2"),
        ("research_documents", "retrieved_at_utc", "2020-01-01T00:00:00+00:00"),
        ("research_documents", "source_type", "web"),
        ("research_documents", "provenance", "llm_reported"),
    ],
)
def test_evidence_may_be_annotated_but_never_re_identified(
    ledger: sqlite3.Connection, table: str, column: str, value: object
) -> None:
    """The other half of the carve-out: completable does not mean rewritable.

    Blocking only DELETE left a stored run's provider or a document's URL and content
    hash open to being rewritten in place, which detaches the evidence from what was
    actually retrieved as effectively as erasing it. (GPT round 1, non-blocking
    observation; the identity set was widened in round 2, finding 4.)
    """
    ledger.execute(
        "INSERT INTO research_documents (document_id, retrieval_run_id, original_url, "
        "canonical_url, retrieved_at_utc, source_type, provenance, content_sha256) "
        "VALUES ('doc-1', 'run-1', 'https://example.test/a', 'https://example.test/a', ?, "
        "'news', 'direct_api', 'hash-1')",
        (TS,),
    )
    _seed_run(ledger, run_id="run-2")
    with pytest.raises(sqlite3.IntegrityError, match="never re-identified"):
        ledger.execute(f"UPDATE {table} SET {column} = ?", (value,))


def test_evidence_written_before_002_can_still_be_backfilled_once(tmp_path: Path) -> None:
    """The pins must not freeze the rows 002 anticipated backfilling.

    question_id, original_url, provenance and source_type are pinned from round 2 on, but
    a row written under 001 holds an honest NULL in each and 002's triggers refuse *any*
    update to it until they are filled in. So the guard is one-way: NULL -> value once,
    value -> anything else never. An unconditional pin would have made those rows
    permanently unupdatable, which is a worse answer than the hole it closes.
    """
    db = tmp_path / "ledger.sqlite3"
    conn = connect(db)
    try:
        conn.executescript(
            files("whiskeyjack_bot.migrations").joinpath("001_initial.sql").read_text()
        )
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, started_at_utc, "
            "created_at_utc) VALUES ('legacy-run', 'asknews', ?, ?)",
            (TS, TS),
        )
        conn.execute(
            "INSERT INTO research_documents (document_id, retrieval_run_id, canonical_url, "
            "retrieved_at_utc, content_sha256) "
            "VALUES ('legacy-doc', 'legacy-run', 'https://example.test/old', ?, 'hash-1')",
            (TS,),
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at_utc, checksum) VALUES (1, ?, ?)",
            (TS, _checksum_of("001_initial.sql")),
        )
    finally:
        conn.close()

    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION

    conn = connect(db)
    try:
        conn.execute("UPDATE research_runs SET question_id = 100")
        conn.execute(
            "UPDATE research_documents SET original_url = 'https://example.test/old', "
            "provenance = 'direct_api', source_type = 'news'"
        )
        # Once. From here the same columns are identity like the rest.
        with pytest.raises(sqlite3.IntegrityError, match="never re-identified"):
            conn.execute("UPDATE research_runs SET question_id = 200")
        with pytest.raises(sqlite3.IntegrityError, match="never re-identified"):
            conn.execute("UPDATE research_documents SET provenance = 'llm_reported'")
    finally:
        conn.close()


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
    # One attempt of each shape the (success, refetch_outcome) partition recognizes, so an
    # event type is never refused merely for citing the wrong kind of attempt. `att-unknown`
    # is M2-711's: a post that raised whose refetch could not be read, which is uncertain
    # rather than failed and so is a *different* kind of attempt from `att-bad`.
    for attempt_id, success, verified, refetch in (
        (f"att-ok-{suffix}", 1, 1, "confirmed"),
        (f"att-unsure-{suffix}", 1, 0, "absent"),
        (f"att-bad-{suffix}", 0, 0, "absent"),
        (f"att-unknown-{suffix}", 0, 0, "unreadable"),
    ):
        conn.execute(
            "INSERT INTO submission_attempts (attempt_id, forecast_record_id, idempotency_key, "
            "requested_at_utc, completed_at_utc, request_payload_sha256, success, "
            "verified_by_refetch, refetch_outcome, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt_id,
                record_id,
                f"idem-{attempt_id}",
                TS,
                TS,
                PAYLOAD_SHA,
                success,
                verified,
                refetch,
                TS,
            ),
        )
    # The resolution has to name the record's own question: a resolution row may point at
    # the right record and still resolve a different question, which is the second thing
    # the link probes check.
    question_id = conn.execute(
        "SELECT question_id FROM forecast_records WHERE record_id = ?", (record_id,)
    ).fetchone()[0]
    resolution = conn.execute(
        "INSERT INTO resolution_events (question_id, forecast_record_id, ingested_at_utc) "
        "VALUES (?, ?, ?)",
        (question_id, record_id, TS),
    ).lastrowid
    score = conn.execute(
        "INSERT INTO score_events (forecast_record_id, metric, value, implementation_version, "
        "computed_at_utc) VALUES (?, 'brier', 0.25, 'v1', ?)",
        (record_id, TS),
    ).lastrowid
    # Both observations a refetch of the uncertain attempt could have made. They are
    # storable whether or not this record ever recorded that attempt as uncertain -- the
    # verification table only requires the attempt to exist -- which is what lets the
    # exhaustive probe below ask the *link* whether it checks for the uncertainty.
    verifications = {
        f"verified_{outcome}": conn.execute(
            "INSERT INTO submission_verifications (submission_attempt_id, outcome, "
            "observed_at_utc, refetched_forecast_snapshot, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"att-unsure-{suffix}", outcome, TS, snapshot, TS),
        ).lastrowid
        # A confirmation must carry what it saw; an `absent` one has nothing to store.
        for outcome, snapshot in (("confirmed", SNAPSHOT), ("absent", None))
    }
    return {
        "approved": approved,
        "rejected": rejected,
        "att_ok": f"att-ok-{suffix}",
        "att_unsure": f"att-unsure-{suffix}",
        "att_bad": f"att-bad-{suffix}",
        "resolved": resolution,
        "scored": score,
        **verifications,
    }


_DERIVE = object()


def _insert_event(
    conn: sqlite3.Connection,
    record_id: str,
    event_type: str,
    from_status: str,
    to_status: str,
    detail: dict[str, object],
    *,
    detail_code: object = _DERIVE,
) -> None:
    """Insert one lifecycle row with a valid detail link for its event type.

    ``detail_code`` defaults to the one the event needs; pass it explicitly to ask the
    database what it does with a code the event should not carry.
    """
    seq = conn.execute(
        "SELECT coalesce(max(event_seq), 0) + 1 FROM lifecycle_events WHERE forecast_record_id = ?",
        (record_id,),
    ).fetchone()[0]
    links: dict[str, object] = {
        "approval_event_id": None,
        "submission_attempt_id": None,
        "submission_verification_id": None,
        "resolution_event_id": None,
        "score_event_id": None,
    }
    if event_type in ("approved", "rejected"):
        links["approval_event_id"] = detail[event_type]
    elif event_type == "submitted":
        links["submission_attempt_id"] = detail["att_ok"]
    elif event_type == "submission_uncertain":
        links["submission_attempt_id"] = detail["att_unsure"]
    elif event_type == "submission_failed":
        links["submission_attempt_id"] = detail["att_bad"]
    elif event_type == "submission_confirmed":
        links["submission_verification_id"] = detail["verified_confirmed"]
    elif event_type == "submission_disconfirmed":
        links["submission_verification_id"] = detail["verified_absent"]
    elif event_type == "resolved":
        links["resolution_event_id"] = detail["resolved"]
    elif event_type == "scored":
        links["score_event_id"] = detail["scored"]
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, from_status, "
        "to_status, detail_code, approval_event_id, submission_attempt_id, "
        "submission_verification_id, resolution_event_id, score_event_id, occurred_at_utc, "
        "created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record_id,
            seq,
            event_type,
            from_status,
            to_status,
            (
                (
                    "internal_error"
                    if to_status == "failed" or event_type == "submission_uncertain"
                    else None
                )
                if detail_code is _DERIVE
                else detail_code
            ),
            links["approval_event_id"],
            links["submission_attempt_id"],
            links["submission_verification_id"],
            links["resolution_event_id"],
            links["score_event_id"],
            TS,
            TS,
        ),
    )


# Which attempt of the three _detail_rows creates each submission event is expected to
# cite. Substituting one for another is how a test asks the database whether the
# (success, verified_by_refetch) partition is really enforced.
_LINK_FOR: dict[str, str] = {
    "submitted": "att_ok",
    "submission_uncertain": "att_unsure",
    "submission_failed": "att_bad",
}


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
    # Not an eighth status -- the record is `approved` either way -- but the two are not
    # interchangeable, and that is the point. From a plain `approved` record a submission
    # may be attempted and a refetch may not resolve anything; from one holding an
    # unresolved uncertainty it is the other way round. Both halves of that are transitions
    # the exhaustive probe below has to be able to reach, so it needs both records.
    "approved_uncertain": (
        ("validated", "draft", "validated"),
        ("approved", "validated", "approved"),
        ("submission_uncertain", "approved", "approved"),
    ),
}

# The two event types that need a record with an unresolved uncertainty to be legal at
# all. Everything else is probed from the plain route to its `from_status`.
_NEEDS_UNCERTAINTY = frozenset({"submission_confirmed", "submission_disconfirmed"})


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

    Two of the triples are probed against a *different* approved record: the refetch's own
    transitions exist only for a record waiting on one, and the retry block means the two
    populations refuse opposite things. Which record a triple is probed against is the
    only place this test knows about that rule -- the assertion below is unchanged.
    """
    detail: dict[str, dict[str, object]] = {}
    for index, route in enumerate((*STATUSES, "approved_uncertain")):
        record_id = f"rec-{route}"
        _seed_draft(ledger, record_id=record_id, question_id=300 + index)
        detail[route] = _walk_to(ledger, record_id, route)

    accepted: set[tuple[str, str, str]] = set()
    for event_type in EVENT_TYPES:
        for from_status in STATUSES:
            route = (
                "approved_uncertain"
                if from_status == "approved" and event_type in _NEEDS_UNCERTAINTY
                else from_status
            )
            for to_status in STATUSES:
                ledger.execute("SAVEPOINT probe")
                try:
                    _insert_event(
                        ledger,
                        f"rec-{route}",
                        event_type,
                        from_status,
                        to_status,
                        detail[route],
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
        event_type="validation_failed",
        detail_code="schema_invalid",
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


LEGACY_RECORD = "rec-legacy"


def _seed_v2_ledger(db: Path, *, status: str = "draft", with_approval: bool = False) -> None:
    """A ledger at schema version 2: 001 and 002 applied, 003 not yet, one record written.

    The state every question about upgrading is asked from. 001 permits any of the seven
    statuses on a new record and has no hash column, so what this writes is exactly what a
    ledger from before this item could hold.
    """
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
            "VALUES (?, 101, 'minibench', 1, 'binary', ?, 'anthropic', 'claude', 'v1', "
            "'abc', 'run-1', ?, '{}', '{}', ?)",
            (LEGACY_RECORD, status, TS, TS),
        )
        if with_approval:
            conn.execute(
                "INSERT INTO approval_events (forecast_record_id, decision, actor, "
                "forecast_sha256, created_at_utc) VALUES (?, 'approved', 'someone', ?, ?)",
                (LEGACY_RECORD, OTHER_SHA, TS),
            )
        for version, name in ((1, "001_initial.sql"), (2, "002_research_document_fields.sql")):
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at_utc, checksum) "
                "VALUES (?, ?, ?)",
                (version, TS, _checksum_of(name)),
            )
    finally:
        conn.close()


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
    legacy = LEGACY_RECORD
    _seed_v2_ledger(db)

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


@pytest.mark.parametrize(
    "status", ["validated", "approved", "submitted", "failed", "resolved", "scored"]
)
def test_the_migration_refuses_a_ledger_holding_a_non_draft_record(
    tmp_path: Path, status: str
) -> None:
    """The upgrade is refused rather than reconciled, because it cannot be reconciled.

    001 permitted any of the seven statuses on a new record. After 003 the status is
    *derived* from the event log, so such a row reports a state no history supports --
    ``current_status()`` answered 'approved' for a record whose ``read_history()`` was
    empty -- and the append-only triggers leave nothing that can correct it. The other
    option was to synthesize the missing events, which would invent attribution data.
    (GPT review round 2, finding 1.)
    """
    db = tmp_path / "ledger.sqlite3"
    _seed_v2_ledger(db, status=status)

    with pytest.raises(LedgerError):
        initialize_ledger(db)

    # And the refusal is clean: the migration ran inside a transaction, so the database is
    # untouched at version 2 rather than half-upgraded.
    conn = connect(db)
    try:
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
                "AND name = 'lifecycle_events'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_the_upgrade_precondition_leaves_nothing_behind(tmp_path: Path) -> None:
    """The guard is a temp table, so it must not survive either outcome.

    On the refusal path it is rolled back with the rest of the migration (asserted above,
    where the database stays at version 2). On the success path it is dropped. A leftover
    table in either schema would be a migration writing something the schema does not
    document.
    """
    db = tmp_path / "ledger.sqlite3"
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION
    conn = connect(db)
    try:
        for table in ("sqlite_master", "sqlite_temp_master"):
            assert (
                conn.execute(
                    f"SELECT count(*) FROM {table} WHERE name LIKE 'migration_003%'"
                ).fetchone()[0]
                == 0
            )
    finally:
        conn.close()


def test_a_refused_upgrade_stays_refused(tmp_path: Path) -> None:
    # Idempotence in the direction that matters: a second run must not find the database
    # half-upgraded and carry on from there.
    db = tmp_path / "ledger.sqlite3"
    _seed_v2_ledger(db, status="approved")
    for _ in range(2):
        with pytest.raises(LedgerError):
            initialize_ledger(db)
    conn = connect(db)
    try:
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 2
        # The ALTER is rolled back with everything else, so the hash column is not there
        # either -- a half-applied 003 would be the worst of both answers.
        assert not any(
            row[1] == "forecast_sha256"
            for row in conn.execute("PRAGMA table_info(forecast_records)").fetchall()
        )
    finally:
        conn.close()


def test_an_approval_written_before_003_cannot_carry_a_record_to_approved(
    tmp_path: Path,
) -> None:
    """The hash binding is checked where the decision becomes the record's state.

    ``approval_events``' own insert trigger cannot see a row that predates it, and every
    pre-003 record's hash is NULL after the ALTER -- so an approval carrying an arbitrary
    digest was linkable and took the record to `approved` bound to no content at all
    (round 2, finding 2). Raw SQL throughout: the point is what the database refuses, not
    what the writer declines to do.
    """
    db = tmp_path / "ledger.sqlite3"
    _seed_v2_ledger(db, with_approval=True)
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION

    conn = connect(db)
    try:
        approval_id = conn.execute("SELECT event_id FROM approval_events").fetchone()[0]
        conn.execute(
            "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
            "from_status, to_status, occurred_at_utc, created_at_utc) "
            "VALUES (?, 1, 'validated', 'draft', 'validated', ?, ?)",
            (LEGACY_RECORD, TS, TS),
        )
        with pytest.raises(sqlite3.IntegrityError, match="does not bind the forecast hash"):
            conn.execute(
                "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
                "from_status, to_status, approval_event_id, occurred_at_utc, created_at_utc) "
                "VALUES (?, 2, 'approved', 'validated', 'approved', ?, ?, ?)",
                (LEGACY_RECORD, approval_id, TS, TS),
            )
        assert current_status(conn, LEGACY_RECORD) == "validated"
    finally:
        conn.close()


def test_an_event_cannot_borrow_another_records_approval(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # On a wholly post-003 ledger the hash probe has a second line in front of it: an
    # approval row that binds another record's hash is refused for being another record's,
    # before the hashes are ever compared. Both are worth having -- the legacy case above
    # is the one only the hash probe catches.
    conn, record_id = draft
    other = _seed_draft(conn, record_id="rec-other", question_id=777, forecast_sha256=OTHER_SHA)
    detail = _walk_to(conn, record_id, "validated")
    other_approval = conn.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "created_at_utc) VALUES (?, 'approved', 'chris', ?, ?)",
        (other, OTHER_SHA, TS),
    ).lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        _insert_event(
            conn,
            record_id,
            "approved",
            "validated",
            "approved",
            {**detail, "approved": other_approval},
        )


def _approve(conn: sqlite3.Connection, record_id: str) -> None:
    record_validation(conn, record_id=record_id, occurred_at=WHEN)
    record_approval(
        conn,
        record_id=record_id,
        decision="approved",
        actor="chris",
        forecast_sha256=SHA,
        occurred_at=WHEN,
    )


# Every (success, refetch_outcome) pair there is -- all eight, spelled out rather than
# generated, so the table below is readable as the rule and a wrong cell is a wrong line
# rather than a wrong expression. `_PARTITION` is reused by the totality test that follows.
_PARTITION: tuple[tuple[bool, RefetchOutcome, str, str], ...] = (
    (True, "confirmed", "submitted", "submitted"),
    (True, "absent", "submission_uncertain", "approved"),
    (True, "mismatched", "submission_uncertain", "approved"),
    (True, "unreadable", "submission_uncertain", "approved"),
    (False, "confirmed", "submission_uncertain", "approved"),
    (False, "absent", "submission_failed", "failed"),
    # M2-711. Both of these were `submission_failed` and terminal before it.
    (False, "mismatched", "submission_uncertain", "approved"),
    (False, "unreadable", "submission_uncertain", "approved"),
)


@pytest.mark.parametrize(("success", "refetch", "expected", "status"), _PARTITION)
def test_the_attempt_pair_decides_the_event(
    draft: tuple[sqlite3.Connection, str],
    success: bool,
    refetch: RefetchOutcome,
    expected: str,
    status: str,
) -> None:
    """The whole (success, refetch_outcome) partition, and where each outcome lands.

    Six of the eight are the uncertain case, and they are the reason the outcome is read
    rather than `success` alone: the post and the platform disagreeing, or the platform
    never being read, is a third outcome and not a failure. Recording either as one moved
    the record to terminal `failed` -- round 2, finding 3 for the disagreement, M2-711 for
    the unread platform.

    The two M2-711 rows are the item's acceptance criterion at this layer: *"a post whose
    outcome no refetch established is recorded as neither submitted nor failed"*. That the
    record lands `approved` rather than `failed` is what the `status` column asserts, and
    it is the half that makes the outcome still resolvable.
    """
    conn, record_id = draft
    _approve(conn, record_id)
    event = record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_attempt(success=success, refetch=refetch),
        occurred_at=WHEN,
        detail_code=None if expected == "submitted" else "refetch_missing",
    )
    assert event.event_type == expected
    assert current_status(conn, record_id) == status


def test_the_partition_covers_every_pair_exactly_once() -> None:
    """The table above is total and single-valued over the vocabulary it partitions.

    Cheap, and it is the assertion the parametrized test cannot make: that one drives every
    row it is given, and would pass just as well if a row were missing. A pair with no legal
    event is an outcome the ledger cannot record -- which is the defect M2-711 exists to
    close, so it is worth an assertion that fails when it recurs rather than a reader
    counting rows.
    """
    pairs = [(success, refetch) for success, refetch, _, _ in _PARTITION]
    assert len(pairs) == len(set(pairs))
    assert set(pairs) == {(s, r) for s in (True, False) for r in get_args(RefetchOutcome)}
    # ... and `submitted` is reachable from exactly one of them, which is M2-704's
    # "success requires refetch confirmation" stated as a property of the table.
    assert [p for p in _PARTITION if p[2] == "submitted"] == [
        (True, "confirmed", "submitted", "submitted")
    ]


# ── M2-711: the schema half of the same rule ─────────────────────────────────
#
# Every test above drives the writer. These drive raw SQL, because the writer agreeing
# with itself is not the guarantee -- `009_submission_refetch_outcome.sql` is, and a rule
# that lives only in Python is one INSERT away from not existing.


def _raw_attempt(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    attempt_id: str = "att-raw",
    success: object = 0,
    verified: object = 0,
    refetch: object = "absent",
) -> None:
    """Write a `submission_attempts` row directly, naming `refetch_outcome` explicitly.

    Separate from `_insert_attempt`, which pins a well-formed confirmed row for the tests
    that need a *valid* attempt and nothing more. Here the three columns under test are all
    parameters and typed `object`, because what the schema does with a malformed one is the
    subject.
    """
    conn.execute(
        "INSERT INTO submission_attempts (attempt_id, forecast_record_id, idempotency_key, "
        "requested_at_utc, completed_at_utc, request_payload_sha256, success, "
        "verified_by_refetch, refetch_outcome, created_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            record_id,
            f"idem-{attempt_id}",
            TS,
            TS,
            PAYLOAD_SHA,
            success,
            verified,
            refetch,
            TS,
        ),
    )


def _raw_submission_event(
    conn: sqlite3.Connection, record_id: str, event_type: str, attempt_id: str = "att-raw"
) -> None:
    """Append one submission lifecycle event by raw SQL, citing `attempt_id`.

    Bypasses `_append_event`'s derivation entirely, which is the point: it asks the
    *database* whether this event may cite this attempt. `to_status` follows from the event
    type, so an illegal pairing is refused by the transition probe rather than by this
    helper picking a destination that hides the failure under test.
    """
    seq = conn.execute(
        "SELECT coalesce(max(event_seq), 0) + 1 FROM lifecycle_events WHERE forecast_record_id = ?",
        (record_id,),
    ).fetchone()[0]
    to_status = {
        "submitted": "submitted",
        "submission_uncertain": "approved",
        "submission_failed": "failed",
    }[event_type]
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, from_status, "
        "to_status, detail_code, submission_attempt_id, occurred_at_utc, created_at_utc) "
        "VALUES (?, ?, ?, 'approved', ?, ?, ?, ?, ?)",
        (
            record_id,
            seq,
            event_type,
            to_status,
            None if event_type == "submitted" else "refetch_missing",
            attempt_id,
            TS,
            TS,
        ),
    )


def test_a_new_attempt_must_say_what_its_refetch_established(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """An attempt that does not say is exactly the ambiguity M2-711 closes.

    Omitting the column leaves it NULL, which is the shape every row written before `009`
    has -- legal for those, and refused for a new one, because a writer that may decline to
    answer reopens the question the column exists to settle.
    """
    conn, record_id = draft
    with pytest.raises(sqlite3.IntegrityError, match="refetch_outcome is required"):
        conn.execute(
            "INSERT INTO submission_attempts (attempt_id, forecast_record_id, idempotency_key, "
            "requested_at_utc, completed_at_utc, request_payload_sha256, success, "
            "verified_by_refetch, created_at_utc) VALUES ('att-raw', ?, 'idem-raw', ?, ?, ?, "
            "0, 0, ?)",
            (record_id, TS, TS, PAYLOAD_SHA, TS),
        )
    assert _counts(conn)["submission_attempts"] == 0


@pytest.mark.parametrize(
    "value",
    [
        "unknown",
        "CONFIRMED",
        "",
        " absent ",
        0,
        # A blob: TEXT affinity leaves it a blob, so `NOT IN (...)` is true of it and the
        # typeof() gate is what makes the refusal say so rather than depending on that.
        b"absent",
    ],
)
def test_the_database_refuses_a_refetch_outcome_outside_the_vocabulary(
    draft: tuple[sqlite3.Connection, str], value: object
) -> None:
    conn, record_id = draft
    with pytest.raises(sqlite3.IntegrityError, match="refetch_outcome"):
        _raw_attempt(conn, record_id, refetch=value, success=1, verified=0)
    assert _counts(conn)["submission_attempts"] == 0


@pytest.mark.parametrize(
    ("verified", "refetch"),
    [
        # A confirmation that does not claim verification, and a verification with nothing
        # that confirmed it. Both directions, because a one-sided implication leaves the
        # other half of the equivalence enforced by nothing.
        (0, "confirmed"),
        (1, "absent"),
        (1, "mismatched"),
        (1, "unreadable"),
    ],
)
def test_the_two_refetch_columns_cannot_disagree(
    draft: tuple[sqlite3.Connection, str], verified: object, refetch: object
) -> None:
    """`verified_by_refetch` is derived in Python; this is what closes the raw-SQL path.

    It is also what lets `lifecycle_events_validate_on_insert`'s `submitted` probe stay as
    003 wrote it: with this clause in force, "success = 1 AND verified_by_refetch = 1" and
    "success = 1 AND refetch_outcome = 'confirmed'" are the same condition, so restating it
    there would be a second spelling rather than a second rule.
    """
    conn, record_id = draft
    with pytest.raises(sqlite3.IntegrityError, match="must agree"):
        _raw_attempt(conn, record_id, success=1, verified=verified, refetch=refetch)
    assert _counts(conn)["submission_attempts"] == 0


@pytest.mark.parametrize(
    ("success", "verified", "refetch", "legal", "illegal"),
    [
        (0, 0, "absent", "submission_failed", "submission_uncertain"),
        (0, 0, "unreadable", "submission_uncertain", "submission_failed"),
        (0, 0, "mismatched", "submission_uncertain", "submission_failed"),
        (1, 0, "unreadable", "submission_uncertain", "submission_failed"),
    ],
)
def test_the_database_decides_the_submission_event_from_the_refetch_outcome(
    draft: tuple[sqlite3.Connection, str],
    success: int,
    verified: int,
    refetch: str,
    legal: str,
    illegal: str,
) -> None:
    """The partition, enforced by the trigger rather than agreed to by the writer.

    Both halves are asserted for every row: the legal event is accepted **and** the other
    one is refused. Asserting only the refusal would pass against a trigger that refused
    everything, and asserting only the acceptance would pass against one that enforced
    nothing -- which is the shape of defect this item exists to fix.
    """
    conn, record_id = draft
    _approve(conn, record_id)
    _raw_attempt(conn, record_id, success=success, verified=verified, refetch=refetch)
    with pytest.raises(sqlite3.IntegrityError):
        _raw_submission_event(conn, record_id, illegal)
    _raw_submission_event(conn, record_id, legal)
    assert read_history(conn, record_id)[-1].event_type == legal


def _verification(
    *,
    attempt_id: str = "att-1",
    outcome: str = "confirmed",
    **extra: object,
) -> SubmissionVerification:
    # The snapshot defaults to present because the default outcome is `confirmed`, which
    # requires one; pass None explicitly to test that rule.
    return SubmissionVerification(
        submission_attempt_id=attempt_id,
        outcome=outcome,  # type: ignore[arg-type]
        observed_at_utc=extra.pop("observed_at_utc", WHEN),  # type: ignore[arg-type]
        refetched_forecast_snapshot=extra.pop(  # type: ignore[arg-type]
            "refetched_forecast_snapshot", SNAPSHOT
        ),
        **extra,  # type: ignore[arg-type]
    )


def _leave_uncertain(conn: sqlite3.Connection, record_id: str) -> None:
    """Approve, post, and have the refetch not confirm it: the state under test."""
    _approve(conn, record_id)
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_attempt(success=True, refetch="absent"),
        occurred_at=WHEN,
        detail_code="refetch_missing",
    )
    assert current_status(conn, record_id) == "approved"


def test_an_uncertain_submission_is_resolved_by_a_refetch_not_a_second_post(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """Round 3, finding 1: the workflow round 2's fix described but could not record.

    An unconfirmed post leaves the record `approved` so a later refetch can still move it
    -- but the only event that reached `submitted` required a `submission_attempts` row,
    and `idempotency_key` is UNIQUE, so recording the resolution meant minting a second
    key for a post that was never made. The ledger's one route out of uncertainty was the
    blind retry the handoff exists to block.

    The assertion that matters is the count: one attempt, because only one request was
    ever sent.
    """
    conn, record_id = draft
    _leave_uncertain(conn, record_id)

    confirmed = record_submission_verification(
        conn,
        record_id=record_id,
        verification=_verification(refetched_forecast_snapshot='{"probability": 0.6}'),
        occurred_at=WHEN,
    )
    assert confirmed.event_type == "submission_confirmed"
    assert confirmed.submission_verification_id is not None
    assert confirmed.submission_attempt_id is None
    assert current_status(conn, record_id) == "submitted"
    assert [event.event_type for event in read_history(conn, record_id)] == [
        "validated",
        "approved",
        "submission_uncertain",
        "submission_confirmed",
    ]
    assert _counts(conn)["submission_attempts"] == 1


def test_a_refetch_that_finds_nothing_ends_the_record(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """The other resolution, and it is terminal.

    `absent` means the post is not there, which is what a (0, 0) attempt means -- and that
    lands in `failed` too. A retry from here is a new forecast version (M1-602), not a
    second attempt against a record whose history says the submission failed.
    """
    conn, record_id = draft
    _leave_uncertain(conn, record_id)

    disconfirmed = record_submission_verification(
        conn,
        record_id=record_id,
        verification=_verification(outcome="absent"),
        occurred_at=WHEN,
        detail_code="refetch_missing",
    )
    assert disconfirmed.event_type == "submission_disconfirmed"
    assert current_status(conn, record_id) == "failed"


def test_the_verification_link_is_checked_against_the_database_too(
    ledger: sqlite3.Connection,
) -> None:
    """Both halves of the link probe, reached with raw SQL rather than through the writer.

    The writer's own guard is what produces the readable message, and it is the thing a
    bypassing caller does not run: the trigger has to re-derive that the verification's
    attempt belongs to *this* record and that this record recorded it as uncertain. The
    two cases here are one probe each -- another record's uncertain attempt, and this
    record's attempt with no uncertain event to its name.

    rec-2's attempt row is written directly, without its lifecycle event, because through
    the writers there is no such thing: every attempt this module records lands one, and an
    attempt that is not uncertain has moved the record somewhere `submission_confirmed` is
    illegal from anyway. Which is the point of testing the probe rather than trusting the
    reachability argument that made it look unnecessary.
    """
    _seed_draft(ledger, record_id="rec-1")
    _seed_draft(ledger, record_id="rec-2", question_id=101)
    _leave_uncertain(ledger, "rec-1")
    _approve(ledger, "rec-2")
    _insert_attempt(ledger, "rec-2", attempt_id="att-2")

    def verify(attempt_id: str) -> int:
        return int(
            ledger.execute(
                "INSERT INTO submission_verifications (submission_attempt_id, outcome, "
                "observed_at_utc, refetched_forecast_snapshot, created_at_utc) "
                "VALUES (?, 'confirmed', ?, ?, ?)",
                (attempt_id, TS, SNAPSHOT, TS),
            ).lastrowid
            or 0
        )

    def cite(record_id: str, seq: int, verification_id: int) -> None:
        ledger.execute(
            "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
            "from_status, to_status, submission_verification_id, occurred_at_utc, "
            "created_at_utc) VALUES (?, ?, 'submission_confirmed', 'approved', 'submitted', "
            "?, ?, ?)",
            (record_id, seq, verification_id, TS, TS),
        )

    # rec-1's uncertain attempt, cited by rec-2's history.
    with pytest.raises(sqlite3.IntegrityError, match="attempt on another forecast record"):
        cite("rec-2", 3, verify("att-1"))
    # rec-2's own attempt, which was submitted outright and so has nothing to resolve.
    with pytest.raises(sqlite3.IntegrityError, match="not recorded as uncertain"):
        cite("rec-2", 3, verify("att-2"))


def test_a_second_attempt_while_uncertain_is_recorded_not_refused(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """Round 4, finding 1: the ledger records what happened, including what should not have.

    Round 3 refused this write, in the trigger and in the writer, and called it "block
    retry until refetch resolves state". It is not: `record_submission_attempt` is handed a
    receipt for a post that has *already been made*, so refusing it cannot un-make the post
    -- it only leaves a live submission with no ledger row. The SQL half was worse, because
    the attempt row committed and only its event was refused: two stored attempts, one
    event, an orphan receipt in an append-only table.

    So both attempts and both events are stored, and the state stays `approved` because
    neither refetch confirmed anything.
    """
    conn, record_id = draft
    _leave_uncertain(conn, record_id)

    second = record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_attempt(attempt_id="att-2", key="idem-2", success=True, refetch="absent"),
        occurred_at=WHEN,
        detail_code="refetch_missing",
    )
    assert second.event_type == "submission_uncertain"
    assert _counts(conn)["submission_attempts"] == 2
    assert [event.event_type for event in read_history(conn, record_id)] == [
        "validated",
        "approved",
        "submission_uncertain",
        "submission_uncertain",
    ]
    assert current_status(conn, record_id) == "approved"
    # No orphan: every stored attempt is cited by exactly one event.
    cited = [
        row[0]
        for row in conn.execute(
            "SELECT submission_attempt_id FROM lifecycle_events "
            "WHERE submission_attempt_id IS NOT NULL"
        )
    ]
    assert sorted(cited) == ["att-1", "att-2"]


def test_unresolved_uncertainties_is_the_pre_request_seam(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """What M2-704 asks *before* posting, since the writers only ever run after.

    The three states that matter: nothing outstanding, one outstanding, and resolved. A
    record that has left `approved` has nothing outstanding by construction -- no
    submission event is legal from `submitted` or `failed`.
    """
    conn, record_id = draft
    _approve(conn, record_id)
    assert unresolved_uncertainties(conn, record_id) == ()

    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_attempt(success=True, refetch="absent"),
        occurred_at=WHEN,
        detail_code="refetch_missing",
    )
    assert unresolved_uncertainties(conn, record_id) == ("att-1",)

    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_attempt(attempt_id="att-2", key="idem-2", success=True, refetch="absent"),
        occurred_at=WHEN,
        detail_code="refetch_missing",
    )
    assert unresolved_uncertainties(conn, record_id) == ("att-1", "att-2")

    record_submission_verification(
        conn,
        record_id=record_id,
        verification=_verification(refetched_forecast_snapshot="{}"),
        occurred_at=WHEN,
    )
    assert current_status(conn, record_id) == "submitted"
    assert unresolved_uncertainties(conn, record_id) == ()


def test_unresolved_uncertainties_rejects_an_unknown_record(ledger: sqlite3.Connection) -> None:
    # Same answer as the other two readers: a caller that cannot tell "nothing
    # outstanding" from "no such record" would post on the strength of the second.
    with pytest.raises(LifecycleError):
        unresolved_uncertainties(ledger, "no-such-record")


def test_a_refetch_cannot_resolve_an_attempt_that_was_never_uncertain(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """An attempt already accounted for is not open to being re-decided.

    A `submitted` attempt has its own event; letting a later refetch attach a second one
    would put two contradicting accounts of one request into an append-only log.
    """
    conn, record_id = draft
    _approve(conn, record_id)
    record_submission_attempt(conn, record_id=record_id, attempt=_attempt(), occurred_at=WHEN)
    assert current_status(conn, record_id) == "submitted"

    with pytest.raises(LifecycleError, match="nothing for a refetch to resolve"):
        record_submission_verification(
            conn, record_id=record_id, verification=_verification(), occurred_at=WHEN
        )
    assert _counts(conn)["submission_verifications"] == 0


def test_a_refetch_cannot_resolve_another_records_attempt(
    ledger: sqlite3.Connection,
) -> None:
    # The verification row stores no forecast_record_id, so "belongs to this record" is a
    # join through the attempt. Getting that wrong would let one record's refetch carry
    # another record to `submitted`.
    _seed_draft(ledger, record_id="rec-1")
    _seed_draft(ledger, record_id="rec-2", question_id=101)
    _leave_uncertain(ledger, "rec-1")
    _approve(ledger, "rec-2")
    record_submission_attempt(
        ledger,
        record_id="rec-2",
        attempt=_attempt(attempt_id="att-2", key="idem-2", success=True, refetch="absent"),
        occurred_at=WHEN,
        detail_code="refetch_missing",
    )

    with pytest.raises(LifecycleError, match="nothing for a refetch to resolve"):
        record_submission_verification(
            ledger,
            record_id="rec-2",
            verification=_verification(attempt_id="att-1"),
            occurred_at=WHEN,
        )


def test_a_refetch_cannot_be_observed_before_the_attempt_it_verifies(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # A receipt for an interval that ran backwards, in the other direction: the
    # observation of a post cannot predate the post finishing.
    conn, record_id = draft
    _leave_uncertain(conn, record_id)
    with pytest.raises(LifecycleError, match="earlier than the completion"):
        record_submission_verification(
            conn,
            record_id=record_id,
            verification=_verification(observed_at_utc=WHEN - timedelta(seconds=1)),
            occurred_at=WHEN,
        )
    with pytest.raises(sqlite3.IntegrityError, match="earlier than the completion"):
        conn.execute(
            "INSERT INTO submission_verifications (submission_attempt_id, outcome, "
            "observed_at_utc, refetched_forecast_snapshot, created_at_utc) "
            "VALUES ('att-1', 'confirmed', ?, ?, ?)",
            ("2020-01-01T00:00:00.000000+00:00", SNAPSHOT, TS),
        )


def test_a_verification_event_must_match_what_the_refetch_saw(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # Raw SQL: the outcome decides the event, so a refetch that found nothing cannot back
    # a `submission_confirmed`.
    conn, record_id = draft
    _leave_uncertain(conn, record_id)
    absent = conn.execute(
        "INSERT INTO submission_verifications (submission_attempt_id, outcome, "
        "observed_at_utc, created_at_utc) VALUES ('att-1', 'absent', ?, ?)",
        (TS, TS),
    ).lastrowid
    with pytest.raises(sqlite3.IntegrityError, match="different observation"):
        conn.execute(
            "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
            "from_status, to_status, submission_verification_id, occurred_at_utc, "
            "created_at_utc) VALUES (?, 4, 'submission_confirmed', 'approved', 'submitted', "
            "?, ?, ?)",
            (record_id, absent, TS, TS),
        )


def test_a_confirmed_refetch_rejects_a_detail_code(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # A confirmation is a success, and the schema forbids a failure code on one.
    conn, record_id = draft
    _leave_uncertain(conn, record_id)
    with pytest.raises(LifecycleError, match="not applicable"):
        record_submission_verification(
            conn,
            record_id=record_id,
            verification=_verification(),
            occurred_at=WHEN,
            detail_code="refetch_missing",
        )


@pytest.mark.parametrize(
    ("snapshot", "message"),
    [
        (None, "required for a confirmed refetch"),
        # Refused one rule earlier, by the field validator every text field goes through:
        # an empty string is not a value this ledger stores anywhere.
        ("", "must be a non-empty string"),
        ("   ", "required for a confirmed refetch"),
        # Round 5: the three cases that separate the two layers. Every one of these is
        # whitespace to `str.strip()` and so refused by the writer, and every one of them
        # reached `submitted` through the schema until the trigger pinned its own set.
        ("\n\t", "required for a confirmed refetch"),
        (" \t ", "required for a confirmed refetch"),
        ("\xa0", "required for a confirmed refetch"),
    ],
)
def test_a_confirmed_refetch_must_carry_what_it_saw(
    draft: tuple[sqlite3.Connection, str], snapshot: str | None, message: str
) -> None:
    """Round 4, finding 2: the column's rationale and its constraint disagreed.

    The snapshot is what makes a confirmation auditable rather than taken on faith, and it
    is the evidence that carries the record to `submitted` -- yet the column was
    unconditionally nullable and the writer took None. Reproduced: a `submission_confirmed`
    transition with NULL stored. Both layers now refuse it, and empty-but-present counts as
    absent because a whitespace snapshot is a snapshot of nothing.

    Round 5 found that claim was only half true, and this test is why it went unnoticed: it
    asserted both layers from the first, but every case it supplied was a *space*, the one
    whitespace character the two layers already agreed on. The writer strips by
    `str.strip()`, which removes all 29 Unicode whitespace codepoints; the trigger used
    SQLite's one-argument `trim()`, which removes U+0020 and nothing else. A snapshot of
    "\\n\\t" was refused by the writer and accepted by the schema, and carried a record to
    `submitted` on two bytes of nothing. The parameters above now cover a case the default
    `trim()` misses (tab/newline), a mixed one (a real value's whitespace is rarely
    uniform), and a non-ASCII one (NBSP arrives from real JSON).
    """
    conn, record_id = draft
    _leave_uncertain(conn, record_id)
    with pytest.raises(LifecycleError, match=message):
        record_submission_verification(
            conn,
            record_id=record_id,
            verification=_verification(refetched_forecast_snapshot=snapshot),
            occurred_at=WHEN,
        )
    assert _counts(conn)["submission_verifications"] == 0
    assert current_status(conn, record_id) == "approved"

    with pytest.raises(sqlite3.IntegrityError, match="must carry the forecast snapshot"):
        conn.execute(
            "INSERT INTO submission_verifications (submission_attempt_id, outcome, "
            "observed_at_utc, refetched_forecast_snapshot, created_at_utc) "
            "VALUES ('att-1', 'confirmed', ?, ?, ?)",
            (TS, snapshot, TS),
        )


def test_the_schemas_blank_snapshot_rule_is_the_writers_rule(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """The two layers agree on *which* characters are whitespace, over the whole set.

    Round 5's defect was not that the trigger was missing -- it was that the trigger and
    the writer each had a definition of "blank" and nobody had compared them. Three
    hand-picked parameters would not have caught it either; a space is whitespace under
    both, which is exactly why five rounds of review did not see it.

    So this asserts the equivalence directly, over every codepoint Python calls
    whitespace, against the character set the trigger pins. It is also a drift guard:
    `str.strip()` follows the Unicode data of whatever Python is running, while the
    trigger's literal is frozen the moment migration 003 lands on master. If a future
    release adds a whitespace codepoint, this fails loudly and the gap gets migration
    004 -- rather than reopening, in silence, the hole it was written to close.
    """
    conn, record_id = draft
    _leave_uncertain(conn, record_id)

    def insert(snapshot: str) -> None:
        conn.execute(
            "INSERT INTO submission_verifications (submission_attempt_id, outcome, "
            "observed_at_utc, refetched_forecast_snapshot, created_at_utc) "
            "VALUES ('att-1', 'confirmed', ?, ?, ?)",
            (TS, snapshot, TS),
        )

    whitespace = [cp for cp in range(sys.maxunicode + 1) if chr(cp).isspace()]
    # A guard on the guard: if this ever comes back empty the loop below passes while
    # asserting nothing, which is the failure mode M1-303's property tests were built on.
    assert len(whitespace) == 29
    for cp in whitespace:
        with pytest.raises(sqlite3.IntegrityError, match="must carry the forecast snapshot"):
            insert(chr(cp) * 3)

    # The other direction, so the pinned set cannot quietly grow into rejecting real
    # content: U+200B is not whitespace to Python, and a snapshot made of it is a value
    # this ledger stores rather than a snapshot of nothing.
    insert("\u200b")
    assert _counts(conn)["submission_verifications"] == 1


def test_a_disconfirming_refetch_needs_no_snapshot(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # The converse, and why the column cannot simply be NOT NULL: a refetch that found
    # nothing has nothing to store, and requiring one would mean inventing it.
    conn, record_id = draft
    _leave_uncertain(conn, record_id)
    event = record_submission_verification(
        conn,
        record_id=record_id,
        verification=_verification(outcome="absent", refetched_forecast_snapshot=None),
        occurred_at=WHEN,
        detail_code="refetch_missing",
    )
    assert event.event_type == "submission_disconfirmed"
    assert current_status(conn, record_id) == "failed"


def test_a_disconfirmed_refetch_requires_a_detail_code(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = draft
    _leave_uncertain(conn, record_id)
    with pytest.raises(LifecycleError, match="detail_code is required"):
        record_submission_verification(
            conn,
            record_id=record_id,
            verification=_verification(outcome="absent"),
            occurred_at=WHEN,
        )
    assert _counts(conn)["submission_verifications"] == 0


def test_a_refetch_rolls_back_its_own_verification_row(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """The atomicity property, for the new pair.

    A second confirmation of the same attempt writes a valid `submission_verifications`
    row and only then discovers there is no transition out of `submitted`. Without one
    transaction around both, the ledger would keep an observation that never became an
    event.
    """
    conn, record_id = draft
    _leave_uncertain(conn, record_id)
    record_submission_verification(
        conn, record_id=record_id, verification=_verification(), occurred_at=WHEN
    )
    before = _counts(conn)
    with pytest.raises(LifecycleError):
        record_submission_verification(
            conn, record_id=record_id, verification=_verification(), occurred_at=WHEN
        )
    assert _counts(conn) == before
    assert current_status(conn, record_id) == "submitted"


def test_an_uncertain_submission_requires_a_detail_code(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # Without it the row records that something unspecified went unconfirmed, which no
    # later attempt can act on.
    conn, record_id = draft
    _approve(conn, record_id)
    with pytest.raises(LifecycleError):
        record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=_attempt(success=True, refetch="absent"),
            occurred_at=WHEN,
        )
    assert current_status(conn, record_id) == "approved"
    assert _counts(conn)["submission_attempts"] == 0


@pytest.mark.parametrize(
    ("event_type", "to_status", "attempt"),
    [
        ("submission_failed", "failed", "att_unsure"),
        ("submission_uncertain", "approved", "att_ok"),
        ("submission_uncertain", "approved", "att_bad"),
    ],
)
def test_a_submission_event_cannot_cite_the_wrong_kind_of_attempt(
    draft: tuple[sqlite3.Connection, str], event_type: str, to_status: str, attempt: str
) -> None:
    # The database half of the partition: each of the three events is legal for exactly
    # one (success, verified_by_refetch) shape, so no writer can record an outcome as
    # something it was not.
    conn, record_id = draft
    detail = _walk_to(conn, record_id, "approved")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_event(
            conn,
            record_id,
            event_type,
            "approved",
            to_status,
            {**detail, _LINK_FOR[event_type]: detail[attempt]},
        )


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
            attempt=_attempt(success=False, refetch="absent"),
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


def test_a_receipt_without_a_completion_time_is_refused(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """An attempt is recorded once, after it is over, and can never be completed later.

    ``submission_attempts`` is append-only, so a receipt stored with no completion time
    keeps none for good -- and the writer's own dataclass defaulted the field to None
    (round 2, finding 5). Required in both layers: the field, and the trigger, since a
    writer that bypasses this module must not be able to store one either.
    """
    conn, record_id = draft
    _approve(conn, record_id)
    with pytest.raises(LifecycleError):
        record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=_attempt(completed_at_utc=None),
            occurred_at=WHEN,
        )
    assert _counts(conn)["submission_attempts"] == 0
    with pytest.raises(sqlite3.IntegrityError, match="completed_at_utc is required"):
        conn.execute(
            "INSERT INTO submission_attempts (attempt_id, forecast_record_id, idempotency_key, "
            "requested_at_utc, request_payload_sha256, success, verified_by_refetch, "
            "created_at_utc) VALUES ('att-raw', ?, 'idem-raw', ?, ?, 1, 1, ?)",
            (record_id, TS, PAYLOAD_SHA, TS),
        )


def test_a_receipt_cannot_complete_before_it_was_requested(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """Round 3, finding 3: the one rule this item enforced in a single layer.

    The writer compared the two instants and the schema did not, so a direct insert could
    store an attempt that completed a day before it was requested -- permanently, on an
    append-only table. Compared as instants on the Python side, and as canonical text on
    the SQL side.
    """
    conn, record_id = draft
    _approve(conn, record_id)
    with pytest.raises(LifecycleError):
        record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=_attempt(completed_at_utc=WHEN - timedelta(seconds=1)),
            occurred_at=WHEN,
        )
    assert _counts(conn)["submission_attempts"] == 0
    with pytest.raises(sqlite3.IntegrityError, match="earlier than requested_at_utc"):
        _insert_attempt(conn, record_id, requested=TS, completed="2026-07-26T00:00:00.000000+00:00")
    assert _counts(conn)["submission_attempts"] == 0


def test_a_receipt_reversed_by_one_microsecond_is_refused(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """Round 4, finding 3: the two layers disagreed at the bottom of the resolution.

    Round 3 compared with ``julianday``, which is a float *day* number -- microseconds fall
    below its precision, so two instants a microsecond apart compared exactly equal and the
    schema accepted a reversed receipt the writer rejects. Milliseconds survived, which is
    what made it look like it worked. Both layers now refuse it.
    """
    conn, record_id = draft
    _approve(conn, record_id)
    requested = WHEN + timedelta(microseconds=2)
    completed = WHEN + timedelta(microseconds=1)
    with pytest.raises(LifecycleError, match="earlier than"):
        record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=_attempt(requested_at_utc=requested, completed_at_utc=completed),
            occurred_at=WHEN,
        )
    with pytest.raises(sqlite3.IntegrityError, match="earlier than requested_at_utc"):
        _insert_attempt(
            conn,
            record_id,
            requested=requested.isoformat(timespec="microseconds"),
            completed=completed.isoformat(timespec="microseconds"),
        )
    assert _counts(conn)["submission_attempts"] == 0


def test_a_refetch_observed_one_microsecond_too_early_is_refused(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # The same precision failure on the other pair, which GPT reproduced through the
    # public writer rather than raw SQL.
    conn, record_id = draft
    _approve(conn, record_id)
    completed = WHEN + timedelta(microseconds=2)
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_attempt(success=True, refetch="absent", completed_at_utc=completed),
        occurred_at=WHEN,
        detail_code="refetch_missing",
    )
    with pytest.raises(LifecycleError, match="earlier than the completion"):
        record_submission_verification(
            conn,
            record_id=record_id,
            verification=_verification(observed_at_utc=WHEN + timedelta(microseconds=1)),
            occurred_at=WHEN,
        )
    assert _counts(conn)["submission_verifications"] == 0


@pytest.mark.parametrize(
    "completed",
    [
        "",
        "not-a-timestamp",
        "2026-13-45T99:00:00Z",
        b"\x00",
        2451545,
        # Real instants in shapes the pin refuses: no microseconds, `Z` instead of the
        # offset, and a non-UTC offset. Each of them breaks a lexicographic comparison
        # against a canonical value, which is the whole reason the shape is pinned.
        "2026-07-27T00:00:00+00:00",
        "2026-07-27T00:00:00.000000Z",
        "2026-07-27T00:00:00.000000-05:00",
    ],
)
def test_a_receipt_timestamp_outside_the_canonical_form_is_refused(
    draft: tuple[sqlite3.Connection, str], completed: object
) -> None:
    """The pin is what makes the TEXT comparison above exact.

    Ordering two stored strings only means anything if both are the same fixed-width UTC
    shape: `2026-07-27T00:00:00+00:00` sorts *before* `2026-07-27T00:00:00.000001+00:00`
    by luck (`+` < `.`), and `...00Z` sorts *after* `...00.5Z` by the same accident going
    the other way. A blob is the case the `typeof` half is for -- TEXT affinity converts a
    number to text but leaves a blob a blob.
    """
    conn, record_id = draft
    _approve(conn, record_id)
    with pytest.raises(sqlite3.IntegrityError, match="YYYY-MM-DDTHH:MM:SS"):
        _insert_attempt(conn, record_id, requested=TS, completed=completed)


def test_the_writer_renders_the_canonical_form(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # The writer's rendering and the schema's pin have to be the same string, or every
    # write through this module would be refused by its own migration. A whole-second
    # instant is the case plain isoformat() gets wrong: it drops the fractional part.
    conn, record_id = draft
    _approve(conn, record_id)
    record_submission_attempt(conn, record_id=record_id, attempt=_attempt(), occurred_at=WHEN)
    stored = conn.execute(
        "SELECT requested_at_utc, completed_at_utc FROM submission_attempts"
    ).fetchone()
    assert tuple(stored) == (TS, TS)
    assert len(TS) == 32


@pytest.mark.parametrize("status", [-1, 0, 99, 600, 1000, 2**63 - 1, 2**63])
def test_an_http_status_outside_the_http_range_is_refused(
    draft: tuple[sqlite3.Connection, str], status: int
) -> None:
    """A status is either absent or a status code; -1 and 2**63-1 are neither.

    The round-1 fix stopped the oversized case escaping as a raw ``OverflowError``, but
    every one of these still persisted as audit data, indistinguishable from a status a
    responder actually returned (round 2, finding 7).
    """
    conn, record_id = draft
    _approve(conn, record_id)
    with pytest.raises(LifecycleError):
        record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=_attempt(http_status=status),
            occurred_at=WHEN,
        )
    assert _counts(conn)["submission_attempts"] == 0


@pytest.mark.parametrize("status", [None, 100, 201, 429, 599])
def test_a_real_http_status_is_stored(
    draft: tuple[sqlite3.Connection, str], status: int | None
) -> None:
    # The complement, including both edges of the range: the guard must not be so strict
    # that a real receipt cannot be recorded.
    conn, record_id = draft
    _approve(conn, record_id)
    record_submission_attempt(
        conn, record_id=record_id, attempt=_attempt(http_status=status), occurred_at=WHEN
    )
    assert (
        conn.execute(
            "SELECT http_status FROM submission_attempts WHERE attempt_id = 'att-1'"
        ).fetchone()[0]
        == status
    )


@pytest.mark.parametrize("status", [-1, 600, 200.5, "ok"])
def test_the_database_refuses_a_malformed_http_status(
    draft: tuple[sqlite3.Connection, str], status: object
) -> None:
    # The trigger behind the validator. typeof() is part of it because INTEGER is
    # affinity, not a type: 200.5 cannot convert losslessly and stays REAL, 'ok' is not
    # numeric and stays TEXT, and TEXT sorts above every number in SQLite -- so a bare
    # `BETWEEN 100 AND 599` would let it through. `'200'` is deliberately absent for the
    # reason the event_seq test gives: affinity converts a well-formed integer literal
    # before any trigger sees it, so that row is correct rather than rejected.
    conn, record_id = draft
    with pytest.raises(sqlite3.IntegrityError, match="http_status"):
        conn.execute(
            "INSERT INTO submission_attempts (attempt_id, forecast_record_id, idempotency_key, "
            "requested_at_utc, completed_at_utc, request_payload_sha256, http_status, success, "
            "verified_by_refetch, created_at_utc) "
            "VALUES ('att-raw', ?, 'idem-raw', ?, ?, ?, ?, 1, 1, ?)",
            (record_id, TS, TS, PAYLOAD_SHA, status, TS),
        )


# (event_type, from_status, to_status) for every event that is not a failure or an
# uncertainty -- the six that must carry no detail_code at all.
_SUCCESS_EVENTS = (
    ("validated", "draft", "validated"),
    ("rejected", "validated", "validated"),
    ("approved", "validated", "approved"),
    ("submitted", "approved", "submitted"),
    ("resolved", "submitted", "resolved"),
    ("scored", "resolved", "scored"),
)


@pytest.mark.parametrize(("event_type", "from_status", "to_status"), _SUCCESS_EVENTS)
def test_a_successful_event_cannot_carry_a_failure_code(
    ledger: sqlite3.Connection, event_type: str, from_status: str, to_status: str
) -> None:
    """The converse of "a failure must say why", which the first cut left open.

    Nothing stopped a `validated` or `submitted` row carrying detail_code =
    'internal_error', so the immutable history could hold a success annotated with a
    failure (round 2, finding 8). `rejected` is in this list by owner decision: a
    rejection is a decision, not a failure, and its account is the actor and note on the
    approval row it cites -- which is why `rejected_by_reviewer` is no longer a
    FailureCode at all.
    """
    record_id = f"rec-{event_type}"
    _seed_draft(ledger, record_id=record_id, question_id=900 + len(event_type))
    detail = _walk_to(ledger, record_id, from_status)
    with pytest.raises(sqlite3.IntegrityError, match="carries no detail_code"):
        _insert_event(
            ledger,
            record_id,
            event_type,
            from_status,
            to_status,
            detail,
            detail_code="internal_error",
        )


def test_a_resolution_for_another_question_cannot_resolve_this_forecast(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """Pointing at the right record is not the same claim as resolving the right question.

    ``resolution_events`` carries its own ``question_id``, and the link probe checked only
    ``forecast_record_id`` -- so another question's outcome could resolve this forecast,
    and M5-803 would then score it against that outcome (round 2, finding 6).
    """
    conn, record_id = draft
    detail = _walk_to(conn, record_id, "submitted")
    foreign = conn.execute(
        "INSERT INTO resolution_events (question_id, forecast_record_id, ingested_at_utc) "
        "VALUES (99999, ?, ?)",
        (record_id, TS),
    ).lastrowid
    with pytest.raises(sqlite3.IntegrityError, match="resolves a different question"):
        _insert_event(
            conn,
            record_id,
            "resolved",
            "submitted",
            "resolved",
            {**detail, "resolved": foreign},
        )
    assert current_status(conn, record_id) == "submitted"


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


class _FailingRelease(sqlite3.Connection):
    """A real connection whose savepoint RELEASE fails; same construction as above."""

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        if sql.startswith("RELEASE "):
            raise sqlite3.OperationalError("database is locked")
        return super().execute(sql, parameters)  # type: ignore[arg-type]


def test_a_failing_release_raises_lifecycle_error_and_spares_the_outer_transaction(
    tmp_path: Path,
) -> None:
    """The nested counterpart of the failing-COMMIT test (GPT round 1, risk area 1).

    A `RELEASE` that fails must unwind only the inner block: `ROLLBACK TO` first, then
    the paired `RELEASE` to pop the savepoint -- and `_unwind` has to attempt the second
    even though the first is what just failed, or the savepoint is leaked onto a
    connection the caller goes on using. The caller's own transaction stays open and
    committable, because it is not this module's to end.
    """
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = sqlite3.connect(db, factory=_FailingRelease)
    try:
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        _seed_run(conn)
        with transaction(conn):  # the caller's transaction: BEGIN, never RELEASEd
            _seed_draft(conn, record_id="rec-outer")
            with pytest.raises(LifecycleError):
                with transaction(conn):  # nested: SAVEPOINT, whose RELEASE fails
                    _seed_draft(conn, record_id="rec-inner", question_id=103)
            assert conn.in_transaction
        # The inner block is gone, the outer one committed, and no savepoint survived to
        # strand the next write on this connection.
        stored = conn.execute(
            "SELECT record_id FROM forecast_records ORDER BY record_id"
        ).fetchall()
        assert [row[0] for row in stored] == ["rec-outer"]
        assert not conn.in_transaction
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

    def submit_subclass() -> LifecycleEvent:
        return record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=HostileAttempt(
                attempt_id="att-1",
                idempotency_key="idem-1",
                requested_at_utc=WHEN,
                completed_at_utc=WHEN,
                request_payload_sha256=PAYLOAD_SHA,
                success=True,
                refetch_outcome="confirmed",
            ),
            occurred_at=WHEN,
        )

    def verify(**overrides: object) -> LifecycleEvent:
        # Refused for the transition (the record is only `validated`), after the fields
        # have been read -- the same shape as `submit`.
        return record_submission_verification(
            conn,
            record_id=record_id,
            verification=_verification(**overrides),  # type: ignore[arg-type]
            occurred_at=WHEN,
        )

    def verify_subclass() -> LifecycleEvent:
        return record_submission_verification(
            conn,
            record_id=record_id,
            verification=HostileVerification(
                submission_attempt_id="att-1",
                outcome="confirmed",
                observed_at_utc=WHEN,
            ),
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
        "verified_attempt_id": lambda: verify(attempt_id=PLANTED_SECRET),
        "verified_outcome": lambda: verify(outcome=PLANTED_SECRET),
        "snapshot_over_cap": lambda: verify(refetched_forecast_snapshot=PLANTED_SECRET * 6000),
        "hostile_verification_subclass": verify_subclass,
        # The three GPT round 1 found: the value is not a field this module reads, but
        # something it *calls* -- a timezone, an attribute, an integer conversion -- so
        # the leak arrives as an exception raised elsewhere rather than as stored text.
        "hostile_timezone": lambda: approve(
            occurred_at=datetime(2026, 7, 27, tzinfo=HostileTimezone())
        ),
        "hostile_attempt_subclass": submit_subclass,
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
        "verified_attempt_id",
        "verified_outcome",
        "snapshot_over_cap",
        "hostile_verification_subclass",
        "hostile_timezone",
        "hostile_attempt_subclass",
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


def test_an_oversized_integer_is_refused_at_the_field(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    """Python ints are unbounded; SQLite's are signed 64-bit.

    Without the range check `sqlite3` raises `OverflowError` while binding the parameter
    -- and `OverflowError` is not a `sqlite3.Error`, so it escaped the wrapper in
    `_insert` and reached the caller as an undocumented type. (GPT round 1, finding 3.)
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
    with pytest.raises(LifecycleError, match="outside the range"):
        record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=_attempt(http_status=10**100),
            occurred_at=WHEN,
        )
    assert _counts(conn)["submission_attempts"] == 0
    assert not conn.in_transaction


def test_the_binding_wrapper_also_catches_an_oversized_integer(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # The field validator is the first line; this pins the second, since a later writer
    # could reach _insert with an integer that never passed through _require_optional_int.
    conn, _ = draft
    with pytest.raises(LifecycleError, match="the ledger rejected this write"):
        lifecycle._insert(
            conn,
            "INSERT INTO resolution_events (question_id, ingested_at_utc) VALUES (?, ?)",
            (10**100, TS),
        )


def test_a_submission_attempt_subclass_is_refused(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # isinstance would admit it, and every `attempt.<field>` read after that is a call
    # into caller-supplied code -- between the two writes, where a raised exception is
    # most expensive.
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
    with pytest.raises(LifecycleError, match="must be a SubmissionAttempt"):
        record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=HostileAttempt(
                attempt_id="att-1",
                idempotency_key="idem-1",
                requested_at_utc=WHEN,
                completed_at_utc=WHEN,
                request_payload_sha256=PAYLOAD_SHA,
                success=True,
                refetch_outcome="confirmed",
            ),
            occurred_at=WHEN,
        )
    assert _counts(conn)["submission_attempts"] == 0


def test_a_submission_verification_subclass_is_refused(
    draft: tuple[sqlite3.Connection, str],
) -> None:
    # The same exact-type gate on the second writer that reads a caller's dataclass
    # between two writes.
    conn, record_id = draft
    _leave_uncertain(conn, record_id)
    with pytest.raises(LifecycleError, match="must be a SubmissionVerification"):
        record_submission_verification(
            conn,
            record_id=record_id,
            verification=HostileVerification(
                submission_attempt_id="att-1",
                outcome="confirmed",
                observed_at_utc=WHEN,
            ),
            occurred_at=WHEN,
        )
    assert _counts(conn)["submission_verifications"] == 0


# --------------------------------------------------------------------------------------
# M1-606: pre-forecast pipeline failures, attempt-scoped.
#
# Same split as the rest of this file. That a failure *cannot* be recorded in a shape the
# ledger would later have to reason around is a property of migration 004's triggers, so
# it is tested with raw SQL bypassing `lifecycle` entirely -- a writer-level suite cannot
# find a schema hole at all, which is M1-603's round-5 lesson. That the writer refuses the
# same shapes as its own error type, before billing a statement, is tested through the
# writer. Both entry points, because the truth table has to hold at both.
# --------------------------------------------------------------------------------------


PRE_FORECAST_EVENT_TYPES: tuple[str, ...] = get_args(PreForecastEventType)
PRE_FORECAST_FAILURE_CODES: tuple[str, ...] = get_args(PreForecastFailureCode)


def _failures(conn: sqlite3.Connection, attempt_id: str = FAILED_ATTEMPT) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pipeline_failure_events WHERE attempt_id = ? ORDER BY event_seq",
        (attempt_id,),
    ).fetchall()


def test_a_failure_is_recordable_with_no_forecast_record_in_existence(
    ledger: sqlite3.Connection,
) -> None:
    """Acceptance criterion 1, at its sharpest and with nothing else in the database.

    The whole reason this table exists: 001 requires `final_prediction_json`,
    `record_json` and `retrieval_run_id` on `forecast_records`, so a research failure has
    no record to hang a `lifecycle_events` row from and never will. The assertion is that
    the event is *queryable*, not merely accepted -- an insert nobody can read back is not
    a ledger entry.
    """
    assert ledger.execute("SELECT count(*) FROM forecast_records").fetchone()[0] == 0
    event = record_pre_forecast_failure(
        ledger,
        attempt_id="att-orphan",
        question_id=100,
        tournament_id="minibench",
        event_type="research_failed",
        detail_code="provider_unavailable",
        occurred_at=WHEN,
    )
    assert event.event_seq == 1
    assert read_pipeline_failure_events(ledger, "att-orphan") == (event,)


# ---- The schema is the enforcement: raw SQL, no writers involved. ---------------------


@pytest.mark.parametrize("bad_seq", ["x", 1.5, b"1", 0, -1])
def test_a_failure_event_seq_must_be_a_positive_integer(
    ledger: sqlite3.Connection, bad_seq: object
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="positive integer"):
        _seed_failure(ledger, event_seq=bad_seq)


@pytest.mark.parametrize("coerced", ["1", 1.0, "01"])
def test_an_affinity_coerced_event_seq_is_stored_as_a_real_integer(
    ledger: sqlite3.Connection, coerced: object
) -> None:
    """`typeof()` is checked because INTEGER is affinity, and affinity is why it passes.

    This is the counterpart the truth table needs and the one it is easy to get wrong. A
    `'1'` bound to an INTEGER-affinity column is converted *before* the BEFORE INSERT
    trigger sees `NEW.event_seq`, so it reaches the trigger already `typeof() = 'integer'`
    and is accepted -- correctly, because what lands in the row is a genuine integer 1.

    Asserting only "accepted" would prove nothing, so the assertion is on the *stored*
    type. And a test that fed `'1'` expecting a refusal would have passed against a
    trigger with no `typeof()` clause at all, for the wrong reason: the values that
    actually reach that clause are the ones affinity cannot losslessly convert, which is
    the sibling test above.
    """
    _seed_failure(ledger, event_seq=coerced)
    stored = ledger.execute(
        "SELECT event_seq, typeof(event_seq) FROM pipeline_failure_events"
    ).fetchone()
    assert (stored[0], stored[1]) == (1, "integer")


def test_a_failure_event_seq_must_be_the_next_one_for_its_attempt(
    ledger: sqlite3.Connection,
) -> None:
    _seed_failure(ledger, event_seq=1)
    with pytest.raises(sqlite3.IntegrityError, match="next sequence number"):
        _seed_failure(ledger, event_seq=3)
    with pytest.raises(sqlite3.IntegrityError, match="next sequence number"):
        _seed_failure(ledger, event_seq=1)
    _seed_failure(ledger, event_seq=2)
    assert [row["event_seq"] for row in _failures(ledger)] == [1, 2]


def test_failure_sequences_are_independent_across_attempts(ledger: sqlite3.Connection) -> None:
    # Two campaigns run against the same question without either one's history renumbering
    # the other -- the same guarantee `lifecycle_events` gives per forecast record.
    _seed_failure(ledger, attempt_id="att-a", event_seq=1)
    _seed_failure(ledger, attempt_id="att-b", event_seq=1)
    _seed_failure(ledger, attempt_id="att-a", event_seq=2)
    assert [row["event_seq"] for row in _failures(ledger, "att-a")] == [1, 2]
    assert [row["event_seq"] for row in _failures(ledger, "att-b")] == [1]


def test_an_attempt_that_already_succeeded_cannot_also_fail(ledger: sqlite3.Connection) -> None:
    # Success and failure are both terminal for one attempt_id. Without this an
    # immutable ledger could show a campaign that both produced a forecast version and
    # did not, with no way to say which happened.
    _seed_draft(ledger, record_id="rec-won", attempt_id="att-won")
    with pytest.raises(sqlite3.IntegrityError, match="already produced a successful"):
        _seed_failure(ledger, attempt_id="att-won")


@pytest.mark.parametrize(("question_id", "tournament_id"), [(101, "minibench"), (100, "other-cup")])
def test_an_attempt_id_cannot_change_the_question_it_names(
    ledger: sqlite3.Connection, question_id: int, tournament_id: str
) -> None:
    # An attempt_id names one campaign toward one forecast version for one question. If a
    # second event could move it, the link acceptance criterion 2 rests on would join a
    # failure to a success that was never about the same thing.
    _seed_failure(ledger, event_seq=1)
    with pytest.raises(sqlite3.IntegrityError, match="different question or tournament"):
        _seed_failure(ledger, event_seq=2, question_id=question_id, tournament_id=tournament_id)


def test_a_forecast_record_cannot_claim_an_attempt_id_from_another_question(
    ledger: sqlite3.Connection,
) -> None:
    """The identity-stability probe from the `forecast_records` side.

    Only reachable from raw SQL today -- nothing in `src/` inserts a `forecast_records`
    row -- but it is the half that makes the link trustworthy in the direction the join
    is actually read: failures first, success later.
    """
    _seed_failure(ledger, attempt_id="att-shared", question_id=100)
    with pytest.raises(sqlite3.IntegrityError, match="different question or tournament"):
        _seed_draft(ledger, record_id="rec-wrong", question_id=101, attempt_id="att-shared")
    _seed_draft(ledger, record_id="rec-right", question_id=100, attempt_id="att-shared")
    assert (
        ledger.execute(
            "SELECT record_id FROM forecast_records WHERE attempt_id = ?", ("att-shared",)
        ).fetchone()["record_id"]
        == "rec-right"
    )


def test_a_generation_failure_must_cite_the_research_run_it_followed(
    ledger: sqlite3.Connection,
) -> None:
    # Generation only runs once research has completed, so there is always a run to cite;
    # a generation failure with none is a claim nobody can audit. Research failures are
    # the opposite case -- the provider call may never have been made.
    with pytest.raises(sqlite3.IntegrityError, match="requires retrieval_run_id"):
        _seed_failure(ledger, attempt_id="att-gen", event_type="generation_failed")
    _seed_failure(ledger, attempt_id="att-res", event_type="research_failed")
    assert _failures(ledger, "att-res")[0]["retrieval_run_id"] is None


def test_a_cited_research_run_must_exist(ledger: sqlite3.Connection) -> None:
    # BEFORE INSERT runs ahead of the foreign-key check, so this arrives with the
    # schema's own message rather than a generic `FOREIGN KEY constraint failed`.
    with pytest.raises(sqlite3.IntegrityError, match="does not name a stored research run"):
        _seed_failure(ledger, retrieval_run_id="run-nope")


def test_a_cited_research_run_must_be_for_this_question(ledger: sqlite3.Connection) -> None:
    # `run-1` is the fixture's run for question 100. A failure about a different question
    # citing it would attribute one question's evidence to another's failure.
    with pytest.raises(sqlite3.IntegrityError, match="linked research run is for another"):
        _seed_failure(ledger, question_id=101, retrieval_run_id="run-1")
    _seed_failure(ledger, question_id=100, retrieval_run_id="run-1")
    assert _failures(ledger)[0]["retrieval_run_id"] == "run-1"


@pytest.mark.parametrize("event_type", ["approved", "submitted", "", "RESEARCH_FAILED"])
def test_the_failure_event_vocabulary_is_closed(
    ledger: sqlite3.Connection, event_type: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _seed_failure(ledger, event_type=event_type)


@pytest.mark.parametrize("detail_code", ["refetch_mismatch", "refetch_missing", "", "nope"])
def test_the_refetch_codes_are_not_reachable_before_a_forecast_exists(
    ledger: sqlite3.Connection, detail_code: str
) -> None:
    # Both refetch codes describe what a refetch saw of an already-posted forecast, which
    # cannot have happened before generation has even succeeded once. Named explicitly
    # rather than left to "some value outside the set", because these two are the members
    # of `FailureCode` this table deliberately does not carry.
    assert detail_code not in PRE_FORECAST_FAILURE_CODES
    with pytest.raises(sqlite3.IntegrityError):
        _seed_failure(ledger, detail_code=detail_code)


def test_every_pre_forecast_code_the_module_declares_is_storable(
    ledger: sqlite3.Connection,
) -> None:
    """The other direction, so the closed-vocabulary tests cannot both pass vacuously.

    A CHECK narrower than the `Literal` would make a legal `detail_code` unwritable, and
    every test above would still be green. Enumerated from `get_args` rather than
    restated, so adding a member to one side without the other fails here.
    """
    for index, code in enumerate(PRE_FORECAST_FAILURE_CODES):
        _seed_failure(ledger, attempt_id=f"att-code-{index}", detail_code=code)
    for index, event_type in enumerate(PRE_FORECAST_EVENT_TYPES):
        _seed_failure(
            ledger,
            attempt_id=f"att-type-{index}",
            event_type=event_type,
            retrieval_run_id="run-1",
        )
    assert ledger.execute("SELECT count(*) FROM pipeline_failure_events").fetchone()[0] == len(
        PRE_FORECAST_FAILURE_CODES
    ) + len(PRE_FORECAST_EVENT_TYPES)


def _insert_record_raw(
    conn: sqlite3.Connection,
    attempt_id: object = "att-raw",
    *,
    record_id: object = "rec-raw",
    tournament_id: object = "minibench",
) -> None:
    """A `forecast_records` INSERT that passes its identifiers through untouched.

    `_seed_draft`'s `attempt_id or f"att-{record_id}"` default substitutes a good value
    for every falsy one, so it cannot be used to test what the column refuses -- it would
    turn `None`, `''` and `'   '` into three more runs of the happy path. `record_id` and
    `tournament_id` are parameters for M1-607's guards on the same trigger, and typed
    `object` for `_seed_failure`'s reason: a helper that could only produce well-formed
    values could not reach the clauses these tests are about.
    """
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
        "forecast_sha256, attempt_id) "
        "VALUES (?, 100, ?, 1, 'binary', 'draft', 'anthropic', "
        "'claude', 'v1', 'abc', 'run-1', ?, '{}', '{}', ?, ?, ?)",
        (record_id, tournament_id, TS, TS, SHA, attempt_id),
    )


@pytest.mark.parametrize("bad_attempt", [None, "", "   ", "\t\n", " ", b"x"])
def test_a_new_record_must_carry_a_non_blank_attempt_id(
    ledger: sqlite3.Connection, bad_attempt: object
) -> None:
    """004's extension of `forecast_records_require_draft_on_insert`.

    `forecast_records` is UPDATE-blocked, so a row admitted without a usable attempt_id
    could never be given one: an optional link is a permanently unjoinable row, not a
    deferrable detail. See `docs/M1-NOTES.md`'s M1-606 section.

    `'\\t\\n'` and `'\\u00a0'` are here because the first draft of this clause used
    one-argument `trim()`, which strips U+0020 and nothing else, and accepted both. That
    is M1-603's round-5 defect reproduced in a new migration by copying the idiom from
    before its fix -- and it is why the sibling test below asserts the whole set rather
    than these two samples.
    """
    with pytest.raises(sqlite3.IntegrityError, match="attempt_id"):
        _insert_record_raw(ledger, bad_attempt)


def test_a_record_attempt_id_over_200_characters_is_refused(
    ledger: sqlite3.Connection,
) -> None:
    """M1-606 review round 1, finding B1, the other end of the join key.

    Without this ceiling the two tables could disagree about what a valid attempt_id is
    by length rather than blankness -- the same failure mode
    `test_both_attempt_id_columns_agree_with_the_writer_on_what_blank_means` guards for
    blank. 200 is still accepted.
    """
    with pytest.raises(sqlite3.IntegrityError, match="attempt_id"):
        _insert_record_raw(ledger, "x" * 201)
    _insert_record_raw(ledger, "y" * 200)


def test_a_record_attempt_id_with_an_embedded_nul_is_refused(
    ledger: sqlite3.Connection,
) -> None:
    """M1-606 review round 1, finding B1, round 2.

    SQLite's `length()` stops counting at an embedded NUL rather than counting the full
    string, so a 202-character attempt_id with a NUL early in it read as `length() == 1`
    to the round-1 guard and passed -- reopening the exact unreadable-row failure that
    guard exists to close. `instr(..., char(0)) > 0` finds the NUL directly instead of
    relying on a length count that disagrees with Python's.
    """
    with pytest.raises(sqlite3.IntegrityError, match="attempt_id"):
        _insert_record_raw(ledger, "a\x00" + "b" * 200)


@pytest.mark.parametrize("coerced", [42, 1.5])
def test_an_affinity_coerced_attempt_id_is_stored_as_real_text(
    ledger: sqlite3.Connection, coerced: object
) -> None:
    # The TEXT-affinity counterpart of the event_seq case: a number bound to a TEXT column
    # arrives at the trigger already converted, so `typeof() <> 'text'` cannot refuse it
    # and what lands is a genuine, non-blank text identifier. A blob is what that clause
    # actually catches -- affinity leaves it a blob, and `_stored_text` could not read it
    # back. Asserted on the stored type, because "accepted" alone would prove nothing.
    _insert_record_raw(ledger, coerced)
    stored = ledger.execute(
        "SELECT attempt_id, typeof(attempt_id) FROM forecast_records WHERE record_id = 'rec-raw'"
    ).fetchone()
    assert (stored["attempt_id"], stored[1]) == (str(coerced), "text")


def test_both_attempt_id_columns_agree_with_the_writer_on_what_blank_means(
    ledger: sqlite3.Connection,
) -> None:
    """The whole whitespace set, at all three places that define "blank" for an attempt_id.

    003's round 5 was not a missing trigger -- it was two layers each holding a definition
    of blank that nobody had compared, and a space is blank under both, which is exactly
    why hand-picked parameters missed it for five rounds. So this asserts the equivalence
    directly over every codepoint Python calls whitespace, against the character set the
    two triggers pin and the `str.strip()` the writer uses.

    It is also a drift guard: `str.strip()` follows the Unicode data of whatever Python is
    running, while the triggers' literal freezes when 004 lands on master. A future
    whitespace codepoint fails here loudly instead of reopening the hole in silence.
    """
    whitespace = [cp for cp in range(sys.maxunicode + 1) if chr(cp).isspace()]
    # A guard on the guard: an empty list would make the loop assert nothing at all.
    assert len(whitespace) == 29
    for cp in whitespace:
        blank = chr(cp) * 3
        with pytest.raises(sqlite3.IntegrityError, match="attempt_id"):
            _insert_record_raw(ledger, blank)
        with pytest.raises(sqlite3.IntegrityError, match="attempt_id must be non-blank"):
            _seed_failure(ledger, attempt_id=blank)
        with pytest.raises(LifecycleError, match="attempt_id must not be blank"):
            record_pre_forecast_failure(
                ledger,
                attempt_id=blank,
                question_id=100,
                tournament_id="minibench",
                event_type="research_failed",
                detail_code="provider_error",
                occurred_at=WHEN,
            )

    # The other direction, so the pinned set cannot quietly grow into rejecting a real
    # identifier: U+200B is not whitespace to Python, and an attempt_id built from it is a
    # value this ledger stores rather than a blank one.
    _seed_failure(ledger, attempt_id="​")
    assert _failures(ledger, "​")


@pytest.mark.parametrize("bad_attempt", [None, "", "   ", "\t\n", b"x"])
def test_a_failure_must_carry_a_non_blank_attempt_id(
    ledger: sqlite3.Connection, bad_attempt: object
) -> None:
    # The same guard on the other end of the join key. Without it a failure could be
    # recorded under a value no forecast_records row is permitted to claim, so the link
    # acceptance criterion 2 rests on would be unjoinable by construction -- on a table
    # that can never be corrected.
    with pytest.raises(sqlite3.IntegrityError, match="attempt_id must be non-blank"):
        _seed_failure(ledger, attempt_id=bad_attempt)  # type: ignore[arg-type]


def test_a_failure_attempt_id_over_200_characters_is_refused(
    ledger: sqlite3.Connection,
) -> None:
    """M1-606 review round 1, finding B1.

    Raw SQL accepted any nonblank attempt_id, with no ceiling matching
    `lifecycle._require_identifier`'s 200-character limit. So a >200-character attempt_id
    could be written directly -- bypassing the writer -- and the row, being append-only,
    would be permanently unreadable through `read_pipeline_failure_events`, which refuses
    anything over that same limit. 200 is still accepted, and the row it produces is
    readable back through the public reader, closing the loop the finding named.
    """
    with pytest.raises(sqlite3.IntegrityError, match="attempt_id"):
        _seed_failure(ledger, attempt_id="x" * 201)

    _seed_failure(ledger, attempt_id="y" * 200)
    assert len(read_pipeline_failure_events(ledger, "y" * 200)) == 1


def test_a_failure_attempt_id_with_an_embedded_nul_is_refused(
    ledger: sqlite3.Connection,
) -> None:
    """M1-606 review round 1, finding B1, round 2 -- the other end of the join key.

    Same SQLite `length()`-vs-NUL mismatch as the sibling test, on this table's own
    guard. The writer's `_require_identifier` refuses U+0000 outright too, at whatever
    length it appears -- the raw-SQL boundary and the writer must not disagree about
    what a valid attempt_id is, the same reasoning the blank and length guards rest on.
    """
    with pytest.raises(sqlite3.IntegrityError, match="attempt_id"):
        _seed_failure(ledger, attempt_id="a\x00" + "b" * 200)
    with pytest.raises(LifecycleError, match="attempt_id"):
        record_pre_forecast_failure(
            ledger,
            attempt_id="a\x00b",
            question_id=100,
            tournament_id="minibench",
            event_type="research_failed",
            detail_code="provider_error",
            occurred_at=WHEN,
        )


@pytest.mark.parametrize("bad_tournament", [None, "", "  ", "\t\n", b"x"])
def test_a_failure_must_carry_a_non_blank_tournament_id(
    ledger: sqlite3.Connection, bad_tournament: object
) -> None:
    # tournament_id is stored on the row rather than joined through, for the reason 001
    # gives on resolution_events -- so nothing else in the schema can vouch for it.
    with pytest.raises(sqlite3.IntegrityError, match="tournament_id must be non-blank"):
        _seed_failure(ledger, tournament_id=bad_tournament)


@pytest.mark.parametrize("bad_question", ["x", 1.5, None, b"1"])
def test_a_failure_question_id_must_be_an_integer(
    ledger: sqlite3.Connection, bad_question: object
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _seed_failure(ledger, question_id=bad_question)


def test_one_attempt_succeeds_at_most_once(ledger: sqlite3.Connection) -> None:
    """The partial UNIQUE index on `forecast_records.attempt_id`.

    Two forecast versions claiming one campaign would make "the forecast version that
    later succeeds" ambiguous for every failure recorded under it.

    The second record takes a **different** `question_id`, and the assertion names the
    constraint. Both matter: the first draft of this test reused question 100, which
    collides with 001's `UNIQUE (question_id, tournament_id, forecast_version)` — so it
    passed with 004's index deleted, on a refusal from an unrelated constraint that
    `match="UNIQUE constraint failed"` could not tell apart. Mutation-checked after the
    fix; it now fails when the index is removed.
    """
    _seed_draft(ledger, record_id="rec-1", question_id=100, attempt_id="att-once")
    with pytest.raises(sqlite3.IntegrityError, match=r"forecast_records\.attempt_id"):
        _seed_draft(ledger, record_id="rec-2", question_id=101, attempt_id="att-once")


def test_rows_written_before_migration_004_keep_a_null_attempt_id(tmp_path: Path) -> None:
    """The nullable carve-out, and the reason the partial index is partial.

    `ADD COLUMN` cannot add NOT NULL without a default, and no default is honest for an
    identifier nobody minted. So pre-004 rows keep NULL -- and because `WHERE attempt_id
    IS NOT NULL` excludes them, *several* such rows coexist under a UNIQUE index. A plain
    UNIQUE index would have made this migration undeployable against any ledger holding
    two forecast records; that it is deployable is what this asserts.

    Unreachable through the writers by construction (they are the thing that requires an
    attempt_id), which is why it is driven from a genuine older-schema database rather
    than by relaxing anything.
    """
    db = tmp_path / "ledger.sqlite3"
    _seed_v2_ledger(db)
    conn = connect(db)
    try:
        # A second pre-004 record, so what the index tolerates is more than one NULL.
        conn.execute(
            "INSERT INTO forecast_records ("
            "record_id, question_id, tournament_id, forecast_version, question_type, status, "
            "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
            "generated_at_utc, final_prediction_json, record_json, created_at_utc) "
            "VALUES ('rec-legacy-2', 102, 'minibench', 1, 'binary', 'draft', 'anthropic', "
            "'claude', 'v1', 'abc', 'run-1', ?, '{}', '{}', ?)",
            (TS, TS),
        )
    finally:
        conn.close()

    # 7, not 4: M1-306's 005_research_run_counters.sql landed alongside 004,
    # M1-607's 006_non_blank_identifiers.sql after them, and M1-602's
    # 007_forecast_version_chain.sql after that. What this line pins is
    # unchanged -- that LEDGER_SCHEMA_VERSION tracks the migrations actually on disk,
    # and that upgrading a v2 ledger runs all of them. The literal is kept rather than
    # dropped, since a constant compared only against itself pins nothing.
    #
    # The v2 rows this seeds are non-blank in every column 006 guards, so they pass its
    # upgrade precondition; `test_migration_006_refuses_a_ledger_holding_a_blank_
    # identifier` is the other side of that.
    #
    # 008 (M1-406) adds three NULLable columns and appends clauses to the same insert
    # trigger, with no upgrade probe of its own, so a v2 ledger reaching 8 is the same
    # statement it was when it reached 7.
    #
    # 010 (M2-708) creates two new tables and rewrites no existing trigger body -- the
    # first migration since 003 to touch none of them -- and has no upgrade precondition
    # scan, because nothing can predate tables the same migration creates. So a v2 ledger
    # reaching 10 is again the same statement it was when it reached 9.
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION == 10

    conn = connect(db)
    try:
        stored = conn.execute(
            "SELECT record_id, attempt_id FROM forecast_records ORDER BY record_id"
        ).fetchall()
        assert [(row["record_id"], row["attempt_id"]) for row in stored] == [
            (LEGACY_RECORD, None),
            ("rec-legacy-2", None),
        ]
    finally:
        conn.close()


def test_migration_004_is_applied_and_recorded(ledger: sqlite3.Connection) -> None:
    # The migration is immutable once on master; this is what would notice an edit to a
    # file the ledger has already applied somewhere.
    recorded = ledger.execute("SELECT checksum FROM schema_migrations WHERE version = 4").fetchone()
    assert recorded["checksum"] == _checksum_of("004_pipeline_failure_events.sql")


# ---- The writer: same refusals, as this module's own error type. ----------------------


def test_the_writer_returns_the_row_the_ledger_stored(ledger: sqlite3.Connection) -> None:
    # Read back after insert rather than assembled from the arguments, so what a caller
    # gets is what the ledger holds -- including anything its constraints coerced.
    event = record_pre_forecast_failure(
        ledger,
        attempt_id="att-1",
        question_id=100,
        tournament_id="minibench",
        event_type="generation_failed",
        detail_code="schema_invalid",
        occurred_at=WHEN,
        retrieval_run_id="run-1",
    )
    stored = ledger.execute(
        "SELECT * FROM pipeline_failure_events WHERE event_id = ?", (event.event_id,)
    ).fetchone()
    assert asdict(event) == dict(stored)
    assert isinstance(event, PreForecastFailure)


def test_the_writer_numbers_an_attempts_failures_from_one(ledger: sqlite3.Connection) -> None:
    seqs = [
        record_pre_forecast_failure(
            ledger,
            attempt_id="att-1",
            question_id=100,
            tournament_id="minibench",
            event_type="research_failed",
            detail_code="timeout",
            occurred_at=WHEN,
        ).event_seq
        for _ in range(3)
    ]
    assert seqs == [1, 2, 3]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_id", None),
        ("attempt_id", ""),
        ("attempt_id", 42),
        ("attempt_id", "a" * 201),
        ("question_id", "100"),
        ("question_id", None),
        ("question_id", 1.5),
        ("tournament_id", None),
        ("tournament_id", 7),
        ("event_type", "approved"),
        ("event_type", None),
        ("detail_code", "refetch_mismatch"),
        ("detail_code", None),
        ("occurred_at", WHEN.replace(tzinfo=None)),
        ("occurred_at", "2026-07-27"),
        ("occurred_at", None),
        ("retrieval_run_id", 7),
        ("retrieval_run_id", "r" * 201),
    ],
)
def test_the_writer_refuses_a_malformed_field_as_a_lifecycle_error(
    ledger: sqlite3.Connection, field: str, value: object
) -> None:
    """Every malformed shape arrives as this module's own error type, and writes nothing.

    `sqlite3.IntegrityError` leaking out of here would be a review finding twice over:
    callers only handle `LifecycleError`, and a database message is a channel that can
    echo the value that caused it.
    """
    kwargs: dict[str, object] = {
        "attempt_id": "att-1",
        "question_id": 100,
        "tournament_id": "minibench",
        "event_type": "research_failed",
        "detail_code": "provider_error",
        "occurred_at": WHEN,
    }
    kwargs[field] = value
    with pytest.raises(LifecycleError):
        record_pre_forecast_failure(ledger, **kwargs)  # type: ignore[arg-type]
    assert ledger.execute("SELECT count(*) FROM pipeline_failure_events").fetchone()[0] == 0


def test_the_writer_requires_a_run_for_a_generation_failure_before_any_statement(
    ledger: sqlite3.Connection,
) -> None:
    # The trigger enforces this too. What the writer adds is that it is refused as a
    # readable LifecycleError rather than the opaque "the ledger rejected this write"
    # `_insert` is obliged to raise, and refused before a statement runs at all.
    with pytest.raises(LifecycleError, match="retrieval_run_id is required"):
        record_pre_forecast_failure(
            ledger,
            attempt_id="att-1",
            question_id=100,
            tournament_id="minibench",
            event_type="generation_failed",
            detail_code="schema_invalid",
            occurred_at=WHEN,
        )
    assert ledger.execute("SELECT count(*) FROM pipeline_failure_events").fetchone()[0] == 0


def test_the_writer_refuses_an_attempt_that_already_succeeded(ledger: sqlite3.Connection) -> None:
    _seed_draft(ledger, record_id="rec-won", attempt_id="att-won")
    with pytest.raises(LifecycleError, match="already produced a stored forecast record"):
        record_pre_forecast_failure(
            ledger,
            attempt_id="att-won",
            question_id=100,
            tournament_id="minibench",
            event_type="research_failed",
            detail_code="timeout",
            occurred_at=WHEN,
        )
    assert ledger.execute("SELECT count(*) FROM pipeline_failure_events").fetchone()[0] == 0


def test_a_refused_second_failure_leaves_the_first_one_intact(ledger: sqlite3.Connection) -> None:
    """Atomicity, driven by a real trigger refusal rather than an injected exception.

    The identity-stability trigger is the one guard the writer does *not* pre-check, so
    this is the reachable path where `_insert` raises mid-transaction. Nothing is mocked:
    what must hold is that the rollback leaves the attempt's existing history untouched
    and its sequence unadvanced.
    """
    first = record_pre_forecast_failure(
        ledger,
        attempt_id="att-1",
        question_id=100,
        tournament_id="minibench",
        event_type="research_failed",
        detail_code="no_evidence",
        occurred_at=WHEN,
    )
    with pytest.raises(LifecycleError):
        record_pre_forecast_failure(
            ledger,
            attempt_id="att-1",
            question_id=101,
            tournament_id="minibench",
            event_type="research_failed",
            detail_code="no_evidence",
            occurred_at=WHEN,
        )
    assert read_pipeline_failure_events(ledger, "att-1") == (first,)
    later = record_pre_forecast_failure(
        ledger,
        attempt_id="att-1",
        question_id=100,
        tournament_id="minibench",
        event_type="research_failed",
        detail_code="timeout",
        occurred_at=WHEN,
    )
    assert later.event_seq == 2


def test_the_reader_returns_an_attempts_failures_in_append_order(
    ledger: sqlite3.Connection,
) -> None:
    written = [
        record_pre_forecast_failure(
            ledger,
            attempt_id="att-1",
            question_id=100,
            tournament_id="minibench",
            event_type="research_failed",
            detail_code=code,
            occurred_at=WHEN,
        )
        for code in ("provider_error", "no_evidence", "timeout")
    ]
    record_pre_forecast_failure(
        ledger,
        attempt_id="att-other",
        question_id=100,
        tournament_id="minibench",
        event_type="research_failed",
        detail_code="stale_evidence",
        occurred_at=WHEN,
    )
    assert read_pipeline_failure_events(ledger, "att-1") == tuple(written)


def test_the_reader_answers_an_unknown_attempt_with_no_events(ledger: sqlite3.Connection) -> None:
    """Deliberately not the `read_history` behaviour, and the asymmetry is the point.

    `read_history` raises on an unknown record because a `forecast_records` row is an
    identity that either exists or does not. An attempt_id has no such row: nothing
    creates one before the first event cites it, so "this attempt never failed" and "no
    such attempt" are the same observable state and `()` is the only honest answer.
    """
    assert read_pipeline_failure_events(ledger, "att-never-existed") == ()


@pytest.mark.parametrize("attempt_id", [None, "", 42, "a" * 201])
def test_the_reader_refuses_a_malformed_attempt_id(
    ledger: sqlite3.Connection, attempt_id: object
) -> None:
    with pytest.raises(LifecycleError):
        read_pipeline_failure_events(ledger, attempt_id)  # type: ignore[arg-type]


def test_a_stored_row_outside_the_vocabulary_is_refused_on_the_way_out(
    ledger: sqlite3.Connection,
) -> None:
    """Values read back out of the ledger are untrusted (CLAUDE.md's threat boundary).

    The row is planted with `PRAGMA ignore_check_constraints` -- not to model an attacker,
    but because the reachable case is a ledger file some other program or an older build
    wrote, and that is the only way to produce one from inside this suite. The CHECK is
    what stops this module writing such a row; the mapper's re-gating is what stops a
    caller receiving a `PreForecastFailure` whose `detail_code` is outside its own type.
    """
    ledger.execute("PRAGMA ignore_check_constraints = ON")
    try:
        _seed_failure(ledger, detail_code="refetch_mismatch")
    finally:
        ledger.execute("PRAGMA ignore_check_constraints = OFF")

    with pytest.raises(LifecycleError, match="detail_code"):
        read_pipeline_failure_events(ledger, FAILED_ATTEMPT)


def test_no_rejected_value_reaches_a_pre_forecast_failure_message(
    ledger: sqlite3.Connection,
) -> None:
    # The leak channel is the message *and* the rendered traceback: a chained exception
    # reprints the value it was raised from, which is why the writers raise `from None`.
    with pytest.raises(LifecycleError) as caught:
        record_pre_forecast_failure(
            ledger,
            attempt_id=PLANTED_SECRET * 40,
            question_id=100,
            tournament_id="minibench",
            event_type="research_failed",
            detail_code="provider_error",
            occurred_at=WHEN,
        )
    rendered = "".join(traceback.format_exception(caught.value))
    assert PLANTED_SECRET not in str(caught.value)
    assert PLANTED_SECRET not in rendered


# ======================================================================================
# M1-607: the non-blank identifier guard, on the columns that predate 004.
# ======================================================================================
#
# 004 built this guard for `attempt_id` and deliberately stopped there, because widening
# `_require_text` would have changed what already-shipped writers accept. These are the
# columns it left: `forecast_records.record_id` and `.tournament_id`,
# `submission_attempts.attempt_id` and `.idempotency_key`, and
# `research_runs.retrieval_run_id` -- whose writer-side half lives in
# `test_research_store.py`, where its writer does.

# Every column 006 guards. Driving the tests below from one list rather than writing five
# of each by hand is the point: the defect this item closes is a guard present on one
# column and absent on the column beside it, and hand-written per-column tests are exactly
# the shape that misses that again.
GUARDED_COLUMNS = [
    "record_id",
    "tournament_id",
    "attempt_id",
    "idempotency_key",
    "retrieval_run_id",
]

# The four whose readers cap at `bounds.MAX_IDENTIFIER_LENGTH`, and which therefore take the ceiling.
# `retrieval_run_id` is deliberately absent -- see
# `test_the_run_id_column_has_no_length_ceiling_and_that_is_deliberate`.
CAPPED_COLUMNS = GUARDED_COLUMNS[:-1]


@pytest.fixture
def guarded(ledger: sqlite3.Connection) -> tuple[sqlite3.Connection, str]:
    """A ledger with a forecast record for `submission_attempts` rows to hang off.

    `question_id=101`, not the `_seed_draft` default: `_insert_record_raw` writes
    `question_id=100` and 001's `UNIQUE (question_id, tournament_id, forecast_version)`
    would otherwise fail the record inserts below on the wrong constraint entirely.
    """
    return ledger, _seed_draft(ledger, record_id="rec-owner", question_id=101)


def _insert_for(conn: sqlite3.Connection, record_id: str, column: str) -> Callable[[object], None]:
    """The raw insert that puts `value` into `column` and leaves every sibling well-formed.

    The siblings matter as much as the target. A helper that derived `idempotency_key`
    from `attempt_id` would push a 200-character attempt_id past the *key's* ceiling and
    report a pass for the wrong clause, and one that reused a constant key would trip the
    UNIQUE index instead. So the untested fields are minted fresh per call.
    """
    unique = next(_MINTED)
    return {
        "record_id": lambda value: _insert_record_raw(
            conn, f"att-{unique}", record_id=value, tournament_id="minibench"
        ),
        "tournament_id": lambda value: _insert_record_raw(
            conn, f"att-{unique}", record_id=f"rec-{unique}", tournament_id=value
        ),
        "attempt_id": lambda value: _insert_attempt(
            conn, record_id, attempt_id=value, key=f"idem-{unique}"
        ),
        "idempotency_key": lambda value: _insert_attempt(
            conn, record_id, attempt_id=f"att-{unique}", key=value
        ),
        "retrieval_run_id": lambda value: _seed_run(conn, value),  # type: ignore[arg-type]
    }[column]


_MINTED = itertools.count()


# `''` and `'   '` were refused by nothing at all on these columns. `'\t\n'` and `'\xa0'`
# are the ones that make the point, because one-argument `trim()` strips U+0020 alone and
# passes both -- M1-603's round 5. `b'x'` is the blob case, worse than blank: `_stored_text`
# refuses it on the way *out*, so the row is append-only and unreadable at once. `None` is
# here even though all five columns are declared NOT NULL, because a trigger must not
# depend on a table constraint it did not write.
BLANK_IDENTIFIERS: list[object] = [None, "", " ", "   ", "\t\n", "\xa0", b"x"]


@pytest.mark.parametrize("column", GUARDED_COLUMNS)
@pytest.mark.parametrize("bad", BLANK_IDENTIFIERS)
def test_the_schema_refuses_a_blank_or_blob_identifier(
    guarded: tuple[sqlite3.Connection, str], column: str, bad: object
) -> None:
    conn, record_id = guarded
    with pytest.raises(sqlite3.IntegrityError, match=column):
        _insert_for(conn, record_id, column)(bad)


@pytest.mark.parametrize("column", CAPPED_COLUMNS)
def test_the_schema_refuses_an_identifier_over_200_characters(
    guarded: tuple[sqlite3.Connection, str], column: str
) -> None:
    """The readers' ceiling, mirrored into the schema (004's finding B1, on new columns).

    Every public entry point in `lifecycle.py` and `approval.py` caps an identifier at
    `bounds.MAX_IDENTIFIER_LENGTH`, so a longer one written through raw SQL would be a row that is
    append-only and permanently unlookupable. 200 itself stays accepted: a ceiling that
    refused the boundary would be a different rule from the one the writers apply, which
    is the same disagreement in the other direction.
    """
    conn, record_id = guarded
    with pytest.raises(sqlite3.IntegrityError, match=column):
        _insert_for(conn, record_id, column)("x" * 201)
    _insert_for(conn, record_id, column)("y" * 200)


@pytest.mark.parametrize("column", CAPPED_COLUMNS)
def test_the_schema_refuses_an_identifier_with_an_embedded_nul(
    guarded: tuple[sqlite3.Connection, str], column: str
) -> None:
    """Round 2 of the same finding, on each new column.

    SQLite's `length()` stops counting at an embedded NUL, so a 202-character identifier
    with a NUL early in it reads as `length() == 1` to the ceiling above and passes, while
    the writers' Python `len()` sees 202 and refuses it on read-back -- the unreadable row
    the ceiling exists to close, reopened through the one input the two counting functions
    disagree about. `instr(..., char(0))` is what removes that input.
    """
    conn, record_id = guarded
    with pytest.raises(sqlite3.IntegrityError, match=column):
        _insert_for(conn, record_id, column)("a\x00" + "b" * 200)


def test_every_guarded_column_agrees_with_the_writers_on_what_blank_means(
    guarded: tuple[sqlite3.Connection, str],
) -> None:
    """The whole whitespace set, at every layer that defines "blank" for an identifier.

    003's round 5 was not a missing trigger -- it was two layers each holding a definition
    of blank that nobody had compared, and a space is blank under both, which is exactly
    why hand-picked parameters missed it for five rounds. So this asserts the equivalence
    directly over every codepoint Python calls whitespace, against the character set the
    triggers pin and the `str.strip()` the writers use.

    It is also a drift guard: `str.strip()` follows the Unicode data of whatever Python is
    running, while the triggers' literal freezes when 006 lands on master. A future
    whitespace codepoint fails here loudly instead of reopening the hole in silence.
    """
    conn, record_id = guarded
    whitespace = [cp for cp in range(sys.maxunicode + 1) if chr(cp).isspace()]
    # A guard on the guard: an empty list would make the loops below assert nothing.
    assert len(whitespace) == 29

    for cp in whitespace:
        blank = chr(cp) * 3
        for column in GUARDED_COLUMNS:
            with pytest.raises(sqlite3.IntegrityError, match=column):
                _insert_for(conn, record_id, column)(blank)

        # The writer half, for the identifiers this module's public entry points take.
        # `record_id` is checked through a reader as well as a writer: both go through
        # `_require_identifier`, and a reader that accepted a blank would look up a row
        # the schema guarantees cannot exist and report "no such record" for what is
        # really a caller mistake.
        with pytest.raises(LifecycleError, match="record_id must not be blank"):
            current_status(conn, blank)
        with pytest.raises(LifecycleError, match="record_id must not be blank"):
            record_validation(conn, record_id=blank, occurred_at=WHEN)
        with pytest.raises(LifecycleError, match=r"attempt\.attempt_id must not be blank"):
            record_submission_attempt(
                conn, record_id=record_id, attempt=_attempt(attempt_id=blank), occurred_at=WHEN
            )
        with pytest.raises(LifecycleError, match=r"attempt\.idempotency_key must not be blank"):
            record_submission_attempt(
                conn, record_id=record_id, attempt=_attempt(key=blank), occurred_at=WHEN
            )

    # Nothing above was allowed to land. A guard that refused 28 codepoints and wrote a row
    # for the 29th would still satisfy every `pytest.raises` in the loop.
    assert _counts(conn)["submission_attempts"] == 0
    assert conn.execute("SELECT count(*) FROM forecast_records").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM research_runs").fetchone()[0] == 1

    # The other direction, so the pinned set cannot quietly grow into rejecting real
    # identifiers: U+200B is not whitespace to Python, and an id built from it is a value
    # this ledger stores rather than a blank one.
    _seed_run(conn, "​")
    assert (
        conn.execute(
            "SELECT count(*) FROM research_runs WHERE retrieval_run_id = ?", ("​",)
        ).fetchone()[0]
        == 1
    )


def test_a_numeric_identifier_is_stored_as_text_rather_than_refused(
    ledger: sqlite3.Connection,
) -> None:
    """What `typeof()` actually catches, asserted so the clause is not misread.

    On a TEXT-affinity column `42` arrives already converted to `'42'` and is accepted --
    correctly, since what lands is genuine text. So the clause earns its place by rejecting
    what affinity *cannot* convert, which is a blob. This is 004's own lesson: a test that
    fed `42` expecting a refusal would pass against a trigger with no `typeof()` clause at
    all, green for the wrong reason.
    """
    _seed_run(ledger, 42)  # type: ignore[arg-type]
    stored = ledger.execute(
        "SELECT retrieval_run_id, typeof(retrieval_run_id) FROM research_runs "
        "WHERE retrieval_run_id = '42'"
    ).fetchone()
    assert (stored[0], stored[1]) == ("42", "text")


def test_the_run_id_column_has_no_length_ceiling_and_that_is_deliberate(
    ledger: sqlite3.Connection,
) -> None:
    """The one asymmetry in 006, pinned so it cannot be "tidied" into consistency.

    The four columns above take a 200-character ceiling because their readers do: a longer
    value would be a row that can never be read back. Nothing in `research/store.py` caps
    `retrieval_run_id` on the way out, so that defect does not exist on this column, and
    adding a ceiling would refuse input the shipped M1-306 writer accepts -- the
    behaviour-change-to-merged-code this whole item exists to avoid making by accident.

    Asserted rather than left as a comment, because a comment does not fail when someone
    makes the five columns "uniform".
    """
    _seed_run(ledger, "y" * 201)
    assert (
        ledger.execute(
            "SELECT count(*) FROM research_runs WHERE length(retrieval_run_id) = 201"
        ).fetchone()[0]
        == 1
    )


# The migrations a ledger at version 8 has applied -- everything before M2-711. Spelled
# out rather than sliced off a listing of the package, for `_PRE_006_MIGRATIONS`' reason:
# a list that grows by itself would silently stop describing "before 009" the day 010
# lands, and this fixture's whole subject is what a pre-009 ledger looks like.
_PRE_009_MIGRATIONS = [
    "001_initial.sql",
    "002_research_document_fields.sql",
    "003_lifecycle_events.sql",
    "004_pipeline_failure_events.sql",
    "005_research_run_counters.sql",
    "006_non_blank_identifiers.sql",
    "007_forecast_version_chain.sql",
    "008_forecast_raw_output.sql",
]


def _seed_v8_ledger(db: Path, *, record_id: str = "rec-legacy") -> str:
    """A ledger at version 8 holding an approved record and a failed, unverified attempt.

    `_seed_v5_ledger`'s method -- the packaged migration files applied directly, their real
    checksums recorded -- so `ledger.py` accepts it as a database a previous build genuinely
    produced. The attempt is `(success=0, verified_by_refetch=0)`, the cell M2-711 splits,
    written when there was no column to split it on.
    """
    attempt_id = "att-legacy"
    conn = connect(db)
    try:
        for name in _PRE_009_MIGRATIONS:
            conn.executescript(files("whiskeyjack_bot.migrations").joinpath(name).read_text())
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
            "'abc', 'run-1', ?, '{}', '{}', ?, ?, 'att-record')",
            (record_id, TS, TS, SHA),
        )
        approval = conn.execute(
            "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
            "created_at_utc) VALUES (?, 'approved', 'chris', ?, ?)",
            (record_id, SHA, TS),
        ).lastrowid
        conn.execute(
            "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
            "from_status, to_status, occurred_at_utc, created_at_utc) "
            "VALUES (?, 1, 'validated', 'draft', 'validated', ?, ?)",
            (record_id, TS, TS),
        )
        conn.execute(
            "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
            "from_status, to_status, approval_event_id, occurred_at_utc, created_at_utc) "
            "VALUES (?, 2, 'approved', 'validated', 'approved', ?, ?, ?)",
            (record_id, approval, TS, TS),
        )
        conn.execute(
            "INSERT INTO submission_attempts (attempt_id, forecast_record_id, "
            "idempotency_key, requested_at_utc, completed_at_utc, request_payload_sha256, "
            "success, verified_by_refetch, created_at_utc) "
            "VALUES (?, ?, 'idem-legacy', ?, ?, ?, 0, 0, ?)",
            (attempt_id, record_id, TS, TS, PAYLOAD_SHA, TS),
        )
        for version, name in enumerate(_PRE_009_MIGRATIONS, start=1):
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at_utc, checksum) "
                "VALUES (?, ?, ?)",
                (version, TS, _checksum_of(name)),
            )
    finally:
        conn.close()
    return attempt_id


def test_an_attempt_written_before_009_still_partitions_by_the_old_rule(
    tmp_path: Path,
) -> None:
    """M2-711 must not change what an already-written row means.

    `009` adds a NULLable column, so every pre-existing attempt holds NULL in it -- and
    both triggers read that NULL as 003 read the absence of the information:
    `COALESCE(refetch_outcome, 'absent')`. So a legacy `(0, 0)` attempt is still
    `submission_failed` and still cannot be recorded as uncertain.

    This is the reason the COALESCE is not a default. A new row cannot reach it: the receipt
    trigger refuses one that does not name an outcome (asserted above). It exists for rows
    written when there was nothing to name.
    """
    db = tmp_path / "ledger.sqlite3"
    attempt_id = _seed_v8_ledger(db)
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION == 10

    conn = connect(db)
    try:
        stored = conn.execute(
            "SELECT refetch_outcome FROM submission_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        assert stored[0] is None
        # Uncertain is what the widened rule would say if NULL fell through it; it does not.
        with pytest.raises(sqlite3.IntegrityError):
            _raw_submission_event(conn, "rec-legacy", "submission_uncertain", attempt_id)
        _raw_submission_event(conn, "rec-legacy", "submission_failed", attempt_id)
        assert current_status(conn, "rec-legacy") == "failed"
    finally:
        conn.close()


# The migrations a ledger at version 5 has applied -- everything before this item.
_PRE_006_MIGRATIONS = [
    "001_initial.sql",
    "002_research_document_fields.sql",
    "003_lifecycle_events.sql",
    "004_pipeline_failure_events.sql",
    "005_research_run_counters.sql",
]


def _seed_v5_ledger(
    db: Path,
    *,
    record_id: object = "rec-legacy",
    tournament_id: object = "minibench",
    run_id: object = "run-1",
    attempt_id: object = "att-legacy",
    key: object = "idem-legacy",
) -> None:
    """A ledger at schema version 5: everything before 006, with one record and one attempt.

    Written by applying the packaged migration files directly and recording their real
    checksums, so this is a database a previous build genuinely produced rather than a
    hand-built approximation -- `ledger.py` verifies those checksums on every open and
    would refuse a fake.

    Every identifier is a parameter, and typed `object`: 006's whole subject is what these
    columns were allowed to hold before it existed, and a helper that could only write
    well-formed values could not set up the case.
    """
    conn = connect(db)
    try:
        for name in _PRE_006_MIGRATIONS:
            conn.executescript(files("whiskeyjack_bot.migrations").joinpath(name).read_text())
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, question_id, "
            "started_at_utc, created_at_utc) VALUES (?, 'asknews', 100, ?, ?)",
            (run_id, TS, TS),
        )
        conn.execute(
            "INSERT INTO forecast_records ("
            "record_id, question_id, tournament_id, forecast_version, question_type, status, "
            "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
            "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
            "forecast_sha256, attempt_id) "
            "VALUES (?, 100, ?, 1, 'binary', 'draft', 'anthropic', 'claude', 'v1', 'abc', "
            "?, ?, '{}', '{}', ?, ?, 'att-record')",
            (record_id, tournament_id, run_id, TS, TS, SHA),
        )
        conn.execute(
            "INSERT INTO submission_attempts (attempt_id, forecast_record_id, "
            "idempotency_key, requested_at_utc, completed_at_utc, request_payload_sha256, "
            "success, verified_by_refetch, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)",
            (attempt_id, record_id, key, TS, TS, PAYLOAD_SHA, TS),
        )
        for version, name in enumerate(_PRE_006_MIGRATIONS, start=1):
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at_utc, checksum) "
                "VALUES (?, ?, ?)",
                (version, TS, _checksum_of(name)),
            )
    finally:
        conn.close()


def test_a_clean_v5_ledger_upgrades_to_006(tmp_path: Path) -> None:
    # The control for the refusals below. Without it, a migration that refused *every*
    # v5 ledger -- a malformed probe, say -- would pass all of them.
    #
    # The name says 006 and the assertion says 7: this test is about 006's precondition,
    # and `initialize_ledger` applies every unrecorded migration, so it now runs 007
    # (M1-602) and 008 (M1-406) as well. Neither adds an upgrade probe of its own -- each
    # redefines the one insert trigger, and 008's added columns are NULLable -- so a clean
    # v5 ledger reaching 8 is the same statement it was when it reached 6. 009 (M2-711)
    # and 010 (M2-708) extend that: 009's added column is NULLable and read through a
    # COALESCE, and 010 only creates tables, so neither probes the rows a v5 ledger holds.
    db = tmp_path / "ledger.sqlite3"
    _seed_v5_ledger(db)
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION == 10


@pytest.mark.parametrize(
    "legacy",
    [
        {"record_id": "\t\n"},
        {"record_id": "x" * 201},
        {"record_id": "a\x00b"},
        {"tournament_id": "\xa0"},
        {"run_id": "  "},
        {"attempt_id": "\t\n"},
        {"key": "\xa0"},
    ],
)
def test_migration_006_refuses_a_ledger_holding_a_blank_identifier(
    tmp_path: Path, legacy: dict[str, object]
) -> None:
    """A `BEFORE INSERT` trigger binds new rows only, so the upgrade checks the old ones.

    Without this the guarantee would quietly become "every row written after 006" -- and
    these are append-only tables, so a violating row can never be corrected afterwards. A
    blank or blob identifier is precisely the row that is unjoinable and unreadable for
    good, which is the condition this item exists to make impossible. So the upgrade
    refuses rather than shipping a rule with a grandfathered exception, the same call 003
    made about a non-draft record for the same reason.

    The parameters are one per guarded column plus the length and NUL cases, because the
    probe is meant to be the *same predicate* as the trigger: a probe that checked only
    blankness would leave two of the three ways a legacy row can be unreadable.
    """
    db = tmp_path / "ledger.sqlite3"
    _seed_v5_ledger(db, **legacy)  # type: ignore[arg-type]

    with pytest.raises(LedgerError) as caught:
        initialize_ledger(db)

    # The refusal must not echo the offending value -- `ledger.py` wraps the failure with
    # `from None` and a message built only from the filename-derived version number.
    for value in legacy.values():
        assert str(value) not in str(caught.value)

    # And it is clean: the migration ran inside a transaction, so the database sits at
    # version 5 with 006's triggers unapplied rather than half-upgraded.
    conn = connect(db)
    try:
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 5
        assert (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'migration_006%'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_migration_006s_upgrade_precondition_leaves_nothing_behind(tmp_path: Path) -> None:
    """The guard is a temp table, so it must not survive either outcome.

    On the refusal path it is rolled back with the rest of the migration (asserted above).
    On the success path it is dropped. A leftover table in either schema would be a
    migration writing something the schema does not document -- 003 makes the same
    assertion about its own precondition.
    """
    db = tmp_path / "ledger.sqlite3"
    _seed_v5_ledger(db)
    initialize_ledger(db)
    conn = connect(db)
    try:
        assert (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'migration_006%'"
            ).fetchone()[0]
            == 0
        )
        # A fresh ledger takes the same path from empty, so assert it there too.
        fresh = tmp_path / "fresh.sqlite3"
        initialize_ledger(fresh)
        other = connect(fresh)
        try:
            assert (
                other.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name LIKE 'migration_006%'"
                ).fetchone()[0]
                == 0
            )
        finally:
            other.close()
    finally:
        conn.close()


def test_rows_written_before_006_survive_it_when_their_identifiers_are_well_formed(
    tmp_path: Path,
) -> None:
    """The converse of the refusal: 006 rejects violating rows, not old rows.

    A migration that refused every pre-006 ledger would satisfy every test above while
    being undeployable, which is the failure mode 002 and 003 both name when they explain
    why their added columns stay NULLable.
    """
    db = tmp_path / "ledger.sqlite3"
    _seed_v5_ledger(db)
    assert initialize_ledger(db) == 10
    conn = connect(db)
    try:
        assert current_status(conn, "rec-legacy") == "draft"
        assert (
            conn.execute(
                "SELECT count(*) FROM submission_attempts WHERE attempt_id = 'att-legacy'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()
