#!/usr/bin/env bash
# Report (and optionally close) how far each active worktree is behind origin/master.
#
#   scripts/sync-worktrees.sh            # report only
#   scripts/sync-worktrees.sh --merge    # merge origin/master into every clean worktree
#
# Run it daily. M1-302 reached merge time 18 commits behind master and the conflicts
# landed all at once, in the backlog CSV and the workbook; a daily merge keeps each
# conflict small enough to resolve in the branch it came from.
set -euo pipefail

merge=0
[[ ${1:-} == "--merge" ]] && merge=1
if [[ $# -gt 0 && ${1:-} != "--merge" ]]; then
  echo "usage: scripts/sync-worktrees.sh [--merge]" >&2
  exit 2
fi

git -C "$(git rev-parse --show-toplevel)" fetch --prune origin

status=0
while IFS= read -r line; do
  [[ "$line" == worktree\ * ]] || continue
  path="${line#worktree }"

  branch="$(git -C "$path" rev-parse --abbrev-ref HEAD 2>/dev/null || echo DETACHED)"
  behind="$(git -C "$path" rev-list --count "HEAD..origin/master" 2>/dev/null || echo '?')"
  ahead="$(git -C "$path" rev-list --count "origin/master..HEAD" 2>/dev/null || echo '?')"
  dirty=""
  [[ -n "$(git -C "$path" status --porcelain)" ]] && dirty=" [dirty]"

  printf '%-46s %-42s %s behind, %s ahead%s\n' \
    "${path/#$HOME/\~}" "$branch" "$behind" "$ahead" "$dirty"

  if [[ $merge -eq 1 && "$behind" != "0" && "$behind" != "?" ]]; then
    if [[ -n "$dirty" ]]; then
      echo "    skipped: worktree has uncommitted changes." >&2
      status=1
      continue
    fi
    if git -C "$path" merge --no-edit origin/master; then
      echo "    merged origin/master."
    else
      echo "    CONFLICT — resolve in $path, then re-run." >&2
      status=1
    fi
  fi
done < <(git worktree list --porcelain)

exit "$status"
