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
- `tests/unit/test_research.py` — 122 tests. Suite: 227 passed; ruff check + format +
  `mypy --strict src` clean.
- `pyproject.toml` — **one new direct dependency, `idna>=3.4,<4`**, for IDNA hostname
  validation in `model.py`. Not a new install: it was already in the lock transitively via
  httpx. Declared because the schema imports it directly, and an undeclared transitive import
  keeps working right up until the intermediate drops it. `tests/unit/test_dependency_pins.py`
  asserts the declaration so it cannot silently regress to transitive.

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
  carries a CHECK (used for `provenance` and `posts_dropped_no_url`), but constraining the
  pre-existing `source_type` / `reliability_tag` columns would require the 12-step table rebuild.
  The triggers close those vocabularies without it.
- **Numeric range checks are enforced in the database too.** Round 2 of this file argued they could
  stay model-side because an off-range number is "a bad measurement, not an uninterpretable row".
  Review round 3 showed that reasoning fails in the case that matters: `posts_dropped_no_url` is an
  accountability counter, so a stored `-1` is not a bad measurement but an unfalsifiable claim
  about how much evidence was discarded. Once it is enforced, `cost_usd` is enforced with it — the
  distinction was never principled. `posts_dropped_no_url` is new and takes a CHECK directly;
  `cost_usd` predates 002 and is guarded by the triggers.
- **The SQL guards check `typeof()`, not just the value.** A column type in SQLite is *affinity*,
  not a constraint: a REAL that cannot be narrowed losslessly stays REAL, and a non-numeric string
  stays TEXT — and TEXT sorts above every number, so `'garbage' >= 0` is true. Round 4: without
  `typeof()`, `posts_dropped_no_url` accepted `1.5` and `'garbage'`, and `cost_usd` accepted
  `'free'` and `+inf`. Lossless affinity conversion is still allowed (`'3'` → `3`, `1` → `1.0`) and
  pinned by a test, so the guards cannot be tightened into refusing ordinary driver round-trips.
- **`cost_usd` must be finite, explicitly.** `ge=0` rejected `-inf` and `NaN` as a side effect
  (both comparisons are false), which made `+inf` look covered when it was not: it validated and
  then serialized to `null`, so an unbounded cost persisted as *no recorded cost*. Same failure
  shape as the `provider_config` one, in a field that predated it. The SQL side tests SQLite's
  infinity **sentinel** (`= 9e999`, which overflows to REAL infinity when parsed), not a
  magnitude ceiling: round 4's `> 1e308` rejected finite costs the model had just accepted, so
  validation passed and persistence failed — reopening the model/table fidelity gap in the act
  of closing another one (round 5). Model and database now share one definition of "finite",
  pinned by a test that round-trips `1.7976931348623157e308` through both.
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
- **URLs must be absolute http(s) with a real hostname**, no whitespace anywhere and no Unicode
  `Cc`/`Cf` characters, checked without rewriting the string. This is not canonicalization (still M1-305) —
  the stored URL stays byte-for-byte what the provider returned, tracking parameters and all. It
  rejects only input that is not a URL. Three traps found in review round 3: `netloc` is non-empty
  for `https://:443/a` and `https://user@/a` (so the check is on `.hostname`, not `.netloc`);
  `urlsplit` *silently deletes* tab/LF/CR, so a control character would survive into the stored
  string while every parser saw a clean host; and `.port` parses lazily, so an unreachable port
  like `:99999` is only caught by touching it. Round 4 then found the character check itself was
  hand-rolled and wrong: it enumerated C0 plus DEL while its comment claimed C0 *and* C1, so U+0085
  and U+009F passed, and raw interior spaces passed because only the ends were checked. It now asks
  `unicodedata` (`str.isspace()` or category `Cc`) rather than enumerating. Round 4 also added a
  blanket `Cf` ban on the theory that zero-width and bidi-override characters are invisible
  wherever a human would inspect a URL — and the claim made here that "IDN hostnames are
  unaffected" was **wrong**: U+200C/U+200D are *required* between certain scripts' letters, so
  the ban rejected standards-valid hostnames like `نامه‌ای.ir` and `क्‍ष.com` (round 5). The
  theory was right and the blanket rule was not. `Cf` is now judged where it actually matters —
  the hostname — by `idna.encode()`, which applies IDNA 2008 including CONTEXTJ: it accepts
  ZWNJ/ZWJ exactly where the standard does and still refuses U+200B and the bidi overrides,
  which are valid in no context. That keeps the spoofing guard without the collateral, and
  follows the same delegate-to-the-authority rule as the character checks.
  Round 6 then found that `urlsplit().hostname` returns **IP literals** as well as domain
  names, and IDNA refuses them: `https://[::1]/a` arrives bracket-stripped as `"::1"` and was
  rejected. IPv4 had been passing only because dotted digits happen to be acceptable IDNA
  labels — luck, not a check. The host is now tried as `ipaddress.ip_address()` first and only
  sent to `idna` if that fails, so each kind of host answers to its own standard. Whether a
  reachable host is an *appropriate* one (loopback, private ranges) is deliberately not decided
  here: this module validates shape and holds no network code.
- **`provider_config` is `dict[str, PersistableJson]`, not `dict[str, Any]` or bare `JsonValue`.**
  The column is `provider_config_json TEXT`; a value that cannot round-trip through JSON is not
  storable, and must fail at validation rather than inside the ledger write, after the run has
  already happened. `JsonValue` alone was insufficient: it admits `NaN`/`±Inf`, which
  `model_dump_json()` renders as `null`, so a run validated with `{"threshold": nan}` would replay
  against `{"threshold": null}` — silent config drift, caught in review round 3. `PersistableJson`
  rejects non-finite floats recursively, since the `dict[str, ...]` annotation only constrains the
  outermost layer.
- **`_sanitize` withholds error-location parts it did not author.** `include_input=False` withholds
  the offending *value*, but under `extra="forbid"` the location **is** the caller's key (likewise
  for `provider_config` dict keys), so a credential pasted as a key leaked where one pasted as a
  value did not. Only `int` indices and declared field names now survive into a message.
- **Sanitizing the location is not sufficient: the *message* is unfilterable.** A `ValueError` from
  any validator becomes `err["msg"]` verbatim, so the companion invariant lives on the validators —
  every raise in `model.py` uses a constant, value-free message. Review round 3 found the one place
  that breached it: `_require_http_url` let `urlsplit`'s own `ValueError` propagate, and that
  exception embeds the offending netloc, leaking a URL through an otherwise airtight sanitizer.
  All parse errors are now caught and replaced with a constant string `from None`. The invariant is
  documented in `_sanitize`'s docstring and netted by
  `test_no_field_leaks_a_planted_secret_through_any_message`, which plants a secret in every field
  of both models rather than in the handful known to have leaked.

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
