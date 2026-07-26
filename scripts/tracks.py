#!/usr/bin/env python3
"""Read the track claims registry (docs/TRACKS.md) across every active branch.

A claim lives on its own branch until that branch merges. Reading the registry from
``origin/master`` alone therefore cannot see a claim for the entire period the claim
exists to cover -- ``scripts/start-item.sh`` did exactly that, so a track that had
already pushed ``Adds deps? = yes`` was invisible to the next ``--deps`` start, and the
two met at a ``uv.lock`` merge conflict (cross-model review, round 2).

So the scan is over refs, not one file: every ``refs/remotes/origin/*`` plus master.
That over-collects, because a row outlives its branch -- ``finish-item.sh`` tells you to
drop the row on your *next* branch, so master carries stale rows by design. A claim is
therefore kept only if its branch is still live: the remote ref exists and is not yet an
ancestor of ``origin/master``. Deleted or merged branch, dead claim.

The direction of the remaining uncertainty is deliberate. A blank Branch cell is live. A
nonblank branch that does not exist is stale only when the exact row already landed on
master; otherwise it is an invalid live claim. A structurally invalid registry also fails
the dependency check. This thing exists to stop a collision, and "I could not tell" is
not evidence that the slot is free.

    scripts/tracks.py claims    # every live claim, one per line
    scripts/tracks.py deps      # exit 1 if the dependency slot is held

Stdlib only, like .github/scripts/check_backlog.py, and loaded by path in tests.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Final, NamedTuple

TRACKS_PATH: Final = "docs/TRACKS.md"
WORKTREES_HEADING: Final = "## Worktrees"

ITEM_COLUMN: Final = "Item"
BRANCH_COLUMN: Final = "Branch"
DEPS_COLUMN: Final = "Adds deps?"
STARTED_COLUMN: Final = "Started"
REQUIRED_COLUMNS: Final = frozenset(
    {ITEM_COLUMN, BRANCH_COLUMN, "Worktree", DEPS_COLUMN, "Migration", STARTED_COLUMN}
)

# Cells that positively mean "no claim". Everything else in the deps column counts as a
# claim, including `Yes (uv.lock)` and anything unrecognized -- the previous detector was
# `grep -qi '| *yes *|'`, which missed every spelling but the bare one and matched a
# `| yes |` anywhere else in the file, including the Standing claims table.
_NEGATIVE_CELLS: Final = frozenset({"", "no", "none", "-", "--", "n/a", "na"})

# The placeholder row in an empty table: `| _(none)_ | | | | | |`.
_PLACEHOLDER_CELLS: Final = frozenset({"", "_(none)_", "(none)", "none"})


class ParseResult(NamedTuple):
    """Claims plus structural errors from one registry."""

    claims: list[dict[str, str]]
    problems: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.problems


class ClaimsResult(NamedTuple):
    """Resolved live claims plus branch references that could not be classified."""

    claims: list[dict[str, str]]
    problems: tuple[str, ...]


class ScanResult(NamedTuple):
    """The complete cross-ref registry snapshot.

    ``notes`` are conditions worth stating that are not failures -- currently only the
    bootstrap case, where the registry exists on a branch but has not reached master yet.
    """

    rows_by_ref: dict[str, list[dict[str, str]]]
    existing: frozenset[str]
    merged: frozenset[str]
    readable: bool
    problems: tuple[str, ...]
    notes: tuple[str, ...] = ()


class RefsResult(NamedTuple):
    """Remote refs plus a diagnostic when enumeration failed."""

    refs: frozenset[str]
    problem: str | None


def _normalize(cell: str) -> str:
    """Strip markdown emphasis and case so cell tests compare like with like."""
    return cell.strip().strip("*_`").strip().lower()


def _split_row(line: str) -> list[str]:
    """Split one markdown table row into its cells.

    Leading and trailing pipes are delimiters, not cells, so they are dropped before the
    split rather than after -- otherwise every row gains two empty cells and the header
    zip is off by one.
    """
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(set(cell) <= {"-", ":", " "} and "-" in cell for cell in cells)


def parse_claims(text: str) -> ParseResult:
    """Parse the Worktrees table of a TRACKS.md into one dict per claim row.

    Scoped to the ``## Worktrees`` section: the Standing claims table above it holds
    prose about dependency serialization, and matching that as a claim is a false
    positive that blocks every dependency-adding track forever.

    Structural errors are data, not exceptions, so callers can name the broken ref and
    fail closed. Returning an empty claim list alone is ambiguous: it could mean a valid
    empty table, or that a typo changed ``## Worktrees`` to ``## Worktree`` and hid a
    live dependency claim.
    """
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == WORKTREES_HEADING),
        None,
    )
    if start is None:
        return ParseResult([], (f"missing {WORKTREES_HEADING!r} section",))

    header: list[str] | None = None
    separator_seen = False
    claims: list[dict[str, str]] = []
    problems: list[str] = []

    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("#"):
            break  # next section; the table is over
        if not stripped.startswith("|"):
            if header is not None:
                break  # blank line or prose after the table body
            continue

        cells = _split_row(stripped)
        if header is None:
            header = cells
            missing = REQUIRED_COLUMNS - set(header)
            if missing:
                problems.append(f"Worktrees table is missing columns: {', '.join(sorted(missing))}")
            if len(set(header)) != len(header):
                problems.append("Worktrees table has duplicate column names")
            continue
        if not separator_seen:
            if not _is_separator(cells) or len(cells) != len(header):
                problems.append("Worktrees table header has no matching separator row")
                break
            separator_seen = True
            continue
        if all(_normalize(cell) in _PLACEHOLDER_CELLS for cell in cells):
            if len(cells) != len(header):
                problems.append("Worktrees placeholder row does not match the table width")
            continue

        if len(cells) != len(header):
            problems.append(
                f"Worktrees claim row has {len(cells)} cells; expected {len(header)}: {stripped}"
            )
            continue
        claims.append(dict(zip(header, cells, strict=True)))

    if header is None:
        problems.append("Worktrees section has no table")
    elif not separator_seen and not any("separator row" in problem for problem in problems):
        problems.append("Worktrees table has no separator row")

    return ParseResult(claims, tuple(problems))


def is_dep_claim(row: dict[str, str]) -> bool:
    """Whether a claim row takes the one dependency-adding slot.

    Fails closed: only an explicit negative frees the slot. An unrecognized cell -- a
    typo, a note, a spelling nobody anticipated -- counts as a claim, because the cost of
    a false positive is a question and the cost of a false negative is a `uv.lock`
    conflict that no merge tool resolves usefully.

    A blank cell in a well-formed row is a stated "no". A row with no deps column at all
    is a truncated row, which states nothing, so it counts as a claim -- reading those two
    as the same thing is how a malformed registry would quietly free the slot.
    """
    if DEPS_COLUMN not in row:
        return True
    return _normalize(row[DEPS_COLUMN]) not in _NEGATIVE_CELLS


def is_live(row: dict[str, str], merged: frozenset[str]) -> bool:
    """Whether a claim row's branch has yet to land on master.

    A row naming a branch that already merged is a leftover -- ``finish-item.sh``
    deliberately leaves those behind for the next branch to sweep -- and must not block
    anyone. A row with no readable branch, or one naming an unknown branch, is live here:
    neither can be proven stale by merge status alone.

    Existence is deliberately *not* checked here. Deciding what an unknown branch means
    needs cross-ref provenance -- a row unique to an unmerged ref is an invalid claim,
    while the same row already on master is a landed one -- and only ``live_claims`` has
    that. Taking ``existing`` as an argument it never read made this look like the whole
    liveness test when it is one half of it.
    """
    branch = row.get(BRANCH_COLUMN, "").strip().strip("`").strip()
    if not branch:
        return True
    ref = branch if branch.startswith("origin/") else f"origin/{branch}"
    return ref not in merged


def live_claims(
    rows_by_ref: dict[str, list[dict[str, str]]],
    existing: frozenset[str],
    merged: frozenset[str],
) -> ClaimsResult:
    """Every live claim across the scanned refs, deduplicated by (item, branch).

    The same row appears on every branch that has merged master since it was written, so
    without the dedup a single claim is reported once per active track. An unknown branch
    is stale only when the exact row has landed on master; if it exists solely on an
    unmerged ref, treating a misspelling as a deleted branch would silently free a claim.
    """
    seen: set[tuple[str, str]] = set()
    live: list[dict[str, str]] = []

    problems: list[str] = []
    master_rows = {tuple(sorted(row.items())) for row in rows_by_ref.get("origin/master", [])}
    for source_ref, rows in sorted(rows_by_ref.items()):
        for row in rows:
            branch = row.get(BRANCH_COLUMN, "").strip().strip("`").strip()
            branch_ref = branch if branch.startswith("origin/") else f"origin/{branch}"
            if branch and branch_ref not in existing:
                signature = tuple(sorted(row.items()))
                if signature in master_rows:
                    continue
                problems.append(
                    f"{source_ref}: claim for {row.get(ITEM_COLUMN, '?')!r} names "
                    f"{branch!r}, which does not exist on origin and has not landed on master"
                )
                continue
            if not is_live(row, merged):
                continue
            identity = (row.get(ITEM_COLUMN, "").strip(), row.get(BRANCH_COLUMN, "").strip())
            if identity in seen:
                continue
            seen.add(identity)
            live.append(row)

    return ClaimsResult(live, tuple(dict.fromkeys(problems)))


def _git(*args: str) -> tuple[int, str]:
    result = subprocess.run(
        ("git", *args), capture_output=True, text=True, check=False, encoding="utf-8"
    )
    return result.returncode, result.stdout


def _remote_refs(merged_only: bool) -> RefsResult:
    args = ["for-each-ref", "--format=%(refname:short)"]
    if merged_only:
        args += ["--merged", "origin/master"]
    args.append("refs/remotes/origin")
    code, out = _git(*args)
    if code != 0:
        kind = "merged" if merged_only else "existing"
        return RefsResult(frozenset(), f"could not enumerate {kind} origin refs")
    # origin/HEAD is a symbolic alias for master, not a branch anyone claims on.
    return RefsResult(frozenset(ref for ref in out.split() if ref != "origin/HEAD"), None)


def _master_lacks_registry() -> bool:
    """Whether ``origin/master`` resolves but simply has no registry file on it.

    The bootstrap state: the branch that introduces ``docs/TRACKS.md`` has not merged, so
    the file exists on a ref and not on master. That is not a broken registry, and
    treating it as one made this tool -- and every ``start-item.sh`` run through it --
    fail on the very branch that adds it, while the tests passed because their throwaway
    repositories always seed the file onto master first.

    An unresolvable ``origin/master`` is a different thing and stays a hard problem.
    """
    code, _ = _git("rev-parse", "--verify", "--quiet", "origin/master")
    return code == 0


def _scan() -> ScanResult:
    """Read and validate TRACKS.md from master and every unmerged origin ref."""
    existing_result = _remote_refs(merged_only=False)
    merged_result = _remote_refs(merged_only=True)
    existing = existing_result.refs
    merged = merged_result.refs

    rows_by_ref: dict[str, list[dict[str, str]]] = {}
    readable = False
    problems: list[str] = []
    notes: list[str] = []
    problems.extend(
        problem
        for problem in (existing_result.problem, merged_result.problem)
        if problem is not None
    )
    refs_to_scan = (existing - merged) | {"origin/master"}
    for ref in sorted(refs_to_scan):
        code, text = _git("show", f"{ref}:{TRACKS_PATH}")
        if code != 0:
            if ref == "origin/master":
                if _master_lacks_registry():
                    notes.append(
                        f"{TRACKS_PATH} is not on origin/master yet, so claims come only "
                        "from open branches. Expected until the branch adding it merges."
                    )
                else:
                    problems.append(f"{ref}: could not read {TRACKS_PATH}")
            continue  # an older feature branch may predate the registry
        readable = True
        parsed = parse_claims(text)
        rows_by_ref[ref] = parsed.claims
        problems.extend(f"{ref}: {problem}" for problem in parsed.problems)

    return ScanResult(rows_by_ref, existing, merged, readable, tuple(problems), tuple(notes))


def _format(row: dict[str, str]) -> str:
    item = row.get(ITEM_COLUMN, "?").strip() or "?"
    branch = row.get(BRANCH_COLUMN, "?").strip() or "?"
    deps = "deps" if is_dep_claim(row) else "no deps"
    started = row.get(STARTED_COLUMN, "").strip()
    suffix = f", claimed {started}" if started else ""
    return f"{item}\t{branch}\t{deps}{suffix}"


def _claims() -> int:
    scan = _scan()
    if not scan.readable:
        print(f"WARNING: no {TRACKS_PATH} found on any origin ref.", file=sys.stderr)
        return 1
    for note in scan.notes:
        print(f"NOTE: {note}", file=sys.stderr)
    resolved = live_claims(scan.rows_by_ref, scan.existing, scan.merged)
    problems = (*scan.problems, *resolved.problems)
    if problems:
        for problem in problems:
            print(f"FAIL: invalid track registry: {problem}", file=sys.stderr)
        return 1
    for row in resolved.claims:
        print(_format(row))
    return 0


def _deps() -> int:
    scan = _scan()

    if not scan.readable:
        print(
            f"FAIL: could not read {TRACKS_PATH} from any origin ref, so the dependency\n"
            "claim cannot be checked. Fetch origin and try again; do not start a\n"
            "dependency-adding item on an unverified registry.",
            file=sys.stderr,
        )
        return 1

    for note in scan.notes:
        print(f"NOTE: {note}", file=sys.stderr)

    resolved = live_claims(scan.rows_by_ref, scan.existing, scan.merged)
    problems = (*scan.problems, *resolved.problems)
    if problems:
        for problem in problems:
            print(f"FAIL: invalid track registry: {problem}", file=sys.stderr)
        print(
            "\nThe dependency slot cannot be proven free. Repair the named TRACKS.md "
            "before starting another dependency-adding item.",
            file=sys.stderr,
        )
        return 1

    held = [row for row in resolved.claims if is_dep_claim(row)]
    if not held:
        return 0

    for row in held:
        item = row.get(ITEM_COLUMN, "?").strip() or "?"
        branch = row.get(BRANCH_COLUMN, "?").strip() or "?"
        started = row.get(STARTED_COLUMN, "").strip()
        print(f"FAIL: {item} already holds the dependency claim.", file=sys.stderr)
        print(f"  branch: {branch} (unmerged on origin)", file=sys.stderr)
        if started:
            print(f"  claimed: {started}", file=sys.stderr)
    print(
        "\nuv.lock serializes tracks. Wait for that PR to merge, or\n"
        f"drop the row if the branch is abandoned ({TRACKS_PATH}).",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("claims", "deps"))
    args = parser.parse_args(argv)

    if args.command == "claims":
        return _claims()
    return _deps()


if __name__ == "__main__":
    raise SystemExit(main())
