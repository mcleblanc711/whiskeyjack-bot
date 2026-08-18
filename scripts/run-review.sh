#!/usr/bin/env bash
# Generate a cross-model review request and hand it to the local reviewer, in one step.
#
#   scripts/run-review.sh M1-310 --round 2 --previous-reviewed 9255030
#   scripts/run-review.sh M1-606 --dry-run     # write the request, spend nothing
#
# Two things this deliberately does NOT do.
#
# It does not use `codex exec review`. That subcommand builds its own diff and its own
# review framing, which is precisely what scripts/review-request.py exists to prevent it
# from doing: the request carries a four-part definition of a blocking finding, three
# scope tests, the round's stopping rule, and a deliberate-choices section written to
# pre-empt findings the author already weighed. A reviewer that frames its own review
# discards all of it, and the failure mode is an unbounded hardening exercise -- the one
# cost this project has spent the most rounds on. So: plain `codex exec`, pointed at the
# request, told to follow it exactly.
#
# It does not live inside review-request.py. That script has one job -- emit a request it
# has verified -- and it is unit-tested on the assumption that it never acts outward.
# Every run of *this* script spends a paid API call, so it stays a separate command and
# stays off the permission allowlist, prompting each time, for the same reason
# finish-item.sh is deliberately absent from .claude/settings.json.
#
# The author-side half of the round is unchanged and still binds: before writing any fix
# code, diff the commit the response names against HEAD and reproduce each finding by
# execution. A finding you cannot reproduce gets a rebuttal, not a fix.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

usage() {
  cat >&2 <<'USAGE'
usage: scripts/run-review.sh <ITEM-ID> [--round N] [--previous-reviewed <commit>]
                             [--model <name>] [--dry-run]

  --round N              review round; rounds 2+ require --previous-reviewed
  --previous-reviewed C  the exact commit the preceding review named
  --model NAME           override the reviewer model (default: codex's own config)
  --dry-run              generate and verify the request, then stop. No API call.
USAGE
  exit 2
}

[[ $# -ge 1 ]] || usage
item_id="${1^^}"
shift

round=1
model=()
dry_run=0
request_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --round)              round="$2"; request_args+=("$1" "$2"); shift 2 ;;
    --previous-reviewed)  request_args+=("$1" "$2"); shift 2 ;;
    --model)              model=(--model "$2"); shift 2 ;;
    --dry-run)            dry_run=1; shift ;;
    *)                    usage ;;
  esac
done

request="GPT_REVIEW_REQUEST_${item_id}_r${round}.md"
response="GPT_REVIEW_RESPONSE_${item_id}_r${round}.md"

# A response is the record of a round that happened. Overwriting one silently would make
# the round number a claim nothing backs -- the confusion that nearly numbered M1-308's
# round 7 as round 8. Regenerating the request is fine; replacing an answer is not.
if [[ -e "$response" ]]; then
  echo "Refusing to overwrite $response -- round $round has already been answered." >&2
  echo "Bump --round, or delete that file if the round genuinely did not happen." >&2
  exit 1
fi

echo "==> generating $request (this runs the four gates and refuses on a red branch)"
if ! scripts/review-request.py "$item_id" "${request_args[@]}" > "$request"; then
  rm -f "$request"
  echo "No request was written, so no review was requested." >&2
  exit 1
fi
if [[ ! -s "$request" ]]; then
  rm -f "$request"
  echo "The generator produced an empty request; refusing to send it." >&2
  exit 1
fi
echo "    $request ($(wc -l < "$request") lines)"

if [[ $dry_run -eq 1 ]]; then
  echo "==> --dry-run: stopping before the reviewer. Nothing was spent."
  exit 0
fi

if ! command -v codex >/dev/null 2>&1; then
  echo "codex not on PATH. The request is written; review it however you normally would." >&2
  exit 1
fi

# The brief is the file. Everything the reviewer needs to bound the review is already in
# it, so this prompt adds no criteria of its own -- it only insists the file is followed.
read -r -d '' prompt <<PROMPT || true
Perform the cross-model review specified in ./${request}.

That file is the complete brief. It contains the review-round contract, the scope tests
that bound a blocking finding, the authoritative spec for this item, the standing
conventions, the branch diff, and the required output format. Follow it exactly.

Review only what it asks you to review. Do not broaden this into a general hardening
audit, and do not apply criteria the brief does not state -- the brief's own rules about
what makes a finding blocking are the ones that decide, not your priors about what good
code should defend against.

Begin your response by stating the exact commit hash you examined.
PROMPT

echo "==> running the reviewer (read-only sandbox, no session files persisted)"
codex exec \
  -C "$repo_root" \
  --sandbox read-only \
  --ephemeral \
  "${model[@]}" \
  -o "$response" \
  "$prompt"

echo
echo "==> round $round for $item_id"
echo "    request:  $request"
echo "    response: $response"
echo
echo "Before writing any fix code: diff the commit the response names against HEAD and"
echo "reproduce each finding by execution. A finding you cannot reproduce gets a rebuttal."
