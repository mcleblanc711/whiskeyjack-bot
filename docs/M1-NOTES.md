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

## M1-201 — Canonical question model

Questions have so far flowed through the pipeline as the pinned SDK's own Pydantic models
(`forecasting_tools.data_models.questions`), which track the package and can shift under us.
M1-201 introduces the **stable internal schema** the rest of M1 depends on instead, so an SDK bump
cannot ripple through retrieval, forecast generation, validation and the ledger writers. It gates
M1-202/M1-203 and Codex's T-901.

Delivered:
- `src/whiskeyjack_bot/questions/model.py` — strict Pydantic models (reusing `config._StrictModel`,
  `extra="forbid"`) as a `qtype`-discriminated union: `CanonicalBinaryQuestion`,
  `CanonicalMultipleChoiceQuestion`, `CanonicalNumericQuestion`, plus the `CanonicalQuestion` union
  alias and a `CanonicalQuestionAdapter` for validating raw dicts. Common fields carry
  `resolution_criteria` + `fine_print` (the M1-201 retention target) and the group-parent identity
  (`group_question_option`, `question_ids_of_group`) that M1-202 needs.
- `src/whiskeyjack_bot/questions/normalize.py` — `normalize_question()` / `normalize_questions()`,
  the single place SDK field names are read. `NormalizationError` follows the
  `ConfigError`/`SnapshotError`/`LedgerError` hygiene rule (inputs stripped via
  `errors(include_input=False)`; `from None`).
- `tests/unit/test_questions.py` — 34 tests: per-type mapping, fine-print retention against the raw
  fixtures, MC options, numeric bounds/cdf, group-identity carry-through, union round-trip,
  malformed-record table, and no-leak planted-secret paths. Suite: 138 passed; ruff check +
  format + `mypy --strict src` clean. (GPT review round 1 raised this to 69 module tests /
  173 suite; round 2 took it to 178 — see the round sections below.)

Hardening — a question object missing the fields its declared type requires is reported as a
`NormalizationError`, not a raw `AttributeError`/`TypeError`. This is the same defect class as the
M0-103 review finding against `SnapshotError` (callers only handle the module's own error type),
so it is pinned by a test here rather than left for review to rediscover.

Decision — **dispatch keys on the SDK's `question_type` literal, not `isinstance`.** The SDK's
`DiscreteQuestion` *subclasses* `NumericQuestion`, so an `isinstance(q, NumericQuestion)` test would
silently normalize the unsupported `discrete` type as numeric. `_SUPPORTED_TYPES` is derived from
`config.SupportedQuestionType` via `get_args`, so D20's type list stays single-sourced. A regression
test pins this (`test_discrete_question_is_rejected_despite_subclassing_numeric`).

Deviation — placed in a **`questions/` subpackage** mirroring `metaculus/`, rather than the flat
`schemas.py` + `normalize.py` in the handoff's proposed tree, since M1-202/M1-203 add type-specific
logic to the same area.

Deferred (do not read the absence as an omission):
- Unsupported types raise `UnsupportedQuestionTypeError` (before any field is read, so zero
  model/submission calls). Turning that refusal into a **logged diagnostic event** is **M1-203**.
- Group **unpacking** is **M1-202**; M1-201 only carries the parent linkage through unchanged.
- The comprehensive valid/invalid **golden fixture set is Codex's T-901**, authored blind; this
  slice ships only the tests that prove its own model and mapping.
- `cdf_size` is stored as a plain int. Enforcing the 201-point count (`config.expected_cdf_points`)
  is calibration-time validation, i.e. **M1-503**.

### M1-201 — GPT review round 1

All four mechanical findings reproduced against the pinned SDK and were fixed:

- **Tag handling** (`normalize.py`). Two defects in one expression. An unhashable
  `question_type` (a list) raised a raw `TypeError` out of the `in _SUPPORTED_TYPES` test,
  escaping the module's error boundary entirely — `isinstance` is now tested first. And an
  arbitrary string tag was echoed verbatim, so the tag is now named only when it is a member
  of `_KNOWN_SDK_TYPES` (derived from the SDK's own `QuestionBasicType`); anything else
  renders as `'unknown'`. The docstring claim that the tag "is a fixed SDK enum value" is now
  enforced rather than assumed.
- **Finite floats** (`model.py`). Pydantic accepts NaN/±inf for a bare `float`; NaN also slips
  past `_bounds_ordered` because both ordering comparisons are false. `model_dump_json` then
  writes `null` and the union adapter cannot read the record back — so the round-trip the
  module advertises was conditionally false. All canonical floats now use
  `_Finite = Annotated[float, Field(allow_inf_nan=False)]`.
- **Option-set integrity** (`model.py`). `["A","A",""]` and `["A"]` both validated under the
  old `min_length=1`. M1-404 must emit "every exact option once with probabilities summing to
  one", which is unrepresentable when duplicate labels collapse as mapping keys — so the
  constraint belongs at the input contract, not downstream. Now `min_length=2` plus a
  validator rejecting blank and duplicate labels (without echoing them).
- **Catch boundary** (`normalize.py`). One `try` spanned both SDK field reads and canonical
  model construction, so a future internal `TypeError` in construction would have been
  reported as a malformed input record. Field reads are now fenced separately; construction
  errors stay visible.

Decision — **`source_categories` carries the SDK's `categories` slugs through uninterpreted.**
The review asked for a source-backed *domain* field. No SDK question class has one: the only
domain-shaped field is `categories: list[Category]`, and this project's domain taxonomy lives
in `config/x_accounts.yaml` (`econ_data`, `space_launch`, …) with no mechanical mapping from
Metaculus categories. No backlog item assigns a domain to a *question*; the only spec text is
one bullet in the downstream **forecast record** list (`CODEX_HANDOFF.md`), owned by M1-602.
The recoverable half of the concern is real, though — `normalize.py` is the single place SDK
fields are read, so a field dropped there cannot be recovered downstream without a re-fetch.
Hence the passthrough, named `source_*` so it is not mistaken for the project's domain tag.
Deriving an actual domain tag remains **M1-307 / M1-602**.

All three repo fixtures carry an empty category list, so the passthrough is pinned by
synthetic-object tests rather than fixture assertions — a fixture-driven check would have
passed against a hardcoded `[]`. The same vacuity affected the group-linkage test the review
flagged (every fixture has a null group parent); it now uses non-null synthetic linkage.

### M1-201 — GPT review round 2

Round 2 confirmed all five round-1 fixes CLOSED and **withdrew the domain finding** on the
grounds above. One regression introduced by the round-1 category fix, plus two minor items:

- **`source_categories` shape** (Medium, the round-1 fix's own regression). Flattening each
  category to `slug or name` mixed two namespaces and destroyed identity:
  `Category(id=17, name="Economics", slug="economy")` and
  `Category(id=18, name="economy", slug=None)` both rendered as `"economy"`, so downstream
  classification could apply the first one's mapping to the second. Now carried as an owned
  `SourceCategory` model (`id`, `name`, `slug`) — ours rather than the SDK's `Category`, for
  the same reason the question models exist. `id` is the only stable identifier; a slug can be
  renamed and is optional, a name is not. `emoji`/`description` stay out: presentational and
  free text respectively, and `description` would widen the no-echo surface for no gain.

  Note the mapping hands the canonical model **plain dicts**, not constructed `SourceCategory`
  objects. `_common_fields` runs inside the field-read fence, which catches only
  `AttributeError`/`TypeError`, so constructing a model there would let a `ValidationError`
  escape `normalize_question` entirely — the exact boundary discipline round 1 established.
- **Constant-fixture assertions** (Low). Every fixture shares one tournament slug, weight and
  open time, so `test_identity_and_common_fields_preserved` would pass against hardcoded
  constants. Added a synthetic-value test covering the common fields with distinct values.
- **`git diff --check` claim was false** (Nit) at `eac283e`: the round-2 review request embeds a
  diff whose context lines carry trailing whitespace, so the gate the file itself claimed to
  pass did not. Whitespace stripped; the claim now holds.

Test-helper hardening (found while fixing the Low, not review-reported): `fake_sdk_question`
accepted any override key, so an override naming a *canonical* field instead of the SDK
attribute — `url` for `page_url` — silently set an attribute nothing reads and the test passed
against the default it meant to replace. It now asserts overrides are actually read. This is the
third instance of the same vacuity class in this slice, so it is closed at the helper.
