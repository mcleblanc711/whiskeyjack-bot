# Tournament operator runbook

Production activation requires owner authorization for the reviewed account, project
**33122**, validity window, and **US$20** ceiling. Committing configuration, installing a
service, or passing tests does not activate the bot. Use testing project **32977** for
rehearsals, with separate SQLite and artifact paths.

**The Cup profile is dormant as of 2026-09-10** (withdrawn, owner decision; see
`docs/M1-NOTES.md`). Its timer is stopped and disabled and its activation is retired, so
the paragraph below describes a profile that is kept for re-entry, not one that is running.
MiniBench is the only live tournament.

**A second tournament is a second, fully independent profile — never a shared ledger.**
`config/tournament-cup.yaml` targets the Metaculus Cup Fall 2026 alongside MiniBench, with
its own SQLite path, artifact root, log file, and `deploy/systemd/whiskeyjack-tournament-cup.*`
units. Activation in `tournament_state.py` is scoped to one ledger/account/project at a time,
so the two tournaments can never share storage — this is the same shape as the rehearsal
(production) split below, just two live profiles instead of one live and one test. Verify
`metaculus.tournament.id` in that file against the live API before first use (D31); the file
ships with the URL slug, unconfirmed. Everything else in this runbook (deployment, rehearsal,
stop/inspect, uncertain-forecast recovery, backup/restore) applies per profile — substitute
`config/tournament-cup.yaml` and the `-cup` unit names throughout.

## Deployment

The supplied files target `/home/cleblanc/projects/whiskeyjack-bot`. First check out the
reviewed release, run `uv sync --locked`, and review `config/tournament.yaml`. Keep the host
online and user-service lingering enabled. Put credentials in the host's `.env` with
mode 0600 using ordinary `NAME=value` entries, without shell commands. The service uses
systemd `EnvironmentFile`; it disables implicit dotenv and remote model-price initialization.

```bash
systemd-analyze verify deploy/systemd/whiskeyjack-tournament.service deploy/systemd/whiskeyjack-tournament.timer
install -D -m 644 deploy/systemd/whiskeyjack-tournament.service ~/.config/systemd/user/whiskeyjack-tournament.service
install -D -m 644 deploy/systemd/whiskeyjack-tournament.timer ~/.config/systemd/user/whiskeyjack-tournament.timer
systemctl --user daemon-reload
```

After explicit owner authorization, load credentials into the operator shell and run the
README's `tournament enable` command with actual UTC timestamps. Activation checks the live
account ID and hashes the complete effective configuration and prompt. Changing either
requires a new reviewed activation. Disabling and re-enabling does not reset spending.
Then start the timer:

```bash
systemctl --user enable --now whiskeyjack-tournament.timer
systemctl --user list-timers whiskeyjack-tournament.timer
journalctl --user -u whiskeyjack-tournament.service -n 100
.venv/bin/whiskeyjack-bot tournament status --config config/tournament.yaml
```

**Watching multiple profiles at once**: `scripts/watch-tournaments.py` tails
`data/logs/tournament.jsonl` and `data/logs/tournament-cup.jsonl` together, tagging each line
by profile and de-emphasizing routine question-discovery/fetch chatter so a `Posted
prediction`/`Posted comment` line (or any `WARNING`/`ERROR`) stands out instead of scrolling
past in the same weight as everything else. Run with no arguments for the two profiles this
repo currently runs, or `LABEL=path/to.jsonl` pairs for others. A plain `tail -f` on one log
file still works fine for a single profile; this is for watching more than one at a time.

The timer polls every five minutes; a file lock prevents overlapping workers. The service
has a 40-minute total bound, each question's paid phase has an eight-minute bound, and
HTTP requests have configured timeouts. A timer tick while the oneshot service is active
does not start a second worker. Processing is sequential and sorted by deadline. No new
forecast starts within five minutes of closing. Questions released during an ongoing poll
are discovered on a later poll.

## Rehearsal

Copy the production profile to a testing profile. Set `environment: test`, project 32977,
separate storage paths, the actual prompt path, and `run_limits.max_questions: 1`. Enable
that profile for a short validity window. Pick one live supported subquestion with no
existing account forecast, then run the same command twice:

```bash
.venv/bin/whiskeyjack-bot tournament run-once --config /absolute/path/testing.yaml --question-id QUESTION_ID
```

The first run must report one confirmed forecast, one completed comment, and zero unresolved
operations/failures. The second must leave the forecast/comment totals unchanged and make
no new paid call. Retain the ledger and artifacts as evidence. Disable testing activation
afterward. A failure or incomplete comment is a failed rehearsal, even if the forecast is
visible on the website.

## Stop and inspect

```bash
systemctl --user stop whiskeyjack-tournament.timer
.venv/bin/whiskeyjack-bot tournament disable --config config/tournament.yaml
systemctl --user stop whiskeyjack-tournament.service
```

Stopping a running service may interrupt an external request. Its durable intent remains
unresolved until refetch establishes the result. Never delete an intent, reservation, or
receipt to make the next run succeed.

Only `tournament enable` may initialize storage. `disable`, `status`, `run-once`, and
`reconcile-restored` require an existing compatible ledger; a missing or incorrect database
path returns nonzero without creating a database. Check the exit code when stopping.

Status validates the effective configuration, prompt, and retained storage guards using the
same activation checks as the worker. `enabled: true` means those local checks passed; it
does not verify live credentials or prove that the timer is running. Normal inactivity
(disabled, not yet valid, or expired) returns zero. Changed bindings, a missing prompt,
inconsistent storage, or a spending hold report `enabled: false` and return nonzero.
`refusal_reason` gives a sanitized local reason without provider content or credentials.

Status reports the last heartbeat, discovered/processed/skipped counts, failures, forecast
confirmation, comment completion, unresolved operations, and actual/reserved/remaining
budget. The heartbeat's `complete` describes the poll finishing; zero failures and zero
unresolved operations are also required for success. Counts include retained operations in
that ledger; use a separate ledger per operating profile for clear reporting.

Budget exhaustion refuses the next purchase before calling a provider. Unknown charges
continue to consume the conservative reservation after restart. Inspect `cost_reserved`,
`cost_settled`, `retrieval_started`, and `model_started` events in `tournament_events` using
a read-only SQLite connection. Do not raise the ceiling or erase charges to continue. The
hard maximum per activation is `MAX_ACTIVATION_BUDGET_USD` (US$40; raised from US$20 by
M1-408 on the owner's explicit authorization, through review, not to get past an exhaustion).
Provider prices above the configured ceiling or unavailable routes cause refusal.

## Uncertain forecast or comment

Rerun `tournament run-once` with the same activation/profile. Recovery first refetches
pending forecast intents, including questions that have closed or left discovery. It
compares the bot's saved payload with its live history and records confirmation. It never
repeats an uncertain forecast POST. A confirmed forecast with no comment intent proceeds
only to comment creation. An uncertain comment is reconciled using the account, post,
privacy flag, and unique record marker; a known returned ID must also agree.

If the platform still shows nothing, the operation remains unresolved. Wait and refetch;
a negative immediate response is not proof that the original request failed. There is no
automatic override for an uncertain write. Inspect Metaculus and retained evidence before
any separately reviewed repair. Group comments explicitly identify their subquestion.
Storage failures stop the worker; ordinary question/provider failures are isolated.

## Consistent backup and restore

Stop the timer and service before taking a backup. Use SQLite's backup API (or the sqlite3
`.backup` command), then copy the entire artifact tree while the worker remains stopped.
Retain the database and artifacts together, plus the exact configuration and prompt. A plain
copy of a live WAL database file is not a consistent backup. Protect backups as account data.

**Keep the current `.posting-guard` directory beside the live database outside the rollback.**
It holds synchronized witnesses for purchases and external write intents. Back it up
separately for host disaster recovery, retaining the newest copy. Restoring both the ledger
and this guard to an older time destroys the evidence needed to detect rollback; do not
resume posting in that state.

To restore: stop service/timer, preserve the current guard, restore the matched database
and artifact backup to the configured paths, then run:

```bash
.venv/bin/whiskeyjack-bot tournament reconcile-restored --config config/tournament.yaml
.venv/bin/whiskeyjack-bot tournament status --config config/tournament.yaml
```

Reconciliation reads Metaculus before importing missing durable witnesses and conservatively
restoring held spending. If a restored purchase witness has no retained provider outcome,
reconciliation appends both its reservation and a persistent account/project
`restored_spending_hold` before marking the witness reconciled. Status exposes
`spending_held` and `restored_spending_holds`. While held, polls fail and no new research,
model request, forecast, or comment is started; read-only forecast/comment reconciliation
continues under a valid activation. Restarting, disabling, reactivating, and repeated
reconciliation cannot clear the hold. There is no automatic release mechanism; resolving
it requires separately reviewed recovery using retained provider evidence. Known completed
charges settle their original reservation once during recovery. Cached retrieval records
zero new calls and spending, while any unknown original cost remains reserved.

A lost submission intent holds the whole question, including when
its forecast is absent from the current refetch. It does not recreate a missing forecast
record or discard a question hold. Restore missing artifacts from retained copies; offline
replay must match before any new submission. Investigate all holds and unresolved operations
before enabling the timer. If the latest guard is unavailable, keep posting disabled until
account history and retained records have been reconciled through a separately reviewed
recovery.
