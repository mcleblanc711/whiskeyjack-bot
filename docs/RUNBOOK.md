# Operator runbook

**D-1001.** What to do when a forecast run, an approval or a submission goes wrong.

Written for the operator at 3am who has just had a submission come back uncertain. The
index below is the entry point: find your symptom, go to that section.

## How to read this document

Three rules govern everything here, and they are the reason the recovery for some states
is "file a ticket" rather than a command.

1. **The ledger is append-only.** Forecast versions and lifecycle events are never mutated
   (D25). Recovery therefore never means "fix the row"; it means "append the event that
   says what really happened", or it means the state is not recoverable by you today.
2. **No recovery in this document is a SQL statement.** Not one. If you find yourself
   opening `sqlite3` against the ledger, stop — you are either in a state this document
   says is a filed gap, or you have found a sixth one worth filing. Editing the ledger by
   hand destroys the thing the ledger exists to be.
3. **Where a state has no recovery, this document says so and names the backlog row.**
   It does not invent a workaround. See [When the only recovery would be a database
   edit](#when-the-only-recovery-would-be-a-database-edit).

Everything below was checked against the code on this branch. Command output shown as a
block was produced by running the command, except where the section says otherwise —
the paid and networked commands (`run`, `submit`, `questions fetch --live`) are documented
from the source that prints them, and each of those blocks is labelled.

### Two things that will mislead you before you read a single error

- **The first lines a command prints are usually not from this program.** The pinned SDK
  emits import-time noise on **stderr** — `METACULUS_TOKEN environment variable and/or
  token field not set`, `Warning: Model ... does not support cost tracking`, a `streamlit`
  cache warning. None of these is the result of your command. This program's own output
  goes to **stdout**. Append `2>/dev/null` and you will see only what the command actually
  decided:

  ```bash
  uv run whiskeyjack-bot submit --config config.yaml --record-id "$REC" 2>/dev/null
  ```

  In particular: seeing `METACULUS_TOKEN ... not set` on stderr does **not** mean your
  token is missing. The program reports a genuinely missing credential on stdout, by name,
  and exits `3`.

- **`forecast_records.status` is not the record's status.** That column is the status the
  row was *created* with and is pinned to `draft` forever, because the table is
  append-only and nothing can update it. The real status is derived from the last
  `lifecycle_events` row (`lifecycle.current_status`, `lifecycle.py:829-852`). Every
  command prints the derived value. A record whose `status` column reads `draft` may well
  be `approved`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | OK |
| `2` | Configuration invalid (or a bad command line — argparse also uses `2`) |
| `3` | A required environment variable or file is missing |
| `4` | Refused: the command understood you and declined, and wrote nothing |

Defined at `env_verify.py:26-28` (`EXIT_OK`, `EXIT_CONFIG_INVALID`, `EXIT_ENV_MISSING`) and
`cli.py:31` (`EXIT_REFUSED`).

**Exit code `4` is the good outcome when something is wrong.** Every refusal in this
program runs *before* the action it refuses, with one named exception (a live post the
ledger then refused to record — see [L4](#l4--a-live-post-the-ledger-refused-to-record)).

**`submit` exits `0` for every outcome it managed to record — including
`submission_uncertain` and `submission_failed`.** Exit `0` from `submit` means "the attempt
completed and was written down", *not* "the forecast is on the platform". Read the
`result:` line, never the exit code. Do not script a retry on `submit`'s exit status.

`run` is the opposite case: it exits `4` if any question failed or if it forecast none, so a
partial batch is a non-zero exit even though the records that succeeded were written.

---

## Symptom index

| What you are looking at | Section |
|---|---|
| `no ledger database at ...` | [C1](#c1--no-ledger-database) |
| `record_id does not name a stored forecast record` | [P0](#p0--two-profiles-one---config-and-the-error-that-looks-like-data-loss) — check `--config` first |
| `Tournament refused: tournament activation is disabled` | [P1](#p1--the-cup-profile-is-dormant-its-refusals-are-correct) |
| `activation retired: ... changed; re-run tournament enable` | [C5](#c5--activation-retired) |
| `operation artifact missing; platform reconciliation required` | [P2](#p2--a-ledger-copied-without-its-artifact-root-will-refuse) |
| `ledger migration N does not match the checksum ...` | [C2](#c2--migration-checksum-mismatch) |
| `invalid configuration:` / exit `2` | [C3](#c3--configuration-refused) |
| `missing env var: ...` / `environment NOT ready` | [C4](#c4--environment-not-ready) |
| `missing env var: EXA_API_KEY` and nothing else is wrong | [C4](#c4--environment-not-ready) — known false red |
| `run` printed `status: research_failed` | [R1](#r1--research_failed) |
| `run` printed `status: generation_failed` | [R2](#r2--generation_failed) |
| `run` printed `status: validation_failed` | [R3](#r3--validation_failed) |
| `run` printed `status: not_recorded` | [R4](#r4--not_recorded-the-forecast-the-ledger-never-saw) |
| A question vanished from the run with no failure line | [R5](#r5--a-deferred-question-leaves-no-trace) |
| `replay` printed `verdict: MISMATCH` | [R6](#r6--replay-mismatch) |
| `forecast_sha256 does not match ...` | [A1](#a1--the-forecast-changed-and-the-approval-no-longer-binds) |
| `... is not a legal transition for a record whose current status is approved` | [A2](#a2--an-approved-record-cannot-be-re-approved-or-rejected) |
| `this submission payload is not the one the approval in force authorized` | [A2](#a2--an-approved-record-cannot-be-re-approved-or-rejected) |
| `the approval in force ... predates the payload binding (migration 011)` | [A3](#a3--an-approval-older-than-migration-011) |
| `live submission is off: ...` | [S1](#s1--live-submission-is-off) |
| `submit` printed `result: submission_uncertain` | [**U**](#the-uncertain-timeout) |
| `submit` refused with `... blind retry ...` | [**U**](#the-uncertain-timeout) |
| `submit` printed `result: submission_failed` | [L1](#l1--submission_failed) |
| `a key reservation is standing for this record` | [L2](#l2--a-standing-key-reservation) |
| `verify-submission` said `resolve it by hand` | [L3](#l3--a-mismatched-refetch) |
| `a live post was made and the ledger refused to record it` | [L4](#l4--a-live-post-the-ledger-refused-to-record) |
| `artifact:  NOT WRITTEN -- ...` | [L5](#l5--the-artifact-was-not-written) |

---

## Before you start

### P0 — Two profiles, one `--config`, and the error that looks like data loss

There are two tournament profiles, and **every command in this document takes `--config`**.
The ledger, artifact root, export root and log file all move with that flag. Point it at the
wrong profile and the command reads a different database.

| | MiniBench | Metaculus Cup Fall 2026 |
|---|---|---|
| config | `config/tournament.yaml` | `config/tournament-cup.yaml` |
| project | 33122 | 33108 |
| ledger | `data/whiskeyjack_bot.sqlite3` | `data/cup/ledger.sqlite3` |
| artifacts | `data/artifacts/` | `data/cup/artifacts/` |
| log | `data/logs/tournament.jsonl` | `data/logs/tournament-cup.jsonl` |
| unit | `whiskeyjack-tournament.timer` | `whiskeyjack-tournament-cup.timer` |
| state | **live** | **dormant since 2026-09-10** |

They share nothing. `tournament_state.py` activation is scoped to one ledger/account/project
at a time, which is what lets two tournaments run as two profiles rather than one process
polling both.

**The failure mode.** A record id is unique within its own ledger and simply absent from the
other. Give a Cup record id to a MiniBench-configured command and you get:

```
refused: record_id does not name a stored forecast record
```

exit `4`. Reproduced by execution, against a *copy* of the MiniBench ledger with a real Cup
record id.

That message is telling the truth about the ledger it was pointed at. It is **not** evidence
that the record was lost, that the ledger is corrupt, or that a migration dropped rows — and
at 3am it reads like all three. Before you investigate anything else, check the `--config`
you passed. The question id tells you which profile a record belongs to: Cup questions came
from project 33108, MiniBench from 33122.

Nothing in this situation needs recovery. Re-run the same command against the other profile.

### P1 — The Cup profile is dormant; its refusals are correct

The Cup was withdrawn on 2026-09-10 (owner decision, to concentrate the OpenRouter grant on
MiniBench). Two things were done, both reversible: the timer was stopped and disabled, and
`tournament disable --config config/tournament-cup.yaml` appended a `disabled` event to the
Cup's ledger. `tournament status --config config/tournament-cup.yaml` reads `"enabled": false`.

So `tournament run-once` against that profile refuses, and `require_activation` is where it
stops (`tournament_state.py`, `ActivationInactive("tournament activation is disabled")`,
surfaced by the CLI as `Tournament refused: ...`). *Read from the source, not run —
demonstrating it needs a live activation this profile no longer has.*

**Do not treat that refusal as a fault to clear.** It is the withdrawal working. Re-entry is
a deliberate operator act, documented in the banner at the top of `config/tournament-cup.yaml`
and in `docs/M1-NOTES.md` — a fresh `tournament enable` plus re-enabling the timer. Editing
the YAML cannot re-enter the tournament; activation lives in SQLite.

The 5 forecasts the Cup posted **stay on Metaculus**. Withdrawing stopped future polling; it
did not and could not retract them. Its `reserved_cost_usd` of $3.175 stands unsettled and
always will — that is the expected steady state for reservations, not a leak. Nothing should
be written to tidy either number.

### P2 — A ledger copied without its artifact root will refuse

If you ever restore or copy a profile's ledger, **take its artifact root with it**. The
posting guard is scoped by the ledger's parent directory, and a ledger whose artifacts are
missing refuses with:

```
Tournament refused: operation artifact missing; platform reconciliation required
```

Found by execution while preparing P0 above — copying a ledger alone was enough to trigger
it. This is the same guard that made both Cup profiles need their own `data/cup/`
subdirectory rather than sharing `data/` with MiniBench under a different filename.

### The three submission flags

```yaml
submission:
  enabled: false
  dry_run: true
  no_submit: true
```

These are the committed defaults and they are what makes a post unreachable. A live post
requires **all three flipped together** — `enabled: true`, `dry_run: false`,
`no_submit: false` — and configuration validation refuses any partial combination
(`config.py:339-387`). Flipping them is a deliberate act, not something you do to get past
an error message.

`--dry-run` and `--no-submit` on `run` and `run-replay` are **assertions, not overrides**.
They check that the configuration already holds the safe value and refuse if it does not.
A flag that silently forced the safe value would let a config with `dry_run: false` pass a
command line that reads as safe.

### First-time setup

```bash
uv run whiskeyjack-bot init-ledger --config config.yaml
uv run whiskeyjack-bot verify-env  --config config.yaml
```

`init-ledger` creates or upgrades the ledger at `storage.sqlite_path` and is a no-op if it
is already current. It is safe to run at any time:

```
ledger:  /path/to/data/bot.sqlite3
version: 11
```

### Which commands cost money, and which touch the network

| Command | Ledger | Network | Spends money |
|---|---|---|---|
| `verify-env` | no (creates configured directories) | no | no |
| `init-ledger` | creates/upgrades schema | no | no |
| `questions fetch` (no `--live`) | no | no | no |
| `questions fetch --live` | no | GET | no |
| `run-replay` | writes a record | no | no |
| `replay` | **read-only** | no | no |
| `approve` / `reject` | appends one event | no | no |
| `release-key` | appends a release row | no | no |
| `verify-submission` | appends an event | **GET** | no |
| `run` | writes records | **yes** | **YES — retrieval and model calls** |
| `submit` | appends an attempt | **POST** | posts a forecast |

`run` is the live paid path and `run-replay` is the free one. The money boundary is a
**subcommand name**, not a flag, deliberately (M1-315).

---

## The lifecycle, end to end

```
run / run-replay  →  replay  →  approve  →  submit  →  verify-submission
   (validated)      (check)    (approved)  (submitted / uncertain / failed)
```

Each arrow is a persisted boundary. What follows is a real walkthrough on the replay path,
which costs nothing and makes no network call.

### 1. Produce a record

```bash
uv run whiskeyjack-bot run-replay --config config.yaml \
  --question-id 91001 --snapshot snapshot.json --attempt-id seed-attempt \
  --dry-run --no-submit
```

```
record:    01a0735c-6732-7203-8cd9-740e6b31df74
question:  91001  tournament: minibench  version: 1
attempt:   01a0735c-6707-718e-9004-9473c6aecf0e (replayed from seed-attempt)
research:  1 run(s), 2 source(s)
packet:    c2389c36305bbbc1fcb912fbd7d0800a428979af7d4ea00fd6607fb32505a969
artifact:  forecast/91001/01a0735c-6707-718e-9004-9473c6aecf0e.json
hash:      a754604dbdb8be153c3a7cc919fec6913d738ee617060c88ea2753f6ce6a343a
status:    validated
submitted: no -- `run` never submits; approve and submit are separate commands
```

**Write down the `record` and the `hash`.** They are what every later command takes.

The live paid equivalent is `run --config … --snapshot … [--question-id N | --limit N]`.
It prints one block per question plus a batch summary, and it exits `4` if any question
failed or if none was forecast. Without `--refresh-research` a rerun repeats no paid
retrieval call for a question whose research the ledger already holds.

### 2. Check the record reproduces

```bash
uv run whiskeyjack-bot replay --config config.yaml --record-id "$REC"
```

```
record:    01a0735c-6732-7203-8cd9-740e6b31df74
artifact:  forecast/91001/01a0735c-6707-718e-9004-9473c6aecf0e.json
calls:     1 invocation(s), 1 stored repl(y/ies), cost 0.250000 USD
stored:    a754604dbdb8be153c3a7cc919fec6913d738ee617060c88ea2753f6ce6a343a
replayed:  a754604dbdb8be153c3a7cc919fec6913d738ee617060c88ea2753f6ce6a343a
verdict:   match
```

Read-only, no API call. `verdict: MISMATCH` exits `4` — see [R6](#r6--replay-mismatch).

Note `replay` requires `forecast.replay_saved_model_output: true`; with it off the command
refuses with `forecast.replay_saved_model_output is disabled; refusing to replay saved
model output` **before** it looks the record up, so that refusal is not evidence that your
record is missing.

### 3. Approve

Review the forecast, then bind your approval to the exact hash you reviewed:

```bash
uv run whiskeyjack-bot approve --config config.yaml --record-id "$REC" \
  --actor "you@example" --forecast-sha256 "$HASH" --note "reviewed"
```

```
record:    01a0735c-6732-7203-8cd9-740e6b31df74
question:  91001  tournament: minibench  version: 1  type: binary
status:    validated
hash:      a754604dbdb8be153c3a7cc919fec6913d738ee617060c88ea2753f6ce6a343a
payload:   sha256 86612429df1408354c1771146b6ca220193df25ac74ad1d7d67e6f5c441f3704
           {"probability_yes":0.37,"question_type":"binary"}
approved 01a0735c-6732-7203-8cd9-740e6b31df74 (approval event 1, lifecycle seq 2)
```

**Always pass `--forecast-sha256`.** It is optional to the parser and mandatory in
practice: it is the only thing that makes your approval a claim about the forecast you
actually read. Without it you are approving whatever the record happens to hold now.

`--actor` is required and has no default. An approval is an attribution claim about a
person, and the program will not infer one from your OS login.

Since M2-707 (migration `011`) the approval also binds the **payload digest** printed
above. One approved forecast no longer authorizes every payload built from it.

### 4. Submit

```bash
uv run whiskeyjack-bot submit --config config.yaml --record-id "$REC"
```

Omit `--payload-file` and the payload the record derives is used — which, since the
approval binds a payload digest, is the only payload that can reach a post anyway. Supply
one to check a payload against that approval **without posting anything it did not cover**.

`submit` applies every gate before the post: configuration, no unresolved uncertainty, the
record's own identity, payload type, the approval and the payload it authorized, and an
atomic idempotency-key reservation. All of them refuse without spending anything.

### 5. Resolve an uncertain outcome

If `submit` printed `result: submission_uncertain`, go to [the uncertain
timeout](#the-uncertain-timeout). Nothing else may be submitted for that record first.

### Where the lifecycle stops today

The legal transitions are fixed in the schema (`009_submission_refetch_outcome.sql:193-207`):

```
validated             draft     -> validated
validation_failed     draft     -> failed
validation_failed     validated -> failed
rejected              validated -> validated
approved              validated -> approved
submitted             approved  -> submitted
submission_uncertain  approved  -> approved
submission_failed     approved  -> failed
submission_confirmed  approved  -> submitted
submission_disconfirmed approved -> failed
resolved              submitted -> resolved
scored                resolved  -> scored
```

Two consequences worth knowing before you need them:

- **`failed` is terminal by omission.** No transition leaves it. A failed record is never
  repaired; you make a new forecast version.
- **`resolved` and `scored` have no writer in the shipped code.** No command and no
  function inserts into `resolution_events` or `score_events`. In practice `submitted` is
  where a record stops today. That is milestone work, not a fault, and not something this
  runbook can give you a command for.

---

## The state vocabularies

You will see these strings in command output. They are closed sets, enforced by the schema.

**`forecast_records.status`** — 7 members (`001_initial.sql:28-30`). Remember this column
is *status at creation*; the derived status is what commands print.

```
draft  validated  approved  submitted  failed  resolved  scored
```

**`lifecycle_events.event_type`** — 11 members (`003_lifecycle_events.sql:237-243`).

```
validated  validation_failed  rejected  approved  submitted
submission_uncertain  submission_failed  submission_confirmed
submission_disconfirmed  resolved  scored
```

**`pipeline_failure_events.event_type`** — exactly **two** members
(`004_pipeline_failure_events.sql:54`). This is the whole vocabulary for failures that
happen before any forecast record exists:

```
research_failed  generation_failed
```

There is deliberately **no member** for "the forecast was fine and the ledger refused it".
See [R4](#r4--not_recorded-the-forecast-the-ledger-never-saw).

**`submission_attempts.refetch_outcome`** — 4 members (migration `009`).

```
confirmed  absent  mismatched  unreadable
```

**Failure detail codes** carried on events — 12 members
(`lifecycle.py:FailureCode`). Pre-forecast events use the same list minus
`refetch_mismatch` and `refetch_missing`, which describe a refetch of an already-posted
forecast and cannot occur before generation has succeeded once:

```
provider_error  provider_unavailable  no_evidence  stale_evidence
malformed_response  schema_invalid  calibration_invalid  http_error
timeout  refetch_mismatch  refetch_missing  internal_error
```

### The submission partition — what `submit`'s result means

The lifecycle event is **derived from the attempt**, never chosen
(`lifecycle.py:1032-1046`). This table is the one to internalise:

| `success` | `refetch_outcome` | event | record ends |
|---|---|---|---|
| true | `confirmed` | `submitted` | `submitted` — done |
| true | `absent` | `submission_uncertain` | `approved` |
| true | `mismatched` | `submission_uncertain` | `approved` |
| true | `unreadable` | `submission_uncertain` | `approved` |
| false | `confirmed` | `submission_uncertain` | `approved` |
| false | `absent` | `submission_failed` | `failed` — terminal |
| false | `mismatched` | `submission_uncertain` | `approved` |
| false | `unreadable` | `submission_uncertain` | `approved` |

**Only two cells are conclusions.** A confirmed success is `submitted`. An observed absence
after a post that raised is `submission_failed`. *Everything else is uncertain*, because
the post and the platform disagree or the platform was never read — and neither of those
is a failure. Recording them as failures would move the record to terminal `failed`, and a
later confirming refetch would have nowhere to land.

### What a refetch establishes

`submission_live.classify_refetch` (`submission_live.py:901-950`) decides the outcome
against a baseline taken **before** the post:

| Outcome | When |
|---|---|
| `confirmed` | An entry newer than the baseline exists **and** its values match what was sent |
| `absent` | Nothing on the platform is newer than the baseline (or there is no entry at all) |
| `mismatched` | Something newer than the baseline is there and it is **not** what was sent |
| `unreadable` | No observation could be made at all — the GET failed, or the values could not be aligned |

"Newer than the baseline" is what makes this a test of *this* post rather than of any post:
a question you had already forecast would otherwise confirm a submission that never landed.

---

## Configuration and startup failures

### C1 — No ledger database

**What you see** (exit `4`):

```
no ledger database at /path/to/data/bot.sqlite3; nothing has been recorded there yet
```

**Confirm.** The path in the message is `storage.sqlite_path` from your config. Check
whether the file exists; if it does, you are pointed at a different config than you think.

**Recovery.**

```bash
uv run whiskeyjack-bot init-ledger --config config.yaml
```

**Never.** Do not create the file yourself, and do not copy a ledger from elsewhere to
satisfy the check. `init-ledger` is idempotent and is the only supported way to make one.

### C2 — Migration checksum mismatch

**What you see.** Every command fails to open the ledger, with a message naming the
migration version whose checksum no longer matches the one recorded when it was applied
(`ledger.py:254-260`).

**Confirm.** This means a migration file that has already been applied to this database was
edited afterwards. Either the file changed (a bad merge, a hand edit) or the database is
from a different build.

**Recovery.** Restore the migration file to the version that was applied — normally
`git checkout` of `src/whiskeyjack_bot/migrations/`. Migrations already on master are
immutable; if yours differs from master, master is right.

**Never.** Do not edit `schema_migrations` to make the check pass. That check is what tells
you the schema you are reading records with is the schema they were written under.

### C3 — Configuration refused

**What you see** (exit `2`):

```
invalid configuration:
  - <field>: <what is wrong>
```

**Confirm.** Every config model forbids unknown keys, so a typo'd key is a load failure
rather than a silently ignored setting. The `loc` in the message names the field.

Common cases: `model.name` still holds the `REPLACE_WITH...` placeholder; a partial
submission-flag combination (see [Before you start](#the-three-submission-flags));
`forecast.fail_on_stale_research` and `flag_on_stale_research` both false.

**Recovery.** Fix the named field. `config.example.yaml` is the contract.

**Never.** Do not set `logging.redact_secrets: false` — it is typed to accept only `true`
and cannot be turned off.

### C4 — Environment not ready

**What you see** (exit `3`):

```
missing env var: METACULUS_TOKEN (set it in the environment; never in config)
environment NOT ready
```

**Confirm.** `verify-env` reports **names only** and never reads a value. Set the variable
in your environment — never in a config file, never in code.

**The known false red.** `verify-env` treats `retrieval.fallback.api_key_env`
(`EXA_API_KEY` in the example config) as **required**, while `run` treats the same key as
**optional** — its absence just marks the fallback retrieval provider unavailable and the
run proceeds. So an install with no Exa account reports `environment NOT ready` and exits
`3` while being perfectly runnable. If `EXA_API_KEY` is the only line in the report, you
are not blocked. Filed as **M0-010**; when it ships, this paragraph goes away.

**Never.** Do not put a credential in `config.yaml` to satisfy this check. The config
carries the *name* of the variable, never its value.

### C5 — Activation retired

**What you see.** Every `tournament run-once` poll refuses, and the worker buys and posts
nothing:

```
activation retired: configuration changed; re-run tournament enable
```

One or more of four binding names appears — `account`, `destination`, `configuration`,
`prompt` — in that order (`tournament_state.py`, `ActivationRetired`). Since **M1-334** this
also pushes one alert per profile, titled `whiskeyjack: activation retired`; the body names
the same bindings and never a digest or a config value.

**Confirm.** An activation binds to the account, the destination project, a digest of the
whole `AppConfig`, and a digest of the forecaster prompt file. Any of them moving retires it.
The binding names tell you which:

| Name | What moved |
|---|---|
| `account` | The token now authenticates a different Metaculus user than the one enabled. |
| `destination` | The configured tournament is not the one enabled, or `use_sdk_current_id` is on, or a non-production profile is pointed somewhere other than the test project. |
| `configuration` | **Any** byte of the effective config. Adding a field to `AppConfig` in a merge is enough — the YAML need not change at all. |
| `prompt` | `forecast.prompt_path`'s bytes changed. |

**`configuration` is the one that surprises people, and it has already cost real downtime.**
On 2026-09-09 a merge that added an `AppConfig` field moved `config_sha256` with the operator's
YAML untouched, and both live workers refused every poll for 2h33m. Nothing in-process could
report it at the time — that gap is what M1-334's alert closes — and the only outward symptom
was the JSONL tail going quiet. **A merge or a dependency bump can retire an activation.
Treat `tournament enable` as part of deploying, not as one-time setup.**

**This is not [P1](#p1--the-cup-profile-is-dormant-its-refusals-are-correct).** A dormant or
out-of-window profile raises `ActivationInactive` and is a deliberate resting state; it is
checked *first* and stays silent even when a binding has also moved, so deploying a config
change against a disabled profile does not page anyone. `activation retired` means a profile
you are running has stopped running.

**Recovery.** Re-run `tournament enable` for the affected profile, with the `--config` that
profile uses, and re-enable its timer. Nothing else clears it.

**Never.** Do not reach into `tournament_events` to re-point the stored digests at the current
config. That row is the evidence that a human authorized *this* configuration to spend money
against *this* project; rewriting it to match whatever is on disk destroys the only thing it
proves. Do not disable the alert to quiet the pages either — the refusal is already silent in
the ledger, and the alert is the only thing that says so out loud.

---

## Run and forecast failures

All four `run` outcomes below are **per question**. A batch continues past a failed
question; `records: N of M question(s)` in the summary tells you how many were written.

### R1 — `research_failed`

**What you see.** A question block from `run` with `status: research_failed` and a detail
code.

**Confirm (read-only).** The `run` output block is your evidence: it carries the question
id, the attempt id and the detail code. A `pipeline_failure_events` row was written under
that attempt id, but **no command can show it to you** — that is **M1-611**. The log file at
`logging.file` has the same information.

**Recovery.** Re-run the question. A new attempt id is minted, and the earlier failure
stays in the ledger as history — that is the point of it. If the detail code is
`no_evidence` or `stale_evidence`, retrieval worked and found nothing usable; retrying
immediately will likely reproduce it.

**Note on spend.** A retrieval failure that happened *after* the provider was billed still
counts its spend in the batch total, and the failure event cites the research run that was
paid for. A failure before any provider call cites no run.

**Never.** Do not pass `--refresh-research` reflexively when retrying — without it, a rerun
repeats no paid retrieval call for research the ledger already holds. With it, you pay
again.

### R2 — `generation_failed`

**What you see.** `status: generation_failed` with a detail code such as
`malformed_response`, `schema_invalid` or `calibration_invalid`.

**Confirm (read-only).** The `run` block gives you the attempt id and detail code, and —
this is the useful part — **the raw model reply is on disk** under `storage.artifact_root`,
keyed by question id and attempt id. The money bought that text, and it is written before
the event precisely so it survives. A `pipeline_failure_events` row was also written; as in
R1, no command reads it back (**M1-611**).

**Recovery.** Read the saved reply to see what the model actually said, then re-run. If the
detail code is `calibration_invalid` or `schema_invalid` the model produced a well-formed
answer that broke a bound; that is worth reading before spending again.

**Never.** Do not treat a `generation_failed` attempt id as reusable — an attempt id names
one campaign, and the schema refuses to record both a failure and a success under it.

### R3 — `validation_failed`

**What you see.** `status: validation_failed`, and a record id — unlike R1 and R2, a
forecast record **was** written.

**Confirm (read-only).** A record id was printed, so you can probe it: with
`submission.enabled: false`, run `submit --record-id <REC>` and read the `status:` line — it
will say `failed`. The command refuses at the configuration gate and writes nothing. This is
the research-sufficiency gate (`forecast.fail_on_stale_research`) refusing a draft rather
than moving it to `validated`.

**Recovery.** `failed` is terminal. Make a new forecast version — with fresher research if
staleness was the cause (`--refresh-research`).

**Never.** Do not try to approve it. `approved` is reachable only from `validated`, so the
attempt will be refused, and correctly.

### R4 — `not_recorded`: the forecast the ledger never saw

**This is the one failure mode that implies a sick ledger, and it is the one the ledger
does not see.** Read this section before you need it.

**What you see.** A question block ending:

```
  status:   not_recorded
    note: <sanitized reason>
```

The forecast generated *successfully* and the ledger then refused to store it.

**What is written: nothing.** There is no `pipeline_failure_events` row, because that
table's vocabulary has exactly two members and neither is true — the generation did not
fail. There is no `forecast_records` row, because writing it is what failed. There is no
`lifecycle_events` row, because there is no record to attach one to. Recording a failure
that did not happen would put a false claim in the ledger, so nothing is recorded at all.

**What survives.**

- The `note:` line on the terminal, right now.
- An `ERROR` line in the log file at `logging.file`.
- **The raw model output artifact on disk**, written before the row was attempted. This is
  the durable trace, and the only one.

**Confirm (read-only).** You cannot confirm this from the ledger — that is the defect. Look
at the log file and at `storage.artifact_root`.

**Recovery.** Read the `note:`: it is the sanitized reason the ledger refused, and it is
usually about the ledger itself (disk, permissions, a schema problem) rather than the
forecast. Fix that, then re-run the question. The spend on the failed attempt is not
recoverable from the ledger.

**Never.** Do not assume the run was clean because the ledger looks clean. If a batch
summary says `records: 3 of 4` and no failure event exists for the fourth, this is what
happened.

**Filed as M1-317** — giving this shape a durable ledger identity. Until it ships, the
terminal and the log are the record.

### R5 — A deferred question leaves no trace

**What you see.** A question in the snapshot does not appear as a forecast and produced no
failure event.

**Confirm.** Deferral events (`deferred_v1_type`, `unrecognized_type`) are **in-memory
only**. There is no table, no migration and no persistence for them — they are reported on
the run's output and nowhere else.

**Recovery.** None needed; a question type outside v1 scope is deferred by design. But it
means you cannot audit deferrals after the fact from the ledger, only from the run output.

### R6 — Replay mismatch

**What you see** (exit `4`): `verdict:   MISMATCH`, with `stored:` and `replayed:` hashes
that differ, or `replayed:  (the stored reply no longer parses)`.

**Confirm.** The command is read-only, so re-running it is free and safe.

**Recovery.** This is a **serious** signal: a stored forecast no longer re-derives from its
saved model output. Do not approve or submit the record. Investigate whether the artifact
was modified, or whether validation or calibration configuration changed since the record
was written — a config change that alters derivation will change the hash.

**Never.** Do not approve a record that does not replay. The approval binds to a hash whose
provenance you can no longer demonstrate, which is the whole thing this instrument exists
to provide.

---

## Approval failures

### A1 — The forecast changed and the approval no longer binds

**What you see** (exit `4`, nothing written):

```
record:    01a0735c-6732-7203-8cd9-740e6b31df74
question:  91001  tournament: minibench  version: 1  type: binary
status:    validated
hash:      a754604dbdb8be153c3a7cc919fec6913d738ee617060c88ea2753f6ce6a343a
refused: forecast_sha256 does not match the stored hash of this forecast record; the forecast changed and any prior approval no longer binds
```

**This is a normal state, not an error.** It means the `--forecast-sha256` you passed is
not what the record stores.

**Confirm (read-only).** The refusal prints the hash the record actually holds, on the
`hash:` line, before refusing. Compare it with what you reviewed.

**Recovery.** Two cases, and they are different:

- *You reviewed an older version of this question.* A forecast version is immutable, so a
  changed forecast is a **new record** with a new id, and that new record has no approval
  at all. Review the new record and approve **it**, by its own id and hash.
- *You mistyped the hash.* Re-run with the hash the refusal printed — after satisfying
  yourself that it is the forecast you actually read.

**Never.** Do not drop `--forecast-sha256` to make the command succeed. That converts a
refusal into an unexamined approval, which is the failure the flag exists to prevent.

### A2 — An approved record cannot be re-approved or rejected

**What you see** (exit `4`):

```
refused: a approved event is not a legal transition for a record whose current status is approved
```

and, from `reject`:

```
refused: a rejected event is not a legal transition for a record whose current status is approved
```

**Why this bites.** `submit` refuses a payload the approval did not authorize with a
message that ends *"either submit the payload this record derives or approve the record
again"* — but **approving again is not possible**. `approved` is reachable only from
`validated` and nothing returns a record to `validated`, so both `approve` and `reject` are
closed for that record. The refusal points at an action the state machine forbids.

You reach this state when the payload derivation changes between approve and submit — a
`numeric_calibration` config edit is enough to do it.

**Confirm (read-only).** Run `submit` while `submission.enabled` is `false`. It prints the
record, its derived status, its hash and the derived payload digest, then refuses at the
configuration gate having written nothing. Compare that digest with the one `approve`
printed.

**Recovery today.** Make a **new forecast version** (a fresh `run` for that question) and
approve that. On the live paid path that costs money.

**Never.** Do not edit the approval row to match the new payload. An approval is a claim
about what a person reviewed; rewriting it makes the ledger lie about a human decision.

**Filed as M2-715.** The message and the state machine disagree, and one of them has to
move.

### A3 — An approval older than migration 011

**What you see** (exit `4`):

```
refused: the approval in force for this forecast record predates the payload binding (migration 011) and so authorizes no particular payload; approve the record again to bind the decision to the payload it authorizes
```

**Confirm.** The approval was written before migration `011` added the payload binding, so
it carries no `payload_sha256`. Such an approval is **refused rather than exempted** — it
authorizes the forecast but no particular payload.

**Recovery.** As the message says, approve the record again — **if** the record is still
`validated`. If it is already `approved`, you are in [A2](#a2--an-approved-record-cannot-be-re-approved-or-rejected)
and the recovery there applies.

**Never.** Do not treat the absence of a payload digest as "no restriction". It means the
opposite.

---

## The uncertain timeout

**This is the state the whole approval boundary exists for. A blind retry here can post the
same forecast twice.**

### What it means

`submit` printed:

```
result:    submission_uncertain (success=..., refetch=...)
the outcome is unresolved; run `whiskeyjack-bot verify-submission --record-id <REC> --attempt-id <ATTEMPT>` before submitting anything else for this record
```

A post was made, or attempted, and the platform's state with respect to *this* forecast
could not be established as either "it is there and it is what we sent" or "nothing newer
than the baseline is there". It is **not** a failure and **not** a success. The record
stays `approved`, which is what keeps it resolvable.

**Copy the attempt id now.** You will need it, and it is not easy to get back — see
["How to find the attempt id"](#how-to-find-the-attempt-id-if-you-lost-it) below.

### Blind retry is blocked, and blocked in front of the action

Try to submit again and you get (exit `4`, nothing posted, nothing even fetched):

```
refused: this record has N submission attempt(s) whose outcome a refetch has not resolved; posting again would be the blind retry the ledger exists to prevent -- resolve them with verify-submission first
```

The gate runs **before** the post, not before the recording. That distinction was settled
deliberately: a refusal at the writer could only stop a completed post from being written
down, which loses the fact instead of preventing the act.

Note the refusal names a **count**, not the attempt ids — they are stored values and the
message does not echo them.

### The recovery

```bash
uv run whiskeyjack-bot verify-submission --config config.yaml \
  --record-id "$REC" --attempt-id "$ATTEMPT"
```

This makes a **GET and no post**. It reads no submission flags, so it works even with
submission switched off, and it is safe to run at any time. It re-runs the *same*
comparison the original submission ran, from the snapshot that submission stored — there is
one implementation of "did the refetch show what we posted", and both callers use it, so a
later verification cannot judge by a different rule than the attempt did.

It requires `METACULUS_TOKEN`, and it builds the poster **before** checking whether there
is anything to verify — so without a token you get exit `3` and a credential message even
when the record has no open uncertainty.

**The four outcomes:**

| Outcome | What happens | Record ends |
|---|---|---|
| `confirmed` | `submission_confirmed` recorded | `submitted` — done |
| `absent` | `submission_disconfirmed` recorded | `failed` — terminal |
| `unreadable` | **Refused**, nothing recorded | still `approved`, still blocked |
| `mismatched` | **Refused**, nothing recorded | still `approved`, still blocked — see [L3](#l3--a-mismatched-refetch) |

For `unreadable` the message is:

```
refused: the question could not be refetched, so nothing was established and nothing was recorded; the attempt stays unresolved
```

That one is simply **retryable** — the network or the platform was unavailable. Run it
again later.

### "Until a refetch resolves the state" does not mean the block lifts

This is the reading that catches people, and it is worth stating plainly. Both resolutions
are **terminal**: `confirmed` moves the record to `submitted` and `absent` moves it to
`failed`. Neither returns it to `approved`. So the next `submit` for that record is refused
either way — not by the uncertainty gate any more, but because the record is no longer
awaiting submission.

**There is no path by which retrying that submission becomes permissible.** A genuine retry
is a new forecast version behind a fresh human approval.

### How to find the attempt id if you lost it

There is no command that lists a record's open uncertainties. In order of preference:

1. **The `submit` output**, which printed the full `verify-submission` command line for you.
2. **The live submission artifact**, which is the durable copy. Each file is
   `submissions/live/<question_id>/<idempotency_key>.json` under `storage.artifact_root`,
   and its `receipt` block carries `attempt_id`, `forecast_record_id`, `success` and
   `refetch_outcome`. Find the file, then read the id out of it:

   ```bash
   grep -rl "\"forecast_record_id\": *\"$REC\"" "$ARTIFACT_ROOT/submissions/live/"
   ```

   ```bash
   uv run python -c "import json,sys; print(json.load(open(sys.argv[1]))['receipt']['attempt_id'])" <file>
   ```

   Both were run against a real live artifact; the JSON is written on one line, so `grep`
   finds it.
3. If neither exists — because the artifact was not written ([L5](#l5--the-artifact-was-not-written))
   and the terminal is gone — **there is no supported way to recover the id.** The
   submission modules do no logging, so the log file will not have it either.

**Filed as M1-611**, the read-only `show` command. `CODEX_HANDOFF.md:276` lists it as a
required entry point and it has never been built.

---

## Submission failures

### S1 — Live submission is off

**What you see** (exit `4`):

```
refused: live submission is off: it requires submission.enabled: true with dry_run: false and no_submit: false, and all three ship as the safe values
```

**This is the committed default and usually the correct state.** The gate runs before the
poster is constructed, so you are told submission is off rather than that a credential is
missing.

**Confirm (read-only).** Everything `submit` printed above the refusal — record, question,
version, type, derived status, hash, payload digest — is real and was read from the ledger
without writing anything. **While submission is off, `submit` is the closest thing to a
read-only inspection command this program has.**

**Recovery.** If you genuinely intend a live post, flip all three flags together and
re-run. That is a deliberate act.

**Never.** Do not flip the flags to get past some *other* error. If you are here because a
different command failed, this is not the fix.

### L1 — `submission_failed`

**What you see.** `result: submission_failed`. The post raised **and** the refetch observed
that nothing newer than the baseline is on the platform. Both halves are required; this is
the only cell in the partition that is an outright failure.

**Confirm (read-only).** `submit`'s own output said `(success=False, refetch=absent)`.
To check the record afterwards, probe it with `submit --record-id <REC>` while
`submission.enabled` is `false` and read the `status:` line — `failed`. The attempt row
itself is not readable by any command (**M1-611**).

**Recovery.** `failed` is terminal. The forecast was not posted. Make a new forecast
version and approve it if you still want to submit for that question.

**Never.** Do not attempt to resubmit the same record — it is no longer `approved` and the
attempt will be refused.

### L2 — A standing key reservation

**What you see.** After a `submit` refusal:

```
a key reservation is standing for this record (1); if you have confirmed nothing was posted, run
  whiskeyjack-bot release-key --record-id <REC> --released-by <you>
```

**Why it exists.** `submit` claims its idempotency key in a durable row *before* any
network I/O, so two concurrent commands for one derived key cannot both post. A process
killed between the claim and the attempt row leaves the claim standing. The key is a pure
function of tournament, question, forecast version and payload hash — the same work derives
the same key forever — so without a way out, one interrupted command would block that
forecast permanently.

**Confirm (read-only).** Run `submit` with submission disabled, or `release-key` when the
record holds more than one reservation; both list what is standing rather than guessing. If
exactly one is standing, `release-key` prints it before acting.

**Recovery — and read the next paragraph first.**

```bash
uv run whiskeyjack-bot release-key --config config.yaml \
  --record-id "$REC" --released-by "you@example" --note "checked, nothing posted"
```

`--released-by` is required and has no default, because releasing is **your assertion that
you went and looked at Metaculus and this forecast is not there**. The program cannot make
that claim. When the program *can* prove no post was made, it releases the key itself under
`not_posted`, naming nobody.

**Never — the one case where releasing is wrong.** If `submit` told you a post was made and
the ledger refused to record it, **the post landed**. Releasing invites a duplicate. That is
[L4](#l4--a-live-post-the-ledger-refused-to-record), and the command prints this warning
before every release:

```
releasing records that you checked Metaculus and this forecast is NOT there. If submit told you a post was made and the ledger refused to record it, the post did land -- do not release; resolve that attempt instead.
```

When there is nothing to release you get, harmlessly:

```
refused: no key reservation is standing for this record; there is nothing to release
```

### L3 — A `mismatched` refetch

**What you see.** `verify-submission` refuses:

```
refused: the platform holds a forecast that is not the one this attempt sent; that is not the same as the forecast being absent, and recording it as absent would end this forecast version on the wrong evidence -- resolve it by hand
```

**What it means.** Something newer than the pre-post baseline is on the platform, and it is
not what this attempt sent. That is genuinely ambiguous: it might be a forecast you posted
by another route, or someone else's action on the same account, or a real disagreement.

**Why nothing is recorded.** The verification vocabulary has two members, `confirmed` and
`absent`, and `absent` is terminal. Recording a mismatch as absent would kill a live
forecast version on evidence that *a* forecast exists. Leaving the uncertainty standing is
the conservative direction — the post gate stays shut and a human decides.

**Confirm (read-only).** Re-run `verify-submission`; it makes only a GET and will keep
telling you the same thing. Then look at the question on Metaculus yourself.

**Recovery today: there is none in this program.** The record sits `approved` with a
standing uncertainty that blocks every further submission for it, permanently.

The honest options are:
- Leave it. The record is not lost and the ledger is not wrong; it is *undecided*, which is
  the truth.
- If you must move forward on that question, make a **new forecast version** and approve
  it. The old record stays undecided in the ledger as history.

**Never.** Do not insert a `submission_verifications` row by hand to close it. Do not
release the key and re-post — something is already on the platform.

**Filed as M2-714.** Recording a human's judgement about a mismatch needs an actor, a note
and an approval-shaped boundary; it is an item, not a clause.

### L4 — A live post the ledger refused to record

**This is the most serious state in this document. Read it slowly.**

**What you see** (exit `4`):

```
refused: a live post was made and the ledger refused to record it (<reason>); the payload and receipt are at <path>
```

or, worse:

```
refused: a live post was made and the ledger refused to record it (<reason>); the artifact could not be written either
```

**What it means.** A forecast **is live on Metaculus**. Every gate in `submit` runs before
the post; this is the single named exception where an error follows a live call.

**What state you are in.**

- A live forecast on the platform.
- **No `submission_attempts` row** — the ledger has no record of the post.
- **A standing key reservation**, which is *not* released (the automatic release runs only
  when the program can prove no post was attempted).
- The forecast record still `approved`.

The ledger and the platform now disagree, which is the one outcome this instrument exists
to prevent.

**Confirm (read-only).** Open the artifact path from the message — it holds the exact
payload that was posted and the receipt. Then look at the question on Metaculus and compare.

**Recovery today: there is no command for this.** Nothing in the program records a post the
ledger missed.

What to do in the meantime:
1. **Do not release the key.** See the warning in [L2](#l2--a-standing-key-reservation).
2. **Do not submit again for this record.** The post landed.
3. **Preserve the artifact.** It is the only durable evidence of what was posted.
4. **Fix why the ledger refused** — the reason is in the message, and it is usually about
   the database rather than the forecast.
5. Escalate. This is an owner-level reconciliation, not an operator fix.

**Filed as M2-713 (Critical).**

### L5 — The artifact was not written

**What you see.** In `submit`'s output:

```
artifact:  NOT WRITTEN -- <reason>
```

**What it means.** The post happened and the ledger row **was** written; only the artifact
file failed. This is deliberate: after a live post the spend is real and irreversible, so
an artifact failure costs the payload record and nothing else. It never blocks the ledger.

The same policy applies after a paid model call: the forecast record is still appended,
with no `raw_output_path`.

**Confirm (read-only).** Check the expected path under `storage.artifact_root` —
`submissions/live/<question_id>/<idempotency_key>.json` — and find it absent. The attempt
row itself was written and is not readable by any command (**M1-611**); `submit`'s output at
the time is the record of what happened.

**Recovery.** The ledger record stands and is authoritative for *what happened*. What is
lost is the verbatim payload — `submission_attempts` stores its digest, not its body — so
an auditor can no longer re-derive the hash from the payload for that attempt. Fix the
underlying disk or permission problem so the next one is written.

One knock-on worth knowing: a **forecast** record whose raw model output was not
retained **cannot be replayed**. `replay --record-id` refuses it with `this record has no
recorded raw model output; it cannot be replayed, which is not the same as a replay that
did not match` — a distinction worth keeping straight when you are deciding whether to
trust a record.

**Never.** Do not reconstruct the artifact by hand. A payload file you wrote afterwards is
not evidence of what was sent.

---

## Never do this

1. **Never trust `submit`'s exit code.** It exits `0` for an uncertain and for a failed
   submission alike. The `result:` line is the answer.
2. **Never retry a submission whose outcome is uncertain.** Resolve it with
   `verify-submission` first. The program blocks this, and the block is the feature.
3. **Never edit the ledger with `sqlite3`.** Not to close a state, not to fix a status, not
   to release a key. Every table you would reach is append-only by trigger, and the ones
   you could reach are the ones whose immutability the whole product rests on. If a state
   seems to need it, it is one of the filed gaps below.
4. **Never release a key reservation after a post that may have landed.** If `submit` said
   a post was made and the ledger refused it, the post landed. Releasing invites a
   duplicate live forecast.
5. **Never flip the submission flags to get past an error.** They gate the only command
   that can post. Flip them only when you intend a live post.
6. **Never approve without `--forecast-sha256`.** The flag is what makes your approval a
   claim about the forecast you actually reviewed.
7. **Never approve a record that does not replay.** A `MISMATCH` verdict means the stored
   forecast no longer re-derives from its saved model output.
8. **Never delete artifacts under `storage.artifact_root`.** They are the only verbatim
   copy of posted payloads and raw model output, and they are the fallback when the ledger
   cannot answer.
9. **Never put a secret in a config file.** Credentials live in environment variables; the
   config carries the variable *name*. Diagnostics print names only, and so should you when
   you paste output into a ticket.

---

## When the only recovery would be a database edit

Five states in this document have no complete recovery through a documented command. They
are listed here rather than papered over with a SQL snippet, because a runbook that teaches
you to edit the ledger has destroyed the thing it documents.

| State | Section | Row | What is missing |
|---|---|---|---|
| A live post the ledger refused to record | [L4](#l4--a-live-post-the-ledger-refused-to-record) | **M2-713** (Critical) | A way to reconcile a post the ledger missed |
| A `mismatched` refetch | [L3](#l3--a-mismatched-refetch) | **M2-714** | A way to record a human's judgement closing it |
| An approved record needing re-approval | [A2](#a2--an-approved-record-cannot-be-re-approved-or-rejected) | **M2-715** | Either a legal path back, or a refusal that stops suggesting one |
| Finding a record's state, or a lost attempt id | [U](#how-to-find-the-attempt-id-if-you-lost-it) | **M1-611** | The read-only `show` command the spec requires |
| A `not_recorded` forecast | [R4](#r4--not_recorded-the-forecast-the-ledger-never-saw) | **M1-317** | A ledger identity for a post-generation persistence failure |

One further filed item is a nuisance rather than a stuck state: **M0-010**, `verify-env`
requiring the optional fallback retrieval key ([C4](#c4--environment-not-ready)).

If you hit a state that is not in this document and whose only exit appears to be a
database edit, that is a sixth gap and it is worth a backlog row. File it rather than
reaching for `sqlite3`.
