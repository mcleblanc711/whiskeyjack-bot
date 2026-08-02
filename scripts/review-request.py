#!/usr/bin/env python3
"""Assemble a cross-model review request for one backlog item, on stdout.

The mechanical parts -- reviewer framing, project context, the item's spec pulled from
docs/backlog/backlog.csv, the referenced D## decisions, the standing conventions, the
diffstat and the branch diff -- are generated. The two sections that actually earn a
short review (deliberate choices, risk areas) are emitted as TODO placeholders,
because those are judgment and a template cannot fake them.

The request also carries the round's stopping condition, because a review with no
termination condition does not terminate: across M1-201 through M1-305, 47 of 121
non-merge commits were review-round commits, and the two items that closed cheapest
(M1-202, M1-401, two round commits each) differ from the ten-round ones only in how much
of the reasoning was written down before round 1. Round 1 is the broad implementation
review, round 2 verifies its remediation, and from round 3 the stopping rule is active.
Three tests disqualify an observation from blocking regardless of merit: it falls outside
the declared trust boundary, it applies unchanged to the diff base, or it was reproduced
against a commit that is not this request's HEAD.

Running it also runs the four toolchain gates and refuses to emit anything if one
fails, so the request cannot claim a green branch that is not green. It requires a clean
working tree first, because the gates run against the tree while the diff is built from
``HEAD``: with uncommitted work those are different code, and the request would report a
pass the reviewer cannot see. ``--no-verify`` skips both and says so in the output rather
than going quiet.

    scripts/review-request.py M1-303 > GPT_REVIEW_REQUEST_M1-303.md   # gitignored
    scripts/review-request.py M1-303 --round 2 --previous-reviewed 42a57ed \
      | xclip -selection clipboard
    scripts/review-request.py M1-303 --no-verify   # explicit "gates not run" banner

These files are scaffolding and are never committed (.gitignore: GPT_REVIEW_*). The
durable record of what shipped and why is docs/M1-NOTES.md and the per-epic notes.
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
BACKLOG_CSV: Final = REPO_ROOT / "docs" / "backlog" / "backlog.csv"
DECISIONS_CSV: Final = REPO_ROOT / "docs" / "backlog" / "decisions.csv"

DECISION_REFERENCE: Final = re.compile(r"\bD(\d{1,2})\b")

ROLE_PROMPT: Final = """\
You are a rigorous senior reviewer performing an independent cross-model review of
code authored by another AI model (Claude). Apply the stricter reading where the
authoritative spec is ambiguous, but keep that skepticism bounded by the review-round
contract below. Do not rubber-stamp, and do not turn an item review into an unbounded
hardening exercise."""

PROJECT_CONTEXT: Final = """\
`whiskeyjack-bot` is a public Metaculus MiniBench forecasting pipeline whose primary
product is an **attribution ledger**: an immutable, replayable SQLite record of every
forecast, its evidence, approvals, submission attempts, resolutions and scores.
Competing is the venue; attribution is the point. Python 3.11, `src/` layout,
offline-first (tests run with sockets disabled)."""

# The four gates, run for real before the request is emitted. This used to be a
# sentence in PROJECT_CONTEXT asserting they all passed -- a claim the script never
# checked, so running it on a red branch produced a review request that opened with a
# falsehood (cross-model review, round 1). A generator may not assert what it has not
# verified: either it runs them, or it says it did not.
GATES: Final = (
    ("pytest", ("uv", "run", "pytest", "-q")),
    ("ruff check", ("uv", "run", "ruff", "check", ".")),
    ("ruff format --check", ("uv", "run", "ruff", "format", "--check", ".")),
    ("mypy --strict src", ("uv", "run", "mypy", "--strict", "src")),
)

NOT_VERIFIED: Final = """\
> **NOT VERIFIED.** This request was generated with `--no-verify`; the toolchain gates
> were **not** run for it. Treat any claim about test or type status as unsubstantiated."""

# The gates run against the working tree; the diff below is built from committed HEAD.
# Those are the same code only while the tree is clean, so a dirty tree can report four
# passes for a fix the reviewer cannot see -- and an untracked test file changes what
# pytest collects without appearing in the diff at all (cross-model review, round 2).
DIRTY_TREE_NOTE: Final = """\
> The working tree also had **uncommitted changes** when this was generated, so the diff
> below is not necessarily the code that was run."""

STANDING_CONVENTIONS: Final = """\
- **Trust boundary.** *Trusted*: `config.yaml`, every filesystem path in it, the local
  filesystem, the operator's shell, and anything reachable only by monkeypatching module
  internals. *Untrusted*: provider JSON (AskNews, Exa), Metaculus API payloads, LLM output,
  any value read back out of the ledger, and config *values* that fail validation. A
  reproduction that requires a hostile local filesystem -- a FIFO or device at a configured
  path, a directory swapped in mid-read, a permission flipped between check and use -- does
  not clear the blocking bar; propose it as a backlog row instead. This is the same
  reasoning as the settled M1-401 path carve-out: an operator-supplied path is
  configuration, not content, and an operator who can plant a FIFO can edit the config.
- **Error hygiene.** Every module owns a sanitized exception (`ConfigError`,
  `SnapshotError`, `LedgerError`, `NormalizationError`, `ResearchError`). A message
  never echoes stored, file or field *values*, and sanitizing raises use `from None`
  so an underlying exception cannot reprint a value through its text or a rendered
  traceback. Pydantic's `ValidationError` interpolates the offending input, so it is
  always rebuilt with `errors(include_input=False, include_url=False)`. Filesystem
  paths are the one deliberate carve-out (settled M1-401 review, owner decision): a
  path is operator-supplied configuration, not content, and is the only thing that
  makes a load failure actionable. Every malformed shape must arrive as the module's
  own error type -- a raw `AttributeError`/`KeyError`/`ValueError` escaping is a
  finding.
- **Never print or persist secrets**; env-var *names* only in diagnostics.
- **Never persist hidden chain-of-thought**; concise auditable rationale fields only.
- **Append-only ledger**: forecast versions and lifecycle events are never mutated.
- **Approval binds to an exact forecast hash**; any content change invalidates it.
- **Community prediction is never a forecaster input in v1.**
- **No reachable submission path until M2**; `submission.enabled: false` and
  `dry_run: true` stay the committed defaults.
- **`forecasting-tools==0.2.92` is pinned**; do not float.
- **Ambiguity rule**: where an acceptance criterion is ambiguous, the stricter reading
  is implemented and noted.
- **Type dispatch**: `DiscreteQuestion` subclasses `NumericQuestion` in the pinned SDK,
  so dispatch is on the `question_type` literal and never `isinstance`.
- **Pure functions carry a hypothesis property pass** (`tests/property/`) asserting
  never-raises, total order where ordering is claimed, replay-stability across the
  persisted JSON form, and no value leak. Findings that a fuzzer should have caught are
  worth calling out as process failures, not just code ones."""

FIRST_REVIEW_POLICY: Final = """\
This is the **implementation review**. Inspect the full branch against the authoritative
spec, standing conventions and declared risk areas. A blocking finding must identify:

1. the exact acceptance criterion or standing convention the current code violates;
2. a reachable product path or public module boundary, using input or persisted state
   that the current contract accepts;
3. a deterministic reproduction against the reviewed commit and the wrong outcome; and
4. the smallest in-scope fix.

Missing one of those makes the observation non-blocking. Defensive hardening can still
be valuable, but value alone does not make it a release blocker."""

# Appended to every round's policy rather than written into each. These three are not
# judgment calls about severity -- they are mechanical facts about a finding, checkable
# before its merit is argued, and each is here because it cost real rounds: the trust
# boundary because a FIFO planted at a configured path was reported as a blocker; the
# diff-base test because M1-303's round 4 reported holes that were equally present in
# already-merged AskNews code (they became M1-309, after the branch paid 791 lines of
# churn); the staleness test because three separate rounds restated findings that were
# already closed on a newer tree.
DISQUALIFYING_TESTS: Final = """\

Three tests disqualify an observation from blocking regardless of its merit. Check them
before arguing severity:

- **Outside the trust boundary.** A reproduction that requires a hostile local filesystem,
  monkeypatched module internals, or any other input the trust boundary above lists as
  trusted is not a blocker here.
- **Already on the diff base.** If the same finding applies unchanged to code that is
  already merged, it is a pre-existing condition, not a defect this branch introduced.
  Propose the backlog row; do not withhold approval for it.
- **Stale.** State the commit hash you actually examined. If it is not this request's
  `HEAD`, say so and stop: the round is void and will be regenerated. Do not restate
  findings against an older tree.

Each of the three is a *backlog candidate*, not a dismissal. Say what you found."""

REMEDIATION_REVIEW_POLICY: Final = """\
This is the **remediation review**. Your primary job is to verify the preceding review's
blocking findings against the remediation delta. Use the full branch diff only for the
context needed to assess those fixes and for regressions introduced by them; do not
restart a blank-slate implementation audit or reopen a settled choice without a new
reproduction against the current commit.

A remaining or newly introduced blocker must identify the violated acceptance criterion
or standing convention, a reachable product path or public module boundary, a
deterministic reproduction against the current commit, the observed wrong outcome, and
the smallest in-scope fix. Otherwise classify it as a non-blocking follow-up."""

STOPPING_RULE_POLICY: Final = """\
This is a **post-remediation review; the stopping rule is active**. This is not another
blank-slate audit. Confirm the prior blockers and remediation first. Approval is required
when those blockers are closed unless the current commit still has a release-blocking
failure that satisfies every item below:

1. Quote the exact acceptance criterion or standing convention that is violated.
2. Name the reachable product path or public module boundary. Use input or persisted
   state the current contract accepts -- not arbitrary monkeypatching, a deliberately
   trusted injection boundary, a hypothetical external consumer, or a new requirement.
3. Give a deterministic reproduction against the current reviewed commit, including the
   concrete input/state, observed result and required result.
4. Explain the user, ledger-integrity, security or paid-call impact and why existing
   tests/gates do not already rule it out.
5. Give the smallest fix that stays inside this backlog item's scope.

If any item is missing, the observation is non-blocking. Put useful out-of-scope
hardening in a proposed backlog follow-up with a one-sentence acceptance criterion, and
do **not** withhold approval for it. A theoretically possible edge case is not by itself
a reason to continue the review loop."""


# The five headings M1-202 and M1-401 were written under -- the only two items to date that
# closed in two review-round commits instead of six to ten. Their notes say what a reviewer
# would otherwise have to infer from the diff, so round 1 had nothing left to discover. The
# headings are emitted rather than described because "write the deliberate choices" is advice
# and a named empty heading is a checklist: anything still carrying its TODO comment when the
# request is sent is a section the author skipped, visible to the reviewer and to the author.
DELIBERATE_CHOICES_TEMPLATE: Final = """\
### Decision — <the call>, and why
<!-- TODO(author): each non-obvious call, with its reason. Not what the code does: why this
     and not the obvious alternative. -->

### Deviation — where this departs from the SDK, the spec or a sibling module
<!-- TODO(author): the departure and what forced it. If there is none, write "none". -->

### Rejected — <the alternative>, and why not
<!-- TODO(author): the designs considered and dropped. A reviewer who cannot see that you
     already weighed an option will propose it as a finding. -->

### Deferred (do not read the absence as an omission)
<!-- TODO(author): boundaries deliberately not crossed, each naming the backlog item that
     owns it. An unclaimed gap gets reported; a claimed one does not. -->

### Standing risk — not verifiable offline
<!-- TODO(author): what the offline suite structurally cannot prove, and what you did
     instead (package version read, recorded fixture, property test). -->"""


def _review_policy(round_number: int) -> str:
    """Return the review contract for this round.

    One broad implementation review and one focused remediation review are enough to
    expose and then verify ordinary defects. From round three onward, another blocker
    must clear the explicit stopping-rule bar rather than merely identify more possible
    hardening.

    ``DISQUALIFYING_TESTS`` is appended to all three rather than written into each. Every
    round it was ever omitted from is a round where it was needed: a pre-existing condition
    or a stale reproduction is as reportable in the first review as in the fifth, and a rule
    the reviewer only sees from round 3 has already let rounds 1 and 2 spend on it.
    """
    if round_number == 1:
        policy = FIRST_REVIEW_POLICY
    elif round_number == 2:
        policy = REMEDIATION_REVIEW_POLICY
    else:
        policy = STOPPING_RULE_POLICY
    return policy + "\n" + DISQUALIFYING_TESTS


def _output_format(round_number: int) -> str:
    late_round_verdict = (
        " If the stopping-rule bar is not met, the verdict must be APPROVE."
        if round_number >= 3
        else ""
    )
    return f"""\
Reply with:
1. **Reviewed commit** — the exact hash you examined, first, before anything else. If it
   is not this request's `HEAD`, stop there and say so: the round is void.
2. **Verdict** — APPROVE, or CHANGES REQUESTED.{late_round_verdict}
3. **Prior findings** — for a remediation round, mark each prior blocker CLOSED or OPEN
   with evidence from the current commit; otherwise write "first review".
4. **Blocking findings** — each must include the violated acceptance criterion or
   standing convention, reachable path, deterministic reproduction against the reviewed
   commit, observed and required outcomes, impact, and minimal in-scope fix. No
   speculative hardening: if the required evidence is absent, it is not blocking. State
   for each that it is inside the trust boundary above and does not apply unchanged to
   the diff base.
5. **Non-blocking observations / backlog candidates** — clearly separated. For useful
   out-of-scope hardening, propose a backlog title and one-sentence acceptance criterion.
6. For each risk area listed above, one line on whether it is actually safe and why."""


def _run(*args: str) -> str:
    result = subprocess.run(
        args, cwd=REPO_ROOT, capture_output=True, text=True, check=False, encoding="utf-8"
    )
    if result.returncode != 0:
        message = result.stderr.strip() or f"{' '.join(args)} failed"
        raise SystemExit(f"FAIL: {message}")
    return result.stdout


def _verify_gates() -> str:
    """Run the four toolchain gates; return the section text, or exit if any fails.

    Refusing to emit is the point. A cross-model review request costs a round-trip and
    the reviewer's attention, and sending one for a branch whose tests are red spends
    both on a finding the author could have had in a minute. Failures go to stderr;
    stdout stays clean so a redirect never captures a half-written request.
    """
    lines: list[str] = []
    failures: list[str] = []

    for label, command in GATES:
        print(f"  running {label} ...", file=sys.stderr, flush=True)
        result = subprocess.run(
            command, cwd=REPO_ROOT, capture_output=True, text=True, check=False, encoding="utf-8"
        )
        if result.returncode == 0:
            lines.append(f"- `{label}` — **pass**")
        else:
            lines.append(f"- `{label}` — **FAIL**")
            failures.append(label)
            tail = (result.stdout + result.stderr).strip().splitlines()[-20:]
            print(f"\n--- {label} failed ---", file=sys.stderr)
            print("\n".join(tail), file=sys.stderr)

    if failures:
        raise SystemExit(
            f"\nFAIL: {', '.join(failures)} did not pass, so no review request was written.\n"
            "Fix the branch first. To send the request anyway, re-run with --no-verify; it "
            "will say plainly that the gates were not run."
        )

    return "\n".join(lines)


def _dirty_paths() -> list[str]:
    """Paths with uncommitted or untracked content. Paths only -- never their contents."""
    return [line.strip() for line in _run("git", "status", "--porcelain").splitlines() if line]


def _require_clean_tree() -> None:
    """Refuse to verify a tree whose state the diff will not show.

    Running the gates against uncommitted work and then diffing ``HEAD`` is how a request
    truthfully reports four passes while omitting the change that produced them. The
    generator cannot vouch for code the reviewer is not being shown, so it stops.
    """
    dirty = _dirty_paths()
    if not dirty:
        return

    shown = "\n".join(f"  {entry}" for entry in dirty[:10])
    if len(dirty) > 10:
        shown += f"\n  ... and {len(dirty) - 10} more"
    raise SystemExit(
        "FAIL: the working tree is not clean, so the gates would run against code that is "
        "not in the diff:\n"
        f"{shown}\n\n"
        "Commit or stash, then re-run. --no-verify emits the request anyway, with an "
        "explicit banner saying the gates were not run and the diff may be incomplete."
    )


def _not_verified_banner(dirty: bool) -> str:
    """The --no-verify banner, carrying the dirty-tree caveat when it applies.

    ``--no-verify`` stays the one way through, because it makes no claim to be true. What
    it must not do is stay silent about a second reason to distrust the request, so a
    dirty tree adds a line rather than being waved past.
    """
    return f"{NOT_VERIFIED}\n>\n{DIRTY_TREE_NOTE}" if dirty else NOT_VERIFIED


def _load(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _decision_blocks(reference: str, decisions: list[dict[str, str]]) -> list[str]:
    by_id = {row["Decision ID"]: row for row in decisions}
    blocks: list[str] = []
    for match in DECISION_REFERENCE.finditer(reference):
        key = f"D{int(match.group(1)):02d}"
        row = by_id.get(key)
        if row is None:
            continue
        blocks.append(
            f"**{key}** ({row['Status']}) — {row['Decision']}\n> Rationale: {row['Rationale']}"
        )
    return blocks


def _positive_round(value: str) -> int:
    """Argparse type for a one-based review round."""
    try:
        round_number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("review round must be a positive integer") from exc
    if round_number < 1:
        raise argparse.ArgumentTypeError("review round must be at least 1")
    return round_number


def _reviewed_revision(round_number: int, requested: str | None) -> str | None:
    """Resolve and validate the commit reviewed before a remediation round.

    Naming the prior commit prevents the stale-review failure mode: without it, a pasted
    response can critique an older tree while the author unknowingly applies the same fix
    again. It also gives the next reviewer a mechanically generated remediation delta
    instead of inviting another full audit from scratch.
    """
    if round_number == 1:
        if requested is not None:
            raise SystemExit("FAIL: --previous-reviewed is only valid with --round 2 or later.")
        return None
    if requested is None:
        raise SystemExit(
            "FAIL: --round 2 or later requires --previous-reviewed <commit>. "
            "Use the exact commit named in the preceding review."
        )

    try:
        reviewed = _run("git", "rev-parse", "--verify", f"{requested}^{{commit}}").strip()
    except SystemExit:
        raise SystemExit(
            f"FAIL: --previous-reviewed {requested!r} does not resolve to a commit."
        ) from None
    head = _run("git", "rev-parse", "HEAD").strip()
    if reviewed == head:
        raise SystemExit(
            "FAIL: --previous-reviewed resolves to HEAD, so there is no remediation "
            "delta to review."
        )
    try:
        _run("git", "merge-base", "--is-ancestor", reviewed, "HEAD")
    except SystemExit:
        raise SystemExit(
            "FAIL: --previous-reviewed must be an ancestor of HEAD; the named review "
            "does not describe this branch's remediation history."
        ) from None
    return reviewed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("item", help="backlog item ID, e.g. M1-303")
    parser.add_argument(
        "--base", default="origin/master", help="diff base (default: origin/master)"
    )
    parser.add_argument(
        "--round", type=_positive_round, default=1, help="review round (default: 1)"
    )
    parser.add_argument(
        "--previous-reviewed",
        metavar="COMMIT",
        help="exact commit named by the preceding review (required from round 2 onward)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the four toolchain gates; the request then says so explicitly",
    )
    args = parser.parse_args(argv)

    item_id = args.item.strip().upper()
    rows = _load(BACKLOG_CSV)
    row = next((candidate for candidate in rows if candidate["ID"] == item_id), None)
    if row is None:
        raise SystemExit(
            f"FAIL: {item_id} has no row in docs/backlog/backlog.csv. "
            "Add the row before requesting a review."
        )

    reviewed_revision = _reviewed_revision(args.round, args.previous_reviewed)

    # Before anything reaches stdout: a failed gate must leave no partial request
    # behind for a shell redirect to capture.
    if args.no_verify:
        gate_status = _not_verified_banner(bool(_dirty_paths()))
    else:
        _require_clean_tree()
        gate_status = _verify_gates()

    branch = _run("git", "rev-parse", "--abbrev-ref", "HEAD").strip()
    diffstat = _run("git", "diff", "--stat", f"{args.base}...HEAD").rstrip()
    names = _run("git", "diff", "--name-only", f"{args.base}...HEAD").split()
    diff = _run("git", "diff", f"{args.base}...HEAD")
    if not diff.strip():
        print(
            f"WARNING: {args.base}...HEAD is empty — is {branch} pushed and based on {args.base}?",
            file=sys.stderr,
        )

    heading = f"# Cross-model review request — whiskeyjack-bot {item_id}"
    if args.round > 1:
        heading += f" (round {args.round})"

    remediation: list[str] = []
    remediation_diff = ""
    if reviewed_revision is not None:
        remediation_diffstat = _run("git", "diff", "--stat", f"{reviewed_revision}..HEAD").rstrip()
        remediation_diff = _run("git", "diff", f"{reviewed_revision}..HEAD")
        remediation = [
            "## Previous review and remediation delta",
            "",
            f"The preceding review examined commit `{reviewed_revision}`. Review the current",
            "`HEAD` against that exact baseline before considering any new finding.",
            "",
            "<!-- TODO(author): list every preceding blocker and its disposition: fixed,",
            "     disputed with evidence, or intentionally moved to a named backlog item. -->",
            "",
            "```",
            remediation_diffstat or "(no committed remediation delta)",
            "```",
            "",
        ]

    out: list[str] = [
        heading,
        "",
        ROLE_PROMPT,
        "",
        "## Review-round contract",
        "",
        _review_policy(args.round),
        "",
        "## Project context",
        "",
        PROJECT_CONTEXT,
        "",
        f"This is **{item_id}** ({row['Epic']}) on branch `{branch}`, diffed against "
        f"`{args.base}`.",
        "",
        "## Toolchain gate status",
        "",
        gate_status,
        "",
        "## Authoritative spec",
        "",
        f"From `docs/backlog/backlog.csv` (the {item_id} row):",
        "",
        f"> **{row['Task']}.** {row['Description']}",
        f'> **Acceptance:** "{row["Acceptance Criteria"]}"',
        f"> Depends on {row['Dependency'] or 'None'}. "
        f"Reference: {row['Source or Decision Reference']}",
        "",
    ]

    blocks = _decision_blocks(row["Source or Decision Reference"], _load(DECISIONS_CSV))
    if blocks:
        out += ["Decisions this item is bound by:", ""]
        for block in blocks:
            out += [block, ""]

    out += remediation

    out += [
        "## Standing conventions this branch must honor",
        "",
        STANDING_CONVENTIONS,
        "",
        "## Deliberate choices / out of scope "
        "(challenge the rationale, but these are not omissions)",
        "",
        DELIBERATE_CHOICES_TEMPLATE,
        "",
        "## Risk areas to pressure-test",
        "",
        "<!-- TODO(author): where you believe this is most likely to be wrong, stated as",
        "     claims a reviewer can falsify from the diff, plus how you verified each one",
        "     (package version read, fixture, property test). -->",
        "",
        "## Property tests run locally before this request",
        "",
        "<!-- TODO(author): for each pure function in the diff, the invariants asserted in",
        "     tests/property/ and the example count. If there is no pure function in the",
        "     diff, say so. -->",
        "",
        "## What changed",
        "",
        "```",
        diffstat,
        "```",
        "",
        f"{len(names)} file(s) changed. Source: "
        f"{sum(1 for name in names if name.startswith('src/'))}; tests: "
        f"{sum(1 for name in names if name.startswith('tests/'))}; docs and config: "
        f"{sum(1 for name in names if not name.startswith(('src/', 'tests/')))}.",
        "",
        "## Output format",
        "",
        _output_format(args.round),
        "",
    ]

    if reviewed_revision is not None:
        out += [
            f"# Remediation diff (`git diff {reviewed_revision}..HEAD`)",
            "",
            remediation_diff.rstrip("\n"),
            "",
        ]

    out += [
        f"# Full branch diff for context only (`git diff {args.base}...HEAD`)",
        "",
        diff.rstrip("\n"),
        "",
    ]

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
