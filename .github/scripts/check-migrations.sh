#!/usr/bin/env bash
# Ledger migration hygiene.
#
# Migration numbers are claimed globally: ledger.py applies unrecorded migrations in
# version order and records each with a sha256 checksum, so two parallel branches each
# adding an 003_*.sql collide. When the filenames differ (003_alpha.sql vs 003_beta.sql)
# git merges both cleanly and the collision only surfaces at runtime, against a database
# that has already recorded one of them. This check makes that a merge-time failure.
#
# Also enforced: a migration that already exists on origin/master is immutable. Editing
# one changes its recorded checksum and breaks every database that applied the original.
set -euo pipefail

migrations_dir="src/whiskeyjack_bot/migrations"
name_pattern='^[0-9]{3}_[a-z0-9_]+\.sql$'

mapfile -t files < <(git ls-files "$migrations_dir/*.sql" | sort)

if [[ ${#files[@]} -eq 0 ]]; then
  echo "Migration check: no migrations tracked under $migrations_dir." >&2
  exit 1
fi

failed=0
declare -A number_to_path=()

for path in "${files[@]}"; do
  base="${path##*/}"
  if [[ ! "$base" =~ $name_pattern ]]; then
    echo "Migration filename is not NNN_snake_case.sql: $path" >&2
    failed=1
    continue
  fi
  number="${base%%_*}"
  if [[ -n "${number_to_path[$number]:-}" ]]; then
    echo "Migration number $number is claimed twice: ${number_to_path[$number]} and $path" >&2
    failed=1
    continue
  fi
  number_to_path["$number"]="$path"
done

# Numbers must run 001..N with no gaps, so "the next free number" is unambiguous
# for whoever claims it in docs/TRACKS.md.
expected=1
for number in $(printf '%s\n' "${!number_to_path[@]}" | sort); do
  if [[ "$number" != "$(printf '%03d' "$expected")" ]]; then
    echo "Migration numbering has a gap or does not start at 001: found $number, expected $(printf '%03d' "$expected")" >&2
    failed=1
  fi
  expected=$((expected + 1))
done

if git rev-parse --verify --quiet origin/master >/dev/null; then
  while IFS= read -r base_path; do
    base="${base_path##*/}"
    [[ "$base" =~ $name_pattern ]] || continue
    number="${base%%_*}"
    branch_path="${number_to_path[$number]:-}"

    if [[ -z "$branch_path" ]]; then
      echo "Migration $base_path exists on origin/master but not on this branch; migrations are append-only." >&2
      failed=1
      continue
    fi

    if [[ "$branch_path" != "$base_path" ]]; then
      echo "Migration number $number is already taken on origin/master by $base_path, but this branch uses $branch_path. Claim the next free number in docs/TRACKS.md and renumber." >&2
      failed=1
      continue
    fi

    if ! git diff --quiet origin/master -- "$base_path"; then
      echo "Migration $base_path is modified relative to origin/master. Applied migrations are immutable (ledger.py records a sha256 per version); add a new migration instead." >&2
      failed=1
    fi
  done < <(git ls-tree -r --name-only origin/master -- "$migrations_dir" | grep '\.sql$' || true)
else
  echo "Migration check: origin/master not available, skipping the cross-branch comparison."
fi

if [[ $failed -ne 0 ]]; then
  exit 1
fi

echo "Migration check passed (${#number_to_path[@]} migrations, next free number $(printf '%03d' "$expected"))."
