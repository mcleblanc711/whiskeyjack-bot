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

# Fetch before reading anything from origin, and before the backlog row check below:
# that check used to run against the on-disk CSV of a possibly-stale master, so an item
# added upstream an hour ago was rejected as having no row (cross-model review, round 2).
git fetch --prune origin

if git show-ref --quiet --verify "refs/remotes/origin/$branch"; then
  echo "Branch $branch already exists on origin." >&2
  exit 1
fi

# Not `>/dev/null`: check_backlog.py prints the problems themselves to stdout and only
# the count to stderr, so swallowing stdout reported "3 backlog problem(s)" and not one
# word about which three.
python3 .github/scripts/check_backlog.py lint
# Fall back to origin/master's copy: the working checkout can be behind, and rejecting an
# item that was added upstream an hour ago is a confusing way to be told to run git pull.
# Read into a variable rather than piping into `grep -q` — grep exits on the first match,
# which SIGPIPEs `git show`, and under pipefail that turns a found row into a failure.
upstream_backlog="$(git show origin/master:docs/backlog/backlog.csv 2>/dev/null || true)"
if ! grep -q "^${item_id}," docs/backlog/backlog.csv \
  && ! grep -q "^${item_id}," <<<"$upstream_backlog"; then
  echo "$item_id has no row in docs/backlog/backlog.csv. Add the row (with acceptance" >&2
  echo "criteria) before starting — the CI backlog-status gate looks the item up by ID." >&2
  exit 1
fi

# The registry is read across every active origin branch, not just origin/master: a claim
# lives on its own branch until that branch merges, so reading master alone could not see
# a claim for the whole period the claim exists to cover (cross-model review, round 2).
# A dependency conflict is fatal here rather than a warning — it used to print "Continuing
# anyway", which is a warning about a collision that has already been decided.
if [[ $adds_deps -eq 1 ]]; then
  python3 scripts/tracks.py deps
else
  claims="$(python3 scripts/tracks.py claims)"
  if [[ -n "$claims" ]]; then
    echo
    echo "Active claims (docs/TRACKS.md, across open branches):"
    sed 's/^/  /' <<<"$claims"
  fi
fi

# Printed before the worktree exists, not after. Claims are now observable across open
# branches, so the blind spot is down to one window: between this run and the moment you
# push the row, nobody else can see it. Writing the row first is what closes that, and
# check-migrations.sh plus uv.lock conflicts are still what enforce the outcome.
cat <<EOF

About to create $worktree (branch $branch, off origin/master).

Claim the track FIRST, in docs/TRACKS.md on the new branch — this row:

  | $item_id | $branch | whiskeyjack-$item_lower | $([[ $adds_deps -eq 1 ]] && echo yes || echo no) | none | $(date -u +%Y-%m-%d) |

If it adds a migration, put the number in that row and in the standing-claims table,
and push it before you write the .sql file.

EOF

git worktree add -b "$branch" "$worktree" origin/master
(cd "$worktree" && uv sync)

cat <<EOF

Worktree ready: $worktree   (branch $branch, off origin/master)

Next:
  1. cd $worktree
  2. Add the claim row above to docs/TRACKS.md and commit it.
  3. Flip $item_id to 'In Review' in docs/backlog/backlog.csv when the PR opens, and to
     'Done' before merge — CI's backlog-status job fails until that flip lands.
EOF
