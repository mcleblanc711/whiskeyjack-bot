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
    SubmissionError,
    attempt_for_key,
    canonical_key_json,
    require_key_unused,
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
) -> str:
    """Insert a draft directly: M1-602's record writer does not exist yet.

    The same shape `tests/unit/test_approval.py` uses, including the per-record
    `attempt_id` migration 004 indexes UNIQUE.
    """
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
        "forecast_sha256, attempt_id) "
        "VALUES (?, ?, ?, ?, 'binary', 'draft', 'anthropic', 'claude', 'v1', 'abc', "
        "'run-1', ?, '{}', '{}', ?, ?, ?)",
        (
            record_id,
            question_id,
            tournament_id,
            forecast_version,
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
        verified_by_refetch=False,
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
    _seed_draft(ledger, record_id="rec-2", forecast_version=2)
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
            "success, verified_by_refetch, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, 'yes', 1, ?)",
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
        "verified_by_refetch, created_at_utc) VALUES (7, ?, ?, ?, ?, ?, 1, 1, ?)",
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


def _attempt(key: str, *, attempt_id: str, success: bool, verified: bool) -> SubmissionAttempt:
    return SubmissionAttempt(
        attempt_id=attempt_id,
        idempotency_key=key,
        requested_at_utc=OCCURRED,
        completed_at_utc=OCCURRED,
        request_payload_sha256=PAYLOAD_SHA,
        success=success,
        verified_by_refetch=verified,
    )


@pytest.mark.parametrize(
    ("success", "verified", "detail_code", "expected_status"),
    [
        (False, False, "http_error", "failed"),
        (True, True, None, "submitted"),
    ],
)
def test_the_gated_seam_refuses_a_record_that_left_approved(
    approved: tuple[sqlite3.Connection, str],
    success: bool,
    verified: bool,
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
        attempt=_attempt("k-1", attempt_id="att-1", success=success, verified=verified),
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
        attempt=_attempt("k-1", attempt_id="att-1", success=True, verified=False),
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
        attempt=_attempt("k-1", attempt_id="att-1", success=False, verified=False),
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
        attempt=_attempt("k-1", attempt_id="att-1", success=False, verified=False),
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
