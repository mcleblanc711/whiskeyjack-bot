"""Derived JSONL/Parquet ledger exports (M1-604).

The acceptance criterion is *"Exports round-trip record IDs/counts and never mutate
SQLite"*, and both halves are stricter than they read:

- **Round-trip** is set equality of primary identifiers, per table, for all thirteen --
  not a count, and not a one-sided subset. A one-sided check passes on an export that
  drops rows, which is the vacuity M1-501 lost a round to. It is also worth nothing on an
  empty table, so `test_the_seed_reaches_every_table` refuses to let that pass unnoticed.
- **Never mutate** is a measurement over file bytes, not an assertion about intent, and it
  has a companion showing the same comparison catches a real mutation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from whiskeyjack_bot.export import (
    EXPORT_SCHEMA_VERSION,
    EXPORTED_TABLES,
    MANIFEST_FILENAME,
    ExportError,
    export_ledger,
)
from whiskeyjack_bot.ledger import LEDGER_SCHEMA_VERSION, LedgerError, connect, connect_readonly
from whiskeyjack_bot.ledger import initialize_ledger

if TYPE_CHECKING:
    from collections.abc import Iterator

TS = "2026-09-04T12:00:00.000000+00:00"
SHA = "a" * 64
PAYLOAD_SHA = "b" * 64
SNAPSHOT = '{"prediction": 0.4}'

# Low-entropy on purpose: CI runs gitleaks over full history, and a realistic-looking
# secret in a committed test is a finding whether or not it is real (see T-901).
FAKE_SECRET = "privateFAKE123456"


# --------------------------------------------------------------------------------------
# A ledger with at least one row in every exported table.
# --------------------------------------------------------------------------------------


def _seed_every_table(conn: sqlite3.Connection) -> None:
    """Write at least one row into each of the thirteen exported tables.

    Raw SQL rather than the production writers, and deliberately: the point here is the
    *schema's* full surface, including tables whose writers are still Not Started
    (`resolution_events` is M4-802's, `score_events` is M5-803's). A seed built only from
    what has a writer today would leave those two empty, and an empty table makes the
    set-equality check vacuous exactly where nobody would look for it.

    `schema_migrations` is not seeded here -- `initialize_ledger` fills it, which is the
    honest way for it to be populated.
    """
    conn.execute(
        "INSERT INTO research_runs (retrieval_run_id, provider, question_id, "
        "started_at_utc, created_at_utc, cost_usd) "
        "VALUES ('run-1', 'asknews', 100, ?, ?, 0.125)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO research_documents (document_id, retrieval_run_id, canonical_url, "
        "retrieved_at_utc, content_sha256, original_url, provenance, source_type) "
        "VALUES ('doc-1', 'run-1', ?, ?, ?, ?, 'direct_api', 'news')",
        ("https://example.org/a", TS, SHA, "https://example.org/a?utm=x"),
    )
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
        "forecast_sha256, attempt_id) "
        "VALUES ('rec-1', 100, 'minibench', 1, 'binary', 'draft', 'anthropic', 'claude', "
        "'v1', ?, 'run-1', ?, '{}', '{}', ?, ?, 'att-rec-1')",
        (SHA, TS, TS, SHA),
    )
    approval_id = conn.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "created_at_utc, payload_sha256) VALUES ('rec-1', 'approved', 'chris', ?, ?, ?)",
        (SHA, TS, PAYLOAD_SHA),
    ).lastrowid
    # The response columns carry what M1-605 already redacted on the way in. Seeding the
    # redacted form is the honest shape: this is what a real ledger holds.
    conn.execute(
        "INSERT INTO submission_attempts (attempt_id, forecast_record_id, idempotency_key, "
        "requested_at_utc, completed_at_utc, request_payload_sha256, http_status, "
        "response_body, response_headers, success, verified_by_refetch, refetch_outcome, "
        "created_at_utc) "
        "VALUES ('att-ok', 'rec-1', 'idem-1', ?, ?, ?, 201, ?, ?, 1, 1, 'confirmed', ?)",
        (TS, TS, PAYLOAD_SHA, '{"ok": true}', "[REDACTED:METACULUS_TOKEN]", TS),
    )
    conn.execute(
        "INSERT INTO submission_verifications (submission_attempt_id, outcome, "
        "observed_at_utc, refetched_forecast_snapshot, created_at_utc) "
        "VALUES ('att-ok', 'confirmed', ?, ?, ?)",
        (TS, SNAPSHOT, TS),
    )
    # Two events, because the state machine will not accept an approval straight off a
    # draft: `from_status` is checked against the record's current status, so the
    # draft -> validated step has to exist before validated -> approved can.
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
        "from_status, to_status, occurred_at_utc, created_at_utc) "
        "VALUES ('rec-1', 1, 'validated', 'draft', 'validated', ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, "
        "from_status, to_status, approval_event_id, occurred_at_utc, created_at_utc) "
        "VALUES ('rec-1', 2, 'approved', 'validated', 'approved', ?, ?, ?)",
        (approval_id, TS, TS),
    )
    conn.execute(
        "INSERT INTO pipeline_failure_events (attempt_id, event_seq, question_id, "
        "tournament_id, event_type, detail_code, retrieval_run_id, occurred_at_utc, "
        "created_at_utc) "
        "VALUES ('att-failed', 1, 100, 'minibench', 'research_failed', "
        "'provider_unavailable', 'run-1', ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO resolution_events (question_id, forecast_record_id, ingested_at_utc) "
        "VALUES (100, 'rec-1', ?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO score_events (forecast_record_id, metric, value, "
        "implementation_version, computed_at_utc) VALUES ('rec-1', 'brier', 0.25, 'v1', ?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO submission_key_reservations (reservation_id, idempotency_key, "
        "forecast_record_id, reservation_seq, reserved_at_utc, created_at_utc) "
        "VALUES ('wjres-1', 'idem-unused-1', 'rec-1', 1, ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO submission_key_releases (release_id, reservation_id, reason, "
        "released_by, note, released_at_utc, created_at_utc) "
        "VALUES ('wjrel-1', 'wjres-1', 'operator_abandoned', 'chris', NULL, ?, ?)",
        (TS, TS),
    )


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        _seed_every_table(conn)
    finally:
        conn.close()
    return db


def _identifiers(conn: sqlite3.Connection, table: str, identifier: str) -> set[object]:
    return {row[0] for row in conn.execute(f'SELECT "{identifier}" FROM "{table}"')}


def _jsonl_rows(destination: Path, table: str) -> list[dict[str, Any]]:
    text = (destination / f"{table}.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _parquet_rows(destination: Path, table: str) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    return list(pq.read_table(destination / f"{table}.parquet").to_pylist())


def _sidecar_fingerprint(db: Path) -> dict[str, str | None]:
    """Hash the database and both WAL sidecars.

    SQLite resolves `-wal` and `-shm` by **pathname**, not through the open descriptor
    (the lesson M2-701 paid for), so hashing only the `.db` would not be testing what it
    claims: a checkpoint moves bytes between these files without necessarily changing the
    one everybody looks at.
    """
    fingerprint: dict[str, str | None] = {}
    for suffix in ("", "-wal", "-shm"):
        sidecar = Path(f"{db}{suffix}")
        fingerprint[suffix or ".db"] = (
            hashlib.sha256(sidecar.read_bytes()).hexdigest() if sidecar.exists() else None
        )
    return fingerprint


# --------------------------------------------------------------------------------------
# The seed itself, because everything below is only as good as its coverage.
# --------------------------------------------------------------------------------------


def test_the_seed_reaches_every_table(ledger_path: Path) -> None:
    """Every exported table holds at least one row.

    This is the anti-vacuity test for the whole file. `set() == set()` is true, so an
    export that dropped a table entirely would satisfy the round-trip check below on any
    table the seed had missed -- and it would pass quietly, because nothing else here
    looks at row counts per table.
    """
    conn = connect_readonly(ledger_path)
    try:
        empty = [
            spec.name
            for spec in EXPORTED_TABLES
            if conn.execute(f'SELECT count(*) FROM "{spec.name}"').fetchone()[0] == 0
        ]
    finally:
        conn.close()
    assert empty == [], f"the seed leaves these tables empty, so their checks are vacuous: {empty}"


def test_every_ledger_table_is_exported(ledger_path: Path) -> None:
    """The spec covers the schema: no table is silently left out of the contract."""
    conn = connect_readonly(ledger_path)
    try:
        in_database = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()
    assert {spec.name for spec in EXPORTED_TABLES} == in_database


def test_the_hand_written_spec_matches_the_schema_column_by_column(ledger_path: Path) -> None:
    """`EXPORTED_TABLES` agrees with the database, name for name and in order.

    This is what makes a hand-transcribed spec safe rather than merely duplicated. The
    spec is deliberately not derived from `PRAGMA table_info` -- a published contract must
    break loudly when a migration reshapes it, not follow it silently -- and this is the
    loud break. A migration adding a column fails here until the spec and the export
    version are considered together.
    """
    conn = connect_readonly(ledger_path)
    try:
        for spec in EXPORTED_TABLES:
            info = conn.execute(f'PRAGMA table_info("{spec.name}")').fetchall()
            assert spec.column_names == tuple(row[1] for row in info), spec.name
            assert tuple(c.declared_type for c in spec.columns) == tuple(row[2] for row in info), (
                spec.name
            )
            primary_key = [row[1] for row in info if row[5]]
            assert primary_key == [spec.identifier], spec.name
    finally:
        conn.close()


# --------------------------------------------------------------------------------------
# Acceptance criterion, half one: round-trip record IDs and counts.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("export_format", ["jsonl", "parquet"])
def test_the_export_round_trips_every_identifier_in_every_table(
    ledger_path: Path, tmp_path: Path, export_format: str
) -> None:
    """Set equality per table, both directions, for all thirteen.

    Equality rather than `<=` or `>=`: a subset check passes on an export that drops rows
    and a superset check passes on one that invents them, and the criterion means neither.
    """
    destination = tmp_path / "out"
    result = export_ledger(ledger_path, destination, export_format=export_format)  # type: ignore[arg-type]

    read_rows = _jsonl_rows if export_format == "jsonl" else _parquet_rows
    conn = connect_readonly(ledger_path)
    try:
        for spec in EXPORTED_TABLES:
            exported = read_rows(destination, spec.name)
            assert {row[spec.identifier] for row in exported} == _identifiers(
                conn, spec.name, spec.identifier
            ), spec.name
            stored_count = conn.execute(f'SELECT count(*) FROM "{spec.name}"').fetchone()[0]
            assert len(exported) == stored_count, spec.name
    finally:
        conn.close()

    assert {table.name for table in result.tables} == {spec.name for spec in EXPORTED_TABLES}
    for spec, table in zip(EXPORTED_TABLES, result.tables, strict=True):
        assert table.row_count == len(read_rows(destination, spec.name)), spec.name


def test_every_column_value_round_trips_not_just_the_identifier(
    ledger_path: Path, tmp_path: Path
) -> None:
    """Whole rows survive, not only the key the criterion names.

    "Round-trips record IDs/counts" read narrowly would be satisfied by an export that
    wrote correct keys beside wrong values. The stricter reading is the one implemented.
    """
    destination = tmp_path / "out"
    export_ledger(ledger_path, destination, export_format="jsonl")
    conn = connect_readonly(ledger_path)
    try:
        for spec in EXPORTED_TABLES:
            exported = {row[spec.identifier]: row for row in _jsonl_rows(destination, spec.name)}
            columns = ", ".join(f'"{name}"' for name in spec.column_names)
            for row in conn.execute(f'SELECT {columns} FROM "{spec.name}"'):
                stored = dict(zip(spec.column_names, tuple(row), strict=True))
                assert exported[stored[spec.identifier]] == stored, spec.name
    finally:
        conn.close()


def test_parquet_and_jsonl_carry_the_same_rows(ledger_path: Path, tmp_path: Path) -> None:
    """The two formats are two spellings of one export, not two exports.

    Parquet's bytes are only reproducible for a fixed pyarrow, so this is what pins its
    *content*: whatever the writer version, it agrees with the byte-stable JSONL.
    """
    as_jsonl = tmp_path / "jsonl"
    as_parquet = tmp_path / "parquet"
    export_ledger(ledger_path, as_jsonl, export_format="jsonl")
    export_ledger(ledger_path, as_parquet, export_format="parquet")
    for spec in EXPORTED_TABLES:
        assert _jsonl_rows(as_jsonl, spec.name) == _parquet_rows(as_parquet, spec.name), spec.name


# --------------------------------------------------------------------------------------
# Acceptance criterion, half two: never mutate SQLite.
# --------------------------------------------------------------------------------------


@pytest.fixture
def ledger_with_live_wal(ledger_path: Path) -> Iterator[sqlite3.Connection]:
    """A ledger whose `-wal` holds committed frames the main database does not.

    The state a real ledger is in while a run is in flight, and the one that separates the
    two candidate read-only openers: `immutable=1` reads straight past these frames.
    Holding the writer open is what stops SQLite checkpointing them away at close.
    """
    writer = connect(ledger_path)
    writer.execute(
        "INSERT INTO score_events (forecast_record_id, metric, value, "
        "implementation_version, computed_at_utc) VALUES ('rec-1', 'log', -0.5, 'v1', ?)",
        (TS,),
    )
    assert Path(f"{ledger_path}-wal").exists(), "the fixture must leave uncheckpointed frames"
    try:
        yield writer
    finally:
        writer.close()


@pytest.mark.parametrize("export_format", ["jsonl", "parquet"])
def test_exporting_does_not_change_the_ledger(
    ledger_path: Path,
    ledger_with_live_wal: sqlite3.Connection,
    tmp_path: Path,
    export_format: str,
) -> None:
    """The database and its WAL are byte-identical across a full export.

    `-shm` is deliberately outside the claim and that is stated rather than hidden: SQLite
    records read locks in it, so *every* reader changes it, `sqlite3`'s own shell included.
    It is derived lock state regenerated from the WAL, holds no ledger content, and
    exempting it is what lets the export read the WAL correctly at all -- the opener that
    leaves it untouched, `immutable=1`, does so by not reading the WAL, which is the
    silent-wrong-answer case `test_immutable_would_have_silently_read_a_stale_ledger`
    pins. The two files that carry content are covered without exception.
    """
    before = _sidecar_fingerprint(ledger_path)
    export_ledger(ledger_path, tmp_path / "out", export_format=export_format)  # type: ignore[arg-type]
    after = _sidecar_fingerprint(ledger_path)

    assert before[".db"] == after[".db"]
    assert before["-wal"] == after["-wal"]
    assert before["-wal"] is not None, "a checkpointed ledger would not exercise the WAL path"


def test_the_no_mutation_check_would_catch_a_real_write(
    ledger_path: Path, ledger_with_live_wal: sqlite3.Connection
) -> None:
    """The mutation control: the same comparison fails when something really writes.

    Without this the test above is unfalsifiable -- it would pass just as happily against
    a fingerprint function that returned a constant, which is the vacuity class that costs
    this project the most rounds.
    """
    before = _sidecar_fingerprint(ledger_path)
    ledger_with_live_wal.execute(
        "INSERT INTO score_events (forecast_record_id, metric, value, "
        "implementation_version, computed_at_utc) VALUES ('rec-1', 'brier2', 0.5, 'v1', ?)",
        (TS,),
    )
    after = _sidecar_fingerprint(ledger_path)
    assert (before[".db"], before["-wal"]) != (after[".db"], after["-wal"])


def test_the_read_only_connection_refuses_every_write(ledger_path: Path) -> None:
    """Structural, not incidental: the connection cannot write even if asked."""
    conn = connect_readonly(ledger_path)
    try:
        for statement in (
            "INSERT INTO score_events (forecast_record_id, metric, value, "
            "implementation_version, computed_at_utc) VALUES ('rec-1', 'x', 1.0, 'v1', 'now')",
            "CREATE TABLE intruder (x TEXT)",
            "PRAGMA user_version = 9",
            "DELETE FROM score_events",
            "UPDATE forecast_records SET status = 'approved'",
        ):
            with pytest.raises(sqlite3.OperationalError, match="readonly database"):
                conn.execute(statement)
    finally:
        conn.close()


def test_immutable_would_have_silently_read_a_stale_ledger(
    ledger_path: Path, ledger_with_live_wal: sqlite3.Connection
) -> None:
    """Why `mode=ro` and not `immutable=1`, kept as an executable record.

    `immutable=1` also leaves every byte alone, so a reviewer could reasonably ask why the
    stronger-looking flag was not used. Because it is not stronger: it asserts no other
    writer exists, and against a live WAL it does not error -- it reads the stale main
    database and returns fewer rows than the ledger holds. An export is a record; one that
    silently omits committed rows is worse than one that fails.
    """
    uri = f"{ledger_path.resolve().as_uri()}?immutable=1"
    immutable = sqlite3.connect(uri, uri=True)
    try:
        through_immutable = immutable.execute("SELECT count(*) FROM score_events").fetchone()[0]
    finally:
        immutable.close()

    readonly = connect_readonly(ledger_path)
    try:
        through_readonly = readonly.execute("SELECT count(*) FROM score_events").fetchone()[0]
    finally:
        readonly.close()

    truth = ledger_with_live_wal.execute("SELECT count(*) FROM score_events").fetchone()[0]
    assert through_readonly == truth
    assert through_immutable < truth, (
        "if immutable=1 ever starts reading the WAL this rationale needs revisiting"
    )


def test_the_export_reads_one_snapshot_even_while_a_writer_commits(
    ledger_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All thirteen tables come from one transaction, not thirteen.

    Without the single deferred transaction each statement takes its own snapshot, so a
    run committing mid-export could land a `forecast_records` row whose `lifecycle_events`
    are missing -- an export that is internally inconsistent while every individual query
    was correct. The writer here commits between two table reads; the export must not see
    it at all.
    """
    from whiskeyjack_bot import export as export_module

    writer = connect(ledger_path)
    real_read_table = export_module.read_table
    committed: list[str] = []

    def read_table_and_commit(connection: sqlite3.Connection, spec: Any) -> Any:
        rows = real_read_table(connection, spec)
        if spec.name == "forecast_records":
            writer.execute(
                "INSERT INTO forecast_records ("
                "record_id, question_id, tournament_id, forecast_version, question_type, "
                "status, model_provider, model_name, prompt_version, prompt_sha256, "
                "retrieval_run_id, generated_at_utc, final_prediction_json, record_json, "
                "created_at_utc, forecast_sha256, attempt_id) "
                "VALUES ('rec-mid', 199, 'minibench', 1, 'binary', 'draft', 'anthropic', "
                "'claude', 'v1', ?, 'run-1', ?, '{}', '{}', ?, ?, 'att-rec-mid')",
                (SHA, TS, TS, SHA),
            )
            committed.append("rec-mid")
        return rows

    monkeypatch.setattr(export_module, "read_table", read_table_and_commit)
    destination = tmp_path / "out"
    try:
        export_ledger(ledger_path, destination, export_format="jsonl")
    finally:
        writer.close()

    assert committed == ["rec-mid"], "the concurrent commit must actually have happened"
    # `lifecycle_events` is read after `forecast_records` alphabetically, so an export
    # taking a fresh snapshot per statement is exactly what would pick the new row up.
    ids = {row["record_id"] for row in _jsonl_rows(destination, "forecast_records")}
    assert "rec-mid" not in ids


# --------------------------------------------------------------------------------------
# Determinism.
# --------------------------------------------------------------------------------------


def test_two_jsonl_exports_of_an_unchanged_ledger_are_byte_identical(
    ledger_path: Path, tmp_path: Path
) -> None:
    """The guarantee JSONL makes unconditionally, including across versions."""
    first = export_ledger(ledger_path, tmp_path / "a", export_format="jsonl")
    second = export_ledger(ledger_path, tmp_path / "b", export_format="jsonl")
    assert [t.sha256 for t in first.tables] == [t.sha256 for t in second.tables]
    for spec in EXPORTED_TABLES:
        name = f"{spec.name}.jsonl"
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_two_parquet_exports_agree_for_a_fixed_pyarrow(ledger_path: Path, tmp_path: Path) -> None:
    """The narrower guarantee Parquet makes, stated as narrowly as it holds.

    Reproducible for a pinned pyarrow; not across versions, because every file embeds
    `parquet-cpp-arrow version <x.y.z>`. The manifest records the version so a digest that
    stops matching after a lock bump is explainable rather than alarming.
    """
    first = export_ledger(ledger_path, tmp_path / "a", export_format="parquet")
    second = export_ledger(ledger_path, tmp_path / "b", export_format="parquet")
    assert [t.sha256 for t in first.tables] == [t.sha256 for t in second.tables]


def test_row_order_is_the_identifier_and_not_insertion_order(
    ledger_path: Path, tmp_path: Path
) -> None:
    """Ordering is the explicit ORDER BY, not whatever SQLite hands back.

    Seeded out of identifier order on purpose: natural order is an implementation detail
    that a differently-planned query can change, and an export whose byte-stability rests
    on it is stable only by luck.
    """
    conn = connect(ledger_path)
    try:
        for record_id in ("rec-9", "rec-0", "rec-5"):
            conn.execute(
                "INSERT INTO forecast_records ("
                "record_id, question_id, tournament_id, forecast_version, question_type, "
                "status, model_provider, model_name, prompt_version, prompt_sha256, "
                "retrieval_run_id, generated_at_utc, final_prediction_json, record_json, "
                "created_at_utc, forecast_sha256, attempt_id) "
                "VALUES (?, ?, 'minibench', 1, 'binary', 'draft', 'anthropic', 'claude', "
                "'v1', ?, 'run-1', ?, '{}', '{}', ?, ?, ?)",
                (record_id, 200 + int(record_id[-1]), SHA, TS, TS, SHA, f"att-{record_id}"),
            )
    finally:
        conn.close()

    destination = tmp_path / "out"
    export_ledger(ledger_path, destination, export_format="jsonl")
    exported = [row["record_id"] for row in _jsonl_rows(destination, "forecast_records")]
    assert exported == sorted(exported)
    assert exported != ["rec-1", "rec-9", "rec-0", "rec-5"], "insertion order must not survive"


# --------------------------------------------------------------------------------------
# The manifest.
# --------------------------------------------------------------------------------------


def test_the_manifest_describes_the_export_it_sits_beside(
    ledger_path: Path, tmp_path: Path
) -> None:
    """D-1002 requires every table/field and export version to have a definition.

    So the definition ships *in the export*, not only in docs: a consumer holding the
    directory can name the export version, the ledger schema that produced it, every
    table's columns in DDL order, and a digest per file.
    """
    destination = tmp_path / "out"
    export_ledger(ledger_path, destination, export_format="jsonl")
    manifest = json.loads((destination / MANIFEST_FILENAME).read_text(encoding="utf-8"))

    assert manifest["export_schema_version"] == EXPORT_SCHEMA_VERSION
    assert manifest["ledger_schema_version"] == LEDGER_SCHEMA_VERSION
    assert manifest["format"] == "jsonl"
    assert manifest["exported_at_utc"].endswith("Z")

    described = {entry["name"]: entry for entry in manifest["tables"]}
    assert described.keys() == {spec.name for spec in EXPORTED_TABLES}
    for spec in EXPORTED_TABLES:
        entry = described[spec.name]
        # DDL order, which `sort_keys=True` drops from the row objects themselves.
        assert entry["columns"] == list(spec.column_names)
        assert entry["identifier"] == spec.identifier
        payload = (destination / entry["file"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
        assert entry["row_count"] == len(_jsonl_rows(destination, spec.name))


def test_the_parquet_manifest_records_the_writer_version(ledger_path: Path, tmp_path: Path) -> None:
    """Because Parquet's bytes depend on it and JSONL's do not."""
    import pyarrow as pa

    destination = tmp_path / "out"
    export_ledger(ledger_path, destination, export_format="parquet")
    manifest = json.loads((destination / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["writer"] == {"pyarrow": str(pa.__version__)}

    plain = tmp_path / "jsonl"
    export_ledger(ledger_path, plain, export_format="jsonl")
    assert json.loads((plain / MANIFEST_FILENAME).read_text(encoding="utf-8"))["writer"] == {}


# --------------------------------------------------------------------------------------
# Refusals, and the hygiene of their messages.
# --------------------------------------------------------------------------------------


def _corrupt(
    ledger_path: Path,
    statement: str,
    parameters: tuple[object, ...] = (),
    *,
    recursive_triggers: bool = True,
) -> None:
    """Plant an off-contract value, bypassing the writers that would refuse it.

    Not an invented attacker: CLAUDE.md's threat boundary says values read back out of the
    ledger are untrusted, and every shape planted here is one SQLite's dynamic typing
    permits regardless of what the writers bind.

    `recursive_triggers=False` is the documented hole rather than a convenience.
    `ledger.connect` sets that pragma precisely because it is **per connection** and
    defaults off, and `ledger.py` says so in as many words: "a raw `sqlite3` CLI session
    against the file can still REPLACE, and SQLite offers no schema-level defence against
    that." An `INSERT OR REPLACE` from such a session skips the BEFORE DELETE block
    triggers, which is the only way an in-place type swap reaches a populated table -- and
    it is reachable by an ordinary operator with a shell, so the export owes it an answer.
    """
    conn = sqlite3.connect(ledger_path)
    try:
        if recursive_triggers:
            conn.execute("PRAGMA recursive_triggers = ON")
        conn.execute(statement, parameters)
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("what", "statement", "parameters", "table", "column"),
    [
        (
            "a non-finite real",
            "INSERT INTO score_events (forecast_record_id, metric, value, "
            "implementation_version, computed_at_utc) VALUES ('rec-1', 'inf', 9e999, 'v1', ?)",
            (TS,),
            "score_events",
            "value",
        ),
        (
            "text that is not valid UTF-8",
            "INSERT INTO score_events (forecast_record_id, metric, value, "
            "implementation_version, computed_at_utc) "
            "VALUES ('rec-1', CAST(x'fffe6162' AS TEXT), 1.0, 'v1', ?)",
            (TS,),
            "score_events",
            "metric",
        ),
        (
            "a blob where the schema declares text",
            "INSERT INTO score_events (forecast_record_id, metric, value, "
            "implementation_version, computed_at_utc) VALUES ('rec-1', x'00ff', 1.0, 'v1', ?)",
            (TS,),
            "score_events",
            "metric",
        ),
        (
            "text where the schema declares a real",
            # Reached through the recursive_triggers hole -- see `_corrupt`. Every other
            # route into a populated append-only table is refused by the block triggers,
            # which is worth knowing: the schema guards this well, and the export's own
            # guard is the second line rather than the first.
            "INSERT OR REPLACE INTO score_events (event_id, forecast_record_id, metric, "
            "value, implementation_version, computed_at_utc) "
            "SELECT event_id, forecast_record_id, metric, 'not-a-number', "
            "implementation_version, computed_at_utc FROM score_events",
            (),
            "score_events",
            "value",
        ),
    ],
)
def test_an_off_contract_stored_value_is_refused_as_an_export_error(
    ledger_path: Path,
    tmp_path: Path,
    what: str,
    statement: str,
    parameters: tuple[object, ...],
    table: str,
    column: str,
) -> None:
    """Every malformed shape arrives as this module's own error type.

    A raw `sqlite3.Error`/`ValueError`/`UnicodeDecodeError` escaping is a review finding in
    this project -- it has been, twice -- and the UTF-8 case is worse than a wrong type:
    sqlite3's own decode failure interpolates the offending bytes into its message.
    """
    _corrupt(
        ledger_path,
        statement,
        parameters,
        recursive_triggers=not statement.startswith("INSERT OR REPLACE"),
    )
    with pytest.raises(ExportError) as excinfo:
        export_ledger(ledger_path, tmp_path / "out", export_format="jsonl")
    message = str(excinfo.value)
    assert table in message and column in message, what


def test_a_refusal_names_the_column_and_never_the_bytes(ledger_path: Path, tmp_path: Path) -> None:
    """The no-echo rule, on the one path where the library would have broken it.

    `sqlite3`'s default text factory raises `OperationalError: Could not decode to UTF-8
    column 'metric' with text '<the bytes>'`. Reading with `text_factory = bytes` and
    decoding in our own code is what keeps that content out of the message; this test is
    what stops the connection quietly reverting to the default.
    """
    _corrupt(
        ledger_path,
        "INSERT INTO score_events (forecast_record_id, metric, value, "
        "implementation_version, computed_at_utc) "
        "VALUES ('rec-1', CAST(x'fffe6162' AS TEXT), 1.0, 'v1', ?)",
        (TS,),
    )
    with pytest.raises(ExportError) as excinfo:
        export_ledger(ledger_path, tmp_path / "out", export_format="jsonl")

    rendered = f"{excinfo.value}{excinfo.value.__cause__}{excinfo.value.__context__}"
    assert "score_events" in rendered and "metric" in rendered
    assert "Could not decode" not in rendered
    assert "ab" not in rendered.replace("table", "").replace("readable", "")
    assert "�" not in rendered
    # from None, so the cause chain cannot reprint through a rendered traceback.
    assert excinfo.value.__cause__ is None


def test_a_planted_secret_never_reaches_a_refusal_message(
    ledger_path: Path, tmp_path: Path
) -> None:
    """A stored value is content whatever it happens to be, including a pasted credential."""
    _corrupt(
        ledger_path,
        "INSERT INTO score_events (forecast_record_id, metric, value, "
        "implementation_version, computed_at_utc) VALUES ('rec-1', ?, 9e999, 'v1', ?)",
        (FAKE_SECRET, TS),
    )
    with pytest.raises(ExportError) as excinfo:
        export_ledger(ledger_path, tmp_path / "out", export_format="jsonl")
    assert FAKE_SECRET not in f"{excinfo.value}{excinfo.value.__cause__}"


def test_an_unknown_format_is_refused_before_anything_is_written(
    ledger_path: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "out"
    with pytest.raises(ExportError, match="export_format must be one of"):
        export_ledger(ledger_path, destination, export_format="csv")  # type: ignore[arg-type]
    assert not destination.exists()


def test_an_existing_export_is_never_overwritten(ledger_path: Path, tmp_path: Path) -> None:
    """An export that replaced an earlier one would destroy the audit trail it provides."""
    destination = tmp_path / "out"
    export_ledger(ledger_path, destination, export_format="jsonl")
    original = (destination / "forecast_records.jsonl").read_bytes()
    with pytest.raises(ExportError, match="already exists"):
        export_ledger(ledger_path, destination, export_format="jsonl")
    assert (destination / "forecast_records.jsonl").read_bytes() == original


def test_a_missing_ledger_is_refused_and_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "absent.sqlite3"
    with pytest.raises(LedgerError):
        export_ledger(missing, tmp_path / "out", export_format="jsonl")
    assert not missing.exists()


def test_a_ledger_behind_this_build_is_refused_rather_than_half_exported(
    tmp_path: Path,
) -> None:
    """`no such column` is a worse answer than "run init-ledger".

    The export names columns that later migrations added, so an older database cannot
    satisfy the contract -- and `connect_readonly` deliberately does not migrate it into
    shape, because a read that rewrites its own source is the thing this item exists to
    avoid.
    """
    db = tmp_path / "old.sqlite3"
    initialize_ledger(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (LEDGER_SCHEMA_VERSION,))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(LedgerError, match="behind this build"):
        export_ledger(db, tmp_path / "out", export_format="jsonl")


def test_the_export_does_not_migrate_a_ledger_it_reads(tmp_path: Path) -> None:
    """The trap `open_verified_ledger` would have walked into.

    That opener calls `_migrate`, so exporting through it would let a derived read apply
    schema changes to its own source. Here a ledger that is already current is exported and
    its schema bookkeeping is unchanged -- and the test above covers the other direction,
    where a pending migration is refused rather than quietly applied.
    """
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect_readonly(db)
    try:
        before = conn.execute(
            "SELECT version, applied_at_utc, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        conn.close()

    export_ledger(db, tmp_path / "out", export_format="jsonl")

    conn = connect_readonly(db)
    try:
        after = conn.execute(
            "SELECT version, applied_at_utc, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    assert [tuple(row) for row in before] == [tuple(row) for row in after]


# --------------------------------------------------------------------------------------
# Secret hygiene, which belongs to M1-605 and is checked here rather than re-implemented.
# --------------------------------------------------------------------------------------


def test_the_export_carries_the_redaction_the_ledger_already_applied(
    ledger_path: Path, tmp_path: Path
) -> None:
    """M1-605's criterion names exports explicitly, so this branch owes it a check.

    Redaction happens at *write* time (`lifecycle.py` scrubs `response_body`,
    `response_headers` and `error_message` before the row is stored), so what the export
    must do is carry that through unaltered. It adds no second redaction pass on purpose:
    a second opinion about what counts as a secret is a second source of truth about it,
    and the two would drift.
    """
    destination = tmp_path / "out"
    export_ledger(ledger_path, destination, export_format="jsonl")
    attempts = _jsonl_rows(destination, "submission_attempts")
    assert attempts[0]["response_headers"] == "[REDACTED:METACULUS_TOKEN]"

    everything = b"".join(
        path.read_bytes() for path in sorted(destination.iterdir()) if path.is_file()
    )
    assert FAKE_SECRET.encode() not in everything
