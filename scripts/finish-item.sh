#!/usr/bin/env bash
# Retire a merged item: remove its worktree, delete its branch, fast-forward master.
#
#   scripts/finish-item.sh M1-303                   # local cleanup
#   scripts/finish-item.sh M1-303 --delete-remote   # also delete the branch on origin
#
# Run from the main checkout, AFTER the PR is merged and WITHOUT `gh pr merge
# --delete-branch`: that flag fails when the branch is checked out in a sibling
# worktree, which is how M1-302's branch survived its own merge. Order matters —
# worktree first, then the branch.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

usage() {
  echo "usage: scripts/finish-item.sh <ITEM-ID> [--delete-remote]" >&2
  exit 2
}

[[ $# -ge 1 ]] || usage
item_id="${1^^}"
shift
delete_remote=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --delete-remote) delete_remote=1 ;;
    *) usage ;;
  esac
  shift
done

if [[ "$(git rev-parse --git-dir)" != ".git" ]]; then
  echo "Run this from the main checkout, not a linked worktree." >&2
  exit 1
fi

item_lower="${item_id,,}"
worktree="$(cd .. && pwd)/whiskeyjack-${item_lower}"

git fetch --prune origin

# Ask check_backlog.py which branches name this item rather than globbing for them here.
# The globs this replaced were `feat/<id>-*` and `fix/<id>-*`, which disagreed with the
# gate's own contract on three axes — it also accepts feature/, bugfix/, hotfix/, every
# upper-case spelling, and a bare `feat/<id>` with no slug — so a branch the gate was
# happy to merge could not afterwards be cleaned up (cross-model review, round 2).
#
# Assigned through a variable, not piped straight into `< <(...)`: process substitution
# swallows the exit status, so a broken classifier would read as "no branches found"
# instead of failing. Through a variable, pipefail and set -e stop the script.
classified="$(git for-each-ref --format='%(refname:short)' refs/heads \
  | python3 .github/scripts/check_backlog.py classify)"
mapfile -t branches < <(
  awk -F'\t' -v id="$item_id" '$1 == "item" && $2 == id { print $3 }' <<<"$classified"
)
if [[ ${#branches[@]} -eq 0 ]]; then
  echo "No local branch names $item_id. Expected <prefix>/${item_lower}[-slug] with a" >&2
  echo "prefix the backlog gate recognizes (feat, feature, fix, bugfix, hotfix)." >&2
  exit 1
fi
if [[ ${#branches[@]} -gt 1 ]]; then
  printf 'Ambiguous: %s\n' "${branches[*]}" >&2
  exit 1
fi
branch="${branches[0]}"

if ! git merge-base --is-ancestor "$branch" origin/master; then
  echo "$branch is not merged into origin/master yet. Merge the PR first." >&2
  echo "(If it is merged, run: git fetch origin)" >&2
  exit 1
fi

# Prove the *remote* branch is merged too, and prove it here — before anything is
# removed. The check above only covers the local ref, so a review fix pushed from another
# machine (or committed in the web UI) is invisible to it, and --delete-remote would
# delete commits that never reached master. Validate everything first, then mutate.
if [[ $delete_remote -eq 1 ]] && git show-ref --quiet --verify "refs/remotes/origin/$branch"; then
  if ! git merge-base --is-ancestor "refs/remotes/origin/$branch" origin/master; then
    echo "origin/$branch has commits that are not in origin/master; refusing to delete it." >&2
    echo "Fetch and inspect it: git log origin/master..origin/$branch" >&2
    exit 1
  fi
fi

if [[ -d "$worktree" ]]; then
  if [[ -n "$(git -C "$worktree" status --porcelain)" ]]; then
    echo "$worktree has uncommitted changes; refusing to remove it." >&2
    exit 1
  fi
  git worktree remove "$worktree"
  echo "Removed worktree $worktree"
else
  echo "No worktree at $worktree (already removed)."
fi

git worktree prune
git branch -d "$branch"
echo "Deleted local branch $branch"

if [[ $delete_remote -eq 1 ]]; then
  if git show-ref --quiet --verify "refs/remotes/origin/$branch"; then
    git push origin --delete "$branch"
    echo "Deleted origin/$branch"
  else
    echo "origin/$branch is already gone."
  fi
elif git show-ref --quiet --verify "refs/remotes/origin/$branch"; then
  echo "origin/$branch still exists. Delete it with:"
  echo "    git push origin --delete $branch"
fi

if [[ "$(git rev-parse --abbrev-ref HEAD)" == "master" ]]; then
  git merge --ff-only origin/master
else
  echo "Main checkout is not on master; skipping the fast-forward."
fi

# Read the status *after* the fast-forward, not before. Reading it first reports the
# pre-merge working tree — typically still 'In Review' — under the word "now", which is
# the exact confusion this line exists to dispel (cross-model review, round 1).
status="$(python3 .github/scripts/check_backlog.py status "$item_id" 2>/dev/null || true)"

echo
echo "Remaining: drop the $item_id row from docs/TRACKS.md on your next branch."
if [[ -n "$status" ]]; then
  echo "Backlog status for $item_id is now: $status"
fi
