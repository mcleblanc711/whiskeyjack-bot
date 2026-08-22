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
| Next free migration number | `008` — **`007` is held by M1-602** | `001`-`006` are immutable on master; `006_non_blank_identifiers.sql` landed with **M1-607**. **M1-602 holds `007` and has now spent it** (claimed 2026-08-21, written 2026-08-22): `007_forecast_version_chain.sql` is on `feat/m1-602-persist-forecast-versions`, so the number is gone as far as any other branch is concerned even though it is not on master yet. It is the first schema work since `006`. The row in the Worktrees table below stays until the PR merges and `finish-item.sh` removes it — a claim lives on its branch. Remember this column is advisory and nothing reads it — `.github/scripts/check-migrations.sh` plus master's up-to-date-branch requirement is the enforcement, as the `004` collision below records. `004_pipeline_failure_events.sql` landed with **M1-606** and `005_research_run_counters.sql` with **M1-306**, and the pair is worth reading as a case study: both branches were told `004` was free, because a claim lives on its holder's branch and this **migration** column is advisory — `scripts/tracks.py` checks the *dependency* claim and nothing reads this one. M1-606 merged first; M1-306 renumbered to `005` at its daily master merge, which is the designed outcome: `.github/scripts/check-migrations.sh` plus master's up-to-date-branch requirement is the enforcement, and it caught this before either number could be applied twice. Renumbering was safe only because M1-306's `004` had never reached master. |

## Worktrees

| Item | Branch | Worktree | Adds deps? | Migration | Started |
| --- | --- | --- | --- | --- | --- |
| M1-602 | feat/m1-602-persist-forecast-versions | whiskeyjack-m1-602 | no | 007 | 2026-08-21 |
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
| 1 — critical path | ~~`M1-402`~~ → ~~`M1-403`~~ → ~~`M1-501`~~ → **`M1-602`** | Strictly serial, and nothing shortens it. Protect this lane: merge master into it just after a review round closes, never mid-round. |
| 2 — M2 path | ~~`M2-701`~~ → ~~`M2-702`~~ → **`M2-703`** | Independent of lane 1 until `M2-702`, which the backlog says needs `M1-602` from it. `lifecycle.py` already documents the seam it plugs into. |
| 3 — debt queue | ~~`M0-007`~~ → ~~`M1-313`~~ → ~~`M1-607`~~ → **`M1-312`** → `M1-309` → `M1-311` | Six small items, one branch and one worktree each, in series in one terminal. |

Struck items are merged. As of 2026-08-21 the live lane heads are **M1-501**, **M2-703**
and **M1-312**, one worktree each, and nothing in this wave adds a dependency or a
migration — `submission_attempts` and `forecast_records` both ship in `001_initial.sql`.

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
- **M1-311 is last, because it is the only one whose shape is unknown.** Rejecting multi-label
  public suffixes (`co.uk`, `com.au`) needs either a real public-suffix list, which is a **new
  dependency and therefore the dep slot**, or an explicit restriction narrow enough to defend
  without one. Sequenced last so that open question stalls one item instead of five.

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
