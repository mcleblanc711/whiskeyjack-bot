#!/usr/bin/env python3
"""Assemble a cross-model review request for one backlog item, on stdout.

The mechanical parts -- reviewer framing, project context, the item's spec pulled from
docs/backlog/backlog.csv, the referenced D## decisions, the standing conventions, the
diffstat and the branch diff -- are generated. The two sections that actually earn a
short review (deliberate choices, risk areas) are emitted as TODO placeholders,
because those are judgment and a template cannot fake them.

    scripts/review-request.py M1-303 > GPT_REVIEW_REQUEST_M1-303.md   # gitignored
    scripts/review-request.py M1-303 --round 2 | xclip -selection clipboard

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
code authored by another AI model (Claude). Apply the **stricter reading**: when a
line could be read as either correct or subtly wrong, assume the wrong reading and
prove it can't happen from the diff. Do **not** rubber-stamp. If you approve, justify
why each risk area below is actually safe; if you don't, list blocking findings."""

PROJECT_CONTEXT: Final = """\
`whiskeyjack-bot` is a public Metaculus MiniBench forecasting pipeline whose primary
product is an **attribution ledger**: an immutable, replayable SQLite record of every
forecast, its evidence, approvals, submission attempts, resolutions and scores.
Competing is the venue; attribution is the point. Python 3.11, `src/` layout,
offline-first (tests run with sockets disabled). The toolchain gates are `pytest`,
`ruff check`, `ruff format --check` and `mypy --strict src`, all of which pass on this
branch."""

STANDING_CONVENTIONS: Final = """\
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

OUTPUT_FORMAT: Final = """\
Reply with:
1. **Verdict** — APPROVE, or CHANGES REQUESTED.
2. **Blocking findings** — each with the file, the concrete failure scenario (inputs or
   state that produce the wrong result), and the minimal fix. No speculative hardening:
   if you cannot state inputs that break it, it is not blocking.
3. **Non-blocking observations** — clearly separated from the above.
4. For each risk area listed above, one line on whether it is actually safe and why."""


def _run(*args: str) -> str:
    result = subprocess.run(
        args, cwd=REPO_ROOT, capture_output=True, text=True, check=False, encoding="utf-8"
    )
    if result.returncode != 0:
        message = result.stderr.strip() or f"{' '.join(args)} failed"
        raise SystemExit(f"FAIL: {message}")
    return result.stdout


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("item", help="backlog item ID, e.g. M1-303")
    parser.add_argument(
        "--base", default="origin/master", help="diff base (default: origin/master)"
    )
    parser.add_argument("--round", type=int, default=1, help="review round (default: 1)")
    args = parser.parse_args(argv)

    item_id = args.item.strip().upper()
    rows = _load(BACKLOG_CSV)
    row = next((candidate for candidate in rows if candidate["ID"] == item_id), None)
    if row is None:
        raise SystemExit(
            f"FAIL: {item_id} has no row in docs/backlog/backlog.csv. "
            "Add the row before requesting a review."
        )

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

    out: list[str] = [
        heading,
        "",
        ROLE_PROMPT,
        "",
        "## Project context",
        "",
        PROJECT_CONTEXT,
        "",
        f"This is **{item_id}** ({row['Epic']}) on branch `{branch}`, diffed against "
        f"`{args.base}`.",
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

    out += [
        "## Standing conventions this branch must honor",
        "",
        STANDING_CONVENTIONS,
        "",
        "## Deliberate choices / out of scope "
        "(challenge the rationale, but these are not omissions)",
        "",
        "<!-- TODO(author): every non-obvious call, with the reason. This section is what",
        "     turns a six-round review into a one-round review: say what you rejected and",
        "     why, and name the boundaries you deliberately did not cross (which later",
        "     backlog item owns them). Invite challenge explicitly. -->",
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
        OUTPUT_FORMAT,
        "",
        f"# Full branch diff (`git diff {args.base}...HEAD`)",
        "",
        diff.rstrip("\n"),
        "",
    ]

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
