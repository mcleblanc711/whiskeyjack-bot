"""Property tests for the approval commands and readers (M2-701).

The CLAUDE.md pre-review fuzz pass, asserting the four invariants that pass has
historically been worth -- never raises outside the module's own error type,
replay-stability across the persisted form, no value leak in any message *or rendered
traceback*, and the ordering claim the readers make -- plus the one specific to this
item:

**A refused decision writes nothing.** The approval boundary is only as good as the
guarantee that a command which said no left the ledger where it was. That is a property
of every refusal, not of the four the unit tests name, so it is checked against arbitrary
malformed input in every parameter position.

Every property here was re-run against deliberately broken code and confirmed to fail
first; three of M1-303's ten new properties passed against the pre-fix tree
(docs/LESSONS.md, lesson 5).
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from strategies import HOSTILE_TEXT

from whiskeyjack_bot.approval import (
    ApprovalError,
    approval_history,
    approve,
    effective_approval,
    read_forecast_summary,
    reject,
)
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import record_validation

TS = "2026-08-19T00:00:00.000000+00:00"
WHEN = datetime(2026, 8, 19, tzinfo=timezone.utc)
SHA = "b" * 64
OTHER_SHA = "c" * 64
# M2-707: the digest of the payload an approval authorizes. Shape-only, as in
# `tests/unit/test_approval.py` -- `approval.py` never derives a payload, so any
# well-formed digest drives the same path a real one does.
PAYLOAD_SHA = "d" * 64
PLANTED_SECRET = "privateFAKE123456"


class HostileTimezone(tzinfo):
    """A ``tzinfo`` whose ``utcoffset`` raises, planting a secret in the message.

    A ``datetime`` carrying this passes every type gate -- it *is* exactly a ``datetime``
    -- and fails only when the writer converts it, running this method.
    """

    def utcoffset(self, dt: datetime | None) -> timedelta:
        raise ValueError(PLANTED_SECRET)

    def tzname(self, dt: datetime | None) -> str:
        return "hostile"

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


# Anything at all, including the values that have broken this code before. The commands
# accept `object` at every position a caller controls, so the strategy has to as well.
ANYTHING = st.one_of(
    HOSTILE_TEXT,
    st.none(),
    st.booleans(),
    st.integers(),
    st.integers(min_value=2**63, max_value=2**80),
    st.floats(allow_nan=True, allow_infinity=True),
    st.binary(max_size=8),
    st.lists(st.integers(), max_size=3),
    st.datetimes(),
    st.datetimes(timezones=st.just(timezone.utc)),
    st.builds(datetime, st.just(2026), st.just(8), st.just(19), tzinfo=st.just(HostileTimezone())),
    st.sampled_from([SHA, OTHER_SHA, "approved", "rejected", object()]),
)

DECIDERS = (approve, reject)

# The caller-controlled parameters both deciders take. `payload_sha256` is `approve`'s
# alone (M2-707): a rejection authorizes no payload and the function takes no such
# argument, so drawing the pair together is what keeps the new field fuzzed without
# spending half the examples on a `TypeError` that would prove nothing.
_SHARED_FIELDS = ["record_id", "actor", "note", "occurred_at", "expected_sha256"]


@st.composite
def _decider_and_field(draw: st.DrawFn, fields: list[str] = _SHARED_FIELDS) -> tuple[Any, str]:
    decider = draw(st.sampled_from(DECIDERS))
    choices = [*fields, "payload_sha256"] if decider is approve else list(fields)
    return decider, draw(st.sampled_from(choices))


def _decision_kwargs(decider: Any, record_id: str, /, **overrides: object) -> dict[str, Any]:
    """The keyword arguments one decider takes, with `payload_sha256` only for `approve`.

    Both parameters are positional-only so that `record_id` -- which is itself one of the
    fuzzed fields -- can be overridden through `**overrides` like any other.
    """
    kwargs: dict[str, Any] = {
        "record_id": record_id,
        "actor": "chris",
        "occurred_at": WHEN,
        "note": None,
        "expected_sha256": None,
    }
    if decider is approve:
        kwargs["payload_sha256"] = PAYLOAD_SHA
    kwargs.update(overrides)
    return kwargs


# --------------------------------------------------------------------------------------
# One session ledger, one record per example. The scope is deliberate for the reason
# `test_lifecycle_properties.py` gives: hypothesis's function-scoped-fixture health check
# exists because such a fixture is shared across every example anyway, and here the
# sharing is the intent. The ledger is append-only, so records cannot interfere.
# --------------------------------------------------------------------------------------

_CONNECTION: sqlite3.Connection | None = None


@pytest.fixture(scope="session", autouse=True)
def ledger(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    global _CONNECTION
    db = tmp_path_factory.mktemp("approval-properties") / "ledger.sqlite3"
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


_SERIAL = itertools.count(1)


@contextmanager
def _record(*, validated: bool = True) -> Iterator[tuple[sqlite3.Connection, str]]:
    """The session ledger and a fresh record of its own, for use in a ``@given`` body.

    The rollback in the ``finally`` keeps one example from costing the other 199: a writer
    that aborted inside its own ``BEGIN`` would otherwise strand every later example on
    this connection. It is a backstop, not the assertion -- the properties check
    ``not conn.in_transaction`` themselves, inside the block.
    """
    if _CONNECTION is None:  # pragma: no cover - the autouse fixture is always active
        raise RuntimeError("the session ledger fixture is not active")
    conn = _CONNECTION
    serial = next(_SERIAL)
    record_id = f"rec-{serial}"
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
        "forecast_sha256, attempt_id) "
        "VALUES (?, ?, 'minibench', 1, 'binary', 'draft', 'anthropic', 'claude', 'v1', 'abc', "
        "'run-1', ?, '{}', '{}', ?, ?, ?)",
        (record_id, serial, TS, TS, SHA, f"att-{serial}"),
    )
    if validated:
        record_validation(conn, record_id=record_id, occurred_at=WHEN)
    try:
        yield conn, record_id
    finally:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - the connection is already unusable
                pass


def _counts(conn: sqlite3.Connection, record_id: str) -> tuple[int, int]:
    approvals = conn.execute(
        "SELECT count(*) FROM approval_events WHERE forecast_record_id = ?", (record_id,)
    ).fetchone()[0]
    events = conn.execute(
        "SELECT count(*) FROM lifecycle_events WHERE forecast_record_id = ?", (record_id,)
    ).fetchone()[0]
    return int(approvals), int(events)


# --------------------------------------------------------------------------------------
# Invariant 1: nothing raises outside ApprovalError.
# --------------------------------------------------------------------------------------


@given(value=ANYTHING, chosen=_decider_and_field())
@settings(max_examples=120, deadline=None)
def test_a_decision_raises_only_approval_error(value: object, chosen: tuple[Any, str]) -> None:
    decider, field = chosen
    with _record() as (conn, record_id):
        try:
            decider(conn, **_decision_kwargs(decider, record_id, **{field: value}))
        except ApprovalError:
            pass
        assert not conn.in_transaction


@given(value=ANYTHING)
@settings(max_examples=60, deadline=None)
def test_the_readers_raise_only_approval_error(value: object) -> None:
    with _record() as (conn, _):
        for reader in (read_forecast_summary, approval_history, effective_approval):
            try:
                reader(conn, value)  # type: ignore[arg-type]
            except ApprovalError:
                pass
        assert not conn.in_transaction


# --------------------------------------------------------------------------------------
# Invariant 2: a refused decision writes nothing.
# --------------------------------------------------------------------------------------


@given(value=ANYTHING, chosen=_decider_and_field())
@settings(max_examples=120, deadline=None)
def test_a_refused_decision_leaves_the_ledger_where_it_was(
    value: object, chosen: tuple[Any, str]
) -> None:
    decider, field = chosen
    with _record() as (conn, record_id):
        before = _counts(conn, record_id)
        try:
            decider(conn, **_decision_kwargs(decider, record_id, **{field: value}))
        except ApprovalError:
            # Refused: not one row of either kind, and the record has not moved.
            assert _counts(conn, record_id) == before
            assert effective_approval(conn, record_id) is None
            return
        # Accepted: exactly one approval row and one lifecycle event, never a partial pair.
        assert _counts(conn, record_id) == (before[0] + 1, before[1] + 1)


@given(expected=ANYTHING)
@settings(max_examples=80, deadline=None)
def test_only_the_stored_hash_is_ever_bound(expected: object) -> None:
    """A decision binds to what the record stores, or it does not happen at all."""
    with _record() as (conn, record_id):
        try:
            recorded = approve(
                conn,
                record_id=record_id,
                actor="chris",
                occurred_at=WHEN,
                payload_sha256=PAYLOAD_SHA,
                expected_sha256=expected,  # type: ignore[arg-type]
            )
        except ApprovalError:
            assert _counts(conn, record_id) == (0, 1)
            return
        assert recorded.forecast_sha256 == SHA
        assert expected in (None, SHA)


@given(payload=ANYTHING)
@settings(max_examples=80, deadline=None)
def test_only_a_well_formed_digest_ever_binds_a_payload(payload: object) -> None:
    """M2-707's half of the same claim: an approval carries a digest or does not happen.

    The sibling above pins what a decision binds to; this pins what it *authorizes*. Both
    are written as "either it is refused and nothing was written, or the stored value is
    exactly this" rather than as a list of the shapes that should be refused -- a list is
    only ever as good as its author's imagination, and `ANYTHING` includes the four kinds
    of value that have broken this code before.

    A `reject` counterpart would be vacuous: `reject` takes no `payload_sha256`, which is
    the strongest form of "a rejection authorizes nothing" available. `011` refuses one
    written by raw SQL, and `tests/unit/test_lifecycle.py` is where that is asserted.
    """
    with _record() as (conn, record_id):
        try:
            recorded = approve(
                conn,
                record_id=record_id,
                actor="chris",
                occurred_at=WHEN,
                payload_sha256=payload,  # type: ignore[arg-type]
            )
        except ApprovalError:
            assert _counts(conn, record_id) == (0, 1)
            return
        assert recorded.payload_sha256 == payload
        assert isinstance(payload, str)
        assert len(payload) == 64 and payload == payload.lower()
        assert all(character in "0123456789abcdef" for character in payload)


# --------------------------------------------------------------------------------------
# Invariant 3: replay-stability across the persisted form.
# --------------------------------------------------------------------------------------


@given(
    note=st.one_of(st.none(), HOSTILE_TEXT),
    actor=HOSTILE_TEXT,
    decider=st.sampled_from(DECIDERS),
)
@settings(max_examples=80, deadline=None)
def test_a_stored_decision_round_trips_through_the_persisted_form(
    note: str | None, actor: str, decider: Any
) -> None:
    """The M1-305 rule: ``json.dumps(asdict(x), ensure_ascii=True, sort_keys=True)``.

    ``model_dump_json``/``repr`` are the two forms that do not survive this -- one raises
    on a lone surrogate, the other carries distinctions JSON drops.
    """
    with _record() as (conn, record_id):
        try:
            recorded = decider(conn, **_decision_kwargs(decider, record_id, actor=actor, note=note))
        except ApprovalError:
            return
        encoded = json.dumps(asdict(recorded), ensure_ascii=True, sort_keys=True)
        assert json.loads(encoded) == asdict(recorded)

        summary = read_forecast_summary(conn, record_id)
        summary_encoded = json.dumps(asdict(summary), ensure_ascii=True, sort_keys=True)
        assert json.loads(summary_encoded) == asdict(summary)

        # ... and the reader returns what was written, not what was passed in.
        assert approval_history(conn, record_id)[-1] == recorded


# --------------------------------------------------------------------------------------
# Invariant 4: no value reaches a message or a rendered traceback.
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
    chosen=_decider_and_field(["record_id", "actor", "note", "expected_sha256"]),
)
@settings(max_examples=60, deadline=None)
def test_a_rejected_value_never_reaches_the_message_or_traceback(
    text: str, chosen: tuple[Any, str]
) -> None:
    decider, field = chosen
    with _record() as (conn, record_id):
        try:
            decider(conn, **_decision_kwargs(decider, record_id, **{field: text}))
        except ApprovalError as error:
            assert PLANTED_SECRET not in str(error)
            # The traceback half is what `from None` exists for; a message-only assertion
            # passes against code that re-raises with its cause chain intact.
            assert PLANTED_SECRET not in "".join(traceback.format_exception(error))


@given(decider=st.sampled_from(DECIDERS))
@settings(max_examples=10, deadline=None)
def test_a_hostile_timezone_cannot_speak_through_a_refusal(decider: Any) -> None:
    with _record() as (conn, record_id):
        with pytest.raises(ApprovalError) as excinfo:
            decider(
                conn,
                **_decision_kwargs(
                    decider, record_id, occurred_at=datetime(2026, 8, 19, tzinfo=HostileTimezone())
                ),
            )
        assert PLANTED_SECRET not in str(excinfo.value)
        assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))


# --------------------------------------------------------------------------------------
# Invariant 5: the ordering and cardinality the readers claim.
# --------------------------------------------------------------------------------------


@given(decisions=st.lists(st.sampled_from(["approved", "rejected"]), min_size=1, max_size=8))
@settings(max_examples=120, deadline=None)
def test_a_record_never_holds_more_than_one_approval(decisions: list[str]) -> None:
    """The cardinality claim `effective_approval` rests on, over arbitrary sequences.

    `approved` is reachable only from `validated` and nothing returns an approved record
    there, so however many decisions are attempted, at most one can be an approval -- and
    every attempt after the first approval is refused rather than silently dropped.
    """
    with _record() as (conn, record_id):
        accepted: list[str] = []
        for decision in decisions:
            decider = approve if decision == "approved" else reject
            try:
                decider(conn, **_decision_kwargs(decider, record_id))
            except ApprovalError:
                continue
            accepted.append(decision)

        history = approval_history(conn, record_id)
        assert [record.decision for record in history] == accepted
        assert accepted.count("approved") <= 1
        # event_seq is the record's own history order and is strictly increasing.
        assert [record.event_seq for record in history] == sorted(
            record.event_seq for record in history
        )
        assert len({record.event_seq for record in history}) == len(history)

        # M2-707: the two decisions carry the column differently, and which one a row is
        # decides what a NULL there means -- so the asymmetry is asserted over every
        # accepted sequence rather than once, in one shape, in a unit test.
        for stored in history:
            if stored.decision == "approved":
                assert stored.payload_sha256 == PAYLOAD_SHA
            else:
                assert stored.payload_sha256 is None

        in_force = effective_approval(conn, record_id)
        if "approved" in accepted:
            assert in_force is not None and in_force.decision == "approved"
            # Nothing after an approval is recorded, so it is always the last decision.
            assert accepted[-1] == "approved"
        else:
            assert in_force is None
