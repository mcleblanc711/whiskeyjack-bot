# Active tracks

Who holds what, right now. Parallel work runs one item per worktree, and two of the
collisions that costs are invisible until merge — so they are claimed here **before** a
worktree is created, not discovered afterwards.

Edit this file on your own branch and merge it like anything else. It is a claims
registry, not a status board: the backlog CSV is the record of what is done.

## Standing claims

| Claim | Held by | Notes |
| --- | --- | --- |
| Dependency additions (`pyproject.toml` + `uv.lock`) | *free* | **One track at a time.** `uv.lock` is a 760 KB generated file; two branches adding dependencies produce a conflict no merge tool resolves usefully. |
| Next free migration number | `003` | `001_initial.sql`, `002_research_document_fields.sql` are taken. Claim the number here before you write the file. CI enforces uniqueness and immutability (`.github/scripts/check-migrations.sh`), but only *after* you push. |

## Worktrees

| Item | Branch | Worktree | Adds deps? | Migration | Started |
| --- | --- | --- | --- | --- | --- |
| _(none)_ | | | | | |

`scripts/start-item.sh <ITEM> <slug> [--deps]` creates the worktree and prints the row
to add; `scripts/finish-item.sh <ITEM>` removes it after the PR merges. One worktree per
item, named for its branch, created fresh and never reused — a worktree called
`whiskeyjack-m1-401` that actually held the `m1-305` branch cost an evening.

## Rules that fall out of this

- **One dependency-adding item per wave.** If your item needs a new package and the
  claim above is taken, either wait or hand the dependency to the track that holds it.
- **Agree the migration number before starting.** Two branches adding `003_alpha.sql`
  and `003_beta.sql` merge cleanly in git and collide only at runtime.
- **Merge `master` into every active branch daily**: `scripts/sync-worktrees.sh --merge`.
  M1-302 reached its merge 18 commits behind and paid for all of them at once.
- **Flip the backlog row to `Done` on the branch before merging.** CI's
  `backlog-status` check fails until you do.
