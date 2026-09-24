# Ledger schema, lifecycle and derived-artifact contracts (D-1002)

This is the reference for what the attribution ledger holds and what is derived from it: every
table and column, the lifecycle state machine, the version constants that travel with the
data, and the contracts of the two derived artifacts (`export` and `report`). A code anchor
names a file under `src/whiskeyjack_bot/` and a module-level symbol in it, joined by a colon
(`ledger.py:connect_readonly`), and resolves against the file's AST. Migrations are `src/whiskeyjack_bot/migrations/NNN_*.sql`.

**This document is tested.** `tests/unit/test_schema_doc.py` parses it and requires each of
these to be a *total partition* of what the code or a freshly initialized ledger actually
has -- nothing missing, nothing extra:

- the tables, their columns and declared types, and the migration that introduced each;
- each table's mutability class, classified from its triggers;
- the lifecycle transitions (`lifecycle.py:_LEGAL_TRANSITIONS`);
- the version constants (every module-level `*_SCHEMA_VERSION` in `src/`), with their values;
- the resolution kinds and the score metrics;
- every field of the export manifest and of the report's three files.

A schema change that is not described here fails that test, which is the point: the
document cannot silently fall behind the ledger.

## Sources of truth

- **The ledger is the only source of truth** (D16): one SQLite database,
  `storage.sqlite_path`, schema version `ledger.py:LEDGER_SCHEMA_VERSION`, migrated only by
  `whiskeyjack-bot init-ledger`.
- **It is append-only** (D25). Forecast versions, lifecycle events, approvals, attempts,
  resolutions and scores are never updated or deleted; a later state is a new row. The
  triggers that enforce it are listed per table below. A forecast record is written once, as
  a `draft`, and its **current status is derived**: the `to_status` of its highest
  `lifecycle_events.event_seq`, or `forecast_records.status` (always `draft`) while it has no
  events (`lifecycle.py:current_status`).
- **Exports and reports are derived artifacts** (D29). Neither is read back by the program,
  and neither can disagree with the ledger for long: re-derive it.

## Versions

Every version constant that travels with stored data. Each one versions a different thing,
and bumping one never bumps another.

| Constant | Value | Defined at | What it versions |
| --- | --- | --- | --- |
| `LEDGER_SCHEMA_VERSION` | `17` | `ledger.py:LEDGER_SCHEMA_VERSION` | The ledger schema: the number of the last migration this build applies. `connect_readonly` refuses a ledger ahead of or behind it. |
| `RECORD_SCHEMA_VERSION` | `1.0.0` | `forecast/record.py:RECORD_SCHEMA_VERSION` | The shape of `forecast_records.record_json` (`ForecastRecord`). Records stamped `1.1.0` are also accepted by the validator. |
| `RESPONSE_SCHEMA_VERSION` | `1.0.0` | `forecast/schema.py:RESPONSE_SCHEMA_VERSION` | The model's output contract (`record_json.forecast`), not the prompt's version. |
| `OBSERVATION_SCHEMA_VERSION` | `1.0.0` | `resolution.py:OBSERVATION_SCHEMA_VERSION` | `resolution_events.resolution_snapshot_json` (`ResolutionObservation`). |
| `PACKET_SCHEMA_VERSION` | `1.0.0` | `research/packet.py:PACKET_SCHEMA_VERSION` | The research-packet digest rule behind `record_json.research_packet_sha256`; part of the hashed payload. |
| `KEY_SCHEMA_VERSION` | `1.0.0` | `submission.py:KEY_SCHEMA_VERSION` | The idempotency-key derivation (`submission_attempts.idempotency_key`, `submission_key_reservations.idempotency_key`); part of the hashed payload and the key prefix. |
| `VERIFICATION_SCHEMA_VERSION` | `1.2.0` | `submission_live.py:VERIFICATION_SCHEMA_VERSION` | The envelope stored in `submission_attempts.refetched_forecast_snapshot`. |
| `ARTIFACT_SCHEMA_VERSION` (submission) | `1.1.0` | `submission_gateway.py:ARTIFACT_SCHEMA_VERSION` | The submission receipt artifact written under `storage.artifact_root`. |
| `ARTIFACT_SCHEMA_VERSION` (research) | `1.0.0` | `research/artifacts.py:ARTIFACT_SCHEMA_VERSION` | The raw retrieval artifact (`research_runs.raw_response_path`). |
| `MODEL_OUTPUT_SCHEMA_VERSION` | `1.0.0` | `forecast/artifacts.py:MODEL_OUTPUT_SCHEMA_VERSION` | The raw model-output artifact (`forecast_records.raw_output_path`). |
| `SNAPSHOT_SCHEMA_VERSION` | `1.0.0` | `metaculus/snapshots.py:SNAPSHOT_SCHEMA_VERSION` | Question snapshot files written by `questions fetch` and the tournament worker. |
| `EXPORT_SCHEMA_VERSION` | `1` | `export.py:EXPORT_SCHEMA_VERSION` | The export contract below. |
| `REPORT_SCHEMA_VERSION` | `1` | `report.py:REPORT_SCHEMA_VERSION` | The report contract below. |

The prompt's own version (the H1 of the prompt file) and each score's
`implementation_version` are data, stored per row, not constants: see
`forecast_records.prompt_version` and the score metrics table.

## Conventions

- **Timestamps** are TEXT, ISO-8601 UTC. Ordered columns written by the lifecycle writers use
  the fixed-width form `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, pinned by trigger GLOBs so they
  sort as text.
- **Identifiers** (record, attempt, run, reservation, release, reconciliation, tournament
  ids) are non-blank text of at most 200 characters with no NUL; "blank" is the 29 code
  points where Python's `str.isspace()` is true, not SQLite's one-argument `trim()` (006).
- **Digests** (`*_sha256`) are 64 lowercase hex characters of a SHA-256 over the rule the
  owning module documents.
- **Booleans** are INTEGER `0`/`1` under a CHECK.
- **Mutability classes**, per table:
  - `append-only`: a BEFORE UPDATE and a BEFORE DELETE trigger refuse every change.
  - `annotatable`: every DELETE is refused and the identity columns are frozen, but the
    columns a run or document is *completed* with stay writable (003).
  - `unguarded`: no trigger; written only by the migration runner.

## Ledger tables

### `approval_events`

**Introduced:** `001`. **Mutability:** append-only. One approval or rejection decision about
one forecast record. An approval binds to the exact forecast hash and the exact payload it
authorized.

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `event_id` | INTEGER | 001 | Row identity (rowid primary key); append order. |
| `forecast_record_id` | TEXT | 001 | The `forecast_records.record_id` decided on. |
| `decision` | TEXT | 001 | `approved` or `rejected` (`lifecycle.py:ApprovalDecision`). |
| `actor` | TEXT | 001 | Who decided, verbatim: an operator's `--actor`, or `policy:<policy-id>:<hash>` for the launch policy. |
| `forecast_sha256` | TEXT | 001 | The record hash the decision binds to; must equal the record's stored `forecast_sha256` (003). |
| `note` | TEXT | 001 | Optional free-text note. |
| `created_at_utc` | TEXT | 001 | When the decision was recorded. |
| `payload_sha256` | TEXT | 011 | Digest of the submission payload an approval authorized. Required on `approved`, forbidden on `rejected`; NULL on an approval means one written before 011, which authorizes no post (D34). |

### `forecast_records`

**Introduced:** `001`. **Mutability:** append-only. One immutable forecast version. The row
is a projection of `record_json`, and `forecast/store.py:read_forecast_record` refuses a row
whose columns disagree with it or whose hash does not match.

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `record_id` | TEXT | 001 | Record identity: a UUIDv7 (time-ordered) minted by `forecast/store.py:mint_record_id`. |
| `question_id` | INTEGER | 001 | Metaculus question id. |
| `post_id` | INTEGER | 001 | Metaculus post id (a group's subquestions share one). |
| `tournament_id` | TEXT | 001 | The tournament or project the forecast was made for (a numeric project id such as `33125`, or a slug). |
| `forecast_version` | INTEGER | 001 | Version within `(question_id, tournament_id)`, from 1; UNIQUE with them. |
| `parent_record_id` | TEXT | 001 | NULL for version 1; otherwise the record of the previous version of the same question and tournament (007). |
| `question_type` | TEXT | 001 | `binary`, `multiple_choice`, `numeric` or `discrete` (`config.py:SupportedQuestionType`; `discrete` since 013). |
| `question_domain` | TEXT | 001 | Free-form, caller-supplied domain tag; never derived from Metaculus categories (M1-201). NULL on every live record. |
| `status` | TEXT | 001 | Status at creation; always `draft` (003). The current status is derived from `lifecycle_events`. |
| `model_provider` | TEXT | 001 | Model provider (`record_json.model_settings.provider`). |
| `model_name` | TEXT | 001 | Model identity as called (`record_json.model_settings.name`). |
| `prompt_version` | TEXT | 001 | The prompt file's version, from its H1 (M1-401). |
| `prompt_sha256` | TEXT | 001 | Digest of the prompt text the model was sent (M1-401). |
| `retrieval_run_id` | TEXT | 001 | The `research_runs` row the forecast's evidence came from. |
| `generated_at_utc` | TEXT | 001 | When the pipeline produced the forecast. |
| `final_prediction_json` | TEXT | 001 | Canonical JSON of `record_json.forecast.final_prediction`: the forecast that is scored, and for binary and multiple choice the posted payload verbatim (D36). |
| `record_json` | TEXT | 001 | Canonical JSON of the whole `ForecastRecord` (`RECORD_SCHEMA_VERSION`): question, model settings, sources, the model's structured response, community-prediction placeholder (`used_as_model_input` is always false). |
| `created_at_utc` | TEXT | 001 | When the row was written. |
| `forecast_sha256` | TEXT | 003 | SHA-256 of `record_json` under the canonical rule (`forecast/record.py:record_sha256`). Approvals and evidence-gap markers bind to it. |
| `attempt_id` | TEXT | 004 | The pipeline attempt that produced this version; links to `pipeline_failure_events` of earlier failed tries. |
| `raw_output_path` | TEXT | 008 | Relative path of the raw model-output artifact under `storage.artifact_root`, or NULL when none was retained. |
| `cost_usd` | REAL | 008 | Model spend recorded with the forecast. NULL means **unknown, not free** (M1-303). |
| `model_invocations` | INTEGER | 008 | Billable model calls made for this forecast; NULL only on rows from before 008. |

### `lifecycle_events`

**Introduced:** `003`. **Mutability:** append-only. One state transition of one forecast
record. Each event links exactly one detail row, chosen by its type.

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `event_id` | INTEGER | 003 | Row identity; global append order. |
| `forecast_record_id` | TEXT | 003 | The record that moved. Declared nullable, required by trigger. |
| `event_seq` | INTEGER | 003 | Per-record sequence, contiguous from 1; UNIQUE with the record, so a missing event is detectable. |
| `event_type` | TEXT | 003 | One of the eleven `lifecycle.py:LifecycleEventType` members (see the state machine). |
| `from_status` | TEXT | 003 | The record's status before the event. |
| `to_status` | TEXT | 003 | The record's status after the event. |
| `detail_code` | TEXT | 003 | Why a failure or uncertainty happened (`lifecycle.py:FailureCode`); NULL on other events. |
| `approval_event_id` | INTEGER | 003 | The `approval_events` row an `approved`/`rejected` event cites. |
| `submission_attempt_id` | TEXT | 003 | The `submission_attempts` row a submission event cites. |
| `submission_verification_id` | INTEGER | 003 | The `submission_verifications` row a `submission_confirmed`/`submission_disconfirmed` event cites. |
| `resolution_event_id` | INTEGER | 003 | The `resolution_events` row a `resolved` event cites. |
| `score_event_id` | INTEGER | 003 | The first `score_events` row a `scored` event cites. |
| `occurred_at_utc` | TEXT | 003 | When the transition happened. |
| `created_at_utc` | TEXT | 003 | When the row was written. |
| `submission_reconciliation_id` | TEXT | 016 | The `submission_reconciliations` row a reconciled `submission_confirmed` event cites. |

### `pipeline_failure_events`

**Introduced:** `004`. **Mutability:** append-only. A failure before any forecast record
existed, scoped to the pipeline attempt rather than to a record.

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `event_id` | INTEGER | 004 | Row identity; append order. |
| `attempt_id` | TEXT | 004 | The pipeline attempt; reused across its retries and stamped onto the record if one later succeeds. |
| `event_seq` | INTEGER | 004 | Per-attempt sequence; UNIQUE with `attempt_id`. |
| `question_id` | INTEGER | 004 | The question being forecast. |
| `tournament_id` | TEXT | 004 | The tournament it was forecast for. |
| `event_type` | TEXT | 004 | `research_failed` or `generation_failed` (`lifecycle.py:PreForecastEventType`). |
| `detail_code` | TEXT | 004 | Why (`lifecycle.py:PreForecastFailureCode`). |
| `retrieval_run_id` | TEXT | 004 | The run involved: required for `generation_failed`, optional for `research_failed`. |
| `occurred_at_utc` | TEXT | 004 | When the failure happened. |
| `created_at_utc` | TEXT | 004 | When the row was written. |

### `research_documents`

**Introduced:** `001`. **Mutability:** annotatable. One retrieved document. Identity,
URL, content hash, retrieval time, provenance and source type are fixed at creation.

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `document_id` | TEXT | 001 | Document identity. |
| `retrieval_run_id` | TEXT | 001 | The run that retrieved it. |
| `canonical_url` | TEXT | 001 | URL after canonicalization for deduplication (M1-305). |
| `title` | TEXT | 001 | Title as the provider reported it. |
| `publisher` | TEXT | 001 | Publisher as reported. |
| `author` | TEXT | 001 | Author as reported. |
| `published_at_utc` | TEXT | 001 | Publication time as reported (a claim for `llm_reported` documents). |
| `updated_at_utc` | TEXT | 001 | Last-update time as reported. |
| `retrieved_at_utc` | TEXT | 001 | When the pipeline retrieved it. |
| `source_type` | TEXT | 001 | `news`, `web`, `official`, `structured` or `social` (`research/model.py:SourceType`). |
| `content_sha256` | TEXT | 001 | Digest of the normalized content; UNIQUE with the run and canonical URL. Forecast citations carry it. |
| `snippet` | TEXT | 001 | Short excerpt. |
| `summary` | TEXT | 001 | Summary, where the provider gave one. |
| `raw_artifact_path` | TEXT | 001 | Path of the raw provider artifact the document came from. |
| `reliability_tag` | TEXT | 001 | Source-trust tag (`research/model.py:ReliabilityTag`); required on `social` documents. |
| `original_url` | TEXT | 002 | The URL exactly as the provider returned it. |
| `provenance` | TEXT | 002 | `direct_api` (fetched by the pipeline) or `llm_reported` (reported by a research agent; content and timestamps are claims). |

### `research_runs`

**Introduced:** `001`. **Mutability:** annotatable. One retrieval run by one provider for one
question. Started, then completed; identity, provider, question and start time are fixed.

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `retrieval_run_id` | TEXT | 001 | Run identity. |
| `provider` | TEXT | 001 | `asknews`, `exa`, `structured` or `xai_x_search` (`research/model.py:RetrievalProvider`). |
| `provider_config_json` | TEXT | 001 | The provider settings the run used, as JSON. |
| `queries_json` | TEXT | 001 | The queries sent, as JSON. |
| `started_at_utc` | TEXT | 001 | When the run started. |
| `completed_at_utc` | TEXT | 001 | When it completed; NULL while running or if it never finished. |
| `freshness_cutoff_utc` | TEXT | 001 | The freshness cutoff applied to its documents. |
| `raw_response_path` | TEXT | 001 | Path of the raw retrieval artifact (research `ARTIFACT_SCHEMA_VERSION`). |
| `error_summary` | TEXT | 001 | A sanitized summary of why the run failed, or NULL. |
| `cost_usd` | REAL | 001 | Provider spend for the run; NULL means unknown, not free. AskNews runs are NULL (credits carry no dollar figure). |
| `created_at_utc` | TEXT | 001 | When the row was written. |
| `agent_model` | TEXT | 002 | The research agent's model, for agent providers (`xai_x_search`). |
| `posts_dropped_no_url` | INTEGER | 002 | Agent-reported posts dropped for lacking a resolvable URL (non-negative). |
| `question_id` | INTEGER | 002 | The question the run researched; required on new rows. |
| `documents_dropped` | INTEGER | 005 | Results that could not be normalized into a document (non-negative). |
| `duplicates_collapsed` | INTEGER | 005 | Repeats of one article collapsed within the run (non-negative). |

### `resolution_events`

**Introduced:** `001`. **Mutability:** append-only. One *observation* of one question's
resolution state, recorded against one forecast record. The current resolution is the
record's latest row; a changed observation is appended, never written over (M4-801).

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `event_id` | INTEGER | 001 | Row identity; the highest per record is the current observation. |
| `question_id` | INTEGER | 001 | The question observed; must match the record's. |
| `forecast_record_id` | TEXT | 001 | The record the observation is recorded against. |
| `resolution_snapshot_json` | TEXT | 001 | Canonical JSON of the classified `ResolutionObservation` (`OBSERVATION_SCHEMA_VERSION`). |
| `outcome` | TEXT | 001 | The normalized outcome (`yes`/`no`, an option label, or a number as text) when the kind is `resolved`; else NULL. |
| `annulled` | INTEGER | 001 | 1 when the kind is `annulled`. |
| `ambiguous` | INTEGER | 001 | 1 when the kind is `ambiguous`. |
| `source_response` | TEXT | 001 | The raw post payload the platform returned, as canonical JSON (the platform's scores for this account are read from it). |
| `ingested_at_utc` | TEXT | 001 | When the row was written. |
| `post_id` | INTEGER | 014 | The post observed. |
| `question_type` | TEXT | 014 | The question type the observation was classified under. |
| `resolution_kind` | TEXT | 014 | The classification (see resolution kinds). |
| `scorable` | INTEGER | 014 | 1 exactly when the kind is `resolved`. |
| `observation_sha256` | TEXT | 014 | Digest of `resolution_snapshot_json`. |
| `source_response_sha256` | TEXT | 014 | Digest of `source_response`. |
| `observed_at_utc` | TEXT | 014 | When the platform state was observed. |

### `schema_migrations`

**Introduced:** `001`. **Mutability:** unguarded. The migration runner's own bookkeeping.

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `version` | INTEGER | 001 | Migration number applied. |
| `applied_at_utc` | TEXT | 001 | When it was applied. |
| `checksum` | TEXT | 001 | SHA-256 of the migration file; a mismatch refuses the ledger. |

### `score_events`

**Introduced:** `001`. **Mutability:** append-only. One score of one forecast record against
one resolution observation. Written only when the record's latest observation is `resolved`
(014) and only citing that latest observation (015); re-scoring is idempotent on
`(record, resolution, metric, implementation_version)`.

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `event_id` | INTEGER | 001 | Row identity; append order. |
| `forecast_record_id` | TEXT | 001 | The record scored. |
| `metric` | TEXT | 001 | The metric (see score metrics); its prefix is its provenance. |
| `value` | REAL | 001 | The finite score value. |
| `implementation_version` | TEXT | 001 | `<metric>/<n>` for local scores, `<metric>/metaculus_score_data/<n>` for platform scores. |
| `comparison_baseline` | TEXT | 001 | `peer` or `baseline` for a platform score; always NULL for a local score. |
| `computed_at_utc` | TEXT | 001 | When the row was computed; never before the observation. |
| `resolution_event_id` | INTEGER | 015 | The `resolution_events` row the score was computed against. |

### `submission_attempts`

**Introduced:** `001`. **Mutability:** append-only. One post of a forecast to the platform,
and what happened.

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `attempt_id` | TEXT | 001 | Attempt identity. |
| `forecast_record_id` | TEXT | 001 | The record posted. |
| `idempotency_key` | TEXT | 001 | The key the post was made under (`KEY_SCHEMA_VERSION`); UNIQUE. |
| `requested_at_utc` | TEXT | 001 | When the post was sent. |
| `completed_at_utc` | TEXT | 001 | When it returned. |
| `request_payload_sha256` | TEXT | 001 | Digest of the payload sent; must be the approved `payload_sha256`. |
| `http_status` | INTEGER | 001 | The HTTP status, when a response arrived. |
| `response_body` | TEXT | 001 | The response body, size-limited and redacted at write time (M1-605). |
| `response_headers` | TEXT | 001 | The response headers, redacted at write time. |
| `success` | INTEGER | 001 | 1 when the post returned success. |
| `error_type` | TEXT | 001 | Exception class name on failure. |
| `error_message` | TEXT | 001 | Redacted error message on failure. |
| `verified_by_refetch` | INTEGER | 001 | 1 exactly when `refetch_outcome` is `confirmed`. |
| `refetched_forecast_snapshot` | TEXT | 001 | The refetch envelope (`VERIFICATION_SCHEMA_VERSION`). |
| `created_at_utc` | TEXT | 001 | When the row was written. |
| `refetch_outcome` | TEXT | 009 | What the attempt's own refetch saw: `confirmed`, `absent`, `mismatched` or `unreadable` (`lifecycle.py:RefetchOutcome`). |

### `submission_key_releases`

**Introduced:** `010`. **Mutability:** append-only. The resolution of a key reservation that
was never spent: at most one per reservation.

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `release_id` | TEXT | 010 | Release identity. |
| `reservation_id` | TEXT | 010 | The reservation released; UNIQUE. |
| `reason` | TEXT | 010 | `not_posted` (the program knows no post was made) or `operator_abandoned` (a person checked and asserts none landed) (`submission.py:ReservationReason`). |
| `released_by` | TEXT | 010 | Who asserted it, verbatim, for `operator_abandoned`. |
| `note` | TEXT | 010 | Optional note. |
| `released_at_utc` | TEXT | 010 | When it was released; not before the reservation. |
| `created_at_utc` | TEXT | 010 | When the row was written. |

### `submission_key_reservations`

**Introduced:** `010`. **Mutability:** append-only. A claim on an idempotency key taken
*before* a post, so an interrupted post cannot be blindly repeated.

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `reservation_id` | TEXT | 010 | Reservation identity. |
| `idempotency_key` | TEXT | 010 | The key claimed. |
| `forecast_record_id` | TEXT | 010 | The record the post is for. |
| `reservation_seq` | INTEGER | 010 | Per-key sequence; UNIQUE with the key. |
| `reserved_at_utc` | TEXT | 010 | When it was claimed. |
| `created_at_utc` | TEXT | 010 | When the row was written. |

### `submission_reconciliations`

**Introduced:** `016`. **Mutability:** append-only. A live post that reached the platform but
was never recorded, and the evidence that it did (M2-713).

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `reconciliation_id` | TEXT | 016 | Reconciliation identity. |
| `reservation_id` | TEXT | 016 | The reservation the post was made under; UNIQUE. |
| `forecast_record_id` | TEXT | 016 | The record posted. |
| `attempt_id` | TEXT | 016 | The attempt identity the post would have carried; no attempt row may hold it. |
| `request_payload_sha256` | TEXT | 016 | Digest of the payload posted; the approved one. |
| `intent_event_id` | TEXT | 016 | The `forecast_intent` journal row committed before the post. |
| `artifact_path` | TEXT | 016 | Path of the captured submission artifact, when one exists. |
| `artifact_sha256` | TEXT | 016 | Its digest. |
| `observed_by` | TEXT | 016 | The person asserting the post landed. |
| `note` | TEXT | 016 | What they saw; required. |
| `refetched_at_utc` | TEXT | 016 | When the program's own confirming refetch ran. |
| `refetched_forecast_snapshot` | TEXT | 016 | What that refetch saw; its outcome must be `confirmed`. |
| `created_at_utc` | TEXT | 016 | When the row was written. |

### `submission_verifications`

**Introduced:** `003`. **Mutability:** append-only. A later, standalone refetch that settled
an uncertain attempt.

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `verification_id` | INTEGER | 003 | Row identity. |
| `submission_attempt_id` | TEXT | 003 | The uncertain attempt it settles. |
| `outcome` | TEXT | 003 | `confirmed` or `absent` (`lifecycle.py:VerificationOutcome`). |
| `observed_at_utc` | TEXT | 003 | When the refetch observed the platform. |
| `refetched_forecast_snapshot` | TEXT | 003 | What the refetch saw. |
| `created_at_utc` | TEXT | 003 | When the row was written. |

### `tournament_events`

**Introduced:** `012`. **Mutability:** append-only. The tournament worker's journal:
activation, budget reservations and settlements, heartbeats, intents and receipts, evidence
gaps. Read through `tournament_state.py:events`.

| Column | Type | Since | Definition |
| --- | --- | --- | --- |
| `seq` | INTEGER | 012 | Row identity; the journal's append order. |
| `event_id` | TEXT | 012 | A random token naming the row; UNIQUE. |
| `kind` | TEXT | 012 | What happened (see journal kinds). |
| `scope` | TEXT | 012 | What it happened to: a record id, an `account:tournament` budget scope, a question fingerprint, or `worker`. |
| `data` | TEXT | 012 | A JSON object (CHECK `json_valid`) whose shape is its kind's writer's; redacted at write time (M1-613). |
| `created_at_utc` | TEXT | 012 | When the row was written. |

**Journal kinds**, for orientation. This list is informative, not a published contract: the
journal is the worker's operational state and each kind's `data` is defined by the code that
writes it. One kind is read by the report and is part of *its* contract: `evidence_gap`,
scoped to a record id, whose `code` is `named_source_absent` (M1-327) or `evidence_poor`
(M1-349) and whose `forecast_sha256` must be the record's.

`activation`, `disabled`, `heartbeat`, `witness`, `question_started`, `question_blocked`,
`question_failure`, `retry_wait`, `research_checkpoint`, `retrieval_started`,
`retrieval_completed`, `model_started`, `model_completed`, `model_response`,
`cost_reserved`, `cost_settled`, `cost_settled_id`, `cost_corrected`, `cost_corrected_id`,
`forecast_intent`, `forecast_confirmed`, `comment_intent`, `comment_receipt`,
`comment_confirmed`, `evidence_gap`, `restored_question_hold`, `restored_spending_hold`,
`restore_reconciled`.

## Lifecycle

A record is created `draft` and moves only by appending a `lifecycle_events` row. The legal
transitions are exactly these (`lifecycle.py:_LEGAL_TRANSITIONS`, and again as a trigger in
003/009/016); `failed` is terminal, and a retry is a new forecast version.

| Event | From | To |
| --- | --- | --- |
| `validated` | `draft` | `validated` |
| `validation_failed` | `draft` | `failed` |
| `validation_failed` | `validated` | `failed` |
| `rejected` | `validated` | `validated` |
| `approved` | `validated` | `approved` |
| `submitted` | `approved` | `submitted` |
| `submission_uncertain` | `approved` | `approved` |
| `submission_failed` | `approved` | `failed` |
| `submission_confirmed` | `approved` | `submitted` |
| `submission_disconfirmed` | `approved` | `failed` |
| `resolved` | `submitted` | `resolved` |
| `scored` | `resolved` | `scored` |

- `rejected` records a decision without moving the record; `submission_uncertain` leaves it
  `approved` so a later refetch (`submission_confirmed`/`submission_disconfirmed`) or a
  reconciliation can still move it.
- Only `resolved`, `annulled` and `ambiguous` observations move a `submitted` record to
  `resolved`; a `withheld` or `unresolved` observation is recorded without an event.
- A re-resolution or a new score implementation after `scored` appends score rows and no
  event (M4-802).
- Failures before a record exists are `pipeline_failure_events`, not lifecycle events.

### Immutable versions

A forecast is never edited. A new forecast for the same question and tournament is a new
`forecast_records` row with `forecast_version` one higher and `parent_record_id` naming the
previous version (007); version 1 is the root. Approval binds to one version's
`forecast_sha256` and one payload's `payload_sha256`, so any content change needs a new
approval (D34). Every stored record re-attests itself on read: its `record_json` must
re-render to the same bytes and hash, and its columns must equal its projection.

## Resolution kinds

`resolution_events.resolution_kind` (`resolution.py:ResolutionKind`).

| Kind | Scorable | Moves the record to resolved | Meaning |
| --- | --- | --- | --- |
| `resolved` | yes | yes | The platform shows a definite outcome. The only scorable kind. |
| `annulled` | no | yes | The platform annulled the question. |
| `ambiguous` | no | yes | The platform resolved it ambiguous. |
| `withheld` | no | no | Status `resolved` with a null value: the API masks the outcome from an account that did not predict. |
| `unresolved` | no | no | A question with an earlier observation that is no longer resolved: a retraction. Refused as a first observation. |

## Score metrics

`score_events.metric`. The prefix is the provenance: `local_` is this program's own
arithmetic (M4-802, D36), `platform_` is Metaculus's own number for this account, copied
from the stored observation (M4-803, D42). The two are never mixed in one summary, and only
the platform scores compare across question types. A *peer* score is measured against the
other forecasters; it is a score of this account's forecast, never a forecaster input.

| Metric | Provenance | Question types | Comparison baseline | Implementation version |
| --- | --- | --- | --- | --- |
| `local_brier_binary` | local | binary | none | `local_brier_binary/1` |
| `local_log_binary` | local | binary | none | `local_log_binary/1` |
| `local_brier_multiclass` | local | multiple_choice | none | `local_brier_multiclass/1` |
| `local_log_multiclass` | local | multiple_choice | none | `local_log_multiclass/1` |
| `platform_spot_peer_score` | platform | all | `peer` | `platform_spot_peer_score/metaculus_score_data/1` |
| `platform_spot_baseline_score` | platform | all | `baseline` | `platform_spot_baseline_score/metaculus_score_data/1` |
| `platform_peer_score` | platform | all | `peer` | `platform_peer_score/metaculus_score_data/1` |
| `platform_baseline_score` | platform | all | `baseline` | `platform_baseline_score/metaculus_score_data/1` |

Local Brier is `(p - o)^2` for binary and the sum of squared errors over options for multiple
choice; local log is the natural log of the probability on the realized outcome, refused
rather than clamped at zero. `spot_peer` is MiniBench's ranking score.

## Export contract (`EXPORT_SCHEMA_VERSION` 1)

`whiskeyjack-bot export --config PATH --format jsonl|parquet [--output DIR]`
(`export.py:export_ledger`). Opens the ledger read-only and reads every table in one
snapshot; never mutates the `.db` or `-wal`.

- **Files:** one `<table>.jsonl` or `<table>.parquet` per ledger table -- all fifteen tables
  above, `schema_migrations` included -- plus `manifest.json`, written last. A directory
  without a manifest is not an export. Nothing is overwritten.
- **Rows:** every column of every row, as `export.py:EXPORTED_TABLES` declares them, read
  `ORDER BY` the table's identifier. JSONL is one canonical JSON object per line (sorted keys, ASCII,
  no NaN) and byte-stable across runs; Parquet is typed from the declared column types and
  byte-stable for a fixed pyarrow. JSON-in-TEXT columns (`record_json`, `data`, ...) are
  exported as the stored string, not parsed.
- **Refused, never coerced:** a value whose storage class disagrees with its declared type,
  a non-finite REAL, a BLOB, or text that is not UTF-8.
- **Secrets:** redaction happens at write time in the ledger; the export adds no second pass.

`manifest.json` fields:

| Field | Definition |
| --- | --- |
| `export_schema_version` | `EXPORT_SCHEMA_VERSION`. |
| `ledger_schema_version` | The ledger's applied schema version. |
| `format` | `jsonl` or `parquet`. |
| `exported_at_utc` | When the export ran, UTC, `Z`-suffixed. |
| `writer` | Writer versions the bytes depend on; empty for JSONL. |
| `writer.pyarrow` | The pyarrow version that wrote a Parquet export. |
| `tables` | One entry per exported table, in name order. |
| `tables[].name` | Table name. |
| `tables[].columns` | Column names in DDL order (the row objects themselves sort their keys). |
| `tables[].identifier` | The column rows are ordered by. |
| `tables[].file` | The file holding the table. |
| `tables[].row_count` | Rows exported. |
| `tables[].sha256` | Digest of the file's bytes. |

## Report contract (`REPORT_SCHEMA_VERSION` 1)

`whiskeyjack-bot report --config PATH [--output DIR]` (`report.py:write_report`, M5-804). The
attribution report dataset: outcomes grouped by the attribution axes, with counts,
calibration bins and score summaries, every cell carrying its `n` and a small-sample flag.

- **Derived and replayable.** Opens the ledger read-only, reads in one snapshot, and reads
  every fact through the ledger's verified readers (a record's hash, an observation's two
  digests and every score's value are re-checked; a row that fails is refused, never
  repaired). `records.jsonl` and `report.json` carry no timestamp, so a report of the same
  ledger -- or of a backup copy of it -- is byte-identical.
- **Files:** `records.jsonl`, `report.json`, then `manifest.json`, written last. Nothing is
  overwritten; a render refusal leaves no files.
- **Population.** Every forecast record appears in `records.jsonl`. Records whose
  `tournament_id` is in `report.py:TEST_TOURNAMENTS` (`bot-testing-area`, `minibench`) are
  marked `included: false` with `exclusion: test_tournament`, counted, and excluded from every
  summary (D43).
- **One state per record** (`report.py:RecordState`), by precedence:
  1. `not_posted`: the record never reached `submitted`.
  2. `superseded`: a later posted version of the same question exists **in the same
     population** (included or excluded). Platform scores are per question, so only the latest
     posted version is scored; an excluded record never supersedes an included one. Unreachable
     within one tournament through the submission path today (012's whole-question
     reservation guard).
  3. By the latest observation: `awaiting_resolution` (none), `withheld`, `unresolved`,
     `annulled`, `ambiguous`, `resolved_unscored` (resolved, no score row cites it), or
     `scored` (resolved, and at least one score row cites it).
- **Axes** (`report.py:AXES`): `all`, `tournament_id`, `question_type`, `model`, `prompt`,
  `question_domain` (as stored), and three **overlapping** axes on which a record sits in
  several groups at once -- `source_category` (Metaculus category, keyed on id, M1-201),
  `reasoning_strategy_tag`, `evidence_gap` -- whose group counts do not sum to the population
  and are never added. A record with no value on an overlapping axis is in its null group.
- **Score cells** are one `(metric, implementation_version)` each, over `scored` records and
  the score rows citing their latest observation only. Rows citing an older observation are
  counted as stale and summarized nowhere. `sum`/`mean` use `math.fsum` (order-independent),
  `mean` is clamped into `[min, max]`, `-0.0` reads as `0.0`, `sample_sd` uses `n - 1`, and a
  statistic that is not a finite number refuses the report.
- **Calibration** is binary only: records whose latest observation is `resolved` (states
  `scored` and `resolved_unscored`), in ten fixed bins over `report.py:CALIBRATION_BIN_EDGES`.
  Bins are left-closed, `[lower, upper)`, the last closed at 1.0; the edges are the doubles
  nearest the decimal tenths, and a probability equal to an edge lies in the bin that edge
  opens (0.3 lies in `[0.3, 0.4)`). Multiple choice, numeric and discrete calibration is out of
  scope.
- **Small samples.** Every score cell, calibration block and bin carries `n` and
  `small_sample` (`n < report.py:SMALL_SAMPLE_THRESHOLD`, 30). It is a flag on a descriptive
  number, not a significance test.
- **Cost** is `forecast_records.cost_usd` only: known and unknown counts and the sum of the
  known. Budget reservations and settlements (the journal) are not attributed per record.
- **Never shown:** the community aggregate.

Paths below use `[]` for an array element. Keys under a `states` object are the nine
`RecordState` members, written `<state>`; keys under `excluded` are the `report.py:Exclusion`
members, written `<exclusion>`.

### `records.jsonl`

| Field | Definition |
| --- | --- |
| `record_id` | The forecast record. |
| `question_id` | Metaculus question id. |
| `post_id` | Metaculus post id. |
| `tournament_id` | The record's tournament. |
| `forecast_version` | The record's version. |
| `parent_record_id` | The previous version's record, or null. |
| `forecast_sha256` | The record's verified hash: the input this row was computed from. |
| `generated_at_utc` | When the forecast was produced. |
| `question_type` | The question type. |
| `question_domain` | The stored domain tag, or null. |
| `source_categories` | Metaculus categories of the question, sorted by id. |
| `source_categories[].id` | Category id (the key). |
| `source_categories[].slug` | Category slug, or null. |
| `model_provider` | Model provider. |
| `model_name` | Model identity. |
| `prompt_version` | Prompt version. |
| `prompt_sha256` | Prompt digest. |
| `reasoning_strategy_tags` | The response's reasoning tags, sorted. |
| `evidence_gaps` | Distinct evidence-gap codes bound to this record's hash, sorted. |
| `lifecycle_status` | Current derived lifecycle status. |
| `probability_yes` | The forecast probability for a binary record; null otherwise. |
| `resolution` | The latest observation, or null. |
| `resolution.event_id` | Its `resolution_events` row. |
| `resolution.kind` | Its kind. |
| `resolution.outcome` | Its outcome, or null. |
| `resolution.observation_sha256` | Its snapshot digest. |
| `resolution.source_response_sha256` | Its payload digest. |
| `resolution.observed_at_utc` | When it was observed. |
| `scores` | Verified score rows citing the latest observation, sorted by metric and version. |
| `scores[].metric` | The metric. |
| `scores[].implementation_version` | Its implementation version. |
| `scores[].provenance` | `local` or `platform`. |
| `scores[].comparison_baseline` | `peer`, `baseline`, or null for a local score. |
| `scores[].value` | The value. |
| `scores[].event_id` | Its `score_events` row. |
| `scores[].resolution_event_id` | The observation it was computed against. |
| `stale_score_rows` | Score rows citing an older observation. |
| `model_cost_usd` | `forecast_records.cost_usd`, or null (unknown). |
| `model_invocations` | `forecast_records.model_invocations`, or null. |
| `included` | False for an excluded record. |
| `exclusion` | `test_tournament`, or null. |
| `state` | The record's `RecordState`. |

### `report.json`

| Field | Definition |
| --- | --- |
| `report_schema_version` | `REPORT_SCHEMA_VERSION`. |
| `ledger_schema_version` | The ledger schema version read. |
| `records_sha256` | Digest of `records.jsonl`: the rows the summaries were computed from. |
| `parameters` | The report's fixed parameters. |
| `parameters.calibration_bin_edges` | The eleven bin edges. |
| `parameters.calibration_bin_rule` | The edge rule, in words. |
| `parameters.calibration_question_types` | Question types calibrated (`binary`). |
| `parameters.small_sample_threshold` | The small-sample threshold (30). |
| `parameters.excluded_tournaments` | `TEST_TOURNAMENTS`. |
| `parameters.states` | The state vocabulary, in precedence order. |
| `population` | Counts over every record. |
| `population.records` | Every record in the ledger. |
| `population.excluded` | Excluded records by reason. |
| `population.excluded.<exclusion>` | Records excluded for that reason. |
| `population.included` | Records summarized. |
| `population.states` | Included records by state; sums to `included`. |
| `population.states.<state>` | Included records in that state. |
| `axes` | One block per axis, in `AXES` order. |
| `axes[].axis` | The axis. |
| `axes[].overlapping` | True when a record can sit in several groups; never total those groups. |
| `axes[].groups` | The axis's groups, sorted by canonical key. |
| `axes[].groups[].key` | The group's identity: `{}` for `all`, else the fields below. |
| `axes[].groups[].key.tournament_id` | `tournament_id` axis. |
| `axes[].groups[].key.question_type` | `question_type` axis. |
| `axes[].groups[].key.model_provider` | `model` axis. |
| `axes[].groups[].key.model_name` | `model` axis. |
| `axes[].groups[].key.prompt_version` | `prompt` axis. |
| `axes[].groups[].key.prompt_sha256` | `prompt` axis. |
| `axes[].groups[].key.question_domain` | `question_domain` axis; null groups the unassigned. |
| `axes[].groups[].key.category_id` | `source_category` axis; null groups records with none. |
| `axes[].groups[].key.tag` | `reasoning_strategy_tag` axis; null groups records with none. |
| `axes[].groups[].key.code` | `evidence_gap` axis; null groups records with no gap. |
| `axes[].groups[].labels` | Human labels: every slug seen for a category id; empty elsewhere. |
| `axes[].groups[].records` | Included records in the group. |
| `axes[].groups[].states` | The group's records by state; sums to `records`. |
| `axes[].groups[].states.<state>` | Records in that state. |
| `axes[].groups[].scores` | Score cells present in the group. |
| `axes[].groups[].scores[].metric` | The metric. |
| `axes[].groups[].scores[].implementation_version` | Its implementation version. |
| `axes[].groups[].scores[].provenance` | `local` or `platform`. |
| `axes[].groups[].scores[].comparison_baseline` | `peer`, `baseline`, or null. |
| `axes[].groups[].scores[].n` | Scores summarized. |
| `axes[].groups[].scores[].sum` | Their `fsum`. |
| `axes[].groups[].scores[].mean` | Their mean. |
| `axes[].groups[].scores[].min` | Their minimum. |
| `axes[].groups[].scores[].max` | Their maximum. |
| `axes[].groups[].scores[].sample_sd` | Their sample standard deviation, or null below two. |
| `axes[].groups[].scores[].small_sample` | `n` below the threshold. |
| `axes[].groups[].calibration` | Binary calibration over the group. |
| `axes[].groups[].calibration.n` | Records calibrated. |
| `axes[].groups[].calibration.small_sample` | `n` below the threshold. |
| `axes[].groups[].calibration.bins` | The ten bins, in order. |
| `axes[].groups[].calibration.bins[].lower` | Inclusive lower edge. |
| `axes[].groups[].calibration.bins[].upper` | Exclusive upper edge (inclusive for the last bin). |
| `axes[].groups[].calibration.bins[].n` | Records in the bin. |
| `axes[].groups[].calibration.bins[].mean_forecast` | Mean `probability_yes`, or null when empty. |
| `axes[].groups[].calibration.bins[].observed_frequency` | Share that resolved `yes`, or null when empty. |
| `axes[].groups[].calibration.bins[].small_sample` | `n` below the threshold. |
| `axes[].groups[].model_cost` | Model cost of the group's records. |
| `axes[].groups[].model_cost.known` | Records with a known cost. |
| `axes[].groups[].model_cost.unknown` | Records whose cost is unknown. |
| `axes[].groups[].model_cost.known_sum_usd` | `fsum` of the known costs, or null when none is known. |
| `warnings` | Report-level warnings with a positive count, in `report.py:WarningCode` order. |
| `warnings[].code` | `small_sample`, `overlapping_axes`, `unknown_model_cost`, `resolved_unscored` or `stale_score_rows`. |
| `warnings[].count` | Cells, axes, records or rows affected. |
| `warnings[].message` | What to make of it; counts only, never a stored value. |

### `manifest.json`

| Field | Definition |
| --- | --- |
| `report_schema_version` | `REPORT_SCHEMA_VERSION`. |
| `ledger_schema_version` | The ledger schema version read. |
| `generated_at_utc` | When the report ran, UTC, `Z`-suffixed. The only timestamp in a report. |
| `files` | The two data files, in write order. |
| `files[].name` | File name. |
| `files[].rows` | Lines in `records.jsonl`; 1 for `report.json`. |
| `files[].sha256` | Digest of the file's bytes. |
