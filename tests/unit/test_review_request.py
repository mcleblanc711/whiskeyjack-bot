"""The review request generator's honesty guards (scripts/review-request.py).

The generator runs the four gates against the *working tree* and builds its diff from
committed ``HEAD``. Those are the same code only while the tree is clean: with an
uncommitted fix in place, the request truthfully reported four passes for a change the
reviewer was never shown, and an untracked test file changed what pytest collected
without appearing in the diff at all (cross-model review, round 2).

So the tests here are about what the generator refuses to say. It is workflow tooling
outside the package, so it is loaded by path rather than imported.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "review-request.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("review_request", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


review_request = _load()


def _fake_status(output: str, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Stub `git status --porcelain` and record what the module actually ran."""
    calls: list[tuple[str, ...]] = []

    def _run(args: tuple[str, ...], **_kwargs: Any) -> SimpleNamespace:
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(review_request.subprocess, "run", _run)
    return calls


def test_a_clean_tree_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_status("", monkeypatch)
    review_request._require_clean_tree()
    assert calls == [("git", "status", "--porcelain")]


@pytest.mark.parametrize(
    "porcelain",
    [
        " M src/whiskeyjack_bot/ledger.py\n",
        "?? tests/unit/test_scratch.py\n",
        "A  docs/backlog/backlog.csv\n M scripts/tracks.py\n",
    ],
    ids=["modified", "untracked", "mixed"],
)
def test_a_dirty_tree_refuses(porcelain: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Untracked files count: pytest collects them, and the diff does not show them."""
    _fake_status(porcelain, monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        review_request._require_clean_tree()

    message = str(excinfo.value)
    assert "not clean" in message
    assert "--no-verify" in message


def test_the_refusal_lists_paths_but_never_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paths are the project's carve-out from the no-values rule; contents are not."""
    _fake_status(" M config.yaml\n", monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        review_request._require_clean_tree()

    assert "config.yaml" in str(excinfo.value)


def test_a_long_dirty_list_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_status("".join(f" M file{index}.py\n" for index in range(25)), monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        review_request._require_clean_tree()

    assert "and 15 more" in str(excinfo.value)


def test_no_verify_banner_is_plain_on_a_clean_tree() -> None:
    assert review_request._not_verified_banner(dirty=False) == review_request.NOT_VERIFIED


def test_no_verify_banner_carries_the_dirty_caveat() -> None:
    """--no-verify stays the one way through, but it may not go quiet about a second
    reason to distrust the request."""
    banner = review_request._not_verified_banner(dirty=True)

    assert review_request.NOT_VERIFIED in banner
    assert "uncommitted changes" in banner
    assert all(line.startswith(">") for line in banner.splitlines())


def test_the_gates_are_the_four_documented_ones() -> None:
    """CLAUDE.md names these four; a request claiming a gate it never ran is the defect
    this section of the script exists to prevent."""
    assert [label for label, _ in review_request.GATES] == [
        "pytest",
        "ruff check",
        "ruff format --check",
        "mypy --strict src",
    ]
