# Milestone 5 implementation notes: experiments and attribution analysis

Running record of the experiment track's decisions and deviations, in the spirit of
`docs/M1-NOTES.md` and `docs/M4-NOTES.md`. M5-804's attribution report predates this file and its
notes stay in `docs/M4-NOTES.md`. This file starts with M5-806 because the experiment track
(M5-805 paired report, M5-806 replay harness, M5-807 source census, and the source-alpha arms of
D63) will accumulate here, and `docs/M4-NOTES.md` is about resolution and scoring.

## M5-806 — Replay experiment harness: never-submittable forecasts from stored packets

Wave 27 (owner decisions 2026-11-15; D62, D63). The ledger holds the exact research packet behind
every resolved forecast, retrieved before the outcome existed. This item adds the instrument that
asks what the forecast would have been under one controlled change, and records the answer where
it can never be submitted. Arm A0 (three control samples) decides sample aggregation (M1-410) and
the calibration fit (M1-515); A1 to A6 are the source-alpha study (`docs/SOURCE-ALPHA.md`); A9
tests the prompt 1.6.0 candidate (M1-411).

### Delivered

- `src/whiskeyjack_bot/experiment/`:
  - `spec.py`: `ArmSpec`, `load_arm_spec`, `arm_sha256`, `MAX_EXPERIMENT_CEILING_USD`.
  - `transforms.py`: `apply_transform`, `ArmDocument`, the labeller interfaces.
  - `fidelity.py`: `check_record`, the AC3 gate and the AC6 leakage guard.
  - `arm_store.py`: the store, its schema, `append_arm_sample`, `read_arm_samples`.
  - `runner.py`: `prepare_arm`, `run_arm`, `check_arm`.
  - `store_schema_001.sql`.
- `whiskeyjack-bot experiment run --spec --ledger --store --config --ceiling-usd [--dry-run]`.
- Arm specs: `experiments/arms/smoke.json` (8 records, one sample) and
  `experiments/arms/a0-control.json` (A0: every record, three samples). Nothing on this branch
  runs either.
- `prompts/archive/forecaster-tournament-1.2.0.md` and `-1.4.0.md`: byte-identical copies of
  the tournament prompts the live records were made under, recovered from `dc3c729` and
  `c3b0260`. Their sha256s are the ledger's `f72a6818…` and `00efd302…`.
- Additive seams in live modules. Each one's default is pinned against bytes captured before the
  change (`tests/unit/test_experiment_seams.py`):
  - `forecast/priced.py`: `ReasoningEffort`, a `reasoning_effort` keyword on `build_request`
    and `PricedClient`, and `MODEL_IN_SERVICE`;
  - `forecast/generate.py`: a `model_input_transform` keyword;
  - `forecast/inputs.py`: `require_transformed_input`;
  - `tournament_state.py`: `BudgetExhausted`.
- No migration, no dependency, no `AppConfig` field (AC9).

### What was established by execution before designing

Measured read-only on the live ledger (`mode=ro`), 2026-11-17.

- **Population.** 229 records: the attribution report's rule (latest posted version per
  question, test tournaments excluded) restricted to a real resolution. By model and prompt, 19
  are Sol on 1.2.0 (33122), 152 Astra on 1.2.0, and 58 Astra on 1.4.0. All 229 ran at medium
  effort.
- **The full request rebuilds.** For all 291 non-test records, D56's recipe rebuilds the stored
  user message byte for byte. The full OpenRouter body also digests to a live `model_started`
  journal scope: the system message is the prompt by sha256, the effort is medium for 1.2.0 and
  1.4.0 and high for 1.5.0. So the strict reading of AC3 is achievable.
- **The gate on the population.** `experiment run --dry-run` against the live ledger, on this
  branch: 229 selected, 19 `model_not_in_service`, **210 passed, every one at medium effort**. The
  live database and WAL were byte-identical afterwards and no store was created.
- **Open times.** Every 33122 question opened between 2026-09-07 and 09-10, and every Astra
  question on or after 09-21. The in-service guard therefore turns away exactly the Sol era, and
  no record sits near the cutoff: the guard's verdict is the same anywhere from 09-11 to 09-20.
- **What the stored request is.** `forecast_records.raw_output_path`'s `request` is only the user
  message (`render_model_input`). The system message is identified by `prompt_sha256` alone. The
  effort is in no record field and in `model_response` only from M1-367 (schema 1.1.0) on.

### Decision — the strict fidelity gate: the full body, not only the user message, and why

AC3 says the record's request is rebuilt "under the record's own prompt and settings" and
compared with "the stored live request". The artifact's `request` is the user message, which
does not depend on the prompt or the effort, so a gate on it alone would check the packet and
never the prompt or the settings the AC names. Under the stricter-reading rule the gate checks
four things, each with its own reason code:

1. The `research_checkpoint` run ids reproduce `research_packet_sha256` under the worker's
   freshness filter at the record's instant.
2. The rebuilt user message, redacted as the artifact writer redacts, equals the artifact's
   `request`.
3. The prompt bytes the record's `prompt_sha256` names are available.
4. `build_request` over that system message, the rebuilt user message and some member of the
   effort vocabulary digests to a live `model_started` scope under the record's own project.

The effort that matches is the record's measured control effort. It is cross-checked against
`model_response.reasoning_effort` where that field exists. Measured, all 210 Astra-era records
pass at medium, which is the fact the plan's "controls are re-run" rule rests on.

The prompt bytes come from the configured prompt directory, the arm's prompt directory, and each
one's `archive/`. Two old tournament prompts are committed there, byte-identical from git.
Recovering them at run time with `git show` would make the gate depend on the checkout being a
repository with that history.

### Decision — one store file with its own journal (owner decision on AC7, 2026-11-17)

AC5 requires spend through the priced client. The priced client reserves through
`tournament_state.Budget`, which journals into a table named `tournament_events`, hard-coded in
`tournament_state`. The posting path reads a table of that name in the live ledger. AC7 says
the store holds no table those paths read. The owner chose this reading:

- The store carries its own copy of migration 012's table, index and two append-only triggers.
  012's `submission_reserve_whole_question` trigger is left out, because it references
  `forecast_records`.
- An insert trigger admits only the eight spend and model-call kinds, so no activation, intent
  or confirmation can be written there.
- The posting path never opens the store, which holds no `forecast_records`, approval or
  submission table.

`test_the_store_holds_no_table_the_posting_path_reads` asserts the intersection with the live
schema is exactly `{"tournament_events"}`. `test_the_store_journal_table_is_migration_012s`
compares the DDL.

Alternatives offered and declined: two files (an arm store and a sibling spend journal); making
the journal's table name a parameter throughout `tournament_state`, the money-critical live
module.

### Decision — the ceiling is cumulative per arm, at most USD 400 (owner decision, 2026-11-17)

Every (arm, record, sample) has its own budget scope, because the priced client's replay cache
is keyed `scope:digest`. One scope for two samples of one request would be one purchase served
twice. Every scope of an arm reserves under one series whose id is the arm's sha256 and whose
ceiling is `--ceiling-usd`, so the ceiling is the arm's total over every run.

A rerun after a ceiling stop, or after a crash, spends only what remains. A later run may raise
the ceiling explicitly. `MAX_EXPERIMENT_CEILING_USD = 400` is a code constant, for the reason
`MAX_ACTIVATION_BUDGET_USD` is one: the limit must not live in a file the operator edits to
authorize the spend. A2, the six-subset arm, is about USD 340.

The reservation is the existing `Budget.reserve`, unchanged. Its threshold alerts go through
`notify.emit`, which is a no-op with no notifier installed, and the harness installs none.

### Decision — the store may not sit beside the live ledger, and why

`Budget.reserve` writes each reservation's witness twice: under the budget root, and under
`guard_root(conn)`, which is the store file's directory plus `.posting-guard`. The live worker's
`check_storage` looks up every file in *its* guard directory in its own journal and stops the
worker on one it lacks ("storage restore detected").

So a store at `data/arms.sqlite3` would stop the worker on its next poll. `require_store_location`
refuses three placements:

- a store whose directory is the live ledger's;
- a store that is the ledger file itself;
- a store whose directory, or its `-files` directory, is the live artifact root.

`test_the_live_ledger_is_never_opened_for_writing` runs the live `check_storage` after a full run
and requires it to pass. The default location is `data/experiments/`. Found by the plan-stage
design check, before any code.

### Decision — the arm binds to its generation settings and to a frozen selection

`arms` records two things at an arm's first run beside its spec:

- **Settings.** A digest of everything `generate_forecast` reads from configuration that the spec
  does not name: the model call's bounds, the forecast bounds and rules, the numeric
  calibration, and the freshness window the gate rebuilds with. A later run under other settings
  is refused, because its rows would not be one arm's.
- **Selection.** The ordered list of control record ids, so a resumed arm never admits a question
  resolved after it started. SOURCE-ALPHA: "each arm runs once over the population as it stands
  when the arm starts". Extending an arm at 450 resolved questions is Deferred.

### Decision — the transform is applied at the spending site and held to one rule there

`generate_forecast` builds the reasoning packet itself, numbering the documents over the whole
packet. A transform receives that numbered packet and returns the one the model is shown.
`inputs.require_transformed_input` holds the result to a structural rule before any spend:

- same plain type, re-validated;
- every non-document field unchanged, the cutoff included;
- `sources` a subsequence of the base in base order;
- the rendered documents exactly those `sources` names, each a base document with at most
  `publisher` and `url` changed.

Because `sources` is what the attribution check reads, a citation of a dropped document is
refused (`test_a_dropped_documents_citation_is_refused_by_the_attribution_check`). An empty
subset turns the must-cite rules off rather than failing every reply. The property
`test_the_spending_site_accepts_every_transforms_output` makes the two layers agree.

The runner builds the transformed input before the call, to hash it and to apply the post-cutoff
guard. It passes it in as a function of the base, so the spending site also confirms the gate's
base and its own are one packet.

### Decision — randomness is a hash, not a generator

A draw is the first 64 bits of `sha256(canonical([seed, question_id, sample_index, key]))` over
2**64. `random.Random`'s algorithms are not a documented compatibility promise across Python
versions, and a hash keyed on the document makes the subset independent of the order documents
arrive in. The sample index is A2's subset index. The shuffle sorts by the same draw.

### Decision — smaller choices, stated so they are not proposed as findings

- **Spec format: JSON.** PyYAML's construction stage raises outside its own hierarchy (M1-308,
  M0-007), and a spec is short. NaN, Infinity and overflowing numbers are refused.
- **Spec hash: the whole validated spec**, its label and prompt path included; any edit is a new
  arm. Sets must be written sorted and without repeats, so one arm has one spelling. Effort is not
  in `RecordedModelSettings`, so a record's `forecast_sha256` does not cover it; the arm sha256 on
  every row is what binds it.
- **Effort is a keyword** on `build_request` and `PricedClient`, defaulting to the constant. The
  worker passes nothing, so its bodies and every live cache scope are unchanged; the pinned
  digests prove it. The vocabulary is closed (`low`, `medium`, `high`).
- **`BudgetExhausted(TournamentError)`** at the round and series refusals, so a run stops cleanly
  on exactly the ceiling and fatally on every other journal refusal. This follows the
  `AskNewsSubcapReached` precedent; every existing catcher is unchanged.
- **Record-major order**, so a ceiling stop leaves complete records for a prefix, in record order
  (SOURCE-ALPHA "Budget").
- **The arm record's `generated_at` is the control's instant**, because it is the request's
  `as_of`, which the reply must echo. The replay's wall-clock time is the row's `created_at_utc`.
- **The row keeps two hashes**: the kept documents' packet hash, and the sha256 of the exact user
  message. `packet_sha256` ignores order and rendering, so a shuffle or a mask would otherwise
  read as the control.
- **Cost is integer micro-USD**, from the store's journal: settled actual, None while any
  reservation is held. Billed calls are the `cost_reserved` count, never
  `generation.invocations`, which counts cache replays (M1-315).
- **Credential first.** The CLI checks the credential and the priced call's model bounds before
  the store is touched. Inside `PricedClient.invoke` a missing key surfaces only after
  `model_started` is written, which would leave that sample's scope refusing every later purchase
  as an unknown outcome.
- **One run at a time**: an exclusive `fcntl` lock on `<store>.lock`. `tournament.worker_lock` is
  off limits, because `tournament` imports the posting path.
- **`mask_publisher`** replaces `publisher` (a missing one stays missing) and the URL's scheme and
  host. Tokens are keyed by `host_identity` and the publisher string, numbered by first appearance
  in source-id order; path and query are kept. These are the only two fields `_document_input`
  renders that name a publisher.
- **In-service registry.** Astra at 2026-09-11T00:00Z, the end of the day M1-408 read OpenRouter's
  listing; an earlier provider release only makes it stricter. Sol is not registered, so a Sol arm
  is refused for every question rather than admitted on a guess.

### Deviation — the cluster and publisher-class transforms are built but not wired (AC4)

AC4 lists `drop` by source cluster or publisher class, and `collapse_syndication`, among the
transforms that "ship as pure functions with a property pass". They do. But the clusters are
M1-321's and the classes M1-369's, and both rows are Not Started.

These kinds therefore take a labeller by name, are property-tested against synthetic labellers,
and are refused by the CLI before anything is spent. The refusal names the backlog items. The
brief settled this ("build the transforms against an interface … say which wait").

| Wired now | Waiting for a labeller |
| --- | --- |
| identity, no_research, drop_provider, mask_publisher, shuffle_order, random_subset(document) | drop_cluster, drop_publisher_class, collapse_syndication, random_subset(cluster) |

### Rejected — and why not

- **Replay rows in the live ledger.** D62: a live migration stops the worker until `init-ledger`,
  and it mixes bulk experiment writes with the posting path.
- **Reading the stored live forecast as the control.** D62: live forecasts before 2026-11-11 were
  made at medium effort and older prompts. The control is the identity arm, re-run.
- **A cross-family arm before in-service dates exist.** The leakage rule cannot be applied
  without a date, so the spec refuses a model with none.
- **Accepting `submission_policy`'s legacy packet rules** (unfiltered, `without_future`). The
  population contains no record made under them, and a gate that accepts three rules is three
  times the surface for a coincidental match.
- **Importing `submission_policy` for the rebuild.** It is on the posting path (AC7).
- **A per-invocation ceiling.** Rejected by the owner: one arm could spend several ceilings'
  worth across reruns.
- **Seeding Python's `random`.** See the randomness decision.
- **Recovering old prompts with `git show` at run time.** See the fidelity decision.

### Deferred (do not read the absence as an omission)

- **Wiring the cluster and class labellers**, when M1-321 and M1-369 merge.
- **Extending an arm to newly resolved questions** at the 450-question analysis point
  (SOURCE-ALPHA "Analysis points"). Today a resumed arm uses its frozen list, so extension needs
  an explicit operation.
- **Scoring arm forecasts.** M5-805 (the paired report) reads `read_arm_samples`.
- **Refusing purchases when a store is reopened after a restore** (**M5-808**, filed from review
  round 1). The live worker runs `check_storage` before purchasing; the store does not yet. The
  row records one design constraint: two stores in one directory share a `.posting-guard`.
- **Locale-rendering (lens) arms.** `generate_forecast` refuses a transform combined with
  `render_locales`. The lens returns as a live labelled arm (M1-614).

### Standing risk — not verifiable offline

- **The provider may change the model behind an identifier.** Every row stores the model
  identifier OpenRouter reported for each billed call, so a change between arms is visible in the
  data, not prevented.
- **Request redaction could hide a real difference.** The gate compares the rebuilt message after
  `redact_secrets`, as the artifact was written. A secret value appearing in a document would be
  redacted on both sides. The full-body digest is taken over the unredacted rebuild, so a
  difference there still fails `call_not_reproduced`.
- **Masking is partial.** Titles and summaries can still name the publisher, and AC4 forbids
  editing content. A3 measures "publisher and host masked", not "publisher unknowable".
- **The archive is only as complete as its files.** A record under a prompt with no archived copy
  is excluded as `prompt_unavailable` and counted. It is never silently admitted.

### AC9 — no AppConfig field, no live migration, the activation not retired

Checked on the branch:

- `git diff origin/master -- src/whiskeyjack_bot/config.py src/whiskeyjack_bot/migrations/` is
  empty.
- `tournament_state.bindings(load_config("config/tournament.yaml"))` gives the same values on
  master (`5af4776`) and on this branch: `config_sha256` `3d35d5238de9ab48…`, `prompt_sha256`
  `cbcd738dd8104780…`.

The live modules this branch touches gain only keywords with unchanged defaults, a new constant
and a new exception subclass, and the worker loads them at its next poll. That is not a deploy:
no migration and no config change, so the activation is not retired.

### Property tests

- `tests/property/test_arm_spec_properties.py`:
  - the loader raises only `ExperimentError` on arbitrary JSON and arbitrary bytes;
  - the hash is stable across the persisted form and ignores key order, whitespace and number
    spelling;
  - non-canonical sets are refused;
  - every field is inside the hash;
  - a planted canary never reaches a refusal;
  - the strategy reaches every transform kind.
- `tests/property/test_transform_properties.py`:
  - a transform adds, renumbers and edits nothing;
  - the spending site accepts every output;
  - order invariance and determinism;
  - mask tokens follow host and publisher identity;
  - misbehaving labellers are refused as `ExperimentError`;
  - inclusion tracks p over 4,000 keys;
  - seeded draws differ by sample, seed and question;
  - the strategy reaches every kind, a non-applicable result and a proper subset.

### Mutation testing

Copy-based, with the M4-809 harness adapted (`~/wj-prompts/wave27/m5-806-drafts/mutate_m5806.py`):

- each worker takes a `git archive HEAD` and never touches the worktree;
- it checks that its copy is the one imported, and that the unmutated copy passes;
- each mutant then runs the four M5-806 test files with `-x` at the `dev` profile.

**First run, on `9c7af91`: 46 mutants, 40 killed, 6 survived.** Every survivor was a test gap,
not an equivalent mutant, and each now has a test:

| # | Mutant | Why it survived | Test added |
| --- | --- | --- | --- |
| 06 | checkpoint first-run filter dropped | the hash check rejects a wrong run set anyway | `test_only_checkpoints_led_by_the_records_own_run_are_candidates` calls the filter directly |
| 22 | frozen selection replaced by the current population | no test changed the population between runs | `test_a_resumed_arm_uses_its_frozen_selection_not_the_population_now` |
| 28 | `drop_provider` keeps the dropped providers | the properties checked "nothing added", never "exactly these kept" | `test_each_kind_keeps_exactly_what_its_spec_says`, a per-kind oracle restated from the spec |
| 29 | `collapse_syndication` keeps the last member | same gap | same oracle |
| 35 | the guard's exact-type check replaced by `isinstance` | re-validation also refuses a lens packet, so the type rule never fired alone | each refused case now names the rule that must refuse it (`_REFUSED_BY`) |
| 40 | the read-only ledger probe removed | the later check still refused, after a read-write open that leaves WAL-mode bytes unchanged | the test spies on every open of the path and requires `?mode=ro` |

The 40 killed include every fidelity check, both leakage guards, the per-sample scope, the
post-cutoff guard, the location guard, the per-arm series, the ceiling maximum, the
resume skip, billed calls and held cost read from the journal, the client-model check, the
settings binding, id renumbering, the sample-index draw, mask's None rule and host identity,
input order, every spending-site guard clause, the transform seam, the effort keyword, the
store's reader and writer hash checks, the journal-kind trigger, and the spec's set, model,
prompt-hash and label rules.

**Re-run of the six on `31e82f8`:** five killed. Mutant 29 survived again. The new oracle
property does kill it when run alone, so the miss is a reach problem: in 200 random draws the
property may never meet `collapse_syndication` over a cluster of two or more. Deterministic
tests now pin the cluster kinds on a fixed packet of three host clusters:

- `test_collapse_keeps_the_lowest_source_id_of_each_cluster`;
- `test_drop_cluster_drops_the_kth_cluster_ordered_by_its_lowest_member`;
- `test_drop_provider_drops_exactly_the_named_providers`.

The first fails against mutant 29 (checked on a mutated `git archive` copy). The lesson is
the vacuous-property class in another form: a property that kills a mutant *sometimes* is a lottery,
not a test. A rule that holds only on a rare input needs a fixed example as well.

### Review

**Round 1 (`ae05693`): CHANGES REQUESTED, one blocking finding.**

- **B1: a prompt path the filesystem cannot take escaped as a raw exception.** Reproduced on
  `ae05693` before any fix:
  - `prompt.path` set to `"bad\u0000prompt.md"`, which is valid JSON, loaded. Reading
    `loaded.prompt_path` then raised `ValueError: embedded null byte`. The CLI reads it outside
    its handlers.
  - Siblings, enumerated per LESSONS 6. A prompt path that is a symlink loop raised
    `RuntimeError` from Python 3.11's non-strict `Path.resolve`. Lone surrogates and over-long
    paths were already refused as `ExperimentError`.

  Fixed as a class:
  - `PromptRef` refuses a NUL with an authored error.
  - `load_arm_spec` resolves the prompt path once, inside its own refusal handling, so
    `prompt_path` is a plain field rather than a lazy property on every caller's path.
  - The other three `resolve` sites in the package now also catch `RuntimeError` and
    `ValueError`.

  Regressions, each confirmed to fail on a `git archive` of `ae05693`:
  - the NUL load;
  - the symlink-loop load;
  - the CLI exiting 2, dry and paid;
  - a property drawing any text as the prompt path.

  That property first passed against the pre-fix code, because plain `st.text()` almost never
  draws a NUL. Its alphabet is now weighted toward NUL, surrogates, separators and `..`, with a
  reach check of its own.
- **Non-blocking: verify spend witnesses when a store is reopened after a restore.** Accepted as
  a backlog candidate and filed as **M5-808**. It is not done here because `check_storage` reads
  the database directory's `.posting-guard`, which two stores in one directory would share, so a
  naive call would refuse a healthy store. That needs its own design.

**Round 2 (`2389f18`): APPROVE, no blocking findings.** B1 closed, and the restore observation
stays tracked as M5-808. The reviewer could not run pytest in its sandbox, because the
virtualenv's interpreter was unavailable to it. Its approval rests on source and diff
inspection. The gates are this branch's own: `review-request.py` ran all four before each
request and refuses to emit on a red gate, and CI's `quality-gate` passed on the PR. Approved
is therefore not the same as independently reproduced.
