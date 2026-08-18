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


def _load_json(value: object, field: str) -> Any:
    if value is None:
        return None
    if type(value) is not str:
        raise StoreError(f"stored {field} is not text (detail withheld: it can echo a value)")
    try:
        return json.loads(value)
    except ValueError:
        # from None: a JSONDecodeError quotes the surrounding document text.
        raise StoreError(
            f"stored {field} is not valid JSON (detail withheld: it can echo a stored value)"
        ) from None


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
        "question_id": run.question_id,
        "provider": run.provider,
        "provider_config_json": (
            None
            if run.provider_config is None
            else _canonical_json(run.provider_config, "provider_config")
        ),
        "queries_json": _canonical_json(list(run.queries), "queries"),
        "started_at_utc": _utc_text(run.started_at_utc),
        "completed_at_utc": _optional_utc_text(run.completed_at_utc) if completed else None,
        "freshness_cutoff_utc": _optional_utc_text(run.freshness_cutoff_utc),
        "raw_response_path": raw_response_path,
        "error_summary": run.error_summary,
        "cost_usd": run.cost_usd,
        "agent_model": run.agent_model,
        "posts_dropped_no_url": run.posts_dropped_no_url,
        "documents_dropped": _require_count(run.documents_dropped, "documents_dropped"),
        "duplicates_collapsed": _require_count(run.duplicates_collapsed, "duplicates_collapsed"),
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
    """
    try:
        return conn.execute(sql, tuple(parameters))
    except sqlite3.Error as exc:
        # The trigger and constraint text is schema-authored -- it names columns and
        # rules, never row content -- so it is the one part of the underlying error
        # worth keeping, and it is what makes a refusal actionable. from None still,
        # so the exception object itself (which can carry parameter values in a
        # rendered traceback) does not reach a reporter through the cause chain.
        raise StoreError(f"ledger refused a research write: {exc}") from None


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
    with _atomic(conn):
        if not _run_exists(conn, validated.retrieval_run_id):
            raise StoreError("no open run with that retrieval_run_id; call open_run first")
        cursor = _execute(
            conn,
            f"UPDATE research_runs SET {assignments} WHERE retrieval_run_id = ?",
            [parameters[column] for column in _COMPLETION_COLUMNS] + [validated.retrieval_run_id],
        )
        if cursor.rowcount != 1:
            raise StoreError("completing the run did not update exactly one row")
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


def _run_exists(conn: sqlite3.Connection, retrieval_run_id: str) -> bool:
    row = _execute(
        conn,
        "SELECT 1 FROM research_runs WHERE retrieval_run_id = ?",
        (retrieval_run_id,),
    ).fetchone()
    return row is not None


def load_run(conn: sqlite3.Connection, retrieval_run_id: str) -> ResearchRun:
    """Read one stored run back as a validated model.

    Re-validated rather than trusted: values read out of the ledger are untrusted
    under CLAUDE.md's threat boundary, and validating on the way out is also what
    makes the packet hash computable from the database alone.
    """
    if type(retrieval_run_id) is not str or not retrieval_run_id:
        raise StoreError("retrieval_run_id must be a non-empty string")
    columns = ", ".join(_RUN_COLUMNS)
    row = _execute(
        conn,
        f"SELECT {columns} FROM research_runs WHERE retrieval_run_id = ?",
        (retrieval_run_id,),
    ).fetchone()
    if row is None:
        raise StoreError("no research run with that retrieval_run_id")
    return _run_from_row(dict(zip(_RUN_COLUMNS, tuple(row), strict=True)))


def load_documents(conn: sqlite3.Connection, retrieval_run_id: str) -> tuple[ResearchDocument, ...]:
    """Read one run's documents back as validated models, in a stable order."""
    if type(retrieval_run_id) is not str or not retrieval_run_id:
        raise StoreError("retrieval_run_id must be a non-empty string")
    columns = ", ".join(_DOCUMENT_COLUMNS)
    rows = _execute(
        conn,
        # Ordered by the ledger's own dedup key rather than by document_id: the key
        # is stable across a re-persist and the minted uuid is not, so a reader gets
        # the same sequence whichever ledger the evidence was written into.
        f"SELECT {columns} FROM research_documents WHERE retrieval_run_id = ? "
        "ORDER BY canonical_url, content_sha256",
        (retrieval_run_id,),
    ).fetchall()
    return tuple(
        _document_from_row(dict(zip(_DOCUMENT_COLUMNS, tuple(row), strict=True))) for row in rows
    )


def _run_from_row(row: dict[str, Any]) -> ResearchRun:
    payload = {
        "retrieval_run_id": row["retrieval_run_id"],
        "question_id": row["question_id"],
        "provider": row["provider"],
        "provider_config": _load_json(row["provider_config_json"], "provider_config_json"),
        "queries": _load_json(row["queries_json"], "queries_json") or [],
        "started_at_utc": _parse_utc(row["started_at_utc"], "started_at_utc"),
        "completed_at_utc": _parse_utc(row["completed_at_utc"], "completed_at_utc"),
        "freshness_cutoff_utc": _parse_utc(row["freshness_cutoff_utc"], "freshness_cutoff_utc"),
        "raw_response_path": row["raw_response_path"],
        "error_summary": row["error_summary"],
        "cost_usd": row["cost_usd"],
        "agent_model": row["agent_model"],
        "posts_dropped_no_url": row["posts_dropped_no_url"],
        "documents_dropped": row["documents_dropped"],
        "duplicates_collapsed": row["duplicates_collapsed"],
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
    payload = {
        "document_id": row["document_id"],
        "retrieval_run_id": row["retrieval_run_id"],
        "original_url": row["original_url"],
        "canonical_url": row["canonical_url"],
        "title": row["title"],
        "publisher": row["publisher"],
        "author": row["author"],
        "published_at_utc": _parse_utc(row["published_at_utc"], "published_at_utc"),
        "updated_at_utc": _parse_utc(row["updated_at_utc"], "updated_at_utc"),
        "retrieved_at_utc": _parse_utc(row["retrieved_at_utc"], "retrieved_at_utc"),
        "source_type": row["source_type"],
        "provenance": row["provenance"],
        "content_sha256": row["content_sha256"],
        "snippet": row["snippet"],
        "summary": row["summary"],
        "raw_artifact_path": row["raw_artifact_path"],
        "reliability_tag": row["reliability_tag"],
    }
    try:
        return validate_document(payload)
    except ResearchSchemaError as exc:
        raise StoreError(f"stored research document does not validate: {exc}") from None


def load_packet(conn: sqlite3.Connection, *, question_id: int) -> ResearchPacket:
    """Assemble one question's stored evidence into a :class:`ResearchPacket`.

    Zero provider calls by construction: this reads SQLite and nothing else.
    """
    if type(question_id) is not int:
        raise StoreError("question_id must be an int")
    rows = _execute(
        conn,
        # Ordered by the run's start, then its id, so the sequence is stable and
        # reads as the retrieval timeline. Packet identity does not depend on it --
        # packet_sha256 sorts -- but a caller presenting evidence should not have to.
        "SELECT retrieval_run_id FROM research_runs WHERE question_id = ? "
        "ORDER BY started_at_utc, retrieval_run_id",
        (question_id,),
    ).fetchall()
    run_ids = [str(row[0]) for row in rows]
    runs = [load_run(conn, run_id) for run_id in run_ids]
    documents = [document for run_id in run_ids for document in load_documents(conn, run_id)]
    return build_packet(question_id, runs, documents)


def replay_research(
    conn: sqlite3.Connection, config: AppConfig, *, question_id: int
) -> ResearchPacket:
    """Return a question's stored research packet instead of retrieving it again.

    The item's acceptance criterion in one function: **zero provider calls, and the
    same packet hash**. The first is structural -- this module imports no provider
    SDK and no HTTP client, so there is no call to make. The second follows from
    reading the same rows the packet was hashed from.

    Two refusals, both deliberate:

    - ``retrieval.replay_saved_research`` must be enabled. Replay is not a fallback a
      caller drifts into; the committed default is ``false``, and honouring it here
      is what keeps "we replayed" from being something that happened by accident.
    - **A question with no stored run raises rather than returning an empty packet.**
      An empty packet is indistinguishable from a question researched and found
      nothing, so returning one would let a caller forecast as though research had
      happened. A forecast built on silently-absent evidence is the precise failure
      this ledger exists to make impossible.
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
    packet = load_packet(conn, question_id=question_id)
    if not packet.runs:
        raise StoreError(
            "no stored research run for that question; refusing to replay an empty "
            "packet (it cannot be told apart from research that found nothing)"
        )
    return packet
