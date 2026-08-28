"""Appending forecast versions to the ledger (M1-602).

The writer for ``forecast_records``, the table ``001_initial.sql`` shipped and that no
production code had ever written a row to. :mod:`whiskeyjack_bot.forecast.record` is the
record; this module is what puts one in the ledger and never takes it out again.

**The acceptance criterion is "updating a question appends v2; v1 remains byte-identical",
and byte-identical is meant literally.** A new forecast for a question that already has one
is a *new row* whose ``parent_record_id`` names the previous version. The earlier row is
not read, not re-hashed and not touched; ``003_lifecycle_events.sql`` blocks UPDATE and
DELETE on this table outright, so the guarantee is the schema's rather than this module's
good behaviour (D25).

**Version and parent are decided by the ledger, never by the caller.** A caller that
passed its own ``forecast_version`` would be asserting what the chain looks like from
outside the transaction that can see it. :func:`append_forecast_version` reads the current
head and derives both, inside one ``BEGIN IMMEDIATE`` -- the lock is taken up front for the
reason :func:`whiskeyjack_bot.lifecycle.transaction` gives: two writers that both read
"the head is v1" would both mint v2, and a deferred BEGIN discovers that only on a lock
upgrade that cannot be retried from inside an open transaction. ``001``'s
``UNIQUE (question_id, tournament_id, forecast_version)`` is the second line of defence and
turns any race that does occur into a loud failure rather than a forked chain.

**Every insert is refused twice.** Migration ``007`` carries the same version/parent rules
as SQL, and ``006``/``004``/``003`` carry the identifier, ``attempt_id``, draft-status and
hash rules. This module checks what it can check for a readable message; the schema is the
binding one. That is not redundancy for its own sake -- M1-603's round 5 is a rule that
lived in the writer and not the schema, defeated by a value the two layers disagreed
about, on an append-only table that could not then be corrected.

**Attribution is checked before the row exists, not after.** A response citing a source it
was never given is exactly the record this project exists to prevent, and once appended it
cannot be withdrawn. :func:`whiskeyjack_bot.forecast.attribution.validate_attribution_fields`
is M1-501's public entry point and names "a validation pass over a stored record" as its
use; running it one moment earlier costs nothing and is the last point at which refusing is
still possible.

**It is the cross-type half only, and that gap is M1-507.** Since M1-506 there is one
composed entry point, ``forecast.validate.validate_output``, which runs the attribution
rules *and* the rules specific to the response's question type. This module does not call
it, because the type-specific checkers need a ``ForecastConfig`` and
:func:`append_forecast_version` has no parameter for one -- so a probability outside the
configured ``forecast.min_probability``/``max_probability`` can be persisted here even
though ``forecast.generate`` refuses it. Not reachable from the product path
(``persist_generation`` runs the full composed check inside the attempt loop before
anything reaches this writer), reachable by any other caller of this public entry point,
and widening when M1-404 registers its checker.

**M1-405 both widened that gap and shrank what closing it costs.** Widened: a numeric
record whose percentiles are not the declared nine, are out of order, or fall outside the
question's bounds is now refused by ``forecast.generate`` and still persistable here.
Shrank: the composed entry point takes a ``CanonicalQuestion`` rather than a bare
``question_id``, and ``ForecastRecordDraft.question`` already carries one -- validated by
``_one_question`` to agree with the row's own ``question_id`` and ``question_type``. So
M1-507 needs only the ``ForecastConfig`` threaded in; the question it would otherwise have
had to thread as well is already inside the argument this writer is handed. Closing it is
still a signature change to a merged, reviewed public entry point, which is why M1-506
filed the row rather than taking it -- same convention as M1-314, M2-709 and M1-608.

**What this module does not do.** It does not append a ``validated`` lifecycle event. A
record is born ``draft`` and every later state is reachable only through
``lifecycle_events``; ``validated`` means the full output-validation gate passed, and that
gate is M1-504 with M1-404, M1-502 and M1-503 still to land. Asserting it here
would put a claim in an append-only ledger for checks that do not exist yet.
:func:`whiskeyjack_bot.lifecycle.transaction` nests as a ``SAVEPOINT``, so a caller that
wants the record and its first event in one unit can have it without this module deciding
that on its behalf.

Purely local SQLite: no network access on any path through here.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from whiskeyjack_bot.config import MAX_MODEL_INVOCATIONS
from whiskeyjack_bot.forecast.attribution import AttributionFieldError, validate_attribution_fields
from whiskeyjack_bot.forecast.record import (
    ForecastRecord,
    ForecastRecordDraft,
    ForecastRecordError,
    assign_identity,
    canonical_final_prediction_json,
    canonical_record_json,
    record_from_json,
    record_sha256,
    require_unassigned_draft,
)
from whiskeyjack_bot.lifecycle import LifecycleError, transaction

# The columns this module writes but does not derive from the record.
#
# `status` is the literal `'draft'`: it is not a parameter and there is no branch that
# could make it anything else. `003`'s trigger refuses a non-draft insert, and a writer
# able to *request* another state would be a writer able to record an approval that never
# happened. `created_at_utc` is writer-owned -- when the ledger stored the row, as distinct
# from `generated_at_utc`, which is when the pipeline produced it and is caller-supplied so
# a replay can reproduce it.
#
# M1-406's three are caller-owned rather than writer-owned, and they are here rather than in
# the record for one reason: `RECORD_SCHEMA_VERSION` is a promise about bytes already
# written. Adding a field to the record changes every future `forecast_sha256` while stored
# records keep their old ones, so an approval bound to one stops verifying -- and
# `test_the_record_carries_exactly_the_contracted_fields` asserts the field set as *set
# equality* precisely so that change cannot be made quietly. Where the artifact landed, what
# the call cost and how many invocations it took are facts about the call, not part of the
# forecast's content, so they are indexed beside the record instead of hashed into it.
_WRITER_OWNED_COLUMNS = (
    "status",
    "created_at_utc",
    "raw_output_path",
    "cost_usd",
    "model_invocations",
)

# The M1-406 columns, read back. Same tuple, same order, used by the reader below.
_MODEL_CALL_COLUMNS = ("raw_output_path", "cost_usd", "model_invocations")

# `008` caps this column at 200 characters, the ceiling `_require_identifier` and `006`
# already apply to every identifier the readers look a record up by.
_MAX_PATH_LENGTH = 200


@dataclass(frozen=True)
class ModelCall:
    """What the model call cost and where its raw output landed (M1-406).

    One type for both directions -- passed to :func:`append_forecast_version` and returned
    by :func:`read_model_call` -- so a round trip is comparable without a translation step
    that could disagree with itself.

    Every field defaults to ``None`` and every ``None`` is a real answer rather than a gap:

    - ``raw_output_path is None`` means no artifact is recorded for this row. Either
      retention was off, or the write failed and ``forecast/persist.py`` committed the row
      anyway rather than lose a call that cost money. **Which of the two is not stored
      here**, and deliberately: it is reported to the caller at write time, and a column
      that guessed would be a claim the ledger cannot stand behind.
    - ``cost_usd is None`` means **unknown, not free** -- the M1-303 rule.
      ``generate_forecast`` publishes a total only when every attempted call reported a
      usable figure; anything less is a subtotal that looks exactly like a complete one.
    - ``model_invocations is None`` means nobody recorded it, which is only reachable for a
      row written before ``008``.

    Validated in ``__post_init__`` so a caller learns the shape is wrong at construction
    rather than from inside the writer's transaction -- the "refuse before you spend" shape
    M1-303 settled, applied to an append-only row instead of to money. ``008``'s trigger is
    still the binding layer; this one is the readable message.
    """

    raw_output_path: str | None = None
    cost_usd: float | None = None
    model_invocations: int | None = None

    def __post_init__(self) -> None:
        path = self.raw_output_path
        if path is not None:
            if type(path) is not str or not path.strip() or len(path) > _MAX_PATH_LENGTH:
                raise ForecastRecordError(
                    "raw_output_path must be None or non-blank text of at most "
                    f"{_MAX_PATH_LENGTH} characters"
                )
            if path.startswith("/") or "\x00" in path or ".." in f"/{path}/".split("/"):
                raise ForecastRecordError(
                    "raw_output_path must be a relative path inside the artifact root, "
                    "with no parent-directory segment"
                )
        cost = self.cost_usd
        if cost is not None:
            # `type() is` rather than `isinstance`: bool subclasses int, and `True`
            # would otherwise be stored as a cost of one dollar.
            if type(cost) is not float and type(cost) is not int:
                raise ForecastRecordError("cost_usd must be None or a number")
            if not isfinite(cost) or cost < 0:
                raise ForecastRecordError("cost_usd must be None or a finite, non-negative number")
        calls = self.model_invocations
        if calls is not None:
            if type(calls) is not int:
                raise ForecastRecordError("model_invocations must be None or an int")
            if not 1 <= calls <= MAX_MODEL_INVOCATIONS:
                raise ForecastRecordError(
                    f"model_invocations must be None or between 1 and {MAX_MODEL_INVOCATIONS}"
                )


def _projection(record: ForecastRecord) -> dict[str, Any]:
    """Every ``forecast_records`` column this module derives from the record.

    **One definition, used to write the row and to check it on the way back out.** Round 1,
    finding B3: the reader compared five identity columns, so a row whose `question_type`
    column said `numeric` while its `record_json` described a binary forecast was returned
    as binary -- while `approval.read_forecast_summary`, which reads the column, reported
    numeric. Two public readers giving incompatible attribution for one immutable record.

    Comparing "the columns the writer derives" against "the record" is only a real check if
    the two lists cannot drift, so there is one list and it is this function.
    `test_the_reader_checks_every_column_the_writer_derives` pins it against the table's own
    column set, so a column added to `forecast_records` and written here without being
    compared fails rather than silently reopening the finding.
    """
    return {
        "record_id": record.record_id,
        "question_id": record.question_id,
        "post_id": record.post_id,
        "tournament_id": record.tournament_id,
        "forecast_version": record.forecast_version,
        "parent_record_id": record.parent_record_id,
        "question_type": record.question_type,
        "question_domain": record.question_domain,
        "model_provider": record.model_settings.provider,
        "model_name": record.model_settings.name,
        "prompt_version": record.model_settings.prompt_version,
        "prompt_sha256": record.model_settings.prompt_sha256,
        "retrieval_run_id": record.retrieval_run_id,
        "generated_at_utc": _utc_text(record.generated_at_utc),
        "final_prediction_json": canonical_final_prediction_json(record),
        "record_json": canonical_record_json(record),
        "forecast_sha256": record_sha256(record),
        "attempt_id": record.attempt_id,
    }


# The order the reader selects in. Spelled out rather than `SELECT *` for the reason
# approval.py and lifecycle.py give for their own column lists: a later ALTER TABLE must not
# be able to reorder it silently. Derived from the projection so the two cannot disagree.
_PROJECTED_COLUMNS: tuple[str, ...] = (
    "record_id",
    "question_id",
    "post_id",
    "tournament_id",
    "forecast_version",
    "parent_record_id",
    "question_type",
    "question_domain",
    "model_provider",
    "model_name",
    "prompt_version",
    "prompt_sha256",
    "retrieval_run_id",
    "generated_at_utc",
    "final_prediction_json",
    "record_json",
    "forecast_sha256",
    "attempt_id",
)

_RECORD_COLUMNS = ", ".join(_PROJECTED_COLUMNS)
_INSERT_COLUMNS = _PROJECTED_COLUMNS + _WRITER_OWNED_COLUMNS

# The timestamp format the rest of the ledger uses: `lifecycle._utc_text` and
# `research.store._utc_text` both write `isoformat()` on a UTC-aware datetime, and 003's
# GLOB pins its 32-character shape. Written the same way here so the columns sort together.
_UTC_MICROSECONDS = "%Y-%m-%dT%H:%M:%S.%f+00:00"

# The largest value SQLite can hold in an INTEGER column. A head already at this value
# cannot be incremented into a storable one, and `sqlite3` raises a raw `OverflowError` at
# bind time rather than anything this module owns (round 1, finding B5). Reachable without
# a hostile operator: `001`-`006` accepted such a `forecast_version`, and `007` deliberately
# adds no backfill probe, so an upgraded ledger can still hold one.
_SQLITE_INT_MAX = 2**63 - 1

_UUID7_VERSION = 0x7
_UUID7_VARIANT = 0b10
_COUNTER_BITS = 12
_COUNTER_MAX = (1 << _COUNTER_BITS) - 1
# Seeded in the low half of the counter's range, so there are always at least 2048
# increments of headroom inside one millisecond before the rollover branch is reached.
_COUNTER_SEED_MASK = _COUNTER_MAX >> 1

_MINT_LOCK = threading.Lock()
_last_milliseconds = -1
_counter = 0


def mint_record_id() -> str:
    """Return a fresh UUIDv7, rendered in the canonical hyphenated form.

    ``CODEX_HANDOFF.md`` asks for UUIDv7/ULID for ``record_id`` and M1-601 deferred the
    choice to this item. UUIDv7 (RFC 9562 § 5.7) is built here from ``os.urandom`` and a
    millisecond timestamp rather than taken from a library: ``uuid.uuid7`` is Python 3.14
    and this project pins 3.11, and a dependency for eighteen bits of layout would cost the
    wave's single ``uv.lock`` slot for nothing.

    Layout, most significant bit first: 48 bits of Unix milliseconds, 4 bits of version,
    12 bits of a **counter**, 2 bits of variant, 62 bits of randomness.

    **The counter is the point, and the first version of this function did not have one.**
    A plain UUIDv7 orders only across milliseconds: two ids minted inside the same
    millisecond carry independent random draws and sort in whichever order the draws fell.
    That is exactly the case this project produces -- appending five versions in a loop, or
    a test building a chain -- so "record ids sort in creation order" would have been a
    claim that failed on its most common input. Caught by
    ``test_minted_ids_are_distinct_and_sort_in_creation_order``, which is why that test
    mints in a tight loop rather than with a sleep between draws.

    So RFC 9562 § 6.2's "Fixed Bit-Length Dedicated Counter" method is used: ``rand_a``
    holds a counter, seeded randomly at each new millisecond and incremented for every id
    minted inside it. The pair ``(milliseconds, counter)`` is therefore strictly increasing,
    and since the rendering is fixed-width lowercase hex, so is the string -- ``ORDER BY
    record_id`` is ``ORDER BY`` creation order, with no join to ``created_at_utc``.

    Two edges, both handled by holding ``_last_milliseconds`` rather than trusting the
    clock: a **backwards clock** (NTP correction, a suspended laptop) reuses the last
    millisecond and increments, and an **exhausted counter** advances the stored
    millisecond by one. Both trade a slightly-wrong timestamp for an id that is still
    unique and still ordered, which is the right way round -- the timestamp is a sorting
    aid, while uniqueness is a primary key.

    The lock makes the read-modify-write of the module state atomic. sqlite3 connections
    are commonly used from one thread, but nothing here enforces that, and two threads
    interleaving inside this function would hand out the same counter value twice.
    """
    global _last_milliseconds, _counter
    with _MINT_LOCK:
        milliseconds = time.time_ns() // 1_000_000
        if milliseconds > _last_milliseconds:
            _last_milliseconds = milliseconds
            _counter = int.from_bytes(os.urandom(2), "big") & _COUNTER_SEED_MASK
        elif _counter < _COUNTER_MAX:
            _counter += 1
        else:
            _last_milliseconds += 1
            _counter = int.from_bytes(os.urandom(2), "big") & _COUNTER_SEED_MASK
        milliseconds = _last_milliseconds
        counter = _counter
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (milliseconds & ((1 << 48) - 1)) << 80
    value |= _UUID7_VERSION << 76
    value |= counter << 64
    value |= _UUID7_VARIANT << 62
    value |= rand_b
    return str(uuid.UUID(int=value))


def _utcnow_text() -> str:
    return datetime.now(timezone.utc).strftime(_UTC_MICROSECONDS)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(_UTC_MICROSECONDS)


def _require_connection(conn: object) -> sqlite3.Connection:
    if not isinstance(conn, sqlite3.Connection):
        raise ForecastRecordError("conn must be a sqlite3.Connection")
    return conn


def _require_draft(draft: object) -> ForecastRecordDraft:
    """Refuse anything that is not exactly a draft, a persisted record included.

    ``record.require_unassigned_draft`` rather than a second copy of the rule: round 1's
    finding B4 was that this check existed here and *not* in ``assign_identity``, which is
    what a rule written twice does. One definition, two callers.
    """
    return require_unassigned_draft(draft)


def append_forecast_version(
    conn: sqlite3.Connection,
    *,
    draft: ForecastRecordDraft,
    call: ModelCall | None = None,
) -> ForecastRecord:
    """Append ``draft`` as the next forecast version for its question and tournament.

    Returns the persisted :class:`ForecastRecord`, carrying the identity the ledger
    assigned: a fresh ``record_id``, ``forecast_version`` one above the current head, and
    ``parent_record_id`` naming that head (``None`` for the first version).

    ``call`` is M1-406's three columns -- where the raw model output landed, what the call
    cost, how many invocations it took. It is optional and defaults to an all-``None``
    :class:`ModelCall` because those columns are nullable and because a caller that has no
    artifact still has a record to store; :func:`whiskeyjack_bot.forecast.persist.persist_generation`
    is what fills it in on the paid path. The default is constructed here rather than in the
    signature: a mutable-looking default in a signature is a trap even when the object is
    frozen, and this one is cheap.

    Raises :class:`ForecastRecordError` and nothing else. Caller mistakes -- a wrong type,
    a response citing an unresolvable source -- are refused before the transaction opens,
    the rule M1-303 round 4 settled for calls that cost something: here what is being
    protected is not money but an append-only row.
    """
    connection = _require_connection(conn)
    validated = _require_draft(draft)
    _require_attributable(validated)
    if call is None:
        call = ModelCall()
    elif type(call) is not ModelCall:
        raise ForecastRecordError("call must be a ModelCall")

    try:
        with transaction(connection):
            head = _current_head(connection, validated)
            if head is None:
                version, parent = 1, None
            else:
                if head[1] >= _SQLITE_INT_MAX:
                    raise ForecastRecordError(
                        "this question's forecast chain is already at the largest version "
                        "the ledger can store; no further version can be appended"
                    )
                version, parent = head[1] + 1, head[0]
            record = assign_identity(
                validated,
                record_id=mint_record_id(),
                forecast_version=version,
                parent_record_id=parent,
            )
            _insert(connection, record, call)
            return record
    except LifecycleError as exc:
        # Message preserved rather than replaced, the rule approval.py settled for the same
        # wrapping: LifecycleError's own contract guarantees its text names no value, and
        # "open the connection with ledger.connect()" is the one thing that makes a
        # transaction-mode failure actionable.
        raise ForecastRecordError(str(exc)) from None


def _require_attributable(draft: ForecastRecordDraft) -> None:
    """Run M1-501's cross-type attribution gate over the response about to be stored.

    Re-raised with the message preserved, for approval.py's reason: ``AttributionFieldError``
    carries a list of field paths and value-free messages, and that list is the entire
    account of why the record was refused. Replacing it with a constant would satisfy the
    letter of the module-own-error rule while destroying what the operator needs.
    """
    try:
        validate_attribution_fields(
            draft.forecast,
            question_id=draft.question_id,
            source_ids=[source.source_id for source in draft.sources],
        )
    except AttributionFieldError as exc:
        raise ForecastRecordError(str(exc)) from None


def _current_head(conn: sqlite3.Connection, draft: ForecastRecordDraft) -> tuple[str, int] | None:
    """The highest existing version of this question in this tournament, if any.

    Read inside the caller's ``BEGIN IMMEDIATE``, which is what makes "the highest" still
    true by the time the insert lands.
    """
    row = _fetch_one(
        conn,
        "SELECT record_id, forecast_version FROM forecast_records "
        "WHERE question_id = ? AND tournament_id = ? "
        "ORDER BY forecast_version DESC LIMIT 1",
        (draft.question_id, draft.tournament_id),
    )
    if row is None:
        return None
    return _stored_text(row[0], "record_id"), _stored_int(row[1], "forecast_version")


def _insert(conn: sqlite3.Connection, record: ForecastRecord, call: ModelCall) -> None:
    """Write the row. Every column the record describes is derived from it, never passed in.

    That is what makes ``forecast_sha256`` mean something: the hash digests the canonical
    JSON of the whole record, and every indexed column beside it is a projection of that
    same object. A column disagreeing with ``record_json`` is unrepresentable here rather
    than merely unlikely.

    ``status`` is pinned to ``'draft'`` as a literal. It is not a parameter and there is no
    branch that could make it anything else: ``003``'s trigger refuses a non-draft insert,
    and a writer able to *request* another state would be a writer able to record an
    approval that never happened.

    M1-406's three columns are the one thing that *is* passed in, and they are outside the
    guarantee above by construction: they describe the call, not the forecast, so no
    projection of the record could produce them. ``008``'s trigger is what constrains them,
    and it is the binding layer here in the same sense ``007`` is for the version chain.
    """
    values: dict[str, Any] = {
        **_projection(record),
        "status": "draft",
        "created_at_utc": _utcnow_text(),
        "raw_output_path": call.raw_output_path,
        "cost_usd": call.cost_usd,
        "model_invocations": call.model_invocations,
    }
    columns = ", ".join(_INSERT_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in _INSERT_COLUMNS)
    try:
        conn.execute(f"INSERT INTO forecast_records ({columns}) VALUES ({placeholders})", values)
    except sqlite3.IntegrityError as exc:
        # The schema's own messages name a column and never a value -- 003/004/006/007 are
        # written that way deliberately -- so the text is carried through for the same
        # reason approval.py carries LifecycleError's. A UNIQUE violation here means a
        # concurrent writer won the race for this version number, and saying which
        # constraint failed is what makes that diagnosable.
        raise ForecastRecordError(f"the forecast record was refused by the ledger: {exc}") from None
    except sqlite3.Error:
        raise ForecastRecordError(
            "the forecast record could not be written to the ledger"
        ) from None


def read_forecast_record(conn: sqlite3.Connection, record_id: str) -> ForecastRecord:
    """Return the stored record, refusing any row that does not attest to itself.

    Three things are checked, and none of them is paranoia about a hostile operator --
    they are the checks that make a *read* of an append-only ledger mean anything:

    1. ``record_json`` parses back and re-renders to exactly the stored bytes
       (:func:`~whiskeyjack_bot.forecast.record.record_from_json`);
    2. the stored ``forecast_sha256`` equals the hash of what was read, so the value an
       approval binds to is the value that describes this record;
    3. the indexed columns agree with the record they index. ``001`` stores identity in
       both places and only this comparison keeps them in step -- a row whose
       ``forecast_version`` column says 2 while its ``record_json`` says 3 would otherwise
       be read as a valid v3 and exported as a valid v2.

    Raises when ``record_id`` names no stored record, matching
    ``approval.read_forecast_summary``, ``submission.submission_key_for_record`` and
    ``lifecycle.current_status``: a caller that could not tell "no such record" from
    "nothing recorded yet" would report the wrong one.
    """
    connection = _require_connection(conn)
    if type(record_id) is not str:
        raise ForecastRecordError("record_id must be a string")
    row = _fetch_one(
        connection,
        f"SELECT {_RECORD_COLUMNS} FROM forecast_records WHERE record_id = ?",
        (record_id,),
    )
    if row is None:
        raise ForecastRecordError("record_id does not name a stored forecast record")
    return _record_from_row(row)


def read_model_call(conn: sqlite3.Connection, record_id: str) -> ModelCall:
    """Return one row's M1-406 columns (M1-406).

    Separate from :func:`read_forecast_record` rather than folded into it, because the two
    answer different questions and only one of them is covered by ``forecast_sha256``. The
    record is the hashed, self-attesting thing; these three columns describe the *call* and
    are outside that hash by design, so a reader that returned them together would invite a
    caller to believe the hash vouched for all of it.

    Raises when ``record_id`` names no stored record, matching :func:`read_forecast_record`:
    a caller that could not tell "no such record" from "that record recorded no cost" would
    report the wrong one -- and here the second really is a meaningful answer.
    """
    connection = _require_connection(conn)
    if type(record_id) is not str:
        raise ForecastRecordError("record_id must be a string")
    row = _fetch_one(
        connection,
        f"SELECT {', '.join(_MODEL_CALL_COLUMNS)} FROM forecast_records WHERE record_id = ?",
        (record_id,),
    )
    if row is None:
        raise ForecastRecordError("record_id does not name a stored forecast record")
    path, cost, calls = row
    # Rebuilt through `ModelCall`, whose `__post_init__` is the writer's own rule -- so a
    # row that a raw-SQL writer put an absolute path or a negative cost into is refused on
    # the way out rather than handed back as though `008` had vouched for it. A ledger
    # upgraded past a row written before `008` holds NULLs here, which every rule permits.
    if path is not None and type(path) is not str:
        raise ForecastRecordError("stored raw_output_path is not text")
    if cost is not None and type(cost) is not float and type(cost) is not int:
        raise ForecastRecordError("stored cost_usd is not a number")
    if calls is not None and (type(calls) is not int or isinstance(calls, bool)):
        raise ForecastRecordError("stored model_invocations is not an integer")
    return ModelCall(raw_output_path=path, cost_usd=cost, model_invocations=calls)


def latest_forecast_version(
    conn: sqlite3.Connection, *, question_id: int, tournament_id: str
) -> ForecastRecord | None:
    """The current head of one question's version chain, or ``None`` if it has none.

    ``None`` means "no forecast has ever been stored for this question in this
    tournament", which is a different answer from the refusal
    :func:`read_forecast_record` gives for an unknown ``record_id`` -- here the absence is
    the answer a caller asked for.
    """
    connection = _require_connection(conn)
    if type(question_id) is not int or isinstance(question_id, bool):
        raise ForecastRecordError("question_id must be an int")
    if type(tournament_id) is not str:
        raise ForecastRecordError("tournament_id must be a string")
    row = _fetch_one(
        connection,
        f"SELECT {_RECORD_COLUMNS} FROM forecast_records "
        "WHERE question_id = ? AND tournament_id = ? "
        "ORDER BY forecast_version DESC LIMIT 1",
        (question_id, tournament_id),
    )
    if row is None:
        return None
    return _record_from_row(row)


def _record_from_row(row: tuple[Any, ...]) -> ForecastRecord:
    """Parse the row's record and refuse it unless every projected column agrees with it.

    The comparison is against :func:`_projection` -- the same function that wrote the row --
    so "the columns agree with the record" is checked over the writer's whole output rather
    than over a hand-picked subset (round 1, finding B3).

    ``forecast_sha256`` is compared first and separately, because a mismatch there means
    something different from a mismatch anywhere else: the record does not attest to itself,
    so an approval bound to the stored hash was bound to a record nobody can reproduce.
    """
    stored = dict(zip(_PROJECTED_COLUMNS, row, strict=True))
    record = record_from_json(_stored_text(stored["record_json"], "record_json"))
    if _stored_text(stored["forecast_sha256"], "forecast_sha256") != record_sha256(record):
        raise ForecastRecordError(
            "the stored forecast_sha256 does not match the stored record_json"
        )
    if stored != _projection(record):
        # Which column disagreed is deliberately not named: the columns hold question text,
        # model names and provider-derived identifiers, and naming one invites naming its
        # value next.
        raise ForecastRecordError(
            "the stored forecast_records columns do not match the stored record_json"
        )
    return record


def _fetch_one(
    conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> tuple[Any, ...] | None:
    try:
        cursor = conn.execute(sql, parameters)
    except sqlite3.Error:
        raise ForecastRecordError("the ledger could not be read") from None
    try:
        row = cursor.fetchone()
    except sqlite3.Error:
        # sqlite3 decodes TEXT at fetch, not at execute (M1-306): a column holding bytes
        # that are not valid UTF-8 raises here rather than above.
        raise ForecastRecordError("the ledger could not be read") from None
    finally:
        cursor.close()
    if row is None:
        return None
    return tuple(row)


def _stored_text(value: object, field: str) -> str:
    if type(value) is not str:
        raise ForecastRecordError(f"stored {field} is not text")
    return value


def _stored_int(value: object, field: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise ForecastRecordError(f"stored {field} is not an integer")
    return value
