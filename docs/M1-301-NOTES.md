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
  on `research_runs`, plus the `BEFORE INSERT`/`BEFORE UPDATE` triggers that enforce them.
  `LEDGER_SCHEMA_VERSION` bumped to 2.
- `tests/unit/test_research.py` — 53 tests. Suite: 157 passed; ruff check + format +
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
- **Migration 002's columns are NULLable, but enforced by trigger, not left to Pydantic.** SQLite
  requires a non-null default on an added NOT NULL column, and defaulting `provenance` to
  `direct_api` would stamp an unearned provenance claim onto any pre-existing row — a false
  attribution record. Column constraints therefore cannot distinguish the two populations, so
  `BEFORE INSERT` / `BEFORE UPDATE` triggers do it instead: pre-002 rows keep their honest NULLs,
  and every row written from here on must carry `original_url`, `provenance`, `source_type` (and
  `question_id` on runs). Intended consequence: a legacy row cannot be UPDATEd until it is also
  backfilled — the ledger is append-only, so nothing should be updating it.
  *(Revised after cross-model review round 2; the first cut deferred all database-level
  enforcement to M1-602, which would have left the database accepting provenance-less writes
  indefinitely on nothing but convention.)*
- **The triggers carry the vocabulary checks a CHECK cannot be retrofitted with.** `ADD COLUMN`
  carries a CHECK (used for `provenance`), but constraining the pre-existing `source_type` /
  `reliability_tag` columns would require the 12-step table rebuild. The triggers close those
  vocabularies without it. Numeric range checks (`cost_usd`, `posts_dropped_no_url`) stay
  model-side: an off-range number is a bad measurement, not a row that cannot be interpreted.
- **`reliability_tag` is conditionally required, never unconditionally.** It is NULL for every
  provider with no trust model of its own; it is required only of social documents. Enforced both
  model-side and by trigger.
- **`source_type` is enumerated** (`news`, `web`, `official`, `structured`, `social`) although the
  handoff leaves it as free-text TEXT. Ambiguity rule 4: an unrecognized source type is a
  normalization bug and should fail loudly rather than land in the ledger as a label.
- **`ResearchRun.question_id` is a plain `int`**, not an M1-201 `CanonicalQuestion`. The run needs
  the question's identity, not its content; importing the model would couple the retrieval epic to
  the normalization epic for no gain. M1-301 has no dependency on the M1-201 branch.
- **`source_type="social"` binds to `provenance="llm_reported"` plus a non-null `reliability_tag`.**
  The brief describes exactly one route to a social document — the xAI research agent reports it,
  so its content and timestamps are claims, and it always carries a tag defaulting to
  `unverified_social`. The forecaster prompt's evidence caps read those two fields, so a social
  document missing either escapes the cap silently. Ambiguity rule 4: implement the stricter
  reading. **Revisit if a direct X API adapter ever ships** — it would produce
  `social`/`direct_api` and require a deliberate schema change, which is the intent.
- **`xai_x_search` runs must carry `agent_model` and `posts_dropped_no_url`.** D27 forbids a silent
  model default, and `agent_model` is config-supplied so it is known even for a run that failed
  outright. `posts_dropped_no_url` is required so that `0` (nothing was discarded) stays
  distinguishable from `NULL` (nobody counted).
- **URLs must be absolute http(s) and free of surrounding whitespace**, checked without rewriting
  the string. This is not canonicalization (still M1-305) — the stored URL stays byte-for-byte what
  the provider returned, tracking parameters and all. It rejects only input that is not a URL.
- **`provider_config` is `dict[str, JsonValue]`, not `dict[str, Any]`.** The column is
  `provider_config_json TEXT`; a value that cannot round-trip through JSON is not storable, and
  must fail at validation rather than inside the ledger write, after the run has already happened.
- **`_sanitize` withholds error-location parts it did not author.** `include_input=False` withholds
  the offending *value*, but under `extra="forbid"` the location **is** the caller's key (likewise
  for `provider_config` dict keys), so a credential pasted as a key leaked where one pasted as a
  value did not. Only `int` indices and declared field names now survive into a message.

Deferred (do not read the absence as an omission):
- URL canonicalization policy, duplicate collapsing and stale-flagging are **M1-305**. Adapters
  landing before it may set `canonical_url` equal to `original_url`.
- `document_id` minting belongs to the first writer (**M1-602**), consistent with how M1-601
  deferred `record_id`; the field is optional on the model for that reason.
- The allowlist loader that consumes `ReliabilityTag` is **M1-308**; it will import the alias from
  this module rather than restate the values that `config/x_accounts.yaml`'s header pins.
- `created_at_utc` is **writer-owned metadata** and deliberately absent from both models: it records
  when the ledger stored the row, so only the write path (**M1-602**) may set it. Letting an adapter
  supply it would let a caller backdate its own audit trail — the same reasoning as `document_id`.
  Documented in the `model.py` docstring alongside the `provider_config` → `provider_config_json`
  and `queries` → `queries_json` column mappings.
