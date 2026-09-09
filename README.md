# whiskeyjack-bot

A Metaculus forecasting pipeline with an append-only SQLite ledger, retained evidence,
forecast validation, and offline replay. It supports binary, multiple-choice, and numeric
questions, including individual subquestions in groups.

The implementation includes research, model generation, numeric conversion, approval,
submission, refetch verification, and private reasoning comments. Autonomous operation
requires an explicit, expiring activation for one bot account and one concrete project.
**Production project 33122 is not activated by this release.** The
[testing-area rehearsal](docs/LAUNCH-REHEARSAL.md) verified one forecast and private comment,
then repeated without duplicates or additional spending.

## Install and verify

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
cp config.example.yaml config.yaml
# Set a supported model in config.yaml before using live generation.
uv run whiskeyjack-bot verify-env --config config.yaml
uv run whiskeyjack-bot questions fetch --config config.yaml \
  --snapshot tests/fixtures/snapshots/minibench_sample_snapshot.json
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy --strict src
```

Tests isolate credentials and block external sockets. Fixture discovery and forecast
replay require no credentials. Live operation reads `METACULUS_TOKEN`,
`OPENROUTER_API_KEY`, `ASKNEWS_API_KEY`, and `EXA_API_KEY` from the environment.
Never commit credentials. The supplied user service loads them explicitly from `.env`.
Social research is disabled in the tournament profile.

## Tournament operation

[The operator runbook](docs/TOURNAMENT-OPERATIONS.md) covers activation, the five-minute
user timer, recovery, and backups. [The production profile](config/tournament.yaml)
contains the deployment host's absolute paths; review those before installing elsewhere.

```bash
uv run whiskeyjack-bot tournament status --config config/tournament.yaml
# Only after owner authorization, bind the reviewed profile to a validity window:
uv run whiskeyjack-bot tournament enable --config config/tournament.yaml \
  --project-id 33122 --starts '<UTC ISO timestamp>' --ends '<UTC ISO timestamp>' \
  --budget-usd 20
uv run whiskeyjack-bot tournament run-once --config config/tournament.yaml
uv run whiskeyjack-bot tournament disable --config config/tournament.yaml
```

A poll saves discovery, processes earliest-closing questions sequentially, records approval
by the activated policy, submits once, verifies the forecast, and posts and verifies a
private rationale comment. An empty poll succeeds. New forecasts do not start within five
minutes of closing. `run-once --question-id ID` restricts new work to one subquestion for
rehearsal; pending external operations are still reconciled. Use a separate testing profile
and ledger for project **32977**. A testing activation cannot authorize production records.

The tournament model is `openrouter/openai/gpt-5.6-sol`, medium reasoning, up to 6,000
output tokens, a 120-second request timeout, and at most two invocations including repair.
Temperature is omitted. Routing requires support for the parameters and caps input/output
prices at US$2/US$10 per million tokens; unavailability fails explicitly. See the
[OpenRouter model listing](https://openrouter.ai/openai/gpt-5.6-sol).

The activated round budget is at most **US$20**, enforced through durable reservations
before tournament research and model calls. Unknown charges retain their estimates;
confirmed actual charges are reported separately. Research uses one consolidated AskNews
query (current and archive calls), plus at most two complementary Exa searches when needed.
Evidence must be contemporary and relevant. Where the question names a resolution source
and no retrieved document comes from it, that gap is **recorded against the forecast** as an
`evidence_gap` ledger row rather than refusing the forecast: the two available retrievers are
news retrievers, and a resolution authority is often not a news publisher. Future-dated
evidence is excluded. Research recovery is
limited to the same unchanged question within 30 minutes.

## Safety and recovery

- Approval binds to both forecast and submission payload hashes. Automatic approvals name
  the activation policy; manual approval commands remain available.
- The live submission boundary checks activation, account, destination, current resolution
  inputs, deadline, existing account forecasts, and replay artifacts.
- A transaction protects the whole question across forecast versions and processes.
  Submission intents are committed before POST. Uncertain outcomes are reconciled by
  reading Metaculus; a missing response never authorizes an automatic repeat POST.
- Forecast and comment completion are separate. Repeating a poll can recover a missing
  comment without resubmitting a forecast. Unresolved operations return a nonzero exit.
- SQLite uses `synchronous=FULL`; artifact files and their directory entries are synchronized.
  A retained host guard detects storage rollback and requires platform reconciliation.
- Old record hashes and offline replay remain supported. Provider responses are evidence,
  never instructions; stored requests and responses redact configured secrets.

The legacy `run`, `approve`, `reject`, `submit`, and `replay` commands remain available
(`--help` lists their arguments). Use `tournament run-once` for the round budget and
private-comment workflow; the older research/generation command retains its per-run cost
reporting. Manually invoked submissions also require a matching activation.

[Metaculus tournament resources](https://www.metaculus.com/notebooks/38928/futureeval-resources-page/)
require one forecast per question and private reasoning comments. Operational verification
does not measure forecasting calibration or competitive performance. Ambiguous external
outcomes may require operator intervention.

## Repository map

| Path | Purpose |
|---|---|
| `src/whiskeyjack_bot/` | Research, forecasts, durable ledger, submission, tournament runner |
| `config.example.yaml` | General configuration contract |
| `config/tournament.yaml` | Explicit production operating profile |
| `prompts/forecaster-tournament.md` | Version 1.2 tournament prompt |
| `deploy/systemd/` | User service and five-minute timer |
| `docs/TOURNAMENT-OPERATIONS.md` | Deployment and recovery instructions |
| `tests/` | Offline unit, property, integration, and acceptance tests |
| `docs/backlog/` | Historical implementation specification |

MIT — see [LICENSE](LICENSE).
