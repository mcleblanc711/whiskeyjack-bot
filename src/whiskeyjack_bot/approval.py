"""Approve or reject a stored forecast, as append-only ledger events (M2-701).

M1-603 built the whole approval *mechanism* -- the `approval_events` table, the
``approved``/``rejected`` lifecycle vocabulary, :func:`lifecycle.record_approval`, and
migration 003's triggers binding an approval to the record's exact ``forecast_sha256``.
It deliberately withheld the command layer, because shipping it there would have put a
reachable approval path in the tree ahead of this item (``lifecycle.py``'s module
docstring says so). This module is that layer, and it adds two things the writer alone
does not give an operator:

**What is being approved.** :func:`read_forecast_summary` is what the command prints
before it writes anything. An approval that binds to a hash the operator never saw is an
attribution claim with nothing behind it, and the hash is the only part of the record
that a later reader can check a decision against.

**Whether it is approved now.** :func:`effective_approval` answers "is this exact
forecast approved?", which is the question M2-704 has to ask before it posts. The answer
is derived from the *lifecycle event*, never from an ``approval_events`` row alone: a row
that no event cites did not move the record, and reporting it as an approval would credit
a decision the ledger never acted on. See :func:`approval_history`.

A record holds **at most one** ``approved`` event, and that is a property of the state
machine rather than a rule enforced here. ``lifecycle._LEGAL_TRANSITIONS`` admits
``approved`` only from ``validated``, and nothing carries an approved record back to
``validated`` -- ``rejected`` itself requires ``from_status = 'validated'``. So
:func:`effective_approval` returns one row or none, and a ledger holding two is corrupt
rather than ambiguous; it says so instead of picking one. Rejections are unbounded: a
record can be rejected repeatedly and then approved, because a rejection records a
decision without moving the record.

**"Changed forecast invalidates prior approval"** (the acceptance criterion) holds in two
layers. Structurally, a forecast version is immutable and a changed forecast is a *new*
record (M1-602), whose ``record_id`` has no approval at all. At the command, an operator
who supplies ``expected_sha256`` -- the hash they actually reviewed -- is refused when it
does not match what the record stores, and nothing is written.

Error hygiene follows ``ConfigError``/``LedgerError``/``LifecycleError``: an
:class:`ApprovalError` never echoes a stored or caller-supplied value, sanitizing raises
use ``from None``, and every malformed shape arrives as an :class:`ApprovalError` rather
than a raw ``sqlite3``/``UnicodeEncodeError`` or a :class:`LifecycleError` from the layer
below.

Purely local file I/O: nothing here contacts Metaculus. The submission gateway is M2-703
and M2-704, and ``submission.enabled``/``dry_run`` remain what they are until then.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import cast, get_args

from whiskeyjack_bot.lifecycle import (
    ApprovalDecision,
    LifecycleError,
    LifecycleStatus,
    current_status,
    record_approval,
    transaction,
)

# The closed vocabulary of `approval_events.decision`, taken from the module that owns it
# rather than respelled here: a second spelling is a second thing to keep in agreement.
_DECISIONS: frozenset[str] = frozenset(get_args(ApprovalDecision))

_HEX_DIGITS: frozenset[str] = frozenset("0123456789abcdef")

# Matches `lifecycle._MAX_IDENTIFIER`. This module validates only the inputs it owns --
# see `_record_decision` -- so this bound exists to keep a hostile `record_id` away from
# sqlite3's parameter binding, not to restate the writer's field rules.
_MAX_IDENTIFIER = 200


class ApprovalError(Exception):
    """An approval decision cannot be recorded, or the ledger cannot be read.

    Same hygiene rule as :class:`lifecycle.LifecycleError`: the message never echoes a
    stored value, a caller-supplied field value, or a database error's text, and
    sanitizing raises use ``from None``.

    A :class:`LifecycleError` from the layer below is re-raised as this type **with its
    message preserved** (:func:`_wrap_lifecycle`). That is deliberate rather than lazy:
    ``LifecycleError``'s own contract guarantees its text names no value, and its
    hash-mismatch message is the one thing that makes a refused approval actionable.
    Replacing it with a constant would satisfy the letter of the module-own-error rule
    while destroying what the operator needs.
    """


@dataclass(frozen=True)
class ForecastSummary:
    """What the operator is shown before a decision is written.

    Every field is JSON-native (``str``/``int``/``None``), so the persisted form used for
    replay comparison is ``json.dumps(dataclasses.asdict(summary), ensure_ascii=True,
    sort_keys=True)`` -- the M1-305 rule that survives the lone surrogates
    ``str.encode('utf-8')`` does not.

    ``status`` is the *derived* current status (``lifecycle.current_status``), never
    ``forecast_records.status``, which is status-at-creation and pinned to ``draft``.

    ``forecast_sha256`` is ``None`` only for a record written before migration 003, which
    keeps an honest NULL and cannot be approved at all.
    """

    record_id: str
    question_id: int
    tournament_id: str
    forecast_version: int
    question_type: str
    status: LifecycleStatus
    forecast_sha256: str | None
    generated_at_utc: str


@dataclass(frozen=True)
class ApprovalRecord:
    """One approval decision that actually moved (or was recorded against) a record.

    The ``approval_events`` columns, plus the ``event_seq`` and ``occurred_at_utc`` of the
    ``lifecycle_events`` row that cites it. Both timestamps are kept, because they mean
    different things: ``occurred_at_utc`` is when the decision was made (caller-supplied,
    so a replayed run reproduces it) and ``created_at_utc`` is when the ledger stored it
    (writer-owned). "actor/timestamp/note are retained" is this dataclass.

    Constructed only from a row the join in :func:`approval_history` returned, so a
    decision with no lifecycle event can never appear as one.
    """

    event_id: int
    forecast_record_id: str
    decision: ApprovalDecision
    actor: str
    forecast_sha256: str
    note: str | None
    occurred_at_utc: str
    created_at_utc: str
    event_seq: int


_SUMMARY_COLUMNS = (
    "record_id, question_id, tournament_id, forecast_version, question_type, "
    "forecast_sha256, generated_at_utc"
)

# Spelled out rather than `SELECT *` for the reason lifecycle's column lists are: the row
# mappers below index positionally, so this order is part of the contract and a later
# ALTER TABLE must not be able to reorder it silently.
_APPROVAL_COLUMNS = (
    "a.event_id, a.forecast_record_id, a.decision, a.actor, a.forecast_sha256, "
    "a.note, e.occurred_at_utc, a.created_at_utc, e.event_seq"
)

# The join that makes a decision *count*. `e.forecast_record_id = a.forecast_record_id` is
# implied by 003's trigger, which refuses an event citing another record's approval row --
# and is written out anyway, because a constraint that holds only because another
# constraint holds is one refactor away from holding for no reason. 003 makes the same
# argument about its own paired probes.
_APPROVAL_JOIN = (
    "FROM approval_events a JOIN lifecycle_events e "
    "ON e.approval_event_id = a.event_id "
    "AND e.forecast_record_id = a.forecast_record_id"
)


def read_forecast_summary(conn: sqlite3.Connection, record_id: str) -> ForecastSummary:
    """Return what a decision on this record would be binding to.

    Raises when ``record_id`` names no stored forecast record, matching
    ``lifecycle.current_status`` and ``lifecycle.read_history``: the three are the read
    seam, and a caller that cannot tell "nothing recorded yet" from "no such record"
    would report the first while looking at the second.
    """
    identifier = _require_text(record_id, "record_id", max_length=_MAX_IDENTIFIER)
    row = _fetch_one(
        conn,
        f"SELECT {_SUMMARY_COLUMNS} FROM forecast_records WHERE record_id = ?",
        (identifier,),
    )
    if row is None:
        raise ApprovalError("record_id does not name a stored forecast record")
    try:
        status = current_status(conn, identifier)
    except LifecycleError as exc:
        raise _wrap_lifecycle(exc) from None
    return ForecastSummary(
        record_id=_stored_text(row[0], "record_id"),
        question_id=_stored_int(row[1], "question_id"),
        tournament_id=_stored_text(row[2], "tournament_id"),
        forecast_version=_stored_int(row[3], "forecast_version"),
        question_type=_stored_text(row[4], "question_type"),
        status=status,
        forecast_sha256=(None if row[5] is None else _stored_text(row[5], "forecast_sha256")),
        generated_at_utc=_stored_text(row[6], "generated_at_utc"),
    )


def approval_history(conn: sqlite3.Connection, record_id: str) -> tuple[ApprovalRecord, ...]:
    """Return every approval decision recorded against this record, in append order.

    Ordered by ``event_seq``, which is the record's own history order, not by
    ``approval_events.event_id`` -- the two agree today and only one of them is the
    ledger's stated ordering.

    **Only decisions the ledger acted on appear here.** An ``approval_events`` row that no
    ``lifecycle_events`` row cites is not part of the record's history: nothing moved, and
    ``current_status`` cannot see it (``tests/unit/test_lifecycle.py`` proves such a row
    can be written by raw SQL). Reporting it would mean crediting a decision that never
    became state.

    An unknown ``record_id`` raises, for :func:`read_forecast_summary`'s reason.
    """
    identifier = _require_text(record_id, "record_id", max_length=_MAX_IDENTIFIER)
    _require_stored_record(conn, identifier)
    rows = _fetch_all(
        conn,
        f"SELECT {_APPROVAL_COLUMNS} {_APPROVAL_JOIN} "
        "WHERE a.forecast_record_id = ? ORDER BY e.event_seq",
        (identifier,),
    )
    return tuple(_approval_from_row(row) for row in rows)


def effective_approval(conn: sqlite3.Connection, record_id: str) -> ApprovalRecord | None:
    """Return the approval in force for this record, or ``None``.

    This is the question M2-704 asks before posting: not "was this ever approved" but "is
    this exact forecast approved", and the hash on the returned record is what a submitted
    payload must still match.

    Returns at most one, **by construction rather than by choosing**. ``approved`` is
    reachable only from ``validated`` and nothing returns an approved record to
    ``validated``, so a second approval event cannot be appended. A ledger that holds two
    was not written through this schema's triggers; that is a corrupt history rather than
    an ambiguous one, and this raises instead of silently answering with one of them.

    A rejection is never "in force". It records a decision and leaves the record
    ``validated``, so a rejected record is simply unapproved -- and may be approved later.
    :func:`approval_history` is where a rejection is read.
    """
    approvals = [
        record for record in approval_history(conn, record_id) if record.decision == "approved"
    ]
    if not approvals:
        return None
    if len(approvals) > 1:
        raise ApprovalError(
            "this forecast record holds more than one approval event, which the lifecycle "
            "state machine cannot produce; the stored history is inconsistent"
        )
    return approvals[0]


def approve(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    actor: str,
    occurred_at: datetime,
    note: str | None = None,
    expected_sha256: str | None = None,
) -> ApprovalRecord:
    """Record an approval: ``validated -> approved``. See :func:`_record_decision`."""
    return _record_decision(
        conn,
        record_id=record_id,
        decision="approved",
        actor=actor,
        occurred_at=occurred_at,
        note=note,
        expected_sha256=expected_sha256,
    )


def reject(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    actor: str,
    occurred_at: datetime,
    note: str | None = None,
    expected_sha256: str | None = None,
) -> ApprovalRecord:
    """Record a rejection, which leaves the record ``validated``.

    A rejection does not move the record and does not make it terminal: the last valid
    record stays intact, per the handoff's failure boundaries, and the record can be
    approved afterwards. It carries no ``detail_code`` -- a rejection is a decision, not a
    failure, and its account is the actor and note stored here (M1-603 owner decision).
    """
    return _record_decision(
        conn,
        record_id=record_id,
        decision="rejected",
        actor=actor,
        occurred_at=occurred_at,
        note=note,
        expected_sha256=expected_sha256,
    )


def _record_decision(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    decision: ApprovalDecision,
    actor: str,
    occurred_at: datetime,
    note: str | None,
    expected_sha256: str | None,
) -> ApprovalRecord:
    """Bind a decision to the record's stored hash and append it, atomically.

    ``expected_sha256`` is the hash the operator reviewed. Supplying it is optional and
    checking it is not: a mismatch is refused and **nothing is written**, because the
    forecast they approved is not the forecast stored under this identifier. Omitting it
    is the ordinary path, and the caller is then relying on the ``record_id`` alone --
    which is why the command layer prints the hash it bound to.

    The read and the write are **one transaction**. Reading the stored hash outside it and
    passing the value back into :func:`lifecycle.record_approval` would make that
    function's binding check a comparison of a value against itself; ``BEGIN IMMEDIATE``
    (via :func:`lifecycle.transaction`) makes the pair a single unit instead.
    :func:`lifecycle.transaction` nests through a SAVEPOINT, so ``record_approval``'s own
    block inside this one is safe.

    Only ``expected_sha256`` is validated here -- it is this module's own parameter, and
    the writer never sees it. ``record_id``, ``actor``, ``note`` and ``occurred_at`` are
    left to ``record_approval``'s validators rather than restated: two sets of field rules
    for one column is how M1-603's round 5 defect happened, one layer up.
    """
    expected = None if expected_sha256 is None else _require_sha256(expected_sha256)
    try:
        with transaction(conn):
            summary = read_forecast_summary(conn, record_id)
            stored = summary.forecast_sha256
            if stored is None:
                raise ApprovalError(
                    "this forecast record stores no content hash, so no approval decision "
                    "can bind to it"
                )
            if expected is not None and expected != stored:
                # Neither hash is printed: one is a stored value, and printing the other
                # would let a caller confirm a guess against it. Same rule, and the same
                # wording, as lifecycle._require_hash_binds.
                raise ApprovalError(
                    "forecast_sha256 does not match the stored hash of this forecast "
                    "record; the forecast changed and any prior approval no longer binds"
                )
            event = record_approval(
                conn,
                record_id=record_id,
                decision=decision,
                actor=actor,
                forecast_sha256=stored,
                occurred_at=occurred_at,
                note=note,
            )
            if event.approval_event_id is None:  # pragma: no cover - 003 forbids it
                raise ApprovalError("the recorded approval decision could not be read back")
            return _read_approval(conn, event.approval_event_id)
    except LifecycleError as exc:
        raise _wrap_lifecycle(exc) from None


def _read_approval(conn: sqlite3.Connection, approval_event_id: int) -> ApprovalRecord:
    """Read one decision back through the same join the history reader uses.

    Read back rather than assembled from the arguments, for ``_append_event``'s reason:
    what is returned is then what the ledger holds, including the values its own
    constraints accepted.
    """
    row = _fetch_one(
        conn,
        f"SELECT {_APPROVAL_COLUMNS} {_APPROVAL_JOIN} WHERE a.event_id = ?",
        (approval_event_id,),
    )
    if row is None:  # pragma: no cover - both rows were just written in this transaction
        raise ApprovalError("the recorded approval decision could not be read back")
    return _approval_from_row(row)


def _approval_from_row(row: sqlite3.Row) -> ApprovalRecord:
    """Build the value object from a stored row, gating every field.

    Every field is re-validated on the way out even though the schema accepted it on the
    way in: values read back out of the ledger are untrusted per CLAUDE.md's threat
    boundary, and a row written by something other than this package is exactly the case
    the ``typeof()``-style guards in ``lifecycle.py`` exist for.
    """
    return ApprovalRecord(
        event_id=_stored_int(row[0], "event_id"),
        forecast_record_id=_stored_text(row[1], "forecast_record_id"),
        decision=cast(ApprovalDecision, _stored_member(row[2], _DECISIONS, "decision")),
        actor=_stored_text(row[3], "actor"),
        forecast_sha256=_stored_text(row[4], "forecast_sha256"),
        note=(None if row[5] is None else _stored_text(row[5], "note")),
        occurred_at_utc=_stored_text(row[6], "occurred_at_utc"),
        created_at_utc=_stored_text(row[7], "created_at_utc"),
        event_seq=_stored_int(row[8], "event_seq"),
    )


def _require_text(value: object, field: str, *, max_length: int) -> str:
    """Return ``value`` as storable text, or raise naming only the *field*.

    The type gate is exact (``type(x) is str``) rather than ``isinstance``, and the encode
    probe is the load-bearing one: ``sqlite3`` encodes text parameters as UTF-8, so a lone
    surrogate reaching a query raises a raw ``UnicodeEncodeError`` **quoting the offending
    character** -- both a leak and an error type callers do not handle. ``lifecycle.py``
    gives the same reasoning at its own boundary.
    """
    if type(value) is not str or not value:
        raise ApprovalError(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise ApprovalError(f"{field} is longer than the {max_length}-character limit")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # from None: UnicodeEncodeError's own message quotes the character it choked on.
        raise ApprovalError(
            f"{field} contains characters that cannot be stored "
            "(detail withheld: it can echo the value)"
        ) from None
    return value


def _require_sha256(value: object) -> str:
    """Return the operator's expected hash as a 64-character lowercase hex digest.

    A malformed digest gets its own message rather than falling through to the mismatch
    below it: "does not match" would be true but would describe a typo as a changed
    forecast, which are different things for an operator to act on.
    """
    text = _require_text(value, "forecast_sha256", max_length=64)
    if len(text) != 64 or not _HEX_DIGITS.issuperset(text):
        raise ApprovalError("forecast_sha256 must be 64 lowercase hexadecimal characters")
    return text


def _stored_member(value: object, allowed: frozenset[str], field: str) -> str:
    """Gate a stored value against a closed vocabulary before anything names it."""
    if type(value) is not str or value not in allowed:
        raise ApprovalError(
            f"stored {field} is not one of the recognized values "
            "(detail withheld: it can echo a stored value)"
        )
    return value


def _stored_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise ApprovalError(
            f"stored {field} is not an integer (detail withheld: it can echo stored values)"
        )
    return value


def _stored_text(value: object, field: str) -> str:
    if type(value) is not str:
        raise ApprovalError(
            f"stored {field} is not text (detail withheld: it can echo stored values)"
        )
    return value


def _wrap_lifecycle(exc: LifecycleError) -> ApprovalError:
    """Re-raise the layer below as this module's own type; see :class:`ApprovalError`."""
    return ApprovalError(str(exc) or "the ledger refused this approval decision")


def _require_stored_record(conn: sqlite3.Connection, record_id: str) -> None:
    row = _fetch_one(conn, "SELECT 1 FROM forecast_records WHERE record_id = ?", (record_id,))
    if row is None:
        raise ApprovalError("record_id does not name a stored forecast record")


def _fetch_one(
    conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> sqlite3.Row | None:
    try:
        row = conn.execute(sql, parameters).fetchone()
    except (sqlite3.Error, OverflowError, UnicodeEncodeError):
        # from None: the underlying error's text and traceback can carry stored values,
        # and sqlite3's UnicodeEncodeError quotes the character it could not encode.
        raise ApprovalError(
            "the ledger could not be read (detail withheld: a database message can echo "
            "stored values)"
        ) from None
    return cast("sqlite3.Row | None", row)


def _fetch_all(
    conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> list[sqlite3.Row]:
    try:
        rows = conn.execute(sql, parameters).fetchall()
    except (sqlite3.Error, OverflowError, UnicodeEncodeError):  # see _fetch_one
        raise ApprovalError(
            "the ledger could not be read (detail withheld: a database message can echo "
            "stored values)"
        ) from None
    return cast("list[sqlite3.Row]", rows)
