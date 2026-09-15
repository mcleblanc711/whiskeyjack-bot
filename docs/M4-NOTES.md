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
