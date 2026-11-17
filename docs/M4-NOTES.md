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
  `build_notify_client`, `httpx.Client.send` and `socket.getaddrinfo`. Those spies cannot see an
  import that already happened, so litellm's import-time cost-map fetch (`litellm/__init__.py`
  calls `get_model_cost_map` at import, which skips the fetch only when
  `LITELLM_LOCAL_MODEL_COST_MAP` is `true`) is covered by the unit's environment instead, which the
  parity test pins (mutant U4). That fetch is unpaid in any case.
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

### Review

**Round 1 — APPROVE on `1dd7c72`** (2026-09-15, local Codex against
`GPT_REVIEW_REQUEST_M4-805_r1.md`, all four gates green in the request). No blocking findings
and no backlog candidates beyond the recorded M4-806, M4-807 and M1-341; each of the eight
falsifiable risk claims was marked safe. This entry is the only change after the approved commit.

## M4-803 (+M4-807) — Platform scores, and the withheld alert

Wave 23 close-out, PR-2. One branch (`feat/m4-803-platform-scores`), one review, one deploy (D39's
bundling). Migration **`017`**.

- **M4-803 — Ingest numeric platform scores.** Acceptance: *Numeric record identifies score source
  and does not label a proxy as Metaculus score.* Governing decision **D30**; owner decisions
  recorded as **D42**.
- **M4-807 — Alert when a posted record's resolution comes back withheld.** Acceptance: *A
  withheld observation of a record this account posted reaches the operator through the existing
  ntfy channel at most once per record per throttle window; ingest-resolutions' exit-code
  contract is unchanged; the alert carries no payload value.*

### Delivered

- `src/whiskeyjack_bot/platform_scores.py` — pure: `PlatformMetric`, `PLATFORM_METRIC_ORDER`,
  `SCORE_DATA_KEYS`, `COMPARISON_BASELINES`, `IMPLEMENTATION_VERSIONS`, `PlatformScore`,
  `PlatformScoreError`, `extract_platform_scores`, `recompute`.
- `src/whiskeyjack_bot/migrations/017_platform_score_events.sql` — DROP 015's
  `score_events_validate_local_score_on_insert`; CREATE `score_events_validate_on_insert` (015's
  clauses, plus the platform branch). `LEDGER_SCHEMA_VERSION` → 17.
- `src/whiskeyjack_bot/lifecycle.py` — `record_platform_scores`, `read_platform_scores`,
  `StoredPlatformScore`, `PlatformScoreWrite`; `StoredResolution.source_response` (the verified
  text, `repr=False`).
- `src/whiskeyjack_bot/resolution.py` — `select_question` public.
- `src/whiskeyjack_bot/score_records.py`, `cli.py` (`score`) — both writers per record, the
  platform column, `ScoreResult.failed`.
- `src/whiskeyjack_bot/show.py`, `cli.py` (`show`) — platform scores in the record's history, with
  their baseline and source.
- `src/whiskeyjack_bot/notify.py` — `resolution_withheld` (86400 s, `default`).
- `src/whiskeyjack_bot/resolution_ingest.py`, `cli.py` (`ingest-resolutions`) — `WithheldRecord`,
  `withheld_records`, `notify_withheld`; `withheld: N` on the last line.
- Tests: `tests/unit/test_platform_score_ledger.py`, `tests/property/test_platform_scores_properties.py`,
  M4-807's block in `tests/unit/test_cli_ingest_resolutions.py`; `tests/resolution_rows.py`'s
  `post_payload` now carries a live-shaped `score_data` (`SCORE_DATA` when resolved to a value,
  `{}` otherwise).
- `docs/RUNBOOK.md` steps 6–7 and the schedule's withheld bullet; `docs/backlog/decisions.csv`
  **D42**; M4-803 and M4-807 `Done`.

### What was established by execution before designing

The brief made the design conditional on a read-only probe (CLAUDE.md: spec versus observed).
It was run 2026-09-23 against post 45556 (CME live cattle, numeric, resolved), with the
watchdog's request shape: an explicit User-Agent, the trailing slash, no redirect followed with
the token. Field names only:

- `question.my_forecasts` holds `history`, `latest` and **`score_data`**, whose keys are
  `baseline_score`, `peer_score`, `spot_baseline_score`, `spot_peer_score`,
  `relative_legacy_score`, `coverage` and `weighted_coverage`. The question also carries
  `default_score_type` (`spot_peer` on every MiniBench question). `aggregations.*.score_data` is
  `null`: the community's scores are not returned, and none are wanted (the community prediction
  is never an input; nothing here reads it).
- **Every one of the 20 scorable live `resolution_events` rows already stores `score_data`** in
  its `source_response`: 11 binary, 3 discrete, 6 numeric. All seven values are JSON floats on
  every row, 140 of 140. The one annulled row carries `{}`, and so does the committed real
  withheld payload (`withheld_minibench_45321.json`).
- Post 45556's stored `score_data`, observed 2026-09-19 12:23 UTC, is **bit-identical** to the
  live refetch on 2026-09-23.
- SQLite's JSON number parse against Python's (the question 017's value clause depends on): on
  the SQLite the deployed venv ships (**3.53.1**), exact for 699,996 doubles (random bit
  patterns, the realistic score range, and wide magnitudes) plus all 140 live values. On the
  system SQLite (**3.45.1**) it is **not** exact: 66 of 599,849 random-bit doubles, and 8 of
  500,000 in the realistic range [-1000, 200].
- Scored against a backup copy of the live ledger at 017: 19 records gain 4 rows each (76 rows);
  every row is admitted by 017's exact-value clause and re-read exactly; the 8 numeric/discrete
  records move to `scored`; the annulled record stays `not_scorable`; a second run is all
  `unchanged`. **19 and 8, not the brief's 20 and 9:** post 45561's record resolved and was then
  annulled, so its latest observation is not scorable.

### M4-803

#### Decision — copy the platform's scores out of the observation already stored, and why

D30 says ingest the platform's numbers before any local replica; the probe shows they are
already in the ledger. `platform_scores.extract_platform_scores` reads them from the latest
scorable resolution row's `source_response` — re-verified against `source_response_sha256` in
the same transaction — for the record's own question, selected by `resolution.select_question`
(made public for this, so the classifier and the extractor cannot pick different questions). No
fetch, no computation, no change to `ingest-resolutions`. Each row cites the resolution row it
was read from, so it replays from the ledger alone.

#### Decision — the four score types, all question types (owner), and why

`platform_spot_peer_score`, `platform_spot_baseline_score`, `platform_peer_score`,
`platform_baseline_score`: the platform's four score types for a forecaster. Written in that
order, so when the platform writer moves a record it links `spot_peer`, the tournament's own
`default_score_type`. **All four question types** (owner decision 2026-09-23): the acceptance
criterion names numeric, but M5-804 groups by type and metric, and only the platform's score is
comparable across types. Binary and multiple choice keep their `local_*` rows alongside.

#### Decision — the source is three columns, and `comparison_baseline` gets its 001 meaning back

"Identifies score source": the `platform_` prefix, `comparison_baseline` (`peer` or `baseline`,
fixed per metric — the column 001 created for exactly this label, and which D36 forbids on a
local score), and `implementation_version` `<metric>/metaculus_score_data/1`, which names the
*extraction path*, since there is no formula to version. Plus `resolution_event_id`, the
evidence.

#### Decision — 017 checks the value itself, where 015 declined to

015 would not recompute a local score in SQL: `ln` depends on build flags, and a second
implementation of a formula is a second source of truth. A platform score has no formula; the
check is a lookup of the stored text (top-level question, or the one group member with the
record's question id — `select_question`'s rule, including refusing a doubled member), so it is
the same source read twice. It makes "a `platform_*` row is the platform's number" a property of
the schema, not of the writer: nothing can write a proxy under a `platform_` name. A JSON
integer is refused too (SQLite's `=` would admit 3 against 3.0, and the Python reader refuses
it; the schema must not be laxer than the reader).

#### Decision — a resolved observation without readable scores fails (owner), and why

Missing, `null` or `{}` `score_data`, a missing key, a non-float or a non-finite score: a
`LifecycleError`, so the record reports `failed` and `score` exits non-zero (the unit's
OnFailure page). Nothing re-fetches an observation whose resolution has not changed, so a quiet
status would be a permanent, silent attribution gap — the "malformed maps to quiet" shape. 0 of
20 live observations would trip it.

#### Decision — `score` runs both writers, local first

Each in its own transaction. Local first, so binary and multiple choice keep the `scored` event
on the local row they always had; for numeric and discrete the platform writer takes it. When the
local writer **fails** for a binary record (an unreadable stored forecast), the platform writer
still records the platform's scores — they need only the observation — and takes the event;
`test_every_record_with_a_resolution_gets_exactly_one_verdict` pins that. `ScoreResult` gains
`platform_status`/`platform_rows_appended` and a `failed` property over both.

#### Deviation

- **None of the three live-run hazards:** no byte change to `config/tournament.yaml`, no new
  `AppConfig` field, no change to `prompts/forecaster*.md`. The live activation is untouched.
- **A migration, 017** — so the deploy takes the worker down until `init-ledger` runs (CONTEXT
  § 3.3), which the brief anticipated.
- **`resolution._select_question` became public** (`select_question`), with no behaviour change.
- **`score`'s output line gained a platform column**, and a `failed` line now prints both
  statuses before the detail. The exit rule is unchanged: non-zero when any record failed.
- **The brief's expected counts were 20 and 9; the measured ones are 19 and 8** (see above).
- **"Bit for bit" through the ledger is IEEE equality, not bit identity, for one value:**
  SQLite's REAL storage turns `-0.0` into `0.0`. The property pass found it. Every comparison in
  the program — the trigger's `=`, the reader's `!=` — treats the two as equal, and the JSON
  round trip in `source_response` does preserve the sign. Pinned as an explicit `@example`.

#### Rejected — a local numeric or discrete score, and why not

D30. Nothing in this branch computes a continuous score; 017 still refuses every `local_*`
metric on a numeric or discrete record.

#### Rejected — `relative_legacy_score`, `coverage`, `weighted_coverage`

`relative_legacy_score` is the platform's pre-2023 score. `coverage` and `weighted_coverage`
qualify the time-weighted scores rather than being scores, and a score row with a coverage value
in it would be a category error. All three stay in the stored `source_response`, replayable.

#### Rejected — re-fetching scores during ingestion

It would be a second network read per post for numbers the first read already stored, and a
second observation of the same resolution the ledger has no row for.

#### Rejected — a quiet `no_platform_scores` status

Owner decision; see above.

#### Deferred (do not read the absence as an omission)

- **A re-fetch path for an observation whose scores arrive late or change.** `ingest-resolutions`
  appends a row only when the *observation* (kind, outcome) changes, and `observation_sha256`
  does not cover `score_data` — deliberately: widening the snapshot would change every existing
  row's digest. M4-804 (deferred by D40) is the re-resolution path and the place for it.
- **The Cup profile.** Its ledger is still at schema 13 and it is not scheduled.

#### Standing risk — not verifiable offline

- **The platform recomputes scores after the observation.** Measured stable over four days on one
  post; nothing re-reads an unchanged observation, so a later platform correction would not reach
  the ledger.
- **SQLite's JSON number parse.** 017's value clause is exact on the deployed SQLite (3.53.1) and
  on CI's (uv's Python, the same build); on an older SQLite (3.45.1) about 1 value in 60,000 in the
  realistic range parses a ulp off, and the clause then **refuses** a genuine score — a loud
  `failed`, never a wrong value admitted. The ledger property
  (`test_the_ledger_stores_and_rereads_every_finite_double_exactly`) carries two doubles 3.45.1
  misparses as `@example`s, so running the suite on such a build says so.

### M4-807

#### Decision — a condition read after the run, not the transition, and why

`IngestResult` carries no kind for an `unchanged` observation, so an alert keyed to the append
would be sent once, ever, and lost if that one push failed. `withheld_records` reads, after the
run, which of the run's records **currently** stand on a `withheld` latest observation — through
`latest_resolution`, which re-verifies both digests — and `notify_withheld` sends one
`resolution_withheld` per record. The event's window is a day (the `unrecorded_post` rationale: a
condition, reminded daily, never 4-a-day); priority `default` (an attribution gap nobody here can
close, not an incident).

#### Decision — the exit code cannot move

`emit` absorbs every failure, and returns `disabled` with no notifier; the notifier is built only
when there is something to send, after the ledger work, and its exit is decided by the results
alone. A digest mismatch while reading the condition is `ResolutionIngestError` — the command's
existing refusal (`EXIT_REFUSED`) — so an unreadable row never reads as "nothing withheld".
`test_the_alert_never_changes_the_exit_code` runs a rejected push, a transport error, a handler
that raises, and no notifier, each with and without a failed record.

#### Decision — what the alert carries

Title a literal; body the record id and question id, both from `forecast_records` (the ledger's
identifiers, as `unrecorded_post` carries them), and nothing from the platform's payload — no
value, no title, no status text. The test plants sentinels in the payload's title, question
title and description.

#### Deviation

None beyond the bundle's. The last line of `ingest-resolutions` gains `withheld: N`.

#### Rejected — an out-of-process check (the watchdog)

The watchdog has no ledger reader for resolutions and a worst-case budget of 238 of 240 s (M1-347);
the condition is known in-process at the only moment it can change.

#### Deferred

None.

#### Standing risk — not verifiable offline

The ntfy push itself (the channel is exercised live by the tournament worker's alerts; this
event has never fired, since 0 live rows are `withheld`).

### Quiet-branch table (M4-807)

Every malformed shape that could otherwise read as "no alert", and where it goes instead:

| shape | outcome |
| --- | --- |
| no `resolution` key | record `failed`, exit 4 (OnFailure), no row |
| `resolution` a number | record `failed`, exit 4, no row |
| an unknown status | record `failed`, exit 4, no row |
| the post names another question | record `failed`, exit 4, no row |
| the question's type disagrees with the record | record `failed`, exit 4, no row |
| an empty post | record `failed`, exit 4, no row |
| a stored withheld row whose content no longer matches its digest | `refused:`, exit 4 |

### Property tests

`tests/property/test_platform_scores_properties.py`, at the `ci` profile (200 examples):

- `extract_platform_scores` never raises outside `PlatformScoreError`, with the payload
  corrupted at one level per test — post, question, `my_forecasts`, `score_data`, one score —
  chosen by `parametrize`, not `sampled_from`, so each level is reached by construction.
  `event()` reach: every refusal message and the accept branch appear; the rarely drawn ones
  (a non-finite score, reached ~1% by the scalar strategy; `recompute`'s accept branch, 0.5%)
  have deterministic tests beside them.
- A malformed `question_id` is refused by the exact-type gate, including `45747.0` and `True`,
  which `select_question` alone would accept (the mutation pass found this; see M-P01).
- A well-formed payload is copied bit for bit and replays through
  `canonical_json` → `json.loads`; `recompute` agrees on the replayed form.
- Through a real file-backed ledger: arbitrary finite doubles are admitted by 017's exact-value
  clause and re-read equal — bit-identical except `-0.0`, which SQLite's REAL storage stores as
  `0.0` (found by this property; pinned as an `@example`). Two doubles SQLite 3.45.1 misparses
  are `@example`s.
- No refusal reprints a value: a sentinel in every string and a float canary in every payload.

### Mutation testing

Committed first (`0b777d1`); `__pycache__` cleared before every mutant; the original bytes
restored after each and the tree checked clean at the end. Runner: the eight affected test
modules, `-x`, `HYPOTHESIS_PROFILE=fast`, baseline green **by exit code**. SQL clauses are
neutered as `WHERE 0 AND (<pred>)`. The set enumerates the siblings of each entry point: every
refusal of `extract_platform_scores`, every arm of the writer and reader, the `score` wiring,
the withheld read and emission, and every clause of 017 plus the parts of its value clause.

**First run: 49 of 58 killed.** The nine survivors, and what each one was:

| mutant | why it lived | disposition |
| --- | --- | --- |
| P01 `question_id` gate removed | `select_question` refuses almost every bad id anyway; only an id *equal* to the real one but not an int (`45747.0`, `True`) needs the gate, and nothing drew one | `@example`s + exact message assertion — killed |
| L02 writer status gate removed | no test put a scorable observation on a record still `submitted` | `test_a_record_that_never_moved_to_resolved_is_refused` — killed |
| L07 second source-digest check removed | **equivalent**: `latest_resolution` had already verified the same text, in the same transaction, on an append-only row | the second read is gone; `StoredResolution` carries the verified text (`2f690d9`) |
| S02 `failed` ignores the platform status | the planned command-level test for absent scores was never written | `test_a_resolved_observation_without_platform_scores_fails_the_command` — killed |
| S04 platform error detail dropped | same | same — killed |
| C01 `score` exit ignores the platform status | same | same — killed |
| R05 throttle subject is the question | no test had two withheld records on one question | `test_two_withheld_records_on_one_question_each_page` — killed |
| Q16 group branch's `json_type = 'real'` dropped | the integer-score test used a top-level question only | `test_a_group_members_integer_score_admits_no_platform_row` — killed |
| Q19 top-level question id unchecked | the classifier refuses such a payload at ingest, so no test planted one | `test_a_stored_top_level_question_with_another_id_admits_no_platform_row` — killed |

**Second run, the survivors against `924ffcd`: 8 of 8 killed** (L07's code no longer exists).
Final: 57 killed, 1 removed as equivalent, 0 surviving.

Killed on the first run (49): P02–P14 (the extractor's other gates, the key and baseline tables,
the version string, `recompute`'s metric check); L01, L03–L06, L08–L11 (scorable gate,
idempotency, the `scored` event and its link, the written baseline, the reader's value, baseline
and recompute checks, a swallowed extractor error); S01, S03 (platform skipped for local types,
`moved` ignoring the platform); C02, C03 (withheld never notified, a withheld read failure made
quiet); R01–R04, R06 (wrong kind, no dedup, an unreadable row read as not withheld, failed
results skipped, the body without the record); N01, N02 (window, priority); Q01–Q15, Q17, Q18,
Q20 (every 017 clause neutered, platform types narrowed, the version tail, the top-level `real`
check, the group branch disabled, group uniqueness, the metric-to-key map crossed).

### Review

**Round 1 — APPROVE on `0f9fbd3`** (2026-09-23, local Codex against
`GPT_REVIEW_REQUEST_M4-803_r1.md`, all four gates green in the request). No blocking findings;
all ten falsifiable risk claims marked safe. One non-blocking observation — the runbook's
command table still said `score` appends *local* score rows — is fixed after approval, together
with the same table's `ingest-resolutions` row (which can now push to ntfy) and this entry.
Those three documentation lines are the only change after the approved commit.


## M5-804 (+D-1002) — The attribution report dataset, and the schema it rests on

Wave 23 close-out, PR-3. One branch (`feat/m5-804-attribution-report`), one review, one deploy
(D39's bundling). **No migration, no `AppConfig` field, no prompt change** — none of the three
things that retire the live activation. Read-only over the ledger; no worker path changes.

- **M5-804 — Generate attribution report dataset.** Group outcomes by domain, type, model,
  prompt and reasoning tags. Acceptance: *Export contains counts, calibration bins and score
  summaries with small-sample warnings.* Owner decisions recorded as **D43**.
- **D-1002 — Document schemas and exports.** Explain ledger lifecycle, immutable versions and
  polygraph export contract. Acceptance: *Every table/field and export version has an auditable
  definition.* Governing decisions D16, D25, D29.

### Delivered

- `src/whiskeyjack_bot/report.py` — the report. A pure layer (`calibration_bin`,
  `summarize_values`, `classify`, `build_report`, `record_row`, `render_records`) over a ledger
  layer (`read_facts`, `write_report`). Vocabularies `RecordState`, `Axis`, `EvidenceGapCode`,
  `WarningCode`, `Exclusion`; constants `REPORT_SCHEMA_VERSION`, `TEST_TOURNAMENTS`,
  `SMALL_SAMPLE_THRESHOLD`, `CALIBRATION_BIN_EDGES`.
- `src/whiskeyjack_bot/cli.py` — `whiskeyjack-bot report --config PATH [--output DIR]`,
  `_run_export`'s shape.
- `docs/SCHEMA.md` — D-1002: every table and column with the migration that introduced it, each
  table's mutability class, the version constants, the lifecycle transitions, resolution kinds,
  score metrics, and the export and report contracts field by field.
- Tests: `tests/unit/test_report.py` (hand-computed oracle over a real fixture ledger),
  `tests/unit/test_cli_report.py`, `tests/unit/test_schema_doc.py` (the partitions),
  `tests/property/test_report_properties.py`, and the fixture `tests/report_rows.py` (16 real
  records through the production writers, reaching every state).
- `docs/RUNBOOK.md` step 8 and a command-table row; `tests/unit/test_runbook.py`'s command set
  gains `report`. `docs/backlog/decisions.csv` **D43**; M5-804 and D-1002 `Done`.

### What was established by execution before designing

Read-only against the live ledger on 2026-09-24 (queries by `mode=ro`, no writes):

- 69 records: 65 in 33122/33125 (all 65 posted), 3 in `bot-testing-area`, 1 in `minibench`
  (question 91001, validated and never posted; the 2026-09-03 rehearsal). Every record is
  `forecast_version` 1. `forecast_records.status` is `draft` on all 69, as 003 intends.
- **`question_domain` is NULL on all 69.** Nothing supplies it (`build_forecast_record_draft`'s
  `question_domain` is caller-supplied and no caller passes one). Every record's
  `record_json.question.source_categories` is populated: 12 Metaculus categories, one record in
  two.
- All 12 reasoning tags appear; `base_rate` and `status_quo` on every record.
- 15 `evidence_gap` journal rows, all `named_source_absent`, all scoped to a record id and bound
  to its `forecast_sha256`. No `evidence_poor` yet.
- Model cost: 38 of the 45 Astra records have `cost_usd` NULL (BYOK before M1-348). Budget
  reservations are scoped `account:tournament`, not per record, so journal cost cannot be
  attributed to a forecast without new linkage — PR-4's territory.
- **012's `submission_reserve_whole_question`** refuses a reservation for a question whose other
  record already has an attempt or an outstanding reservation. Two *posted* versions of one
  question therefore cannot arise through the submission path.

### M5-804

#### Decision — a separate derived artifact with its own version, and why

The export is *what the ledger holds*: fifteen tables, raw, one reading imposed by nobody. The
report is *what was derived from it*, and a derived artifact that shares a version number with
a raw dump makes a change to either look like a change to both. So `report.py` is its own
module with `REPORT_SCHEMA_VERSION = 1`, and it reuses the export's pieces rather than its
contract: `ledger.connect_readonly` (whose docstring already named "the attribution report
dataset" as its next caller), one deferred `BEGIN … ROLLBACK` snapshot, `export.canonical_json`
(re-raised as `ReportError`), and `artifacts.write_new_file` (create-or-fail; manifest last).

#### Decision — every fact through the verified readers, and why

A value read back out of the ledger is untrusted, and a report is only an attribution claim
while every number in it is what the evidence says. So nothing is read raw:
`read_forecast_record` re-verifies `forecast_sha256` and the column projection;
`latest_resolution` recomputes both digests and re-validates the snapshot;
`read_local_scores` recomputes every local score and `read_platform_scores` re-reads every
platform score out of its cited observation, exactly. A row that fails is a refusal
(`ReportError`, the collaborator's own sanitized message, `show.py`'s pattern), never a skip.
The only SQL in `report.py` lists the record ids. `tournament.py` is deliberately not imported
(it reaches the paid and submission paths); the journal is read through
`tournament_state.events`.

#### Decision — one state per record, by precedence; every exclusion counted

`RecordState` partitions every record: `not_posted` → `superseded` → by the latest
observation (`awaiting_resolution`, `withheld`, `unresolved`, `annulled`, `ambiguous`,
`resolved_unscored`, `scored`). "Latest" is `latest_resolution`'s, M4-801's rule, so post
45561's record — lifecycle `resolved`, latest observation annulled — reads `annulled`, not
`scored`. Records outside the included population are never dropped: every record is a line in
`records.jsonl` with `included`, `exclusion` and `state`, and `report.json`'s `population`
counts them. Every `states` object is zero-filled, so the key set is static.

#### Decision — two domain axes: the stored tag, and the Metaculus category (owner)

The AC groups by "domain". The ledger's only domain field is NULL everywhere, and M1-201
settled that a Metaculus category is not a domain and that no mapping may be improvised. So
`question_domain` is an axis exactly as stored (today one `null` group, which the report shows
rather than hides), and `source_category` is a separate, overlapping axis keyed on the category
**id** (M1-201: a slug can be renamed and is optional). The slugs seen for an id are carried as
the group's `labels`, beside the key rather than in it, so a renamed slug cannot split a group.

#### Decision — overlapping axes are flagged and never totalled

A record carries several reasoning tags and may carry several categories and evidence-gap codes.
On those three axes the groups overlap, so each axis block carries `overlapping: true`, a
report-level warning says so, and **no field anywhere sums across an axis's groups** — not in
`report.json`, not in the CLI's output. A record with no value on an overlapping axis sits in
that axis's null group, so every included record appears on every axis.

#### Decision — a score cell is one `(metric, implementation_version)`, and why

Keying the cell on the metric makes it structurally impossible for a `local_*` and a
`platform_*` number to share one, and for two implementation versions of one metric to be
averaged together. Each cell carries its `provenance` (`local`/`platform`, from the metric
vocabularies, never from the string prefix) and `comparison_baseline`. Only `scored` records
contribute, and only their rows citing the latest observation; rows citing an older
observation (a re-resolution, a retraction) are counted as `stale_score_rows` and summarized
nowhere.

Numerics, each a property: sums are `math.fsum` (correctly rounded, so order-independent);
values are normalized with `v + 0.0` so `-0.0` reads as `0.0`, as SQLite REAL already stores it
— without it `min` over `[0.0, -0.0]` depends on row order; the mean is clamped into `[min, max]`
because `fsum` then a division can land one ulp outside (three 0.1s give
0.10000000000000002); `sample_sd` uses `n - 1` and `d * d` rather than `d ** 2` (which raises
`OverflowError`); and any statistic that is not finite refuses the report. The last is
reachable: 017 admits any finite double, and two `sys.float_info.max` scores overflow a sum.

#### Decision — calibration is binary, in fixed tenths, left-closed

Ten bins over the literal doubles `0.0, 0.1, …, 1.0`, found by `bisect_right(edges, p) - 1`, the
last bin closed at 1.0. A probability equal to an edge's double lies in the bin that edge opens:
the literal `0.3` is the same double as the edge, so it lands in `[0.3, 0.4)`, and
`nextafter(0.3, 0)` lands below. The population is binary records whose latest observation is
`resolved` (states `scored` and `resolved_unscored`: calibration needs an outcome, not a score
row). Multiple choice, numeric and discrete are declared out of scope in `parameters`.

#### Decision — every cell below n = 30 is flagged (owner)

One declared threshold, stated in `parameters.small_sample_threshold`: every score cell,
calibration block and bin carries `n` and `small_sample`. It is a flag on a descriptive number,
not a significance test. On the live corpus **every cell is flagged**; that is the report's
main honest output, and the report-level `small_sample` warning counts the cells.

#### Decision — the model-cost column only (owner)

`forecast_records.cost_usd` per record; per group, `known`, `unknown` and the `fsum` of the
known (`null`, not `0.0`, when none is known — a zero would read as "free"). The journal's
actual-versus-held figures are per `account:tournament` scope and PR-4 is reworking the
AskNews settlement they depend on.

#### Decision — one scored subject per question

Platform scores are per question for the account. Two posted versions of one question would
each carry the same platform numbers, and summing both would count one number twice. So the
latest posted version (highest `forecast_version`, then `record_id`, a UUIDv7) is the subject
and an earlier posted one is `superseded`. **The subject is chosen within a population**
(included or excluded): round 1 showed that choosing it across both let a test-tournament
record of the same question supersede the included one and remove a verified outcome from
every summary. The lifecycle writers admit the shape (the fixture builds it through them);
012's reservation guard keeps two versions *in one tournament* out of the submission path, but
nothing stops one question id appearing in a test tournament and a real one.

#### Decision — evidence-gap markers are verified; an unknown code is refused

`tournament.is_evidence_poor`'s rule, applied to both codes: a marker bound to another
`forecast_sha256` describes other content and refuses the report rather than being read as
present or absent. The code vocabulary is closed (`EvidenceGapCode`), so a new writer code
fails loudly until it is described here and in `docs/SCHEMA.md`. Every malformed shape — not an
object, an unhashable code, a nesting depth SQLite's `json_valid` accepts and Python's parser
cannot (`RecursionError`, which `events()` does not catch) — arrives as `ReportError` without echoing the value.

#### Deviation

- **None of the three** (config bytes, `AppConfig`, prompt bytes). No migration.
- `export.py`'s docstrings said "fourteen tables"; there are fifteen since 012. Docstring words
  only, and the same two words in `tests/unit/test_export.py`.
- `tests/unit/test_runbook.py`'s documented-command set and count gain `report` (the runbook now
  shows it). A test-data change, not a rule change.
- The stricter reading of "group by model": the key is provider **and** name.

#### Rejected — extending `export` with a derived table, and why not

A join or a summary baked into the export imposes one analytical reading a consumer cannot
undo — M1-604's own reason for leaving the per-record join out — and would move
`EXPORT_SCHEMA_VERSION` every time a report parameter changed.

#### Rejected — cross-tabulations (model × type, and so on)

Every cell of a cross-tab would be a handful of records. Single axes already flag everything.

#### Rejected — calibration or a local score for multiple choice, numeric and discrete

D30 forbids a local continuous replica; multiple-choice calibration needs a per-option
reliability design the corpus (2 records) cannot exercise. Platform scores already compare every
type.

#### Rejected — Parquet output

The report is a few hundred KB of nested JSON; the byte-stable JSONL/JSON pair is the replay
form, and Parquet's bytes depend on the writer version (M1-604).

#### Deferred (do not read the absence as an omission)

- **Journal cost per record** (actual versus held) → PR-4 (M1-336), which makes the settlement
  attributable.
- **A time axis** (by week, by series phase) — not in the criterion; add when the corpus is large
  enough for it to mean anything.
- **A rendered view** — the owner follow-up is a refreshed Forecast Ledger artifact, offered
  after deploy, not part of this PR.

#### Standing risk — not verifiable offline

- **Every live cell is small** (at most 19 platform scores per metric, 11 binary calibration
  points). The report says so on every cell; nothing it prints is evidence of skill yet.
- **`question_domain` is unpopulated**, so the domain axis is one `null` group until something
  supplies it; D43's revisit trigger.
- **`tournament_state.events` is a new caller's surface.** It catches `sqlite3.Error` and
  `ValueError`; the report adds `RecursionError` at its own call. A journal row is written by
  this program through `canonical`, so the refused shapes are hand-planted ones.

### D-1002

#### Decision — one document, tested as a set of partitions, and why

`docs/SCHEMA.md` is one reference for the ledger and both derived artifacts. D-1001 spent three
rounds on a table checked row by row; T-908 fixed it by asserting the table is a **total
partition** of a universe built from the code. `tests/unit/test_schema_doc.py` does that for
every table in the document: columns and declared types against `PRAGMA table_info` on a fresh
ledger (170 columns, 15 tables); `_LEGAL_TRANSITIONS`; `RESOLUTION_KINDS` with
`SCORABLE_KINDS`/`DEFINITIVE_KINDS`; the metric vocabularies with their versions, baselines and
question types; every module-level `*_SCHEMA_VERSION` found by an AST scan of `src/` (13) with
its value; and every field path of a generated export manifest (JSONL and Parquet) and of a
generated report's three files. A `file.py:symbol` anchor must resolve against the file's AST.

#### Decision — "since" is replayed, not transcribed

The migration that introduced each column is established by applying the packaged migrations
one at a time (`_load_migrations` narrowed to ≤ k, the upgrade tests' own pattern) and recording
where each table and column first exists. A table rebuilt later still first exists at its
creator, which is what "since" means here.

#### Decision — mutability is classified from the triggers' SQL

`append-only` (an unconditional BEFORE UPDATE and BEFORE DELETE), `annotatable` (unconditional
DELETE block, only conditional UPDATE guards: `research_runs`, `research_documents`, per 003) or
`unguarded` (`schema_migrations`). The test classifies each table from `sqlite_master` and
compares; a table that fits none is its own answer and no document row can match it.

#### Decision — map-valued keys are documented once

The keys of a `states` object are the nine states and of `excluded` the exclusions; the field
tables write them `<state>` and `<exclusion>`, the path collector maps them the same way, and the
state vocabulary itself is pinned elsewhere (`parameters.states`, the unit tests).

#### Deviation — the journal kinds are listed, not specified

`tournament_events.data`'s shape is per kind and owned by each writer; the document lists the
kinds for orientation and specifies only the one the report reads (`evidence_gap`). The column
itself is fully defined.

#### Rejected — generating the document from the schema

A generated document can never be wrong and never says anything: the definitions are the part a
reader needs, and a hand-written table plus a partition test turns a schema change into a
decision — M1-604's argument for a hand-written `EXPORTED_TABLES`.

#### Deferred (do not read the absence as an omission)

- **Per-kind `tournament_events.data` shapes** — operational state read only by this program;
  specify one when an external consumer needs it.
- **Indexes and trigger bodies** — enforcement, cited by migration where a column's meaning
  depends on it, not re-documented.

#### Standing risk — not verifiable offline

- **A definition can be wrong while its row exists.** The partitions prove every column, kind,
  metric, transition, version and field has a row; the wording of each definition was checked
  against the migrations by hand and is reviewable, not testable.

### Property tests

`tests/property/test_report_properties.py`, over hypothesis-built `RecordFacts` lists (0–12
records; question ids that collide so `superseded` is reached; every type, status and kind;
score values on one ±100 scale plus signed zeros, subnormals and `±float_info.max`). Asserted:
`ReportError` is the only exception; every partition (states, single-valued axes, overlapping
axes at `max(1, |values|)` groups per record, calibration bins); per-cell `n` equals the scored
subjects' rows; at most one scored subject per question; shuffled facts give byte-identical
output; the canonical form round-trips; the bin index is monotone with every edge opening its
own bin; the small-sample flag flips exactly at 30; `sample_sd` divides by `n - 1`; 13 refused
shapes never echo a planted value.

**Reach is measured** (`test_the_strategy_reaches_every_branch`, 400 derandomized draws): each of
the nine states ≥ 20, excluded and multi-valued rows ≥ 20, an edge probability in the
calibration population ≥ 5. The first strategy starved `resolved_unscored` (12 of 400) and the
refusal branch (0 of 400); statuses, kinds and tournaments are now weighted by repetition in
`sampled_from` (which does weight, unlike `st.one_of` — M1-348), and overflow has its own
property driving one cell with extremes.

### Mutation testing

Committed first (`b84c16b`); `__pycache__` cleared before every mutant; each file restored with
`git checkout` and the tree checked clean at the end. Runner: `test_report.py`,
`test_cli_report.py`, `test_schema_doc.py` and the property file, `-x`,
`HYPOTHESIS_PROFILE=ci`, baseline green **by exit code**. The set enumerates the siblings of
each entry point rather than of chosen values: every rule of `calibration_bin` and
`summarize_values`, every branch of `_state` and `classify`, every axis and cell builder, every
check in `_require_consistent`, every refusal in the marker reader, the snapshot, the write
order, the CLI's refusal arm, and 19 document mutants (a column, transition, version, field or
warning code dropped or added; a type, since, mutability, introduced, transition, version value,
baseline, scorable flag or anchor changed).

**68 mutants: 66 killed, 2 equivalent, 0 surviving** (the last two, `subject_across_populations` and
`excluded_compared_to_included_subject`, were added with round 1's fix and are killed by it).

| mutant | why it lived | disposition |
| --- | --- | --- |
| `superseded_before_not_posted` | the reorder I wrote kept the `in _POSTED` guard, so the two branches are disjoint and their order cannot matter | **equivalent**; replaced by `superseded_first_unguarded` (the real reorder), killed |
| `binary_probability_unchecked` | `read_forecast_record` validates `ForecastRecord`, whose `_one_question` validator and discriminated union make a `binary` record carry a `BinaryForecastResponse`; the check cannot fire on anything the reader returns | **equivalent**; kept because it is what narrows the union for mypy, `lifecycle._prediction_inputs`' guard |

Two mutants were first **skipped**, not survived: `ruff format` had reflowed their target text
(`calibration_every_type`, `manifest_written_first`). Re-run against the formatted text: both
killed. Every other mutant was killed on the first run.

### Live-copy smoke

`write_report` against a `sqlite3.backup` copy of the live ledger (2026-09-24, schema 17), 0.46 s:
69 records, 4 excluded (`test_tournament`), 65 included — 19 `scored`, 1 `annulled` (post
45561), 45 `awaiting_resolution`. `all` group: each `platform_*` cell n = 19, `local_*_binary`
n = 11, binary calibration n = 11 across seven bins, model cost 27 known ($2.119) and 38
unknown. Warnings: `small_sample` (276 cells), `overlapping_axes` (3), `unknown_model_cost`
(38). A second run on the same copy was byte-identical in all three files (same `now`).
Nothing was published.

### Review

**Round 1 — CHANGES REQUESTED on `bd55ab1`** (2026-09-24, local Codex against
`GPT_REVIEW_REQUEST_M5-804_r1.md`, all four gates green in the request). One blocking finding,
all twelve risk claims otherwise marked safe, no non-blocking observations.

- **B1 — an excluded test record could supersede an included one.** `classify` chose one posted
  subject per `question_id` *before* the test-tournament exclusion, so a `bot-testing-area`
  record of the same question that sorted later made the included record `superseded` and
  removed its verified outcome from every summary. **Reproduced by execution on `bd55ab1`**
  (`rec-a` in 33125 read `superseded`, `rec-z` excluded read `scored`, the included population
  reported zero scored records). **Fixed in `1fed9d6`:** subjects are keyed on
  `(exclusion, question_id)`. `test_an_excluded_record_never_supersedes_an_included_one`
  (production writers) and the reworked property both fail on `bd55ab1` and pass on the fix.
  **Why the property missed it:** it asserted one subject per question across both
  populations — which the bug satisfies. It is now per population, with an explicit check and a
  reach floor (≥ 20 in 400 draws) for questions posted in both. Same family as the vacuous
  property class: the assertion was about the wrong partition.

**Round 2 — APPROVE on `3661084`** (2026-09-24, against `GPT_REVIEW_REQUEST_M5-804_r2.md`, which
led with the `bd55ab1`→HEAD delta). B1 closed; no blocking findings; all thirteen risk claims
marked safe. One non-blocking observation — `report.py`'s module docstring still said "one
scored subject per question" — is fixed after approval ("within each population"). That
docstring and this entry are the only change after the approved commit.

## M4-808 — Accept integer-valued platform scores (D52, amends D42)

Filed and merged first as PR #132 (row + D52), then built on `feat/m4-808-platform-score-int`.

### What happened

2026-10-03 12:28 MDT: `score` began exiting 4 (`records: 63 failed: 2`) and the watchdog paged
every five minutes. Questions 45978 (binary) and 45971 (multiple_choice) carried
`baseline_score` and `spot_baseline_score` as the JSON integer `0`. `platform_scores._read`
required `type(value) is float` on the evidence of 140 floats seen 2026-09-23. The same two
records were absent from the attribution report (n = 58 of 60 scored).

### Decision — convert in the reader AND widen the trigger, and why

The plan said "no migration". That was wrong, found before any code was written: 017's value
clause requires `json_type(source, '$...score_data.<k>') = 'real'` (top level and group member),
so a reader-only fix would pass in Python and be refused at INSERT. `018` is one DROP/CREATE of
`score_events_validate_on_insert`, 017's text verbatim with the two `= 'real'` tests changed to
`IN ('real', 'integer')` and one comment. The owner approved the migration at plan time
(2026-10-05). `typeof(NEW.value) <> 'real'` is untouched: what is stored is still a REAL.

### Decision — the boundary is exactness, not magnitude

An exact-type `int` is accepted iff `float(v)` holds it exactly (`int(float(v)) == v`).
`2**53 + 1` and `10**400` refuse as `PlatformScoreError` ("must be a number"); `2**60` is
accepted because a double holds it. This is the same line the schema draws: SQLite compares an
integer with a REAL exactly, so the trigger refuses exactly the integers the reader refuses.
`bool` is refused (it is an `int`; `type(v) is int` excludes it).

### Deviation

The error text changed from "must be a float" to "must be a number" (the rule is no longer
float-only). Tests that matched the old text were updated. `test_an_observation_without_a_real_score…`
was renamed `…numeric_score…`: it pinned `{"peer_score": 3}` as refused, which is now admitted.

### Rejected — and why not

- *Hand-patch the two rows:* leaves the next integer-valued score to page again.
- *Accept any numeric type:* admits `bool`.
- *Record a quiet `no_platform_scores` gap:* reverses D42's loud-failure decision.

### Standing risk — not verifiable offline

The platform could send some other shape (a numeric string, a rational). That still fails loudly
by design; D52's revisit trigger names it. The live path (apply 018 to the production ledger, then
`score` over the two observations) is verified only after deploy — see below.

### Deploy

A merged migration stops the worker at the next poll: back the ledger up, merge between polls,
run `init-ledger` at once, then `score`. Expected: 8 new `platform_*` rows, resolutions unit
green, watchdog clear; regenerate the M5-804 report (n = 60).

---

## M4-806 — Schedule resolution ingestion for the Cup profile (D40, D54)

**What shipped.**
- **Units:** `deploy/systemd/whiskeyjack-resolutions-cup.{service,timer}`. They are M4-805's
  pair with three differences: both `ExecStart` lines name `config/tournament-cup.yaml`, the
  descriptions, and an `OnCalendar` of `03/6:23` instead of `00/6:23`.
- **Tests:** `tests/unit/test_deploy_resolutions_unit.py` runs every contract once per
  profile, against that profile's own tournament poll: pager parity, environment parity, the
  ExecStart contract, the timer, and the driven measurement that reaches nothing paid or
  posting. One new test pins that the two profiles name distinct configs, distinct ledgers
  and distinct hours.
- **Runbook:** the "Cup is not scheduled" line is replaced by the Cup schedule, with its
  one-time backup-then-`init-ledger` procedure.

### Decision — a second unit pair, not a second `ExecStart` pair in one unit, and why

One unit running both profiles would chain them. systemd runs a later line only after the
earlier one exits 0, so a Cup failure would stop MiniBench scoring, or the other way round,
and the page would not say which profile failed. Separate units page separately
(`whiskeyjack-resolutions-cup failed`), and each profile's failure is its own.

### Decision — parity is checked against each profile's own poll

The Cup unit's config path is compared with `whiskeyjack-tournament-cup.service`'s, not with a
literal. A Cup unit left pointing at `config/tournament.yaml` fails two tests (mutation-checked).
That is the M4-805 witness rule, applied per profile.

### Decision — three hours out of phase

The two runs touch different ledgers, so they cannot contend on a lock. The offset keeps the
two profiles' Metaculus GETs out of the same window, and it keeps minute 23, which is clear
of the poll (`*:0/5`) and the watchdog (`*:2/5`).

### Deviation — the row said "init-ledger upgrades it to 15"

When M4-806 was filed the head was 15. The Cup ledger is still at 13 and the head is now 19,
or 20 once M1-359's `020` merges, so the upgrade is to the current head (D54 amends the row).
The runbook says to run `init-ledger` on the Cup ledger after **every** later migration,
because the Cup unit otherwise refuses and pages at its next run.

### Deferred (do not read the absence as an omission)

- **Watchdog coverage of the Cup timer.** `deploy/wj-watchdog` hard-codes
  `RESOLUTIONS_UNIT = "whiskeyjack-resolutions"`. The criterion asks that a *failure* page
  through the existing notify template, which `OnFailure=` gives. A stopped Cup timer goes
  quiet instead, as the MiniBench timer did before M1-341. The runbook says so at the point
  of use.
- **The live upgrade and install** are an operator step after merge, done behind a verified
  backup. They are not a test, because CI has no Cup ledger.

### Standing risk — not verifiable offline

Whether the five posted Cup questions come back from Metaculus as resolved observations. The
platform masks resolutions unless the account predicted (M4-801), and it did predict all five.
q45572 (Nobel) was due 2026-10-09 and is the first live check.

### Round 1 — one blocking finding, reproduced and closed

On `b4baeaa`, the reviewer found **B1**: the runbook's Cup block ran `init-ledger` even when the
backup failed. The commands were separate lines, and bash carries on past a failed one.
Reproduced by execution: `sqlite3` and the interpreter were replaced with shims and the backup
made to fail, the init-ledger marker was still written, and the block exited 0.

**Fix:**
- `init-ledger` runs inside an `if`, gated on the backup being written **and**
  `integrity_check` printing exactly `ok`.
- The install steps are a second block, run only after `UPGRADED`.

**Test:** `test_the_cup_upgrade_runs_only_behind_a_verified_backup` runs the runbook's own
block under shims. It has a positive control and three failure cases: the write fails, the
check prints an error, the check prints nothing. All three failure cases fail against the
round-1 text.

Non-blocking: the reviewer suggested watchdog coverage for the Cup timer as a backlog
candidate. It is already listed under Deferred above.

### Round 2 — approved

`aaca885`: APPROVE, with B1 closed. The reviewer ran the extracted block under shims for seven
outcomes, including a failing integrity command and a failing upgrade. No new candidates; Cup
watchdog coverage stays Deferred.

## M4-809 — A local log score for numeric and discrete forecasts

Wave 27 (owner decisions 2026-11-15, D62). Every experiment arm this wave plans, the replay
arms of M5-806 and the live arms of M1-614, is a forecast that was never posted, so Metaculus
never scores it. `scoring.py` scored binary and multiple-choice forecasts locally (M4-802).
Numeric and discrete forecasts are 105 of the 229 scored so far (46%) and had no local score,
so half the questions could not enter a paired comparison.

### Delivered

- `scoring.score_continuous`: the natural log of the probability a stored CDF puts on the
  resolved bucket, with the bucket and the open-bound count it was read with
  (`ContinuousScore`). `scoring.continuous_log_v1` returns the number alone.
- `scoring.platform_baseline_points`: that score on Metaculus's spot baseline scale, so it can
  be checked against the platform's own number. Nothing stores it.
- `scoring.peer_points_per_nat`: 100 for binary and multiple choice, 50 for numeric and
  discrete (AC3).
- `tests/fixtures/scoring/continuous_platform_scores.json`: 61 cases extracted read-only from
  the live ledger, numbers only, by `scripts/regenerate_continuous_score_fixture.py`.
- `scripts/measure_log_scale_location.py`: prints the log-scale location's distance from exact
  arithmetic by regime, and its bucket agreement with the platform's expression in floats.
  Seeded, reads and writes nothing, about four minutes. Added in review round 1.
- No migration, no `score_events` row, no `AppConfig` field, nothing the worker runs (AC4).

### What was established by execution before designing

Measured read-only on the live ledger (`mode=ro`), 2026-11-15.

- **A candidate rule reproduces every platform score.** For all 105 numeric and discrete
  records that hold a `platform_spot_baseline_score`, the posted CDF
  (`question.my_forecasts.latest.forecast_values` in the stored resolution observation), the
  stored `scaling` block and the stored resolution give Metaculus's number with a largest
  absolute difference of 4.3e-14 points.
- **Two records fix the edge rule.** A plain floor-plus-one bucket rule reproduces 103 of 105.
  The two it misses resolve exactly on a bucket edge: q45747 (215.65 in [190, 235], where
  `u * 200` computes to 114.00000000000003) and q45962 (70250 in [65000, 95000], exactly
  35.0). The platform scores both from the lower bucket.
- **The source says the same.** Metaculus's `main` at `7b47f09` (2026-11-15):
  `utils/the_math/formulas.py` (last changed `b8e6cf4`), `scoring/score_math.py` (`8d7ac79`)
  and `questions/models.py` (`7e4489e`). `unscaled_location_to_bucket_index` is
  `max(int(u * N + 1 - 1e-10), 1)` with `u < 0` giving 0, `u > 1` giving `N + 1` and `u == 1`
  giving `N`; `get_pmf` is `cdf[0]`, the differences, then `1 - cdf[-1]`; the continuous
  baseline is `100 * ln(pmf[b] / baseline) / 2` with a baseline of 0.05 outside a bound and
  `(1 - 0.05 * open_bounds) / (len(pmf) - 2)` inside. Ledger and source agree, so nothing was
  adapted and there was nothing to stop and ask about.
- **Our own record gives the platform's CDF.** `authorized_payload(record,
  calibration=...)` rebuilt from each stored `forecast_records` row yields a
  `continuous_cdf` bit-identical to the one the platform holds, on all 105. An experiment arm
  can therefore be scored from its own record with no platform round trip.
- **What the payload looks like.** `inbound_outcome_count` is present on all 105 (200 for
  every numeric question; 3 to 161 for discrete). `range_min` and `range_max` arrive as JSON
  integers on about half. A closed lower bound's `cdf[0]` is the JSON integer `0` on 33.
  `zero_point` is null on all 105: no live question is log-scaled.
- **A closed bound's end of the CDF is exact on every live record** (measured read-only on
  2026-11-16, for review round 1). 33 of the 105 have a closed lower bound and `cdf[0]` is `0`
  on all 33; 6 have a closed upper bound and `cdf[-1]` is `1` on all 6. The 72 open lower
  bounds all hold `cdf[0] > 0` and the 99 open upper bounds all hold `cdf[-1] < 1`. The fixture
  carries 28 of the closed lower bounds and all 6 closed upper ones, and a test asserts them.

### Decision — the score is the log of the bucket's mass, and the platform scales are helpers

The stored quantity for binary and multiple choice is `ln p` (M4-802). The continuous score
follows it: `ln` of the mass on the resolved bucket, with no baseline and no halving. The
baseline scale needs the bucket count, whether the bucket is outside a bound and the number
of open bounds, so `ContinuousScore` carries those and `platform_baseline_points` derives the
platform's number from it. A caller that wants peer points multiplies a difference of two
`log_mass` values by `peer_points_per_nat`.

### Decision — the paired identity, stated (AC3)

For two forecasts on one question, with every other forecaster fixed:

    spot peer(A) - spot peer(B) = k * (ln mass_A - ln mass_B)

with `k = 100` for binary and multiple choice and `k = 50` for numeric and discrete. The
platform's peer score is `100 * (n / (n - 1)) * ln(p / gm)`, where `gm` is the geometric mean
over all `n` forecasters including this one. Because `ln gm = (ln p + sum of the others) / n`,
that expression is exactly `100 * (ln p - mean of the others' ln p)`: the `n / (n - 1)` factor
removes this forecaster's own share of the mean. The others' term is the same for A and B and
cancels. Numeric, date and discrete scores are then halved. The same difference appears on
the baseline scale, because A and B share one baseline. `test_scoring_continuous.py` checks
this against the platform formula written out in the test, not against this module.

### Deviation — four places where the text differs from the platform's, on purpose

All four coincide with the platform on a linear scale, which is every live question, and that
is what the 105-of-105 reproduction shows. They differ only on a log scale, in the cases
named, where the platform's float arithmetic has no meaningful answer.

1. **The bound markers go straight to their buckets.** The platform maps
   `below_lower_bound` to `range_min - 1.0` and `above_upper_bound` to `range_max + 1.0` and
   unscales those; on a log scale with `range_min - zero_point < 1` the first is the logarithm
   of a negative number.
2. **A value outside the range is decided by comparison, before unscaling.** Found by the
   property pass, before review: with range `[3.6e-229, 1]` and zero point `-1`, the value
   `0.0` is below the range, but `(value - low) * (ratio - 1) + width` loses a difference of
   3.6e-229 against a width of 1, the location computes to exactly 0, and the value was scored
   in the first inbound bucket. The platform itself writes a value below `range_min` as the
   marker (`scaled_location_to_string_location`), so the comparison is the rule.
3. **A log scale's location is computed in a different form, from the bound nearest the zero
   point.** Rewritten in review round 1; it replaces a clamp. The platform computes
   `(ln((x - min) * (ratio - 1) + (max - min)) - ln(max - min)) / ln(ratio)` with
   `ratio = (max - zero) / (min - zero)`. This module computes
   `ln(1 + (offset / width) * (R - 1)) / ln(R)`, both logarithms as `log1p`, where `offset` is
   the distance from the bound nearest the zero point and `R > 1` is the far bound's distance
   from the zero point over the near bound's; with the zero point above the range the result
   is reflected (`1 - ...`). In exact arithmetic the two are the same number. In floats the
   platform's fails in three ways. Each has a unit witness that executes the platform's
   expression, restated in the test file, and asserts the failure:
   - **Its product overflows** (review round 1, B2). Range `[1, 1e200]`, zero point 0, value
     1e150: the product is 1e350 and the location is infinite. The true location is 0.75.
   - **Its sum cancels when the zero point is above the range** (found while fixing B2). The
     platform measures from `min`, which is then the far bound. Zero point 1e-200 above
     `[-1, 0]`, value -1e-60: `value - min` is exactly 1.0 and the sum is exactly 0. The true
     location is 0.3. Round 1's code turned that zero into the last bucket, through a guard
     written for the upper bound alone.
   - **Its two logarithms cancel when the zero point is far from the range.** Round 1 listed
     this as a standing risk at "about 1e15 range-widths". Measured, it starts far sooner:
     the error is 1.6e-11 of the range at 1e3 range-widths, 1.7e-8 at 1e6, 1.6e-5 at 1e9, 1.7%
     at 1e12, and more than the whole range from 1e14. Round 1 clamped the result to `[0, 1]`,
     which kept the bucket inside the CDF and said nothing about whether it was the right
     bucket. The same clamp is what turned B2's infinity into a score.

   The form used here is within 4e-16 of the exact location in every one of those regimes and
   inside `[0, 1]` on every draw: `scripts/measure_log_scale_location.py`, 180,000 scales
   against 60-digit arithmetic. So there is no clamp, no guard and no second branch.

   What it costs is agreement with the platform's own rounding at one kind of point. On scales
   the size of a real question (width 1e-3 to 1e6, ratio up to 1e9, 200 buckets), the bucket
   is the one the platform's expression gives in floats for every interior value measured:
   600,000 of 600,000, both orientations. For a value exactly on a bucket edge it is the same
   with the zero point below the range (299,999 of 300,000), and with the zero point above the
   range it differs in 0 of 100,000 at ratios up to 1e3, 675 of 100,000 up to 1e6 and 5,009 of
   100,000 up to 1e9. Those are the points where the platform's own location is up to 5.6e-9 of
   the range from exact, against an edge epsilon worth 5e-13 of the range, so which side of
   the edge its floats land on is decided by its rounding and not by its rule. See the
   standing risk.
4. **No separate branch for `u == 1`.** The platform returns `N` there explicitly.
   `int(N + 1 - 1e-10)` is already `N` while half an ulp of `N + 1` is below the epsilon,
   which holds to about one million (measured: still `N` at 1,000,000, `N + 1` at
   4,000,000); the count is bounded at 10,000 (next decision), so the branch would change
   no result and no test could kill it.
   The same epsilon and the clamp to bucket 1 also place a location one ulp outside `[0, 1]`,
   which item 3's form can produce: an ulp past 1 times the largest count is 2e-12.

### Decision — the outcome count is bounded at 10,000

`inbound_outcome_count` is untrusted payload and sizes the CDF the function walks. The
largest a live question has carried is 200 (D57 admits 201 for one discrete shape). 10,000
leaves a fifty-fold margin and keeps the epsilon rule exact.

### Decision — the CDF must have exactly `inbound_outcome_count + 1` points

The bucket rule multiplies by the question's inbound outcome count; the PMF is read by
index. A CDF of any other length would be scored against buckets it does not have, so it is
refused. An absent count means the platform's default of 200.

### Decision — a closed bound pins its end of the CDF (review round 1, B1)

A closed lower bound means no outcome exists below it, so `cdf[0]` must be 0; a closed upper
bound means `cdf[-1]` must be 1. Round 1 read the two flags for the baseline and never
compared them with the CDF, so a CDF with 5% of its mass outside a closed bound scored as if
the bound were open. Each end is now refused with its own sentence, the lower bound first,
after the CDF's shape rules and before the resolution is looked at.

- **Exact, no tolerance.** `forecast/cdf.py` builds the endpoints with `!=`, and all 39 closed
  bounds on the 105 live records hold exactly 0 or 1 (above). The JSON integers `0` and `1`,
  which are what the platform stores, and `-0.0` compare equal and pass.
- **One way only.** An open bound with no mass outside it is not refused. It is still a
  distribution over this question's outcomes, and an outcome outside that bound is refused as
  probability 0 by the rule that was already there. Whether the platform accepts such a CDF
  at posting was not looked into: the scorer is not a posting validator, and does not check
  the minimum step either.

### Decision — a log scale is usable when the ratio it is evaluated with is finite and above 1

Changed in review round 1, with deviation 3. Round 1 tested the platform's `deriv_ratio`
(finite, positive, not 1). With the zero point above the range that is the reciprocal of the
ratio now used, so a scale whose `deriv_ratio` was 1e-310 passed, and its reciprocal does not
fit a float. The rule is now stated on the ratio the location is computed with: the far
bound's distance from the zero point over the near bound's must be finite and above 1. That
refuses two things, with the one sentence round 1 already had: bounds whose distances from
the zero point differ by a factor beyond 1.8e308, either way up, and a zero point so far away
(about 1e16 range-widths) that the two distances are equal in floats.

### Decision — an integer is a number here, a bool is not

M4-802's probabilities are exact `float`s because the forecast schema stores floats. This
function reads Metaculus payload, where a closed bound's CDF end and a whole-number range are
JSON integers. Exact `int` and exact `float` are accepted; `bool`, `str` and everything else
refuse. An integer too large for a float refuses as `ScoreError`, never `OverflowError`.

### Decision — a resolution string is matched, not handed to `float()`

`float()` accepts `nan`, `inf`, `1_0`, surrounding whitespace and non-ASCII digits. A
resolution is matched against an ASCII decimal pattern first, bounded in length, and must
then be finite.

### Rejected — and why not

- **A new stored metric.** `score_events.metric` is closed by 017/018's trigger. Posted
  numeric records already carry the platform's own scores, and the forecasts this score
  exists for have no `score_events` row to hold one.
- **Reading the ledger inside the scorer.** `scoring.py` is pure; the caller supplies the CDF
  and the scaling.
- **Clamping a zero-mass bucket.** Same reason as M4-802: a clamp makes the function total by
  changing the score it reports. A posted CDF cannot have an empty inbound bucket (the
  platform enforces a minimum step); a closed bound's outside bucket is empty by
  construction, and an outcome there is refused.
- **Returning the baseline-scale number as the score.** It bakes the platform's baseline and
  halving into a stored-looking value; the log mass is what a paired comparison needs.
- **Refusing an ill-conditioned log scale.** It needs a conditioning threshold the platform
  does not define, and any number chosen here would refuse some question the platform
  accepts. Deviation 3 removes the need: the form used has no ill-conditioned regime short of
  a ratio that is exactly 1 in floats (a zero point about 1e16 range-widths away), and that
  was already refused, because the logarithm that divides is then zero.
- **The reviewer's minimal fix for B2: keep the platform's expression and change form only
  when it overflows.** Built first (`5a88000`), then replaced (`a765849`). It repairs the
  overflow and leaves the platform's expression in use where its two logarithms cancel, which
  needs the clamp back, and the clamp is what hid B2. A mutant that puts that expression and
  its upper clamp back is in the mutation run below.
- **A tolerance on a closed bound's endpoint.** No live record needs one, our own builder is
  exact, and a tolerance would be a second definition of "closed".
- **Refusing an open bound that has no mass outside it.** See the endpoint decision.

### Deferred (do not read the absence as an omission)

- Local `score_events` rows for posted numeric and discrete records: a vocabulary migration
  and its own row, if wanted.
- Date questions. The platform treats them as continuous; this project does not forecast
  them (`forecast.supported_question_types`).
- The spot peer score itself. It needs the other forecasters' PMFs, which the ledger does not
  hold; only differences between our own forecasts are computable, which is what AC3 states.

### Standing risk — not verifiable offline

- **The log-scale branch.** No live question has a `zero_point`, so that branch rests on the
  source text and on hand-computed cases, not on a platform score.
- **On a log scale, an outcome exactly on a bucket edge may be scored one bucket from the
  platform's.** Measured against the platform's expression restated in Python floats, never
  against the platform: with the zero point above the range, 675 of 100,000 edge values at
  ratios up to 1e6 and 5,009 of 100,000 up to 1e9 land in the neighbouring bucket (deviation
  3). This module's bucket there is the one the exact location gives. The platform's is
  whatever its floats round to, and whether its floats and Python's agree to the last bit at
  such a point has not been checked. A paired comparison scores both arms in one bucket
  either way. If a log-scaled question is ever forecast and resolves on an edge, compare the
  platform's score before relying on the local one.
- **The platform changing its rule.** The bucket epsilon and the baseline constants are
  restated here from the commits named above. The fixture pins today's behaviour; a platform
  change would show up as posted records whose platform score no longer matches.
- **Spot, not coverage-weighted.** The fixture compares against `spot_baseline_score`. The
  time-weighted `baseline_score` multiplies by coverage and is not reproduced.

### Property tests

`tests/property/test_scoring_continuous_properties.py`, sixteen tests. Reach is recorded with
`hypothesis.event`: scale kind (linear, zero point below, zero point above) and size, bucket
kind, closed bounds, whether the platform's own product overflowed, and refusals.

1. Nothing but `ScoreError` escapes arbitrary input, and every refusal's text is one of 21
   sentences written out in the test, so no message can carry a value. A second property
   puts junk in one argument at a time behind valid neighbours, so each guard is reached.
2. On valid input the score is `ln` of the mass an independently derived PMF puts on the
   returned bucket, and the only refusal is an empty resolved bucket.
3. The resolved value lies inside its bucket, with the edges rebuilt through the platform's
   *inverse* scaling (which this module does not contain); the two outside buckets are
   exactly the two strict inequalities on every scale.
4. On every log scale the bucket holds the value's exact location to a billionth of a
   bucket. The location is the platform's formula in 60-digit decimal arithmetic over exact
   rational distances. Values are drawn evenly over the locations, a hair either side of a
   bucket edge (from a third of a bucket down to 1e-8 of one), uniformly, and on the bounds.
   Added in review round 1.
5. An exact edge of a linear scale belongs to the lower bucket; edge 0 is bucket 1.
6. A larger resolution never lands in an earlier bucket.
7. One resolution per bucket recovers all the mass: the masses sum to one.
8. More mass on the resolved bucket scores higher; mass moved between other buckets changes
   nothing.
9. The persisted JSON form replays to the same score, a tuple scores as a list, and an
   integer 0 or 1 scores as the float does.
10. `platform_baseline_points` equals the platform's formula restated in the test.
11. A closed bound refuses exactly when the CDF puts mass outside it, with that bound's
    sentence, the lower bound first. The flags are drawn independently of the CDF here and
    nowhere else. Added in review round 1.

**Why round 1's pass missed both blockers, and what changed.** Two causes in what the
strategies could reach, and one in what a property could see.

- The bound flags were drawn independently of the CDF's ends, so no property could say what
  a closed bound means. `valid_inputs` now builds the CDF from the flags, and property 11
  draws them apart on purpose.
- Every scale was within 1e6 of zero with a ratio of at most about 1e9, so no product could
  overflow.
  Scales now come in five sizes: that one; the same ranges with the zero point 1e9 to 1e15
  range-widths away; the size of a real question; any width from 1e-280 to 1e280 with a
  ratio up to 1e300; and the corner where the platform's product overflows.
- Property 3 checks in value space with a slack of a millionth of the range, which on a wide
  log scale is wider than most buckets. Checked by execution: it would have refused B2's own
  case had it drawn the scale (1e150 is far below the last bucket), but it accepts 1e100 in
  bucket 150 of that scale, and it accepts the mirror defect's -1e-60 in the last bucket of
  four. Property 4 is in location space.

Run against round 1's scorer (`bbab3ca`'s `scoring.py`, in a copy of the tree) at the 25-draw
profile, properties 4, 7 and 11 fail, and so do 14 unit cases in 7 tests.

M4-802's raise-site tripwire (`test_no_canary_repr_collides_with_any_refusal_message`)
counted 14 `raise ScoreError` statements and failed on the fifteenth, as designed. It now
holds a probe for each of M4-809's 19 sites (33 sites, 36 distinct messages), every probe
input carrying a canary. The two endpoint sites were added in review round 1, and the test
failed on the count until their probes were.

### Mutation testing

53 mutants of `scoring.py`, each one exact text replacement, committed first, `__pycache__`
cleared before every run. The unit file, the property file (fast profile, 25 draws) and
M4-802's tripwire test ran together and kills were attributed per file. A replacement whose
text did not occur exactly once would have been counted as skipped, apart from survivors;
none was.

- **First run, 51 mutants: 49 killed, 2 alive.** Both survivors were the same shape: another
  guard refused first, so the guard under test was never the one deciding.
  - *Any container with a length accepted as a CDF.* Every malformed container in the tests
    also had the wrong length. Six containers of the right length that are not a list or a
    tuple (a list subclass, a dict, a deque, an array, a frozenset, a range) now refuse.
  - *Outcome count unbounded above.* The over-limit count was only offered with a five-point
    CDF, which the length rule refuses. It is now offered with a CDF that matches it.
- **Two mutants added** for a branch a proofread found untested: the log-scale guard for a
  ratio too small to register against 1, where the platform's argument is exactly zero at the
  upper bound. Mutated to return the lower end, and removed; both killed.
- **One kill moved down a level.** A mutant that scored the above-bound bucket on the inbound
  baseline was killed only by a property; the hand-computed baseline test now covers both
  outside buckets. A mutant that echoed a number field's value was killed by the property and
  the tripwire but by no unit test; the unit canary list now reaches the number rule.
- **Final: 53 of 53 killed, 0 skipped.** Every mutant is killed by a unit test; 27 also by a
  property at 25 draws; 10 also by the tripwire.

The mutants cover: the edge epsilon (zeroed, sign flipped), the clamp to bucket 1, the two
comparisons (loosened and removed), the location clamp, the log-scale formula (ratio
inverted, `ln(width)` dropped, scale ignored), both bound markers, the three mass reads, the
empty-bucket refusal, each CDF rule, each type gate, each scaling refusal, the count's two
bounds and its default, the resolution pattern, length and finiteness, the baseline's
constant, open-bound term, spread and halving, the peer factors, and two messages rewritten
to echo their input.

Running it cost one lesson worth keeping: a mutant that fails a property makes hypothesis
shrink, so a run is about 10 seconds per mutant when tests pass and minutes when they do not.
51 mutants did not fit a ten-minute slot, and an interrupted harness leaves the source file
mutated; `git checkout -- <file>` and a `git status` check follow every run.

**Second run, for review round 1's fixes.** 73 mutants against `1ecd02a` (`scoring.py` has
not changed since): 47 of the 53 above, 5 of them reworded for changed text, and 26 new. The
6 dropped are for code that no longer exists: the location clamp (two), the platform's
`ln(width)` term, the zero-ratio check and the vanishing-ratio guard (two). **73 of 73
killed, 0 skipped, every one by a unit test**; 48 also by a property at 25 draws, 18 also by
the tripwire. This run worked on `git archive` copies of the tree, one per worker, so an
interrupted run cannot leave the worktree mutated.

- **The endpoint rule, 11 new mutants:** each check removed, each given a tolerance of 1e-12,
  each keyed on the other bound's flag, each applied to open bounds instead, the upper rule
  reading the first point, and each message rewritten to echo the point.
- **The log-scale location, 14 mutants, 13 of them new:** the width not divided out, `log`
  for `log1p` in the numerator and in the denominator, the division by `ln(ratio)` dropped,
  the ratio used for `ratio - 1`, the offset fraction inverted, each orientation measured
  from the wrong bound or reflected wrongly, the scale ignored, and the platform's
  expression put back two ways.
- **The ratio and the flags, 4 mutants, 2 of them new:** each orientation's ratio inverted,
  the side test flipped, and the open-bound count.

One mutant deserves its own line, because it is the code this branch nearly shipped: the
platform's expression behind an overflow fallback, which is `5a88000`'s. It picks a wrong
bucket only when the zero point is far away, and even there it is right for most values.
Measured with the mutant applied to a copy of the tree, 4,000 uniform values per distance: 0
wrong at 1e10 range-widths, 1 at 1e11, 13 at 1e12, 131 at 1e13 and 1,160 at 1e14. The eight
far-zero-point unit cases kill it every time. The exact-location property killed it in 1 of
10 seeded runs at 200 draws as first written. Its far scales are now drawn from 1e9
range-widths outward, with the decade drawn as an integer and three shares in seven, and it
kills it in 9 of 10 at 200 draws and 5 of 10 at 25. That is the same lesson as the two
blockers, met a third time: a property is only as good as the draws that reach the defect,
and the reach has to be measured against a scorer that has the defect.

### Review

**Round 1, on `bbab3ca`: CHANGES REQUESTED, two blocking findings.** Both were reproduced by
execution on that commit before any change. No non-blocking observations were raised.

- **B1: closed-bound CDF endpoints are not validated.** Reproduced: `[0.05, 0.25, 0.5, 0.75,
  0.95]` with both bounds closed scored `below_lower_bound` in bucket 0 with `ln(0.05)` and
  0.0 baseline points. Closed in `5a88000`: see the endpoint decision above.
- **B2: overflow puts an interior log-scale resolution in the last bucket.** Reproduced: range
  `[1, 1e200]`, zero point 0, value 1e150 scored bucket 200 with `log_mass` -4.6077, where
  bucket 150 and -4.8962 are right. Closed in `5a88000` by the reviewer's minimal fix, which
  `a765849` then replaced with deviation 3's single form, for the reason under Rejected.

**Found while closing B2, not raised by the review.** Both are the same kind of defect as B2,
an interior value scored in a bucket it is not in, and both are closed by deviation 3.

- With the zero point above the range, the platform's sum cancels where B2's product
  overflows. Zero point 1e-200 above `[-1, 0]` put -1e-100, which is at 0.5, in the last
  bucket. This was live in `bbab3ca`.
- With the zero point far from the range, the two logarithms cancel. Round 1 carried this as
  a standing risk at "about 1e15 range-widths" and clamped the result. Measured, the location
  is 1.7% of the range out by 1e12 range-widths. Eight unit cases now assert the exact bucket
  where round 1 asserted only "inbound".

**What goes beyond the review.** The reviewer asked for the normalized expression "when the
existing intermediate overflows". `a765849` uses it everywhere and removes the clamp, which
changes the arithmetic on every log scale, not only an overflowing one. Deviation 3 gives the
measurement behind that and what it costs.

**Owner decision, 2026-11-16, before round 2.** The owner had agreed to the reviewer's minimal
B2 fix, and was asked again once `a765849` had replaced it. The answer was to keep the single
form.

**Round 2, on `2883806`: APPROVE, no blocking findings.** B1 and B2 were both marked closed,
each on its round-1 reproduction: the first now refuses with the lower-bound sentence, the
second returns bucket 150 and `log_mass` -4.896191159705386. None of the request's seven risk
claims was faulted. The reviewer reports running the 196 scoring unit tests under Python
3.14.4 and a containment check of its own on the log-scale bucket: 4,415 cases against an
80-digit oracle, both orientations, counts up to 10,000. It could not run the property suite
(no Python 3.11 there, and hypothesis's native extension did not load), as in round 1.

One non-blocking candidate: verify platform parity at log-scale bucket edges, with the
acceptance criterion "when such records become available, commit numerical fixtures covering
edge resolutions and both zero-point orientations, and document every discrepancy". That is
the second standing risk above, which names the same trigger. No backlog row was filed for
it: no live question is log-scaled, so there is no record to build a fixture from.

This entry and the backlog row's flip to Done are the only change after the approved commit.
