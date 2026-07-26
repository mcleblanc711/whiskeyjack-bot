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

mapfile -t branches < <(git for-each-ref --format='%(refname:short)' \
  "refs/heads/feat/${item_lower}-*" "refs/heads/fix/${item_lower}-*")
if [[ ${#branches[@]} -eq 0 ]]; then
  echo "No local branch matches feat/${item_lower}-* or fix/${item_lower}-*." >&2
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

status="$(python3 .github/scripts/check_backlog.py status "$item_id" 2>/dev/null || true)"
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

echo
echo "Remaining: drop the $item_id row from docs/TRACKS.md on your next branch."
if [[ -n "$status" ]]; then
  echo "Backlog status for $item_id is now: $status"
fi
