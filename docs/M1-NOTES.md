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
