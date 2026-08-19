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
| Dependency additions (`pyproject.toml` + `uv.lock`) | *free* | **One track at a time.** `uv.lock` is a 760 KB generated file; two branches adding dependencies produce a conflict no merge tool resolves usefully. Claimed by M1-303 on 2026-07-27 and **released unused**: the Exa adapter calls the HTTP API through `httpx`, already a declared dependency, rather than adding `exa-py` (which would pull in `openai`, `requests` and `python-dotenv` for one POST). Releasing a claim you did not spend is part of holding it. |
| Workflow / test-infrastructure change | *free* — **and should stay free until this wave closes** | Three concurrent lanes means every workflow change lands in three open review cycles at once, which is lesson 1 at triple cost. If one is genuinely needed mid-wave, claim the slot here and **say so in the next review request on every open lane** — the reviewer is stateless and reads a format change as a substantive one. Lesson 1: a workflow change is a track and takes a slot. Held 2026-08-17 by `test/tmpfs-temp-root` (PR #24) and `chore/review-loop` (PR #25), **both merged and released the same day**. #24 moved pytest's temp root to tmpfs (`tests/conftest.py`): full suite 497.7s → 81.3s, `test_lifecycle.py` 96.5s → 3.2s, because the dev machine's only drive is a 7200rpm platter at 49.6ms/fsync. #25 added `scripts/gate.sh`, `scripts/run-review.sh` and the `fast` hypothesis profile. They landed **mid-wave** by deliberate exception, having been checked against every live branch first — the only conflict was this table. **Say so in the next review request:** the conftest change moves where temp files land, nothing about what is asserted. |
| Next free migration number | `006` held by **M1-607** (queued behind M0-007/309/311/312/313); next free is `007` | `001`-`005` are immutable on master. `004_pipeline_failure_events.sql` landed with **M1-606** and `005_research_run_counters.sql` with **M1-306**, and the pair is worth reading as a case study: both branches were told `004` was free, because a claim lives on its holder's branch and this **migration** column is advisory — `scripts/tracks.py` checks the *dependency* claim and nothing reads this one. M1-606 merged first; M1-306 renumbered to `005` at its daily master merge, which is the designed outcome: `.github/scripts/check-migrations.sh` plus master's up-to-date-branch requirement is the enforcement, and it caught this before either number could be applied twice. Renumbering was safe only because M1-306's `004` had never reached master. |

## Worktrees

| Item | Branch | Worktree | Adds deps? | Migration | Started |
| --- | --- | --- | --- | --- | --- |
| M1-402 | feat/m1-402-structured-model-call | whiskeyjack-m1-402 | no | none | 2026-08-18 |
| M2-701 | feat/m2-701-approval-commands | whiskeyjack-m2-701 | no | none | 2026-08-18 |
| M0-007 | feat/m0-007-sanitize-yaml-constructor-errors | whiskeyjack-m0-007 | no | none | 2026-08-18 |

The three rows above are one wave, claimed together before any worktree existed rather
than each on its own branch. That is a deliberate departure from the usual flow and it is
worth saying why: the Worktrees table is the one part of this file that three concurrent
branches reliably conflict on (it was the only conflict PRs #24 and #25 produced), and
claiming a wave up front also closes the one blind spot the section above names — the
window between `start-item.sh` and pushing the row. It only works when the whole wave is
known at once. A track started later still claims on its own branch.

M0-007 is the head of a **queue**, not a single item: `M0-007 → M1-309 → M1-311 → M1-312
→ M1-313 → M1-607`, one branch and one worktree each, run in series in one terminal. Only
the head is claimed here because only the head has a worktree; the next is claimed when
the previous merges. **M1-607 is the one that needs migration `006`** — claimed below now,
because it is knowable now, even though its branch will not exist for days.

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
