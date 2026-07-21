# Cross-model review request — whiskeyjack-bot M1-301

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
+    secret = "sk-live-planted-9d2f1a"
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
