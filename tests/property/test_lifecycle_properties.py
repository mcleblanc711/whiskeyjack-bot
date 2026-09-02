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
from strategies import ENCODABLE_TEXT, HOSTILE_TEXT

from whiskeyjack_bot import approval, lifecycle
from whiskeyjack_bot.approval import ApprovalError
from whiskeyjack_bot.forecast.record import _require_identifier_text as _record_identifier_text
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    LifecycleEvent,
    LifecycleEventType,
    LifecycleStatus,
    SubmissionAttempt,
    SubmissionVerification,
    current_status,
    read_history,
    record_approval,
    record_validation,
)
from whiskeyjack_bot.research.model import _require_identifier_text as _research_identifier_text

STATUSES: tuple[str, ...] = get_args(LifecycleStatus)
EVENT_TYPES: tuple[str, ...] = get_args(LifecycleEventType)
FAILURE_CODES: tuple[str, ...] = get_args(lifecycle.FailureCode)
REFETCH_OUTCOMES: tuple[str, ...] = get_args(lifecycle.RefetchOutcome)

# Canonical UTC form: 003 pins it on the columns it orders (see test_lifecycle.py).
TS = "2026-07-27T00:00:00.000000+00:00"
WHEN = datetime(2026, 7, 27, tzinfo=timezone.utc)
SHA = "b" * 64
# What a confirming refetch saw. Required for a `confirmed` outcome: a confirmation
# with nothing stored is what carries a record to `submitted` on no evidence.
SNAPSHOT = '{"probability": 0.6}'
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
        lambda: lifecycle._require_identifier(value, "field"),
        lambda: lifecycle._require_sha256(value, "field"),
        lambda: lifecycle._require_bool(value, "field"),
        lambda: lifecycle._require_optional_int(value, "field"),
        lambda: lifecycle._require_http_status(value, "field"),
        lambda: lifecycle._require_utc(value, "field"),
        lambda: lifecycle._require_aware_utc(value, "field"),
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
    # The vocabulary members as well as arbitrary junk: a fuzz that only ever sends junk
    # exercises the type gate and never the derivation the gate stands in front of.
    refetch=st.sampled_from(REFETCH_OUTCOMES) | ANYTHING,
    body=st.none() | ANYTHING,
    # Both ends of what SQLite can hold plus the HTTP range's own edges: a status outside
    # 100..599 is not a status, and one outside the signed 64-bit range raises
    # OverflowError while *binding*, which is not a sqlite3.Error.
    http_status=st.none()
    | st.sampled_from([-1, 0, 99, 100, 599, 600, 2**63 - 1, 2**63, -(2**70)])
    | ANYTHING,
    requested_at=st.just(WHEN) | ANYTHING,
    completed_at=st.just(WHEN) | ANYTHING,
    detail_code=st.none() | st.sampled_from(FAILURE_CODES) | ANYTHING,
)
@settings(max_examples=60, deadline=None)
def test_the_submission_writer_raises_only_lifecycle_error(
    attempt_id: object,
    key: object,
    payload_sha: object,
    success: object,
    refetch: object,
    body: object,
    http_status: object,
    requested_at: object,
    completed_at: object,
    detail_code: object,
) -> None:
    with _ledger() as (conn, valid_id):
        attempt = SubmissionAttempt(
            attempt_id=attempt_id,  # type: ignore[arg-type]
            idempotency_key=key,  # type: ignore[arg-type]
            requested_at_utc=requested_at,  # type: ignore[arg-type]
            completed_at_utc=completed_at,  # type: ignore[arg-type]
            request_payload_sha256=payload_sha,  # type: ignore[arg-type]
            success=success,  # type: ignore[arg-type]
            refetch_outcome=refetch,  # type: ignore[arg-type]
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


@given(
    success=st.booleans(),
    refetch=st.sampled_from(REFETCH_OUTCOMES),
)
@settings(max_examples=40, deadline=None)
def test_every_attempt_shape_has_exactly_one_recordable_outcome(
    success: bool, refetch: str
) -> None:
    """M2-711's core property: the partition is total, and the ledger agrees with it.

    For every ``(success, refetch_outcome)`` pair there is: the writer produces exactly one
    event type, the database accepts that one, and it is `submitted` only for a
    refetch-confirmed success. Totality is the half that matters here -- a pair with no
    legal event is an outcome that *happened* and cannot be recorded, and the defect this
    item closes was the dual of it: a pair recorded as an outcome it was not.

    Driven through the real writer against the real schema rather than against
    ``_DESTINATIONS``, so it fails if either layer drifts. The record is left `approved` by
    ``_approved``, which is the only status any of these events is legal from.

    **Mutation-checked**, per docs/LESSONS.md: inverting the `outcome == "absent"` arm of
    the derivation makes it fail on `(False, "absent")` and `(False, "unreadable")`; making
    `verified` unconditional makes it fail on the database's `submitted` probe. Neither is
    caught by the type-gate fuzzers above, which never reach the derivation with a valid
    vocabulary member and a valid record.
    """
    with _ledger() as (conn, valid_id):
        lifecycle.record_approval(
            conn,
            record_id=valid_id,
            decision="approved",
            actor="chris",
            forecast_sha256=SHA,
            occurred_at=WHEN,
        )
        verified = refetch == "confirmed"
        event = lifecycle.record_submission_attempt(
            conn,
            record_id=valid_id,
            attempt=SubmissionAttempt(
                attempt_id=f"att-{valid_id}",
                idempotency_key=f"idem-{valid_id}",
                requested_at_utc=WHEN,
                completed_at_utc=WHEN,
                request_payload_sha256="d" * 64,
                success=success,
                refetch_outcome=refetch,  # type: ignore[arg-type]
            ),
            occurred_at=WHEN,
            detail_code=None if (success and verified) else "refetch_missing",
        )
        # Exactly one, and the database stored it: the row is read back rather than the
        # return value trusted, because the return value is assembled from the stored row
        # only if the insert the trigger guards actually happened.
        assert event.event_type in EVENT_TYPES
        assert (event.event_type == "submitted") == (success and verified)
        assert event.event_type != "submission_failed" or refetch == "absent"
        assert lifecycle.current_status(conn, valid_id) == event.to_status
        assert lifecycle.read_history(conn, valid_id)[-1] == event
        # ... and it is resolvable exactly when it left the record where a refetch can
        # still reach it, which is the acceptance criterion's second clause.
        outstanding = lifecycle.unresolved_uncertainties(conn, valid_id)
        assert bool(outstanding) == (event.event_type == "submission_uncertain")
        assert not conn.in_transaction


class HostileAttempt(SubmissionAttempt):
    """A subclass that turns one field read into caller-supplied code."""

    def __getattribute__(self, name: str) -> object:
        if name in ("idempotency_key", "request_payload_sha256", "response_body"):
            raise RuntimeError(PLANTED_SECRET)
        return object.__getattribute__(self, name)


@given(field_values=st.lists(ANYTHING, min_size=7, max_size=7))
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
            completed_at_utc=field_values[3],  # type: ignore[arg-type]
            request_payload_sha256=field_values[4],  # type: ignore[arg-type]
            success=field_values[5],  # type: ignore[arg-type]
            refetch_outcome=field_values[6],  # type: ignore[arg-type]
        )
        try:
            lifecycle.record_submission_attempt(
                conn, record_id=valid_id, attempt=attempt, occurred_at=WHEN
            )
        except LifecycleError as exc:
            assert PLANTED_SECRET not in str(exc)
            assert PLANTED_SECRET not in "".join(traceback.format_exception(exc))
        assert not conn.in_transaction


@given(
    attempt_id=ANYTHING,
    outcome=st.sampled_from(["confirmed", "absent"]) | ANYTHING,
    observed_at=st.just(WHEN) | ANYTHING,
    snapshot=st.none() | ANYTHING,
    detail_code=st.none() | st.sampled_from(FAILURE_CODES) | ANYTHING,
)
@settings(max_examples=60, deadline=None)
def test_the_verification_writer_raises_only_lifecycle_error(
    attempt_id: object,
    outcome: object,
    observed_at: object,
    snapshot: object,
    detail_code: object,
) -> None:
    """The round-3 writer, held to the same bar as the two before it.

    Its first act is a query the *caller's* text goes into (`_require_verifiable_attempt`),
    and its second is a two-row write -- so both the reader and the rollback path see fuzzed
    input, and neither may raise anything but a LifecycleError or leave a transaction open.
    """
    with _ledger() as (conn, valid_id):
        verification = SubmissionVerification(
            submission_attempt_id=attempt_id,  # type: ignore[arg-type]
            outcome=outcome,  # type: ignore[arg-type]
            observed_at_utc=observed_at,  # type: ignore[arg-type]
            refetched_forecast_snapshot=snapshot,  # type: ignore[arg-type]
        )
        try:
            lifecycle.record_submission_verification(
                conn,
                record_id=valid_id,
                verification=verification,
                occurred_at=WHEN,
                detail_code=detail_code,  # type: ignore[arg-type]
            )
        except LifecycleError:
            pass
        assert not conn.in_transaction


class HostileVerification(SubmissionVerification):
    """The subclass trick against the writer that reads a verification between two writes."""

    def __getattribute__(self, name: str) -> object:
        if name in ("outcome", "observed_at_utc", "refetched_forecast_snapshot"):
            raise RuntimeError(PLANTED_SECRET)
        return object.__getattribute__(self, name)


@given(field_values=st.lists(ANYTHING, min_size=4, max_size=4))
@settings(max_examples=60, deadline=None)
def test_a_hostile_verification_subclass_raises_only_lifecycle_error(
    field_values: list[object],
) -> None:
    with _ledger() as (conn, valid_id):
        verification = HostileVerification(
            submission_attempt_id=field_values[0],  # type: ignore[arg-type]
            outcome=field_values[1],  # type: ignore[arg-type]
            observed_at_utc=field_values[2],  # type: ignore[arg-type]
            refetched_forecast_snapshot=field_values[3],  # type: ignore[arg-type]
        )
        try:
            lifecycle.record_submission_verification(
                conn, record_id=valid_id, verification=verification, occurred_at=WHEN
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


# The one rule the transition table cannot express, because it turns on the record's
# history rather than on its current status: the refetch's two events exist only once
# there is an uncertainty for them to resolve. A walk that ignored it would be rejected by
# the database and the fuzzer would be testing its own generator.
#
# There is no rule in the other direction. Round 3 blocked a further attempt while an
# uncertainty stood; round 4 withdrew that (finding 1), so a walk may hold several
# `submission_uncertain` events -- which is why `_insert_event` mints a fresh attempt row
# per submission event rather than reusing one from the fixture.
_AFTER_UNCERTAINTY = frozenset({"submission_confirmed", "submission_disconfirmed"})


@st.composite
def legal_walks(draw: st.DrawFn) -> tuple[tuple[str, str, str], ...]:
    """A random walk of legal transitions starting from ``draft``.

    Drawn from ``_DESTINATIONS`` rather than written out, so a transition added to the
    machine is fuzzed the day it is added.
    """
    status = "draft"
    uncertain = False
    walk: list[tuple[str, str, str]] = []
    for _ in range(draw(st.integers(min_value=0, max_value=8))):
        options = sorted(
            (event_type, to_status)
            for (event_type, from_status), to_status in lifecycle._DESTINATIONS.items()
            if from_status == status and (uncertain or event_type not in _AFTER_UNCERTAINTY)
        )
        if not options:
            break
        event_type, to_status = draw(st.sampled_from(options))
        walk.append((event_type, status, to_status))
        uncertain = uncertain or event_type == "submission_uncertain"
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
def test_no_detail_row_backs_more_than_one_event(
    walk: tuple[tuple[str, str, str], ...],
) -> None:
    """Round 3, finding 2, as a property rather than as the one example that reproduced it.

    The reproduction was two `rejected` events citing one approval decision, but the claim
    is about every link column at once: a lifecycle history is a sequence of distinct
    facts, and two events resting on one detail row means the log counts a thing that
    happened once as having happened twice.

    Worth stating over walks rather than trusting the indexes: this property is what makes
    the fixture's own shortcut visible. Reusing a stored approval row for a second
    rejection is precisely what the writer must not do either, and the first run of this
    file after the index landed failed here, in the fixture.
    """
    links = (
        "approval_event_id",
        "submission_attempt_id",
        "submission_verification_id",
        "resolution_event_id",
        "score_event_id",
    )
    with _ledger(validated=False) as (conn, record_id):
        detail = _detail_rows(conn, record_id)
        for index, (event_type, from_status, to_status) in enumerate(walk, start=1):
            _insert_event(conn, record_id, index, event_type, from_status, to_status, detail)

        for link in links:
            cited = [
                row[0]
                for row in conn.execute(
                    f"SELECT {link} FROM lifecycle_events WHERE forecast_record_id = ? "
                    f"AND {link} IS NOT NULL",
                    (record_id,),
                )
            ]
            assert len(cited) == len(set(cited)), link


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
        submission_verification_id=None,
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


@pytest.mark.parametrize(
    ("guarded", "recording_events"),
    [
        ("approved", ("approved",)),
        # Two events mean "this forecast is on the platform": the attempt that posted and
        # was confirmed on the spot, and the refetch that confirmed one left uncertain.
        ("submitted", ("submitted", "submission_confirmed")),
    ],
)
def test_a_guarded_state_is_unreachable_without_its_own_event(
    guarded: str, recording_events: tuple[str, ...]
) -> None:
    """Exhaustive over the graph, not sampled.

    Breadth-first from ``draft`` over every legal transition *except* the ones that record
    the guarded state. If the state is still reachable, then some other event can produce
    it and "an approved state always has an approval event" is false. Because a record is
    born a ``draft`` and the schema admits no other way to change its status, this
    settles the acceptance criterion for the state machine as a whole.

    ``recording_events`` is written out rather than derived from ``_DESTINATIONS``, and
    that is the point: deriving it would remove every edge into the state and make the
    result vacuous. Spelled out, a future event type that reaches ``submitted`` fails this
    test until someone decides deliberately that it belongs on the list -- which is how
    round 3's `submission_confirmed` got there.
    """
    reachable = {"draft"}
    queue = deque(["draft"])
    while queue:
        status = queue.popleft()
        for (event_type, from_status), to_status in lifecycle._DESTINATIONS.items():
            if from_status != status or event_type in recording_events:
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
    # attempt_id comes off the same serial as record_id: migration 004 requires it and
    # indexes it UNIQUE where not null, and one database serves the whole session here, so
    # every record in a run needs its own.
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
        "forecast_sha256, attempt_id) "
        "VALUES (?, ?, 'minibench', 1, 'binary', 'draft', 'anthropic', 'claude', "
        "'v1', 'abc', 'run-1', ?, '{}', '{}', ?, ?, ?)",
        (record_id, serial, TS, TS, SHA, f"att-{serial}"),
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
    attempts = {
        "att_ok": f"att-ok-{record_id}",
        "att_unsure": f"att-unsure-{record_id}",
        "att_bad": f"att-bad-{record_id}",
    }
    for key, success, verified, refetch in (
        ("att_ok", 1, 1, "confirmed"),
        ("att_unsure", 1, 0, "absent"),
        ("att_bad", 0, 0, "absent"),
    ):
        attempt_id = attempts[key]
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
                "d" * 64,
                success,
                verified,
                refetch,
                TS,
            ),
        )
    # The resolution must name this record's own question, not a constant: a row may point
    # at the right record and still resolve a different question.
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
    # What a refetch of the uncertain attempt could have seen. Storable up front: the
    # verification table only requires its attempt to exist, and it is the *link* that
    # requires the uncertainty.
    verifications = {
        f"verified_{outcome}": conn.execute(
            "INSERT INTO submission_verifications (submission_attempt_id, outcome, "
            "observed_at_utc, refetched_forecast_snapshot, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (attempts["att_unsure"], outcome, TS, snapshot, TS),
        ).lastrowid
        # A confirmation must carry what it saw; an `absent` one has nothing to store.
        for outcome, snapshot in (("confirmed", SNAPSHOT), ("absent", None))
    }
    return {
        "approved": approved,
        "rejected": rejected,
        "att_ok": attempts["att_ok"],
        "att_unsure": attempts["att_unsure"],
        "att_bad": attempts["att_bad"],
        "resolved": resolution,
        "scored": score,
        **verifications,
    }


def _uncited_attempt(
    conn: sqlite3.Connection,
    record_id: str,
    attempt_id: object,
    success: int,
    verified: int,
    refetch: str,
) -> object:
    """The fixture's attempt while nothing cites it, then fresh ones shaped like it.

    A detail row backs at most one event (round 3's index), and since round 4 withdrew the
    retry block a walk may hold several `submission_uncertain` events -- so the second one
    needs a receipt of its own. That is also the truthful shape: two uncertain posts are two
    requests, and the fixture reusing one row for both was the same shortcut the schema
    forbids the writer.

    The first uncertain event keeps the fixture's `att_unsure`, which is what the
    verification rows in `_detail_rows` were written against.
    """
    if (
        conn.execute(
            "SELECT 1 FROM lifecycle_events WHERE submission_attempt_id = ?", (attempt_id,)
        ).fetchone()
        is None
    ):
        return attempt_id
    fresh = f"{attempt_id}-{next(_SERIAL)}"
    conn.execute(
        "INSERT INTO submission_attempts (attempt_id, forecast_record_id, idempotency_key, "
        "requested_at_utc, completed_at_utc, request_payload_sha256, success, "
        "verified_by_refetch, refetch_outcome, created_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (fresh, record_id, f"idem-{fresh}", TS, TS, "d" * 64, success, verified, refetch, TS),
    )
    return fresh


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
        "submission_verification_id": None,
        "resolution_event_id": None,
        "score_event_id": None,
    }
    if event_type in ("approved", "rejected"):
        # A fresh decision per event, not the one _detail_rows made. `rejected` is a
        # self-transition, so a walk can hold several -- and since round 3 a detail row may
        # back only one event, which is the truthful shape anyway: two rejections are two
        # decisions, each with its own actor, note and timestamp.
        links["approval_event_id"] = conn.execute(
            "INSERT INTO approval_events (forecast_record_id, decision, actor, "
            "forecast_sha256, created_at_utc) VALUES (?, ?, 'chris', ?, ?)",
            (record_id, event_type, SHA, TS),
        ).lastrowid
    elif event_type == "submitted":
        links["submission_attempt_id"] = _uncited_attempt(
            conn, record_id, detail["att_ok"], 1, 1, "confirmed"
        )
    elif event_type == "submission_uncertain":
        links["submission_attempt_id"] = _uncited_attempt(
            conn, record_id, detail["att_unsure"], 1, 0, "absent"
        )
    elif event_type == "submission_failed":
        links["submission_attempt_id"] = _uncited_attempt(
            conn, record_id, detail["att_bad"], 0, 0, "absent"
        )
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
                "internal_error"
                if to_status == "failed" or event_type == "submission_uncertain"
                else None
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


# --------------------------------------------------------------------------------------
# M1-606: the pre-forecast failure writer, over the same four invariants.
#
# Written before round 1 and re-run against deliberately broken code, per CLAUDE.md. That
# second half is not ceremony: M1-303 shipped ten new properties of which three passed
# against the pre-fix implementation, so a property nobody has seen fail is evidence of
# nothing. What each of these was checked against is named in its docstring.
# --------------------------------------------------------------------------------------


PRE_FORECAST_EVENT_TYPES: tuple[str, ...] = get_args(lifecycle.PreForecastEventType)
PRE_FORECAST_FAILURE_CODES: tuple[str, ...] = get_args(lifecycle.PreForecastFailureCode)

_ATTEMPT_SERIAL = itertools.count(1)


def _fresh_attempt() -> str:
    """An attempt_id no earlier example has used.

    One database serves the whole session, and 004 makes an attempt_id's question,
    tournament and sequence sticky for the life of the table. Reusing one across examples
    would make each draw depend on its predecessors, so a shrink would report the wrong
    input.
    """
    return f"prop-att-{next(_ATTEMPT_SERIAL)}"


@given(
    attempt_id=ANYTHING,
    question_id=ANYTHING,
    tournament_id=ANYTHING,
    event_type=st.sampled_from(PRE_FORECAST_EVENT_TYPES) | ANYTHING,
    detail_code=st.sampled_from(PRE_FORECAST_FAILURE_CODES) | ANYTHING,
    occurred_at=st.just(WHEN) | ANYTHING,
    retrieval_run_id=st.none() | st.just("run-1") | ANYTHING,
)
@settings(max_examples=80, deadline=None)
def test_the_pre_forecast_writer_raises_only_lifecycle_error(
    attempt_id: object,
    question_id: object,
    tournament_id: object,
    event_type: object,
    detail_code: object,
    occurred_at: object,
    retrieval_run_id: object,
) -> None:
    """Invariant 1, over every parameter at once.

    Each argument is fuzzed independently rather than one-at-a-time-around-a-good-record,
    because the interesting failures are combinations: a valid `event_type` with a hostile
    `occurred_at`, an out-of-range `question_id` that only raises while *binding*
    (OverflowError, which is not a `sqlite3.Error`), a blob `attempt_id` that the schema
    accepts and the reader cannot decode.

    **What this property does not discriminate, measured rather than assumed.** It was
    mutation-checked against five deliberate breakages and caught none of the two it was
    first documented as catching: with `_require_int` returning its argument unchecked,
    and with `_require_identifier` weakened back to `_require_text`, the bad value still
    arrives as a `LifecycleError` -- because `_insert` and `_fetch_*` wrap
    `sqlite3.Error` *and* `OverflowError`, so a validator hole simply moves the refusal
    from the writer to the database without changing its type. That is the invariant
    holding, not the test being weak, and it is exactly why it is not the only property
    here: `test_a_refused_write_is_refused_with_a_field_level_message` below is the one
    that notices, and the docstring is written from what was observed rather than from
    what seemed likely.
    """
    conn = _CONNECTION
    assert conn is not None
    try:
        lifecycle.record_pre_forecast_failure(
            conn,
            attempt_id=attempt_id,  # type: ignore[arg-type]
            question_id=question_id,  # type: ignore[arg-type]
            tournament_id=tournament_id,  # type: ignore[arg-type]
            event_type=event_type,  # type: ignore[arg-type]
            detail_code=detail_code,  # type: ignore[arg-type]
            occurred_at=occurred_at,  # type: ignore[arg-type]
            retrieval_run_id=retrieval_run_id,  # type: ignore[arg-type]
        )
    except LifecycleError:
        pass
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    # A writer that leaves its transaction open strands every later example on this
    # connection, so this is asserted rather than only tidied up above.
    assert not conn.in_transaction


# Bogus *per field*, not one shared list. The first version shared one, and hypothesis
# immediately drew `question_id=42` -- a perfectly good question id -- and failed on a
# value the writer is right to accept. A hostile-input set that is not relative to the
# field it targets tests the strategy, not the code.
_TEXT_BOGUS: list[object] = [None, "", "   ", "\t\n", "\u00a0", 42, 1.5, b"x", object()]
_BOGUS_FOR_FIELD: dict[str, list[object]] = {
    "attempt_id": [*_TEXT_BOGUS, "a" * 201],
    "tournament_id": [*_TEXT_BOGUS, "t" * 201],
    "question_id": [None, "", "   ", "100", 1.5, b"1", object()],
    "event_type": [*_TEXT_BOGUS, "approved", "RESEARCH_FAILED", "submission_failed"],
    "detail_code": [*_TEXT_BOGUS, "refetch_mismatch", "refetch_missing", "nope"],
}


@st.composite
def _a_bogus_field(draw: st.DrawFn) -> tuple[str, object]:
    field = draw(st.sampled_from(sorted(_BOGUS_FOR_FIELD)))
    return field, draw(st.sampled_from(_BOGUS_FOR_FIELD[field]))


@given(case=_a_bogus_field())
@settings(max_examples=80, deadline=None)
def test_a_refused_write_is_refused_with_a_field_level_message(
    case: tuple[str, object],
) -> None:
    """A caller mistake is refused *by the writer*, naming the field -- not by the database.

    This is the property that discriminates, and it exists because the only-LifecycleError
    invariant above provably does not. `_insert` wraps every `sqlite3.Error` into one
    opaque message -- "the ledger rejected this write (detail withheld: a database message
    can echo stored values)" -- which is correct for it to do and useless to a caller. So
    a validator hole is invisible to a test that only checks the exception *type*: the bad
    value reaches the trigger, the trigger refuses it, and the caller gets a
    `LifecycleError` that says nothing about which field was wrong.

    The claim here is therefore stronger: for inputs the writer is contracted to judge for
    itself, the refusal must arrive before any statement runs and must name the field.
    That is the M1-303 round-4 lesson as a property -- refuse caller mistakes *before*
    reaching the expensive layer, as this module's own error, with something actionable in
    it.

    Confirmed to fail against broken code, both mutations the invariant above missed:
    `_require_int` returning its argument unchecked, and `_require_identifier` weakened
    back to `_require_text`.
    """
    field, value = case
    conn = _CONNECTION
    assert conn is not None
    kwargs: dict[str, object] = {
        "attempt_id": _fresh_attempt(),
        "question_id": 100,
        "tournament_id": "minibench",
        "event_type": "research_failed",
        "detail_code": "provider_error",
        "occurred_at": WHEN,
    }
    kwargs[field] = value
    before = conn.execute("SELECT count(*) FROM pipeline_failure_events").fetchone()[0]
    try:
        lifecycle.record_pre_forecast_failure(conn, **kwargs)  # type: ignore[arg-type]
    except LifecycleError as error:
        assert "the ledger rejected this write" not in str(error), (
            f"{field} was refused by the database, not by the writer: {error}"
        )
        assert field in str(error), f"the refusal does not name {field}: {error}"
    else:  # pragma: no cover - reached only if a bogus value is accepted
        raise AssertionError(f"a bogus {field} was accepted")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    assert conn.execute("SELECT count(*) FROM pipeline_failure_events").fetchone()[0] == before


@given(attempt_id=ANYTHING)
@settings(max_examples=60, deadline=None)
def test_the_pre_forecast_reader_raises_only_lifecycle_error(attempt_id: object) -> None:
    conn = _CONNECTION
    assert conn is not None
    try:
        result = lifecycle.read_pipeline_failure_events(conn, attempt_id)  # type: ignore[arg-type]
    except LifecycleError:
        return
    # An accepted identifier must produce a well-formed answer, not merely not raise.
    assert isinstance(result, tuple)
    assert all(isinstance(event, lifecycle.PreForecastFailure) for event in result)


# --------------------------------------------------------------------------------------
# Invariant 2: a total order wherever ordering is claimed.
# --------------------------------------------------------------------------------------


@given(
    codes=st.lists(st.sampled_from(PRE_FORECAST_FAILURE_CODES), min_size=1, max_size=6),
    other_codes=st.lists(st.sampled_from(PRE_FORECAST_FAILURE_CODES), min_size=1, max_size=4),
)
@settings(max_examples=40, deadline=None)
def test_each_attempts_failures_are_contiguous_from_one_and_read_back_in_order(
    codes: list[str], other_codes: list[str]
) -> None:
    """Invariant 2: `event_seq` is `1..n` per attempt, and interleaving cannot disturb it.

    Two attempts are written *interleaved* rather than one after the other, which is the
    part that discriminates. A `_next_pipeline_failure_seq` that took `max(event_seq)`
    over the whole table -- the obvious wrong implementation, and the one a
    copy-of-`_next_seq` becomes if the WHERE clause is dropped -- passes a
    one-attempt-at-a-time test and fails here on the second attempt's first event.

    Contiguity from 1 is what makes a *missing* failure detectable at all: a gap is
    unforgeable on an append-only table, so `[1, 3]` is evidence, not noise.

    Confirmed to fail against two broken trees: `_next_pipeline_failure_seq` with its
    `WHERE attempt_id = ?` removed, and `_PRE_FORECAST_FAILURE_COLUMNS` with `event_seq`
    and `question_id` transposed. The second is worth naming because it is the one a
    reordered `SELECT` produces and the *replay* property does **not** catch it: the
    writer and the reader index the same wrong column, so their two objects agree with
    each other perfectly. Only a test that knows what the sequence should *be* sees it.
    """
    conn = _CONNECTION
    assert conn is not None
    first, second = _fresh_attempt(), _fresh_attempt()

    def write(attempt_id: str, code: str) -> lifecycle.PreForecastFailure:
        return lifecycle.record_pre_forecast_failure(
            conn,
            attempt_id=attempt_id,
            question_id=100,
            tournament_id="minibench",
            event_type="research_failed",
            detail_code=code,  # type: ignore[arg-type]
            occurred_at=WHEN,
        )

    written: dict[str, list[lifecycle.PreForecastFailure]] = {first: [], second: []}
    for index in range(max(len(codes), len(other_codes))):
        if index < len(codes):
            written[first].append(write(first, codes[index]))
        if index < len(other_codes):
            written[second].append(write(second, other_codes[index]))

    for attempt_id, events in written.items():
        stored = lifecycle.read_pipeline_failure_events(conn, attempt_id)
        assert stored == tuple(events)
        assert [event.event_seq for event in stored] == list(range(1, len(events) + 1))
        # A total order, not merely a sorted list: event_id and event_seq agree, so the
        # append order the ledger records and the order the reader returns cannot diverge.
        assert [event.event_id for event in stored] == sorted(e.event_id for e in stored)


# --------------------------------------------------------------------------------------
# Invariant 3: replay-stability across the persisted form.
# --------------------------------------------------------------------------------------


@given(
    attempt_id=HOSTILE_TEXT,
    tournament_id=HOSTILE_TEXT,
    run_id=st.none() | HOSTILE_TEXT,
    occurred=HOSTILE_TEXT,
    question_id=st.integers(min_value=-(2**40), max_value=2**40),
    event_type=st.sampled_from(PRE_FORECAST_EVENT_TYPES),
    detail_code=st.sampled_from(PRE_FORECAST_FAILURE_CODES),
)
@settings(max_examples=60, deadline=None)
def test_pre_forecast_failures_survive_the_persisted_json_form_unchanged(
    attempt_id: str,
    tournament_id: str,
    run_id: str | None,
    occurred: str,
    question_id: int,
    event_type: str,
    detail_code: str,
) -> None:
    """Invariant 3, on the value object rather than on the database round trip.

    The claim is idempotence of the *encoding*, for the reason
    `test_events_survive_the_persisted_json_form_unchanged` sets out at length: a
    surrogate pair and its astral scalar are distinct Python strings that
    `ensure_ascii=True` then `json.loads` collapses into one, so asserting the object
    survives unchanged would be stricter than storage can honour and would make a
    replayed run disagree with the live one.

    Not `model_dump_json()` and not `repr`: the first raises on the lone surrogates that
    arrive from provider JSON, the second preserves distinctions JSON drops.
    """
    event = lifecycle.PreForecastFailure(
        event_id=1,
        attempt_id=attempt_id,
        event_seq=1,
        question_id=question_id,
        tournament_id=tournament_id,
        event_type=event_type,  # type: ignore[arg-type]
        detail_code=detail_code,  # type: ignore[arg-type]
        retrieval_run_id=run_id,
        occurred_at_utc=occurred,
        created_at_utc=TS,
    )
    encoded = json.dumps(asdict(event), ensure_ascii=True, sort_keys=True)
    assert json.dumps(json.loads(encoded), ensure_ascii=True, sort_keys=True) == encoded
    # The encoding is total: every field is JSON-native, so no input to this dataclass
    # makes the persisted form unrepresentable.
    assert set(json.loads(encoded)) == set(asdict(event))


@given(
    attempt_id=st.text(
        st.characters(exclude_categories=["Cc", "Cs"], exclude_characters="\x00"),
        min_size=1,
        max_size=24,
    ).filter(lambda text: text.strip() != ""),
    tournament_id=st.text(
        st.characters(exclude_categories=["Cc", "Cs"], exclude_characters="\x00"),
        min_size=1,
        max_size=24,
    ).filter(lambda text: text.strip() != ""),
)
@settings(max_examples=40, deadline=None)
def test_a_stored_failure_replays_identically_through_the_ledger(
    attempt_id: str, tournament_id: str
) -> None:
    """The other half of invariant 3: what the writer returned is what the reader returns.

    Storage is the step that can lose a distinction -- SQLite normalises nothing, but the
    text goes out as UTF-8 and comes back decoded -- so this is the round trip the value
    object test above deliberately does not make. Restricted to identifiers the schema
    actually accepts, because what is being checked here is fidelity, not refusal; the
    refusal path is invariant 1's.

    Confirmed to fail against broken code: `_next_pipeline_failure_seq` with its
    `WHERE attempt_id = ?` removed, where the writer's returned `event_seq` and the one
    the reader finds under this attempt diverge.

    It does **not** catch a transposed `SELECT` column order, and the docstring says so
    rather than claiming a coverage it was measured not to have: writer and reader read
    the same wrong position, so the two objects agree. The contiguity property above is
    what notices that one.
    """
    conn = _CONNECTION
    assert conn is not None
    unique = f"{_fresh_attempt()}-{attempt_id}"
    written = lifecycle.record_pre_forecast_failure(
        conn,
        attempt_id=unique,
        question_id=100,
        tournament_id=tournament_id,
        event_type="research_failed",
        detail_code="provider_error",
        occurred_at=WHEN,
    )
    (read_back,) = lifecycle.read_pipeline_failure_events(conn, unique)
    assert read_back == written
    assert json.dumps(asdict(read_back), ensure_ascii=True, sort_keys=True) == json.dumps(
        asdict(written), ensure_ascii=True, sort_keys=True
    )


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
            f"  {PLANTED_SECRET}  ",
        ]
    ),
    field=st.sampled_from(["attempt_id", "tournament_id", "retrieval_run_id"]),
)
@settings(max_examples=40, deadline=None)
def test_no_pre_forecast_value_reaches_the_message_or_traceback(text: str, field: str) -> None:
    """Invariant 4, on the message *and* the rendered traceback.

    The traceback half is what `from None` exists for and what a message-only assertion
    misses: a chained exception reprints the value it was raised from when the traceback
    is formatted, so a writer that sanitises its own string and re-raises normally still
    leaks. Over-long and lone-surrogate variants are included because those take different
    branches inside `_require_text` -- the length check and the encode probe -- and each
    branch is its own message.
    """
    conn = _CONNECTION
    assert conn is not None
    kwargs: dict[str, object] = {
        "attempt_id": _fresh_attempt(),
        "question_id": 100,
        "tournament_id": "minibench",
        "event_type": "research_failed",
        "detail_code": "provider_error",
        "occurred_at": WHEN,
    }
    kwargs[field] = text
    try:
        lifecycle.record_pre_forecast_failure(conn, **kwargs)  # type: ignore[arg-type]
    except LifecycleError as error:
        rendered = "".join(traceback.format_exception(error))
        assert PLANTED_SECRET not in str(error)
        assert PLANTED_SECRET not in rendered
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")


@given(attempt_id=st.sampled_from([PLANTED_SECRET, PLANTED_SECRET * 400]))
@settings(max_examples=10, deadline=None)
def test_no_rejected_reader_value_reaches_the_message_or_traceback(attempt_id: str) -> None:
    conn = _CONNECTION
    assert conn is not None
    try:
        lifecycle.read_pipeline_failure_events(conn, attempt_id)
    except LifecycleError as error:
        rendered = "".join(traceback.format_exception(error))
        assert PLANTED_SECRET not in str(error)
        assert PLANTED_SECRET not in rendered


# --------------------------------------------------------------------------------------
# M1-607: the writer's definition of "identifier" is the schema's, over fuzzed input.
# --------------------------------------------------------------------------------------
#
# The unit suite asserts this over the 29 whitespace codepoints, which is the set the
# defect actually lived in. These fuzz the rest of the input space, because the failure
# mode being guarded against is not "the set is wrong" -- it is "two layers hold two
# definitions and nobody compared them", and that can differ anywhere, not only on
# whitespace. M1-603 spent five rounds on one function for exactly this shape.

IDENTIFIER_TEXT = st.one_of(
    ENCODABLE_TEXT,
    # Around the 200-character ceiling, where the two layers count differently.
    st.text(st.characters(exclude_categories=["Cs"]), min_size=195, max_size=205),
    # Control characters are deliberately *not* excluded: U+0000 is the one input where
    # SQLite's length() and Python's len() disagree about the same string.
    st.text(st.characters(exclude_categories=["Cs"]), max_size=8),
    st.sampled_from(
        ["", " ", "\t\n", "\xa0", "\x00", "a\x00b", "x" * 200, "x" * 201, "\x00" + "y" * 205]
    ),
)


def _schema_accepts_record_id(conn: sqlite3.Connection, value: str) -> bool:
    """Whether `forecast_records`' trigger admits `value` as a record_id.

    Probed inside a transaction that is always rolled back: `forecast_records` is
    append-only, so an accepted row could not be cleaned up afterwards and the next
    example would collide on the primary key rather than testing anything.

    `UnicodeEncodeError` counts as a refusal, not an error, because it is one -- sqlite3
    encodes text parameters as UTF-8 and cannot bind a lone surrogate. The writer refuses
    the same input through `_require_text`'s encode probe, which is the agreement being
    asserted.
    """
    serial = next(_SERIAL)
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO forecast_records ("
            "record_id, question_id, tournament_id, forecast_version, question_type, "
            "status, model_provider, model_name, prompt_version, prompt_sha256, "
            "retrieval_run_id, generated_at_utc, final_prediction_json, record_json, "
            "created_at_utc, forecast_sha256, attempt_id) "
            "VALUES (?, ?, 'minibench', 1, 'binary', 'draft', 'anthropic', 'claude', "
            "'v1', 'abc', 'run-1', ?, '{}', '{}', ?, ?, ?)",
            (value, 900000 + serial, TS, TS, SHA, f"att-probe-{serial}"),
        )
    except (sqlite3.IntegrityError, UnicodeEncodeError, sqlite3.InterfaceError):
        return False
    finally:
        conn.execute("ROLLBACK")
    return True


@given(value=IDENTIFIER_TEXT)
@settings(max_examples=120, deadline=None)
def test_the_writer_and_the_schema_admit_exactly_the_same_identifiers(value: str) -> None:
    """The two-layer agreement, fuzzed rather than sampled.

    A writer that accepted what the schema refuses fails at the statement with an opaque
    message instead of a field-level one. A writer that refused what the schema accepts is
    worse: raw SQL could then mint a `record_id` no reader can ever look up, on a table
    that is append-only, so the row is unreadable for good.

    Asserting equality in both directions is what makes this more than a restatement of
    the guard. `record_id` is the column where the two definitions must match exactly --
    the ceiling included, which is why the strategy above straddles 200.
    """
    with _ledger() as (conn, _):
        try:
            lifecycle._require_identifier(value, "record_id")
        except LifecycleError:
            writer_accepts = False
        else:
            writer_accepts = True

        assert writer_accepts == _schema_accepts_record_id(conn, value)
        assert not conn.in_transaction


@given(value=ANYTHING)
def test_the_two_module_copies_of_the_identifier_rule_agree(value: object) -> None:
    """`approval.py` holds its own copy of `_require_identifier`; it must not drift.

    The copy exists because each module owns its sanitized exception type, so a shared
    helper would have to raise one module's error inside the other. That is the right
    trade, but a second copy of a rule is a second thing that can be wrong -- and the two
    are used against the *same column*, `forecast_records.record_id`, so a disagreement
    means one of them contradicts the schema.
    """
    try:
        lifecycle._require_identifier(value, "record_id")
    except LifecycleError:
        lifecycle_accepts = False
    else:
        lifecycle_accepts = True

    try:
        approval._require_identifier(value, "record_id")
    except ApprovalError:
        approval_accepts = False
    else:
        approval_accepts = True

    assert lifecycle_accepts == approval_accepts


@given(value=IDENTIFIER_TEXT)
@settings(max_examples=120, deadline=None)
def test_the_record_and_research_model_identifier_rules_agree(value: str) -> None:
    """`forecast.record` (M1-610) holds its own copy of `research.model`'s rule (M1-607).

    Both are used against `retrieval_run_id` -- `forecast_records.retrieval_run_id` and
    `research_runs.retrieval_run_id` -- and `006`'s own header documents that the former has
    no trigger clause of its own, covered only transitively through the foreign key into the
    latter. So the two Python copies are the only witness this column's blank/NUL rule has;
    `test_shared_bounds.py`'s `record_id`/`tournament_id`/`attempt_id` parity test has a real
    trigger to compare against and does not need this.
    """
    try:
        _record_identifier_text(value)
    except ValueError:
        record_accepts = False
    else:
        record_accepts = True

    try:
        _research_identifier_text(value)
    except ValueError:
        research_accepts = False
    else:
        research_accepts = True

    assert record_accepts == research_accepts


# Every message `_require_identifier` and the `_require_text` beneath it can produce,
# with only the field name interpolated. Spelled out rather than derived from the source,
# so a new branch that built its message from the *value* fails this instead of being
# described by it.
def _permitted_refusals(field: str) -> frozenset[str]:
    return frozenset(
        {
            f"{field} must be a non-empty string",
            f"{field} is longer than the 200-character limit",
            f"{field} contains characters that cannot be stored "
            "(detail withheld: it can echo the value)",
            f"{field} must not be blank",
            f"{field} must not contain a NUL character",
        }
    )


@given(value=ANYTHING)
def test_an_identifier_refusal_says_one_of_a_fixed_set_of_things(value: object) -> None:
    """Invariant 4 on the new validator, as a closed message set.

    The obvious spelling -- "the value is not a substring of the message" -- is vacuous
    here and worse than nothing: a one-character whitespace identifier is a substring of
    every message that contains a space, so the property would fail on correct code and
    then be "fixed" into asserting less. Closing the message set instead says the thing
    that actually matters, and says it for inputs a substring check cannot speak about at
    all.

    The rendered traceback is checked too, because the leak channels this project has
    actually found were indirect: a pydantic `loc` carrying an int key, logging's `%d`
    printing its raw argument past caplog, a chained `__cause__` reprinting the input
    through a rendered traceback. `_require_text` uses `from None` for exactly that.
    """
    try:
        lifecycle._require_identifier(value, "record_id")
    except LifecycleError as error:
        assert str(error) in _permitted_refusals("record_id")
        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        # No chained cause: a re-raised UnicodeEncodeError would reprint the character.
        assert "During handling of the above exception" not in rendered
        assert "The above exception was the direct cause" not in rendered
