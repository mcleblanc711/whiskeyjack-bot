# Milestone 1 implementation notes

Running record of M1 decisions and deviations, in the spirit of `docs/M0-REVIEW.md`.
M1 began after the owner's explicit stop-point go-ahead (see `docs/M0-REVIEW.md`); Codex
retains independent verification and owns M1-605 plus the acceptance/contract suites
(T-901/903/904), which are authored blind against M1 code as it lands.

## M1-601 — Initial SQLite ledger migration + DB layer

The attribution ledger is the v1 source of truth (D16) and the tap-root of M1: this migration
gates M1-602/603/604, M1-406 and Codex's T-903 dry-run acceptance test.

Delivered:
- `src/whiskeyjack_bot/migrations/001_initial.sql` — the seven append-only ledger tables
  (`forecast_records`, `research_runs`, `research_documents`, `approval_events`,
  `submission_attempts`, `resolution_events`, `score_events`) plus a `schema_migrations`
  tracker. Constraints per `CODEX_HANDOFF.md` "Ledger design": `UNIQUE(question_id,
  tournament_id, forecast_version)`, `submission_attempts.idempotency_key UNIQUE`,
  `UNIQUE(retrieval_run_id, canonical_url, content_sha256)`, foreign keys between tables,
  and a `status` CHECK over the seven lifecycle states.
- `src/whiskeyjack_bot/ledger.py` — `connect()` (WAL, `foreign_keys=ON`, `busy_timeout`,
  autocommit + explicit `BEGIN`/`COMMIT`) and idempotent `initialize_ledger()` that applies
  unrecorded migrations and tracks each by version + sha256 checksum. `LedgerError` follows the
  `ConfigError`/`SnapshotError` hygiene rule (never echo stored values; `from None`).
- `tests/unit/test_ledger.py` — 10 tests: table set, WAL/FK pragmas, each unique constraint,
  FK enforcement, `status` CHECK, deterministic + idempotent re-run, and a no-leak `LedgerError`
  path. Suite: 96 passed; ruff check + format + `mypy --strict src` clean.

Deviation — **migrations live inside the package** (`whiskeyjack_bot.migrations`) rather than at
the repo root shown in the handoff's *proposed* tree. Rationale: they then ship in the wheel and
load via `importlib.resources` regardless of install layout; `hatchling` already packages
`src/whiskeyjack_bot`, so the subdir is included with no config change. No new runtime dependency
(`sqlite3` is stdlib) — `uv.lock` is untouched and the locked-sync CI step stays green.

Deferred (do not read the absence as an omission):
- The append-only **enforcement mechanism** (UPDATE/DELETE-blocking triggers on the event tables)
  and how `forecast_records.status` transitions relate to immutability land with **M1-602/M1-603**,
  where the write paths are built.
- `record_id` generation (UUIDv7/ULID) belongs with the first writer (**M1-602**); no ID minting
  in this DB-layer-only slice.

## M1-301 — Research-run and research-document schema

Gates the whole Retrieval epic: M1-302 (AskNews), M1-303 (Exa), M1-304 (structured router),
M1-305 (dedup/freshness) and M1-307 (X agent) all normalize into this shape.

Delivered:
- `src/whiskeyjack_bot/research/model.py` — `ResearchDocument` and `ResearchRun`, strict
  (`extra="forbid"`, reusing `config._StrictModel`), with closed `Literal` vocabularies
  (`SourceType`, `Provenance`, `ReliabilityTag`, `RetrievalProvider`). Timestamps are
  timezone-aware-only and normalized to UTC, matching the snapshot rule that a naive timestamp is
  not valid provenance. `validate_document()` / `validate_run()` are the sanctioned entry points:
  they sanitize pydantic errors exactly as `ConfigError` does, since a research document carries
  arbitrary provider text.
- `src/whiskeyjack_bot/research/hashing.py` — `content_sha256()` and its pinned normalization
  rule (NFC → collapse whitespace runs → strip → UTF-8 → SHA-256). Case and punctuation are
  deliberately *not* normalized: both carry meaning in a quoted statement.
- `src/whiskeyjack_bot/migrations/002_research_document_fields.sql` — `original_url` and
  `provenance` on `research_documents`; `agent_model`, `posts_dropped_no_url` and `question_id`
  on `research_runs`. `LEDGER_SCHEMA_VERSION` bumped to 2.
- `tests/unit/test_research.py` — 20 tests. Suite: 124 passed; ruff check + format +
  `mypy --strict src` clean.

Two fields did not exist in M1-601 and are added here rather than by editing `001_initial.sql`
(which is checksum-pinned):
- **`provenance`** was introduced by the brief's X-adapter amendment (`CLAUDE_CODE_PROMPT.md` § B)
  *after* M1-601 shipped, and that amendment assigns the backfill to M1-301. M1-601 was correct
  against `CODEX_HANDOFF.md`'s column list as it stood.
- **`original_url`** is required by the M1-301 acceptance criterion ("preserves original URL").
  M1-305 rewrites `canonical_url` for dedup; without this column the as-retrieved URL is
  unrecoverable, which is an attribution loss.

Deviations:
- **Migration 002's columns are NULLable, not NOT NULL.** SQLite requires a non-null default on an
  added NOT NULL column, and defaulting `provenance` to `direct_api` would stamp an unearned
  provenance claim onto any pre-existing row — a false attribution record. Pydantic is therefore
  the enforcement point (required in the model, nullable in the table) until the write path and
  its append-only triggers land in M1-602/M1-603.
- **No CHECK on `source_type` / `reliability_tag`.** `ADD COLUMN` carries a CHECK (used for
  `provenance`), but constraining a pre-existing column requires the 12-step table rebuild — not
  worth the risk on a merged migration for vocabularies the strict models already close.
- **`source_type` is enumerated** (`news`, `web`, `official`, `structured`, `social`) although the
  handoff leaves it as free-text TEXT. Ambiguity rule 4: an unrecognized source type is a
  normalization bug and should fail loudly rather than land in the ledger as a label.
- **`ResearchRun.question_id` is a plain `int`**, not an M1-201 `CanonicalQuestion`. The run needs
  the question's identity, not its content; importing the model would couple the retrieval epic to
  the normalization epic for no gain. M1-301 has no dependency on the M1-201 branch.

Deferred (do not read the absence as an omission):
- URL canonicalization policy, duplicate collapsing and stale-flagging are **M1-305**. Adapters
  landing before it may set `canonical_url` equal to `original_url`.
- `document_id` minting belongs to the first writer (**M1-602**), consistent with how M1-601
  deferred `record_id`; the field is optional on the model for that reason.
- The allowlist loader that consumes `ReliabilityTag` is **M1-308**; it will import the alias from
  this module rather than restate the values that `config/x_accounts.yaml`'s header pins.
