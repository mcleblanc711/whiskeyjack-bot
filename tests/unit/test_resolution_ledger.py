"""M4-801 acceptance: a resolution reaches the ledger only as an appended event, and an
annulled or ambiguous outcome is never scored.

Both halves are tested twice, the way M1-603 tests its own criterion. The schema half uses
raw SQL and bypasses the writer entirely: `014_resolution_ingestion.sql`'s triggers are the
witness outside the program, so a writer bug cannot also be what makes these pass. The writer
half drives `lifecycle.record_resolution_observation` and checks what it leaves behind.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from resolution_rows import (
    OBSERVED_AT,
    insert_resolution_row,
    kind_payload,
    post_payload,
    resolution_columns,
    seed_record,
    seed_submitted,
)
from whiskeyjack_bot import ledger as ledger_module
from whiskeyjack_bot import lifecycle
from whiskeyjack_bot.ledger import LedgerError, connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    current_status,
    latest_resolution,
    read_history,
    read_resolution_history,
    record_resolution_observation,
)
from whiskeyjack_bot.resolution import classify_resolution

QUESTION_ID = 45747
POST_ID = 45556
RECORD = "rec-resolve"
T0 = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    connection = connect(db)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def submitted(conn: sqlite3.Connection) -> str:
    return seed_submitted(conn, RECORD, question_id=QUESTION_ID, post_id=POST_ID)


def _payload(kind: str, question_type: str = "binary") -> dict[str, Any]:
    return kind_payload(question_type, kind, post_id=POST_ID, question_id=QUESTION_ID)


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _score(conn: sqlite3.Connection, record_id: str = RECORD) -> None:
    conn.execute(
        "INSERT INTO score_events (forecast_record_id, metric, value, implementation_version, "
        "computed_at_utc) VALUES (?, 'brier', 0.25, 'v1', ?)",
        (record_id, OBSERVED_AT),
    )


def _at(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat(timespec="microseconds")


# ── the schema: only by append ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "column", ["outcome", "resolution_kind", "scorable", "annulled", "resolution_snapshot_json"]
)
def test_a_stored_resolution_can_be_neither_updated_nor_deleted(
    conn: sqlite3.Connection, submitted: str, column: str
) -> None:
    insert_resolution_row(conn, submitted, kind="annulled")
    before = conn.execute("SELECT * FROM resolution_events").fetchall()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"UPDATE resolution_events SET {column} = ?", ("resolved",))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM resolution_events")
    assert conn.execute("SELECT * FROM resolution_events").fetchall() == before


def test_replace_with_a_new_observation_cannot_overwrite_a_stored_one(
    conn: sqlite3.Connection, submitted: str
) -> None:
    """The REPLACE-as-DELETE route, with a row every insert rule accepts.

    A REPLACE of the *same* row would be refused by 014's idempotency clause before the
    conflict is ever resolved, which tests the wrong guard. This one is a different, valid,
    later observation aimed at the stored event_id, so the only thing that can refuse it is
    the append-only delete trigger firing on REPLACE's implicit delete.
    """
    event_id = insert_resolution_row(conn, submitted, kind="annulled", observed_at_utc=_at(0))
    before = conn.execute("SELECT * FROM resolution_events").fetchall()
    columns = resolution_columns(conn, submitted, kind="resolved", observed_at_utc=_at(5))
    columns["event_id"] = event_id
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            f"INSERT OR REPLACE INTO resolution_events ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(columns.values()),
        )
    assert conn.execute("SELECT * FROM resolution_events").fetchall() == before


REFUSED_ROWS: dict[str, tuple[dict[str, object], str]] = {
    "no record": ({"forecast_record_id": None}, "must name a stored forecast record"),
    "unknown record": ({"forecast_record_id": "nope"}, "must name a stored forecast record"),
    "other question": ({"question_id": 1}, "must match the forecast record"),
    "other post": ({"post_id": 1}, "must match the forecast record"),
    # Text an INTEGER column's affinity cannot convert: "45747" would be stored as 45747,
    # which is an integer and correctly accepted.
    "text question id": ({"question_id": f"q{QUESTION_ID}"}, "must match the forecast record"),
    "other type": ({"question_type": "numeric"}, "must match the forecast record"),
    "unknown kind": ({"resolution_kind": "cancelled"}, "not a recognized kind"),
    "null kind": ({"resolution_kind": None}, "not a recognized kind"),
    "scorable annulled": ({"scorable": 1}, "must follow from resolution_kind"),
    "text scorable": ({"scorable": "no"}, "must follow from resolution_kind"),
    "unflagged annulled": ({"annulled": 0}, "must follow from resolution_kind"),
    "ambiguous flag": ({"ambiguous": 1}, "must follow from resolution_kind"),
    "outcome on annulled": ({"outcome": "yes"}, "outcome is required"),
    "snapshot not json": ({"resolution_snapshot_json": "{"}, "must be JSON objects"),
    "snapshot array": ({"resolution_snapshot_json": "[]"}, "must be JSON objects"),
    "source not json": ({"source_response": "nope"}, "must be JSON objects"),
    "source blank": ({"source_response": ""}, "must be JSON objects"),
    "upper-case digest": ({"observation_sha256": "A" * 64}, "64 lowercase hex"),
    "short digest": ({"source_response_sha256": "a" * 63}, "64 lowercase hex"),
    "loose timestamp": ({"observed_at_utc": "2026-09-17T18:00:00Z"}, "canonical UTC"),
    "loose ingest time": ({"ingested_at_utc": "2026-09-17 18:00:00"}, "canonical UTC"),
}


@pytest.mark.parametrize("name", sorted(REFUSED_ROWS))
def test_the_schema_refuses_a_row_no_writer_would_write(
    conn: sqlite3.Connection, submitted: str, name: str
) -> None:
    overrides, message = REFUSED_ROWS[name]
    with pytest.raises(sqlite3.IntegrityError, match=message):
        insert_resolution_row(conn, submitted, kind="annulled", **overrides)
    assert _count(conn, "resolution_events") == 0


def test_the_columns_must_agree_with_the_snapshot_they_index(
    conn: sqlite3.Connection, submitted: str
) -> None:
    """A row whose scorable column says one thing while its hashed snapshot says another."""
    resolved = resolution_columns(conn, submitted, kind="resolved")
    annulled = resolution_columns(conn, submitted, kind="annulled")
    forged = {**annulled, "resolution_kind": "resolved", "scorable": 1, "annulled": 0}
    forged["outcome"] = resolved["outcome"]
    with pytest.raises(sqlite3.IntegrityError, match="agree with resolution_snapshot_json"):
        conn.execute(
            f"INSERT INTO resolution_events ({', '.join(forged)}) "
            f"VALUES ({', '.join('?' for _ in forged)})",
            tuple(forged.values()),
        )


def test_a_repeated_observation_is_refused_but_a_return_to_an_earlier_one_is_not(
    conn: sqlite3.Connection, submitted: str
) -> None:
    insert_resolution_row(conn, submitted, kind="resolved", observed_at_utc=_at(0))
    with pytest.raises(sqlite3.IntegrityError, match="already the latest"):
        insert_resolution_row(conn, submitted, kind="resolved", observed_at_utc=_at(1))
    insert_resolution_row(conn, submitted, kind="annulled", observed_at_utc=_at(2))
    insert_resolution_row(conn, submitted, kind="resolved", observed_at_utc=_at(3))
    kinds = [row[0] for row in conn.execute("SELECT resolution_kind FROM resolution_events")]
    assert kinds == ["resolved", "annulled", "resolved"]


def test_a_retraction_needs_something_to_retract(conn: sqlite3.Connection, submitted: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="must follow an earlier observation"):
        insert_resolution_row(conn, submitted, kind="unresolved")
    insert_resolution_row(conn, submitted, kind="resolved", observed_at_utc=_at(0))
    insert_resolution_row(conn, submitted, kind="unresolved", observed_at_utc=_at(1))


def test_an_observation_older_than_the_latest_is_refused(
    conn: sqlite3.Connection, submitted: str
) -> None:
    insert_resolution_row(conn, submitted, kind="resolved", observed_at_utc=_at(10))
    with pytest.raises(sqlite3.IntegrityError, match="earlier than the latest observation"):
        insert_resolution_row(conn, submitted, kind="annulled", observed_at_utc=_at(9))


@pytest.mark.parametrize("explicit_id", [-1, 0, 1])
def test_an_explicit_event_id_cannot_make_a_row_older_than_its_predecessors(
    conn: sqlite3.Connection, submitted: str, explicit_id: int
) -> None:
    conn.execute(
        "INSERT INTO research_runs (retrieval_run_id, provider, question_id, "
        "started_at_utc, created_at_utc) VALUES ('r2', 'asknews', 1, ?, ?)",
        (OBSERVED_AT, OBSERVED_AT),
    )
    for index in range(3):
        insert_resolution_row(
            conn,
            submitted,
            kind=("resolved", "annulled", "resolved")[index],
            observed_at_utc=_at(index),
        )
    if explicit_id == 1:
        with pytest.raises(sqlite3.IntegrityError):  # the primary key refuses a reuse first
            insert_resolution_row(
                conn, submitted, kind="ambiguous", observed_at_utc=_at(5), event_id=explicit_id
            )
    else:
        with pytest.raises(sqlite3.IntegrityError, match="greater than every existing"):
            insert_resolution_row(
                conn, submitted, kind="ambiguous", observed_at_utc=_at(5), event_id=explicit_id
            )
    assert _count(conn, "resolution_events") == 3


# ── the schema: annulled and ambiguous are not scored ────────────────────────


def test_a_score_needs_a_resolution(conn: sqlite3.Connection, submitted: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="not scorable"):
        _score(conn)


@pytest.mark.parametrize("kind", ["annulled", "ambiguous", "withheld"])
def test_a_score_is_refused_when_the_latest_resolution_is_not_scorable(
    conn: sqlite3.Connection, submitted: str, kind: str
) -> None:
    insert_resolution_row(conn, submitted, kind=kind)
    with pytest.raises(sqlite3.IntegrityError, match="not scorable"):
        _score(conn)
    assert _count(conn, "score_events") == 0


def test_a_score_follows_the_latest_resolution_not_the_first(
    conn: sqlite3.Connection, submitted: str
) -> None:
    insert_resolution_row(conn, submitted, kind="resolved", observed_at_utc=_at(0))
    _score(conn)
    insert_resolution_row(conn, submitted, kind="unresolved", observed_at_utc=_at(1))
    with pytest.raises(sqlite3.IntegrityError, match="not scorable"):
        _score(conn)
    insert_resolution_row(conn, submitted, kind="annulled", observed_at_utc=_at(2))
    with pytest.raises(sqlite3.IntegrityError, match="not scorable"):
        _score(conn)
    insert_resolution_row(conn, submitted, kind="resolved", observed_at_utc=_at(3))
    _score(conn)
    assert _count(conn, "score_events") == 2


def test_another_records_scorable_resolution_does_not_make_this_one_scorable(
    conn: sqlite3.Connection, submitted: str
) -> None:
    other = seed_submitted(conn, "rec-other", question_id=QUESTION_ID + 1, post_id=POST_ID + 1)
    insert_resolution_row(conn, other, kind="resolved")
    insert_resolution_row(conn, submitted, kind="annulled")
    _score(conn, other)
    with pytest.raises(sqlite3.IntegrityError, match="not scorable"):
        _score(conn, submitted)


# ── the migration ────────────────────────────────────────────────────────────


def _ledger_at_013(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packaged = ledger_module._load_migrations
    with monkeypatch.context() as patch:
        patch.setattr(
            ledger_module, "_load_migrations", lambda: [m for m in packaged() if m[0] <= 13]
        )
        assert initialize_ledger(db) == 13


def test_a_ledger_at_013_upgrades_to_014(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "ledger.sqlite3"
    _ledger_at_013(db, monkeypatch)
    connection = connect(db)
    try:
        seed_submitted(connection, RECORD, question_id=QUESTION_ID, post_id=POST_ID)
    finally:
        connection.close()
    assert initialize_ledger(db) == 14
    connection = connect(db)
    try:
        write = record_resolution_observation(
            connection, record_id=RECORD, source_response=_payload("resolved"), observed_at=T0
        )
        assert write.outcome == "appended"
        assert current_status(connection, RECORD) == "resolved"
        assert not connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE name LIKE 'migration_014%'"
        ).fetchall()
    finally:
        connection.close()


@pytest.mark.parametrize("table", ["resolution_events", "score_events"])
def test_the_migration_refuses_a_ledger_already_holding_unclassified_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: str
) -> None:
    db = tmp_path / "ledger.sqlite3"
    _ledger_at_013(db, monkeypatch)
    connection = connect(db)
    try:
        seed_submitted(connection, RECORD, question_id=QUESTION_ID, post_id=POST_ID)
        if table == "resolution_events":
            connection.execute(
                "INSERT INTO resolution_events (question_id, forecast_record_id, ingested_at_utc) "
                "VALUES (?, ?, ?)",
                (QUESTION_ID, RECORD, OBSERVED_AT),
            )
        else:
            _score(connection)
    finally:
        connection.close()
    with pytest.raises(LedgerError, match="failed to apply ledger migration 14"):
        initialize_ledger(db)
    connection = connect(db)
    try:
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 13
        columns = {row[1] for row in connection.execute("PRAGMA table_info(resolution_events)")}
        assert "resolution_kind" not in columns, "a refused upgrade must leave no column behind"
    finally:
        connection.close()


# ── the writer ───────────────────────────────────────────────────────────────


def test_a_resolved_observation_moves_a_submitted_record_and_is_scorable(
    conn: sqlite3.Connection, submitted: str
) -> None:
    payload = _payload("resolved")
    write = record_resolution_observation(
        conn, record_id=submitted, source_response=payload, observed_at=T0
    )
    assert write.outcome == "appended" and write.stored is not None and write.event is not None
    assert write.event.event_type == "resolved"
    assert write.event.resolution_event_id == write.stored.event_id
    assert current_status(conn, submitted) == "resolved"
    latest = latest_resolution(conn, submitted)
    assert latest == write.stored
    assert latest is not None and latest.scorable and latest.kind == "resolved"
    assert latest.observation == classify_resolution(
        payload, question_id=QUESTION_ID, question_type="binary"
    )
    _score(conn)


@pytest.mark.parametrize("kind", ["annulled", "ambiguous"])
def test_a_cancellation_resolves_the_record_and_can_never_be_scored(
    conn: sqlite3.Connection, submitted: str, kind: str
) -> None:
    write = record_resolution_observation(
        conn, record_id=submitted, source_response=_payload(kind), observed_at=T0
    )
    assert write.event is not None and current_status(conn, submitted) == "resolved"
    latest = latest_resolution(conn, submitted)
    assert latest is not None and latest.kind == kind and not latest.scorable
    with pytest.raises(sqlite3.IntegrityError, match="not scorable"):
        _score(conn)


def test_a_withheld_value_is_recorded_but_moves_nothing(
    conn: sqlite3.Connection, submitted: str
) -> None:
    write = record_resolution_observation(
        conn, record_id=submitted, source_response=_payload("withheld"), observed_at=T0
    )
    assert (write.outcome, write.event) == ("appended", None)
    assert current_status(conn, submitted) == "submitted"
    later = record_resolution_observation(
        conn,
        record_id=submitted,
        source_response=_payload("resolved"),
        observed_at=T0 + timedelta(hours=1),
    )
    assert later.event is not None and current_status(conn, submitted) == "resolved"


def test_a_repeated_poll_writes_nothing(conn: sqlite3.Connection, submitted: str) -> None:
    for minutes in (0, 5, 10):
        write = record_resolution_observation(
            conn,
            record_id=submitted,
            source_response=_payload("resolved"),
            observed_at=T0 + timedelta(minutes=minutes),
        )
        assert write.outcome == ("appended" if minutes == 0 else "unchanged")
    assert _count(conn, "resolution_events") == 1
    assert [event.event_type for event in read_history(conn, submitted)][-1] == "resolved"
    assert sum(e.event_type == "resolved" for e in read_history(conn, submitted)) == 1


def test_a_poll_before_resolution_writes_nothing(conn: sqlite3.Connection, submitted: str) -> None:
    write = record_resolution_observation(
        conn, record_id=submitted, source_response=_payload("unresolved"), observed_at=T0
    )
    assert (write.outcome, write.stored, write.event) == ("nothing_to_retract", None, None)
    assert _count(conn, "resolution_events") == 0


def test_a_retraction_and_re_resolution_are_both_kept(
    conn: sqlite3.Connection, submitted: str
) -> None:
    sequence = [
        ("resolved", "yes"),
        ("unresolved", None),
        ("resolved", "no"),
        ("resolved", "yes"),
    ]
    for step, (kind, value) in enumerate(sequence):
        payload = (
            _payload("unresolved")
            if kind == "unresolved"
            else post_payload("binary", post_id=POST_ID, question_id=QUESTION_ID, resolution=value)
        )
        write = record_resolution_observation(
            conn,
            record_id=submitted,
            source_response=payload,
            observed_at=T0 + timedelta(minutes=step),
        )
        assert write.outcome == "appended"
        assert (write.event is not None) is (step == 0)
    history = read_resolution_history(conn, submitted)
    assert [(r.kind, r.observation.outcome) for r in history] == sequence
    assert current_status(conn, submitted) == "resolved"
    latest = latest_resolution(conn, submitted)
    assert latest is not None and latest.observation.outcome == "yes"


@pytest.mark.parametrize("status", ["draft", "validated", "approved"])
def test_a_record_that_was_never_posted_cannot_be_resolved(
    conn: sqlite3.Connection, status: str
) -> None:
    seed_record(conn, RECORD, question_id=QUESTION_ID, post_id=POST_ID)
    if status != "draft":
        lifecycle.record_validation(conn, record_id=RECORD, occurred_at=T0 - timedelta(days=3))
    if status == "approved":
        lifecycle.record_approval(
            conn,
            record_id=RECORD,
            decision="approved",
            actor="policy:test",
            forecast_sha256="b" * 64,
            payload_sha256="d" * 64,
            occurred_at=T0 - timedelta(days=2),
        )
    with pytest.raises(LifecycleError, match=f"current status is {status}"):
        record_resolution_observation(
            conn, record_id=RECORD, source_response=_payload("resolved"), observed_at=T0
        )
    assert _count(conn, "resolution_events") == 0


def test_a_record_without_a_post_id_is_refused(conn: sqlite3.Connection) -> None:
    seed_submitted(conn, RECORD, question_id=QUESTION_ID, post_id=None)
    with pytest.raises(LifecycleError, match="no post_id"):
        record_resolution_observation(
            conn, record_id=RECORD, source_response=_payload("resolved"), observed_at=T0
        )


def test_a_payload_for_another_post_is_refused(conn: sqlite3.Connection, submitted: str) -> None:
    payload = post_payload("binary", post_id=POST_ID + 9, question_id=QUESTION_ID)
    with pytest.raises(LifecycleError, match="different post"):
        record_resolution_observation(
            conn, record_id=submitted, source_response=payload, observed_at=T0
        )
    assert _count(conn, "resolution_events") == 0


def test_a_malformed_payload_writes_nothing_and_leaves_no_transaction(
    conn: sqlite3.Connection, submitted: str
) -> None:
    payload = _payload("resolved")
    payload["question"]["resolution"] = "maybe"
    with pytest.raises(LifecycleError, match="cannot be recorded"):
        record_resolution_observation(
            conn, record_id=submitted, source_response=payload, observed_at=T0
        )
    assert _count(conn, "resolution_events") == 0
    assert not conn.in_transaction


def test_an_out_of_order_observation_is_refused_by_the_writer(
    conn: sqlite3.Connection, submitted: str
) -> None:
    record_resolution_observation(
        conn, record_id=submitted, source_response=_payload("annulled"), observed_at=T0
    )
    with pytest.raises(LifecycleError, match="earlier than the latest"):
        record_resolution_observation(
            conn,
            record_id=submitted,
            source_response=_payload("resolved"),
            observed_at=T0 - timedelta(seconds=1),
        )


def test_an_oversized_source_response_is_refused(
    conn: sqlite3.Connection, submitted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload("resolved")
    size = len(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    monkeypatch.setattr(lifecycle, "MAX_SOURCE_RESPONSE_LENGTH", size - 1)
    with pytest.raises(LifecycleError, match="larger than the ledger stores"):
        record_resolution_observation(
            conn, record_id=submitted, source_response=payload, observed_at=T0
        )


def test_the_row_and_its_lifecycle_event_are_atomic(
    conn: sqlite3.Connection, submitted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise LifecycleError("injected")

    monkeypatch.setattr(lifecycle, "_append_event", fail)
    with pytest.raises(LifecycleError, match="injected"):
        record_resolution_observation(
            conn, record_id=submitted, source_response=_payload("resolved"), observed_at=T0
        )
    assert _count(conn, "resolution_events") == 0
    assert current_status(conn, submitted) == "submitted"


def test_a_stored_row_that_no_longer_matches_its_digest_is_refused_on_read(
    conn: sqlite3.Connection, submitted: str
) -> None:
    """A value read back out of the ledger is untrusted; the reader re-verifies it.

    Reaching the row requires removing 003's update block on this test's own ledger, which is
    the only way to produce a stored row the digest no longer covers.
    """
    record_resolution_observation(
        conn, record_id=submitted, source_response=_payload("resolved"), observed_at=T0
    )
    conn.execute("DROP TRIGGER resolution_events_block_update")
    conn.execute(
        "UPDATE resolution_events SET source_response = replace(source_response, '\"yes\"', "
        "'\"no\"')"
    )
    with pytest.raises(LifecycleError, match="does not match its digest"):
        latest_resolution(conn, submitted)


def test_the_stored_row_replays_to_its_own_digest(conn: sqlite3.Connection, submitted: str) -> None:
    write = record_resolution_observation(
        conn, record_id=submitted, source_response=_payload("resolved"), observed_at=T0
    )
    row = conn.execute(
        "SELECT resolution_snapshot_json, observation_sha256, source_response FROM "
        "resolution_events"
    ).fetchone()
    assert write.stored is not None
    assert row[1] == write.stored.observation.observation_sha256
    assert json.loads(row[2]) == _payload("resolved")


def test_a_stored_snapshot_that_no_longer_matches_its_digest_is_refused_on_read(
    conn: sqlite3.Connection, submitted: str
) -> None:
    """The snapshot half of the reader's re-verification (mutation W4 survived without it).

    Only a field that keeps the snapshot valid and consistent with its indexed columns is
    changed, so the digest comparison is the one check that can refuse it.
    """
    record_resolution_observation(
        conn, record_id=submitted, source_response=_payload("resolved"), observed_at=T0
    )
    conn.execute("DROP TRIGGER resolution_events_block_update")
    conn.execute(
        "UPDATE resolution_events SET resolution_snapshot_json = json_set("
        "resolution_snapshot_json, '$.actual_resolve_time', '2026-09-18T12:00:00.000000+00:00')"
    )
    with pytest.raises(LifecycleError, match="snapshot does not match its recorded digest"):
        latest_resolution(conn, submitted)
