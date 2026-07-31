"""Property tests for the lifecycle state machine and its writers (M1-603).

The CLAUDE.md pre-review fuzz pass. The four invariants it asserts -- never raises
outside the module's own error type, a total order wherever ordering is claimed,
replay-stability across the persisted form, and no value leak in any message -- are the
ones cross-model review has historically found by hand, one round per property. M1-305's
tiebreak took five rounds on a single function for three of them.

There is a fifth here, specific to this item: the acceptance criterion itself is a
reachability property of the transition graph, so it is checked exhaustively rather than
by example. If no path from ``draft`` reaches ``approved`` without traversing an
``approved`` edge, then an approved state without its event record is not a bug that
testing might miss -- it is not expressible.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import traceback
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, get_args

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from strategies import HOSTILE_TEXT

from whiskeyjack_bot import lifecycle
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    LifecycleEvent,
    LifecycleEventType,
    LifecycleStatus,
    SubmissionAttempt,
    current_status,
    read_history,
    record_approval,
    record_validation,
)

STATUSES: tuple[str, ...] = get_args(LifecycleStatus)
EVENT_TYPES: tuple[str, ...] = get_args(LifecycleEventType)
FAILURE_CODES: tuple[str, ...] = get_args(lifecycle.FailureCode)

TS = "2026-07-27T00:00:00+00:00"
WHEN = datetime(2026, 7, 27, tzinfo=timezone.utc)
SHA = "b" * 64
PLANTED_SECRET = "privateFAKE123456"


class HostileTimezone(tzinfo):
    """A `tzinfo` whose `utcoffset` raises, planting a secret in the message.

    A `datetime` carrying this passes every type gate the module has -- it *is* exactly a
    `datetime` -- and only fails when the writer converts it, running this method. GPT
    round 1 found the unguarded version leaking the message and traceback.
    """

    def utcoffset(self, dt: datetime | None) -> timedelta:
        raise ValueError(PLANTED_SECRET)

    def tzname(self, dt: datetime | None) -> str:
        return "hostile"

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


class FlakyTimezone(tzinfo):
    """A `tzinfo` that answers the aware-ness check and then fails the conversion.

    `_require_utc` reads the offset twice -- once itself, once inside `astimezone` -- so
    the two calls need separate guards, and only a *stateful* tzinfo distinguishes them.
    A single check plus an unguarded conversion would pass every stateless probe and
    still leak here.
    """

    def __init__(self) -> None:
        self._answered = False

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if self._answered:
            raise ValueError(PLANTED_SECRET)
        self._answered = True
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "flaky"

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


# Anything at all, including the values that have broken this code before. `_require_*`
# takes `object`, so the strategy has to as well -- a fuzzer restricted to str would
# never reach the branches that exist because a caller can pass something else.
ANYTHING = st.one_of(
    HOSTILE_TEXT,
    st.none(),
    st.booleans(),
    st.integers(),
    # Beyond SQLite's signed 64-bit INTEGER, where sqlite3 raises OverflowError while
    # binding the parameter -- not a sqlite3.Error, so not caught by the obvious wrapper.
    st.integers(min_value=2**63, max_value=2**80),
    st.integers(min_value=-(2**80), max_value=-(2**63) - 1),
    st.floats(allow_nan=True, allow_infinity=True),
    st.binary(max_size=8),
    st.lists(st.integers(), max_size=3),
    st.datetimes(),
    st.datetimes(timezones=st.just(timezone.utc)),
    # Aware datetimes whose tzinfo is caller-supplied code, not a fixed offset.
    st.builds(datetime, st.just(2026), st.just(7), st.just(27), tzinfo=st.just(HostileTimezone())),
    st.builds(datetime, st.just(2026), st.just(7), st.just(27), tzinfo=st.builds(FlakyTimezone)),
    st.sampled_from([SHA, "draft", "approved", "validated", object()]),
)


# --------------------------------------------------------------------------------------
# Invariant 1: nothing raises outside LifecycleError.
# --------------------------------------------------------------------------------------


@given(value=ANYTHING)
def test_validators_raise_only_lifecycle_error(value: object) -> None:
    validators = (
        lambda: lifecycle._require_text(value, "field", max_length=32),
        lambda: lifecycle._require_optional_text(value, "field", max_length=32),
        lambda: lifecycle._require_sha256(value, "field"),
        lambda: lifecycle._require_bool(value, "field"),
        lambda: lifecycle._require_optional_int(value, "field"),
        lambda: lifecycle._require_utc(value, "field"),
        lambda: lifecycle._require_optional_utc(value, "field"),
        lambda: lifecycle._require_member(value, lifecycle._STATUSES, "field"),
    )
    for validator in validators:
        try:
            validator()
        except LifecycleError:
            pass


@given(
    record_id=ANYTHING,
    actor=ANYTHING,
    note=st.none() | ANYTHING,
    digest=ANYTHING,
    occurred_at=ANYTHING,
    decision=st.sampled_from(["approved", "rejected"]) | ANYTHING,
)
@settings(max_examples=60, deadline=None)
def test_the_approval_writer_raises_only_lifecycle_error(
    record_id: object,
    actor: object,
    note: object,
    digest: object,
    occurred_at: object,
    decision: object,
) -> None:
    # Against a real ledger holding a real validated record, so a call that happens to be
    # well formed takes the success path rather than being rejected on a technicality.
    with _ledger() as (conn, valid_id):
        try:
            record_approval(
                conn,
                record_id=record_id,  # type: ignore[arg-type]
                decision=decision,  # type: ignore[arg-type]
                actor=actor,  # type: ignore[arg-type]
                forecast_sha256=digest,  # type: ignore[arg-type]
                occurred_at=occurred_at,  # type: ignore[arg-type]
                note=note,  # type: ignore[arg-type]
            )
        except LifecycleError:
            pass
        # Whatever happened, the connection must not be left mid-transaction: a writer
        # that aborts inside its own BEGIN would strand every later write on this
        # connection.
        assert not conn.in_transaction
        assert current_status(conn, valid_id) in STATUSES


@given(
    attempt_id=ANYTHING,
    key=ANYTHING,
    payload_sha=ANYTHING,
    success=ANYTHING,
    verified=ANYTHING,
    body=st.none() | ANYTHING,
    http_status=st.none() | ANYTHING,
    requested_at=st.just(WHEN) | ANYTHING,
    detail_code=st.none() | st.sampled_from(FAILURE_CODES) | ANYTHING,
)
@settings(max_examples=60, deadline=None)
def test_the_submission_writer_raises_only_lifecycle_error(
    attempt_id: object,
    key: object,
    payload_sha: object,
    success: object,
    verified: object,
    body: object,
    http_status: object,
    requested_at: object,
    detail_code: object,
) -> None:
    with _ledger() as (conn, valid_id):
        attempt = SubmissionAttempt(
            attempt_id=attempt_id,  # type: ignore[arg-type]
            idempotency_key=key,  # type: ignore[arg-type]
            requested_at_utc=requested_at,  # type: ignore[arg-type]
            request_payload_sha256=payload_sha,  # type: ignore[arg-type]
            success=success,  # type: ignore[arg-type]
            verified_by_refetch=verified,  # type: ignore[arg-type]
            response_body=body,  # type: ignore[arg-type]
            http_status=http_status,  # type: ignore[arg-type]
        )
        try:
            lifecycle.record_submission_attempt(
                conn,
                record_id=valid_id,
                attempt=attempt,
                occurred_at=WHEN,
                detail_code=detail_code,  # type: ignore[arg-type]
            )
        except LifecycleError:
            pass
        assert not conn.in_transaction


class HostileAttempt(SubmissionAttempt):
    """A subclass that turns one field read into caller-supplied code."""

    def __getattribute__(self, name: str) -> object:
        if name in ("idempotency_key", "request_payload_sha256", "response_body"):
            raise RuntimeError(PLANTED_SECRET)
        return object.__getattribute__(self, name)


@given(field_values=st.lists(ANYTHING, min_size=6, max_size=6))
@settings(max_examples=60, deadline=None)
def test_a_hostile_attempt_subclass_raises_only_lifecycle_error(
    field_values: list[object],
) -> None:
    """`isinstance` admitted subclasses; every field read then ran foreign code.

    Fuzzed over the field values as well, so the refusal cannot depend on the payload
    being well formed -- it has to come from the type gate, before any attribute is read.
    (GPT round 1, finding 3.)
    """
    with _ledger() as (conn, valid_id):
        attempt = HostileAttempt(
            attempt_id=field_values[0],  # type: ignore[arg-type]
            idempotency_key=field_values[1],  # type: ignore[arg-type]
            requested_at_utc=field_values[2],  # type: ignore[arg-type]
            request_payload_sha256=field_values[3],  # type: ignore[arg-type]
            success=field_values[4],  # type: ignore[arg-type]
            verified_by_refetch=field_values[5],  # type: ignore[arg-type]
        )
        try:
            lifecycle.record_submission_attempt(
                conn, record_id=valid_id, attempt=attempt, occurred_at=WHEN
            )
        except LifecycleError as exc:
            assert PLANTED_SECRET not in str(exc)
            assert PLANTED_SECRET not in "".join(traceback.format_exception(exc))
        assert not conn.in_transaction


@given(record_id=ANYTHING)
@settings(max_examples=60, deadline=None)
def test_the_readers_raise_only_lifecycle_error(record_id: object) -> None:
    with _ledger() as (conn, _):
        for reader in (current_status, read_history):
            try:
                reader(conn, record_id)  # type: ignore[arg-type]
            except LifecycleError:
                pass


# --------------------------------------------------------------------------------------
# Invariant 2: a total order wherever ordering is claimed.
# --------------------------------------------------------------------------------------


@st.composite
def legal_walks(draw: st.DrawFn) -> tuple[tuple[str, str, str], ...]:
    """A random walk of legal transitions starting from ``draft``.

    Drawn from ``_DESTINATIONS`` rather than written out, so a transition added to the
    machine is fuzzed the day it is added.
    """
    status = "draft"
    walk: list[tuple[str, str, str]] = []
    for _ in range(draw(st.integers(min_value=0, max_value=8))):
        options = sorted(
            (event_type, to_status)
            for (event_type, from_status), to_status in lifecycle._DESTINATIONS.items()
            if from_status == status
        )
        if not options:
            break
        event_type, to_status = draw(st.sampled_from(options))
        walk.append((event_type, status, to_status))
        status = to_status
    return tuple(walk)


@given(walk=legal_walks())
@settings(max_examples=100, deadline=None)
def test_the_derived_status_is_the_fold_over_the_event_sequence(
    walk: tuple[tuple[str, str, str], ...],
) -> None:
    """The database's derived status equals a pure fold over the same events.

    This is the whole claim the design rests on -- that "where is this record now" can be
    answered from the append-only log alone -- expressed over every walk the machine
    permits rather than the one the happy-path test takes.
    """
    with _ledger(validated=False) as (conn, record_id):
        detail = _detail_rows(conn, record_id)
        for index, (event_type, from_status, to_status) in enumerate(walk, start=1):
            _insert_event(conn, record_id, index, event_type, from_status, to_status, detail)

        expected = walk[-1][2] if walk else "draft"
        assert current_status(conn, record_id) == expected

        history = read_history(conn, record_id)
        # Contiguous from 1 and strictly increasing: a gap would be a lost event, and the
        # order is what makes the fold above well defined.
        assert [event.event_seq for event in history] == list(range(1, len(walk) + 1))
        assert [event.event_type for event in history] == [step[0] for step in walk]


@given(walk=legal_walks())
@settings(max_examples=100, deadline=None)
def test_each_events_from_status_is_the_previous_events_destination(
    walk: tuple[tuple[str, str, str], ...],
) -> None:
    # The chain has no seams: the log is a path through the graph, not a bag of events.
    previous = "draft"
    for event_type, from_status, to_status in walk:
        assert from_status == previous
        assert (event_type, from_status) in lifecycle._DESTINATIONS
        previous = to_status


# --------------------------------------------------------------------------------------
# Invariant 3: replay-stability across the persisted form.
# --------------------------------------------------------------------------------------


@given(
    record_id=HOSTILE_TEXT,
    attempt_id=st.none() | HOSTILE_TEXT,
    occurred=HOSTILE_TEXT,
    event_type=st.sampled_from(EVENT_TYPES),
    from_status=st.sampled_from(STATUSES),
    to_status=st.sampled_from(STATUSES),
)
def test_events_survive_the_persisted_json_form_unchanged(
    record_id: str,
    attempt_id: str | None,
    occurred: str,
    event_type: str,
    from_status: str,
    to_status: str,
) -> None:
    """The **persisted form** is stable under a round trip -- not the in-memory object.

    The distinction is the whole M1-305 lesson and this fuzzer re-derived it: the first
    version of this test asserted ``json.loads(dumps(asdict(e))) == asdict(e)``, and
    hypothesis produced ``'\\ud83d\\ude00'`` -- the UTF-16 surrogate-pair spelling of an
    astral scalar. ``ensure_ascii=True`` escapes both units, and ``json.loads``
    recombines them into the single scalar, so the object does *not* survive unchanged.
    That is correct behaviour, not a defect: storage genuinely cannot tell the two
    spellings apart, so a comparison that can is stricter than replay can honour and
    would make a replayed run disagree with the live one.

    So the claim is idempotence of the encoding. Two events with the same persisted form
    stay the same after any number of round trips, which is exactly what a replay needs
    and no more. Not ``model_dump_json()`` and not ``repr``: the first raises on the lone
    surrogates that arrive from provider JSON, the second preserves distinctions JSON
    drops.
    """
    event = LifecycleEvent(
        event_id=1,
        forecast_record_id=record_id,
        event_seq=1,
        event_type=event_type,  # type: ignore[arg-type]
        from_status=from_status,  # type: ignore[arg-type]
        to_status=to_status,  # type: ignore[arg-type]
        detail_code=None,
        approval_event_id=None,
        submission_attempt_id=attempt_id,
        resolution_event_id=None,
        score_event_id=None,
        occurred_at_utc=occurred,
        created_at_utc=TS,
    )
    encoded = json.dumps(asdict(event), ensure_ascii=True, sort_keys=True)
    # Encoding the decoded form reproduces the same bytes, so a replay comparison cannot
    # turn on which side of a round trip a value was read from.
    assert json.dumps(json.loads(encoded), ensure_ascii=True, sort_keys=True) == encoded
    # The encoding is total: every field of every event is JSON-native, so there is no
    # input to this dataclass that makes the persisted form unrepresentable. (This is
    # what `model_dump_json()` would not give -- it raises on a lone surrogate.)
    assert set(json.loads(encoded)) == set(asdict(event))


# --------------------------------------------------------------------------------------
# Invariant 4: no value leak in any message.
# --------------------------------------------------------------------------------------


@given(
    text=st.sampled_from(
        [
            PLANTED_SECRET,
            PLANTED_SECRET * 400,
            f"\ud800{PLANTED_SECRET}",
            f"{PLANTED_SECRET}\N{ZERO WIDTH SPACE}",
        ]
    ),
    field=st.sampled_from(["record_id", "actor", "note", "forecast_sha256"]),
)
@settings(max_examples=40, deadline=None)
def test_a_rejected_value_never_reaches_the_message_or_traceback(text: str, field: str) -> None:
    with _ledger() as (conn, valid_id):
        kwargs: dict[str, Any] = {
            "record_id": valid_id,
            "decision": "approved",
            "actor": "chris",
            "forecast_sha256": SHA,
            "occurred_at": WHEN,
        }
        kwargs[field] = text
        try:
            record_approval(conn, **kwargs)
        except LifecycleError as error:
            assert PLANTED_SECRET not in str(error)
            assert PLANTED_SECRET not in "".join(traceback.format_exception(error))


@given(value=ANYTHING)
def test_a_rejected_value_never_reaches_a_validator_message(value: object) -> None:
    # The validators are where an unvetted value is closest to an error string, so the
    # no-echo claim is checked against the rendering of the value itself rather than a
    # single planted marker.
    rendered = repr(value)
    for name, validator in (
        ("text", lambda: lifecycle._require_text(value, "field", max_length=8)),
        ("sha256", lambda: lifecycle._require_sha256(value, "field")),
        ("utc", lambda: lifecycle._require_utc(value, "field")),
        ("member", lambda: lifecycle._require_member(value, lifecycle._STATUSES, "field")),
    ):
        try:
            validator()
        except LifecycleError as error:
            message = str(error)
            assert "field" in message, name
            # A short value can coincide with ordinary message text ("field", "8"), so
            # the assertion is over renderings long enough to be a real echo.
            if len(rendered) > 12:
                assert rendered not in message, name
                assert rendered not in "".join(traceback.format_exception(error)), name


# --------------------------------------------------------------------------------------
# Invariant 5: the acceptance criterion as a reachability property.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("guarded", ["approved", "submitted"])
def test_a_guarded_state_is_unreachable_without_its_own_event(guarded: str) -> None:
    """Exhaustive over the graph, not sampled.

    Breadth-first from ``draft`` over every legal transition *except* the one that names
    the guarded state. If the state is still reachable, then some other event can produce
    it and "an approved state always has an approval event" is false. Because a record is
    born a ``draft`` and the schema admits no other way to change its status, this
    settles the acceptance criterion for the state machine as a whole.
    """
    reachable = {"draft"}
    queue = deque(["draft"])
    while queue:
        status = queue.popleft()
        for (event_type, from_status), to_status in lifecycle._DESTINATIONS.items():
            if from_status != status or event_type == guarded:
                continue
            if to_status not in reachable:
                reachable.add(to_status)
                queue.append(to_status)
    assert guarded not in reachable


def test_the_transition_table_covers_every_event_type() -> None:
    # An event type in the vocabulary with no transition would be dead: writable by
    # nothing, and silently so.
    assert {event_type for event_type, _, _ in lifecycle._LEGAL_TRANSITIONS} == set(EVENT_TYPES)


def test_failed_has_no_outgoing_transition() -> None:
    # Terminal by omission, deliberately: a retry is a new forecast version (M1-602).
    assert not [key for key in lifecycle._DESTINATIONS if key[1] == "failed"]


# --------------------------------------------------------------------------------------
# Fixtures.
#
# One ledger for the whole session, and a *fresh record* per example rather than a fresh
# database. Creating the database per example meant three migrations and a WAL setup for
# every one of ~280 examples, which took the property file alone past two minutes; the
# ledger is append-only, so records cannot interfere with each other and there is nothing
# a new database would isolate that a new record does not.
#
# Session-scoped deliberately: hypothesis's function_scoped_fixture health check exists
# because a function-scoped fixture is set up once for the whole @given run, so every
# example silently shares it. Here that sharing is the intent, and saying so in the scope
# is more honest than suppressing the warning.
#
# The fixture is `autouse` and publishes its connection through the module-level holder
# below, because a @given body cannot reach a fixture it did not declare as a parameter --
# and declaring one would be the function-scoped sharing the health check warns about.
# --------------------------------------------------------------------------------------

_CONNECTION: sqlite3.Connection | None = None


@pytest.fixture(scope="session", autouse=True)
def ledger(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    global _CONNECTION
    db = tmp_path_factory.mktemp("lifecycle-properties") / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    _CONNECTION = conn
    try:
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, question_id, "
            "started_at_utc, created_at_utc) VALUES ('run-1', 'asknews', 100, ?, ?)",
            (TS, TS),
        )
        yield conn
    finally:
        _CONNECTION = None
        conn.close()


@contextmanager
def _ledger(*, validated: bool = True) -> Iterator[tuple[sqlite3.Connection, str]]:
    """The session ledger and a record of its own, for use inside a ``@given`` body.

    ``validated=False`` yields the untouched ``draft`` the walk fuzzers need; the default
    yields one already walked to ``validated``, which is what makes it approvable and so
    lets the writer fuzzers reach the success path rather than being turned away at the
    first gate.

    The rollback in the ``finally`` is the thing that keeps one example from costing the
    other 199: a writer that somehow aborted inside its own ``BEGIN`` would otherwise
    strand every later example on the same connection. It is a backstop, not the
    assertion -- the fuzzers check ``not conn.in_transaction`` themselves, inside the
    block, so a writer that leaks a transaction still fails rather than being tidied up.
    """
    if _CONNECTION is None:  # pragma: no cover - the autouse fixture is always active
        raise RuntimeError("the session ledger fixture is not active")
    conn = _CONNECTION
    try:
        yield conn, _new_record(conn, validated=validated)
    finally:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - the connection is already unusable
                pass


_SERIAL = itertools.count(1)


def _new_record(conn: sqlite3.Connection, *, validated: bool = True) -> str:
    """A fresh draft record, optionally already walked to ``validated``.

    The walks start from ``draft``, so they need the untouched record; the writer fuzzers
    need one that is approvable, so they take the default.
    """
    serial = next(_SERIAL)
    record_id = f"rec-{serial}"
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
        "forecast_sha256) "
        "VALUES (?, ?, 'minibench', 1, 'binary', 'draft', 'anthropic', 'claude', "
        "'v1', 'abc', 'run-1', ?, '{}', '{}', ?, ?)",
        (record_id, serial, TS, TS, SHA),
    )
    if validated:
        record_validation(conn, record_id=record_id, occurred_at=WHEN)
    return record_id


def _detail_rows(conn: sqlite3.Connection, record_id: str) -> dict[str, object]:
    """One valid detail row of every kind, so only the transition can be at fault.

    The attempt identifiers are per record. One database serves the whole session, and
    ``submission_attempts.attempt_id`` is the primary key with ``idempotency_key`` unique
    over the table -- so a constant ``att-ok`` would make every example after the first
    die on an IntegrityError from the fixture rather than from anything under test.
    """
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
    attempts = {"att_ok": f"att-ok-{record_id}", "att_bad": f"att-bad-{record_id}"}
    for key, verified in (("att_ok", 1), ("att_bad", 0)):
        attempt_id = attempts[key]
        conn.execute(
            "INSERT INTO submission_attempts (attempt_id, forecast_record_id, idempotency_key, "
            "requested_at_utc, request_payload_sha256, success, verified_by_refetch, "
            "created_at_utc) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (attempt_id, record_id, f"idem-{attempt_id}", TS, "d" * 64, verified, TS),
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
        "att_ok": attempts["att_ok"],
        "att_bad": attempts["att_bad"],
        "resolved": resolution,
        "scored": score,
    }


def _insert_event(
    conn: sqlite3.Connection,
    record_id: str,
    seq: int,
    event_type: str,
    from_status: str,
    to_status: str,
    detail: dict[str, object],
) -> None:
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
