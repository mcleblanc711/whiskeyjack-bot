# Active tracks

Who holds what, right now. Parallel work runs one item per worktree, and two of the
collisions that costs are invisible until merge — so they are claimed here **before** a
worktree is created, not discovered afterwards.

Edit this file on your own branch and merge it like anything else. It is a claims
registry, not a status board: the backlog CSV is the record of what is done.

**The registry is advisory, and it is worth being precise about why.** A claim lives on
a branch until that branch merges. `scripts/start-item.sh` therefore reads this file from
**every active `origin` branch**, not from `origin/master` alone — reading master could
not see a claim for the whole period the claim exists to cover, which is how two
dependency-adding tracks could each be told the slot was free and meet at a `uv.lock`
conflict. A row whose branch has been deleted or already merged is ignored: `finish-item.sh`
deliberately leaves those behind for the next branch to sweep, and a leftover must not
block anyone.

So the remaining blind spot is one window: **between running `start-item.sh` and pushing
your claim row, nobody else can see it.** Write the row first and the window is minutes.
What actually *enforces* the outcome is still downstream —
`.github/scripts/check-migrations.sh` fails a duplicate or mutated migration number at
merge (master requires branches to be up to date, so that check runs against the base
that will really be merged), and two branches editing `uv.lock` conflict loudly.

One thing is no longer advisory: `start-item.sh --deps` **exits** on a live dependency
claim rather than warning and continuing. There is no override flag. If the holding
branch is genuinely abandoned, delete the branch or drop its row — both are honest edits
to the registry, and neither is a habit you can form by reflex.

## Standing claims

| Claim | Held by | Notes |
| --- | --- | --- |
| Dependency additions (`pyproject.toml` + `uv.lock`) | *free* | **M1-604 claimed it 2026-09-04, spent it on `pyarrow`, and merged** (PR #87, round-2 approval, 2026-09-10); released now that the branch is on master. **Spent on `pyarrow`, and the slot buys a *declaration*, not an install.** Parquet is a binary columnar container with no stdlib writer. `pyarrow 24.0.0` is already resolved into `uv.lock` transitively (`forecasting-tools==0.2.92` -> `streamlit 1.59.2` -> `pyarrow>=7.0,<25`) and already installed in every `.venv`, so declaring it directly adds zero bytes -- the `asknews`/`httpx`/`idna` case `pyproject.toml` already comments: declared because it is imported directly. Bound `>=24,<25`, inside streamlit's range, or `uv sync --locked` conflicts. `fastparquet` would drag in pandas+numba; `polars` would be a second dataframe engine for one call. *(This prose was written by `67610aa` and lost at the first master merge, `c986ca4`, which resolved the row back to `*free*`; enforcement never lapsed, because `scripts/tracks.py` reads only the Worktrees `Adds deps?` column below. Restored 2026-09-10.)* Previously claimed by M1-303 on 2026-07-27 and **released unused** (the Exa adapter used `httpx` instead of adding `exa-py`). **M1-311 claimed it 2026-08-25, spent it, and merged** (PR #40, round-2 approval, 2026-08-25): `publicsuffix2` rejects multi-label suffixes (`co.uk`, `com.au`) that the old dependency-free rule missed. Released now that the branch is on master. |
| Workflow / test-infrastructure change | *free* — **and should stay free until this wave closes** | Three concurrent lanes means every workflow change lands in three open review cycles at once, which is lesson 1 at triple cost. If one is genuinely needed mid-wave, claim the slot here and **say so in the next review request on every open lane** — the reviewer is stateless and reads a format change as a substantive one. Lesson 1: a workflow change is a track and takes a slot. Held 2026-08-17 by `test/tmpfs-temp-root` (PR #24) and `chore/review-loop` (PR #25), **both merged and released the same day**. #24 moved pytest's temp root to tmpfs (`tests/conftest.py`): full suite 497.7s → 81.3s, `test_lifecycle.py` 96.5s → 3.2s, because the dev machine's only drive is a 7200rpm platter at 49.6ms/fsync. #25 added `scripts/gate.sh`, `scripts/run-review.sh` and the `fast` hypothesis profile. They landed **mid-wave** by deliberate exception, having been checked against every live branch first — the only conflict was this table. **Say so in the next review request:** the conftest change moves where temp files land, nothing about what is asserted. |
| Next free migration number | **`014`** — next free. **M1-205 spends `013`** (`013_discrete_question_type.sql`, 2026-09-07): admitting `discrete` to `forecast_records_require_draft_on_insert`'s question_type vocabulary, which is the DROP/CREATE escape hatch 008's own comment predicted for exactly this case rather than the append-only table rebuild 003's header warns about. Fifth rewrite of that trigger (004, 006, 007, 008, 013). **Claimed after the item started, which is a process miss worth recording:** the item planned "no migration" because it checked `forecast_records` for a CHECK constraint on the column and found none — the vocabulary is enforced by a trigger. Cross-model review round 3 found it. Launch readiness claimed `012`. **M2-707 spends `011`** (`011_approval_payload_binding.sql`, 2026-09-02, on `feat/m2-707-bind-approval-payload`): closing `D33` needed somewhere to put the digest of the payload a decision authorized, and this is the *column* case rather than `010`'s new-table case — the hash has exactly the lifetime and exactly the cardinality of the `approval_events` row it sits on, so a side table would have bought a join per approval read for a row count that is always one. One `ADD COLUMN` plus one `DROP`/`CREATE` of `approval_events_bind_forecast_hash_on_insert` — the **first** rewrite of that trigger since `003` wrote it, and the same cheap escape hatch `004`, `006`, `007`, `008` and `009` used rather than the append-only table rebuild `003`'s header exists to warn about. The recreated trigger is `003`'s definition with two clauses appended and nothing else changed: a payload hash is **required** for `approved` and **forbidden** for `rejected`, which is what makes a NULL mean exactly one thing per decision (on an approval, a pre-`011` row). `010` was M2-708's, merged (PR #51, 2026-08-29) and now immutable on master | `001`-`010` are immutable on master; `006_non_blank_identifiers.sql` landed with **M1-607**, `007_forecast_version_chain.sql` with **M1-602** (PR #38), and **`008_forecast_raw_output.sql` with M1-406** (merged 2026-08-25, PR #41) — it added `raw_output_path`, `cost_usd` and `model_invocations` to `forecast_records` and appended three clauses to `forecast_records_require_draft_on_insert`, the fourth DROP/CREATE of that trigger (`004`, `006`, `007`, `008`) and the reason that pattern is worth keeping cheap. **M2-711 spent `009`** (`009_submission_refetch_outcome.sql`, 2026-08-26): recording a post whose outcome no refetch established needed a vocabulary member that `(success, verified_by_refetch)` had no room for. Worth recording **which** vocabulary, because the choice was the item: not a twelfth `lifecycle_events.event_type`, which is a column `CHECK` and so costs a rebuild of the append-only table `003`'s header exists to protect, but a new `submission_attempts.refetch_outcome` column reached by `ADD COLUMN`, with `submission_uncertain` widened to cover the new cell. One `ADD COLUMN` plus two `DROP`/`CREATE` trigger rewrites — the cheap escape hatch `004`, `006`, `007` and `008` used, now on `submission_attempts_require_receipt_on_insert` (second rewrite, after `006`) and `lifecycle_events_validate_on_insert` (**first** rewrite since `003` wrote it). It was the only item in wave 9 that needed a migration. **M2-708 spends `010`** (`010_submission_key_reservations.sql`, 2026-08-28): reserving an idempotency key *before* a post needs somewhere durable to put the claim, and neither existing table could hold it — `submission_attempts` is written once, after the call, and widening `lifecycle_events` means the `CHECK` rebuild `009` refused. So this one adds two new tables rather than a column: `submission_key_reservations` (the claim) and `submission_key_releases` (its resolution), the same claim/resolution pair `submission_attempts`/`submission_verifications` already are. New tables cost no precondition scan and no trigger rewrite — the first migration since `003` that touches none of the existing trigger bodies. Remember this column is advisory and nothing reads it — `.github/scripts/check-migrations.sh` plus master's up-to-date-branch requirement is the enforcement, as the `004` collision records: `004_pipeline_failure_events.sql` landed with **M1-606** and `005_research_run_counters.sql` with **M1-306**, and both branches were told `004` was free, because a claim lives on its holder's branch and this column is advisory — `scripts/tracks.py` checks the *dependency* claim and nothing reads this one. M1-606 merged first; M1-306 renumbered to `005` at its daily master merge, which is the designed outcome, and renumbering was safe only because M1-306's `004` had never reached master. |

## Worktrees

| Item | Branch | Worktree | Adds deps? | Migration | Started |
| --- | --- | --- | --- | --- | --- |
| M1-205 | feat/m1-205-discrete-questions | whiskeyjack-m1-205 | no | 013 | 2026-09-07 |
| T-902 | feat/t-902-mock-metaculus | whiskeyjack-t-902 | no | none | 2026-09-04 |
| M1-326 | fix/m1-326-deterministic-failure-gate | whiskeyjack-m1-326 | no | none | 2026-09-09 |
| M1-327 | feat/m1-327-named-source-evidence-gap | whiskeyjack-m1-327 | no | none | 2026-09-09 |
| M1-407 | feat/m1-407-prompt-bounds-crosscheck | whiskeyjack-m1-407 | no | none | 2026-09-04 |
| D-1001 | feat/d-1001-operator-runbook | whiskeyjack-d-1001 | no | none | 2026-09-04 |
| M1-604 | feat/m1-604-ledger-exports | whiskeyjack-m1-604 | **yes** | none | 2026-09-04 |
| M2-705 | feat/m2-705-response-capture | whiskeyjack-m2-705 | no | none | 2026-09-04 |
| M1-329 | feat/m1-329-ntfy-operational-alerts | whiskeyjack-m1-329 | no | none | 2026-09-09 |
| M1-408 | feat/m1-408-gpt-6-astra-client | whiskeyjack-m1-408 | no | none | 2026-09-10 |
| M1-333 | feat/m1-333-payload-property-filtering | whiskeyjack-m1-333 | no | none | 2026-09-11 |
| M1-334 | feat/m1-334-retired-activation-alert | whiskeyjack-m1-334 | no | none | 2026-09-11 |
| M1-613 | feat/m1-613-central-journal-redaction | whiskeyjack-m1-613 | no | none | 2026-09-11 |
*(Merged and left in place, per the rule below: **D-1001** (PR #95, round-3 approve,
2026-09-12 — the operator runbook, `docs/RUNBOOK.md`, 21 failure states; it filed six rows,
of which **M2-713** is Critical: a live post the ledger refused to record, with no
reconciliation command). Its row stays in the table above as the landed-claim evidence
`scripts/tracks.py` needs. The M1-407 handoff said to delete that row; the rule below says
not to, and the rule wins — this note is the record of the merge, in addition to the row.*

*(Merged and left in place, per the rule below: **M1-326** (PR #79, round-2 approve,
2026-09-09) and **M1-327** (PR #80, round-2 approve, 2026-09-09 — it demoted the
named-resolution-source gate from fatal to a recorded `evidence_gap` row and scoped M1-326's
attempt counters to `activation_id`). Their rows stay in the table above as the landed-claim
evidence `scripts/tracks.py` needs; this note is the record of the merge, **in addition to**
the row and never instead of it.*

*The four rows added above are the **live** wave-13 lanes, written into the table on master so
the board is readable from one place. Each row also exists on its own branch, which is what
`scripts/tracks.py` actually reads — `M1-407`'s claim was invisible until 2026-09-09 because
its branch had never been pushed, so the claim existed only in a local commit. Pushing a claim
branch is what publishes the claim.*

*`M1-329` claims **no dependency slot** — `httpx` is already a direct dependency
(`pyproject.toml:24`), so the notifier adds no package and no `uv.lock` churn, leaving
M1-604's Parquet claim uncontested. **The conditional migration claim is resolved: it needs
none, and `014` stays free.** The durable throttle landed as a stamp file claimed through
`artifacts.write_new_file`, whose `os.link` already fails `EEXIST` rather than clobbering and
is therefore a cross-process compare-and-set. A ledger table was rejected on three grounds,
the decisive one being that the systemd `OnFailure` half has no ledger connection at all —
state only the healthy program can reach is not state an alerting path may depend on. See
`docs/M1-NOTES.md` § M1-329.*

*`M1-329` **changes `AppConfig`**, which no other live lane does, and that is worth knowing
at a merge: `tournament_state.bindings()` digests `config.model_dump(mode="json")`, so any
new field changes `config_sha256` for byte-identical YAML and both live activations must be
re-enabled at deploy. The exact commands are in `docs/M1-NOTES.md` § M1-329 "Deviation". Any
other lane that adds a config field lands the same cost, so it is cheaper for the two to
merge close together than a week apart.*

**Do not sweep a landed row out of this table into the prose below.** `scripts/tracks.py`
proves a claim is a *stale landed* one rather than a misspelling by finding the exact row on
`origin/master` (`live_claims`: `signature in master_rows`) — so deleting the row destroys the
only evidence that its deleted branch merged rather than vanished. `T-902` was swept on
2026-09-07 while three parked branches still carried its row, and from then until 2026-09-09
`scripts/tracks.py claims` exited 1, which under `set -euo pipefail` made `start-item.sh` abort
for **every** item, deps or not. The row above is restored for that reason; `M1-205`'s row, also
merged, was correctly left in place. Record a merge in the prose *in addition to* the row, never
instead of it.

*(Swept `LAUNCH` (`release/launch-readiness`, merged PR #75, 2026-09-07 — it shipped the
operator runbook and the activated tournament runner, and spent migration `012`) and `T-902`
(merged PR #74, round-1 approve, 0 findings; its branch is already gone from `origin`). The
board is otherwise clear: four parked branches remain, all 0 behind master and none
tournament-relevant.*

**This item lands mid-round, while the tournament is live, and that is deliberate.** `discrete`
is 11 of MiniBench's 42 questions and every one is currently deferred under D21.

**Correction (round 1 caught this claim before it could mislead anyone):** an earlier version
of this note said the item changes no activation-hashed file. That was wrong.
`prompts/forecaster-tournament.md` is untouched, but **`config/tournament.yaml` is not** --
`forecast.supported_question_types` gates generation at `forecast/generate.py:351`, so it
gains `- discrete` and the worker **will** refuse until `tournament enable` is re-run.

Deploy order: pull, re-enable, confirm `status` reads `enabled: true` with a null
`refusal_reason`. Re-enabling does not reset spending — it is scoped by
`account_id:project_id` (`tournament.py:227`), not by `activation_id`. Merging changes
nothing on the live host until someone pulls.*

*(Swept `T-904` (merged, PR #72, round-1 approve, 2026-09-04), `M2-712` (merged, PR #71,
2026-09-04) and `T-907` (merged, PR #70, 2026-09-04) at this branch's master merge. The
master-side note anticipated this exact resolution: it deliberately left `T-905`, `M1-504`,
`M1-507` and `M1-605` in place because their drop was already staged here, and the merge has
now applied both halves — so those four rows are gone, once, rather than racing themselves
into a second conflict. Also swept `M2-709` (merged, PR #60, 2026-09-02; `finish-item.sh`
found its branch already
gone on both local and origin — deleted at merge time — so this row-drop is all the sweep
left to do), `M1-514` (merged, PR #63, round-2 approve, 2026-09-02 — its branch is still on
`origin` because `finish-item.sh` has not run yet; `scripts/tracks.py` ignores a merged
branch either way), `M1-610` (merged, PR #62, round-2 approve, 2026-09-02), `M2-707`
(merged, PR #61), `M1-315` (PR #55), `M1-608` (PR #57) and `M1-314` (PR #58). `M2-709` was
the last item on Lane 3's debt queue — one atomic create-or-fail helper shared by every
artifact writer. **Its row named a duplication that was already half removed**:
`research/artifacts._write_new_file` does not exist, because M1-406 extracted it to
`whiskeyjack_bot/artifacts.py` as public `write_new_file` when it added a second artifact
kind. The surviving copy was `submission_gateway._write_or_confirm` alone, and the item is
the part M1-406 could not do — parameterizing the two ways the gateway differs, its EEXIST
policy and its error type.)*

*(Swept again 2026-09-04, opening Wave 12: `T-905` (the `gate.sh` exit-code lie — the
script reported success on every failing gate, so a green last line was the only honest
signal it produced), `M1-504`, `M1-507` and `M1-605` (PR #69). All four merged; the rows
are dropped here rather than by `finish-item.sh`, which leaves them for the next branch
to sweep.)*

## Planned next wave

Not claims. A claim is a row in the table above, on the branch that holds it, and
`scripts/tracks.py` **rejects** a row naming a branch that does not exist on origin — which
is how the first draft of this section was caught. Writing the wave down here instead is
free of that, because nothing parses this prose.

Worth recording why the attempt was made and why it failed, since the reasoning was not
silly. The Worktrees table is the one part of this file concurrent branches reliably
collide on — it was the only conflict PRs #24 and #25 produced between them — so claiming
a whole known wave in one commit would both avoid a three-way collision and close the
blind spot the section above names, the window between `start-item.sh` and pushing the
row. The validator refuses it anyway, and it is right to: a row whose branch does not
exist cannot be distinguished from a stale row whose branch was deleted, and the whole
registry rests on being able to tell those apart. **The three-line collision is the
cheaper problem.** Take it.

**Wave 9 closed 2026-08-28 — all three lane heads merged.** `M1-506` (PR #43, r1 approve),
`M1-404` (PR #47, r2) then `M1-405` (PR #45, r3) on lane 1; `M2-711` (PR #46) on lane 2;
`M1-609` (merged, backlog-Done) opening lane 3's debt queue. The board was clear again
before this section was rewritten — zero open PRs, zero worktrees — the same state Wave
Eight described at its own boundary.

Lane 3's debt queue is on its last item. `M1-609` ran first, then `M2-710` (merged PR
#54, 2026-09-01, round-1 approval), `M1-608` (merged PR #57, round-1 approve), and `M1-314`
(merged PR #58, 2026-09-02). **`M2-709` is live as of 2026-09-02** — the worktree is set up
but work has not started.

Wave 9, three lanes, run concurrently. **Lane 1 is two stages**: `M1-506` must be *on
master* before `M1-404`/`M1-405` start, because both of those register a checker into the
seam `M1-506` moves and neither can be written against the private `parse._output_problems`
they would otherwise both edit (lesson 1 — a seam that changes underneath an open review).

| Lane | Items, in order | Notes |
| --- | --- | --- |
| 1 — critical path | **`M1-506`** → `M1-404` → `M1-405` | `M1-506` exposes one public composed output-validation entry point as a **table keyed on the `question_type` literal** with an explicit entry per supported type. That shape was expected to reduce `M1-404` and `M1-405` to one changed line each so the two could run concurrently. **It did not, for either of them, and both corrections are below.** `M1-405` (Critical) unblocks `M1-503` → `T-904`; `M1-404` unblocks `M1-502`; `M2-707` needs both. |

**Lane 1 stage B was serial, and neither branch predicted it correctly.** The "one changed
line each" claim held for the *registration* and not for the *signature*, and both items
discovered that independently, at the same time, in the same file.

`M1-405`'s criterion is "percentile levels are exact; values are finite, ordered and
**compatible with question bounds**"; `M1-404`'s is "every exact option once". Neither the
response nor the config carried a question, so each branch widened `_TypeChecker`,
`output_problems`/`validate_output`, `parse._parse` and `generate._run_attempts` — and each
wrote into its own notes that the *other* would be the easy one-line case. Both were wrong.
They differed in shape, too: `M1-404` added a keyword-only `options: Sequence[str] | None`
threaded from `ModelInput.packet` and kept `question_id: int`; `M1-405` replaced the id with
`question: CanonicalQuestion`.

**`M1-404` merged first (PR #47, 2026-08-28), so `M1-405` converged onto it.** The converged
seam is `question`-only: `multiple_choice_output_problems` reads `question.options` rather
than a separate argument, because `forecast/inputs.py` builds that packet field as
`list(question.options)` and carrying both is one fact reached two ways — M2-703's
second-source-of-truth lesson, the same one that removed `question_id`. Two things fell out
rather than being designed: `M1-404`'s biconditional option/type pairing gate is retired
(the `qtype` gate subsumes it), and so is its standing risk that nothing verified the packet
copy, because there is no copy.

**The registry could not have prevented this and should not be read as though it could.**
Nothing here records *which signatures* an item will touch, only deps and migration numbers.
Two items editing one file's signature in incompatible ways is a collision this table is
blind to by construction — worth knowing before the next wave plans two checkers against one
dispatch table.
| 2 — M2 path | **`M2-711`** | Records a submission whose outcome no refetch established — the `(False, False)` cell that today reads as terminal `submission_failed`, which is more than the ledger knows. Needs a lifecycle vocabulary member and therefore **migration `009`**, claimed above. No `forecast/` overlap with lane 1. |
| 3 — debt queue | **`M1-609`** → `M2-710` → `M1-608` → `M1-314` → `M2-709` | One branch each, sequentially. Every one closes a deferral already filed off a previous review, which is `docs/LESSONS.md` checklist item 4 paid down rather than re-reported as a finding next round. All sized S. |

As of 2026-08-25 all three lane heads went live: `M1-506`, `M2-711`, `M1-609`. Stage B took
the wave to four concurrent worktrees, which is `wj-layout`'s `max_panes` exactly. All are
now merged; see "Wave 9 closed" above.

## Wave 10

Four heads, one per pane, started 2026-08-28 off a clean `origin/master` (0 open PRs, 0
worktrees). Guidance below draws on the Wave Eight write-up (published as an Artifact,
"Whiskeyjack Wave Eight"), which planned this pairing before `M1-506`/`M1-404`/`M1-405`
existed and turned out right about which items are safe to run concurrently and which
are not.

| Lane | Item | Worktree | Notes |
| --- | --- | --- | --- |
| 1 — critical path, fan-out | `M1-502` | `whiskeyjack-m1-502` | Categorical validation (binary + multiple-choice), feeding on `M1-404`. Deps (`M1-403`, `M1-404`) both merged. Unlike the `M1-404`/`M1-405` collision, this and `M1-503` are expected to land in **different files** — `binary.py`/`multiple_choice.py` versus `numeric.py` — so Wave Eight's own chain diagram runs them side by side rather than serially. Verify that assumption early rather than at round 1: if both end up widening one shared entry point the way `M1-404`/`M1-405` widened `parse.py`, stop and serialize. |
| 1 — critical path, fan-out | `M1-503` | `whiskeyjack-m1-503` | Numeric CDF via `NumericDistribution.from_question`/`get_cdf` on the pinned 0.2.92 SDK — 201 monotone values, PMF-constrained. `T-904` (contract tests) depends on this and stays queued behind it. Highest-subtlety item in the wave; Wave Eight rated it `xhigh`. |
| 2 — submission safety | `M2-708` | `whiskeyjack-m2-708` | Atomic idempotency-key reservation before any network I/O — closes the read-then-post race `require_key_unused()` leaves open. Deps (`M2-702`, `M2-704`) both merged. Must land **before `M2-706`** (the first live-network smoke test) ever fires; this is the project's own "when a shortcut would weaken the ledger, don't take it" rule applied to submission rather than the ledger schema. Evaluate whether the reservation needs a schema change — if so, claim migration `010` in the standing-claims table above *before* writing the `.sql` file, not after. |
| 3 — acceptance evidence | `T-903` | `whiskeyjack-t-903` | Dry-run acceptance test: one command, one validated record, zero provider/submission calls. All three deps (`M1-306`, `M1-406`, `M1-602`) closed within the last few days. No `forecast/` or `submission` overlap with the other three lanes — genuinely independent, which is why it took the fourth pane instead of a fifth debt-queue branch. |

**Queued behind these four, not yet started — pull into the next pane that frees:**

- Lane 3 debt queue, in order: `M2-710` → `M1-608` → `M1-314` → `M2-709` (all size S, all
  Low priority, all closing a deferral already filed off a previous review). This is the
  order `M1-609` was queued in front of and it predates this rewrite; no dependency in
  `backlog.csv` forces it (each depends only on items already on master), so re-check it
  is still the right order before pulling the next one rather than assuming the sequencing
  reasoning survived the rewrite.
- Lane 2 continuation: `M2-710` also refuses an identifier the ledger cannot store when
  deriving a submission key, so it touches `submission.py` — check it against `M2-708`'s
  diff before starting in case the two touch the same validator.
- Test queue continuation, after `T-903`: `T-902` (mock Metaculus — needs `T-903`'s
  fixtures to be worth writing against), then `T-901` / `M1-605` / `M2-705` in parallel.
- Lane 4, owner-only, still `Blocked`: `A-1101`–`A-1104`. Nothing here substitutes for
  them; `D-1001` (operator runbook) is unblocked and owner-facing but not owner-only —
  worth writing before `M2-706` is anywhere close, per Wave Eight's argument that the
  runbook belongs before the first live post, not after it.

**Why `M1-502`/`M1-503` are treated as parallel-safe where `M1-404`/`M1-405` were not.**
The latter pair collided because both widened the *same* private dispatch function,
`parse._output_problems`, before `M1-506` gave each a separate registration slot in a
shared table. `M1-502` and `M1-503` register into that same table too (per `M1-506`'s
design, exactly as intended) but the type-specific logic each adds lives in the file that
already owns its type — there is no shared function body left to widen. This is a
prediction, not a settled fact; if it is wrong, it will be wrong the same way the last one
was, in one file, discovered independently by both branches. Watch for it at each daily
`sync-worktrees.sh --merge`.

**Why `M1-506` leads rather than follows `M1-404`/`M1-405`.** The opposite order is the
tempting one — its criterion is *"a test fails if a supported question type has a checker the
entry point does not reach"*, which sounds like it wants the other two checkers to exist first.
It does not. `forecast/schema.py`'s `_RESPONSE_MODELS` already carries all three keys
(`binary`, `multiple_choice`, `numeric`), so the coverage test is fully discriminating today:
the failure it must catch is *a supported type with no entry*, and two of the three types are
in exactly that state right now. Running it first is also what keeps the other two off one
shared private function on concurrent branches.

**One thing `M1-506` deliberately does not fix, filed as `M1-507`.** `forecast/store.py`
never imports `ForecastConfig` and never calls `binary_output_problems` — `_require_attributable`
runs `validate_attribution_fields` alone, so the persist path validates attribution but not the
type-specific bounds. Closing that needs `ForecastConfig` threaded into `append_forecast_version`,
a signature change to a merged, reviewed public entry point, and `M1-506`'s criteria do not ask
for it. Same convention as `M1-314`, `M2-709` and `M1-608`: an adjacent pre-existing defect is a
row, not a cross-item fix.

**From wave 8, kept because the reasoning still applies.** Its lane-3 order was not the
numeric one either, and the two departures are why:

- **M1-607 was third, not last.** It puts the non-blank identifier guard on
  `forecast_records.record_id`, and `M1-602` — last on lane 1 — is the item that starts
  writing that column. The guard is on master before the writer, which was the whole point.
- **M1-311 was sequenced last because its shape was unknown, and it was — it spent the
  dependency slot.** Rejecting multi-label public suffixes (`co.uk`, `com.au`) needed a real
  public-suffix-list package rather than the narrower dependency-free rule (owner decision,
  2026-08-25); it merged the same day and the slot is free again above.

`scripts/start-item.sh <ITEM> <slug> [--deps]` creates the worktree and prints the row
to add; `scripts/finish-item.sh <ITEM>` removes it after the PR merges. One worktree per
item, named for its branch, created fresh and never reused — a worktree called
`whiskeyjack-m1-401` that actually held the `m1-305` branch cost an evening.

**Wave 10 closed 2026-08-30 — all four lane heads merged** (`M1-502` PR, `M1-503` PR #52,
`M2-708` PR #51, `T-903` PR #50), board clean at `origin/master` `84d0856` before Wave 11
started.

## Wave 11

Three heads, one per pane, started 2026-08-30 off a clean `origin/master` (0 open PRs, 0
worktrees). `M2-707` is deliberately **not** a fourth pane this wave — see below.

| Lane | Item | Worktree | Notes |
| --- | --- | --- | --- |
| 1 — pipeline | `M1-315` | `whiskeyjack-m1-315` | Compose the live paid forecast run. Deps (`M1-312`, `T-903`) both merged. Its own acceptance criteria require either moving the paid composition outside `whiskeyjack_bot.pipeline` or restating that module's zero-provider-call import-graph test — a design decision to write down before round 1, not discover during it. No overlap with the other two lanes. **Ran L, not the M the row estimated** (row updated): the criterion needs live retrieval and **no retrieval orchestrator existed** — M1-306 deferred `decide_fallback` → `retrieve_web` as "a follow-up row, not this branch" and no row was ever filed, so this item absorbed it as a second module (owner decision, 2026-08-30). Worth carrying into the next wave's planning: a backlog estimate made when a dependency was assumed to exist is one this registry has no column to catch. Also chose **two commands** — `run` is now the live paid one and T-903's replay is `run-replay` — so the money boundary is a subcommand name rather than an omitted flag. Filed M1-317 … M1-321 off the work. |
| 2 — submission | `M2-710` | `whiskeyjack-m2-710` | Debt-queue continuation from Wave 9/10 (`M1-609` → `M2-710` → `M1-608` → `M1-314` → `M2-709`). Deps (`M1-607`, `M2-702`) both merged. S/Low, mechanical: align `submission._require_text` with `006_non_blank_identifiers.sql`'s trigger, restore the property strategy narrowed on the M2-703 branch. No schema change. |
| 3 — testing | `T-901` | `whiskeyjack-t-901` | Golden schema fixtures. Deps (`M1-201`, `M1-501`) both merged. **Owner-split note:** CLAUDE.md assigns T-901–T-904 to Codex — independent acceptance-test authorship, written from spec without reading the implementation. Started here as Claude Code work by owner decision (2026-08-30); whoever works this branch should draft from `CODEX_HANDOFF.md`'s test-requirements section and the `M1-201`/`M1-501` schemas, not from reading `forecast/`'s current implementation, to preserve the independent-test intent. |

**`M2-707` is live as of 2026-09-02**, on `feat/m2-707-bind-approval-payload`, alongside
`M2-709` on `feat/m2-709-shared-artifact-writer`. **The two overlap in one file and it is
worth naming the regions now rather than at a merge conflict**, because this is the
`M1-404`/`M1-405` shape the section below already warns about — two items editing one
module, discovered independently at round 1. `M2-709` touches `submission_gateway.py` in
five places and no others: the module docstring header, the import block, one paragraph of
`write_live_artifact`'s docstring, the two artifact-writer call sites, and the deleted
private `_write_or_confirm`/`_confirm_identical` block. It touches **no validator and no
payload function**, which is where `M2-707` is expected to work, so the collision should be
textual at worst. Whichever branch merges second should re-read the other's diff at its daily
`sync-worktrees.sh --merge` rather than trusting this note, which was written before
`M2-707` had any code.

The original queueing reasoning, kept because it is still why they are worth watching. Both touch `submission.py`'s
validators — `M2-710` refuses a bad identifier when deriving a key, `M2-707` binds an
approval to the payload the key was derived from (decision `D33`) — the same shape of
collision this file already documents for `M1-404`/`M1-405` (two items widening one shared
function before a review round catches it). Deps (`M2-702`, `M1-502`, `M1-503`) are all
merged, so it is otherwise ready; pull it into the pane `M2-710` frees. It will also need
**migration `011`** (claim it in the standing-claims table above before writing the `.sql`
file) — `D33` names the shape: "Bind approval to a payload hash now (migration + a changed
M2-701 command...)".

## Rules that fall out of this

- **One dependency-adding item per wave.** If your item needs a new package and the
  claim above is taken, either wait or hand the dependency to the track that holds it.
  `scripts/start-item.sh <ITEM> <slug> --deps` refuses to start while the slot is held;
  `scripts/tracks.py claims` lists what is live right now. The Worktrees heading, columns and row
  widths are validated; a malformed registry or an unknown branch that has not landed on master
  blocks rather than silently reading as “free.”
- **Agree the migration number before starting.** Two branches adding `003_alpha.sql`
  and `003_beta.sql` merge cleanly in git and collide only at runtime.
- **Merge `master` into every active branch daily**: `scripts/sync-worktrees.sh --merge`.
  M1-302 reached its merge 18 commits behind and paid for all of them at once.
- **Flip the backlog row to `Done` on the branch before merging.** CI's
  `backlog-status` check fails until you do.
