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

### Delivered

- `src/whiskeyjack_bot/research/packet.py` — `ResearchPacket`, `build_packet`,
  `packet_sha256`, `canonical_packet_json`, `PacketError`, `PACKET_SCHEMA_VERSION`.
- `src/whiskeyjack_bot/research/store.py` — `open_run` / `complete_run` /
  `persist_retrieval`, `with_retrieval_counts`, `load_run` / `load_documents` /
  `load_packet`, `replay_research`, `StoreError`.
- `src/whiskeyjack_bot/research/artifacts.py` — `write_raw_responses`,
  `read_raw_responses`, `artifact_relative_path`, `ArtifactError`,
  `ARTIFACT_SCHEMA_VERSION`.
- `src/whiskeyjack_bot/migrations/005_research_run_counters.sql` —
  `documents_dropped` and `duplicates_collapsed` on `research_runs`;
  `LEDGER_SCHEMA_VERSION` → 5. **Written as `004` and renumbered at the daily master
  merge**: M1-606 held a `004` claim on its own unmerged branch and landed
  `004_pipeline_failure_events.sql` first. The migration column in `docs/TRACKS.md` is
  advisory — `scripts/tracks.py` checks the *dependency* claim and nothing reads that one —
  so `.github/scripts/check-migrations.sh` plus master's up-to-date-branch requirement is
  the real enforcement, and it worked: the collision surfaced at a merge, before either
  number could be applied twice. Renumbering was safe only because this branch's `004` had
  never reached master and so was never immutable.
- `src/whiskeyjack_bot/research/model.py` — the two counter fields on `ResearchRun`.
- `tests/unit/test_research_{packet,store,artifacts}.py` (46 tests),
  `tests/property/test_packet_properties.py` (8 properties),
  `tests/property/strategies.py` (`research_runs`, `persisted_run`, `round_trip_run`),
  two migration-005 cases in `tests/unit/test_ledger.py`.

Nothing here is reachable from the CLI, and nothing calls a provider.

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

### Found while building, before any review

Two defects, both caught by the checks CLAUDE.md requires rather than by reading.

**The counters skewed the packet hash.** The first cut took `documents_dropped` and
`duplicates_collapsed` as keyword arguments to `complete_run`/`persist_retrieval`, alongside
the run. The end-to-end smoke test then produced a stored packet hash that did not match the
in-memory one, because the caller's `ResearchRun` carried `None` for both while the row carried
`2` and `1`. Any value that is both hashed into the packet **and** accepted separately by the
writer can be stored inconsistent with the object that was hashed. Fixed by reading both off
the run — `with_retrieval_counts()` is the seam that puts them there, going through
`validate_run` rather than `model_copy`, which skips validation and would defer a negative
count to 004's CHECK, at write time, after the calls were paid for.

`raw_response_path` is the deliberate counter-example and it is why the exclusion list is not
arbitrary: it is *also* passed separately to the writer, and it cannot skew anything, because
it is excluded from the hash.

**A property strategy laundered away the distinction its own property tested for.**
`test_the_hash_survives_the_persisted_round_trip` exists for the `datetime.fold` class of bug
that cost M1-305 its round 3. The `packets()` strategy re-keyed each generated run onto the
packet's question with `model_dump(mode="json")` → `validate_run` — which round-trips every
timestamp through ISO-8601 and **drops `fold`**. So no packet reaching any property could
carry the distinction, and a deliberately fold-sensitive `packet.py` passed the property that
is named after it. `tests/property/strategies.py` generates `fold` correctly; the consumer
threw it away.

Caught only by the mutation step — the property was green both before and after the strategy
fix, so nothing but running it against broken code could have distinguished them. Fixed with
`model_copy`. All seven mutations now discriminate: the persisted-form rule (two variants,
fold-sensitive and surrogate-pair-sensitive), the sorting, the exclusion list, a constant
digest, `ensure_ascii`, and the constructor's validation.

The generalizable form is written up as `docs/LESSONS.md` lesson 9, with the measurement:
**a strategy that normalizes its generated inputs before handing them to a property can be the
thing that makes the property vacuous**, and the mutation has to express the defect the property
is *named for* — two other mutations of the same function were caught, which is exactly what made
the suite look discriminating when it was not.

### Round 1 review (GPT) — seven blocking findings, all reproduced

Reviewed commit `837f333`, which was HEAD, so nothing was stale. **Every finding was
reproduced by execution before any fix was written**, per the standing rule. All seven were
real; none was rebutted.

1. **`cost_usd = -0.0` changed the packet hash.** `-0.0` satisfies `ge=0`, renders as `-0.0`
   in the persisted form, and comes back out of a REAL column as `0.0`. An ordinary accepted
   input that falsified the acceptance criterion. Fixed by canonicalizing in
   `_require_finite` (`value + 0.0`), so both sides agree — a normalization rather than a
   rejection, because the two spellings are the same amount of money.
2. **Question-wide replay had no stable packet identity.** `load_packet(question_id=...)`
   meant "every row currently sharing this question", so persisting a second run silently
   changed what the *first* packet was and made the earlier one unaddressable. An open run
   was replayable too. Fixed by making the run set explicit: `list_retrieval_run_ids()` for
   discovery, `load_packet(..., retrieval_run_ids=...)` for assembly, and `replay_research`
   refusing an incomplete run. **This is the finding that mattered most** — the deliberate
   rule that a fresh retrieval hashes differently is only coherent if the earlier packet
   stays replayable, and it did not.
3. **Packet assembly was not one snapshot.** Three unsynchronized reads, so a concurrent
   `complete_run` between them returned an unfinished run together with the documents it only
   has once finished — a state the ledger never held. Fixed with `_read_snapshot`, a deferred
   `BEGIN` (not `BEGIN IMMEDIATE`: a reader takes no write lock, and WAL fixes the view at the
   first statement).
4. **`complete_run` was neither bound to the opened run nor once-only.** Completing twice
   rewrote a stored run's queries and cost; completing with a model carrying a different
   question, provider and start time kept the opened identity and took the other payload.
   Fixed in the `WHERE` clause — `completed_at_utc IS NULL` plus the identity columns — rather
   than a preceding `SELECT`, because a read-then-write is a race even inside `BEGIN IMMEDIATE`
   and only one statement makes `rowcount` mean what it says.
5. **The store did not own every SQLite failure, and one leaked.** A schema-valid
   `question_id = 2**63` raised raw `OverflowError`; a lone-surrogate run id raised raw
   `UnicodeEncodeError`; and fetching a column holding invalid UTF-8 raised
   `sqlite3.OperationalError` **whose message printed the planted value verbatim** — decoding
   happens at *fetch*, and the protection stopped at `conn.execute`. Fixed with `_fetch_one`/
   `_fetch_all`, an integer range check, and the rule that only `IntegrityError` keeps its
   text (that text is schema-authored; `OperationalError`'s is not).
6. **Malformed stored values were coerced into valid evidence.** `_load_json(...) or []` turned
   stored `false`, `0`, `""` and `{}` into `[]`, and a BLOB in a TEXT column came back as
   `bytes` for pydantic to coerce into a `str`. Reading is not the place to repair the ledger.
   Fixed with `expect=` on `_load_json` and `_stored_text`/`_stored_int`/`_stored_real` gates.
7. **The artifact reader accepted envelopes the writer cannot produce** — `NaN` bodies
   (`json.loads` accepts the constant) and envelopes with no run id, question, provider or
   timestamp, i.e. an unattributable blob. Fixed with `parse_constant` and full envelope
   validation.

Both non-blocking observations were acted on: the two backlog candidates are filed as
**M1-312** (compose artifact and ledger persistence) and **M1-313** (deep-freeze the packet),
and the `artifacts.py` docstring that claimed `persist_retrieval` already performed the
artifact-failure fallback was simply false and now says who actually owns it.

### The eighth defect — found by the property the review asked for

The review's finding 1 came with a process note: `round_trip_run` simulates storage with
`json.dumps` → `json.loads` rather than SQLite, and the float strategy never generated `-0.0`.
Both were true. Closing them meant adding `test_the_hash_survives_a_real_sqlite_round_trip`,
which persists a generated packet into a real ledger and re-hashes it — **and that property
immediately found a defect nobody had reported.**

A **surrogate pair** in a `provider_config` key or a query:

- `chr(0xD800) + chr(0xDC00)` is two Python code points and is **not** UTF-8 encodable;
- `json.dumps(..., ensure_ascii=True)` writes it and `json.loads` **recombines it** into the
  single scalar `U+10000` — so what comes back out of the ledger is a *different Python
  string*;
- and pydantic's `model_dump(mode="json")` renders the original pair as six `U+FFFD`.

So the in-memory packet hashed over replacement characters while the stored packet hashed over
the clean scalar. The module had explicitly claimed the JSON columns were immune because
`ensure_ascii` escapes surrogates — true of *lone* surrogates, false of pairs, and the pair is
the more dangerous case because it round-trips **successfully into something else** rather than
failing. Fixed with `_require_storable_json`, refusing rather than normalizing, on the same
reasoning `_require_storable_text` gives.

The generalizable point, and the reason this is written down rather than just fixed: **a
simulated boundary tests the simulation.** JSON was the half of storage that behaves; the
defect lived in the half being simulated away. That is the same shape as lesson 9 — a test
harness that normalizes what it is supposed to be probing — one level up, at the boundary
rather than at the strategy.

### Round 2 review (GPT) — six of seven closed, two new findings, both reproduced

Reviewed commit `c7c6052`, which was HEAD. Findings 1-6 confirmed **CLOSED**. Finding 7 was
confirmed only in part, and two new blocking findings landed — both narrow, both introduced by
the round-1 remediation itself, and both reproduced by execution before any fix.

1. **The artifact reader still accepted malformed provenance.** Round 1's fix validated that
   `provider` was a non-empty string; the *writer* refuses a **whitespace-only** one. So
   `"provider": "   "` was still accepted by the reader — an effectively unattributed artifact
   reported as valid audit evidence. Fixed by applying the writer's exact rule
   (`not value.strip()`).

   The instructive part is why the first fix missed it: I validated the envelope against a
   rule I wrote at the reader, rather than against the rule the writer already enforced. **A
   reader's job is to admit exactly what the writer can emit**, so the two must share the rule
   and not merely resemble each other. The regression asserts both halves for that reason.

2. **The new run-selection inputs were not fully validated**, in two ways:
   - `load_packet(retrieval_run_ids=[[]])` raised a raw `TypeError: unhashable type` from
     `set(requested)`. The duplicate check assumed a shape nothing had checked. Fixed by
     validating each id *before* the set operation — ordering is the whole fix.
   - `list_retrieval_run_ids(completed_only=None)` took the `False` branch by truthiness and
     returned an **open** run, i.e. a spend record surfaced from a call that reads as asking
     for finished evidence. Fixed with `type(completed_only) is not bool`.

Both are the same shape as findings the project has taken before: a public boundary added in a
hurry inherits the module's error contract only if someone applies it. Worth noting plainly —
**the remediation for finding 2 introduced two of its own defects**, which is the argument for
round 2 existing at all rather than merging on a round-1 fix.

The review's risk-area answers closed the six questions the round-2 request raised, including
the two I most expected to be wrong: arbitrary run subsets are correct semantics (the blocker
was input validation, not subset meaning), and the `IntegrityError`/`OperationalError` text
split is drawn in the right place under the fixed schema.

### Round 3 review (GPT) — nine prior findings closed, one new, reproduced

Reviewed commit `8e7870f`. **All nine prior findings confirmed CLOSED.** One new blocking
finding, and it is the sharpest of the whole item because it is the rule I had just written
into the round-3 request, applied to a place I had not applied it.

**The reader admitted what its own writer refuses.** Round 1's finding 6 taught the reader not
to *coerce* stored values, and the eighth defect taught the *writer* to refuse text that cannot
survive the round trip (`_require_storable_json`). Nobody applied that second rule on the way
back out. So:

```sql
UPDATE research_runs SET queries_json = '["\ud800"]' WHERE retrieval_run_id = 'run-1';
```

`load_run` returned `queries == ["\ud800"]` and `load_packet` built a packet from it, while
`persist_retrieval` refuses that exact value. The source-of-truth reader certified as valid
replay provenance a query its own writer declares unpersistable.

Two details make it worth recording rather than just fixing:

- **The shape gate could not have caught it.** A one-element list of strings is exactly the
  right shape; the defect is in what the string *is*. Shape validation and value validation are
  different checks and the first does not imply the second.
- **The stored bytes are pure ASCII.** `'["\ud800"]'` contains no non-UTF-8 byte on disk — the
  surrogate only exists after `json.loads`. So no check on the column's bytes, and no
  `sqlite3` decoding error, could ever have found it. It is only visible one layer up.

Fixed by calling `_require_storable_json` from `_load_json` after the shape check.

**The property this round added is the general form**, and it took two attempts, which is the
instructive part. The first cut asserted "if the writer refused this text, the reader must
refuse it too" — and failed on its second example, correctly. A surrogate **pair** is refused by
the writer, but `json.loads` recombines it into an astral scalar, and that scalar is an
ordinary value the writer would happily store; the reader returning it is right. The invariant
is not about the text that was encoded. It is about **what the reader returns**: everything the
reader hands back must be something the writer would accept, and must survive being written
back. Restated that way it passes, and it fails when the fix is reverted.

That distinction — between "the input was refused" and "the output is unwritable" — is the
whole content of the finding, and the first property would have enshrined the wrong one.

### A third demonstration of the same lesson, from the gate rather than a review

`test_a_changed_query_moves_the_hash` — written at round 1, green through two review rounds —
failed on the round-3 gate run at the full 200-example profile. It passes at 25, which is
precisely what `CLAUDE.md` says `fast` is for and not for.

The counterexample: the packet held `"\U0001f600"` and the drawn "changed" query was
`"\ud83d\ude00"`, its UTF-16 surrogate-pair spelling. Two distinct Python strings that
`ensure_ascii` renders identically, so the digest correctly did **not** move. The property's
skip-guard compared the two in memory and therefore called it a change when the ledger cannot.

No source defect: the hash is right and the test was asking it to distinguish two spellings of
one stored value, which is the M1-305 round-4 bug requested back. Fixed by comparing the
persisted rendering in the guard.

Three times now on this item — the strategy that laundered `fold`, the round trip that
simulated storage with JSON, and this — the *test* has held the wrong notion of equality while
the code held the right one. The recurring shape is worth naming: **wherever identity is
persisted-equivalence, every helper around the assertion has to use that equality too**, and
in-memory `==` is the easy default that silently means something stricter.

### Round 4 review (GPT) — APPROVE

Reviewed commit `df17907`. Round 3's finding confirmed **CLOSED**; rounds 1-2's nine remain
closed and untouched. No blocking findings, no new backlog candidates. All four risk areas the
request raised — the migration renumber, the storability boundaries, the raw-SQL property, and
M1-606's merged content — came back safe, including the one I was least sure of: that TEXT
columns need no read-side storability re-check, because a value that reached one was encodable
by construction.

One non-blocking observation, acted on: several comments and two test names still called the
counter migration `004` after the renumber. Executable references and version pins were already
correct, so it was nomenclature only — but a comment naming the wrong migration is the drift
this project spends rounds on, so they now read `005`. (Three `004` references remain and are
correct: they are M1-606's, and one M1-603-era note that predates both.)

**Four rounds.** For the record against `docs/LESSONS.md`'s table: 7 blocking findings, then 2,
then 1, then approve. Every one of the ten was reproduced by execution before any fix was
written and none was rebutted — but the more useful number is that **three further defects were
found by this branch's own tests rather than by any review**, each one from closing a process
gap a review had named rather than from the finding itself. The reviews were worth more for
what they pointed at than for what they caught.

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

## M1-606 — Recording pre-forecast pipeline failures

The acceptance criterion is two-part: "a research or generation failure with no forecast record
still produces a queryable ledger event", and "the failed attempt is linked to the forecast version
that later succeeds for the same question". M1-603 shipped `lifecycle_events` scoped to a
`forecast_records` row and explicitly could not cover this — 001 requires a non-null
`final_prediction_json`, `record_json` and `retrieval_run_id`, so no `forecast_records` row exists
until generation has already succeeded, and a research or generation failure happens before that.
003's header names this item and this migration by number: `forecast_record_id` is nullable in the
DDL specifically so this item would not have to rebuild an append-only table to get here. This item
settles the two things 003 left open.

**What shipped.** `004_pipeline_failure_events.sql`: a new `pipeline_failure_events` table scoped to
a caller-minted `attempt_id`, with a validating `BEFORE INSERT` trigger and the append-only
`_block_update`/`_block_delete` pair; `forecast_records.attempt_id` plus a partial `UNIQUE` index and
an extended `forecast_records_require_draft_on_insert`. In `lifecycle.py`:
`record_pre_forecast_failure`, `read_pipeline_failure_events`, the `PreForecastFailure` value object,
the `PreForecastEventType`/`PreForecastFailureCode` literals, and `_require_identifier`.
`LEDGER_SCHEMA_VERSION` 3 → 4. Tests: the 004 truth table driven at both entry points in
`tests/unit/test_lifecycle.py` (raw SQL for the schema, the writer for the writer) and four invariants
in `tests/property/test_lifecycle_properties.py`.

### Decision — a new attempt-scoped table, not a widened `lifecycle_events`

**Owner decision, per the task brief.** `lifecycle_events.event_type` is a closed `CHECK` and does
not include `research_failed`/`generation_failed` — the first draft of 003 carried them and they
were removed in round 1 as structurally unreachable (no record to attach to). SQLite cannot widen a
`CHECK` in place; the only route is `CREATE TABLE ... AS SELECT` plus a rename, which means dropping
and recreating every trigger on the table — including the append-only `_block_update`/`_block_delete`
pair, which is "precisely the operation the ledger exists to make impossible" (003's own words,
about itself). So a pre-forecast failure needs a table whose *own* `CHECK` can name these two event
types without touching `lifecycle_events` at all.

New table `pipeline_failure_events`, scoped to a caller-minted `attempt_id` (TEXT) rather than a
`forecast_record_id`. An attempt is the end-to-end campaign to produce one forecast version for one
question — it may fail research, fail again, and eventually either succeed (decision 2) or be
abandoned — so more than one failure can share an `attempt_id`, and `UNIQUE (attempt_id, event_seq)`
with a contiguous-next-value trigger probe is `lifecycle_events`' own `UNIQUE (forecast_record_id,
event_seq)` pattern, reused rather than reinvented. `question_id`/`tournament_id` are stored directly
on the row (not inferred through a FK) for the same reason `resolution_events` (001) carries its own
`question_id` alongside a nullable `forecast_record_id`: the thing this event is about may have no
forecast record to join through, ever.

`event_type` is `CHECK (event_type IN ('research_failed', 'generation_failed'))` — a full return of
the two vocabulary members 003 removed, just on a table where they are reachable. `detail_code` is a
`FailureCode` subset (see below). `retrieval_run_id` is a nullable `REFERENCES research_runs`:
required by trigger for `generation_failed` (generation only runs after research has completed, so
there is always a run to cite) and optional for `research_failed` (a failure can occur before any
`research_runs` row exists, e.g. the provider call never got made).

Append-only from creation, like everything else `lifecycle_events`-adjacent: unconditional
`_block_update`/`_block_delete` triggers, same shape as 003's. `PRAGMA recursive_triggers` (already
`ON` in `ledger.py` since 003) is what stops `INSERT OR REPLACE` bypassing them here too — 003 found
this the hard way in its own round 1; this migration inherits the connection-level setting rather
than needing anything new, but the header says so explicitly rather than leaving it to be
rediscovered.

The rejected alternative: widen `lifecycle_events.forecast_record_id`'s trigger-enforced
requirement to accept an attempt-only row with `forecast_record_id NULL`, and give `event_type` a
second closed set depending on which of `forecast_record_id`/`attempt_id` is present. That still
needs the `CHECK` widened (the vocabulary has to grow either way) and it makes one table cover two
unrelated identity spaces, which is the same polymorphic-FK problem 003 rejected for its own detail
columns ("a polymorphic pair cannot be a real foreign key"). A second table is the narrower change
and never puts `lifecycle_events` at risk.

### Decision — `attempt_id` links a failure to the forecast version that follows it

**Owner decision, per the task brief.** Acceptance criterion 2 has no natural home: `forecast_records`
is UPDATE-blocked (003, D25), so nothing can annotate an existing successful row with the failures
that preceded it after the fact, and nothing can annotate a failure row with a success that has not
happened yet. The link has to be established once, at the moment the successful row is written, by
both sides already agreeing on a shared value.

`forecast_records` gets `attempt_id TEXT`, added `NULLable` with a `BEFORE INSERT` trigger requiring
it of every new row — the exact pattern 003 used for `forecast_sha256` (`ALTER TABLE ... ADD COLUMN`
cannot add `NOT NULL` without a default, and no default is honest for an identifier nobody minted for
a pre-existing row). Concretely: an orchestrator mints `attempt_id` once, before the first research
call, for the whole campaign toward one forecast version; every `pipeline_failure_events` row logged
while pursuing that version cites the same `attempt_id`; if the campaign eventually succeeds, the
`forecast_records` row it produces is stamped with that same `attempt_id` at `INSERT` time. "The
forecast version that later succeeds" a given failure is then a plain equality join
(`pipeline_failure_events.attempt_id = forecast_records.attempt_id`), not an inferred one over
question/tournament and timestamps — which matters, because a question can have several independent
retry campaigns and several successful versions over its lifetime, and an inferred join has no
principled way to pair the right failure with the right success.

Two triggers close the loop from both directions: `pipeline_failure_events` refuses a new row whose
`attempt_id` already has a `forecast_records` row (an attempt that already succeeded cannot also
fail — terminal both ways), and refuses a row whose `attempt_id` was previously used for a *different*
`question_id`/`tournament_id` (identity stability — the same reasoning 003 applies to
`resolution_events` citing the wrong question). `forecast_records`' new trigger clause mirrors the
second check from its own side. A partial `UNIQUE INDEX (attempt_id) WHERE attempt_id IS NOT NULL`
on `forecast_records` enforces that one attempt succeeds at most once.

### Rejected — an append-only link table instead of the column, and why not

An `attempt_forecast_links` table written once, after the fact, by whoever creates the successful
`forecast_records` row. Rejected because it adds a second place to look for the same fact
`forecast_records.attempt_id` already states directly, with no offsetting benefit — the link table
would still have to be written atomically with the `forecast_records` INSERT (same transaction, same
ordering constraint), so it does not relax anything decision 2 needs, and it is one more append-only
table whose own integrity (what stops two link rows citing the same successful record, or a link row
citing a nonexistent attempt) has to be independently reasoned about. A column with a `UNIQUE`
partial index says the same thing with one fewer table.

### Decision — M1-606 imposes the mandatory `attempt_id`, not M1-602

**Owner decision, 2026-08-06, taken explicitly because this is the one place this item reaches
outside its own surface.** 004 extends 003's `forecast_records_require_draft_on_insert` so that
*every* `forecast_records` INSERT must carry a non-blank `attempt_id`. That is a writer contract for
a table M1-602 owns, and M1-602 is `Not Started`. The question is whether M1-606 should be setting
it. It should, on three grounds:

- **An optional link is unrepairable, not deferrable.** `forecast_records` is UPDATE-blocked (003,
  D25). A row admitted without an `attempt_id` can never be given one afterwards, so every failure
  recorded against that campaign is orphaned permanently. The cost of being wrong is asymmetric:
  requiring it too early costs M1-602 one column in an INSERT it has not written yet; requiring it
  too late costs a hole in the ledger that no later migration can close.
- **The precedent is exact and already settled.** 003 added a required `forecast_sha256` to
  `forecast_records` through this same trigger, before M1-602 existed, for the same reason (approval
  binds to a hash; a record with none is unapprovable). M1-602 picks up an already-required column
  either way; this makes it two rather than one.
- **The affected population is empty.** No production code writes `forecast_records` today — only
  tests, via raw SQL. The `NULLable`-column carve-out is a formality for a population of zero, not a
  live migration.

CLAUDE.md's stricter-reading rule covers the residual ambiguity in acceptance criterion 2: "the
failed attempt is linked to the forecast version that later succeeds" reads either as "there is a
column for the link" or as "the link exists for every success". The second is stricter and is what
shipped.

**What M1-602 inherits, stated so it is not discovered:** its `forecast_records` writer must mint or
accept an `attempt_id` and pass it at INSERT time, non-blank; it cannot add one later. The
`pipeline_failure_events` side is already written and needs nothing from it.

### Rejected — deferring the requirement to M1-602, and why not

Keep 004's column, its partial `UNIQUE` index and its identity-stability clause, but drop only the
`attempt_id is required` clause, leaving M1-602 to add it in a later migration when it writes the
real writer. This is the narrower change and it was seriously considered: it keeps each item inside
its own table.

Rejected because the window is not free. Between 004 landing and M1-602 landing, any
`forecast_records` row written — by a test fixture, an operator's manual insert, an M1-604 export
fixture — is admitted with a NULL `attempt_id` and is then permanently unjoinable, on a table whose
whole point is that it cannot be corrected. The item's own acceptance criterion would be satisfied
only in the sense that a column exists. Deferring an enforcement onto an append-only table is not
the same kind of deferral as deferring a feature.

### Decision — `detail_code` is `FailureCode` minus the two refetch codes

`pipeline_failure_events.detail_code` reuses `lifecycle.FailureCode`'s vocabulary rather than
inventing a parallel one, minus `refetch_mismatch`/`refetch_missing` — both describe what a refetch
saw of an *already-posted* forecast (M2-704's contract in `submission_verifications`), which cannot
occur before generation has even succeeded once. A new `Literal`, `PreForecastFailureCode`, spells the
ten remaining members out explicitly rather than deriving them, the same style `PipelineFailureEvent`
already uses for `record_failure`'s one-member vocabulary — a spelled-out list is what keeps a future
`FailureCode` addition from silently becoming reachable here before anyone decides it should be.

### Deviation — 004's blank-identifier test is 003's pinned character set, not `trim()`

The first draft of this migration wrote `trim(NEW.attempt_id) = ''`, and self-review before round 1
found it accepts `'\t\n'`. **That is M1-603's round-5 defect, reproduced in a new migration by
copying the idiom from before its fix.** SQLite's one-argument `trim()` strips U+0020 and nothing
else, so tabs, newlines and NBSP pass straight through it. 004 now spells out the same 29 codepoints
003 pinned — the set where Python's `str.isspace()` is true — at every place that defines "blank"
for an `attempt_id`.

Three things came out of chasing it, and all three are in the shipped code:

- **`pipeline_failure_events.attempt_id`, `tournament_id` and `question_id` had no shape guard at
  all.** The first draft guarded only the `forecast_records` end. The failure table accepted `''`,
  `'  '` and a BLOB `attempt_id` — and a blob is worse than blank, because
  `_pre_forecast_failure_from_row`'s `_stored_text` refuses it on the way out: an append-only row
  that can never be read back. Both ends of a join key have to agree on what a valid key is, or a
  failure can be recorded under a value no `forecast_records` row is permitted to claim.
- **`typeof()` on an affinity column catches less than it looks like it does.** `'1'` bound to
  `event_seq` (INTEGER affinity) reaches the trigger already converted to integer 1 and is accepted
  — correctly, since what lands is a genuine integer. What the clause actually catches is what
  affinity *cannot* convert: `'x'`, `1.5`, a blob. Same on the TEXT side: `42` becomes `'42'`, and
  the clause earns its place by rejecting blobs. The tests assert the **stored type** for the
  coerced cases, because a test that fed `'1'` expecting a refusal would have passed against a
  trigger with no `typeof()` clause at all — green for the wrong reason.
- **The writer had the same gap.** `_require_text` refuses `''` but `'\n\t'` is truthy and reached
  storage through it. New `_require_identifier` adds the `str.strip()` blank test, and a test
  asserts the writer's definition and both triggers' agree over every codepoint Python calls
  whitespace — the drift guard 003 wrote for the same reason.

**`_require_identifier` is scoped to this item's writer rather than folded into `_require_text`.**
The older identifier columns (`record_id`, `submission_attempt_id`, `idempotency_key`) have never
had a blank guard, so widening the shared validator would change what already-shipped, already-
reviewed writers accept — a behaviour change to merged code, smuggled in under a new item. That is a
**backlog candidate, not a fix here**, and the same call M1-308 made when it found `config.py`'s
copy of its YAML hole (filed as M0-007). Filed as **M1-607**.

### Deviation — every guard was mutation-checked, and three tests failed the check

A trigger test that stays green with its trigger removed is testing nothing, and M1-303 shipped
three properties out of ten that passed against the pre-fix code. So each of 004's 16 `RAISE(ABORT)`
clauses, both append-only block triggers and the partial `UNIQUE` index was removed one at a time
with the suite re-run against each — **18/18 are load-bearing for at least one test** — and eight
deliberate breakages were injected into `lifecycle.py` the same way. What the exercise actually
found is the point of recording it:

- **`test_one_attempt_succeeds_at_most_once` passed with the index deleted.** Both its records used
  question 100, which collides with 001's `UNIQUE (question_id, tournament_id, forecast_version)`,
  and `match="UNIQUE constraint failed"` cannot tell two unique constraints apart. Now uses a
  different `question_id` and matches `forecast_records.attempt_id`.
- **The only-`LifecycleError` property caught neither of the two writer holes its docstring claimed.**
  `_insert`/`_fetch_*` wrap `sqlite3.Error` *and* `OverflowError`, so a validator hole just moves the
  refusal from the writer to the database without changing the exception type. The invariant holds;
  the property simply cannot see it. `test_a_refused_write_is_refused_with_a_field_level_message` is
  the property that can — for inputs the writer is contracted to judge itself, the refusal must not
  be `_insert`'s opaque fallback and must name the field. That is M1-303's round-4 lesson (refuse
  caller mistakes before reaching the expensive layer) written as a property. It catches 7 of the 8
  injected breakages between it and its siblings; the eighth, dropping the writer's prior-success
  probe, is caught by `test_the_writer_refuses_an_attempt_that_already_succeeded` in the unit suite.
- **Two mutation results were unsound and had to be re-run.** Deleting the single statement from a
  block trigger leaves `BEGIN END;` — a syntax error, so the migration fails to apply and every test
  goes red for the wrong reason. Removing the whole `CREATE TRIGGER` is the sound mutation. A green
  mutation result is only evidence if the mutant still runs.
- **The first version of the field-level property was wrong about its own strategy**, drawing
  `question_id=42` — a valid question id — from a bogus-value set shared across fields. Hostile
  inputs have to be relative to the field they target or the test is testing the strategy.

Every docstring that names a breakage now names one that was **observed** failing, not one that
seemed likely. Two docstrings state, explicitly, a breakage the test does *not* catch and which
sibling does.

**A note for whoever runs this next.** The sweep first appeared to hang: every test builds a SQLite
ledger under `tmp_path`, and on ext4 the journal commits dominate — 3m55s of wall clock for 6s of
CPU, with pytest sitting in `D` state in `jbd2_log_wait_commit`. `--basetemp=/dev/shm/...` puts the
same databases on tmpfs and the same run takes 2.8s. That is an 85x difference on identical tests,
and it is what makes mutation testing practical here at all.

### Deferred (do not read the absence as an omission)

- **The `lifecycle_events` ∪ `pipeline_failure_events` merged read/export view → M1-604.** This item
  ships `read_pipeline_failure_events(conn, attempt_id)`, a direct per-attempt reader, the same
  granularity `read_history()` offers for `lifecycle_events`. Joining the two into one chronological
  view of "everything that happened toward this question" is M1-604's `show`/export seam to build.
- **No orchestrator wiring.** Nothing yet mints an `attempt_id` or calls `record_pre_forecast_failure`
  in production code — M1-306 (research retrieval), M1-602 (the `forecast_records` writer) and T-903
  (the dry-run acceptance test that would exercise the whole path) are all `Not Started`. This item
  ships the ledger primitive and its schema contract only, tested directly against hand-built rows,
  the same posture 003 shipped in before M1-602 existed to call it.
- **Calendar/format validity of `occurred_at_utc`/`created_at_utc`.** Rendered by the same
  `_require_utc`/`_utc_text` this module already uses for `lifecycle_events`; 003's M1-NOTES entry on
  this (non-blocking, round 5) applies unchanged and is not re-litigated here.

### Review round 1 — the schema had no ceiling matching the reader's

Cross-model review of `1ef9661` returned one blocking finding, and it was correct. Reproduced by
execution against that exact HEAD before any fix code was written:

```
INSERT a pipeline_failure_events row with a 201-character attempt_id via raw SQL -> succeeds
read_pipeline_failure_events(conn, that attempt_id)                              -> LifecycleError:
    attempt_id is longer than the 200-character limit
```

Both new `BEFORE INSERT` triggers guarded blankness and type but not length, while
`_require_identifier` (the writer's own gate, and the reader's) refuses anything over
`_MAX_IDENTIFIER = 200`. So raw SQL — an intended schema-enforcement boundary in this branch, the
same one the whole 004 truth table is driven against — could admit a row the public reader could
never retrieve, on a table that is append-only by construction: unlike a validation gap on a mutable
table, there is no later UPDATE that repairs it.

**Fix.** Both triggers (`pipeline_failure_events_validate_on_insert`'s `attempt_id` clause and
`forecast_records_require_draft_on_insert`'s) gained `OR length(NEW.attempt_id) > 200`, matching the
writer's ceiling exactly — the same "the two tables must not disagree" reasoning the blank-identifier
guard was already built on, extended from blankness to length.

**Tests.** `test_a_failure_attempt_id_over_200_characters_is_refused` and
`test_a_record_attempt_id_over_200_characters_is_refused`
(`tests/unit/test_lifecycle.py`) pin 201 refused and 200 accepted at both ends, and the first also
asserts the 200-character row round-trips through `read_pipeline_failure_events` — closing the loop
the finding named, not just the insert. Re-run against the round-1 code (fix reverted), both fail on
exactly the reviewer's counterexample.

### Review round 2 — `length()` does not count what Python counts

Cross-model review of `1b533c1` (round-1's fix) returned one blocking finding: B1 reopened through a
different vector. Reproduced by execution against that exact HEAD before any fix code was written:

```
attempt_id = "a\x00" + "b" * 200                     # 202 characters in Python
sqlite length(attempt_id)                            -> 1
INSERT via raw SQL                                    -> succeeds (length() guard sees 1, not 202)
read_pipeline_failure_events(conn, attempt_id)        -> LifecycleError:
    attempt_id is longer than the 200-character limit
```

SQLite's `length()` on TEXT stops counting at an embedded NUL rather than counting the full stored
string, so the round-1 `length(NEW.attempt_id) > 200` guard cannot see past one: a 202-character
identifier with a NUL at position 2 reads as `length() == 1` and passes, while `_require_identifier`'s
Python `len()` sees the true 202 on read-back and refuses it — the exact unreadable-append-only-row
failure round 1 closed, reopened through the one input the two counting functions disagree about
rather than through a bigger number.

**Fix.** Rather than chasing SQLite's counting semantics to match Python's, U+0000 is refused
outright wherever an `attempt_id` is validated: `_require_identifier` now raises on any NUL, and both
migration triggers gained `OR instr(NEW.attempt_id, char(0)) > 0` — `instr()` finds the NUL directly
(verified by execution: `instr()` returns 2 for the counterexample above while `length()` still
returns 1), so it does not depend on a length count either. Removing the one input class the two
layers disagree about closes the mismatch structurally instead of adding a second special case.

**Tests.** `test_a_record_attempt_id_with_an_embedded_nul_is_refused` and
`test_a_failure_attempt_id_with_an_embedded_nul_is_refused` (`tests/unit/test_lifecycle.py`) pin the
reviewer's counterexample refused at the raw-SQL boundary on both tables, and the second also asserts
`record_pre_forecast_failure` refuses a short NUL-bearing identifier through the writer. Re-run against
the round-2 code (fix reverted), both fail exactly as the reviewer's reproduction predicts.

### Standing risk — not verifiable offline

The `attempt_id`-sharing contract (decision 2) is an interface promise to code that does not exist
yet: nothing today constructs the "mint once, reuse across retries, stamp on success" sequence
end-to-end, so its only exercise is this item's own tests inserting hand-built rows in the shape a
real orchestrator would produce. That is the same position 003 was in before M1-602, and the same
answer applies — the schema and its triggers are the enforcement regardless of who calls them, and
`tests/unit/test_lifecycle.py` drives the truth table directly rather than through a caller that does
not exist.

## M1-308 — Account allowlist loader

New `research/allowlist.py`: `AllowlistEntry`/`_AllowlistFile` (pydantic, `extra="forbid"`),
`AllowlistError`, `AccountAllowlist` (`@dataclass(frozen=True)`) with `lookup_by_username` and
`match_domain`, and `load_allowlist(path)` mirroring `config.load_config`'s read/parse/validate
flow (same YAML-error handling, same path-is-the-carve-out rendering). `reliability_tag` reuses
`research.model.ReliabilityTag` rather than restating it, per that Literal's own comment.
Username uniqueness is case-insensitive and its violation message names only account indices,
never the colliding username.

Wired into `env_verify.py` as `_verify_account_allowlist`, run in `verify_environment()` right
after `_verify_referenced_files` — gated on the file already existing (skip so a missing file
reports one problem, not two, matching `_verify_prompt_version`'s convention). This is the
"at startup, not at retrieval time" surface the acceptance criteria asks for; no `config.py`
change was needed since `SocialRetrievalConfig.account_allowlist_path` already existed.

**Round-2/3 cross-model review fixes.** The first cut gated this check on
`retrieval.social.enabled` — with the committed default (`false`), a malformed allowlist was
never checked, and `questions fetch` (the only other config-consuming command) called
`load_config()` directly and never ran `verify_environment()` at all, so even an *enabled*
malformed allowlist reached nothing that validated it. Fixed by extracting
`load_and_verify_account_allowlist(config)`, unconditional on `enabled`, and adding
`cli._load_verified_config()` as the boundary every config-consuming command must call instead
of raw `load_config()`. Separately, `AllowlistError` gained `is_filesystem_error` (set only on
the `read_bytes()` failure) so an unreadable file classifies as a filesystem/`EXIT_ENV_MISSING`
problem rather than a config/`EXIT_CONFIG_INVALID` one, and that same read-failure raise switched
from `from exc` to `from None` (matching `prompt.py`'s identical translation) so a raw `OSError`
no longer rides along as `__cause__`.

**Round-4 cross-model review fix.** Round 3 closed the hole for *malformed* allowlists and left it
open for *absent* ones: `load_and_verify_account_allowlist` returned `None` whenever
`path.is_file()` was false, regardless of `enabled`, and `cli._load_verified_config` discarded that
return — so `retrieval.social.enabled: true` plus a nonexistent `account_allowlist_path` started
`questions fetch` clean and exited 0, deferring the failure to retrieval. The docstring justified
the skip by delegating absence to `_verify_referenced_files`, which was the false part: that
function only runs inside `verify_environment()`, which `questions fetch` never calls. Now the
helper skips in exactly one case — disabled *and* absent — and otherwise calls `load_allowlist`,
which is deliberately left to raise rather than building a second error here: its `OSError` branch
is already sanitized, `from None`-chained and `is_filesystem_error=True`, and its `strerror` stays
accurate across every case `is_file()` collapses into one answer (absent, a directory, a dangling
symlink, unreadable) — an explicit "does not exist" raise would mislabel three of those four.
`_verify_referenced_files` gave the allowlist up in the same change, or `verify-env` would print
two lines for one missing file; `tests/unit/test_env_verify.py::test_enabled_social_with_missing_
allowlist_is_a_filesystem_problem` asserts `len(filesystem_problems) == 1` to hold that.

Left alone deliberately, and stated in the round-4 review response: `_load_verified_config`
discards the loaded `AccountAllowlist`, so M1-307 will re-parse — worth wiring where a consumer
exists, not speculatively here; `_load_verified_config` remains enforced by convention rather than
by a type, adequate at two commands; and `account_allowlist_path` stays CWD-relative
(`load_config` never rebases it onto the config file's directory), which this fix makes *loud*
when enabled instead of a silent skip, but re-resolving it is a config-contract change.

**Deliberate scope boundary (owner-confirmed):** `domains` stays free-form `list[str]`, validated
only for non-emptiness (list and per-element). The 19-tag taxonomy documented in
`config/x_accounts.yaml`'s header comment is *not* enforced as a closed set in code — the
acceptance criteria only ask for "non-empty domains", and a code-level taxonomy constant would be
a second source of truth that could drift from the comment. Only `reliability_tag` gets closed-set
enforcement (via the existing `ReliabilityTag` Literal).

Verified `match_domain` against the real, committed `config/x_accounts.yaml` for both example
domains in the acceptance criteria: `econ_data` → `{BLS_gov, BEA_News, stlouisfed, StatCan_eng,
ONS, EU_Eurostat, IMFNews, EIAgov, Reuters}` (9 accounts), `space_launch` → `{NASA, SpaceX, esa,
RocketLab, ulalaunch}` (5 accounts).

Property suite (`tests/property/test_allowlist_properties.py`) fuzzes the validation layer
directly on parsed dicts rather than through file I/O (already covered by the unit tests) —
same split `test_canonical_properties.py`/`test_dedup_properties.py` use. The "no value leak"
property is covered deterministically instead of by the fuzzer: every raise in this module is
either a pydantic error rendered with `include_input=False` or one of the module's own
constant-shaped, index-only messages, so leak-freedom doesn't depend on which value was
generated — `tests/unit/test_allowlist.py::test_no_field_leaks_a_planted_secret_through_any_message`
plants a fixed secret across every field and shape instead. `AllowlistEntry` is not hashable
(plain `_StrictModel`, not frozen), so the subset property in `test_match_domain_result_is_a_
subset_and_deterministic` checks identity membership rather than using `set()`.

**Round-5 cross-model review fixes.** Two P2 findings, both reproduced before anything changed.

*1 — `verify-env` was importing the whole provider stack.* `env_verify.py` imports
`research.allowlist`, and importing any submodule executes the package `__init__.py`, which
re-exported from `research.asknews` → `metaculus.client` → `forecasting_tools`. Measured in a
fresh process: `import whiskeyjack_bot.env_verify` took **7.0s** and printed a Metaculus-token
warning, a model-cost warning and a Streamlit cache warning into the output of the one command
whose entire job is to report cleanly on the environment. Fixed by reducing
`research/__init__.py` to a one-line docstring — which is what CLAUDE.md's conventions already
prescribe ("Subpackages get a one-line-docstring `__init__.py`"); `research/` was the only
subpackage that deviated, and the deviation *was* the coupling. Nothing in `src/` imported from
the package (every internal import was already submodule-level); the five test files that did now
import from the owning submodule. Import is now **0.204s** with no output. The lazy-`__getattr__`
alternative was rejected: it keeps a re-export surface no `src/` module uses, at the price of an
importlib indirection, an `-> Any` escape hatch under `mypy --strict`, and a name→module table to
keep in step with `__all__`. `research.asknews` itself is as costly as before, correctly — the
AskNews adapter needs that stack; `verify-env` never did.

Guarded by `tests/unit/test_env_verify.py::test_startup_module_does_not_import_provider_sdks`,
which must run **in a subprocess**: inside pytest the SDKs are already in `sys.modules` from the
adapter suites, so an in-process assertion would pass for the wrong reason. A companion test
imports `research.asknews` through the same probe and asserts it *does* report both SDKs — a
negative test with a misspelled module name or a marker that never prints is a test of nothing.

*2 — Surrounding whitespace defeated username validation, uniqueness and lookup.* `" BLS_gov "`
passed the non-blank check, stored padded, counted as **distinct** from `"BLS_gov"` for the
case-insensitive uniqueness check, and was then unreachable through
`lookup_by_username("BLS_gov")`. That fails *open*: M1-307 finds no match and applies the
`unverified_social` default to an account the operator believed was tagged `official_primary`.
Fixed by validating `username` against the actual X handle rule — `[A-Za-z0-9_]{1,15}` via
`re.fullmatch` (never a `$` anchor: `$` also matches before a trailing newline, so `"BLS_gov\n"`
would pass — the same greedy-anchor trap M1-401 hit). Owner decision to take the charset over a
whitespace-only check: `username` is not free text but the key both accessors and the uniqueness
check use, and one predicate closes the whole class — padding, interior spaces, a leading `@`, a
zero-width character — instead of the one reported instance. Verified to accept all 46 committed
entries unchanged. `domains` has the identical hazard (`match_domain` compares exactly, so
`" econ_data "` matches nothing) and gets the whitespace half only, since it stays free-form.

Neither accessor normalizes its argument: validation is strict at load, and stripping at query
time would be a second contract `lookup_by_username` and `match_domain` would each have to keep
in step with.

The property suite gained the invariant whose absence let this through — and getting it right
took three attempts worth recording, since the first two would have shipped as false assurance:

- Stated as a round trip (`lookup_by_username(entry.username)`), it **passes on the broken code**:
  looking an entry up by the exact bytes it was stored with succeeds even for `" BLS_gov "`. The
  property has to be stated against the key *a caller actually has* — the normalized form.
- The existing `_entry_payload` fuzzes hostile text into every field and adds a forbidden extra
  key half the time, so only **1 in ~300** of its allowlists validates. Any property of the form
  "an accepted allowlist guarantees X" was therefore asserting nothing. Added
  `_plausible_payload`, weighted 7:1 toward valid values per field, because validity compounds
  across twelve values in a three-entry file.
- The companion uniqueness property (no two accepted entries collide once normalized) still
  passed against the pre-fix validator at 800 examples, because it needs two entries whose
  usernames differ *only* by padding and independent 15-character draws never collide. Usernames
  for that strategy now come from an eight-handle pool.

Both properties were then re-run against the pre-fix validator and **fail** there, which is the
only evidence that either is worth having. The same check is why `_mostly` uses a named picker
rather than an inline lambda: hypothesis prints the strategy's callable in every counterexample.

*Cross-track note:* M1-303 (PR #16) added Exa names to the re-export block this change deletes.
It merged first, and master's `origin/master` merge into this branch conflicted here exactly as
expected; resolved by dropping the added block and keeping the one-line docstring. No test import
needed repointing after all — M1-303's `tests/unit/test_exa.py` already does
`from whiskeyjack_bot.research import exa`, a *submodule* import, which resolves through the
package regardless of what `__init__.py` re-exports. A grep of the merged tree confirms it is the
only `from whiskeyjack_bot.research import ...` left in `src/`, `tests/` or `scripts/`.

**Round-6 cross-model review fixes.** (This file numbers *fix* rounds; the commit messages number
the *review* round that raised the findings, so this section is `close round-5 review findings` in
the log. The two sequences have been one apart since the round-2/3 section and are left that way
rather than renumbering shipped history.) Two blocking findings, both reproduced first.

*1 — `_sanitize` leaked integer YAML keys.* It rendered every `int` in a pydantic `loc` tuple as
if it were a list index. But under `extra="forbid"` the location of an unexpected key **is** that
key, and pydantic's `invalid_key` error puts it in `loc` — so an unquoted numeric key, which YAML
parses as an `int`, came straight back out: `987654321: Keys should be strings`, and
`accounts.0.424242: ...` for the nested case. A digits-only value is exactly the shape of an
account id or a numeric token, so this is the leak rule's own failure mode, not a technicality.
Fixed by making the discrimination positional: an `int` renders only when the preceding `loc` part
names a list-valued field, tracked in `_SEQUENCE_FIELDS` — derived from each model's `model_fields`
annotations (`get_origin(...) is list`) rather than hardcoded as `{"accounts", "domains"}`, so a
field added later cannot silently start rendering, and a later `list[str] | None` (origin
`UnionType`) drops out and over-redacts, which is the fail-safe direction. Withholding *every* int
would also close the finding and was rejected: `accounts.<withheld>.username` cannot be acted on
against a 46-entry file, and an index under a list field is schema-authored, not file content.
String parts need no positional test — a part that is not a declared field name is withheld
outright, which already covered unknown *string* keys.

*2 — the startup skip was `Path.is_file()`, which answers False for far more than absence.* The
contract is "skip only when social is disabled *and* the file is absent", but `is_file()` returns
False for a directory, a dangling symlink and any stat failure exactly as it does for a missing
file — so with the committed default (`enabled: false`), an `account_allowlist_path` pointing at a
directory, at a broken symlink, or inside an unsearchable parent started clean at both entry
points. Fixed with `_nothing_exists_at()`: `os.lstat`, and only `FileNotFoundError` counts as
absence. `lstat` not `stat` on purpose — a dangling symlink *is* an object at the configured path,
and following it would relabel an operator's broken link as "optional file not present". Every
other condition falls through to `load_allowlist`, whose `OSError` branch already yields a
sanitized, `from None`-chained, `is_filesystem_error=True` error carrying the real `strerror`.

The docstring now carries the full eight-row truth table, and — because this hole has moved in
three consecutive rounds (round 3: enabled-and-malformed; round 4: enabled-and-absent; round 5:
disabled-and-not-a-regular-file), each round having tested only the case it had just been shown —
the table is now enumerated mechanically in `tests/unit/test_env_verify.py` at **both** entry
points (`verify_environment` and `cli._load_verified_config`), with "anything else" expanded into
the three shapes that reach it. `enabled` buys exactly one thing: permission for the file to be
*absent*. The named regression tests are kept alongside it; they carry the *why*, the table carries
the completeness.

Two test-rigor defects found while checking those fixes, both in the same class as the round-5
property lessons above:

- The new non-string-key property was written as `try: ... except AllowlistError:` with no
  assertion on the non-raising path, so any draw that validated would have passed while proving
  nothing. Now `pytest.raises`; every key shape it draws (int, float, bool, `None`, date) is in
  fact rejected as `invalid_key`, so a passing validation is a schema regression, not a case to
  skip.
- `test_disabled_social_with_missing_allowlist_is_not_a_problem` asserted the skip is silent with
  `"allowlist" not in report.render()` — but every path line in the render contains `tmp_path`,
  which pytest names after the test, and this one passes only because pytest truncates the name to
  30 characters *before* reaching "allowlist". Copying the assertion into a shorter-named test
  failed immediately. All of these now key on the check's own wording (`loads clean`, and the
  loader's `invalid account allowlist` prefix) rather than on a word that a temp path can supply.

Every test added this round was re-run against the pre-fix `src/` and **fails** there — including
all three disabled/non-regular rows of the new table at both entry points. The `enabled` rows pass
pre-fix, correctly: the guard short-circuited on `enabled`, so that half of the table was already
right.

Non-blocking, and answered rather than changed: deleting the `research/__init__.py` re-exports does
break a hypothetical external `from whiskeyjack_bot.research import ResearchDocument`. Nothing in
`src/` or `tests/` imports from the package, the package is not published, and a
one-line-docstring `__init__.py` is what CLAUDE.md's conventions prescribe — a compatibility shim
would reinstate the import coupling the finding was about. The PR description was also stale
(still describing the check as gated on `retrieval.social.enabled`, which round 3 made false) and
has been rewritten.

No migration, no new dependency, no `docs/TRACKS.md` change (already claimed with `none`/`no`),
no wiring into M1-307 (doesn't exist yet on this branch).

**Round-6 review — answered with a rebuttal, no source change.** The review restated the round-5
findings against a tree that predates the head its own request embedded; both were already closed.
Recorded here because it is why the fix-round and review-round numbers stop tracking each other
from this point, and because it is the case that put "diff the commit a pasted review names against
`HEAD` and reproduce by execution before writing any fix code" into CLAUDE.md.

### Round-7 review — one blocking finding, reproduced and widened

Reviewed commit `7deeb2c`, which *was* `HEAD`; both prior findings closed. The finding: PyYAML
constructor failures escape `load_allowlist` as a raw `ValueError`, against the project rule that
every malformed shape arrives as the module's own error type. Reproduced end-to-end before writing
anything — `whiskeyjack-bot verify-env` exits with an unhandled traceback through
`allowlist.py:297` under the committed default `retrieval.social.enabled: false`.

Reproducing it also widened it. Only PyYAML's scanner, parser and composer raise `YAMLError`; the
*constructor* stage raises whatever Python raised at it, so the escape is a class, not one shape.
All six of these come from a one-line edit to an otherwise-valid allowlist:

| content | escapes as | leaks the value |
| --- | --- | --- |
| `display_name: 2026-02-30` | `ValueError('day is out of range for month')` | no |
| `display_name: 2026-01-01 12:60:00` | `ValueError('minute must be in 0..59')` | no |
| `notes: !!bool maybe` | `KeyError('maybe')` | **yes** |
| `notes: !!int abc` | `ValueError("invalid literal for int()…: 'abc'")` | **yes** |
| `notes: !!timestamp bogus` | `AttributeError` | no |
| `domains: [[[…2000 deep…]]]` | `RecursionError` | no |

`AttributeError`/`KeyError`/`ValueError` is exactly the trio CLAUDE.md names, and two of the six
put file content in the message — so this is a secret-hygiene leak channel as well as a raw-type
escape, which the finding as written did not reach.

**Decision — the catch is "not a `YAMLError`", not a list of types.** The review's minimal fix
(catch `ValueError`) closes three of the six; `!!bool <junk>` and `!!timestamp <junk>` — the
leaking one and the `AttributeError` one — survive it. An enumerated tuple is not better in kind:
the enumeration belongs to PyYAML, and a shape missing from it escapes raw, which is how this
reached a review in the first place. What is actually known at that line is the contract — nothing
but a parsed document may leave `yaml.safe_load` — so `except Exception` around **only** that call
is the accurate statement of it. Same reasoning as the username charset predicate two sections up:
close the class, not the reported instance.

**Rejected — a custom `SafeLoader` with the implicit timestamp resolver removed, and why not.** It
would make `display_name: 2026-02-30` a plain string instead of an error, which is arguably nicer
for a curated file. It also changes *what the file accepts*, silently diverges from `config.py`'s
loader, and leaves every explicit-tag shape untouched. Translating the failure is the smaller
contract change and the one the acceptance criteria imply.

**Rejected — catching at the caller.** `env_verify`/`cli` already handle `AllowlistError`; adding a
second handler there would have to be repeated at every future entry point and would put the
sanitizing decision outside the module that owns the error type.

Tests, and which of them actually discriminate:

- Six parametrized cases through `load_allowlist`, plus the leak check on the two tagged shapes.
  All seven **fail against the pre-fix loader**; that is the evidence.
- `test_each_constructor_case_still_escapes_pyyaml_untranslated` passes both ways *by design* — it
  asserts against PyYAML, not against us, so that if a future release turns one of these into a
  `YAMLError` the case stops silently testing a branch that was already there. The round-6
  permission test had gone vacuous exactly that way.
- `test_a_valid_implicit_timestamp_is_still_a_schema_error` also passes both ways by design:
  `2026-02-28` constructs into a `datetime.date` and must still reach pydantic, so the new branch
  is a translation and not a mask.
- The property suite gained its **first deliberate exception** to the after-the-parse split its
  header describes. That split assumed the parse either succeeds or raises `YAMLError`, and the
  assumption *was* the finding — the defect lives in the transition into a dict, which cannot be
  fuzzed from one. Two properties now drive `load_allowlist` over generated YAML text (only
  `AllowlistError` escapes, and a drawn scalar never reaches the message); both fail pre-fix. The
  file is written through a **module-scoped** `tmp_path_factory` fixture: `@given` with the
  function-scoped `tmp_path` is a hypothesis health-check failure.

**The round-7 request overstated its own test coverage**, and the review caught it: it claimed a
mid-read `OSError` and descriptor cleanup were exercised, and nothing in `tests/` patched
`os.read`. Now real — `test_mid_read_failure_is_a_filesystem_error_and_closes_the_descriptor_once`
wraps `os.open`/`os.read`/`os.close` keyed on the one descriptor the load opens (wholesale patching
breaks pytest's own capture), and asserts `intercepted` alongside the outcome. Mutation-checked
rather than assumed: deleting the `finally: os.close(fd)` makes it fail with `descriptor closed 0
times`.

### Deferred (do not read the absence as an omission)

`config.py:363` has the identical hole — `load_config` catches only `YAMLError`, and
`KeyError('<value>')` out of the primary config file is the worse leak of the two. It is on the
diff base, so per the review contract it is a backlog row, not a fix in this branch: **M0-007**,
filed with the reproduction and acceptance criteria. Widening this branch into the primary config
loader would hand a round-8 reviewer a second file to audit for a defect that predates the item.

## M1-310 — The terminal DNS root dot in the canonical URL

Acceptance: *either `canonicalize_url` normalizes one terminal root dot and the identity change is
recorded, or the dot is documented as identity-bearing with a rationale; dedup collapses the two
spellings of one host iff that decision says it should.*

The criterion offers two answers and asks for the reasoning either way. This note is that
reasoning; the code implements the first.

### What the criterion is actually guarding against

`canonicalize_url` derives the `canonical_url` that the ledger keys dedup on —
`UNIQUE (retrieval_run_id, canonical_url, content_sha256)` (`001_initial.sql:73`). It preserved a
terminal root dot, so `https://bls.gov./x` and `https://bls.gov/x` — two valid spellings of one
DNS host, both accepted by the schema gate — were two dedup keys for one page.

PR #16's round-5 cross-model review found it through its *attribution* symptom rather than its
dedup one: an Exa result at `https://bls.gov./report`, returned under a `bls.gov` allowlist, was
labelled `source_type: web`, because the two spellings never compared equal in either direction.
That branch fixed the symptom locally (`exa._without_root_dot`) and explicitly declined to touch
canonical form, filing this item. The underlying duplicate-identity defect was untouched until now.

### Decision — one terminal root dot is normalized away (D32)

`canonicalize_url` strips exactly one trailing root dot from the host — **in any of the four
spellings UTS-46 maps onto `.`** (ASCII `.`, U+3002 `。`, U+FF0E `．`, U+FF61 `｡`; the non-ASCII
three were missed in the first implementation and added in review round 1, below) — so the
spellings produce one `canonical_url` and collapse to one dedup key. Four things decide it.

**1. The dedup key contains the content hash, so this cannot collapse two different pages.** The
one real argument for treating the dot as identity is that it is genuinely observable on the wire:
`Host:`, TLS SNI and cookie scope all differ between the two spellings, so a virtual-hosting stack
*can* serve different bodies at `bls.gov.` and `bls.gov`. That argument does not survive the shape
of the key. Two documents collapse only when `(retrieval_run_id, canonical_url, content_sha256)`
matches — same run, and **byte-identical content after the pinned normalization**. If the two
spellings really served different pages, the digests differ and both rows survive. What collapses
is one page fetched twice under two spellings, which is exactly the collapse dedup exists for.

**2. Attribution is not lost.** `original_url` holds the URL exactly as the provider returned it
(`model.py:288`, `002_research_document_fields.sql:41`), and both are stored. Canonicalization has
never been an attribution loss and this does not make it one: the as-retrieved spelling is still
in the ledger, and both columns are immutable after insert (`003_lifecycle_events.sql:989`).

**3. Nothing is stored yet, so the identity change is free now and expensive later.** No ledger
holds research documents; there has been no production run. This is the same class of change as
`hashing.py`'s pinned normalization rule, whose docstring requires any change to ship as a new
versioned function *because previously stored digests keep their old values*. That constraint is
about committed data, and there is none. **M1-306 is the deadline, not a complication**: its
acceptance is *"replay produces zero provider calls and the same research packet hash"*, and that
packet is built from documents keyed on `canonical_url`. Settling the dot after M1-306 lands means
changing an identity its hash has already been computed over — the same restatement problem, just
arrived at a milestone later. (Nothing in the M1-306 branch depends on this change today; it picks
it up on its next daily `master` merge.)

**4. The blast radius is one adapter.** `canonicalize_url` has exactly one production caller today
— Exa, at `exa.py:913` (allowlist entries) and `exa.py:988` (each result). AskNews still sets
`canonical_url = url` unmodified (`asknews.py:177`); that gap is M1-309 and is untouched here.

### Decision — UTS-46 mapping first, then the strip, then the IP-literal branch

`_canonical_host` runs `idna.uts46_remap(host, std3_rules=False)`, then strips one trailing ASCII
dot, then splits `ipaddress`/`idna`. The order is the whole correctness argument and each step is
where it is for a stated reason:

**The mapping is first because the separator is not only ASCII.** UTS-46 folds U+3002, U+FF0E and
U+FF61 onto `.`, and `idna.encode(uts46=True)` applies that mapping *itself*. The first
implementation stripped ASCII and then encoded, so for `https://bls.gov。/report` the strip ran
before the separator existed and IDNA then *created* a terminal dot that nothing removed. Review
round 1 found it; see that section below.

**The strip is before the IP/domain split** so `https://127.0.0.1./a` collapses to
`https://127.0.0.1/a` on the same rule as `https://bls.gov./x`. Putting it after the split would
have made the dot a domain-name special case and left the dotted IPv4 spelling as a second,
undocumented identity — one rule, applied where the host is decided, is the reason this module has
a single `_canonical_host` at all. The mapping running ahead of it is what makes
`https://127.0.0.1。/a` reach `ipaddress` as an IP literal at all rather than falling through to
the domain branch as `127.0.0.1.`.

**The separator table stays inside `idna`.** The alternative — a local `("." , "。", "．", "｡")`
tuple matched before the split — was rejected: it is exactly the speculative host transform this
module's header records losing to three times, and it puts a copy of a Unicode mapping in a module
whose stated rule is *delegate host classification to the authority*. `std3_rules=False` is passed
explicitly because it is `uts46_remap`'s non-default while being what `idna.encode` applies
internally; the two calls disagreeing about which codepoints survive is the only way this
decomposition can go wrong, so it is pinned rather than defaulted.

`uts46_remap` is verified not to disturb the IP path: `::1`, `2001:db8::1`, `::ffff:192.168.0.1`
and `127.0.0.1` all pass through unchanged. IPv6 cannot carry a root dot anyway — `urlsplit` hands
`::1` over unbracketed, and `::1.` fails `_require_http_url` before canonicalization runs.

### Decision — exactly one dot, and only after the gate has run

Only one trailing dot can ever reach the stripper, and this was verified by execution rather than
assumed: `https://bls.gov../x`, `https://.bls.gov/x`, `https://./x` and `https://..../x` are all
already refused by `_require_http_url`, because `idna` rejects an empty label
(`idna.IDNAError: Empty Label`). So there is no loop, no "strip while endswith" and no question of
what `bls.gov..` should mean — it means nothing and is refused, exactly as before.

Re-verified across the folded separators in round 1, since a widened fold could have widened the
accepted set: `https://bls.gov。。/x`, `https://bls.gov。./x`, `https://bls.gov.。/x`,
`https://。bls.gov/x` and `https://。/x` are all refused too, and as `CanonicalizationError`. A
double separator is an empty label whichever way it is spelled.

The strip therefore runs **after** the syntactic gate, never before it, and the accepted-input set
is unchanged by construction: canonicalization still accepts precisely what `_require_http_url`
accepts. A property asserts that rather than leaving it as a claim.

The `host != "."` guard in front of the strip is not defending against a reachable input — the gate
refuses `"."` — it keeps `_canonical_host` total on its own terms instead of on a promise made by
its caller, in the same spirit as the `parts.hostname or ""` beside it.

### Delivered

- `src/whiskeyjack_bot/research/canonical.py` — `_canonical_host` maps UTS-46 separators via
  `idna.uts46_remap`, then strips one terminal root dot; module and function docstrings state the
  rule and cite D32.
- `src/whiskeyjack_bot/research/exa.py` — `_without_root_dot` and both call sites **removed**; the
  two docstrings that asserted "`canonicalize_url` preserves a terminal DNS root dot … see M1-310"
  now record what M1-310 decided.
- `docs/backlog/decisions.csv` — **D32**, and the M1-310 backlog row references it.
- `tests/property/test_canonical_properties.py`, `tests/property/strategies.py`,
  `tests/unit/test_dedup_freshness.py`.

### Decision — M1-303's local workaround is retired, not layered

`exa._without_root_dot` existed only because canonicalization did not do this. Keeping it would
leave two normalization rules for one question, in two modules, able to drift — the precise failure
`canonical.py`'s "the gate is *reused*, not re-implemented" paragraph was written against, and the
reason the round-5 fix also routed allowlist entries through `canonicalize_url` instead of
lowercasing them locally. Worse, its docstrings assert as fact something this branch makes false;
a stale rationale beside dead code is how a later reader re-derives a decision that was already
made.

**The round-5 tests it was written for keep all their original cases unchanged** —
`test_a_terminal_root_dot_is_the_same_host` (its 5 parametrized pairs),
`test_the_root_dot_does_not_make_a_suffix_coincidence_a_match`,
`test_the_two_spellings_of_a_host_select_each_other` and
`test_the_root_dot_does_not_widen_a_suffix_coincidence`. Those cases passing against a deleted
helper is the evidence that the canonical rule subsumes the workaround; rewriting them alongside
the code would have destroyed that evidence.

Round 1 **added** cases to two of them rather than editing any — the Unicode separator pairs to
`test_a_terminal_root_dot_is_the_same_host`, and a separator parametrization to
`test_the_root_dot_does_not_make_a_suffix_coincidence_a_match`. That is the honest bookkeeping: the
original assertions still stand as written and still pass, and the additions are what the deleted
helper had also been covering and the first fix did not. See the round-1 section below — the
"subsumes the workaround" claim was true of the ASCII pairs and false of these, which is precisely
why deleting the helper was a regression rather than a cleanup.

`_validated_domains`' single-label refusal is unaffected: `gov.` canonicalizes to `gov`, and
`"." not in host` refuses it for the same reason it refuses `gov`.

### Rejected — documenting the dot as identity-bearing, and why not

The criterion's other branch. It fails on consequences rather than on principle: every consumer
that compares hosts would have to re-derive the strip locally — Exa's attribution already had to,
M1-306's replay hash would have to reason about it, and any future allowlist or citation view
would too. That is a rule maintained in *n* places by convention, which is what this module exists
to avoid. And it preserves a distinction with no consumer: nothing in the pipeline fetches
`canonical_url` (it is a dedup key and an attribution string; adapters fetch through their
provider), so the Host-header difference that makes the dot meaningful on the wire is never
exercised by anything we do.

### Rejected — a versioned `canonicalize_url_v2` alongside the current one

`hashing.py`'s pattern, and the right one *when digests are already committed to*. Here it would
buy nothing: there is no stored `canonical_url` to protect, so the branch would ship two live
canonicalizers, a choice at every call site, and a permanent question about which one a given row
was written under — the cost of the pattern with none of its benefit.

### Rejected — normalizing the dot at the comparison instead of in canonical form

`_matches_official_domain` could keep folding the dot at compare time (which is what it did). That
leaves the *stored* identity duplicated, which is the actual defect: two ledger rows for one page
under one run is a dedup failure whether or not any comparison later treats them as equal.

### Deferred (do not read the absence as an omission)

- **AskNews does not canonicalize at all.** `asknews.py:177` writes the raw URL into
  `canonical_url`, so no AskNews document is affected by this change in either direction. That is
  **M1-309**, filed off PR #16; folding it in here would widen an identity decision into an adapter
  rewrite.
- **Multi-label public suffixes** (`co.uk`, `com.au`) still over-attribute under Exa's subdomain
  rule. Stated in `exa.py` and unchanged: closing it needs a public-suffix list, a dependency, and
  `uv.lock` serializes tracks.
- **No schema, migration or dependency change.** Nothing about the column changes; only the value
  written into it, and nothing has been written yet.
- **Other canonicalization questions stay open on purpose** — case in the path, `index.html`,
  trailing slashes, `www.`, sorted query parameters. Each is a separate identity decision with its
  own risk of collapsing two real resources; this item was scoped to the dot and the stricter
  reading of a scoped criterion is to answer it and stop.

### Standing risk — not verifiable offline

A host that genuinely serves different content at `bls.gov.` and `bls.gov` cannot be discovered
without live traffic. The exposure is bounded by fact 1 above: such a host produces different
digests and two surviving rows, so the risk is not a wrong collapse but the reverse — two rows that
look like duplicates in a listing and are not. `original_url` distinguishes them, and is stored
precisely so that this is answerable after the fact.

Whether providers emit the dotted spelling often enough to matter is likewise unmeasurable here.
Round 5 saw it in a review scenario, not in captured traffic. If it turns out never to occur, this
change is redundant rather than wrong; if it occurs once, it would have been a duplicate ledger row
and a mislabelled source.

### On the property tests, and the check that they discriminate

Four new properties, each run against the pre-fix `canonical.py` first and confirmed **failing**
there — the M1-303 lesson, where 3 of 10 new properties passed on the broken code and proved
nothing:

| property | pre-fix result |
|---|---|
| the canonical host never ends in a dot | **fails** (`https://bls.gov./x`) |
| the two spellings of one host canonicalize identically | **fails** |
| two hosts canonicalize equal **iff** they are the same host | **fails** — see below |
| the accepted-input set is exactly `_require_http_url`'s | passes both ways — kept as the regression pin on "the strip runs after the gate" |

The third one was written expecting it to pass both ways, as the guard on the *other* direction
(normalizing must not turn a suffix coincidence like `notbls.gov` into a match). Run against the
pre-fix code it failed, because its **iff** form also covers the same-host case: `bls.gov.` and
`bls.gov` are one host, so the pre-fix canonicalizer answered "distinct" to a pair the property
says must be equal. Stated here because the difference between "I expected this to discriminate"
and "it does" is exactly what the pre-fix run is for, and the table was wrong until that run.

The fourth is kept knowing it passes both ways: it pins where the strip runs (after the gate, so
the accepted-input set cannot move) rather than what the strip does.

The paired-spelling strategy draws the two spellings **independently**, for the reason
`test_the_two_spellings_of_a_host_select_each_other` records — a pair derived from one string
carries one spelling on both sides and holds on the pre-fix code.

### Review round 1 — the separator was not only ASCII

Cross-model review of `d3bcdc5` returned one blocking finding, and it was correct. Reproduced by
execution against that exact HEAD before any fix code was written:

```
canonicalize_url("https://bls.gov。/report")  ->  "https://bls.gov./report"
canonicalize_url("https://bls.gov./report")  ->  "https://bls.gov/report"
```

`_canonical_host` removed ASCII `.` *before* IDNA encoding, but UTS-46 maps U+3002, U+FF0E and
U+FF61 onto `.` during that encoding. So the strip ran before the separator existed, and the
encoder then produced the very character the strip was there to remove. Three consequences, all
verified rather than argued:

1. **Canonicalization was not a fixed point.** `f(f(x)) != f(x)` for every non-ASCII separator —
   the one property a canonicalizer exists to have, and the one this module's docstring claims.
2. **One page kept two ledger identities**, which is the defect this whole item was filed to close.
3. **An official source was recorded as `web`.** This is the part that made it a *regression* and
   not a pre-existing gap, and it is worth stating precisely because the branch causality decides
   whether a finding is blocking at all. At the merge base, `exa._without_root_dot` ran **after**
   `canonicalize_url`, so it caught the ASCII dot that IDNA had just produced from `。`. Measured
   at both trees with a `bls.gov` allowlist and a result at `https://bls.gov。/report`:

   | tree | `source_type` |
   |---|---|
   | base `e6e83a7` | `official` |
   | this branch at `d3bcdc5` | `web` |

   Deleting the workaround was justified by a canonicalization rule that did not in fact cover
   everything the workaround covered. The "retire, don't layer" decision above is still right; it
   was applied one spelling too early.

**Fix.** `idna.uts46_remap(host, std3_rules=False)` runs first, so the strip sees one settled
spelling — see the ordering decision above for why the separator table is delegated rather than
re-tabled locally. The review proposed the local tuple instead; it would close the same
counterexample, and it was declined for the reason recorded there.

**Why the existing tests missed it.** Not because the properties were weak — they were asserting
the right statements over an input space that could not express the counterexample.
`ROOT_DOT_SUFFIXES` now draws from all four spellings and `URL_CANDIDATES` carries the Unicode
forms, so `host_spellings` can straddle the ASCII/Unicode boundary and
`test_distinct_hosts_stay_distinct` draws separators rather than a boolean. Re-run against the
round-1 code, all four properties fail, on exactly the counterexample
(`suffix_left=''`, `suffix_right='。'`); every new unit case fails there too, except the
double-separator rejection cases, which pass both ways and are kept as the bound on the widened
fold. **This is the M1-303 lesson landing one level up: a property can discriminate perfectly and
still prove nothing if the generator cannot reach the defect.**

**One test assertion was corrected, not weakened.** Parametrizing
`test_the_two_spellings_of_one_host_collapse_to_one_document` over the separators failed on its
final assertion — that the *dotted* document is the survivor. That assertion was pinning
`_sort_key`'s tiebreak, not M1-310's rule, and it is separator-dependent for a legitimate reason:
the tiebreak orders the persisted JSON, where `ensure_ascii=True` renders U+3002 as a
backslash-`u` escape, so `/` (U+002F) sorts ahead of `\` (U+005C) while a bare `.` (U+002E) sorts
ahead of `/`. The plain document therefore wins three of the four cases. The assertion now pins
what this rule actually claims — the survivor is one of the two documents exactly as retrieved,
with its own `original_url` beside the shared canonical form — and the tiebreak keeps its own
tests. No source change followed from it.

### Review round 2 — an IPv6 zone ID is not a DNS label

Cross-model review of `d32a4bc` (round-1 fixes plus a `master` merge) returned one blocking
finding, and it was correct. Reproduced by execution against that exact HEAD before any fix code
was written:

```
canonicalize_url("https://[fe80::1%25ETH0]/report")   -> "https://[fe80::1%25eth0]/report"
canonicalize_url("https://[fe80::1%25eth0.]/report")  -> "https://[fe80::1%25eth0]/report"
canonicalize_url("https://[fe80::1%25eth0]/report")   -> "https://[fe80::1%25eth0]/report"
```

`_canonical_host` ran `idna.uts46_remap` before attempting `ipaddress` classification at all, so a
scoped IPv6 literal was folded as if its zone ID were a DNS label: case-folded (`ETH0` -> `eth0`)
and dot-stripped (`eth0.` -> `eth0`). A zone ID is opaque per RFC 4007 s11.2 — its string form is
implementation-dependent, not a DNS spelling — so three distinct scoped addresses collapsed to one
canonical form, and dedup would have discarded two of the three attribution records.

**Fix.** `ipaddress.ip_address(host)` is attempted *before* `uts46_remap` runs, and returned
immediately on success — an IPv6 literal (zone ID included, verbatim) or a non-dotted IPv4 literal
never reaches the DNS-mapping path at all. On failure it falls through to the existing
mapping/strip/re-classify path unchanged, so `https://127.0.0.1./a` (not a valid IP literal on the
first attempt, because of the trailing dot) still collapses to `https://127.0.0.1/a` exactly as
before. The ordering decision above — mapping, then strip, then IP/domain split — still governs
everything that *isn't* already a bare IP literal; this adds an earlier exit, not a new branch.

**Tests.** `test_ipv6_zone_id_is_a_fixed_point` and `test_ipv6_zone_id_spellings_stay_distinct`
(`tests/unit/test_dedup_freshness.py`) pin the three inputs above as fixed points that stay
pairwise distinct. Re-run against the round-2 code (fix reverted), both fail on exactly the
reviewer's counterexample.

## M1-306 — Persisting replayable retrieval runs

**Acceptance criterion:** *replay produces zero provider calls and the same research packet
hash.* One of those two nouns did not exist. `content_sha256`'s docstring promises "the
research-packet hash that replay reproduces", `CLAUDE_CODE_PROMPT.md` § B requires M1-307 to
reproduce it, and the handoff's canonical record lists "retrieval run and normalized source
references" — but nothing in the repository defined a research packet or hashed one. **M1-306
defines it**, and most of what follows is that definition and its consequences.

Scope was settled with the owner before any code: this item is **persistence and replay only**.
The cross-provider orchestration that `docs/M1-303-NOTES.md` assigns here — `decide_fallback()`
then `retrieve_web()` — is a follow-up row, not this branch.

### What the criterion is actually guarding against

Not "can we write rows". The failure it exists to prevent is a forecast whose evidence cannot
be re-derived: a stored forecast that says it saw twelve articles, where nothing can now
establish *which* twelve, gathered under which queries, at what cost. The packet hash is the
one value that makes that claim falsifiable — if the ledger's rows still hash to what the
forecast recorded, the evidence is the evidence. If a normalization change, a re-persist, or a
different machine moves that hash, the instrument has quietly lied.

That framing is what decides every question below, and in particular the two that look like
implementation detail and are not: *what the hash is computed over* (§ persisted form) and
*what replay reads* (§ the ledger, not the artifacts).

### Decision — the packet is derived, not stored

A `ResearchPacket` is a frozen value object over rows that already exist: one `question_id`,
its `research_runs`, and their deduplicated `research_documents`. There is **no
`research_packets` table and no packet row**.

The alternative — persist the packet and its hash — was rejected because it creates a second
source of truth for something wholly derivable, and the two can disagree. A stored hash that
no longer matches the rows it summarizes is worse than no stored hash: it is an attribution
claim the evidence contradicts, and the ledger's whole purpose is that those cannot diverge.
Derived means the hash is *recomputed* from the rows every time it is asked for, so a
mismatch is unrepresentable rather than merely unlikely.

M1-602 will store the packet hash on `forecast_records.record_json`, and that is a different
thing: a forecast records the packet it *saw*, and comparing that stamp against a recomputed
hash is exactly the audit this design makes possible. The stamp lives with the forecast; the
truth lives in the evidence rows.

It also costs nothing in migration budget — see the counters decision for what 004 is actually
spent on.

### Decision — the hash keys on the persisted form, not the in-memory objects

`packet_sha256` digests `model_dump(mode="json")` → `json.dumps(..., ensure_ascii=True,
sort_keys=True, separators=(",", ":"))` → UTF-8 → SHA-256.

This is M1-305's lesson applied verbatim, and it is the one place on this branch where the
wrong choice is invisible until replay (`research/dedup.py:_sort_key`, `docs/M1-305-NOTES.md`
rounds 2-4, five review rounds on one function):

- **Never `model_dump_json()`** — it raises on a lone surrogate, which is reachable from
  provider JSON through any schema-valid text field.
- **Never `repr` or a plain `model_dump()`** — the in-memory form carries distinctions JSON
  drops, notably `datetime.fold` and the astral-scalar/surrogate-pair spelling of one
  character. A hash over those is stable in memory and *changes across a store→load
  round-trip*, which is precisely the hash that would fail the acceptance criterion while
  passing every test that never went through SQLite.

`ensure_ascii=True` escapes surrogates instead of encoding them; `sort_keys` and the compact
separators make the rendering canonical. Runs are ordered by `retrieval_run_id`, documents by
`dedup.dedup_key` — reusing the ledger's own identity tuple rather than restating it, so the
hash's ordering and the `UNIQUE` constraint can never drift apart.

The rule is versioned (`PACKET_SCHEMA_VERSION`) and carries the same warning `hashing.py` and
`prompt.py` carry: **changing it breaks replay for every packet already hashed**, so it changes
as a new function alongside this one, never as an edit to it.

### Decision — the hash covers how the evidence was gathered, not only what was found

Owner decision, taken over the narrower reading. The digest includes each run's
`retrieval_run_id`, `question_id`, `provider`, `provider_config`, `queries`,
`started_at_utc`/`completed_at_utc`, `freshness_cutoff_utc`, `cost_usd`, `error_summary`,
`agent_model`, `posts_dropped_no_url` and the two new counters — alongside every document.

Hashing documents alone was the tempting option: it is stable across re-runs that happen to
surface the same articles, which *sounds* like the property replay wants. It is the wrong
property. Two runs that found the same twelve articles under different queries, a different
provider, or a different freshness window are not the same research; a packet that cannot tell
them apart cannot testify to how the evidence was found, and "the same research packet" would
degrade into "the same reading list". A run is an event, and the packet is the record of it.

The consequence is deliberate and worth stating plainly: **a fresh retrieval over identical
evidence produces a different packet hash**, because it is a different gathering event. Replay
reproduces the hash because it reads the *same rows*, which is what the criterion asks.

### Decision — three fields are excluded from the digest, each for its own reason

- **`document_id`** — a writer-minted UUID. A document's identity in this ledger is its dedup
  key (`retrieval_run_id`, `canonical_url`, `content_sha256`), which the `UNIQUE` constraint
  already says out loud; the UUID is an addressing convenience assigned at write time. Include
  it and re-persisting byte-identical evidence hashes differently, which would make the digest
  a fact about *when rows were written* rather than about the evidence.
- **`raw_response_path` / `raw_artifact_path`** — where bytes were filed is not what was
  retrieved. These are stored relative to `storage.artifact_root`, which is operator
  configuration; including them would make the packet hash **machine-dependent**, so the same
  evidence would fail to verify after a config edit or on a second checkout. An operator
  reorganizing their artifact directory must not be able to invalidate an attribution record.

`created_at_utc` needs no exclusion rule: it is writer-owned metadata that `ResearchRun` and
`ResearchDocument` deliberately do not carry (`research/model.py:24-27`), so it never reaches
the dump.

### Decision — replay reads the ledger; raw artifacts are audit evidence, not the substrate

`replay_research()` reconstructs the packet from `research_runs` and `research_documents`. It
does **not** re-parse the stored raw provider bodies.

Re-normalizing from raw responses is the option that looks more faithful and is in fact the
dangerous one: normalization lives in the adapters, so a replayed packet would depend on
*adapter code version*. Fix a bug in `_to_document`, and every historical forecast's evidence
silently re-derives into something else — the ledger would rewrite its own history on a
refactor, which is the exact failure D25 and the append-only triggers exist to prevent. The
normalized rows are the record; the raw bodies are the evidence that those rows were derived
from something a provider really returned.

This is also what makes the zero-provider-calls property structural rather than a mock count:
the replay path reads SQLite, and the module it lives in imports no provider SDK at all.

A consequence, stated rather than hidden: **a deleted or corrupted artifact does not break
replay.** That is correct — replay does not depend on it — but it does mean artifact loss is
an *audit* loss that replay will not report. Recorded under standing risks.

### Decision — the run row is opened before the calls and completed after

`open_run()` inserts identity and `started_at_utc` with `completed_at_utc` NULL; `complete_run()`
fills in what the run learned. Master already committed to this shape — 003's triggers pin
identity and provenance while deliberately leaving `completed_at_utc`, `error_summary` and
`cost_usd` writable, and `docs/M1-NOTES.md` says so under M1-603's consequences.

The reason to actually use it, rather than doing a single insert at the end: a run makes up to
`max_queries_per_question * 2` billable calls, and both adapters were already hardened so a
mid-run provider failure returns everything paid for instead of raising it away (M1-302 round 1,
finding 2). A single terminal insert reopens the same hole one level up — a hard crash between
the last paid call and the write loses the record of every call. Opening the row first means
the spend is attributable even when the process does not survive to describe it.

`persist_retrieval()` composes both inside one transaction for the case where the caller
already holds a completed run.

### Decision — migration 004 holds the discarded-evidence counters

Both adapters return `documents_dropped` and `duplicates_collapsed` on their in-memory result
objects, with no column to land in; `docs/M1-301-NOTES.md` left the call here. They get columns.

002 already settled the principle for exactly this shape of number, and settled it against its
own first instinct: an off-range count is "not a bad measurement but an unfalsifiable claim
about how much evidence was discarded". That is why `posts_dropped_no_url` took a column and a
`typeof()` guard rather than staying model-side. `documents_dropped` is the same claim about
the same subject, so it is stored the same way — including the `typeof()` guard, because
`INTEGER` in SQLite is affinity rather than a type and `1.5` and `'garbage'` both satisfy a
bare `>= 0`.

**NULLable, deliberately.** Under the two-phase write the counts are not knowable at insert
time, and rows predating 004 never had them. NULL means *unmeasured*; `0` is the auditable
claim that nothing was discarded. Collapsing the two would manufacture a measurement, which is
the failure 002's note names. No trigger changes are needed: 003 pins identity, and these are
completion data.

### Decision — artifacts are relative, atomic, and never overwritten

`write_raw_responses()` writes a versioned envelope (shaped after `metaculus/snapshots.py`, the
existing precedent for a replay substrate on disk) to
`<artifact_root>/research/<question_id>/<retrieval_run_id>.json`, via a temp file and
`os.replace`, and **refuses to overwrite an existing file**. An artifact is the record that a
paid run happened; silently replacing one destroys evidence, and the same rule already governs
`run-review.sh` refusing to overwrite a review response.

The path stored on the run is **relative to `artifact_root`**, so a ledger stays readable after
the artifact directory moves or is opened on another machine. Retention is the caller's flag
(`retrieval.retain_raw_responses`, `storage.retain_raw_research`), passed explicitly — the
module reads no config and does not guess; off means no file and `raw_response_path is None`.

The envelope holds provider **response bodies only**. No request headers, no request URL: both
carry the API key, and both adapters already discard provider exceptions unread for that reason.

### Deviation — `research` imports `transaction()` from `lifecycle`

`lifecycle.transaction()` is a reviewed, savepoint-aware `BEGIN IMMEDIATE` helper, and the
writers here need exactly it: a run row and its documents must never be observable one without
the other. So `research/store.py` imports it, which points the retrieval epic at the lifecycle
module.

Both alternatives are worse. Duplicating the helper puts a second transaction implementation in
the tree, and the savepoint/nesting behaviour it encodes took M1-603 a review round to get
right. Relocating it to `ledger.py` — arguably its real home — is a refactor of merged, approved
code carried on a review branch, which is the shape of change `docs/LESSONS.md` measures the
cost of. **Stated, not half-fixed**: if the relocation is wanted, it is its own row.

### Rejected — options weighed and not taken

- **A `research_packets` table.** A second source of truth for a derivable value; see the first
  decision.
- **Hashing the document set alone.** Cheaper and stable across re-runs, but the packet then
  cannot testify to the queries, provider or freshness window that produced it.
- **Re-normalizing from raw responses on replay.** Makes replay depend on adapter code version,
  so a normalization fix rewrites history.
- **Touching the two merged adapters.** `AskNewsRetrieval`/`ExaRetrieval` keep their shape; the
  caller hands the counters to `complete_run()`. Editing merged, approved modules to save one
  argument is not a trade this branch makes.
- **A `replay` CLI subcommand.** The handoff spells it `replay --record-id ID`, and
  `forecast_records` does not exist yet (M1-602). Adding a `--run-id` variant now would ship a
  command shape the handoff does not specify and M1-602 would rewrite.

### Deferred (do not read the absence as an omission)

- **Cross-provider orchestration** — `decide_fallback()` → `retrieve_web()` → cross-run dedup,
  assigned here by `docs/M1-303-NOTES.md`. Owner decision to split; it is a paid-call policy
  surface and does not belong in a persistence review.
- **The `replay --record-id` CLI** — M1-602 owns the key.
- **Budget enforcement.** `run_limits.max_cost_usd` still has no consumer. This branch preserves
  the invariant M1-303 round 3 established — **`cost_usd is None` means unknown, never free** —
  by round-tripping NULL as `None` and never summing it as zero. Enforcing a cap is M1-504's.
- **M1-309's shared caller preflight.** Unchanged here.

### Standing risk — not verifiable offline

- **Artifacts can drift from the ledger.** Replay survives a missing artifact by design (it
  reads rows), which means artifact loss is an audit loss replay will not report.
- **AskNews summary-derived `content_sha256` is not byte-stable across re-retrieval**
  (`docs/M1-301-NOTES.md`): the summary is LLM-generated. This does not affect replay — replay
  re-reads stored rows and issues zero calls — but it does mean a *fresh* run over the same
  article can produce a different document identity, and therefore a different packet hash.
- **The packet hash is anchored to `canonicalize_url` as of D32.** M1-310 changed document
  identity by stripping a terminal root dot; a further canonicalization change is a
  packet-hash change and must be treated as one.
- **`content_sha256` still raises on a lone surrogate** — open owner decision, xfail in
  `tests/property/test_canonical_properties.py`. It is upstream of the packet hash: a document
  that cannot be hashed never becomes a row, so this is a reachability limit on what can be
  persisted, not a defect introduced here.

## M0-007 — Sanitizing PyYAML constructor errors in `load_config`

`config.load_config` caught only `yaml.MarkedYAMLError`/`yaml.YAMLError` around
`yaml.safe_load`. PyYAML's construction stage (as opposed to its scanner/parser/composer) raises
whatever Python raised at it, so it is not a `YAMLError` at all — the same hole M1-308 round 7
found and closed in `research/allowlist.py`, filed here because it predated that branch's diff
base. Reproduced the same six shapes against `config.py` before writing anything: an out-of-range
implicit date/timestamp (`ValueError`), `!!bool`/`!!int` with an unparseable scalar (`KeyError`/
`ValueError`, both leaking the value), `!!timestamp` with an unparseable scalar (`AttributeError`),
and flow nesting past the recursion limit (`RecursionError`).

### Decision — copy the reviewed fix verbatim, not a variant

Added `except Exception:` immediately after the existing `except yaml.YAMLError:` clause, scoped
to only the `yaml.safe_load(raw_text)` call, raising the same sanitized `ConfigError` message the
two existing branches already use, `from None`. Deliberately not an enumerated tuple of exception
types — GPT r7 rejected that shape for `allowlist.py` on the grounds that the enumeration belongs
to PyYAML and any type missing from a hand-written list would escape raw; the same reasoning
applies here unchanged.

### Rejected — adding `is_filesystem_error` to `ConfigError`

`AllowlistError` carries an `is_filesystem_error` flag that `cli.py` uses to route between
`EXIT_ENV_MISSING` and `EXIT_CONFIG_INVALID`; `ConfigError` has no equivalent, so every
`load_config` failure — including an unreadable file — maps to `EXIT_CONFIG_INVALID`. Out of
scope for this item's acceptance criterion, which is about sanitizing the constructor-error leak,
not exit-code routing; changing exit code semantics for existing `ConfigError` callers on a
sanitization branch would be a second, unrelated change. Left as a possible future backlog row.

### Verification

Six-case parametrized suite mirrors `test_allowlist.py`'s `CONSTRUCTOR_FAILURES` table, plus the
same meta-guard (`yaml.safe_load` on each source must still raise something that is *not* a
`yaml.YAMLError`, so the suite cannot silently go vacuous against a future PyYAML) and a negative
control (a value that constructs cleanly must still reach pydantic and fail as a schema error, not
get misclassified as "not valid YAML"). Confirmed all six new tests fail against the pre-fix tree
(raw `ValueError`/`KeyError`/`AttributeError`/`RecursionError` escaping `load_config`) and pass
post-fix.

## M1-313 — Deep-freezing the research packet

`ResearchPacket` is a frozen dataclass, but `ResearchRun`/`ResearchDocument` are plain
(unfrozen) pydantic models with mutable list/dict fields (`queries`, `provider_config`), so a
tuple of them is immutable only at the container level. `build_packet` stored the exact objects
a caller handed it; a caller that retained a reference to a run or document — or to one of its
mutable fields — could mutate it after construction and change what `packet_sha256` returns for
an object the rest of the system treats as an immutable attribution record. No current product
path does this (round-1 finding on PR #26, filed as a hardening item, not a live defect), but the
acceptance criterion is that it becomes structurally impossible rather than merely unobserved.

### Decision — copy at construction, not at read

`ResearchPacket.__post_init__` now deep-copies every run and document via
`model.model_copy(deep=True)` before any validation runs, then rebinds `self.runs`/
`self.documents` to the copies via `object.__setattr__` — the same idiom `questions/events.py`
already uses to normalize fields on a frozen dataclass from inside its own `__post_init__`.
Validation then runs against the packet's own copies, so what is checked is what is stored.

Considered and rejected: freezing only at the `build_packet` entry point rather than in
`__post_init__`. `ResearchPacket`'s own docstring already documents that constructing the
dataclass directly with a list (rather than a tuple) raises deliberately, so direct construction
is a supported path, not just an implementation detail `build_packet` wraps. Freezing in
`__post_init__` covers both.

The copy failure path (`model_copy` raising) follows the same precedent `_dump`'s parser-failure
branch set at M1-308 round 7: caught broadly, scoped to the one call, `from None`, a constant
message that withholds detail because a third-party copy routine can echo the value it failed on.

### Verification

New property `test_mutating_caller_owned_inputs_does_not_move_the_hash` builds a packet from
caller-held `run`/`document` objects, hashes it, then mutates every mutable surface the
acceptance criterion names — the run object, the document object, `run.queries` (append),
`run.provider_config` (add a key) — and asserts the digest is unchanged. Confirmed it fails
against the pre-fix tree (the digest moves) and passes post-fix, per the project's mutation-check
convention. No schema, no migration, no new dependency.

## M1-607 — The non-blank identifier guard on the pre-004 columns

Migration **006** (`006_non_blank_identifiers.sql`). No new dependency, no new table, no new
column: three existing `BEFORE INSERT` triggers redefined, an upgrade precondition, and the
matching writer-side guard in five modules.

M1-606 built this guard for `attempt_id` and **deliberately stopped there**, because widening
`_require_text` would have changed what already-shipped, already-reviewed writers accept — a
behaviour change to merged code smuggled in under a different item. It filed the widening as
its own reviewed change (this one), the same call M1-308 made when it found `config.py`'s copy
of its YAML hole and filed M0-007. So the interesting content here is not the guard, which is
copied verbatim from 004; it is the five decisions about *where the copy stops*.

### Decision — five columns, and why `tournament_id` is one of them

The backlog row names four: `forecast_records.record_id`, `submission_attempts.attempt_id` and
`.idempotency_key`, `research_runs.retrieval_run_id`. `forecast_records.tournament_id` is the
fifth, added on purpose.

004 already guards `pipeline_failure_events.tournament_id`, and 004's identity-stability probe
compares the two columns directly — it refuses a forecast record whose `attempt_id` was recorded
under a *different* tournament. That probe can only be as trustworthy as the weaker of the two
columns it joins, and 004 guarded one end. Guarding one end of a join key and not the other is
the precise defect M1-606's own notes describe finding in its first draft. Owner agreed the
widening before implementation.

### Decision — foreign-key columns are covered transitively, and the residual is stated

Every remaining identifier column is a foreign key into one of the five guarded primary keys
(`approval_events`/`submission_attempts`/`resolution_events`/`score_events.forecast_record_id`
into `forecast_records.record_id`; `forecast_records`/`research_documents.retrieval_run_id` into
`research_runs.retrieval_run_id`), and `research_documents.document_id` is minted by the writer
as `uuid4().hex`. Duplicating the clause onto each would be a second copy of a rule with nothing
keeping the copies in agreement.

**The residual, stated rather than half-fixed** (M1-303 round 5's `co.uk` lesson): that
transitivity rests on `PRAGMA foreign_keys = ON`, which `ledger.py` sets at line 95 but — unlike
`journal_mode` and `recursive_triggers` — **does not read back**. An unknown or ignored PRAGMA is
a silent no-op in SQLite. So this is an assumption, not a verified property. Reading it back is a
change to `ledger.py`'s connection contract and belongs to its own item; it is not smuggled in
here, for the same reason M1-606 did not smuggle this one in there.

### Deviation — `research_runs.retrieval_run_id` gets no 200-character ceiling

The other four columns take the full 004 clause: `IS NULL`, `typeof <> 'text'`, `length > 200`,
`instr(..., char(0)) > 0`, and the 29-codepoint `trim()`. This one takes everything except the
ceiling, and the asymmetry is deliberate.

The ceiling exists in 004 to close one specific defect (M1-606 review round 1, finding B1): raw
SQL writing an identifier longer than the **reader** accepts, producing an append-only row that
can never be read back. Every reader of the other four caps at `_MAX_IDENTIFIER`.
`store.load_run` and `store.load_documents` impose no length limit at all — so that defect does
not exist on this column, and inventing a ceiling would refuse input the shipped M1-306 writer
accepts, which is exactly the behaviour-change-to-merged-code this item exists to avoid making by
accident.

The NUL check stays on all five, because it is justified independently of any ceiling: U+0000 is
the one input where SQLite's `length()`/`trim()` and Python's `len()`/`strip()` disagree about the
*same* string, so a NUL-bearing identifier is one the schema and the writer cannot both reason
about. `test_the_run_id_column_has_no_length_ceiling_and_that_is_deliberate` and
`test_a_run_id_longer_than_200_characters_round_trips` assert the asymmetry from both sides, so it
cannot be quietly "tidied" into uniformity.

### Decision — the upgrade refuses a ledger that already holds a violating row

`BEFORE INSERT` triggers bind new rows only. Without a precondition the guarantee would quietly
become "every row written after 006" — and these are append-only tables, so a violating legacy row
can never be corrected. A blank or blob identifier is precisely the row that is unjoinable and
unreadable for good, which is the condition this item exists to make impossible.

Same mechanism 003 uses for its non-draft-record precondition: `RAISE()` is legal only inside a
trigger body, so the refusal is a `CREATE TEMP TABLE` whose `CHECK` the offending row violates and
whose **name** carries the reason. `ledger.py` wraps the failure without echoing any stored value
and rolls back, so a refused upgrade leaves the database exactly at version 5. Each probe is the
*same predicate* as the trigger clause it corresponds to — two definitions of "blank" that nobody
compared is the defect this whole family descends from, and a third layer with its own wording
would reopen it.

Realistic population today is empty: `forecast_records` has no writer until M1-602, submission is
disabled until M2, and `research_runs` ids are short caller-supplied strings.
`test_a_clean_v5_ledger_upgrades_to_006` and
`test_rows_written_before_006_survive_it_when_their_identifiers_are_well_formed` are the controls —
without them a migration that refused *every* v5 ledger would satisfy all seven refusal cases while
being undeployable.

### Rejected — changing `research/asknews.py`

AskNews is the one reachable path that still notices a blank `retrieval_run_id` only *after*
billing. It was left alone anyway. Its module docstring contracts that `MissingCredentialError` is
the only exception it raises, and `retrieve_news` contracts **never to raise on provider failure**
so that a partly-paid-for run is still recordable. Adding a raise there is a contract change, not a
guard application.

What covers it instead is the model-level validator: `asknews.py` builds `ResearchDocument`
objects directly, so `IdentifierString` refuses a blank run id at construction and the *ledger* is
protected, which is this item's actual subject. The residual — that AskNews spends before it
notices — is the same class of hole **M1-309** was already filed for ("the same holes in merged
AskNews", M1-303's follow-up) and belongs there, with the preflight redesign that contract change
needs.

`research/exa.py` **was** changed, and the difference is the point: it already has
`_require_run_metadata`, a preflight whose entire reason for existing is refusing caller mistakes
before the money (M1-303 round 4, finding 4). Its test was `not retrieval_run_id`, which refuses
`''` and passes `'\n\t'` — the hole in the exact place built to prevent it. Extending an existing
gate is not a contract change.

### Rejected — folding `_require_identifier` into `_require_text`

Kept as two functions, in all of `lifecycle.py`, `approval.py` and `research/store.py`. Blank prose
— an `actor` note, an `error_message`, a `response_body` — is a thin record; blank identity is an
unjoinable one. Collapsing them would put the identifier rule on `refetched_forecast_snapshot` and
`note`, which is a different change with a different justification.

The three module-level copies are also deliberate rather than a shared helper: each module owns its
sanitized exception type, and a shared helper would have to raise one module's error inside
another. That is the same trade `_require_text` itself already makes. Because a second copy of a
rule is a second thing that can be wrong, a property test
(`test_the_two_module_copies_of_the_identifier_rule_agree`) asserts `lifecycle`'s and `approval`'s
copies accept and reject exactly the same inputs.

### Deferred (do not read the absence as an omission)

- Reading `PRAGMA foreign_keys` back in `ledger.py` — see the residual above.
- AskNews's pre-billing preflight — M1-309.
- `forecast_records.parent_record_id`, and the `*_forecast_record_id` FK columns — covered
  transitively; guarding them directly would duplicate the rule.

### Standing risk — not verifiable offline

The 29-codepoint set is frozen in SQL the moment 006 lands on master, while `str.strip()` follows
the Unicode data of whatever Python is running. A future Python whose `str.isspace()` covers a new
codepoint reopens the gap on the schema side. That cannot be prevented, only detected: four drift
guards (in `test_lifecycle.py`, `test_approval.py`, `test_research_store.py` and `test_exa.py`)
each recompute the set from the running interpreter and assert `len(...) == 29` before looping, so
the change surfaces as a failed test and a migration 007 rather than as a silently reopened hole.

### Verification — 18 of 18 guards are load-bearing

Every new `RAISE(ABORT)` clause (5), every upgrade precondition probe (5) and every writer-side
branch (8) was broken one at a time with the suite re-run against each. All 18 were caught by at
least one test. Two details worth keeping:

- **Trigger clauses were neutered, not deleted.** Removing the only statement from a trigger body
  leaves `BEGIN END;` — a syntax error, so the migration fails to apply and every test fails for
  the wrong reason, reporting a guard as load-bearing when nothing tested it. Each clause's `WHERE`
  was replaced with `WHERE 0` instead. 004's notes record hitting exactly this.
- **`__pycache__` was cleared between mutations.** Bytecode is validated on size plus a
  one-second mtime, so a same-size edit reverted within the same second can be served from stale
  bytecode and a mutation silently never runs.

The value-leak property needed rewriting rather than the obvious spelling. "The value is not a
substring of the message" is **vacuous and worse than nothing** here: a one-character whitespace
identifier is a substring of every message containing a space, so it fails on correct code and
would then be "fixed" into asserting less. `test_an_identifier_refusal_says_one_of_a_fixed_set_of_things`
closes the message set instead — five constants with only the field name interpolated — which says
the thing that matters and says it for inputs a substring check cannot speak about at all.

`test_the_writer_and_the_schema_admit_exactly_the_same_identifiers` fuzzes the two-layer agreement
on `record_id` in **both** directions, with a strategy that straddles the 200-character boundary
and includes embedded NULs. The unit suite asserts the same agreement over the 29 whitespace
codepoints; the property covers the rest of the input space, because the failure mode is not "the
set is wrong" but "two layers hold two definitions and nobody compared them", which can differ
anywhere.

### Review outcome — approved in round 1

Cross-model review round 1 (reviewed commit `07b3297`) returned **APPROVE with no blocking
findings**, the second single-round approval in the project after M1-202. The one non-blocking
observation was the `PRAGMA foreign_keys` residual this item had already declared as a standing
risk, and the reviewer's own note records why it is not a defect on the branch: the PRAGMA is
recognized on the supported SQLite runtime and set outside a transaction, so there is no
deterministic failure to reproduce. Filed as **M1-609** rather than fixed here, for the reason the
Deferred section gives — reading it back changes `ledger.connect`'s contract.

Worth recording alongside `docs/LESSONS.md`'s count of review-round commits: the difference between
this and M1-305's ten rounds was not the code. It was writing the five headings above, the risk
areas and the mutation-check result *before* round 1 instead of discovering them through it. The
deviation most likely to be read as an oversight — `retrieval_run_id` having no length ceiling —
was stated first and came back marked "Safe" rather than as a finding.

## M1-312 — Composing artifact and ledger persistence for a paid run

M1-306 shipped `research/artifacts.py` and `research/store.py` as complete primitives and
deliberately shipped no composition of them, because the composed entry point belongs with the
retrieval orchestrator that item does not ship. What it left behind was an ordering rule living
only in a docstring — *write the artifact first; if it fails, persist the ledger row anyway with
`raw_response_path` NULL* — in a paragraph that said in as many words that **no function here or
in the store performs that composition**. Nothing outside tests called either primitive, so the
first caller on the paid path would have had to re-derive the rule. This item is that rule,
executed, in `research/persist.py`.

The whole design follows from *when* it is called. By then up to `max_queries_per_question * 2`
billable calls have been made. An artifact is evidence that the rows came from a real provider
response; the rows are the record. Losing the evidence is an audit loss. Losing the row is losing
the record that money was spent at all. **Every decision below resolves in favour of the run
staying recorded, with the loss reported rather than hidden.**

No migration, no dependency, no schema change: `research_runs.raw_response_path` has been
nullable since `001_initial.sql`.

### Decision — one function, and a result object that cannot misreport what happened

`persist_paid_run(conn, config, run, documents, *, raw_responses, written_at_utc=None,
run_opened=False)` attempts `artifacts.write_raw_responses` first, catches `ArtifactError`, and
then calls `store.persist_retrieval` (or `store.complete_run`) with whatever path survived. It
returns a frozen `PaidRunPersistence` carrying `document_ids`, `raw_response_path`,
`artifact_outcome` and `artifact_error`.

`artifact_outcome` is a three-valued `Literal`, not a bool, because `retention_disabled` and
`failed` both leave the path NULL and a caller auditing the ledger cannot otherwise tell "the
operator asked us not to keep it" from "we tried and lost it". The dataclass's `__post_init__`
refuses every combination that would misdescribe the outcome — a "written" result with no path, a
"failed" one with no error, a "disabled" one carrying either — so the report is structurally
unable to hide an audit loss. That guard is not decoration: it is the second half of "reports the
audit loss to its caller rather than swallowing it", and mutation 2 below is caught by it.

### Decision — `retrieval.retain_raw_responses AND storage.retain_raw_research`, resolved here

Two configured flags both mean "keep raw research" and **nothing in the project had ever combined
them**. They are combined with `and`: either one off means no file and no recorded path. Honouring
the narrower of two switches is the reading that cannot store something an operator turned off,
and it is the stricter reading of an ambiguity CLAUDE.md says to resolve strictly and note.

`artifacts.py` still reads no config and is still handed an explicit `retain`, as its own
docstring promises; `persist.py` is the one place that knows what the operator's two switches mean
together. It takes `config: AppConfig` for the same reason `store.replay_research` does, and
type-checks `artifact_root` rather than trusting it, so an `AppConfig`-shaped object that is not
one arrives as `StoreError` instead of a raw `TypeError` from `artifact_root / relative` two calls
down.

### Decision — every `ArtifactError` degrades, including a caller mistake

`write_raw_responses` raises one exception type for an ordinary I/O failure, a destination that
already exists, **and** a caller mistake — a run id that satisfies `IdentifierString` but not the
artifact layout's `_SAFE_RUN_ID_RE` (`run/1` is non-blank and NUL-free, so the model and
`006_non_blank_identifiers.sql` both accept it, and the artifact layer refuses it because a run id
becomes a path component). Sorting them by type is not possible, and it is not desirable either:
the calls are already paid for, so refusing any of them would trade a lost artifact for a lost
run. All of them degrade to `outcome="failed"` with the message reported and logged.

This is a deliberate departure from M1-303's round-4 rule that a caller mistake is refused *before*
billing. That rule holds on the other side of the spend; this function is only ever called after
it.

### Decision — `StoreError`, not a third exception type

The only exception a caller handles is `StoreError`. `persist.py` adds no failure mode of its own:
an artifact failure never raises out of it by design, and its input refusals are the store's own
refusals applied one call earlier. A separate `PersistError` would fragment one contract for no
gain, and callers would have to catch both to learn nothing extra. Both wrapped error types are
already sanitized, and filesystem paths remain the settled M1-401 carve-out — a path is what makes
an artifact failure actionable.

### Decision — `run_opened` is a checked bool, not a truthiness test

It selects *which* ledger write happens and has no safe default to guess at: guessing wrong either
inserts a duplicate or completes the wrong row. Checked with `type(...) is not bool`, which is
M1-306 round 2's `completed_only=None` defect one argument over — that one returned an **open** run
from a call asking for finished evidence, by taking the false branch of a truthiness test.

### Deviation — three refusals still lose the run, and the list is deliberately short

`persist_paid_run` raises before doing anything for a non-`ResearchRun`, a run with no
`completed_at_utc`, and a run already carrying `raw_response_path`. Each of those loses a paid
run's row, which is the outcome this module exists to prevent, so each has to earn its place by
being a **contradiction** rather than a mishap:

- the first two are refused by both ledger writers anyway; failing before the artifact write at
  least leaves no orphan file behind.
- the third: `store._run_parameters` writes the *argument* and ignores the model's field, so
  accepting a run that already claims a path would silently discard the caller's claim about where
  its evidence lives. This function mints that path.

A caller that cannot tolerate the loss opens the run with `store.open_run` before the billable
calls — which is exactly why the two-phase shape exists — and then this function completes it.
`written_at_utc` is deliberately *not* pre-validated for the same reason inverted: a bad timestamp
only affects the artifact, so it degrades through the artifact layer instead of refusing.

### Rejected — recording the artifact failure as a `pipeline_failure_event` (M1-606), and why not

`pipeline_failure_events` is scoped to an `attempt_id` and a `tournament_id` this API does not have
and should not invent, and its `research_failed` event means the research failed. Here the research
succeeded and only the evidence copy was lost — recording it as a research failure would make the
ledger claim something untrue about the retrieval.

### Rejected — deleting an orphan artifact when the ledger write then fails, and why not

If the artifact is written and the ledger write fails, the file stays. An artifact is never
deleted, for the same reason it is never overwritten; a file with no row is inert, and the next run
mints a new id, so there is no collision to clean up for. The ledger failure is *raised*, not
reported — there is no record to report it on.

### Deferred (do not read the absence as an omission)

- **No orchestrator, and no adapter changes.** `AskNewsRetrieval`/`ExaRetrieval` still hold
  `raw_responses` in memory and nothing calls this function in the product yet. Wiring it is the
  orchestrator's item; this is the API it will call, and the acceptance criterion is about the API.
- **The document-level `raw_artifact_path` stays `None`.** Per-document artifacts are a separate
  layout question the artifact module has not shipped.
- **Nothing re-attempts a failed artifact write.** A retry API would need to decide what a second
  attempt means for an immutable record; the criterion asks for the loss to be reported, and it is.

### Standing risk — not verifiable offline

A caller that hands over documents the store refuses (after a successful artifact write) gets a
`StoreError` and leaves an orphan artifact behind. That is the ordering working as designed, but it
means document validation happens one step later than the artifact write. Re-validating documents
before the artifact would duplicate `store._prepare_documents`, and the failure it would avoid is a
stray file rather than a lost run.

### Verification

`tests/unit/test_research_persist.py` (25 cases) and `tests/property/test_persist_properties.py`
(2 properties). The acceptance criterion is asserted by three tests driven by **real** failures
rather than monkeypatched ones — a destination that already exists, an unwritable directory
(skipped as root), and the `run/1` run id — each asserting no exception, the run row committed,
`raw_response_path` NULL, the documents present, and the loss reported. The leak test plants
`privateFAKE123456` in a provider body that `json.dumps` cannot render (`json.dumps` names the
offending value) and asserts it reaches neither the report, `caplog`, nor `capsys` — both channels,
because logging's own `%s` interpolation writes to stderr past `caplog` (M1-303).

The properties assert what the unit tests cannot enumerate: over generated runs and documents
crossed with all three artifact outcomes, the run is always recorded and the path is NULL in
exactly the two cases where no file was written; and **the packet hash is identical whether the
artifact was written or lost**, which is the composition-level consequence of `packet.py` excluding
`raw_response_path` from the digest. That second one is what makes a lost artifact an *audit* loss
and not a change to what was retrieved.

**Mutation check — all ten discriminate.** `ArtifactError` propagated; the failure swallowed
without an error; the ledger written before the artifact (caught by the orphan-artifact ordering
test); retention resolved with `or`; `run_opened` tested for truthiness; the stale-path refusal
removed; the `run_opened` branch inverted; each of the three result-object guards removed; and the
artifact's identity taken from something other than the run. Every one turned at least one new test
red.

One process note worth keeping, because it cost a real correction: the mutation runner's first
version restored `persist.py` from a backup it took at the start of each run, and a 2-minute
command timeout killed it mid-mutation, so the `finally` never ran — the *next* invocation then
backed up the already-mutated file and "restored" the mutation. Rewritten to restore from a
pristine copy taken once, outside the loop, and to assert byte-equality afterwards. This is
`docs/LESSONS.md`'s stale-bytecode trap in a different costume: **a mutation harness that can
silently fail to restore is indistinguishable from a test suite that does not catch the mutation.**

## M1-309 — AskNews caller preflight

Filed off M1-303's round-4 cross-model review, which found five caller-mistake holes in
`research/exa.py` and noted that two of them are equally present in the already-merged AskNews
adapter: `queries: Sequence[str]` accepts a bare `str` (which `list()` silently explodes into one
billable call per character), and malformed run metadata (`question_id`, `retrieval_run_id`, `now`)
reaches `ResearchRun` validation only at the end of a run, after every call has already been billed.
The other three round-4 findings (client-URL binding, `decide_fallback`'s bool gating, domain
canonicalization) have no AskNews analog — no client-URL-spoofing surface, no `include_domains`.

### Decision — a new shared module, `research/preflight.py`

The acceptance criterion requires the guard be shared with Exa, not copied. `string_list` and
`require_run_metadata` moved there verbatim from `exa.py`'s `_string_list`/`_require_run_metadata`,
generalized to take the caller's own exception type as `error: type[Exception]` — the guard logic
lives in exactly one place, while each adapter still raises only its own module's error, per the
project's error-hygiene rule. `exa.py`'s two private functions are now one-line delegators bound to
`error=ExaFallbackError`; every existing call site and every existing `test_exa.py` case is
unchanged.

### Decision — `AskNewsRetrievalError`, parallel to `ExaFallbackError`

AskNews previously raised only `MissingCredentialError` and defined no exception of its own; the
module docstring said so explicitly. `AskNewsRetrievalError` is the new module-owned error for the
two preflight refusals, named to parallel `ExaFallbackError` the way the two adapters' docstrings
already read as one family.

### Deviation — `now` is normalized to UTC in AskNews too, not just checked

`require_run_metadata` is validate-and-return: it hands back `now` converted to UTC, not merely a
confirmation that it was tz-aware. AskNews's `retrieve_news` previously used the caller's raw `now`
directly for `started_at_utc`/`completed_at_utc`/`freshness_cutoff_utc`/`retrieved_at_utc` with no
runtime check at all. Adopting the shared function closes the same coercion-before-billing shape
Exa's round-4 finding 4 closed — for free, since it's the same call — rather than writing a
narrower AskNews-only check that only validates and doesn't normalize. Confirmed live by
`test_now_is_normalized_to_utc_before_any_use`.

### Rejected — Exa-style client/config-binding checks for AskNews, and why not

Exa's `_require_exa_client` and `_ensure_exa_is_configured_fallback` exist because a caller could
hold an `httpx.Client` built independently of `build_exa_client`, pointed at another host, while the
run still records `provider="exa"` — the silent-provider-switch concern D18 and M1-303 are about.
AskNews has no parallel: `build_asknews_client` returns an `AskNewsSDK` object whose request routing
isn't caller-swappable the way an `httpx.Client`'s `base_url` is, and `config.retrieval.primary`
already has to name `asknews` for `retrieve_news` to be called at all in the pipeline's own control
flow — there is no finding this would close.

### Deferred (do not read the absence as an omission)

No change to AskNews's "never raises on provider failure" contract for the query loop itself — the
two new raises both happen before `config.retrieval` is even read, so no call has been attempted.

### Standing risk — not verifiable offline

None beyond what the Exa adapter already carries via the shared functions: a caller-supplied
`tzinfo`/broken iterator still runs arbitrary code inside the guard (caught and converted to the
module's own error, per `research/preflight.py`'s docstring), and that boundary is unchanged by
this item.

### Verification

`tests/property/test_preflight_properties.py` — totality (`string_list`/`require_run_metadata`
raise only the bound `error` class, exercised against two distinct dummy error classes so the
parameterization is proven, not assumed) and correctness on the valid domain (a non-blank string
list round-trips; a valid tz-aware `now` always returns UTC-aware and denotes the same instant),
plus the two concrete round-5 regressions (a broken `tzinfo`, a boundary `datetime` whose UTC
conversion overflows).

`tests/unit/test_asknews.py` adds eleven parametrized cases (five malformed `queries` shapes, six
malformed run-metadata shapes) asserting `AskNewsRetrievalError` and `sdk.news.calls == []`, plus
one asserting the UTC-normalization behavior. All eleven were confirmed to fail against the
pre-fix code before the guard was wired in — one surfaced the exact live bug shape, a raw
`TypeError: unsupported operand type(s) for -: 'str' and 'datetime.timedelta'` from an unvalidated
`now` reaching `freshness_cutoff_utc`'s subtraction.

`tests/unit/test_exa.py` unchanged and green — the extraction is behavior-preserving.

### Round 1 review (GPT) — one blocking finding, reproduced

Reviewed commit `64d9790`. **Finding:** `require_run_metadata` accepts an ordinary aware `now`
near `datetime.min`, but `freshness_cutoff_utc = now_utc - timedelta(days=...)` was still computed
only inside the final `validate_run({...})` dict, after the query loop — so that `now` billed both
the current- and historical-strategy calls and then raised a raw `OverflowError`, with no
recordable run. Exactly the shape Exa's round-5 finding 3 closed, and the analog my round-4
port missed: I moved `now`'s tz-awareness/UTC-conversion preflight into the shared function, but
did not also move the freshness-bound subtraction ahead of the loop the way `exa.py`'s
`retrieve_web` does.

Reproduced by direct execution against `64d9790` before writing any fix: `now=datetime.min` with
`tzinfo=timezone.utc`, one valid query, a fake SDK — 2 provider calls made, then
`OverflowError: date value out of range` raised, no `ResearchRun` returned.

**Fix:** `freshness_cutoff_utc` is now computed once, immediately after the two preflight calls
and before the query loop, wrapped in `try`/`except OverflowError` that raises
`AskNewsRetrievalError(...) from None` — the same pattern `exa.py`'s `published_after` computation
already uses. The value is reused (not recomputed) when the run is built. Added
`("now", datetime.min.replace(tzinfo=timezone.utc))` as a case in
`test_malformed_run_metadata_is_refused_before_any_call`, mirroring the equivalent case already in
`test_exa.py`. Re-verified post-fix: `AskNewsRetrievalError` raised, zero calls made.

## M1-501 — Validating the common attribution fields

**Acceptance criterion:** *schema rejects missing or unknown required fields and invalid source IDs.*
Two clauses again, and again they are satisfied by different things. The first is **half already
true** — `forecast/schema.py` makes nine fields structurally required and forbids extras — so what
this row owes is the rest of it. The second is entirely new, and it is the one that matters: nothing
in this pipeline had ever resolved a citation against the documents the model was actually shown.

### What the criterion is actually guarding against

`forecast/inputs.py` mints `src-001`… over a packet's documents in `dedup_key` order and hands the
mapping back as `ModelInput.sources`. It is the only structure that knows which evidence a given
forecast was allowed to cite, it is built one function above the parse step, and **nothing read it**.
So a response citing `src-009` against a five-document packet parsed, passed M1-403's bounds, came
out of `generate_forecast` as typed output, and would have reached `forecast_records` as an
attribution claim with no evidence behind it — discovered, if ever, at an audit where the mapping no
longer exists.

The same is true one field up. `prompts/forecaster.md:157` asks the model to confirm that "the
question ID and type match the input". The **type** is enforced, structurally, by which response
model the dispatch selected. The **id** was taken on trust, so a record could have carried a question
the model invented.

And three of the fields M1-501's row names — `evidence_adjustments`, `load_bearing_facts`,
`failure_modes` — carry `default_factory=list` in the schema, so an omitted key or an empty list
parsed. A forecast with no evidence and no failure-mode check was a well-formed forecast.

### Delivered

- `forecast/attribution.py` — `attribution_problems()` (the pure checker, returning sanitized problem
  strings) and `validate_attribution_fields()` (the raising wrapper). Seven rules, three of them
  conditional. Imports no provider SDK, no question model and **not `forecast.inputs`**.
- `forecast/generate.py` — the cross-type checks threaded into `_output_problems` ahead of the
  existing question-type dispatch, and `question_id` handed down to `_run_attempts`. No new preflight
  refusal, and no change to `_classify`, `_repair_turn` or the invocation accounting.
- `tests/fixtures/forecasts/binary_golden_sources.json` — the five ids M1-403's golden output cites.
- `tests/unit/test_forecast_attribution.py` (65 cases), 9 cases added to
  `tests/unit/test_forecast_generate.py` (7 new plus the import probe going from one case to
  three), 9 properties added to `tests/property/test_forecast_properties.py`, plus 2 boundary cases
  from round 1. Suite: **1939 -> 2024 passed** against this branch's diff base (master `c3034d0`, after the daily
  merge), 1 xfailed (the pre-existing `content_sha256` lone-surrogate xfail) — **+83 tests, all of
  them this row's.** Measured before the merge the branch read 1912 -> 1995; the 27 tests between
  the two figures are M1-312's, already approved on PR #35, and naming the real surface after a
  master merge is CLAUDE.md's rule. Four gates green, `pytest` 151s.
- No migration, no dependency, no `uv.lock` change, no config-contract change, no prompt edit, and no
  edit to any merged source module other than `generate.py`.

**Two merged test files changed, and both changes are widenings rather than corrections.**
`_packet()` in `test_forecast_generate.py` built **one** document, so `forecast.inputs` minted only
`src-001` — while `good_reply()` is the prompt's own example and cites `src-002` in
`evidence_adjustments` and `load_bearing_facts`. Eleven merged tests failed on the new checks for a
reason that had nothing to do with what they assert, so the packet now supplies two documents and a
`documents=0` argument builds the no-research case. And `test_the_response_schema_reaches_no_provider_
client` was parametrized from `schema` alone over `schema`, `binary` and `attribution`: all three make
the claim in their docstrings, and this row rests a design decision on it.

### Decision — the rules live on the output path, not in the schema (M1-403's placement, again)

The presence rules need no config and no question, so `min_length=1` on three fields in `schema.py`
was the shorter diff. It was rejected for three reasons, and the third is the one that decides it:

- it contradicts that module's stated scope in its own docstring;
- it would break `test_a_structurally_invalid_response_is_refused`, whose payloads pass
  `"source_ids": []` deliberately, and reverse M1-402's recorded decision rather than layer over it;
- **a schema failure and a checker problem are not equally repairable.** They are today — `_parse`
  catches `ForecastSchemaError` and turns it into problems — but that equivalence is `generate.py`'s
  to keep, not the schema's to depend on. The citation rules *cannot* live in the schema at all
  (they need the supplied ids), so splitting M1-501 across two modules would have put half of one
  row's contract behind a boundary the other half cannot cross.

`test_the_schema_alone_still_accepts_an_empty_attribution_field` pins the split from the inside, the
idiom M1-402 established and M1-403 reused.

### Decision — three lists are required, and three stay optional (owner decision)

Required non-empty: `evidence_adjustments`, `load_bearing_facts`, `failure_modes` — exactly the set
the row names. Each of the three that stays optional has its own reason, and each has a test so the
absence is a decision rather than an oversight:

- `source_disagreements` — the prompt's own shared-fields example prints it as `[]`. Requiring it
  would fail a model that followed the prompt exactly, which is the test M1-402 applied to this very
  field and M1-403 applied to the priors.
- `uncertainty_notes` — not named by the row.
- `base_rate.source_ids` — `prompts/forecaster.md:30` says "if none is defensible, say so and use a
  broad prior", so a base rate can rest on no citable reference class at all. Ids that *are* present
  are still resolved; only the *presence* requirement is declined.

### Decision — the evidence rules stand down when there was no evidence (owner decision)

The three rules that require a citation — both evidence lists being non-empty, and each of their
entries citing at least one id — apply **only when the packet supplied at least one document**.

A zero-document packet is a real state, not a bug: `research/store.py` names it outright, "a question
researched and found nothing", and refuses to *replay* one for exactly that reason. **M1-504** owns
the gate over it, with `forecast.fail_on_stale_research` and `forecast.flag_on_stale_research` as its
committed config, and that row depends on this one.

So the alternative was not "be stricter", it was "decide M1-504's question here and remove its knob"
— and pay for the decision at two billed calls per question, because an unsatisfiable rule fails
through the repair loop. That is precisely what `binary.py::_require_config` refuses for an inverted
bounds pair.

**Nothing passes silently.** The unconditional half still bites with nothing supplied: a citation
naming evidence that does not exist is still refused, and `failure_modes` — which needs no research
to write — is still required. What M1-501 declines to decide is whether a no-research forecast may
proceed at all. `test_the_evidence_rules_stand_down_exactly_when_nothing_was_supplied` states the
condition as a biconditional, so an implementation that dropped it or inverted it fails in one
direction or the other.

### Decision — the checker takes primitives, not a `ModelInput` (and why that is not tidiness)

The natural signature takes the built reasoning packet: it carries both the question id and the
source mapping, and a caller could not then pair a question with another question's ids.

It is not taken. Importing `forecast.inputs` reaches `questions.model` and through it
`forecasting_tools`, `litellm`, `httpx` and `streamlit` — the coupling `inputs.py` documents and
**M1-204** was filed for. `forecast/schema.py` and `forecast/binary.py` are both deliberately clean
of it because M1-406 must replay a stored response and reproduce the parsed forecast with the
provider client not importable at all, and M1-306 established that zero-calls is a property of the
import graph rather than of a mock count. A validator M1-406 and M1-602 both have to reach is the
last module to make SDK-dependent.

The mapping is one line at the one call site. The pairing risk it gives up is already closed twice
over: `generate_forecast` refuses a packet belonging to another question before anything is spent,
and `build_model_input` refuses it again.

### Decision — no message renders the supplied ids, unlike `binary.py`

M1-403 renders its configured bounds into the repair turn, and argued the case at length: the prompt
prints 0.001–0.999 as a literal while config may narrow it, so a model told only "out of bounds" has
nothing to aim at, and an error nobody can act on is its own failure mode.

**That argument does not carry here, and the asymmetry is deliberate rather than an inconsistency.**
The model is holding the entire id list already — `forecast.inputs` put it in the request under
`research_documents`, which is where the ids came from. Naming them back buys nothing, and it grows
every problem message by every document retrieved (`retrieval.max_queries_per_question` ×
`max_documents_per_query` is 48 at the committed defaults). Neither side is rendered: not the cited
value, which is model output, and not the supplied set, which reaches this function from a caller.

`_citation_problems` also aggregates: one problem per rule per location, never one per offending id.
A per-id list would leak **how many** citations were bad through a channel no leak test that reads
only message text would see. M1-302's rule is that a channel is a channel.

### Deviation — none

Nothing here departs from the SDK, the spec, the prompt or a sibling module. The two departures from
`binary.py`'s shape — primitives instead of a value object, and a message that renders nothing — are
recorded above as decisions with their reasons, and neither changes an interface `binary.py` owns.

### Rejected — options weighed and not taken

- **`min_length=1` in `schema.py`.** See the placement decision above.
- **Requiring a citation on `base_rate.source_ids`.** The prompt explicitly permits a broad prior
  with no defensible reference class. A rule that fails a reply which followed the prompt exactly is
  a rule that will be repaired around, not obeyed.
- **Refusing a zero-document packet in `generate_forecast`'s preflight.** Strictly the simplest
  implementation, and pre-spend, which is normally this project's tiebreak. Rejected because it
  decides M1-504's row from inside M1-501 and leaves that row nothing to configure.
- **Requiring that every `load_bearing: true` adjustment also appear in `load_bearing_facts`.** A
  cross-field consistency rule the prompt never states, over two lists it never says are related.
  Inventing a contract and then enforcing it is how a validator starts failing correct output.
- **Reporting one problem per offending id.** Leaks the count. See above.
- **Rendering the supplied ids in the message.** See above.
- **A `ValidatedForecast` value object** carrying the response plus its resolved citations.
  `BinaryForecastResponse` plus `ModelInput.sources` already *is* that pair, M1-602 consumes both,
  and a third near-identical shape is a third thing to keep in agreement (M1-403 rejected the same
  option for the same reason).

### Deferred (do not read the absence as an omission)

- **The stale/insufficient research gate — M1-504.** Whether a forecast built on no documents, or on
  stale ones only, may proceed at all, and whether that flags or fails. This row makes the state
  visible and refuses to invent evidence; it does not choose the policy.
- **The multiple-choice option set (M1-404) and the numeric percentile levels (M1-405).**
  `test_the_rules_are_cross_type` asserts M1-501's rules on all three question types; nothing here
  reads an option list or a percentile level, and `generate._output_problems` still has exactly one
  type-specific branch.
- **The comprehensive valid/invalid golden set — Codex's T-901**, authored blind from spec. One
  companion fixture ships here because this row's tests need the ids M1-403's golden cites.
- **Binding an approval to a payload — M2-707**, and **the canonical stored form — M1-602**. This row
  is M1-602's last dependency; it deliberately decides nothing about `record_json` or
  `final_prediction_json`.
- **`M1-204`** — the `questions/__init__.py` re-export block that makes any importer of the canonical
  question model load the provider SDK. Pre-existing, filed, and the reason for this module's
  signature rather than something it fixes.
- **The prompt's evidence caps — filed as `M1-505` while implementing this row.**
  `prompts/forecaster.md:52-53` caps what a document may justify by its `reliability_tag` and
  `provenance`: an `unverified_social` document "may justify a `tiny` or `small` adjustment at most,
  never a load-bearing fact". M1-501 resolves a citation to a supplied `source_id`, and a `source_id`
  is only an *identity* — so a forecast can still rest a load-bearing fact on one unverified social
  post and be recorded as valid. Enforcing the cap needs the documents themselves, not their ids,
  which is a wider input than this checker takes and would have made it SDK-dependent for the reason
  the signature decision gives. A row, not a fix on this branch.

### Standing risk — not verifiable offline

- **A repair turn's effectiveness is unmeasured.** The tests establish that a model shown
  `evidence_adjustments.0.source_ids` and told the citation was not supplied can correct it in one
  further call. Whether a real model does so — rather than dropping the claim, or inventing a
  different id — is not something an offline suite can answer.
- **The conditional rests on a count, and the count comes from retrieval.** "At least one document"
  is the only threshold M1-501 can defend without reading config. A packet with one stale, irrelevant
  document turns every evidence rule back on, which is correct for this row and is exactly the case
  M1-504 exists to judge.
- **Nothing checks that the ids in `ModelInput.sources` are the ids the model was actually sent.**
  They are, structurally — `build_model_input` builds the request and the mapping from one sorted
  list in one pass, and `test_source_ids_are_a_bijection_independent_of_the_supplied_order` pins the
  ordering — but this module is handed the mapping rather than the request, and takes it on trust.
  Closing that would mean parsing the rendered request back, which is a worse dependency than the
  one it removes.

### On the property tests, and the check that they discriminate

Nine properties, and **nineteen mutations were run against the source before any of them was
trusted** (`docs/LESSONS.md` #5): each of the seven rules removed; the conditional inverted; the
conditional dropped; the cited value spliced into the message; the supplied set rendered into the
message; only the first problem reported; a location the schema never declared; the list index
dropped from a location; `base_rate` citations not resolved; the parent error raised instead of this
module's own; a `str` accepted as a sequence of ids; the checks never reached from `_parse`; and the
wrong `question_id` handed down. **Nineteen of nineteen caught**, at the gate's own profile (`dev`,
200 draws) rather than `fast` — that was M1-403's harness bug, and 25 draws let a real mutation
escape one run in three. `__pycache__` is swept and `PYTHONDONTWRITEBYTECODE` set: a same-size
mutation restored inside one second is otherwise served back from cache.

**The interesting result was the split, and it found a real weakness in a property rather than in
the source.** On the first run the unit suite caught all nineteen and the properties caught **nine**.
The mutations the properties missed were mostly one shape: R1, R3, R4, R5 and R7 *removed outright*,
and the supplied set rendered into the message. The reason is that
`test_the_evidence_rules_stand_down_exactly_when_nothing_was_supplied` is **one-sided** — it asserts
that the three conditional rules produce nothing when nothing was supplied, and says nothing about
their firing when something was. That is enough to catch the condition being inverted or dropped,
which is what it was written for, and it is not enough to notice a rule that quietly stopped
existing. This section originally claimed more than it delivered.

Re-measured after the fix below, property leg only: **seven of those eight are now caught by the
properties alone**, so the properties account for eighteen of the nineteen without the unit suite.

Closed by adding `test_the_problem_set_is_exactly_what_the_seven_rules_specify`: the truth table
enumerated mechanically from the *drawn* inputs, asserted as set **equality**, with every message
written out in the test as a literal rather than imported from the module. That is M1-308 round 5's
remedy applied to a checker instead of a startup path, and the duplication is the point — the
failure mode it exists for is not "the rule is written wrongly twice", which the unit suite pins
against literals, but "a rule quietly stopped firing", which every one-sided property misses.
`test_the_truth_table_property_is_not_vacuous` reports the split through a hypothesis `event`
(**91.7% of draws produce at least one problem**), because a truth table asserted only over inputs
that produce nothing is a test that the function returns an empty list.

The one mutation the properties still miss is "a `str` accepted as a sequence of ids" — measured,
not assumed — and it stays missed on purpose: the never-raises property accepts either a problem list or this module's own
error, which is exactly the invariant it claims. The unit suite owns that case, with the six other
caller-shape mistakes beside it.

The two worth reading are still the pair, M1-403's shape adapted.
`test_an_attribution_problem_never_varies_with_the_cited_id_that_failed` says the output is invariant
in the model's value; `test_the_invariance_property_can_see_the_supplied_set_change` says it is
**not** invariant in what was supplied. Either alone is satisfied by a constant string, which would
pass every leak check while making the checker useless.

`_attribution_location_resolves` reuses `_resolves_through_the_schema`, which walks `model_fields` in
the *test* rather than importing `schema._schema_field_names` — a property that asserts against the
constant the implementation uses passes whatever that constant says (M1-303's lesson). It drops
all-digit parts first, because `schema._sanitize` renders an int `loc` part as `str(part)` and this
module spells its nested locations the same way, so `evidence_adjustments.0.source_ids` is
indistinguishable from a location the sanitizer produced.

### Review round 1 — one blocking finding, rebutted by execution, with two real fixes behind it

Reviewed commit `84510a2`, which was `HEAD`; the diff against `HEAD` was empty, so the finding was
not stale (the check that has cost this project three rounds elsewhere). The reviewer ran the
focused suites at that commit and confirmed all five declared risk areas safe.

**The finding:** *"Binary forecasts are accepted without the required prior."* It cites
`schema.py:267`, notes that `attribution._problems` checks neither prior, and states the reachable
path as *"`generate_forecast` then treats the response as valid after attribution and bounds
checks."*

**Reproduced before any fix code, and the reachable path does not hold.** The rule exists — it has
since **M1-403**, in `binary.py`, which `generate._output_problems` reaches for every binary
response. Three executions at `84510a2`:

1. The reviewer's literal reproduction — `attribution_problems` alone on a priorless golden —
   returns `[]`. **True**, and deliberately so: the rule is binary-specific by construction and this
   module reads no question type for its own rules.
2. `binary_output_problems` on the same response returns
   `base_rate.prior_probability: must be supplied for a binary question` and the same for
   `model_prior`.
3. `generate_forecast` driven with a priorless reply that stays priorless: **2 invocations,
   `forecast=None`, `failure_code="schema_invalid"`**, both prior problems in `failure_problems`.
   The composed path refuses it, spends the one repair M1-402 budgets, and returns no forecast.

So the finding fails the second scope test — the condition is neither introduced nor amplified here —
and the stated impact, a priorless forecast reaching the ledger as valid, is not reachable. A
finding that cannot be reproduced gets a rebuttal, not a fix.

**But the reviewer was reading a real defect, and it was ours.** `schema.py`'s `_reject_priors`
docstring said the converse rule *"is M1-501's row"*. That was true when M1-402 wrote it; M1-403 then
took the rule, and the owner settled it there because it is binary-specific. Nobody updated the
pointer. Once M1-501 shipped without the rule, that sentence became a live trap: it tells a careful
reader to look in `attribution.py`, where the rule correctly is not. It cost a blocking finding and a
review round, which is precisely the price `docs/LESSONS.md` puts on a stale cross-reference.

Fixed on this branch, in three places:

- `forecast/schema.py` — the docstring now names `binary.py`/M1-403, and records why it used to say
  M1-501 and what that cost, so the correction cannot be re-reverted as a tidy-up.
- `tests/unit/test_forecast_schema.py` — `test_a_binary_response_may_carry_priors` repeated the same
  claim in its docstring; corrected the same way.
- `tests/unit/test_forecast_attribution.py` — two new tests pin the split **from M1-501's side**,
  which is the side the reviewer was standing on:
  `test_the_binary_prior_rule_belongs_to_binary_py` (this checker is silent, M1-403's is not, and it
  names both spellings) and
  `test_the_two_checkers_compose_so_a_priorless_binary_forecast_is_refused` (the composed
  `_output_problems` returns exactly the two prior problems). Both were mutation-checked: removing
  the prior rule from `binary.py` fails both.

**And one real gap, filed as `M1-506`.** The finding's underlying instinct — that reading one checker
in isolation makes a rule look absent — is a property of the seam, not of the reviewer.
`generate._output_problems` composes the checkers, but it is private to the call path, so M1-406
replaying a stored response and M1-602 validating a record before persisting it would each have to
know the full list and call every member, with nothing to tell them when the list grows (M1-404 and
M1-405 both add to it). One public composed entry point, with `generate._output_problems` defined in
terms of it, closes that. A row rather than a fix here: it is M1-406's and M1-602's interface, and
pre-deciding it on a branch whose review is about attribution fields is how two items end up
disagreeing about one function.

**Process note, taken.** The reviewer observed that the mutation campaign "enumerates only the
implementation's seven chosen rules, so it could not detect omission of this eighth authoritative
requirement." That is exactly right as a statement about mutation testing, and worth writing down
next to the one-sided-property lesson above: **a mutation harness measures whether your tests defend
the rules you wrote; it is silent about a rule you never wrote.** The defence against a missing rule
is the acceptance criterion read against the spec, and the reviewer is doing that job — which is why
a finding filed off a wrong pointer still found a defect worth two source fixes and a backlog row.

## M1-602 — Persisting immutable forecast versions

Acceptance: *updating a question appends v2; v1 remains byte-identical.*

`forecast_records` shipped in `001_initial.sql` with M1-601 and has been *read* by
`approval.py`, `submission.py` and `lifecycle.py` ever since. Nothing in `src/` had ever written a
row: every `INSERT INTO forecast_records` in the tree was raw SQL inside a test fixture. This item
is that writer.

### Delivered

- `src/whiskeyjack_bot/forecast/record.py` — `ForecastRecordDraft` / `ForecastRecord`,
  `build_forecast_record_draft`, `assign_identity`, `canonical_record_json`,
  `canonical_final_prediction_json`, `record_sha256`, `record_from_json`,
  `RecordedModelSettings` / `RecordedSource` / `RecordedCommunityPrediction`,
  `ForecastRecordError`, `RECORD_SCHEMA_VERSION`.
- `src/whiskeyjack_bot/forecast/store.py` — `append_forecast_version`, `read_forecast_record`,
  `latest_forecast_version`, `mint_record_id`.
- `src/whiskeyjack_bot/migrations/007_forecast_version_chain.sql` — the version/parent chain,
  the JSON-object shape of both JSON columns and the `question_type` vocabulary, as clauses on
  a redefined `forecast_records_require_draft_on_insert`; `LEDGER_SCHEMA_VERSION` → 7.
- `tests/unit/test_forecast_record.py` (39), `tests/unit/test_forecast_store.py` (45),
  `tests/property/test_forecast_record_properties.py` (10 properties). Suite: 2118 pass, 1 xfail.

Nothing here is reachable from the CLI and nothing calls a provider. `submission.enabled: false`
and `dry_run: true` are untouched.

### Decision — the writer decides the version and the parent, and the caller cannot

`append_forecast_version` takes a *draft* — a record with no identity — and returns a
`ForecastRecord` carrying the `record_id`, `forecast_version` and `parent_record_id` the ledger
assigned. A caller passing its own `forecast_version` would be asserting what the chain looks like
from outside the transaction that can see it.

The two are different **types**, not one type with three optional fields. `ForecastRecord`
subclasses `ForecastRecordDraft`, so the writer checks `type(draft) is ForecastRecordDraft` rather
than `isinstance`: an already-appended record would otherwise pass an `isinstance` gate and be
minted a second identity, which is the duplicate `001`'s UNIQUE constraint exists to prevent.

The head is read inside `lifecycle.transaction`'s `BEGIN IMMEDIATE`. The lock is taken up front for
the reason that function's docstring gives: two writers that both read "the head is v1" would both
mint v2, and a deferred `BEGIN` discovers that only on a lock upgrade that cannot be retried from
inside an open transaction. `UNIQUE (question_id, tournament_id, forecast_version)` is the second
line of defence and turns any race that does occur into a loud failure rather than a forked chain.

### Decision — `007` carries the whole invariant, not a subset

`001` gives `forecast_version INTEGER NOT NULL`, a self-referencing `parent_record_id` and the
UNIQUE triple. Those forbid two rows claiming the same version and a parent pointer naming no row
at all. They do **not** forbid `forecast_version = 0`; a version 1 carrying a parent; a version 4
carrying none; a version 2 of question 100 pointing at version 1 of question **200**, or at version
1 of the same question in a *different tournament*; a version 5 pointing at version 1; or a row
pointing at itself. Every one of those satisfies the foreign key perfectly, and `003` blocks UPDATE
and DELETE on this table, so none of them can be corrected afterwards.

Contiguity needs no separate clause: requiring the parent to be version `N-1` of the same
`(question_id, tournament_id)` makes version N unreachable unless N-1 exists, and the UNIQUE triple
forbids two children of one parent. So the chain is a path, not a tree. Self-reference falls out
too — at `BEFORE INSERT` the row's own `record_id` is not in the table yet, so the `EXISTS` finds
nothing.

Both JSON columns are required to be `json_type(...) = 'object'`. That is the **stricter reading**
of "the complete Pydantic record as canonical JSON": `001` made them `NOT NULL` and nothing more,
so `''` and `not json at all` were both permanently storable on a row whose hash claims to attest
to them. Cost stated because the trigger is immutable once merged: storing a non-object in either
column later needs a migration to widen it. Same for the `question_type` vocabulary — widening it
to `discrete` costs one more DROP/CREATE of this trigger, which is `004`'s and `006`'s escape
hatch, not a table rebuild.

### Decision — a `draft`, and no `validated` event

`M1-603`'s notes say this item "must insert at `status='draft'`, supply `forecast_sha256`, and
append a `validated` event; it can do both inside one `transaction()`". The first two shipped; the
third did not, and the owner settled it before any code was written.

`validated` means the output-validation gate passed. That gate is M1-504, and M1-404, M1-405,
M1-502 and M1-503 are all `Not Started`. Appending the event today would put a claim in an
append-only ledger for checks that do not exist. `lifecycle.transaction` nests as a `SAVEPOINT`, so
a caller that wants the record and its first event as one unit still can — `test_the_writer_composes
_inside_a_callers_transaction` asserts both halves, including the rollback.

What the writer *does* run is `attribution.validate_attribution_fields` (M1-501, shipped, public —
its own docstring names "a validation pass over a stored record" as its use), before the transaction
opens. A response citing a source it was never given is exactly the record this project exists to
prevent, and once appended it cannot be withdrawn. The sanitized problem list is carried through
rather than replaced with a constant, for the reason `approval.py` carries `LifecycleError`'s text:
`AttributionFieldError`'s contract guarantees it names no value, and it is the whole account of why
the record was refused.

### Decision — UUIDv7 with a counter, and the first version of it was wrong

`CODEX_HANDOFF.md` asks for UUIDv7/ULID and M1-601 deferred the choice here. UUIDv7 is built from
`os.urandom` and a millisecond timestamp in `store.py`: `uuid.uuid7` is Python 3.14, this project
pins 3.11, and a dependency for eighteen bits of layout would have cost the wave's single
`uv.lock` slot.

**The first implementation was a plain RFC 9562 § 5.7 UUIDv7 and it did not sort.** A plain UUIDv7
orders only *across* milliseconds; two ids minted inside one millisecond carry independent random
draws and sort however the draws fell. That is the case this project actually produces — appending
five versions in a loop — so "record ids sort in creation order" was a claim that failed on its
most common input. `test_minted_ids_are_distinct_and_sort_in_creation_order` found it, which is why
that test mints in a tight loop with no sleep: a version with a `sleep` between draws passes against
an implementation with no counter at all.

The fix is § 6.2's "Fixed Bit-Length Dedicated Counter": `rand_a` holds a counter seeded randomly at
each new millisecond and incremented for every id inside it, so `(milliseconds, counter)` is
strictly increasing. Two edges are handled by holding `_last_milliseconds` rather than trusting the
clock — a **backwards clock** reuses the last millisecond and increments, an **exhausted counter**
advances the stored millisecond — both trading a slightly-wrong timestamp for an id that is still
unique and still ordered, which is the right way round for a primary key. A lock makes the
read-modify-write atomic; nothing enforces single-threaded use, and two threads interleaving would
hand out one counter value twice.

### Deviation — a surrogate **pair** is refused, and a lone surrogate is not

Measured, not assumed. Of everything a record can hold, exactly one shape fails to survive
`model_dump(mode="json")` → `json.dumps(ensure_ascii=True)` → `json.loads`: a UTF-16 **surrogate
pair**. `"\ud83d\ude00"` is two Python code points going in and `U+1F600` coming back, so a record
holding one is stored as something the forecaster did not produce and a replay reproduces the
ledger's version rather than the model's.

A **lone** surrogate is deliberately *not* refused. It escapes to `"\ud800"` and comes back as the
same lone surrogate — verified — so it round-trips exactly, and
`forecast/inputs.render_model_input` says as much about the same rendering rule. Refusing it here
would contradict a sibling in this subpackage over an input that is provably safe.
`research/store.py` refuses both, correctly: its columns hold bare TEXT, which cannot carry either,
while this column holds `ensure_ascii` output, which is pure ASCII and can carry one.

Reachable rather than theoretical, and the *reachable surface is narrower than it first looks*: a
pydantic **constrained** string (`Field(min_length=...)`) refuses a surrogate pair on its own, so
`question.title` is not a way in. A bare `str` and a `NonBlankStr` (a bare `str` with an
`AfterValidator`) both accept one — which covers `resolution_criteria`, untrusted Metaculus text,
and `rationale_summary`, untrusted model output. That distinction is checked in the test rather than
asserted in a comment.

### Deviation — `questions/__init__.py` gutted to a one-line docstring

`record.py` needs `CanonicalQuestion`, and importing `whiskeyjack_bot.questions.model` executes the
package `__init__.py` first, which re-exported `normalize` and so pulled in `forecasting_tools`.
That put the whole SDK on the replay path: M1-306 established that "zero provider calls" is a
property of the **import graph**, not of a mock count, and M1-406 has to reach this schema without
the provider client being importable at all. Measured: importing `forecast.record` loaded
`forecasting_tools`; importing `forecast.schema` did not.

Gutted the way M1-308 gutted `research/__init__.py`, and for the same reason — CLAUDE.md's
convention is that "subpackages get a one-line-docstring `__init__.py`", which this one had been
violating. Blast radius was two test files (`test_questions.py`, `test_groups.py`), both repointed
to submodules; nothing in `src/` imported the package root. Recorded as a deviation because it
touches a merged module under a different item's number.

### Deviation — three merged test fixtures were writing incoherent rows

`007` turned three latent fixture defects into failures. All three are fixture bugs, not
accommodations, and none of them was fixed by weakening the migration:

- `tests/property/test_submission_properties.py::_seed` took `forecast_version` from a global
  counter, so it wrote a version 5 with no versions 1-4 behind it — a chain with a hole. It now
  keeps a real per-`(tournament, question)` chain and appends to it, which satisfies both the
  UNIQUE triple and the parent clause and is what the ledger actually looks like.
- `tests/unit/test_submission.py::_seed_draft` seeded a bare v2. It now takes `parent_record_id`.
- `tests/unit/test_submission.py::test_a_refusal_from_the_ledger_never_echoes_a_stored_value`
  seeded `forecast_version = "two"` on purpose, which `007` now refuses at INSERT. The row is still
  **reachable** — `007` redefines a trigger and adds no backfill probe, so a ledger written before
  it keeps whatever its rows held, which is the population `submission._stored_int` exists to
  refuse — so the test drops the trigger to seed it. That simulates a reachable condition rather
  than inventing one, which is what CLAUDE.md permits.

And one **pre-existing** defect surfaced rather than introduced: `TOURNAMENTS` in
`test_submission_properties.py` is `ENCODABLE_TEXT` filtered only by length, so it can draw `" "` —
which `006_non_blank_identifiers.sql` has refused at INSERT since it merged. The suite had only
stopped drawing one by luck; changing `_seed` changed hypothesis's exploration and it drew one.
`SEEDABLE_TOURNAMENTS` narrows the domain for the two properties that *store* the value, leaving
the ones that only derive a key untouched.

### Rejected — deriving `question_domain` from `source_categories`, and why not

`CODEX_HANDOFF.md` lists "domain and question-type tags" and `docs/M1-NOTES.md` (M1-201) points
`M1-307 / M1-602` at the mapping. Not taken. There is no mechanical mapping from Metaculus
categories to this project's `config/x_accounts.yaml` taxonomy (`econ_data`, `space_launch`, …),
M1-308 settled that domains stay free-form, and inventing one under an item whose acceptance
criterion is about version chains would put a taxonomy nobody reviewed into an append-only column.
`question_domain` stays an optional caller-supplied string. `source_categories` is carried through
inside the stored question, so nothing is lost.

### Rejected — storing the research packet rather than stamping its hash

M1-306 decided against a `research_packets` table because a stored hash that no longer matches the
rows it summarizes is an attribution claim the evidence contradicts. Its notes reserved
`record_json` for the stamp, and that is what shipped: `research_packet_sha256` records the packet
the forecast *saw*, and a later audit compares it against a hash recomputed from the evidence rows.
The stamp lives with the forecast; the truth lives in the rows.

### Rejected — a permissive reader

`read_forecast_record` refuses three things rather than returning a best-effort record: a
`record_json` that does not re-render to exactly the stored bytes, a `forecast_sha256` that is not
the hash of what was read, and indexed columns that disagree with the record they index.

The round-trip comparison is the load-bearing one. `_StrictModel` is `extra="forbid"` but **not**
`strict` — M1-303 round 4 — so pydantic will coerce `"123"` to `123`, and a reader that coerced
would hand back a record that is not what the ledger holds and that hashes to something other than
the stored `forecast_sha256`, silently, as a successful read. Comparing the re-rendered bytes to the
stored text catches coercion, key reordering, dropped defaults and any future drift in the
rendering rule at once, and it is the exact property the ledger needs.

### Deferred (do not read the absence as an omission)

- **Approval state and history, submission attempts, resolution and score events → M1-604 /
  `show`.** `CODEX_HANDOFF.md` lists them under the canonical record; M1-603's own notes settle that
  they are joined at read/export time and never written back, because writing them back means
  updating a stored forecast version, which is what D25 forbids.
- **Validation results and the generated numeric CDF → M1-502 / M1-503**, both `Not Started`.
- **The raw provider response, invocation count and cost → M1-406.** `forecast_records` has no
  column for any of them, and `ForecastGeneration` carries `request` and `raw_responses` precisely
  so that item can persist them separately. `test_the_record_stores_no_hidden_reasoning_and_no_raw
  _response` asserts on the rendered bytes that neither reaches `record_json`.
- **A composed output-validation entry point → M1-506, and the persist path → M1-507.** This
  writer calls `attribution.validate_attribution_fields` by name. **Corrected after M1-506
  shipped:** this paragraph used to say "when M1-506 lands, that call becomes the composed one",
  and it does not. M1-506 built `forecast.validate.validate_output` but deliberately left this
  caller alone — running it here needs `ForecastConfig` threaded into `append_forecast_version`,
  a signature change to a merged public entry point, which is `M1-507`. Left as written, this
  sentence would have told the next reader the gap was closed; that is the exact failure mode
  `schema.py::_reject_priors`'s stale pointer cost M1-501 a blocking finding and a review round,
  so it is corrected here rather than repeated.
- **The `run` / `replay --record-id` CLI wiring → M1-406 / T-903.** This slice is library-only.
- **A community-prediction snapshot.** The handoff says "when technically available"; it is not.
  `normalize.py` deliberately never carries the parent post's payload because it holds
  community-prediction aggregations. The record stores `snapshot: null` with
  `used_as_model_input: false` rather than omitting the field, so a reader can tell "unavailable"
  from "absent". Both snapshot fields are typed `None` and the flag is `Literal[False]`: "community
  prediction is never a forecaster input in v1" is a hard constraint, and a record able to claim
  otherwise would be a place for it to be breached quietly. Making it available later is a
  `RECORD_SCHEMA_VERSION` bump, which is the visible change it should be.

### Standing risk — not verifiable offline

- **`RECORD_SCHEMA_VERSION` is a promise about bytes already written.** Changing any part of the
  rendering rule — key order, separators, `ensure_ascii`, the field set — changes every future hash
  while previously stored records keep their old ones, so an approval bound to one stops verifying.
  `research/hashing.py` makes the same warning about its own rule. If it must change, it changes as
  a new version alongside this one, never as an edit. `test_the_record_carries_exactly_the
  _contracted_fields` asserts the key set as **set equality**, so *adding* a field is as much a test
  failure as removing one — a one-sided assertion would have been vacuous against exactly the change
  that breaks stored records (M1-501's lesson).
- **`PRAGMA foreign_keys` is still never read back.** `007`'s parent clause checks the parent row
  itself, so it does not lean on the foreign key — but the transitive identifier guarantees `006`
  describes still do. Unchanged from M1-607 and filed as `M1-609`.
- **Calendar validity of `generated_at_utc`.** Written through `strftime` from an aware datetime, so
  nothing this writer produces can be malformed; the shape pin is `003`'s GLOB and it pins shape,
  not that the digits name a real instant. Unchanged from M1-603.

### What the review should look at first

The migration, because it is immutable after merge: the parent clause's three conditions, the
`json_type(...) = 'object'` decision and the `question_type` vocabulary. Then the split between what
`record_json` holds and what M1-604 joins — that boundary is D25, and getting it wrong means a
stored version that has to be updated.

### Mutation check

Fourteen mutations, one per invocation, each applied to a pristine copy and reverted after
(a harness killed by a command timeout silently leaves the mutation applied — M1-312's lesson).
All fourteen were killed: `sort_keys`, `ensure_ascii`, the reader's round-trip comparison, the
version increment, the parent link, the attribution gate, the reader's hash and column checks, the
pinned `'draft'` status, the UUIDv7 counter, and four clauses of `007`
(parent, version floor, JSON-object shape, `question_type` vocabulary).

### Round 1 — five blocking findings, all reproduced, all fixed

Reviewed commit `bc64df1`, which was HEAD; nothing was stale. Every finding was reproduced by
execution before any fix code was written, and none was rebutted — all five were real, in scope,
and introduced by this branch.

**B1 — pydantic's `msg` echoes the value that `include_input=False` suppresses.** The project-wide
rule is to rebuild a `ValidationError` with `errors(include_input=False, include_url=False)`. Those
flags suppress the `input` field; they do **not** stop several of pydantic's `msg` strings from
interpolating the offending value into the message text. The discriminated union is the reachable
case: a stored `forecast.question_type` produced `Input tag 'WJLEAKMARKER-secret' found using
'question_type' does not match ...`, which reached `ForecastRecordError` verbatim.

Fixed by dropping `msg` **entirely** and rebuilding from `loc` and `type` only — a field path this
module declared, and a slug from pydantic's fixed error catalogue. `msg` is dropped even for
`value_error` entries raised by this project's own validators, whose texts happen to be value-free
today: an allowlist keyed on error type would make every future validator's wording part of the
leak surface, silently, and this rule has already been breached once by a message nobody wrote.

**And it had a second half the first fix did not close.** Dropping `msg` left `loc` alone, and
under `extra="forbid"` the location of an unexpected key *is* that key — so a stored `record_json`
carrying a key named `WJLEAKMARKER-extra-key` still produced
`(WJLEAKMARKER-extra-key: extra_forbidden)`. Found by probing the fix rather than by the property,
which only ever planted its marker in *values*: **a leak channel a property does not feed is a leak
channel it does not test.** The property now plants the marker as a key too.

Closed by withholding any `loc` part the schema did not author, reusing
`schema._schema_field_names` and `schema._WITHHELD` rather than writing the traversal twice — a
private name from a sibling in the same subpackage, imported deliberately, because a rule written
twice is exactly what finding B4 turned out to be. Mutated **both ways**: withholding nothing and
withholding everything are both killed, so the rule is neither absent nor blanket. A refusal
rendered as `<withheld>.<withheld>` is one nobody can act on, which is its own failure mode.

**This generalizes past M1-602, and is filed as `M0-008` rather than fixed here.**
`errors(include_input=False, include_url=False)` appears in five merged modules and the codebase
has been reading it as "the offending value cannot escape". It is not sufficient, on both counts
above. `forecast/schema.py` and `research/model.py` already guard the `loc` half; `config.py`,
`research/allowlist.py` and `questions/normalize.py` render `err['msg']` and do not guard the
first. **No merged module has a reachable leak today** — checked rather than assumed: the only
discriminated-union adapter in `src/` is `CanonicalQuestionAdapter`, which is defined and never
called from production code. So it is a latent hazard and a false rule, not a live defect, which is
what makes it a row instead of a fix on this branch.

**B2 — the lone-surrogate rationale was right about `record_json` and wrong about the columns.**
The branch argued that a lone surrogate is safe because `record_json` holds `ensure_ascii` output
and is therefore pure ASCII. True, and beside the point: the writer also copies a dozen scalars
into their own bare TEXT columns, and sqlite3 encodes a TEXT parameter as UTF-8 at bind time, so
`question_domain="\ud800"` raised a raw `UnicodeEncodeError` **quoting the character** — a raw
exception out of a public boundary and a leak in the same line. The transaction rolled back and no
row was written, so nothing was corrupted.

Fixed with `_require_storable_in_a_text_column`, a second and narrower rule beside
`_require_replayable`: the first refuses what does not survive the JSON round trip, this one
refuses what cannot be *written*. Their domains genuinely differ, which is why they are two rules
and not one.

Worth recording precisely, because the reachable surface is one field: every other projected column
is a pydantic **constrained** string, and a constrained string refuses a non-UTF-8-encodable value
on its own — the same behaviour that keeps a surrogate *pair* out of `question.title`.
`question_domain` is a bare `str | None`. So the probe is the fix for one field and defence in
depth for eleven, and `test_the_other_column_backed_fields_were_never_reachable` asserts that split
rather than a comment claiming it.

**B3 — the strict reader was checking five columns out of eighteen.** A row whose `question_type`
column read `numeric` while its `record_json` described a binary forecast was returned as binary —
while `approval.read_forecast_summary`, which reads the column, reported numeric. Two public
readers, incompatible attribution, one immutable record.

Fixed by making the projection a single function, `store._projection`, used to write the row **and**
to check it coming back. "The columns agree with the record" is only a real check if the two lists
cannot drift, so there is one list. `test_the_reader_checks_every_column_the_writer_derives`
compares it as set equality against `PRAGMA table_info(forecast_records)`, so a column added to the
table and written without being compared fails.

**B4 — a rule written twice held in one place.** `ForecastRecord` subclasses `ForecastRecordDraft`,
and `store._require_draft` checked `type(...) is` while `record.assign_identity` checked
`isinstance` — so an already-assigned record reached `ForecastRecord(**dump, record_id=...)` and
raised `TypeError: got multiple values for keyword argument 'record_id'`. The branch had *made*
this distinction and then not applied it at the second boundary. Fixed with one shared
`require_unassigned_draft`, which both call.

**B5 — the no-backfill-probe decision has a consequence the branch did not follow through.**
`001`-`006` accepted `forecast_version = 2**63-1`, and `007` deliberately adds no probe, so an
upgraded ledger can still hold such a row. Incrementing it produces `2**63`, which sqlite3 refuses
at bind time with a raw `OverflowError`. Fixed by refusing a head already at `_SQLITE_INT_MAX`
before incrementing.

### Round 1 — a vacuity trap found in this branch's own fixture while fixing B3

Adding thirteen parametrized cases for B3 turned up a defect in the test helper they used.
`_insert_raw` wrote `attempt_id` as a fresh `raw-<uuid>` while the record it built said
`attempt-1`, so **every row it produced already contradicted its own `record_json`** — and every
test asserting "the reader refuses a row where column X disagrees" passed without column X
mattering. Three tests written earlier on this branch were passing for that reason.

Fixed by making `_insert_raw` coherent unless an override makes it otherwise, and by adding
`test_a_coherent_raw_row_reads_back` as the control. The control earned its place immediately: it
exposed that the `retrieval_run_id` case was still vacuous, because the "different" value being
written was the same `RUN_ID` the record already held. A second `research_runs` row fixed it.

This is the same lesson M1-303 and M1-308 both paid for, in a new shape: **a negative test needs a
positive control, or it cannot tell "the check fired" from "the fixture was broken all along."**

### Round 1 — the non-blocking observation, and what was done with it

The reviewer noted that the UUIDv7 counter proves ordering only within one module instance:
separate processes seed `rand_a` independently, so two ids minted in different processes inside one
millisecond do not sort. Correct, and correctly filed as non-blocking — cross-process id ordering is
not this item's acceptance criterion. Recorded here rather than fixed: closing it needs either a
durable reservation or an ordering guarantee scoped in the docstring, and the honest version is the
latter. `mint_record_id`'s docstring already scopes its claim to what the counter provides.

### Round 2 — APPROVE, and the one observation that was worth acting on

Reviewed commit `7117583`, which was HEAD. All five round-1 blockers closed, no blocking findings,
no finding disputed in either round.

The reviewer's non-blocking observation was a real flake in **this branch's own strategy**, and it
is fixed rather than filed: `records()` substituted `rationale or "a rationale"`, but `NonBlankStr`
refuses anything blank *after stripping*, and a whitespace-only draw is truthy — so a rare draw
raised `ForecastSchemaError` while generating an example, turning a required gate red for a reason
unrelated to the code under test. Reproduced by execution (`" "`, `"\t"`, `"\n\t"` all reach the
schema and are refused).

Substituted only for the blank family, never `.strip()`-ed: every draw the schema accepts is fed
**as written**, because normalizing a property's input is how it stops testing what it was written
for (M1-303). `test_the_record_strategy_never_raises_while_generating` asserts the substitution
directly rather than leaving it to the odds of another property drawing one.

The other two observations were correctly non-blocking and are left as they are: cross-process
UUIDv7 ordering needs a durable reservation and is out of this row's scope (the docstring already
scopes the claim to what the counter provides), and `M0-008` is the right home for the generalized
pydantic-sanitizer rule.

### Round 1 — final state

20 mutations, one per invocation from a pristine copy, 20 killed — the original 14, one per
blocking finding, and two for the `loc` rule in both directions, so each regression test is shown to
fail against the pre-fix code. Suite: 2165 pass, 1 xfail. Four gates green.

---

## M1-311 — Reject public-suffix-only official allowlists

Acceptance: *`include_domains` rejects public suffixes, including multi-label suffixes such as
`co.uk` and `com.au`, before any provider call, while accepting registrable domains and their
subdomains.*

PR #16 round 4 closed the single-label case (`_validated_domains` refuses `"com"`, `"gov"`) and
round 5 tightened it to a two-label heuristic (`"." not in host`). Round 6 named the residual that
heuristic left open rather than proposing a partial fix on a review branch:
`include_domains=("co.uk",)` has two labels, clears the heuristic, and `_matches_official_domain`'s
subdomain rule still labels every host beneath it `official` — `co.uk` is a public suffix, not a
site. Filed as M1-311 (`docs/M1-303-NOTES.md:440-444,512-516`), sequenced last in the current
debt-queue wave because it was the only item whose shape turned on a since-answered question:
whether closing it needs a real public-suffix list (a new dependency) or a defensible
dependency-free rule.

### Decision — a real public-suffix list, and `publicsuffixlist` specifically

`docs/TRACKS.md`'s dependency-claim row settled the first half before this branch wrote any code:
"a real public-suffix-list package... rather than a narrower dependency-free rule." A
dependency-free rule (an extra label-count threshold, a hardcoded exception list for `co.uk`-shaped
suffixes) would have been exactly the kind of speculative host-classification logic
`docs/M1-310-NOTES.md`'s canonicalizer history already argues against maintaining locally.

Package choice, confirmed with the owner before implementation: **`publicsuffixlist`**, over two
alternatives considered —

- `publicsuffix2`: also bundles offline PSL data, but is less actively maintained and its bundled
  snapshot lags further behind current registrations.
- `tldextract`: the most widely used option, but by default it caches a *fetched* copy of the list
  and can attempt a network refresh unless explicitly configured with `suffix_list_urls=()` and a
  disabled cache — a worse fit for a suite that runs with sockets blocked (`tests/conftest.py`).

`publicsuffixlist` bundles its PSL snapshot as package data (`public_suffix_list.dat`, verified
present in the installed package), declares zero mandatory runtime dependencies, and its
`privatesuffix(host)` method answers exactly the question this function needs answered: does
`host` have anything registrable *beyond* the public-suffix boundary. Verified by direct call
before writing the fix (`privatesuffix("co.uk") is None`, `privatesuffix("com.au") is None`,
`privatesuffix("bls.gov") == "bls.gov"`, `privatesuffix("data.bls.gov") == "bls.gov"`,
`privatesuffix("bbc.co.uk") == "bbc.co.uk"`), so the one check subsumes the round-5 single-label
rule (a lone label is never more than a suffix) instead of sitting beside it as a second rule.

`publicsuffixlist` ships no `py.typed` marker, so `pyproject.toml` gets an
`[[tool.mypy.overrides]]` entry for `publicsuffixlist.*` — `ignore_missing_imports = true`,
mirroring the existing `forecasting_tools.*` override rather than inventing a second pattern for
the same problem.

### Delivered

- `pyproject.toml` / `uv.lock` — `publicsuffixlist>=1.0,<2` (resolved `1.0.2.20260821`); the mypy
  override above.
- `src/whiskeyjack_bot/research/exa.py` — `_PUBLIC_SUFFIXES: Final = PublicSuffixList()`, built
  once at import time from the bundled snapshot (no network call, so this is safe under the
  socket-blocked suite); `_validated_domains`'s single-label check replaced with
  `_PUBLIC_SUFFIXES.privatesuffix(host) is None`; both docstrings (module-level and the function's
  own) updated to record the residual as closed rather than left stated.
- `tests/unit/test_exa.py` — four multi-label suffix cases added to
  `test_malformed_domain_allowlist_is_refused_before_any_call`
  (`co.uk`, `com.au`, `org.uk`, and a mixed `("bls.gov", "co.uk")`); a new positive-control test,
  `test_a_registrable_domain_under_a_public_suffix_is_accepted` (`bbc.co.uk` must still earn
  `official`).
- `tests/property/test_exa_properties.py` — `_MULTI_LABEL_PUBLIC_SUFFIXES` fixture (hardcoded, not
  sourced from the `publicsuffixlist` instance under test — the M1-303 lesson about not asserting
  an implementation against its own oracle applies to a third-party library instance as much as to
  a private constant); `test_no_validated_entry_is_a_bare_public_suffix`,
  `test_a_multi_label_public_suffix_is_refused`, and a positive-control property,
  `test_a_registrable_domain_under_a_public_suffix_is_accepted`, that validates a synthesized
  `"{label}.{suffix}"` entry unchanged for every sampled suffix.

All six new/changed test cases were run against the pre-fix module first (`git stash` +
`git checkout HEAD -- src/whiskeyjack_bot/research/exa.py pyproject.toml uv.lock`, keeping only the
test edits) and confirmed **failing** there: the four unit parametrizations and the two
discriminating property tests. The two positive-control tests (one unit, one property) passed
pre-fix as well as post-fix, which is what makes them controls rather than redundant negatives.

### Rejected — keeping the old `"." not in host` check alongside the new one

Defense-in-depth was considered and declined: `privatesuffix(host) is None` is true for *any*
single-label `host` regardless of whether that label is explicitly listed in the PSL data, because
with one label there is by definition nothing registrable beyond a suffix boundary — the PSL
algorithm's own default rule. Keeping both checks would have restated one invariant as two,
inviting exactly the kind of divergence-by-partial-edit this item exists to close.

### Deferred (do not read the absence as an omission)

- **No live update mechanism for the bundled snapshot.** See Standing risk below.
- **`_matches_official_domain` is unchanged.** Its subdomain-match logic was already correct; it
  was only ever as safe as its input, and its input can no longer contain a suffix-only entry.
- **AskNews's `canonical_url` gap (M1-309) and the terminal root dot (M1-310)** are both already
  closed on separate branches and are unrelated to this one.

### Standing risk — a static snapshot, not a live list

`publicsuffixlist`'s bundled data is frozen at whatever version is pinned (`1.0.2.20260821`); there
is no network refresh at runtime, deliberately, since the test suite blocks sockets
(`tests/conftest.py`). A public suffix registered after that snapshot would not be rejected until
the pin is bumped. Low impact in practice: every suffix this item's tests assert against (`co.uk`,
`com.au`, `org.uk`, `gov.uk`) has been an ICANN-section entry for decades, and the risk is
one-directional — a missed new suffix means a residual identical in shape to the one this item
closes, not a new failure mode.

### Round 1 — GPT cross-model review (PR #40) — CHANGES REQUESTED, one blocking finding, fixed

Reviewed commit `5f553004d12ceab9c88f8f1a3133b031c3d480a4`. One blocking finding, reproduced by
execution before any fix code was written: `_PUBLIC_SUFFIXES = PublicSuffixList()` ran as a bare
module-level statement at import time, so an ordinary local I/O failure reading the bundled
`public_suffix_list.dat` (a permission error, a broken install) escaped as a raw `PermissionError`
rather than this module's own `ExaFallbackError` — in scope under the threat boundary (ordinary
local I/O failures are reachable reliability conditions, and the reviewer's monkeypatch simulated
exactly one: `open` raising `PermissionError` only for that filename).

Fixed by moving the construction below `ExaFallbackError`'s definition and wrapping it in
`try/except OSError: raise ExaFallbackError(_BAD_PUBLIC_SUFFIX_DATA) from None`, matching the
`except OSError` sanitization pattern already used throughout the codebase (`prompt.py`,
`config.py`, `research/allowlist.py`, `metaculus/snapshots.py`, `research/artifacts.py`). The
message is a bare constant, not path-carrying: unlike the M1-401 carve-out's operator-supplied
config paths, the PSL data file's location is an installed-package implementation detail with no
operator action tied to it, so it stays out of the message like every other constant in this
module. `ExaFallbackError`'s docstring is widened by one clause to say so, rather than left
describing only caller-side mistakes while now also covering this.

Regression test: `tests/unit/test_exa.py::test_an_unreadable_public_suffix_file_raises_the_module_error`,
run in a fresh interpreter subprocess (the singleton is built once at import time, so patching
`open` after `whiskeyjack_bot.research.exa` is already in `sys.modules` — as it is by the time any
other test in the file has run — cannot reach the construction again). Confirmed **failing** against
the pre-fix commit before the remediation (`RAISED:PermissionError`, not `ExaFallbackError`).

**Process note, not a code finding:** this round's request was generated via
`scripts/run-review.sh M1-311 --round 1` (no `--dry-run`), which calls `review-request.py` fresh and
overwrites any hand-spliced `## Deliberate choices` / `## Risk areas` content back to its blank
`TODO(author)` template — confirmed by the reviewer's own remark that the risk-areas section
"contains only its template placeholder," and by re-checking the request file after the round ran.
The blocking finding is unaffected (it does not depend on that section), but the round did not
carry the context those sections exist to pre-empt. Round 2 is generated with `--dry-run`, spliced
by hand, and sent with a direct `codex exec` call rather than through the wrapper, to avoid
repeating this.

### Round 2 — GPT cross-model review (PR #40) — APPROVE

Reviewed commit `bde091164ffec0c41e5330a1b638e77a5c4335a6`. Round-1's blocker confirmed **CLOSED**
against a fresh reproduction: the same unreadable-PSL-data simulation now raises `ExaFallbackError`
with the constant message, with neither `PermissionError` nor `public_suffix_list.dat` appearing in
the rendered traceback. No blocking findings. This round's request was generated with `--dry-run`
and spliced by hand (see the round-1 process note above), so the deliberate-choices and risk-areas
sections reached the reviewer intact this time.

One non-blocking observation, acted on rather than filed: the module-level docstring still said
every exception this module raises is a caller mistake, which became stale the moment the round-1
fix added a case that is not one (an ordinary local I/O failure loading the bundled PSL data).
Fixed directly — the docstring now names both categories — since it is a one-line precision fix
with no behavior change, not a new finding needing its own round.

Suite: 2361 pass, 1 xfail (the standing lone-surrogate xfail predates this branch). Four gates
green on `bde0911` plus the docstring fix.

---

## M1-406 — Persisting raw model output and replaying it

**Acceptance criterion:** *"Model replay makes zero API calls and reproduces the parsed forecast
hash."* Both halves are asserted structurally rather than behaviourally, and that is the whole
shape of this item.

### What shipped

| Module | What it is |
| --- | --- |
| `whiskeyjack_bot/artifacts.py` | The filesystem primitives both artifact kinds share, extracted from `research/artifacts.py`: `ArtifactError`, the safe-path-component rule, the int guard, and the atomic never-overwrite writer. |
| `forecast/parse.py` | A **pure move** of `_strip_fences`, `_NOT_JSON`, `_output_problems`, `_parse`, `_classify`, `ModelSettings` and `ForecastGeneration` out of `generate.py`. No behaviour change; what changed is what importing them costs. |
| `forecast/artifacts.py` | The raw-model-output envelope: writer, reader, `StoredModelOutput`, `MODEL_OUTPUT_SCHEMA_VERSION`. Namespaced under `forecast/`, keyed on `attempt_id`. |
| `migrations/008_forecast_raw_output.sql` | `raw_output_path`, `cost_usd`, `model_invocations` on `forecast_records`, plus three clauses appended to `forecast_records_require_draft_on_insert`. |
| `forecast/store.py` | `ModelCall` (one type for both directions), the three columns as writer-owned, `read_model_call`. |
| `forecast/persist.py` | The artifact-first ordering rule, executed: `persist_generation` and `persist_raw_output`. |
| `forecast/replay.py` | `replay_forecast` and `ForecastReplay`. |
| `cli.py` | `whiskeyjack-bot replay --record-id <id>`. |

### Decision — the split of `forecast/parse.py`, and why the criterion forces it

M1-306 settled that **zero-calls is a property of the import graph, not of a mock count**, and
`test_the_response_schema_reaches_no_provider_client` is that property asserted out of process. A
replay must run *the identical parse* the generating call ran — otherwise it verifies a different
function than the one that produced the record — but reaching that parse inside `generate.py` pulls
`GeneralLlm` and litellm into the replay process. The two requirements are only compatible if the
parse lives in a module that imports no SDK. Hence the move.

The import-graph test is **widened from three modules to eight** and is now the acceptance
criterion itself rather than a property something later rests on. No mock, no double and no call
counter could establish it, because each of those proves only that *this* test made no call.

### Decision — replay re-derives, and that is the opposite of what retrieval replay does

`research/artifacts.py` argues explicitly that its files are **not** the replay substrate:
re-normalizing from raw would make a replayed packet depend on adapter code version, so a bug fix
in a `_to_document` would silently re-derive every historical forecast's evidence — the ledger
rewriting its own history on a refactor. This item does the opposite and it is not a contradiction,
because re-parsing *to compare* and re-parsing *to substitute* are different acts:

- the record stays authoritative; nothing in `replay.py` writes, and `003` blocks UPDATE and DELETE
  on `forecast_records` anyway;
- the re-parsed forecast is rebuilt into a record carrying the **stored** identity and hashed, and
  both hashes are reported;
- a mismatch is an answer, not an exception, and never a new row.

`test_replay_re_derives_rather_than_reading_the_stored_answer_back` is the load-bearing test: it
edits the stored reply to a different but still valid forecast and requires a mismatch. Mutating
`replayed_sha256 = record_sha256(rebuilt)` to `= stored_sha256` kills exactly that test and nothing
else, which is the point — every other test here would pass against the vacuous implementation.

### Decision — the artifact is keyed on `attempt_id`, not `record_id`

An `attempt_id` is minted once per campaign, *before* the call; it is UNIQUE on `forecast_records`
since `004`; and it is the only key a **failed** generation has, since a generation that produced no
forecast produces no record. A call that cost money and returned unusable text is still a call whose
evidence must survive, so `persist_raw_output` writes the artifact for one on its own. The result is
a file with no row pointing at it, which is the same benign direction `research/persist.py` already
documents and deliberately does not tidy up.

### Decision — the three columns are indexed beside the record, never hashed into it

`RECORD_SCHEMA_VERSION` is a promise about bytes already written. Adding a field to the record
changes every future `forecast_sha256` while stored records keep their old ones, so an approval
bound to one stops verifying — and `test_the_record_carries_exactly_the_contracted_fields` asserts
the field set as *set equality* precisely so that change cannot be made quietly. Where the artifact
landed, what the call cost and how many invocations it took are facts about the **call**, not part
of the forecast's content. So they are columns, they join `_WRITER_OWNED_COLUMNS`, and `_insert`'s
docstring is corrected from "every scalar column is derived from the record" to "every *indexed*
column", which is what makes `forecast_sha256` mean something.

`read_model_call` is a separate reader rather than fields on `ForecastRecord` for the same reason: a
reader that returned them together would invite a caller to believe the hash vouched for all of it.

### Decision — the request is stored, unlike the retrieval envelope

`research/artifacts.py` excludes the request outright because a retrieval request's URL and headers
carry the API key. A forecaster request carries none — it is the prompt this project rendered, and
the key travels in a litellm header this module never sees. Without it the artifact cannot show what
the model was actually asked, which is most of what makes it evidence. `GeneralLlm.to_dict()` is
still never called anywhere in this project: it dumps `litellm_kwargs` wholesale, API key included.

**D24 is untouched.** What is stored is the provider's returned text — the reply the parser was
handed — not a reasoning trace, and nothing here has access to one. `record_json` still carries no
raw response, which `test_the_record_stores_no_hidden_reasoning_and_no_raw_response` asserts on the
rendered bytes.

### Decision — a configuration change is visible in a replay, on purpose

`_parse` runs the config-dependent output checks (`forecast.min_probability`/`max_probability`, the
attribution rules) exactly as generation ran them. An operator who narrows the probability bounds
after a forecast was stored gets a replay reporting that the record no longer re-derives. That is a
true statement about the ledger under the current configuration, and suppressing it would make the
instrument agree with itself by construction.

### Deviation — three defects found by this branch's own tests, not by review

1. **A lone surrogate in `raw_output_path` escaped as a raw `UnicodeEncodeError`.** `read_raw_model_output`
   caught `OSError` around `path.read_bytes()`; `open()` raises `UnicodeEncodeError` (a `ValueError`)
   *before* any I/O, because the path cannot be encoded for the syscall. A raw exception out of a
   public boundary is a review finding in this project — it has been, twice. Found by
   `test_the_reader_raises_only_its_own_error`. The refusal must not render the path, since
   interpolating it is itself the failing operation.
2. **`failure_code not in _FAILURE_CODES` raised `TypeError` on an unhashable value**, because
   membership in a `frozenset` calls `hash()`. Present in both the writer and the reader. Closed by
   testing `type(...) is not str` first.
3. **The reader coerced a JSON int to a float** for `cost_usd` and the two float settings fields, so
   `{"cost_usd": 0}` came back as `0.0` as though it had round-tripped. `json.dumps` renders every
   float this writer stores with a decimal point, so a bare int in one of those positions is a value
   the writer could not have produced. The coercion was harmless arithmetically, which is exactly
   what made it worth closing: a silent coercion on an audited value is indistinguishable from a
   value that really was stored that way.

**Number 3 is the one worth recording.** It passed the `fast` profile and failed at 200 draws, which
is the concrete case for the rule that `fast` is never a gate.

### Deviation — a stale docstring claim, corrected rather than inherited

`forecast/inputs.py` and `forecast/attribution.py` both said that importing `forecast.inputs`
reaches `forecasting_tools`, `litellm` and `httpx` through `questions/__init__.py`'s re-export block
(filed as **M1-204**). That block has since been gutted, and a fresh interpreter importing
`forecast.inputs` now loads none of them — measured, not assumed. The correction matters here
because `replay.py` names `SourceReference` at runtime on the strength of it, so `forecast.inputs`
joins the import-graph parametrization: a fact a replay path depends on belongs in the assertion
rather than in prose about it.

**M1-204's acceptance criteria appear already met on master.** Observed, not flipped — that is its
owner's call, and flipping another item's row from this branch is exactly the cross-item edit the
workflow forbids.

### Rejected — a `forecast_model_calls` table, and why not

The right shape for a variable number of calls; wrong here. A forecast is one attempt with at most
two invocations of one model, not a collection, and `research_runs` already carries
`raw_response_path` and `cost_usd` on the run row. A per-invocation table would add a join to every
reader in exchange for a row count that is always 1 or 2, and the repair turn's own text is already
in the artifact the path names. (Owner decision at plan time.)

### Rejected — making `generate.py` a re-export shim for the moved names

The first cut imported all seven moved names back into `generate.py` so no test would change. Ruff
then removed the four it no longer uses, which is the right answer: `forecast.parse` is the real
home, and four test modules now name it directly. A shim would have kept the old coupling readable
and would have made the next reader believe the parse still lives where the SDK is.

### Rejected — a shared `ArtifactOutcome` Literal across the two persist modules

`forecast/persist.py` restates `research/persist.py`'s three-value vocabulary rather than importing
it. They agree today; a shared alias would make one item's decision to add a fourth outcome silently
rewrite the other item's contract.

### Deferred (do not read the absence as an omission)

- **A composed output-validation entry point → M1-506.** Moving `_output_problems` to an SDK-free
  module is adjacent to that row and is not it: the function stays private and uncomposed, and
  `generate` still calls the checkers rather than being defined in terms of a public entry point.
- **Budget enforcement → M1-504.** `cost_usd` is now recorded; nothing reads it as a limit.
- **`pipeline_failure_events` for a failed generation → M1-606's writer.** `persist_raw_output`
  guarantees the text the money bought is on disk and stops there. Writing the failure row here
  would be two writers for one failure.
- **The `run` command → the retrieval/forecast orchestrator.** This branch ships `replay` only;
  there is still no production caller that generates a forecast end to end.
- **`M1-314`**, filed off this branch's property suite: `research/artifacts.py::read_raw_responses`
  has defect 1 above, unfixed. Pre-existing on the diff base, in a merged module, so it is a row and
  not a cross-item fix — and this branch neither depends on it nor increases its reachability.

### Standing risk — not verifiable offline

- **`MODEL_OUTPUT_SCHEMA_VERSION` is a promise about bytes already written**, the same warning
  `record.py` and `research/hashing.py` make about their own rules. Changing `ensure_ascii`, the
  field set, or the float strictness changes what future artifacts look like while stored ones keep
  their shape, and the reader would then refuse them. If it must change it changes as a new version
  alongside this one.
- **A replay verifies the parse, not the model.** It says the stored record is what the stored
  provider text parses to under today's code and configuration. It cannot say the provider really
  returned that text — that is what the never-overwrite rule and the write-before-the-row ordering
  are for, and both are circumstantial.
- **The suite is slower.** `scripts/gate.sh` measured 234s on this branch against the ~85s in
  CLAUDE.md. The property file and three new unit files account for it; no individual test is slow.
  Worth a look before the number normalizes as expected.
- **`PRAGMA foreign_keys` is still never read back** (`M1-609`), unchanged.

### Mutation checks

Six mutations, one per invocation from a pristine copy, six killed:

| Mutation | Killed by |
| --- | --- |
| `ensure_ascii=False` + `surrogatepass` | the round-trip property and the lone-surrogate unit test |
| settings set-equality weakened to a subset check | `test_the_reader_refuses_extra_settings_fields` |
| negative `cost_usd` accepted | one writer case and one reader case |
| `replayed_sha256 = stored_sha256` (read the answer back) | `test_replay_re_derives_rather_than_reading_the_stored_answer_back`, alone |
| `_require_matching_artifact` made a no-op | the wrong-attempt refusal and the no-leak test |
| `008`'s three clauses neutered to `WHERE 0` | all eight direct-SQL cases |

Clauses were neutered to `WHERE 0` rather than deleted: an empty trigger body (`BEGIN END;`) is a
syntax error (M1-607).

### Round 1 — one blocking finding, remediated

Reviewed `5329743`. **CHANGES REQUESTED**, one blocking finding; risk areas 1, 2, 4, 5 and 6 all
returned safe. Risk area 3 — the one flagged as most likely to be wrong — was the finding, which is
the outcome the section is for.

**Finding 1: the reader accepted envelopes the writer cannot produce.** `_settings` enforced set
equality on the nested `model_settings` dict, citing M1-501's lesson about one-sided assertions; the
envelope *containing* it was read field-by-field with `.get()` and enforced nothing about its own key
set. Three sub-cases, all reproduced by execution before any fix was written:

| Sub-case | Pre-fix behaviour |
| --- | --- |
| `cost_usd` key deleted | accepted, read back as `None` |
| unknown top-level key added | accepted, ignored |
| `written_at_utc` re-rendered as `+02:00` | accepted |

The first is the one with audit meaning. `cost_usd: null` is a real thing the writer emits — the call
happened and its cost was not reported — so under `.get()` a **truncated** artifact and one that
**recorded an unknown cost** were the same value, and a replay could report a clean hash match while
inventing the second claim from the first. The third matters because the writer only ever emits
`astimezone(timezone.utc).isoformat()`: checking `tzinfo is not None` accepts a different rendering
of the very instant stored, which is format skew rather than a wrong value.

The fix is the rule already applied one level down: `_ENVELOPE_FIELDS`, set equality checked **after**
the schema-version check so a genuine skew still reports as a skew, and an exact canonical-form
comparison for `written_at_utc`. Missing is now refused; explicit `null` still reads back.

Scope was checked before fixing rather than after: persisted artifact content is untrusted under this
project's boundary, no malicious operator is required (truncation and version skew reach it), the
reader is new on this branch, and all three reproduced at the exact request HEAD.

**Regression cover.** The missing-field parametrization is now driven off `_ENVELOPE_FIELDS` itself
rather than a hand-kept list — the hand-kept list had omitted `cost_usd` and `failure_code`, the two
nullable fields, which is precisely the gap. Added
`test_the_writer_emits_exactly_the_envelope_fields` (a constant compared only against itself pins
nothing), `test_the_reader_refuses_an_unknown_envelope_field`,
`test_a_missing_nullable_field_is_not_read_as_an_explicit_null` (asserting both halves, so it cannot
pass by refusing everything), and `test_the_reader_refuses_a_noncanonical_utc_rendering`.

The existing property replaced a value under an existing key, so it structurally could not reach a
key-set difference. `test_the_reader_refuses_any_envelope_whose_key_set_the_writer_could_not_emit`
draws deletions and insertions instead, with the unedited draw as its positive control.

Three further mutations, one per invocation from a pristine copy, three killed:

| Mutation | Killed by |
| --- | --- |
| top-level set equality → `if False:` | both nullable missing-field cases, the unknown-key test, both halves of the nullable test, and the new property |
| canonical-UTC check → `if False:` | all three `test_the_reader_refuses_a_noncanonical_utc_rendering` cases |
| set equality weakened to a subset check | `test_the_reader_refuses_an_unknown_envelope_field` |

Suite: 2494 pass, 1 xfail. Four gates green.

### Round 2 — approved, final state

Reviewed `e334385`. **APPROVE.** Finding 1 closed; no blocking findings; all five risk areas safe.
The reviewer verified each half of the fix independently — key equality enforced after schema-version
validation, deleted nullables refused while an explicit `null` is still accepted, noncanonical UTC
renderings refused, and no stored value in any diagnostic — and ran the targeted artifact unit and
property suites itself (78 tests).

Two judgment calls in the remediation were put to the round explicitly and both held:

- **the timestamp fix is stricter than the finding required.** The review demonstrated `+02:00`; an
  offset-only check would close that and still admit `Z` and a `.000000` this writer omits. The
  exact-round-trip form is the ambiguity rule applied, and risk area 1 came back safe on the
  ground that the writer normalizes through the same representation the reader demands.
- **the analogous `.get()` shape in `research/artifacts.py` was not fixed here.** Round 2:
  "remains correctly assigned to M1-314 and does not block this branch." Pre-existing, in a module
  this branch does not modify, and not the same defect — that reader is not on a replay path, so a
  field it waves through costs an audit trail rather than a false hash match.

**Two rounds.** The one finding was in risk area 3 — the area named in the round-1 request as most
likely to be wrong — which is what that section is for: it did not prevent the finding, it aimed the
round at it. The other five areas were answered and closed without a round of their own.

The transferable lesson is not the missing check but why 200 property draws never reached it.
`test_the_reader_refuses_any_value_the_writer_could_not_emit` replaces a value under an *existing*
key, so it cannot construct an envelope whose key set differs from the writer's. **A shape property
and a value property are different properties, and the value one cannot subsume the key set.** The
same blind spot had a second half in the hand-kept missing-field list, which omitted exactly the two
nullable fields; both are now derived from `_ENVELOPE_FIELDS`.

Final: 2494 pass, 1 xfail. Four gates green. Both CI checks green on `e334385`.

---

## M1-609 — Verifying SQLite foreign-key enforcement after connection setup

Acceptance: *`ledger.connect` reads `PRAGMA foreign_keys` back and raises `LedgerError` unless it is
enabled, with a message naming the path and no stored value; a test asserts the refusal, and the
existing `journal_mode`/`recursive_triggers` read-backs are the shape to follow.*

Closes the standing risk carried since M1-607 and restated unchanged through M1-602 and M1-406:
`connect()` set `PRAGMA foreign_keys = ON` but never read it back, so the transitive coverage
`006_non_blank_identifiers.sql` describes — every unguarded identifier column is a foreign key into
one of the five guarded primary keys — rested on an assumption rather than a verified setting. Fixed
by giving `foreign_keys` the exact `journal_mode`/`recursive_triggers` shape: read back immediately
after the `= ON` pragma, check `== 1`, close and raise `LedgerError(f"ledger database at {path} does
not support foreign keys")` otherwise. No stored value in the message, matching the other two.

### Deferred (do not read the absence as an omission)

No migration and no new dependency — this is a connection-contract change only, so migration `009`
stays free for the next item in the debt-queue wave.

### Standing risk — not verifiable offline

As the acceptance criteria states up front, no deterministic failure is reproducible on the supported
SQLite runtime: `PRAGMA foreign_keys` is recognized and takes effect outside a transaction on every
build this project runs against, so the refusal branch has no real trigger to exercise. The new test,
`test_foreign_keys_not_enabled_raises_ledger_error`, installs a `sqlite3.Connection` subclass as
`sqlite3.connect`'s `factory` (the built-in type is immutable, so its bound method can't be
monkeypatched directly) that intercepts only the bare `"PRAGMA foreign_keys"` read-back and reports
it disabled, leaving every other pragma and query untouched. This exercises the refusal branch's
logic, not a reproduced defect.

Four gates green.

---

## M1-506 — One composed output-validation entry point

The row was filed off **M1-501's round-1 cross-model review**, and the filing is worth restating
because it is what decides the scope. The review reported *"binary forecasts are accepted without
the required prior"* against `attribution.py`. The rule was in `binary.py`, the composed path did
refuse the response, and the finding was reproduced by execution and rebutted in round 2 — at the
price of a round. The reviewer was not careless. `parse._output_problems` composed the checkers but
was **private with one caller**, so nothing a reader could reach showed the two layers together.

The deliverable is `forecast/validate.py`: `output_problems` / `validate_output`, a
`ForecastOutputError`, and a table keyed on the `question_type` literal with an explicit entry per
supported type. `parse._output_problems` is **gone**, not delegating.

### Decision — a new module, not an extension of `attribution.py`

The composition has to import `binary.py`. Putting it in `attribution.py` would have made M1-501's
module import M1-403's, which is precisely the cross-type / type-specific split the round-1 rebuttal
turned on: `attribution.py`'s own docstring rests a design decision on reading no question type. One
module per backlog item also keeps the docstring able to say what this item is *for*.

Placing it in `parse.py` was the other candidate and is the smaller diff — the function already
lived there. Rejected: `parse.py`'s stated job (M1-406) is the SDK-free parse, and M1-404, M1-405
and M1-507 would then all import `forecast.parse` in order to *validate*.

### Decision — the registry is total over the supported types, and `None` is an entry

`_TYPE_CHECKERS` carries all three keys today; two are `None`. That is deliberate and it is what
makes the acceptance criterion's coverage test discriminating **now** rather than after M1-404 and
M1-405 land: `None` means "supported, and no type-specific checks yet", which is a decision, and the
absence of a key means nobody decided. The set of keys is asserted equal to
`get_args(SupportedQuestionType)` in both directions — `schema.SUPPORTED_RESPONSE_TYPES`' idiom, and
`test_lifecycle.py`'s both-directions argument, because a subset assertion either way is green for
one of the two defects.

### Decision — `_TypeChecker` is typed over `Any`, and this is the one loose annotation

`binary_output_problems` takes a `BinaryForecastResponse`, so it is not assignable to a `Callable`
declared over the `ForecastResponse` union — parameters are contravariant and `mypy --strict` is
right to refuse it. The narrowing the table relies on is a runtime fact the type system cannot
express through a heterogeneous mapping: the lookup is keyed on `question_type`, and every checker
exact-type gates its own argument anyway, so a mis-keyed entry surfaces as `BinaryOutputError`
rather than as a wrong answer.

`validate_output`'s own TypeVar is bound to the **union**, not to `schema.ForecastResponseT`'s
broader `_ForecastResponseBase`, so a caller that passes a `BinaryForecastResponse` gets that type
back.

### Decision — one error carrying the whole list

`validate_output` raises a single `ForecastOutputError` with every problem from both layers, rather
than letting whichever member fired first raise its own type. The complete list is the entire
account of why the response was refused; that is `store.py::_require_attributable`'s argument for
carrying `AttributionFieldError`'s text through instead of replacing it with a constant.
`ForecastOutputError` subclasses `ForecastSchemaError` for `BinaryOutputError`'s stated reason, so a
caller writing one `except` clause still catches every route through the entry point — including the
member modules' caller-mistake errors, which is asserted as a property rather than assumed.

### Decision — the private function is deleted, and the criterion is asserted as an absence

*"Defined in terms of it rather than beside it, so the two cannot diverge."* A one-line delegating
wrapper also cannot diverge, but deleting leaves one name for the next reader and makes the property
structural. `test_the_parse_path_has_no_second_composition` asserts that `parse` has no
`_output_problems` and that **no other module in the package** holds `attribution_problems` or
`binary_output_problems` behind the entry point's back. A test comparing the two lists would have
proved only that they agree at the moment it ran.

### Deviation — three stale cross-references fixed, and one of them was ours to fix

M1-501 lost a blocking finding and a round to a docstring pointer that had been true when it was
written. This branch renames the composition, which creates the same condition three times:
`generate.py`'s header, `schema.py::_reject_priors` (the docstring M1-501 already had to correct
once), and `docs/M1-NOTES.md:4020` — which said of `store.py` *"when M1-506 lands, that call becomes
the composed one."* **It does not.** That is M1-507, and the sentence is corrected below rather than
left to mislead the next reader the way its predecessor did. Each correction records what it used to
say, so it cannot be re-reverted as a tidy-up.

### Rejected — per-type adapter lambdas to avoid the `Any`

`{"binary": lambda f, c: binary_output_problems(f, c) if isinstance(f, BinaryForecastResponse) ...}`
types cleanly and reintroduces exactly what the table exists to remove: a second place per question
type for M1-404 and M1-405 to edit, and an `isinstance` narrowing in a project whose stated gotcha
is that `DiscreteQuestion` subclasses `NumericQuestion`.

### Rejected — a re-export shim in `parse.py`

Keeping `_output_problems = output_problems` so no test would change. It would have kept the old
coupling readable and told the next reader the composition still lives where it does not — the same
reason M1-406 rejected the shim for its own move.

### Rejected — running the composed check from `store.py` on this branch

That is M1-507, filed before any code was written here. It needs `ForecastConfig` threaded into
`append_forecast_version`, a signature change to a merged and reviewed public entry point, and this
row's criteria do not ask for it. Same convention as M1-314, M2-709 and M1-608.

### Deferred (do not read the absence as an omission)

- **The persist path → `M1-507`.** `forecast/store.py` still runs
  `attribution.validate_attribution_fields` alone, so a probability outside the configured bounds
  can be persisted even though `forecast.generate` refuses it. Not reachable from the product path
  — `persist_generation` runs the composed check inside the attempt loop before anything reaches
  the writer — reachable by any other caller of the public writer, and widening when M1-404 and
  M1-405 register their checkers. `store.py`'s docstring now names the gap and the row.
- **The multiple-choice and numeric checkers → `M1-404` / `M1-405`.** Their registry entries are
  `None` and nothing approximates them in the meantime. Each is one changed line, which is the
  whole reason this item leads the wave.
- **`forecast/__init__.py` re-exports.** This package has never had any (`research/artifacts.py`
  holds the only `__all__` in `src/`), and adding one for this item alone would make the entry
  point look like a different kind of name than its members.

### Standing risk — not verifiable offline

- **The reachability test's discovery hook is a naming convention.**
  `test_no_output_checker_in_the_package_is_unreachable` walks the package for public functions
  named `*_output_problems`. Every checker follows it today and the entry point falls outside it
  for free (`output_problems` has no type prefix, so no leading underscore before `output`), which
  is asserted rather than assumed. But **a checker named off the convention escapes the test** —
  `check_option_set`, say. Nothing can catch that mechanically without a registration decorator,
  which would move the coupling rather than remove it. Declared here so a reviewer does not have to
  find it, and named in the test's own docstring so M1-404 and M1-405 read it at the moment it
  matters.
- **The walk imports every module in the package**, `generate.py` included, and so pulls litellm
  into the test process. It is a unit test that reads no network — `tests/conftest.py` blocks
  sockets and the run emits its hostname-resolution warning — but it is the one test in this file
  that is not import-cheap. `forecast.validate` itself stays SDK-free and is pinned in
  `test_the_response_schema_reaches_no_provider_client`.

### Tests, and what each mutation killed

Four mutations against `validate.py`, each killed by the test that claims it:

| Mutation | Killed by |
| --- | --- |
| `"binary"`'s checker set to `None` | `test_no_output_checker_in_the_package_is_unreachable` (+5 others) |
| `"numeric"` key removed | `test_every_supported_type_has_an_explicit_registry_entry` (+3) |
| composition order reversed | `test_the_composition_runs_attribution_first_then_the_type_specific_layer` |
| `validate_output` raises the attribution half only | `test_validate_output_raises_with_exactly_the_problems_the_other_half_returns` |
| type-specific layer dropped entirely | `test_the_composition_is_exactly_its_two_layers_in_order` (property) |
| `attribution._citation_problems` made to append the offending ids | `test_a_composed_problem_never_varies_with_the_cited_id_that_failed` (property) |

**One property was drafted wrong and the suite caught it, which is worth recording.** The composed
leak property was first written as `hostile not in message` over independently drawn hostile text,
and it failed on a drawn `"_"` — a substring of `source_ids` and `research_documents`, which the
messages render for reasons that have nothing to do with leaking. That is the precise trap
`test_an_attribution_problem_never_varies_with_the_cited_id_that_failed`'s docstring already warned
about two sections above, in this same file. Rewritten to the project's shape — **invariance**: two
different invented citations in the same place must produce byte-identical composed output. The
mutation above confirms it bites. The lesson is not the wasted draft; it is that the existing
property said so in prose and the prose was not read before writing a new one beside it.

The property section's anti-vacuity guard is `test_the_composition_is_reached_on_both_sides`, which
event-tags the four cells: over 200 draws the type-specific layer bites on **29.8%** and is silent
on 62.3%, and the attribution layer bites on 91.6%. Without that spread the equality property would
be asserting a concatenation with an empty list — the vacuous-property class this project has paid
for more than any other.

## M1-404 — The multiple-choice output path

*"Produce every exact option once with probabilities summing to one. Unknown/missing/duplicate
options fail validation; tolerance is 1e-6."*

The deliverable is `forecast/multiple_choice.py` — `multiple_choice_output_problems` /
`validate_multiple_choice_output`, a `MultipleChoiceOutputError`, and the registry entry in
`forecast/validate.py` that `M1-506` left at `None` for it. Nothing compared a reply's option
labels against the question's before this row: three tests pinned that absence deliberately
(`test_forecast_schema.py`, `test_forecast_generate.py`, and `schema.py`'s own docstring), and one
of them passed a reply naming two options at 0.9995 each as valid typed output.

### Decision — the seam had to widen, and "one changed line" was wrong

`docs/TRACKS.md` and `docs/M1-NOTES.md:4760` both advertise this item as one changed line in
`_TYPE_CHECKERS`. That is true of `M1-405` and false here. `_TypeChecker` was
`Callable[[Any, ForecastConfig], list[str]]` and `output_problems` took `question_id` and
`source_ids`; **none of the three carries the question's option list**, and without it "every exact
option once" is unsatisfiable. `schema.py:184-192` said so already — *"both need the question's
option list, which this module does not read"* — so the gap was known and its size was not.

Four places grew an `options` argument: the callable type, `output_problems` /`validate_output`,
`parse._parse`, and `generate._run_attempts`. `replay.py` was the fifth and was not predicted; it
calls `_parse` directly, which is exactly the coupling M1-406 built it to have.

### Decision — a `Protocol`, not a `Callable` alias, and `binary_output_problems` declares the argument

`options` is keyword-only and `Callable` cannot spell that. Keyword-only is what keeps the table
uniform without every checker taking an argument positionally in an order it does not care about.
`binary_output_problems` gains `*, options: Sequence[str] | None = None` and never reads it; its
docstring says so and says why. The alternatives were both worse and both were M1-506's own
rejected options in another form: a `QuestionFacts` value object builds M1-405's field before
M1-405 has asked for it, and a special case beside the table is the heterogeneous dispatch M1-506
exists to remove.

### Decision — `options` is a **required** keyword on the entry points, not a defaulted one

The same rule M1-506 applied to the registry's own entries: `None` is a decision, not a gap. A
defaulted argument turns a caller's omission from a signature error into a multiple-choice response
validated against no option list — silently passing the only type-specific check that type has,
which is the precise failure the table's explicit `None` entries exist to prevent. The blast radius
was two call sites, because `store.py` still does not reach this entry point (M1-507).

### Decision — the pairing rule lives in the dispatcher, in both directions

`output_problems` refuses a `multiple_choice` response with no option list **and** a non-`None`
option list with any other type. It is stated here rather than in the member checkers because this
is the only layer that sees both the type and the argument for *every* supported type — including
`numeric`, whose entry is still `None` and which therefore has no checker to hold a rule of its own.
Both directions for `test_every_supported_type_has_an_explicit_registry_entry`'s reason: a
one-directional gate is green for one of the two defects. It runs *after* the `question_type` gate,
so an unregistered type reports that rather than sending a caller after the option list.

### Decision — per-option bounds are enforced here (owner decision, 2026-08-26)

The criterion names three failures and a tolerance. The owner settled a fifth rule onto the row:
each option probability must lie inside `forecast.min_probability`/`max_probability`.
`prompts/forecaster.md:130` already tells the model *"Probabilities must be between 0.001 and
0.999"*, so a compliant reply passes unchanged and no repair turn is spent discovering the rule.
What it buys is the ordering: `submission_live._require_probability` refuses an out-of-bounds option
at **post** time, after the forecast is recorded and approved, and a repair turn at generation costs
one call against a live post that cannot happen.

### Decision — exact string equality, and nothing is renormalized

`questions/normalize.py` hands the SDK's labels through byte-for-byte and `schema.NonBlankStr`
validates without stripping, so `" A"` answered against a supplied `"A"` is one *unknown* plus one
*missing*. That is the criterion's word. A rule that quietly accepted the near-miss would store a
forecast against a label the platform will not score, and the disagreement would be invisible in the
ledger. For the same reason a bad sum is refused rather than rescaled: M1-502's criterion is that
*"no arbitrary post-hoc renormalization is hidden"*, and a scaled vector is a distribution the model
did not produce.

### Decision — the bounds and the tolerance are rendered; no label, probability or count ever is

M1-403's asymmetry and M1-501's, applied together and for their own reasons. The bounds are operator
configuration and a repair turn that does not state the actual bound is one no model can satisfy.
The labels are the opposite case: the model is **already holding the option list** — `inputs.py:284`
put it in the request under `options` — so naming them back buys nothing and echoes model output.
Each rule contributes at most one problem, never one per offending label, because a per-label list
leaks *how many* were wrong through a channel no leak test that reads only message text would see.

### Decision — the option list comes from `ModelInput.packet`, not from `question.options`

`generate_forecast` holds both. It passes the packet's copy, which is what `build_model_input`
rendered into the request, so the checker compares the reply against the list the model was actually
shown rather than against a second read of the same source. M1-501 records the converse as a
standing risk for `source_ids` — that module is handed the mapping rather than the request — and
there was no reason to reproduce it. `replay.py` takes the same list from the stored record's own
`CanonicalQuestion`, dispatching on the `qtype` literal.

### Deviation — `_require_config` and `_format_bound` are copied from `binary.py`

Not extracted. Every module in this project owns its own sanitized exception, which is a hard
constraint rather than a style, so a shared helper would have to be parameterized by the error type
it raises — more machinery than the eight lines it saves, and a refactor of a merged and reviewed
module inside an item that is not about it.
`test_this_module_and_binary_refuse_the_same_inverted_config` pins both halves of the copy, the
refusal and the rendered bound, so a change to one that is not made to the other fails there.

### Deviation — one pinned-absence test was inverted rather than deleted

`test_a_non_binary_response_is_not_bound_checked_here` (M1-403's, the M1-402 idiom) asserted that a
multiple-choice reply with two options at 0.9995 returned as typed output in one call. That absence
is what this row closes. The test is now
`test_a_multiple_choice_response_is_option_checked_here`, asserting the two problems and the one
repair turn, and its docstring records what it used to say so it cannot be re-reverted as a tidy-up.
**The numeric half of the old pin survives** as
`test_a_numeric_response_is_not_percentile_checked_here`, because M1-405 still owns that one.

### Deviation — three stale pointers swept, M1-506's own deviation applied again

`schema.py`'s module header and `MultipleChoicePrediction`'s docstring both said the option rule
needs a list *"which this module does not read"* — true when written, and a pointer that says only
where a rule is **not** is how M1-501 lost a round. Both now name `forecast/multiple_choice.py`, and
each records what it used to say.

### Rejected — options weighed and not taken

- **A `QuestionFacts` value object as the third checker argument.** Types cleanly and is genuinely
  more extensible, which is the problem: it decides the shape of the input M1-405 has not specified
  yet, on a branch that cannot know it.
- **A special case for `multiple_choice` beside the table.** Smallest diff to `binary.py` and
  reintroduces the second dispatch path M1-506 deleted.
- **`min_length=2` on `MultipleChoicePrediction.options` in `schema.py`.** The shorter diff again,
  and M1-403's placement decision again: that module's stated scope reads no question, the *missing*
  rule already catches a short list, and a schema failure and a checker problem are not equally
  repairable.
- **Reporting one problem per offending label.** Leaks the count. See above.
- **Rendering the supplied labels in the message.** See above.
- **Refusing an option list longer than some cap**, `submission_live._MAX_CATEGORIES`' rule. That
  bound exists to keep a verification snapshot inside `_MAX_BODY`; nothing here serializes, and a
  cap this row invented would refuse a question Metaculus accepted.
- **Threading the composed check into `store.py`.** That is `M1-507`, filed by M1-506 before this
  branch existed, and it needs `ForecastConfig` *and now `options`* threaded into
  `append_forecast_version`. This row widens that gap and does not close it.

### Deferred (do not read the absence as an omission)

- **The persist path → `M1-507`.** `forecast/store.py` still runs
  `attribution.validate_attribution_fields` alone. The gap this row widens is exactly the one M1-506
  predicted it would: a record whose options are unknown, missing or duplicated can be persisted by
  any caller of the public writer, even though the generating path refuses it. M1-507's signature
  change now carries two arguments rather than one, which is worth knowing before it starts.
- **The numeric percentile levels → `M1-405`.** Its registry entry is still `None` and nothing here
  approximates it. `test_a_numeric_response_is_not_percentile_checked_here` pins that from the
  inside.
- **The submission payload built from a validated multiple-choice forecast → `M2-707`.** This row
  decides nothing about `final_prediction_json` or the option ordering a post uses.
- **The comprehensive valid/invalid golden set — Codex's `T-901`**, authored blind from spec. This
  row ships **no** new fixture: the reply and the question's option list are both read back out of
  `prompts/forecaster.md`'s own multiple-choice block, so the two are a matched pair by construction
  and cannot drift from the prompt.

### Tests, and what each mutation killed

Nineteen mutations against `multiple_choice.py` and `validate.py`, each aimed at the defect the
property it targets is *named for* (LESSONS #9), run at the gate's own profile (`dev`, 200 draws)
rather than `fast`, with `__pycache__` swept and `PYTHONDONTWRITEBYTECODE` set (LESSONS #8).
**17 of 19 killed**, and the two survivors are the two declared as standing risks below — both
survive by construction rather than through a gap in the tests.

| Mutation | Killed by |
| --- | --- |
| each of the five rules removed, one at a time | its own unit test, and `test_the_option_rules_accept_exactly_the_valid_set` |
| exact match relaxed to `.strip()` | `test_matching_is_exact_string_equality` |
| exact match relaxed to `.casefold()` | `test_matching_is_exact_string_equality` |
| tolerance widened to `1e-5` | `test_the_tolerance_is_applied_where_the_criterion_says` |
| bare-`str` `options` guard dropped | `test_an_option_list_that_would_make_a_rule_lie_is_a_caller_mistake[bare-str]` |
| repeated supplied label tolerated | the same, `[repeated]` |
| one problem per offending label (count leak) | `test_an_option_problem_never_varies_with_how_many_labels_failed` |
| offending label spliced into the message | `test_two_different_offending_label_sets_produce_identical_text` |
| only the first problem reported | `test_every_problem_is_reported_at_once` |
| registry entry put back to `None` | `test_no_output_checker_in_the_package_is_unreachable` (+6 others) |
| pairing gate dropped | `test_the_option_list_and_the_question_type_are_paired_in_both_directions` |
| pairing gate made one-directional | the same test's second half |
| `options` never passed to the checker | `test_the_type_specific_rules_reach_multiple_choice` (+ the generate and replay tests) |
| **`>` → `>=` on the tolerance** | **survived — no float is at distance exactly `1e-6` from 1** |
| **`math.fsum` → `sum`** | **survived — the difference is ~1e-16 against a 1e-6 tolerance** |

A twentieth was run separately against `replay.py`, because M1-404 gave that module a branch and
nothing in `tests/unit/test_forecast_replay.py` reached it — every record built there is binary, so
`options` was `None` on every replay and the side that reads the stored question's option list was
dead under test. Forcing `options = None` there is killed by both new tests, and the failure it
produces is worth noting: it arrives as the **pairing gate**, not as a wrong answer. A replay that
silently skipped the option rules is exactly what that gate exists to make impossible.

### On the mutation harness, and a way it can lie that cost this branch an hour

`docs/LESSONS.md` #8 says a mutation check can be answered by stale bytecode. This branch found a
second way it can be answered wrongly, and the mechanism is worth writing down because the failure
**looks exactly like a clean result**.

The harness applies one textual mutation, runs the suite, and restores the file in a `finally`. The
first run was launched in the foreground and killed by a two-minute tool timeout **that did not reap
the Python child**. A second instance was then started, and the two raced: each had read its own
"pristine" snapshot, so each restore wrote back a file the *other* had already mutated. The result
was two live mutations welded into `multiple_choice.py` — the sum comparison left at `>=`, and the
unknown rule left reporting one problem per offending label — while two other mutations reported
"anchor matched 0 times" and were silently skipped.

**`git diff` could not see any of it**: `forecast/multiple_choice.py` is a new file and was
untracked, so the check that would normally catch a stray edit reported nothing to report. The two
greps run to confirm the restore both happened to land on lines neither mutation touched.

Three things now stand between that and a false report, and all three are cheap:

- the harness takes an **exclusive lockfile** and refuses to start while another instance holds it;
- every restore is **read back and asserted equal** to the snapshot it wrote;
- an **audit script** checks, independently of the harness, that every mutation's *anchor* is present
  and every mutation's *replacement* is absent. That is what actually found the corruption, and it is
  the only one of the three that works after the fact.

The rule this generalizes to: **a mutation harness must be able to prove the tree is pristine when it
finishes**, because the state it leaves behind is invisible to the one tool everyone reaches for. A
LESSONS entry is the right home for it; it is not written there on this branch because a workflow-layer
change lands at a wave boundary (lesson 1), and two other lanes are open.

### Standing risk — not verifiable offline

- **The `1e-6` tolerance has no reachable boundary case, so its strictness cannot be tested.**
  There is no probability vector whose distance from 1 is exactly `1e-6`: neither `1.0 + 1e-6` nor
  `1.0 - 1e-6` round-trips to that difference in binary floating point (they land at
  `9.999999999177334e-07` and `1.0000000000287557e-06`). So `>` and `>=` are observationally
  identical here and the `>= ` mutation **survives by construction**, not by a gap in the tests.
  `test_the_tolerance_is_applied_where_the_criterion_says` straddles the boundary by a single ulp,
  which pins the comparison to the right place; nothing can pin its strictness.
- **`math.fsum` is defensive here rather than load-bearing, and the mutation to `sum` survives.**
  With at most a few dozen options of magnitude ~1, naive summation error is ~1e-16 — four orders of
  magnitude below the tolerance — so no reply this checker will meet can be judged differently by
  the two. `fsum` is kept because it makes the verdict exactly order-independent rather than
  approximately so, and `test_the_option_verdict_does_not_depend_on_the_order_answered` states that
  as the property it buys. It is recorded here rather than defended with a test that cannot fail.
- **Nothing checks that the labels in `ModelInput.packet.options` are the labels the request
  rendered.** They are, structurally — `build_model_input` writes the field and
  `render_model_input` prints it in one pass — but this checker is handed the packet rather than the
  rendered request. Closing it would mean parsing the request back, which is a worse dependency than
  the one it removes. The same sentence M1-501 wrote about `source_ids`, and for the same reason.
- **Whether a real model repairs an option set in one turn is unmeasured.** The tests establish that
  a reply told *"must name every option the question supplied"* has everything it needs — the list
  is in its own request. Whether it re-answers rather than dropping the claim is not something an
  offline suite can answer.

### Round 1 — one blocking finding, and the shape of the miss

`GPT_REVIEW_RESPONSE_M1-404_r1.md`, reviewed commit `a6ab19e`. **CHANGES REQUESTED, one blocking
finding, zero non-blocking observations.** Eight of the nine declared risk areas came back *safe*;
risk 4 — "every caller-mistake path raises `MultipleChoiceOutputError`, never a raw error" — did
not, and it was wrong in a way the section above did not anticipate.

**The finding.** `_supplied_options` refused `None`, a bare `str`, a non-`str` member, an empty
list, a repeated label and a blank label, and its docstring said each *"mirrors an invariant
`CanonicalMultipleChoiceQuestion` already enforces"*. It did not refuse a **one-element** list, and
that model's field is `options: list[str] = Field(min_length=2)`. So the guard set mirrored
`min_length=1`.

**Reproduced by execution before any fix was written**, per the standing rule, and the reproduction
went further than the review did. The review showed one case; the question worth answering was
whether *any* singleton reply could pass. Against the committed `0.001`/`0.999` bounds, none can:

| sole probability | verdict before the fix |
| --- | --- |
| `1.0` | *bounds* |
| `0.999` | *sum* |
| `0.9999995` | *bounds* |

The two rules cover the line between them and leave no gap — summing to 1 within `1e-6` forces the
sole probability to at least `1 - 1e-6 = 0.999999`, which is already above `max_probability`. So
the pre-fix behaviour returned a *repairable problem* for a response no model could ever repair,
which is exactly the failure `_require_config` refuses for an inverted bounds pair, reached through
the other argument, at two billed calls per question. That is what makes it blocking rather than
cosmetic, and it is the argument the fix is justified by.

**The fix.** `len(supplied) < 2` raises `MultipleChoiceOutputError`, beside the empty-list check it
generalizes. Three tests, each killed by removing the guard (mutation run with `__pycache__` swept,
`PYTHONDONTWRITEBYTECODE` set, and the restore read back and asserted byte-identical):

- the `[singleton]` case in `test_an_option_list_that_would_make_a_rule_lie_is_a_caller_mistake`;
- `test_no_reply_to_a_singleton_option_list_could_ever_have_passed`, which pins the *reason* as an
  assertion on the committed config — `max_probability < 1 - _SUM_TOLERANCE` — so a future config
  that narrowed the gap and made a singleton satisfiable would fail here rather than silently
  invalidate the guard's rationale;
- `test_an_option_list_of_fewer_than_two_always_raises_rather_than_returning` in
  `tests/property/`.

### Why the existing property did not catch it — the vacuity trap, one layer up

Worth writing down, because the strategy was **not** the problem. `HOSTILE_OPTIONS` already drew
`st.tuples(HOSTILE_TEXT)` and `st.lists(HOSTILE_TEXT, max_size=3)`, both of which produce
one-element lists. The singleton branch was reachable, and was reached, on a large fraction of
draws.

`test_multiple_choice_never_raises_outside_its_own_error_type` passed anyway — before the fix and
after it — because its assertion is only that nothing escapes `MultipleChoiceOutputError`, and it
`continue`s on that exception. A *returned* list of problems satisfies it exactly as well as a
raise. The property was true of both behaviours, so it could not distinguish them.

This is the vacuous-property class the project already tracks, with the usual diagnosis inverted.
The recorded form is *"the strategy cannot reach the branch the assertion is about"*; this one is
**the strategy reaches the branch and the assertion is not about it**. Widening the strategy would
have done nothing. The fix is a second property whose assertion is arity itself, and the mutation
check is what tells the two cases apart — a property that survives removing the code it is named
for is not evidence, whichever of the two reasons it survived for.

### The generalization worth carrying to M1-405

The nineteen mutations, the eight properties and the leak-invariance work above were all sound, and
the miss was upstream of all of it: the guard list was derived from another model's invariants
**by reading them off rather than enumerating them**. Five of six were mirrored. The sixth was the
only constraint about *how many* rather than about what each element is — and arity is precisely
the constraint whose absence makes a rule set unsatisfiable rather than merely wrong, because the
other rules keep answering and their answers look repairable.

Same shape as M1-308's round-7 lesson (when a finding names one exception type from a third-party
parser, enumerate the siblings by execution): the escape is a class, not an instance. Here the
class is "every constraint on the model this guard claims to mirror", and the cheap discipline is
to list that model's fields and check them off one by one. `M1-405` takes an option list's
counterpart in the question bounds and should do exactly that against
`CanonicalNumericQuestion` before its own round 1.

## M1-405 — The numeric percentile path

*"Produce the nine declared percentiles for conversion by forecasting-tools. Percentile levels are
exact; values are finite, ordered and compatible with question bounds."*

### What the criterion is actually guarding against

Before this item a numeric response reached `forecast_records` with **no semantic check at all**.
`schema.NumericPrediction` takes `percentiles: list[PercentilePoint]` with `min_length=1` and says
so outright, deferring every clause here; `validate._TYPE_CHECKERS["numeric"]` was `None`, the
explicit "supported, and no checker yet" entry M1-506 put there for this row to fill. So a reply
carrying two percentiles, or nine descending ones, or values a hundred times outside a closed bound,
was stored as a forecast and handed to M1-503 to fail inside the SDK — where the failure is a
`ValueError` whose message **prints the offending percentiles verbatim**, one billed call after the
point where a repair turn was still free.

The rules are all cheap and all repairable. What makes the row size `M` is that its last clause
needed a channel that did not exist.

### Delivered

- **`src/whiskeyjack_bot/forecast/numeric.py`** — `DECLARED_PERCENTILE_LEVELS`,
  `NumericOutputError`, `numeric_output_problems`, `validate_numeric_output`. `binary.py`'s shape,
  including the returner/raiser pair and the `repr`-rendered bound.
- **The seam widened** — `validate._TypeChecker` is now
  `Callable[[Any, ForecastConfig, Any], list[str]]`; `output_problems` / `validate_output` take
  `question: CanonicalQuestion` in place of `question_id: int`; `binary_output_problems` takes the
  question and gates it; `parse._parse`, `generate._run_attempts` and `replay` thread it.
- **A second preflight in `generate_forecast`** — a question whose `zero_point` is at or above its
  `lower_bound`, refused before any billable call.
- **Tests** — `tests/unit/test_forecast_numeric.py` (36), an M1-405 section in
  `test_forecast_generate.py` (6), a `5b.` section in `tests/property/test_forecast_properties.py`
  (9), and new coverage in `test_forecast_binary.py` / `test_forecast_validate.py` for the widened
  signature.

### Decision — non-decreasing, not strictly increasing

The row says "ordered". `prompts/forecaster.md` says **non-decreasing**, twice (line 48 and line
153). CLAUDE.md's stricter-reading rule would ordinarily resolve that upward; it is not applied
here, and the reason is that the stricter reading *contradicts* the prompt rather than sharpening
it. A reply with two equal values followed the instructions exactly, and refusing it spends the one
budgeted repair call on this project's own inconsistency — `schema.py` makes the same argument about
`source_disagreements`, and `binary.py`'s `_PRIOR_REQUIRED` comment makes it about the priors.

The pinned SDK agrees with the prompt, whatever its message says:
`NumericDistribution._check_percentiles_increasing` raises on `percentiles[i].value >
percentiles[i + 1].value` — a tie passes. Verified by reading `numeric_report.py` at 0.2.92 and by
executing the constructor in `test_a_percentile_set_this_checker_accepts_is_one_the_pinned_sdk_accepts`.

**The cost of that decision is filed, not hidden.** What the SDK then does with a tie is
`_check_and_update_repeating_values`, which **mutates `declared_percentiles` in place**, nudging a
repeated value by `1e-6` inside the bounds or `1e-10` at one — inside the `model_validator`, so on
construction, before `get_cdf` is called. The record stores what the model returned; a CDF built
from it would be built from values this project neither produced nor persists. Against "nothing is
clamped" that is a real divergence. It is **M1-508**, filed against M1-503, because the nudge
happens in the conversion M1-503 owns and every remedy — record the nudged values as a derived
artifact, refuse ties at the conversion boundary, or bump the prompt to ask for strictly increasing
values and re-hash — is that row's or the prompt owner's to weigh.

### Decision — no message renders a value, and round 1 corrected this one

**The first cut rendered the bounds, and the reasoning was wrong.** It read `binary.py`'s
argument — a repair turn that does not name the bound is one no model can aim at — and applied it
here. Round 1 filed it blocking, and reproducing it took one call:

```
final_prediction.percentiles: values must be at least 3.141592653589793 for this question's
closed lower bound (offending input withheld)
```

`binary.py`'s argument holds *there* for a specific reason: `prompts/forecaster.md` prints
`0.001`-`0.999` to the model as a **literal**, while config is free to narrow it, so a binary model
genuinely does not know its effective bound. A numeric model does — `forecast/inputs.py:284-290`
puts `lower_bound`, `upper_bound`, `open_lower_bound`, `open_upper_bound` and `zero_point` into the
request it was sent. Naming them back buys nothing.

`attribution.py` had already drawn this exact line and written it down: it declines to render the
supplied `source_ids` because "the model already holds the whole id list in its own request, under
`research_documents`, so naming them back buys nothing". Two modules, two sides of one distinction,
and this one cited `binary.py`'s side without reading `attribution.py`'s. **The lesson is not that
the rule was unclear — it is that the project had already resolved it in prose, in a sibling file,
and the prose was not read before writing a third module beside them.** That is the same shape
M1-506's round-1 leak-property draft hit.

It is also not confined to a repair turn. A response that fails twice puts these strings in
`ForecastGeneration.failure_problems`, and `forecast/artifacts.py` writes that into the persisted
raw-output envelope. Question fields come from Metaculus payloads, which CLAUDE.md classes as
untrusted; the carve-out that lets a *path* be rendered is about operator-supplied configuration,
not provider content. So a rendered bound was provider data entering the ledger's diagnostics.

The three messages are now constants naming the rule and the question field it is about
(`lower_bound`, `upper_bound`, `zero_point` — names this project's canonical model authored).
Nothing reached through `forecast` or `question` is interpolated anywhere in the module. The nine
declared levels are still named and are a different category: this project's own constant,
transcribed from the hashed prompt, the same thing `schema.response_model_for` prints.

`binary.py` is left alone and the difference is now deliberate — **M1-509** is filed to settle
whether its configured bounds belong in a diagnostic at all, which is a question about
`ForecastConfig` and CLAUDE.md's carve-out rather than about this row.

### Decision — the bound rules are the ones that hold for *any* configuration

`NumericDistribution` applies two tiers. One runs unconditionally: `_check_percentiles_increasing`
and `_check_log_scaled_fields` (`lower_bound > zero_point`, and no value below `zero_point`). The
other runs only under `strict_validation` **and** `standardize_cdf`: `_check_too_far_from_bounds`'s
25%-wiggle and 2×-range tail rules, `_check_percentile_spacing`, `_check_distribution_too_tall`.

Both of those flags are `config.numeric_calibration`, which is a **sibling of** `config.forecast` on
`AppConfig`, not a member of it — so a checker handed a `ForecastConfig` cannot see them. Applying a
conditional rule unconditionally would refuse a forecast a permissive config accepts, and this
module has no way to know which it is looking at. So the line between M1-405 and M1-503 is drawn at
the config boundary rather than guessed at: **M1-405 owns what is true of a percentile set for any
configuration** — the exact levels, non-decreasing values, closed bounds, and `zero_point` — and
M1-503 owns everything `numeric_calibration` decides, along with the 201-point CDF itself.

`test_an_open_bound_constrains_nothing_here` is the clause that proves the rule reads
`open_lower_bound`/`open_upper_bound` rather than applying the bound flat.

### Decision — the question replaces `question_id` on the composed entry point

`output_problems(forecast, config, *, question_id, source_ids)` could not answer this row's last
clause, so a question had to reach it. Adding one *beside* `question_id` would put two sources of
truth for the same fact inside one entry point and then require a cross-check between them — which
is exactly what M2-703's round-1 review said to remove rather than guard. So the primitive is gone
and `question.question_id` is what `attribution_problems` is called with.

`attribution.py` keeps its primitive signature deliberately. Its docstring rests a design decision
on taking primitives — "taking primitives is what makes this module's independence a property of its
own interface rather than of another package's `__init__.py`" — and nothing about this item argues
against that.

**Import cost: none.** `questions/model.py` reaches no provider SDK and `forecast/inputs.py` has
imported it since M1-402; `test_the_response_schema_reaches_no_provider_client` pins both, and
`forecast/numeric.py` was added to the probed list.

### Decision — every checker takes the question, including the one with no use for it

`binary_output_problems` now takes a third argument it has no *rule* for. The alternative was a
per-type adapter in `_TYPE_CHECKERS` narrowing the question — and `validate.py`'s own note on
`_TypeChecker` already rejects an adapter, for the reason that an adapter is a second place per type
to edit and the table exists so there is one. A uniform signature means the type that needs the
question and the type that does not are registered identically, which is what keeps M1-404's change
to a single line.

The parameter is not decoration: binary spends it on the same exact-type gate it already applies to
its other two arguments, and `test_a_question_of_the_wrong_type_arrives_as_this_projects_error`
kills the mutation that deletes it. The *pairing* — question type equals response type — is checked
once in `output_problems`, centrally, so no checker has to reason about the relationship between two
of its arguments.

### Deviation — `docs/TRACKS.md` said "one changed line each" and it was wrong

The wave was planned on M1-506's prediction that M1-404 and M1-405 would each change one line of
`forecast/validate.py` and could therefore run concurrently. That held for the registration and not
for the signature. Both items need the question — M1-405 for the bounds, M1-404 for the option list
— so both would have edited the same three signatures and met at a conflict the registry could not
have warned either about.

M1-405 took the widening; **lane 1 stage B is serial**, and `docs/TRACKS.md` now says so with the
reasoning attached. M1-404 merges master after this lands and does get its one line. `validate.py`'s
docstring records the falsified prediction rather than quietly deleting it, because the shape of the
mistake — a seam sized against the *registration* and not against the *arguments* — is the reusable
part.

### Rejected — re-checking that values are finite

The criterion says "values are finite". `schema.PercentilePoint.value` carries
`Field(allow_inf_nan=False)`, so a non-finite value is refused before any checker is called. A
duplicate check in `numeric.py` could only be reached through `model_construct`, and a test that has
to bypass the schema to reach the branch it asserts is the vacuous-property class this project has
paid for more than any other. Pinned instead as a schema fact that *can* fail —
`test_a_non_finite_value_never_reaches_this_module` drives `Infinity`, `-Infinity` and `NaN` through
`validate_forecast_response` and asserts each is refused.

Recorded here because reading `numeric.py` alone, the rule looks absent — which is the seam defect
M1-506 exists to close, met from a third direction.

### Rejected — a tolerance on the declared levels

Every level is a decimal literal in the prompt and arrives through `json.loads`, which maps a
literal to the nearest double: `0.10`, `0.1` and `1e-1` are the same value, asserted in
`test_a_level_written_another_way_is_the_same_double`. So exact equality is not brittle, and the
only thing a tolerance would buy is accepting levels the prompt does not print. Comparing the whole
tuple rather than the set also settles count, order and duplication in one step — and it is what
makes the ordering rule well defined, since "non-decreasing values" is only a rule once the levels
they belong to are known to ascend.

### Rejected — pre-checking the 201-point CDF or the PMF step cap

`M1-503`'s criterion, verbatim: *"Exactly 201 monotone values for normal numeric questions;
maintained PMF constraint passes."* Nothing here approximates it. `config.numeric_calibration`
(`expected_cdf_points`, `max_adjacent_pmf`, `strict_validation`,
`use_forecasting_tools_standardization`, `calibration_profile`) still has no consumer in `src/`
outside `submission_live.py`'s length check — the same "this item defines them" condition M1-403
found for the probability bounds, and it is M1-503's to define, not this row's.

### Deferred (do not read the absence as an omission)

- **The tie nudge → M1-508**, filed this branch. Above.
- **The persist path → M1-507.** Widened by this item (a malformed percentile set is now refused by
  `generate` and still persistable by `append_forecast_version`) and simultaneously made cheaper to
  close: the composed entry point wants a `CanonicalQuestion`, and `ForecastRecordDraft.question`
  already carries one that `_one_question` has validated against the row's own `question_id` and
  `question_type`. Only `ForecastConfig` is still missing. `store.py`'s docstring and the M1-507 row
  both say so now.
- **The `validated` lifecycle event → M1-504.** Unchanged: `store.py` still does not append one, and
  M1-404, M1-502 and M1-503 are still outstanding.
- **`unit_of_measure` in the prompt's Inputs list.** `forecast/inputs.py` has flagged this as a
  candidate for the prompt's next version bump since M1-402, and it is directly numeric-relevant — a
  percentile *value* has no meaning without its unit, and the bounds it must respect are printed in
  that unit. Not taken here: a prompt bump is a re-hash, a `RELEASED_PROMPT_SHA256` entry, a
  `config.example.yaml` edit and a third test, and bundling it into a checker item would put an
  unrelated attribution-surface change inside this review.

### Standing risk — not verifiable offline

`test_a_percentile_set_this_checker_accepts_is_one_the_pinned_sdk_accepts` constructs a
`NumericDistribution` with `strict_validation=False`, which is exactly the SDK's unconditional tier
and is the whole claim this module makes about agreement. It does **not** establish that Metaculus
accepts the resulting CDF — that is M1-503's boundary and, past it, a live call. The claim in the
docstring is deliberately narrow for that reason.

The 0.25 wiggle factor and the 2× buffer are read from `numeric_report.py` at the pin and are *not*
duplicated here, so there is nothing in this module for a pin move to invalidate. What a pin move
could invalidate is the agreement test itself, which is why it is written as a construction rather
than as a description.

### Tests, and what each mutation killed

Ten mutations against `numeric.py`, run with `HYPOTHESIS_PROFILE=ci` and `__pycache__` cleared
between each (the stale-bytecode trap: a same-size, same-second edit is served from cache).

| Mutation | Killed by |
| --- | --- |
| non-decreasing → strictly increasing | `test_equal_neighbouring_values_are_accepted` |
| levels compared as a multiset (order lost) | `test_the_levels_must_be_exactly_the_declared_ones[right set, wrong order]` |
| levels compared as a subset (count lost) | `test_the_levels_must_be_exactly_the_declared_ones[one missing]` |
| lower bound applied even when open | `test_an_open_bound_constrains_nothing_here` |
| upper-bound check removed | `test_one_ulp_outside_a_closed_bound_is_refused` |
| lower bound inclusive → exclusive | `test_a_value_exactly_on_a_closed_bound_is_accepted` |
| `zero_point` check removed | `test_a_value_below_the_zero_point_is_refused` |
| `zero_point` preflight `<=` → `<` | `test_a_question_whose_zero_point_is_not_below_its_lower_bound_is_a_caller_mistake` |
| question gate removed | `test_every_refusal_path_raises_this_modules_own_type_exactly[a binary question]` |
| `_format_number`: `repr` → `f"{v:.2f}"` | **survived the first pass** — see below |

**The tenth mutation is the one worth recording.** `repr` rather than a fixed precision is M1-403's
`_format_bound` decision, and its reason is that a bound rendered as `0.00` is one the model could
aim at and still miss. Nothing in thirty-five tests and nine properties noticed when it was
replaced: the bounds these tests happened to use (`0.0`, `100.0`, `3.0`, `20.0`) all render
identically either way. Closed by
`test_the_rendered_bound_round_trips_back_to_the_bound_exactly`, which parses the number back out of
the message and compares it to the bound — asserted by round-trip rather than against `repr(bound)`,
because a test comparing against `repr` agrees with any renderer that happens to be `repr`,
including a wrong one. Four of its six parameters fail under the mutation.

**Anti-vacuity.** `test_every_numeric_rule_is_reached` event-tags all five rules; over 200 draws
each of the ten cells is populated — levels exact on 14.8% / not on 80.9%, non-decreasing on 25.8% /
not on 69.9%, closed-lower biting on 39.7%, closed-upper on 44.5%, `zero_point` on 32.5%. That
spread is not free: the first draft drew the nine values freely, and an unsorted list of nine is
non-decreasing about once in 400,000, so the ordering rule bit on essentially every example and the
`iff` property never saw its accepting side. The strategy now sorts on half the draws, and the
`sampled_from` pool is built out of both bounds, one ulp either side of each, and a repeat.

**Two properties were drafted wrong and the suite caught both.**
`test_a_percentile_problem_never_varies_with_the_value_that_failed` first put the drawn value in the
*first* position and constants elsewhere, so a large draw made the ordering rule fire for one value
and not the other and the property was comparing two different rules; it now fills every position,
where a constant list is trivially non-decreasing and only the value-rendering rules can speak.
`test_a_percentile_set_this_checker_accepts_is_one_the_pinned_sdk_accepts` was written as
`assume(problems == [])` over the general strategy, and hypothesis refused it outright — fifty
filtered, none generated. Constructing the accepted case is also the more honest strategy: the claim
is about the accepted set, so that is what should be sampled, and `accepted_numeric_cases` carries
its own vacuity guard asserting the cases it builds really are accepted.

### Round 1 — two blocking findings, both reproduced before any fix

The review is `GPT_REVIEW_RESPONSE_M1-405_r1.md` against `4ace6c6`. Both findings were reproduced by
execution against that exact commit first, per the standing rule; neither was stale and neither was
rebutted.

**Finding 1 — question-field values in the diagnostics.** Written up as its own Decision above,
because the correction replaces a rationale this file originally stated the other way round.

**Finding 2 — `replay_forecast` could raise `NumericOutputError`.** `CanonicalNumericQuestion`
accepts `zero_point == lower_bound`; so does `ForecastRecordDraft`; and so did every writer before
this branch, because numeric had no checker at the pinned base. `numeric._require_question` raises
for that question rather than reporting a repairable problem — correctly, since no percentile set
could satisfy it — and `generate_forecast` refuses it in a preflight before anything is spent.
**Replay has no preflight.** It reads a row that already exists, so it reached `_parse` and let a
member module's exception out of a public boundary, which `replay.py`'s own docstring calls a review
finding in this project. Reproduced by building a draft with `lower_bound=1.0, zero_point=1.0`,
persisting it (the writer accepts it — that is the premise), and replaying.

Fixed with the handler `response_model_for` already had one line above:
`except ForecastSchemaError as exc: raise ForecastRecordError(str(exc)) from None`.

**What generalised.** `parse._parse` carried a comment claiming it "cannot raise, on any of its
paths". Every clause of that claim rested on `generate_forecast`'s preflight, so it was never true
of a caller without one — the comment asserted a property of the *function* that was really a
property of one *call site*. It now says so and names replay as the caller that owes its own
handler. A caller added later owes the same.

### Round 1 — mutations, including the findings themselves

Both findings are pinned as mutations of their own, so a regression reintroducing either fails:

| Mutation | Killed by |
| --- | --- |
| finding 1: `lower_bound` rendered again | `test_no_message_renders_a_question_field_value` |
| finding 1: `upper_bound` rendered again | `test_one_ulp_outside_a_closed_bound_is_refused` |
| finding 1: `zero_point` rendered again | `test_no_message_renders_a_question_field_value` |
| finding 2: replay stops translating | `test_a_stored_question_no_percentile_set_could_satisfy_is_a_record_error` |
| the five messages collapse to one | `test_the_messages_still_distinguish_the_rule_they_report` |
| levels renderer `repr` → `.1f` | `test_the_rendered_levels_round_trip_back_to_the_levels_exactly` |

**One mutation survives and it is equivalent, not a gap.** `repr` → `f"{v:.2f}"` on the declared
levels: all nine have at most two decimals, so `.2f` round-trips exactly and is a *correct*
renderer. The genuinely truncating `.1f` (`0.01` → `"0.0"`) is killed. After finding 1's fix the only
rendered values are that fixed constant, so the renderer choice is a convention here rather than a
load-bearing one — recorded rather than papered over with a test contrived to kill a correct
mutation. Round 1's version of this entry claimed the `repr` decision was load-bearing for the
bounds; it was, and those bounds are gone.

**The leak property had to be re-aimed, and its companion with it.** The old companion asserted the
message *varied* with the question's bounds — that is exactly what must no longer be true. What
replaces it is `test_a_percentile_problem_never_varies_with_the_question_it_was_checked_against`
(scale the bounds by any factor; identical text for a response violating the same rules) plus
`test_the_percentile_messages_still_distinguish_the_rule_they_report`, which is the real
anti-vacuity guard: a checker whose every message was one constant would satisfy every invariance
property here and tell a reader nothing.

**A process note on the mutation harness.** The round-1 harness snapshotted the source to `/tmp`
and restored from that snapshot; one snapshot was taken inside a previous mutation's window, so a
mutation survived into the working tree and was only caught because the suite went red on clean
code. The round-2 harness restores with `git checkout --` and asserts `git diff --quiet` after every
restore. A harness that can silently leave a mutation behind is worse than no harness, because its
"killed" results are then attributed to the wrong change.

### M1-405 — the merge with M1-404, and the seam the two items converged on

M1-404 merged first (PR #47, `f8577eb`). Both items had widened the same seam, independently and
incompatibly, and each had written into its own notes that the *other* would be the easy one-line
case. Both were wrong, and neither could have learned it from `docs/TRACKS.md`, which records deps
and migration numbers and nothing about which signatures an item will touch.

| | `_TypeChecker` | `output_problems` / `validate_output` |
| --- | --- | --- |
| M1-404 as merged | `Protocol`, keyword-only `options: Sequence[str] \| None` | kept `question_id: int`, **added** `options` |
| M1-405 as reviewed | `Callable[[Any, ForecastConfig, Any], list[str]]`, positional `question` | **replaced** `question_id` with `question: CanonicalQuestion` |

### Decision — the converged seam is `question`-only, and `options` is gone (owner decision)

`forecast/inputs.py` builds the packet field as `list(question.options)`, so the option list and
the question are the same fact reached two ways. Carrying both inside one entry point is the
second-source-of-truth shape M2-703's round-1 review filed a lesson about — and the same lesson
M1-405 had already invoked, and a reviewer approved, to remove `question_id`. Applying it once more
was the consistent move rather than a new judgement.

`multiple_choice_output_problems` therefore takes `CanonicalMultipleChoiceQuestion` and reads
`question.options`. Three things fell out of that rather than being designed:

- **M1-404's biconditional option/type pairing gate is retired.** It existed to catch a
  multiple-choice response validated against no option list, and an option list handed to another
  type. Neither is expressible once the list comes from the question that `output_problems`' `qtype`
  gate already pairs with the response. `test_the_option_list_and_the_question_type_are_paired_in_both_directions`
  went with it; `test_a_question_of_another_type_is_refused_before_any_checker_runs` is the
  surviving statement.
- **M1-404's standing risk is retired.** It recorded that nothing verified `ModelInput.packet.options`
  against the labels the request rendered. There is no copy to verify.
- **M1-404's round-1 finding is dissolved rather than carried.** That review found `_supplied_options`
  mirroring five of `CanonicalMultipleChoiceQuestion`'s six invariants by hand, missing
  `min_length=2`. The mirror is gone: `_require_question` gates the type and restates nothing the
  model guarantees, which is exactly what `numeric._require_question` already did (it re-checks
  `zero_point`, which the model does not guarantee, and deliberately does not restate
  `lower_bound < upper_bound`, which it does).

### Deviation — a module docstring claimed more than the constraint needed

`multiple_choice.py` said it imports "no provider SDK **and no question model**", and took
primitives on that basis. The first half is the real constraint (M1-406's replay path). The second
was wrong about where the SDK enters: `questions/model.py` imports only stdlib, pydantic and
`config`. It is `forecast.inputs` that reaches a provider. Both checkers are now in the
import-graph probe in `test_forecast_generate.py`, so the claim is enforced rather than asserted.

### Deviation — two pinned-absence tests, one restored and one deleted

The merge initially re-reverted `test_a_multiple_choice_response_is_option_checked_here` back to
M1-404's pre-inversion `test_a_non_binary_response_is_not_bound_checked_here`, which is precisely
the "re-reverted as a tidy-up" that test's docstring warns about. Restored from master.
`test_a_numeric_response_is_not_percentile_checked_here` went the other way: it survives on master
because M1-404 kept the numeric half of the pin, and M1-405 deleted it when numeric got a checker.
The deletion is correct and the test is gone; the section below it is the same statement from the
other side.

### Where the tests moved, and why none were simply dropped

`test_an_option_list_that_would_make_a_rule_lie_is_a_caller_mistake` tested `_supplied_options`,
which no longer exists. Its cases are kept and pointed at their real enforcement point —
`CanonicalMultipleChoiceQuestion` refuses each shape at the input contract — because losing them is
what would let the guarantee quietly weaken. The property `test_an_option_list_of_fewer_than_two_always_raises_rather_than_returning`
followed the same guarantee to the same place, asserting the `too_short` error type specifically so
a blank or duplicate label cannot make it green for the wrong reason.

`composed_cases()`' fifth element became the paired question rather than a loose option list, and
`_type_specific_half` dispatches through `_TYPE_CHECKERS` rather than an if/else over two types, so
a third registration cannot leave the anti-vacuity guard silently checking two.

### Also closed here — M1-405's round-2 non-blocking observation

`numeric._require_question` still carried a comment saying its two question numbers "are rendered
elsewhere in this module", which round 1's remediation had made false. Corrected.

### M1-405 round 3 — APPROVE, and the one observation is a false claim of mine

`GPT_REVIEW_RESPONSE_M1-405_r3.md`, reviewed `ac73229`. **APPROVE. No blocking findings.** All
three prior findings confirmed closed, including round 2's stale-comment observation.

The one non-blocking observation falsifies **risk 15 of the round-3 request**, which I wrote and
which was wrong. I claimed that dispatching `_type_specific_half` through `_TYPE_CHECKERS` meant
"a third registration cannot leave the guard checking two". The dispatch is registry-driven; the
**strategy feeding it is not**. Verified by execution over 300 draws:

| type | registered | drawn |
| --- | --- | --- |
| `binary` | yes | 110 |
| `multiple_choice` | yes | 190 |
| `numeric` | yes | **0** |

So `numeric_output_problems` is never reached through the composed entry point by any property.
No product defect follows — section 5b covers that checker directly, and the reviewer said so in
declining to block — but this is the project's most expensive defect class reappearing one level
up. The recorded forms are "the strategy cannot reach the branch the assertion is about" (M1-305,
M1-501) and, from M1-404 round 1, "the strategy reaches it and the assertion is not about it".
This is a third: **the assertion is registry-complete and the strategy is not**, so the guard whose
entire job is anti-vacuity is itself partly vacuous, and silently worsens with each registration.

Filed as **M1-510** rather than fixed here: widening the strategy needs a numeric arm that reaches
both a biting and a silent verdict, which is a substantive property change on an approved branch and
would earn a round 4 for a test-only gap the reviewer declined to block.

Two comments were corrected in place, because leaving them is the stale-pointer failure that cost
M1-501 a round: `_type_specific_half`'s docstring now says the *helper* cannot fall behind a
registration while the strategy currently does, and `composed_cases()` no longer says "both
registered types" now that there are three.

**The generalizable rule.** Registry-driven dispatch behind a hand-written strategy is not
registry-complete coverage, and the two are easy to conflate precisely because the dispatch half
looks rigorous. When a guard claims completeness against a table, assert the *draws* against that
table, not just the lookup.

## M1-502 — Validate categorical forecasts

*"Apply binary bounds and exact multiple-choice normalization. Boundary and sum tests pass; no
arbitrary post-hoc renormalization is hidden."*

### Decision — this row is a pin, not a build, and the evidence is why

**There is no `src/` diff, and that is the finding rather than an omission.** M1-502's two
dependencies shipped its mechanism before its own row opened:

- **M1-403** → `forecast/binary.py:174` bounds `probability_yes` inside
  `forecast.min_probability`/`max_probability`, inclusive. `validate_binary_output`'s docstring
  already cites this row by name — *"Nothing is clamped … M1-502's criterion is that 'no arbitrary
  post-hoc renormalization is hidden'"*.
- **M1-404** → `forecast/multiple_choice.py` holds exact string equality against the question's
  option list, per-option bounds, and `math.fsum` against `_SUM_TOLERANCE = 1e-6`. Its own notes
  above (`### Decision — exact string equality, and nothing is renormalized`) quote this row's
  criterion as the reason a bad sum is refused rather than rescaled.
- **M1-506** → `forecast/validate.py:130-134` composes them, and `_TYPE_CHECKERS` has no `None`
  entry left for this row to fill.

Two more facts close the gap structurally rather than by inspection. `schema.Probability` is
`Field(ge=0.0, le=1.0, allow_inf_nan=False)` (`schema.py:137`), so no NaN or infinity reaches a
checker and every comparison below is over an ordered domain. `config.py:192-193` pins both bounds
into `[0.001, 0.999]`, so a configured pair can only ever *narrow* the range
`submission_live._require_probability` enforces at post time — the two gates cannot disagree in the
direction that would matter, which is generation accepting what a post refuses.

A repo-wide sweep for rescaling, clamping or division by a total finds nothing anywhere on the
forecast path. So the criterion's second sentence is the deliverable: make the boundary and sum
behaviour, and the *absence* of renormalization, hold as pinned facts across the whole path rather
than at each module's own edge.

### Decision — "normalization" in the row title is implemented as refusal

The title says *exact multiple-choice normalization*; the code refuses a non-normalized vector and
never produces one. The criterion's own second clause is the argument, and M1-404 made it first: a
scaled vector is a distribution the model did not produce, and the ledger would record a forecast
nobody made, with nothing in the record to show it had been altered. `submission_live`
`_require_categories` reaches the same conclusion from the other end and records it as a stricter
reading — the platform's behaviour for a non-normalized vector is not knowable offline, and neither
silent renormalization nor rejection is something an attribution record should have to explain after
the fact.

### Delivered

Tests, notes and backlog only.

- **`tests/unit/test_forecast_generate.py`** — an M1-502 section: the sum rule end to end. It was
  the one categorical rule with no test of its own on this path. M1-404's
  `test_a_multiple_choice_response_is_option_checked_here` reaches it *incidentally* — its two
  0.9995 options trip the bounds rule as well and its assertion is about both — so nothing said what
  a bad sum alone costs. Four tests: one repair and a successful correction; a sum that stays wrong
  costing exactly two calls with a one-element `failure_problems`; the `1e-6` straddle through
  `generate` rather than at the checker; and the repair turn naming the tolerance and no probability.
- **`tests/unit/test_forecast_multiple_choice.py`** — `test_the_configured_bounds_are_inclusive_to
  _one_ulp`, `test_nothing_at_a_bound_is_nudged_onto_it`, and
  `test_the_verdict_cannot_depend_on_the_order_the_model_answered_in`.
- **`tests/unit/test_forecast_record.py`** — an M1-502 section pinning that the numbers the ledger
  stores are the numbers the model produced.
- **`tests/property/test_forecast_properties.py`** — `_MC_BOUND_NEIGHBOURS`: the strategy now draws
  both float neighbours of every bound in `_MC_BOUNDS`.
- **`tests/unit/test_forecast_validate.py`** — `multiple_choice_output_problems` named in
  `test_the_discovery_walk_would_notice_a_checker`.

### Decision — the boundary work went into the property strategy, not a new property

Section 8 already holds a full independent oracle (`_expected_option_problems`, with the sum
predicate computed in `Fraction`) and an event-tagged anti-vacuity guard. A new "bounds iff" property
beside it would have restated a truth table that was already asserted, so the gap had to be somewhere
else — and it was in the **draws**, not the assertions. `_MC_PROBABILITIES` was ten round values plus
the bounds themselves: it reaches every boundary and can never cross one by the smallest step that
exists. Section 5's `bounds_and_probability` records what that costs in this exact codebase — an
off-by-one-ulp boundary error survived the mutation harness there until the neighbours were drawn
explicitly, "the single earlier catch being hypothesis's boundary heuristics being lucky".

**The strengthening is measured, not asserted.** With the bound comparison mutated to a tolerant
`low - 1e-12 <= p <= high + 1e-12` — the sloppy-epsilon defect one ulp exists to catch — the
truth-table property `test_the_option_rules_accept_exactly_the_valid_set` **fails with the
neighbours in the pool and passes without them**. That is the direct evidence that this is coverage
the section did not have, rather than a strategy edit that looks thorough.

### Decision — `math.fsum` is defensive here, and the surviving mutation is provably equivalent

`fsum` → `sum` survives the harness, and this is the account rather than a gap. The two cannot reach
different verdicts on any admissible vector, and the arithmetic says so: the per-option minimum and
the sum rule together cap the option count at `1 / min_probability` — 1000 at the committed default —
and naive accumulation of n values totalling 1 carries an error of order `n · 2⁻⁵³`, about 1e-13 at
n = 1000. That is seven orders of magnitude inside `_SUM_TOLERANCE`, and a 1000-option vector at
0.001 was executed in three orderings to confirm the two routes agree to the last bit.

`fsum` stays, because the bound depends on `min_probability` remaining non-trivial and that is
config; the cost is zero. `test_the_verdict_cannot_depend_on_the_order_the_model_answered_in` records
the measurement so the survivor is explained where a reader meets it.

**The sum comparison's `>` → `>=` also survives, and is equivalent for a reason already written
down**: `test_the_tolerance_is_applied_where_the_criterion_says` states that no probability vector's
distance from 1 is *exactly* `1e-6`, because neither `1.0 + 1e-6` nor `1.0 - 1e-6` round-trips to
that difference. With the boundary case unreachable, the two operators cannot be told apart.

### Deviation — the criterion was satisfied before its own row opened

M1-404 could not state its criterion ("every exact option once, tolerance `1e-6`") without building
the sum rule, so it built it while M1-502 read `Not Started`. Nothing went wrong; it is worth
recording because a reviewer meeting a `Done` flip with no `src/` diff will otherwise read it as a
row closed without work, and because the same shape is coming for **M1-503** — M1-508 already sits
against it, filed by M1-405.

### Rejected — options weighed and not taken

- **Threading the composed check into `store.py`.** That is **M1-507**, filed by M1-506 before this
  branch existed and widened by M1-404 and M1-405 in turn. A record whose options are unknown,
  missing, duplicated or out of bounds is still persistable by a direct caller of the public writer.
  Taking it here would be scope theft from a row that already names its own signature change.
- **Capping the option count at generation** to match `submission_live._MAX_CATEGORIES = 64`. The
  ordering argument is the one M1-404 used for per-option bounds: a payload with 65 options is
  refused at *post* time, after the forecast is recorded and approved, where a repair turn at
  generation would have cost one call against a live post that cannot happen. M1-404 rejected it
  anyway — *"a cap this row invented would refuse a question Metaculus accepted"* — and that
  argument is unchanged. **Filed as a backlog row** rather than decided twice in notes.
- **A guard for jointly unsatisfiable rules.** With `min_probability = 0.001`, a question carrying
  more than 1000 options makes the per-option bound and the sum rule unsatisfiable together, so
  every reply burns two calls and fails. Real, and unreachable: Metaculus multiple-choice questions
  carry a few dozen, and `submission_live` says so at `_MAX_CATEGORIES`. Filed, not built.
- **Unifying `multiple_choice._SUM_TOLERANCE` with `submission_live._CATEGORY_SUM_TOLERANCE`.** Same
  number, two owners, deliberately independent — `multiple_choice.py:86` says so, and the submission
  package does not import the forecast package.
- **A new "bounds iff" property.** Above: the truth table was already asserted, and the gap was in
  the draws.
- **Renormalizing anything.** The criterion forbids it.

### Deferred (do not read the absence as an omission)

- **The persist path → M1-507.** Unchanged and not widened here; this row adds no rule.
- **The `validated` lifecycle event → M1-504.** M1-603's notes deferred it until the output-validation
  gate existed. With this row closed, **M1-503 is the last of the four it named** (M1-404, M1-405,
  M1-502, M1-503) still outstanding, so M1-504 becomes reachable immediately after it.
- **The submission payload built from a validated categorical forecast → M2-707.** This row decides
  nothing about `final_prediction_json`'s use at post time or the option order a post carries.
- **The comprehensive valid/invalid golden set → Codex's T-901.** No fixture ships here: every reply
  in the new tests is built from `prompts/forecaster.md`'s own JSON blocks, so the reply and the
  option list are a matched pair by construction and cannot drift from the prompt.

### Standing risk — not verifiable offline

- **What Metaculus does with a vector that sums to 1 only within `1e-6`.** This project accepts it
  and stores it unscaled; the platform may renormalize it silently. If it does, the ledger and the
  platform will disagree in the last few bits of a recorded forecast. M2-711's refetch vocabulary is
  where that would surface, not here.
- **The per-option bound is this project's, not the platform's.** `prompts/forecaster.md:130` prints
  0.001–0.999 and `submission_live` enforces the same pair, but neither is read from Metaculus. A
  platform that widened its accepted range would leave this project refusing forecasts it would have
  taken.

### Round 1 — CHANGES REQUESTED, one blocking finding, and it falsified a risk claim I wrote

The request's risk claim 6 said `config.py:192-193` made it impossible for a configured bound
pair to be wider than the one `submission_live._require_probability` enforces. **That claim is
false, and the review falsified it exactly as intended.** `ForecastConfig.model_copy(update=...)`
builds a config *without* running its field validators — a public pydantic API this repo's own
test helpers use (`test_forecast_binary.py::_bounds`, `test_forecast_multiple_choice.py::_narrowed`,
`test_forecast_generate.py::_narrowed`) — and both `_require_config`s re-checked only `low < high`.

Reproduced by execution at the reviewed commit before any fix was written:

- `ForecastConfig(...)` **refuses** `0.0`/`1.0` with a `ValidationError`; `model_copy` on the
  committed config **produces** it.
- `multiple_choice_output_problems` returns `[]` for a `0.0`/`1.0` two-option vector under that
  config, while `submission_live._require_categories` raises `LiveSubmissionError` on the same
  numbers.

The cost is not a repair loop, which is what makes it worse than the `min < max` check sitting
beside it: nothing fails at generation, so a forecast is **billed, recorded and approved** for a
post that deterministically cannot happen.

**Scope.** Pre-existing at the base — but the branch depends on it, documents it as proven, and
closed the row on that basis, which is the "depends on it" arm of the non-blocking test. Fixed
rather than rebutted.

### Decision — the envelope lives in `config.py`, and the checkers read it from there

The obvious fix was to write `0.001` and `0.999` into both `_require_config`s. That would have
made a **fourth** copy of the literals (`config.py`, `prompts/forecaster.md`, `submission_live`,
and now two checkers), which is precisely the silent-drift shape M1-509 already exists for. So
`config.py` names `PROBABILITY_BOUND_FLOOR`/`PROBABILITY_BOUND_CEILING`, its own `Field(ge=…, le=…)`
is expressed in them, and `forecast/binary.py`, `forecast/multiple_choice.py` and
`forecast/generate.py` re-check against the same two constants. The envelope is the spec's
(`CODEX_HANDOFF.md § Configuration schema`: `0.001 <= min < max <= 0.999`), so enforcing it is
faithful rather than a bound this project invented — the distinction M1-511 turns on.

The preflight in `generate.py` is widened alongside the checkers so the refusal happens **before
any billable call**, matching the two preflights already there.

The message names the envelope's two ends and withholds the configured pair. The ends are spec
constants, not values read off this config; `submission_live` words its own the same way. That is
deliberately *stricter* than the surrounding module, which does render the configured pair — see
the M1-509 note below.

### Deviation — this row now has a `src/` diff after all

The round-1 section above supersedes "Decision — this row is a pin, not a build" to this extent:
the pin was right about the mechanism (nothing renormalizes, and that is still true and still
untouched) and wrong about one structural premise it leaned on. Three source files change:
`config.py`, `forecast/binary.py`, `forecast/multiple_choice.py`, plus the `generate.py` preflight.

### Non-blocking observations — what was accepted and what was corrected

- **Risk claim 4 was overstated.** `test_the_repair_turn_for_a_bad_sum_names_the_tolerance_and_no_probability`
  carries no exact invocation-count assertion; the sibling persistent-bad-sum test pins that path
  with both `result.invocations == 2` and `len(client.calls) == 2`. Accepted as stated, no product
  gap, no change.
- **Risk claim 5 was false, and M1-509 is widened.** The new one-ulp boundary tests compare exact
  problem lists built by `_bounds_problem(low, high)`, so they *do* assert messages containing
  configured bounds. The rendering is pre-existing (M1-404 built it), so it is not blocking — but
  the row's framing of `binary.py` as the lone outlier was wrong: `multiple_choice.py` renders the
  same pair. M1-509's description, dependencies and acceptance criteria are widened to cover both
  categorical checkers.
- **The order-independence test really tested one ordering.** With all 1000 entries equal to
  `min_probability`, `reversed()` and `sorted()` return the same list. The reviewer judged the
  analytic argument sufficient and asked for no backlog row; the test is fixed anyway, because a
  test that names ordering and cannot vary it is this project's top recurring defect class wearing
  a different hat. The n = 1000 vector is *forced* — 1000 × 0.001 is exactly 1, so every entry is
  pinned to the minimum — so it stays as the worst case for the accumulation bound, and a
  heterogeneous 500-option vector (each entry distinct, all ≥ the minimum, summing to 1) carries
  the ordering claim across four genuinely distinct orderings, asserted distinct.

### Round 2 — APPROVE, prior finding closed, one row filed

The round-1 blocker is closed at its own reproduction: the `0.0`/`1.0` copied config now raises
from both categorical checkers and is refused by `generate_forecast` before client construction or
any billable call.

One non-blocking observation, and it is a fair correction to **risk claim 4**, which I wrote as
"there is exactly one source for the two literals in `src/`". That was true only of the *forecast*
package: `submission_live.py` still declares its own executable `0.001`/`0.999`, and the new
diagnostics embed the endpoints as literal strings in their messages rather than deriving them
from the constants. Two sources for one spec number, with nothing failing if they drift.

Filed as **M1-513** with the reviewer's acceptance wording. It is deliberately *not* M1-509:
M1-509 asks whether a **configured** pair may be rendered at all, and M1-513 asks who owns the
**spec** endpoints. Non-blocking here because the submission declarations predate both this branch
and the pinned base, and the two sources agree today.

**Two rounds, and the reason is worth recording.** M1-502's round-1 request stated nine falsifiable
risk claims; the review falsified two of them (6 blocking, 4 non-blocking) and confirmed the other
seven. That is the mechanism `docs/LESSONS.md` describes working as intended — the blocking finding
was *found by my own claim being specific enough to check*. A vaguer claim 6 ("config validates the
bounds") would have passed unexamined and shipped the defect.

## T-903 — one saved question, one command, one validated record

The first end-to-end proof the project has. Every stage it needs shipped in an earlier item
and none of them was reachable: `generate_forecast`, `persist_generation`,
`normalize_questions` and `lifecycle.record_validation` had, between them, no caller a
command could reach. M1-406's own notes recorded the gap — "the `run` / `replay --record-id`
CLI wiring" was deferred and "this slice is library-only". The backlog criterion is a
sentence about a command, so this item is the wiring plus the suite that holds it to the
sentence.

`whiskeyjack_bot/pipeline.py` composes it; `run` is the command; `forecast/replay.py` grows
`replay_generation`, a `(question_id, attempt_id)`-keyed artifact reader, because
`replay_forecast` starts from a `record_id` and verifies a row that already exists — the
wrong direction for a run that has no record yet.

### Decision — the record is stamped with a freshly minted attempt id, not the replayed one

`--attempt-id` names the *saved* attempt whose reply is replayed. The record gets a new id.
Reusing the saved one is the obvious alternative and it does not survive contact with the
schema: `idx_forecast_records_attempt_id` is a partial unique index and `004`'s trigger
cross-checks `pipeline_failure_events`, so reuse collides the moment that attempt also has a
record — which is the ordinary case, since the artifact usually came from a run that produced
one. The cost is that `persist_generation` writes a second copy of the reply under the new
id. That copy is honest: this is a new attempt, and what it consumed is what it stores.
`ReplayRun.replayed_attempt_id` records where it came from.

The consequence is pinned by a test rather than left implicit
(`test_two_runs_of_the_same_seed_write_two_records_with_different_hashes`): two runs of one
seed produce two records with two hashes. That reads like a defect if you expect a replay to
be idempotent, so the test says why it is not, and stops anyone "fixing" it by reusing the
saved id. The reproducible hash the criterion asks for is `replay --record-id` re-deriving
**one** record's hash, which is a different claim and is asserted separately.

### Decision — the rebuilt request is compared byte-for-byte, with `as_of_utc` recovered

A saved reply is an answer to a specific reasoning packet. Replaying it against research it
never saw would write an attribution claim the reply does not support — documents cited in a
record the model was never shown. So the packet is rebuilt from the replayed research,
rendered through the same `render_model_input` the generating call used, and required to
equal the stored request exactly.

Exact equality needs `as_of_utc`, which cannot be re-derived from the clock, so it is
recovered from the stored request. The alternative — comparing with `as_of_utc` excluded — is
strictly worse: it turns an equality into a hand-maintained list of fields allowed to differ,
and every future field on `ForecastModelInput` joins that list by default rather than by
decision. Recovering the one value that provably cannot be re-derived keeps the comparison
total. `_as_of_from_request` is the function that does it, and it is the one new pure
function here, so it gets the property pass.

### Decision — `--dry-run` and `--no-submit` assert the configuration, they do not override it

A flag that silently forced the safe value would let a config with `dry_run: false` pass a
command line that reads as safe, and the operator would have been told the wrong thing about
their own file. Each refuses when the setting it names is not set; omitting it asserts
nothing, because the committed defaults are already safe and this command cannot post either
way. Both directions are tested.

### Deviation — `run`'s signature is not the one `CODEX_HANDOFF.md:274` writes

The handoff writes `run --config PATH [--limit N] [--question-id ID] [--dry-run]
[--no-submit]`. This one makes `--question-id`, `--snapshot` and `--attempt-id` required and
has no `--limit`.

The reason is that this `run` is replay-only, and a replay is addressed by the
`(question_id, attempt_id)` pair the artifact layout is keyed on — there is nothing for
`--limit` to iterate that would not also need per-question failure isolation, which is a
different item's risk. Composing paid calls is a different risk from composing free ones,
which is the same split M1-312 made against M1-306. Filed as **M1-315**, which owns
`--limit`, the batch loop and the live path. Taken to the owner and confirmed rather than
assumed.

### Rejected — seeding the acceptance scenario through `persist_generation`

It is the shortest way to produce an artifact, and `tests/unit/test_cli_replay.py` does
exactly that. It cannot be used here: it writes a forecast record, and "`run` produces
**exactly one** validated record" is then unfalsifiable, because the row is already there
before the command runs. The seed writes the artifact with `write_raw_model_output` alone, so
the ledger holds research and nothing else when `run` starts.

### Rejected — copying `test_research_store.py`'s forbidden-package set

That test proves "zero provider calls" by asserting `asknews_sdk`, `httpx`,
`forecasting_tools`, `requests`, `urllib.request` and `ssl` are absent from the import graph.
Copying it here would have been wrong in both directions, and the measurement is worth
recording: importing `whiskeyjack_bot.pipeline` **does** pull in all six. `metaculus/
snapshots.py` and `questions/normalize.py` both import `forecasting_tools`, because a saved
snapshot holds serialized SDK question objects and normalization dispatches on their types,
and the SDK drags `httpx`, `openai`, `litellm` and `asknews_sdk` transitively.

So a copied test would fail for a reason unrelated to whether a call can happen, and a test
weakened until it passed would assert nothing. The check that bites is the whiskeyjack-layer
one: none of this project's own paid or posting adapters — `research.asknews`,
`research.exa`, `forecast.generate`, `metaculus.client`, `metaculus.poster`, `submission.*`,
`approval` — is reachable. If none is on the graph there is no code here to make a call with.
The module docstring now says this instead of the stronger, false thing it said before.

### Deviation — a defect found while writing the test that the test was written to catch

`_select_question` reached the snapshot through
`metaculus.fetch.fetch_open_questions_fixture`, which is `load_snapshot` plus one log line
but lives beside the live fetcher and so imports `whiskeyjack_bot.metaculus.client`. It was a
**deferred, function-local import**, and that is the part worth recording: the module-level
import graph looked clean while the executed path pulled the live Metaculus API client in.

Measured, with the defect reintroduced deliberately:

| test | with the defect |
| --- | --- |
| `test_the_pipeline_cannot_reach_a_paid_or_posting_module` (module-level graph) | **passed** |
| `test_the_guard_is_not_vacuous` | **passed** |
| `test_the_command_path_imports_no_more_than_the_module_does` (AST walk) | **failed** |

This is the project's most expensive defect class — an assertion that cannot fail for the
thing it names — arriving in the test written to prevent it. The module-level test is the
obvious one to write and it is blind to exactly the import style that most easily sneaks a
client onto a path. The fix was to call `load_snapshot` directly and log the line here; the
guard that catches a regression is an `ast.walk` over the whole module, which reaches an
import statement inside a function body, and it asserts that importing *everything*
`pipeline.py` names still reaches no paid or posting module.

**The generalizable rule: an import-graph guarantee must be measured over the imports the
code can execute, not over the ones at the top of the file.** A module-level `sys.modules`
delta is a proxy for reachability, and a deferred import is precisely where the proxy and the
property come apart.

### Deferred (do not read the absence as an omission)

- **The live paid run — M1-315.** `--limit`, the batch loop, live retrieval and a live model
  call. Filed with this item; see the Deviation above.
- **`retrieval_run_id` on the record is the packet's first run.** The column is a single FK
  while a packet may carry many runs. What pins the whole packet is `research_packet_sha256`,
  which is a record field and therefore inside `forecast_sha256`, so nothing is unattributed
  — but the column is narrower than the thing it points at. Widening a merged, reviewed
  schema is not this item's job and the tension is left visible here rather than fixed
  sideways.
- **No numeric or multiple-choice acceptance scenario.** The committed snapshot holds all
  three types (91001 binary, 91002 numeric, 91003 multiple-choice) and `_select_question`
  picks one out of the normalized batch, so the *selection* is exercised across types. The
  end-to-end scenario is binary only, because the reply it replays is the forecaster prompt's
  own example and the prompt ships one worked example. M1-503 and T-904 own the numeric path.

### Standing risk — not verifiable offline

- **The command has never run against a real snapshot fetched from Metaculus.** Every
  scenario here starts from the committed fixture snapshot, which was itself built from
  committed API-post fixtures. If a live snapshot carries a shape the fixture does not, the
  first place it appears is `normalize_questions`, not this module — but this module is where
  an operator would see it.
- **`_require_settings_agree` compares four fields and deliberately not the other four.**
  `provider`, `name`, `prompt_version` and `prompt_sha256` become NOT NULL columns and are
  the attribution claim, so they must agree. `temperature`, `max_output_tokens`,
  `timeout_seconds` and `allowed_tries` shaped the reply when it was made and cannot change
  what it says now that it exists, so requiring them to agree would refuse a replay for a
  reason that provably cannot affect the answer. The record still stores the artifact's
  values for all eight. If that reasoning is wrong, it is wrong in the direction of accepting
  a replay that should have been refused.

### Observation — the suite is no longer the ~85s `CLAUDE.md` claims

Measured on this branch: `tests/` minus this item's additions is **250s**; this item adds
**31s**, of which 28s is four clean-interpreter subprocess launches for the import-graph
guards (each pays `forecasting_tools`' import cost, the same price
`test_research_store.py`'s equivalent already pays). The tmpfs temp root is active — pytest's
basetemp is under `/dev/shm` — so this is growth across waves rather than the regression that
fix addressed. `CLAUDE.md`'s "~85s total" and `scripts/gate.sh`'s per-gate figures are now
stale by roughly 3x. Not corrected here: `CLAUDE.md` is shared by every active branch and
editing it mid-wave is lesson 1 at triple cost. Flagged for the owner to land at a wave
boundary.

### Round-1 remediation — two blocking findings, both reproduced before any fix was written

Round 1 returned CHANGES REQUESTED with two blocking findings and no non-blocking ones. The
review named `6e3f3d3`, which was `HEAD`, so nothing was stale; both were still reproduced by
execution first, per the standing rule. Both reproduced exactly as described.

**Finding 1 — the record and its validation event were not atomic.** `persist_generation`
committed the row, then `record_validation` opened a second transaction. Monkeypatching
`record_validation` to raise `LifecycleError` — an ordinary busy timeout or full disk between
the two writes — left `forecast_records` holding one `draft` row with no `validated` event,
and a retry appended v2 beside it rather than completing v1.

The code carried an explicit argument for this, and the argument was wrong: it said the row
was "already appended and `003` blocks UPDATE and DELETE on it", so rolling back would be the
append-only violation the project exists to prevent. **That conflates two different things.**
Append-only forbids *mutating a row that exists*. An insert that never commits is not a
mutation — there is no row to mutate, and no history to destroy. `forecast/store.py` had
already said so in its own docstring: `lifecycle.transaction` nests as a `SAVEPOINT`
"so a caller that wants the record and its first event in one unit can have it without this
module deciding that on its behalf". This pipeline is that caller and did not take the offer.

Worth recording as a defect class: **a wrong invariant defended in a comment is more durable
than one left undefended.** The comment is why the gap survived writing the module, writing
459 lines of refusal tests, and writing the deliberate-choices section — three passes that
each read it and accepted it.

**Finding 2 — an accepted configuration produced an incomplete record while `run` reported
success.** `storage.retain_raw_model_output` defaults to `True` and the validator accepts
`False`. With it off, `persist_generation` returns `artifact_outcome="retention_disabled"`
and appends the row with a NULL `raw_output_path`; `run_replay` then validated it and the CLI
printed `artifact: (none: retention_disabled)` beside `status: validated`. The record cannot
be re-derived by `replay --record-id`, which is the one thing this item's own completeness
test requires.

#### Decision — the strictness lives in the pipeline, not in `persist_generation`

The obvious fix is to make `persist_generation` refuse a non-`written` outcome. That would be
wrong, and the reason is M1-312: for a **paid** attempt, the cost and the invocation count are
facts whether or not the evidence survived, so the row is written regardless — the inversion
of M1-303's refuse-before-billing rule, which only holds *before* the spend. M1-315's live run
needs exactly that behaviour. This command spends nothing, so it can hold itself to the
stricter bar without taking M1-312's decision away from the path that needs it.

So the fix is two checks in `pipeline.py`, not one in the writer:

- `_require_retained_output` refuses up front when the configuration disables retention —
  `_require_replay_enabled`'s reason, that a refusal an operator can act on arrives before the
  work rather than after a row was nearly appended.
- A non-`written` outcome raises **inside** the new transaction, so the row rolls back. This
  covers the case the preflight cannot see: retention permitted, write attempted, write
  failed. What survives is at worst an orphaned file, harmless under
  `forecast/persist.py`'s convention; what must not survive is the inverse.

The CLI's `(none: {artifact_outcome})` fallback is gone with it: it printed a state the
pipeline can no longer return, and a fallback for an unreachable state only teaches a reader
that this command can produce a record without its evidence.

#### The three regression tests were mutation-tested before they were trusted

Each new assertion was checked against a deliberately broken tree, since a test that cannot
fail for the thing it names is this project's most expensive recurring defect:

| mutation | test | result |
| --- | --- | --- |
| outer `transaction(conn)` removed | `test_a_failed_validation_event_leaves_no_forecast_record` | **failed** |
| `_require_retained_output` call removed | `test_disabled_raw_output_retention_refuses_before_anything_is_read` | **failed** |
| `artifact_outcome != "written"` check removed | `test_an_artifact_that_could_not_be_written_appends_no_record` | **failed** |

#### What round 1 got right that is worth naming

Both findings landed on risk claim 7 ("`run` writes exactly one forecast record"), which the
request stated as falsifiable and the review falsified in two independent ways. The other nine
claims came back safe. That is the same mechanism M1-502 recorded one wave earlier: the
blocking findings were found *because* the claim was specific enough to check. Neither
finding's failure mode was covered by the 37 acceptance tests written before the review — both
needed a monkeypatched local I/O failure, which is a reachable reliability condition rather
than a hostile one, and the suite had no such simulation in it at all.

### Round 2 — APPROVE, both blockers closed, and one of my own claims falsified

Round 2 (`d7a5976`, baseline `6e3f3d3`) closed both round-1 findings by execution and returned
no blocking findings. One non-blocking observation, and it is the entry worth keeping:

**Risk claim 7 was overstated and the review was right.** I claimed
`_require_retained_output` "cannot be satisfied by a non-`AppConfig`". It can:
`SimpleNamespace(storage=SimpleNamespace(retain_raw_model_output=True))` passes it, because the
guard reads the attribute and then checks the *value's* type — never the object's. Non-blocking
for the stated reasons (the guard is private, the CLI always supplies `load_config`'s
`AppConfig`, and the shape predates this branch), and filed as **M1-316**.

What makes it worth a row rather than a shrug: `_require_replay_enabled` has the identical
shape and was written first, so this is a pattern, not a slip. Every `AttributeError` →
`PipelineError` guard in `pipeline.py` prints "config must be an AppConfig" while checking
something strictly weaker. **The message is the part that is wrong** — either the check matches
its claim or it stops making it. That is the same defect class as the vacuous-property findings
this project keeps paying for, one layer over: a guard whose stated claim is stronger than
anything it can fail on. I copied the shape from a sibling module without asking what it
actually verified, which is exactly how the class propagates.

**Two rounds, and the mechanism worked the way `docs/LESSONS.md` describes.** Round 1's ten
risk claims were stated as things a reviewer could falsify; claim 7 in round 1 was falsified in
two independent ways and became both blockers, and claim 7 in round 2 was falsified again. A
vaguer request would have produced neither finding, and the incomplete-record path would have
merged. The cost of writing falsifiable claims is that some of them turn out to be false in
public — which is the point of writing them.

## M1-503 — The numeric CDF conversion

*"Use NumericDistribution.from_question and get_cdf to produce the submission array. Exactly 201
monotone values for normal numeric questions; maintained PMF constraint passes."*

### What the criterion is actually guarding against

Two things, and they fail at different distances from the model.

The first is the one M1-405 named before this module existed: a percentile set that no conversion
accepts. M1-405 closed the half that needs no configuration — exact levels, non-decreasing values,
closed bounds, `zero_point` — and stopped at the config boundary. What it could not close is the
half `config.numeric_calibration` decides, because `NumericCalibrationConfig` is a **sibling** of
`ForecastConfig` on `AppConfig` and a registered `_TYPE_CHECKERS` entry is handed only the latter.
So until this row, `numeric_calibration` had no consumer in `src/` outside `submission_live.py`'s
length check, and a reply that M1-405 accepted could still be unconvertible.

That is not hypothetical, and the concrete case is worth stating because it is the whole argument
for where this gate runs. **Nine equal values** are non-decreasing, inside the bounds and exactly
the declared levels — `prompts/forecaster.md` says "non-decreasing" twice, so such a reply followed
the prompt to the letter, and `numeric_output_problems` returns `[]`. `get_cdf` refuses it: the
resulting CDF is flat over most of its range and the SDK's own re-validation raises
`Percentiles must be in strictly increasing order`. Without a gate in the loop, that reply is
billed, parsed, hashed, recorded and **approved**, and fails inside the dependency at submission
time — past the approval boundary, which is worse than a wasted call.

The second is the PMF cap, and it is the clause that reading the SDK casually gets wrong.
`_check_distribution_too_tall` is the package's cap and it does exactly what the criterion asks
for. It is **not applied to the array `get_cdf` returns.** `get_cdf` re-validates its output by
constructing a `NumericDistribution` from it *without* `cdf_size`, and the cap is gated on
`len(percentiles) == self.cdf_size`, which is false for every length once that field is `None`. So
"maintained PMF constraint passes" is genuinely this row's to assert. Verified by execution, not by
reading — `test_the_sdk_does_not_check_the_pmf_cap_on_its_own_output` hands the SDK an array over
the cap and watches it accept it, so a pin move that starts enforcing it fails there rather than
leaving a redundant check that looks load-bearing.

### Delivered

- **`src/whiskeyjack_bot/forecast/cdf.py`** — `NumericCdfError`, `NumericCdf`,
  `numeric_cdf_or_problems`, `build_numeric_cdf`. `numeric.py`'s shape (a returner and a raiser, a
  module-owned `ForecastSchemaError` subclass, value-free messages), with `parse._parse`'s
  `tuple[value | None, problems]` signature because the caller needs the array and the problems
  from one pass rather than converting twice.
- **The four calibration knobs get their first consumer.** `expected_cdf_points` is the length
  rule and the `cdf_size` preflight; `max_adjacent_pmf` is the step cap; `strict_validation` and
  `use_forecasting_tools_standardization` are passed to the SDK. `calibration_profile` is **not**
  read and this module applies no post-hoc transform, which `cdf.py`'s docstring states rather
  than leaving as an absence — a knob with no reader looks identical to a knob someone forgot,
  and "nothing is clamped" is only checkable if an operator can tell which is which.
- **A second numeric preflight in `generate_forecast`** — a question whose `cdf_size` disagrees
  with `expected_cdf_points`, refused before any billable call, beside M1-405's `zero_point` one.
- **The conversion gate in `_run_attempts`** — `_conversion_problems`, run after `_parse` succeeds
  and dispatching on the `question_type` literal. `numeric_calibration` is threaded into
  `_run_attempts` for it.
- **Tests** — `tests/unit/test_forecast_cdf.py` (35), an M1-503 section in
  `test_forecast_generate.py` (6) plus a negative import probe, and a `5c.` section in
  `tests/property/test_forecast_properties.py`.

### Decision — a second gate, not a `_TYPE_CHECKERS` entry

M1-506 built one composed output-validation entry point precisely so a caller would not have to
know which modules the rules live in, and this row does not register with it. Two independent
reasons, and both are written into `cdf.py`'s docstring and `generate._conversion_problems`'
docstring rather than left to be inferred — a reviewer reading `validate.py` alone would otherwise
see a rule that looks absent, which is the seam defect M1-506 exists to close and the exact shape
of M1-501's round-1 blocking finding.

1. **The import graph.** The conversion is `NumericDistribution`, which lives in
   `forecasting_tools`. `test_the_response_schema_reaches_no_provider_client` forbids that import
   from `forecast.validate` and the ten modules around it, and that probe **is** M1-406's
   acceptance criterion: a replay reproduces a stored forecast with zero provider calls, asserted
   as a property of the import graph rather than of a mock count. A checker that could reach an SDK
   would end that guarantee for `parse`, `replay` and `store` at once.
2. **The config boundary.** A registered checker receives `(response, ForecastConfig, question)`.
   Every rule here is keyed on `numeric_calibration`. Widening the registry signature to `AppConfig`
   would hand every checker the whole configuration to reach one section, and it would not help
   with (1) anyway.

So `forecast.validate` remains the complete account of everything a response must satisfy that is
**SDK-free and `ForecastConfig`-keyed**, and the conversion is a second, SDK-bound gate that only
`forecast.generate` runs. Stated that way in both places, because "complete" was true before this
row and is now true only with a qualifier.

**A naming consequence, and it is load-bearing.** Nothing in `cdf.py` is called
`*_output_problems`. `test_no_output_checker_in_the_package_is_unreachable` walks this package for
that suffix and requires every match to be registered, so a conversion gate that *cannot* be
registered must not answer to the convention that means "registered". The alternative — registering
it and letting the probe fail — is not available, and a suffix exemption would make the discovery
walk skip silently, which is the failure `.github/scripts/check_backlog.py` was rewritten to avoid.

### Decision — inside the attempt loop, while a repair turn is still free

`_conversion_problems` runs in `_run_attempts` immediately after `_parse` returns a response, and a
non-empty result sets `forecast = None` so the existing repair path handles it unchanged. Its
strings are the shape `schema._sanitize` produces, so `_repair_turn` renders them and `_classify`
reads them as `schema_invalid` with **no change on either side** — the same property M1-405's
problems have, and the reason neither function needed touching.

The alternative was the submission boundary, where the payload is built. It is cheaper in diff and
strictly worse in outcome: a conversion failure there lands after the forecast has been billed,
recorded, hashed and approved. M1-502's round-1 finding drew this distinction for the probability
envelope — "the cost is not a repair loop but a forecast billed, recorded and approved for a post
that cannot happen" — and the numeric case is the same shape with a larger class of triggering
replies.

### Deviation — `NumericDistribution` is constructed directly, not through `from_question`

The row says to use `NumericDistribution.from_question`. It is not used, and the reason is a
boundary this project drew two milestones ago rather than a preference.

`from_question` takes an SDK `NumericQuestion | DateQuestion` and reads six fields off it. That
object does not survive `questions/normalize.py`: the canonical model is what reaches the
forecaster, what the ledger stores, and what any later conversion from a stored record has. So
calling `from_question` would mean **fabricating** an SDK question from the canonical one purely to
have its fields read straight back out — a second source of truth for six values, built and
discarded inside one call, which is exactly the shape M2-703's round-1 review said to remove rather
than guard and the shape M1-405 removed `question_id` for.

Two further reasons found by reading the pinned source: `from_question` never passes
`strict_validation`, so it cannot honour `numeric_calibration.strict_validation` — the knob would
be silently inert; and it branches on `isinstance(question, DateQuestion)`, the
`isinstance`-on-an-SDK-type shape `DiscreteQuestion` subclassing `NumericQuestion` makes a standing
gotcha here.

**The mapping is therefore ours, and it is pinned rather than asserted.**
`test_the_field_mapping_is_the_one_from_question_would_have_made` builds a real SDK
`NumericQuestion`, calls `from_question`, and compares both the seven mapped fields and **the
resulting 201-value array**. A pin move that renamed a field, changed a default or derived one
differently fails there. Only `strict_validation` (the deliberate difference) and `is_date` (no
date questions in v1) are excluded, and both are named in the test.

### Decision — `use_forecasting_tools_standardization: false` is honoured, and the array is still checked

The knob is a plain `bool` with a `true` default, unlike `expected_cdf_points`, which is a
`Literal[201]` with a comment saying any other value is a hard error rather than a tunable. The
config author distinguished the two, so this one is settable and it is passed straight through as
`standardize_cdf`.

What makes that safe is that the array is checked afterwards either way. Standardization is what
caps the inbound PMF at `0.2 * 0.95 = 0.19`; with it off nothing does, and
`test_standardization_off_is_honoured_and_an_uncapped_array_is_refused` shows a concentrated reply
coming back over the configured `0.2` and being refused — the same reply that converts cleanly with
the committed default. The operator may turn the dependency's normalization off; they cannot
thereby post an array this project's own cap refuses.

Rejected: preflight-refusing `false` as unsupported in v1. It is the stricter reading, and the
argument for it is real — standardization is the only path verified against 0.2.92. It was not
taken because refusing a configuration the schema deliberately admits is a change to the config
contract, which belongs to the config owner and not to a conversion row, and because the check that
would make it safe already exists.

### Decision — `-0.0` is normalized in the producer, never in the hash rule

`docs/M2-NOTES.md` records this as a standing risk that "becomes reachable when M1-503's CDF arrays
exist": `0.0` and `-0.0` compare equal but render differently, so they hash differently, so two
arrays an operator would call equal derive two idempotency keys and two submissions. M2-NOTES
declined to normalize floats *inside* the replay-critical hash rule, and rightly — that changes what
every future key means.

`_values` adds `0.0` to each point instead, which maps `-0.0` to `0.0` and is the identity on every
other float. That is a change of spelling and never of value. It is not reachable through 0.2.92 —
`_standardize_cdf` pins a closed lower bound to a positive `0.0` — so the test simulates the
condition at the seam and says so, and a second test asserts the rule itself over ordinary floats.
The claim the pair supports is "the normalization is applied", never "the SDK emits this today".

### Correction — the SDK does not nudge "in place"

`docs/M1-NOTES.md` (M1-405) and `forecast/numeric.py` both said
`_check_and_update_repeating_values` "mutates `declared_percentiles` in place". It does not. It
builds fresh `Percentile` objects into a fresh list and rebinds the attribute; the caller's list and
the caller's objects are untouched. Verified by execution and corrected in both places on this
branch.

The distinction is not pedantry — it is what makes M1-508 cheap. "Compare what we handed in against
what the distribution holds" is a sound way to detect the nudge only because our copy survives it.
Under the in-place reading there would be nothing left to compare against, and recording the
divergence would have needed a defensive copy nobody had written.

### Decision — M1-508's divergence is made observable, and the policy is left to M1-508

`NumericCdf.percentiles_used` is what the conversion actually built from, and `.adjusted` says
whether it differs from what the model returned. An INFO log names the **count** of adjusted points
and never the values: a tie is not an error, and putting model output into an operator's logs for a
non-error is the wrong trade.

That is the precondition M1-508's criterion needs — *"the difference between the declared
percentiles and the ones the submitted CDF was built from is recorded rather than silent"* — and it
is deliberately not the whole of it. Refusing ties at the conversion boundary was rejected here for
M1-405's reason, restated: the prompt says "non-decreasing" twice, so a tie is a compliant reply,
and refusing one spends the single budgeted repair call on this project's own inconsistency.
Persisting the adjusted set, and bumping the prompt to demand strictly increasing values, are both
still open on that row. **One backlog item per branch**, and M1-508 is a row with its own
acceptance criterion.

### Rejected — re-checking the SDK's minimum step

`_check_percentile_spacing` enforces `5e-05` between adjacent points and `get_cdf` runs it against
its own output, so a violation already arrives as an SDK refusal this module translates. Restating
`5e-05` would copy a pinned constant into this repository, and M1-405 refused to duplicate the 0.25
wiggle factor and the 2x buffer for exactly that reason: *"there is nothing in this module for a
pin move to invalidate."* The cap is a different case and the difference is the point —
`max_adjacent_pmf` is **our** configured value, not a transcription of theirs.

### Rejected — building the submission payload

The wire shape is settled (`{"question_type": "numeric", "continuous_cdf": [...]}`,
`submission_live.plan_from_payload`), and emitting it from here is one line. Not taken: M1-502
shipped validation without a payload builder, `cli.py`, `submission.py` and `submission_gateway.py`
all say "M1-502/M1-503 own that", and a builder covering one of three types is a half-seam that
leaves those three statements exactly as wrong as they were. The array is the row's deliverable;
**M2-707** is where the mapping and the approval binding land together.

### Deferred (do not read the absence as an omission)

- **The persisted CDF → still absent, and still `record.py`'s stated gap.** `forecast_records` has
  no column for it, and adding one is a migration, a `RECORD_SCHEMA_VERSION` bump and a store/replay
  change. The array is deterministic in `(percentiles, question, calibration)`, so it is
  re-derivable rather than lost; what is *not* re-derivable is the adjusted percentile set if the
  pin moves, which is the half M1-508 owns. `docs/TRACKS.md` claims no migration for this row.
- **The payload builder and the approval binding → M2-707 / D33.** Above.
- **The `validated` lifecycle event → M1-504.** This row closes the last of the four M1-603
  named when it declined to append the event — *"that gate is M1-504, and M1-404, M1-405,
  M1-502 and M1-503 are all `Not Started`"* (M1-603's notes, and M1-502's repeated the
  count). `store.py` still appends nothing, and that is M1-504's to change.

  **Two records disagree here and it is worth naming which is which**, because a reviewer
  checking one will find the other wrong. The four-item chain is the *notes'*, and it is
  the substantive precondition: an append-only `validated` event should not claim a gate
  that does not exist. `backlog.csv` gives M1-504's formal Dependency as `M1-305; M1-501`
  and names none of the four. Neither is a defect in this row — the CSV records what
  M1-504 was scoped against, the notes record what its event would be asserting — but the
  divergence is real and this is the row where it becomes visible, so it is written down
  rather than left for M1-504 to rediscover.
- **`T-904`, the contract tests, is Codex's.** Deliberately not pre-written — the unit tests here
  are the minimum to keep this branch honest, and the golden set and drift guard are authored from
  spec without reading this implementation.
- **`M1-510` is untouched.** The composed entry point's property strategy still draws no numeric
  responses. Section 5c draws numeric directly and does not go through `output_problems`, so it
  neither closes nor worsens that row.

### The finding — `get_cdf` does not always return, and both hanging knobs are the committed defaults

This is the most important thing on the branch, and it was not in the row's scope.

`forecasting-tools==0.2.92`'s `NumericDistribution._standardize_cdf` finds its scale factor with

```python
lo = hi = scale = 1.0
while capped_sum(hi) < 1.0:
    hi *= 1.2
```

where `capped_sum(scale)` sums `min(cap, scale * pmf)` over the interior points. If any interior
PMF entry is **negative**, that sum falls without bound as `scale` grows instead of rising. `hi`
reaches `inf`, `inf * 1.2` is still `inf`, and `capped_sum(inf)` is `-inf` — still below `1.0`. The
loop has no exit. Measured on the captured case: `capped_sum(1) = 0.99999…`,
`capped_sum(1e12) = -9.4e11`, `capped_sum(inf) = -inf`.

**It does not raise and it is not slow. It never returns.** There is no deadline anywhere on the
forecast path, so `generate_forecast` stops inside the attempt loop with no error, no pipeline
failure event, and no billing bound.

**How it was found, and why not earlier.** The `5c.` property section appeared to be "slow" — a
31-minute run that seemed to pass. It had not passed: the exit code belonged to a `tail` in the
pipeline, not to pytest. Timing each property at 25 examples (~3s) against 200 (>600s) gave a
200× cost for 8× the work, which is not a cost curve. `faulthandler.dump_traceback_later` with
pytest's capture disabled put the process inside `while capped_sum(hi) < 1.0` and settled it.

**Two unrelated inputs reach it**, both from replies `numeric_output_problems` returns `[]` for —
asserted in the regression test rather than assumed — and both compliant with
`prompts/forecaster.md`, which says "non-decreasing" and therefore permits a tie:

1. **A repeated value at or beyond a bound.** `_check_and_update_repeating_values` rewrites it
   **onto** the bound (plus ~1e-10) while leaving an *unrepeated* value further out untouched. A
   reply ending `…, 101.43, 148.15, 148.15` against `upper_bound=100.0` becomes
   `…, 101.43, 100.0000000001, 100.0000000001` — the SDK's own tie repair makes the set decrease.
2. **A log-scaled question spanning many orders of magnitude** (`zero_point=-1.0`, values from
   `-4e-181` to `63`), where the interpolation loses order with no rewrite involved at all.

The second was found only *after* the first was closed, which is the whole argument for guarding
the mechanism rather than a list of triggers.

### Decision — guard the mechanism, on the SDK's own pre-standardization curve

`_standardization_can_converge` evaluates `distribution._get_cdf_at` over `get_cdf`'s own
evaluation grid — the first of the three things `get_cdf` does, on the distribution the conversion
will actually use, so the tie rewrite has already happened and the log scaling is the SDK's. It
refuses a curve that is not non-decreasing, or whose span is not strictly positive (`_standardize_cdf`
divides by that span). Both known inputs are refused; the committed prompt example and all four
bound-flag combinations still convert.

Why a survivor is safe, stated because it is what makes this a guard rather than a guess:
`apply_minimum` is affine and increasing in the height plus a strictly increasing term in position,
so an already-increasing curve stays increasing and every interior PMF entry is non-negative;
`capped_sum` is then non-decreasing in `scale` and tends to
`pmf[0] + pmf[-1] + cap * (interior points with mass)` with `cap = 0.19`, which clears `1.0` unless
nearly every interior step is zero — and a curve that flat is what `_check_percentile_spacing`'s
`5e-05` minimum already refuses.

**Rejected — probing with `standardize_cdf=False`.** Tried first and reverted. An unstandardized
curve legitimately has flat regions, so `get_cdf`'s own `5e-05` re-validation raises on it, and the
probe refused ordinary open-bounded forecasts — `test_the_endpoints_follow_the_bound_flags` caught
it on two of its four combinations. A liveness fix bought by rejecting good forecasts is not a fix.

**Rejected — enumerating the triggers.** The first version checked that the rewritten percentile set
was still non-decreasing. It closed input 1 and the properties still hung, on input 2. Recorded
because the failure is the argument: a trigger list is only as good as the fuzzing behind it.

**Deferred — the general bound is M1-514.** The guard is keyed on the mechanism it has evidence for,
not on a proof that no other input diverges, and it reaches into a private SDK method. A wall-clock
bound on the conversion, so that an unknown non-terminating input degrades to a recorded failure
rather than a stopped run, is that row. `M0-002` pins `0.2.92` with CI failing on drift, so
upgrading past the defect is itself a decision rather than an action.

**M1-508 was tightened rather than left.** It owned the tie question as an *attribution* problem —
its own text says the rewrite happens "before `get_cdf` is ever called", so it never reached this.
Its acceptance criterion was a disjunction whose second branch ("the difference is recorded") could
have closed the row with the hang still in place. It now requires the liveness guard to survive or
be replaced.

**The suite cost is the incidental win.** The `5c.` section went from non-terminating to 25 seconds.

### Standing risk — not verifiable offline

Whether **Metaculus** accepts an array this module produces is not knowable without a live call.
What is established is agreement with the two things that are checkable here: the pinned SDK's own
re-validation, and `submission_live._require_cdf`, which is asserted directly rather than restated
—`test_a_converted_cdf_is_one_the_submission_path_accepts` calls the consumer. M1-405's version of
this note said the same about its own boundary and pointed here; this is where the pointer stops
being able to go further offline.

The `0.19` margin the concentrated case lands on is the SDK's wiggle room (`0.2 * 0.95`), not a
number this project chose. A pin move that removed the wiggle would put a legitimate concentrated
forecast exactly on the committed `max_adjacent_pmf: 0.2` boundary.

## T-901 — golden schema fixtures

`CLAUDE.md` and `CLAUDE_CODE_PROMPT.md` assign T-901–T-904 to Codex as independent,
implementation-blind acceptance-test authorship. `docs/TRACKS.md` Wave 11 records an owner
override for this item: worked by Claude Code, but still drafted blind — from
`CODEX_HANDOFF.md`'s test-requirements section and the M1-201/M1-501 schemas, not from reading
`forecast/`'s current implementation — to preserve the independent-test intent even though the
author changed. Every module that was going to need this item's fixtures said so while it
shipped: `test_questions.py`, `test_forecast_binary.py`, `test_forecast_multiple_choice.py` and
`test_forecast_numeric.py` each defer "the comprehensive valid/invalid golden set" to T-901 by
name.

`CODEX_HANDOFF.md`'s "Schema tests" bullets are the spec: valid golden records for binary,
multiple-choice and numeric; malformed model outputs rejected; unknown fields rejected or
versioned; and DB migration determinism.

### Decision — authored blind, and the boundary is enforced in the test file itself

Built only from `CODEX_HANDOFF.md`'s test requirements, `prompts/forecaster.md` (the contract
`forecast/schema.py` is transcribed from), `questions/model.py` (M1-201), `forecast/schema.py`
and `forecast/attribution.py` (M1-402/M1-501), and `forecast/validate.py`'s public
`output_problems` signature. Deliberately not read: `forecast/binary.py`, `forecast/numeric.py`,
`forecast/multiple_choice.py`, or their test files — those own the type-specific bound rules
(percentile-level exactness, option-set matching, the binary prior requirement). Where this
suite needs to exercise one of those rules, it reaches it only through the composed
`output_problems` entry point and asserts the returned list is non-empty, never the exact
wording — so a round of review reading this file cannot mistake it for evidence about what those
modules' messages say.

**The boundary held through the M1-201 fix below, and it is worth saying why rather than letting
a reviewer find the tension.** Closing the blank-title defect required reading `questions/model.py`
and `forecast/schema.py`'s guard — both already on this item's declared-readable list, because the
fixtures are written against those two schemas. `forecast/binary.py`, `forecast/numeric.py` and
`forecast/multiple_choice.py` were still not read, and the suite still reaches their rules only
through `output_problems`. What changed is the *shape* of the deliverable, not the reading list.

### Decision — malformed fixtures are generated from the golden fixture, not shipped as files

The alternative — one JSON fixture file per malformed case — was raised and rejected for this
branch (owner-confirmed direction): it does not scale past a handful of cases per type and gives
no structural guarantee that every field got one. Instead, `test_golden_schema_fixtures.py`
introspects each pydantic model's own `model_fields` for required-ness against the golden
fixture's real shape, and mutates a deep copy per field. A field added to a schema later is
covered automatically; a field removed disappears from the walk rather than leaving a stale
fixture file behind. The walk descends into a nested model's first list item only — every item
shares one schema, so descending into the rest would multiply the case count without adding
coverage — and `test_the_required_field_walk_is_not_vacuous` pins the load-bearing paths the walk
must keep finding, per `docs/LESSONS.md`'s vacuous-property guard.

### Decision — canonical-question fixtures get their own directory

`tests/fixtures/questions/{binary,multiple_choice,numeric}_golden.json`, parallel to the
existing `tests/fixtures/forecasts/`. No canonical-question fixture file existed before this
item; `test_questions.py` built its records ad hoc. The three question fixtures and the three
response fixtures are paired by construction — the multiple-choice question's `options` are
exactly the labels the response fixture answers, the numeric question's bounds contain the
response's percentile values — so the golden pair also serves as the single node this suite
threads through the composed `forecast.validate.output_problems()` entry point, not only through
the two schemas separately.

### Deferred — DB migration determinism is not duplicated here

`CODEX_HANDOFF.md`'s fourth schema-test bullet, "database migration from empty file is
deterministic," is already fully covered by `tests/unit/test_ledger.py`
(`test_schema_is_deterministic`, `test_migration_is_idempotent`, M1-601). Repeating it here would
be a second copy of the same assertion with no additional coverage; the module docstring says so
explicitly rather than leaving the omission to look like an oversight.

### Rejected — asserting the type-specific bound messages verbatim

Tempting, since the exact strings are visible in the sibling modules' own docstrings and in
`test_forecast_validate.py`. Rejected because asserting them would make this suite's pass/fail
outcome depend on wording owned by modules it was told not to read for this item — the opposite
of what "authored blind" is for. `output_problems() != []` is the claim this suite can make
independently; the exact wording is `binary.py`/`numeric.py`/`multiple_choice.py`'s own tests to
own.

### Deviation — this test-authoring item changed `src/`, and that is the headline

T-901's row is a testing item, so a reviewer should expect a tests-only diff. It is not one.
Writing the suite surfaced a defect in M1-201 and the owner's decision was to fix it here rather
than file it, so three source files changed:

- `src/whiskeyjack_bot/config.py` — now owns `_require_non_blank` and `NonBlankStr`, **moved**
  from `forecast/schema.py`. Not copied: `forecast/*` imports `questions/model.py` and never the
  reverse, so `config.py` is the only module both layers already depend on. A second definition
  would have been the two-sources-of-truth failure M2-703's review was about.
- `src/whiskeyjack_bot/forecast/schema.py` — imports the two names instead of defining them.
  **No semantic change**: `NonBlankStr` still sits on a bare `str`.
- `src/whiskeyjack_bot/questions/model.py` — `_CanonicalQuestionBase.title` and
  `SourceCategory.name` change from `Field(min_length=1)` to the new `NonBlankQuestionStr`.

The defect, reproduced against the golden fixtures before any fix was written:

```
CanonicalBinaryQuestion(**{**binary_golden, "title": "   "})  -> ACCEPTED
SourceCategory(id=17, name="   ")                             -> ACCEPTED
```

`min_length` counts characters, so whitespace-only satisfies a constraint whose whole purpose is
to require content. Same defect class as the M1-603 round-5 `trim()` finding and the M1-316 row:
**a guard whose stated claim is stronger than what it can fail on.**

### Decision — compose both constraints rather than swap one for the other

The obvious fix is to replace `Field(min_length=1)` with the `NonBlankStr` the response schema
already uses. Checked by execution first, and it would have been wrong — the two guards refuse
*different* things:

| | surrogate pair | whitespace-only |
|---|---|---|
| `Field(min_length=1)` (a pydantic *constrained* string) | refuses | **accepts** |
| `NonBlankStr` (bare `str` + `AfterValidator`) | **accepts** | refuses |

A swap would have closed the blank hole and opened a surrogate hole on question text — text that
does not survive this project's persisted form, and the subject of CLAUDE.md's standing
`content_sha256` gotcha. `tests/unit/test_forecast_record.py:410` is explicit that
`question.title` is *not* a way in for a surrogate pair precisely because it is a constrained
string, so the swap would also have silently falsified a comment another suite reasons from.

`NonBlankQuestionStr` is therefore `Annotated[str, Field(min_length=1), AfterValidator(...)]` —
both. `config.NonBlankStr` is deliberately left alone: the response schema *wants* a surrogate to
reach `forecast/attribution.py`'s record guard, which refuses it with a better message, and
`test_forecast_record.py` asserts that path.

### Decision — every constraint walk is generated from one traversal

The first pass generated required-field coverage by introspection but hand-listed the blank and
bounds cases, and the hand-list named four paths. The asymmetry was the bug: the suite's own
docstring argues for generation, and the four-path list reached no nested field and no
`list[NonBlankStr]` item. All walks now run off one `_iter_field_paths`, keyed on each field's
own metadata — non-blank strings, `allow_inf_nan=False` numbers, `MinLen` sequences. Response
blank coverage went from 4 paths to 12 per type. `PercentilePoint.value` carries no `ge`/`le`, so
the non-finite walk is the only thing in the file that reaches it at all.

### Standing risk — not verifiable offline

- **The walks are keyed on pydantic's metadata representation**, not on a public API.
  `_rejects_non_finite` reads `allow_inf_nan` off a `_PydanticGeneralMetadata` object whose name
  is private to pydantic. A pin bump that changed how constraints are recorded would empty a walk
  rather than break it, and every loop over it would pass over nothing.
  `test_the_constraint_walks_are_not_vacuous` exists exactly for that: it pins the load-bearing
  paths each walk must keep finding, so the failure mode is a red suite rather than silent
  vacuity. It cannot be verified against a pydantic version that does not exist yet.
- **The golden fixtures are hand-authored, not captured from Metaculus.** They are structurally
  valid against the schemas and mutually consistent (the multiple-choice question's `options` are
  exactly the labels its response answers; the numeric question's bounds contain its response's
  percentile values), but nothing offline proves a real MiniBench post normalizes to a record of
  this shape. That is T-902's mocked-integration surface, not this item's.
- **The blank-title fix changes what the pipeline accepts from the live API.** Offline, no
  fixture in the repo carries a whitespace-only title, and the full suite is green. A real
  Metaculus post whose title is whitespace-only would now raise `NormalizationError` where it
  previously normalized. That is the intended stricter reading — a question with no title is not
  forecastable — but it is a live-behaviour change this branch cannot exercise offline.

### Deferred — the suite proves its own discrimination by a mutation run, not by a committed harness

Each constraint this suite claims to guard was neutered in `src/` and the suite confirmed red:
16 mutations, 16 caught. Two are worth recording because they are what the run *bought*:

- dropping `Field(min_length=1)` while keeping the validator **survived** the first run — nothing
  asserted that question text refuses a surrogate, so half the composed guard was an unbacked
  claim. `test_content_requiring_question_strings_reject_unencodable_text` closes it, and both
  single-constraint mutants now die.
- the first run also destroyed its own evidence: the harness restored with `git checkout -- src/`
  while the fix was still uncommitted, so mutations after the first ran against unmodified
  source and reported anchor misses rather than results. **Commit before mutating.**

The harness itself is not committed — it is scaffolding, and a mutation harness that lives in the
repo becomes a thing to maintain rather than a thing to run. The result is recorded here.

## M1-608 — Pin the shared identifier bound between lifecycle and approval

`approval._MAX_IDENTIFIER` restated `lifecycle._MAX_IDENTIFIER`; both were 200, behaviour
agreed at every revision anyone checked, and nothing held them there. A change to either
would have let the two entry points accept different `record_id` sets — one path writing a
record the other cannot look up, on an append-only table.

**The surface was six modules, not two.** `submission.py`, `submission_gateway.py` and
`submission_live.py` each restated the number too, under comments that named *this item* as
the one that would pin them together, and `forecast/record.py` restated it with no comment
at all. Owner decision (2026-09-01): pin all six and the whole bound family, because
merging M1-608 while four modules still carried a literal under a comment pointing at a
closed item would have been worse than not doing it.

### Decision — a leaf module, and only the constants move

`src/whiskeyjack_bot/bounds.py` holds `MAX_IDENTIFIER_LENGTH`, `MAX_ACTOR_LENGTH`,
`MAX_NOTE_LENGTH` and `MAX_BODY_LENGTH`, and imports nothing from this package.
`forecast/record.py` is a pydantic schema module and had to be able to take a number
without acquiring a dependency on the ledger writer stack; a leaf is what makes that free
and what makes a cycle impossible.

**Only the constants are shared. The validators stay duplicated**, and the argument that
kept them apart is untouched: each module owns its sanitized exception type, so a shared
`_require_identifier` would have to raise one module's error inside another. That argument
was never about the *number* — a constant carries no error type — which is why the two
spellings of the rule are tested for equality and the two spellings of the bound were not
tested at all. That asymmetry was the whole defect.

### Decision — `MAX_ACTOR_LENGTH` stays a separate name from `MAX_IDENTIFIER_LENGTH`

Both are 200. Collapsing them would be this item's own defect pointed the other way: two
bounds that agree by coincidence, fused into one that cannot move independently. An
identifier is the value every other table points at; an actor name is prose that `010`
happens to guard (M2-710). Every test is driven from the constant that names the field it
is about, so the day the two numbers diverge nothing has to be untangled first.

### Deviation — the scope is the family, not the pair

The backlog row's criteria name approval and lifecycle. Implementing only those would have
satisfied the text and left the defect, for the reason above. The three `submission*`
modules and `forecast/record.py` are in this diff on an owner decision, not on a reading of
the row.

### Rejected — putting the constants in `ledger.py`

It is the obvious home: it already owns `LEDGER_SCHEMA_VERSION` and `LedgerError`, and
every candidate module already imports it. Its own docstring scopes it to *"database
connections and migration application only"*, and `forecast/record.py` importing the
connection layer to learn a field length is a dependency the schema module should not have.
A four-line leaf costs less than widening a module's stated scope.

### Rejected — a source scan asserting no module spells a bare `200`

Tempting, and it fails on the first legitimate `200` anywhere in the package. The parity
test below already fails on a re-spelled literal, by execution, and names which layer
disagreed. A grep-shaped guard would add a second thing to keep true without adding a
second thing that is true.

### Rejected — one `_require_text` with the strict check, widened across modules

M2-710 rejected this on `submission.py` and the reasoning carries: a writer stricter than
its schema is the same defect as one looser than it. Nothing here widens what any validator
accepts; the diff is a rename plus an import in every module it touches.

### Deferred (do not read the absence as an omission)

- **`forecast/record.py`'s identifier fields carry `min_length=1` and no blank/NUL rule.**
  Every other layer refuses a whitespace-only or NUL-bearing identifier. Closing it would
  change what a merged, reviewed pydantic model accepts — the behaviour-change-to-merged-code
  M1-606 refused to smuggle in under a different item. **Filed as `M1-610`.** This branch
  pins that module's *length* and nothing else about it.
- **`retrieval_run_id` has no schema ceiling and is not given one.** `006` leaves it off
  deliberately (`research/store.py` imposes no length on the way out, so the unreadable-row
  defect the ceiling closes does not exist on that column), and
  `test_the_run_id_column_has_no_length_ceiling_and_that_is_deliberate` pins the asymmetry.
  Nothing here regularizes it. `lifecycle.py`'s writer-side cap on it is unchanged.
- **`_SHA256_LENGTH`/`64` stays where it is.** 64 is fixed by SHA-256, not a policy number
  that can drift, and M1-401 already records that the four sha256 rules must not be
  conflated.
- **The migrations still say `_MAX_IDENTIFIER` in their comments** (`004`, `006`, `007`,
  `009`). A migration on master is immutable by checksum, so those comments now name a
  constant that no longer exists and **cannot be corrected**. That immutability is also
  what makes the test below work, so this is the cost of the mechanism rather than an
  oversight.

### Standing risk — two bounds equal at 200 cannot be told apart by any test

`MAX_ACTOR_LENGTH == MAX_IDENTIFIER_LENGTH == 200`. Wiring `submission`'s `released_by`
validator to the identifier bound instead of the actor bound is a mutation that **survives
the whole suite**, and it was run: nothing goes red. No test can close this while the two
numbers agree; what the separate names buy is that the day they diverge, the wiring is
already pointing at the right one. Recorded rather than defended, because a wrong invariant
defended in a comment is what T-903 cost three passes.

Second, smaller: `MAX_NOTE_LENGTH` and `MAX_BODY_LENGTH` have **no schema clause anywhere**.
`010` asks only that `note` be text; `003`/`004` put no clause on their body columns. Their
tests assert cross-writer agreement and nothing more, and the docstrings say so. Do not read
`bounds.py` as a claim that the ledger enforces those two.

### On the mutation pass

Eight mutants, each restored from a pristine copy with `__pycache__` cleared first. **Two
survived on the first attempt and both were the test's fault, not the mutant's** — which is
the part worth keeping.

| Mutant | Killed by |
| --- | --- |
| `approval` re-spells the bound as a literal `201` | the seven-layer parity test |
| `bounds.MAX_IDENTIFIER_LENGTH = 201` | the same test, at its schema layer |
| `bounds.MAX_ACTOR_LENGTH = 201` | the actor test, at `010`'s trigger |
| `submission`'s `released_by` call site re-spells `199` | the actor test, at its writer layer |
| `submission`'s `note` call site re-spells `3999` | the note test — **only after the rewrite** |
| `lifecycle`'s `note` call site re-spells `4001` | the note test |
| `storable_text` truncates to `limit + 5` | the receipt-body composition test |
| `released_by` validator wired to `MAX_IDENTIFIER_LENGTH` | **nothing — see the standing risk** |

**`bounds.MAX_NOTE_LENGTH = 3999` survived, and it was the wrong mutation to aim.** Both
writers read one constant, so moving it moves both and parity still holds — the note test
*cannot* detect that, and no test without an independent witness can. The identifier test
survives the same edit only because the migrations' frozen `length(...) > 200` is a witness
that `bounds.py` cannot reach. That contrast is the actual content of this item: the
identifier bound is pinned to something outside the program, and the other three are pinned
only to each other.

**Then `submission`'s `note` call site re-spelled as `3999` survived as well**, and that one
was a real hole. The first note test drove
`_require_optional_text(value, field, max_length=MAX_NOTE_LENGTH)` on each side — two calls
to the same generic helper handed the same number, which is the test asserting itself. It
was rewritten to drive `lifecycle.record_approval` and `submission.release_submission_key`
end to end. This is M2-710's finding on `released_by` recurring one column over: **the rule
and the wiring are two claims, and a validator-level parity test only makes the first.** The
actor test was rewritten the same way for the same reason, and keeps the raw INSERT beside
the writer because the two probe different things.

### Verification

`tests/unit/test_shared_bounds.py` is the acceptance criterion executed rather than asserted
in a comment. `test_every_layer_reads_the_same_identifier_ceiling` probes at
`MAX_IDENTIFIER_LENGTH` minus one, exactly, and plus one, and collects an accept vector from
five Python validators (each caught on its own error type), `forecast.record`'s pydantic
bound, and rollback-wrapped INSERTs into `forecast_records.record_id` and `.tournament_id`.
It asserts every vector is equal **and** that the value is `(True, True, False)` — equality
alone is satisfied by a set of layers that all refuse everything, which is how a parity test
goes green while asserting nothing.

The assertion is cross-layer equality, never a comparison against the constant: the constant
only chooses where to probe, and the witness is the schema. That is the M1-303 rule ("a
private constant imported to assert against tests the constant") satisfied rather than
worked around.

`forecast.record`'s layer is reached by validating `{"record_id": value}` alone and counting
only a `string_too_long`/`string_too_short` error *on that field* as a refusal — every other
field fails as `missing` and is ignored. That reaches the declared bound without building a
whole record and without restating the annotation in the test.

### On the process

`git checkout src/whiskeyjack_bot/lifecycle.py` was used to restore one mutant and, because
the branch's own rewiring of that file was still uncommitted, reverted that too — putting
`_MAX_IDENTIFIER = 200` back in the one module the whole item is anchored on. Caught
immediately by grepping for the old names, before any gate ran, so nothing was reported
green against it. **The reason it would not have been caught by a test is the point**: the
private constant and the shared one are numerically identical, so a reverted `lifecycle.py`
passes the entire suite, including the new parity test. This item exists precisely because
that condition is undetectable.

`docs/M1-NOTES.md`'s T-901 entry already says **commit before mutating**, after the same
restore step destroyed its own evidence there. Second occurrence, so state it as a rule
rather than an anecdote: while the fix is uncommitted, restore a mutant from a pristine
*copy*, never from git.
