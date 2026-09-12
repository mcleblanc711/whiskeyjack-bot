# D-1001 — Operator runbook implementation notes

Running record of D-1001's decisions and deviations, in the spirit of `docs/M1-NOTES.md`
and `docs/M2-NOTES.md`.

**This file is temporary and merges back into `docs/M2-NOTES.md`.** It is split out for
`docs/M1-301-NOTES.md`'s reason, which applies exactly here: four lanes are live in this
wave (`D-1001`, `M2-705`, `M1-604`, `M1-407`) and `M2-705` is appending to
`docs/M2-NOTES.md` right now. A shared notes file is the one thing concurrent branches
reliably collide on, and this item's notes have no ordering relationship to M2-705's.

**Merge-back trigger:** when this item and `M2-705` are both on master, append this file's
body to `docs/M2-NOTES.md` as a `## D-1001` section and delete this file, as one docs-only
commit.

Acceptance: *another operator can recover from each failure state without editing the
database.*

## Delivered

- `docs/RUNBOOK.md` — the runbook. A symptom index, the lifecycle walkthrough, the state
  vocabularies, twenty failure states in four fixed headings each (*what you see* / *how to
  confirm, read-only* / *recovery* / *never*), a dedicated uncertain-timeout section, a
  "never do this" list, and a closing section naming every state whose only recovery would
  be a database edit.
- `docs/backlog/backlog.csv` — five new rows (**M2-713**, **M2-714**, **M2-715**,
  **M1-611**, **M0-010**), and `D-1001` flipped to `Done`. Twenty failure states became
  twenty-one after the pre-PR master merge — see the C5 deviation below.
- `docs/TRACKS.md` — the claim row, committed first.

No code, no migration, no dependency. Nothing in this item is reachable from the CLI
because this item adds nothing to the CLI; what it adds is the statement of what the CLI
already does and where it stops.

## What the criterion is actually guarding against

The criterion is one sentence — *another operator can recover from each failure state
without editing the database* — and it has two halves that fail differently.

**"Each failure state"** fails by enumeration. A runbook that covers the states its author
remembered is worse than none, because it reads as complete. So the states here were
derived from the schema and the code rather than from experience: the `CHECK` vocabularies
of `forecast_records.status` (7), `lifecycle_events.event_type` (11),
`pipeline_failure_events.event_type` (2) and `submission_attempts.refetch_outcome` (4);
the `(success, refetch_outcome)` partition in `lifecycle.record_submission_attempt`; the
transition table in `009`; and every `raise` on the submission path.

**"Without editing the database"** fails by accommodation. The tempting move, every time a
state turns out to have no command, is to write the `UPDATE` that closes it — and each one
of those would be a documented instruction to corrupt the instrument the project exists to
produce. Under CLAUDE.md's stricter-reading rule the recovery has to be a command or an
owner decision, so a state with neither is **a missing feature, and the runbook's job is to
say so and name the row**. Five of them did.

That is the finding this item actually produced. The prose was the cheap half.

## The five states with no complete recovery

Each is reproduced or traced to the exact `raise`, and each is now a row.

| State | Row | Why it is a gap |
|---|---|---|
| A live post the ledger refused to record | **M2-713** (Critical) | Forecast on the platform, no attempt row, key reservation standing, record still `approved`. `release-key` is explicitly the *wrong* action. Nothing records a post the ledger missed. |
| A `mismatched` refetch | **M2-714** | `verify_uncertain_attempt` refuses to record it; the record sits `approved` with an uncertainty blocking every further submission, permanently. |
| An `approved` record needing re-approval | **M2-715** | The refusal says "approve the record again"; the transition table forbids it. |
| Finding a record's state, or a lost attempt id | **M1-611** | `show` is a required entry point in `CODEX_HANDOFF.md` and was never built. |
| A `not_recorded` forecast | **M1-317** (already filed) | Generation succeeded, the ledger refused the row, and no event type is true. |

**M2-713 is the one to look at first.** It is the only state in the product where the
ledger and the platform can disagree permanently, and it is reached by an ordinary
durability failure rather than by operator error.

**M2-714 was already known and never filed.** `docs/M2-NOTES.md:1697-1711` names it as a
standing risk in M2-711's own notes, in the right words — *"there is no command today that
lets an operator say 'I looked; it is not mine' and close it… it needs an actor, a note,
and an approval-shaped boundary, which is an item, not a clause"* — and no row followed.
That is `docs/LESSONS.md`'s stop-point checklist item 4 (*check every "Deferred" note
landed a backlog row*) catching one late rather than never.

### Decision — a missing recovery is a row, not a `sqlite3` snippet, and the runbook says which

The alternative was to document the SQL for the states that have no command, marked
"advanced" or "last resort". Rejected, and not on tidiness grounds: the ledger's tables are
append-only by trigger precisely so that no operator can do this, and a runbook is the one
document persuasive enough to talk someone into working around a safety property at 3am. A
document that teaches the edit has destroyed the thing it documents.

The cost is real and is stated rather than hidden — `docs/RUNBOOK.md` has a closing section
listing all five states, and each failure section says plainly that there is no recovery
today and names the row. An operator who reads "there is nothing you can do here, escalate"
is better served than one who reads an `UPDATE` that appears to work.

The runbook also asks for the sixth: *if you hit a state that is not in this document and
whose only exit appears to be a database edit, that is a gap and it is worth a backlog
row.* The enumeration is a claim, and that sentence is how it gets falsified.

### Decision — every command, flag and message was checked by execution

The brief's own list of subcommands was wrong in two places, which is the argument for this
in miniature: the real names are **`verify-submission`** (not `verify`) and **`release-key`**
(not `release`). Both were caught by running `whiskeyjack-bot --help`, and a runbook
carrying either wrong name would fail an operator at the exact moment it is read.

So: `--help` was run for all eleven commands, and the whole offline lifecycle was driven end
to end against a scratch ledger seeded through `tests/acceptance/scenario.py` — production
writers only, no hand-written fixture JSON. `init-ledger`, `verify-env`, `run-replay`,
`replay`, `approve` (right hash and wrong), `reject`, `submit`, `release-key` and
`verify-submission` all ran; every output block in the runbook that is not labelled
otherwise is copied from that run.

The paid and networked commands — `run`, `submit`'s post path, `questions fetch --live` —
were **not** run, and the runbook says which blocks those are. Documenting a `run` block
from the code that prints it is honest; inventing one and presenting it as observed is not,
and the distinction is `CODEX_HANDOFF.md`'s first prohibited claim in a different costume.

### Decision — the runbook opens with the two things that mislead before any error is read

Two findings from the execution pass earned a section ahead of the symptom index, because
both actively point an operator at the wrong problem:

- **The pinned SDK prints import-time noise on stderr**, including the literal
  `METACULUS_TOKEN environment variable and/or token field not set`, on commands that have
  nothing to do with the token and are about to refuse for an entirely different reason.
  The program's own output is on stdout and is clean; `2>/dev/null` separates them. An
  operator who reads the first line of `submit`'s output learns something false.
- **`forecast_records.status` is not the record's status.** It is status-at-creation,
  pinned to `draft` forever because the table is append-only, and the real status is derived
  from the last `lifecycle_events` row (`lifecycle.current_status`). Anyone who reaches for
  the database — which this runbook is trying to talk them out of — reads `draft` on an
  approved record and concludes the ledger is broken.

A third joined them from reading `_run_submit`'s return paths: **`submit` exits `0` for
every outcome it managed to record**, `submission_uncertain` and `submission_failed`
included. Exit `0` means "the attempt completed and was written down", not "the forecast is
on the platform" — the outcome is on the `result:` line and nowhere else. It is the same
shape as T-905's `gate.sh` defect (trust the printed line, not `$?`) and it is the first
entry in the runbook's "never do this" list for the same reason: it is the mistake that
looks like diligence.

None of the three is a defect worth a row. All three are documentation's job exactly.

### Decision — `docs/RUNBOOK.md`, top level in `docs/`

Not `docs/operations/` or a `README` section. Every existing operator-facing document is
flat in `docs/`, the file is named for what it is, and `M2-712`'s notes already searched for
`*runbook*` and recorded that nothing matched — so this is the file every existing
cross-reference has been pointing at. `docs/TRACKS.md:170`, `docs/M2-NOTES.md:716` and
`docs/M2-NOTES.md:1099` all name it.

### Decision — `M0-009` was renumbered to `M0-010` before the PR opened

The new `verify-env` row was filed as `M0-009`, which was free in the CSV and should not
have been. `M0-009` already denoted something else in this project's history: it was filed
on the M2-707 branch for the `scripts/gate.sh` exit-code defect and then **deleted** as a
duplicate of `T-905` (`docs/M2-NOTES.md` records both the filing and the deletion, and that
paragraph is still on master). Recycling the identifier would make the two references in
`docs/M2-NOTES.md` and this file's runbook paragraph point at two different defects with no
way to tell them apart. A deleted row frees the *slot*, not the *name*. Renumbered to
`M0-010`, which has never appeared in the backlog on any branch.

### Deviation — the runbook grew a twenty-first state, `C5`, in the pre-PR master merge

The runbook was written against a base sixty-three commits behind the master it merged
before this PR opened, and one of those commits added a failure state rather than changing
an existing one: **M1-334** split `ActivationRetired` out of `ActivationInactive`, so
`activation retired: ... changed; re-run tournament enable` is now a distinct refusal with
its own alert and its own recovery. It is also the failure state with the worst measured
cost in this project — the 2026-09-09 outage, 2h33m of every poll refusing because a merge
moved `config_sha256` with the operator's YAML untouched. Omitting it would have failed the
criterion on exactly the enumeration half this item's notes argue is the hard one, so `C5`
was written rather than deferred: the binding table, the `P1` contrast (resting states are
checked first and stay silent), and the "treat `tournament enable` as part of deploying"
rule.

The rest of the merge was checked and changed nothing: every error string the symptom index
quotes was re-grepped against the merged tree and all of them still exist verbatim.

### Deferred — `export` (M1-604) landed in that same merge and has no section

`whiskeyjack-bot export` did not exist when the runbook was written. It gets no section
because it has no failure state this criterion is about: it never writes to the ledger, so
there is nothing it can leave half-done and nothing to recover from. `M1-611`'s `show` will
change the command list anyway, and the deferred CI check below — asserting every command
the runbook names still parses — is the thing that should catch a command *drifting*, not a
prose section per subcommand.

### Deviation — the runbook documents a read-only probe that is a coincidence of gate ordering

`submit` prints the record, question, version, type, derived status, content hash and
derived payload digest **before** it reaches the configuration gate, so with
`submission.enabled: false` it is a read-only inspection command that writes nothing and
posts nothing. The runbook says so, because it is the best state probe available today and
an operator needs one tonight, not when M1-611 ships.

It is recorded as a deviation rather than presented as an interface, and the runbook says
the same: this is gate ordering, not a designed affordance. A future change that moves the
configuration check earlier — a reasonable change, since it would refuse sooner — would
silently remove it. That is one reason M1-611 is filed rather than treated as covered.

### Rejected — reordering the runbook by ledger table, and why not

The obvious structure for a document derived from a schema is one section per table:
`pipeline_failure_events`, `lifecycle_events`, `submission_attempts`, the reservation pair.
It was drafted that way and abandoned. An operator does not arrive knowing which table
their problem lives in — they arrive with a string on a terminal. So the entry point is a
**symptom index keyed on the literal text the program printed**, and the vocabularies are
kept as reference material behind it.

The schema-shaped view is not lost: the state vocabularies section carries every `CHECK`
list and the `(success, refetch_outcome)` partition, which is where a reader who *does*
know the tables will look.

### Rejected — a `verify-env` change to fix the false red in passing

`verify-env` requires `retrieval.fallback.api_key_env` (`EXA_API_KEY`) while `run` treats it
as optional, so a runnable install reports `environment NOT ready` and exits `3`. The fix
looks like one line in `AppConfig.secret_env_var_names()`.

Refused for this branch. It changes merged, reviewed configuration behaviour and its exit
code from a documentation item, which is the sideways fix `M1-314`, `M1-507`, `M2-709` and
`M1-608` all exist as rows instead of. Filed as **M0-010**, and the runbook documents the
false red with the row number and the sentence that the paragraph is deleted when the row
ships — so the doc patch has an expiry date rather than becoming permanent.

### Deferred (do not read the absence as an omission)

- **`resolved` and `scored` have no writer, and the runbook has no section for them.**
  Nothing in `src/` inserts into `resolution_events` or `score_events`, so `submitted` is
  where a record stops today. The lifecycle section says exactly that rather than omitting
  it, but there is no failure state to document because there is no code to fail. That is
  milestone work already on the backlog — `M4-801` ingests resolution snapshots and
  `M4-802`/`M4-803` compute and record scores — not a gap this item should file.
- **No `docs/RUNBOOK.md` section on the `A-110x` owner tasks.** Those are owner-only
  account and credential setup and are tracked as their own `Blocked` rows; the operator
  runbook starts from a checkout that already has credentials. The two documents meet at
  `verify-env` and nowhere else.
- **The `--payload-file` affordance is documented but not exercised.** `M2-703`'s notes said
  the operator affordance lands "with M2-704 and D-1001's runbook", and it is described in
  the submit section — supply a payload to check it against an approval without posting
  anything it did not cover. Driving it end to end needs a live post, which this item does
  not make.
- **No CI check that the runbook's commands still exist.** A doc naming a subcommand that
  gets renamed is exactly the failure this item's own research found in its brief. A test
  asserting every command in `docs/RUNBOOK.md` parses would catch it, and it is a testing
  item rather than a clause here — not filed, because `M1-611` will change this document's
  command list anyway and the check is worth writing once, after.
- **README staleness.** `README.md` still says "There is no submission path in this
  codebase yet; `submission.enabled: false` and `dry_run: true` are enforced by config
  validation" and its Status section describes Milestone 0. Both predate M2-704. Left alone:
  the README is the project's front door and rewriting it is a docs item with its own
  audience, not a side effect of writing a runbook.

### Standing risk — the enumeration is a claim, and it is not verifiable by a test

Everything else in this project defends its claims with a test. This one cannot: there is
no assertion that expresses *"every reachable failure state appears in this document"*. The
states were derived from the closed vocabularies, the transition table and the `raise`
sites on the submission path, and that derivation is as good as its coverage of those
sources — a state reachable through a path none of them names would not appear here and
nothing would fail.

Two things bound it rather than close it. The vocabularies **are** closed and schema-
enforced, so a new state needs a migration, and a migration is reviewed. And the runbook's
closing section asks the reader to file a row for any sixth state they find, which makes
the claim falsifiable by use even though it is not falsifiable by CI.

The honest statement of confidence: the states are enumerated from the schema, the five
gaps are each reproduced or traced to an exact `raise`, and the completeness of the
enumeration is argued rather than proven.

### Standing risk — output blocks age, and nothing tells you when

Every observed block in `docs/RUNBOOK.md` was copied from a real run on this branch. There
is no mechanism that fails when one drifts. A changed print statement, a renamed flag or a
reordered gate makes a block wrong silently, and the reader most likely to be misled is the
one under the most pressure.

The mitigation is weak and worth naming as weak: message text is quoted rather than
paraphrased, so a `grep` for a quoted string finds both the code and the doc. The real fix
is the CI check deferred above.

## Verification

- Every subcommand's `--help` run; all eleven names and their flags confirmed.
- The offline lifecycle driven end to end against a scratch ledger seeded through
  `tests/acceptance/scenario.py`: `init-ledger` (fresh and idempotent, schema version 11),
  `verify-env`, `run-replay` (one validated record), `replay` (`verdict: match`), `approve`
  with a wrong hash (refused, nothing written — `approval_events` count confirmed `0`),
  `approve` with the right hash, `approve`/`reject` on the approved record (both refused —
  **M2-715 reproduced**), `submit` with submission off, `release-key` with nothing standing,
  `verify-submission` without a token.
- The live-artifact recovery recipe in the uncertain-timeout section was run against a real
  artifact produced by `write_live_artifact`: the `grep` matches and the `attempt_id` reads
  back.
- `uv run python .github/scripts/check_backlog.py lint` — passed, 106 rows.
- `BRANCH_NAME=feat/d-1001-operator-runbook … check_backlog.py gate` — `D-1001 is 'Done'. OK.`
- `docs/backlog/backlog.csv` rewritten with `csv.writer(lineterminator="\r\n")`; zero lines
  without CRLF, and the diff is six rows rather than the whole file.
- `scripts/gate.sh` — see the commit that records it; trust the last printed line (T-905).
