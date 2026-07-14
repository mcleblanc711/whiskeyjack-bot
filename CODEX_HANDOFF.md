# `whiskeyjack-bot` — Codex implementation handoff

Checked: 2026-07-09 (America/Edmonton)

> Committed 2026-07-14 after recovery (see `docs/M0-REVIEW.md`, deviation 2).
> This document is the canonical full spec. Its "First GitHub issue" section
> predates the split backlog: for scope and sequencing, the backlog under
> `docs/backlog/` is authoritative.

## Objective

Implement a small, observable forecasting pipeline that can compete in the active Metaculus MiniBench while serving primarily as an attribution instrument. The first operational target is one question processed end to end: fetch, normalize, research, forecast, validate, persist the complete pre-submission record, obtain human approval, submit once, and persist the submission receipt.

The repository will be public at `mcleblanc711/whiskeyjack-bot`. Do not place credentials, raw private comments, or unrestricted model traces in version control.

## Verified environment

- Active MiniBench: slug/SDK identifier `minibench`; 59 questions in the current series; runs 2026-06-29 through 2026-07-18; USD 1,000 prize pool. Source: https://www.metaculus.com/tournament/minibench/
- Active seasonal tournament: Summer 2026 FutureEval Bot Tournament; tournament ID `33022`; runs 2026-05-18 through 2026-09-06; USD 50,000 prize pool. Sources: https://www.metaculus.com/tournament/summer-futureeval-2026/ and the maintained SDK source below.
- Package: `forecasting-tools==0.2.92`, released 2026-05-27; Python `>=3.11,<4.0`. Source: https://pypi.org/project/forecasting-tools/
- Authentication: `METACULUS_TOKEN`; the maintained client sends `Authorization: Token <token>`. Source: https://github.com/Metaculus/forecasting-tools/blob/main/forecasting_tools/helpers/metaculus_client.py
- Current package constants: `MetaculusClient.CURRENT_MINIBENCH_ID == "minibench"` and `MetaculusClient.CURRENT_AI_COMPETITION_ID == 33022` on the checked source. Treat both as rotating defaults and allow config overrides.
- Verified question types in the active MiniBench public pages: binary and numeric. Whether this round includes multiple-choice or group posts was not published as a type count. The package supports binary, multiple-choice, numeric, date, conditional and group-question unpacking; v1 supports binary, multiple-choice and numeric, while date/conditional remain deferred.
- Numeric submission: normal numeric questions use a 201-point CDF; `NumericDistribution` standardizes it and caps an inbound PMF step at 0.2 (with 0.95 wiggle room in its validator). Source: https://github.com/Metaculus/forecasting-tools/blob/main/forecasting_tools/data_models/numeric_report.py

## Exact v1 scope

In scope:

1. Config loading and environment validation.
2. Fetch current eligible questions or load a saved question fixture.
3. Normalize binary, multiple-choice and numeric questions.
4. Research through AskNews, with Exa as a configured fallback; direct structured sources may bypass both.
5. One strong configurable forecaster model called through `GeneralLlm`.
6. Strict Pydantic output validation and one bounded repair attempt.
7. Numeric CDF construction with `NumericDistribution.from_question(...)` and `get_cdf()`.
8. Human approval state before any live submission.
9. SQLite attribution ledger with append-only lifecycle events and idempotency keys.
10. Dry-run, no-submit and replay modes.
11. One-question live smoke test after owner approval.
12. Resolution ingestion and binary/multiple-choice local scores after the first submission.

Out of scope:

- Nine-agent Council or any multi-agent debate.
- Continuous Market Pulse updates.
- PaperPolyMkt.
- Local competitive inference.
- Autonomous submission during initial development.
- Date and conditional questions.
- Detailed multi-agent A/B experiment design.
- A hosted database, queue, workflow engine or web UI.

## Package interfaces to use

Pin and code against `forecasting-tools==0.2.92`. Add a weekly dependency check; do not float the production dependency.

```python
from forecasting_tools import (
    GeneralLlm,
    MetaculusClient,
    NumericDistribution,
    Percentile,
)
```

Use these verified interfaces:

```python
client = MetaculusClient()  # reads METACULUS_TOKEN by default

questions = client.get_all_open_questions_from_tournament(
    tournament_id=MetaculusClient.CURRENT_MINIBENCH_ID,
    group_question_mode="unpack_subquestions",
)

question = client.get_question_by_post_id(
    post_id,
    group_question_mode="unpack_subquestions",
)

model = GeneralLlm(model=config.model.name, temperature=config.model.temperature)
typed_output = await model.invoke_and_return_verified_type(
    prompt,
    ExpectedPydanticType,
    allowed_invoke_tries_for_failed_output=2,
)

distribution = NumericDistribution.from_question(
    [Percentile(percentile=p.percentile, value=p.value) for p in output.percentiles],
    question,
)
cdf_values = [point.percentile for point in distribution.get_cdf()]
```

The maintained public posting methods are:

```python
client.post_binary_question_prediction(question_id, prediction_in_decimal)
client.post_multiple_choice_question_prediction(question_id, options_with_probabilities)
client.post_numeric_question_prediction(question_id, cdf_values)
client.post_question_comment(post_id, comment_text, is_private=True, included_forecast=True)
```

Important limitation: all four return `None`. They raise on failure but do not expose the HTTP response. Do not claim that they provide a submission receipt.

## Submission seam

Create a `SubmissionGateway` protocol owned by this repository. Its return type is a sanitized `SubmissionReceipt` containing:

- `attempt_id`
- `forecast_record_id`
- `idempotency_key`
- `requested_at_utc`
- `completed_at_utc`
- `request_payload_sha256`
- `http_status` (nullable)
- `response_body` (JSON/text, nullable and size-limited)
- `response_headers` (allowlisted only)
- `success`
- `error_type` and `error_message`
- `verified_by_refetch`
- `refetched_forecast_snapshot`

Implement two gateways:

1. `DryRunSubmissionGateway`: never calls Metaculus and returns a deterministic dry-run receipt.
2. `MetaculusSubmissionGateway`: initially call the package's public post method, capture success/exception, then refetch the question and verify `previous_forecasts` changed as expected. This is the supported-path implementation.

Before Milestone 2, run a spike to determine whether an exact response body is required and can be captured without depending on a private package method. If yes, add a narrow HTTP adapter using the endpoint and payload shape observed in maintained source, protected by contract tests. Keep the SDK path as the default until that adapter passes the bot-testing-area smoke test. Never post both through the SDK and direct HTTP for the same idempotency key.

## Proposed repository tree

```text
whiskeyjack-bot/
├── pyproject.toml
├── README.md
├── LICENSE
├── .env.example
├── .gitignore
├── config.example.yaml
├── prompts/
│   └── forecaster.md
├── src/whiskeyjack_bot/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── schemas.py
│   ├── metaculus_gateway.py
│   ├── normalize.py
│   ├── retrieval.py
│   ├── structured_sources.py
│   ├── forecaster.py
│   ├── validation.py
│   ├── numeric.py
│   ├── approval.py
│   ├── submission.py
│   ├── ledger.py
│   ├── resolution.py
│   ├── scoring.py
│   └── logging_config.py
├── migrations/
│   └── 001_initial.sql
├── tests/
│   ├── fixtures/
│   ├── unit/
│   ├── integration/
│   └── acceptance/
└── data/
    ├── .gitkeep
    └── README.md
```

Do not split modules further until a file becomes difficult to reason about.

## Configuration schema

Implement a Pydantic settings model matching `config.example.yaml`. Required validation:

- Explicit `model.name`; no silent frontier-model default.
- Live submission requires `submission.enabled=true`, `submission.dry_run=false`, `submission.require_human_approval=true`, a recorded approval, and `METACULUS_TOKEN`.
- `tournament.id` accepts integer or string and defaults to the SDK alias only when `use_sdk_current_id=true`.
- `supported_question_types` may include only `binary`, `multiple_choice`, `numeric` in v1.
- Probability bounds must satisfy `0.001 <= min < max <= 0.999`.
- Research freshness and per-run cost caps must be positive.
- Secrets are environment-variable references, never YAML values.

## Ledger design

SQLite is the v1 source of truth. Use WAL mode, foreign keys and explicit transactions. Store compact indexed scalar fields plus the complete Pydantic record as canonical JSON. Do not store hidden chain-of-thought; store concise rationale summaries, evidence references, priors, adjustments and failure-mode checks.

Minimum tables:

### `forecast_records`

- `record_id TEXT PRIMARY KEY` (UUIDv7/ULID)
- `question_id INTEGER NOT NULL`
- `post_id INTEGER`
- `tournament_id TEXT NOT NULL`
- `forecast_version INTEGER NOT NULL`
- `parent_record_id TEXT`
- `question_type TEXT NOT NULL`
- `question_domain TEXT`
- `status TEXT NOT NULL` (`draft`, `validated`, `approved`, `submitted`, `failed`, `resolved`, `scored`)
- `model_provider TEXT NOT NULL`
- `model_name TEXT NOT NULL`
- `prompt_version TEXT NOT NULL`
- `prompt_sha256 TEXT NOT NULL`
- `retrieval_run_id TEXT NOT NULL`
- `generated_at_utc TEXT NOT NULL`
- `final_prediction_json TEXT NOT NULL`
- `record_json TEXT NOT NULL`
- `created_at_utc TEXT NOT NULL`
- unique `(question_id, tournament_id, forecast_version)`

### `research_runs`

- identity, provider/config, query list, started/completed timestamps, freshness cutoff, raw-response paths, error summary and cost.

### `research_documents`

- `document_id`, `retrieval_run_id`, canonical URL, title, publisher, author, published/updated/retrieved timestamps, source type, content hash, snippet/summary, raw artifact path and reliability tag. Unique `(retrieval_run_id, canonical_url, content_sha256)`.

### `approval_events`

- append-only approval/rejection events, actor, timestamp, forecast hash and note. Approval is valid only for the exact forecast hash.

### `submission_attempts`

- the `SubmissionReceipt` fields above; unique idempotency key. Never overwrite an attempt.

### `resolution_events`

- question resolution snapshot, outcome, annulment/ambiguity flags, source response and ingestion timestamp.

### `score_events`

- metric, value, implementation version, comparison baseline and computed timestamp. Store binary Brier and log scores; multiclass Brier/log where applicable; ingest platform numeric scores rather than claiming an unverified local replica.

Exports are derived artifacts: JSONL for audit/interchange and Parquet for `polyberg-polygraph` analysis. They are not competing sources of truth.

## Canonical forecast record

The `record_json` must include:

- forecast identity/version/parent;
- question, post and tournament identifiers;
- question text, background, resolution criteria, fine print, bounds/options and timestamps;
- domain and question-type tags;
- model/provider/temperature and prompt version/hash;
- retrieval run and normalized source references;
- base-rate statement and source/analogy;
- model prior before current evidence;
- community prediction snapshot and timestamp when technically available, plus `used_as_model_input=false` for the v1 baseline;
- evidence adjustments with direction, magnitude label, source IDs and load-bearing flag;
- load-bearing facts;
- concise reasoning strategy tags and rationale summary;
- failure modes/counterarguments;
- final typed forecast and process confidence;
- validation results and generated CDF when numeric;
- approval state/history;
- submission attempts;
- later resolution and score events.

Repeat forecasts append a new version and reference the previous record. Never mutate an earlier forecast into the new one.

## Required CLI entry points

Use one console entry point, `whiskeyjack-bot`, backed by `argparse` or Typer. These commands are required:

```text
whiskeyjack-bot verify-env --config PATH
whiskeyjack-bot fetch --config PATH [--limit N] [--question-id ID] [--save]
whiskeyjack-bot run --config PATH [--limit N] [--question-id ID] [--dry-run] [--no-submit]
whiskeyjack-bot replay --config PATH --record-id ID [--research] [--model-output]
whiskeyjack-bot show --record-id ID
whiskeyjack-bot approve --record-id ID [--note TEXT]
whiskeyjack-bot reject --record-id ID [--note TEXT]
whiskeyjack-bot submit --record-id ID
whiskeyjack-bot ingest-resolutions --config PATH [--question-id ID]
whiskeyjack-bot score [--record-id ID]
whiskeyjack-bot export --format jsonl|parquet --output PATH
```

`run` writes a validated record before it can request approval. It must never submit implicitly when `require_human_approval=true`; submission is a separate command.

## Pipeline and failure boundaries

```text
config -> fetch/load -> normalize -> retrieve/replay -> forecast/replay
       -> validate -> persist draft -> approve -> submit -> verify/refetch
       -> append receipt -> later resolution -> score -> export/analyze
```

Each arrow is a persisted boundary. Retrying a later phase must not repeat an earlier paid call unless explicitly requested. A failed research source, malformed model response, validation failure, rejected approval, API timeout or verification mismatch must produce a ledger event and leave the last valid record intact.

## Test requirements

Unit tests:

- config validation and secret redaction;
- question normalization for all three v1 types;
- canonical URL/content-hash deduplication;
- freshness policy;
- strict binary bounds and multiple-choice normalization;
- numeric percentile ordering, bounds, 201-point output and PMF-step constraint through `NumericDistribution`;
- forecast versioning and immutable approval hash;
- idempotency-key generation;
- binary and multiclass scoring;
- JSONL/Parquet export round-trip.

Schema tests:

- valid golden records for binary, multiple-choice and numeric;
- malformed model outputs rejected;
- unknown fields either rejected or explicitly versioned;
- database migration from empty file is deterministic.

Mocked integration tests:

- MiniBench fetch with binary/numeric/multiple-choice fixtures;
- group post unpacking;
- AskNews success, stale result, partial error and Exa fallback;
- model timeout and one repair attempt;
- approval required before submission;
- 429/5xx retry and final failure;
- post success followed by refetch verification;
- uncertain timeout where posting may have succeeded: block retry until refetch resolves state.

Acceptance tests:

1. `verify-env` passes with a fixture config and no secrets printed.
2. `run --question-id <fixture> --dry-run --no-submit` creates exactly one complete validated forecast record.
3. Replaying saved research and saved model output makes zero provider calls and reproduces the same forecast hash.
4. Re-running the same submission command with the same idempotency key makes at most one live post.
5. One bot-testing-area forecast is posted only after explicit approval and is confirmed by refetch.
6. One live MiniBench forecast is posted only after the test-area smoke test passes and owner approval is recorded.

## Objective Milestone 1 pass/fail

Pass only if one current or saved question can be loaded, normalized, researched, forecast, validated and persisted with:

- complete question/tournament/model/prompt/retrieval metadata;
- at least one source or an explicit `insufficient_research` flag;
- base rate, prior, evidence adjustments, failure modes and final typed forecast;
- numeric CDF validation when applicable;
- no submission network call;
- replayable research and raw model output;
- no secrets in logs or artifacts.

## Implementation order

1. Scaffold Python 3.11 project, pin package, add CI and config model.
2. Freeze Pydantic record schemas and initial SQLite migration.
3. Add saved-question fixtures and normalization.
4. Implement AskNews adapter, Exa fallback, normalized documents and replay cache.
5. Implement prompt loading/hash and structured forecaster outputs.
6. Add validation and numeric CDF conversion.
7. Persist dry-run records and implement replay.
8. Add approval state and idempotency.
9. Implement dry-run and Metaculus submission gateways plus bot-testing-area smoke test.
10. Submit one owner-approved MiniBench forecast.
11. Add resolution/scoring/export after the first successful submission.

## Known blockers and owner actions

1. Chris must create a Metaculus bot account/token and keep it outside the repository.
2. Chris must select and fund/authorize the model route used for MiniBench. Metaculus publicly states seasonal inference costs are covered; this does not establish free MiniBench inference.
3. Chris must obtain AskNews credentials or approve Exa-only fallback for the first dry run.
4. Exact current eligible/open question count must be fetched with the bot token; the public tournament page only establishes 59 series questions.
5. MiniBench-specific prize eligibility, payout and post-round administration were not fully stated on the public pages reviewed; confirm before treating the bot as prize-eligible.
6. The submission-receipt spike must resolve whether refetch verification is sufficient or a response-capturing HTTP adapter is required.

## Prohibited implementation claims

- Do not say a live API call succeeded without a recorded receipt and refetch confirmation.
- Do not say MiniBench has only binary questions.
- Do not say a 201-point CDF is valid merely because it has length 201; validate monotonicity, bounds and the PMF-step constraint through the maintained package.
- Do not call a provider output a source; retain original URLs and timestamps.
- Do not persist or publish hidden chain-of-thought. Store concise, auditable reasoning summaries.
- Do not make the community prediction an input to the v1 forecaster; log it separately when available.

## First GitHub issue

**Title:** `M0-001 Scaffold Python 3.11 project and freeze v1 config + attribution schemas`

**Body:**

Create the public `whiskeyjack-bot` Python package with `src/` layout, `pyproject.toml`, `forecasting-tools==0.2.92`, test tooling, `config.example.yaml`, Pydantic settings, the canonical forecast-record models for binary/multiple-choice/numeric outputs, and migration `001_initial.sql`. Add `whiskeyjack-bot verify-env --config config.example.yaml`. No provider calls and no submission code in this issue.

**Acceptance criteria:** clean install on Python 3.11; `pytest` passes; example config validates after replacing the model placeholder; missing secrets are reported without values; all three golden forecast records validate; initial migration creates every table and unique constraint; CI runs unit/schema tests; README documents the exact local commands.

## Implementation sources

- FutureEval participation: https://www.metaculus.com/futureeval/participate/
- Active MiniBench: https://www.metaculus.com/tournament/minibench/
- MiniBench overview: https://www.metaculus.com/aib/minibench/
- Summer tournament: https://www.metaculus.com/tournament/summer-futureeval-2026/
- FutureEval methodology: https://www.metaculus.com/futureeval/methodology/
- Tournament resources: https://www.metaculus.com/notebooks/38928/futureeval-resources-page/
- Package metadata: https://pypi.org/project/forecasting-tools/
- Package README/interfaces: https://github.com/Metaculus/forecasting-tools
- Current template bot: https://github.com/Metaculus/metac-bot-template/blob/main/main.py
- Metaculus client source: https://github.com/Metaculus/forecasting-tools/blob/main/forecasting_tools/helpers/metaculus_client.py
- Numeric CDF source: https://github.com/Metaculus/forecasting-tools/blob/main/forecasting_tools/data_models/numeric_report.py
- Scoring definitions: https://www.metaculus.com/help/scores-faq/
