"""Persisting and replaying retrieval runs (M1-306).

The ledger's evidence tables have never had a writer. ``research_runs`` and
``research_documents`` were defined by M1-601 and completed by 002/003, both retrieval
adapters produce validated rows for them, and nothing has ever inserted one. This
module is that writer, its readers, and the replay entry the item's acceptance
criterion is about: **replay produces zero provider calls and the same research packet
hash**.

Zero provider calls is structural here, not a mock count. Replay reads SQLite; this
module imports no provider SDK, no HTTP client and nothing that opens a socket. The
packet hash is :mod:`whiskeyjack_bot.research.packet`'s, computed from rows read back
out of the ledger, which is what makes "the same hash" an assertion about stored
evidence rather than about objects that happen to still be in memory.

**A run is written in two phases**, which is the shape 003 was built to permit (it
pins a run's identity and provenance and deliberately leaves the completion columns
writable). :func:`open_run` inserts identity and the start time *before* the billable
calls; :func:`complete_run` fills in what the run learned. The reason is the one
M1-302's first review round established at the adapter level and this closes one level
up: a run makes up to ``max_queries_per_question * 2`` billable calls, the adapters no
longer raise a mid-run failure away, and a single terminal insert would still lose the
record of every paid call if the process died before it. Opening the row first means
the spend stays attributable even when the process does not survive to describe it.
:func:`persist_retrieval` is the one-shot composition for a caller that already holds
a completed run.

**Replay reads the normalized rows, never the raw artifacts.** Re-normalizing stored
provider bodies would make a replayed packet depend on adapter *code version*, so
fixing a bug in a ``_to_document`` would silently re-derive every historical forecast's
evidence -- the ledger rewriting its own history on a refactor, which is what D25 and
003's append-only triggers exist to prevent. Raw artifacts
(:mod:`whiskeyjack_bot.research.artifacts`) are the evidence that the rows came from a
real provider response; the rows are the record.

Documents are deduplicated before insert with :func:`whiskeyjack_bot.research.dedup.deduplicate`,
whose key *is* ``UNIQUE (retrieval_run_id, canonical_url, content_sha256)``. That is
constraint safety on a path where the money is already spent -- the AskNews current and
historical passes overlap by design -- not a second dedup policy.

Error hygiene follows ``LedgerError``/``LifecycleError``: :class:`StoreError` never
echoes a stored value, sanitizing raises use ``from None``, and **every** malformed
shape arrives as a :class:`StoreError` rather than a raw ``sqlite3.Error``,
``ResearchSchemaError``, ``LifecycleError`` or ``UnicodeEncodeError``. One of those is
not hypothetical: a lone surrogate in a document field makes ``sqlite3`` raise a
``UnicodeEncodeError`` **that quotes the offending character**, so it is a leak channel
as well as an escaping exception -- see :func:`_require_storable_text`.

Purely local file I/O: no network access on any path through here.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from whiskeyjack_bot.lifecycle import LifecycleError, transaction
from whiskeyjack_bot.research.dedup import deduplicate
from whiskeyjack_bot.research.model import (
    ResearchDocument,
    ResearchRun,
    ResearchSchemaError,
    validate_document,
    validate_run,
)
from whiskeyjack_bot.research.packet import ResearchPacket, build_packet

if TYPE_CHECKING:
    from whiskeyjack_bot.config import AppConfig

_RUN_COLUMNS = (
    "retrieval_run_id",
    "question_id",
    "provider",
    "provider_config_json",
    "queries_json",
    "started_at_utc",
    "completed_at_utc",
    "freshness_cutoff_utc",
    "raw_response_path",
    "error_summary",
    "cost_usd",
    "agent_model",
    "posts_dropped_no_url",
    "documents_dropped",
    "duplicates_collapsed",
    "created_at_utc",
)

_DOCUMENT_COLUMNS = (
    "document_id",
    "retrieval_run_id",
    "original_url",
    "canonical_url",
    "title",
    "publisher",
    "author",
    "published_at_utc",
    "updated_at_utc",
    "retrieved_at_utc",
    "source_type",
    "provenance",
    "content_sha256",
    "snippet",
    "summary",
    "raw_artifact_path",
    "reliability_tag",
)

# The columns complete_run may write. Everything absent from this tuple is either
# identity that 003 pins or was fixed when the row was opened; keeping the list here
# and building the UPDATE from it means the module cannot drift into writing a column
# the schema would refuse.
_COMPLETION_COLUMNS = (
    "completed_at_utc",
    "provider_config_json",
    "queries_json",
    "freshness_cutoff_utc",
    "raw_response_path",
    "error_summary",
    "cost_usd",
    "agent_model",
    "posts_dropped_no_url",
    "documents_dropped",
    "duplicates_collapsed",
)


class StoreError(Exception):
    """A retrieval run or its documents cannot be written to, or read from, the ledger.

    Same hygiene rule as ``LedgerError``: the message never echoes a stored value, a
    query, a provider config or a document's text, and wrapped raises use
    ``from None`` so an underlying exception cannot reprint one through its text or a
    rendered traceback.
    """


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _utc_text(value: datetime) -> str:
    """Render an aware datetime in the ledger's canonical stored form.

    ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``, matching ``lifecycle._utc_text`` exactly.
    The research tables carry no GLOB pinning it the way 003 pins the lifecycle
    columns, so this is uniformity rather than a constraint -- but a ledger with two
    timestamp renderings orders inconsistently the moment anything compares the text,
    and "uniform where checked" is how that gets discovered late.

    Microseconds are always present because plain ``isoformat()`` omits a zero
    fractional part, which makes the rendered width vary.
    """
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _optional_utc_text(value: datetime | None) -> str | None:
    return None if value is None else _utc_text(value)


def _parse_utc(value: object, field: str) -> datetime | None:
    """Read a stored timestamp back, or fail as this module's own error type.

    Values read out of the ledger are untrusted (CLAUDE.md's threat boundary), and
    ``fromisoformat`` raises a ``ValueError`` that quotes the offending string.
    """
    if value is None:
        return None
    if type(value) is not str:
        raise StoreError(f"stored {field} is not text (detail withheld: it can echo a value)")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise StoreError(
            f"stored {field} is not an ISO-8601 timestamp "
            "(detail withheld: it can echo a stored value)"
        ) from None
    if parsed.tzinfo is None:
        raise StoreError(f"stored {field} has no timezone offset")
    return parsed


def _require_storable_text(value: object, field: str) -> None:
    """Refuse text SQLite cannot store, before it reaches a raw ``UnicodeEncodeError``.

    ``sqlite3`` encodes TEXT parameters as UTF-8, and a **lone surrogate** cannot be
    encoded: the insert raises ``UnicodeEncodeError: 'utf-8' codec can't encode
    character '\\ud800'``. That is two defects at once -- a raw exception escaping a
    module that contracts to raise only its own type, and a message quoting untrusted
    provider text.

    Lone surrogates are reachable here, not hypothetical: ``json.loads`` accepts
    ``"\\ud800"`` and hands back a Python string containing it, so any provider body
    can put one in a title, snippet or summary. (``content_sha256`` has the same
    exposure on the *hashed* text and is an open owner decision; this is the same
    family of defect at the storage boundary, refused rather than left to surface as
    whatever SQLite raises.)

    Refusing rather than escaping is deliberate. Escaping would change stored content
    and therefore document identity, so the row would no longer be what was retrieved.
    The run's raw artifact is written before the ledger row, so the evidence still
    exists on disk when this fires.
    """
    if value is None:
        return
    if type(value) is not str:
        raise StoreError(f"{field} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # from None and no value: the underlying message quotes the character.
        raise StoreError(
            f"{field} contains a lone surrogate and cannot be stored as text "
            "(offending input withheld)"
        ) from None


def _require_storable_json(value: object, field: str) -> None:
    """Refuse text inside a JSON column that cannot survive the round trip.

    ``queries`` and ``provider_config`` are stored as JSON with
    ``ensure_ascii=True``, and an earlier version of this module claimed that made
    them immune to the surrogate problem that ``_require_storable_text`` guards the
    TEXT columns against. That was true of *lone* surrogates and false of
    **surrogate pairs**, which is the more dangerous half:

    - ``"\ud800\udc00"`` is two Python code points and is **not** UTF-8 encodable;
    - ``json.dumps(..., ensure_ascii=True)`` writes it as ``"\ud800\udc00"`` and
      ``json.loads`` **recombines it** into the single scalar ``U+10000`` -- so what
      comes back out of the ledger is a *different Python string* than what went in;
    - and pydantic's ``model_dump(mode="json")`` renders the original pair as six
      ``U+FFFD`` replacement characters, so the in-memory packet hashes over garbage
      while the stored packet hashes over the clean scalar.

    The two therefore hash differently, which falsifies the acceptance criterion for
    an input the schema accepts. Found by the SQLite round-trip property added in
    response to review round 1 -- the JSON-simulated round trip could not see it,
    because JSON is precisely the half that behaves.

    Refusing rather than normalizing, for the reason ``_require_storable_text``
    gives: rewriting the value would store something other than what the caller
    supplied. One check covers both spellings, since neither encodes to UTF-8.
    """
    if isinstance(value, str):
        _require_storable_text(value, field)
    elif isinstance(value, dict):
        for key, nested in value.items():
            _require_storable_text(key, f"{field} key")
            _require_storable_json(nested, field)
    elif isinstance(value, list):
        for item in value:
            _require_storable_json(item, field)


def _checked_json(value: object, field: str) -> object:
    """``_require_storable_json`` as an expression, so the check cannot be skipped."""
    _require_storable_json(value, field)
    return value


def _canonical_json(value: object, field: str) -> str:
    """Serialize a run's JSON-column value in a form that round-trips exactly.

    The same rendering rule the packet hash uses, for the same reason: what is read
    back has to equal what was written, or a replayed packet hashes differently from
    the one that was stored. ``ensure_ascii`` also makes a lone surrogate storable
    here (as an escape sequence) where a bare TEXT column could not hold one.
    """
    try:
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        raise StoreError(
            f"{field} could not be rendered as JSON (detail withheld: it can echo a value)"
        ) from None


def _load_json(value: object, field: str, *, expect: type | tuple[type, ...]) -> Any:
    """Parse a stored JSON column and require the shape the writer put there.

    ``expect`` is not decoration. Round 1, finding 6: the reader spelled the query
    list as ``_load_json(...) or []``, so a stored ``false``, ``0``, ``""`` or
    ``{}`` all became ``[]`` -- a malformed row silently *rewritten* into a
    schema-valid run with no queries, which then hashed as a perfectly good packet.
    Only SQL NULL may take the legacy default; every other shape is a refusal.
    """
    if value is None:
        return None
    if type(value) is not str:
        raise StoreError(f"stored {field} is not text (detail withheld: it can echo a value)")
    try:
        parsed = json.loads(value)
    except ValueError:
        # from None: a JSONDecodeError quotes the surrounding document text.
        raise StoreError(
            f"stored {field} is not valid JSON (detail withheld: it can echo a stored value)"
        ) from None
    if not isinstance(parsed, expect) or isinstance(parsed, bool):
        # bool is excluded explicitly: it subclasses int, so `expect=int` would
        # otherwise accept `true`.
        raise StoreError(
            f"stored {field} does not hold the shape the writer stores there "
            "(detail withheld: it can echo a stored value)"
        )
    return parsed


def _require_count(value: object, field: str) -> int | None:
    """Gate a discarded-evidence counter.

    ``None`` is *unmeasured* and ``0`` is the claim that nothing was discarded; 004
    keeps them distinct on purpose, so this must not coerce one into the other.
    ``type() is int`` rather than ``isinstance``: ``bool`` subclasses ``int``, and
    ``True`` would otherwise be stored as the count 1.
    """
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise StoreError(f"{field} must be None or a non-negative int")
    return value


def _require_run(run: object) -> ResearchRun:
    if not isinstance(run, ResearchRun):
        raise StoreError("run must be a ResearchRun")
    return run


def _run_parameters(
    run: ResearchRun,
    *,
    completed: bool,
    raw_response_path: str | None,
    created_at_utc: str,
) -> dict[str, object]:
    """Build the full column map for a run row, checking every text value first.

    The discarded-evidence counters are read **off the run**, never taken as a
    separate argument. They are hashed into the research packet, so a writer that
    accepted them alongside the model could store a run whose counters differ from
    the one the caller hashed -- and the two packets would then disagree while both
    looked correct. ``raw_response_path`` is the deliberate exception: it is
    excluded from the packet hash precisely because it is a fact about storage
    rather than about the run, so it cannot skew anything by arriving here.
    """
    _require_storable_text(run.retrieval_run_id, "retrieval_run_id")
    _require_storable_text(run.error_summary, "error_summary")
    _require_storable_text(run.agent_model, "agent_model")
    _require_storable_text(raw_response_path, "raw_response_path")
    return {
        "retrieval_run_id": run.retrieval_run_id,
        # Range-checked here rather than left to the binding: the models bound these
        # below zero but not above, so a schema-valid width wider than SQLite's
        # signed 64-bit integer is ordinary accepted input (round 1, finding 5).
        "question_id": _require_bindable_int(run.question_id, "question_id"),
        "provider": run.provider,
        "provider_config_json": (
            None
            if run.provider_config is None
            else _canonical_json(
                _checked_json(run.provider_config, "provider_config"), "provider_config"
            )
        ),
        "queries_json": _canonical_json(_checked_json(list(run.queries), "queries"), "queries"),
        "started_at_utc": _utc_text(run.started_at_utc),
        "completed_at_utc": _optional_utc_text(run.completed_at_utc) if completed else None,
        "freshness_cutoff_utc": _optional_utc_text(run.freshness_cutoff_utc),
        "raw_response_path": raw_response_path,
        "error_summary": run.error_summary,
        "cost_usd": run.cost_usd,
        "agent_model": run.agent_model,
        "posts_dropped_no_url": _optional_bindable_int(
            run.posts_dropped_no_url, "posts_dropped_no_url"
        ),
        "documents_dropped": _optional_bindable_int(
            _require_count(run.documents_dropped, "documents_dropped"), "documents_dropped"
        ),
        "duplicates_collapsed": _optional_bindable_int(
            _require_count(run.duplicates_collapsed, "duplicates_collapsed"),
            "duplicates_collapsed",
        ),
        "created_at_utc": created_at_utc,
    }


def _document_parameters(document: ResearchDocument, *, document_id: str) -> dict[str, object]:
    for field in ("title", "publisher", "author", "snippet", "summary", "raw_artifact_path"):
        _require_storable_text(getattr(document, field), field)
    _require_storable_text(document.original_url, "original_url")
    _require_storable_text(document.canonical_url, "canonical_url")
    return {
        "document_id": document_id,
        "retrieval_run_id": document.retrieval_run_id,
        "original_url": document.original_url,
        "canonical_url": document.canonical_url,
        "title": document.title,
        "publisher": document.publisher,
        "author": document.author,
        "published_at_utc": _optional_utc_text(document.published_at_utc),
        "updated_at_utc": _optional_utc_text(document.updated_at_utc),
        "retrieved_at_utc": _utc_text(document.retrieved_at_utc),
        "source_type": document.source_type,
        "provenance": document.provenance,
        "content_sha256": document.content_sha256,
        "snippet": document.snippet,
        "summary": document.summary,
        "raw_artifact_path": document.raw_artifact_path,
        "reliability_tag": document.reliability_tag,
    }


def _execute(conn: sqlite3.Connection, sql: str, parameters: Sequence[object]) -> sqlite3.Cursor:
    """Run one statement, translating every database failure into a ``StoreError``.

    Includes the trigger failures 002 and 003 raise: a vocabulary violation, a
    social document missing its trust fields, an attempt to re-identify a stored run.
    Their ``RAISE(ABORT, ...)`` messages name fields and never interpolate a row
    value, but they arrive as ``sqlite3.IntegrityError``, and a caller of this module
    handles ``StoreError``.

    **Only ``IntegrityError`` keeps the underlying text.** That distinction is the fix
    for review round 1, finding 5: an integrity failure's message is schema-authored
    (constraint names and our own ``RAISE`` strings), but an ``OperationalError`` can
    quote *data* -- fetching a column holding invalid UTF-8 produces
    ``Could not decode to UTF-8 column 'error_summary' with text '...'``, which
    printed a planted value verbatim. Every other shape now gets a constant message.

    ``sqlite3.Error`` is also not the whole class. Binding a Python ``int`` wider than
    64 bits raises ``OverflowError`` and binding a lone surrogate raises
    ``UnicodeEncodeError`` -- both from ``conn.execute``, neither a ``sqlite3.Error``,
    and the second quotes the character. This is M1-308's round-7 lesson in a different
    library: when a dependency can fail, it fails as a *class*, so enumerate the
    siblings rather than the one type you predicted.
    """
    try:
        return conn.execute(sql, tuple(parameters))
    except sqlite3.IntegrityError as exc:
        # The trigger and constraint text is schema-authored -- it names columns and
        # rules, never row content -- so it is the one part of the underlying error
        # worth keeping, and it is what makes a refusal actionable. from None still,
        # so the exception object itself (which can carry parameter values in a
        # rendered traceback) does not reach a reporter through the cause chain.
        raise StoreError(f"ledger refused a research write: {exc}") from None
    except (sqlite3.Error, OverflowError, UnicodeEncodeError, UnicodeDecodeError, ValueError):
        raise StoreError(
            "the ledger could not execute a research statement "
            "(detail withheld: it can echo a stored or bound value)"
        ) from None


def _fetch_one(
    conn: sqlite3.Connection, sql: str, parameters: Sequence[object]
) -> sqlite3.Row | None:
    """Run a query and take one row, owning the failures ``_execute`` cannot see.

    Decoding happens at **fetch**, not at execute: a TEXT column holding invalid
    UTF-8 -- ordinary foreign-tool-written or corrupted state, which CLAUDE.md
    classifies as untrusted -- raises ``sqlite3.OperationalError`` here, quoting the
    stored bytes. Ending the protection at ``conn.execute`` left that outside the
    module's error type *and* outside its no-echo rule (round 1, finding 5).
    """
    cursor = _execute(conn, sql, parameters)
    try:
        row = cursor.fetchone()
    except (sqlite3.Error, UnicodeDecodeError, ValueError):
        raise StoreError(
            "the ledger returned a row that could not be read "
            "(detail withheld: it can echo a stored value)"
        ) from None
    return None if row is None else row


def _fetch_all(
    conn: sqlite3.Connection, sql: str, parameters: Sequence[object]
) -> list[sqlite3.Row]:
    """Run a query and take every row. See :func:`_fetch_one`."""
    cursor = _execute(conn, sql, parameters)
    try:
        return list(cursor.fetchall())
    except (sqlite3.Error, UnicodeDecodeError, ValueError):
        raise StoreError(
            "the ledger returned a row that could not be read "
            "(detail withheld: it can echo a stored value)"
        ) from None


# SQLite stores integers as signed 64-bit. A wider Python int is not a number it can
# hold, and `conn.execute` reports that as a raw OverflowError.
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


def _require_bindable_int(value: int, field: str) -> int:
    """Refuse an integer SQLite cannot store, as this module's error type.

    ``question_id`` and the counters are schema-valid at any Python width -- the
    models bound them below, not above -- so this is reachable from ordinary
    accepted input rather than from hostile state (round 1, finding 5).
    """
    if not _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX:
        # The bound is this module's own literal; the offending value is not named.
        raise StoreError(
            f"{field} is outside the range SQLite can store as an integer "
            "(offending input withheld)"
        )
    return value


def _optional_bindable_int(value: int | None, field: str) -> int | None:
    return None if value is None else _require_bindable_int(value, field)


def _stored_text(value: object, field: str) -> str | None:
    """Gate a value read out of a TEXT column, without coercing it.

    Round 1, finding 6: a BLOB in a TEXT-affinity column comes back as ``bytes``,
    and pydantic would coerce it into a ``str`` -- so a corrupt row would be
    *rewritten* into valid-looking evidence rather than refused. Reading is not the
    place to repair the ledger.
    """
    if value is None or type(value) is str:
        return value if value is None else str(value)
    raise StoreError(f"stored {field} is not text (detail withheld: it can echo a value)")


def _stored_int(value: object, field: str) -> int | None:
    if value is None or type(value) is int:
        return None if value is None else int(value)
    raise StoreError(f"stored {field} is not an integer (detail withheld: it can echo a value)")


def _stored_real(value: object, field: str) -> float | None:
    """Gate a REAL column. ``int`` is accepted because SQLite may return one for a
    whole number written to a REAL column; everything else is refused rather than
    coerced."""
    if value is None:
        return None
    if type(value) is float:
        return value
    if type(value) is int:
        return float(value)
    raise StoreError(f"stored {field} is not a number (detail withheld: it can echo a value)")


def _insert_run(conn: sqlite3.Connection, parameters: dict[str, object]) -> None:
    columns = ", ".join(_RUN_COLUMNS)
    placeholders = ", ".join("?" for _ in _RUN_COLUMNS)
    _execute(
        conn,
        f"INSERT INTO research_runs ({columns}) VALUES ({placeholders})",
        [parameters[column] for column in _RUN_COLUMNS],
    )


def _insert_documents(
    conn: sqlite3.Connection, documents: Sequence[ResearchDocument]
) -> tuple[str, ...]:
    columns = ", ".join(_DOCUMENT_COLUMNS)
    placeholders = ", ".join("?" for _ in _DOCUMENT_COLUMNS)
    sql = f"INSERT INTO research_documents ({columns}) VALUES ({placeholders})"
    ids: list[str] = []
    for document in documents:
        # Minted here, as research/model.py says it would be: adapters build documents
        # before the transaction that gives them identity. uuid4 rather than a
        # content-derived id -- a document's *content* identity is already its dedup
        # key, and reusing that as the primary key would make the row un-addressable
        # the moment two runs surfaced one article, which is a case the schema exists
        # to keep as two rows.
        document_id = uuid.uuid4().hex
        parameters = _document_parameters(document, document_id=document_id)
        _execute(conn, sql, [parameters[column] for column in _DOCUMENT_COLUMNS])
        ids.append(document_id)
    return tuple(ids)


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """``lifecycle.transaction`` with its errors translated to this module's type.

    This has to be a context manager of its own rather than a function returning
    ``transaction(conn)``. ``transaction`` is a generator-based context manager, so
    calling it runs none of its body: its refusal of a connection not in
    explicit-transaction mode, and every failure of ``BEGIN``/``COMMIT``/``RELEASE``,
    raise on ``__enter__``/``__exit__``. A ``try`` around the *call* would catch none
    of them and a ``LifecycleError`` would escape a module that contracts to raise
    only ``StoreError``.

    ``LifecycleError`` messages are that module's own literals -- they name the
    connection contract, never a stored value -- so the text is safe to carry.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise StoreError("conn must be a sqlite3.Connection opened with ledger.connect()")
    try:
        with transaction(conn):
            yield
    except LifecycleError as exc:
        raise StoreError(str(exc)) from None


def with_retrieval_counts(
    run: ResearchRun, *, documents_dropped: int | None, duplicates_collapsed: int | None
) -> ResearchRun:
    """Return ``run`` carrying the adapter's discarded-evidence counts.

    Both adapters report ``documents_dropped`` and ``duplicates_collapsed`` on their
    own result object (``AskNewsRetrieval``/``ExaRetrieval``) rather than on the run,
    because until 004 there was no column for either. This is the seam that moves
    them onto the run, which is where the writer and the packet hash both read them.

    It exists so callers do not reach for ``model_copy(update=...)``, which **skips
    validation** -- a negative count would sail past the model and be caught only by
    004's CHECK, at write time, after the calls were paid for. Re-validating here
    means a bad count is refused as this module's own error, before any I/O.
    """
    validated = _require_run(run)
    payload = validated.model_dump(mode="json", warnings=False)
    payload["documents_dropped"] = _require_count(documents_dropped, "documents_dropped")
    payload["duplicates_collapsed"] = _require_count(duplicates_collapsed, "duplicates_collapsed")
    try:
        return validate_run(payload)
    except ResearchSchemaError as exc:
        raise StoreError(f"run does not validate with those counts: {exc}") from None


def open_run(conn: sqlite3.Connection, run: ResearchRun) -> None:
    """Insert a run row before its billable calls, with the completion columns empty.

    Refuses a run that already carries ``completed_at_utc``: a run being opened has
    not completed, and accepting one here would let the two-phase shape be used to
    write a finished run without its documents. Use :func:`persist_retrieval` for
    that case.
    """
    validated = _require_run(run)
    if validated.completed_at_utc is not None:
        raise StoreError(
            "open_run takes a run that has not completed; use persist_retrieval "
            "to write a completed run and its documents in one transaction"
        )
    parameters = _run_parameters(
        validated,
        completed=False,
        raw_response_path=None,
        created_at_utc=_utc_text(_utcnow()),
    )
    with _atomic(conn):
        _insert_run(conn, parameters)


def complete_run(
    conn: sqlite3.Connection,
    run: ResearchRun,
    documents: Iterable[ResearchDocument],
    *,
    raw_response_path: str | None = None,
) -> tuple[str, ...]:
    """Fill in what a previously opened run learned, and insert its documents.

    One transaction, so a caller can never observe a completed run without its
    evidence, or evidence attached to a run that still reads as in-flight.

    Returns the minted ``document_id``s in insert order. ``documents`` is
    deduplicated first; the count collapsed here is *not* added to the run's
    ``duplicates_collapsed``, which is the adapter's own count of what it collapsed
    during retrieval. Conflating them would double-count, and the run is the only
    place that knows which is which -- see :func:`with_retrieval_counts`.
    """
    validated = _require_run(run)
    if validated.completed_at_utc is None:
        raise StoreError("complete_run requires a run carrying completed_at_utc")
    prepared = _prepare_documents(validated, documents)
    parameters = _run_parameters(
        validated,
        completed=True,
        raw_response_path=raw_response_path,
        created_at_utc="",  # unused on the UPDATE path; the stored value is fixed.
    )
    assignments = ", ".join(f"{column} = ?" for column in _COMPLETION_COLUMNS)
    # The guard is in the WHERE clause, not in a preceding SELECT. `completed_at_utc
    # IS NULL` makes completion a **once-only transition**, and matching the identity
    # columns makes it apply to *the run that was opened* rather than to whatever row
    # happens to share the id. Both were missing (round 1, finding 4): completing
    # twice rewrote a stored run's queries and cost and moved its already-computed
    # packet hash, and completing with a model carrying a different question and
    # provider silently combined the opened identity with the other model's payload.
    #
    # Enforced in SQL rather than by a read-then-write, because a read-then-write is
    # a race even inside BEGIN IMMEDIATE's serialization -- the condition and the
    # write have to be the same statement for `rowcount` to mean what it says.
    guard = "retrieval_run_id = ? AND completed_at_utc IS NULL AND question_id = ? "
    guard += "AND provider = ? AND started_at_utc = ?"
    with _atomic(conn):
        cursor = _execute(
            conn,
            f"UPDATE research_runs SET {assignments} WHERE {guard}",
            [parameters[column] for column in _COMPLETION_COLUMNS]
            + [
                validated.retrieval_run_id,
                _require_bindable_int(validated.question_id, "question_id"),
                validated.provider,
                _utc_text(validated.started_at_utc),
            ],
        )
        if cursor.rowcount != 1:
            # One message for all three causes, and deliberately so: distinguishing
            # them means reporting which stored column disagreed, and a stored value
            # is exactly what this module does not echo. The causes are named as
            # possibilities, which is actionable without printing anything.
            raise StoreError(
                "no open run matches this one: it was never opened, it has already "
                "been completed, or its identity (question_id, provider, "
                "started_at_utc) differs from the row that was opened"
            )
        return _insert_documents(conn, prepared)


def persist_retrieval(
    conn: sqlite3.Connection,
    run: ResearchRun,
    documents: Iterable[ResearchDocument],
    *,
    raw_response_path: str | None = None,
) -> tuple[str, ...]:
    """Write a completed run and its documents in one transaction.

    The one-shot composition for a caller that already holds a finished
    ``AskNewsRetrieval``/``ExaRetrieval``. It does not weaken the two-phase
    guarantee -- it simply does not offer it: a caller that wants the spend recorded
    before the calls uses :func:`open_run` and :func:`complete_run`.
    """
    validated = _require_run(run)
    if validated.completed_at_utc is None:
        raise StoreError("persist_retrieval requires a run carrying completed_at_utc")
    prepared = _prepare_documents(validated, documents)
    parameters = _run_parameters(
        validated,
        completed=True,
        raw_response_path=raw_response_path,
        created_at_utc=_utc_text(_utcnow()),
    )
    with _atomic(conn):
        _insert_run(conn, parameters)
        return _insert_documents(conn, prepared)


def _prepare_documents(
    run: ResearchRun, documents: Iterable[ResearchDocument]
) -> tuple[ResearchDocument, ...]:
    """Validate ownership and collapse ledger-duplicate documents before any write."""
    try:
        candidates = tuple(documents)
    except TypeError:
        raise StoreError("documents must be iterable") from None
    for document in candidates:
        if not isinstance(document, ResearchDocument):
            raise StoreError("every document must be a ResearchDocument")
        if document.retrieval_run_id != run.retrieval_run_id:
            # No ids in the message. Caught here rather than left to the foreign key,
            # because the FK would accept a document belonging to a *different*
            # existing run -- silently filing evidence under the wrong retrieval.
            raise StoreError("every document must carry this run's retrieval_run_id")
    return deduplicate(candidates).documents


def load_run(conn: sqlite3.Connection, retrieval_run_id: str) -> ResearchRun:
    """Read one stored run back as a validated model.

    Re-validated rather than trusted: values read out of the ledger are untrusted
    under CLAUDE.md's threat boundary, and validating on the way out is also what
    makes the packet hash computable from the database alone.
    """
    if type(retrieval_run_id) is not str or not retrieval_run_id:
        raise StoreError("retrieval_run_id must be a non-empty string")
    # Before binding: a lone surrogate cannot be encoded as a SQL parameter, and the
    # UnicodeEncodeError that raises quotes the character (round 1, finding 5).
    _require_storable_text(retrieval_run_id, "retrieval_run_id")
    columns = ", ".join(_RUN_COLUMNS)
    row = _fetch_one(
        conn,
        f"SELECT {columns} FROM research_runs WHERE retrieval_run_id = ?",
        (retrieval_run_id,),
    )
    if row is None:
        raise StoreError("no research run with that retrieval_run_id")
    return _run_from_row(dict(zip(_RUN_COLUMNS, tuple(row), strict=True)))


def load_documents(conn: sqlite3.Connection, retrieval_run_id: str) -> tuple[ResearchDocument, ...]:
    """Read one run's documents back as validated models, in a stable order."""
    if type(retrieval_run_id) is not str or not retrieval_run_id:
        raise StoreError("retrieval_run_id must be a non-empty string")
    _require_storable_text(retrieval_run_id, "retrieval_run_id")
    columns = ", ".join(_DOCUMENT_COLUMNS)
    rows = _fetch_all(
        conn,
        # Ordered by the ledger's own dedup key rather than by document_id: the key
        # is stable across a re-persist and the minted uuid is not, so a reader gets
        # the same sequence whichever ledger the evidence was written into.
        f"SELECT {columns} FROM research_documents WHERE retrieval_run_id = ? "
        "ORDER BY canonical_url, content_sha256",
        (retrieval_run_id,),
    )
    return tuple(
        _document_from_row(dict(zip(_DOCUMENT_COLUMNS, tuple(row), strict=True))) for row in rows
    )


def _run_from_row(row: dict[str, Any]) -> ResearchRun:
    """Rebuild a run from stored columns, type-checking each one first.

    Every column is gated by the ``_stored_*`` helpers before it reaches pydantic.
    Handing raw values to ``validate_run`` let coercion repair malformed state --
    a BLOB in a TEXT column arrived as ``bytes`` and came back out a ``str`` --
    which turns "the ledger is corrupt" into "here is some evidence" (round 1,
    finding 6). Values read out of the ledger are untrusted, and validating on the
    way out is also what makes the packet hash computable from the database alone.
    """
    queries = _load_json(row["queries_json"], "queries_json", expect=list)
    payload = {
        "retrieval_run_id": _stored_text(row["retrieval_run_id"], "retrieval_run_id"),
        "question_id": _stored_int(row["question_id"], "question_id"),
        "provider": _stored_text(row["provider"], "provider"),
        "provider_config": _load_json(
            row["provider_config_json"], "provider_config_json", expect=dict
        ),
        # Only SQL NULL means "no queries recorded"; see _load_json.
        "queries": [] if queries is None else queries,
        "started_at_utc": _parse_utc(row["started_at_utc"], "started_at_utc"),
        "completed_at_utc": _parse_utc(row["completed_at_utc"], "completed_at_utc"),
        "freshness_cutoff_utc": _parse_utc(row["freshness_cutoff_utc"], "freshness_cutoff_utc"),
        "raw_response_path": _stored_text(row["raw_response_path"], "raw_response_path"),
        "error_summary": _stored_text(row["error_summary"], "error_summary"),
        "cost_usd": _stored_real(row["cost_usd"], "cost_usd"),
        "agent_model": _stored_text(row["agent_model"], "agent_model"),
        "posts_dropped_no_url": _stored_int(row["posts_dropped_no_url"], "posts_dropped_no_url"),
        "documents_dropped": _stored_int(row["documents_dropped"], "documents_dropped"),
        "duplicates_collapsed": _stored_int(row["duplicates_collapsed"], "duplicates_collapsed"),
    }
    try:
        return validate_run(payload)
    except ResearchSchemaError as exc:
        # Re-raised as this module's own type so a caller handles one error class.
        # The text is safe to carry: ResearchSchemaError is already sanitized -- it
        # withholds inputs and every validator in research/model.py contracts to use
        # a constant message.
        raise StoreError(f"stored research run does not validate: {exc}") from None


def _document_from_row(row: dict[str, Any]) -> ResearchDocument:
    """Rebuild a document from stored columns, type-checking each one. See
    :func:`_run_from_row`."""
    payload = {
        "document_id": _stored_text(row["document_id"], "document_id"),
        "retrieval_run_id": _stored_text(row["retrieval_run_id"], "retrieval_run_id"),
        "original_url": _stored_text(row["original_url"], "original_url"),
        "canonical_url": _stored_text(row["canonical_url"], "canonical_url"),
        "title": _stored_text(row["title"], "title"),
        "publisher": _stored_text(row["publisher"], "publisher"),
        "author": _stored_text(row["author"], "author"),
        "published_at_utc": _parse_utc(row["published_at_utc"], "published_at_utc"),
        "updated_at_utc": _parse_utc(row["updated_at_utc"], "updated_at_utc"),
        "retrieved_at_utc": _parse_utc(row["retrieved_at_utc"], "retrieved_at_utc"),
        "source_type": _stored_text(row["source_type"], "source_type"),
        "provenance": _stored_text(row["provenance"], "provenance"),
        "content_sha256": _stored_text(row["content_sha256"], "content_sha256"),
        "snippet": _stored_text(row["snippet"], "snippet"),
        "summary": _stored_text(row["summary"], "summary"),
        "raw_artifact_path": _stored_text(row["raw_artifact_path"], "raw_artifact_path"),
        "reliability_tag": _stored_text(row["reliability_tag"], "reliability_tag"),
    }
    try:
        return validate_document(payload)
    except ResearchSchemaError as exc:
        raise StoreError(f"stored research document does not validate: {exc}") from None


@contextmanager
def _read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """Hold one consistent read view across a multi-statement read.

    Assembling a packet takes three queries -- the run ids, each run, then each
    run's documents. Issued outside a transaction they are three independent reads,
    and an ordinary concurrent ``complete_run`` landing between them produced a
    packet holding an *unfinished* run together with the documents that run only has
    once it is finished: a state that never existed in the ledger, hashing to
    something neither the before nor the after committed state matches (round 1,
    finding 3).

    A deferred ``BEGIN`` is deliberate where the writers use ``BEGIN IMMEDIATE``:
    this takes no write lock, and under WAL the read view is fixed at the first
    statement inside it, which is exactly the snapshot semantics wanted. Nesting is a
    no-op so a caller who already holds a transaction keeps their own view.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise StoreError("conn must be a sqlite3.Connection opened with ledger.connect()")
    if conn.isolation_level is not None:
        raise StoreError(
            "the ledger connection must be in explicit-transaction mode; "
            "open it with whiskeyjack_bot.ledger.connect()"
        )
    if conn.in_transaction:
        yield
        return
    try:
        conn.execute("BEGIN")
    except sqlite3.Error:
        raise StoreError("the ledger could not open a read transaction") from None
    try:
        yield
    finally:
        try:
            conn.execute("COMMIT")
        except sqlite3.Error:
            # A read transaction has nothing to lose by failing to close cleanly, but
            # leaving one open would strand every later statement on the connection.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass


def list_retrieval_run_ids(
    conn: sqlite3.Connection, *, question_id: int, completed_only: bool = True
) -> tuple[str, ...]:
    """The runs stored for one question, oldest first.

    Discovery is separated from packet assembly on purpose. A packet built from
    "every row currently sharing a question" has no stable identity: persisting a
    second run changes what the *first* packet was, and the earlier one becomes
    unaddressable (round 1, finding 2). So this answers "what is there now", and
    :func:`load_packet` takes the answer -- or a recorded subset -- explicitly.
    A caller that stores the ids it used can reproduce that packet forever.

    ``completed_only`` defaults to true because an open run is a spend record, not
    evidence: it has no documents yet and its own columns are still to be written.
    """
    if type(question_id) is not int:
        raise StoreError("question_id must be an int")
    _require_bindable_int(question_id, "question_id")
    clause = " AND completed_at_utc IS NOT NULL" if completed_only else ""
    rows = _fetch_all(
        conn,
        # Ordered by the run's start, then its id, so the sequence is stable and
        # reads as the retrieval timeline. Packet identity does not depend on it --
        # packet_sha256 sorts -- but a caller presenting evidence should not have to.
        f"SELECT retrieval_run_id FROM research_runs WHERE question_id = ?{clause} "
        "ORDER BY started_at_utc, retrieval_run_id",
        (question_id,),
    )
    return tuple(_require_stored_run_id(row[0]) for row in rows)


def _require_stored_run_id(value: object) -> str:
    text = _stored_text(value, "retrieval_run_id")
    if text is None:
        raise StoreError("stored retrieval_run_id is NULL")
    return text


def load_packet(
    conn: sqlite3.Connection, *, question_id: int, retrieval_run_ids: Sequence[str]
) -> ResearchPacket:
    """Assemble a packet from an explicitly named set of runs.

    Zero provider calls by construction: this reads SQLite and nothing else.

    ``retrieval_run_ids`` is required rather than defaulted to "whatever this
    question has now", because the packet hash is an attribution claim and a claim
    whose subject changes when unrelated evidence is added later cannot be checked.
    Use :func:`list_retrieval_run_ids` to discover them; record what you used.

    Every read happens inside one snapshot, so the packet is a state the ledger
    actually held.
    """
    if type(question_id) is not int:
        raise StoreError("question_id must be an int")
    if isinstance(retrieval_run_ids, (str, bytes)) or not isinstance(retrieval_run_ids, Sequence):
        # A bare str satisfies Sequence and would be read one character per run id.
        raise StoreError("retrieval_run_ids must be a sequence of run ids")
    requested = list(retrieval_run_ids)
    if len(set(requested)) != len(requested):
        raise StoreError("retrieval_run_ids must not repeat a run id")
    with _read_snapshot(conn):
        runs = [load_run(conn, run_id) for run_id in requested]
        for run in runs:
            if run.question_id != question_id:
                # No ids in the message; a question id is row content.
                raise StoreError("a named run belongs to a different question")
        documents = [document for run_id in requested for document in load_documents(conn, run_id)]
    return build_packet(question_id, runs, documents)


def replay_research(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    question_id: int,
    retrieval_run_ids: Sequence[str],
) -> ResearchPacket:
    """Return a stored research packet instead of retrieving it again.

    The item's acceptance criterion in one function: **zero provider calls, and the
    same packet hash**. The first is structural -- this module imports no provider
    SDK and no HTTP client, so there is no call to make. The second follows from
    reading the same rows, named explicitly, inside one snapshot.

    Three refusals, all deliberate:

    - ``retrieval.replay_saved_research`` must be enabled. Replay is not a fallback a
      caller drifts into; the committed default is ``false``, and honouring it here
      is what keeps "we replayed" from being something that happened by accident.
    - **An empty run set raises rather than returning an empty packet.** An empty
      packet is indistinguishable from a question researched and found nothing, so
      returning one would let a caller forecast as though research had happened.
    - **An incomplete run raises.** A run row opened before its billable calls is a
      spend record, and completing it later changes what the packet is. Replaying one
      would reproduce a hash that the finished run will not match (round 1, finding 2).
    """
    if type(question_id) is not int:
        raise StoreError("question_id must be an int")
    try:
        enabled = config.retrieval.replay_saved_research
    except AttributeError:
        raise StoreError("config must be an AppConfig") from None
    if not enabled:
        raise StoreError(
            "retrieval.replay_saved_research is disabled; refusing to replay saved research"
        )
    packet = load_packet(conn, question_id=question_id, retrieval_run_ids=retrieval_run_ids)
    if not packet.runs:
        raise StoreError(
            "no runs named for that question; refusing to replay an empty packet "
            "(it cannot be told apart from research that found nothing)"
        )
    if any(run.completed_at_utc is None for run in packet.runs):
        raise StoreError(
            "a named run has not completed; refusing to replay a packet whose "
            "contents will change when the run finishes"
        )
    return packet
