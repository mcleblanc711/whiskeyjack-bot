# Cross-model review request — whiskeyjack-bot M1-601

You are a rigorous senior reviewer performing an independent cross-model review of
code authored by another AI model (Claude). Apply the **stricter reading**: when a
line could be read as either correct or subtly wrong, assume the wrong reading and
prove it can't happen from the diff. Do **not** rubber-stamp. If you approve, justify
why each risk area below is actually safe; if you don't, list blocking findings.

## Project context

`whiskeyjack-bot` is a public Metaculus MiniBench forecasting pipeline whose primary
product is an **attribution ledger**: an immutable, replayable SQLite record of every
forecast, its evidence, approvals, submission attempts, resolutions and scores. Python
3.11, `src/` layout, offline-first (tests run with sockets disabled), toolchain gates
are `pytest`, `ruff check`, `ruff format --check`, `mypy --strict src`.

This is **M1-601**, the first Milestone-1 branch: the ledger's initial SQLite migration
and DB-access layer only. No row-writing/versioning yet (those are M1-602/603).

## Authoritative spec (from CODEX_HANDOFF.md "Ledger design" + decisions)

- SQLite is the v1 source of truth (D16). Use WAL mode, foreign keys, explicit
  transactions. Append-only; never overwrite history (D25).
- Minimum tables: `forecast_records`, `research_runs`, `research_documents`,
  `approval_events`, `submission_attempts`, `resolution_events`, `score_events`.
- `forecast_records` must have `UNIQUE(question_id, tournament_id, forecast_version)`
  and a `status` in {draft, validated, approved, submitted, failed, resolved, scored}.
- `research_documents` must have `UNIQUE(retrieval_run_id, canonical_url, content_sha256)`.
- `submission_attempts` carries the SubmissionReceipt fields and a unique idempotency key.
- Schema test requirement: "database migration from empty file is deterministic."
- **M1-601 acceptance:** "Fresh migration succeeds with WAL/foreign keys; unique
  version/idempotency constraints exist."
- Error-hygiene convention (established by `ConfigError`/`SnapshotError`): error messages
  never echo stored/file values, and sanitizing raises use `from None` so a mistakenly
  stored secret cannot surface through the exception text or a rendered traceback.

## Deliberate choices / out of scope (challenge the rationale, but these are not omissions)

- **Migrations live inside the package** (`src/whiskeyjack_bot/migrations/`) rather than
  repo-root as the handoff's *proposed* tree showed — so they ship in the wheel and load
  via `importlib.resources`. No new dependency (`sqlite3` is stdlib); `uv.lock` untouched.
- Append-only **enforcement** (UPDATE/DELETE-blocking triggers), `status`-transition
  semantics, and `record_id` (UUIDv7/ULID) minting are deferred to M1-602/603.
- Codex owns M1-605 (redaction) and the acceptance/contract suites (T-901/903/904); they
  are intentionally absent here.

## What to scrutinize (pressure-test these specifically)

1. **Transaction correctness.** `connect()` sets `isolation_level = None` (autocommit) and
   `_apply_migration` wraps DDL + the `schema_migrations` insert in explicit `BEGIN`/
   `COMMIT`/`ROLLBACK`. Is this actually atomic across CREATE TABLE + INSERT? Any path
   where a partial apply leaves tables without the version row (so a re-run fails)?
2. **Migration runner.** `_statements()` strips `--` full-line comments and splits on `;`.
   Where does this break for plausible future migrations (semicolons in string literals,
   `BEGIN…END` trigger bodies, inline `--` comments)? Is that acceptable for now, and is it
   documented/guarded?
3. **Idempotency & determinism.** Re-running `initialize_ledger` is claimed to be a no-op;
   `test_schema_is_deterministic` compares `sqlite_master` rows across two DBs. Are there
   non-deterministic elements (rowids, autoindex names, the `applied_at_utc` timestamp) that
   could make "deterministic" weaker than claimed?
4. **Foreign keys.** `forecast_records.retrieval_run_id` forward-references `research_runs`
   (declared later) and there's a self-FK on `parent_record_id`. Valid in SQLite? Is
   `foreign_keys=ON` reliably set on every connection that writes?
5. **Error hygiene.** Does `LedgerError` truly avoid echoing stored values, and does
   `test_non_database_file_raises_ledger_error_without_leaking` actually exercise a real leak
   path, or is it trivially green (i.e., would SQLite ever echo file contents here anyway)?
6. **Schema fidelity.** Do the DDL columns/constraints match the spec's ledger design and the
   SubmissionReceipt field list? Any missing NOT NULL / index / unique that M1-602/603 will
   need? Any type choice (booleans as INTEGER CHECK, timestamps as TEXT) that will bite later?
7. **Scope creep / hallucinations.** Anything implemented beyond M1-601, any invented API, or
   any claim in comments/docs not supported by the code?

## Output format

- **Verdict:** APPROVE / APPROVE-WITH-NITS / DO-NOT-APPROVE.
- **Findings**, ranked by severity (Blocker / High / Medium / Low / Nit), each with
  `file:line`, a one-line defect statement, and a concrete failure scenario (inputs → wrong
  outcome). Separate must-fix from optional.
- Explicitly note anything you **cannot** verify from the diff alone.
- If APPROVE, one line per risk area (1–7) stating why it's safe.

The complete branch diff (`git diff master...feat/m1-601-ledger-migration`) follows.

---

```diff
diff --git a/docs/M1-NOTES.md b/docs/M1-NOTES.md
new file mode 100644
index 0000000..31c5de1
--- /dev/null
+++ b/docs/M1-NOTES.md
@@ -0,0 +1,40 @@
+# Milestone 1 implementation notes
+
+Running record of M1 decisions and deviations, in the spirit of `docs/M0-REVIEW.md`.
+M1 began after the owner's explicit stop-point go-ahead (see `docs/M0-REVIEW.md`); Codex
+retains independent verification and owns M1-605 plus the acceptance/contract suites
+(T-901/903/904), which are authored blind against M1 code as it lands.
+
+## M1-601 — Initial SQLite ledger migration + DB layer
+
+The attribution ledger is the v1 source of truth (D16) and the tap-root of M1: this migration
+gates M1-602/603/604, M1-406 and Codex's T-903 dry-run acceptance test.
+
+Delivered:
+- `src/whiskeyjack_bot/migrations/001_initial.sql` — the seven append-only ledger tables
+  (`forecast_records`, `research_runs`, `research_documents`, `approval_events`,
+  `submission_attempts`, `resolution_events`, `score_events`) plus a `schema_migrations`
+  tracker. Constraints per `CODEX_HANDOFF.md` "Ledger design": `UNIQUE(question_id,
+  tournament_id, forecast_version)`, `submission_attempts.idempotency_key UNIQUE`,
+  `UNIQUE(retrieval_run_id, canonical_url, content_sha256)`, foreign keys between tables,
+  and a `status` CHECK over the seven lifecycle states.
+- `src/whiskeyjack_bot/ledger.py` — `connect()` (WAL, `foreign_keys=ON`, `busy_timeout`,
+  autocommit + explicit `BEGIN`/`COMMIT`) and idempotent `initialize_ledger()` that applies
+  unrecorded migrations and tracks each by version + sha256 checksum. `LedgerError` follows the
+  `ConfigError`/`SnapshotError` hygiene rule (never echo stored values; `from None`).
+- `tests/unit/test_ledger.py` — 10 tests: table set, WAL/FK pragmas, each unique constraint,
+  FK enforcement, `status` CHECK, deterministic + idempotent re-run, and a no-leak `LedgerError`
+  path. Suite: 96 passed; ruff check + format + `mypy --strict src` clean.
+
+Deviation — **migrations live inside the package** (`whiskeyjack_bot.migrations`) rather than at
+the repo root shown in the handoff's *proposed* tree. Rationale: they then ship in the wheel and
+load via `importlib.resources` regardless of install layout; `hatchling` already packages
+`src/whiskeyjack_bot`, so the subdir is included with no config change. No new runtime dependency
+(`sqlite3` is stdlib) — `uv.lock` is untouched and the locked-sync CI step stays green.
+
+Deferred (do not read the absence as an omission):
+- The append-only **enforcement mechanism** (UPDATE/DELETE-blocking triggers on the event tables)
+  and how `forecast_records.status` transitions relate to immutability land with **M1-602/M1-603**,
+  where the write paths are built.
+- `record_id` generation (UUIDv7/ULID) belongs with the first writer (**M1-602**); no ID minting
+  in this DB-layer-only slice.
diff --git a/docs/backlog/backlog.csv b/docs/backlog/backlog.csv
index c4181a9..73d6796 100644
--- a/docs/backlog/backlog.csv
+++ b/docs/backlog/backlog.csv
@@ -28,7 +28,7 @@ M1-501,Validation and Calibration,Validate common attribution fields,"Require pr
 M1-502,Validation and Calibration,Validate categorical forecasts,Apply binary bounds and exact multiple-choice normalization.,M1-403; M1-404,Critical,Claude Code,Boundary and sum tests pass; no arbitrary post-hoc renormalization is hidden.,S,Not Started,https://github.com/Metaculus/forecasting-tools/blob/main/forecasting_tools/helpers/metaculus_client.py
 M1-503,Validation and Calibration,Build and validate numeric CDF,Use NumericDistribution.from_question and get_cdf to produce the submission array.,M1-405,Critical,Claude Code,Exactly 201 monotone values for normal numeric questions; maintained PMF constraint passes.,M,Not Started,https://github.com/Metaculus/forecasting-tools/blob/main/forecasting_tools/data_models/numeric_report.py
 M1-504,Validation and Calibration,Add stale/insufficient research gate,Flag or fail according to config before approval.,M1-305; M1-501,High,Claude Code,No-sources and stale-only fixtures produce explicit states and never pass silently.,S,Not Started,config.example.yaml
-M1-601,Attribution Ledger,Create initial SQLite migration,"Create append-only forecast, research, document, approval, submission, resolution and score tables.",M0-001,Critical,Claude Code,Fresh migration succeeds with WAL/foreign keys; unique version/idempotency constraints exist.,M,Not Started,D16; CODEX_HANDOFF Ledger design
+M1-601,Attribution Ledger,Create initial SQLite migration,"Create append-only forecast, research, document, approval, submission, resolution and score tables.",M0-001,Critical,Claude Code,Fresh migration succeeds with WAL/foreign keys; unique version/idempotency constraints exist.,M,Done,D16; CODEX_HANDOFF Ledger design
 M1-602,Attribution Ledger,Persist immutable forecast versions,Append one record per forecast version with parent linkage and canonical JSON.,M1-601; M1-501,Critical,Claude Code,Updating a question appends v2; v1 remains byte-identical.,M,Not Started,D25
 M1-603,Attribution Ledger,Record lifecycle events atomically,"Persist validation, approval, submission and failure events in transactions.",M1-601,Critical,Claude Code,Injected failures cannot leave an approved/submitted state without its event record.,M,Not Started,D25
 M1-604,Attribution Ledger,Export JSONL and Parquet,Create analysis-friendly derived exports for audit and polygraph use.,M1-602,Medium,Claude Code,Exports round-trip record IDs/counts and never mutate SQLite.,M,Not Started,D29
diff --git a/src/whiskeyjack_bot/ledger.py b/src/whiskeyjack_bot/ledger.py
new file mode 100644
index 0000000..5e1bd99
--- /dev/null
+++ b/src/whiskeyjack_bot/ledger.py
@@ -0,0 +1,135 @@
+"""SQLite attribution-ledger schema and access layer (M1-601).
+
+The ledger is the v1 source of truth (decision D16): an append-only, replayable
+record of every forecast, its evidence, approvals, submission attempts,
+resolutions and scores. This module owns database connections and migration
+application only; row-writing helpers, forecast versioning and the append-only
+enforcement mechanism land with M1-602/M1-603.
+
+Migrations live inside the package (:mod:`whiskeyjack_bot.migrations`) rather
+than at the repository root shown in the handoff's proposed tree, so they ship
+in the wheel and load via ``importlib.resources`` regardless of install layout.
+
+Error hygiene follows ``ConfigError``/``SnapshotError``: a :class:`LedgerError`
+never echoes stored row values, and wrapped raises use ``from None`` so an
+underlying database exception cannot reprint a value through its cause chain.
+"""
+
+from __future__ import annotations
+
+import hashlib
+import re
+import sqlite3
+from datetime import datetime, timezone
+from importlib.resources import files
+from pathlib import Path
+
+LEDGER_SCHEMA_VERSION = 1
+
+_MIGRATIONS_PACKAGE = "whiskeyjack_bot.migrations"
+_MIGRATION_NAME_RE = re.compile(r"^(\d+)_.*\.sql$")
+_BUSY_TIMEOUT_MS = 5000
+
+
+class LedgerError(Exception):
+    """The ledger database cannot be opened or migrated.
+
+    Same hygiene rule as ``ConfigError``: the message never echoes stored
+    values, and wrapped raises use ``from None`` so a mistakenly stored secret
+    cannot surface through the underlying exception's text or traceback.
+    """
+
+
+def connect(path: Path) -> sqlite3.Connection:
+    """Open the ledger with WAL, foreign keys and explicit-transaction mode.
+
+    Purely local file I/O: no network access on any path through here.
+    """
+    try:
+        conn = sqlite3.connect(path)
+    except sqlite3.Error:
+        # from None: the underlying error can name the file; wrap it and keep
+        # the cause chain from reprinting anything about the target.
+        raise LedgerError(f"cannot open ledger database at {path}") from None
+    try:
+        conn.row_factory = sqlite3.Row
+        # Autocommit mode: DDL and migration bookkeeping run inside explicit
+        # BEGIN/COMMIT blocks (spec: explicit transactions), while the pragmas
+        # below run outside any transaction as SQLite requires.
+        conn.isolation_level = None
+        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
+        conn.execute("PRAGMA journal_mode = WAL")
+        conn.execute("PRAGMA synchronous = NORMAL")
+        conn.execute("PRAGMA foreign_keys = ON")
+    except sqlite3.Error:
+        conn.close()
+        raise LedgerError(f"cannot open ledger database at {path}") from None
+    return conn
+
+
+def initialize_ledger(path: Path) -> int:
+    """Create or upgrade the ledger schema; return the applied schema version.
+
+    Idempotent: migrations already recorded in ``schema_migrations`` are
+    skipped, so re-running is a no-op that does not error.
+    """
+    path.parent.mkdir(parents=True, exist_ok=True)
+    conn = connect(path)
+    try:
+        applied = _applied_versions(conn)
+        for version, sql, checksum in _load_migrations():
+            if version not in applied:
+                _apply_migration(conn, version, sql, checksum)
+        return _current_version(conn)
+    finally:
+        conn.close()
+
+
+def _applied_versions(conn: sqlite3.Connection) -> set[int]:
+    present = conn.execute(
+        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
+    ).fetchone()
+    if present is None:
+        return set()
+    return {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
+
+
+def _current_version(conn: sqlite3.Connection) -> int:
+    row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
+    return int(row[0]) if row is not None and row[0] is not None else 0
+
+
+def _load_migrations() -> list[tuple[int, str, str]]:
+    migrations: list[tuple[int, str, str]] = []
+    for entry in files(_MIGRATIONS_PACKAGE).iterdir():
+        match = _MIGRATION_NAME_RE.match(entry.name)
+        if match is None:
+            continue
+        data = entry.read_bytes()
+        migrations.append(
+            (int(match.group(1)), data.decode("utf-8"), hashlib.sha256(data).hexdigest())
+        )
+    migrations.sort(key=lambda item: item[0])
+    return migrations
+
+
+def _apply_migration(conn: sqlite3.Connection, version: int, sql: str, checksum: str) -> None:
+    applied_at = datetime.now(tz=timezone.utc).isoformat()
+    try:
+        conn.execute("BEGIN")
+        for statement in _statements(sql):
+            conn.execute(statement)
+        conn.execute(
+            "INSERT INTO schema_migrations (version, applied_at_utc, checksum) VALUES (?, ?, ?)",
+            (version, applied_at, checksum),
+        )
+        conn.execute("COMMIT")
+    except sqlite3.Error:
+        conn.execute("ROLLBACK")
+        # version is a filename-derived integer, never a stored value.
+        raise LedgerError(f"failed to apply ledger migration {version}") from None
+
+
+def _statements(sql: str) -> list[str]:
+    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
+    return [chunk.strip() for chunk in body.split(";") if chunk.strip()]
diff --git a/src/whiskeyjack_bot/migrations/001_initial.sql b/src/whiskeyjack_bot/migrations/001_initial.sql
new file mode 100644
index 0000000..1d21902
--- /dev/null
+++ b/src/whiskeyjack_bot/migrations/001_initial.sql
@@ -0,0 +1,124 @@
+-- M1-601: initial attribution-ledger schema.
+--
+-- SQLite is the v1 source of truth (decision D16). The ledger is append-only
+-- and replayable: forecasts, their evidence, approvals, submission attempts,
+-- resolutions and scores (decision D25 -- never overwrite history).
+--
+-- Connection-level settings (WAL journal, foreign_keys, busy_timeout) are set
+-- per connection in whiskeyjack_bot.ledger, not here: PRAGMAs are not part of
+-- the persisted schema and several are ignored inside a transaction.
+--
+-- Timestamps are TEXT ISO-8601 UTC, matching the snapshot convention.
+
+CREATE TABLE forecast_records (
+    record_id             TEXT PRIMARY KEY,
+    question_id           INTEGER NOT NULL,
+    post_id               INTEGER,
+    tournament_id         TEXT NOT NULL,
+    forecast_version      INTEGER NOT NULL,
+    parent_record_id      TEXT REFERENCES forecast_records (record_id),
+    question_type         TEXT NOT NULL,
+    question_domain       TEXT,
+    status                TEXT NOT NULL CHECK (
+        status IN ('draft', 'validated', 'approved', 'submitted', 'failed', 'resolved', 'scored')
+    ),
+    model_provider        TEXT NOT NULL,
+    model_name            TEXT NOT NULL,
+    prompt_version        TEXT NOT NULL,
+    prompt_sha256         TEXT NOT NULL,
+    retrieval_run_id      TEXT NOT NULL REFERENCES research_runs (retrieval_run_id),
+    generated_at_utc      TEXT NOT NULL,
+    final_prediction_json TEXT NOT NULL,
+    record_json           TEXT NOT NULL,
+    created_at_utc        TEXT NOT NULL,
+    UNIQUE (question_id, tournament_id, forecast_version)
+);
+
+CREATE TABLE research_runs (
+    retrieval_run_id     TEXT PRIMARY KEY,
+    provider             TEXT NOT NULL,
+    provider_config_json TEXT,
+    queries_json         TEXT,
+    started_at_utc       TEXT NOT NULL,
+    completed_at_utc     TEXT,
+    freshness_cutoff_utc TEXT,
+    raw_response_path    TEXT,
+    error_summary        TEXT,
+    cost_usd             REAL,
+    created_at_utc       TEXT NOT NULL
+);
+
+CREATE TABLE research_documents (
+    document_id       TEXT PRIMARY KEY,
+    retrieval_run_id  TEXT NOT NULL REFERENCES research_runs (retrieval_run_id),
+    canonical_url     TEXT NOT NULL,
+    title             TEXT,
+    publisher         TEXT,
+    author            TEXT,
+    published_at_utc  TEXT,
+    updated_at_utc    TEXT,
+    retrieved_at_utc  TEXT NOT NULL,
+    source_type       TEXT,
+    content_sha256    TEXT NOT NULL,
+    snippet           TEXT,
+    summary           TEXT,
+    raw_artifact_path TEXT,
+    reliability_tag   TEXT,
+    UNIQUE (retrieval_run_id, canonical_url, content_sha256)
+);
+
+CREATE TABLE approval_events (
+    event_id           INTEGER PRIMARY KEY,
+    forecast_record_id TEXT NOT NULL REFERENCES forecast_records (record_id),
+    decision           TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
+    actor              TEXT NOT NULL,
+    forecast_sha256    TEXT NOT NULL,
+    note               TEXT,
+    created_at_utc     TEXT NOT NULL
+);
+
+CREATE TABLE submission_attempts (
+    attempt_id                  TEXT PRIMARY KEY,
+    forecast_record_id          TEXT NOT NULL REFERENCES forecast_records (record_id),
+    idempotency_key             TEXT NOT NULL UNIQUE,
+    requested_at_utc            TEXT NOT NULL,
+    completed_at_utc            TEXT,
+    request_payload_sha256      TEXT NOT NULL,
+    http_status                 INTEGER,
+    response_body               TEXT,
+    response_headers            TEXT,
+    success                     INTEGER NOT NULL CHECK (success IN (0, 1)),
+    error_type                  TEXT,
+    error_message               TEXT,
+    verified_by_refetch         INTEGER NOT NULL CHECK (verified_by_refetch IN (0, 1)),
+    refetched_forecast_snapshot TEXT,
+    created_at_utc              TEXT NOT NULL
+);
+
+CREATE TABLE resolution_events (
+    event_id                 INTEGER PRIMARY KEY,
+    question_id              INTEGER NOT NULL,
+    forecast_record_id       TEXT REFERENCES forecast_records (record_id),
+    resolution_snapshot_json TEXT,
+    outcome                  TEXT,
+    annulled                 INTEGER NOT NULL DEFAULT 0 CHECK (annulled IN (0, 1)),
+    ambiguous                INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous IN (0, 1)),
+    source_response          TEXT,
+    ingested_at_utc          TEXT NOT NULL
+);
+
+CREATE TABLE score_events (
+    event_id               INTEGER PRIMARY KEY,
+    forecast_record_id     TEXT NOT NULL REFERENCES forecast_records (record_id),
+    metric                 TEXT NOT NULL,
+    value                  REAL NOT NULL,
+    implementation_version TEXT NOT NULL,
+    comparison_baseline    TEXT,
+    computed_at_utc        TEXT NOT NULL
+);
+
+CREATE TABLE schema_migrations (
+    version        INTEGER PRIMARY KEY,
+    applied_at_utc TEXT NOT NULL,
+    checksum       TEXT NOT NULL
+);
diff --git a/src/whiskeyjack_bot/migrations/__init__.py b/src/whiskeyjack_bot/migrations/__init__.py
new file mode 100644
index 0000000..c085f62
--- /dev/null
+++ b/src/whiskeyjack_bot/migrations/__init__.py
@@ -0,0 +1,6 @@
+"""Ledger schema migrations, applied by :mod:`whiskeyjack_bot.ledger`.
+
+Migrations live inside the package (not at the repository root shown in the
+handoff's proposed tree) so they ship in the wheel and load via
+``importlib.resources`` regardless of install layout.
+"""
diff --git a/tests/unit/test_ledger.py b/tests/unit/test_ledger.py
new file mode 100644
index 0000000..f461439
--- /dev/null
+++ b/tests/unit/test_ledger.py
@@ -0,0 +1,226 @@
+"""M1-601 acceptance: the initial migration creates the append-only ledger
+schema with WAL, foreign keys and the unique version/idempotency constraints,
+and applies deterministically and idempotently without leaking stored values."""
+
+import sqlite3
+import traceback
+from pathlib import Path
+
+import pytest
+
+from whiskeyjack_bot.ledger import (
+    LEDGER_SCHEMA_VERSION,
+    LedgerError,
+    connect,
+    initialize_ledger,
+)
+
+LEDGER_TABLES = {
+    "forecast_records",
+    "research_runs",
+    "research_documents",
+    "approval_events",
+    "submission_attempts",
+    "resolution_events",
+    "score_events",
+    "schema_migrations",
+}
+
+TS = "2026-07-17T00:00:00+00:00"
+
+
+def _table_names(conn: sqlite3.Connection) -> set[str]:
+    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
+
+
+def _seed_run(conn: sqlite3.Connection, run_id: str = "run-1") -> None:
+    conn.execute(
+        "INSERT INTO research_runs (retrieval_run_id, provider, started_at_utc, created_at_utc) "
+        "VALUES (?, 'asknews', ?, ?)",
+        (run_id, TS, TS),
+    )
+
+
+def _seed_forecast(
+    conn: sqlite3.Connection,
+    *,
+    record_id: str = "rec-1",
+    question_id: int = 100,
+    version: int = 1,
+    status: str = "draft",
+    run_id: str = "run-1",
+) -> None:
+    conn.execute(
+        "INSERT INTO forecast_records ("
+        "record_id, question_id, tournament_id, forecast_version, question_type, status, "
+        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
+        "generated_at_utc, final_prediction_json, record_json, created_at_utc) "
+        "VALUES (?, ?, 'minibench', ?, 'binary', ?, 'anthropic', 'claude', 'v1', 'abc', ?, "
+        "?, '{}', '{}', ?)",
+        (record_id, question_id, version, status, run_id, TS, TS),
+    )
+
+
+def _seed_attempt(
+    conn: sqlite3.Connection, *, attempt_id: str, key: str, record_id: str = "rec-1"
+) -> None:
+    conn.execute(
+        "INSERT INTO submission_attempts ("
+        "attempt_id, forecast_record_id, idempotency_key, requested_at_utc, "
+        "request_payload_sha256, success, verified_by_refetch, created_at_utc) "
+        "VALUES (?, ?, ?, ?, 'deadbeef', 0, 0, ?)",
+        (attempt_id, record_id, key, TS, TS),
+    )
+
+
+def _seed_document(
+    conn: sqlite3.Connection,
+    *,
+    document_id: str,
+    run_id: str = "run-1",
+    url: str = "https://example.test/a",
+    sha: str = "hash-1",
+) -> None:
+    conn.execute(
+        "INSERT INTO research_documents ("
+        "document_id, retrieval_run_id, canonical_url, retrieved_at_utc, content_sha256) "
+        "VALUES (?, ?, ?, ?, ?)",
+        (document_id, run_id, url, TS, sha),
+    )
+
+
+def test_fresh_migration_creates_all_tables(tmp_path: Path) -> None:
+    db = tmp_path / "ledger.db"
+    version = initialize_ledger(db)
+    assert version == LEDGER_SCHEMA_VERSION
+    conn = connect(db)
+    try:
+        assert _table_names(conn) == LEDGER_TABLES
+    finally:
+        conn.close()
+
+
+def test_connect_enables_wal_and_foreign_keys(tmp_path: Path) -> None:
+    db = tmp_path / "ledger.db"
+    initialize_ledger(db)
+    conn = connect(db)
+    try:
+        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
+        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
+    finally:
+        conn.close()
+
+
+def test_forecast_version_uniqueness_enforced(tmp_path: Path) -> None:
+    db = tmp_path / "ledger.db"
+    initialize_ledger(db)
+    conn = connect(db)
+    try:
+        _seed_run(conn)
+        _seed_forecast(conn, record_id="rec-1", version=1)
+        # Same (question_id, tournament_id, forecast_version); different PK.
+        with pytest.raises(sqlite3.IntegrityError):
+            _seed_forecast(conn, record_id="rec-2", version=1)
+    finally:
+        conn.close()
+
+
+def test_idempotency_key_uniqueness_enforced(tmp_path: Path) -> None:
+    db = tmp_path / "ledger.db"
+    initialize_ledger(db)
+    conn = connect(db)
+    try:
+        _seed_run(conn)
+        _seed_forecast(conn)
+        _seed_attempt(conn, attempt_id="att-1", key="idem-1")
+        with pytest.raises(sqlite3.IntegrityError):
+            _seed_attempt(conn, attempt_id="att-2", key="idem-1")
+    finally:
+        conn.close()
+
+
+def test_research_document_triple_uniqueness_enforced(tmp_path: Path) -> None:
+    db = tmp_path / "ledger.db"
+    initialize_ledger(db)
+    conn = connect(db)
+    try:
+        _seed_run(conn)
+        _seed_document(conn, document_id="doc-1")
+        # Same (retrieval_run_id, canonical_url, content_sha256); different PK.
+        with pytest.raises(sqlite3.IntegrityError):
+            _seed_document(conn, document_id="doc-2")
+    finally:
+        conn.close()
+
+
+def test_foreign_key_enforced(tmp_path: Path) -> None:
+    db = tmp_path / "ledger.db"
+    initialize_ledger(db)
+    conn = connect(db)
+    try:
+        with pytest.raises(sqlite3.IntegrityError):
+            conn.execute(
+                "INSERT INTO approval_events ("
+                "forecast_record_id, decision, actor, forecast_sha256, created_at_utc) "
+                "VALUES ('does-not-exist', 'approved', 'chris', 'sha', ?)",
+                (TS,),
+            )
+    finally:
+        conn.close()
+
+
+def test_status_check_rejects_unknown_state(tmp_path: Path) -> None:
+    db = tmp_path / "ledger.db"
+    initialize_ledger(db)
+    conn = connect(db)
+    try:
+        _seed_run(conn)
+        with pytest.raises(sqlite3.IntegrityError):
+            _seed_forecast(conn, status="bogus")
+    finally:
+        conn.close()
+
+
+def test_migration_is_idempotent(tmp_path: Path) -> None:
+    db = tmp_path / "ledger.db"
+    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION
+    # Second run applies nothing and does not error.
+    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION
+    conn = connect(db)
+    try:
+        applied = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
+        assert applied == LEDGER_SCHEMA_VERSION  # exactly one row per applied migration
+    finally:
+        conn.close()
+
+
+def test_schema_is_deterministic(tmp_path: Path) -> None:
+    def schema(db: Path) -> list[tuple[object, ...]]:
+        initialize_ledger(db)
+        conn = connect(db)
+        try:
+            rows = conn.execute(
+                "SELECT type, name, sql FROM sqlite_master "
+                "WHERE sql IS NOT NULL ORDER BY type, name"
+            ).fetchall()
+            return [tuple(row) for row in rows]
+        finally:
+            conn.close()
+
+    assert schema(tmp_path / "a.db") == schema(tmp_path / "b.db")
+
+
+PLANTED_SECRET = "privateFAKE123456"
+
+
+def test_non_database_file_raises_ledger_error_without_leaking(tmp_path: Path) -> None:
+    # A non-SQLite file at the target path makes the first PRAGMA raise; the
+    # module wraps it in LedgerError with `from None` so the file's bytes
+    # cannot surface through the message or a rendered traceback.
+    db = tmp_path / "not.db"
+    db.write_text(PLANTED_SECRET, encoding="utf-8")
+    with pytest.raises(LedgerError) as excinfo:
+        initialize_ledger(db)
+    assert PLANTED_SECRET not in str(excinfo.value)
+    rendered = "".join(traceback.format_exception(excinfo.value))
+    assert PLANTED_SECRET not in rendered
```

---

# Changes since round 1 (please re-review)

Round 1 verdict was **DO-NOT-APPROVE** (4 High, 2 Medium, nits). Every finding was
independently reproduced against the code and then fixed on
`feat/m1-601-ledger-migration`. The working tree now holds the updated code — please
re-run the gates and re-review the resolutions below. No row writers or lifecycle were
added; scope remains the migration + DB-access layer.

**Toolchain (re-verified locally):** `pytest` all pass (incl. 6 new ledger tests),
`ruff check`, `ruff format --check`, `mypy --strict src` all clean; wheel builds and
includes `whiskeyjack_bot/migrations/001_initial.sql`.

## Finding-by-finding resolution

- **H1 — nullable textual PKs.** Every textual primary key now carries an explicit
  `NOT NULL` (`forecast_records.record_id`, `research_runs.retrieval_run_id`,
  `research_documents.document_id`, `submission_attempts.attempt_id`) in
  `migrations/001_initial.sql`, with a header comment explaining the SQLite rowid-table
  caveat. New test `test_null_textual_primary_key_rejected`. (INTEGER PK event tables
  are rowid aliases and already reject NULL.)

- **H2 — semicolon splitter can't apply triggers.** `_statements` in `ledger.py` no
  longer splits on `;`. It accumulates lines and uses `sqlite3.complete_statement`
  (the `sqlite3_complete` wrapper), which understands `CREATE TRIGGER … BEGIN … END;`
  bodies, unclosed string literals, and inline comments. A non-comment remainder after
  the last complete statement is rejected as unterminated. The supported grammar is
  documented in the function docstring. New test
  `test_statement_splitter_preserves_triggers_and_literals` applies a real
  `RAISE(ABORT, 'no; updates')` trigger as one statement; a live check confirmed the
  applied trigger actually blocks `UPDATE`. Plus `test_statement_splitter_rejects_unterminated_statement`.

- **H3 — checksums never validated.** `_applied_migrations` now returns
  `{version: stored_checksum}`. On every `initialize_ledger`, an already-applied
  migration is re-checked against the packaged sha256 via `_verify_checksum` and fails
  with `LedgerError` on drift (constant message, no checksum echoed). Duplicate
  migration numbers among packaged files are rejected in `_load_migrations`, and
  `_reject_newer_database` rejects a DB whose max recorded version exceeds the highest
  packaged version. New tests `test_checksum_drift_is_rejected`,
  `test_newer_database_version_is_rejected`.

- **H4 — error hygiene / inert leak test.** All `int()` conversions
  (`_applied_migrations`, `_current_version`) and `path.parent.mkdir` are wrapped to
  raise `LedgerError` with a constant, value-free message using `from None` and the
  existing `(detail withheld: it can echo stored values)` idiom. The inert no-leak test
  is replaced by `test_malformed_schema_migrations_raises_ledger_error_without_leaking`,
  which builds a **valid** SQLite DB with a planted secret in `schema_migrations.version`
  and asserts the secret appears in neither the message nor the rendered traceback. The
  original non-DB-file test is retained for the `connect()` path.

- **M5 — WAL not verified.** `connect()` now captures the row returned by
  `PRAGMA journal_mode = WAL` and raises `LedgerError` if the mode in force is not
  `wal` (covers WAL-incapable VFS and special paths like `:memory:`). Existing
  `test_connect_enables_wal_and_foreign_keys` continues to assert `wal`.

- **M6 — missing FK indexes.** Added `CREATE INDEX` for
  `approval_events.forecast_record_id`, `submission_attempts.forecast_record_id`,
  `resolution_events.forecast_record_id`, `resolution_events.question_id`, and
  `score_events.forecast_record_id`.

- **Nits.** SQL header and module docstring now say "append-only **by policy**;
  database enforcement (UPDATE/DELETE-blocking triggers) lands in M1-602/603."
  `test_schema_is_deterministic` carries a comment clarifying it asserts *schema* (DDL)
  determinism only, not data / WAL / bytes / `applied_at_utc`.

## Note on the migration checksum

`001_initial.sql` was edited **in place** rather than adding a new migration. This
migration has not shipped to any real database (feature branch, no production caller
yet), so rewriting it — and its resulting sha256 — is correct; no forward-migration is
warranted for a schema that has never been applied outside tests.

---

# Changes since round 2 (please re-review)

Round 2 verdict was **DO-NOT-APPROVE** (1 High remaining, 1 Medium, 1 Low). Both
graded findings are fixed and the Low is addressed, all in `ledger.py` /
`tests/unit/test_ledger.py` on `feat/m1-601-ledger-migration`. Toolchain re-verified:
`pytest` all pass (incl. the new/updated splitter + no-leak tests), `ruff check`,
`ruff format --check`, `mypy --strict src` all clean.

- **H (round-2) — newer-version message echoed a stored value.** `_reject_newer_database`
  no longer interpolates `highest_applied` (which is `int(stored schema_migrations.version)`,
  i.e. row content). The message is now value-free — it names only `highest_packaged`,
  which is derived from the packaged migration filenames — and the misleading comment is
  corrected. New regression test `test_newer_database_version_rejected_without_leaking`
  plants a distinctive numeric version and asserts it appears in neither the message nor
  the rendered traceback.

- **M (round-2) — two statements on one physical line.** `_statements` now scans
  character-by-character and emits at each top-level terminator (gating the
  `sqlite3.complete_statement` call on `;`), so `CREATE TABLE a(x); CREATE TABLE b(y);`
  splits into two independently executable chunks instead of a single chunk that
  `conn.execute` would reject. String literals, `--` comments, and `BEGIN…END` trigger
  bodies are still respected (the semicolon check stays False inside them). The docstring
  now states multiple statements per line are supported. New test
  `test_statement_splitter_splits_multiple_statements_on_one_line` splits and executes both.

- **L (round-2) — trigger test now applies the trigger.** `test_statement_splitter_applies_triggers_and_literals`
  (renamed from `…preserves…`) executes the split statements against an in-memory
  database and asserts the applied `RAISE(ABORT, …)` trigger actually blocks `UPDATE`,
  rather than only inspecting the split strings.

Round-2 residuals confirmed still-good in this pass: H1 NOT NULL PKs, H3 checksum/drift,
H4 non-numeric sanitization, M5 WAL, M6 indexes, wording nits — all unchanged.
