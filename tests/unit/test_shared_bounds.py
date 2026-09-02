"""M1-608: every layer that bounds a ledger field reads the same number.

Six modules used to spell `200` themselves, each under a comment saying it matched the
others. They did match. Nothing held them there, and the acceptance criterion is that they
*cannot* diverge if the limit changes -- which is a different claim from "they agree
today", and the one the existing
`test_lifecycle_properties.test_the_two_module_copies_of_the_identifier_rule_agree` does
not make.

**The assertion here is cross-layer equality, never a comparison against the constant.**
`bounds.MAX_IDENTIFIER_LENGTH` only chooses where to probe; the thing every Python layer is
measured against is a real INSERT, and the migrations spell their `length(...) > 200`
clauses as frozen literals that no edit to `bounds.py` can reach (a migration on master is
immutable by checksum). So moving the constant moves the Python layers and leaves the
schema behind, and the vectors stop matching. That is what makes this more than the
constant asserting itself -- the M1-303 rule `test_submission.py`'s
`test_the_key_the_derivation_mints_is_acceptable_to_the_real_writer` states the other way
round.

The three bounds are not equally witnessed, and each test below says which witness it has.
`MAX_NOTE_LENGTH` and `MAX_BODY_LENGTH` have no schema clause at all; do not read their
tests as claiming the ledger enforces them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from whiskeyjack_bot import approval, lifecycle, submission, submission_gateway, submission_live
from whiskeyjack_bot.approval import ApprovalError
from whiskeyjack_bot.bounds import (
    MAX_ACTOR_LENGTH,
    MAX_BODY_LENGTH,
    MAX_IDENTIFIER_LENGTH,
    MAX_NOTE_LENGTH,
)
from whiskeyjack_bot.forecast.record import ForecastRecord
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import LifecycleError, record_approval, record_validation
from whiskeyjack_bot.submission import (
    KeyReservation,
    SubmissionError,
    release_submission_key,
    reserve_submission_key,
)
from whiskeyjack_bot.submission_gateway import GatewayError
from whiskeyjack_bot.submission_live import LiveSubmissionError

TS = "2026-09-01T00:00:00.000000+00:00"
OCCURRED = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
SHA = "b" * 64
# M2-707: `011` requires an approval to carry the payload digest it authorized. Shape
# only -- this module is about the `note` bound, and any well-formed digest reaches it.
PAYLOAD_SHA = "d" * 64


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, question_id, "
            "started_at_utc, created_at_utc) VALUES ('run-1', 'asknews', 100, ?, ?)",
            (TS, TS),
        )
        yield conn
    finally:
        conn.close()


def _accepts(validate: Callable[[str], object], error: type[Exception], value: str) -> bool:
    """Whether one layer admits `value`, catching only that layer's own error type.

    A raw `AttributeError`/`ValueError` escaping is a review finding in this project, so
    the narrow `except` is deliberate: it fails the test rather than being counted as a
    refusal.
    """
    try:
        validate(value)
    except error:
        return False
    return True


# Every Python layer that applies the identifier bound, with the sanitized error each one
# owns. Driven from a list rather than written out five times, for `006`'s reason: the
# defect this item closes is a bound present in one module and different in the one beside
# it, and hand-written per-module cases are the shape that misses that again.
_IDENTIFIER_LAYERS: list[tuple[str, Callable[[str], object], type[Exception]]] = [
    ("lifecycle", lambda v: lifecycle._require_identifier(v, "record_id"), LifecycleError),
    ("approval", lambda v: approval._require_identifier(v, "record_id"), ApprovalError),
    ("submission", lambda v: submission._require_identifier(v, "record_id"), SubmissionError),
    (
        "submission_gateway",
        lambda v: submission_gateway._require_identifier(v, "record_id"),
        GatewayError,
    ),
    (
        "submission_live",
        lambda v: submission_live._require_identifier(v, "record_id"),
        LiveSubmissionError,
    ),
]


def _record_model_accepts(value: str) -> bool:
    """Whether `forecast.record`'s pydantic bound admits `value` as a `record_id`.

    Validated against a dict holding nothing else, so every other field fails as
    `missing`; only a `string_too_long`/`string_too_short` error *on `record_id`* counts as
    a refusal. That reaches the field's declared bound without building a whole record, and
    without restating the annotation here -- an `Annotated[str, Field(max_length=...)]`
    spelled in the test would be the test asserting itself.
    """
    try:
        ForecastRecord.model_validate({"record_id": value})
    except ValidationError as error:
        return not any(
            item["loc"] == ("record_id",)
            and item["type"] in {"string_too_long", "string_too_short"}
            for item in error.errors(include_input=False, include_url=False)
        )
    return True  # pragma: no cover - the other fields are missing, so this cannot be reached


def _schema_accepts(conn: sqlite3.Connection, column: str, value: str, serial: int) -> bool:
    """Whether the migrations' triggers admit `value` in `forecast_records.<column>`.

    Rolled back either way: `forecast_records` is append-only, so an accepted row could not
    be removed and the next probe would collide on the primary key instead of measuring
    anything.
    """
    record_id = value if column == "record_id" else f"rec-probe-{serial}"
    tournament_id = value if column == "tournament_id" else "minibench"
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO forecast_records ("
            "record_id, question_id, tournament_id, forecast_version, parent_record_id, "
            "question_type, status, model_provider, model_name, prompt_version, "
            "prompt_sha256, retrieval_run_id, generated_at_utc, final_prediction_json, "
            "record_json, created_at_utc, forecast_sha256, attempt_id) "
            "VALUES (?, ?, ?, 1, NULL, 'binary', 'draft', 'anthropic', 'claude', 'v1', "
            "'abc', 'run-1', ?, '{}', '{}', ?, ?, ?)",
            (record_id, 900000 + serial, tournament_id, TS, TS, SHA, f"att-probe-{serial}"),
        )
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.execute("ROLLBACK")
    return True


def test_every_layer_reads_the_same_identifier_ceiling(ledger: sqlite3.Connection) -> None:
    """The acceptance criterion, executed: seven layers, one ceiling, one witness.

    Probes sit at `MAX_IDENTIFIER_LENGTH` minus one, exactly, and plus one, so the vector
    each layer produces pins the boundary rather than the neighbourhood of it. `200` itself
    must be *accepted*: a ceiling that refused its own boundary is a different rule from
    the one the schema applies, which is the same disagreement pointed the other way
    (`test_lifecycle.test_the_schema_refuses_an_identifier_over_200_characters`).

    Re-spell a literal in any one module and that module's vector differs from the rest.
    Move `bounds.MAX_IDENTIFIER_LENGTH` and every Python layer follows while the
    migrations' frozen `200` does not, so the probes land on the wrong side of the schema
    and this fails there. Neither edit can be made quietly, which is the whole item.
    """
    lengths = [MAX_IDENTIFIER_LENGTH - 1, MAX_IDENTIFIER_LENGTH, MAX_IDENTIFIER_LENGTH + 1]

    vectors: dict[str, tuple[bool, ...]] = {
        name: tuple(_accepts(validate, error, "x" * n) for n in lengths)
        for name, validate, error in _IDENTIFIER_LAYERS
    }
    vectors["forecast.record"] = tuple(_record_model_accepts("x" * n) for n in lengths)
    for column in ("record_id", "tournament_id"):
        vectors[f"schema.{column}"] = tuple(
            _schema_accepts(ledger, column, "x" * n, serial)
            for serial, n in enumerate(lengths, start=1 if column == "record_id" else 100)
        )
    assert not ledger.in_transaction

    # The guard on the guard. Equality alone is satisfied by a set of layers that all
    # refuse everything, or all accept everything -- which is how a parity test goes green
    # while asserting nothing. Naming the vector is what makes the equality mean the
    # boundary is where it should be.
    expected = (True, True, False)
    assert vectors == {name: expected for name in vectors}, vectors


def _seed_draft(conn: sqlite3.Connection, record_id: str, question_id: int) -> str:
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, parent_record_id, "
        "question_type, status, model_provider, model_name, prompt_version, prompt_sha256, "
        "retrieval_run_id, generated_at_utc, final_prediction_json, record_json, "
        "created_at_utc, forecast_sha256, attempt_id) "
        "VALUES (?, ?, 'minibench', 1, NULL, 'binary', 'draft', 'anthropic', 'claude', "
        "'v1', 'abc', 'run-1', ?, '{}', '{}', ?, ?, ?)",
        (record_id, question_id, TS, TS, SHA, f"att-{record_id}"),
    )
    return record_id


def _reservation(conn: sqlite3.Connection, serial: int) -> KeyReservation:
    """A live reservation with a record of its own, ready to be released."""
    record_id = _seed_draft(conn, f"rec-res-{serial}", 900000 + serial)
    return reserve_submission_key(
        conn,
        record_id=record_id,
        idempotency_key=f"wjsub-1-{serial:064x}",
        reserved_at=OCCURRED,
    )


def test_the_actor_bound_agrees_with_the_schema(ledger: sqlite3.Connection) -> None:
    """`MAX_ACTOR_LENGTH`, against the one column that witnesses it -- and its wiring.

    `010` guards `submission_key_releases.released_by` with the same clause it puts on an
    identifier -- "present means a claim about a human, and a blank one is worse than none"
    -- and `lifecycle`'s own `actor` columns (`003`, `004`) carry no ceiling at all. So
    this is the only schema witness the actor bound has, and `submission` is the only
    module that had to follow it (M2-710).

    **Two layers, because one would not be enough**, which is M2-710's own mutation
    finding on this exact column: a check that drove `_require_optional_identifier`
    directly would prove the *validator* agrees with the schema and say nothing about
    which validator `release_submission_key` calls. So the writer is driven for the
    wiring, and a raw INSERT for the rule.

    `MAX_ACTOR_LENGTH` is a separate constant from `MAX_IDENTIFIER_LENGTH` although both
    are 200, and this is driven from the actor one deliberately. Wire it to the identifier
    bound and nothing here changes today -- a standing risk recorded in `docs/M1-NOTES.md`
    rather than a gap any test can close while the two numbers are equal.
    """
    host = _seed_draft(ledger, "rec-actor", 100)

    for serial, n in enumerate([MAX_ACTOR_LENGTH - 1, MAX_ACTOR_LENGTH, MAX_ACTOR_LENGTH + 1]):
        value = "a" * n

        # The writer, end to end. A bare `pytest.raises(SubmissionError)` would pass
        # against a writer that let the value reach the INSERT -- the ledger raises there
        # too -- so the accepted case is what carries the weight, together with the
        # message assertion below.
        try:
            release_submission_key(
                ledger,
                _reservation(ledger, serial),
                reason="operator_abandoned",
                released_at=OCCURRED,
                released_by=value,
            )
            writer_accepts = True
        except SubmissionError as error:
            writer_accepts = False
            assert "released_by" in str(error)
            # Refused by the writer, not by the ledger underneath it.
            assert "detail withheld" not in str(error)

        # The rule, against `010`'s trigger directly.
        reservation_id = f"wjres-raw-{serial}"
        ledger.execute(
            "INSERT INTO submission_key_reservations (reservation_id, idempotency_key, "
            "forecast_record_id, reservation_seq, reserved_at_utc, created_at_utc) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (reservation_id, f"res-key-raw-{serial}", host, TS, TS),
        )
        try:
            ledger.execute(
                "INSERT INTO submission_key_releases (release_id, reservation_id, reason, "
                "released_by, note, released_at_utc, created_at_utc) "
                "VALUES (?, ?, 'operator_abandoned', ?, NULL, ?, ?)",
                (f"wjrel-raw-{serial}", reservation_id, value, TS, TS),
            )
            schema_accepts = True
        except sqlite3.IntegrityError:
            schema_accepts = False

        assert writer_accepts == schema_accepts == (n <= MAX_ACTOR_LENGTH), (
            f"released_by of length {n}: writer accepts={writer_accepts}, "
            f"010's trigger accepts={schema_accepts}"
        )


def test_the_note_bound_is_the_same_for_both_writers(ledger: sqlite3.Connection) -> None:
    """`MAX_NOTE_LENGTH`, which **no migration witnesses.**

    `010` asks only that `note` be text, and `003`/`004` put no clause on their body
    columns at all, so there is no schema half to compare against -- saying so is the
    point, not an omission. What the two writers must not do is disagree with *each
    other*: a note `lifecycle.record_approval` stores is one
    `submission.release_submission_key` must be able to store, since an operator writes
    both through the same CLI and neither has a trigger to fall back on.

    Driven through the two public writers rather than through
    `_require_optional_text(value, field, max_length=MAX_NOTE_LENGTH)` on each side. That
    spelling passed while `submission`'s call site was mutated to a bare `3999`, because
    both halves were then the same generic helper handed the same number -- the test
    asserting itself. With no schema witness available, the wiring is the only thing left
    that can actually differ.
    """
    for serial, n in enumerate([MAX_NOTE_LENGTH - 1, MAX_NOTE_LENGTH, MAX_NOTE_LENGTH + 1]):
        note = "n" * n

        record_id = _seed_draft(ledger, f"rec-note-{serial}", 100 + serial)
        record_validation(ledger, record_id=record_id, occurred_at=OCCURRED)
        try:
            record_approval(
                ledger,
                record_id=record_id,
                decision="approved",
                actor="ops",
                forecast_sha256=SHA,
                payload_sha256=PAYLOAD_SHA,
                occurred_at=OCCURRED,
                note=note,
            )
            lifecycle_accepts = True
        except LifecycleError:
            lifecycle_accepts = False

        try:
            release_submission_key(
                ledger,
                _reservation(ledger, 500 + serial),
                reason="operator_abandoned",
                released_at=OCCURRED,
                released_by="ops",
                note=note,
            )
            submission_accepts = True
        except SubmissionError:
            submission_accepts = False

        assert lifecycle_accepts == submission_accepts == (n <= MAX_NOTE_LENGTH), (
            f"note of length {n}: lifecycle accepts={lifecycle_accepts}, "
            f"submission accepts={submission_accepts}"
        )


def test_a_pre_sanitized_receipt_body_is_always_short_enough_for_the_writer() -> None:
    """`MAX_BODY_LENGTH`, as the claim that actually matters for it.

    `submission_live.storable_text` truncates where the other layers refuse, on purpose:
    after a post has happened, a value the ledger rejects is a live post with no row --
    this product's primary failure mode. So the two layers do not have equal accepted sets
    and never should. What they must share is the *number*: if `submission_live` truncated
    to a larger bound than `lifecycle` accepts, the cleaned value would be refused at the
    INSERT, which is the exact failure `storable_text` exists to prevent.

    Asserted by composition rather than by comparing constants -- feed the truncator's
    output straight into the writer's validator and require that it never raises.
    """
    for n in [MAX_BODY_LENGTH - 1, MAX_BODY_LENGTH, MAX_BODY_LENGTH + 1, MAX_BODY_LENGTH * 2]:
        cleaned = submission_live.storable_text("b" * n, MAX_BODY_LENGTH)
        assert cleaned is not None and len(cleaned) > 0, n
        # No `pytest.raises`: the assertion is that this does not raise at all.
        assert (
            lifecycle._require_optional_text(
                cleaned, "attempt.response_body", max_length=MAX_BODY_LENGTH
            )
            == cleaned
        )
