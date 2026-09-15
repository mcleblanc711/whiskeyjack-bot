# Milestone 4 implementation notes

Running record of M4 decisions and deviations, in the spirit of `docs/M1-NOTES.md` and
`docs/M2-NOTES.md`. M4 is resolution and scoring: the half of the attribution record that
arrives after a forecast is posted. Ingestion lands first because both scorers depend on it.

## M4-801 — Ingest resolution snapshots

Acceptance: *Resolved fixture updates only via append event; annulled/ambiguous outcomes are
not scored.*

### Delivered

- `src/whiskeyjack_bot/migrations/014_resolution_ingestion.sql` — seven `ADD COLUMN`s on
  `resolution_events` (`post_id`, `question_type`, `resolution_kind`, `scorable`,
  `observation_sha256`, `source_response_sha256`, `observed_at_utc`); an upgrade precondition;
  `resolution_events_validate_on_insert`, `resolution_events_append_in_order` (AFTER INSERT)
  and `score_events_require_scorable_resolution`. `LEDGER_SCHEMA_VERSION` → 14.
- `src/whiskeyjack_bot/resolution.py` — `ResolutionError`, `ResolutionKind`,
  `ResolutionObservation`, `classify_resolution`, `observation_from_snapshot`,
  `canonical_json`, `sha256_text`. Pure.
- `src/whiskeyjack_bot/lifecycle.py` — `record_resolution_observation`,
  `latest_resolution`, `read_resolution_history`, `StoredResolution`, `ResolutionWrite`;
  `_append_event` gains `resolution_event_id`.
- `src/whiskeyjack_bot/resolution_ingest.py` — `ingest_resolutions`, `sdk_post_fetcher`,
  `IngestResult`, `ResolutionFetchError`, `ResolutionIngestError`.
- `src/whiskeyjack_bot/cli.py` — `whiskeyjack-bot ingest-resolutions --config PATH
  [--question-id ID]`, the name `CODEX_HANDOFF.md` § Required CLI entry points gives it.
- `src/whiskeyjack_bot/export.py` — the seven columns in the `resolution_events` spec.
- `tests/resolution_rows.py` (shared fixtures), `tests/unit/test_resolution.py`,
  `tests/unit/test_resolution_ledger.py`, `tests/unit/test_cli_ingest_resolutions.py`,
  `tests/property/test_resolution_properties.py`, and
  `tests/fixtures/api_posts/resolution/withheld_minibench_45321.json` (a real payload).
- `docs/backlog/decisions.csv` — **D35**.

No `AppConfig` field was added, so `config_sha256` and the live activation are untouched. No
dependency was added. The command makes GETs only and imports nothing that can post.

### What was established by execution before designing

**Metaculus withholds resolution values from an account that did not predict on the
question.** With this project's bot token (account 305299, `is_bot: true`), ~270 resolved
questions sampled on 2026-09-14 -- 2024 AIB, the 2026-08-24 MiniBench batch, Metaculus Cup,
public "learning" posts -- all came back `question.status: "resolved"` with
`question.resolution: null`; `?with_cp=true` changed nothing and an unauthenticated request is
a 403. The cause is documented, not a defect: Metaculus's own `docs/openapi.yml`
(`Metaculus/metaculus@1c9685a`, the source of `/api/`) says under "All Authenticated
Accounts": *"For closed questions, you can access the text and resolution value of any
question you have predicted on."* Their serializer (`questions/serializers/common.py:134`)
returns the stored value unmodified, so the masking happens outside the public code. The
~250-question "Bot Benchmarking Access Tier" is the documented way to see more.

This is what made the question "is the spec in conflict with the platform?" come out *no* for
this item's scope: every record M4-801 resolves is one this account posted. It also created
the `withheld` kind (below) and the scope rule (only records that reached `submitted`).

**The pinned SDK:** `MetaculusQuestion.resolution_string` is `question.resolution` verbatim.
`typed_resolution` is lossy -- an unknown string falls through unchanged, `float("nan")`
parses, anything `pendulum` accepts becomes a datetime. `CanceledResolution` is
`annulled | ambiguous`; `OutOfBoundsResolution` is `above_upper_bound | below_lower_bound`.
`get_question_by_post_id(post_id, "unpack_subquestions")` is a GET with the SDK's read retry;
group unpacking deep-copies the post and puts one subquestion under `question`. The SDK also
exposes `unresolve_question`, so a resolution can be retracted on the platform.

**The schema already had the table, append-only.** `001` created `resolution_events`; `003`
gave it unconditional UPDATE/DELETE blocks, a `resolved` lifecycle event
(`submitted -> resolved`) that must cite one of its rows, and two ownership probes. Nothing
wrote to it and nothing constrained an INSERT.

**The live worker does not read the status this item moves.** `tournament.py` decides "done"
from `tournament_events.forecast_confirmed` and only checks `current_status == "validated"`
(`tournament.py:729`); `check_storage` witnesses only `tournament_events`. A record moving
`submitted -> resolved` changes nothing it does.

**The live ledger at design time:** schema 13; 20 records on 33122, all `submitted` (11
binary, 6 numeric, 3 discrete); `resolution_events` and `score_events` empty.

### Decision — widen `resolution_events`, not a new table, and why

`lifecycle_events.resolution_event_id` and 003's probes already point at this table. A new one
would strand that column and leave the `resolved` transition reachable only through a rewrite
of `lifecycle_events_validate_on_insert`. `resolution_events` has no column CHECK to widen,
so this is the `ADD COLUMN` + trigger path (009, 011, 013), not the rebuild 003's header warns
about. The brief's "prefer a new table (the 010 precedent)" was written against widening
`lifecycle_events`' CHECK, which this does not do.

### Decision — five kinds, one scorable, and the vocabulary is a trigger clause

`resolved` (a definite outcome; the only scorable kind), `annulled`, `ambiguous`, `withheld`
(status resolved, value masked) and `unresolved` (status not resolved -- recorded only after
an earlier observation, where it is a retraction). `scorable`, `annulled` and `ambiguous` are
stored flags derived from the kind and pinned by the trigger, so a scorer need not know the
vocabulary. The vocabulary lives in a trigger rather than a CHECK so widening it is a
DROP/CREATE.

### Decision — the "not scored" guard is on `score_events`, where a scorer cannot route around it

`score_events_require_scorable_resolution` refuses any score row unless the record's
**latest** resolution row has `scorable = 1`. That is the acceptance criterion's second clause
made structural, it binds M4-802 and M4-803 without a migration of their own, and it is the
witness outside the program: `test_resolution_ledger.py` drives it by raw SQL.

### Decision — the current resolution is the latest row, not the row the lifecycle event cites

A platform can re-resolve or retract after the record has moved. The `resolved` lifecycle event
is appended once, on the first definitive observation of a `submitted` record, and 003 has no
`resolved -> resolved` transition. So later observations are appended as rows only, and both
the score guard and `latest_resolution` read the highest `event_id`.
`resolution_events_append_in_order` makes "highest `event_id`" mean "appended last" even
against an explicit `event_id` (an AFTER trigger, because a BEFORE trigger reads an
auto-assigned id as -1, indistinguishable from an explicit -1).

### Decision — idempotency compares with the latest row, and the database enforces it

A repeated poll that sees what the latest row records appends nothing. A change appends,
including a return to an earlier state: A, B, A is three rows. The writer skips the repeat
(`outcome="unchanged"`); the trigger refuses it against raw SQL.

### Decision — the writer derives the observation from the payload; the payload is the only input

`record_resolution_observation(conn, record_id=, source_response=, observed_at=)` classifies
the payload itself against the record's own `question_id`, `post_id` and `question_type`. A
caller-supplied observation next to the payload it came from would be a second source of
truth for the hashed field (lesson 9's corollary; M2-703's). Both digests are computed in the
writer from the canonical text it stores, and the reader recomputes both on the way out.

### Decision — classify the raw payload, never `typed_resolution`

See "established by execution". Binary accepts `yes`/`no`; multiple choice accepts a label in
`all_options_ever` (falling back to `options` -- Metaculus can add options to an open
question); numeric and discrete accept a strict decimal (`-?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?`,
finite) or an out-of-bounds token. Dispatch is on the ledger's `question_type` literal, and the
payload's `question.type` must equal it -- so a discrete payload cannot resolve a numeric
record even though `DiscreteQuestion` subclasses `NumericQuestion`.

### Decision — scope is records whose history reached `submitted`

Including records already `resolved` or `scored`, so a retraction is seen. A record never
posted is not polled: the API would return `withheld` for it forever. The writer refuses any
record whose current status is not `submitted`/`resolved`/`scored`.

### Decision — one GET per post, through the SDK

Records are grouped by `post_id`, so a group post whose subquestions were each forecast is
fetched once and every record reads one payload observed at one instant (asserted). The fetch
uses `get_question_by_post_id` (D03) with its read retry as shipped; the SDK's parse runs on
the way, and a payload it cannot parse is a fetch failure. `tests/unit/test_resolution.py`
drives the real SDK method with `requests.get` stubbed over every type × kind, a group post and
the real masked payload.

### Decision — `ingest-resolutions` exits non-zero if any record was skipped

After recording every record it could. A scheduled run that half-failed must not report success.

### Deviation — the stricter reading of a five-word criterion

"Resolved fixture updates only via append event" is read as applying to **every** ingested
resolution, not only to fixtures, and "annulled/ambiguous outcomes are not scored" is read as
"cannot be scored" (a database refusal) rather than "the scorer skips them". Two kinds the
criterion does not name (`withheld`, `unresolved`) exist because the platform produces those
states; both are not scorable.

### Deviation — 003's question-mismatch probe is now shadowed

`lifecycle_events_validate_on_insert` refuses a `resolved` event citing a resolution row for a
different question. 014 refuses that row at INSERT, so the older probe cannot be reached by
ordinary SQL. `test_a_resolution_for_another_question_cannot_resolve_this_forecast` now asserts
both layers: 014 refuses the row, and with 014's trigger dropped on that test's own ledger,
003's probe still refuses the event.

### Deviation — `test_foreign_key_enforced` moved tables, again

014's score guard fires before `score_events`' foreign key, so the unmatched
`pytest.raises(IntegrityError)` kept passing on a different mechanism than its name. It now
inserts into `research_documents` (whose trigger checks provenance, not the run) and matches
`FOREIGN KEY constraint failed`.

### Rejected — a new `resolution_observations` table

See the first decision.

### Rejected — a UNIQUE index on `observation_sha256`

It would refuse the second A in A, B, A and lose the retraction between them.

### Rejected — storing the raw response as an artifact file instead of in the row

The row is the ledger's replay substrate for everything else this small, a MiniBench post as
this account sees it is ~5 KB (aggregations are masked), and an artifact would add a
filesystem dependency and a second place for the evidence to go missing. Capped at 4 MiB.

### Rejected — a range check on numeric outcomes

Metaculus validates a numeric resolution against the question's bounds when it is set
(`questions/serializers/common.py` `validate_question_resolution`), and M4-803 ingests platform
scores rather than computing a local numeric score (D30). A local range check would be a
second, divergent copy of the platform's rule.

### Rejected — an `AppConfig` field for ingestion

Nothing needed one, and any field retires the live activation.

### Deferred (do not read the absence as an omission)

- **A `resolved -> resolved` lifecycle transition** so a re-resolution is also a lifecycle
  event. A rewrite of `lifecycle_events_validate_on_insert`; nothing consumes it yet, because
  the score guard and the reader already use the latest row. Backlog: **M4-804**.
- **Records left `approved` by an uncertain submission** are not polled. Such a record may have
  posted; it becomes resolvable once `verify-submission` confirms it. Backlog: none -- the
  existing uncertainty path is the route in.
- **Scheduling ingestion** (a timer, or a phase of `tournament run-once`). Operator-run for now;
  a timer is a deploy change. Backlog: **M4-805**.
- **Scoring.** M4-802 (Brier/log) and M4-803 (platform scores) consume `latest_resolution` and
  are bound by the score guard.

### Standing risk — not verifiable offline

- **Unmasking of our own questions is documented but not yet observed.** No question this
  account forecast had resolved on 2026-09-14; batch-1 resolves around 2026-09-17. If the value
  stays null, every record ingests as `withheld`, nothing is scorable, and the fault is access,
  not this code. The first live `ingest-resolutions` after 2026-09-17 answers it.
- **Real resolved payload shapes are inferred.** The resolved fixtures are the committed post
  fixtures with `status`/`resolution` replaced by documented shapes (`openapi.yml` examples:
  `"no"`, `"77289125.94957079"`). The only real resolved payload is the masked one. A resolved
  MC or discrete value in a shape the classifier refuses would surface as a per-record
  `failed` line, not as a wrong row.
- **Metaculus Terms of Use** (`openapi.yml` preamble) restrict using API data "to train,
  evaluate, or otherwise create or develop AI/ML models" without permission. Resolutions of this
  account's own tournament forecasts are "your own data" under the same page, but that is a
  reading, not legal advice; flagged for the owner.

### Found during the gate run — filed, not fixed here

- **T-909.** `test_a_retired_profile_polled_every_five_minutes_pages_once` fails whenever it
  starts between 23:00 and 00:00 UTC: its injected clock starts at the real `utcnow()`, and
  `notify.py`'s tumbling day window pages again across midnight. Reproduced on master
  `a0e9faf` at 23:36 UTC; untouched by this branch.
- **Two seeds this branch had missed**, found by the gate rather than by the earlier
  unit/integration/acceptance run, because that run did not include `tests/property`:
  `test_lifecycle_properties.py`'s detail-row seed wrote a bare resolution row, and
  `test_sdk_contract.py` needed a `THIRD_PARTY_REACHES` row for the fetcher's `api_json` read.

### Mutation testing

Committed before mutating (lessons 5 and 8); `__pycache__` cleared before every mutant;
each mutant restored with `git checkout` and the tree asserted clean at the end. Runner:
four new test files, `-x`.

**Trigger clauses — neutered whole, `WHERE 0 AND (<predicate>)`: 14 of 14 killed.** Every
`RAISE` in 014, including the score guard and the AFTER INSERT ordering trigger. A first
pass wrote these as `WHERE 0 AND a OR b`, which SQL parses as `(0 AND a) OR b` and so
neutered only the first disjunct; seven of those "survived". They are not survivors -- they
were not mutants of the clause -- and the whole-clause pass is the one recorded. What the
malformed pass did show is that each clause's leading `typeof` check is subsumed by the
disjuncts after it for every value these tests write (INTEGER affinity converts `"0"` to 0
before the trigger reads it), which is harmless redundancy, not a gap.

The upgrade precondition (`LIMIT 1` → `LIMIT 0`): killed.

**Python — 16 mutants, 16 killed after one fix.**

| mutant | killed by |
|---|---|
| C1 a cancellation classified `resolved` | `test_every_kind_classifies_for_every_type` |
| C2 `scorable` always true | same |
| C3 status check dropped | malformed shape "resolution on a closed question" |
| C4 multiple-choice membership dropped | `test_a_multiple_choice_label_must_be_one_the_question_has_had` |
| C5 strict decimal dropped (`float()` only) | `test_a_continuous_outcome_float_would_accept_is_still_refused` |
| C6 missing `resolution` key read as withheld | malformed shape "no resolution key" |
| C7 payload/record type mismatch accepted | `test_a_discrete_payload_does_not_resolve_a_numeric_record` |
| C8 snapshot digest drops `resolution_set_time` | snapshot replay test; **and** property 6 alone |
| C9 binary outcome set widened | out-of-bounds token refused for binary |
| W1 writer idempotency skip dropped | `test_a_repeated_poll_writes_nothing`; **and** property 5 alone |
| W2 lifecycle link dropped | `test_a_ledger_at_013_upgrades_to_014` |
| W3 every kind moves the record | `test_a_withheld_value_is_recorded_but_moves_nothing` |
| W4 reader skips the snapshot digest | **survived**, then `test_a_stored_snapshot_that_no_longer_matches_its_digest_is_refused_on_read` |
| W5 writer accepts an unposted record | `test_a_record_that_was_never_posted_cannot_be_resolved` |
| W6 writer accepts another post's payload | `test_a_payload_for_another_post_is_refused` |
| I1 ingest scope includes unposted records | `test_every_submitted_record_is_resolved_with_one_fetch_per_post` |

W4 was a real gap: the only tamper test altered `source_response`, so the snapshot half of
the reader's re-verification could be deleted with every test green.

**The property file on its own**, since `-x` over four files means a unit test usually
kills a mutant first: C8, W1 and a planted leak (the status value interpolated into its
refusal) each fail `tests/property/test_resolution_properties.py` run alone -- C8 fails
property 6 (`test_two_observations_share_a_digest_only_if_they_are_equal`) in isolation.

**Strategy reach, measured with `hypothesis.event` before trusting any property.** Two were
nearly vacuous as first written and were fixed before commit:

| property | as written | after |
|---|---|---|
| replay through the persisted form | 90% of draws refused; <3% classified | 0% refused; every kind reached (29% resolved, 44% unresolved, 7-8% each other) |
| digest injectivity (tiny alphabet) | 98.6% of draws invalid (~7 useful examples) | 0% invalid; 20% equal pairs |

### Review

**Round 1 — APPROVE on `7163136`** (2026-09-15, local Codex against
`GPT_REVIEW_REQUEST_M4-801_r1.md`, all four gates green in the request). No blocking findings
and no backlog candidates; each of the eight falsifiable risk claims was marked safe. The
reviewer could not run pytest in its environment and says so; its verdict rests on the pinned
diff and the implementation. This entry is the only change after the approved commit.

## M4-802 — Compute binary and multiclass scores

Acceptance: *Known examples match hand calculations; extreme probabilities are handled safely.*
Description: *Calculate Brier and log scores with versioned implementations.*

### Delivered

- `src/whiskeyjack_bot/scoring.py` — pure: `ScoreError`, `LocalMetric` (`local_brier_binary`,
  `local_log_binary`, `local_brier_multiclass`, `local_log_multiclass`),
  `IMPLEMENTATION_VERSIONS`, `LocalScore`, the four `*_v1` functions, `score_binary`,
  `score_multiple_choice`, `recompute`.
- `src/whiskeyjack_bot/migrations/015_local_score_events.sql` — `score_events.resolution_event_id`
  by `ADD COLUMN`; an upgrade precondition (no pre-015 score rows);
  `score_events_validate_local_score_on_insert`; the UNIQUE index
  `score_events_one_per_observation_metric_version`. `LEDGER_SCHEMA_VERSION` → 15.
- `src/whiskeyjack_bot/lifecycle.py` — `record_local_scores`, `read_local_scores`, `StoredScore`,
  `ScoreWrite`; `_append_event` gains `score_event_id`.
- `src/whiskeyjack_bot/score_records.py` — `score_records`, `ScoreResult`, `ScoreRecordsError`.
- `src/whiskeyjack_bot/cli.py` — `whiskeyjack-bot score --config PATH [--record-id ID]`.
- `src/whiskeyjack_bot/export.py` — `resolution_event_id` in the `score_events` spec.
- `tests/unit/test_scoring.py`, `tests/unit/test_score_ledger.py`, `tests/unit/test_cli_score.py`,
  `tests/property/test_scoring_properties.py`; shared `tests/score_rows.py` (real, hash-verified
  records walked to `submitted` and resolved through the production writers) and
  `resolution_rows.insert_score_row` (a 015-valid raw row).
- `docs/backlog/decisions.csv` — **D36**.

No `AppConfig` field was added, so `config_sha256` and the live activation are untouched. No
dependency was added. `score` builds no client and makes no network or paid call; its tests
replace `build_client` and every `requests` verb with refusals and assert none was reached.

### What was established by execution before designing

**Nothing this account posted had resolved.** `ingest-resolutions --config config/tournament.yaml`
from the main checkout at 2026-09-15 00:44 UTC: 23 records polled, all `nothing_to_retract`,
0 failed, none `withheld`. So M4-801's unmasking risk has neither fired nor been retired, and no
real resolved payload exists to capture as a fixture.

**What is posted is what the record says, for these two types.** The live ledger (schema 14,
opened `mode=ro`): 23 successful attempts, all `refetch_outcome=confirmed`, and 23/23
`request_payload_sha256` equal to the latest approval's `payload_sha256`. Of those, 13 are binary
or multiple choice (11 MiniBench binary; one binary and one multiple-choice on the bot-testing
area -- **batch 1 has no MiniBench multiple-choice question**). For all 13, `final_prediction`
equals the refetched platform value (binary stored as `[1 - p, p]` with float noise, multiple
choice by label under a `label_order` permutation). And `submission_payload._binary_payload` /
`_multiple_choice_payload` are verbatim copies of `final_prediction` -- calibration touches only
the numeric CDF.

**Metaculus's scores are not Brier and not raw log.** The FAQ URL returns 403 to a fetch; the
source is `Metaculus/metaculus@8069116` `scoring/score_math.py`. It uses `np.log` (natural log)
throughout; the binary baseline score is `100 * ln(2p) / ln 2`, the peer score
`100 * ln(p / geometric_mean)`, both over time-weighted coverage; there is **no Brier score**; and
a multiple-choice forecast that lacks the resolved option falls back to `pmf[-1]`.

**`score_events` could not say which outcome a score measured.** 001's columns are `metric`,
`value`, `implementation_version`, `comparison_baseline`, `computed_at_utc`. With re-resolution
and retraction possible (M4-801), neither "a new score against the new latest resolution" nor a
database-witnessed idempotency rule is expressible without a link. 003 lets a `scored` lifecycle
event link exactly one score row (`lifecycle_events_one_event_per_score`), and its only scoring
transition is `resolved -> scored`.

**The live records score end to end (on a copy).** The live ledger was copied with SQLite's
backup API into a scratch directory, upgraded to 15 by `initialize_ledger`, and scored: nothing
to score (no resolutions). One real MiniBench binary record (`probability_yes` 0.91) was then
resolved `no` through `record_resolution_observation` with a committed post fixture, and scored:
`local_brier_binary` 0.8281000000000001 (0.91^2) and `local_log_binary` -2.4079456086518722
(ln 0.09 = 2 ln 0.3 = -2.40794560865187 by `bc -l`); a second run `unchanged`; the JSONL export
carries both rows with `resolution_event_id`. So a record written by the live pipeline reads back
through `read_forecast_record` and scores. The live ledger itself was not written.

**SQLite fires the newer trigger first.** With no resolution row, a score row is refused by 015
("must name a resolution row") before 014 ("not scorable"). `test_a_score_needs_a_resolution`
now asserts both layers, the second with 015's trigger dropped on its own ledger.

### Decision — score `final_prediction`; for binary and multiple choice it is the posted payload

The brief asked whether to score the model's `final_prediction` or the payload actually posted.
For these two types they are the same numbers by construction (the builder copies), bound to the
post by D34 (the approval digest is derived from the record, and the key seam refuses any other
request digest), and equal on all 13 live records. So there is one subject, and a second
"payload" row would duplicate every value. The writer reads the prediction back through
`read_forecast_record`, which re-verifies `forecast_sha256`. **Tripwire:** the existing
`test_a_binary_record_derives_the_wire_body_and_nothing_else` and
`test_a_multiple_choice_record_derives_one_entry_per_option` assert the payload *equals*
`final_prediction`; mutants T1/T2 (scale the payload by 0.999) are killed by them. If a transform
ever enters the builder, those go red and this decision has to be revisited.

### Decision — natural log, single-term binary Brier, and a `local_` vocabulary

`local_brier_binary` is `(p - o)^2` in [0, 1], not the two-category sum (twice that).
`local_log_binary` is `ln(p)` on yes and `ln(1.0 - p)` on no. Multiclass Brier is
`sum_i (f_i - [i == k])^2` in [0, 2]; multiclass log is `ln(f_k)`. Natural log because it is the
platform's base. Nothing is scaled, shifted or compared against a baseline, so nothing resembles
a baseline or peer score, and every metric name says `local_` (D30 by analogy). `015` pins the
vocabulary and requires `comparison_baseline IS NULL`.

### Decision — refuse a zero on the realized outcome; never clamp

The logarithm of every positive double is finite (the smallest subnormal gives -744.44, pinned
by a `bc -l` literal), so the only unsafe input is exactly 0 on what happened, and it is a
`ScoreError`: nothing is written, not even the Brier row (property 7 asserts that across about
ten real-ledger examples per run). A clamp would make the function total by reporting a number nobody
forecast. The refusal is unreachable for a posted forecast (bounds ≥ 0.001 on both sides), so
it guards a malformed stored value. `015` also refuses a non-finite value (IEEE infinity is
storable in REAL; a Python NaN binds as NULL).

### Decision — a score cites the observation it measured, and the database enforces idempotency

`015` adds `resolution_event_id`, and its trigger requires it to be the record's **latest**
resolution row (014 still decides that row is scorable). The UNIQUE index on
`(forecast_record_id, resolution_event_id, metric, implementation_version)` is the idempotency
rule: scoring the same observation again writes nothing, while a new observation or a new
implementation version is a new row. The cost is operational: merging a migration stops the live worker at its next poll until
`init-ledger` runs, so 015 was claimed in `TRACKS.md` before it was written.

### Decision — the writer's only input about what is scored is the record id

`record_local_scores(conn, *, record_id, computed_at)` reads the forecast and the latest
resolution itself; no caller hands it a probability, an outcome or a score (M2-707/M4-801: a
caller-supplied value next to the evidence is a second source of truth). The forecast stack is
imported inside the function, the `approval.approve` precedent, because `forecast.store` imports
`lifecycle`.

### Decision — a re-resolution after `scored` appends rows and no lifecycle event

003 has no `scored -> scored` transition. So the first scoring of a `resolved` record appends the
rows **and** the `scored` event (linking the first row, the Brier), in one transaction; a later
scoring against a new observation or a new version appends rows only. A retraction after scoring
leaves the old rows and makes the record `not_scorable` until it resolves again. The current
score of a record is the rows citing its latest resolution under the current versions.

### Decision — the reader recomputes every row, exactly

`read_local_scores` re-verifies each cited resolution (both digests), recomputes the value with
the implementation named by the row's `implementation_version` (`scoring.recompute`), and refuses
any difference, an unregistered version, or a metric/version mismatch. Old versions stay
registered in `scoring._IMPLEMENTATIONS`; each registered version has a golden table in
`test_scoring.py` keyed by its version string.

### Deviation — `score --config PATH`, not `score [--record-id ID]`

`CODEX_HANDOFF.md` lists `score [--record-id ID]`. The ledger path lives in the config, and every
other ledger command takes `--config`.

### Deviation — the stricter reading of the criterion

"Known examples match hand calculations" is read as: every expected value is written out by hand
(Brier as exact decimal arithmetic in a comment; logs as `bc -l` literals, so the oracle does not
share Python's libm), never produced by the implementation. "Extreme probabilities are handled
safely" is read as: 0, 1, 0.001, 0.999, the smallest subnormal and `1 - 2**-53` are each pinned;
nothing non-finite can be computed or stored; and the one undefined case is a refusal rather
than an adjusted number.

### Deviation — the multiple-choice sum tolerance is a third independent constant

The plan said to promote `forecast/multiple_choice.py`'s `_SUM_TOLERANCE` to `bounds.py`. That
module's comment records that it and `submission_live._CATEGORY_SUM_TOLERANCE` are deliberately
independent constants (one may not import the other's stack), so `scoring.py` follows the same
rule with its own `1e-6` rather than refactoring two approved modules.

### Deviation — existing seeds write score rows through a 015-valid helper

Eight raw `INSERT INTO score_events` sites (export, lifecycle, resolution ledger and two property
suites) wrote `metric='brier', implementation_version='v1'` with no resolution link; 015 refuses
all of them. They now use `resolution_rows.insert_score_row`. `test_export.py`'s `_corrupt`
drops 015's trigger on its own ledger before planting an off-contract value, because every shape
it plants is now also refused at INSERT, and the export's refusal is still the thing under test.

### Rejected — two score subjects (forecast and payload)

Identical by construction for these types; see the first decision and its tripwire.

### Rejected — recomputing the score inside the trigger

A second implementation of the number, and SQLite's `ln` depends on build flags. The reader
recomputes instead.

### Rejected — an upper bound on multiclass Brier in the trigger

The mathematical bound is 2 + O(tolerance), and a copy of the Python tolerance in SQL would be a
second, divergent spelling of it. The trigger enforces the bounds that follow from the definitions
alone (Brier ≥ 0, binary Brier ≤ 1, log ≤ 0); the property test enforces `[0, 2 + 2e-6]`.

### Rejected — an input-digest column on the score row

The record (`forecast_sha256`) and the resolution row (both digests) are re-verified on every
read, and the reader recomputes from them, so a digest of the inputs would attest nothing new.

### Rejected — renormalizing a distribution that does not sum to 1

That scores a forecast nobody made. Outside `1e-6` is a `ScoreError`.

### Rejected — replicating Metaculus's `pmf[-1]` fallback for an unpriced option

It is a platform rule about options added after forecasting. Replicating it would make a local
score claim a platform behaviour nobody has verified here (D30's spirit).

### Deferred (do not read the absence as an omission)

- **An option added after the forecast.** A multiple-choice outcome the forecast did not price is
  refused (`failed` in `score`). Batch 1 has no MiniBench multiple-choice question. Backlog: none
  until one is observed; the refusal names the rule.
- **A `scored -> scored` lifecycle event** for a re-scoring. Same shape as M4-801's
  `resolved -> resolved` deferral; the score rows are the record. Backlog: **M4-804**.
- **Scheduling `score`.** Operator-run, like `ingest-resolutions`; both belong to the same timer.
  Backlog: **M4-805**.
- **Platform scores for numeric and discrete.** `score` reports them `out_of_scope`. Backlog:
  **M4-803**.
- **Summaries across records** (calibration, means). Backlog: **M5-804**.

### Standing risk — not verifiable offline

- **No real resolved payload has been seen**, and M4-801's unmasking risk is still open: the first
  `ingest-resolutions` after ~2026-09-17 answers both. If a posted question comes back `withheld`,
  nothing is scorable and the fault is access, not this code.
- **The platform's stored value is not re-checked at score time.** Scoring trusts the D34 chain
  and the confirmed refetch recorded at post time. A platform that later rewrote a stored
  forecast would not be noticed here.
- **The two scores are not directly comparable to anything the platform shows.** A reader who
  wants Metaculus's numbers needs M4-803.

### Mutation testing

Committed before mutating (lessons 5 and 8); `__pycache__` cleared before every mutant; each
restored with `git checkout` and the tree asserted clean at the end. Runner: the four new test
files, `-x`, full `dev` hypothesis profile. The baseline (169 passed) was confirmed green
**by exit code** before the final passes: one intermediate pass piped pytest through `tail`,
committed a new test that was failing on the unmutated tree, and reported two "kills" that were
really that failure. Both were re-run against a green baseline.

**015 — every clause neutered whole, `WHERE 0 AND (<predicate>)`: 9 of 9 killed**, plus the
UNIQUE index made plain (killed) and the upgrade precondition `LIMIT 1` → `LIMIT 0` (killed).

| mutant | killed by |
|---|---|
| P1 realized option found by sorted position | hand-calculated multiclass table (`Republicans`) |
| P2 clamp at 1e-15 instead of refusing | `test_the_smallest_positive_probability_gives_a_finite_log_score` |
| P3 binary log ignores the outcome | binary log table (`0.7`, `no`) |
| P4 sum check dropped | refusal table, `sum to 1` |
| P5 multiclass Brier `sum` instead of `fsum` | **property 3 only** (option-order invariance) |
| P6 binary Brier two-category | binary Brier table |
| P7 log base 2 | binary log table (`0.5`) |
| P8 int/bool accepted as a probability | refusal table (`True`) |
| P9 `isfinite` dropped | **survived -- equivalent**: `0.0 <= value <= 1.0` already rejects NaN and ±inf |
| P10 `recompute` metric check dropped | `test_recompute_refuses_an_unregistered_version_and_a_mismatched_metric` |
| P11 duplicate label accepted | refusal table, `more than once` |
| W1 writer idempotency skip dropped | `test_scoring_again_writes_nothing`; **and** property 7 alone |
| W2 `scored` event on a re-scoring too | `test_a_re_resolution_after_scoring_appends_new_rows_and_no_lifecycle_event` |
| W3 no `scored` event | `test_a_resolved_binary_record_is_scored_and_moves_to_scored_atomically` |
| W4 reader skips the recompute comparison | `test_the_reader_refuses_a_stored_value_that_no_longer_recomputes` |
| W5 non-scorable latest resolution scored anyway | `..._is_not_scorable[annulled]` |
| W6 status check dropped | `test_a_scorable_row_on_a_record_that_never_moved_is_refused` |
| W7 `computed_at` ordering check dropped | `test_a_score_computed_before_its_observation_is_refused` |
| W8 event links the last row, not the first | the atomic-scoring test |
| W9 reader reads non-local metric rows | **survived**, then `test_the_reader_leaves_rows_of_other_metrics_alone` |
| W10 reader skips "cited row belongs to this record" | **survived**, then `test_the_reader_refuses_a_score_citing_another_records_resolution` |
| O1 `out_of_scope` filter dropped | `test_every_record_with_a_resolution_gets_exactly_one_verdict` |
| O2 unknown `--record-id` not refused | `test_a_malformed_or_unknown_record_id_is_refused_without_echo` |
| O3 `score` exits 0 on a failure | `test_the_command_exits_refused_when_any_record_failed` |
| T1/T2 payload builder scales binary / MC by 0.999 | the two existing `test_submission_payload.py` derive tests (the tripwire) |

W9 and W10 were real gaps of the M4-801 W4 kind: both branches are unreachable by ordinary SQL
once 015 is applied, so nothing exercised them; each test drops 015's trigger on its own ledger.

**The property file alone** kills P1, P2, P3, P5, P6 and W1. It does not kill P4, P7, P8, P11,
W4 or W10, and is not meant to: its strategies draw valid distributions (P4, P11), it asserts
the sign of a log score rather than its base (P7), totality is not refusal (P8), and it does not
tamper (W4, W10). Each of those dies on a unit test above.

**Strategy reach**, `hypothesis.event`, `dev` profile (200 examples): every branch reached. The
zero-on-outcome refusal: 2% of binary draws, 35% of multiclass draws, ~5% of ledger replay draws
(about ten real-ledger examples per run). Ledger replay: 62% multiple choice scored, 22% binary
scored. Option-order property: 54% of draws actually changed the order. Two strategies first used
`.filter(sum > 0)` and discarded up to 31% of draws; they now `map` a zero vector to a non-zero
one and discard nothing on that account.

**One property was wrong as first written, and the property found it**: a binary Brier is not
*exactly* half the two-option Brier, because `0.999` is not exactly `1 - 0.001`; at `p = 0.001`,
`no` they differ by ~1e-21. The assertion is now `isclose(rel_tol=1e-12, abs_tol=1e-18)`, and the
docstring says why.

### Review

**Round 1 — APPROVE on `d79a7c6`** (2026-09-15, local Codex against
`GPT_REVIEW_REQUEST_M4-802_r1.md`, all four gates green in the request). No blocking findings
and no backlog candidates; each of the nine falsifiable risk claims was marked safe. The
reviewer ran the focused scoring suite (169 tests) and `ruff check`. This entry is the only
change after the approved commit.

## M4-805 — Schedule resolution ingestion

Acceptance: *Resolutions for submitted records are ingested without an operator command at a
documented cadence; a failed ingest surfaces through the existing alerting path; the schedule
makes no paid call and never reaches the submission path.* Description (amended by M4-802):
`score` belongs on the same schedule, after ingestion.

### Delivered

- `deploy/systemd/whiskeyjack-resolutions.service` — `Type=oneshot`,
  `OnFailure=whiskeyjack-notify@%N.service`, the poll's `WorkingDirectory`/`EnvironmentFile`/
  `Environment` and interpreter, and two `ExecStart=` lines: `ingest-resolutions --config
  …/config/tournament.yaml`, then `score --config …/config/tournament.yaml`. `TimeoutStartSec=2400`.
- `deploy/systemd/whiskeyjack-resolutions.timer` — `OnCalendar=*-*-* 00/6:23:00`,
  `Persistent=true`, `AccuracySec=1min`.
- `tests/unit/test_deploy_resolutions_unit.py` — the unit read as a contract (parity with
  `whiskeyjack-tournament.service`, the ExecStart contract, the timer) and run as a measurement
  (the unit's own argv through `cli.main`, with every paid and posting entry point refused).
- `docs/RUNBOOK.md` — § Scheduled ingestion and scoring (cadence, what a push means, install
  commands), a symptom-index row, and a pointer from step 6.
- `docs/backlog/backlog.csv` — **M4-806**, **M4-807**, **M1-341** (the deferrals below);
  `docs/backlog/decisions.csv` — **D37**.

**No `src/` change, no `AppConfig` field, no migration, no dependency.** `config_sha256` and the
live activation cannot be affected by this branch.

### What was established by execution before designing

**Nothing this account posted has resolved.** The live MiniBench ledger, opened `mode=ro` on
2026-09-15 at ~04:40 UTC: schema 15, 23 records whose history reached `submitted`,
`resolution_events` 0, `score_events` 0. The Cup ledger (`data/cup/ledger.sqlite3`): schema **13**,
5 submitted records, last heartbeat 2026-09-10.

**systemd's multi-`ExecStart` semantics, on this host's systemd 255.** A throwaway
`wj-m4805-probe.service` in `$XDG_RUNTIME_DIR/systemd/user/`, whose `OnFailure=` pointed at a
scratch logger rather than the ntfy pager, then removed:

| lines | second line ran? | `Result` | `ExecMainStatus` | `OnFailure` fired, and saw |
| --- | --- | --- | --- | --- |
| `exit 4`, then `touch marker` | **no** (no marker) | `exit-code` | 4 | yes, `exit=4` |
| `exit 0`, then `touch marker` | yes | `success` | 0 | no |
| `true`, then `exit 4` | — | `exit-code` | 4 | yes, `exit=4` |

So the notify template's `exit=` field is the failing command's own exit code, whichever line
failed, and a failed first line keeps the second from running.

**Which path fires for a non-zero exit.** The pager is systemd's `OnFailure=` →
`whiskeyjack-notify@.service` (shell + curl, 30-minute throttle per unit). In-process `notify.py`
is not involved: neither command builds a notifier. The watchdog (`~/.local/bin/wj-watchdog`,
operator-local) checks only `whiskeyjack-tournament`.

**Exit codes the pager catches** (from `cli.py`, pinned by the tests below): a missing
`METACULUS_TOKEN` is `3`, before any request; a missing or unopenable ledger is `4`, with nothing
created or rewritten; any per-record failure is `4`, after recording the rest; an unhandled
exception is Python's `1`. All are non-zero.

**Overlap with the poll.** `ingest_resolutions` does every GET outside a transaction; each record's
write is its own `BEGIN IMMEDIATE`. Against a second connection holding the write lock, a record's
write waited **5.0 s** (`ledger._BUSY_TIMEOUT_MS`) and came back
`failed: the ledger could not complete this transaction (detail withheld: ...)`; after the lock was
released the next run appended it. So an overlap costs at worst one page and one cycle's delay for
that record, never a wrong row. An idle poll takes ~16 s wall (journal, 2026-09-15 03:40–03:55 UTC).

**Live run duration.** The unit's two commands, run by hand from the main checkout (`c22994c`)
with the unit's environment, started 2026-09-15 04:55:26 UTC between polls: `ingest-resolutions`
**108.7 s** wall (7.4 s CPU), 23 records, all `nothing_to_retract`, none `withheld`, `failed: 0`,
exit 0; then `score` **0.28 s**, `records: 0  failed: 0`, exit 0. About 4.7 s per post, so
`TimeoutStartSec=2400` is ~22x today's run and leaves room for the post count to grow and for the
SDK's read retry on a bad network.

### Decision — a separate timer, not a phase of `tournament run-once`, and why

See Rejected. The deciding facts are in `tournament.run_once` and `cli._run_tournament`: the poll
refuses unless the activation is live and live submission is enabled, so it stops exactly when a
tournament closes or is withdrawn — which is when resolutions arrive.

### Decision — two `ExecStart` lines, so `score` runs only after ingestion exited 0

Owner decision, 2026-09-14. systemd gives the ordering and the refusal (table above), so no new
Python and no shell wrapper stand between the unit and the two already-reviewed commands. "Scoring
never runs against a ledger ingestion just failed to open" follows from three pinned facts: no line
carries a `-` prefix (test), ingestion exits non-zero whenever it cannot open the ledger (test), and
systemd does not run a later line after a non-zero one (probe). The stricter consequence is
accepted: **any** ingestion failure, including one bad record, skips scoring for that cycle. The
operator is paged each time and `score` is safe to run by hand.

### Decision — every six hours, at minute 23

Owner decision, 2026-09-14, on these numbers: a question resolves once and nothing consumes a score
within the day; every run is one GET per posted post at the configured spacing (3.5 s + up to 1 s
jitter), and that count only grows because resolved posts stay polled to see a retraction — 92
GETs/day at 23 posts against 552 hourly; and the notify throttle is 30 minutes, so a persistent
failure pages once per run (4/day, against 24 hourly). Minute 23 is clear of the poll (`*:0/5`) and
the watchdog (`*:2/5`). Times are host local time (MDT here), which the runbook says.

### Decision — MiniBench profile only

Owner decision, 2026-09-14. The Cup ledger is at schema 13 and its questions resolve months out.
Deferred to M4-806.

### Deviation — the stricter reading of the criterion

- **"A failed ingest"** is read as any non-zero exit of *either* command, or a run past
  `TimeoutStartSec`, and as including a partial failure (one record). All of them fail the unit.
- **"The existing alerting path"** is read as the same `OnFailure=` template the poll uses, proven
  by comparison with `whiskeyjack-tournament.service` rather than by a constant, plus the absence of
  every unit setting that would convert a failure into success (`SuccessExitStatus`, `Restart`).
- **"No paid call, never the submission path"** is read as a measurement of the unit's own command
  lines, not a reading of imports: the argv is parsed out of the unit file and driven through
  `cli.main` with refusal spies on the `requests` write verbs and `Session.request`,
  `build_poster`, `post_approved_forecast`, `run_live`, `build_asknews_client`, `build_exa_client`,
  `build_forecaster_client`, `PricedClient`, `litellm.completion`/`acompletion`,
  `build_notify_client`, `httpx.Client.send` and `socket.getaddrinfo`. It also covers litellm's
  import-time cost-map fetch: `LITELLM_LOCAL_MODEL_COST_MAP=True` is pinned in the unit.
- **Scoring is on the schedule too**, and the end-to-end test asserts it ran (records end `scored`,
  with both values hand-checked: Brier `(0.7 - 1)^2 = 0.09`, log `ln 0.7` by `bc -l`).

### Rejected — a phase of `tournament run-once`

- `run_once` raises before any work unless live submission is enabled and the activation is live
  and in window (`require_live_submission_enabled`, `require_activation`). A disabled, retired or
  ended tournament — the state in which resolutions arrive, and the Cup's state today — would stop
  ingestion.
- It runs under `worker_lock` with a `SingleAttemptPoster` built from `build_poster`, so "never
  reaches the submission path" could not be shown by what the process constructs.
- `_run_tournament` returns 1 on any refusal, unresolved attempt or heartbeat failure. An ingestion
  failure folded in would fail a healthy forecasting poll, trip the watchdog's "last service run
  FAILED", and share one throttle stamp with forecasting failures, so one would mute the other.
- At the poll's five-minute cadence it would be 6,624 GETs/day at 23 posts without a new throttle,
  and a new throttle means new state.

### Rejected — a combined `resolutions-cycle` subcommand

It could score past one bad record while still refusing after an open failure, but it is new CLI
surface and review scope for a case the pager already reports. Owner chose the systemd form.

### Rejected — a shell wrapper (`ingest && score`)

It is equivalent to two `ExecStart` lines with one more interpreter in the way, and its exit code
would have to be preserved by hand for the notify body.

### Rejected — taking the tournament's `worker_lock`

The poll takes it non-blocking and refuses with exit 1 when it is held, so a two-minute ingestion
holding it would fail the polls that start meanwhile and page. SQLite's write lock plus the busy
timeout already serialize the writes that matter (measured above).

### Rejected — an `AppConfig` field for the cadence

A cadence is a timer property, and any field changes `config_sha256` and retires the live
activation.

### Deferred (do not read the absence as an omission)

- **The Cup profile's schedule** — needs a backup and `init-ledger` 13 → 15 on the Cup ledger.
  Backlog: **M4-806**.
- **A page on `withheld`** — exits 0 by M4-801's contract, so the schedule does not page on it
  (owner decision). The runbook says to read the `kind` column. Backlog: **M4-807**.
- **Watchdog coverage of the resolutions timer** — a disabled or stopped timer never runs, so it
  never fails and never pages. The runbook says how to check. Backlog: **M1-341**.
- **Installing the unit on the live host** is an operator action after merge, with owner approval
  (runbook § Scheduled ingestion and scoring gives the commands). Not part of the diff.

### Standing risk — not verifiable offline

- **No real resolved payload yet**, and M4-801's unmasking risk is still open; the first scheduled
  run after ~2026-09-17 answers both. A `withheld` there will not page (M4-807).
- **Metaculus's rate limit is undocumented here.** A 429 is retried by the SDK's read retry; one
  that persists becomes a per-post `failed` and a page, not a wrong row.
- **Lock contention with a long forecasting write** could produce a spurious page. Bounded, as
  measured above, and self-healing on the next run.
- **The unit files hard-code `/home/cleblanc/projects/whiskeyjack-bot`**, as the existing units do;
  the tests pin parity with the tournament unit, not that the path exists on another machine.
- **`systemd-analyze --user verify`** accepts both files on systemd 255 here; CI does not run it.

### Mutation testing

Committed first (`e230cf8`); `__pycache__` cleared before every mutant; each restored with
`git checkout` and the tree asserted clean at the end. Runner: the new test file, `-x`, baseline
green **by exit code** (output redirected to a file). Every kill was checked against its log for the
assertion that failed, so no kill is a collection error or an unrelated failure. Planted calls
(C1–C8) are wrapped in `try: … except Exception: pass`, so only the spy's record can catch them.

| mutant | killed by |
| --- | --- |
| U1 `-` prefix on the ingestion line | interpreter parity first; the prefix assertion alone also kills it |
| U2 lines swapped | `test_exactly_ingestion_then_scoring_and_nothing_else_runs` |
| U3 `OnFailure` dropped | `test_a_failure_starts_the_same_pager_the_tournament_poll_uses` |
| U4 `LITELLM_LOCAL_MODEL_COST_MAP` dropped | `test_the_environment_matches_the_tournament_poll` |
| U5 `score` → `scores` | the ExecStart contract (argparse refuses) |
| U6 `score --config` → the Cup profile | the ExecStart contract (config parity) |
| U7 hourly `OnCalendar` | the timer test |
| U8 a third `ExecStart` running `tournament run-once` | the ExecStart contract (`3 == 2`) |
| U9 `SuccessExitStatus=4` | the pager test |
| U10 an `ExecStartPost=` | the ExecStart contract (`Exec*` keys) |
| U11 `Persistent=false` | the timer test |
| U12 ingestion narrowed with `--question-id 1` | the ExecStart contract |
| U13 timer `Unit=` the tournament service | the timer test |
| C1 ingestion builds a poster | the measured run (`['build_poster']`) |
| C2 ingestion `requests.post` | the measured run (`['requests.post']`) |
| C3 scoring builds the forecaster client | the measured run |
| C4 scoring calls `litellm.completion` | the measured run |
| C5 scoring builds an AskNews client | the measured run |
| C6 scoring builds an Exa client | the measured run |
| C7 ingestion calls `post_approved_forecast` | the measured run |
| C8 ingestion sends through `httpx` | the measured run (`['httpx.Client.send']`) |
| C9 ingestion exits 0 on an unopenable ledger | `test_scoring_does_not_run_after_an_ingestion_that_could_not_open_the_ledger` (score then ran) |
| C10 ingestion exits 0 when a record failed | `test_a_failed_record_fails_the_unit_and_scoring_waits` |
| C11 ingestion exits 0 on a missing token | `test_a_missing_token_fails_the_unit_before_any_request` |

24 of 24 killed.
