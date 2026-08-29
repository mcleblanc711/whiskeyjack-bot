"""M2-702: the idempotency-key derivation, its accepted domain, and the key readers.

What is under test is the layer above `lifecycle.record_submission_attempt` and above
`001`'s `idempotency_key ... UNIQUE`: that the key is a function of the four declared
inputs and nothing else, that it survives the store->load round-trip the ledger puts every
value through, and that a spent key is refused *before* a gateway decides to post.
"""

from __future__ import annotations

import json
import re
import sqlite3
import traceback
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from whiskeyjack_bot.approval import approve, effective_approval, reject
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    SubmissionAttempt,
    current_status,
    record_submission_attempt,
    record_validation,
)
from whiskeyjack_bot.submission import (
    KEY_LENGTH,
    KEY_SCHEMA_VERSION,
    AttemptSummary,
    KeyReservation,
    SubmissionError,
    attempt_for_key,
    canonical_key_json,
    live_reservation_for_key,
    live_reservations_for_record,
    release_submission_key,
    require_key_unused,
    reserve_submission_key,
    submission_key,
    submission_key_for_approved_record,
    submission_key_for_record,
)

TS = "2026-08-20T00:00:00.000000+00:00"
OCCURRED = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
SHA = "b" * 64
PAYLOAD_SHA = "d" * 64
OTHER_PAYLOAD_SHA = "e" * 64

KEY_RE = re.compile(r"^wjsub-1-[0-9a-f]{64}\Z")

# The four declared inputs, as one reusable baseline. Every "changing X changes the key"
# test varies exactly one of them.
BASE: dict[str, Any] = {
    "tournament_id": "minibench",
    "question_id": 100,
    "forecast_version": 1,
    "request_payload_sha256": PAYLOAD_SHA,
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
    tournament_id: str = "minibench",
    forecast_version: int = 1,
    parent_record_id: str | None = None,
) -> str:
    """Insert a draft directly, past `forecast.store`, to exercise the reader alone.

    The same shape `tests/unit/test_approval.py` uses, including the per-record
    `attempt_id` migration 004 indexes UNIQUE.

    `parent_record_id` is required for any version above 1: migration 007 (M1-602) makes
    version 1 the root of a chain and every later version name the record it supersedes,
    so a fixture that seeded a bare v2 would be seeding a chain with a hole in it.
    """
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, parent_record_id, "
        "question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
        "forecast_sha256, attempt_id) "
        "VALUES (?, ?, ?, ?, ?, 'binary', 'draft', 'anthropic', 'claude', 'v1', 'abc', "
        "'run-1', ?, '{}', '{}', ?, ?, ?)",
        (
            record_id,
            question_id,
            tournament_id,
            forecast_version,
            parent_record_id,
            TS,
            TS,
            SHA,
            f"att-{record_id}",
        ),
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
def approved(ledger: sqlite3.Connection) -> tuple[sqlite3.Connection, str]:
    """A record at `approved` -- the only state a submission attempt is legal from."""
    record_id = _seed_draft(ledger)
    record_validation(ledger, record_id=record_id, occurred_at=OCCURRED)
    approve(ledger, record_id=record_id, actor="chris", occurred_at=OCCURRED)
    return ledger, record_id


def _uncertain_attempt(key: str, *, attempt_id: str) -> SubmissionAttempt:
    """An attempt whose outcome is uncertain, so the record stays at `approved`.

    Deliberately not a verified success: `submitted` is terminal for the purposes of a
    second attempt, and a test that could not tell "refused by the UNIQUE key" from
    "refused by the state machine" would prove neither.
    """
    return SubmissionAttempt(
        attempt_id=attempt_id,
        idempotency_key=key,
        requested_at_utc=OCCURRED,
        completed_at_utc=OCCURRED,
        request_payload_sha256=PAYLOAD_SHA,
        success=True,
        refetch_outcome="absent",
    )


# --- the derivation ------------------------------------------------------------------


def test_the_same_four_inputs_give_the_same_key() -> None:
    assert submission_key(**BASE) == submission_key(**BASE)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("tournament_id", "minibench-2"),
        ("question_id", 101),
        ("forecast_version", 2),
        ("request_payload_sha256", OTHER_PAYLOAD_SHA),
    ],
)
def test_changing_any_declared_input_changes_the_key(field: str, changed: object) -> None:
    """Four assertions, not one: an input that is accepted but never reaches the digest
    would still pass a test that only varied one field."""
    assert submission_key(**{**BASE, field: changed}) != submission_key(**BASE)


def test_a_changed_payload_is_a_different_key() -> None:
    """The acceptance criterion's second half, stated at the derivation."""
    same_forecast = {**BASE, "request_payload_sha256": OTHER_PAYLOAD_SHA}
    assert submission_key(**same_forecast) != submission_key(**BASE)


def test_the_key_has_the_declared_format() -> None:
    key = submission_key(**BASE)
    assert KEY_RE.match(key)
    assert len(key) == KEY_LENGTH == 72


def test_the_visible_prefix_and_the_hashed_version_cannot_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The import-time guard, exercised rather than assumed.

    Called directly with a mismatched version: the module already imported cleanly, so
    the only way to see the check work is to make it fail.
    """
    from whiskeyjack_bot import submission

    monkeypatch.setattr(submission, "KEY_SCHEMA_VERSION", "2.0.0")
    with pytest.raises(SubmissionError) as excinfo:
        submission._assert_prefix_matches_version()
    assert "KEY_SCHEMA_VERSION" in str(excinfo.value)


def test_the_prefix_agrees_with_the_declared_version_as_shipped() -> None:
    assert submission_key(**BASE).startswith(f"wjsub-{KEY_SCHEMA_VERSION.split('.')[0]}-")


def test_the_canonical_material_is_exactly_the_five_declared_keys() -> None:
    material = canonical_key_json(**BASE)
    assert sorted(json.loads(material)) == [
        "forecast_version",
        "key_schema_version",
        "question_id",
        "request_payload_sha256",
        "tournament_id",
    ]
    # Compact and sorted: the rendering is canonical, not merely valid JSON.
    assert material == json.dumps(
        json.loads(material), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def test_the_record_id_is_not_part_of_the_key() -> None:
    """Two records holding the same version of the same question give the same key.

    Not a hypothetical: replaying a run mints a new `record_id`, and a key that moved
    with it would claim a second live post for work already done. `001`'s
    UNIQUE(question_id, tournament_id, forecast_version) is why two such records cannot
    both be stored, so this is asserted at the derivation.
    """
    assert submission_key(**BASE) == submission_key(**BASE)
    left = canonical_key_json(**BASE)
    assert "rec-" not in left


# --- the derivation, over a stored record --------------------------------------------


def test_the_record_reader_agrees_with_the_pure_derivation(
    ledger: sqlite3.Connection,
) -> None:
    _seed_draft(ledger)
    assert submission_key_for_record(
        ledger, "rec-1", request_payload_sha256=PAYLOAD_SHA
    ) == submission_key(**BASE)


def test_a_second_forecast_version_gets_its_own_key(ledger: sqlite3.Connection) -> None:
    """ "Changed forecast requires a new key", structurally: a changed forecast is a new
    record at a new version (M1-602/D25), and the version is in the key material."""
    _seed_draft(ledger)
    _seed_draft(ledger, record_id="rec-2", forecast_version=2, parent_record_id="rec-1")
    first = submission_key_for_record(ledger, "rec-1", request_payload_sha256=PAYLOAD_SHA)
    second = submission_key_for_record(ledger, "rec-2", request_payload_sha256=PAYLOAD_SHA)
    assert first != second


def test_an_unknown_record_raises(ledger: sqlite3.Connection) -> None:
    with pytest.raises(SubmissionError, match="does not name a stored forecast record"):
        submission_key_for_record(ledger, "nope", request_payload_sha256=PAYLOAD_SHA)


def test_a_malformed_payload_hash_is_refused_before_the_ledger_is_read(
    ledger: sqlite3.Connection,
) -> None:
    """The caller's own parameter is checked first, so a caller mistake costs no read.

    The connection is closed, so any read at all raises `_fetch_one`'s wrapped "could not
    be read". Getting the hex message instead is what proves the validation ran first --
    and it is a real reachable condition rather than a monkeypatched one.
    """
    _seed_draft(ledger)
    ledger.close()
    with pytest.raises(SubmissionError, match="64 lowercase hexadecimal"):
        submission_key_for_record(ledger, "rec-1", request_payload_sha256="nope")
    # ...and the same call with a well-formed hash does reach the (closed) ledger.
    with pytest.raises(SubmissionError, match="could not be read"):
        submission_key_for_record(ledger, "rec-1", request_payload_sha256=PAYLOAD_SHA)


def test_the_key_survives_the_store_and_load_round_trip(ledger: sqlite3.Connection) -> None:
    """The M1-305 rule: the digest keys on the *persisted* form.

    Derived from in-memory values, then re-derived from what SQLite hands back. A rule
    that carried a distinction JSON or SQLite drops would be stable in memory and change
    here -- which is the hash that passes every test that never went through the ledger.
    """
    _seed_draft(ledger, tournament_id="minibench", question_id=100, forecast_version=1)
    in_memory = submission_key(**BASE)
    from_ledger = submission_key_for_record(ledger, "rec-1", request_payload_sha256=PAYLOAD_SHA)
    assert in_memory == from_ledger


# --- the readers ---------------------------------------------------------------------


def test_an_unused_key_reads_as_unused(ledger: sqlite3.Connection) -> None:
    key = submission_key(**BASE)
    assert attempt_for_key(ledger, key) is None
    require_key_unused(ledger, key)  # does not raise


def test_a_spent_key_is_read_back_and_then_refused(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    require_key_unused(conn, key)
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_uncertain_attempt(key, attempt_id="att-1"),
        occurred_at=OCCURRED,
        detail_code="timeout",
    )

    summary = attempt_for_key(conn, key)
    assert summary == AttemptSummary(
        attempt_id="att-1",
        forecast_record_id=record_id,
        idempotency_key=key,
        requested_at_utc=summary.requested_at_utc if summary else "",
        completed_at_utc=summary.completed_at_utc if summary else "",
        request_payload_sha256=PAYLOAD_SHA,
        success=True,
        verified_by_refetch=False,
        refetch_outcome="absent",
        created_at_utc=summary.created_at_utc if summary else "",
    )
    with pytest.raises(SubmissionError, match="already been used"):
        require_key_unused(conn, key)


def test_the_key_the_derivation_mints_is_acceptable_to_the_real_writer(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """Asserted through `lifecycle.record_submission_attempt` rather than against
    `lifecycle._MAX_IDENTIFIER`: a private constant imported to assert against tests the
    constant, not the writer that enforces it."""
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    event = record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_uncertain_attempt(key, attempt_id="att-1"),
        occurred_at=OCCURRED,
        detail_code="timeout",
    )
    assert event.event_type == "submission_uncertain"


def test_the_same_key_cannot_create_two_attempts(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """The acceptance criterion's first half, with the guard bypassed.

    The guard is an explanation; `001`'s UNIQUE constraint is the enforcement, and this
    asserts the enforcement by going straight to the writer. The first attempt is
    deliberately *uncertain* so the record stays at `approved` -- otherwise the state
    machine would refuse the second attempt and this would prove nothing about the key.
    """
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_uncertain_attempt(key, attempt_id="att-1"),
        occurred_at=OCCURRED,
        detail_code="timeout",
    )
    # Narrowed to the writer's own error, and to its sanitized text: `Exception` would
    # have passed for an unrelated failure and proved nothing about the key
    # (cross-model review round 1, non-blocking finding 1).
    with pytest.raises(LifecycleError) as excinfo:
        record_submission_attempt(
            conn,
            record_id=record_id,
            attempt=_uncertain_attempt(key, attempt_id="att-2"),
            occurred_at=OCCURRED,
            detail_code="timeout",
        )
    assert "the ledger rejected this write" in str(excinfo.value)
    assert key not in str(excinfo.value)
    rows = conn.execute(
        "SELECT count(*) FROM submission_attempts WHERE idempotency_key = ?", (key,)
    ).fetchone()[0]
    assert rows == 1


def test_a_second_attempt_under_a_different_payload_is_not_blocked(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """The guard blocks duplicates, not new work."""
    conn, record_id = approved
    first = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_uncertain_attempt(first, attempt_id="att-1"),
        occurred_at=OCCURRED,
        detail_code="timeout",
    )
    second = submission_key_for_record(conn, record_id, request_payload_sha256=OTHER_PAYLOAD_SHA)
    assert second != first
    require_key_unused(conn, second)  # does not raise


def test_the_reader_sees_a_key_this_module_would_not_mint(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """A ledger may hold keys from an earlier scheme version. A reader that refused to
    look at them would report an unused key for one that is spent -- the one answer that
    costs a second live post."""
    conn, record_id = approved
    legacy = "legacy-key-from-some-earlier-rule"
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_uncertain_attempt(legacy, attempt_id="att-1"),
        occurred_at=OCCURRED,
        detail_code="timeout",
    )
    assert attempt_for_key(conn, legacy) is not None
    with pytest.raises(SubmissionError, match="already been used"):
        require_key_unused(conn, legacy)


def test_a_summary_round_trips_through_the_persisted_form(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_uncertain_attempt(key, attempt_id="att-1"),
        occurred_at=OCCURRED,
        detail_code="timeout",
    )
    summary = attempt_for_key(conn, key)
    assert summary is not None
    rendered = json.dumps(asdict(summary), ensure_ascii=True, sort_keys=True)
    assert json.loads(rendered) == asdict(summary)


def test_a_summary_carries_no_response_body_or_error_text() -> None:
    """A duplicate-check that handed back a stored response body would be a leak channel
    for one line of convenience."""
    fields = set(AttemptSummary.__dataclass_fields__)
    assert fields.isdisjoint({"response_body", "response_headers", "error_type", "error_message"})


def test_the_schema_is_what_refuses_a_flag_outside_zero_and_one(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """`001`'s CHECK is the enforcement, and it is reachable; the reader's gate is not.

    Worth pinning both ways round rather than contriving a row that cannot exist. With
    `success INTEGER ... CHECK (success IN (0, 1))`, INTEGER affinity converts a numeric
    string and the CHECK refuses everything else, so no INSERT can leave a third value in
    the column. `_stored_flag` stays as defense in depth for a row this package did not
    write -- tested directly below, because the schema will not produce one.
    """
    conn, record_id = approved
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO submission_attempts (attempt_id, forecast_record_id, "
            "idempotency_key, requested_at_utc, completed_at_utc, request_payload_sha256, "
            "success, verified_by_refetch, refetch_outcome, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, 'yes', 1, 'confirmed', ?)",
            ("att-raw", record_id, "raw-key", TS, TS, PAYLOAD_SHA, TS),
        )


@pytest.mark.parametrize("value", ["1", 2, None, 1.0, True])
def test_the_stored_flag_gate_refuses_anything_but_zero_or_one(value: object) -> None:
    """Called directly: see the test above for why no INSERT can deliver such a value."""
    from whiskeyjack_bot import submission

    with pytest.raises(SubmissionError, match="stored success is not 0 or 1"):
        submission._stored_flag(value, "success")


def test_text_affinity_is_why_the_stored_text_gate_is_defense_in_depth(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """A TEXT column coerces an integer on the way in, so the gate cannot fire from a row
    this schema accepted. Asserted rather than assumed, because the reasoning is what
    justifies keeping the gate without a reachable test for it."""
    conn, record_id = approved
    conn.execute(
        "INSERT INTO submission_attempts (attempt_id, forecast_record_id, idempotency_key, "
        "requested_at_utc, completed_at_utc, request_payload_sha256, success, "
        "verified_by_refetch, refetch_outcome, created_at_utc) "
        "VALUES (7, ?, ?, ?, ?, ?, 1, 1, 'confirmed', ?)",
        (record_id, "raw-key", TS, TS, PAYLOAD_SHA, TS),
    )
    summary = attempt_for_key(conn, "raw-key")
    assert summary is not None
    assert summary.attempt_id == "7"


def test_a_stored_forecast_record_column_of_the_wrong_type_is_refused(
    ledger: sqlite3.Connection,
) -> None:
    # `forecast_records` is append-only too, so the bad value goes in at INSERT. SQLite's
    # INTEGER affinity leaves a non-numeric string as TEXT, and no trigger types this
    # column -- so this row is reachable, and the key derived from it would be a fact
    # about a value the ledger cannot hold.
    _seed_draft(ledger, question_id="many")  # type: ignore[arg-type]
    with pytest.raises(SubmissionError, match="stored question_id is not an integer"):
        submission_key_for_record(ledger, "rec-1", request_payload_sha256=PAYLOAD_SHA)


# --- the accepted domain -------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("tournament_id", None, "tournament_id must be a non-empty string"),
        ("tournament_id", "", "tournament_id must be a non-empty string"),
        ("tournament_id", b"minibench", "tournament_id must be a non-empty string"),
        ("tournament_id", "x" * 201, "longer than the 200-character limit"),
        ("tournament_id", "\ud800", "cannot be stored"),
        ("question_id", "100", "question_id must be an integer"),
        ("question_id", True, "question_id must be an integer"),
        ("question_id", 1.0, "question_id must be an integer"),
        ("question_id", 0, "question_id must be a positive integer"),
        ("question_id", -1, "question_id must be a positive integer"),
        ("question_id", 2**63, "larger than a 64-bit integer"),
        ("forecast_version", None, "forecast_version must be an integer"),
        ("forecast_version", 0, "forecast_version must be a positive integer"),
        ("forecast_version", 2**63, "larger than a 64-bit integer"),
        ("request_payload_sha256", "D" * 64, "64 lowercase hexadecimal"),
        ("request_payload_sha256", "d" * 63, "64 lowercase hexadecimal"),
        ("request_payload_sha256", "g" * 64, "64 lowercase hexadecimal"),
        ("request_payload_sha256", None, "must be a non-empty string"),
    ],
)
def test_every_malformed_input_arrives_as_a_submission_error(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(SubmissionError, match=re.escape(message)):
        submission_key(**{**BASE, field: value})


def test_an_uppercase_digest_is_refused_rather_than_normalized() -> None:
    """Normalizing case would hide which of two spellings the stored key was built from,
    and an uppercase digest arriving from a caller is a bug in the caller."""
    with pytest.raises(SubmissionError):
        submission_key(**{**BASE, "request_payload_sha256": PAYLOAD_SHA.upper()})


@pytest.mark.parametrize("value", [None, "", 7, b"key", "x" * 201])
def test_a_malformed_key_is_refused_by_both_readers(
    ledger: sqlite3.Connection, value: object
) -> None:
    with pytest.raises(SubmissionError):
        attempt_for_key(ledger, value)  # type: ignore[arg-type]
    with pytest.raises(SubmissionError):
        require_key_unused(ledger, value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [None, "", 7, b"rec", "x" * 201])
def test_a_malformed_record_id_is_refused(ledger: sqlite3.Connection, value: object) -> None:
    with pytest.raises(SubmissionError):
        submission_key_for_record(
            ledger,
            value,  # type: ignore[arg-type]
            request_payload_sha256=PAYLOAD_SHA,
        )


# --- no value reaches a message or a rendered traceback ------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tournament_id", "\ud800secret-tournament"),
        ("tournament_id", "s3cr3t-tournament" * 20),
        ("request_payload_sha256", "s3cr3t-payload-hash"),
        ("question_id", "s3cr3t-question"),
    ],
)
def test_a_refused_value_never_reaches_the_message_or_the_traceback(
    field: str, value: object
) -> None:
    """`str(exc)` is not enough: a chained cause reprints the value through the rendered
    traceback, which is why every sanitizing raise here uses `from None`."""
    with pytest.raises(SubmissionError) as excinfo:
        submission_key(**{**BASE, field: value})
    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    needle = value if isinstance(value, str) else repr(value)
    assert needle not in str(excinfo.value)
    assert needle not in rendered


def test_a_refusal_from_the_ledger_never_echoes_a_stored_value(
    ledger: sqlite3.Connection,
) -> None:
    """The reader's defence, on a row only a pre-007 ledger can hold.

    M1-602's `007_forecast_version_chain.sql` refuses a non-integer `forecast_version` at
    INSERT, so this row can no longer be written through the schema as it now stands. It is
    still reachable: 007 redefines a trigger and adds no backfill probe, so a ledger written
    before it and upgraded afterwards keeps whatever its rows already held -- which is
    exactly the population `submission._stored_int` exists to refuse. The trigger is dropped
    to seed that row, which simulates a reachable condition rather than inventing one, and
    the connection is this test's own.
    """
    ledger.execute("DROP TRIGGER forecast_records_require_draft_on_insert")
    _seed_draft(
        ledger,
        tournament_id="s3cr3t-tournament",
        forecast_version="two",  # type: ignore[arg-type]
    )
    with pytest.raises(SubmissionError) as excinfo:
        submission_key_for_record(ledger, "rec-1", request_payload_sha256=PAYLOAD_SHA)
    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    assert "s3cr3t-tournament" not in rendered
    assert "two" not in str(excinfo.value)


def test_the_duplicate_refusal_never_echoes_the_key(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_uncertain_attempt(key, attempt_id="att-1"),
        occurred_at=OCCURRED,
        detail_code="timeout",
    )
    with pytest.raises(SubmissionError) as excinfo:
        require_key_unused(conn, key)
    assert key not in str(excinfo.value)


# --- the gated seam ------------------------------------------------------------------


def test_the_gated_seam_agrees_with_the_ungated_one_on_an_approved_record(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = approved
    assert submission_key_for_approved_record(
        conn, record_id, request_payload_sha256=PAYLOAD_SHA
    ) == submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)


def test_the_gated_seam_refuses_a_draft(ledger: sqlite3.Connection) -> None:
    _seed_draft(ledger)
    with pytest.raises(SubmissionError, match="holds no approval in force"):
        submission_key_for_approved_record(ledger, "rec-1", request_payload_sha256=PAYLOAD_SHA)


def test_the_gated_seam_refuses_a_validated_but_undecided_record(
    ledger: sqlite3.Connection,
) -> None:
    record_id = _seed_draft(ledger)
    record_validation(ledger, record_id=record_id, occurred_at=OCCURRED)
    with pytest.raises(SubmissionError, match="holds no approval in force"):
        submission_key_for_approved_record(ledger, record_id, request_payload_sha256=PAYLOAD_SHA)


def test_the_gated_seam_refuses_a_rejected_record(ledger: sqlite3.Connection) -> None:
    """A rejection records a decision and leaves the record `validated`; it is not an
    approval in force, so no key may be derived for a post."""
    record_id = _seed_draft(ledger)
    record_validation(ledger, record_id=record_id, occurred_at=OCCURRED)
    reject(ledger, record_id=record_id, actor="chris", occurred_at=OCCURRED)
    with pytest.raises(SubmissionError, match="holds no approval in force"):
        submission_key_for_approved_record(ledger, record_id, request_payload_sha256=PAYLOAD_SHA)


def test_the_ungated_seam_still_serves_a_draft(ledger: sqlite3.Connection) -> None:
    """The dry-run path (M2-703): an operator sees what would be submitted *before*
    deciding whether to approve it, so the ungated seam must not require an approval."""
    _seed_draft(ledger)
    assert KEY_RE.match(
        submission_key_for_record(ledger, "rec-1", request_payload_sha256=PAYLOAD_SHA)
    )


def test_an_approval_row_no_lifecycle_event_cites_does_not_open_the_gate(
    ledger: sqlite3.Connection,
) -> None:
    """`effective_approval` derives from the lifecycle event, so a raw-SQL
    `approval_events` row that moved nothing is not an approval -- and must not mint a
    submission key. Proven here rather than assumed, because it is the one shape that
    satisfies 003's hash-binding trigger while changing no state."""
    record_id = _seed_draft(ledger)
    record_validation(ledger, record_id=record_id, occurred_at=OCCURRED)
    ledger.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "created_at_utc) VALUES (?, 'approved', 'nobody', ?, ?)",
        (record_id, SHA, TS),
    )
    with pytest.raises(SubmissionError, match="holds no approval in force"):
        submission_key_for_approved_record(ledger, record_id, request_payload_sha256=PAYLOAD_SHA)


def test_the_gated_seam_refuses_an_unknown_record(ledger: sqlite3.Connection) -> None:
    with pytest.raises(SubmissionError, match="does not name a stored forecast record"):
        submission_key_for_approved_record(ledger, "nope", request_payload_sha256=PAYLOAD_SHA)


def test_the_gated_seam_validates_the_payload_hash_before_reading(
    ledger: sqlite3.Connection,
) -> None:
    _seed_draft(ledger)
    ledger.close()
    with pytest.raises(SubmissionError, match="64 lowercase hexadecimal"):
        submission_key_for_approved_record(ledger, "rec-1", request_payload_sha256="nope")


def test_an_approval_layer_error_never_escapes_as_itself(
    ledger: sqlite3.Connection,
) -> None:
    """Callers handle this module's error type only; an `ApprovalError` from the layer
    below must arrive as a `SubmissionError` with its (value-free) message preserved."""
    from whiskeyjack_bot.approval import ApprovalError

    with pytest.raises(SubmissionError) as excinfo:
        submission_key_for_approved_record(ledger, "nope", request_payload_sha256=PAYLOAD_SHA)
    assert not isinstance(excinfo.value, ApprovalError)


def test_the_documented_gap_is_real_and_is_asserted(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """The recorded limitation, pinned as a test rather than left as prose (D33, M2-707).

    Two different payloads for one approved forecast get two different keys and are both
    served by the gated seam, because an approval binds to `forecast_sha256` and one
    approved forecast covers every payload built from it. If a later change closes this,
    the test fails and the note must be updated -- which is the point of asserting a known
    gap rather than only describing one.
    """
    conn, record_id = approved
    first = submission_key_for_approved_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    second = submission_key_for_approved_record(
        conn, record_id, request_payload_sha256=OTHER_PAYLOAD_SHA
    )
    assert first != second
    approval = effective_approval(conn, record_id)
    assert approval is not None
    assert approval.forecast_sha256 == SHA


# --- the gate is about the status now, not only the history --------------------------


def _attempt(key: str, *, attempt_id: str, success: bool, refetch: str) -> SubmissionAttempt:
    return SubmissionAttempt(
        attempt_id=attempt_id,
        idempotency_key=key,
        requested_at_utc=OCCURRED,
        completed_at_utc=OCCURRED,
        request_payload_sha256=PAYLOAD_SHA,
        success=success,
        refetch_outcome=refetch,
    )


@pytest.mark.parametrize(
    ("success", "refetch", "detail_code", "expected_status"),
    [
        (False, "absent", "http_error", "failed"),
        (True, "confirmed", None, "submitted"),
    ],
)
def test_the_gated_seam_refuses_a_record_that_left_approved(
    approved: tuple[sqlite3.Connection, str],
    success: bool,
    refetch: str,
    detail_code: str | None,
    expected_status: str,
) -> None:
    """An approval event is append-only history; a record carries it forever.

    So `effective_approval()` still reports one after the record has reached terminal
    `failed` or `submitted`, and a gate resting on it alone would mint a key for a record
    `record_submission_attempt` can no longer append an event for -- a live post the ledger
    cannot record. Reproduced by execution at cross-model review round 2.
    """
    conn, record_id = approved
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_attempt("k-1", attempt_id="att-1", success=success, refetch=refetch),
        occurred_at=OCCURRED,
        detail_code=detail_code,  # type: ignore[arg-type]
    )
    assert current_status(conn, record_id) == expected_status
    # The history still says approved -- which is why the second check is not redundant.
    assert effective_approval(conn, record_id) is not None
    with pytest.raises(
        SubmissionError, match=f"no longer awaiting submission \\(it is {expected_status}\\)"
    ):
        submission_key_for_approved_record(
            conn, record_id, request_payload_sha256=OTHER_PAYLOAD_SHA
        )


def test_an_unresolved_uncertain_attempt_still_passes_the_gate(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """`submission_uncertain` leaves the record at `approved`, deliberately (M1-603).

    Whether to make a second request while one is unresolved is decided by
    `lifecycle.unresolved_uncertainties`, not by refusing to derive a key -- and an
    uncertain attempt must not be terminal, or a later confirming refetch has nowhere to
    land. So this must keep passing; a fix that made it fail would have overshot.
    """
    conn, record_id = approved
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_attempt("k-1", attempt_id="att-1", success=True, refetch="absent"),
        occurred_at=OCCURRED,
        detail_code="timeout",
    )
    assert current_status(conn, record_id) == "approved"
    assert KEY_RE.match(
        submission_key_for_approved_record(
            conn, record_id, request_payload_sha256=OTHER_PAYLOAD_SHA
        )
    )


def test_the_ungated_seam_still_serves_a_terminal_record(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """The status check belongs to the gated seam only: reading back the key a *past*
    attempt used must keep working for a `failed` or `submitted` record, or the ledger
    could not explain its own history."""
    conn, record_id = approved
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_attempt("k-1", attempt_id="att-1", success=False, refetch="absent"),
        occurred_at=OCCURRED,
        detail_code="http_error",
    )
    assert current_status(conn, record_id) == "failed"
    assert KEY_RE.match(
        submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    )


def test_the_status_refusal_names_only_the_closed_vocabulary(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """The message names the status, which is a `LifecycleStatus` this package defines --
    not a stored value. Nothing else about the record appears."""
    conn, record_id = approved
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_attempt("k-1", attempt_id="att-1", success=False, refetch="absent"),
        occurred_at=OCCURRED,
        detail_code="http_error",
    )
    with pytest.raises(SubmissionError) as excinfo:
        submission_key_for_approved_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    message = str(excinfo.value)
    assert "failed" in message
    assert record_id not in message
    assert SHA not in message
    assert PAYLOAD_SHA not in message


def test_every_status_reachable_from_approved_is_accounted_for() -> None:
    """Enumerated from the state machine, not from a hand-written list.

    M1-308's lesson: a guard tested against the cases the author thought of moves when the
    truth table does. `_LEGAL_TRANSITIONS` is read here to *generate* the cases, never as
    the expected value of an assertion -- the assertion is about what the gate accepts.
    """
    from whiskeyjack_bot.lifecycle import _LEGAL_TRANSITIONS

    reachable = {
        to_status
        for _event, from_status, to_status in _LEGAL_TRANSITIONS
        if from_status == "approved"
    }
    # The gate admits exactly one of them. If a future migration adds a transition out of
    # `approved`, this fails until someone decides which side of the gate it belongs on.
    assert reachable == {"approved", "submitted", "failed"}


# --- M2-708: the reservation, which is the claim the read never was ------------------
#
# Everything above this line tests a *read*. `require_key_unused` answers "has this key
# been spent", and two commands could both get "no" from it, both post, and only then
# discover that `001`'s UNIQUE refuses the second row -- after its call had been made.
# These drive the layer where the check and the claim are one act.
#
# The unit of behaviour here is the Python seam; `tests/unit/test_lifecycle.py` drives
# `010`'s triggers with raw SQL, because the writer agreeing with itself is not the
# guarantee.


def _reserved(conn: sqlite3.Connection, record_id: str, key: str) -> KeyReservation:
    return reserve_submission_key(
        conn, record_id=record_id, idempotency_key=key, reserved_at=OCCURRED
    )


def _abandon(conn: sqlite3.Connection, reservation: KeyReservation) -> None:
    """Release by the operator route, the only one a person can take."""
    release_submission_key(
        conn,
        reservation,
        reason="operator_abandoned",
        released_at=OCCURRED,
        released_by="chris",
    )


def _reservation_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    return (
        conn.execute("SELECT count(*) FROM submission_key_reservations").fetchone()[0],
        conn.execute("SELECT count(*) FROM submission_key_releases").fetchone()[0],
    )


def test_a_reservation_is_minted_and_reads_back(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)

    assert reservation.idempotency_key == key
    assert reservation.forecast_record_id == record_id
    assert reservation.reservation_seq == 1
    assert reservation.reserved_at_utc == OCCURRED.isoformat(timespec="microseconds")
    assert reservation.reservation_id.startswith("wjres-")
    # The reader agrees with what the writer returned. Asserting only the return value
    # would pass against a writer that never committed.
    assert live_reservation_for_key(conn, key) == reservation


def test_a_second_command_cannot_reserve_a_held_key(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """The item, in one test: the loser is refused before it can decide to post."""
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    _reserved(conn, record_id, key)

    with pytest.raises(SubmissionError, match="reserved by a submission that has not finished"):
        _reserved(conn, record_id, key)
    # Both halves: the refusal happened *and* nothing was written by the loser.
    assert _reservation_counts(conn) == (1, 0)


def test_a_spent_key_cannot_be_reserved(approved: tuple[sqlite3.Connection, str]) -> None:
    """Terminal beats free. An attempt row is a call that was made."""
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_uncertain_attempt(key, attempt_id="att-1"),
        occurred_at=OCCURRED,
        detail_code="timeout",
    )
    with pytest.raises(SubmissionError, match="already been used by a recorded submission"):
        _reserved(conn, record_id, key)
    assert _reservation_counts(conn) == (0, 0)


def test_a_released_key_can_be_claimed_again_under_the_next_sequence_number(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """Why a release exists at all.

    A key is a pure function of the tournament, question, forecast version and payload
    hash, so the same work derives the same key forever. Without a second sequence number
    a single transient pre-post failure would wedge that forecast permanently, on an
    append-only table.
    """
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    first = _reserved(conn, record_id, key)
    _abandon(conn, first)

    assert live_reservation_for_key(conn, key) is None
    second = _reserved(conn, record_id, key)
    assert second.reservation_seq == 2
    assert second.reservation_id != first.reservation_id
    assert _reservation_counts(conn) == (2, 1)


def test_require_key_unused_now_refuses_a_reserved_key(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """One question, one answer.

    A caller asking "may I claim this key" is asking one thing, and spent and reserved are
    both "no". A second guard for the second condition would be two spellings of one bound
    with nothing keeping them in agreement.
    """
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    require_key_unused(conn, key)  # free: returns silently

    _reserved(conn, record_id, key)
    with pytest.raises(SubmissionError, match="reserved by a submission that has not finished"):
        require_key_unused(conn, key)


def test_a_reserved_key_is_free_again_to_the_cheap_reader_after_release(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    _abandon(conn, _reserved(conn, record_id, key))
    require_key_unused(conn, key)


# --- the two release reasons are not the same claim ----------------------------------


def test_not_posted_refuses_an_actor(approved: tuple[sqlite3.Connection, str]) -> None:
    """The program proved no post was made; there is no person to name.

    Accepting one would put a name against a conclusion no person reached.
    """
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)
    with pytest.raises(SubmissionError, match="omit released_by"):
        release_submission_key(
            conn,
            reservation,
            reason="not_posted",
            released_at=OCCURRED,
            released_by="chris",
        )
    assert _reservation_counts(conn) == (1, 0)


def test_operator_abandoned_requires_an_actor(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """`approve`'s rule: an attribution claim about a person is never inferred."""
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)
    with pytest.raises(SubmissionError, match="released_by is required"):
        release_submission_key(conn, reservation, reason="operator_abandoned", released_at=OCCURRED)
    assert _reservation_counts(conn) == (1, 0)


def test_the_program_route_writes_no_actor(approved: tuple[sqlite3.Connection, str]) -> None:
    """The control for the two refusals above: each reason's legal form is accepted."""
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)
    release_submission_key(conn, reservation, reason="not_posted", released_at=OCCURRED)
    stored = conn.execute(
        "SELECT reason, released_by FROM submission_key_releases WHERE reservation_id = ?",
        (reservation.reservation_id,),
    ).fetchone()
    assert tuple(stored) == ("not_posted", None)


@pytest.mark.parametrize("value", ["", "spent", "NOT_POSTED", " not_posted ", 0, None, b"x"])
def test_a_reason_outside_the_vocabulary_is_refused(
    approved: tuple[sqlite3.Connection, str], value: object
) -> None:
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)
    with pytest.raises(SubmissionError, match="reason must be one of"):
        release_submission_key(
            conn,
            reservation,
            reason=value,  # type: ignore[arg-type]
            released_at=OCCURRED,
            released_by="chris",
        )
    assert _reservation_counts(conn) == (1, 0)


def test_a_reservation_is_released_once(approved: tuple[sqlite3.Connection, str]) -> None:
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)
    _abandon(conn, reservation)
    with pytest.raises(SubmissionError, match="already been released"):
        _abandon(conn, reservation)
    assert _reservation_counts(conn) == (1, 1)


def test_a_consumed_reservation_is_not_abandoned(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """A release is not a way to un-spend a key.

    Recording a consumed reservation as abandoned would assert something false about an
    irreversible call -- and would give the derived-state table two answers for one key.
    """
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_uncertain_attempt(key, attempt_id="att-1"),
        occurred_at=OCCURRED,
        detail_code="timeout",
    )
    with pytest.raises(SubmissionError, match="consumed by a recorded submission attempt"):
        _abandon(conn, reservation)
    assert _reservation_counts(conn) == (1, 0)


def test_the_spent_test_reads_the_key_from_the_stored_row_not_the_object(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """M2-703 round 1, applied by construction: one source of truth.

    Only `reservation_id` reaches the row. A caller handing back an object whose other
    fields have been altered cannot steer the decision, because the key the "already
    spent" test runs against is read back from the database.
    """
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_uncertain_attempt(key, attempt_id="att-1"),
        occurred_at=OCCURRED,
        detail_code="timeout",
    )
    # A lie about the key, which would make the spent test pass if the object were trusted.
    lying = replace_reservation(reservation, idempotency_key="wjsub-1-" + "f" * 64)
    with pytest.raises(SubmissionError, match="consumed by a recorded submission attempt"):
        _abandon(conn, lying)


def replace_reservation(reservation: KeyReservation, **changes: object) -> KeyReservation:
    from dataclasses import replace

    return replace(reservation, **changes)  # type: ignore[arg-type]


def test_a_reservation_subclass_is_refused(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """Exact type, not isinstance: a subclass can shadow a field with a property, turning
    the read of `reservation_id` into caller code that can raise anything."""
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)

    class Sneaky(KeyReservation):
        pass

    impostor = Sneaky(**asdict(reservation))
    with pytest.raises(SubmissionError, match="must be a KeyReservation"):
        _abandon(conn, impostor)
    assert _reservation_counts(conn) == (1, 0)


def test_an_unknown_reservation_is_refused(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)
    ghost = replace_reservation(reservation, reservation_id="wjres-" + "0" * 32)
    with pytest.raises(SubmissionError, match="does not name a stored key reservation"):
        _abandon(conn, ghost)
    assert _reservation_counts(conn) == (1, 0)


# --- the reservation's accepted domain -----------------------------------------------


@pytest.mark.parametrize("value", [None, "", 0, b"rec-1", "x" * 201, "\ud800", ["rec-1"]])
def test_a_malformed_record_id_is_refused_before_the_ledger_is_touched(
    approved: tuple[sqlite3.Connection, str], value: object
) -> None:
    """M1-303 round 4: refuse a caller mistake before the spend."""
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    with pytest.raises(SubmissionError):
        reserve_submission_key(
            conn,
            record_id=value,  # type: ignore[arg-type]
            idempotency_key=key,
            reserved_at=OCCURRED,
        )
    assert _reservation_counts(conn) == (0, 0)


@pytest.mark.parametrize("value", [None, "", 0, b"k", "x" * 201, "\ud800", {"k": 1}])
def test_a_malformed_key_is_refused_by_the_reservation_writer(
    approved: tuple[sqlite3.Connection, str], value: object
) -> None:
    conn, record_id = approved
    with pytest.raises(SubmissionError):
        reserve_submission_key(
            conn,
            record_id=record_id,
            idempotency_key=value,  # type: ignore[arg-type]
            reserved_at=OCCURRED,
        )
    assert _reservation_counts(conn) == (0, 0)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "2026-08-20T12:00:00+00:00",
        datetime(2026, 8, 20, 12, 0),  # naive
        0,
    ],
)
def test_a_reserved_at_that_is_not_an_aware_datetime_is_refused(
    approved: tuple[sqlite3.Connection, str], value: object
) -> None:
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    with pytest.raises(SubmissionError):
        reserve_submission_key(
            conn,
            record_id=record_id,
            idempotency_key=key,
            reserved_at=value,  # type: ignore[arg-type]
        )
    assert _reservation_counts(conn) == (0, 0)


def test_a_non_utc_reserved_at_is_converted_rather_than_refused(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """The control for the refusals above, and the reason `010` can compare two columns.

    An aware datetime in another zone is legal and is *converted*, so the stored text is
    the one fixed-width UTC form `submission_key_releases.released_at_utc` is compared
    against lexicographically. Asserting only the shape would pass against a writer that
    rendered the local wall clock and appended `+00:00` -- which is the same instant
    written as a different, and later, string.
    """
    from datetime import timedelta

    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    elsewhere = OCCURRED.astimezone(timezone(timedelta(hours=-5)))
    assert elsewhere.isoformat().startswith("2026-08-20T07:00:00")  # same instant, 07:00 local

    reservation = reserve_submission_key(
        conn, record_id=record_id, idempotency_key=key, reserved_at=elsewhere
    )
    assert reservation.reserved_at_utc == "2026-08-20T12:00:00.000000+00:00"
    assert len(reservation.reserved_at_utc) == 32
    # And the database holds what the value object reported.
    stored = conn.execute(
        "SELECT reserved_at_utc FROM submission_key_reservations WHERE reservation_id = ?",
        (reservation.reservation_id,),
    ).fetchone()[0]
    assert stored == reservation.reserved_at_utc


def test_one_key_cannot_be_reserved_against_two_records(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """A key is a function of (tournament, question, version, payload), and `001` declares
    UNIQUE (question_id, tournament_id, forecast_version) -- so key -> record is a
    function. Two records claiming one key means a derivation is wrong, and the
    reservation is the last place that is cheap to notice.
    """
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    other = _seed_draft(conn, record_id="rec-2", question_id=200)
    reservation = _reserved(conn, record_id, key)
    _abandon(conn, reservation)  # released, so the "already reserved" clause cannot fire

    # `010`'s trigger is what refuses this, so it arrives as the wrapped ledger refusal:
    # every case the Python layer can name has its own message, and this is not one of
    # them -- the payload hash the key is derived from is the caller's and is stored
    # nowhere this could read.
    with pytest.raises(SubmissionError, match="the ledger rejected this write"):
        _reserved(conn, other, key)
    assert _reservation_counts(conn) == (1, 1)


def test_a_reservation_against_an_unknown_record_is_refused(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    with pytest.raises(SubmissionError):
        reserve_submission_key(
            conn, record_id="rec-missing", idempotency_key=key, reserved_at=OCCURRED
        )
    assert _reservation_counts(conn) == (0, 0)


def test_the_reservation_refusals_never_echo_the_key(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """Same rule as every other refusal here: the key is derived from a payload hash, and
    echoing it would let a caller confirm a guess about stored content."""
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    _reserved(conn, record_id, key)
    with pytest.raises(SubmissionError) as excinfo:
        _reserved(conn, record_id, key)
    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    assert key not in str(excinfo.value)
    assert key not in rendered


def test_a_refusal_raised_by_the_trigger_never_echoes_a_stored_value(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """The one refusal path the property tests cannot reach.

    Every value `LEAKY_REFUSED` draws is turned away by a Python validator before the
    ledger is touched, so `_execute`'s wrap -- the `from None` that keeps a database
    message and its traceback out of the refusal -- is only reachable with values that
    are *individually* well-formed and collectively refused by `010`. This is that case:
    a key already reserved against another record, re-reserved against a record whose
    identifier carries the planted secret.
    """
    secret = "privateFAKE123456"
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    other = _seed_draft(conn, record_id=f"rec-{secret}", question_id=200)
    _abandon(conn, _reserved(conn, record_id, key))

    with pytest.raises(SubmissionError) as excinfo:
        _reserved(conn, other, key)
    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    assert secret not in str(excinfo.value)
    assert secret not in rendered
    assert key not in rendered


# ---------------------------------------------------------------------------
# `010`'s triggers, driven directly.
# ---------------------------------------------------------------------------
#
# The writer above is documented as *not* being the guarantee: `reserve_submission_key`'s
# own checks turn the guarantee into a message an operator can act on, and the trigger is
# "the enforcement, the layer that cannot be raced". A mutation pass proved that claim was
# untested -- neutering the seq, already-reserved, already-spent, release-ordering and
# consumed-reservation clauses to `WHERE 0` left the whole suite green, because every test
# that could reach them is refused one layer earlier by Python.
#
# That is the vacuous-property class in `docs/LESSONS.md` wearing a different coat: the
# assertion was about the trigger, and nothing could reach it. So these probes bypass the
# writer entirely and INSERT raw, which is also the only shape that resembles the case the
# trigger exists for -- a second process whose read and write did interleave.

# `reserved_at_utc` is pinned by `010` to the fixed-width UTC form, so the probes below
# spell it the way the writer does rather than reusing `TS`, whose only job is an unordered
# `created_at_utc`.
OCCURRED_TEXT = OCCURRED.isoformat(timespec="microseconds")

_RAW_RESERVATION = (
    "INSERT INTO submission_key_reservations (reservation_id, idempotency_key, "
    "forecast_record_id, reservation_seq, reserved_at_utc, created_at_utc) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_RAW_RELEASE = (
    "INSERT INTO submission_key_releases (release_id, reservation_id, reason, "
    "released_by, note, released_at_utc, created_at_utc) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def test_the_trigger_refuses_a_second_live_reservation(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """The clause the whole item rests on: one key, one live claim.

    Reached only by raw SQL -- `reserve_submission_key` refuses this itself, one layer up.
    """
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    _reserved(conn, record_id, key)
    with pytest.raises(sqlite3.IntegrityError, match="already reserved"):
        conn.execute(_RAW_RESERVATION, ("wjres-raw", key, record_id, 2, OCCURRED_TEXT, TS))
    assert _reservation_counts(conn) == (1, 0)


def test_the_trigger_refuses_a_key_a_recorded_attempt_already_spent(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """Terminal beats free at the schema, not only at the writer."""
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_uncertain_attempt(key, attempt_id="att-1"),
        occurred_at=OCCURRED,
        detail_code="timeout",
    )
    with pytest.raises(sqlite3.IntegrityError, match="already been used"):
        conn.execute(_RAW_RESERVATION, ("wjres-raw", key, record_id, 1, OCCURRED_TEXT, TS))
    assert _reservation_counts(conn) == (0, 0)


@pytest.mark.parametrize("sequence", [0, 1, 3, -1])
def test_the_trigger_refuses_a_sequence_number_that_is_not_the_next_one(
    approved: tuple[sqlite3.Connection, str], sequence: int
) -> None:
    """`_next_reservation_seq` always computes the right one, so only raw SQL gets here.

    The parameters straddle the boundary rather than sitting on one side of it: `1` is the
    number already taken, `3` skips one, `0` and `-1` are below the floor. A clause tested
    with a single wrong value passes just as well when it is an inequality pointing the
    wrong way.
    """
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)
    _abandon(conn, reservation)  # released, so the "already reserved" clause cannot fire
    with pytest.raises(sqlite3.IntegrityError, match="next sequence number"):
        conn.execute(_RAW_RESERVATION, ("wjres-raw", key, record_id, sequence, OCCURRED_TEXT, TS))
    assert _reservation_counts(conn) == (1, 1)


def test_the_trigger_refuses_a_release_earlier_than_the_reservation_it_releases(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """`post_approved_forecast` reuses one instant for both, so this needs raw SQL.

    A release that predates its own claim is not an ordering nicety: `released_at_utc` is
    what an audit reads to decide when the key became free again, and a value before the
    reservation says the key was free while it was held.
    """
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)
    earlier = "2020-01-01T00:00:00.000000+00:00"
    with pytest.raises(sqlite3.IntegrityError, match="earlier than the reservation"):
        conn.execute(
            _RAW_RELEASE,
            (
                "wjrel-raw",
                reservation.reservation_id,
                "operator_abandoned",
                "chris",
                None,
                earlier,
                TS,
            ),
        )
    assert _reservation_counts(conn) == (1, 0)


def test_the_trigger_refuses_releasing_a_reservation_an_attempt_consumed(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """A spent reservation was not abandoned, and recording it as abandoned asserts
    something false about a call that was really made. The writer refuses it; so does
    `010`, which is the copy that survives a second program.
    """
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=_uncertain_attempt(key, attempt_id="att-1"),
        occurred_at=OCCURRED,
        detail_code="timeout",
    )
    with pytest.raises(sqlite3.IntegrityError, match="was not abandoned"):
        conn.execute(
            _RAW_RELEASE,
            (
                "wjrel-raw",
                reservation.reservation_id,
                "operator_abandoned",
                "chris",
                None,
                OCCURRED_TEXT,
                TS,
            ),
        )
    assert _reservation_counts(conn) == (1, 0)


def test_the_reader_is_total_against_a_ledger_holding_two_live_reservations(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """`010` allows at most one, so this is a ledger some other program wrote.

    A reader that raised on it would refuse to report the very state an operator needs to
    see. The trigger is dropped to reach the row, which simulates a reachable condition on
    this test's own connection rather than inventing an attacker.
    """
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    first = _reserved(conn, record_id, key)
    conn.execute("DROP TRIGGER submission_key_reservations_validate_on_insert")
    conn.execute(
        "INSERT INTO submission_key_reservations (reservation_id, idempotency_key, "
        "forecast_record_id, reservation_seq, reserved_at_utc, created_at_utc) "
        "VALUES (?, ?, ?, 2, ?, ?)",
        ("wjres-" + "a" * 32, key, record_id, TS, TS),
    )
    live = live_reservation_for_key(conn, key)
    assert live is not None
    # The highest sequence number wins, deterministically -- not an arbitrary row.
    assert live.reservation_seq == 2 and live.reservation_id != first.reservation_id


# --- the by-record reader, which is what an operator can actually ask ------------------


def test_the_by_record_reader_returns_nothing_when_no_key_is_held(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = approved
    assert live_reservations_for_record(conn, record_id) == ()


def test_the_by_record_reader_finds_the_held_key(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    reservation = _reserved(conn, record_id, key)
    assert live_reservations_for_record(conn, record_id) == (reservation,)


def test_the_by_record_reader_returns_every_live_claim_in_sequence_order(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """One record can hold two live reservations, and that is why this returns a tuple.

    `010` constrains one *key* to one live reservation. Two payloads for one record derive
    two keys, so both claims stand at once -- and a reader that returned a single row
    would have to pick between them, invisibly, in front of an operator deciding which
    submission they went and checked.
    """
    conn, record_id = approved
    first = _reserved(
        conn,
        record_id,
        submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA),
    )
    second = _reserved(
        conn,
        record_id,
        submission_key_for_record(conn, record_id, request_payload_sha256=OTHER_PAYLOAD_SHA),
    )
    assert live_reservations_for_record(conn, record_id) == (first, second)


def test_the_by_record_reader_drops_a_released_claim(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    """ "Live" is the whole contract: a released claim is history, not current state."""
    conn, record_id = approved
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    kept = _reserved(
        conn,
        record_id,
        submission_key_for_record(conn, record_id, request_payload_sha256=OTHER_PAYLOAD_SHA),
    )
    _abandon(conn, _reserved(conn, record_id, key))
    assert live_reservations_for_record(conn, record_id) == (kept,)


def test_the_by_record_reader_does_not_answer_for_another_record(
    approved: tuple[sqlite3.Connection, str],
) -> None:
    conn, record_id = approved
    other = _seed_draft(conn, record_id="rec-2", question_id=200)
    key = submission_key_for_record(conn, record_id, request_payload_sha256=PAYLOAD_SHA)
    _reserved(conn, record_id, key)
    assert live_reservations_for_record(conn, other) == ()


@pytest.mark.parametrize("value", [None, "", 0, b"rec-1", "x" * 201, "\ud800"])
def test_the_by_record_reader_refuses_a_malformed_identifier(
    approved: tuple[sqlite3.Connection, str], value: object
) -> None:
    conn, _record_id = approved
    with pytest.raises(SubmissionError):
        live_reservations_for_record(conn, value)  # type: ignore[arg-type]
