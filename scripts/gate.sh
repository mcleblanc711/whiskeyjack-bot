#!/usr/bin/env bash
# The four toolchain gates, cheapest first, stopping at the first failure.
#
#   scripts/gate.sh
#
# This is the inner-loop counterpart to scripts/review-request.py's gate block, and it is
# deliberately the same four commands with the same hypothesis profile pinned: a green
# gate.sh must mean the same thing as a green gate section in a review request, or it is
# just a second opinion to reconcile. The two differ only in what they do with a failure --
# the request runs all four and reports every one, because a reviewer wants the whole
# picture in one pass; this stops at the first, because you are about to fix it anyway.
#
# Ordering is measured, not guessed: ruff 0.5s, ruff format 0.1s, mypy 1.0s, pytest ~85s.
# Running pytest first meant a formatting slip cost the whole suite before you heard about
# it -- eight minutes of it, before the temp root moved to tmpfs.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

# Pinned for the same reason review-request.py pins it: tests/property/conftest.py
# registers `fast` at 25 draws for the inner loop, and a gate that inherited it would go
# green on an eighth of the search. If you want the fast profile, run pytest directly --
# this command is the one that is allowed to mean "done".
export HYPOTHESIS_PROFILE="${GATE_HYPOTHESIS_PROFILE:-dev}"

run_gate() {
  local label="$1"; shift
  printf '  %-22s ' "$label"
  local started output status
  started=$SECONDS
  if output="$("$@" 2>&1)"; then
    status=0
  else
    status=$?
  fi
  if [ "$status" -eq 0 ]; then
    printf 'pass  (%ds)\n' "$((SECONDS - started))"
    return 0
  fi
  printf 'FAIL  (%ds)\n\n' "$((SECONDS - started))"
  printf '%s\n' "$output" | tail -40
  printf '\n%s failed; the remaining gates were not run.\n' "$label" >&2
  exit "$status"
}

echo "gates (hypothesis profile: $HYPOTHESIS_PROFILE)"
run_gate "ruff check"          uv run ruff check .
run_gate "ruff format --check" uv run ruff format --check .
run_gate "mypy --strict src"   uv run mypy --strict src
run_gate "pytest"              uv run pytest -q
echo
echo "All four gates pass."
