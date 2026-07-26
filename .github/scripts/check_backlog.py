#!/usr/bin/env python3
"""Backlog lint and the Done-flip merge gate.

Two subcommands, both driven by ``docs/backlog/backlog.csv`` (the single source of
truth for backlog state since the workbook stopped being tracked):

``lint``
    Structural checks that must hold on every branch and on master: one row per
    unique ID, closed vocabularies for Status/Priority/Owner/Complexity, no empty
    required cells, and every dependency naming a real row.

``gate``
    The check that exists because the Status flip to ``Done`` was forgotten at merge
    three times (M1-203, M1-401, M1-305). On an item branch the backlog row for
    ``<item>`` must already read ``Done``. Draft pull requests and branches whose
    prefix is on the infrastructure skip list are skipped; **every other branch name
    fails**, because the first version of this gate skipped anything its pattern did
    not recognize and a cross-model review found two live false-greens in it
    (``feat/M1-303-x`` -- upper case -- and ``feature/m1-303-x``). A gate against
    silently forgetting a step must not itself silently skip.

Standard library only: this runs before ``uv sync`` in CI, and the acceptance column
carries commas inside quotes, so a shell one-liner cannot parse it safely.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
BACKLOG_CSV: Final = REPO_ROOT / "docs" / "backlog" / "backlog.csv"

EXPECTED_HEADER: Final = [
    "ID",
    "Epic",
    "Task",
    "Description",
    "Dependency",
    "Priority",
    "Suggested Owner",
    "Acceptance Criteria",
    "Estimated Complexity",
    "Status",
    "Source or Decision Reference",
]

# Workflow vocabulary (CLAUDE.md "Backlog status"): Not Started -> In Review (PR
# open) -> Done (at merge). Blocked is for owner-gated items.
VALID_STATUSES: Final = frozenset({"Not Started", "In Review", "Done", "Blocked"})
VALID_PRIORITIES: Final = frozenset({"Critical", "High", "Medium", "Low"})
VALID_COMPLEXITIES: Final = frozenset({"S", "M", "L"})
VALID_OWNERS: Final = frozenset({"Claude Code", "Codex", "Chris", "Chris + Codex"})

REQUIRED_CELLS: Final = ("ID", "Epic", "Task", "Description", "Acceptance Criteria")

ID_PATTERN: Final = re.compile(r"[A-Z][0-9]*-[0-9]{3,4}")

# feat/m1-303-exa-fallback -> M1-303. Matched case-insensitively and across the
# aliases people actually type: the original pattern was anchored to lower case and
# to feat|fix alone, so `feat/M1-303-x` and `feature/m1-303-x` fell through to the
# "not an item branch" arm and reported success on a Not Started row.
BRANCH_PATTERN: Final = re.compile(r"^([A-Za-z]+)/([A-Za-z][0-9]*-[0-9]{3,4})(?:-|$)")

# Prefixes that name a backlog item. Anything matching one of these *and* the item
# shape is gated.
ITEM_PREFIXES: Final = frozenset({"feat", "feature", "fix", "bugfix", "hotfix"})

# Infrastructure work that legitimately has no backlog row. This list is exhaustive
# by design -- an unrecognized prefix is a failure, not a skip, so adding a new kind
# of branch is a deliberate edit here rather than an accident nobody notices.
SKIP_PREFIXES: Final = frozenset(
    {
        "chore",
        "ci",
        "build",
        "deps",
        "dependabot",
        "docs",
        "refactor",
        "release",
        "revert",
        "test",
    }
)

_RENAME_HINT: Final = (
    "Rename the branch to <prefix>/<item>-<slug> (prefixes: "
    + ", ".join(sorted(ITEM_PREFIXES))
    + ") if it implements a backlog item, or use one of the infrastructure prefixes ("
    + ", ".join(sorted(SKIP_PREFIXES))
    + "). If this is a new kind of branch that genuinely owns no backlog row, add its "
    "prefix to SKIP_PREFIXES in .github/scripts/check_backlog.py in the same PR."
)


def _annotate(level: str, message: str) -> None:
    """Print a message, as a GitHub annotation when running under Actions."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{message}")
    else:
        print(f"{level}: {message}")


def _read_rows() -> list[dict[str, str]]:
    with BACKLOG_CSV.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            print(f"FAIL: {BACKLOG_CSV} is empty.", file=sys.stderr)
            raise SystemExit(1) from None
        if header != EXPECTED_HEADER:
            print(
                "FAIL: backlog.csv header does not match the expected columns.\n"
                f"  expected: {EXPECTED_HEADER}\n  found:    {header}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        rows: list[dict[str, str]] = []
        for line_number, record in enumerate(reader, start=2):
            if not record:
                continue
            if len(record) != len(EXPECTED_HEADER):
                print(
                    f"FAIL: backlog.csv line {line_number} has {len(record)} fields, "
                    f"expected {len(EXPECTED_HEADER)}.",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            row = dict(zip(EXPECTED_HEADER, record, strict=True))
            row["__line__"] = str(line_number)
            rows.append(row)
    return rows


def _lint(rows: list[dict[str, str]]) -> list[str]:
    problems: list[str] = []
    seen: dict[str, str] = {}

    for row in rows:
        item_id = row["ID"]
        line = row["__line__"]
        where = f"line {line} ({item_id or 'no ID'})"

        if not ID_PATTERN.fullmatch(item_id):
            problems.append(f"{where}: ID does not match the M0-001 / T-901 / A-1101 shape.")
        if item_id in seen:
            problems.append(f"{where}: duplicate ID, first seen on line {seen[item_id]}.")
        else:
            seen[item_id] = line

        for column in REQUIRED_CELLS:
            if not row[column].strip():
                problems.append(f"{where}: {column} is empty.")

        if row["Status"] not in VALID_STATUSES:
            problems.append(
                f"{where}: Status {row['Status']!r} is not one of {sorted(VALID_STATUSES)}."
            )
        if row["Priority"] not in VALID_PRIORITIES:
            problems.append(
                f"{where}: Priority {row['Priority']!r} is not one of {sorted(VALID_PRIORITIES)}."
            )
        if row["Estimated Complexity"] not in VALID_COMPLEXITIES:
            problems.append(
                f"{where}: Estimated Complexity {row['Estimated Complexity']!r} is not one of "
                f"{sorted(VALID_COMPLEXITIES)}."
            )
        if row["Suggested Owner"] not in VALID_OWNERS:
            problems.append(
                f"{where}: Suggested Owner {row['Suggested Owner']!r} is not one of "
                f"{sorted(VALID_OWNERS)} (the owner split is a project contract, not free text)."
            )

    known = set(seen)
    for row in rows:
        where = f"line {row['__line__']} ({row['ID']})"
        raw = row["Dependency"].strip()
        if not raw or raw == "None":
            continue
        for dependency in (part.strip() for part in raw.split(";")):
            if dependency and dependency not in known:
                problems.append(f"{where}: Dependency {dependency!r} has no backlog row.")

    return problems


def _classify_branch(branch: str) -> tuple[str, str]:
    """Sort a branch name into ``item`` / ``skip`` / ``unknown``.

    Pure and separate from ``_gate`` so the branch-name table can be tested directly;
    the classification used to be an inline ``re.match`` inside the gate, which is
    exactly why its two false-greens went unnoticed until a cross-model review.

    Returns the disposition and its subject: the upper-case item ID for ``item``, the
    lower-case prefix for ``skip``, and the branch itself for ``unknown``.

    An item-shaped branch under an unlisted prefix is ``unknown`` (fails), and so is a
    listed item prefix carrying no item ID (``feat/tidy-imports``) -- a branch claiming
    to implement a feature but naming no backlog row is the case the gate exists for.
    """
    prefix, separator, _ = branch.partition("/")
    if not separator:
        # No prefix at all: nothing to classify against, so it fails closed rather
        # than matching a bare branch named `chore` against the skip list.
        return "unknown", branch

    match = BRANCH_PATTERN.match(branch)
    if match is not None and prefix.lower() in ITEM_PREFIXES:
        return "item", match.group(2).upper()
    if prefix.lower() in SKIP_PREFIXES:
        return "skip", prefix.lower()
    return "unknown", branch


def _gate(rows: list[dict[str, str]]) -> int:
    branch = os.environ.get("BRANCH_NAME", "").strip()
    if not branch:
        print("FAIL: BRANCH_NAME is unset; the gate cannot tell which item to check.")
        return 1

    if os.environ.get("IS_DRAFT", "").strip().lower() == "true":
        _annotate("notice", f"backlog-status: {branch} is a draft PR — Done-flip check skipped.")
        return 0

    disposition, subject = _classify_branch(branch)

    if disposition == "skip":
        _annotate(
            "notice",
            f"backlog-status: {branch} is {subject}/ infrastructure work with no backlog "
            "row of its own — Done-flip check skipped.",
        )
        return 0

    if disposition == "unknown":
        _annotate(
            "error",
            f"backlog-status: {branch} is neither a recognized item branch nor recognized "
            f"infrastructure work, so the gate cannot tell whether a Done-flip is owed. "
            f"{_RENAME_HINT}",
        )
        return 1

    item_id = subject
    row = next((candidate for candidate in rows if candidate["ID"] == item_id), None)

    if row is None:
        _annotate(
            "error",
            f"backlog-status: branch {branch} names {item_id}, which has no row in "
            "docs/backlog/backlog.csv. Add the row (with its acceptance criteria) before "
            "opening the PR — the backlog is the record of scope, so an item without a row "
            "is an item nobody agreed to.",
        )
        return 1

    status = row["Status"]
    if status != "Done":
        _annotate(
            "error",
            f"backlog-status: {item_id} is still {status!r} in docs/backlog/backlog.csv. "
            "Flip it to 'Done' on this branch before merging — Done is set at merge, and "
            "forgetting it is the most-repeated process defect in this project "
            "(M1-203, M1-401, M1-305).",
        )
        return 1

    print(f"backlog-status: {item_id} is 'Done'. OK.")
    return 0


def _status(rows: list[dict[str, str]], item_id: str | None) -> int:
    """Print one item's Status. The shell scripts use this instead of cutting the CSV:
    quoted commas in the acceptance column make field N unreliable."""
    if not item_id:
        print("FAIL: `status` needs an item ID.", file=sys.stderr)
        return 1
    row = next((candidate for candidate in rows if candidate["ID"] == item_id.upper()), None)
    if row is None:
        print(f"FAIL: {item_id.upper()} has no backlog row.", file=sys.stderr)
        return 1
    print(row["Status"])
    return 0


def _classify(branch: str | None) -> int:
    """Expose ``_classify_branch`` to the shell, so the contract has one home.

    ``finish-item.sh`` used to re-implement branch discovery as two globs
    (``feat/<id>-*``, ``fix/<id>-*``). That silently disagreed with this file on three
    axes at once -- three prefixes, every upper-case spelling, and the no-slug form -- so
    seven of the eleven branch shapes the gate accepts could be merged and then never
    cleaned up (cross-model review, round 2). A second implementation of a contract is a
    second thing to keep in step, so there is no second implementation.

    With a branch: prints ``<disposition>\\t<subject>``, exit 1 for ``unknown``. Without
    one: reads branch names from stdin and prints ``<disposition>\\t<subject>\\t<branch>``
    per line, so classifying a whole repo costs one process. The batch form exits 0 as
    long as it read its input -- ``unknown`` branches are a normal part of any repo, and
    the caller filters on the disposition column.
    """
    if branch is not None:
        disposition, subject = _classify_branch(branch.strip())
        print(f"{disposition}\t{subject}")
        return 1 if disposition == "unknown" else 0

    for line in sys.stdin:
        candidate = line.strip()
        if not candidate:
            continue
        disposition, subject = _classify_branch(candidate)
        print(f"{disposition}\t{subject}\t{candidate}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("lint", "gate", "status", "classify"))
    parser.add_argument(
        "item",
        nargs="?",
        help="item ID for `status`; branch name for `classify` (else read from stdin)",
    )
    args = parser.parse_args(argv)

    # Before _read_rows(): classification is a pure function of the branch name, and
    # making it depend on the CSV would couple `finish-item.sh` to a file it does not
    # need -- including in the temp repositories its tests build.
    if args.command == "classify":
        return _classify(args.item)

    rows = _read_rows()

    if args.command == "status":
        return _status(rows, args.item)

    if args.command == "lint":
        problems = _lint(rows)
        if problems:
            for problem in problems:
                _annotate("error", f"backlog lint: {problem}")
            print(f"FAIL: {len(problems)} backlog problem(s).", file=sys.stderr)
            return 1
        print(f"Backlog lint passed ({len(rows)} rows).")
        return 0

    return _gate(rows)


if __name__ == "__main__":
    raise SystemExit(main())
