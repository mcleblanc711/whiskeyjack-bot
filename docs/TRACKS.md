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
| Dependency additions (`pyproject.toml` + `uv.lock`) | *free* | Previously claimed by M1-303 on 2026-07-27 and **released unused** (the Exa adapter used `httpx` instead of adding `exa-py`). **M1-311 claimed it 2026-08-25, spent it, and merged** (PR #40, round-2 approval, 2026-08-25): `publicsuffix2` rejects multi-label suffixes (`co.uk`, `com.au`) that the old dependency-free rule missed. Released now that the branch is on master. |
| Workflow / test-infrastructure change | *free* — **and should stay free until this wave closes** | Three concurrent lanes means every workflow change lands in three open review cycles at once, which is lesson 1 at triple cost. If one is genuinely needed mid-wave, claim the slot here and **say so in the next review request on every open lane** — the reviewer is stateless and reads a format change as a substantive one. Lesson 1: a workflow change is a track and takes a slot. Held 2026-08-17 by `test/tmpfs-temp-root` (PR #24) and `chore/review-loop` (PR #25), **both merged and released the same day**. #24 moved pytest's temp root to tmpfs (`tests/conftest.py`): full suite 497.7s → 81.3s, `test_lifecycle.py` 96.5s → 3.2s, because the dev machine's only drive is a 7200rpm platter at 49.6ms/fsync. #25 added `scripts/gate.sh`, `scripts/run-review.sh` and the `fast` hypothesis profile. They landed **mid-wave** by deliberate exception, having been checked against every live branch first — the only conflict was this table. **Say so in the next review request:** the conftest change moves where temp files land, nothing about what is asserted. |
| Next free migration number | **`009` — held by M2-711** (`feat/m2-711-submission-outcome-unknown`, claimed 2026-08-25); `010` is the next free one | `001`-`008` are immutable on master; `006_non_blank_identifiers.sql` landed with **M1-607**, `007_forecast_version_chain.sql` with **M1-602** (PR #38), and **`008_forecast_raw_output.sql` with M1-406** (merged 2026-08-25, PR #41) — it added `raw_output_path`, `cost_usd` and `model_invocations` to `forecast_records` and appended three clauses to `forecast_records_require_draft_on_insert`, the fourth DROP/CREATE of that trigger (`004`, `006`, `007`, `008`) and the reason that pattern is worth keeping cheap. **M2-711 spent `009`** (`009_submission_refetch_outcome.sql`, 2026-08-26): recording a post whose outcome no refetch established needed a vocabulary member that `(success, verified_by_refetch)` had no room for. Worth recording **which** vocabulary, because the choice was the item: not a twelfth `lifecycle_events.event_type`, which is a column `CHECK` and so costs a rebuild of the append-only table `003`'s header exists to protect, but a new `submission_attempts.refetch_outcome` column reached by `ADD COLUMN`, with `submission_uncertain` widened to cover the new cell. One `ADD COLUMN` plus two `DROP`/`CREATE` trigger rewrites — the cheap escape hatch `004`, `006`, `007` and `008` used, now on `submission_attempts_require_receipt_on_insert` (second rewrite, after `006`) and `lifecycle_events_validate_on_insert` (**first** rewrite since `003` wrote it). It was the only item in wave 9 that needed a migration. Remember this column is advisory and nothing reads it — `.github/scripts/check-migrations.sh` plus master's up-to-date-branch requirement is the enforcement, as the `004` collision records: `004_pipeline_failure_events.sql` landed with **M1-606** and `005_research_run_counters.sql` with **M1-306**, and both branches were told `004` was free, because a claim lives on its holder's branch and this column is advisory — `scripts/tracks.py` checks the *dependency* claim and nothing reads this one. M1-606 merged first; M1-306 renumbered to `005` at its daily master merge, which is the designed outcome, and renumbering was safe only because M1-306's `004` had never reached master. |

## Worktrees

| Item | Branch | Worktree | Adds deps? | Migration | Started |
| --- | --- | --- | --- | --- | --- |
| M1-609 | feat/m1-609-verify-foreign-keys | whiskeyjack-m1-609 | no | none | 2026-08-25 |
| M1-405 | feat/m1-405-numeric-percentile-path | whiskeyjack-m1-405 | no | none | 2026-08-26 |
| M2-711 | feat/m2-711-submission-outcome-unknown | whiskeyjack-m2-711 | no | 009 (spent) | 2026-08-25 |
| T-903 | feat/t-903-dry-run-acceptance | whiskeyjack-t-903 | no | none | 2026-08-28 |
*(rows above; each lands on its own branch as it starts — see the planned wave below. The
three rows above this one are Wave 9's, now merged — `feat/m1-502-categorical-validation`
sweeps them; take that version at the next `sync-worktrees.sh --merge` rather than
re-sweeping here.)*

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

As of 2026-08-25 all three lane heads are live: `M1-506`, `M2-711`, `M1-609`. Stage B takes
the wave to four concurrent worktrees, which is `wj-layout`'s `max_panes` exactly.

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
