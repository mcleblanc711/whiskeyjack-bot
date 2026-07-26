"""End-to-end cleanup behaviour of scripts/finish-item.sh, against a throwaway repo.

The bug this exists for is not in any Python function: ``finish-item.sh`` discovered
branches with two globs (``feat/<id>-*``, ``fix/<id>-*``) while the backlog gate accepted
five prefixes, every casing and a bare ``feat/<id>`` with no slug. Seven of the eleven
branch shapes the gate would merge could then never be cleaned up. A unit test of the
classifier cannot catch that -- the two agreed on nothing and both were internally
consistent -- so the shell has to be run for real (cross-model review, round 2).

Everything here is local: a bare repository on disk stands in for ``origin``, so no
socket is opened and the suite's network guard is untouched. Git config is isolated from
the developer's, since a stray ``init.defaultBranch`` or commit template would otherwise
decide whether these pass.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FINISH_ITEM = REPO_ROOT / "scripts" / "finish-item.sh"
CHECK_BACKLOG = REPO_ROOT / ".github" / "scripts" / "check_backlog.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

# The forms the backlog gate accepts. The first is the only shape the old globs found;
# the rest are the ones that could be merged and then never cleaned up.
MERGEABLE_BRANCHES = [
    ("M1-303", "feat/m1-303-exa-fallback"),
    ("M1-303", "feature/m1-303-x"),
    ("M1-303", "bugfix/m1-303-x"),
    ("M1-305", "hotfix/M1-305-y"),
    ("M1-303", "FEAT/M1-303-x"),
    ("M1-303", "feat/m1-303"),
    ("M1-305", "fix/m1-305-tiebreak"),
]


def _env() -> dict[str, str]:
    """Git that ignores the developer's own configuration and identity."""
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


def _run_finish(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("bash", str(FINISH_ITEM), *args),
        cwd=checkout,
        env=_env(),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout with a local bare `origin`, one commit on master, and the real gate
    script in place -- finish-item.sh shells out to it to classify branch names."""
    origin = tmp_path / "origin.git"
    # The worktree path is derived from the *parent* of the repo root, so the checkout
    # cannot sit at tmp_path itself.
    work = tmp_path / "work"
    work.mkdir()
    checkout_path = work / "whiskeyjack-bot"

    subprocess.run(
        ("git", "init", "--bare", "--initial-branch=master", str(origin)),
        env=_env(),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ("git", "init", "--initial-branch=master", str(checkout_path)),
        env=_env(),
        capture_output=True,
        check=True,
    )

    scripts_dir = checkout_path / ".github" / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy(CHECK_BACKLOG, scripts_dir / "check_backlog.py")
    (checkout_path / "README.md").write_text("seed\n", encoding="utf-8")

    _git(checkout_path, "add", "-A")
    _git(checkout_path, "commit", "-m", "seed")
    _git(checkout_path, "remote", "add", "origin", str(origin))
    _git(checkout_path, "push", "-u", "origin", "master")
    return checkout_path


def _merged_branch(checkout: Path, branch: str) -> None:
    """Create `branch`, commit on it, merge it to master and push -- the state
    finish-item.sh is designed to clean up."""
    _git(checkout, "checkout", "-b", branch)
    (checkout / f"{branch.replace('/', '-')}.txt").write_text("work\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-m", f"work on {branch}")
    _git(checkout, "push", "-u", "origin", branch)
    _git(checkout, "checkout", "master")
    _git(checkout, "merge", "--no-ff", "-m", f"merge {branch}", branch)
    _git(checkout, "push", "origin", "master")


@pytest.mark.parametrize(("item", "branch"), MERGEABLE_BRANCHES)
def test_every_mergeable_branch_can_be_cleaned_up(checkout: Path, item: str, branch: str) -> None:
    """The regression barrier. Each of these passes the backlog gate, so each must be
    discoverable here -- previously only the feat/<id>-<slug> and fix/<id>-<slug> forms
    were, and the other five were stranded after their own merge."""
    _merged_branch(checkout, branch)

    result = _run_finish(checkout, item)

    assert result.returncode == 0, result.stderr
    remaining = _git(checkout, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert branch not in remaining.split()


def test_the_worktree_is_removed(checkout: Path) -> None:
    branch = "feat/m1-303-exa-fallback"
    worktree = checkout.parent / "whiskeyjack-m1-303"
    _merged_branch(checkout, branch)
    _git(checkout, "worktree", "add", str(worktree), branch)
    assert worktree.exists()

    result = _run_finish(checkout, "M1-303")

    assert result.returncode == 0, result.stderr
    assert not worktree.exists()


def test_a_dirty_worktree_is_refused(checkout: Path) -> None:
    branch = "feat/m1-303-exa-fallback"
    worktree = checkout.parent / "whiskeyjack-m1-303"
    _merged_branch(checkout, branch)
    _git(checkout, "worktree", "add", str(worktree), branch)
    (worktree / "uncommitted.txt").write_text("mine\n", encoding="utf-8")

    result = _run_finish(checkout, "M1-303")

    assert result.returncode == 1
    assert "uncommitted changes" in result.stderr
    assert worktree.exists()


def test_an_unmerged_branch_is_refused(checkout: Path) -> None:
    _git(checkout, "checkout", "-b", "feat/m1-303-exa-fallback")
    (checkout / "work.txt").write_text("work\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-m", "unmerged work")
    _git(checkout, "checkout", "master")

    result = _run_finish(checkout, "M1-303")

    assert result.returncode == 1
    assert "not merged into origin/master" in result.stderr


def test_an_item_with_no_branch_is_refused(checkout: Path) -> None:
    result = _run_finish(checkout, "M1-999")

    assert result.returncode == 1
    assert "No local branch names M1-999" in result.stderr


def test_ambiguous_branches_are_refused(checkout: Path) -> None:
    """Two branches naming one item is a state the script must not guess its way out
    of -- one of them is somebody else's."""
    _merged_branch(checkout, "feat/m1-303-exa-fallback")
    _merged_branch(checkout, "fix/m1-303-followup")

    result = _run_finish(checkout, "M1-303")

    assert result.returncode == 1
    assert "Ambiguous" in result.stderr


def test_delete_remote_refuses_a_remote_that_is_ahead(checkout: Path) -> None:
    """The local ancestor check cannot see commits pushed from somewhere else. Deleting
    the remote branch on the strength of a stale local ref destroys unmerged work."""
    branch = "feat/m1-303-exa-fallback"
    _merged_branch(checkout, branch)

    # Somebody pushes a review fix from another machine, and this checkout never has it.
    origin_url = _git(checkout, "remote", "get-url", "origin").strip()
    elsewhere = checkout.parent / "elsewhere"
    subprocess.run(
        ("git", "clone", "--branch", branch, origin_url, str(elsewhere)),
        env=_env(),
        capture_output=True,
        check=True,
    )
    (elsewhere / "review-fix.txt").write_text("fix\n", encoding="utf-8")
    _git(elsewhere, "add", "-A")
    _git(elsewhere, "commit", "-m", "review fix")
    _git(elsewhere, "push", "origin", branch)

    result = _run_finish(checkout, "M1-303", "--delete-remote")

    assert result.returncode == 1
    assert "not in origin/master" in result.stderr
    # Refused before anything was removed, not halfway through.
    assert branch in _git(checkout, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert f"origin/{branch}" in _git(
        checkout, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"
    )


def test_delete_remote_removes_a_merged_remote_branch(checkout: Path) -> None:
    branch = "feat/m1-303-exa-fallback"
    _merged_branch(checkout, branch)

    result = _run_finish(checkout, "M1-303", "--delete-remote")

    assert result.returncode == 0, result.stderr
    remote_refs = _git(checkout, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    assert f"origin/{branch}" not in remote_refs.split()
