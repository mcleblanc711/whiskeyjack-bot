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

import argparse
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


@pytest.mark.parametrize(
    ("round_number", "expected", "unexpected"),
    [
        (1, "implementation review", "stopping rule is active"),
        (2, "remediation review", "stopping rule is active"),
        (3, "stopping rule is active", "implementation review"),
        (9, "stopping rule is active", "implementation review"),
    ],
)
def test_review_policy_changes_after_the_remediation_round(
    round_number: int, expected: str, unexpected: str
) -> None:
    policy = review_request._review_policy(round_number).casefold()
    assert expected in policy
    assert unexpected not in policy


def test_post_remediation_output_requires_approval_without_a_qualified_blocker() -> None:
    output = review_request._output_format(3)
    assert "verdict must be APPROVE" in output
    assert "violated acceptance criterion" in output
    assert "reachable path" in output
    assert "deterministic reproduction" in output
    assert "backlog title" in output


def test_the_trust_boundary_names_both_sides() -> None:
    """A boundary stated as one list is not a boundary.

    The rule has to be checkable without the reviewer inferring the complement: naming only
    what is trusted leaves "is provider JSON trusted?" open, and naming only what is not
    leaves the FIFO-at-a-configured-path case exactly where it was.
    """
    # Leading newline so the first bullet splits like the rest of them.
    bullets = ("\n" + review_request.STANDING_CONVENTIONS).split("\n- ")
    boundary = next((entry for entry in bullets if entry.startswith("**Trust boundary.**")), None)
    assert boundary is not None, "STANDING_CONVENTIONS has no trust-boundary bullet"

    # Scoped to the one bullet: asserting these against the whole block would pass on a
    # boundary that named only one side, since the other terms appear in later conventions.
    trusted, _, untrusted = boundary.partition("*Untrusted*")
    assert untrusted, "the untrusted side is missing"
    for trusted_item in ("config.yaml", "filesystem", "operator's shell", "monkeypatching"):
        assert trusted_item in trusted, trusted_item
    for untrusted_item in ("provider JSON", "Metaculus API", "LLM output", "ledger"):
        assert untrusted_item in untrusted, untrusted_item
    assert "backlog row" in untrusted


def test_every_round_carries_the_three_disqualifying_tests() -> None:
    """These are mechanical, so they belong in round 1 as much as in round 5.

    Withholding them until the stopping rule engages is what let rounds 1 and 2 spend on a
    pre-existing condition and on a reproduction against an older tree.
    """
    for round_number in (1, 2, 3, 9):
        policy = review_request._review_policy(round_number)
        assert "trust boundary" in policy.casefold(), round_number
        assert "already merged" in policy.casefold(), round_number
        assert "Stale" in policy, round_number
        # A dismissal loses the finding; a backlog row keeps it. The distinction is the
        # whole reason these are safe to apply -- assert the request still asks for it.
        assert "backlog" in policy.casefold(), round_number


def test_the_reviewed_commit_is_demanded_before_the_verdict() -> None:
    """Order is the point, not mere presence.

    A response that names its commit in a closing footnote has already spent the round; the
    hash has to come first so a stale review is caught before its findings are written. Three
    rounds were lost to reviews of an older tree (M1-308 r6, M1-603 r4, M1-303).
    """
    for round_number in (1, 2, 3):
        output = review_request._output_format(round_number)
        assert output.index("**Reviewed commit**") < output.index("**Verdict**"), round_number
        assert "the round is void" in output


def test_the_deliberate_choices_template_emits_the_five_headings() -> None:
    """M1-202's headings, which is the item that closed in two review-round commits.

    Emitted as headings rather than described in a comment: an unfilled heading still
    carrying its TODO is visible in the sent request, and a paragraph of advice is not.
    """
    template = review_request.DELIBERATE_CHOICES_TEMPLATE
    headings = [line for line in template.splitlines() if line.startswith("### ")]
    assert len(headings) == 5
    for expected in ("Decision", "Deviation", "Rejected", "Deferred", "Standing risk"):
        assert any(expected in heading for heading in headings), expected
    assert template.count("TODO(author)") == 5


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_review_round_must_be_positive(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        review_request._positive_round(value)


def test_first_review_refuses_a_previous_review_commit() -> None:
    with pytest.raises(SystemExit) as excinfo:
        review_request._reviewed_revision(1, "abc123")
    assert "only valid" in str(excinfo.value)


def test_remediation_review_requires_the_previous_review_commit() -> None:
    with pytest.raises(SystemExit) as excinfo:
        review_request._reviewed_revision(2, None)
    assert "requires --previous-reviewed" in str(excinfo.value)
    assert "exact commit" in str(excinfo.value)


def test_previous_review_commit_must_be_an_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str) -> str:
        calls.append(args)
        if args[:3] == ("git", "rev-parse", "--verify"):
            return "a" * 40 + "\n"
        if args == ("git", "rev-parse", "HEAD"):
            return "b" * 40 + "\n"
        raise SystemExit("not an ancestor")

    monkeypatch.setattr(review_request, "_run", fake_run)
    with pytest.raises(SystemExit) as excinfo:
        review_request._reviewed_revision(3, "reviewed-sha")

    assert "must be an ancestor" in str(excinfo.value)
    assert calls[-1] == ("git", "merge-base", "--is-ancestor", "a" * 40, "HEAD")


def test_previous_review_commit_resolves_to_the_exact_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = "a" * 40

    def fake_run(*args: str) -> str:
        if args[:3] == ("git", "rev-parse", "--verify"):
            return reviewed + "\n"
        if args == ("git", "rev-parse", "HEAD"):
            return "b" * 40 + "\n"
        assert args == ("git", "merge-base", "--is-ancestor", reviewed, "HEAD")
        return ""

    monkeypatch.setattr(review_request, "_run", fake_run)
    assert review_request._reviewed_revision(4, "reviewed-sha") == reviewed
