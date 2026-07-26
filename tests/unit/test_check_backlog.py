"""The Done-flip gate's branch-name table (.github/scripts/check_backlog.py).

The gate's first version skipped any branch its pattern did not recognize, and the
pattern was anchored to lower case and to ``feat|fix`` alone. A cross-model review
produced two live false-greens against it -- ``feat/M1-303-x`` and
``feature/m1-303-x`` both reported success while M1-303 was still ``Not Started``.
Both appear verbatim below: the gate is the thing standing between a forgotten
status flip and master, so its own classification needs a regression barrier.

The script lives outside the package (CI runs it before ``uv sync``), so it is loaded
by path rather than imported.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_backlog.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_backlog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_backlog = _load()


def _rows() -> list[dict[str, str]]:
    """Two synthetic rows, so the table does not drift as the real backlog moves."""
    return [
        {"ID": "M1-303", "Status": "Not Started"},
        {"ID": "M1-305", "Status": "Done"},
        {"ID": "M1-401", "Status": "In Review"},
    ]


@pytest.mark.parametrize(
    ("branch", "disposition", "subject"),
    [
        # The shape the workflow actually produces.
        ("feat/m1-303-exa-fallback", "item", "M1-303"),
        ("fix/m1-305-tiebreak", "item", "M1-305"),
        # Round 1, finding 1: upper case fell through to "not an item branch".
        ("feat/M1-303-x", "item", "M1-303"),
        ("FEAT/M1-303-x", "item", "M1-303"),
        ("Feat/M1-303-X", "item", "M1-303"),
        # ...and so did the feature/ spelling.
        ("feature/m1-303-x", "item", "M1-303"),
        ("bugfix/m1-303-x", "item", "M1-303"),
        ("hotfix/M1-305-y", "item", "M1-305"),
        # No trailing slug is still an item branch.
        ("feat/m1-303", "item", "M1-303"),
        # Four-digit IDs (A-1101) and single-letter epics (T-901) are real shapes.
        ("feat/a-1101-submission", "item", "A-1101"),
        ("feat/t-901-acceptance", "item", "T-901"),
        # Infrastructure work owns no backlog row.
        ("chore/workflow-hardening", "skip", "chore"),
        ("docs/m1-notes", "skip", "docs"),
        ("ci/pin-actions", "skip", "ci"),
        ("deps/bump-ruff", "skip", "deps"),
        ("CHORE/Shouting", "skip", "chore"),
        # Everything else fails closed rather than skipping.
        ("chris/experiment", "unknown", "chris/experiment"),
        ("feat/tidy-imports", "unknown", "feat/tidy-imports"),
        ("m1-303-no-prefix", "unknown", "m1-303-no-prefix"),
        ("feat-m1-303-slash-typo", "unknown", "feat-m1-303-slash-typo"),
        # A bare branch name has no prefix to classify, so the skip list must not
        # match it on the strength of the whole name.
        ("chore", "unknown", "chore"),
        ("feature", "unknown", "feature"),
    ],
)
def test_classify_branch(branch: str, disposition: str, subject: str) -> None:
    assert check_backlog._classify_branch(branch) == (disposition, subject)


def test_skip_and_item_prefixes_are_disjoint() -> None:
    """A prefix in both lists would make the gate's behaviour depend on match order."""
    assert not check_backlog.ITEM_PREFIXES & check_backlog.SKIP_PREFIXES


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        # The regression cases: an item branch whose row is not Done must fail
        # whatever the casing or alias.
        ("feat/m1-303-x", 1),
        ("feat/M1-303-x", 1),
        ("FEAT/M1-303-x", 1),
        ("feature/m1-303-x", 1),
        ("fix/m1-401-prompt", 1),
        # Flipped to Done: the only passing item case.
        ("feat/m1-305-dedup", 0),
        ("FIX/M1-305-dedup", 0),
        # An item with no backlog row at all.
        ("feat/m9-999-invented", 1),
        # Infrastructure skips; unrecognized fails.
        ("chore/workflow-hardening", 0),
        ("chris/experiment", 1),
        ("feat/tidy-imports", 1),
    ],
)
def test_gate_exit_codes(branch: str, expected: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRANCH_NAME", branch)
    monkeypatch.setenv("IS_DRAFT", "false")
    assert check_backlog._gate(_rows()) == expected


def test_gate_fails_when_the_branch_name_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset BRANCH_NAME means the gate cannot tell what to check, so it must not
    report success -- the failure mode this whole check exists to prevent."""
    monkeypatch.setenv("BRANCH_NAME", "   ")
    monkeypatch.delenv("IS_DRAFT", raising=False)
    assert check_backlog._gate(_rows()) == 1


@pytest.mark.parametrize("is_draft", ["true", "True", "TRUE"])
def test_draft_pull_requests_skip(is_draft: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Done is set at merge, so the flip is expected to be missing while a PR is a
    draft. GitHub renders the boolean lower-case; the comparison is case-folded anyway."""
    monkeypatch.setenv("BRANCH_NAME", "feat/m1-303-x")
    monkeypatch.setenv("IS_DRAFT", is_draft)
    assert check_backlog._gate(_rows()) == 0


def test_a_non_draft_boolean_does_not_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """``github.event.pull_request.draft`` renders as an empty string on events that
    carry no pull request; only a literal true may skip."""
    monkeypatch.setenv("BRANCH_NAME", "feat/m1-303-x")
    monkeypatch.setenv("IS_DRAFT", "")
    assert check_backlog._gate(_rows()) == 1


def test_lint_accepts_the_tracked_backlog() -> None:
    """The gate reads the same CSV the lint validates; a lint failure here means the
    tracked backlog drifted from the schema the gate assumes."""
    assert check_backlog._lint(check_backlog._read_rows()) == []
