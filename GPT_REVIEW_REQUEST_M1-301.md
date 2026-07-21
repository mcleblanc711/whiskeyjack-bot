# Cross-model review request — whiskeyjack-bot M1-301

You are a rigorous senior reviewer performing an independent cross-model review of
code authored by another AI model (Claude). Apply the **stricter reading**: when a
line could be read as either correct or subtly wrong, assume the wrong reading and
prove it can't happen from the diff. Do **not** rubber-stamp. If you approve, justify
why each risk area below is actually safe; if you don't, list blocking findings.

## How to get the code

The branch is pushed. Prefer reviewing the working tree over the pasted diff — the diff is
included below as a fallback, but several questions (does a field have a column? does a
pydantic symbol exist?) are answerable only against the full files and the pinned dependency
versions.

```bash
git clone https://github.com/mcleblanc711/whiskeyjack-bot.git
cd whiskeyjack-bot
git checkout feat/m1-301-research-document-schema
git diff master...HEAD          # this branch's three commits

# optional, to reproduce the gates (needs uv):
uv sync
uv run pytest                   # expect 124 passed
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
```

Key files: `src/whiskeyjack_bot/research/{model,hashing}.py`,
`src/whiskeyjack_bot/migrations/002_research_document_fields.sql`,
`tests/unit/test_research.py`. Read `001_initial.sql` and `ledger.py` from master for the
schema and migration-runner context those build on.

## Project context

`whiskeyjack-bot` is a public Metaculus MiniBench forecasting pipeline whose primary
product is an **attribution ledger**: an immutable, replayable SQLite record of every
forecast, its evidence, approvals, submission attempts, resolutions and scores. Python
3.11, `src/` layout, offline-first (tests run with sockets disabled), toolchain gates
are `pytest`, `ruff check`, `ruff format --check`, `mypy --strict src`.

This is **M1-301**, the schema that gates the entire Retrieval epic: M1-302 (AskNews),
M1-303 (Exa), M1-304 (structured-source router), M1-305 (dedup/freshness) and M1-307
(xAI X Search agent) all normalize into the models defined here. It builds on **M1-601**
(the ledger migration + DB layer), which you reviewed and approved across three rounds.

## Authoritative spec

From `docs/backlog/backlog.csv` (M1-301 row):

> **Define research-document schema.** Normalize provider results with stable source IDs,
> URLs, timestamps, hashes and reliability tags.
> **Acceptance:** "Pydantic schema preserves original URL, published/retrieved times and
> raw artifact reference."

From `CODEX_HANDOFF.md` "Ledger design":

> `research_documents`: `document_id`, `retrieval_run_id`, canonical URL, title, publisher,
> author, published/updated/retrieved timestamps, source type, content hash, snippet/summary,
> raw artifact path and reliability tag. Unique `(retrieval_run_id, canonical_url, content_sha256)`.
> `research_runs`: identity, provider/config, query list, started/completed timestamps,
> freshness cutoff, raw-response paths, error summary and cost.

From `CLAUDE_CODE_PROMPT.md` § B (the brief amendment that postdates M1-601):

> `source_type` = `social`; **new field** `provenance` = `llm_reported` (AskNews/Exa/structured
> documents set `direct_api`; add this field to the M1-301 schema and backfill the other adapters)
> `reliability_tag` ∈ {`official_primary`, `verified_org`, `journalist`, `unverified_social`}
> `content_sha256` over normalized `quoted_text`; dedup by `(canonical_url, content_sha256)`

Standing conventions this branch must honor:

- **Error hygiene** (established by `ConfigError`/`SnapshotError`/`LedgerError`): messages never
  echo stored or input values, and sanitizing raises use `from None` so a mistakenly pasted
  secret cannot surface through exception text or a rendered traceback.
- **Ambiguity rule 4**: where an acceptance criterion is ambiguous, implement the stricter
  reading and note it.
- **D27**: no silent defaults for model identity.
- Migrations are checksum-pinned once applied; an applied migration file is never edited.

## Deliberate choices / out of scope (challenge the rationale, but these are not omissions)

- **Migration 002 adds five NULLable columns** rather than `NOT NULL`. SQLite requires a
  non-null default on an added `NOT NULL` column, and defaulting `provenance` to `'direct_api'`
  would stamp an unearned provenance claim onto any pre-existing row. Pydantic is the
  enforcement point until M1-602 builds the write path. **This is a real model/table gap and
  the single choice most worth attacking.**
- **No CHECK on the pre-existing `source_type` / `reliability_tag` columns.** `ADD COLUMN`
  carries a CHECK (used for the new `provenance`), but constraining an existing column requires
  the 12-step table-rebuild procedure.
- **`source_type` is enumerated** (`news`/`web`/`official`/`structured`/`social`) though the
  handoff leaves it free-text — ambiguity rule 4.
- **`ResearchRun.question_id` is a plain `int`**, not the M1-201 `CanonicalQuestion` model
  (which lives unmerged on a parallel branch). The run needs the question's identity, not its
  content.
- **Deferred, by design:** URL canonicalization, duplicate collapsing and stale-flagging are
  M1-305; `document_id` minting is M1-602 (consistent with how M1-601 deferred `record_id`);
  the allowlist loader that consumes `ReliabilityTag` is M1-308. No adapter, no network code.
- Codex owns the acceptance/contract suites (T-901/903/904); they are intentionally absent.

## What to scrutinize (pressure-test these specifically)

1. **The NULLable-column decision.** Is "Pydantic requires it, SQLite doesn't" actually safe
   until M1-602, or does it create a window where a writer bypassing `validate_document()`
   silently persists a document with NULL `provenance` — a document whose trustworthiness is
   then unknowable? Is the stated alternative (defaulting to `direct_api`) really worse? Is
   there a third option that was missed?
2. **The content-hash rule.** `content_sha256` normalizes NFC → collapse whitespace runs →
   strip → UTF-8 → SHA-256, and deliberately does *not* fold case or punctuation. Does this
   under-normalize (two renderings of one article hashing differently, defeating dedup) or
   over-normalize (two genuinely different claims colliding)? The digest feeds
   `UNIQUE(retrieval_run_id, canonical_url, content_sha256)` and the replay hash, so a change
   later is a breaking change — is the rule right *now*?
3. **Timestamp handling.** All datetimes are aware-only and normalized to UTC via
   `AfterValidator`. Does `AwareDatetime` + `astimezone` behave as claimed for every input
   pydantic accepts (ISO strings, epoch ints, `datetime` objects)? Can a naive value slip
   through any path? Is discarding the original offset an attribution loss for
   `published_at_utc` (a post's local time can be evidence)?
4. **Error hygiene.** Does `validate_document`/`validate_run` genuinely prevent leakage?
   `exc.errors(include_input=False, include_url=False)` suppresses the input, but do any
   pydantic `msg` strings themselves embed the offending value (pattern mismatches, literal
   enumerations, `extra="forbid"` naming an unexpected *key*)? Is the leak test real or
   trivially green? Note that an unexpected key's *name* does appear in `loc` — is that a leak
   when the key name comes from provider data?
5. **Schema fidelity.** Do the two models cover every column in `research_documents` /
   `research_runs`, and vice versa? Anything the spec lists that has no field? Any field with
   no column (which would silently not persist)? Is `provider_config`/`queries` as
   `dict`/`list` reconcilable with the `*_json TEXT` columns?
6. **The migration itself.** Is `002_research_document_fields.sql` correct under the M1-601
   runner (`_statements()` splitting, the `BEGIN`/`COMMIT` wrapper, checksum recording)? Does
   the `CHECK` on an added column behave as expected in SQLite? Is bumping
   `LEDGER_SCHEMA_VERSION` to 2 consistent everywhere it is used?
7. **Downstream fitness.** This schema is a contract four adapters must satisfy. Read it as
   M1-302 and M1-307 will: is anything missing that an adapter will be forced to work around
   (e.g. multi-document runs, per-document cost, retraction/edit of a post, a document from
   more than one run)? Is `posts_dropped_no_url` on the *run* rather than per-call adequate
   for the citation-hygiene audit the brief requires?
8. **Scope creep / hallucinations.** Anything implemented beyond M1-301, any invented API
   (check every pydantic symbol actually exists in pydantic v2), or any claim in comments/docs
   not supported by the code?

## Output format

- **Verdict:** APPROVE / APPROVE-WITH-NITS / DO-NOT-APPROVE.
- **Findings**, ranked by severity (Blocker / High / Medium / Low / Nit), each with
  `file:line`, a one-line defect statement, and a concrete failure scenario (inputs → wrong
  outcome). Separate must-fix from optional.
- Explicitly note anything you **cannot** verify from the diff alone.
- If APPROVE, one line per risk area (1–8) stating why it's safe.

Toolchain state on this branch: 124 passed (20 new), `ruff check` + `ruff format --check` +
`mypy --strict src` all clean.

The complete branch diff (`git diff master...feat/m1-301-research-document-schema`) follows.

---

```diff
diff --git a/docs/M1-301-NOTES.md b/docs/M1-301-NOTES.md
new file mode 100644
index 0000000..9bac25b
--- /dev/null
+++ b/docs/M1-301-NOTES.md
@@ -0,0 +1,70 @@
+# Milestone 1 — Retrieval epic (M1-30x) implementation notes
+
+Running record of M1-30x decisions and deviations, in the spirit of `docs/M0-REVIEW.md`
+and `docs/M1-NOTES.md`.
+
+**This file is temporary and merges back into `docs/M1-NOTES.md`.** It is split out
+because M1 is being built across parallel worktrees (one branch per backlog item), and
+`docs/M1-NOTES.md` is the one file every branch would otherwise append to — guaranteeing a
+textual merge conflict on every merge. Keeping the retrieval epic's notes here means this
+branch shares zero files with the other M1 tracks.
+
+**Merge-back trigger:** when the retrieval epic (M1-301 through M1-308) is complete and
+merged to master, append these sections to `docs/M1-NOTES.md` in issue order and delete
+this file. Do it as a single docs-only commit, after the last M1-30x merge, so the running
+record ends up in one place for the milestone review.
+
+## M1-301 — Research-run and research-document schema
+
+Gates the whole Retrieval epic: M1-302 (AskNews), M1-303 (Exa), M1-304 (structured router),
+M1-305 (dedup/freshness) and M1-307 (X agent) all normalize into this shape.
+
+Delivered:
+- `src/whiskeyjack_bot/research/model.py` — `ResearchDocument` and `ResearchRun`, strict
+  (`extra="forbid"`, reusing `config._StrictModel`), with closed `Literal` vocabularies
+  (`SourceType`, `Provenance`, `ReliabilityTag`, `RetrievalProvider`). Timestamps are
+  timezone-aware-only and normalized to UTC, matching the snapshot rule that a naive timestamp is
+  not valid provenance. `validate_document()` / `validate_run()` are the sanctioned entry points:
+  they sanitize pydantic errors exactly as `ConfigError` does, since a research document carries
+  arbitrary provider text.
+- `src/whiskeyjack_bot/research/hashing.py` — `content_sha256()` and its pinned normalization
+  rule (NFC → collapse whitespace runs → strip → UTF-8 → SHA-256). Case and punctuation are
+  deliberately *not* normalized: both carry meaning in a quoted statement.
+- `src/whiskeyjack_bot/migrations/002_research_document_fields.sql` — `original_url` and
+  `provenance` on `research_documents`; `agent_model`, `posts_dropped_no_url` and `question_id`
+  on `research_runs`. `LEDGER_SCHEMA_VERSION` bumped to 2.
+- `tests/unit/test_research.py` — 20 tests. Suite: 124 passed; ruff check + format +
+  `mypy --strict src` clean.
+
+Two fields did not exist in M1-601 and are added here rather than by editing `001_initial.sql`
+(which is checksum-pinned):
+- **`provenance`** was introduced by the brief's X-adapter amendment (`CLAUDE_CODE_PROMPT.md` § B)
+  *after* M1-601 shipped, and that amendment assigns the backfill to M1-301. M1-601 was correct
+  against `CODEX_HANDOFF.md`'s column list as it stood.
+- **`original_url`** is required by the M1-301 acceptance criterion ("preserves original URL").
+  M1-305 rewrites `canonical_url` for dedup; without this column the as-retrieved URL is
+  unrecoverable, which is an attribution loss.
+
+Deviations:
+- **Migration 002's columns are NULLable, not NOT NULL.** SQLite requires a non-null default on an
+  added NOT NULL column, and defaulting `provenance` to `direct_api` would stamp an unearned
+  provenance claim onto any pre-existing row — a false attribution record. Pydantic is therefore
+  the enforcement point (required in the model, nullable in the table) until the write path and
+  its append-only triggers land in M1-602/M1-603.
+- **No CHECK on `source_type` / `reliability_tag`.** `ADD COLUMN` carries a CHECK (used for
+  `provenance`), but constraining a pre-existing column requires the 12-step table rebuild — not
+  worth the risk on a merged migration for vocabularies the strict models already close.
+- **`source_type` is enumerated** (`news`, `web`, `official`, `structured`, `social`) although the
+  handoff leaves it as free-text TEXT. Ambiguity rule 4: an unrecognized source type is a
+  normalization bug and should fail loudly rather than land in the ledger as a label.
+- **`ResearchRun.question_id` is a plain `int`**, not an M1-201 `CanonicalQuestion`. The run needs
+  the question's identity, not its content; importing the model would couple the retrieval epic to
+  the normalization epic for no gain. M1-301 has no dependency on the M1-201 branch.
+
+Deferred (do not read the absence as an omission):
+- URL canonicalization policy, duplicate collapsing and stale-flagging are **M1-305**. Adapters
+  landing before it may set `canonical_url` equal to `original_url`.
+- `document_id` minting belongs to the first writer (**M1-602**), consistent with how M1-601
+  deferred `record_id`; the field is optional on the model for that reason.
+- The allowlist loader that consumes `ReliabilityTag` is **M1-308**; it will import the alias from
+  this module rather than restate the values that `config/x_accounts.yaml`'s header pins.
diff --git a/src/whiskeyjack_bot/ledger.py b/src/whiskeyjack_bot/ledger.py
index 2541531..017c1b6 100644
--- a/src/whiskeyjack_bot/ledger.py
+++ b/src/whiskeyjack_bot/ledger.py
@@ -25,7 +25,7 @@ from datetime import datetime, timezone
 from importlib.resources import files
 from pathlib import Path
 
-LEDGER_SCHEMA_VERSION = 1
+LEDGER_SCHEMA_VERSION = 2
 
 _MIGRATIONS_PACKAGE = "whiskeyjack_bot.migrations"
 _MIGRATION_NAME_RE = re.compile(r"^(\d+)_.*\.sql$")
diff --git a/src/whiskeyjack_bot/migrations/002_research_document_fields.sql b/src/whiskeyjack_bot/migrations/002_research_document_fields.sql
new file mode 100644
index 0000000..6cd988a
--- /dev/null
+++ b/src/whiskeyjack_bot/migrations/002_research_document_fields.sql
@@ -0,0 +1,44 @@
+-- M1-301: fields the research-document schema needs and 001 does not have.
+--
+-- 001_initial.sql built research_documents from the handoff's column list,
+-- which predates the brief amendment introducing `provenance` (the amendment
+-- assigns the backfill to M1-301). The other four columns are the same shape of
+-- gap: the schema requires them, the initial migration has no slot for them.
+--
+-- 001 is not edited: ledger.py records each migration's sha256 when applied and
+-- refuses to run against a database whose stored checksum no longer matches.
+--
+-- All columns are added NULLable. SQLite requires a non-null default on an
+-- added NOT NULL column, and defaulting `provenance` to 'direct_api' would
+-- stamp an unearned provenance claim onto any pre-existing row -- a false
+-- attribution record, which the ledger exists to prevent. The Pydantic models
+-- require these fields; database-level enforcement arrives with the write path
+-- and its append-only triggers (M1-602/M1-603).
+--
+-- A CHECK is attached to the new `provenance` column, which ADD COLUMN permits.
+-- No CHECK is added to the pre-existing `source_type` / `reliability_tag`
+-- columns: constraining an existing column requires the full table-rebuild
+-- procedure, which is not worth the risk on a merged migration for vocabularies
+-- whose strict models already reject off-list values.
+
+-- The URL exactly as the provider returned it. M1-305 rewrites canonical_url
+-- for deduplication; without this column the as-retrieved URL is unrecoverable.
+ALTER TABLE research_documents ADD COLUMN original_url TEXT;
+
+-- 'direct_api': the pipeline retrieved the document itself.
+-- 'llm_reported': a research agent reported it; content and timestamps are
+-- claims, and the forecaster prompt caps how load-bearing such a document may be.
+ALTER TABLE research_documents ADD COLUMN provenance TEXT
+    CHECK (provenance IS NULL OR provenance IN ('direct_api', 'llm_reported'));
+
+-- Identity of the second model participating in evidence gathering (M1-307).
+ALTER TABLE research_runs ADD COLUMN agent_model TEXT;
+
+-- Citation hygiene: agent-reported posts dropped for lacking a resolvable
+-- status URL. Counted so a run's dropped-citation rate stays auditable.
+ALTER TABLE research_runs ADD COLUMN posts_dropped_no_url INTEGER;
+
+-- The question a run gathered evidence for. Runs are per question, but 001
+-- carried the linkage only in the reverse direction, via
+-- forecast_records.retrieval_run_id.
+ALTER TABLE research_runs ADD COLUMN question_id INTEGER;
diff --git a/src/whiskeyjack_bot/research/__init__.py b/src/whiskeyjack_bot/research/__init__.py
new file mode 100644
index 0000000..e64d6b5
--- /dev/null
+++ b/src/whiskeyjack_bot/research/__init__.py
@@ -0,0 +1,32 @@
+"""Research retrieval: the normalized evidence schema and its primitives (M1-301).
+
+Adapters (M1-302 AskNews, M1-303 Exa, M1-304 structured router, M1-307 X agent)
+import from here so every provider produces one comparable evidence record.
+"""
+
+from whiskeyjack_bot.research.hashing import content_sha256, normalize_content
+from whiskeyjack_bot.research.model import (
+    Provenance,
+    ReliabilityTag,
+    ResearchDocument,
+    ResearchRun,
+    ResearchSchemaError,
+    RetrievalProvider,
+    SourceType,
+    validate_document,
+    validate_run,
+)
+
+__all__ = [
+    "Provenance",
+    "ReliabilityTag",
+    "ResearchDocument",
+    "ResearchRun",
+    "ResearchSchemaError",
+    "RetrievalProvider",
+    "SourceType",
+    "content_sha256",
+    "normalize_content",
+    "validate_document",
+    "validate_run",
+]
diff --git a/src/whiskeyjack_bot/research/hashing.py b/src/whiskeyjack_bot/research/hashing.py
new file mode 100644
index 0000000..7253255
--- /dev/null
+++ b/src/whiskeyjack_bot/research/hashing.py
@@ -0,0 +1,48 @@
+"""Content hashing for research documents (M1-301).
+
+``content_sha256`` is the single definition of a document's content identity.
+It participates in ``UNIQUE(retrieval_run_id, canonical_url, content_sha256)``
+(M1-601) and, through it, in the research-packet hash that replay reproduces.
+
+**Changing the normalization rule below breaks replay**: previously stored
+documents keep their old digests, so a re-run over the same evidence would
+produce different hashes, defeat the dedup constraint and invalidate the
+attribution claim that a replayed forecast saw the same sources. If the rule
+must ever change, it changes as a new versioned function alongside this one,
+never as an edit to this one.
+
+The primitive lives here rather than in each adapter so that AskNews (M1-302),
+Exa (M1-303), the structured router (M1-304) and the X agent (M1-307) cannot
+drift into per-provider hashing of the same article.
+"""
+
+from __future__ import annotations
+
+import hashlib
+import re
+import unicodedata
+
+# The pinned normalization rule. Each step exists to make the digest stable
+# across cosmetically different renderings of identical content:
+#
+# 1. Unicode NFC -- providers disagree on composed vs decomposed accents, so
+#    "resumé" and "resumé" must not hash differently.
+# 2. Collapse every run of whitespace (including newlines and tabs) to a single
+#    space -- reflowed or re-wrapped article text is the same content.
+# 3. Strip leading/trailing whitespace.
+# 4. Encode UTF-8, then SHA-256.
+#
+# Deliberately NOT normalized: letter case and punctuation. Both can carry
+# meaning in a quoted statement, and an adapter must not be able to collapse two
+# genuinely different claims into one document.
+_WHITESPACE_RUN_RE = re.compile(r"\s+")
+
+
+def normalize_content(text: str) -> str:
+    """Apply the pinned normalization rule; exposed for tests and diagnostics."""
+    return _WHITESPACE_RUN_RE.sub(" ", unicodedata.normalize("NFC", text)).strip()
+
+
+def content_sha256(text: str) -> str:
+    """Return the lowercase hex SHA-256 of ``text`` under the pinned rule."""
+    return hashlib.sha256(normalize_content(text).encode("utf-8")).hexdigest()
diff --git a/src/whiskeyjack_bot/research/model.py b/src/whiskeyjack_bot/research/model.py
new file mode 100644
index 0000000..2da9bdb
--- /dev/null
+++ b/src/whiskeyjack_bot/research/model.py
@@ -0,0 +1,195 @@
+"""Canonical research-run and research-document schema (M1-301).
+
+Every retrieval provider normalizes into these two models: AskNews (M1-302),
+Exa (M1-303), the structured-source router (M1-304) and the xAI X Search agent
+(M1-307). Fixing the shape here is what lets those adapters be swapped, and
+what lets the ledger store one comparable evidence record regardless of where a
+document came from.
+
+The two models mirror the two ledger tables (``research_runs`` and
+``research_documents``, M1-601). Two fields have no column in the initial
+migration and are added by ``002_research_document_fields.sql``:
+
+- ``provenance`` -- introduced by the brief's X-adapter amendment after M1-601
+  shipped, and explicitly assigned to M1-301 to backfill across adapters. It
+  separates a document the pipeline retrieved itself (``direct_api``) from one a
+  research agent *told* us about (``llm_reported``). The forecaster prompt caps
+  how load-bearing the latter may be, so the distinction has to survive storage.
+- ``original_url`` -- the URL exactly as the provider returned it. M1-305 will
+  rewrite ``canonical_url`` for dedup; without this field the as-retrieved URL
+  would be unrecoverable, which is an attribution loss.
+
+Vocabularies are closed ``Literal`` sets. The handoff does not enumerate
+``source_type``, so this module enumerates it (ambiguity rule 4: implement the
+stricter reading) -- an unrecognized source type is a normalization bug and must
+fail loudly rather than land in the ledger as a free-text label.
+
+Models are strict (``extra="forbid"``, reusing ``config._StrictModel``). Use
+:func:`validate_document` / :func:`validate_run` rather than bare
+``model_validate``: pydantic's own error rendering echoes the offending input,
+and a research document can hold arbitrary retrieved text.
+"""
+
+from __future__ import annotations
+
+from datetime import datetime, timezone
+from typing import Annotated, Any, Literal
+
+from pydantic import AfterValidator, AwareDatetime, Field, ValidationError, model_validator
+
+from whiskeyjack_bot.config import _StrictModel
+
+# Where a document came from. ``structured`` is the M1-304 router's official
+# dataset path (FRED and friends); ``official`` is a primary-source web document
+# reached through ordinary retrieval; ``social`` is the X adapter (M1-307).
+SourceType = Literal["news", "web", "official", "structured", "social"]
+
+# How we came to hold the document. ``direct_api`` means the pipeline fetched it;
+# ``llm_reported`` means a research agent reported it and its content and
+# timestamps are claims, not verified facts (brief § B, citation hygiene).
+Provenance = Literal["direct_api", "llm_reported"]
+
+# Source-trust tags. This is the canonical set referenced by the header comment
+# in config/x_accounts.yaml ("must match schema"); M1-308's allowlist loader
+# imports this alias rather than restating the values.
+ReliabilityTag = Literal["official_primary", "verified_org", "journalist", "unverified_social"]
+
+# Retrieval providers, matching the config vocabularies in
+# ``RetrievalProviderConfig`` and ``SocialRetrievalConfig`` plus the structured
+# router, which has no provider credential of its own.
+RetrievalProvider = Literal["asknews", "exa", "structured", "xai_x_search"]
+
+_SHA256_HEX = r"^[0-9a-f]{64}$"
+
+
+def _to_utc(value: datetime) -> datetime:
+    return value.astimezone(timezone.utc)
+
+
+# Timezone-aware only, normalized to UTC. A naive timestamp is not valid
+# provenance (the rule metaculus/snapshots.py already applies to snapshot
+# metadata): "published 09:00" is unusable evidence without an offset, and
+# freshness windows (M1-305) compare these across providers in different zones.
+UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
+
+
+class ResearchSchemaError(Exception):
+    """A research run or document failed validation, with inputs withheld.
+
+    Same hygiene rule as ``ConfigError``/``SnapshotError``: pydantic renders the
+    offending input in its message, and a research document carries arbitrary
+    provider text, so consumers print this exception and never the raw
+    ``ValidationError``.
+    """
+
+    def __init__(self, problems: list[str]):
+        self.problems = problems
+        super().__init__("invalid research record:\n" + "\n".join(f"  - {p}" for p in problems))
+
+
+class ResearchDocument(_StrictModel):
+    """One normalized piece of evidence, from any provider."""
+
+    # Minted by the first writer (M1-602), consistent with how M1-601 deferred
+    # forecast_records.record_id: adapters construct documents before the ledger
+    # transaction that assigns identity.
+    document_id: str | None = None
+    retrieval_run_id: str = Field(min_length=1)
+
+    # As returned by the provider; never rewritten. M1-305 derives canonical_url
+    # from it, and until then adapters may set the two to the same value.
+    original_url: str = Field(min_length=1)
+    canonical_url: str = Field(min_length=1)
+
+    title: str | None = None
+    publisher: str | None = None
+    author: str | None = None
+
+    published_at_utc: UtcDatetime | None = None
+    updated_at_utc: UtcDatetime | None = None
+    # Required: a document with no retrieval time cannot be placed in a run's
+    # timeline or checked against a freshness window.
+    retrieved_at_utc: UtcDatetime
+
+    source_type: SourceType
+    provenance: Provenance
+    # Lowercase hex; see research.hashing.content_sha256 for the pinned input rule.
+    content_sha256: str = Field(pattern=_SHA256_HEX)
+
+    snippet: str | None = None
+    summary: str | None = None
+    raw_artifact_path: str | None = None
+
+    # Absent for providers with no trust model of their own; the X adapter always
+    # assigns one, defaulting to unverified_social.
+    reliability_tag: ReliabilityTag | None = None
+
+
+class ResearchRun(_StrictModel):
+    """One provider invocation for one question, and how it went."""
+
+    retrieval_run_id: str = Field(min_length=1)
+    # Integer reference only. Deliberately not the M1-201 CanonicalQuestion: the
+    # run needs the question's identity, not its content, and importing the model
+    # would couple the retrieval epic to the normalization epic for nothing.
+    question_id: int
+
+    provider: RetrievalProvider
+    provider_config: dict[str, Any] | None = None
+    queries: list[str] = Field(default_factory=list)
+
+    started_at_utc: UtcDatetime
+    completed_at_utc: UtcDatetime | None = None
+    freshness_cutoff_utc: UtcDatetime | None = None
+
+    raw_response_path: str | None = None
+    # Set when the run failed or returned nothing. Social retrieval is additive
+    # evidence: its failure is recorded here and must not fail a research run in
+    # which AskNews or Exa succeeded (brief § B, failure mode).
+    error_summary: str | None = None
+    cost_usd: float | None = Field(default=None, ge=0)
+
+    # A second model participating in evidence gathering must be identified by
+    # name and version; attribution requires it (brief § B).
+    agent_model: str | None = None
+    # Citation-hygiene counter: agent-reported posts dropped for lacking a
+    # resolvable status URL (M1-307). None for providers where it has no meaning.
+    posts_dropped_no_url: int | None = Field(default=None, ge=0)
+
+    @model_validator(mode="after")
+    def _completion_not_before_start(self) -> ResearchRun:
+        if self.completed_at_utc is not None and self.completed_at_utc < self.started_at_utc:
+            # No values in the message: a run's timestamps are row content and
+            # this class contracts not to echo it.
+            raise ValueError("completed_at_utc must not precede started_at_utc")
+        return self
+
+
+def _sanitize(exc: ValidationError) -> ResearchSchemaError:
+    problems = []
+    for err in exc.errors(include_input=False, include_url=False):
+        location = ".".join(str(part) for part in err["loc"]) or "<root>"
+        problems.append(f"{location}: {err['msg']}")
+    return ResearchSchemaError(problems)
+
+
+def validate_document(data: Any) -> ResearchDocument:
+    """Validate a document payload; raises ResearchSchemaError on failure.
+
+    The sanctioned entry point: unlike a bare ``model_validate``, its errors
+    never echo the retrieved content.
+    """
+    try:
+        return ResearchDocument.model_validate(data)
+    except ValidationError as exc:
+        # from None: a chained __cause__ re-exposes the raw ValidationError (which
+        # echoes inputs) whenever this error reaches a traceback renderer.
+        raise _sanitize(exc) from None
+
+
+def validate_run(data: Any) -> ResearchRun:
+    """Validate a run payload; raises ResearchSchemaError on failure."""
+    try:
+        return ResearchRun.model_validate(data)
+    except ValidationError as exc:
+        raise _sanitize(exc) from None
diff --git a/tests/unit/test_research.py b/tests/unit/test_research.py
new file mode 100644
index 0000000..3e72f42
--- /dev/null
+++ b/tests/unit/test_research.py
@@ -0,0 +1,241 @@
+"""M1-301: the research-run/document schema round-trips, closes its vocabularies,
+rejects unusable provenance, hashes content deterministically, withholds inputs
+from validation errors, and is storable by the ledger that migration 002 upgrades."""
+
+import sqlite3
+import traceback
+from datetime import datetime, timedelta, timezone
+from pathlib import Path
+
+import pytest
+
+from whiskeyjack_bot.ledger import LEDGER_SCHEMA_VERSION, connect, initialize_ledger
+from whiskeyjack_bot.research import (
+    ResearchDocument,
+    ResearchRun,
+    ResearchSchemaError,
+    content_sha256,
+    validate_document,
+    validate_run,
+)
+
+TS = "2026-07-17T00:00:00+00:00"
+SHA = "a" * 64
+
+
+def _document(**overrides: object) -> dict[str, object]:
+    data: dict[str, object] = {
+        "retrieval_run_id": "run-1",
+        "original_url": "https://example.org/a?utm_source=x",
+        "canonical_url": "https://example.org/a",
+        "title": "Payrolls rose in June",
+        "publisher": "Example Wire",
+        "author": "A. Reporter",
+        "published_at_utc": TS,
+        "retrieved_at_utc": TS,
+        "source_type": "news",
+        "provenance": "direct_api",
+        "content_sha256": SHA,
+        "snippet": "Nonfarm payrolls rose.",
+    }
+    data.update(overrides)
+    return data
+
+
+def _run(**overrides: object) -> dict[str, object]:
+    data: dict[str, object] = {
+        "retrieval_run_id": "run-1",
+        "question_id": 100,
+        "provider": "asknews",
+        "queries": ["june payrolls"],
+        "started_at_utc": TS,
+    }
+    data.update(overrides)
+    return data
+
+
+def test_document_round_trips() -> None:
+    doc = validate_document(_document())
+    assert validate_document(doc.model_dump()) == doc
+
+
+def test_run_round_trips() -> None:
+    run = validate_run(_run(completed_at_utc=TS, cost_usd=0.02))
+    assert validate_run(run.model_dump()) == run
+
+
+def test_original_url_is_retained_alongside_canonical_url() -> None:
+    # The backlog acceptance criterion: the schema preserves the original URL.
+    doc = validate_document(_document())
+    assert doc.original_url != doc.canonical_url
+    assert doc.original_url.endswith("utm_source=x")
+
+
+def test_unknown_key_is_rejected() -> None:
+    with pytest.raises(ResearchSchemaError):
+        validate_document(_document(reliability="high"))
+
+
+@pytest.mark.parametrize(
+    ("field", "value"),
+    [
+        ("source_type", "blog"),
+        ("provenance", "scraped"),
+        ("reliability_tag", "probably_fine"),
+    ],
+)
+def test_closed_vocabularies_reject_off_list_values(field: str, value: str) -> None:
+    with pytest.raises(ResearchSchemaError):
+        validate_document(_document(**{field: value}))
+
+
+def test_provider_vocabulary_is_closed() -> None:
+    with pytest.raises(ResearchSchemaError):
+        validate_run(_run(provider="tavily"))
+
+
+def test_naive_timestamp_is_rejected() -> None:
+    # A naive timestamp is not valid provenance: freshness windows compare these
+    # across providers in different zones.
+    with pytest.raises(ResearchSchemaError):
+        validate_document(_document(retrieved_at_utc="2026-07-17T00:00:00"))
+
+
+def test_aware_timestamp_is_normalized_to_utc() -> None:
+    doc = validate_document(_document(retrieved_at_utc="2026-07-17T02:00:00+02:00"))
+    assert doc.retrieved_at_utc == datetime(2026, 7, 17, tzinfo=timezone.utc)
+    assert doc.retrieved_at_utc.tzinfo == timezone.utc
+
+
+def test_malformed_content_hash_is_rejected() -> None:
+    for bad in ("not-a-hash", SHA.upper(), "a" * 63):
+        with pytest.raises(ResearchSchemaError):
+            validate_document(_document(content_sha256=bad))
+
+
+def test_completion_may_not_precede_start() -> None:
+    earlier = (datetime.fromisoformat(TS) - timedelta(minutes=1)).isoformat()
+    with pytest.raises(ResearchSchemaError):
+        validate_run(_run(completed_at_utc=earlier))
+
+
+def test_negative_counters_are_rejected() -> None:
+    with pytest.raises(ResearchSchemaError):
+        validate_run(_run(cost_usd=-0.01))
+    with pytest.raises(ResearchSchemaError):
+        validate_run(_run(posts_dropped_no_url=-1))
+
+
+def test_content_hash_is_stable_across_cosmetic_variation() -> None:
+    base = content_sha256("Payrolls rose in June.")
+    assert content_sha256("  Payrolls\n rose\tin   June.  ") == base
+    # NFC: composed vs decomposed accents are the same content.
+    assert content_sha256("resumé") == content_sha256("resumé")
+    # But real content change, including case, must not collapse.
+    assert content_sha256("Payrolls fell in June.") != base
+    assert content_sha256("payrolls rose in june.") != base
+
+
+def test_content_hash_pins_a_known_digest() -> None:
+    # Regression guard on the normalization rule itself: changing it breaks
+    # replay, so it may only change as a new versioned function.
+    assert content_sha256("  hello   world  ") == content_sha256("hello world")
+    assert content_sha256("hello world") == (
+        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
+    )
+
+
+def test_validation_error_never_echoes_retrieved_content() -> None:
+    # A research document carries arbitrary provider text; a credential pasted
+    # into a fixture must not surface through a diagnostic or its traceback.
+    secret = "privateFAKE123456"
+    with pytest.raises(ResearchSchemaError) as excinfo:
+        validate_document(_document(source_type=secret))
+    rendered = "".join(
+        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
+    )
+    assert secret not in str(excinfo.value)
+    assert secret not in rendered
+    assert excinfo.value.__cause__ is None  # a chained ValidationError would re-leak
+
+
+def test_migration_002_makes_the_document_storable(tmp_path: Path) -> None:
+    db = tmp_path / "ledger.sqlite3"
+    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION == 2
+
+    doc = validate_document(_document(document_id="doc-1"))
+    run = validate_run(_run())
+    conn = connect(db)
+    try:
+        columns = {row[1] for row in conn.execute("PRAGMA table_info(research_documents)")}
+        assert {"original_url", "provenance"} <= columns
+        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(research_runs)")}
+        assert {"agent_model", "posts_dropped_no_url", "question_id"} <= run_columns
+
+        conn.execute(
+            "INSERT INTO research_runs (retrieval_run_id, question_id, provider, "
+            "started_at_utc, created_at_utc) VALUES (?, ?, ?, ?, ?)",
+            (run.retrieval_run_id, run.question_id, run.provider, TS, TS),
+        )
+        conn.execute(
+            "INSERT INTO research_documents (document_id, retrieval_run_id, original_url, "
+            "canonical_url, retrieved_at_utc, source_type, provenance, content_sha256) "
+            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
+            (
+                doc.document_id,
+                doc.retrieval_run_id,
+                doc.original_url,
+                doc.canonical_url,
+                doc.retrieved_at_utc.isoformat(),
+                doc.source_type,
+                doc.provenance,
+                doc.content_sha256,
+            ),
+        )
+        stored = conn.execute(
+            "SELECT original_url, provenance FROM research_documents WHERE document_id = 'doc-1'"
+        ).fetchone()
+        assert stored[0] == doc.original_url
+        assert stored[1] == "direct_api"
+    finally:
+        conn.close()
+
+
+def test_database_rejects_off_list_provenance(tmp_path: Path) -> None:
+    # The CHECK is real, not merely a Pydantic-level convention.
+    db = tmp_path / "ledger.sqlite3"
+    initialize_ledger(db)
+    conn = connect(db)
+    try:
+        conn.execute(
+            "INSERT INTO research_runs (retrieval_run_id, provider, started_at_utc, "
+            "created_at_utc) VALUES ('run-1', 'asknews', ?, ?)",
+            (TS, TS),
+        )
+        with pytest.raises(sqlite3.IntegrityError):
+            conn.execute(
+                "INSERT INTO research_documents (document_id, retrieval_run_id, canonical_url, "
+                "retrieved_at_utc, content_sha256, provenance) "
+                "VALUES ('doc-1', 'run-1', 'https://example.org/a', ?, ?, 'scraped')",
+                (TS, SHA),
+            )
+    finally:
+        conn.close()
+
+
+def test_reapplying_migrations_is_a_no_op(tmp_path: Path) -> None:
+    db = tmp_path / "ledger.sqlite3"
+    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION
+    assert initialize_ledger(db) == LEDGER_SCHEMA_VERSION
+    conn = connect(db)
+    try:
+        applied = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
+        assert applied == LEDGER_SCHEMA_VERSION
+    finally:
+        conn.close()
+
+
+def test_models_are_importable_without_the_questions_package() -> None:
+    # M1-301 stays decoupled from M1-201: a run references its question by id.
+    assert ResearchRun.model_fields["question_id"].annotation is int
+    assert "document_id" in ResearchDocument.model_fields
```

---

# Round 2 — disposition of the DO-NOT-APPROVE review

All eight findings are accepted and fixed. Gates after the changes: **157 passed** (was 124;
the M1-301 suite went 20 → 53), `ruff check` clean, `ruff format --check` clean,
`mypy --strict src` clean, `git diff --check master...HEAD` clean.

Two findings are implemented differently from the review's prescription. Both are flagged
below rather than folded in quietly.

## High

**1. Input-controlled field names leak through sanitized errors — fixed, and widened.**
`_sanitize` now takes the model class and admits a location part only if the schema authored
it: an `int` list index, or a name in `model.model_fields`. Anything else renders as
`<withheld>`. Implemented as a general allowlist rather than an `extra_forbidden` special
case, because the review's example was not the only instance of the bug — **`provider_config`
dict keys land in `loc` the same way**, so `{"privateFAKE123456": <bad value>}` leaked identically
through a completely different error type. Both are now covered by their own leak test using
the planted secret as the key, asserting on `str(exc)` and on `traceback.format_exception`.
A rejected unexpected key still reports `<withheld>: Extra inputs are not permitted`, so the
diagnostic survives.

**2. xAI runs do not require model identity or citation-drop accounting — fixed.**
New `_agent_runs_account_for_themselves` validator: `provider == "xai_x_search"` requires a
non-blank `agent_model` (strip-checked) and a non-`None` `posts_dropped_no_url`. Enforced
**even when `error_summary` is set** — `agent_model` is config-supplied under D27 so a failed
run still knows it, and a run that gathered nothing dropped nothing and can say so. This is
what makes `0` distinguishable from "not measured". Mirrored by a trigger, so the guarantee
holds against direct SQL too. Non-agent providers are unaffected and still leave both `None`.

**3. Migration 002 permits new provenance-less ledger records — fixed via the third option.**
Took the trigger route. Note one correction to the finding: it lists `reliability_tag` among
the fields an insert must not leave NULL, but that field is legitimately NULL for every
non-social document (`ReliabilityTag | None`) and the brief requires it only of the X
adapter's output. It is enforced **conditionally** (see 4), never unconditionally. The
finding also missed `research_runs.question_id`, which has the same unconditional-required
shape; it is fixed alongside the others.

`BEFORE INSERT` and `BEFORE UPDATE` triggers on both tables now reject NULL `original_url` /
`provenance` / `source_type` (and `question_id` on runs), close the `source_type`,
`reliability_tag` and `provider` vocabularies that a CHECK could not be retrofitted onto, and
enforce the social contract at the storage layer. Columns stay NULLable so pre-002 rows keep
their honest NULLs — the review agreed a `direct_api` default would be a false attribution.
Intended consequence, documented in the migration: a legacy row cannot be UPDATEd until it is
also backfilled. The ledger is append-only, so nothing should be updating it; M1-602/M1-603
still add the triggers that forbid UPDATE outright.

Migration 002 was edited in place rather than superseded by a 003. It is not on master and
has never been applied to any database — master ships only `001_initial.sql` at
`LEDGER_SCHEMA_VERSION = 1` — so the checksum-pinning rule that forbids editing 001 does not
reach it. No drift is possible.

The review's observation that "the current test helpers already exercise this bypass" was
exactly right, and it is how the fix was confirmed: adding the triggers immediately failed
five pre-existing tests whose `_seed_run` / `_seed_document` helpers wrote 001-era columns
only. Those helpers now supply the required values. A dedicated test also applies 001 alone,
writes a legacy row, then migrates and asserts the row survives with its NULLs intact while
the same insert is refused from that point on.

**4. The social-document trust contract is not enforced — fixed.**
`source_type == "social"` now requires `provenance == "llm_reported"` **and** a non-null
`reliability_tag`, model-side and by trigger. Rationale recorded in the code and the notes:
the brief describes exactly one route to a social document (the agent reports it, and it
always carries a tag defaulting to `unverified_social`), and the forecaster prompt's evidence
caps read precisely those two fields. Per ambiguity rule 4 this takes the stricter reading;
a future direct X API adapter would produce `social`/`direct_api` and require a deliberate
schema change, which is the intent rather than an oversight.

## Medium

**5. `provider_config` is not guaranteed to be JSON-persistable — fixed.** Now
`dict[str, JsonValue] | None`. Tests cover both the rejection of `{"session": object()}` and
a nested config round-tripping through `model_dump_json()`. `queries` was already `list[str]`.

**6. URL fields accept whitespace and arbitrary non-URLs — fixed as suggested.** New
`_require_http_url` `AfterValidator` (mirroring the existing `UtcDatetime` pattern): rejects
leading/trailing whitespace explicitly *before* parsing, since `urlsplit` would silently strip
it, then requires an `http`/`https` scheme and a non-empty host. **Returns the string
unmodified** — canonicalization stays M1-305, and a test pins that a URL with port, query,
tracking parameter and fragment survives byte-for-byte.

## Optional

**7. Models do not fully mirror the tables — fixed as documentation.** The `model.py`
docstring now states that `created_at_utc` is writer-owned metadata assigned by the M1-602
write path (an adapter supplying it could backdate its own audit trail — same reasoning as
`document_id`), and documents the `provider_config` → `provider_config_json` and
`queries` → `queries_json` mappings.

**8. `git diff --check` fails — fixed, but not by editing the lines.** Both flagged lines are
*correct* unified-diff context lines: in diff format a blank source line becomes a line
containing exactly one space. Stripping them would corrupt the diff under review. Suppressed
via `.gitattributes` (`GPT_REVIEW_REQUEST_*.md -whitespace`) instead, following the precedent
already in that file for `docs/backlog/*.csv`. `git diff --check master...HEAD` is now clean.

## Not addressed

The two items the review noted it could not verify — adapter/write-path behavior, and how
multiple raw responses will be bundled behind the singular `raw_response_path` — remain out
of scope for M1-301 and are unimplemented on this branch. The second is a real design
question for M1-307 and is being carried forward rather than resolved here.

---

# Round 3 — disposition

All four findings reproduced exactly as written, and all four are fixed. No pushback this
round: each one is a real hole, and two of them defeated defenses this branch had already
claimed were complete. Gates: **189 passed** (was 157; the M1-301 suite went 53 → 85),
`ruff check` clean, `ruff format --check` clean, `mypy --strict src` clean,
`git diff --check master...HEAD` clean.

## 1. High — error hygiene bypassable through `urlsplit()` messages

Reproduced: `https://privateFAKE123456／example.com/a` produced
`netloc 'privateFAKE123456／example.com' contains invalid characters…`.

The diagnosis is the important part and it was correct: **sanitizing `loc` was never
sufficient, because `msg` is unfilterable.** A `ValueError` from any validator becomes
`err["msg"]` verbatim, and `_sanitize` has no way to tell a value-free message from one
carrying the input. The real invariant lives on the validators — every raise in the module
must use a constant string — and round 2 breached it the moment it introduced a validator
that called into a third-party parser.

`_require_http_url` now wraps all parsing in `try/except ValueError` and re-raises a single
module-level constant `from None`. Every rejection it emits is that one string, so there is
no message channel left to vary with the input.

Two things beyond the literal fix, since the finding exposed a class rather than an instance:

- The invariant is now written down in `_sanitize`'s docstring, naming this incident, so the
  next person adding a validator sees why a "helpful" message naming the bad value is a leak.
- `test_no_field_leaks_a_planted_secret_through_any_message` plants the secret in **every
  field of both models**, as a bare string, a list, a dict key, and inside a URL. The
  previous tests only covered the fields already known to have leaked, which is precisely why
  this one got through. Parametrized URL cases pin the specific shapes from the finding.

## 2. Medium — hostless and control-character URLs

All three inputs reproduced. Three distinct traps, all now closed:

- `netloc` is non-empty for `https://:443/a` (port only) and `https://user@/a` (userinfo
  only). The check is now on `.hostname`, which excludes both.
- `urlsplit` **silently deletes** tab/LF/CR per the WHATWG rule, so `https://exa\nmple.org/a`
  parsed to a clean host while the string being stored still carried the newline — a stored
  URL that no parser would ever agree with. All C0/C1 control characters are now rejected
  outright rather than stripped, consistent with this validator never rewriting.
- `.port` parses lazily, so an unreachable port is only caught by touching it. It is now
  read inside the guarded block and range-checked to 1–65535.

## 3. Medium — `JsonValue` silently nulls non-finite numbers

Reproduced, and the worst of the four: `{"x": nan}` validated, stored as `nan`, and
serialized to `{"x": null}`. Infinity behaves the same. A run would replay against a
configuration that was never the one used, with nothing anywhere recording the substitution —
exactly the drift the ledger exists to make impossible, arriving through the field added to
*fix* JSON persistence.

`provider_config` is now `dict[str, PersistableJson]`, whose `AfterValidator` walks the value
recursively and rejects `NaN`/`±Inf` at any depth. Recursion is not optional here: the
`dict[str, ...]` annotation only constrains the outermost layer, so `{"a": [{"b": nan}]}`
would otherwise still pass. Tested at four nesting depths against all three values, with a
companion test pinning that ordinary floats (including `1e300`) still round-trip.

## 4. Medium — negative dropped-citation count via direct SQL

Reproduced. The finding is also a fair catch on the round-2 write-up: it claimed drop
accounting was "mirrored by a trigger", which was true of the counter's *presence* and not of
its value. The claim was broader than the code.

The round-2 rationale for leaving range checks model-side — "an off-range number is a bad
measurement, not a row that cannot be interpreted" — fails in this specific case, and the
finding is right that it fails. `posts_dropped_no_url` is an accountability counter: a stored
`-1` is not a bad measurement, it is an unfalsifiable claim about how much evidence was
discarded. Taking the suggested route, it now carries a nonnegative CHECK directly (it is a
new column, so `ADD COLUMN` permits one and every legacy row is NULL and passes).

`cost_usd` is enforced alongside it, by trigger since it predates 002. It was not in the
finding, but once the principle is conceded the distinction between the two was never real,
and leaving it would just be the same finding again next round. The migration comment records
the reversal rather than quietly presenting the new position as the original one.

## Standing items

Unchanged and still unverifiable on this branch: adapter/write-path behavior, and how multiple
raw responses will be bundled behind the singular `raw_response_path`. The latter remains a
genuine M1-307 design question being carried forward.

---

# Round 4 — disposition

All three findings reproduced and are fixed. Gates: **205 passed** (was 189; the M1-301 suite
went 85 → 101), `ruff check` clean, `ruff format --check` clean, `mypy --strict src` clean,
`git diff --check master...HEAD` clean.

A pattern worth naming, since it now accounts for three rounds of findings: each of these is a
**hand-rolled enumeration that drifted from the claim written next to it**. `_CONTROL_CHARS`
said C0 *and* C1 and contained only C0. `ge=0` was treated as a finiteness check because it
happened to reject two of the three non-finite values. `INTEGER` was read as a type when SQLite
means it as affinity. In all three the comment was right and the code was narrower. The fixes
therefore delegate to something authoritative — `unicodedata`, `math.isfinite`, `typeof()` —
rather than extending the enumeration.

## 1. URL validation accepts raw whitespace and real C1 controls

All four inputs reproduced. Two independent bugs behind them:

- `_CONTROL_CHARS` was `range(0x20)` plus DEL — C0 only, despite the comment claiming C1 too.
- Whitespace was checked with `value != value.strip()`, which is an *ends* check, so interior
  spaces were never examined.

Both now go through `_is_forbidden_in_url`, which rejects any character where `str.isspace()`
is true or `unicodedata.category()` is `Cc` **or `Cf`**. The strip check is gone — the
character scan subsumes it.

`Cf` is beyond what the finding asked for, and is the ambiguity-rule-4 reading: zero-width
joiners and bidi overrides (U+200B, U+202E) are invisible in every renderer a human would check
a URL in, which makes them a spoofing vector rather than a typo, and no legitimate URL carries
one unencoded. While fixing this I also found NBSP (U+00A0) got through — it is `Zs`, not `Cc`,
so it would have survived a fix that only widened the control range as specified.

IDN hostnames are unaffected (`https://münchen.de/a` still validates); the rejected categories
contain no letters. Test cases are written as `\u` escapes with inline names, since every one of
them is invisible in a source listing.

## 2. SQLite accepts fractional and textual citation-drop counts

Reproduced: `1.5` stored as REAL, `'garbage'` stored as TEXT. The finding's diagnosis is
exactly right, including the reason `>= 0` does not catch the string — SQLite orders TEXT above
every number, so `'garbage' >= 0` is true.

The CHECK is now
`typeof(posts_dropped_no_url) = 'integer' AND posts_dropped_no_url >= 0`.

One deliberate limit: *lossless* affinity conversion is still allowed and now pinned by a test —
`'3'` is stored as integer `3`, and `1` into the REAL-affinity `cost_usd` as `1.0`. Both are
ordinary driver behavior, and a guard that refused them would break normal writes. The
constraint rejects conversions that would *lose* information, not conversion itself.

## 3. Infinite `cost_usd` serializes as null

Reproduced. This is the same defect as round 3's `provider_config` finding, in a field that
predated it — and the round-3 fix did not generalize because `ge=0` made `cost_usd` *look*
covered: `-inf < 0` and every `NaN` comparison is false, so two of the three non-finite values
were already rejected by accident. Only `+inf` got through, which is precisely the one that
serializes to `null` rather than erroring.

`cost_usd` is now `FiniteFloat` (an explicit `math.isfinite` check) rather than relying on the
bound. On the SQL side the trigger tests `typeof(NEW.cost_usd) IN ('integer','real')` and
`NEW.cost_usd > 1e308`, which rejects both `'free'` and infinity — SQLite stores `9e999` as REAL
infinity, so a `< 0` test could never have caught it.

## Unrelated fix carried in the same commit

The planted leak-test secret is renamed from `sk-live-planted-9d2f1a` to `privateFAKE123456`.
CI's gitleaks step scans **all** branches' object databases, so this branch's realistic-looking
fixture was failing the quality gate on unrelated PRs. A leak test needs a value that is
distinctive in output, not one that looks like a credential; the scanner's `generic-api-key`
rule fires on the prefix and entropy. Not allowlisted — the secret-hygiene gate is
non-negotiable, and an allowlist broad enough to pass fixtures is broad enough to pass a real
leak.

## Standing items

Adapter/write-path behavior and `raw_response_path` bundling remain unimplemented and
unverifiable here, as in previous rounds.

---

# Round 5 — disposition

Both findings reproduced and are fixed. Gates: **219 passed** (was 205; the M1-301 suite went
101 → 114), `ruff check` clean, `ruff format --check` clean, `mypy --strict src` clean,
`git diff --check master...HEAD` clean.

Both findings are **self-inflicted**: each one is a defect in a fix from the previous round,
not in the original schema. Worth stating plainly, because the shape is now unmistakable —
round 4's two additions were a blanket category ban and a magnitude ceiling, and both were
approximations standing in for a rule I did not look up. That is the same "hand-rolled
enumeration drifts from its claim" pattern named in round 4's own disposition, committed in
the act of fixing it.

## 1. Blanket `Cf` ban rejects standards-valid IDN hostnames

Both examples reproduced: `نامه‌ای.ir` and `क्‍ष.com` were rejected, and both encode
cleanly to punycode. This directly falsifies round 4's written claim that "IDN hostnames are
unaffected (`https://münchen.de/a` still validates)" — that check confirmed the case that
happened to be safe and generalized from it. U+200C/U+200D are *required* between certain
scripts' letters; a category-wide ban cannot express "valid here, invalid there".

The underlying theory was still right — U+200B and the bidi overrides are invisible wherever a
human would check a URL — so the fix keeps the guard and moves it to where the standard has an
opinion:

- `_is_forbidden_in_url` now rejects whitespace and `Cc` only, everywhere in the URL.
- The hostname additionally goes through `idna.encode()` (IDNA 2008, including CONTEXTJ), which
  accepts ZWNJ/ZWJ exactly where the contextual rules do and still refuses U+200B, U+200E and
  U+202E, which are valid in no context. Its errors are caught and replaced with the same
  constant message `from None`, since `idna` embeds the offending label in its exceptions.

This adds one direct dependency, `idna>=3.4,<4`. It is **not a new install** — it was already
in `uv.lock` transitively via httpx — but the schema now imports it, and an undeclared
transitive import works right up until the intermediate drops it, then fails as a missing
module at validation time rather than at install time. `test_dependency_pins.py` asserts the
declaration so it cannot regress to transitive. Flagging the dependency addition explicitly
rather than letting it pass as an implementation detail on a schema ticket.

## 2. Model and database disagree about valid finite costs

Reproduced: `cost_usd=1.1e308` validates and then fails to persist. Round 4 introduced
`> 1e308` in the trigger as a stand-in for "infinite" and described it in the migration comment
as "no real run costs 1e308 dollars" — a plausibility argument where a correctness one was
needed. The finding is right that this recreates the exact model/table fidelity gap this
schema exists to close, in the same commit that was closing another instance of it.

Taking the suggested route: the trigger now tests `NEW.cost_usd = 9e999`. SQLite overflows that
literal to REAL infinity when parsing, making it an infinity **sentinel** rather than a
magnitude bound, so the SQL and `math.isfinite` now share one definition of finite. Negative
infinity needs no case of its own — `< 0` already covers it. A parametrized test round-trips
`0`, `0.02`, `1.1e308` and `1.7976931348623157e308` (the largest float) through both the model
and the database and asserts they agree.

## Standing items

Adapter/write-path behavior and `raw_response_path` bundling remain unimplemented and
unverifiable, unchanged across all five rounds.

---

# Round 6 — disposition

Reproduced and fixed. Gates: **227 passed** (was 219; the M1-301 suite went 114 → 122),
`ruff check` clean, `ruff format --check` clean, `mypy --strict src` clean,
`git diff --check master...HEAD` clean.

## Medium — IDNA validation rejects valid IPv6-literal URLs

Both cases reproduced. `urlsplit` strips the brackets from an IPv6 authority, so
`https://[::1]/a` reaches the check as the bare string `"::1"`, and `idna.encode` refuses it as
an invalid codepoint.

The observation that **IPv4 passed only incidentally** is the more important half of the
finding, and it is correct: `192.168.1.1` satisfies `idna.encode` because dotted digits are
acceptable IDNA labels. Nothing was validating it as an address. So the class of bug was
"hostnames are not all one kind of thing", and the IPv4 case was hiding it.

Taking the suggested route: `_require_resolvable_hostname` now tries
`ipaddress.ip_address(hostname)` first and falls through to `idna.encode` only when that fails,
so each kind of host is judged against its own standard. Malformed literals are still rejected
— `urlsplit` refuses an unclosed bracket and a bracketed non-address on its own, and an IP
literal does not exempt the port check (`https://[::1]:99999/a` is still refused). All of that
is now pinned by tests, alongside a re-check that the ZWSP/bidi and contextual-ZWNJ cases from
round 5 still behave.

One thing deliberately **not** added: any judgement about loopback or private ranges. This
module validates shape and contains no network code, so "is this host appropriate to fetch" is
not its question. Flagging that explicitly because inventing a guard nobody asked for is what
produced the last two rounds of findings.

## On the pattern

Rounds 4, 5 and 6 have each found a defect introduced by the previous round's fix — Cf ban →
broke IDN → IDNA check → broke IP literals. Each fix was correct about the case in front of it
and wrong about the space around it. The common failure is verifying a fix against the example
that prompted it rather than against the range of inputs it now governs, and I have stopped
treating "the reported case now behaves" as evidence the change is safe.

## Standing items

Adapter/write-path behavior and `raw_response_path` bundling remain unimplemented and
unverifiable, unchanged across all six rounds.
