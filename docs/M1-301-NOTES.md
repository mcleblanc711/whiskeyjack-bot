# Milestone 1 — Retrieval epic (M1-30x) implementation notes

Running record of M1-30x decisions and deviations, in the spirit of `docs/M0-REVIEW.md`
and `docs/M1-NOTES.md`.

**This file is temporary and merges back into `docs/M1-NOTES.md`.** It is split out
because M1 is being built across parallel worktrees (one branch per backlog item), and
`docs/M1-NOTES.md` is the one file every branch would otherwise append to — guaranteeing a
textual merge conflict on every merge. Keeping the retrieval epic's notes here means this
branch shares zero files with the other M1 tracks.

**Merge-back trigger:** when the retrieval epic (M1-301 through M1-308) is complete and
merged to master, append these sections to `docs/M1-NOTES.md` in issue order and delete
this file. Do it as a single docs-only commit, after the last M1-30x merge, so the running
record ends up in one place for the milestone review.

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
