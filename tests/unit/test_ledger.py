"""M1-601 acceptance: the initial migration creates the append-only ledger
schema with WAL, foreign keys and the unique version/idempotency constraints,
and applies deterministically and idempotently without leaking stored values."""

import sqlite3
import traceback
from pathlib import Path

import pytest

from whiskeyjack_bot.ledger import (
    LEDGER_SCHEMA_VERSION,
    LedgerError,
    _statements,
    connect,
    initialize_ledger,
)

LEDGER_TABLES = {
    "forecast_records",
    "research_runs",
    "research_documents",
    "approval_events",
    "submission_attempts",
    "resolution_events",
    "score_events",
    "schema_migrations",
}

TS = "2026-07-17T00:00:00+00:00"


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _seed_run(conn: sqlite3.Connection, run_id: str = "run-1") -> None:
    conn.execute(
        "INSERT INTO research_runs (retrieval_run_id, provider, started_at_utc, created_at_utc) "
        "VALUES (?, 'asknews', ?, ?)",
        (run_id, TS, TS),
    )


def _seed_forecast(
    conn: sqlite3.Connection,
    *,
    record_id: str = "rec-1",
    question_id: int = 100,
    version: int = 1,
    status: str = "draft",
    run_id: str = "run-1",
) -> None:
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc) "
        "VALUES (?, ?, 'minibench', ?, 'binary', ?, 'anthropic', 'claude', 'v1', 'abc', ?, "
        "?, '{}', '{}', ?)",
        (record_id, question_id, version, status, run_id, TS, TS),
    )


def _seed_attempt(
    conn: sqlite3.Connection, *, attempt_id: str, key: str, record_id: str = "rec-1"
) -> None:
    conn.execute(
        "INSERT INTO submission_attempts ("
        "attempt_id, forecast_record_id, idempotency_key, requested_at_utc, "
        "request_payload_sha256, success, verified_by_refetch, created_at_utc) "
        "VALUES (?, ?, ?, ?, 'deadbeef', 0, 0, ?)",
        (attempt_id, record_id, key, TS, TS),
    )


def _seed_document(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    run_id: str = "run-1",
    url: str = "https://example.test/a",
    sha: str = "hash-1",
) -> None:
    conn.execute(
        "INSERT INTO research_documents ("
        "document_id, retrieval_run_id, canonical_url, retrieved_at_utc, content_sha256) "
        "VALUES (?, ?, ?, ?, ?)",
        (document_id, run_id, url, TS, sha),
    )


def test_fresh_migration_creates_all_tables(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    version = initialize_ledger(db)
    assert version == LEDGER_SCHEMA_VERSION
    conn = connect(db)
    try:
        assert _table_names(conn) == LEDGER_TABLES
    finally:
        conn.close()


def test_connect_enables_wal_and_foreign_keys(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    initialize_ledger(db)
    conn = connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_forecast_version_uniqueness_enforced(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    initialize_ledger(db)
    conn = connect(db)
    try:
        _seed_run(conn)
        _seed_forecast(conn, record_id="rec-1", version=1)
        # Same (question_id, tournament_id, forecast_version); different PK.
        with pytest.raises(sqlite3.IntegrityError):
            _seed_forecast(conn, record_id="rec-2", version=1)
    finally:
        conn.close()


def test_idempotency_key_uniqueness_enforced(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    initialize_ledger(db)
    conn = connect(db)
    try:
        _seed_run(conn)
        _seed_forecast(conn)
        _seed_attempt(conn, attempt_id="att-1", key="idem-1")
        with pytest.raises(sqlite3.IntegrityError):
            _seed_attempt(conn, attempt_id="att-2", key="idem-1")
    finally:
        conn.close()


def test_research_document_triple_uniqueness_enforced(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    initialize_ledger(db)
    conn = connect(db)
    try:
        _seed_run(conn)
        _seed_document(conn, document_id="doc-1")
        # Same (retrieval_run_id, canonical_url, content_sha256); different PK.
        with pytest.raises(sqlite3.IntegrityError):
            _seed_document(conn, document_id="doc-2")
    finally:
        conn.close()


def test_foreign_key_enforced(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    initialize_ledger(db)
    conn = connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO approval_events ("
                "forecast_record_id, decision, actor, forecast_sha256, created_at_utc) "
                "VALUES ('does-not-exist', 'approved', 'chris', 'sha', ?)",
                (TS,),
            )
    finally:
        conn.close()


def test_status_check_rejects_unknown_state(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    initialize_ledger(db)
    conn = connect(db)
    try:
        _seed_run(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _seed_forecast(conn, status="bogus")
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION
    # Second run applies nothing and does not error.
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION
    conn = connect(db)
    try:
        applied = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        assert applied == LEDGER_SCHEMA_VERSION  # exactly one row per applied migration
    finally:
        conn.close()


def test_schema_is_deterministic(tmp_path: Path) -> None:
    # Asserts *schema* (DDL) determinism only: two fresh initializations produce an
    # identical set of CREATE statements in sqlite_master. It deliberately does not
    # compare data, file bytes, WAL state, or schema_migrations.applied_at_utc (which
    # is a wall-clock timestamp and so is expected to differ between runs).
    def schema(db: Path) -> list[tuple[object, ...]]:
        initialize_ledger(db)
        conn = connect(db)
        try:
            rows = conn.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE sql IS NOT NULL ORDER BY type, name"
            ).fetchall()
            return [tuple(row) for row in rows]
        finally:
            conn.close()

    assert schema(tmp_path / "a.db") == schema(tmp_path / "b.db")


PLANTED_SECRET = "privateFAKE123456"


def _assert_no_leak(excinfo: pytest.ExceptionInfo[LedgerError]) -> None:
    assert PLANTED_SECRET not in str(excinfo.value)
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert PLANTED_SECRET not in rendered


def test_non_database_file_raises_ledger_error_without_leaking(tmp_path: Path) -> None:
    # A non-SQLite file at the target path makes the first PRAGMA raise; the
    # module wraps it in LedgerError with `from None` so the file's bytes
    # cannot surface through the message or a rendered traceback.
    db = tmp_path / "not.db"
    db.write_text(PLANTED_SECRET, encoding="utf-8")
    with pytest.raises(LedgerError) as excinfo:
        initialize_ledger(db)
    _assert_no_leak(excinfo)


def test_malformed_schema_migrations_raises_ledger_error_without_leaking(tmp_path: Path) -> None:
    # A *valid* SQLite database whose schema_migrations.version holds a non-integer
    # secret. Reading it converts version with int(); the module must wrap the
    # resulting ValueError in LedgerError so the planted value never surfaces
    # through the message or a rendered traceback.
    db = tmp_path / "planted.db"
    raw = sqlite3.connect(db)
    try:
        raw.execute(
            "CREATE TABLE schema_migrations (version TEXT, applied_at_utc TEXT, checksum TEXT)"
        )
        raw.execute(
            "INSERT INTO schema_migrations (version, applied_at_utc, checksum) VALUES (?, ?, ?)",
            (PLANTED_SECRET, TS, "sha"),
        )
        raw.commit()
    finally:
        raw.close()
    with pytest.raises(LedgerError) as excinfo:
        initialize_ledger(db)
    _assert_no_leak(excinfo)


def test_null_textual_primary_key_rejected(tmp_path: Path) -> None:
    # Textual PKs carry an explicit NOT NULL; without it SQLite rowid tables accept
    # multiple NULL-identity rows.
    db = tmp_path / "ledger.db"
    initialize_ledger(db)
    conn = connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO research_runs (retrieval_run_id, provider, started_at_utc, "
                "created_at_utc) VALUES (NULL, 'asknews', ?, ?)",
                (TS, TS),
            )
    finally:
        conn.close()


def test_checksum_drift_is_rejected(tmp_path: Path) -> None:
    # Corrupting the recorded checksum simulates the packaged migration changing
    # after it was applied; re-initialization must fail rather than silently accept
    # the drift.
    db = tmp_path / "ledger.db"
    initialize_ledger(db)
    conn = connect(db)
    try:
        conn.execute("UPDATE schema_migrations SET checksum = 'tampered'")
    finally:
        conn.close()
    with pytest.raises(LedgerError):
        initialize_ledger(db)


# A distinctive numeric version that would be conspicuous if echoed in an error.
NUMERIC_VERSION_SECRET = 9876543210987654


def test_newer_database_version_rejected_without_leaking(tmp_path: Path) -> None:
    # A schema_migrations row from a future build must be rejected -- and, per the
    # LedgerError hygiene contract, the rejection must not echo the stored (numeric)
    # version through the message or a rendered traceback.
    db = tmp_path / "ledger.db"
    initialize_ledger(db)
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at_utc, checksum) VALUES (?, ?, ?)",
            (NUMERIC_VERSION_SECRET, TS, "future"),
        )
    finally:
        conn.close()
    with pytest.raises(LedgerError) as excinfo:
        initialize_ledger(db)
    assert str(NUMERIC_VERSION_SECRET) not in str(excinfo.value)
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert str(NUMERIC_VERSION_SECRET) not in rendered


def test_statement_splitter_applies_triggers_and_literals() -> None:
    # The runner must apply the append-only triggers deferred to M1-602/603, so this
    # executes the split statements for real: a semicolon inside a trigger body or a
    # string literal must not split a statement, a trailing inline comment must not
    # create a spurious one, and the applied trigger must actually block UPDATE.
    sql = (
        "CREATE TABLE t (a TEXT);\n"
        "CREATE TRIGGER t_no_update BEFORE UPDATE ON t BEGIN\n"
        "    SELECT RAISE(ABORT, 'no; updates');\n"
        "END;\n"
        "INSERT INTO t (a) VALUES ('x;y'); -- trailing; comment\n"
    )
    statements = _statements(sql)
    assert len(statements) == 3
    conn = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            conn.execute(statement)
        assert conn.execute("SELECT a FROM t").fetchone()[0] == "x;y"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE t SET a = 'z'")
    finally:
        conn.close()


def test_statement_splitter_splits_multiple_statements_on_one_line() -> None:
    # Two complete statements on a single physical line must split into two executable
    # chunks -- conn.execute rejects a chunk that holds two statements.
    statements = _statements("CREATE TABLE a (x TEXT); CREATE TABLE b (y TEXT);\n")
    assert statements == ["CREATE TABLE a (x TEXT);", "CREATE TABLE b (y TEXT);"]
    conn = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            conn.execute(statement)
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables == {"a", "b"}
    finally:
        conn.close()


def test_statement_splitter_ignores_trailing_comments() -> None:
    # A migration may end with trailing -- line or /* block */ comments after the
    # final terminator; these are not an unterminated statement.
    assert _statements("SELECT 1; /* trailing; comment */\n") == ["SELECT 1;"]
    assert _statements("SELECT 1;\n-- trailing line comment\n") == ["SELECT 1;"]


def test_statement_splitter_rejects_unterminated_statement() -> None:
    with pytest.raises(LedgerError):
        _statements("CREATE TABLE t (a TEXT)")  # no terminating semicolon
