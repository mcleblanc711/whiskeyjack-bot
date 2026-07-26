#!/usr/bin/env bash
# Start a backlog item in its own fresh worktree.
#
#   scripts/start-item.sh M1-303 exa-fallback --deps
#
# One worktree per item, named for the branch, created fresh and removed at merge
# (scripts/finish-item.sh). Reusing or renaming a worktree is what produced a directory
# called whiskeyjack-m1-401 holding the m1-305 branch, so this refuses to touch an
# existing directory or branch.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

usage() {
  echo "usage: scripts/start-item.sh <ITEM-ID> <slug> [--deps]" >&2
  echo "  --deps  this item will add a dependency (uv.lock); warns if another track holds it" >&2
  exit 2
}

[[ $# -ge 2 ]] || usage
item_id="${1^^}"
slug="$2"
shift 2
adds_deps=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --deps) adds_deps=1 ;;
    *) usage ;;
  esac
  shift
done

if [[ "$(git rev-parse --git-dir)" != ".git" ]]; then
  echo "Run this from the main checkout, not a linked worktree." >&2
  exit 1
fi
if [[ ! "$slug" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "Slug must be lower-case kebab-case: '$slug'" >&2
  exit 1
fi

python3 .github/scripts/check_backlog.py lint >/dev/null
if ! grep -q "^${item_id}," docs/backlog/backlog.csv; then
  echo "$item_id has no row in docs/backlog/backlog.csv. Add the row (with acceptance" >&2
  echo "criteria) before starting — the CI backlog-status gate looks the item up by ID." >&2
  exit 1
fi

item_lower="${item_id,,}"
branch="feat/${item_lower}-${slug}"
worktree="../whiskeyjack-${item_lower}"

if [[ -e "$worktree" ]]; then
  echo "$worktree already exists. Finish or remove that track first (scripts/finish-item.sh)." >&2
  exit 1
fi
if git show-ref --quiet --verify "refs/heads/$branch"; then
  echo "Branch $branch already exists locally." >&2
  exit 1
fi

git fetch --prune origin

if git show-ref --quiet --verify "refs/remotes/origin/$branch"; then
  echo "Branch $branch already exists on origin." >&2
  exit 1
fi

if [[ $adds_deps -eq 1 ]] && grep -qi '| *yes *|' docs/TRACKS.md 2>/dev/null; then
  echo
  echo "WARNING: docs/TRACKS.md already shows an active dependency-adding track." >&2
  echo "uv.lock serializes tracks: two items adding dependencies at once merge messily." >&2
  echo "Continuing anyway — check docs/TRACKS.md before you touch pyproject.toml." >&2
  echo
fi

git worktree add -b "$branch" "$worktree" origin/master
(cd "$worktree" && uv sync)

cat <<EOF

Worktree ready: $worktree   (branch $branch, off origin/master)

Next:
  1. Claim the track in docs/TRACKS.md (on this branch), e.g.
     | $item_id | $branch | whiskeyjack-$item_lower | $([[ $adds_deps -eq 1 ]] && echo yes || echo no) | none | $(date -u +%Y-%m-%d) |
  2. Flip $item_id to 'In Review' in docs/backlog/backlog.csv when the PR opens, and to
     'Done' before merge — CI's backlog-status job fails until that flip lands.
  3. cd $worktree
EOF
