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

## M1-202 — Group-question unpacking

Acceptance: *unpacked fixtures produce one unique internal question per subquestion and no
duplicate IDs.*

### What the criterion is actually guarding against

Group questions arrive as a single *post* carrying a `group_of_questions` block.
`group_of_questions` is a post-level tag, never a `question_type`, so there is no
`GroupQuestion` model in the pinned SDK and expansion is purely a fetch-time concern.

Expansion — ours and the SDK's alike — deep-copies the **parent post** once per subquestion
and swaps in that subquestion's block. Verified against `forecasting-tools==0.2.92`
(`helpers/metaculus_client.py:682-700`), every sibling therefore shares:

| field | across siblings |
|---|---|
| `question_id` | **unique** |
| `post_id`, `url` | identical (the URL is built from the post id) |
| `fine_print`, `background_info`, `resolution_criteria` | identical — the parent's, overwriting each subquestion's own |
| `question_ids_of_group` | identical (full sibling list) |
| `group_question_option` | unique (the subquestion's label) |

So **any identity keyed on `post_id` or `url` collapses an entire group to one record.**
That is the failure the acceptance criterion is written against, and
`test_siblings_share_the_post_but_not_the_question_id` pins it explicitly so a future
refactor cannot reintroduce it quietly.

Decision: **`question_id` is the internal identity anchor.** It agrees with the ledger's
existing `UNIQUE (question_id, tournament_id, forecast_version)`
(`migrations/001_initial.sql:40`), so no migration and no schema change were needed —
canonical questions are not persisted yet, and there is no questions table.

### Delivered

- `src/whiskeyjack_bot/questions/groups.py` — `unpack_group_post()` and `is_group_post()`.
  Our own expansion, mirroring the SDK's semantics.
- `src/whiskeyjack_bot/questions/model.py` — one new canonical field, `group_parent_title`.
- `src/whiskeyjack_bot/questions/normalize.py` — `_group_parent_title()` recovery, and
  batch-level duplicate-`question_id` enforcement in `normalize_questions()`.
- `tests/fixtures/api_posts/group/minibench_group.json` — the repo's first group fixture.
- `tests/unit/test_groups.py` — 22 tests. Suite 301 → 323.

### Decision — we own the expansion rather than calling the SDK's

The SDK's expansion is reachable only as `MetaculusClient._unpack_group_question`, a private
static method on a network-bound client class. Binding to a `_`-prefixed API is not a
contract, and it is what makes a raw group post exercisable as an offline fixture.

The cost is ~20 duplicated lines that can drift on an SDK bump. Mitigated by
`test_our_unpacking_matches_the_pinned_sdk`, which compares our output to the SDK's
field-by-field on the same fixture. It is a **drift alarm, not a guarantee** — it proves the
two agree on this fixture, not on all inputs.

Excluded from that comparison: `date_accessed` (set at construction, so two expansions
legitimately differ) and `api_json` (the raw post echoed back verbatim by both — comparing it
adds nothing and would dominate the diff on failure).

### Decision — `group_parent_title`, and why only the title

Expansion sets each subquestion's `question_text` from the **subquestion** block
(`questions.py:171`) and discards the parent post's title entirely. Metaculus titles some
subquestions with only their option label, so a subquestion can reach the forecaster as the
bare string `"September 2026"` — which states no question at all.

The parent title survives on `api_json`, the raw post payload the SDK retains, and is lifted
back out into a canonical `group_parent_title`.

**Only the title is lifted; `api_json` itself is never carried onto the canonical model.**
The payload contains the community-prediction `aggregations`, and the canonical question is
the forecaster's input boundary — *community prediction is never a forecaster input in v1*
is a hard constraint, and carrying the payload would breach it by accident.

`group_parent_title` degrades to `None` rather than raising when the payload is absent (a
question rebuilt from a snapshot has no obligation to carry one) or blank. The fixture
deliberately contains a label-only subquestion title so
`test_group_parent_title_is_load_bearing_not_decorative` fails if the field ever stops doing
real work — the vacuity failure mode M1-201 hit three times.

### Decision — duplicate IDs are refused at the boundary

`normalize_questions()` now enforces `question_id` uniqueness across the batch. A duplicate
would otherwise collide on the ledger's unique constraint — but only *after* a forecast had
been generated and paid for. The error reports the **count only**, not the colliding ids:
an id is low-risk content, but the no-echo rule is unconditional and the softer reading of it
has been a review finding before.

### Deviation from the SDK — tolerant parent overrides

The SDK indexes `group_json["fine_print"]`, `["description"]` and `["resolution_criteria"]`
directly, raising `KeyError` when a group block omits one. `unpack_group_post` overrides only
the keys actually **present**.

The tolerance is scoped to **absent** keys only. A key the parent carries explicitly as
`null` still overwrites the subquestion's own value with `None` — matching the SDK, and
intended: an explicit null is the parent stating the field is empty for the whole group,
which is not the same as the parent not addressing it at all. Pinned by
`test_explicit_parent_null_overrides_subquestion_value`.

(An earlier draft of this note claimed the deviation "never erases a subquestion's own value
by replacing it with `None`". That was too strong and was corrected after review.)

On a well-formed post the two implementations are identical, which is what the drift test
compares.

### Deferred (do not read the absence as an omission)

- **Type policy stays in `normalize`, not `groups`.** A well-formed `date` subquestion
  expands without complaint and is refused downstream by `normalize_question` (D21), so the
  reason reported is the real one rather than "malformed group". Pinned by
  `test_deferred_subquestion_types_are_refused_by_normalize_not_unpack`.
- **A deferred subquestion still aborts the whole batch**, because `normalize_questions`
  propagates the first failure. Turning that into a per-question skip with a logged
  diagnostic event is **M1-203**, which owns the diagnostic path.
- **Nothing calls `unpack_group_post` in the pipeline yet.** On the live path the SDK
  already expands groups inside `get_all_open_questions_from_tournament`
  (`metaculus/fetch.py:85`, `group_question_mode` default `"unpack_subquestions"`), so
  `normalize_question` receives subquestions already separated. Our seam exists for offline
  fixtures and for any future path that reads raw post JSON.
- The comprehensive valid/invalid **golden fixture set is Codex's T-901**; this slice ships
  only the group fixture its own acceptance needs.

### Standing risk — not verifiable offline

Whether real MiniBench group subquestion titles are already self-describing cannot be checked
without live data. `group_parent_title` is the **stricter reading** (brief rule 4): it costs
one nullable field and removes a class of unforecastable prompt input. If live data shows
subquestion titles are always full questions, the field becomes redundant but not wrong.

Same class as M1-201's standing risk about one-option multiple-choice questions.

### Fixture note

`tests/fixtures/api_posts/group/minibench_group.json` sits in a **subdirectory**. The existing
loader `load_fixture_questions()` (`tests/unit/test_questions.py`) globs `api_posts/*_post.json`
non-recursively and feeds each straight to `DataOrganizer.get_question_from_post_json`, which
asserts `"question" in post_json` and knows nothing about groups. A group post placed under
that glob would break **every** existing question test.

Per M1-201's review history, the fixture varies its subquestion ids, timestamps and question
weights rather than reusing shared constants — shared-constant vacuity was a repeat finding
there, and this fixture's entire purpose is that siblings differ.

## M1-203 — Rejecting unsupported types safely

Acceptance: *unsupported types create a diagnostic event and make zero model/submission
calls.*

### What the criterion is actually guarding against

Half of it already held. `normalize_question` refused an unsupported tag before reading any
field, so a `date` question could never reach a model or a submission call — that is D21's
real safety property, and it was pinned from M1-201.

The live defect was the other half. `normalize_questions` propagated the first failure, so a
single deferred question **discarded the normalization of every supported question fetched
alongside it**. On a tournament pull containing one date question, the batch returned
nothing. And "diagnostic event" named a mechanism that existed nowhere in `src/` — neither
`CODEX_HANDOFF.md` nor `CLAUDE_CODE_PROMPT.md` defines it.

### Delivered

- `questions/events.py` — `DeferralEvent` and `NormalizationResult`, frozen dataclasses.
- Refusal is now **two-tier**. `normalize_question` (singular) still raises
  `UnsupportedQuestionTypeError`, message byte-identical. `normalize_questions` (batch)
  skips, records a `DeferralEvent` and logs at WARNING.
- `normalize_questions` returns `NormalizationResult`, not `list[CanonicalQuestion]`.
- Four defensive helpers (`_safe_attr`, `_type_tag`, `_supported_type`, `_safe_int`).
- 332 tests (up from 323).

### Decision — the event is an in-process value, not a ledger row

The obvious reading of "diagnostic event" is a ledger row. It was rejected for now, on three
grounds: M1-602 owns ledger writers and is Not Started; there is **no run or tournament
context at this layer** to key a row on (nothing in `src/` even calls `normalize_questions`
yet); and every `*_events` table in `001_initial.sql` is FK-bound to `forecast_records`,
which by definition does not exist for a question refused before forecasting.

A migration `003` would also have collided with two live parallel branches — CLAUDE.md's
"migration numbers are claimed globally" gotcha.

`DeferralEvent` is shaped so M1-602 can persist it later without rework, and lives in its own
module importing only `model.py`, so a ledger writer can import it without dragging in the SDK.

### Decision — the event carries `question_id`/`post_id`; the int gate is why that is safe

This is the one deliberate reading of the no-echo rule as **scoped**, and it should be the
first thing a reviewer pressure-tests.

CLAUDE.md's rule is written about error messages: *"an error message never echoes
stored/file/field values."* A `DeferralEvent` is not an error message; it is the diagnostic
artifact the criterion demands. An event that says "3 questions were deferred" without saying
*which* satisfies the criterion's words and fails its purpose — an operator cannot act on it.

Carrying identity is safe **by construction, not by promise**. `_safe_int` returns `None` for
anything that is not an `int` (and rejects `bool`, an `int` subclass). So the event contains
**zero unvetted strings**: `reason` is a module literal, `question_type` is
`_KNOWN_SDK_TYPES`-gated or `'unknown'`, and both ids are `int | None`. Hand it an object whose
`id_of_question` is a leaked credential and the event carries `None` —
`test_deferral_withholds_non_integer_identity` pins exactly that.

**The duplicate-id error is unchanged and still withholds ids.** That is an error message
interpolating into free prose, the softer reading there was already a review finding, and this
does not reopen it.

### Decision — `logging_setup.py` was not touched

Values are interpolated into the log **message** with lazy `%` args, because
`record.getMessage()` is already redacted twice (filter + formatter).

An `extra`-field passthrough would have been *worse*, not just bigger: the redaction
comprehension in `JsonFormatter.format` is `isinstance(value, str)` over **top-level values
only**, so a dict or list arriving via `extra` sails past it untouched — a new leak class in
the one module that must not have one.

Cost, stated plainly: the record is JSON with a string message, so a machine consumer needs a
regex until M1-602 gives deferrals a real row. Accepted — the machine-readable form today is
the returned `DeferralEvent`.

`test_deferral_log_record_is_not_a_leak_vector` renders a real record through the real
`JsonFormatter` rather than asserting on `caplog.text`, since the formatter is what production
writes.

### Decision — only a deferred *type* is skipped

The stricter reading (CLAUDE.md rule 4). A malformed *supported*-type question and a duplicate
`question_id` both still raise and abort the batch. D21 defers date and conditional questions;
it does not make malformed records survivable, and reporting a real defect as "deferred" would
hide it behind a diagnostic that says the opposite.

Uniqueness is checked over **accepted** questions only: a deferred question has no canonical
model and never reaches the ledger, so it is not part of the contract that check protects.

### Note — the tripwire test derives from `BaseException` deliberately

`tripwire_question` arms every content attribute to raise on access, making explicit a
guarantee the older `_OnlyTag` tests held only by accident (an object exposing just a tag
proves "nothing crashed", not "nothing was read").

It raises `_ContentFieldRead(BaseException)`, **not** `AssertionError`. `_safe_attr` swallows
`Exception` by design, so an `AssertionError` tripwire would be caught and the test would pass
vacuously the moment anyone routed a content read through that helper. This was verified by
mutation: injecting `_safe_attr(q, "resolution_criteria")` into the deferral path fails the
test.

### Rejected — attaching the event to the exception

`UnsupportedQuestionTypeError.event`, so the batch could catch and read it. It guarantees
message/event agreement at one construction site, but puts state on a sanitized exception type
whose entire contract is "nothing but a safe string". Extracting `_type_tag`, shared by both
the message and the event, delivers the same drift protection without that.

### Deferred (do not read the absence as an omission)

- **Ledger persistence of deferrals → M1-602**, when writers and a run context exist.
- **Structured `extra` fields on log records** — deliberately not built; see above.
- **Golden valid/invalid fixture coverage remains Codex's T-901.** The tests here are the
  minimum to stay honest, and none of them is a golden-record suite.

### Standing note — no production caller yet

Nothing in `src/` calls `normalize_questions`; both callers are tests. That is why the return
type changed *now* rather than later — it is the cheapest this change will ever be. It also
means the behavioural check for M1-203 **is** the test suite, with no end-to-end path to
exercise until the pipeline lands.

The M1-202 bullet above saying a deferred subquestion "still aborts the whole batch … is
M1-203" is left as written: these notes are a historical record, and this section supersedes it.

### M1-203 round-2 — GPT cross-model review findings addressed

GPT returned **changes requested** on two blocking findings, both against the *claimed*
error-hygiene guarantees rather than the batch behaviour (which it accepted). Both were
reproducible. Suite 332 → 336.

**Finding 1 — `isinstance` gates accept subclasses with attacker-controlled rendering.**
`_type_tag`'s `isinstance(x, str)` returned a `str` *subclass* unchanged (its value passes the
`_KNOWN_SDK_TYPES` membership check while its `__str__`/`__repr__` renders anything), and
`_safe_int`'s `isinstance(x, int)` accepted `int` subclasses and `IntEnum` (whose repr embeds
its class/member name). GPT reproduced `PLANTED_SECRET` surfacing through both the WARNING log
(`%s` → `__str__`) and `DeferralEvent`'s generated `__repr__`.

Fix: **exact-type gates** — `type(x) is str` in `_type_tag`/`_supported_type`, `type(v) is int`
(plus `v > 0`) in `_safe_int`. Anything not exactly the built-in type degrades to
`'unknown'`/`None`, so only a built-in's rendering — which carries no payload — can ever run. A
`str`-subclass valued `"binary"` is now deferred as unknown rather than normalized (stricter
reading). And because field annotations do not validate an exported frozen dataclass, the
invariant is now **enforced on `DeferralEvent` itself** in `__post_init__`, which coerces every
unsafe field (subclass reason/tag, `IntEnum` id) to a safe module-owned value regardless of how
the event was constructed — matching the "by construction, not by promise" line the event's own
docstring already made.

*Deviation / decision:* `__post_init__` **coerces** rather than **raises**. Coercion was chosen
(owner-confirmed) because it matches how ids already degrade to `None`, keeps a diagnostic value
from turning a deferral into a crash, and avoids events.py needing to own or lazy-import a
sanitized exception to dodge the normalize↔events circular import.

**Finding 2 — reading `question_type` through `_safe_attr` hid malformed records.**
`_supported_type` read the type via `_safe_attr`, which swallows *all* exceptions → `None` → a
`question_type` getter that *raises* was silently turned into an `unrecognized_type` deferral,
hiding the defect and violating the rule that every malformed shape arrives as the module's own
error. Fix: a dedicated `_read_question_type` reads the type once and converts a failing getter
into a constant-message `NormalizationError … from None` (so the getter's exception, which can
echo field values, surfaces in neither message nor traceback). The single read is threaded
through classification, event creation and the error message — no double getter call, no
inconsistent result from a stateful getter. Best-effort `_safe_attr` swallowing is now reserved
for the optional *identity* reads only, exactly as GPT scoped it. Extracted `_build_canonical`
so the batch path builds an accepted question from that same single read.

Four regression tests added: `str`-subclass tag (event + log + singular raise all render
`'unknown'`, no leak); `int`-subclass and `IntEnum` ids withheld; direct `DeferralEvent`
construction coerced; and a raising `question_type` getter aborting as `NormalizationError`
(asserted *not* `UnsupportedQuestionTypeError`, i.e. not silently deferred).

### M1-203 round-3 — GPT cross-model review: approved

Round 3 confirmed both round-2 blockers genuinely closed and returned one Medium (diagnostic
reason/type coherence) plus one Nit (read-once not regression-tested); both were addressed and
GPT then **approved**. Suite 336 → 351.

**Medium — reason/type coherence.** `DeferralEvent.__post_init__` still derived `reason` partly
from a caller-supplied value, so direct construction accepted contradictory pairs:
`("date", "unrecognized_type")` misclassified a known deferred type, and
`("binary", "deferred_v1_type")` claimed a *supported* type was deferred. Fix: **`reason` is now
derived exclusively from the canonicalized `question_type`, never trusted from the caller.** A
known SDK type outside `_SUPPORTED_TYPES` is kept with `reason="deferred_v1_type"`; everything
else — a non-`str`, a `str` subclass, an unvetted tag, or even a *supported* type (which is never
deferred) — collapses to `("unknown", "unrecognized_type")`. `reason` is defaulted, `_REASONS`
removed, and `normalize._deferral_event` no longer computes or passes `reason`, so derivation
lives in one place. `events.py` derives `_SUPPORTED_TYPES` from `config.SupportedQuestionType`
directly — `normalize` imports *from* `events`, so importing back would be circular, and `config`
imports nothing from `questions`.

**Nit — read-once not regression-tested.** None of the round-2 tests failed if a second
successful `question_type` read were reintroduced. Fix: a `_CountingQuestionType` wrapper whose
getter returns a tag once then raises on any second access, exercised on the batch-defer,
batch-accept and both `normalize_question` branches (each asserts a single read); plus a
`[None, [], foreign-str]` parametrization pinning the readable-but-weird → defer distinction
against a raising getter → abort.

GPT's approval mutation-tested the read-once guards (a reintroduced second read fails all three),
verified the `events → config` import is acyclic with identical supported sets at runtime, and
confirmed no legitimate path constructs an event with a supported type.

## Workflow hardening (chore, not a backlog item)

Five recurring process defects, closed with tooling rather than more discipline. Landed as one
chore branch **before** the next parallel wave so the `uv.lock` touch could not collide with
M1-303's Exa dependency.

**1 — The Done-flip miss (M1-203, M1-401, M1-305).** The backlog `.xlsx` is no longer tracked: the
four `docs/backlog/*.csv` are the single source and `scripts/backlog_xlsx.py` rebuilds the workbook
on demand. That removes the hand zip-patching, the CSV/xlsx drift fixed in PR #13, and the
two-file status edit. `.github/scripts/check_backlog.py` adds a `lint` (unique IDs, closed
vocabularies for Status/Priority/Owner/Complexity, resolvable dependencies) that runs in
`quality-gate`, and a `gate` that runs as its own required job, `backlog-status`: on a
`feat/<item>-*` PR the row for `<item>` must read `Done`. Draft PRs and non-item branches skip.
Kept out of `quality-gate` deliberately — it is expected red for most of a branch's life, and
mixing that into the code signal would train us to ignore both.

**2 — The GPT-review spiral.** `tests/property/` (hypothesis, dev-only) asserts the invariants
review has been finding one per round: never raises, strict weak ordering, permutation-invariance,
replay-stability across the persisted JSON form, and no value leak. The M1-305 tiebreak's five
rounds map onto four properties in one file. It found a new defect on its first run — see below.

`scripts/review-request.py` now also gives that loop an explicit exit policy. Round 1 is the broad
implementation review; round 2 is a remediation review focused on the preceding findings and
regressions introduced by their fixes. From round 3 onward, the stopping rule is active: another
blocker must quote the violated acceptance criterion or standing convention, identify a reachable
product path or public module boundary using accepted input/state, reproduce the wrong result
against the current commit, state its impact, and propose an in-scope fix. Otherwise it is a
non-blocking backlog candidate and cannot withhold approval. Remediation rounds require
`--previous-reviewed <commit>` and put that exact commit-to-HEAD delta before the full branch diff,
so a stale response cannot silently restart work against an older tree and each round is not a new
blank-slate audit.

**Three disqualifying tests, carried by every round.** The stopping rule bounds how *severe* a
finding must be; it does nothing about findings that were never this branch's to answer. Those get
a mechanical test each, checkable before merit is argued, and each one is here because it was paid
for:

- **Outside the trust boundary.** `CLAUDE.md` now states it: trusted is `config.yaml`, every
  filesystem path in it, the local filesystem, the operator's shell, and anything reachable only
  by monkeypatching internals; untrusted is provider JSON, Metaculus payloads, LLM output, values
  read back out of the ledger, and config *values* that fail validation. It extends the M1-401
  path carve-out with the same argument — an operator-supplied path is configuration, not content,
  and an operator who can plant a FIFO can edit the config. It binds the author as well as the
  reviewer, which is the half that matters: the hardening gets written before anyone reviews it.
  M1-308's round-6 `verify-env` hang is the case that named the rule. It stays fixed — the
  boundary is not retroactive, and reverting written, passing, gate-green code to prove a point
  costs more than it saves.
- **Already on the diff base.** M1-303's round 4 reported holes equally present in merged AskNews
  code. They were right, and they became M1-309 — after the branch had paid 791 lines of churn for
  findings that were never about this branch.
- **Stale.** Three separate rounds (M1-308 r6, M1-603 r4, M1-303) restated findings already closed
  on a newer tree. `--previous-reviewed` fixes the request side; the response side is now a
  contract term — the reviewed commit hash is demanded *before* the verdict, so a void round is
  caught before its findings are written.

None of the three is a dismissal: each says "propose the backlog row", because the finding is
usually real and only misfiled.

**The author-side half.** 47 of the first 121 non-merge commits were review-round commits, but the
spread is what matters: M1-202 and M1-401 closed in two round-commits each, M1-305 took ten, and
the cheap two are not the easy two. The difference is that M1-202's notes (§ M1-202 above) were
written as `### Decision — X, and why`, `### Deviation`, `### Rejected — X, and why not`,
`### Deferred (do not read the absence as an omission)`, `### Standing risk — not verifiable
offline` — before round 1, so round 1 had nothing left to discover. `review-request.py` now emits
those five headings into the request instead of one paragraph of advice, each with its own TODO:
an unfilled heading is visible in the sent request, and advice is not.

**3 — `GPT_REVIEW_*` files.** Gitignored and blocked by the tracked-artifact check;
`scripts/review-request.py <ITEM>` generates the request on stdout from the backlog row, its `D##`
decisions and the branch diff, leaving *deliberate choices* and *risk areas* as explicit TODOs.
Those two sections are the judgment; the rest was always mechanical.

**4 — Parallel-track collisions.** `docs/TRACKS.md` is a claims registry (who holds the
dependency-adding item, which migration number is taken).
`.github/scripts/check-migrations.sh` turns the migration gotcha into a gate: unique and
contiguous numbers, no number reused from master under a different filename (which git merges
cleanly and only breaks at runtime), and no edit to a migration already on master.

**5 — Branch drift and worktree cleanup.** `scripts/start-item.sh`, `sync-worktrees.sh --merge`
and `finish-item.sh` cover create → daily merge → retire. `finish-item.sh` refuses unless the
branch is an ancestor of `origin/master`, removes the worktree *before* deleting the branch (the
ordering `gh pr merge --delete-branch` gets wrong when master lives in a sibling worktree), and
leaves the remote branch alone unless `--delete-remote` is passed.

Also: **M1-307, M1-308 and A-1106 now have backlog rows**, drafted from `CLAUDE_CODE_PROMPT.md`
§ B. They were in the brief but not the CSV, which the new gate would have rejected on M1-308's
first PR — and "the CSV is not the complete scope" was a gotcha worth deleting rather than
documenting. A committed `.claude/settings.json` gives every worktree the same permission
allowlist (outward-facing actions — `git push`, `gh pr merge` — deliberately left out).

### Open defect found by the new property suite

`research/hashing.py: content_sha256()` raises a raw `UnicodeEncodeError` on a lone surrogate,
and that exception's message quotes the offending character. Lone surrogates are reachable:
`json.loads('"\\ud800"')` returns one and `ResearchDocument` accepts it in `title`/`snippet`/
`summary`, so an adapter hashing provider text can crash with an unsanitized error. Two clean
fixes exist — reject the document with a sanitized `ResearchError`, or encode with
`surrogatepass` — and they differ in policy, not mechanism, so this is an owner call. Neither
changes any existing digest (the inputs in question currently raise rather than hash). Recorded as
a strict `xfail` in `tests/property/test_canonical_properties.py` so it converts to a hard failure
the moment it is fixed. **Not** fixed on the branch that found it: that would be scope creep into
M1-301's module from a workflow chore.

### Workflow hardening — cross-model review round 1

GPT returned CHANGES REQUESTED with five blocking findings. All five reproduced against the
repo before anything was changed; none were speculative. Two were false claims the tooling
itself made, which is the category worth being loudest about: a gate that reports success it
did not establish is worse than no gate, because it is read as evidence.

**1 — The Done-flip gate had two live false-greens.** `BRANCH_PATTERN` was anchored to lower
case and to `feat|fix`, and every non-matching branch was *skipped*, so `feat/M1-303-x` and
`feature/m1-303-x` both exited 0 while M1-303 read `Not Started`. The classification now lives
in `_classify_branch()` — a pure function, which is why it can be tested at all — and sorts into
item / infrastructure / unrecognized, with **unrecognized failing**. Owner decision: fail closed.
The cost is that a branch like `chris/experiment` cannot open a PR without a rename or a one-line
addition to `SKIP_PREFIXES`; the benefit is that no future prefix silently skips.
`tests/unit/test_check_backlog.py` holds the branch-name table, including GPT's exact
counterexamples. A branch with no `/` at all is unrecognized too — a bare `chore` used to match
the skip list on the strength of its whole name (found while writing that table, not by review).

**2 — The migration gate could be bypassed by a stale check.** `check-migrations.sh` compares
against the `origin/master` of its own run, but master had `required_status_checks.strict =
false`, so PR A and PR B could each add an `003_*.sql` off the same base, both go green, and B
merge on its old result. The two files merge cleanly and only collide at runtime. Owner decision:
`strict: true` on master (set via the `required_status_checks` sub-resource, so `enforce_admins`
survived). The script's header now records that its central claim depends on that setting.

**3 — The claims registry is advisory, and now says so.** `start-item.sh` read `TRACKS.md` from
the working copy (which goes stale) and printed the claim row *after* creating the worktree. It
now reads `git show origin/master:docs/TRACKS.md` after fetching, and prints the claim first.
Deliberately **not** fixed: the underlying race. A shared atomic reservation means pushing a claim
commit to a protected branch before every item, which is heavier than the collision it prevents in
a repo where one person serializes the tracks. `TRACKS.md` now states plainly that it is
coordination, not a lock, and names `check-migrations.sh` and `uv.lock` conflicts as the actual
enforcement.

**4 — `review-request.py` asserted the gates passed without running them.** The sentence lived in
`PROJECT_CONTEXT`, so generating a request on a red branch produced a document opening with a
falsehood. It now runs all four gates and **exits non-zero with nothing on stdout** if any fail —
verified by injecting a failing test and confirming a zero-byte redirect. `--no-verify` emits an
explicit NOT VERIFIED banner rather than going quiet.

**5 — The permission allowlist did not preserve the boundary it implied.** Rules are
prefix-matched, so `Bash(uv run *)` matched `uv run git push` and `Bash(python3 *)` matched any
subprocess. Both are replaced by the specific invocations. The file now records what it is (prompt
reduction, small blast radius) and what it is not (a sandbox) — `Bash(scripts/*)` still reaches
`finish-item.sh --delete-remote`, and the boundary on outward actions is the operator.

**Non-blocking, all three accepted.** `st.randoms(use_true_random=True)` contradicted the CI
profile's `derandomize=True`, so the one property whose counterexample *is* an ordering would have
arrived unreproducible and unshrinkable — now `st.permutations`. Two strategy claims were
overstated: the astral/surrogate-pair "pair" was one literal written twice, and `TIMESTAMPS` held
only ISO strings, which cannot carry `datetime.fold`. Both now generate the real distinction —
`fold` survives the schema because `_to_utc`'s `astimezone` returns `self` when the tzinfo is
already `timezone.utc`, so the round-3 bug's input class is finally fuzzed rather than only
unit-tested. And `finish-item.sh` read the backlog status before fast-forwarding master, printing
a pre-merge `In Review` under the word "now".

### Workflow hardening — cross-model review round 2

Four blocking findings, all reproduced before anything was changed. The theme is sharper than
round 1's: each of the four mechanisms *claimed* to enforce something it never observed. A check
that cannot see the state it guards is not a weak check, it is a false one — and three of these
four had a comment in the file cheerfully describing the hole.

**1 — The claims registry could not see a live claim.** Round 1 moved the `TRACKS.md` read from
the working copy to `origin/master`, which was the wrong axis: a claim lives on its *own branch*
until that branch merges, so master is precisely where it is not. The registry was therefore blind
for the entire lifetime of every claim it existed to record. New `scripts/tracks.py` scans
`docs/TRACKS.md` across every `refs/remotes/origin/*` plus master, and keeps a row only if its
branch is still live — the remote ref exists and is not yet an ancestor of `origin/master`. That
liveness test is what made a hard failure affordable: rows outlive their branches by design
(`finish-item.sh` tells you to sweep them on your next branch), so without it a merged claim would
have blocked the next track forever. **Owner decision: `--deps` exits, with no override flag.** If
the holding branch is genuinely abandoned, delete the branch or drop the row — both are honest
edits to the registry, and neither is a habit that forms by reflex. Two secondary defects died
with the old detector: `grep -qi '| *yes *|'` matched a `| yes |` anywhere in the file (the
Standing claims table included) and missed `Yes (uv.lock)`. Parsing is now scoped to the Worktrees
section, and an unrecognized deps cell counts as a claim. A row with no readable Branch cell counts
as live, and a registry unreadable on every ref fails the check: "I could not tell" is not evidence
that the slot is free. The residual window is now one thing only, and `TRACKS.md` names it —
between running `start-item.sh` and pushing your row, nobody else can see it.

**2 — A dirty tree produced a truthfully-worded false review request.** The four gates run against
the working tree; the diff is built from `origin/master...HEAD`. Those are the same code only when
the tree is clean, so an uncommitted fix yielded four honest passes for a change the reviewer was
never shown — and an untracked test file changes what pytest collects while appearing in no diff at
all. `review-request.py` now requires a clean tree before verifying. `--no-verify` remains the one
way through, because it claims nothing; on a dirty tree its banner gains a second line rather than
staying quiet about the second reason to distrust the request.

**3 — Branch discovery disagreed with the branch contract.** `finish-item.sh` globbed
`feat/<id>-*` and `fix/<id>-*` while `check_backlog.py` accepted five prefixes, any casing, and a
bare `feat/<id>` with no slug. Seven of the eleven forms the gate would merge could never be
cleaned up afterwards — a branch that passes CI and then strands its own worktree. Fixed by
deleting the second implementation rather than syncing it: a new `check_backlog.py classify`
subcommand exposes `_classify_branch` to the shell (single branch, or a batch on stdin so one
process classifies a whole repo), and `finish-item.sh` filters its output. Verified by comparison
— against five branch forms the old globs found one and `classify` finds all five.

**4 — The allowlist bypassed the operator it named as the boundary.** `Bash(scripts/*)` reached
`finish-item.sh --delete-remote` and its `git push origin --delete` with no prompt, while
`git push` and `gh pr merge` were excluded by intent. That is the same defect as round 1's
`Bash(uv run *)` one level up, and the file's own comment documented it as accepted. There are now
no directory wildcards over `scripts/` and no interpreter wildcards; scripts are listed one at a
time, and `finish-item.sh` is deliberately absent, so it prompts once per merge.

**Beyond the review, in the same lines.** `finish-item.sh --delete-remote` proved only that the
*local* ref was merged, then deleted the *remote* one — so a review fix pushed from another machine
was deleted on the strength of a stale local ref. The remote-side ancestor proof now runs before
anything is removed, so a refusal cannot land halfway. `start-item.sh` validated the backlog row
against a possibly-stale on-disk CSV *before* fetching (GPT's non-blocking note); it now fetches
first and falls back to `origin/master`'s copy, read into a variable rather than piped into
`grep -q`, since grep's early exit SIGPIPEs `git show` and pipefail would turn a found row into a
failure. And its lint call no longer redirects stdout to `/dev/null`, which was hiding every
problem detail and showing only the count.

**Tests.** `tests/unit/test_tracks.py` (parsing table, liveness table, and the cross-branch scan
run for real against a throwaway repo with a local bare `origin`), `tests/unit/test_finish_item.py`
(every mergeable branch form cleaned up end to end, plus the refusals), `tests/unit/
test_review_request.py`, and `classify` cases added to `tests/unit/test_check_backlog.py`. The
first and second are integration tests on purpose: both defects lived in the disagreement *between*
two internally-consistent components, which no unit test of either one can see. 503 → 612 tests.

### Workflow hardening — cross-model review round 3

Two blocking findings, both false-safe paths in mechanisms whose purpose is to stop an unsafe
workflow before it starts.

**1 — A malformed claim registry read as an empty registry.** `parse_claims()` returned `[]` for
a misspelled Worktrees heading, a missing table and a ragged row; `_scan()` then called the ref
readable and `deps` exited successfully. A typo in a claim's Branch cell also made `is_live()`
classify it as stale even when the containing ref was still active. Parsing now returns claims and
structural problems separately, and every readable unmerged ref is validated. Failure to enumerate
remote refs also blocks instead of becoming an empty ref set. An unknown branch
whose exact row exists on master is a stale landed claim; an unknown row unique to an unmerged ref
is invalid and blocks. That provenance distinction preserves cleanup after a merged branch is
deleted without turning a typo into evidence that the dependency slot is free.

**2 — The remaining script wildcard bypassed the outward-action prompt.** Although direct `git
push` and the top-level `scripts/*` wildcard were gone, `Bash(bash .github/scripts/*)` could run a
newly-created helper containing `git push` without prompting. It is replaced by exact entries for
the migration and tracked-artifact checks; a regression test rejects script-directory wildcards.

**Tests.** Command-level throwaway-repository tests cover the exact singular-heading and mistyped
cases the review named, plus ragged tables, a deleted branch whose row landed on master, and a
forced failure of remote-ref enumeration. `tests/unit/test_claude_settings.py` pins the allowlist
shape. 612 → 624 tests.

**Three follow-ups found while verifying round 3, fixed in the same commit.**

*The registry could not bootstrap itself.* `_scan()` treated an unreadable
`origin/master:docs/TRACKS.md` as a broken registry, but on the branch that *introduces*
`TRACKS.md` the file is legitimately not on master yet. So both subcommands exited 1 in this very
repository, and because `start-item.sh` runs them under `set -euo pipefail`, no item could be
started until this branch merged — the tool was unusable on the branch that added it. Every
throwaway-repo test missed it by seeding the registry onto master first. `_scan()` now separates
"`origin/master` does not resolve" (a problem) from "it resolves and carries no registry" (a
`NOTE:` on stderr, exit 0), and claims are read from the open branches meanwhile. This does not
soften the check: with no master rows, the landed-stale-claim exception has nothing to match, so
every unknown branch still fails closed, and a live dependency claim on a branch still blocks.

*The allowlist regression test covered one round of two.* It asserted only the absence of round
2's `scripts/*`, leaving round 1's interpreter wildcards — the exact defect the file's own comment
opens by describing — pinned by nothing. It now also rejects interpreter wildcards by name and by
shape (any rule whose last token before `*` is an interpreter), asserts `git push` / `gh pr create`
/ `gh pr merge` / `gh release` are absent, and asserts `finish-item.sh` appears in no rule.

*`is_live()` took an `existing` argument it never read.* Existence needs the cross-ref provenance
only `live_claims()` has, so the parameter made a half-test look like the whole liveness test.
Dropped, and its docstring now says why existence is decided elsewhere.

### Workflow hardening — the request pins its own HEAD

The review-round contract closed the *response* end of the staleness problem: the reviewer is
asked for the commit hash they examined, before the verdict. It left the *request* end open, and
that half is the one that had already cost a round. Until now the request never stated the commit
it described — it said only "on branch `feat/…`, diffed against `origin/master`", both of which
name different commits on different days. So when M1-308's round 6 answered against a tree that
predated the head its own request embedded, nothing in the document contradicted it; the round was
reconstructed by hand afterwards, from commit timestamps.

`scripts/review-request.py` now resolves `HEAD` and the diff base to full commit hashes once, up
front, prints both, and builds **every** range from them — the branch diff, the remediation diff
and the ancestry check that validates `--previous-reviewed`. Resolving a symbolic revision twice
is the defect in miniature: two lookups of `HEAD` can name two commits, and the request would then
validate against one and print the other.

Two smaller holes closed with it:

- **The clean-tree check ran only *before* the gates.** `pytest` takes minutes here. A commit or a
  stray write inside that window would attach four green gates to a revision they never described,
  under the hash the request now pins. Both the clean-tree check and the `HEAD` resolution are
  repeated after the gates, and a mismatch is a hard failure with a "re-run the request" message
  rather than a footnote.
- **`_reviewed_revision` re-resolved `HEAD` itself**, so the ancestry check and the printed range
  were two independent lookups. It now takes the caller's already-pinned hash.

**Deliberately not taken from the same working branch: dropping the embedded diffs.** The draft
this was split out of replaced the remediation and branch diffs with `git diff <range>` commands,
on the reasoning that "the reviewer is already running in the worktree" — worth ~150 KB a request.
That reasoning does not hold for this project's reviewer, which receives pasted context and has no
filesystem, and the evidence is in the reviews themselves: M1-308's round 7 cited
`research/allowlist.py:297` and `tests/property/test_allowlist_properties.py:3`, neither of which
it could have read from a diffstat. An inspection command the reviewer cannot run is not a
substitute for the code. `test_the_embedded_diffs_survive_the_pinning` pins that, because pinning
the ranges is exactly what makes the substitution look free.

The rest of that draft — a rewrite of the trust boundary in `CLAUDE.md` and of the three
disqualifying tests — is a policy change rather than tooling, and is held back for its own
decision. Landing it alongside a mechanical fix would have moved the bar mid-item, while M1-308
was still open on findings judged against the current wording.

## M1-603 — Recording lifecycle events atomically

The acceptance criterion is "injected failures cannot leave an approved/submitted state without
its event record", and the useful question turned out to be *where an approved state can be
written at all*. M1-601 shipped `forecast_records` with a mutable-looking `status` column and
explicitly deferred the answer (see the M1-601 "Deferred" block above, and the same sentence in
both `001_initial.sql` and `002_research_document_fields.sql`: "M1-602/M1-603 add the triggers
that forbid UPDATE outright"). This item settles it.

### Decision — the record is immutable and the status is derived

**Owner decision, taken before coding.** A forecast record is written once, as a `draft`, and is
never updated. Every later state exists only as an appended `lifecycle_events` row, so a record's
current status is the `to_status` of its highest `event_seq`, falling back to
`forecast_records.status` while it has no events. `forecast_records.status` therefore means
*status at creation* and is pinned to `'draft'` by a `BEFORE INSERT` trigger.

The alternative — a narrowly constrained `UPDATE` of `status` alongside its event, with a trigger
asserting every other column is unchanged — was considered and rejected. It keeps one obvious
column to read, but it makes a stored record's bytes change after creation, which is the
guarantee D25 and M1-602's "v1 remains byte-identical" exist to provide.

What this buys is that the criterion stops being a property of the write path. There is no second
place for an approved state to live, so an approval event is not *evidence of* the approved state
— it **is** the approved state. `tests/property/test_lifecycle_properties.py` checks this as
reachability rather than by example: breadth-first from `draft` over every legal transition except
the `approved` one, and `approved` is unreachable. Same for `submitted`.

The stricter reading was taken twice more, per the ambiguity rule:

- **A record may only be created as `draft`** — not `draft` or `validated`. It costs M1-602 one
  extra event write per record and makes the event log the complete history.
- **`submitted` requires a refetch-verified attempt.** M2-704's "success requires refetch
  confirmation" is enforced by trigger, not left as a convention for that item to keep.

And twice more in round 2, for the same reason:

- **A submission receipt must carry its completion time**, because the table is append-only and
  the ledger only hears about an attempt once it is over.
- **The upgrade is refused rather than reconciled** when a ledger already holds a non-draft
  record. The looser reading — migrate what can be migrated — means synthesizing events nobody
  recorded.

### Delivered

- `src/whiskeyjack_bot/migrations/003_lifecycle_events.sql` — twenty-three triggers: the state
  machine, the draft-only, hash-binding and two receipt insert guards, sixteen append-only blocks
  and two evidence identity pins. Plus `lifecycle_events` (the ordered spine,
  `UNIQUE (forecast_record_id, event_seq)` and a partial unique index on each of its five detail
  links), `submission_verifications`, `forecast_records.forecast_sha256`, and an upgrade
  precondition that refuses a ledger holding a non-draft record. `LEDGER_SCHEMA_VERSION` 2 → 3.
- `src/whiskeyjack_bot/ledger.py` — `connect()` also sets and verifies
  `PRAGMA recursive_triggers = ON`, without which none of the above holds; see the round-1 section.
- `src/whiskeyjack_bot/lifecycle.py` — the `Literal` vocabularies, `_LEGAL_TRANSITIONS`,
  `LifecycleError`, the `LifecycleEvent`/`SubmissionAttempt`/`SubmissionVerification` value
  objects, a nesting-safe `transaction()`, three readers —
  `current_status()`/`read_history()`/`unresolved_uncertainties()` — and five writers:
  `record_validation`, `record_failure`, `record_approval`, `record_submission_attempt`,
  `record_submission_verification`.
- `tests/unit/test_lifecycle.py` (188 tests) and `tests/property/test_lifecycle_properties.py`
  (17, ~15s: one ledger for the session with a fresh record per example, because a database
  per example put the file past two minutes).

003 was edited in place through three review rounds rather than superseded, so
`LEDGER_SCHEMA_VERSION` stays 3 and its checksum changed with it. A database migrated from an
earlier cut of the branch therefore fails `ledger.py`'s checksum-drift check by design; nothing on
master has ever been at version 3, so the affected population is local scratch databases.

### Decision — approval binds to a stored hash, enforced by the database

003 adds `forecast_records.forecast_sha256` (NULLable, with a `BEFORE INSERT` trigger requiring it
of every new row — 002's pattern, for 002's reason: a column default would stamp an unearned
content claim onto a row nobody hashed). `approval_events` gets a trigger requiring its
`forecast_sha256` to equal the record's.

This reaches slightly into M1-602's column set, deliberately. Without a stored hash, "approval
binds to an exact forecast hash" can only ever be a Python-side convention and M2-701's "changed
forecast invalidates prior approval" has nothing to compare against. A pre-003 record keeps its
honest NULL and is therefore **unapprovable**, not approvable-by-any-hash — the `COALESCE(..., '')`
in the trigger is what draws that line, since no 64-character hex string equals `''`.

### Decision — append-only triggers cover the forecast and event tables, not evidence

`UPDATE` and `DELETE` are both blocked on `forecast_records`, `lifecycle_events`,
`approval_events`, `submission_attempts`, `resolution_events` and `score_events`. `research_runs`
and `research_documents` get a `DELETE` block and a *partial* `UPDATE` block.

D25's wording is "append forecast versions and lifecycle events", and the handoff describes a
research run as carrying started *and* completed timestamps, an error summary and a cost — a row
M1-306 starts and later finishes. Blocking `UPDATE` outright would decide that unstarted item's
write shape from outside it. So the rule is narrower and says what it means: **evidence may be
completed and annotated, but never re-identified and never erased.** Identity and provenance —
`retrieval_run_id`, `provider`, `question_id` and the run's timestamps; a document's
`document_id`, `retrieval_run_id`, `canonical_url`, `original_url`, `content_sha256`,
`retrieved_at_utc`, `provenance` and `source_type` — are pinned by two `BEFORE UPDATE` triggers;
everything M1-306 fills in on completion stays writable. 002's completeness triggers stay live on
that pair and remain the enforcement on the rest.

The first cut blocked `DELETE` only, which left a stored run's provider or a document's URL and
content hash rewritable in place — detaching the evidence from what was actually retrieved as
effectively as erasing it. GPT round 1 raised it as a non-blocking observation; it was worth
taking. Round 2 then showed the pinned set was drawn on the wrong test — "NOT NULL in 001" rather
than "established at creation" — and widened it; see finding 4 below.

`schema_migrations` is deliberately untouched: it is the migration runner's own bookkeeping, and
`test_ledger.py::test_checksum_drift_is_rejected` corrupts it on purpose.

### Decision — `rejected` is `validated -> validated`

The seven states have no `rejected` member, and the handoff requires a rejected approval to "leave
the last valid record intact". So a rejection records a decision without moving the record, as a
self-transition. `failed` is terminal by omission: a retry is a new forecast *version* (M1-602),
not a resurrected record.

### Decision — a submission has three outcomes, not two *(revised in round 2)*

The first cut required `success = 0` for `submission_failed`, which the migration's own smoke test
showed left a hole: an attempt that posted successfully but whose refetch did not confirm it —
M2-704's uncertain timeout — satisfied neither event's precondition and so could record no
lifecycle event at all. The fix then was to make the two events exact complements, so every attempt
had exactly one legal event.

That was still wrong, and round 2 caught it: the complement put the uncertain attempt in
`submission_failed`, whose destination is terminal `failed`. See the round-2 section below. The
three events now partition the `(success, verified_by_refetch)` pair:

| success | verified | event | destination |
|---------|----------|-------|-------------|
| `True` | `True` | `submitted` | `submitted` |
| `True` | `False` | `submission_uncertain` | `approved` (unchanged) |
| `False` | `True` | `submission_uncertain` | `approved` (unchanged) |
| `False` | `False` | `submission_failed` | `failed` |

Total and disjoint, so every attempt still has exactly one legal event — and the two signals
disagreeing is now its own outcome rather than a failure. `detail_code` (`refetch_mismatch`,
`refetch_missing`, `timeout`, `http_error`) carries which case.

Round 3 added the exits, which round 2's fix described but could not record: an uncertain record
stays `approved` and *only* a `submission_verifications` row moves it, to `submitted` or to
terminal `failed`. Until one does, no further attempt is accepted. See round 3, finding 1 — the
partition above is unchanged, including `(False, True)`.

### Decision — no `phase` column, and no free text on the event row

Every event type names its own pipeline phase (`submission_failed` happens at submission), so a
`phase` column would be a second spelling of `event_type` that has to be kept in agreement with
it. What is *not* derivable is why a phase failed, and that is `detail_code` — a closed
vocabulary, checked by the schema.

There is deliberately no free-text column on `lifecycle_events` at all. A failure's
provider-supplied text stays in `submission_attempts.error_message`/`response_body`, which the
event row points at through a typed foreign key rather than copying. That is what lets a lifecycle
history be logged and exported without a redaction pass. The detail link is five typed nullable
FKs rather than a polymorphic `(related_table, related_id)` pair: a polymorphic pair cannot be a
real foreign key and comparing it would mean `CAST`ing across SQLite's affinity rules — the trap
002 documents for `posts_dropped_no_url`. Exactly one is set per event, which one is fixed by
`event_type`, and since round 3 each of them backs **at most one** event.

### Decision — the transition table is written twice, and pinned

`_LEGAL_TRANSITIONS` in Python and the trigger in SQL describe the same machine. The database is
the enforcement; the Python table is the writer. `test_database_accepts_exactly_the_legal_
transitions` drives all 539 `(event_type, from_status, to_status)` triples through the trigger
against a record actually sitting in each `from_status`, inside a rolled-back savepoint, and
asserts the accepted set equals `_LEGAL_TRANSITIONS` exactly. Since round 3 there are two records
sitting in `approved` — one waiting on a refetch and one not — because the retry block means the
two populations refuse opposite things; which record a triple is probed against is the only place
that rule appears in the test, and the assertion is unchanged. Drift between the two is the obvious
failure mode of duplicating a table, and it is the one thing no happy-path test would catch.

### `BEGIN IMMEDIATE`, and nesting

`transaction()` opens `BEGIN IMMEDIATE`, not a bare `BEGIN`. Every writer reads the record's
current status and then appends against it; a deferred `BEGIN` takes the write lock lazily, so two
writers can both read "validated", both decide their event is seq 2, and discover the conflict
only on a lock upgrade that cannot be retried from inside an open transaction. `UNIQUE
(forecast_record_id, event_seq)` is the second line of defence and turns any surviving race into a
loud `IntegrityError` rather than a silently reordered history.

Nested use opens a `SAVEPOINT` instead, so M1-602 can write a forecast record and its first
lifecycle event as one unit without this module either committing early or rolling back work it
does not own.

The transaction-control statements are guarded too (`_control`), which the first cut of this
module did not do: `BEGIN IMMEDIATE`, `COMMIT`, `SAVEPOINT` and `RELEASE` ran bare, so a `COMMIT`
that failed on a busy timeout or a full disk escaped as a raw `sqlite3.Error` — an error type
callers do not handle — *and* left the caller holding an open transaction that strands every later
write on the connection. Each control statement now names what has to be unwound before the
failure is reported (`COMMIT` → `ROLLBACK`, `RELEASE` → `ROLLBACK TO` then `RELEASE`), so no path
out of `transaction()` leaves the connection inside a transaction the caller believes was closed.
`tests/unit/test_lifecycle.py` proves it against a real connection whose `COMMIT` raises — a
`sqlite3.Connection` subclass rather than a monkeypatch, so every other path, including the
rollback that has to follow, is exactly the shipped one.

Cross-connection isolation was checked by hand and is what the criterion ultimately rests on: with
WAL and `BEGIN IMMEDIATE`, a second connection reading mid-transaction sees neither the approval
row nor a moved status. The rollback tests prove the failure case; this is the other half — the
intermediate state is not merely undone, it is never observable.

### Atomicity is tested twice, once without mocking

The mocked test injects a `RuntimeError` between the detail row and its lifecycle row. The
unmocked one is better: approving an already-approved record inserts a perfectly valid
`approval_events` row and *then* discovers there is no legal transition out of `approved`. Without
one transaction around both, the ledger would keep an approval decision that never became an
approval. Same shape for a second submission.

### Found by the property suite

The first version of the replay-stability property asserted
`json.loads(json.dumps(asdict(event), ensure_ascii=True, sort_keys=True)) == asdict(event)`.
Hypothesis produced `'😀'` — the UTF-16 surrogate-pair spelling of an astral scalar —
and the round trip recombined it into the single scalar. **The property was wrong, not the code**:
storage genuinely cannot tell the two spellings apart, so an equality that can is stricter than
replay can honour and would make a replayed run disagree with the live one. The claim is now
idempotence of the encoding, which is exactly what a replay needs. This is the M1-305 round-4
lesson arriving again by a different route, and a local fuzz run cost one minute where review
would have cost a round.

Two smaller things the same run pinned. `event_seq`'s `typeof()` probe cannot catch `'1'` or
`1.0`: INTEGER is *affinity*, so SQLite converts a well-formed integer literal and a lossless REAL
before any trigger sees `NEW`. Only `'abc'` and `1.5` stay in their original type, and those are
what the probe is for — the test says so, because the absence of `'1'` from that parameter list
otherwise looks like an oversight. And a `FOR EACH ROW` trigger does not fire on `DELETE FROM t`
against an empty table, so the append-only tests seed a row first; the first draft of that test
reported a pass for a trigger that never ran.

Both claims were re-checked against the finished suite rather than left as recollections of the
authoring pass, since the section was drafted before the property file could run: the
surrogate-pair spelling does break naive object-equality while leaving the encoding idempotent,
and `'1'`/`1.0` are converted and stored as integers while `'abc'`/`1.5` are refused.

### What the property suite missed, and why — the generalizable lesson

The suite was green in round 1 and still missed all three parts of finding 3. Worth writing down,
because the fix is not "more examples".

The properties were right; the **strategy** was wrong. `ANYTHING` contained hostile text, `None`,
bools, ints, floats, bytes, lists, datetimes and a bare `object()` — every one of them **inert
data**. All three escapes were values that *run code* when the module touches them: a `tzinfo`
whose `utcoffset()` raises (`tzinfo` is an abstract base class, so it is caller-supplied code), a
`SubmissionAttempt` subclass whose `__getattribute__` raises, and an unbounded Python `int` that
raises during parameter binding rather than during validation.

So: **"never raises outside our own error type" is only as strong as the assumption that inputs
are inert**, and that assumption is false for anything with a dunder, an ABC in its ancestry, or
an unbounded representation. A fuzzer restricted to data can only find data bugs. The strategy now
carries one instance of each shape — including a *stateful* `tzinfo`, because the UTC guard reads
the offset twice (once itself, once inside `astimezone`) and a stateless hostile timezone cannot
tell the two guards apart.

This generalizes past this item, and is the thing to carry into M1-604's exporters and M2-703's
gateway: when fuzzing a boundary that takes `object`, ask what in the input the code *calls*, not
just what it stores.

**Round 2 adds the other half of the same lesson.** With the strategy fixed, the suite was green
again and missed all eight findings — because five of them (1, 2, 6, 7's SQL half, 8) are only
reachable by a writer that does not go through this module at all, and two more (3, 5) are about
what the writer records *correctly* being the wrong thing to record. A property suite over the
public writers can only ever test the writers. The invariants that matter here are the database's,
and the tests that find holes in them are the ones that reach past the module with raw SQL — which
is why the exhaustive transition test drives triples through the trigger rather than through
`_append_event`, and why every fix in round 2 has a raw-SQL test beside its writer test.

**Round 3 is the third variant, and the sharpest.** Finding 2 was reachable through the *public
writers* — two rejections, both legal — and the suite still missed it, because no property said
anything about a detail row being cited twice. The strategy was not the problem this time and
neither was the layer; the invariant simply had not been written down. There is now a property over
every legal walk asserting each link column's citations are distinct, and it earns its place
immediately: the first run after the index landed failed inside the *fixture*, which had been
reusing one stored approval row for every rejection — the same shortcut the schema now forbids the
writer. A property that fails in the fixture is a property that was measuring the fixture.

### Decision — both readers answer an unknown record the same way

`read_history()` first returned `()` for a `record_id` that does not exist, while
`current_status()` raised for the same input. That makes an empty history indistinguishable from a
missing record, and these two functions are the read seam M1-604 and `show` are built on — a
caller would report "this record has no events yet" while looking at nothing at all. `read_history`
now raises `LifecycleError` too. The stricter reading, per the ambiguity rule.

### Decision — `detail_code` is required where it is the account, forbidden where it contradicts *(revised in round 2)*

The first cut required `detail_code` when `to_status = 'failed'` and said nothing about the other
events, so a non-failure event *may* carry one. Forbidding it outright was considered and
deliberately not taken, on the reasoning that `rejected_by_reviewer` was in the vocabulary, a
rejection ends at `validated` rather than `failed`, and M2-701 is the item that decides whether a
rejection records its reason that way — so leaving the door open cost nothing while closing it
later would cost a migration.

Round 2 showed the cost was not nothing: the immutable history could hold `validated ...
detail_code='internal_error'`, a success annotated with a failure, reachable by any raw-SQL writer.
The rule is now three probes — required when `to_status = 'failed'`, required on
`submission_uncertain` (a reason that does not end in failure), forbidden on the six events that
are neither. **Owner decision** on the case that motivated the original hesitation: a rejection
carries no code. It is a decision, not a failure, and its account is the actor and note on the
`approval_events` row the event cites. `rejected_by_reviewer` is therefore unwritable, and is
removed from `FailureCode` and from 003's CHECK rather than shipped as dead vocabulary in an
immutable migration — the same call round 1 made on `research_failed`.

### Round 1 review (GPT) — three blocking findings, all reproduced

All three were real. Each was reproduced against the branch before the fix and re-run after.

**1. `INSERT OR REPLACE` bypassed every append-only trigger.** SQLite resolves a REPLACE conflict
by *deleting* the row in the way, and with `PRAGMA recursive_triggers` off — its default — those
deletes fire no `BEFORE DELETE` trigger. Fourteen triggers, one statement past all of them.
Reproduced on all three of the paths GPT named: an approval row rewritten to `decision='rejected'`
underneath the `approved` event pointing at it; a refetch-verified attempt downgraded to
`success=0, verified_by_refetch=0` while the `submitted` event stood; and a history reduced to
`[(1, 2, 'approved')]` by replacing event 1 with a row at seq 2 — which passes the state-machine
trigger, because when that trigger runs the row it is about to delete is still there.

`ledger.connect()` now sets `PRAGMA recursive_triggers = ON` and **verifies the readback**, the
same shape as the existing WAL check: an ignored PRAGMA is a silent no-op in SQLite, and a silently
off setting here restores the hole invisibly. `UPDATE OR REPLACE` can delete conflicting rows too,
but the `BEFORE UPDATE` block pre-empts it; that ordering is now pinned by a test rather than
assumed.

The residual risk, stated plainly: **this is a per-connection setting**, so the guarantee is over
connections opened through `connect()`. A raw `sqlite3` CLI session against the file can still
REPLACE, and SQLite offers no schema-level defence. That is a real limit of enforcing append-only
in SQLite at all, not something this migration can close.

This is also the honest verdict on the item's own risk-area 3. "Only inserts, plus appended events"
was asserted as a structural claim, and REPLACE falsified it. The claim holds now because a pragma
makes it hold — one line of connection setup, load-bearing for the entire ledger.

**2. `research_failed` and `generation_failed` were unreachable, so they are gone.** 001 requires a
`forecast_records` row to carry a non-null `final_prediction_json`, `record_json` and
`retrieval_run_id`. No record exists until generation has already *succeeded* — so a research or
generation failure, which happens before that, had no row to attach to, and `record_failure` could
only ever answer "unknown record". Both types are removed from `LifecycleEventType`, from
`_LEGAL_TRANSITIONS` and from 003's `event_type` CHECK and transition disjunction.
`PipelineFailureEvent` is a one-member `Literal` today, kept as a named alias because M1-606 is
expected to widen it. `validation_failed` is unaffected: the draft is persisted before validation.

**Owner decision.** The alternative — inventing an attempt-scoped identity table here so those
events had somewhere to live — was considered and not taken: it decides M1-602's record-minting
shape from outside it, and it is a second event scope bolted into an item whose acceptance
criterion is about the first. Better to remove an API that cannot work than to ship it. A research
failure already has a home in the meantime (`research_runs.error_summary`, per
CLAUDE_CODE_PROMPT.md's retrieval section); **M1-606** is filed for the generation case and owns
migration 004.

One forward-compatibility gesture, because migrations are immutable and this is the last moment it
is free: `lifecycle_events.forecast_record_id` is now declared **nullable**, with the trigger's
first probe rejecting a NULL. The constraint in force is exactly `NOT NULL` — a test pins that —
but M1-606 can add attempt-scoped events without rebuilding the table, which SQLite's `ALTER TABLE`
cannot do in place and which for an append-only table would mean dropping and recreating the very
block triggers the ledger exists to make permanent.

**3. Three raw, value-bearing exceptions escaped the error boundary.** All three are the same
mistake in different clothes: a value that passes every type gate and then gets *called*.

- A `datetime` carrying a hostile `tzinfo` — `tzinfo` is an abstract base class, so `utcoffset()`
  is caller-supplied code — propagated its own `ValueError`, message and traceback included.
  `_require_aware_utc` now guards both reads, and it takes two guards, not one: `astimezone()` calls
  `utcoffset()` a *second* time, so only a stateful timezone distinguishes them. The property suite
  has one of each.
- `http_status=10**100` passed `_require_optional_int` (it is exactly an `int`) and leaked a raw
  `OverflowError` from parameter binding — which is not a `sqlite3.Error`, so the wrapper in
  `_insert` never saw it. Bounded at the field to SQLite's signed 64-bit range, and `_insert` /
  `_fetch_*` now catch `OverflowError` as the second line.
- A `SubmissionAttempt` subclass passed the module's one `isinstance` check, and every
  `attempt.<field>` read after it is a call into foreign code, positioned between the two writes.
  Now `type(attempt) is not SubmissionAttempt`, which is the gate every other validator in the
  module already used and had a written rationale for.

`except Exception` is deliberate in the first case. The set of exceptions arbitrary caller code can
raise is not enumerable, and a narrow catch here is a guess about someone else's `tzinfo`.

**Also fixed, found while confirming the above:** `_unwind` returned at the first failing statement
instead of continuing, so a failed `ROLLBACK TO <sp>` skipped its paired `RELEASE <sp>` and leaked
a savepoint onto a connection the caller goes on using — contradicting the two-part unwind contract
`_control` documents. GPT's risk-area 1 asked for a direct failing-`RELEASE` regression test; there
is now one, built like the failing-`COMMIT` test on a real `sqlite3.Connection` subclass, and it
asserts the caller's outer transaction survives and commits.

**Non-blocking, corrected:** the round-1 review request said 003 adds 16 triggers. It had 17
`CREATE TRIGGER` statements, of which 14 were append-only blocks — the request's number was simply
wrong. It is 20 and 14 now.

### Round 2 review (GPT) — eight findings, all reproduced

Four P1 and four P2, every one of them a hole in what the *schema* guarantees rather than a broken
test: CI was green and all 111 lifecycle tests passed at the time. Each was reproduced against the
branch before the fix and the same reproduction re-run after. Migration 003 was still unmerged, so
all of it landed in 003 itself rather than in a migration 004.

**1 (P1). An upgraded ledger could hold an approved record with no history.** 001 permitted any of
the seven statuses on a new record; 003's draft-only trigger constrains inserts only. Reproduced:
`current_status()` answered `'approved'` for a record whose `read_history()` was empty, with the
append-only triggers leaving nothing that could correct it. **Owner decision: refuse the upgrade.**
The alternative was to synthesize the missing events, which means inventing transitions, actors and
timestamps nobody recorded — fabricated attribution data in the one table that exists to be
trusted. `RAISE()` is legal only in a trigger body, so the refusal is a temp table whose `CHECK` the
offending row violates and whose *name* is the reason; `ledger.py` applies each migration inside
`BEGIN`/`COMMIT`, so a refused upgrade leaves the database untouched at version 2. A pre-003
*draft* is unaffected — it keeps its NULL hash and stays unapprovable, which is the case the hash
column was written for.

**2 (P1). A pre-003 approval row bypassed the hash binding.** `approval_events`' own insert trigger
cannot see a row written before it, and every pre-003 record's hash is NULL after the ALTER — so an
approval carrying an arbitrary digest could be linked by a raw insert and carry the record to
`approved`, bound to no content at all. Reproduced end to end. The binding is now checked **at the
link** as well as at the insert: an `approved`/`rejected` event's approval row must carry the hash
the record stores, and that hash must be non-NULL. Checking it where the decision becomes the
record's state is the durable half; finding 1's precondition is not what closes this one.

**3 (P1). An uncertain submission was terminal.** `success=True, verified_by_refetch=False` mapped
to `submission_failed`, whose destination is `failed`, from which nothing is legal. So a later
confirming refetch could never be recorded, the ledger would disagree with the platform for good,
and the only way forward was the blind retry the handoff exists to prevent — while the handoff's
actual requirement is that an uncertain attempt *block* retry until a refetch resolves the state.
The migration's own comment had already conceded the third state existed before collapsing it into
the second. **Owner decision: `submission_uncertain`, `approved → approved`**, with a required
`detail_code`; see the revised decision section above for the full partition. Deciding it here
rather than deferring to M2-704 is the reasoning the resolution and score transitions are already
defined here for: migrations are immutable, and a missing event type later costs a whole migration.

**4 (P1). Evidence provenance was rewritable.** Round 1's identity pins named only the columns 001
declared NOT NULL, which is the wrong test for what identity is: 002 *requires*
`research_runs.question_id` and `research_documents.original_url` / `provenance` / `source_type` of
every row it inserts, so they are established at creation too — nullable only because ADD COLUMN
cannot retrofit NOT NULL. Reproduced on all four: a run reassigned to another question, and a
document retrieved from a provider API rewritten as an agent's claim, which is a provenance
forgery. Now pinned — but as `OLD.x IS NOT NULL AND NEW.x IS NOT OLD.x`, one-way. A row written
under 001 holds an honest NULL in each and 002's triggers refuse *any* update to it until they are
filled in, so an unconditional pin would have frozen exactly the rows 002 anticipated backfilling.
NULL → value once; value → anything else never.

**5 (P2). A receipt could omit its completion time.** `SubmissionAttempt.completed_at_utc`
defaulted to `None` and `submission_attempts` is append-only, so a verified submission could be
recorded with no completion time, permanently. The field is now required, and a new
`BEFORE INSERT` trigger requires it of any writer that bypasses this module. There is no in-flight
row to leave open: the ledger only hears about an attempt once it is over. The writer also rejects
a receipt that completed before it was requested — not asked for, but the same row and the same
permanence.

**6 (P2). A resolution could resolve the wrong question.** `resolution_events` carries its own
`question_id`, and the link probe checked only `forecast_record_id` — so another question's outcome
could resolve this forecast and M5-803 would then score it against that outcome. Reproduced. A
second probe now requires the linked resolution's `question_id` to equal the record's.

**7 (P2). Malformed HTTP statuses were stored.** Half of this was closed in round 1: the oversized
case no longer escapes as a raw `OverflowError`. The other half stood — `-1`, `0`, `600` and
`2**63-1` all persisted as audit data, indistinguishable from a status a responder returned.
Now validated to 100..599 in the writer and by the same new receipt trigger, with `typeof()` in the
SQL for the affinity reason 002 documents. Worth recording that `'200'` **is** accepted by the
trigger and deliberately not in the test table: INTEGER affinity converts a well-formed integer
literal before any trigger sees `NEW`, so that row is correct rather than rejected.

**8 (P2). A success could carry a failure code.** See the revised `detail_code` decision above.

Two of the eight (2 and 4) were not on the PR as review threads; six were. Nothing was found that
did not reproduce.

### Round 3 review (GPT) — three findings, all reproduced

Two P1 and one P2. All three landed in 003, which was still unmerged; the first of them could not
have been deferred, because it needs new `event_type` members and SQLite cannot alter a CHECK
constraint — adding one later means rebuilding an append-only table, which is the operation this
whole item exists to make impossible.

**1 (P1). An uncertain submission had no way out that was not a second live post.** Round 2's fix
made the state non-terminal *so that* a later refetch could resolve it, and then gave the refetch
nothing to write. `submission_attempts` is append-only, so the original attempt cannot be updated
to say "confirmed after all", and 001 declares `idempotency_key` NOT NULL UNIQUE — so the only
route to `submitted` was a second attempt row with a new key, which is a second live post. The test
that certified the fix (`test_an_uncertain_submission_can_still_be_confirmed`) did exactly that,
with `att-2`/`idem-2`: it asserted the workflow while performing the thing the workflow exists to
prevent. Nothing about the retry was blocked either — the handoff's "block retry until refetch
resolves state" was a promise M2-704 would have had to keep unaided.

**Owner decision: record the observation, not a fake request.** `submission_verifications` is a new
append-only table — the attempt it verifies, what it saw (`confirmed` / `absent`), when it was
observed, and the snapshot it saw it in. It carries no `forecast_record_id`: the attempt already
names one, and the link probe joins through it rather than storing a second copy of the same claim.
Two new event types cite it, and they are the *refetch's* transitions rather than an attempt's:

| outcome | event | transition |
|---------|-------|------------|
| `confirmed` | `submission_confirmed` | `approved → submitted` |
| `absent` | `submission_disconfirmed` | `approved → failed` (terminal) |

`absent` is terminal for the same reason a `(0, 0)` attempt is: the post is not there, and the
retry is a new forecast version. Round 3 also made the retry block structural — no further
submission event while an uncertainty stood — and **round 4 withdrew that**; see finding 1 below.
The verification table and the two event types are what survived, and they are the part that
mattered.

The verification vocabulary is deliberately two-valued. A refetch that could not be *performed*
observed nothing, changes no state, and would be a detail row no event can cite; the uncertainty
and its `detail_code` already record that the post is unconfirmed. That is a judgment call inside
an immutable CHECK, and it is written down in the migration as one: if M2-704 needs failed check
attempts recorded, they are telemetry and want their own table, not a third outcome meaning "no
outcome".

**`(success=0, verified=1)` stays uncertain (owner decision).** GPT read it as already
refetch-confirmed. `success = 0` means no receipt, and the handoff's prohibited claims forbid
saying a live call succeeded without one — so the stricter reading holds, and the confirming
refetch now has an event of its own to be recorded as. The partition table above is unchanged.

**2 (P1). One detail row could back several lifecycle events.** The link probes checked that a
detail row belonged to this record and recorded this outcome, and never that it had not already
been cited. Because `rejected` and `submission_uncertain` are self-transitions, two immutable
events could rest on one approval decision or one attempt — a history showing a decision taken
twice on the evidence of one row. Reproduced on both. Closed with a partial unique index on **each**
of the five link columns, not only the two that were reachable: "not reachable today" is a fact
about the current transition table, which M4-802 and M5-803 have yet to add to. An index rather
than a probe, because a UNIQUE violation names the column and never a value, and because the
constraint doubles as the foreign-key index these columns lacked. Note the consequence for
`INSERT OR REPLACE`: each index is a new conflict target whose replacement DELETE must be caught by
the append-only trigger, which is another thing `PRAGMA recursive_triggers` is load-bearing for —
pinned by its own test.

**3 (P2). Raw SQL could persist a reversed receipt.** `completed_at_utc >= requested_at_utc` was
checked in the writer only — the one rule in this item enforced in a single layer, while the
sibling test two functions away pins both layers for the NULL case. Reproduced with a completion a
day before its request, permanent on an append-only table whose `requested_at_utc` is what an
idempotency key is reasoned about against. The receipt trigger compared them with `julianday`,
preceded by a `typeof`/`julianday IS NULL` probe — and the comparison itself turned out to be
lossy, which is round 4's finding 3 below. The rule and its second layer are right; the operator
was wrong.

The same treatment went to the new table: a verification names an attempt this ledger holds and
cannot have been observed before that attempt finished.

### Round 4 review (GPT) — three findings, all reproduced

One P1 and two P2, and the P1 overturns a round-3 owner decision. All three landed in 003, which is
still unmerged.

**1 (P1). The retry block was enforced where it could not work.** `record_submission_attempt` is
handed a *completed receipt* — this module is persistence, by design and by its own docstring — so
refusing the write cannot prevent a second post. It can only make a post that already happened
**unrecordable**. The SQL half was worse: the guard sat on `lifecycle_events` alone, so a writer
that records the attempt row and then its event ends up with the attempt committed and the event
refused. Reproduced: **two `submission_attempts` rows against one event**, an orphan receipt in an
append-only table, which is the exact inverse of what this item is accepted against.

**Owner decision: the ledger records; the pipeline decides.** The trigger and the writer guard are
gone. In their place `lifecycle.unresolved_uncertainties(conn, record_id)` returns the attempt ids
awaiting a refetch — a **reader**, asked *before* posting, which is the only point at which the
rule can be kept. M2-704 owns the pre-request guard and the durable reservation that serializes
concurrent submitters; neither is expressible as a constraint on a table of past events.

Two consequences worth stating. A record may now hold several `submission_uncertain` events, one
per attempt — and the round-3 unique index is what stops two of them citing the same receipt, which
makes that index *reachable* rather than defence in depth, so it now has a real reproduction rather
than a schema-shape assertion. And the property suite's walk generator lost its
`_BLOCKED_WHILE_UNCERTAIN` rule, which immediately made its fixture reuse one attempt row across
two uncertain events — the same shortcut the schema forbids the writer, found the same way as in
round 3.

The general lesson, which is not specific to this rule: **a module that only ever runs after the
fact cannot enforce a rule about whether the fact should happen.** Every guard here is a guard on
the record, and the strongest thing a record can say about an action it disapproves of is to
describe it accurately.

**2 (P2). A confirmation could reach `submitted` with no evidence.**
`submission_verifications.refetched_forecast_snapshot` was unconditionally nullable and the writer
accepted `None` — reproduced, a `submission_confirmed` transition with NULL stored — while the
column's own comment said the snapshot is what makes a confirmation auditable rather than taken on
faith. A stated guarantee with no constraint behind it is the failure this file exists to prevent.
Now required for `confirmed` in both layers, with empty-or-whitespace counting as absent; `absent`
outcomes stay nullable, which is why the column cannot simply be NOT NULL.

**3 (P2). `julianday()` cannot order microseconds.** It returns a float *day* number, so at ~2.46e6
days a double has no bits left below ~10µs: two timestamps one microsecond apart compare exactly
equal (confirmed — the difference is `0.0`, while milliseconds survive). The schema therefore
accepted a reversed receipt that the Python writer rejects, which is the same two-layer
disagreement round 3 set out to close.

Exactness needs a form that orders lexicographically, so `_require_utc` now renders a canonical
`YYYY-MM-DDTHH:MM:SS.ffffff+00:00` (fixed width 32, always UTC, microseconds always present) for
every timestamp this module writes, and 003 pins that shape on the three columns it compares —
`requested_at_utc`, `completed_at_utc`, `observed_at_utc` — then compares them as TEXT.

**This reverses round 3's "no format pin, do not become the lone outlier" reasoning, narrowly and
on purpose.** That argument holds for a column nothing is compared against. It does not survive a
column an ordering claim rests on, where the choice is not "pin or stay uniform" but "pin or
compare wrongly". `occurred_at_utc` and `created_at_utc` stay unpinned because nothing orders them
— event order is `event_seq`. Where a stored `completed_at_utc` is *not* canonical (a hypothetical
pre-003 row), the verification trigger **refuses rather than guesses**: a lexicographic comparison
against an unknown format is a coin toss that reads like a check.

### Round 5 review (GPT) — one finding, reproduced

Findings 1 and 3 above confirmed closed. Finding 2 came back: the fix was real but only half
applied, and the half that was missing is the half this file is about.

**1 (P2). The schema still permitted an evidence-free confirmation.** Round 4's note one section
up says the snapshot is "required for `confirmed` in both layers, with empty-or-whitespace counting
as absent". Both layers did require it. They did not agree on what whitespace *is*. The writer
blanks with Python's `str.strip()`, which removes all 29 codepoints where `str.isspace()` holds;
the trigger used SQLite's **one-argument** `trim()`, which removes U+0020 and nothing else.

Reproduced at `4ef7328` — real writers to reach the uncertain state, then raw SQL, which is the
boundary the trigger exists to defend:

```
VERIFICATION INSERT ACCEPTED: len=2 hex=0A09
EVENT ACCEPTED -> status: submitted
history: ['validated', 'approved', 'submission_uncertain', 'submission_confirmed']
```

A record reached `submitted` — the ledger asserting the platform confirmed the post — on a
snapshot of one newline and one tab. The `submission_confirmed` event trigger checks only that the
linked row's `outcome` is `confirmed`, so that `trim()` was the sole gate on whether a confirmation
carried any evidence at all.

The trigger now spells its whitespace set out as `trim(x, char(9, 10, 11, ..., 12288))` — the 29
`str.isspace()` codepoints — instead of inheriting a default nobody had compared against the
writer. `lifecycle.py` is unchanged; it was the layer that was already right.

**The lesson is about the test, not the trigger.** `test_a_confirmed_refetch_must_carry_what_it_saw`
had asserted *both* layers since round 4 and still missed this, because all three of its parameters
were spaces — the one character the two definitions already agreed on. Two-layer parity is not
tested by exercising both layers; it is tested by exercising a case where they could differ. It now
carries tab/newline, mixed and NBSP parameters, and a companion test asserts the equivalence over
the entire `str.isspace()` set rather than over hand-picked examples.

**Standing risk — the pinned literal is frozen against a moving definition.** `str.strip()` follows
the Unicode data of whichever Python is running; the trigger's set is fixed once 003 lands on
master and migrations are immutable. The equivalence test is the guard: a future release that adds
a whitespace codepoint fails it, and the answer is migration 004. That is the intended outcome — a
loud failure rather than a silent reopening — but it is a real maintenance obligation, recorded
here so it is not rediscovered as a surprise.

### Deferred (do not read the absence as an omission)

- **Pre-forecast research and generation failures → M1-606.** Not an oversight and not a gap left
  open silently: the event types that claimed to cover them were removed because the schema cannot
  store them. See finding 2 above.
- **`approve` / `reject` CLI commands → M2-701.** Adding them here would put a reachable approval
  path in the tree ahead of its item. This slice is library-only and makes zero network calls.
- **Resolution and score writers → M4-802 / M5-803.** Their *transitions* are defined here because
  migrations are immutable and a missing event type would later cost a whole migration.
- **Assembly of the handoff's full canonical record → M1-604 / `show`.** Approval and submission
  history is joined at read/export time, never written back into `record_json`: writing it back
  would mean updating a stored forecast version, which is what D25 forbids. `current_status()` and
  `read_history()` are the seam.
- **`record_id` minting (UUIDv7/ULID) → M1-602**, unchanged from M1-601's note.
- **Evidence on an *immediately* confirmed attempt.** An attempt with
  `success=True, verified_by_refetch=True` reaches `submitted` in one write, and its
  `refetched_forecast_snapshot` may be NULL — the substance rule above binds
  `submission_verifications`, not that path. Raised as a non-blocking observation in round 5 and
  left alone deliberately: it is unchanged from `42550b1`, predating this branch's verification
  work, and M1-603's acceptance criterion does not require the field on the initial receipt.
  Tightening it means deciding whether M2-704 can always produce a snapshot at post time, which is
  M2-704's call to make, not this item's.
- **Calendar validity of the pinned timestamps.** The round-4 GLOB pins *shape* — 32 characters in
  `YYYY-MM-DDTHH:MM:SS.ffffff+00:00` form — not that the digits name a real instant, so a direct
  insert of month `00` is shape-valid. Nothing this module writes can produce one (`_require_utc`
  takes a `datetime`), which is why it stayed non-blocking in round 5. Enforcing it in SQL means
  either a much larger GLOB set or a `strftime()` round-trip probe, and the honest place to decide
  that is alongside the pre-003 legacy-row question the verification trigger already refuses on.

### Consequences for other items

- **M1-602** must insert at `status='draft'`, supply `forecast_sha256`, and append a `validated`
  event; it can do both inside one `transaction()`.
- **M1-306** keeps its started-then-completed write shape on `research_runs`.
- **M2-701** builds its commands on `record_approval`; the hash binding is already enforced.
- **M2-704** cannot record a `submitted` state without a refetch-verified attempt row, and gets a
  non-terminal `submission_uncertain` to park an unconfirmed post in. It **does** own the retry
  guard: call `unresolved_uncertainties()` before posting and do not post while it is non-empty,
  because the ledger cannot refuse an action it only hears about afterwards (round 4, finding 1).
  Serializing concurrent submitters needs a durable reservation, also M2-704's. Every attempt it
  records must carry `completed_at_utc`, and an `http_status` if there was one; a confirming refetch
  must carry the snapshot it saw; a refetch that answers nothing is not a ledger event.
- **Operators** cannot upgrade a ledger holding a non-draft forecast record. Nothing has written
  one — M1-602 is the record writer and is unstarted — so the population is expected to be empty;
  if it is not, the ledger predates the guarantee and a fresh one is the honest answer.
