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
    three times (M1-203, M1-401, M1-305). On a ``feat/<item>-*`` or ``fix/<item>-*``
    pull request the backlog row for ``<item>`` must already read ``Done``. Draft
    pull requests and non-item branches (``chore/``, ``docs/``, ``ci/``) are skipped.

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
# feat/m1-303-exa-fallback -> M1-303. Only feat/ and fix/ branches name an item;
# every other prefix is infrastructure work with no backlog row of its own.
BRANCH_PATTERN: Final = re.compile(r"^(?:feat|fix)/([a-z][0-9]*-[0-9]{3,4})(?:-|$)")


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


def _gate(rows: list[dict[str, str]]) -> int:
    branch = os.environ.get("BRANCH_NAME", "").strip()
    if not branch:
        print("FAIL: BRANCH_NAME is unset; the gate cannot tell which item to check.")
        return 1

    if os.environ.get("IS_DRAFT", "").strip().lower() == "true":
        _annotate("notice", f"backlog-status: {branch} is a draft PR — Done-flip check skipped.")
        return 0

    match = BRANCH_PATTERN.match(branch)
    if match is None:
        _annotate(
            "notice",
            f"backlog-status: {branch} does not name a backlog item "
            "(only feat/<item>-* and fix/<item>-* do) — Done-flip check skipped.",
        )
        return 0

    item_id = match.group(1).upper()
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("lint", "gate", "status"))
    parser.add_argument("item", nargs="?", help="item ID, for `status`")
    args = parser.parse_args(argv)

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
