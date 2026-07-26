"""The track claims registry reader (scripts/tracks.py).

Two defects motivate this table. The claim scan read ``docs/TRACKS.md`` from
``origin/master`` alone, so a claim was invisible for exactly as long as it mattered --
a claim lives on its own branch until that branch merges. And the detector was
``grep -qi '| *yes *|'`` over the whole file, which matched a ``| yes |`` anywhere in it
(the Standing claims table included) and missed every spelling but the bare one.

Both live below: parsing is scoped to the Worktrees section, and liveness is proven per
row against the refs rather than assumed. The script is workflow tooling outside the
package, so it is loaded by path rather than imported.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "tracks.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tracks", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tracks = _load()

# The shape of the real file: a Standing claims table, then the Worktrees table. The
# `| *free* |` cell in the first table is the false positive the old detector produced.
REGISTRY = """\
# Active tracks

Prose about the registry being advisory.

## Standing claims

| Claim | Held by | Notes |
| --- | --- | --- |
| Dependency additions | *free* | One track at a time; yes, really. |
| Next free migration number | `003` | Claim it here first. |

## Worktrees

| Item | Branch | Worktree | Adds deps? | Migration | Started |
| --- | --- | --- | --- | --- | --- |
| M1-303 | feat/m1-303-exa | whiskeyjack-m1-303 | yes | none | 2026-07-24 |
| M1-308 | feat/m1-308-x | whiskeyjack-m1-308 | no | 003 | 2026-07-25 |

## Rules that fall out of this

- One dependency-adding item per wave.
"""

EMPTY_REGISTRY = """\
## Worktrees

| Item | Branch | Worktree | Adds deps? | Migration | Started |
| --- | --- | --- | --- | --- | --- |
| _(none)_ | | | | | |
"""


def test_parses_the_worktrees_table() -> None:
    parsed = tracks.parse_claims(REGISTRY)
    assert parsed.valid
    claims = parsed.claims
    assert [row["Item"] for row in claims] == ["M1-303", "M1-308"]
    assert claims[0]["Branch"] == "feat/m1-303-exa"
    assert claims[0]["Started"] == "2026-07-24"


def test_the_standing_claims_table_is_not_a_claim() -> None:
    """The old detector matched `| yes |` anywhere in the file, including prose above."""
    parsed = tracks.parse_claims(REGISTRY)
    assert parsed.valid
    claims = parsed.claims
    assert all(row["Item"].startswith("M1-") for row in claims)
    assert not any("free" in row.get("Branch", "") for row in claims)


def test_rules_section_ends_the_table() -> None:
    assert len(tracks.parse_claims(REGISTRY).claims) == 2


def test_placeholder_row_is_not_a_claim() -> None:
    parsed = tracks.parse_claims(EMPTY_REGISTRY)
    assert parsed.valid
    assert parsed.claims == []


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# Active tracks\n\nNo tables at all.\n",
        "## Standing claims\n\n| Claim | Held by |\n| --- | --- |\n| Deps | yes |\n",
        "## Worktrees\n",
        "## Worktrees\n\n| Item | Branch |\n| --- | --- |\n",
    ],
    ids=["empty", "no-table", "wrong-section", "no-table-body", "header-only"],
)
def test_malformed_tables_are_distinct_from_an_empty_registry(text: str) -> None:
    """Malformed metadata is not evidence that the dependency slot is free."""
    parsed = tracks.parse_claims(text)
    assert not parsed.valid
    assert parsed.claims == []


def test_ragged_row_is_invalid() -> None:
    text = "## Worktrees\n\n| Item | Branch | Adds deps? |\n| --- | --- | --- |\n| M1-303 | b |\n"
    parsed = tracks.parse_claims(text)
    assert not parsed.valid
    assert parsed.claims == []
    assert any("claim row" in problem for problem in parsed.problems)


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("yes", True),
        ("Yes", True),
        ("YES", True),
        # The spelling the old bare-`yes` grep missed.
        ("Yes (uv.lock)", True),
        ("**yes**", True),
        ("`yes`", True),
        # Unrecognized cells fail closed: a question costs less than a uv.lock conflict.
        ("maybe", True),
        ("asknews + exa", True),
        ("no", False),
        ("No", False),
        ("*no*", False),
        ("none", False),
        ("-", False),
        ("n/a", False),
        ("", False),
        ("   ", False),
    ],
)
def test_dep_claim_detection(cell: str, expected: bool) -> None:
    assert tracks.is_dep_claim({"Adds deps?": cell}) is expected


def test_a_blank_cell_and_a_missing_column_are_different() -> None:
    """A blank cell in a well-formed row states "no"; a truncated row states nothing."""
    assert tracks.is_dep_claim({"Adds deps?": ""}) is False
    assert tracks.is_dep_claim({"Item": "M1-303"}) is True


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        # Open branch: the claim is real.
        ("feat/m1-303-exa", True),
        # Merged: finish-item.sh leaves the row behind on purpose, so it must not block.
        ("feat/m1-305-done", False),
        # Unknown cannot be proven stale without cross-ref provenance.
        ("feat/m1-299-gone", True),
        # Already qualified with the remote name.
        ("origin/feat/m1-303-exa", True),
        # Unreadable branch cell cannot be proven stale, so it blocks.
        ("", True),
        ("   ", True),
    ],
)
def test_liveness(branch: str, expected: bool) -> None:
    merged = frozenset({"origin/feat/m1-305-done", "origin/master"})
    assert tracks.is_live({"Branch": branch}, merged) is expected


def test_live_claims_deduplicates_across_refs() -> None:
    """One row appears on every branch that has merged master since it was written."""
    rows = tracks.parse_claims(REGISTRY).claims
    existing = frozenset({"origin/feat/m1-303-exa", "origin/feat/m1-308-x", "origin/master"})
    merged = frozenset({"origin/master"})
    rows_by_ref = {"origin/master": rows, "origin/feat/m1-308-x": rows}

    resolved = tracks.live_claims(rows_by_ref, existing, merged)
    assert not resolved.problems

    assert [row["Item"] for row in resolved.claims] == ["M1-303", "M1-308"]


def test_live_claims_drops_merged_rows() -> None:
    rows = tracks.parse_claims(REGISTRY).claims
    existing = frozenset({"origin/feat/m1-303-exa", "origin/feat/m1-308-x", "origin/master"})
    merged = frozenset({"origin/feat/m1-303-exa", "origin/master"})

    resolved = tracks.live_claims({"origin/master": rows}, existing, merged)
    assert not resolved.problems

    assert [row["Item"] for row in resolved.claims] == ["M1-308"]
    assert not [row for row in resolved.claims if tracks.is_dep_claim(row)]


def test_the_tracked_registry_parses() -> None:
    """The real docs/TRACKS.md, so the reader and the file cannot drift apart."""
    text = (REPO_ROOT / "docs" / "TRACKS.md").read_text(encoding="utf-8")
    parsed = tracks.parse_claims(text)
    assert parsed.valid
    assert all(tracks.ITEM_COLUMN in row for row in parsed.claims)


def test_ref_enumeration_failure_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_git(*args: str) -> tuple[int, str]:
        if args[0] == "for-each-ref":
            return 1, ""
        if args[0] == "show":
            return 0, EMPTY_REGISTRY
        raise AssertionError(args)

    monkeypatch.setattr(tracks, "_git", fake_git)
    assert tracks._deps() == 1
    captured = capsys.readouterr()
    assert "could not enumerate" in captured.err


# --- The cross-branch scan, against real refs -------------------------------------
#
# The parsing above cannot show the actual defect, which was about *which files get
# read*: every unit here passed while the scan looked only at origin/master. So the
# reported scenario gets run for real -- one track pushes a claim to its own branch,
# and the next track has to be stopped by it.

WORKTREES_TABLE = """\
## Worktrees

| Item | Branch | Worktree | Adds deps? | Migration | Started |
| --- | --- | --- | --- | --- | --- |
{row}
"""
NO_CLAIM_ROW = "| _(none)_ | | | | | |"


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args), cwd=cwd, env=_env(), capture_output=True, text=True, check=True
    )
    return result.stdout


def _tracks_cli(cwd: Path, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("python3", str(SCRIPT), command),
        cwd=cwd,
        env=_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def _write_registry(checkout: Path, row: str) -> None:
    (checkout / "docs").mkdir(exist_ok=True)
    (checkout / "docs" / "TRACKS.md").write_text(WORKTREES_TABLE.format(row=row), encoding="utf-8")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout with a local bare `origin` and an empty registry on master."""
    origin = tmp_path / "origin.git"
    checkout_path = tmp_path / "work"

    for args in (
        ("git", "init", "--bare", "--initial-branch=master", str(origin)),
        ("git", "init", "--initial-branch=master", str(checkout_path)),
    ):
        subprocess.run(args, env=_env(), capture_output=True, check=True)

    _write_registry(checkout_path, NO_CLAIM_ROW)
    _git(checkout_path, "add", "-A")
    _git(checkout_path, "commit", "-m", "seed")
    _git(checkout_path, "remote", "add", "origin", str(origin))
    _git(checkout_path, "push", "-u", "origin", "master")
    return checkout_path


def _claim_on_a_branch(
    checkout: Path, branch: str, row: str, *, heading: str = "## Worktrees"
) -> None:
    _git(checkout, "checkout", "-b", branch)
    _write_registry(checkout, row)
    path = checkout / "docs" / "TRACKS.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("## Worktrees", heading, 1), encoding="utf-8"
    )
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-m", f"claim on {branch}")
    _git(checkout, "push", "-u", "origin", branch)
    _git(checkout, "checkout", "master")


def test_an_empty_registry_leaves_the_slot_free(checkout: Path) -> None:
    assert _tracks_cli(checkout, "deps").returncode == 0
    assert _tracks_cli(checkout, "claims").stdout.strip() == ""


def test_a_claim_pushed_to_a_branch_blocks_the_next_track(checkout: Path) -> None:
    """The reported scenario. M1-303 commits `Adds deps? yes` on its feature branch;
    M1-307 then starts a dependency-adding item and must not be told the slot is free."""
    _claim_on_a_branch(
        checkout,
        "feat/m1-303-exa",
        "| M1-303 | feat/m1-303-exa | whiskeyjack-m1-303 | yes | none | 2026-07-24 |",
    )

    # Master's own copy still shows nothing -- which is exactly why reading it was blind.
    assert "yes" not in (checkout / "docs" / "TRACKS.md").read_text(encoding="utf-8")

    result = _tracks_cli(checkout, "deps")

    assert result.returncode == 1
    assert "M1-303 already holds the dependency claim" in result.stderr
    assert "feat/m1-303-exa" in result.stderr


def test_a_malformed_live_registry_blocks_the_dependency_check(checkout: Path) -> None:
    branch = "feat/m1-303-exa"
    _claim_on_a_branch(
        checkout,
        branch,
        f"| M1-303 | {branch} | whiskeyjack-m1-303 | yes | none | 2026-07-24 |",
        heading="## Worktree",
    )

    result = _tracks_cli(checkout, "deps")

    assert result.returncode == 1
    assert "invalid track registry" in result.stderr
    assert "missing '## Worktrees' section" in result.stderr


def test_a_mistyped_branch_claim_blocks_the_dependency_check(checkout: Path) -> None:
    branch = "feat/m1-303-exa"
    _claim_on_a_branch(
        checkout,
        branch,
        "| M1-303 | feat/m1-303-exaa | whiskeyjack-m1-303 | yes | none | 2026-07-24 |",
    )

    result = _tracks_cli(checkout, "deps")

    assert result.returncode == 1
    assert "invalid track registry" in result.stderr
    assert "feat/m1-303-exaa" in result.stderr


def test_a_merged_claim_stops_blocking(checkout: Path) -> None:
    """The row outlives its branch on purpose -- finish-item.sh leaves it for the next
    branch to sweep. A leftover must not hold the slot forever."""
    branch = "feat/m1-303-exa"
    _claim_on_a_branch(
        checkout,
        branch,
        f"| M1-303 | {branch} | whiskeyjack-m1-303 | yes | none | 2026-07-24 |",
    )
    _git(checkout, "merge", "--no-ff", "-m", "merge M1-303", branch)
    _git(checkout, "push", "origin", "master")

    # The claim row is now on master, and it is stale.
    assert "yes" in (checkout / "docs" / "TRACKS.md").read_text(encoding="utf-8")
    assert _tracks_cli(checkout, "deps").returncode == 0

    # Once the merged branch is deleted, master is the provenance that proves the row stale.
    _git(checkout, "push", "origin", "--delete", branch)
    _git(checkout, "fetch", "--prune", "origin")
    assert _tracks_cli(checkout, "deps").returncode == 0


def test_a_deleted_branch_stops_blocking(checkout: Path) -> None:
    branch = "feat/m1-303-exa"
    _claim_on_a_branch(
        checkout,
        branch,
        f"| M1-303 | {branch} | whiskeyjack-m1-303 | yes | none | 2026-07-24 |",
    )
    assert _tracks_cli(checkout, "deps").returncode == 1

    _git(checkout, "push", "origin", "--delete", branch)
    _git(checkout, "fetch", "--prune", "origin")

    assert _tracks_cli(checkout, "deps").returncode == 0


def test_an_unmerged_claim_keeps_blocking_whatever_its_spelling(checkout: Path) -> None:
    """`Yes (uv.lock)` is a claim. The detector this replaced matched the bare word
    only, so annotating your own claim row silently released the slot."""
    _claim_on_a_branch(
        checkout,
        "feat/m1-999-abandoned",
        "| M1-999 | feat/m1-999-abandoned | wt | Yes (uv.lock) | none | 2026-07-01 |",
    )

    result = _tracks_cli(checkout, "deps")

    assert result.returncode == 1
    assert "M1-999" in result.stderr


def test_a_non_dependency_claim_does_not_block(checkout: Path) -> None:
    _claim_on_a_branch(
        checkout,
        "feat/m1-308-x",
        "| M1-308 | feat/m1-308-x | whiskeyjack-m1-308 | no | 003 | 2026-07-25 |",
    )

    assert _tracks_cli(checkout, "deps").returncode == 0
    # ...but it is still a live track, and `claims` says so.
    assert "M1-308" in _tracks_cli(checkout, "claims").stdout


def test_a_registry_not_yet_on_master_is_not_a_broken_registry(tmp_path: Path) -> None:
    """The bootstrap state: the branch that adds TRACKS.md has not merged yet.

    Read as a broken registry, this made `deps` -- and every `start-item.sh` run through
    it -- fail on the very branch introducing the file. No throwaway-repo test above can
    see it, because they all seed the registry onto master first; this one does not.
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    for args in (
        ("git", "init", "--bare", "--initial-branch=master", str(origin)),
        ("git", "init", "--initial-branch=master", str(work)),
    ):
        subprocess.run(args, env=_env(), capture_output=True, check=True)

    (work / "README.md").write_text("seed\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "seed")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-u", "origin", "master")

    _claim_on_a_branch(
        work,
        "chore/adds-the-registry",
        "| M1-303 | chore/adds-the-registry | whiskeyjack-m1-303 | yes | 003 | 2026-07-26 |",
    )

    listed = _tracks_cli(work, "claims")
    assert listed.returncode == 0
    assert "M1-303" in listed.stdout
    assert "not on origin/master yet" in listed.stderr

    # The note is not a pass: a claim on that branch still holds the dependency slot.
    blocked = _tracks_cli(work, "deps")
    assert blocked.returncode == 1
    assert "M1-303" in blocked.stderr


def test_an_unreadable_registry_blocks_rather_than_assuming_free(tmp_path: Path) -> None:
    """No registry on any ref is not evidence that the slot is free, and this check
    exists to prevent a collision rather than to be reassuring."""
    empty = tmp_path / "bare"
    subprocess.run(
        ("git", "init", "--initial-branch=master", str(empty)),
        env=_env(),
        capture_output=True,
        check=True,
    )

    result = _tracks_cli(empty, "deps")

    assert result.returncode == 1
    assert "could not read" in result.stderr
