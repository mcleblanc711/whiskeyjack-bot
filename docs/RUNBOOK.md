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
  `lifecycle_events` row (`lifecycle.py:current_status`). Every command prints the derived
  value. A record whose `status` column reads `draft` may well be `approved`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | OK |
| `2` | Configuration invalid (or a bad command line — argparse also uses `2`) |
| `3` | A required environment variable or file is missing |
| `4` | Refused, or did not fully succeed. Usually nothing was written — but see `submit` below |

Defined at `env_verify.py:EXIT_OK`, `env_verify.py:EXIT_CONFIG_INVALID`,
`env_verify.py:EXIT_ENV_MISSING` and `cli.py:EXIT_REFUSED`.

**Exit code `4` is the good outcome when something is wrong.** Almost every refusal in this
program runs *before* the action it refuses, so `4` normally means nothing happened. Two
commands break that and both are documented below: `submit`, and a live post the ledger then
refused to record ([L4](#l4--a-live-post-the-ledger-refused-to-record)), which
`reconcile-submission` records.

**`submit` exits `0` if and only if the refetch confirmed the post *and* the artifact was
written.** The return is literally:

```python
return EXIT_OK if receipt.verified_by_refetch and recorded.artifact_path else EXIT_REFUSED
```

(`cli.py:_run_submit`), and `verified_by_refetch` is not an independent fact — it is exactly
`refetch_outcome == "confirmed"`
(`lifecycle.py:SubmissionAttempt.verified_by_refetch`). **So the exit code is decided by
those two conditions and by nothing else.** In particular it is *not* decided by the
`result:` line: the recorded outcome and the exit code partition the same attempts
differently, and reading either as a proxy for the other is the mistake this section exists
to prevent.

The full grid — eight `(success, refetch_outcome)` combinations, each with the artifact
written or not (`lifecycle.py:record_submission_attempt` derives the event;
`cli.py:_run_submit` the exit):

| `success` | `refetch_outcome` | `result:` | Artifact | Exit |
|---|---|---|---|---|
| `True` | `confirmed` | `submitted` | written | **`0`** |
| `False` | `confirmed` | `submission_uncertain` | written | **`0`** |
| `True` | `confirmed` | `submitted` | NOT WRITTEN | `4` |
| `False` | `confirmed` | `submission_uncertain` | NOT WRITTEN | `4` |
| `True` | `absent` / `mismatched` / `unreadable` | `submission_uncertain` | either | `4` |
| `False` | `absent` | `submission_failed` | either | `4` |
| `False` | `mismatched` / `unreadable` | `submission_uncertain` | either | `4` |

Two rows are worth reading twice.

**`submission_uncertain` can exit `0`.** `(success=False, refetch=confirmed)` is the ordinary
lost-response recovery: the POST raised — a timeout, a dropped connection — and the refetch
then *found the forecast on the platform*. The post landed, the artifact was written, the
command exits `0`, and the outcome is still recorded as uncertain because the attempt and the
platform were not observed together. **You still owe it a `verify-submission`**, and the
command tells you so in its last line; a `0` here does not close the uncertainty. See
[**U**](#the-uncertain-timeout).

**A confirmed post whose artifact could not be written exits `4`.** The forecast is live on
Metaculus behind a non-zero exit. See [L5](#l5--the-artifact-was-not-written).

**So `4` from `submit` does not mean nothing happened and does not mean no post was made; `0`
does not mean there is nothing left to do.** Read the `result:` and `artifact:` lines and the
instruction line beneath them. Never infer the outcome from the exit code, and never script a
retry on it.

Uncertainty is also not one condition, and the `refetch=` half of the `result:` line is
which one. `confirmed` is "the forecast is on the platform"; `absent` is "the refetch looked
and found nothing newer"; `mismatched` is "something newer is there and it is not what this
attempt sent"; `unreadable` is "the platform could not be read at all". They are not
interchangeable. **Note that `absent` is the one that is not always uncertainty** — paired
with `success=False` it is the single combination the program calls an outright failure (row
six above). `mismatched` is the one with no operator-closable path today
([L3](#l3--a-mismatched-refetch), and **M2-714**); the full breakdown is at
[the uncertain timeout](#what-it-means).

`run` is a milder version of the same thing: it exits `4` if any question failed or if it
forecast none, so a partial batch is a non-zero exit even though the records that succeeded
were written.

---

## Symptom index

| What you are looking at | Section |
|---|---|
| `no ledger database at ...` | [C1](#c1--no-ledger-database) |
| `record_id does not name a stored forecast record` | [P0](#p0--two-profiles-one---config-and-the-error-that-looks-like-data-loss) — check `--config` first |
| `Tournament refused: tournament activation is disabled` | [P1](#p1--the-cup-profile-is-dormant-its-refusals-are-correct) |
| `activation retired: ... changed; re-run tournament enable` | [C5](#c5--activation-retired) |
| Polls succeed, heartbeats are fresh, and every poll discovers 0 questions while MiniBench has open ones | [P3](#p3--minibench-rolled-over-to-a-new-project) |
| ntfy push `whiskeyjack: MINIBENCH ROLLED OVER` | [P3](#p3--minibench-rolled-over-to-a-new-project) |
| ntfy push `whiskeyjack: MiniBench rollover cleared` | [P3](#p3--minibench-rolled-over-to-a-new-project) — the activation covers the series again; nothing to do |
| ntfy push `whiskeyjack: rollover check failed` | [P3](#when-the-rollover-check-itself-fails) — the detector is blind, the worker is not down |
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
| `unrecorded-posts` lists a record, or `submit` refused with `durable submission intent` | [L4](#l4--a-live-post-the-ledger-refused-to-record) |
| `reconcile-submission` refused | [L4](#l4--a-live-post-the-ledger-refused-to-record) — the refusal table |
| `artifact:  NOT WRITTEN -- ...` | [L5](#l5--the-artifact-was-not-written) |
| ntfy push `whiskeyjack: whiskeyjack-resolutions failed` | [Scheduled ingestion and scoring](#scheduled-ingestion-and-scoring) |
| ntfy push `whiskeyjack: a live forecast is missing from the ledger` | [L4](#l4--a-live-post-the-ledger-refused-to-record) |
| ntfy push `whiskeyjack: RESOLUTIONS SCHEDULE STOPPED` | [W1](#w1--resolutions-schedule-stopped) |
| ntfy push `whiskeyjack: resolutions schedule recovered` | [W1](#w1--resolutions-schedule-stopped) — the condition cleared; nothing to do |
| ntfy push `whiskeyjack: WORKER DOWN` | [The watchdog itself](#the-watchdog-itself) |
| ntfy push `whiskeyjack: watchdog state unwritable`, or a `STATE:` line in the watchdog's journal | [W2](#w2--watchdog-state-unwritable) — every check still runs |
| `tournament status` shows `unrecorded_posts` above 0 | [L4](#l4--a-live-post-the-ledger-refused-to-record) |

---

## Before you start

### P0 — Two profiles, one `--config`, and the error that looks like data loss

There are two tournament profiles, and **every command in this document takes `--config`**.
The ledger, artifact root, export root and log file all move with that flag. Point it at the
wrong profile and the command reads a different database.

| | MiniBench | Metaculus Cup Fall 2026 |
|---|---|---|
| config | `config/tournament.yaml` | `config/tournament-cup.yaml` |
| project | 33125 (was 33122 until 2026-09-21, see [P3](#p3--minibench-rolled-over-to-a-new-project)) | 33108 |
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
from project 33108, MiniBench from 33122 (Sep 7-20) and 33125 (from Sep 21).

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

### P3 — MiniBench rolled over to a new project

**What you see.** An ntfy push titled `whiskeyjack: MINIBENCH ROLLED OVER`, priority
`urgent`, within one watchdog run (five minutes) of the first open question on the new project
(M1-347). Nothing else looks wrong: `tournament run-once` exits 0 every five minutes, heartbeats
are fresh, `WORKER DOWN` stays quiet, and every poll discovers 0 questions. On Metaculus,
MiniBench questions are open and nobody is forecasting them. Before M1-347 that was *all* you
saw, which is how the rollover of 2026-09-21 went unnoticed for about twelve hours.

The watchdog asks Metaculus what the `minibench` slug resolves to
(`/api/projects/tournaments/minibench/`, the slug the pinned SDK names as
`CURRENT_MINIBENCH_ID`) and compares that project id with `project_id` on the newest
`activation` event in the ledger. It pages only when they differ **and** the new project has at
least one open question — a new series with nothing open yet has nothing to lose. The push
carries no project id or count, so read both from the commands under **Confirm**. It re-pages
hourly while the mismatch stands, and at once if the series moves again to a third project.
When the activation covers the series again you get one `whiskeyjack: MiniBench rollover
cleared` notice. The page changes nothing: re-pointing stays your action.

**Why.** Metaculus runs MiniBench as a series of projects. On 2026-09-21 00:00 UTC it moved
from project 33122 to project 33125. An activation binds to one concrete project, so the worker
kept polling 33122, which had nothing open. Each MiniBench question is open for three hours, so
a question that opens and closes while the worker is on the old project is lost, not delayed.
That day cost 13 questions over about 12 hours.

**Confirm.** Open the MiniBench tournament page on Metaculus and read the project id of the
open questions. Compare it with `project_id` in `tournament status`. The watchdog's journal
names which state it saw (`journalctl --user -u whiskeyjack-watchdog.service -n 5 -o cat`).

**Recovery.** Two steps, in this order:

1. Set `metaculus.tournament.id` in `config/tournament.yaml` to the new project id. This retires
   the current activation ([C5](#c5--activation-retired)) because both `destination` and
   `configuration` moved, so the worker refuses from the next poll.
2. Bind the new project, with the new series' window and the budget you intend:

   ```bash
   uv run whiskeyjack-bot tournament enable --config config/tournament.yaml \
     --project-id '<new project id>' --starts '<UTC ISO timestamp>' --ends '<UTC ISO timestamp>' \
     --budget-usd 40
   ```

   `--starts` is the activation window's lower bound, and nothing else. The worker checks it
   against the current time on every poll (`require_activation`); it does **not** filter
   questions by when they opened. A `--starts` in the future makes every poll refuse as
   inactive until that moment, and any question that closes in the meantime is lost. So set it
   to now or earlier — the series' own start is the natural choice.

Spending is tracked per project, so the new activation starts with its full budget.

**Never.** Do not run `tournament disable` to tidy up the old series. It disables the
**latest** activation (`tournament_state.py`, `disable`), and once step 2 is done that is the
new one. The old activation is already superseded and needs nothing.

#### When the rollover check itself fails

An ntfy push titled `whiskeyjack: rollover check failed`, priority `default`, naming one of:

| line | what went wrong |
|---|---|
| the live activation's project could not be read from the ledger | no `activation` event, a ledger the watchdog cannot open read-only, or a `project_id` that is not a positive integer |
| Metaculus did not answer with a usable MiniBench project | the GET failed (network, HTTP error, a redirect, which is refused so the token cannot follow it) or answered something without a positive integer `id` |
| Metaculus did not answer with a usable list of MiniBench's open posts | the projects differ, and the open-posts GET failed or answered something that is not a list of posts |
| the check did not finish within 15 s | the reads, together, outran their wall-clock bound |

Those four lines are the whole vocabulary. **The worker is not down** — this page never says so,
and `WORKER DOWN` has its own rule. What it means is that a rollover would go unreported until
the check can run again: usually a Metaculus outage or a slow answer, and it clears by itself on
the next good read, silently. It re-pages every six hours while it stands. A failed read never
clears or re-sends a `MINIBENCH ROLLED OVER` that is already standing.

If it does not clear: `METACULUS_TOKEN` must be in `.env` (the watchdog unit loads it; Metaculus
refuses the API to unauthenticated callers), and the watchdog's journal shows which line it hit.

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
(`config.py:SubmissionConfig._reject_live_submit_combinations`). Flipping them is a
deliberate act, not something you do to get past an error message.

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
| `unrecorded-posts` | **read-only** | no | no |
| `reconcile-submission` | appends a reconciliation and an event | **GET** (no post) | no |
| `verify-submission` | appends an event | **GET** | no |
| `ingest-resolutions` | appends resolution rows and `resolved` events | **GET** | no |
| `score` | appends local score rows and `scored` events | no | no |
| `run` | writes records | **yes** | **YES — retrieval and model calls** |
| `submit` | appends an attempt | **POST** | posts a forecast |
| `tournament correct-costs` | **read-only** | no | no |
| `tournament correct-costs --apply` | appends `cost_corrected` events | no | no |

`run` is the live paid path and `run-replay` is the free one. The money boundary is a
**subcommand name**, not a flag, deliberately (M1-315).

`tournament correct-costs` (M1-348) corrects what the budget guard **counts**, not what was
billed. Before M1-348 every bring-your-own-key model call (GPT-6 Astra) settled at $0, because
OpenRouter reports `usage.cost: 0` for BYOK and the real charge in
`usage.cost_details.upstream_inference_cost`. The command reads each reservation settled at 0,
finds its stored `model_response`, and appends one `cost_corrected` event at the upstream
figure. It makes no network call and needs no activation. Run it without `--apply` first: the
dry run opens the ledger read-only and prints `reservations` and `total_usd`. A second
`--apply` writes nothing (`already_corrected` counts what an earlier run wrote).
`refused_no_upstream_figure` counts reservations settled at 0 with no BYOK figure. On the live
ledger these are the Exa searches, which really do settle at 0. `tournament status` then
reports `actual_cost_usd` including the corrections.

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

### 6. Ingest resolutions

```bash
uv run whiskeyjack-bot ingest-resolutions --config config.yaml [--question-id ID]
```

Fetches every post this ledger has a `submitted` (or later) record for — one GET per post — and
appends what the platform shows (M4-801). Safe to run at any time and as often as you like: a
poll that sees nothing new writes nothing. One line per record, then a count:

```
question 45747  record 0192...  appended  kind resolved  scorable yes  -> resolved
question 45748  record 0192...  unchanged  kind -  scorable -
records: 2  failed: 0
```

`kind` is one of five, and only `resolved` can ever be scored — `score_events` refuses a row
for anything else, whatever wrote it:

| kind | means | record moves to `resolved`? |
|---|---|---|
| `resolved` | a definite outcome | yes (first time) |
| `annulled` / `ambiguous` | the platform cancelled the question | yes (first time) |
| `withheld` | status resolved, value `null` | no |
| `unresolved` | the platform retracted an earlier resolution | no |

**`withheld` is an access fact, not a bug.** Metaculus returns a resolution value only for a
question the account predicted on (its API docs, "All Authenticated Accounts"). Every record
this command polls was posted, so `withheld` should be rare; if every record reads `withheld`
after questions have resolved, the account cannot see its own resolutions — do not score, and
raise it with Metaculus.

Exit `4` means at least one record printed `failed:`; every other record was still recorded.
A `failed` line names a rule, never a value. Re-running is safe.

This step and step 7 also run on a timer every six hours (M4-805); see
[Scheduled ingestion and scoring](#scheduled-ingestion-and-scoring). Running them by hand is still
safe at any time.

### 7. Score resolved forecasts

```bash
uv run whiskeyjack-bot score --config config.yaml [--record-id ID]
```

Computes **local** Brier and natural-log scores for every binary and multiple-choice record with
a `resolved` latest observation, and appends them (M4-802, D36). No network call, no paid call.
Run it after step 6; like step 6 it is safe to repeat — scoring an observation already scored
writes nothing.

```
question 45747  record 0192...  binary  appended  rows 2  -> scored
question 45748  record 0192...  multiple_choice  unchanged  rows 0
question 45749  record 0192...  numeric  out_of_scope  rows 0
question 45750  record 0192...  binary  not_scorable  rows 0
records: 4  failed: 0
```

- **These are not Metaculus scores.** The metrics are `local_brier_binary`, `local_log_binary`,
  `local_brier_multiclass` and `local_log_multiclass`; none is a baseline or peer score, and none
  should be quoted as one. Platform scores are M4-803.
- `not_scorable`: no observation yet, or the latest is `annulled`/`ambiguous`/`withheld`/
  `unresolved`. `out_of_scope`: numeric and discrete are never scored locally.
- **A re-resolution adds rows; nothing is overwritten.** Each score row names the resolution
  row it measured (`resolution_event_id`). A record's current score is the rows citing its
  latest resolution.
- A `failed` line names a rule. The one you may meet in practice is a multiple-choice question
  that resolved to an option the forecast never priced (an option added after forecasting),
  which is refused rather than guessed. Exit `4` if any record failed.

### Scheduled ingestion and scoring

Steps 6 and 7 run unattended (M4-805), from `deploy/systemd/whiskeyjack-resolutions.service` and
its timer, against the MiniBench profile (`config/tournament.yaml`) — the same profile, `.env` and
interpreter as the tournament poll.

- **When:** every six hours, at 00:23, 06:23, 12:23 and 18:23 **host local time**. That is clear
  of the poll (`*:0/5`) and the watchdog (`*:2/5`). `Persistent=true` means a run missed while
  the machine was off happens at the next boot.
- **What:** `ingest-resolutions`, then `score`. `score` runs **only if ingestion exited 0**.
  Neither command costs money, and neither can post. Both are idempotent.
- **How you hear about a failure:** a non-zero exit from either command, or a run past
  `TimeoutStartSec`, fails the unit. That starts the same `whiskeyjack-notify@` pager the poll
  uses. The push reads `whiskeyjack: whiskeyjack-resolutions failed` with
  `result=exit-code exit=N`, and `N` is the exit code of whichever command failed (table
  [above](#exit-codes)). Because the pager throttles to one push per unit per 30 minutes, a
  failure that keeps happening pages once per run, four times a day.
- **A `withheld` observation does not page.** It exits `0`, by design (step 6), so check the
  `kind` column in the journal after questions resolve.
- **The Cup profile is not scheduled.** Its ledger is still at schema 13.

When the push arrives:

```bash
journalctl --user -u whiskeyjack-resolutions.service -n 80 --no-pager   # which command, which lines
systemctl --user show whiskeyjack-resolutions.service -p Result -p ExecMainStatus
```

1. Read the `failed:` lines. Each names a rule (step 6 and step 7 say what the rules mean).
   Every record not printed `failed` was still recorded.
2. If ingestion failed, scoring did not run this cycle. Once the cause is fixed, run step 6 and
   then step 7 by hand, or wait for the next run.
3. A `refused:` or `no ledger database` line means nothing was written. Handle it like any other
   configuration or ledger failure ([C1](#c1--no-ledger-database)–[C5](#c5--activation-retired)).
   A just-merged migration refuses until `init-ledger` runs. `missing env var` / exit `3` means
   `METACULUS_TOKEN` is missing from `.env`.
4. `failed: the ledger could not complete this transaction (detail withheld: ...)` means another
   writer held the ledger's write lock for more than five seconds. Usually that is a manual run
   overlapping the scheduled one, or a forecasting poll mid-write. Nothing is corrupted: each
   record's write is its own `BEGIN IMMEDIATE` transaction, every Metaculus GET happens outside a
   transaction, and the refused record is simply picked up by the next run. You can also re-run
   it by hand.

**Installing or changing the unit** (the files under `deploy/systemd/` are copied, not linked):

```bash
cd ~/projects/whiskeyjack-bot && git pull --ff-only
cp deploy/systemd/whiskeyjack-resolutions.service deploy/systemd/whiskeyjack-resolutions.timer \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start whiskeyjack-resolutions.service          # one run now, in the foreground
systemctl --user show whiskeyjack-resolutions.service -p Result -p ExecMainStatus
systemctl --user enable --now whiskeyjack-resolutions.timer
systemctl --user list-timers whiskeyjack-resolutions.timer
```

**Timers are not self-healing, and the watchdog is what now notices** (M1-341). A disabled or
stopped `whiskeyjack-resolutions.timer` never runs, so it never fails, so the `OnFailure` pager
above says nothing — that half of the pipeline just goes quiet. Since M1-341 the out-of-process
watchdog checks this timer as well as the poll, and pages
[**W1**](#w1--resolutions-schedule-stopped) within one watchdog interval (five minutes). To pause
the schedule deliberately, `systemctl --user disable --now whiskeyjack-resolutions.timer` — and
expect the page, once a day, until you start it again.

### W1 — `RESOLUTIONS SCHEDULE STOPPED`

**What you see.** An ntfy push titled `whiskeyjack: RESOLUTIONS SCHEDULE STOPPED`, priority
`high`, listing one or more of:

| line | what the watchdog asked |
|---|---|
| the timer is not active, so no run is scheduled | `systemctl --user is-active whiskeyjack-resolutions.timer` answered anything but `active` |
| the timer is not enabled, so it will not survive a reboot | `is-enabled` answered anything but `enabled` |
| the last run FAILED | `is-failed whiskeyjack-resolutions.service` answered `failed` |
| the timer is active and enabled but has not fired within its own interval | `show -p LastTriggerUSec` on the timer is more than **seven hours** old — its six-hour interval plus a one-hour margin (M1-343) |

Those four lines are the whole vocabulary: the body is built by indexing that table's codes, so
a line you see is one of these and nothing else.

There is also a `last run: result=… exit=…` line, which is `Result` and `ExecMainStatus` — the
same two fields the `whiskeyjack-notify@` pager quotes — and, for a stall only, a `last fired:
N h ago (interval 6 h + margin 1 h)` line. Exit codes are in the [table above](#exit-codes).

**What it means.** Resolution ingestion and scoring are not scheduled. **Nothing is lost while
it is stopped**: `ingest-resolutions` and `score` are both idempotent, `Persistent=true` means a
missed run happens at the next start, and no forecast or approval is affected. What stops is
resolutions and scores *arriving* — so `resolution_events` and `score_events` quietly stop
growing while everything else looks healthy.

**What to do.**

```bash
systemctl --user list-timers whiskeyjack-resolutions.timer
systemctl --user enable --now whiskeyjack-resolutions.timer     # covers inactive AND disabled
journalctl --user -u whiskeyjack-resolutions.service -n 80 --no-pager
```

For a **timer that is running but has not fired**, the schedule itself is what to look at —
the unit is active and enabled, so nothing about its *state* is wrong:

```bash
systemctl --user show whiskeyjack-resolutions.timer -p TimersCalendar -p NextElapseUSecRealtime
systemd-analyze calendar '*-*-* 00/6:23:00'      # does the expression still match anything?
timedatectl                                      # did the clock move?
systemctl --user restart whiskeyjack-resolutions.timer
```

`Persistent=true`, so a restart fires the missed run within a minute and the condition clears
itself. **Three things deliberately do not page this way**, each refused by its own rule rather
than by luck: a timer that has *never* fired (a fresh install answers an empty
`LastTriggerUSec`), a timer restarted inside the last seven hours (it is recovering, not
stalled), and a window the watchdog did not watch end to end — a host that was off, asleep or
logged out. The last is why a stall is reported some hours *after* a reboot rather than at once:
the watchdog keeps its own observation record in `~/.local/state/wj-watchdog.json`
(`observed.since`) and will not claim a gap it did not see.

Once reported, a stall **stays** reported until the timer is seen to fire again — a failed
`systemctl` query or a reboot stops the watchdog re-checking it, and neither is evidence about
the timer. So a page is not repeated when the check goes blind, and
`resolutions schedule recovered` is never sent while a stall is standing: the only thing that
clears one is a `LastTriggerUSec` inside the window.

For a **failed last run**, the cause is in the journal and the remedies are the ones in
§ Scheduled ingestion and scoring above — it is the same failure the `OnFailure` pager reports,
restated by the watchdog because a unit left in `failed` state stays there until its next run.
A `systemctl --user reset-failed whiskeyjack-resolutions.service` clears the state once the
cause is fixed; the next successful run clears it anyway.

**When the page stops.** The watchdog re-pages a standing condition **once a day**, not once
per five-minute run — but a *new* fault appearing alongside the old one pages at once, because
that is new information. When the condition clears you get one
`whiskeyjack: resolutions schedule recovered` notice and nothing further.

**A page that names a timer you can see running** means the watchdog is pointed at a unit that
does not exist: `systemctl --user is-active` answers `inactive` for a missing unit exactly as it
does for a stopped one. Compare `RESOLUTIONS_UNIT` in `deploy/wj-watchdog` against the filenames
in `deploy/systemd/` — `tests/unit/test_watchdog.py` asserts they agree, so this should only ever
be reachable from a hand-edited installed copy.

### W2 — `watchdog state unwritable`

**What you see.** An ntfy push titled `whiskeyjack: watchdog state unwritable`, priority
`default`, at most once a day. It names the state file (`~/.local/state/wj-watchdog.json`) and
an errno name (`EACCES`, `EROFS`, `ENOSPC`...). Every run's journal also carries a `STATE:`
line for as long as the condition holds.

**What it means.** The watchdog could not write its state file. Every check still ran, and
this is **not** why a run exits `1`. The throttle stamps went to
`$XDG_RUNTIME_DIR/wj-watchdog.json` instead (a tmpfs, `/run/user/1000`), and the next run
reads them back from there, so each subject still pages at its documented rate. That tmpfs is
cleared at reboot, so after a restart each standing fault may page once more.

If the line reads `... or the runtime-directory fallback; throttle windows cannot hold`, then
neither file could be written. No bound is possible then: standing faults page on **every** run
until one of the two is writable again. This push is not sent in that case, because it would
repeat every run too.

**What to do.** `ls -l ~/.local/state/wj-watchdog.json` and `df -h ~/.local/state`. Fix the
mode or free the space. The next run writes the file, removes the fallback and prints
`STATE: ... is writable again`. Nothing needs restarting.

### The watchdog itself

`deploy/wj-watchdog` runs every five minutes from `deploy/systemd/whiskeyjack-watchdog.timer`
(`OnCalendar=*:2/5`, offset from the poll's `*:0/5` and clear of the resolutions timer's `:23`).
It is the only liveness check that survives the program being broken, so it is **stdlib-only,
runs under the system interpreter rather than the venv, imports nothing from the package, reads
the ledger read-only and touches no `AppConfig` field** — it cannot change `config_sha256` and
so cannot retire a live activation ([C5](#c5--activation-retired)).

It watches three subjects with **separate state, separate throttles and separate pushes**: the
poll (`whiskeyjack: WORKER DOWN`, heartbeat staleness, 60-minute re-alert),
the resolutions schedule (W1 above, no heartbeat rule, 24-hour re-alert) and the MiniBench
series ([P3](#p3--minibench-rolled-over-to-a-new-project), hourly re-alert; its failed check
re-alerts every six hours). None can mute another, and "worker recovered" is never sent while
the schedule is still stopped.

The third is the only one that touches the network for its evidence: one read-only GET to
Metaculus per run, and a second only when the projects differ, all under one 15-second
wall-clock bound, authenticated with `METACULUS_TOKEN` from the unit's `.env`.

A run makes eleven `systemctl` queries at most and prints three `OK:` lines. The resolutions half
asks its two timestamp queries (`LastTriggerUSec`, `ActiveEnterTimestamp`) only while the timer
is active and enabled, and asks them under `TZ=UTC LC_ALL=C`, because `systemctl show` renders
timestamps in the client's local zone and `--timestamp=` does not change that.

Its own unit deliberately has **no `OnFailure=`**: a notifier launched by this unit's own
failure is not independent evidence about this unit. A dark host — machine off, user session
gone — is covered only by setting `WJ_HEALTHCHECK_URL` to an external dead-man service, which
alerts when the watchdog's pings stop.

**Installing or changing it** (copied, not linked, like the other units):

```bash
cd ~/projects/whiskeyjack-bot && git pull --ff-only
cp deploy/wj-watchdog ~/.local/bin/wj-watchdog && chmod +x ~/.local/bin/wj-watchdog
cp deploy/systemd/whiskeyjack-watchdog.service deploy/systemd/whiskeyjack-watchdog.timer \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start whiskeyjack-watchdog.service    # one check now, in the foreground
journalctl --user -u whiskeyjack-watchdog.service -n 5 -o cat --no-pager
systemctl --user enable --now whiskeyjack-watchdog.timer
```

A healthy run prints three `OK:` lines, one per subject, and exits `0`. Any fault exits `1`; the
unit has no `OnFailure`, so that exit is visible in `systemctl --user status` and nowhere else.

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
- **`resolved` and `scored` both have writers.** `ingest-resolutions` (step 6) moves a record
  to `resolved`; `score` (step 7) moves a binary or multiple-choice record to `scored`. A numeric
  or discrete record stops at `resolved` until M4-803. A later re-resolution or retraction is
  appended as a row, not as another lifecycle event (there is no `resolved -> resolved` or
  `scored -> scored` transition); a record's current resolution is always its latest row.

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
(`lifecycle.py:record_submission_attempt`). This table is the one to internalise:

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

`submission_live.py:classify_refetch` decides the outcome against a baseline taken
**before** the post:

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
(`ledger.py:_verify_checksum`).

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
that attempt id, but **no command can show it to you.** `show --record-id` (**M1-611**)
does not help here: a pre-forecast failure has no `forecast_records` row to key on at all,
so there is nothing for it to be shown against — `pipeline_failure_events` is keyed by
`attempt_id` alone. The log file at `logging.file` has the same information.

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
R1, no command reads it back, and `show --record-id` does not apply for the same reason.

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

A post was made, or attempted, and the attempt and the platform were not observed *together*
as either "it is there and it is what we sent" or "nothing newer than the baseline is there".
It is **not** a failure and **not** a success. The record stays `approved`, which is what
keeps it resolvable.

Read the `refetch=` half of the `result:` line, because uncertainty covers four different
situations and they are not interchangeable:

| `refetch=` | What was observed | Exit | Resolvable? |
|---|---|---|---|
| `confirmed` (with `success=False`) | The POST raised, then the refetch **found the forecast on the platform**. The post landed. | `0` if the artifact was written | yes — `verify-submission` closes it |
| `absent` (with `success=True`) | The POST returned, then the refetch found nothing newer. The two disagree. | `4` | yes |
| `unreadable` | The platform could not be read at all. No observation was made. | `4` | yes — retryable, read again |
| `mismatched` | Something newer is there and it is **not** what this attempt sent. | `4` | **no** — see [L3](#l3--a-mismatched-refetch) and **M2-714** |

**The first row is the one that surprises people: it exits `0`.** A `0` from `submit` does not
mean the uncertainty is closed — the command's own last line still tells you to run
`verify-submission`, and the blind-retry gate below is still shut. Do not read the exit code
as an all-clear; see [Exit codes](#exit-codes).

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

```bash
uv run whiskeyjack-bot show --config config.yaml --record-id "$REC"
```

**`show --record-id` (M1-611) is the read-only command for this.** It reads the ledger only
(`ledger.connect_readonly`) and lists every `unresolved uncertainties` attempt id the record
holds, each printed with the exact `verify-submission --attempt-id` command line to resolve
it — no artifact file, log or scrollback required. If it prints `unresolved uncertainties:
none`, there is nothing outstanding for that record.

If the ledger itself is unreachable, two fallbacks remain, in order of preference:

1. **The `submit` output**, which printed the full `verify-submission` command line at the
   time.
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

If none of these work, the submission modules do no logging, so the log file will not have
it either.

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
without writing anything. `show --record-id` (M1-611) reads the same state, and more of it
(approval, full lifecycle history, unresolved uncertainties, standing reservations), without
needing `submission.enabled` off to do it.

**Recovery.** If you genuinely intend a live post, flip all three flags together and
re-run. That is a deliberate act.

**Never.** Do not flip the flags to get past some *other* error. If you are here because a
different command failed, this is not the fix.

### L1 — `submission_failed`

**What you see.** `result: submission_failed`. The post raised **and** the refetch observed
that nothing newer than the baseline is on the platform. Both halves are required; this is
the only cell in the partition that is an outright failure.

**Confirm (read-only).** `submit`'s own output said `(success=False, refetch=absent)`.
`show --record-id <REC>` (M1-611) now gives the same `status: failed` line without
disabling submission first, plus the `lifecycle history` entry for the transition itself
(attempt id, `detail_code`, timestamp). The full `submission_attempts` row — HTTP status,
response body and headers, error text — is still not readable by any command.

**Recovery.** `failed` is terminal. The forecast was not posted. Make a new forecast
version and approve it if you still want to submit for that question.

**Never.** Do not attempt to resubmit the same record — it is no longer `approved` and the
attempt will be refused.

### L2 — A standing key reservation

**What you see.** After a `submit` refusal:

```
a key reservation is standing for this record (1); if you have confirmed nothing was posted, run
  whiskeyjack-bot release-key --record-id <REC> --released-by <you>
if the forecast IS on Metaculus, do not release; record the post with
  whiskeyjack-bot reconcile-submission --record-id <REC> --observed-by <you> --note "<what you saw>"
```

Since M2-713 this is printed only for a reservation that is genuinely standing — unreleased
and spent by nothing. Before it, a reservation an ordinary post had already spent was listed
too, so a refusal for a record that was simply `submitted` told you to release its key.

**Why it exists.** `submit` claims its idempotency key in a durable row *before* any
network I/O, so two concurrent commands for one derived key cannot both post. A process
killed between the claim and the attempt row leaves the claim standing. The key is a pure
function of tournament, question, forecast version and payload hash — the same work derives
the same key forever — so without a way out, one interrupted command would block that
forecast permanently.

**Confirm.**

```bash
uv run whiskeyjack-bot show --config config.yaml --record-id "$REC"
```

`show --record-id` (M1-611) lists every standing reservation for a record — `standing key
reservations`, with each reservation id, key, sequence number and timestamp — read-only,
whether it is the only one or one of several. `unrecorded-posts` (M2-713) remains the command
for the part of this that matters most across the whole ledger: reservations whose command
committed a submission intent, which is to say reached the POST, so the forecast may be live —
check each on Metaculus and go to [L4](#l4--a-live-post-the-ledger-refused-to-record) if it is
there. A standing reservation it does *not* list never reached the POST.

Before `show` existed, two things came close to a per-record listing and neither was one:
`release-key` with no `--reservation-id`, when the record holds more than one reservation,
refused and listed all of them as a side effect (`cli.py:_run_release_key`); with exactly
one standing, `release-key` printed it and then released it in the same act. `show`
replaces both as the inspection step — `release-key` is still the only way to act on what
it shows.

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
releasing records that you checked Metaculus and this forecast is NOT there. If submit told you a post was made and the ledger refused to record it, the post did land -- do not release; record it with reconcile-submission instead.
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

**What you see.** One of three things, and the third is nothing at all.

`submit` exits `4` with:

```
refused: a live post was made and the ledger refused to record it (<reason>); the payload and receipt are at <path>; do not release the key or submit again -- once you have confirmed the forecast on Metaculus, record the post with reconcile-submission
```

or the same message ending `the artifact could not be written either; do not release ...`.

**Or the command was killed while it was posting** — Ctrl-C during `submit`, systemd's
`TimeoutStartSec` on a long poll, the OOM killer, a reboot. Nothing is printed on the command
itself. On the live worker the next poll confirms the forecast in the tournament journal,
pushes `whiskeyjack: forecast confirmed` and posts the private comment, so *that* push looks
like success.

**What tells you anyway (M1-342).** The same poll pages:

```
whiskeyjack: a live forecast is missing from the ledger
A forecast is live on the platform and the lifecycle ledger has not recorded the post, so it
is never resolved or scored. record=<record id> question=<question id>. Check the question on
Metaculus, then record it with `reconcile-submission` (runbook L4). Nothing will retry this
on its own.
```

and `tournament status` counts it:

```json
"unrecorded_posts": 1
```

The page repeats **once a day per record** until someone reconciles it, and it is keyed on the
record, so two records in this state both page. It is a read: the poll writes nothing to the
lifecycle ledger and posts nothing for it, and the count deliberately does **not** feed
`unresolved`, so the poll's exit code and its systemd unit stay green — a unit that failed
every five minutes over a condition only a person can clear would page through
`whiskeyjack-notify@` on every poll for days.

A **zero** count is the healthy reading, and it stays zero through resolution and scoring: the
predicate is "the ledger never recorded the post", not "the record is not `submitted` right
now", which every resolved forecast stops being.

**What it means.** A forecast **is live on Metaculus** and the lifecycle ledger does not know:

- **no `submission_attempts` row** — the post is unrecorded;
- **a standing key reservation** — the automatic release runs only when the program proved
  nothing was posted;
- the forecast record still **`approved`**, so ingestion and scoring never see it.

What the ledger *does* hold is the evidence that a post was reached: the reservation, the
approval's payload digest, and the `forecast_intent` journal row the submission policy commits
immediately before every POST. The artifact holds the receipt when the command got that far,
and nothing does when it did not. Both shapes are recoverable the same way.

**Confirm (read-only).**

```bash
uv run whiskeyjack-bot unrecorded-posts --config config.yaml
```

lists every record holding a submission intent and a standing, unspent reservation. It reads
the ledger only: no credential, no network. It is a list of places to look, not a verdict — a
process killed between the intent and the POST leaves the same rows with nothing posted.

`tournament status`'s `unrecorded_posts` is a different question, and it **is** a verdict: it
counts the records whose forecast the worker's own refetch already confirmed on the platform
and whose lifecycle ledger holds no event that ever recorded the post. The two sets usually
overlap, and where they do not, the difference is the diagnosis:

| In the listing | In the count | What it is |
|---|---|---|
| yes | yes | this section: a live post the ledger never recorded |
| yes | no | the process was killed *before* the POST — check Metaculus, and if the forecast is not there, [L2](#l2--a-standing-key-reservation) |
| no | yes | the reservation was released for a post that landed — **M2-716**, and there is no recovery for it yet |

Then **look at the question on Metaculus** for each one.

**Recovery — if the forecast IS there:**

```bash
uv run whiskeyjack-bot reconcile-submission --config config.yaml \
  --record-id "$REC" --observed-by "you@example" --note "question page shows our 35%, posted 14:02"
```

`--observed-by` and `--note` are required and have no default: they record that *you* looked
and what you saw. The command then checks that against the program's own evidence and refuses,
writing nothing, when they disagree. Before it builds a poster it prints what it found —
record, question, reservation, key, attempt id, payload digest, intent, and the artifact path
with its sha256, or `artifact: none` — so every local refusal needs no `METACULUS_TOKEN`.
Then it makes **one identity read and one GET, and posts nothing**:

| It refuses when | Message starts | What to do |
|---|---|---|
| The record is not `approved` | `this forecast record is <status>` | Nothing: it is not this state |
| No reservation holds the approval's key | `no key reservation is standing` | Nothing was claimed, so nothing was posted |
| No submission intent | `this record holds no durable submission intent` | The command never reached the POST: check, then `release-key` |
| An artifact at the path cannot be read | `cannot read the submission artifact` | Fix its permissions and re-run; it may be the receipt |
| The token is another account | `the configured token is not the account` | Use the account that posted |
| The platform shows nothing | `the platform shows no forecast from this account` | Your observation and the platform disagree: look again, then `release-key` if it is not there |
| Something else is there | `the platform holds a forecast that is not the payload` | [L3](#l3--a-mismatched-refetch) territory: resolve by hand |
| The platform could not be read | `the question could not be refetched` | Re-run later |

On success it prints `result: submission_confirmed -> submitted (reconciliation wjrec-…)`.
The record is `submitted`, the key is spent (`submit`, `release-key` and a new reservation all
refuse it), and the record is picked up by the next `ingest-resolutions`.

**What gets written, and what does not.** One `submission_reconciliations` row — your name and
note, the confirming refetch, the reservation, the attempt id the post carried, the payload
digest, the intent, and the artifact's path and sha256 when there is one — and one
`submission_confirmed` lifecycle event citing it. **No `submission_attempts` row.** Nobody
captured the POST's response, so no receipt is invented for it; the artifact, when it exists,
is pinned by its digest and left where it is.

**Recovery — if the forecast is NOT there:** the post did not land. That is
[L2](#l2--a-standing-key-reservation): `release-key`.

**Never.**
1. **Do not release the key for a post that landed.** A released reservation cannot be
   reconciled afterwards — the ledger would hold both answers to one question — and that
   mistake has no recovery yet (**M2-716**).
2. **Do not submit again for this record.** The post landed.
3. **Do not delete or rewrite the artifact.** Its sha256 goes on the reconciliation row.

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
`submissions/live/<question_id>/<idempotency_key>.json` — and find it absent. `show
--record-id` (M1-611) shows the lifecycle event for the attempt (attempt id, whether it
was `submitted`, `submission_uncertain` or `submission_failed`, and its timestamp); the
full `submission_attempts` row is still not readable by any command. `submit`'s output at
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

1. **Never trust `submit`'s exit code.** It is `0` exactly when the refetch confirmed *and*
   the artifact was written, which does not line up with the recorded outcome in either
   direction: a confirmed post whose artifact failed exits `4` with the forecast live on the
   platform, and an uncertain outcome whose refetch confirmed exits `0` with a
   `verify-submission` still owed. The `result:`, `artifact:` and instruction lines are the
   answer.
2. **Never retry a submission whose outcome is uncertain.** Resolve it with
   `verify-submission` first. The program blocks this, and the block is the feature.
3. **Never edit the ledger with `sqlite3`.** Not to close a state, not to fix a status, not
   to release a key. Every table you would reach is append-only by trigger, and the ones
   you could reach are the ones whose immutability the whole product rests on. If a state
   seems to need it, it is one of the filed gaps below.
4. **Never release a key reservation after a post that may have landed.** If `submit` said
   a post was made and the ledger refused it, the post landed. Releasing invites a
   duplicate live forecast, and a released reservation cannot be reconciled afterwards.
   Check Metaculus: `reconcile-submission` if it is there, `release-key` only if it is not.
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

Three states in this document have no complete recovery through a documented command. They
are listed here rather than papered over with a SQL snippet, because a runbook that teaches
you to edit the ledger has destroyed the thing it documents. (**M1-611**'s `show --record-id`
closed a fourth: finding a record's state, or a lost attempt id, from the ledger alone — see
[U](#how-to-find-the-attempt-id-if-you-lost-it). It does not reach a pre-forecast failure's
`pipeline_failure_events` row — [R1](#r1--research_failed)/[R2](#r2--generation_failed) — or
the full `submission_attempts` row behind a lifecycle event — [L1](#l1--submission_failed)/
[L5](#l5--the-artifact-was-not-written) — neither of which this item's scope covered.)

| State | Section | Row | What is missing |
|---|---|---|---|
| A `mismatched` refetch | [L3](#l3--a-mismatched-refetch) | **M2-714** | A way to record a human's judgement closing it |
| An approved record needing re-approval | [A2](#a2--an-approved-record-cannot-be-re-approved-or-rejected) | **M2-715** | Either a legal path back, or a refusal that stops suggesting one |
| A `not_recorded` forecast | [R4](#r4--not_recorded-the-forecast-the-ledger-never-saw) | **M1-317** | A ledger identity for a post-generation persistence failure |

One further filed item is a nuisance rather than a stuck state: **M0-010**, `verify-env`
requiring the optional fallback retrieval key ([C4](#c4--environment-not-ready)).

If you hit a state that is not in this document and whose only exit appears to be a
database edit, that is a sixth gap and it is worth a backlog row. File it rather than
reaching for `sqlite3`.
