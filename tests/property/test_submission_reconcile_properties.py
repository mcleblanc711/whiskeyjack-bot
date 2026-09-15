"""Property tests for M2-713's reconciliation: the two pure checks and the writer.

CLAUDE.md's pre-review fuzz pass, applied to every new function that reads untrusted input:
never raises outside the module's own error type; no value leak in any message or rendered
traceback; and -- the property that decides whether the checks are worth anything -- an
**iff**. :func:`read_intent` and :func:`check_artifact_binds` accept exactly the draws that
describe this post and refuse every single-field departure from one. A one-sided property
("a valid intent is accepted") holds just as well for a check that accepts everything, which
is how M2-704's transposition defect survived its first property pass.

The writer fuzz draws a junk value into one field position at a time over a freshly seeded
unrecorded post, so the field validators are reached with every other input valid, and a
refusal is checked to have written nothing.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import traceback
from collections.abc import Iterator
from dataclasses import fields, replace
from datetime import datetime, timezone
from typing import Any

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from strategies import HOSTILE_TEXT

from reconciliation_rows import CONFIRMING_SNAPSHOT, seed_unrecorded_post
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    SubmissionReconciliation,
    record_submission_reconciliation,
)
from whiskeyjack_bot.submission_gateway import payload_sha256
from whiskeyjack_bot.submission_reconcile import (
    ReconciliationError,
    check_artifact_binds,
    read_intent,
)

PLANTED_SECRET = "privateFAKE123456"
SHA = "b" * 64
PAYLOAD_SHA = "d" * 64
WHEN = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
TS = "2026-09-15T09:00:00.000000+00:00"

PAYLOAD: dict[str, object] = {"question_type": "binary", "probability_yes": 0.35}
DIGEST = payload_sha256(PAYLOAD)
RECORD = {"record_id": "rec-1", "question_id": 7, "post_id": 8, "tournament_id": "32977"}

VALID_INTENT: dict[str, object] = {
    "record_id": "rec-1",
    "account_id": 42,
    "project_id": "32977",
    "question_id": 7,
    "post_id": 8,
    "payload": PAYLOAD,
    "payload_sha256": DIGEST,
    "baseline": [],
    "activation_id": "act-1",
}

# Values that are wrong for every intent field, including secret-bearing and type-confusable
# ones: `True` for an int field (bool subclasses int), a numeric string for an int, a list for
# an object, and the planted secret in every text shape.
JUNK = st.one_of(
    HOSTILE_TEXT,
    st.none(),
    st.booleans(),
    st.integers(min_value=-5, max_value=5),
    st.floats(allow_nan=False, allow_infinity=False),
    st.lists(st.integers(), max_size=2),
    st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
    st.just(PLANTED_SECRET),
    st.just({PLANTED_SECRET: PLANTED_SECRET}),
    st.just([PLANTED_SECRET]),
    st.just("7"),
    st.just(True),
    # Equal to the valid integers under `==` and refused only by an exact-type gate. Without
    # these the gate is unreachable: the mutation pass removed `type(...) is not int` from both
    # checks and every property stayed green.
    st.just(7.0),
    st.just(8.0),
)


# A field departure is only a departure if the drawn value differs from the valid one under
# the comparison the check itself makes -- `7 == 7.0` and `True == 1`, so those draws are the
# discriminating ones for the exact-type gates, and equal-valued draws of the same type are
# filtered out rather than miscounted as refusals.
def _differs(valid: object, drawn: object) -> bool:
    return type(valid) is not type(drawn) or valid != drawn


def _rendered(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _read(data: object) -> Any:
    return read_intent(data, **RECORD)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# read_intent
# --------------------------------------------------------------------------------------


@given(
    field=st.sampled_from(sorted(k for k in VALID_INTENT if k != "activation_id")),
    value=JUNK,
    mutate=st.booleans(),
)
# The draws only an exact-type gate refuses, pinned rather than left to a ~0.4%-per-draw
# chance: the property-only mutation pass saw `type(post_id) is not int` removed and 200
# random draws never produce `post_id=8.0`.
@example(field="question_id", value=7.0, mutate=True)
@example(field="post_id", value=8.0, mutate=True)
@example(field="account_id", value=42.0, mutate=True)
def test_an_intent_is_accepted_iff_it_describes_this_post(
    field: str, value: object, mutate: bool
) -> None:
    intent = dict(VALID_INTENT)
    departed = mutate and _differs(intent[field], value)
    if departed and field == "account_id" and type(value) is int and value >= 1:
        # The one field the intent does not bind to this record: which account posted is
        # compared against the poster later, so any positive account is a valid intent.
        departed = False
    if departed:
        intent[field] = value
    try:
        rendered = json.dumps(intent)
    except (TypeError, ValueError):
        return  # not representable as a journal row at all; the text property covers it
    if not departed:
        assert _read(rendered).payload_sha256 == DIGEST
        return
    with pytest.raises(ReconciliationError) as refused:
        _read(rendered)
    assert PLANTED_SECRET not in str(refused.value)
    assert PLANTED_SECRET not in _rendered(refused.value)


@given(field=st.sampled_from(sorted(VALID_INTENT)))
def test_a_missing_intent_field_is_refused_unless_it_is_unread(field: str) -> None:
    intent = {k: v for k, v in VALID_INTENT.items() if k != field}
    if field == "activation_id":
        _read(json.dumps(intent))
        return
    with pytest.raises(ReconciliationError):
        _read(json.dumps(intent))


@given(
    text=st.one_of(
        HOSTILE_TEXT,
        st.binary(max_size=8),
        st.none(),
        st.integers(),
        st.just("[" * 100_000),
        st.just('{"a": NaN}'),
        st.recursive(
            st.none() | st.booleans() | st.integers() | HOSTILE_TEXT,
            lambda children: (
                st.lists(children, max_size=3) | st.dictionaries(HOSTILE_TEXT, children, max_size=3)
            ),
            max_leaves=8,
        ).map(lambda value: json.dumps(value)),
    )
)
def test_reading_any_journal_value_raises_only_reconciliation_error(text: object) -> None:
    try:
        _read(text)
    except ReconciliationError as exc:
        # Nothing chained: a cause or an unsuppressed context reprints the value.
        assert exc.__cause__ is None and (exc.__context__ is None or exc.__suppress_context__)


# --------------------------------------------------------------------------------------
# check_artifact_binds
# --------------------------------------------------------------------------------------

VALID_RECEIPT: dict[str, object] = {
    "mode": "live",
    "idempotency_key": "wjsub-1-" + "c" * 64,
    "attempt_id": "wjlive-1-" + "e" * 64,
    "forecast_record_id": "rec-1",
    "request_payload_sha256": DIGEST,
}
BINDING = {
    "record_id": "rec-1",
    "question_id": 7,
    "key": "wjsub-1-" + "c" * 64,
    "attempt_id": "wjlive-1-" + "e" * 64,
    "digest": DIGEST,
}


def _envelope() -> dict[str, object]:
    return {"question_id": 7, "request_payload": dict(PAYLOAD), "receipt": dict(VALID_RECEIPT)}


@given(
    where=st.sampled_from(
        [*(f"receipt.{k}" for k in VALID_RECEIPT), "question_id", "request_payload", "receipt"]
    ),
    value=JUNK,
    mutate=st.booleans(),
)
# As above: a well-formed payload that is not the authorized one, and a question id equal to
# the right one under `==`, are the draws the re-hash and the exact-type gate exist for.
@example(
    where="request_payload", value={"question_type": "binary", "probability_yes": 0.4}, mutate=True
)
@example(where="question_id", value=7.0, mutate=True)
def test_an_artifact_binds_iff_it_describes_this_post(
    where: str, value: object, mutate: bool
) -> None:
    envelope = _envelope()
    if where.startswith("receipt."):
        container: dict[str, object] = envelope["receipt"]  # type: ignore[assignment]
        key = where.removeprefix("receipt.")
    else:
        container, key = envelope, where
    departed = mutate and _differs(container[key], value)
    if departed:
        container[key] = value
    if not departed:
        check_artifact_binds(envelope, **BINDING)  # type: ignore[arg-type]
        return
    with pytest.raises(ReconciliationError) as refused:
        check_artifact_binds(envelope, **BINDING)  # type: ignore[arg-type]
    assert PLANTED_SECRET not in str(refused.value)
    assert PLANTED_SECRET not in _rendered(refused.value)


@given(
    payload=st.dictionaries(HOSTILE_TEXT, JUNK, max_size=3),
    receipt=st.one_of(st.dictionaries(HOSTILE_TEXT, JUNK, max_size=3), JUNK),
    question=JUNK,
)
def test_binding_any_envelope_raises_only_reconciliation_error(
    payload: object, receipt: object, question: object
) -> None:
    try:
        check_artifact_binds(
            {"question_id": question, "request_payload": payload, "receipt": receipt},
            **BINDING,  # type: ignore[arg-type]
        )
    except ReconciliationError as exc:
        assert exc.__cause__ is None and (exc.__context__ is None or exc.__suppress_context__)


# --------------------------------------------------------------------------------------
# The writer
# --------------------------------------------------------------------------------------

_CONNECTION: sqlite3.Connection | None = None
_COUNTER = itertools.count()


@pytest.fixture(scope="module", autouse=True)
def ledger(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    global _CONNECTION
    db = tmp_path_factory.mktemp("reconcile-properties") / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    _CONNECTION = conn
    try:
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, question_id, started_at_utc, "
            "created_at_utc) VALUES ('run-1', 'asknews', 1, ?, ?)",
            (TS, TS),
        )
        yield conn
    finally:
        _CONNECTION = None
        conn.close()


def _conn() -> sqlite3.Connection:
    assert _CONNECTION is not None
    return _CONNECTION


def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
    return (
        conn.execute("SELECT count(*) FROM submission_reconciliations").fetchone()[0],
        conn.execute("SELECT count(*) FROM lifecycle_events").fetchone()[0],
    )


WRITER_JUNK = st.one_of(
    JUNK,
    st.binary(max_size=8),
    st.datetimes(),
    st.integers(min_value=2**63, max_value=2**80),
    st.sampled_from(["", "x" * 201, "x" * 4001, "a\x00b", f"\ud800{PLANTED_SECRET}", object()]),
)
_FIELDS = [f.name for f in fields(SubmissionReconciliation)]


@given(position=st.sampled_from([*_FIELDS, "record_id", "occurred_at"]), value=WRITER_JUNK)
@settings(max_examples=150)
def test_the_writer_raises_only_lifecycle_error_and_a_refusal_writes_nothing(
    position: str, value: object
) -> None:
    conn = _conn()
    serial = next(_COUNTER)
    record_id = f"rec-fuzz-{serial}"
    post = seed_unrecorded_post(
        conn,
        record_id,
        question_id=10_000 + serial,
        run_id="run-1",
        forecast_sha256=SHA,
        payload_sha256=PAYLOAD_SHA,
    )
    reconciliation = SubmissionReconciliation(
        reservation_id=post.reservation_id,
        attempt_id=post.attempt_id,
        request_payload_sha256=PAYLOAD_SHA,
        intent_event_id=post.intent_event_id,
        observed_by="chris",
        note="saw it",
        refetched_at_utc=WHEN,
        refetched_forecast_snapshot=CONFIRMING_SNAPSHOT,
    )
    arguments: dict[str, Any] = {"record_id": record_id, "occurred_at": WHEN}
    if position in arguments:
        arguments[position] = value
    else:
        reconciliation = replace(reconciliation, **{position: value})
    before = _counts(conn)
    try:
        record_submission_reconciliation(conn, reconciliation=reconciliation, **arguments)
    except LifecycleError as exc:
        assert _counts(conn) == before
        assert not conn.in_transaction
        assert PLANTED_SECRET not in str(exc)
        assert PLANTED_SECRET not in _rendered(exc)
