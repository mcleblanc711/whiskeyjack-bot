"""Lifecycle state machine and atomic event writers (M1-603).

A forecast record is written once, as a ``draft``, and is never updated. Every later
state -- validated, approved, submitted, failed, resolved, scored -- exists only as an
appended :data:`lifecycle_events` row, so a record's **current status is derived**: the
``to_status`` of its highest ``event_seq``, or its stored ``status`` while it has no
events. ``003_lifecycle_events.sql`` holds the reasoning and the enforcement.

Not every event moves the record. A rejection and an *uncertain* submission -- one whose
refetch neither confirmed nor refuted the post -- are recorded where they happened and
leave the status where it was, because in both cases the record has not gone anywhere and
moving it would be a claim nothing supports. That is why a submission has three outcomes
here and not two; see :func:`record_submission_attempt`.

An uncertainty is not a resting place, though. What resolves it is a refetch --
:func:`record_submission_verification`, which writes what the platform actually showed and
carries the record to ``submitted`` or to ``failed``. Recording that observation as another
*attempt* would mean claiming a second live post, which is the retry the handoff exists to
block; see :class:`SubmissionVerification`.

Blocking that retry is **not** something this module can do, and round 4 removed the
attempt to. Every writer here runs after the fact it records, so refusing a write cannot
prevent an action -- it can only lose the evidence of one. The rule lives in front of the
request instead: :func:`unresolved_uncertainties` is what a submitter asks *before* posting.

That is what M1-603's acceptance criterion reduces to. "Injected failures cannot leave
an approved/submitted state without its event record" is not a property of the code
below; it is a property of the schema, because there is nowhere else for the state to
be written. What this module owns is the other half -- **atomicity**. An approval is a
row in ``approval_events`` *and* a row in ``lifecycle_events``, a submission is a row in
``submission_attempts`` *and* a row in ``lifecycle_events``, and a caller must never be
able to observe one without the other. Every writer here wraps both in one transaction.

The module deliberately does **not** ship:

- ``approve`` / ``reject`` CLI commands -- M2-701 owns those, and adding them here would
  put a reachable approval path in the tree ahead of its item;
- resolution and score writers -- M4-802 and M5-803 own those. Their *transitions* are
  defined (here and in the migration) because migrations are immutable and a missing
  event type would later cost a whole migration to add;
- assembly of the handoff's full canonical record. Approval and submission history is
  joined at read/export time (M1-604, ``show``), never written back into ``record_json``
  -- writing it back would mean updating a stored forecast version, which is the thing
  D25 forbids.

Error hygiene follows ``ConfigError``/``LedgerError``: :class:`LifecycleError` never
echoes a stored value, sanitizing raises use ``from None``, and every malformed shape
arrives as a :class:`LifecycleError` rather than a raw ``sqlite3``/``TypeError``. The
vocabularies below are the one thing safe to name in a message -- they are this module's
own closed literals, not content -- and values read back out of the database are gated
against them before they are allowed anywhere near an error string.

Purely local file I/O: no network access on any path through here.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast, get_args

# The seven states of 001's `forecast_records.status` CHECK.
LifecycleStatus = Literal[
    "draft", "validated", "approved", "submitted", "failed", "resolved", "scored"
]

# What can happen to a forecast record. Each event type names its own pipeline phase,
# which is why there is no separate `phase` column: it would be a second spelling of
# this one that has to be kept in agreement with it.
#
# Every member is scoped to a *forecast record*, and that is what bounds the list. An
# earlier draft also carried `research_failed` and `generation_failed`; both were
# structurally unreachable, because `forecast_records` (001) requires a non-null
# `final_prediction_json`, `record_json` and `retrieval_run_id`, so no record exists
# until generation has already succeeded. There was no row for those events to attach
# to, and no honest one to invent. They are removed rather than left as an API that can
# only raise "unknown record"; M1-606 owns pre-forecast failures and the attempt-scoped
# identity they need. (GPT review round 1, finding 2.) A research failure already has a
# home in the meantime: `research_runs.error_summary`, per CLAUDE_CODE_PROMPT.md's
# retrieval section.
#
# `submission_uncertain` is the one member that exists because the alternative was a
# false claim. An attempt that posted but whose refetch did not confirm it is neither a
# verified success nor an outright failure; recording it as `submission_failed` moved the
# record to terminal `failed`, so a later confirming refetch had no legal event and the
# ledger would disagree with the platform permanently -- the opposite of the handoff's
# "an uncertain timeout blocks blind retry until a refetch resolves the state". It leaves
# the record `approved`. (GPT review round 2, finding 3.)
#
# `submission_confirmed` and `submission_disconfirmed` are the two ways back out of that
# state, and they belong to the *refetch* rather than to another attempt. Without them the
# only route to `submitted` ran through a second `submission_attempts` row, which needs a
# new idempotency key (001 declares it UNIQUE) and therefore claims a second live post --
# the blind retry the handoff forbids, reached by way of the mechanism that was supposed
# to prevent it. (GPT review round 3, finding 1.) They cite a `submission_verifications`
# row; see :func:`record_submission_verification`.
LifecycleEventType = Literal[
    "validated",
    "validation_failed",
    "rejected",
    "approved",
    "submitted",
    "submission_uncertain",
    "submission_failed",
    "submission_confirmed",
    "submission_disconfirmed",
    "resolved",
    "scored",
]

# The subset :func:`record_failure` writes: failures that happen once the draft record
# exists but before it is approved, and so carry no detail row. One member today, kept as
# a named alias because M1-606 is expected to widen it, and because it keeps
# `record_failure`'s vocabulary gate honest about which events it will accept. A
# submission failure is not here -- it has an attempt row and is written by
# :func:`record_submission_attempt`.
PipelineFailureEvent = Literal["validation_failed"]

# What a refetch saw. Two-valued for the reason the migration gives: a refetch that could
# not be *performed* observed nothing and changes no state, so it has no lifecycle event
# to produce and would be a detail row nothing can cite. Like `ApprovalDecision`, the
# member and the event type are deliberately different words -- `confirmed`/`absent`
# describe the platform, `submission_confirmed`/`submission_disconfirmed` describe what
# that does to the record -- so the migration maps one to the other explicitly rather than
# comparing two columns that happen to agree.
VerificationOutcome = Literal["confirmed", "absent"]

# The 001 vocabulary of `approval_events.decision`, reused verbatim: the decision and
# the lifecycle event type are the same word, which is what lets the migration's trigger
# check that a lifecycle event cites an approval row recording the same decision.
ApprovalDecision = Literal["approved", "rejected"]

# Why something failed, or why a submission is unconfirmed. A closed vocabulary, because
# this is the only "reason" the lifecycle log carries and it must be safe to export and
# log without review. Provider text stays in
# `submission_attempts.error_message`/`response_body`, which the event row points at
# rather than copies.
#
# `rejected_by_reviewer` was a member and is deliberately gone. A rejection is a decision,
# not a failure: it lands validated -> validated, its account is the actor and note on the
# `approval_events` row the event cites, and the migration forbids a `detail_code` on it.
# Nothing could ever write the code, so it is removed rather than shipped as dead
# vocabulary in an immutable migration -- the call round 1 made on `research_failed`.
# (Owner decision, round 2 finding 8.)
FailureCode = Literal[
    "provider_error",
    "provider_unavailable",
    "no_evidence",
    "stale_evidence",
    "malformed_response",
    "schema_invalid",
    "calibration_invalid",
    "http_error",
    "timeout",
    "refetch_mismatch",
    "refetch_missing",
    "internal_error",
]

_STATUSES: frozenset[str] = frozenset(get_args(LifecycleStatus))
_EVENT_TYPES: frozenset[str] = frozenset(get_args(LifecycleEventType))
_FAILURE_CODES: frozenset[str] = frozenset(get_args(FailureCode))
_APPROVAL_DECISIONS: frozenset[str] = frozenset(get_args(ApprovalDecision))
_VERIFICATION_OUTCOMES: frozenset[str] = frozenset(get_args(VerificationOutcome))
_PIPELINE_FAILURE_EVENTS: frozenset[str] = frozenset(get_args(PipelineFailureEvent))

# The state machine, spelled out here and again as a trigger in
# `003_lifecycle_events.sql`. The duplication is deliberate -- the database is the
# enforcement, this table is the writer -- and `tests/unit/test_lifecycle.py` drives
# every possible (event_type, from_status, to_status) triple through the database and
# asserts the accepted set is exactly this one, so the two cannot drift apart.
#
# `failed` is terminal by omission: a retry is a new forecast *version* (M1-602), not a
# resurrected record. `rejected` is validated -> validated because the seven states have
# no 'rejected' member and a rejected approval must "leave the last valid record intact"
# (CODEX_HANDOFF, pipeline and failure boundaries) -- it records a decision without
# moving the record. `submission_uncertain` is approved -> approved for the reason given
# at its vocabulary member: an unresolved submission must stay somewhere a later refetch
# can still move it, and `approved` is where the record was. The refetch is what moves it
# from there -- `submission_confirmed` to `submitted`, `submission_disconfirmed` to
# terminal `failed`, the same destination a (0, 0) attempt reaches and for the same
# reason: the post is not there, and the retry is a new forecast version.
#
# This table is the whole rule again, as of round 4. Round 3 added a history-dependent
# guard on top of it -- no further attempt while an uncertainty stood -- which is not a
# transition rule and, more to the point, not a rule a record of past events can enforce.
# See :func:`unresolved_uncertainties`.
_LEGAL_TRANSITIONS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("validated", "draft", "validated"),
        ("validation_failed", "draft", "failed"),
        ("validation_failed", "validated", "failed"),
        ("rejected", "validated", "validated"),
        ("approved", "validated", "approved"),
        ("submitted", "approved", "submitted"),
        ("submission_uncertain", "approved", "approved"),
        ("submission_failed", "approved", "failed"),
        ("submission_confirmed", "approved", "submitted"),
        ("submission_disconfirmed", "approved", "failed"),
        ("resolved", "submitted", "resolved"),
        ("scored", "resolved", "scored"),
    }
)

# (event_type, from_status) -> to_status. Derived rather than written out a third time.
# The mapping is total over `_LEGAL_TRANSITIONS` and single-valued -- no event type can
# mean two different destinations from the same state -- which a unit test pins by
# comparing sizes.
_DESTINATIONS: dict[tuple[str, str], str] = {
    (event_type, from_status): to_status
    for event_type, from_status, to_status in _LEGAL_TRANSITIONS
}

# Length ceilings, by what the field is. Identifiers and actors are short by nature; an
# operator's note is prose; a provider response body is the one field that can be large
# and is capped where the handoff says the receipt is "size-limited". These bound what a
# single row can put into the ledger; they are not a substitute for M1-605's redaction.
_MAX_IDENTIFIER = 200
_MAX_ACTOR = 200
_MAX_NOTE = 4000
_MAX_BODY = 65536

_HEX_DIGITS: frozenset[str] = frozenset("0123456789abcdef")

# What SQLite's INTEGER can actually hold. Python's int is unbounded, so this is a real
# boundary rather than a defensive nicety; see _require_optional_int.
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


class LifecycleError(Exception):
    """A lifecycle event cannot be recorded, or the ledger rejected it.

    Same hygiene rule as ``ConfigError``/``LedgerError``: the message never echoes a
    stored value, a caller-supplied field value, or a database error's text, and
    sanitizing raises use ``from None`` so nothing can be reprinted through a cause
    chain or a rendered traceback. Vocabulary members (this module's own literals) are
    named, because they are the only thing that makes a rejection actionable and they
    are not content.
    """


@dataclass(frozen=True)
class LifecycleEvent:
    """One recorded transition, read back from the ledger.

    Constructed only by this module, from a row the database has already accepted, so
    every field has passed the migration's CHECKs and triggers. It carries **no free
    text**: identifiers, closed-vocabulary tags and ISO-8601 timestamps only. That is
    what lets a history be logged or exported without a redaction pass -- the free-text
    detail lives in the row this one points at.

    All fields are JSON-native (``str``/``int``/``None``), so the persisted form used
    for replay comparison is ``json.dumps(dataclasses.asdict(event),
    ensure_ascii=True, sort_keys=True)`` -- the M1-305 rule, which survives the lone
    surrogates that ``str.encode('utf-8')`` does not.
    """

    event_id: int
    forecast_record_id: str
    event_seq: int
    event_type: LifecycleEventType
    from_status: LifecycleStatus
    to_status: LifecycleStatus
    detail_code: FailureCode | None
    approval_event_id: int | None
    submission_attempt_id: str | None
    submission_verification_id: int | None
    resolution_event_id: int | None
    score_event_id: int | None
    occurred_at_utc: str
    created_at_utc: str


@dataclass(frozen=True)
class SubmissionAttempt:
    """The `submission_attempts` row a submission produced, minus writer-owned metadata.

    This is the **ledger-side** shape. M2-703's ``SubmissionReceipt`` is the gateway's
    return type and is that item's to define; it maps into this. Keeping them separate
    is what stops a persistence concern (column set, size caps) from being decided here
    on behalf of the submission seam, and vice versa.

    ``created_at_utc`` is absent deliberately: it records when the ledger stored the
    row, so only the write path may set it. Letting a caller supply it would let a
    caller backdate its own audit trail -- the rule ``research/model.py`` already states
    for the same column.

    ``completed_at_utc`` is **required**, and was optional in the first cut. The ledger
    only hears about an attempt once it is over -- there is no in-flight row to leave open
    -- and ``submission_attempts`` is append-only, so a receipt written without a
    completion time could never acquire one. (GPT review round 2, finding 5.)
    """

    attempt_id: str
    idempotency_key: str
    requested_at_utc: datetime
    completed_at_utc: datetime
    request_payload_sha256: str
    success: bool
    verified_by_refetch: bool
    http_status: int | None = None
    response_body: str | None = None
    response_headers: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    refetched_forecast_snapshot: str | None = None


@dataclass(frozen=True)
class SubmissionVerification:
    """A refetch, and what it saw of an attempt whose outcome was left uncertain.

    Deliberately **not** a ``SubmissionAttempt``. An attempt is the record of a request:
    it carries an idempotency key (unique, per 001), a request payload hash and an HTTP
    status, and none of those exist for an observation. Resolving an uncertainty by
    writing a second attempt row meant minting a second key -- which is to say, claiming a
    second live post -- so the thing the handoff asks for ("block retry until refetch
    resolves state") could only be recorded by doing the thing it forbids. (GPT review
    round 3, finding 1.)

    ``outcome`` decides the event: ``confirmed`` carries the record to ``submitted``,
    ``absent`` to terminal ``failed``. The attempt named here must be one this ledger
    recorded as ``submission_uncertain``; an attempt already accounted for as submitted or
    failed is not open to being re-decided by a later refetch.

    ``refetched_forecast_snapshot`` is optional in the type and **required for a
    ``confirmed`` outcome** by both the writer and the schema: a confirmation with nothing
    stored is a claim about the platform with no evidence behind it, and it is the claim
    that moves the record to ``submitted``.

    ``created_at_utc`` is absent for :class:`SubmissionAttempt`'s reason -- it is when the
    ledger stored the row, so only the write path may set it.
    """

    submission_attempt_id: str
    outcome: VerificationOutcome
    observed_at_utc: datetime
    refetched_forecast_snapshot: str | None = None


def _utcnow() -> datetime:
    """Writer-owned clock. A seam for tests; never a parameter of the public writers."""
    return datetime.now(tz=timezone.utc)


def _require_text(value: object, field: str, *, max_length: int) -> str:
    """Return ``value`` as storable text, or raise naming only the *field*.

    The type gate is exact (``type(x) is str``) rather than ``isinstance``: a ``str``
    subclass can carry an attacker-controlled ``__str__``/``__repr__`` whose value slips
    into a log line or a dataclass repr, which is the same reasoning
    ``questions/events.py`` gives for its gates.

    The encode probe is the important one. A lone surrogate reaches this layer from
    provider JSON, and ``sqlite3`` encodes text parameters as UTF-8 -- so without it a
    writer raises a raw ``UnicodeEncodeError`` **quoting the offending character**,
    which is both a leak and an error type callers do not handle. (The same defect is
    open against ``research/hashing.py``; here it is closed at the boundary.)
    """
    if type(value) is not str or not value:
        raise LifecycleError(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise LifecycleError(f"{field} is longer than the {max_length}-character limit")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # from None: UnicodeEncodeError's own message quotes the character it choked on.
        raise LifecycleError(
            f"{field} contains characters that cannot be stored "
            "(detail withheld: it can echo the value)"
        ) from None
    return value


def _require_optional_text(value: object, field: str, *, max_length: int) -> str | None:
    return None if value is None else _require_text(value, field, max_length=max_length)


def _require_sha256(value: object, field: str) -> str:
    """Return ``value`` as a 64-character lowercase hex digest, or raise.

    Mirrors the migration's ``length(...) = 64 AND ... NOT GLOB '*[^0-9a-f]*'`` so a
    caller gets a field-level message instead of a constraint violation.
    """
    text = _require_text(value, field, max_length=64)
    if len(text) != 64 or not _HEX_DIGITS.issuperset(text):
        raise LifecycleError(f"{field} must be 64 lowercase hexadecimal characters")
    return text


def _require_bool(value: object, field: str) -> int:
    """Return 0/1 for the ``CHECK (... IN (0, 1))`` integer columns.

    ``bool`` exactly, not "anything truthy" and not ``int``. ``success`` and
    ``verified_by_refetch`` decide whether a submission becomes ``submitted`` or
    ``submission_failed``, so a stray ``1`` arriving where a ``bool`` was meant would
    silently promote an unverified post to a verified one -- an unearned claim rather
    than a type error.
    """
    if type(value) is not bool:
        raise LifecycleError(f"{field} must be True or False")
    return 1 if value else 0


def _require_optional_int(value: object, field: str) -> int | None:
    """Return ``value`` as a storable integer, or raise naming only the *field*.

    The range check is not decoration. Python integers are unbounded and SQLite's are
    signed 64-bit, so ``sqlite3`` raises a raw ``OverflowError`` when it binds one that
    does not fit -- and ``OverflowError`` is not a ``sqlite3.Error``, so it sails past
    the wrapper in :func:`_insert` and reaches the caller as an exception type this
    module does not document. Rejecting it here makes it a field-level message instead.
    (GPT review round 1, finding 3.)
    """
    if value is None:
        return None
    if type(value) is not int:
        raise LifecycleError(f"{field} must be an integer")
    if not _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX:
        raise LifecycleError(f"{field} is outside the range the ledger can store")
    return value


def _require_http_status(value: object, field: str) -> int | None:
    """Return ``value`` as an HTTP status code, or raise naming only the *field*.

    A status is either absent -- no response arrived -- or a status code. Storing -1, 0 or
    2**63-1 puts a number that no HTTP responder can have produced into an append-only
    receipt, where it is indistinguishable from a real one. :func:`_require_optional_int`
    is the wider gate underneath (a Python int too large to bind raises ``OverflowError``,
    which is not a ``sqlite3.Error``); this narrows it to the range the field means.
    (GPT review round 2, finding 7.)
    """
    status = _require_optional_int(value, field)
    if status is not None and not 100 <= status <= 599:
        raise LifecycleError(f"{field} must be an HTTP status code between 100 and 599")
    return status


def _require_aware_utc(value: object, field: str) -> datetime:
    """Return an aware datetime converted to UTC, or raise.

    Exact type rather than ``isinstance``: a ``datetime`` subclass can override
    ``isoformat()`` and write arbitrary text into a NOT NULL timestamp column, which
    would put unvetted content into a field the ledger's replay ordering depends on.
    (It is also what makes the conversion below safe to call ``isoformat()`` on:
    ``astimezone`` returns the same class it was given.)

    The conversion itself is guarded, and broadly. ``tzinfo`` is an abstract base class,
    so ``value.utcoffset()`` and ``astimezone()`` run *caller-supplied code* on a value
    that has passed every type gate above -- a ``datetime`` carrying a hostile ``tzinfo``
    whose ``utcoffset`` raises will propagate whatever that method raises, message and
    traceback included. ``except Exception`` is the right width precisely because the
    set of exceptions arbitrary code can raise is not enumerable.

    Separate from :func:`_require_utc` so a caller that has to *compare* two timestamps
    can do it on datetimes rather than on their rendered text.
    """
    if type(value) is not datetime:
        raise LifecycleError(f"{field} must be a datetime")
    try:
        aware = value.tzinfo is not None and value.utcoffset() is not None
    except Exception:
        # from None: the tzinfo's own exception text and traceback are attacker-shaped.
        raise LifecycleError(
            f"{field} has a timezone that could not be read "
            "(detail withheld: it can echo the value)"
        ) from None
    if not aware:
        raise LifecycleError(f"{field} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except Exception:
        raise LifecycleError(
            f"{field} could not be converted to UTC (detail withheld: it can echo the value)"
        ) from None


def _utc_text(value: datetime) -> str:
    """Render an aware UTC datetime in the canonical stored form.

    One function, because the form is a contract with ``003_lifecycle_events.sql`` rather
    than a formatting preference: the migration pins this exact shape on the columns it
    orders, so a second rendering anywhere would be refused by our own schema. That is not
    hypothetical -- the attempt writer rendered its two timestamps with a bare
    ``isoformat()`` while :func:`_require_utc` had been made canonical, and every write
    through it failed until both agreed.

    Takes an **already validated** datetime: :func:`_require_aware_utc`'s output or
    :func:`_utcnow`'s. The ``astimezone`` here is a normalization, not a guard -- given a
    caller's raw datetime it would run that caller's ``tzinfo`` code unprotected, which is
    what :func:`_require_aware_utc` exists to wrap.
    """
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _require_utc(value: object, field: str) -> str:
    """Return an aware datetime as a *canonical* ISO-8601 UTC string, or raise.

    ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``: fixed width 32, always UTC, microseconds always
    present. Plain ``isoformat()`` omits the fractional part when it is zero, which makes
    the rendered width vary and the ordering of two stored values depend on which shape
    each happens to have.

    That matters because the schema compares two of these columns, and it can only do so
    exactly by comparing the text: ``julianday()`` is a float day number, so microseconds
    fall below its precision and two instants a microsecond apart compare equal (GPT review
    round 4, finding 3). ``003_lifecycle_events.sql`` pins this exact form on the columns it
    orders; rendering it here for *every* timestamp keeps the stored ledger uniform rather
    than uniform-where-checked.
    """
    return _utc_text(_require_aware_utc(value, field))


def _require_member(value: object, allowed: frozenset[str], field: str) -> str:
    """Gate a value against one of this module's closed vocabularies.

    Used on caller input *and* on values read back out of the database. A stored value
    is only safe to name in a message once it has been proven to be a member of a
    vocabulary this module defines; until then it is content, and the rejection says so
    without printing it.
    """
    if type(value) is not str or value not in allowed:
        raise LifecycleError(
            f"{field} is not one of the recognized values "
            "(detail withheld: it can echo a stored value)"
        )
    return value


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a block atomically, nesting safely inside a caller's own transaction.

    ``BEGIN IMMEDIATE``, not a bare ``BEGIN``. Every writer here reads the record's
    current status and then appends against it; a deferred ``BEGIN`` takes the write
    lock lazily, so two writers can both read "validated", both decide their event is
    seq 2, and only discover the conflict on a lock upgrade that cannot be retried from
    inside an open transaction. Taking the write lock up front serializes the
    read-then-write instead. (``UNIQUE (forecast_record_id, event_seq)`` is the second
    line of defence, and turns any race that does occur into a loud failure rather than
    a silently reordered history.)

    Nested use opens a ``SAVEPOINT`` instead, so M1-602 can write a forecast record and
    its first lifecycle event in one unit without this module either committing early
    or rolling back work it does not own.

    The transaction-control statements are themselves guarded (:func:`_control`). A
    ``COMMIT`` can fail -- a busy timeout, a full disk -- and an unguarded one would both
    escape as a raw ``sqlite3.Error`` and leave the caller holding an open transaction
    that strands every later write on the connection.
    """
    if conn.isolation_level is not None:
        # ledger.connect() sets isolation_level = None so that BEGIN/COMMIT are explicit.
        # Under the default, sqlite3 opens and commits transactions on its own schedule,
        # and "one transaction" below would silently not be one.
        raise LifecycleError(
            "the ledger connection must be in explicit-transaction mode; "
            "open it with whiskeyjack_bot.ledger.connect()"
        )
    if conn.in_transaction:
        savepoint = f"wj_{uuid.uuid4().hex}"
        _control(conn, f"SAVEPOINT {savepoint}")
        try:
            yield
        except BaseException:
            _unwind(conn, f"ROLLBACK TO {savepoint}", f"RELEASE {savepoint}")
            raise
        # The same unwind the exception path uses: ROLLBACK TO leaves the savepoint on the
        # stack, so it takes both statements to undo this block without touching the
        # caller's transaction, which is not ours to roll back or commit.
        _control(
            conn,
            f"RELEASE {savepoint}",
            unwind=(f"ROLLBACK TO {savepoint}", f"RELEASE {savepoint}"),
        )
        return
    _control(conn, "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        _unwind(conn, "ROLLBACK")
        raise
    _control(conn, "COMMIT", unwind=("ROLLBACK",))


def _control(conn: sqlite3.Connection, statement: str, *, unwind: tuple[str, ...] = ()) -> None:
    """Run a transaction-control statement, or fail as this module's own error type.

    ``unwind`` is what has to run before the failure is reported, so the connection is
    never left inside a transaction the caller believes was closed: a failed ``COMMIT``
    rolls back, a failed ``RELEASE`` unwinds to its savepoint. Best-effort by way of
    :func:`_unwind` -- if that fails too the connection is unusable, and the original
    failure is still the one worth reporting.
    """
    try:
        conn.execute(statement)
    except sqlite3.Error:
        _unwind(conn, *unwind)
        # from None: the underlying error's text and traceback can carry stored values.
        # The statement itself is this module's own constant SQL, never caller content,
        # but naming it would say nothing a caller could act on.
        raise LifecycleError(
            "the ledger could not complete this transaction (detail withheld: a database "
            "message can echo stored values)"
        ) from None


def _unwind(conn: sqlite3.Connection, *statements: str) -> None:
    """Best-effort rollback that never replaces the exception being propagated.

    If the rollback itself fails the connection is already unusable, and surfacing that
    instead of the original error would hide why the block failed in the first place.

    Every statement is attempted, including those after one that failed. The two-part
    savepoint unwind is why: ``ROLLBACK TO`` leaves the savepoint on the stack and only
    the paired ``RELEASE`` pops it, so abandoning the sequence at the first failure would
    leak a savepoint onto a connection the caller goes on using.
    """
    for statement in statements:
        try:
            conn.execute(statement)
        except sqlite3.Error:
            continue


def current_status(conn: sqlite3.Connection, record_id: str) -> LifecycleStatus:
    """Return the record's derived current status.

    The ``to_status`` of its highest ``event_seq``, or the status it was created with
    while it has no events. ``forecast_records.status`` is *status at creation* and is
    pinned to ``draft`` by the migration; it is never the answer to "where is this
    record now" once any event exists.
    """
    identifier = _require_text(record_id, "record_id", max_length=_MAX_IDENTIFIER)
    row = _fetch_one(
        conn,
        "SELECT to_status FROM lifecycle_events WHERE forecast_record_id = ? "
        "ORDER BY event_seq DESC LIMIT 1",
        (identifier,),
    )
    if row is None:
        row = _fetch_one(
            conn,
            "SELECT status FROM forecast_records WHERE record_id = ?",
            (identifier,),
        )
    if row is None:
        raise LifecycleError("record_id does not name a stored forecast record")
    return cast(LifecycleStatus, _require_member(row[0], _STATUSES, "status"))


def read_history(conn: sqlite3.Connection, record_id: str) -> tuple[LifecycleEvent, ...]:
    """Return every recorded event for a record, in append order.

    ``event_seq`` is contiguous from 1 per record, so a gap in the returned sequence is
    a detectable defect rather than an unremarkable rowid jump.

    An unknown ``record_id`` raises rather than returning an empty history, matching
    :func:`current_status`. The two are the read seam M1-604 and ``show`` build on, and
    a caller that cannot tell "this record has no events yet" from "there is no such
    record" would report the first while looking at the second.
    """
    identifier = _require_text(record_id, "record_id", max_length=_MAX_IDENTIFIER)
    _require_stored_record(conn, identifier)
    rows = _fetch_all(
        conn,
        f"SELECT {_EVENT_COLUMNS} FROM lifecycle_events WHERE forecast_record_id = ? "
        "ORDER BY event_seq",
        (identifier,),
    )
    return tuple(_event_from_row(row) for row in rows)


def unresolved_uncertainties(conn: sqlite3.Connection, record_id: str) -> tuple[str, ...]:
    """Attempt ids this record recorded as uncertain that no refetch has resolved yet.

    **Ask this before submitting, not after.** It is the ledger's half of the handoff's
    "an uncertain timeout blocks blind retry until a refetch resolves the state": an empty
    tuple means nothing is outstanding, and a non-empty one names the attempts M2-704 has
    to refetch and pass to :func:`record_submission_verification` first.

    Round 3 tried to enforce that rule at write time instead, in the trigger and in
    :func:`record_submission_attempt`. Both run on a *finished* receipt, so refusing there
    could not stop a second post -- only stop it being recorded, which loses the fact
    instead of preventing the act, and left the attempt row committed with its event
    refused (GPT review round 4, finding 1). A rule about what to do next belongs in front
    of the action; this is that seam, and it is a reader.

    "Unresolved" is: an uncertain event whose record is *still* ``approved``. A refetch
    that resolved one carried the record to ``submitted`` or ``failed``, and no submission
    is legal from either -- so once the record has moved, nothing here is outstanding.
    """
    identifier = _require_text(record_id, "record_id", max_length=_MAX_IDENTIFIER)
    if current_status(conn, identifier) != "approved":
        return ()
    rows = _fetch_all(
        conn,
        "SELECT submission_attempt_id FROM lifecycle_events "
        "WHERE forecast_record_id = ? AND event_type = 'submission_uncertain' "
        "ORDER BY event_seq",
        (identifier,),
    )
    return tuple(_stored_text(row[0], "submission_attempt_id") for row in rows)


def record_validation(
    conn: sqlite3.Connection, *, record_id: str, occurred_at: datetime
) -> LifecycleEvent:
    """Record that a draft passed validation: ``draft -> validated``.

    The success half of M1-501/M1-504's gate. The failure half is
    :func:`record_failure` with ``event_type='validation_failed'``.
    """
    return _append_event(
        conn,
        record_id=record_id,
        event_type="validated",
        occurred_at_utc=_require_utc(occurred_at, "occurred_at"),
    )


def record_failure(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    event_type: PipelineFailureEvent,
    detail_code: FailureCode,
    occurred_at: datetime,
) -> LifecycleEvent:
    """Record a pre-approval failure of a stored draft: validation, today.

    These carry no detail row -- there is no provider receipt to point at -- so
    ``detail_code`` is the whole account of what went wrong and is required. A failed
    record is terminal: a further attempt is a new forecast version (M1-602), which is
    why there is no transition out of ``failed``.

    ``event_type`` is a one-member vocabulary rather than a fixed literal because the
    events this *cannot* record are the interesting ones: a research or generation
    failure happens before any ``forecast_records`` row exists, so it has no record to
    name here. That is M1-606's problem to solve, with an attempt-scoped identity and
    migration 004; see :data:`LifecycleEventType`.
    """
    _require_member(event_type, _PIPELINE_FAILURE_EVENTS, "event_type")
    _require_member(detail_code, _FAILURE_CODES, "detail_code")
    return _append_event(
        conn,
        record_id=record_id,
        event_type=event_type,
        detail_code=detail_code,
        occurred_at_utc=_require_utc(occurred_at, "occurred_at"),
    )


def record_approval(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    decision: ApprovalDecision,
    actor: str,
    forecast_sha256: str,
    occurred_at: datetime,
    note: str | None = None,
) -> LifecycleEvent:
    """Append an approval decision and its lifecycle event, atomically.

    ``forecast_sha256`` must equal the hash stored on the record: approval binds to an
    exact forecast, and any content change invalidates it (D12/D23). The check is made
    here for a readable failure and again by the migration's trigger, which is the
    binding one -- a writer that forgot to compare cannot get past the database.

    ``'rejected'`` leaves the record ``validated``. A rejection records a decision; it
    does not move the record, and the last valid record stays intact.
    """
    _require_member(decision, _APPROVAL_DECISIONS, "decision")
    identifier = _require_text(record_id, "record_id", max_length=_MAX_IDENTIFIER)
    actor_text = _require_text(actor, "actor", max_length=_MAX_ACTOR)
    digest = _require_sha256(forecast_sha256, "forecast_sha256")
    note_text = _require_optional_text(note, "note", max_length=_MAX_NOTE)
    occurred = _require_utc(occurred_at, "occurred_at")

    with transaction(conn):
        _require_hash_binds(conn, identifier, digest)
        approval_id = _insert(
            conn,
            "INSERT INTO approval_events "
            "(forecast_record_id, decision, actor, forecast_sha256, note, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (identifier, decision, actor_text, digest, note_text, _utc_text(_utcnow())),
        )
        return _append_event(
            conn,
            record_id=identifier,
            event_type=decision,
            approval_event_id=approval_id,
            occurred_at_utc=occurred,
        )


def record_submission_attempt(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    attempt: SubmissionAttempt,
    occurred_at: datetime,
    detail_code: FailureCode | None = None,
) -> LifecycleEvent:
    """Append a submission attempt and its lifecycle event, atomically.

    The event type is **derived from the attempt**, not chosen by the caller, and the
    ``(success, verified_by_refetch)`` pair partitions into three outcomes rather than
    two::

        (True,  True)   submitted             the post went through, a refetch confirmed it
        (True,  False)  submission_uncertain  it went through; the refetch did not confirm
        (False, True)   submission_uncertain  it errored; the refetch says something is there
        (False, False)  submission_failed     it did not go through and nothing is there

    ``submitted`` is M2-704's "success requires refetch confirmation". The middle two are
    the handoff's uncertain timeout: the two signals disagree, which is a third outcome
    and not a failure. Recording those as ``submission_failed`` moved the record to
    terminal ``failed``, so a later confirming refetch had nowhere to land and blind retry
    was the only thing left -- exactly what the handoff says the ledger must prevent
    (GPT review round 2, finding 3). An uncertain attempt leaves the record ``approved``.

    A second attempt made while an earlier one is still uncertain **is recorded**, not
    refused. Round 3 refused it here and in the trigger; round 4 withdrew that, because
    this function is handed a receipt for a post that has already happened, and the only
    thing a refusal achieves is a live post with no ledger row. Whether to make that
    request is decided before it is made -- see :func:`unresolved_uncertainties` -- and a
    record may therefore hold more than one uncertain attempt, each with its own event.

    ``detail_code`` is required for both non-verified outcomes and refused for
    ``submitted``.

    Persistence only. Nothing here contacts Metaculus; the gateways that do are M2-703
    and M2-704, and ``submission.enabled``/``dry_run`` remain what they are until then.
    """
    # Exact type, not isinstance -- the same gate every validator below uses, and for a
    # stronger reason. A subclass can override __getattribute__ or shadow a field with a
    # property, so each `attempt.<field>` read here becomes a call into caller-supplied
    # code that can raise anything, from anywhere between the two writes. The field
    # validators cannot help: they only see what the attribute access returns.
    # (GPT review round 1, finding 3.)
    if type(attempt) is not SubmissionAttempt:
        raise LifecycleError("attempt must be a SubmissionAttempt")
    identifier = _require_text(record_id, "record_id", max_length=_MAX_IDENTIFIER)
    occurred = _require_utc(occurred_at, "occurred_at")

    attempt_id = _require_text(attempt.attempt_id, "attempt.attempt_id", max_length=_MAX_IDENTIFIER)
    success = _require_bool(attempt.success, "attempt.success")
    verified = _require_bool(attempt.verified_by_refetch, "attempt.verified_by_refetch")

    event_type: LifecycleEventType
    if success == 1 and verified == 1:
        event_type = "submitted"
        if detail_code is not None:
            raise LifecycleError("detail_code is not applicable to a verified submission")
    else:
        # The two signals disagreeing is the uncertain case; both saying no is the failure.
        event_type = "submission_uncertain" if success != verified else "submission_failed"
        if detail_code is None:
            raise LifecycleError(
                "detail_code is required for an attempt that is not a refetch-verified success"
            )
        _require_member(detail_code, _FAILURE_CODES, "detail_code")

    # Both timestamps as datetimes, so the ordering check below compares instants rather
    # than rendered text. A receipt that finished before it was requested is not a clock
    # curiosity here: `requested_at_utc` is what an idempotency key is reasoned about
    # against, and the row is append-only, so a reversed pair is permanent.
    requested = _require_aware_utc(attempt.requested_at_utc, "attempt.requested_at_utc")
    completed = _require_aware_utc(attempt.completed_at_utc, "attempt.completed_at_utc")
    if completed < requested:
        raise LifecycleError("attempt.completed_at_utc is earlier than attempt.requested_at_utc")

    values = (
        attempt_id,
        identifier,
        _require_text(
            attempt.idempotency_key, "attempt.idempotency_key", max_length=_MAX_IDENTIFIER
        ),
        _utc_text(requested),
        _utc_text(completed),
        _require_sha256(attempt.request_payload_sha256, "attempt.request_payload_sha256"),
        _require_http_status(attempt.http_status, "attempt.http_status"),
        _require_optional_text(
            attempt.response_body, "attempt.response_body", max_length=_MAX_BODY
        ),
        _require_optional_text(
            attempt.response_headers, "attempt.response_headers", max_length=_MAX_BODY
        ),
        success,
        _require_optional_text(
            attempt.error_type, "attempt.error_type", max_length=_MAX_IDENTIFIER
        ),
        _require_optional_text(
            attempt.error_message, "attempt.error_message", max_length=_MAX_BODY
        ),
        verified,
        _require_optional_text(
            attempt.refetched_forecast_snapshot,
            "attempt.refetched_forecast_snapshot",
            max_length=_MAX_BODY,
        ),
        _utc_text(_utcnow()),
    )

    with transaction(conn):
        _insert(
            conn,
            "INSERT INTO submission_attempts "
            "(attempt_id, forecast_record_id, idempotency_key, requested_at_utc, "
            "completed_at_utc, request_payload_sha256, http_status, response_body, "
            "response_headers, success, error_type, error_message, verified_by_refetch, "
            "refetched_forecast_snapshot, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return _append_event(
            conn,
            record_id=identifier,
            event_type=event_type,
            detail_code=detail_code,
            submission_attempt_id=attempt_id,
            occurred_at_utc=occurred,
        )


def record_submission_verification(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    verification: SubmissionVerification,
    occurred_at: datetime,
    detail_code: FailureCode | None = None,
) -> LifecycleEvent:
    """Append a refetch observation and its lifecycle event, atomically.

    This is how an uncertain submission ends. The attempt named by ``verification`` must
    be one this record's history holds a ``submission_uncertain`` event for; what the
    refetch saw then decides where the record goes::

        confirmed  submission_confirmed     approved -> submitted
        absent     submission_disconfirmed  approved -> failed

    As with :func:`record_submission_attempt`, the event type is **derived** and never
    chosen by the caller. ``detail_code`` is required for ``absent`` -- ``refetch_missing``
    is the usual one -- and refused for ``confirmed``, which is a success and carries no
    failure code. ``refetched_forecast_snapshot`` runs the other way: **required for
    ``confirmed``**, because it is the evidence that carries the record to ``submitted``,
    and empty for ``absent``, which saw nothing to store.

    Until this is written the uncertainty is outstanding, and
    :func:`unresolved_uncertainties` keeps saying so -- which is what M2-704 consults
    before deciding to post again. ``submission_disconfirmed`` is terminal, so a genuinely
    lost post is retried as a new forecast version (M1-602), which is what every other
    route to ``failed`` already means.

    Persistence only. Nothing here contacts Metaculus; M2-704 owns the refetch itself.
    """
    # Exact type, for the reason spelled out in record_submission_attempt: a subclass can
    # turn every attribute read below into a call into caller-supplied code.
    if type(verification) is not SubmissionVerification:
        raise LifecycleError("verification must be a SubmissionVerification")
    identifier = _require_text(record_id, "record_id", max_length=_MAX_IDENTIFIER)
    occurred = _require_utc(occurred_at, "occurred_at")

    attempt_id = _require_text(
        verification.submission_attempt_id,
        "verification.submission_attempt_id",
        max_length=_MAX_IDENTIFIER,
    )
    outcome = _require_member(verification.outcome, _VERIFICATION_OUTCOMES, "verification.outcome")
    observed = _require_utc(verification.observed_at_utc, "verification.observed_at_utc")
    snapshot = _require_optional_text(
        verification.refetched_forecast_snapshot,
        "verification.refetched_forecast_snapshot",
        max_length=_MAX_BODY,
    )

    event_type: LifecycleEventType
    if outcome == "confirmed":
        event_type = "submission_confirmed"
        if detail_code is not None:
            raise LifecycleError("detail_code is not applicable to a confirmed submission")
        # A confirmation carries the record to `submitted`, and what it saw is the whole
        # of the evidence for that. Round 3 wrote the snapshot column and left it
        # optional, so a confirmation could be recorded on nothing (round 4, finding 2).
        if snapshot is None or not snapshot.strip():
            raise LifecycleError(
                "verification.refetched_forecast_snapshot is required for a confirmed "
                "refetch; a confirmation is only auditable if it stores what it saw"
            )
    else:
        event_type = "submission_disconfirmed"
        if detail_code is None:
            raise LifecycleError(
                "detail_code is required for a refetch that did not find the forecast"
            )
        _require_member(detail_code, _FAILURE_CODES, "detail_code")

    with transaction(conn):
        _require_verifiable_attempt(conn, identifier, attempt_id, observed)
        verification_id = _insert(
            conn,
            "INSERT INTO submission_verifications "
            "(submission_attempt_id, outcome, observed_at_utc, "
            "refetched_forecast_snapshot, created_at_utc) VALUES (?, ?, ?, ?, ?)",
            (attempt_id, outcome, observed, snapshot, _utc_text(_utcnow())),
        )
        return _append_event(
            conn,
            record_id=identifier,
            event_type=event_type,
            detail_code=detail_code,
            submission_verification_id=verification_id,
            occurred_at_utc=occurred,
        )


_EVENT_COLUMNS = (
    "event_id, forecast_record_id, event_seq, event_type, from_status, to_status, "
    "detail_code, approval_event_id, submission_attempt_id, submission_verification_id, "
    "resolution_event_id, score_event_id, occurred_at_utc, created_at_utc"
)


def _append_event(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    event_type: LifecycleEventType,
    detail_code: FailureCode | None = None,
    approval_event_id: int | None = None,
    submission_attempt_id: str | None = None,
    submission_verification_id: int | None = None,
    occurred_at_utc: str,
) -> LifecycleEvent:
    """Append one lifecycle row, in a transaction, and return it as stored.

    ``from_status`` is read here rather than accepted from the caller: a caller that can
    assert its own starting point can skip a state. The destination follows from
    ``(event_type, from_status)`` via :data:`_DESTINATIONS`, so an illegal transition is
    a readable error before any statement runs -- and the migration's trigger re-derives
    the same thing against the row it is actually inserting, which is what makes it
    enforcement rather than agreement.

    The row is read back after insert rather than assembled from the arguments: what is
    returned is then what the ledger holds, including the values its own constraints
    accepted.
    """
    identifier = _require_text(record_id, "record_id", max_length=_MAX_IDENTIFIER)
    _require_member(event_type, _EVENT_TYPES, "event_type")

    with transaction(conn):
        from_status = current_status(conn, identifier)
        to_status = _DESTINATIONS.get((event_type, from_status))
        if to_status is None:
            # Both halves are vetted vocabulary members, so naming them is safe and is
            # the only thing that makes this actionable.
            raise LifecycleError(
                f"a {event_type} event is not a legal transition for a record whose "
                f"current status is {from_status}"
            )
        event_seq = _next_seq(conn, identifier)
        event_id = _insert(
            conn,
            "INSERT INTO lifecycle_events "
            "(forecast_record_id, event_seq, event_type, from_status, to_status, "
            "detail_code, approval_event_id, submission_attempt_id, "
            "submission_verification_id, occurred_at_utc, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                event_seq,
                event_type,
                from_status,
                to_status,
                detail_code,
                approval_event_id,
                submission_attempt_id,
                submission_verification_id,
                occurred_at_utc,
                _utc_text(_utcnow()),
            ),
        )
        row = _fetch_one(
            conn,
            f"SELECT {_EVENT_COLUMNS} FROM lifecycle_events WHERE event_id = ?",
            (event_id,),
        )
        if row is None:  # pragma: no cover - the row was just inserted in this transaction
            raise LifecycleError("the recorded lifecycle event could not be read back")
        return _event_from_row(row)


def _require_stored_record(conn: sqlite3.Connection, record_id: str) -> None:
    """Fail readably when an identifier names no stored forecast record."""
    row = _fetch_one(conn, "SELECT 1 FROM forecast_records WHERE record_id = ?", (record_id,))
    if row is None:
        raise LifecycleError("record_id does not name a stored forecast record")


def _require_hash_binds(conn: sqlite3.Connection, record_id: str, digest: str) -> None:
    """Fail readably when an approval names a hash the record does not have."""
    row = _fetch_one(
        conn, "SELECT forecast_sha256 FROM forecast_records WHERE record_id = ?", (record_id,)
    )
    if row is None:
        raise LifecycleError("record_id does not name a stored forecast record")
    if row[0] is None:
        raise LifecycleError(
            "this forecast record stores no content hash and so cannot be approved"
        )
    if row[0] != digest:
        # Neither hash is printed: one is a stored value, and printing the other would
        # let a caller confirm a guess against it.
        raise LifecycleError(
            "forecast_sha256 does not match the stored hash of this forecast record; "
            "the forecast changed and any prior approval no longer binds"
        )


def _require_verifiable_attempt(
    conn: sqlite3.Connection, record_id: str, attempt_id: str, observed_at_utc: str
) -> None:
    """Fail readably when a refetch names an attempt it cannot be resolving.

    Both halves are re-derived by the migration's trigger, which is the binding check.
    The attempt must be one *this* record recorded as uncertain -- which subsumes
    ownership, since the uncertain event names both -- and the observation cannot predate
    the attempt it observes.

    The ordering comparison is left to SQL rather than parsed here, because the stored
    value is text this module did not necessarily write and ``fromisoformat`` on it would
    be one more place a stored value can raise. It compares TEXT, not ``julianday``: a
    float day number cannot represent microseconds, so that comparison called two instants
    a microsecond apart equal (round 4, finding 3). Both sides are the canonical fixed-width
    UTC form :func:`_require_utc` renders and the migration pins, which is what makes a
    text comparison exact.
    """
    row = _fetch_one(
        conn,
        "SELECT 1 FROM lifecycle_events WHERE forecast_record_id = ? "
        "AND submission_attempt_id = ? AND event_type = 'submission_uncertain' LIMIT 1",
        (record_id, attempt_id),
    )
    if row is None:
        raise LifecycleError(
            "this record has no uncertain submission attempt by that identifier, so there "
            "is nothing for a refetch to resolve"
        )
    row = _fetch_one(
        conn,
        "SELECT 1 FROM submission_attempts WHERE attempt_id = ? AND completed_at_utc > ?",
        (attempt_id, observed_at_utc),
    )
    if row is not None:
        raise LifecycleError(
            "verification.observed_at_utc is earlier than the completion of the attempt it verifies"
        )


def _next_seq(conn: sqlite3.Connection, record_id: str) -> int:
    row = _fetch_one(
        conn,
        "SELECT max(event_seq) FROM lifecycle_events WHERE forecast_record_id = ?",
        (record_id,),
    )
    if row is None or row[0] is None:
        return 1
    if type(row[0]) is not int:
        # A non-integer event_seq means the column's affinity was defeated by a writer
        # that bypassed this module; the value itself is stored content and stays unnamed.
        raise LifecycleError(
            "the stored lifecycle sequence is malformed "
            "(detail withheld: it can echo stored values)"
        )
    return row[0] + 1


def _event_from_row(row: sqlite3.Row) -> LifecycleEvent:
    """Build the value object from a stored row, gating every vocabulary field."""
    return LifecycleEvent(
        event_id=_stored_int(row[0], "event_id"),
        forecast_record_id=_stored_text(row[1], "forecast_record_id"),
        event_seq=_stored_int(row[2], "event_seq"),
        event_type=cast(LifecycleEventType, _require_member(row[3], _EVENT_TYPES, "event_type")),
        from_status=cast(LifecycleStatus, _require_member(row[4], _STATUSES, "from_status")),
        to_status=cast(LifecycleStatus, _require_member(row[5], _STATUSES, "to_status")),
        detail_code=(
            None
            if row[6] is None
            else cast(FailureCode, _require_member(row[6], _FAILURE_CODES, "detail_code"))
        ),
        approval_event_id=None if row[7] is None else _stored_int(row[7], "approval_event_id"),
        submission_attempt_id=(
            None if row[8] is None else _stored_text(row[8], "submission_attempt_id")
        ),
        submission_verification_id=(
            None if row[9] is None else _stored_int(row[9], "submission_verification_id")
        ),
        resolution_event_id=None
        if row[10] is None
        else _stored_int(row[10], "resolution_event_id"),
        score_event_id=None if row[11] is None else _stored_int(row[11], "score_event_id"),
        occurred_at_utc=_stored_text(row[12], "occurred_at_utc"),
        created_at_utc=_stored_text(row[13], "created_at_utc"),
    )


def _stored_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise LifecycleError(
            f"stored {field} is not an integer (detail withheld: it can echo stored values)"
        )
    return value


def _stored_text(value: object, field: str) -> str:
    if type(value) is not str:
        raise LifecycleError(
            f"stored {field} is not text (detail withheld: it can echo stored values)"
        )
    return value


def _insert(conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]) -> int:
    """Execute one INSERT and return its rowid, wrapping every database failure.

    Callers only handle this module's own error type, so a raw ``sqlite3.Error`` --
    including the ``IntegrityError`` a trigger raises -- must not escape. The database's
    text is not forwarded: SQLite's constraint messages name tables and columns rather
    than values today, but that is a property of the engine's formatting and not a
    contract, and this module's guarantee is not allowed to depend on it. The
    actionable cases (illegal transition, unknown record, hash mismatch, malformed
    field) are all raised with their own messages before the statement runs.

    ``OverflowError`` is caught alongside ``sqlite3.Error`` because it is not one:
    ``sqlite3`` raises it while *binding* a Python int too large for a signed 64-bit
    column, before any database code runs. :func:`_require_optional_int` now rejects
    those at the field, so this is the second line -- but the two must both hold, since
    a later writer could pass an integer that never went through that validator.
    """
    try:
        cursor = conn.execute(sql, parameters)
    except (sqlite3.Error, OverflowError):
        # from None: the underlying error's text and traceback can carry stored values.
        raise LifecycleError(
            "the ledger rejected this write (detail withheld: a database message can "
            "echo stored values)"
        ) from None
    rowid = cursor.lastrowid
    if rowid is None:  # pragma: no cover - INSERT always sets lastrowid
        raise LifecycleError("the ledger did not report an identifier for this write")
    return rowid


def _fetch_one(
    conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> sqlite3.Row | None:
    try:
        row = conn.execute(sql, parameters).fetchone()
    except (sqlite3.Error, OverflowError):  # OverflowError: see _insert
        raise LifecycleError(
            "the ledger could not be read (detail withheld: a database message can "
            "echo stored values)"
        ) from None
    return cast("sqlite3.Row | None", row)


def _fetch_all(
    conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> list[sqlite3.Row]:
    try:
        rows = conn.execute(sql, parameters).fetchall()
    except (sqlite3.Error, OverflowError):  # OverflowError: see _insert
        raise LifecycleError(
            "the ledger could not be read (detail withheld: a database message can "
            "echo stored values)"
        ) from None
    return cast("list[sqlite3.Row]", rows)
