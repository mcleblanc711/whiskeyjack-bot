"""Derived JSONL and Parquet exports of the attribution ledger (M1-604).

Decision D29: JSONL and Parquet are **derived** artifacts for audit, interchange and
polygraph analysis, never competing sources of truth. The ledger (D16) stays the only
place a fact is recorded; this module reads it and writes it out again.

This is the first thing in the project that makes the ledger readable by anything other
than this program, and ``D-1002`` (document schemas and exports) and ``M5-804`` (the
attribution report dataset) both sit behind it. So the shape here is a **published
contract**, and three consequences follow that a dump would not have:

- **The table spec is written down, not discovered.** :data:`EXPORTED_TABLES` names every
  table, every column in DDL order and every column's declared type, transcribed from the
  migrations. Deriving it from ``PRAGMA table_info`` would be less code and would silently
  reshape the published contract the first time a migration lands.
  ``tests/unit/test_export.py`` compares the two and fails loudly instead, which is the
  half that makes a hand-written spec safe.
- **Row order is explicit.** Every table is read ``ORDER BY`` its primary identifier --
  never SQLite's natural order, which is an implementation detail that a vacuum or a
  differently-planned query can change.
- **Nothing is coerced.** A value whose storage class disagrees with its column's declared
  type, a non-finite REAL, a BLOB, or text that is not valid UTF-8 is refused with an
  :class:`ExportError` rather than repaired into something that would no longer round-trip.

**All fourteen tables are exported, and none is excluded.** ``schema_migrations`` is in
deliberately: it is what lets a consumer tell which schema produced the files it is
holding. The joined per-forecast view that ``lifecycle.py`` and ``forecast/record.py``
anticipate "at read/export time" is **not** here: assembling one record with its history
is ``show --record-id`` (``M1-612``), and grouping outcomes for analysis is ``M5-804``. A
join baked into the export would impose one analytical reading a consumer cannot undo.
See ``docs/M1-NOTES.md``.

Secret hygiene is M1-605's and it is applied **at write time** (``lifecycle.py``
redacts ``response_body``/``response_headers``/``error_message`` before the row is
stored). This module deliberately adds no second redaction pass: a second opinion about
what counts as a secret is a second source of truth about it.

Error hygiene follows ``LedgerError``/``ArtifactError``: an :class:`ExportError` names the
table and column at fault -- schema, not content -- and never the value, never the row's
identifier, and never the bytes. Filesystem paths are the settled M1-401 carve-out and are
rendered. ``pyarrow`` is imported inside the writer that needs it, never at module scope.

Purely local: no network access on any path through here.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Final, Literal, get_args

from whiskeyjack_bot.artifacts import write_new_file
from whiskeyjack_bot.ledger import connect_readonly

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

EXPORT_SCHEMA_VERSION: Final = 1

ExportFormat = Literal["jsonl", "parquet"]

MANIFEST_FILENAME: Final = "manifest.json"

_WHAT: Final = "ledger export"

# The three declared types the ledger's DDL actually uses. Kept as a closed set so a
# migration introducing a fourth fails the spec-vs-schema parity test rather than
# arriving here as an unhandled branch.
ColumnType = Literal["TEXT", "INTEGER", "REAL"]

# SQLite storage classes, as `typeof()` spells them.
_NULL: Final = "null"
_INTEGER: Final = "integer"
_REAL: Final = "real"
_TEXT: Final = "text"

# Which storage classes a column of each declared type may hold. NULL is always allowed --
# nullability is the schema's business, enforced there by NOT NULL and by triggers, not
# something an export re-litigates. Everything absent from this mapping (notably 'blob') is
# refused: SQLite's dynamic typing permits it, every writer in this tree binds correctly,
# and quietly exporting it would break the round-trip the acceptance criterion is about.
_ALLOWED_STORAGE: Final[dict[str, frozenset[str]]] = {
    "TEXT": frozenset({_NULL, _TEXT}),
    "INTEGER": frozenset({_NULL, _INTEGER}),
    "REAL": frozenset({_NULL, _REAL}),
}


class ExportError(Exception):
    """The ledger cannot be exported.

    Same hygiene rule as ``LedgerError``: the message never echoes a stored value, a row
    identifier or raw bytes -- only the table and column at fault, which are schema rather
    than content -- and sanitizing raises use ``from None`` so an underlying exception
    cannot reprint a value through its text or a rendered traceback. Filesystem paths are
    the settled M1-401 carve-out and are rendered.
    """


@dataclass(frozen=True)
class Column:
    """One exported column: its name and the type the DDL declares for it."""

    name: str
    declared_type: ColumnType


@dataclass(frozen=True)
class TableSpec:
    """One exported table: its name, its columns in DDL order, and its row identifier.

    ``identifier`` is the single-column primary key, and it does double duty: it is the
    ``ORDER BY`` that makes the export byte-deterministic, and it is the key the acceptance
    criterion's set-equality check compares. All fourteen tables have one, so no table
    needs a composite or a synthetic ordering.
    """

    name: str
    identifier: str
    columns: tuple[Column, ...]

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)


@dataclass(frozen=True)
class TableExport:
    """What one table contributed to an export."""

    name: str
    filename: str
    row_count: int
    sha256: str


@dataclass(frozen=True)
class ExportResult:
    """What a completed export wrote."""

    destination: Path
    export_format: ExportFormat
    ledger_schema_version: int
    tables: tuple[TableExport, ...]

    @property
    def row_count(self) -> int:
        return sum(table.row_count for table in self.tables)


# Transcribed column by column from src/whiskeyjack_bot/migrations/, in DDL order --
# including the columns later migrations appended, which is why several tables do not read
# in the order a fresh CREATE TABLE would produce (M2-710's lesson: follow the schema
# column by column, not by category). Ordered by table name so the manifest and the
# directory listing agree.
EXPORTED_TABLES: Final[tuple[TableSpec, ...]] = (
    TableSpec(
        name="approval_events",
        identifier="event_id",
        columns=(
            Column("event_id", "INTEGER"),
            Column("forecast_record_id", "TEXT"),
            Column("decision", "TEXT"),
            Column("actor", "TEXT"),
            Column("forecast_sha256", "TEXT"),
            Column("note", "TEXT"),
            Column("created_at_utc", "TEXT"),
            # 011_approval_payload_binding.sql
            Column("payload_sha256", "TEXT"),
        ),
    ),
    TableSpec(
        name="forecast_records",
        identifier="record_id",
        columns=(
            Column("record_id", "TEXT"),
            Column("question_id", "INTEGER"),
            Column("post_id", "INTEGER"),
            Column("tournament_id", "TEXT"),
            Column("forecast_version", "INTEGER"),
            Column("parent_record_id", "TEXT"),
            Column("question_type", "TEXT"),
            Column("question_domain", "TEXT"),
            Column("status", "TEXT"),
            Column("model_provider", "TEXT"),
            Column("model_name", "TEXT"),
            Column("prompt_version", "TEXT"),
            Column("prompt_sha256", "TEXT"),
            Column("retrieval_run_id", "TEXT"),
            Column("generated_at_utc", "TEXT"),
            Column("final_prediction_json", "TEXT"),
            Column("record_json", "TEXT"),
            Column("created_at_utc", "TEXT"),
            # 003_lifecycle_events.sql
            Column("forecast_sha256", "TEXT"),
            # 004_pipeline_failure_events.sql
            Column("attempt_id", "TEXT"),
            # 008_forecast_raw_output.sql
            Column("raw_output_path", "TEXT"),
            Column("cost_usd", "REAL"),
            Column("model_invocations", "INTEGER"),
        ),
    ),
    TableSpec(
        name="lifecycle_events",
        identifier="event_id",
        columns=(
            Column("event_id", "INTEGER"),
            Column("forecast_record_id", "TEXT"),
            Column("event_seq", "INTEGER"),
            Column("event_type", "TEXT"),
            Column("from_status", "TEXT"),
            Column("to_status", "TEXT"),
            Column("detail_code", "TEXT"),
            Column("approval_event_id", "INTEGER"),
            Column("submission_attempt_id", "TEXT"),
            Column("submission_verification_id", "INTEGER"),
            Column("resolution_event_id", "INTEGER"),
            Column("score_event_id", "INTEGER"),
            Column("occurred_at_utc", "TEXT"),
            Column("created_at_utc", "TEXT"),
        ),
    ),
    TableSpec(
        name="pipeline_failure_events",
        identifier="event_id",
        columns=(
            Column("event_id", "INTEGER"),
            Column("attempt_id", "TEXT"),
            Column("event_seq", "INTEGER"),
            Column("question_id", "INTEGER"),
            Column("tournament_id", "TEXT"),
            Column("event_type", "TEXT"),
            Column("detail_code", "TEXT"),
            Column("retrieval_run_id", "TEXT"),
            Column("occurred_at_utc", "TEXT"),
            Column("created_at_utc", "TEXT"),
        ),
    ),
    TableSpec(
        name="research_documents",
        identifier="document_id",
        columns=(
            Column("document_id", "TEXT"),
            Column("retrieval_run_id", "TEXT"),
            Column("canonical_url", "TEXT"),
            Column("title", "TEXT"),
            Column("publisher", "TEXT"),
            Column("author", "TEXT"),
            Column("published_at_utc", "TEXT"),
            Column("updated_at_utc", "TEXT"),
            Column("retrieved_at_utc", "TEXT"),
            Column("source_type", "TEXT"),
            Column("content_sha256", "TEXT"),
            Column("snippet", "TEXT"),
            Column("summary", "TEXT"),
            Column("raw_artifact_path", "TEXT"),
            Column("reliability_tag", "TEXT"),
            # 002_research_document_fields.sql
            Column("original_url", "TEXT"),
            Column("provenance", "TEXT"),
        ),
    ),
    TableSpec(
        name="research_runs",
        identifier="retrieval_run_id",
        columns=(
            Column("retrieval_run_id", "TEXT"),
            Column("provider", "TEXT"),
            Column("provider_config_json", "TEXT"),
            Column("queries_json", "TEXT"),
            Column("started_at_utc", "TEXT"),
            Column("completed_at_utc", "TEXT"),
            Column("freshness_cutoff_utc", "TEXT"),
            Column("raw_response_path", "TEXT"),
            Column("error_summary", "TEXT"),
            Column("cost_usd", "REAL"),
            Column("created_at_utc", "TEXT"),
            # 002_research_document_fields.sql
            Column("agent_model", "TEXT"),
            Column("posts_dropped_no_url", "INTEGER"),
            Column("question_id", "INTEGER"),
            # 005_research_run_counters.sql
            Column("documents_dropped", "INTEGER"),
            Column("duplicates_collapsed", "INTEGER"),
        ),
    ),
    TableSpec(
        name="resolution_events",
        identifier="event_id",
        columns=(
            Column("event_id", "INTEGER"),
            Column("question_id", "INTEGER"),
            Column("forecast_record_id", "TEXT"),
            Column("resolution_snapshot_json", "TEXT"),
            Column("outcome", "TEXT"),
            Column("annulled", "INTEGER"),
            Column("ambiguous", "INTEGER"),
            Column("source_response", "TEXT"),
            Column("ingested_at_utc", "TEXT"),
        ),
    ),
    TableSpec(
        name="schema_migrations",
        identifier="version",
        columns=(
            Column("version", "INTEGER"),
            Column("applied_at_utc", "TEXT"),
            Column("checksum", "TEXT"),
        ),
    ),
    TableSpec(
        name="score_events",
        identifier="event_id",
        columns=(
            Column("event_id", "INTEGER"),
            Column("forecast_record_id", "TEXT"),
            Column("metric", "TEXT"),
            Column("value", "REAL"),
            Column("implementation_version", "TEXT"),
            Column("comparison_baseline", "TEXT"),
            Column("computed_at_utc", "TEXT"),
        ),
    ),
    TableSpec(
        name="submission_attempts",
        identifier="attempt_id",
        columns=(
            Column("attempt_id", "TEXT"),
            Column("forecast_record_id", "TEXT"),
            Column("idempotency_key", "TEXT"),
            Column("requested_at_utc", "TEXT"),
            Column("completed_at_utc", "TEXT"),
            Column("request_payload_sha256", "TEXT"),
            Column("http_status", "INTEGER"),
            Column("response_body", "TEXT"),
            Column("response_headers", "TEXT"),
            Column("success", "INTEGER"),
            Column("error_type", "TEXT"),
            Column("error_message", "TEXT"),
            Column("verified_by_refetch", "INTEGER"),
            Column("refetched_forecast_snapshot", "TEXT"),
            Column("created_at_utc", "TEXT"),
            # 009_submission_refetch_outcome.sql
            Column("refetch_outcome", "TEXT"),
        ),
    ),
    TableSpec(
        name="submission_key_releases",
        identifier="release_id",
        columns=(
            Column("release_id", "TEXT"),
            Column("reservation_id", "TEXT"),
            Column("reason", "TEXT"),
            Column("released_by", "TEXT"),
            Column("note", "TEXT"),
            Column("released_at_utc", "TEXT"),
            Column("created_at_utc", "TEXT"),
        ),
    ),
    TableSpec(
        name="submission_key_reservations",
        identifier="reservation_id",
        columns=(
            Column("reservation_id", "TEXT"),
            Column("idempotency_key", "TEXT"),
            Column("forecast_record_id", "TEXT"),
            Column("reservation_seq", "INTEGER"),
            Column("reserved_at_utc", "TEXT"),
            Column("created_at_utc", "TEXT"),
        ),
    ),
    TableSpec(
        name="submission_verifications",
        identifier="verification_id",
        columns=(
            Column("verification_id", "INTEGER"),
            Column("submission_attempt_id", "TEXT"),
            Column("outcome", "TEXT"),
            Column("observed_at_utc", "TEXT"),
            Column("refetched_forecast_snapshot", "TEXT"),
            Column("created_at_utc", "TEXT"),
        ),
    ),
    # 012_tournament_safety.sql. Arrived by a master merge after this spec was written, and
    # the spec-vs-schema parity test is what caught it -- the reason the spec is written
    # down rather than derived. `seq` is the primary key and the order the runner appends
    # in; `event_id` is UNIQUE but a TEXT token, so it would order by value, not by time.
    TableSpec(
        name="tournament_events",
        identifier="seq",
        columns=(
            Column("seq", "INTEGER"),
            Column("event_id", "TEXT"),
            Column("kind", "TEXT"),
            Column("scope", "TEXT"),
            # Exported as the stored JSON string, like record_json: parsing it would put
            # this module's JSON reading between the ledger and the consumer.
            Column("data", "TEXT"),
            Column("created_at_utc", "TEXT"),
        ),
    ),
)


def canonical_json(payload: object, what: str) -> str:
    """Render ``payload`` in the project's settled canonical JSON form.

    ``model_dump(mode="json")`` -> ``json.dumps(ensure_ascii=True, sort_keys=True)``, the
    form M1-305 settled on after a tiebreak keyed on ``model_dump_json()`` proved unstable.
    ``allow_nan=False`` is load-bearing rather than decorative: Python's encoder otherwise
    emits bare ``Infinity``/``NaN``, which no JSON parser is obliged to accept, so an
    export could be written that a consumer cannot read.

    ``sort_keys=True`` orders the keys of every row alphabetically, not in DDL order. That
    is deliberate -- it makes the bytes independent of :data:`EXPORTED_TABLES`'s ordering,
    so reordering the spec cannot change an export. DDL order is preserved where it is
    actually a contract, in the manifest's per-table ``columns`` list.
    """
    try:
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        # from None: json.dumps names the offending value in its own message.
        raise ExportError(
            f"cannot render {what} as JSON (detail withheld: it can echo a stored value)"
        ) from None


def _refuse(table: str, column: str, problem: str) -> ExportError:
    """Build the one error shape this module raises for an off-contract stored value.

    Table and column are schema and are named; the value, its bytes and the row's
    identifier are content and are never named. M1-202 settled that an identifier is not
    an exception to the no-echo rule just because it is low-risk.
    """
    return ExportError(
        f"ledger column {table}.{column} holds a value this export cannot represent "
        f"({problem}); the value is withheld"
    )


def _decode_value(table: str, column: Column, storage_class: object, value: object) -> object:
    """Convert one stored value to its JSON-ready form, or refuse it.

    ``storage_class`` is SQLite's own ``typeof()``, taken alongside the value in the same
    row rather than inferred from the Python object, because ``sqlite3`` has already
    applied its own conversions by the time a value reaches here.
    """
    if not isinstance(storage_class, bytes):
        raise _refuse(table, column.name, "its storage class could not be read")
    kind = storage_class.decode("ascii", "replace")
    if kind not in _ALLOWED_STORAGE[column.declared_type]:
        # `kind` is one of SQLite's five fixed storage-class names, so naming it is
        # naming a type, not a value.
        raise _refuse(
            table,
            column.name,
            f"declared {column.declared_type} but stored as {kind}",
        )
    if kind == _NULL:
        return None
    if kind == _INTEGER:
        if not isinstance(value, int):
            raise _refuse(table, column.name, "an integer did not arrive as one")
        return value
    if kind == _REAL:
        if not isinstance(value, float):
            raise _refuse(table, column.name, "a real did not arrive as one")
        if not math.isfinite(value):
            # Reachable, measured: SQLite stores +/-inf in a REAL column happily (it
            # cannot store NaN, which becomes NULL). JSON has no spelling for either.
            raise _refuse(table, column.name, "a non-finite real has no JSON form")
        return value
    if not isinstance(value, bytes):
        raise _refuse(table, column.name, "text did not arrive as bytes")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        # from None, and this is why the connection runs with `text_factory = bytes`:
        # sqlite3's *own* decode raises OperationalError with the offending bytes
        # interpolated into the message ("Could not decode to UTF-8 column 'x' with
        # text '...'"), so letting it decode would leak content through an error the
        # project never wrote. Decoding here keeps the refusal ours.
        raise _refuse(table, column.name, "text that is not valid UTF-8") from None


def read_table(connection: sqlite3.Connection, spec: TableSpec) -> list[dict[str, object]]:
    """Read one table in identifier order as JSON-ready rows.

    The connection must have ``text_factory = bytes`` set (see :func:`_decode_value`);
    :func:`export_ledger` does that for the connection it owns.
    """
    quoted = ", ".join(f'typeof("{c.name}"), "{c.name}"' for c in spec.columns)
    statement = f'SELECT {quoted} FROM "{spec.name}" ORDER BY "{spec.identifier}"'
    try:
        cursor = connection.execute(statement)
    except sqlite3.Error:
        # from None: the underlying message can quote stored bytes.
        raise ExportError(f"cannot read ledger table {spec.name}") from None
    rows: list[dict[str, object]] = []
    try:
        while True:
            try:
                raw = cursor.fetchone()
            except sqlite3.Error:
                # sqlite3 decodes at fetch, not at execute (M1-306), so a malformed
                # stored value surfaces here rather than above. from None for the same
                # reason as the decode arm in _decode_value.
                raise ExportError(f"cannot read ledger table {spec.name}") from None
            if raw is None:
                break
            values = tuple(raw)
            if len(values) != 2 * len(spec.columns):
                raise ExportError(f"ledger table {spec.name} returned an unexpected row shape")
            rows.append(
                {
                    column.name: _decode_value(
                        spec.name, column, values[2 * index], values[2 * index + 1]
                    )
                    for index, column in enumerate(spec.columns)
                }
            )
    finally:
        cursor.close()
    return rows


def render_jsonl(rows: Sequence[dict[str, object]], table: str) -> bytes:
    """Render rows as newline-delimited canonical JSON.

    One object per line, trailing newline on the last. Byte-identical for identical rows,
    unconditionally and across versions -- the guarantee Parquet cannot make.
    """
    return "".join(f"{canonical_json(row, f'a {table} row')}\n" for row in rows).encode("utf-8")


def render_parquet(rows: Sequence[dict[str, object]], spec: TableSpec) -> bytes:
    """Render rows as a single-row-group Parquet file.

    Column types come from the spec's declared types rather than from the data, so an empty
    table still produces a correctly-typed file and two exports of the same table cannot
    disagree about a column's type because one of them happened to be all-NULL.

    **Determinism is version-scoped, and that is stated rather than papered over.** For a
    fixed pyarrow the bytes are reproducible (measured: identical digests across repeats
    and across time, under every compression setting tried). Across versions they are not
    -- every file embeds the writer's identity, ``parquet-cpp-arrow version <x.y.z>`` --
    and the knobs that would strip it are undocumented writer behaviour that a minor bump
    can silently change. So the manifest records the pyarrow version instead, which makes a
    digest mismatch explainable rather than mysterious. The JSONL beside it is the
    byte-stable form, and the two are asserted semantically equal.

    ``pyarrow`` is imported here, not at module scope: it is ~146MB unpacked, and
    ``cli.py``'s function-local import convention exists to keep exactly this off the
    import graph of anything that does not need it.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    arrow_types = {"TEXT": pa.string(), "INTEGER": pa.int64(), "REAL": pa.float64()}
    schema = pa.schema([pa.field(c.name, arrow_types[c.declared_type]) for c in spec.columns])
    try:
        table = pa.Table.from_pydict(
            {c.name: [row[c.name] for row in rows] for c in spec.columns},
            schema=schema,
        )
        buffer = pa.BufferOutputStream()
        pq.write_table(table, buffer)
        written = buffer.getvalue().to_pybytes()
    except (pa.ArrowException, TypeError, ValueError, OverflowError):
        # from None: Arrow's conversion errors quote the offending value.
        raise ExportError(
            f"cannot render ledger table {spec.name} as Parquet "
            "(detail withheld: it can echo a stored value)"
        ) from None
    return bytes(written)


def _writer_versions(export_format: ExportFormat) -> dict[str, str]:
    """Record who wrote the files, for formats whose bytes depend on it."""
    if export_format != "parquet":
        return {}
    import pyarrow as pa

    return {"pyarrow": str(pa.__version__)}


def export_ledger(
    ledger_path: Path,
    destination: Path,
    *,
    export_format: ExportFormat,
    now: datetime | None = None,
) -> ExportResult:
    """Export every ledger table to ``destination``; never mutate the ledger.

    Takes a **path, not a connection**, and opens it itself through
    :func:`ledger.connect_readonly`. That is the difference between a promise and a
    property: a function handed a connection cannot promise anything about how it was
    opened, and "never mutate SQLite" is the acceptance criterion here.

    Every table is read inside **one deferred transaction**, so the whole export is a
    single consistent snapshot. Without it each statement would get its own snapshot
    (measured), and a run committing between two tables could produce an export whose
    ``forecast_records`` holds a record its ``lifecycle_events`` has never heard of.

    What "never mutate" is measured as: the main database and the ``-wal`` are byte-
    identical before and after. The ``-shm`` is not covered and cannot be -- SQLite writes
    read locks into it, and it is derived lock state regenerated from the WAL, which every
    reader including ``sqlite3``'s own shell touches. See :func:`ledger.connect_readonly`.

    ``destination`` must not already contain these files: every write goes through the
    shared atomic create-or-fail writer, so an export never overwrites an earlier one.
    """
    if export_format not in get_args(ExportFormat):
        # Validated before any I/O, mirroring artifacts.write_new_file's policy check.
        raise ExportError(
            f"export_format must be one of {get_args(ExportFormat)} (offending input withheld)"
        )
    stamped = now if now is not None else datetime.now(timezone.utc)

    connection = connect_readonly(ledger_path)
    try:
        # The default text_factory decodes TEXT at fetch and interpolates the offending
        # bytes into its own OperationalError; bytes keeps every decision -- and every
        # error message -- in this module. See _decode_value.
        connection.text_factory = bytes
        ledger_schema_version = _read_schema_version(connection)
        try:
            connection.execute("BEGIN")
        except sqlite3.Error:
            raise ExportError(f"cannot read ledger database at {ledger_path}") from None
        try:
            tables = {spec.name: read_table(connection, spec) for spec in EXPORTED_TABLES}
        finally:
            # A read-only connection has nothing to commit; ROLLBACK simply drops the
            # snapshot. Suppressed because the export itself has already succeeded or
            # failed by here, and a failure to end a read transaction must not mask it.
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
    finally:
        connection.close()

    # Every table is rendered before the first file is written, so a refusal at render time
    # (a Parquet conversion, a JSON encoding) leaves no files at all rather than k-1 of them
    # in a directory the next attempt is then refused from. What can still interrupt the
    # write loop is ordinary I/O, and for that `manifest.json` is written last: it is the
    # completion marker, and a directory without one is not an export.
    payloads: list[tuple[TableSpec, bytes]] = [
        (
            spec,
            render_jsonl(tables[spec.name], spec.name)
            if export_format == "jsonl"
            else render_parquet(tables[spec.name], spec),
        )
        for spec in EXPORTED_TABLES
    ]
    exported: list[TableExport] = []
    for spec, payload in payloads:
        filename = f"{spec.name}.{export_format}"
        write_new_file(destination / filename, payload, what=_WHAT, error=ExportError)
        exported.append(
            TableExport(
                name=spec.name,
                filename=filename,
                row_count=len(tables[spec.name]),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )

    manifest = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "ledger_schema_version": ledger_schema_version,
        "format": export_format,
        "exported_at_utc": stamped.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "writer": _writer_versions(export_format),
        "tables": [
            {
                "name": table.name,
                # DDL order, which sort_keys drops from the row objects themselves.
                "columns": list(spec.column_names),
                "identifier": spec.identifier,
                "file": table.filename,
                "row_count": table.row_count,
                "sha256": table.sha256,
            }
            for spec, table in zip(EXPORTED_TABLES, exported, strict=True)
        ],
    }
    write_new_file(
        destination / MANIFEST_FILENAME,
        f"{canonical_json(manifest, 'the export manifest')}\n".encode(),
        what=_WHAT,
        error=ExportError,
    )
    return ExportResult(
        destination=destination,
        export_format=export_format,
        ledger_schema_version=ledger_schema_version,
        tables=tuple(exported),
    )


def _read_schema_version(connection: sqlite3.Connection) -> int:
    """Return the ledger's applied schema version.

    ``connect_readonly`` has already refused a database that is ahead of or behind this
    build, so this is a read for the manifest rather than a second check.
    """
    try:
        row = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()
    except sqlite3.Error:
        raise ExportError("cannot read the ledger schema version") from None
    if row is None or row[0] is None:
        raise ExportError("the ledger records no applied schema version")
    try:
        return int(row[0])
    except (TypeError, ValueError):
        # from None: same hygiene as ledger._current_version -- never echo the value.
        raise ExportError(
            "the ledger schema version is not an integer "
            "(detail withheld: it can echo stored values)"
        ) from None
