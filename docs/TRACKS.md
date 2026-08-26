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
| Next free migration number | **`009` — held by M2-711** (`feat/m2-711-submission-outcome-unknown`, claimed 2026-08-25); `010` is the next free one | `001`-`008` are immutable on master; `006_non_blank_identifiers.sql` landed with **M1-607**, `007_forecast_version_chain.sql` with **M1-602** (PR #38), and **`008_forecast_raw_output.sql` with M1-406** (merged 2026-08-25, PR #41) — it added `raw_output_path`, `cost_usd` and `model_invocations` to `forecast_records` and appended three clauses to `forecast_records_require_draft_on_insert`, the fourth DROP/CREATE of that trigger (`004`, `006`, `007`, `008`) and the reason that pattern is worth keeping cheap. **M2-711 holds `009`**: recording a post whose outcome no refetch established needs a lifecycle vocabulary member that `(success, verified_by_refetch)` has no room for, and a vocabulary member is a migration. It is the only item in wave 9 that needs one. Remember this column is advisory and nothing reads it — `.github/scripts/check-migrations.sh` plus master's up-to-date-branch requirement is the enforcement, as the `004` collision records: `004_pipeline_failure_events.sql` landed with **M1-606** and `005_research_run_counters.sql` with **M1-306**, and both branches were told `004` was free, because a claim lives on its holder's branch and this column is advisory — `scripts/tracks.py` checks the *dependency* claim and nothing reads this one. M1-606 merged first; M1-306 renumbered to `005` at its daily master merge, which is the designed outcome, and renumbering was safe only because M1-306's `004` had never reached master. |

## Worktrees

| Item | Branch | Worktree | Adds deps? | Migration | Started |
| --- | --- | --- | --- | --- | --- |
| M2-711 | feat/m2-711-submission-outcome-unknown | whiskeyjack-m2-711 | no | 009 | 2026-08-25 |
*(rows above; each lands on its own branch as it starts — see the planned wave below)*

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

Three lanes, run concurrently:

| Lane | Items, in order | Notes |
| --- | --- | --- |
| 1 — critical path | ~~`M1-402`~~ → ~~`M1-403`~~ → ~~`M1-501`~~ → ~~`M1-602`~~ → ~~`M1-406`~~ | Closed. `M1-406` merged (PR #41, 2026-08-25), spending migration `008`; it closed the replay loop the ledger exists for. |
| 2 — M2 path | ~~`M2-701`~~ → ~~`M2-702`~~ → ~~`M2-703`~~ → **`M2-704`** | `M2-703` merged (PR #39, 2026-08-22). `M2-704` (package-backed gateway) is the first item in the repo that can actually post to Metaculus — sized L, highest blast radius so far. |
| 3 — debt queue | ~~`M0-007`~~ → ~~`M1-313`~~ → ~~`M1-607`~~ → ~~`M1-312`~~ → ~~`M1-309`~~ → ~~`M1-311`~~ | Closed. `M1-311` merged (PR #40, round-2 approval, 2026-08-25) — the last of the six small items. |

Struck items are merged. As of 2026-08-25 the only live lane head is **M2-704**,
one worktree; lanes 1 and 3 are closed. `M2-704` expects no new dependency and no
migration, and the dependency slot is free now that `M1-311` spent and released it. **M1-312 merged approved in round 1** (PR #35), the project's
third single-round approval; its composition (`research/persist.py`) is the API a
retrieval orchestrator will call.

**M1-602 waits for M1-501, and the M2-702 precedent does not license skipping it.** M2-702
shipped against `forecast_records` while M1-602 was `Not Started`, and its notes say why
that was safe: it reads three columns that have been immutable since `001`, and it
anticipates nothing about M1-602's shape. M1-602 is the opposite case — it writes
`record_json` and `final_prediction_json`, which are exactly what M1-501 constrains. So the
serial hop is paid on purpose (owner decision, 2026-08-21).

The lane-3 order is not the numeric one, and the two departures are the point:

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
