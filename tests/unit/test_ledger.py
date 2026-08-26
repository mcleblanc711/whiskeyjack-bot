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
    open_verified_ledger,
)

LEDGER_TABLES = {
    "forecast_records",
    "research_runs",
    "research_documents",
    "approval_events",
    "submission_attempts",
    "submission_verifications",
    "resolution_events",
    "score_events",
    "lifecycle_events",
    "pipeline_failure_events",
    "schema_migrations",
}

# Canonical UTC form: 003 pins it on the columns it orders (see test_lifecycle.py).
TS = "2026-07-17T00:00:00.000000+00:00"
# Migration 003 requires every new forecast record to carry a 64-hex content hash.
FORECAST_SHA = "b" * 64


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _seed_run(conn: sqlite3.Connection, run_id: str = "run-1") -> None:
    # question_id is supplied because migration 002's triggers require it of every
    # new row. These helpers previously wrote 001-era columns only, which is
    # exactly the provenance-less write the triggers exist to stop.
    conn.execute(
        "INSERT INTO research_runs (retrieval_run_id, provider, question_id, started_at_utc, "
        "created_at_utc) VALUES (?, 'asknews', 100, ?, ?)",
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
    forecast_sha256: str | None = FORECAST_SHA,
    attempt_id: str | None = None,
) -> None:
    # forecast_sha256 is supplied for the same reason _seed_run supplies question_id:
    # migration 003's triggers require it of every new row, and a record with no content
    # hash is exactly the unapprovable row that requirement exists to stop. attempt_id is
    # migration 004's equivalent, and defaults per record_id because 004 also indexes it
    # UNIQUE where not null -- a shared default would fire that index in tests aimed at a
    # different constraint.
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
        "forecast_sha256, attempt_id) "
        "VALUES (?, ?, 'minibench', ?, 'binary', ?, 'anthropic', 'claude', 'v1', 'abc', ?, "
        "?, '{}', '{}', ?, ?, ?)",
        (
            record_id,
            question_id,
            version,
            status,
            run_id,
            TS,
            TS,
            forecast_sha256,
            attempt_id or f"att-{record_id}",
        ),
    )


def _seed_attempt(
    conn: sqlite3.Connection, *, attempt_id: str, key: str, record_id: str = "rec-1"
) -> None:
    conn.execute(
        "INSERT INTO submission_attempts ("
        "attempt_id, forecast_record_id, idempotency_key, requested_at_utc, "
        "completed_at_utc, request_payload_sha256, success, verified_by_refetch, "
        "refetch_outcome, created_at_utc) VALUES (?, ?, ?, ?, ?, 'deadbeef', 0, 0, 'absent', ?)",
        (attempt_id, record_id, key, TS, TS, TS),
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
        "document_id, retrieval_run_id, original_url, canonical_url, retrieved_at_utc, "
        "source_type, provenance, content_sha256) "
        "VALUES (?, ?, ?, ?, ?, 'news', 'direct_api', ?)",
        (document_id, run_id, url, url, TS, sha),
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
        # Not a stylistic pragma: with this off (SQLite's default), the deletes that
        # `INSERT OR REPLACE` performs to clear a constraint conflict skip every BEFORE
        # DELETE trigger, which is the whole of migration 003's append-only enforcement.
        # tests/unit/test_lifecycle.py exercises the REPLACE statements themselves.
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    finally:
        conn.close()


def test_foreign_keys_not_enabled_raises_ledger_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No reachable SQLite build here ever ignores `PRAGMA foreign_keys = ON`, so this
    # patches the read-back query itself to report the disabled value -- exercising the
    # refusal path per M1-609's acceptance note that no deterministic failure is
    # reproducible on the supported runtime.
    db = tmp_path / "ledger.db"
    initialize_ledger(db)

    class _ForeignKeysDisabledConnection(sqlite3.Connection):
        # sqlite3.Connection is a C type: its methods can't be monkeypatched on the
        # instance or the class, so the fake reports the disabled read-back through a
        # subclass installed as sqlite3.connect's factory instead.
        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            if sql == "PRAGMA foreign_keys":
                return super().execute("SELECT 0")
            return super().execute(sql, *args)

    real_connect = sqlite3.connect

    def fake_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = _ForeignKeysDisabledConnection
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    with pytest.raises(LedgerError, match="does not support foreign keys"):
        connect(db)


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
    # score_events rather than approval_events: migration 003 puts a BEFORE INSERT
    # trigger on approval_events that rejects a row naming an unknown record before the
    # foreign key is ever reached, so an approval row can no longer reach the FK and
    # would leave this test asserting a different mechanism than its name claims.
    db = tmp_path / "ledger.db"
    initialize_ledger(db)
    conn = connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO score_events ("
                "forecast_record_id, metric, value, implementation_version, computed_at_utc) "
                "VALUES ('does-not-exist', 'brier', 0.25, 'v1', ?)",
                (TS,),
            )
    finally:
        conn.close()


def test_status_check_rejects_unknown_state(tmp_path: Path) -> None:
    # 001's CHECK over the seven states is now the second line of defence: migration 003
    # pins a *new* record to 'draft', so the trigger rejects 'bogus' first and the CHECK
    # is only reachable by a write path that bypasses the trigger. Both refuse it.
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


def test_migration_005_adds_the_discarded_evidence_counters(tmp_path: Path) -> None:
    """M1-306: the counts of retrieved evidence that never became a document row.

    Both are NULLable on purpose, and the two states are different claims: NULL is
    *unmeasured* (a run opened before its calls, or a row predating 005), 0 is the
    auditable claim that nothing was discarded. A NOT NULL DEFAULT 0 would collapse
    them into the second, manufacturing a measurement -- the same reasoning that
    stopped 002 defaulting `provenance` to 'direct_api'.
    """
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(research_runs)")}
        assert {"documents_dropped", "duplicates_collapsed"} <= columns

        _seed_run(conn, "run-null")
        stored = conn.execute(
            "SELECT documents_dropped, duplicates_collapsed FROM research_runs "
            "WHERE retrieval_run_id = 'run-null'"
        ).fetchone()
        assert tuple(stored) == (None, None)
    finally:
        conn.close()


def test_migration_005_counters_reject_a_non_integer_or_negative_count(
    tmp_path: Path,
) -> None:
    """`INTEGER` in SQLite is affinity, not a type.

    A REAL that cannot be losslessly converted stays REAL and a non-numeric string
    stays TEXT, so without the `typeof()` half of the CHECK both 1.5 and 'garbage'
    satisfy `>= 0` -- 'garbage' passing only because SQLite orders TEXT above every
    number. 002 spells this out for `posts_dropped_no_url`; 005 inherits it.
    """
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        for value in (-1, 1.5, "garbage"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO research_runs (retrieval_run_id, provider, question_id, "
                    "started_at_utc, created_at_utc, documents_dropped) "
                    "VALUES (?, 'asknews', 100, ?, ?, ?)",
                    (f"run-{value}", TS, TS, value),
                )
    finally:
        conn.close()


def test_open_verified_refuses_an_absent_database_and_creates_nothing(tmp_path: Path) -> None:
    """`create=False` is what closes a check-then-open race (M2-701 review round 1).

    A caller that has already checked the file exists still races its own answer:
    `sqlite3.connect` brings a database into being for any path it is handed, so a
    deletion or rotation between the check and the open yields a brand-new empty ledger
    that then answers questions about content it never held. Re-checking cannot close
    that window; an open that *cannot* create can.
    """
    db = tmp_path / "gone" / "ledger.db"
    with pytest.raises(LedgerError):
        connect(db, create=False)
    assert not db.exists()
    assert not db.parent.exists()

    with pytest.raises(LedgerError):
        open_verified_ledger(db)
    # initialize_ledger's create path makes the parent directory; the existing-only path
    # must not, or a mistyped --config would leave a directory tree behind it.
    assert not db.exists()
    assert not db.parent.exists()


def test_open_verified_refuses_a_database_removed_after_it_was_seen(tmp_path: Path) -> None:
    """The race run forwards: a ledger that existed, and then did not.

    This is the shape the CLI hits -- `_open_existing_ledger` checks, and the file is
    gone by the time the open runs. It must refuse, and must not leave a replacement
    behind for the next command to read as the real ledger.
    """
    db = tmp_path / "ledger.db"
    initialize_ledger(db)
    db.unlink()
    with pytest.raises(LedgerError):
        open_verified_ledger(db)
    assert not db.exists()


def test_open_verified_returns_the_connection_it_verified(tmp_path: Path) -> None:
    """The second window, and the reason this is one function (review round 2).

    Verifying through one connection and reopening the pathname is worse than the first
    race, because refusing to create catches nothing: both files exist. An atomic
    rotation in between -- a backup, a restore, a rename -- and the schema that was
    checked is not the database that gets written. Holding the connection makes the
    rotation harmless: the descriptor still refers to the database that was verified.
    """
    db = tmp_path / "ledger.db"
    replacement = tmp_path / "replacement.db"
    initialize_ledger(db)
    initialize_ledger(replacement)
    conn = open_verified_ledger(db)
    try:
        conn.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, question_id, "
            "started_at_utc, created_at_utc) VALUES ('verified', 'asknews', 100, ?, ?)",
            (TS, TS),
        )
        replacement.replace(db)  # the rotation, after the connection was handed over
        rows = conn.execute("SELECT retrieval_run_id FROM research_runs").fetchall()
        assert [row[0] for row in rows] == ["verified"]
    finally:
        conn.close()
    # The replacement was never opened by us and never written through: its own content
    # is untouched. (Read it in isolation -- the rotation strands the verified ledger's
    # WAL under the old pathname, and SQLite replays a foreign WAL beside a database it
    # was not written for. See the standing risk in docs/M2-NOTES.md.)
    isolated = tmp_path / "isolated.db"
    isolated.write_bytes(db.read_bytes())
    conn = sqlite3.connect(isolated)
    try:
        assert conn.execute("SELECT count(*) FROM research_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_open_verified_verifies_the_schema_and_hands_back_a_usable_connection(
    tmp_path: Path,
) -> None:
    """Refusing to create is not the only difference from `connect`: it also migrates."""
    db = tmp_path / "ledger.db"
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION
    conn = open_verified_ledger(db)
    try:
        assert _table_names(conn) == LEDGER_TABLES
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    finally:
        conn.close()


def test_open_verified_closes_the_connection_when_verification_fails(tmp_path: Path) -> None:
    """A refusal must not leak the descriptor it opened to decide on the refusal."""
    db = tmp_path / "ledger.db"
    initialize_ledger(db)
    conn = connect(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE schema_migrations SET checksum = 'drifted' WHERE version = 1")
        conn.execute("COMMIT")
    finally:
        conn.close()
    with pytest.raises(LedgerError):
        open_verified_ledger(db)
    # A leaked connection would still hold this ledger's locks; an exclusive write proves
    # nothing is holding it open.
    conn = connect(db)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_open_verified_refuses_without_leaking_the_reason(tmp_path: Path) -> None:
    """Same hygiene as every other ledger raise: the path, and nothing else.

    SQLite's own message for a `mode=rw` miss is "unable to open database file", and the
    URI it was handed is percent-encoded -- neither may reach the operator through the
    message or a rendered traceback.
    """
    db = tmp_path / "secret-value-in-name" / "ledger.db"
    with pytest.raises(LedgerError) as excinfo:
        connect(db, create=False)
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert str(excinfo.value) == f"cannot open ledger database at {db}"
    assert "unable to open" not in rendered
    assert "mode=rw" not in rendered
    assert "file://" not in rendered


def test_initialize_ledger_still_creates_so_every_existing_caller_is_unchanged(
    tmp_path: Path,
) -> None:
    """The existing-only seam is a separate function; the pipeline's writers still create."""
    db = tmp_path / "fresh" / "ledger.db"
    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION
    assert db.is_file()
    second = tmp_path / "fresh" / "second.db"
    conn = connect(second)
    try:
        assert second.is_file()
    finally:
        conn.close()
