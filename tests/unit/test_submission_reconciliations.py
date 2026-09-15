"""M2-713: the schema and the writer for a live post the ledger never recorded.

Two layers, tested the way the rest of the ledger is. ``016_submission_reconciliations.sql``
is the enforcement, so every clause is probed with a raw INSERT that bypasses the writer --
M2-708's mutation pass found five of six clauses unreachable through a writer that refused
the same cases first. :func:`lifecycle.record_submission_reconciliation` restates the clauses
for readable messages, so each Python guard is tested by the message it produces: with the
guard gone the trigger still refuses, but as ``the ledger rejected this write``.

The orchestrator that assembles the evidence and makes the refetch is
``test_submission_reconcile.py``'s.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

import pytest

from reconciliation_rows import (
    CONFIRMING_SNAPSHOT,
    RECONCILED_AT,
    RESERVED_AT,
    Unrecorded,
    insert_reconciled_event,
    insert_reconciliation,
    seed_reconciled_post,
    seed_unrecorded_post,
)
from resolution_rows import seed_record, walk_to_submitted_raw
from whiskeyjack_bot import ledger as ledger_module
from whiskeyjack_bot.ledger import LEDGER_SCHEMA_VERSION, connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    SubmissionReconciliation,
    current_status,
    read_history,
    record_submission_reconciliation,
)
from whiskeyjack_bot.submission import (
    SubmissionError,
    attempt_for_key,
    key_is_reconciled,
    live_reservation_for_key,
    live_reservations_for_record,
    release_submission_key,
    require_key_unused,
    reserve_submission_key,
)
from whiskeyjack_bot.submission_live import live_attempt_id

SHA = "b" * 64
PAYLOAD_SHA = "d" * 64
OTHER_SHA = "e" * 64
TS = "2026-09-15T09:00:00.000000+00:00"
WHEN = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
RECORD = "rec-unrecorded"


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, question_id, started_at_utc, "
            "created_at_utc) VALUES ('run-1', 'asknews', 500, ?, ?)",
            (TS, TS),
        )
        yield conn
    finally:
        conn.close()


@pytest.fixture
def post(ledger: sqlite3.Connection) -> Unrecorded:
    return _unrecorded(ledger)


def _unrecorded(
    conn: sqlite3.Connection, record_id: str = RECORD, question_id: int = 500
) -> Unrecorded:
    return seed_unrecorded_post(
        conn,
        record_id,
        question_id=question_id,
        run_id="run-1",
        forecast_sha256=SHA,
        payload_sha256=PAYLOAD_SHA,
    )


def _counts(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
    return tuple(  # type: ignore[return-value]
        conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "submission_reconciliations",
            "lifecycle_events",
            "submission_key_releases",
            "submission_attempts",
        )
    )


def _reconciliation(post: Unrecorded, **changes: object) -> SubmissionReconciliation:
    base = SubmissionReconciliation(
        reservation_id=post.reservation_id,
        attempt_id=post.attempt_id,
        request_payload_sha256=post.payload_sha256,
        intent_event_id=post.intent_event_id,
        observed_by="chris",
        note="saw 35% on the question page",
        refetched_at_utc=WHEN,
        refetched_forecast_snapshot=CONFIRMING_SNAPSHOT,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def _insert_attempt(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    attempt_id: str,
    key: str,
    success: int = 1,
    refetch: str = "confirmed",
) -> None:
    conn.execute(
        "INSERT INTO submission_attempts (attempt_id, forecast_record_id, idempotency_key, "
        "requested_at_utc, completed_at_utc, request_payload_sha256, success, "
        "verified_by_refetch, refetch_outcome, created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            record_id,
            key,
            TS,
            TS,
            PAYLOAD_SHA,
            success,
            1 if refetch == "confirmed" else 0,
            refetch,
            TS,
        ),
    )


def _event(
    conn: sqlite3.Connection,
    record_id: str,
    seq: int,
    event_type: str,
    frm: str,
    to: str,
    **links: object,
) -> None:
    columns: dict[str, object] = {
        "forecast_record_id": record_id,
        "event_seq": seq,
        "event_type": event_type,
        "from_status": frm,
        "to_status": to,
        "occurred_at_utc": TS,
        "created_at_utc": TS,
        **links,
    }
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO lifecycle_events ({names}) VALUES ({marks})", tuple(columns.values())
    )


# --------------------------------------------------------------------------------------
# The rewrite of 009's trigger changes exactly the marked hunks.
# --------------------------------------------------------------------------------------


def _trigger_body(migration: str) -> str:
    text = files("whiskeyjack_bot.migrations").joinpath(migration).read_text(encoding="utf-8")
    matches = re.findall(
        r"^CREATE TRIGGER lifecycle_events_validate_on_insert\n.*?^END;$", text, re.S | re.M
    )
    assert len(matches) == 1, migration
    return str(matches[0])


def test_016_rewrites_009s_lifecycle_trigger_only_in_its_marked_hunks() -> None:
    """``016``'s header says NOTHING ELSE CHANGED; this is what makes that a checked claim.

    Strip every ``>>> M2-713`` ... ``<<< M2-713`` block and every line carrying the inline
    ``-- M2-713`` mark, and what is left must be 009's body byte for byte. A clause dropped or
    reworded in the copy -- the silent failure of a DROP/CREATE -- fails here.
    """
    rewritten = _trigger_body("016_submission_reconciliations.sql")
    stripped = re.sub(r"    -- >>> M2-713\n.*?    -- <<< M2-713\n\n", "", rewritten, flags=re.S)
    stripped = "\n".join(line for line in stripped.split("\n") if "-- M2-713" not in line)
    assert stripped == _trigger_body("009_submission_refetch_outcome.sql")
    # And the marks are really there: three hunks, so the strip above removed something.
    assert rewritten.count("-- >>> M2-713") == 2
    assert rewritten.count("-- M2-713:") == 1


def test_the_applied_trigger_is_the_one_016_writes(ledger: sqlite3.Connection) -> None:
    """The file is what the diff test reads; the database is what runs. Tie them together."""
    stored = ledger.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
        "AND name = 'lifecycle_events_validate_on_insert'"
    ).fetchone()[0]
    assert stored == _trigger_body("016_submission_reconciliations.sql").removesuffix(";")


# --------------------------------------------------------------------------------------
# The shape the schema accepts.
# --------------------------------------------------------------------------------------


def test_a_reconciliation_carries_an_approved_record_to_submitted(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    reconciliation_id = insert_reconciliation(ledger, post)
    insert_reconciled_event(ledger, RECORD, reconciliation_id)
    assert current_status(ledger, RECORD) == "submitted"
    last = read_history(ledger, RECORD)[-1]
    assert (last.event_type, last.submission_reconciliation_id) == (
        "submission_confirmed",
        reconciliation_id,
    )
    assert (last.submission_attempt_id, last.submission_verification_id) == (None, None)


def test_the_captured_artifact_is_pinned_by_path_and_digest(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    insert_reconciliation(
        ledger, post, artifact_path="submissions/live/500/k.json", artifact_sha256=OTHER_SHA
    )
    row = ledger.execute(
        "SELECT artifact_path, artifact_sha256 FROM submission_reconciliations"
    ).fetchone()
    assert tuple(row) == ("submissions/live/500/k.json", OTHER_SHA)


# Every clause of `submission_reconciliations_validate_on_insert`, probed raw. Each override
# breaks exactly one clause and leaves every earlier one satisfied; the fragment is the
# clause's own message, so a clause removed and caught by a later one still fails the test.
_BLANKS = ["", "   ", "　", " \t"]
_ROW_PROBES: list[tuple[str, object, str]] = [
    *[
        ("reconciliation_id", v, "reconciliation_id must be non-blank")
        for v in [*_BLANKS, None, "a\x00b", "x" * 201, b"blob"]
    ],
    (
        "forecast_record_id",
        "rec-nobody",
        "forecast_record_id does not name a stored forecast record",
    ),
    (
        "reservation_id",
        "wjres-nobody",
        "does not name a key reservation held against this forecast record",
    ),
    *[
        ("attempt_id", v, "attempt_id must be a live attempt identifier")
        for v in [
            "wjdry-1-" + "a" * 64,
            "wjlive-1-" + "A" * 64,
            "wjlive-1-" + "a" * 63,
            "wjlive-1-" + "a" * 65,
            7,
            b"wjlive-1-" + b"a" * 64,
        ]
    ],
    *[
        ("request_payload_sha256", v, "request_payload_sha256 must be 64 lowercase hex")
        for v in ["D" * 64, "d" * 63, 5, b"d" * 64]
    ],
    ("request_payload_sha256", OTHER_SHA, "is not the payload this record's approval authorized"),
    *[
        ("intent_event_id", v, "does not name this record's durable submission intent")
        for v in ["tev-nobody", None]
    ],
    ("artifact_path", "submissions/live/500/k.json", "recorded together or not at all"),
    ("artifact_sha256", OTHER_SHA, "recorded together or not at all"),
    *[
        ("observed_by", v, "observed_by must be non-blank")
        for v in [*_BLANKS, None, "a\x00b", "x" * 201, b"chris"]
    ],
    *[
        ("note", v, "note must be non-blank")
        for v in [*_BLANKS, None, "a\x00b", "x" * 4001, b"note"]
    ],
    *[
        ("refetched_at_utc", v, "refetched_at_utc must be a UTC timestamp")
        for v in ["2026-09-15T12:00:00+00:00", "2026-09-15 12:00:00.000000+00:00", 5]
    ],
    (
        "refetched_at_utc",
        "2026-09-15T10:59:59.999999+00:00",
        "earlier than the reservation it reconciles",
    ),
    *[
        ("refetched_forecast_snapshot", v, "must record a confirming refetch")
        for v in [
            "not json",
            '{"outcome":"absent"}',
            "{}",
            "[]",
            '{"outcome":1}',
            b'{"outcome":"confirmed"}',
            "   ",
        ]
    ],
]


@pytest.mark.parametrize(("column", "value", "fragment"), _ROW_PROBES)
def test_the_schema_refuses_each_malformed_reconciliation(
    ledger: sqlite3.Connection, post: Unrecorded, column: str, value: object, fragment: str
) -> None:
    before = _counts(ledger)
    with pytest.raises(sqlite3.IntegrityError, match=re.escape(fragment)):
        insert_reconciliation(ledger, post, **{column: value})
    assert _counts(ledger) == before


@pytest.mark.parametrize(
    ("path", "digest"),
    [("   ", OTHER_SHA), ("a\x00b", OTHER_SHA), ("x" * 201, OTHER_SHA), (b"path", OTHER_SHA)],
)
def test_the_schema_refuses_a_malformed_artifact_path(
    ledger: sqlite3.Connection, post: Unrecorded, path: object, digest: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="artifact_path, when present"):
        insert_reconciliation(ledger, post, artifact_path=path, artifact_sha256=digest)


@pytest.mark.parametrize("digest", ["E" * 64, "e" * 63, b"e" * 64])
def test_the_schema_refuses_a_malformed_artifact_digest(
    ledger: sqlite3.Connection, post: Unrecorded, digest: object
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="artifact_sha256, when present"):
        insert_reconciliation(
            ledger, post, artifact_path="submissions/live/500/k.json", artifact_sha256=digest
        )


def test_the_schema_refuses_another_records_reservation(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    other = _unrecorded(ledger, "rec-other", question_id=501)
    with pytest.raises(sqlite3.IntegrityError, match="held against this forecast record"):
        insert_reconciliation(ledger, post, reservation_id=other.reservation_id)


def test_the_schema_refuses_a_released_reservation(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    ledger.execute(
        "INSERT INTO submission_key_releases (release_id, reservation_id, reason, released_by, "
        "released_at_utc, created_at_utc) VALUES ('wjrel-1', ?, 'operator_abandoned', 'chris', ?, ?)",
        (post.reservation_id, RESERVED_AT, RESERVED_AT),
    )
    with pytest.raises(sqlite3.IntegrityError, match="this reservation was released"):
        insert_reconciliation(ledger, post)


def test_the_schema_refuses_a_post_an_attempt_already_records_under_its_key(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    _insert_attempt(ledger, RECORD, attempt_id="att-recorded", key=post.idempotency_key)
    with pytest.raises(sqlite3.IntegrityError, match="already records the post made"):
        insert_reconciliation(ledger, post)


def test_the_schema_refuses_an_attempt_id_an_attempt_row_already_holds(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    # A different key, so only the attempt-id arm of the clause can be what refuses.
    _insert_attempt(ledger, RECORD, attempt_id=post.attempt_id, key="wjsub-1-elsewhere")
    with pytest.raises(sqlite3.IntegrityError, match="already records the post made"):
        insert_reconciliation(ledger, post)


def test_the_schema_refuses_a_record_that_is_not_awaiting_submission(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    # Submitted through an attempt under a different key and attempt id: neither arm of the
    # attempt clause sees it, so the status clause is the one that has to refuse.
    _insert_attempt(ledger, RECORD, attempt_id="att-other", key="wjsub-1-other")
    _event(
        ledger, RECORD, 3, "submitted", "approved", "submitted", submission_attempt_id="att-other"
    )
    with pytest.raises(sqlite3.IntegrityError, match="not awaiting submission"):
        insert_reconciliation(ledger, post)


def test_the_schema_refuses_a_record_with_an_unresolved_uncertainty(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    _insert_attempt(
        ledger, RECORD, attempt_id="att-uncertain", key="wjsub-1-other", refetch="unreadable"
    )
    _event(
        ledger,
        RECORD,
        3,
        "submission_uncertain",
        "approved",
        "approved",
        submission_attempt_id="att-uncertain",
        detail_code="timeout",
    )
    with pytest.raises(sqlite3.IntegrityError, match="whose outcome is unresolved"):
        insert_reconciliation(ledger, post)


@pytest.mark.parametrize("variant", ["wrong_kind", "wrong_scope"])
def test_the_schema_refuses_a_journal_row_that_is_not_this_records_intent(
    ledger: sqlite3.Connection, post: Unrecorded, variant: str
) -> None:
    kind, scope = ("heartbeat", RECORD) if variant == "wrong_kind" else ("forecast_intent", "rec-x")
    ledger.execute(
        "INSERT INTO tournament_events (event_id, kind, scope, data, created_at_utc) "
        "VALUES ('tev-decoy', ?, ?, '{}', ?)",
        (kind, scope, TS),
    )
    with pytest.raises(sqlite3.IntegrityError, match="durable submission intent"):
        insert_reconciliation(ledger, post, intent_event_id="tev-decoy")


def test_a_reservation_is_reconciled_at_most_once(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    insert_reconciliation(ledger, post)
    # The status is still `approved` (no event yet), so every probe passes and only the UNIQUE
    # constraint stands between the reservation and a second reconciliation.
    with pytest.raises(sqlite3.IntegrityError):
        insert_reconciliation(ledger, post, reconciliation_id="wjrec-second")


# --------------------------------------------------------------------------------------
# A reconciled key is spent: the two new triggers.
# --------------------------------------------------------------------------------------


def test_a_reconciled_reservation_cannot_be_released(ledger: sqlite3.Connection) -> None:
    post = seed_reconciled_post(
        ledger,
        RECORD,
        question_id=500,
        run_id="run-1",
        forecast_sha256=SHA,
        payload_sha256=PAYLOAD_SHA,
    )
    with pytest.raises(
        sqlite3.IntegrityError, match="reconciled as a post that reached the platform"
    ):
        ledger.execute(
            "INSERT INTO submission_key_releases (release_id, reservation_id, reason, released_by, "
            "released_at_utc, created_at_utc) VALUES ('wjrel-1', ?, 'operator_abandoned', 'chris', ?, ?)",
            (post.reservation_id, RECONCILED_AT, RECONCILED_AT),
        )


def test_no_attempt_row_can_be_written_under_a_reconciled_key(ledger: sqlite3.Connection) -> None:
    post = seed_reconciled_post(
        ledger,
        RECORD,
        question_id=500,
        run_id="run-1",
        forecast_sha256=SHA,
        payload_sha256=PAYLOAD_SHA,
    )
    with pytest.raises(sqlite3.IntegrityError, match="a reconciliation already records the post"):
        _insert_attempt(ledger, RECORD, attempt_id="att-second-record", key=post.idempotency_key)


def test_no_attempt_row_can_carry_a_reconciled_attempt_id(ledger: sqlite3.Connection) -> None:
    post = seed_reconciled_post(
        ledger,
        RECORD,
        question_id=500,
        run_id="run-1",
        forecast_sha256=SHA,
        payload_sha256=PAYLOAD_SHA,
    )
    with pytest.raises(sqlite3.IntegrityError, match="a reconciliation already records the post"):
        _insert_attempt(ledger, RECORD, attempt_id=post.attempt_id, key="wjsub-1-elsewhere")


def test_an_ordinary_attempt_is_untouched_by_the_new_attempt_trigger(
    ledger: sqlite3.Connection,
) -> None:
    """The trigger runs on every live attempt write; for a key nobody reconciled it is a no-op."""
    seed_reconciled_post(
        ledger,
        RECORD,
        question_id=500,
        run_id="run-1",
        forecast_sha256=SHA,
        payload_sha256=PAYLOAD_SHA,
    )
    other = _unrecorded(ledger, "rec-ordinary", question_id=501)
    _insert_attempt(ledger, "rec-ordinary", attempt_id="att-ordinary", key=other.idempotency_key)
    assert attempt_for_key(ledger, other.idempotency_key) is not None


# --------------------------------------------------------------------------------------
# The lifecycle link: the three hunks.
# --------------------------------------------------------------------------------------


def test_a_confirmation_with_no_link_is_still_refused(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    """Hunk 1 stands the verification clause aside only when a reconciliation is linked."""
    with pytest.raises(
        sqlite3.IntegrityError, match="must link exactly one submission_verifications row"
    ):
        _event(ledger, RECORD, 3, "submission_confirmed", "approved", "submitted")


@pytest.mark.parametrize(
    "extra_link",
    [
        "approval_event_id",
        "submission_attempt_id",
        "submission_verification_id",
        "resolution_event_id",
        "score_event_id",
    ],
)
def test_a_reconciled_confirmation_carries_no_other_link(
    ledger: sqlite3.Connection, post: Unrecorded, extra_link: str
) -> None:
    reconciliation_id = insert_reconciliation(ledger, post)
    value: object = "att-anything" if extra_link == "submission_attempt_id" else 1
    with pytest.raises(sqlite3.IntegrityError, match="links only a submission_confirmed event"):
        insert_reconciled_event(ledger, RECORD, reconciliation_id, **{extra_link: value})


def test_a_reconciliation_cannot_back_a_disconfirmation(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    reconciliation_id = insert_reconciliation(ledger, post)
    with pytest.raises(sqlite3.IntegrityError, match="links only a submission_confirmed event"):
        insert_reconciled_event(
            ledger,
            RECORD,
            reconciliation_id,
            event_type="submission_disconfirmed",
            to_status="failed",
            detail_code="refetch_missing",
        )


def test_a_reconciliation_cannot_back_an_event_of_any_other_type(
    ledger: sqlite3.Connection,
) -> None:
    """Every other type's own link clause is 009's and does not read the new column."""
    post = seed_reconciled_post(
        ledger,
        RECORD,
        question_id=500,
        run_id="run-1",
        forecast_sha256=SHA,
        payload_sha256=PAYLOAD_SHA,
    )
    reconciliation_id = f"wjrec-{post.record_id}"
    seed_record(ledger, "rec-draft", question_id=502, post_id=1502)
    with pytest.raises(sqlite3.IntegrityError, match="links only a submission_confirmed event"):
        _event(
            ledger,
            "rec-draft",
            1,
            "validated",
            "draft",
            "validated",
            submission_reconciliation_id=reconciliation_id,
        )


def test_a_record_cannot_cite_another_records_reconciliation(ledger: sqlite3.Connection) -> None:
    theirs = _unrecorded(ledger, "rec-theirs", question_id=501)
    theirs_id = insert_reconciliation(ledger, theirs)
    _unrecorded(ledger)
    with pytest.raises(
        sqlite3.IntegrityError,
        match="submission_reconciliations row is for another forecast record",
    ):
        insert_reconciled_event(ledger, RECORD, theirs_id)


# --------------------------------------------------------------------------------------
# The writer.
# --------------------------------------------------------------------------------------


def test_the_writer_records_the_row_and_its_event_together(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    event = record_submission_reconciliation(
        ledger, record_id=RECORD, reconciliation=_reconciliation(post), occurred_at=WHEN
    )
    assert (event.event_type, event.from_status, event.to_status) == (
        "submission_confirmed",
        "approved",
        "submitted",
    )
    assert event.submission_reconciliation_id is not None
    assert event.submission_reconciliation_id.startswith("wjrec-")
    row = ledger.execute(
        "SELECT reservation_id, forecast_record_id, attempt_id, observed_by, note, "
        "refetched_at_utc FROM submission_reconciliations WHERE reconciliation_id = ?",
        (event.submission_reconciliation_id,),
    ).fetchone()
    assert tuple(row) == (
        post.reservation_id,
        RECORD,
        post.attempt_id,
        "chris",
        "saw 35% on the question page",
        "2026-09-15T12:00:00.000000+00:00",
    )
    assert read_history(ledger, RECORD)[-1] == event


def test_the_writer_accepts_the_attempt_id_the_live_minter_produces(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    """``lifecycle`` spells the live tag as a literal; the produced id is what keeps it honest."""
    assert post.attempt_id == live_attempt_id(post.idempotency_key)
    record_submission_reconciliation(
        ledger, record_id=RECORD, reconciliation=_reconciliation(post), occurred_at=WHEN
    )


def test_a_reconciled_key_reads_as_spent_everywhere(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    record_submission_reconciliation(
        ledger, record_id=RECORD, reconciliation=_reconciliation(post), occurred_at=WHEN
    )
    key = post.idempotency_key
    assert key_is_reconciled(ledger, key)
    assert live_reservation_for_key(ledger, key) is None
    assert live_reservations_for_record(ledger, RECORD) == ()
    with pytest.raises(SubmissionError, match="spent by a post recorded through reconciliation"):
        require_key_unused(ledger, key)
    with pytest.raises(SubmissionError, match="spent by a post recorded through reconciliation"):
        reserve_submission_key(ledger, record_id=RECORD, idempotency_key=key, reserved_at=WHEN)
    reservation = ledger.execute(
        "SELECT reservation_id, idempotency_key, forecast_record_id, reservation_seq, "
        "reserved_at_utc FROM submission_key_reservations WHERE reservation_id = ?",
        (post.reservation_id,),
    ).fetchone()
    from whiskeyjack_bot.submission import KeyReservation

    with pytest.raises(SubmissionError, match="reconciled as a post that reached the platform"):
        release_submission_key(
            ledger,
            KeyReservation(*reservation),
            reason="operator_abandoned",
            released_at=WHEN,
            released_by="chris",
        )


def test_a_reservation_an_attempt_spent_is_not_standing(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    """The Deviation: before M2-713 the readers listed every unreleased reservation as standing,
    so after an ordinary post ``release-key`` offered to release a spent key."""
    _insert_attempt(ledger, RECORD, attempt_id=post.attempt_id, key=post.idempotency_key)
    assert live_reservation_for_key(ledger, post.idempotency_key) is None
    assert live_reservations_for_record(ledger, RECORD) == ()


def test_a_standing_reservation_is_still_standing(
    ledger: sqlite3.Connection, post: Unrecorded
) -> None:
    """The other direction: the narrower predicate must not hide a genuinely held key."""
    held = live_reservation_for_key(ledger, post.idempotency_key)
    assert held is not None and held.reservation_id == post.reservation_id
    assert [r.reservation_id for r in live_reservations_for_record(ledger, RECORD)] == [
        post.reservation_id
    ]


_WRITER_FIELD_PROBES: list[tuple[dict[str, object], str]] = [
    ({"reservation_id": "  "}, "reconciliation.reservation_id must not be blank"),
    (
        {"attempt_id": "wjdry-1-" + "a" * 64},
        "reconciliation.attempt_id must be a live attempt identifier",
    ),
    (
        {"attempt_id": "wjlive-1-" + "a" * 63},
        "reconciliation.attempt_id must be a live attempt identifier",
    ),
    ({"request_payload_sha256": "D" * 64}, "reconciliation.request_payload_sha256 must be 64"),
    ({"intent_event_id": ""}, "reconciliation.intent_event_id must be a non-empty string"),
    ({"observed_by": " \t"}, "reconciliation.observed_by must not be blank"),
    ({"observed_by": "a\x00b"}, "reconciliation.observed_by must not contain a NUL"),
    ({"observed_by": "x" * 201}, "reconciliation.observed_by is longer than"),
    ({"note": "　"}, "reconciliation.note must not be blank"),
    ({"note": "a\x00b"}, "reconciliation.note must not contain a NUL"),
    ({"note": "x" * 4001}, "reconciliation.note is longer than"),
    (
        {"refetched_at_utc": datetime(2026, 9, 15, 12, 0)},
        "reconciliation.refetched_at_utc must be timezone-aware",
    ),
    ({"refetched_forecast_snapshot": '{"outcome":"absent"}'}, "must record a confirming refetch"),
    ({"refetched_forecast_snapshot": "not json"}, "must record a confirming refetch"),
    ({"artifact_path": "submissions/live/500/k.json"}, "recorded together or not at all"),
    ({"artifact_sha256": OTHER_SHA}, "recorded together or not at all"),
    (
        {"artifact_path": " ", "artifact_sha256": OTHER_SHA},
        "reconciliation.artifact_path must not be blank",
    ),
    (
        {"artifact_path": "p", "artifact_sha256": "E" * 64},
        "reconciliation.artifact_sha256 must be 64",
    ),
]


@pytest.mark.parametrize(("changes", "fragment"), _WRITER_FIELD_PROBES)
def test_the_writer_refuses_each_malformed_field_before_the_ledger(
    ledger: sqlite3.Connection, post: Unrecorded, changes: dict[str, object], fragment: str
) -> None:
    before = _counts(ledger)
    with pytest.raises(LifecycleError, match=re.escape(fragment)):
        record_submission_reconciliation(
            ledger,
            record_id=RECORD,
            reconciliation=_reconciliation(post, **changes),
            occurred_at=WHEN,
        )
    assert _counts(ledger) == before


def test_the_writer_refuses_a_subclass(ledger: sqlite3.Connection, post: Unrecorded) -> None:
    class Hostile(SubmissionReconciliation):
        pass

    with pytest.raises(LifecycleError, match="must be a SubmissionReconciliation"):
        record_submission_reconciliation(
            ledger,
            record_id=RECORD,
            reconciliation=Hostile(**_reconciliation(post).__dict__),
            occurred_at=WHEN,
        )


def _submit_other(conn: sqlite3.Connection) -> None:
    _insert_attempt(conn, RECORD, attempt_id="att-other", key="wjsub-1-other")
    _event(conn, RECORD, 3, "submitted", "approved", "submitted", submission_attempt_id="att-other")


def _uncertain_other(conn: sqlite3.Connection) -> None:
    _insert_attempt(
        conn, RECORD, attempt_id="att-uncertain", key="wjsub-1-other", refetch="unreadable"
    )
    _event(
        conn,
        RECORD,
        3,
        "submission_uncertain",
        "approved",
        "approved",
        submission_attempt_id="att-uncertain",
        detail_code="timeout",
    )


def _release(conn: sqlite3.Connection, post: Unrecorded) -> None:
    conn.execute(
        "INSERT INTO submission_key_releases (release_id, reservation_id, reason, released_by, "
        "released_at_utc, created_at_utc) VALUES ('wjrel-1', ?, 'operator_abandoned', 'chris', ?, ?)",
        (post.reservation_id, RESERVED_AT, RESERVED_AT),
    )


# Each readable mirror of a 016 clause, by the message only it produces. With the Python check
# removed the trigger still refuses, but as "the ledger rejected this write".
_WRITER_STATE_PROBES = [
    ("unknown_record", "record_id does not name a stored forecast record"),
    ("other_reservation", "does not name a key reservation held against this forecast record"),
    ("released", "this reservation was released"),
    ("already_reconciled", "this post has already been reconciled"),
    ("attempt_under_key", "a submission attempt already records the post"),
    ("attempt_id_taken", "a submission attempt already records the post"),
    ("unapproved_digest", "is not the payload this record's approval authorized"),
    ("submitted", "this forecast record is submitted, not awaiting submission"),
    ("uncertain", "whose outcome is unresolved"),
    ("no_intent", "does not name this record's durable submission intent"),
    ("refetched_too_early", "earlier than the reservation it reconciles"),
]


@pytest.mark.parametrize(("state", "fragment"), _WRITER_STATE_PROBES)
def test_the_writer_explains_each_state_that_cannot_be_reconciled(
    ledger: sqlite3.Connection, post: Unrecorded, state: str, fragment: str
) -> None:
    record_id = RECORD
    changes: dict[str, object] = {}
    if state == "unknown_record":
        record_id = "rec-nobody"
    elif state == "other_reservation":
        changes["reservation_id"] = _unrecorded(ledger, "rec-other", question_id=501).reservation_id
    elif state == "released":
        _release(ledger, post)
    elif state == "already_reconciled":
        insert_reconciliation(ledger, post)
    elif state == "attempt_under_key":
        _insert_attempt(ledger, RECORD, attempt_id="att-recorded", key=post.idempotency_key)
    elif state == "attempt_id_taken":
        _insert_attempt(ledger, RECORD, attempt_id=post.attempt_id, key="wjsub-1-elsewhere")
    elif state == "unapproved_digest":
        changes["request_payload_sha256"] = OTHER_SHA
    elif state == "submitted":
        _submit_other(ledger)
    elif state == "uncertain":
        _uncertain_other(ledger)
    elif state == "no_intent":
        changes["intent_event_id"] = "tev-nobody"
    elif state == "refetched_too_early":
        changes["refetched_at_utc"] = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
    before = _counts(ledger)
    with pytest.raises(LifecycleError, match=re.escape(fragment)):
        record_submission_reconciliation(
            ledger,
            record_id=record_id,
            reconciliation=_reconciliation(post, **changes),
            occurred_at=WHEN,
        )
    assert _counts(ledger) == before


def test_a_failed_event_write_takes_the_reconciliation_row_with_it(
    ledger: sqlite3.Connection, post: Unrecorded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomicity: the row and its event land together or not at all."""
    from whiskeyjack_bot import lifecycle

    def refuse(*args: object, **kwargs: object) -> object:
        raise LifecycleError("simulated event refusal")

    monkeypatch.setattr(lifecycle, "_append_event", refuse)
    before = _counts(ledger)
    with pytest.raises(LifecycleError, match="simulated event refusal"):
        record_submission_reconciliation(
            ledger, record_id=RECORD, reconciliation=_reconciliation(post), occurred_at=WHEN
        )
    assert _counts(ledger) == before
    assert not ledger.in_transaction


# --------------------------------------------------------------------------------------
# The upgrade.
# --------------------------------------------------------------------------------------


def test_a_v15_ledger_upgrades_and_its_events_carry_no_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ledger a previous build produced, holding a submitted record, reaches 16 unchanged.

    Seeded raw at 15 -- this build's writers name 016's column -- and then read and written
    through this build.
    """
    db = tmp_path / "ledger.sqlite3"
    packaged = ledger_module._load_migrations
    with monkeypatch.context() as patch:
        patch.setattr(
            ledger_module, "_load_migrations", lambda: [m for m in packaged() if m[0] <= 15]
        )
        assert initialize_ledger(db) == 15
    conn = connect(db)
    try:
        seed_record(conn, "rec-old", question_id=700, post_id=1700)
        walk_to_submitted_raw(conn, "rec-old")
    finally:
        conn.close()
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION == 16
    conn = connect(db)
    try:
        history = read_history(conn, "rec-old")
        assert [e.event_type for e in history] == ["validated", "approved", "submitted"]
        assert all(e.submission_reconciliation_id is None for e in history)
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, question_id, started_at_utc, "
            "created_at_utc) VALUES ('run-1', 'asknews', 500, ?, ?)",
            (TS, TS),
        )
        post = _unrecorded(conn)
        record_submission_reconciliation(
            conn, record_id=RECORD, reconciliation=_reconciliation(post), occurred_at=WHEN
        )
        assert current_status(conn, RECORD) == "submitted"
    finally:
        conn.close()


def test_the_confirming_snapshot_fixture_is_a_confirmation() -> None:
    """Guards the helper: a fixture that stopped saying `confirmed` would make every probe
    above refuse on the snapshot clause and read as passing."""
    assert json.loads(CONFIRMING_SNAPSHOT)["outcome"] == "confirmed"
