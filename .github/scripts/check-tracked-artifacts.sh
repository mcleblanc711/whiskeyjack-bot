#!/usr/bin/env bash
set -euo pipefail

tracked_files="$(git ls-files)"
forbidden="$({
  grep -E '(^|/)\.env[^/]*$' <<<"$tracked_files" || true
  grep -E '(^|/)data/' <<<"$tracked_files" || true
  grep -E '(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.cache|\.tox|\.nox)/' <<<"$tracked_files" || true
  grep -E '(^|/)(build|dist|htmlcov|\.eggs|[^/]+\.egg-info)/' <<<"$tracked_files" || true
  grep -E '(^|/)\.coverage(\..*)?$|(^|/)coverage\.xml$' <<<"$tracked_files" || true
  grep -E '\.(py[co]|egg|whl|tar\.gz)$|\.(db|sqlite|sqlite3)(-(wal|shm))?$' <<<"$tracked_files" || true
} | sort -u)"

if [[ -n "$forbidden" ]]; then
  echo "Forbidden secret or generated artifacts are tracked:" >&2
  echo "$forbidden" >&2
  exit 1
fi

echo "Tracked-artifact hygiene check passed."
