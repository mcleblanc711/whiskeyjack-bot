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

Copy-based (`git archive HEAD` per worker, never the worktree), with the M4-809 harness adapted:
`~/wj-prompts/wave27/m5-806-drafts/mutate_m5806.py`. Results are recorded below once run.
