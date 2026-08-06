"""The review request generator's honesty guards (scripts/review-request.py).

The generator runs the four gates against the *working tree* and builds its diffs from a
``HEAD`` it pins to an immutable hash. Those are the same code only while the tree is
clean *for the whole gate run*: with an uncommitted fix in place, the request truthfully
reported four passes for a change the reviewer was never shown, and an untracked test
file changed what pytest collected without appearing in the diff at all (cross-model
review, round 2).

The pinning is the same failure one level up. A request that never stated its own commit
could be answered against a different tree with nothing in the document contradicting it
(M1-308 round 6), so ``HEAD`` and the diff base are resolved once, printed, and used to
build every range and every check.

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


def test_the_boundary_names_both_what_is_out_of_scope_and_what_stays_in() -> None:
    """A boundary stated as one list is not a boundary.

    The rule has to be checkable without the reviewer inferring the complement. Naming only
    the untrusted inputs leaves "is a FIFO at a configured path a blocker?" open; naming only
    the excluded attacker is worse, because it reads as licence to wave through *any* local
    failure. The second assertion is the one that would have caught the wrong reading: an
    ordinary unreadable file is not a hostile filesystem, and M1-308's round-6 FIFO hang was
    a real defect found in self-review.
    """
    # Leading newline so the first bullet splits like the rest of them.
    bullets = ("\n" + review_request.STANDING_CONVENTIONS).split("\n- ")
    boundary = next(
        (entry for entry in bullets if entry.startswith("**Threat and operational boundary.**")),
        None,
    )
    assert boundary is not None, "STANDING_CONVENTIONS has no threat-boundary bullet"

    # Scoped to the one bullet: asserting these against the whole block would pass on a
    # boundary that named only one side, since the other terms appear in later conventions.
    for excluded in ("non-malicious", "hostile local state", "backlog candidate"):
        assert excluded in boundary, excluded
    # Case-insensitive: the untrusted list opens a sentence, so "Provider JSON" is
    # capitalized here and lower-case wherever it is cited.
    for untrusted_item in ("provider json", "metaculus", "llm output", "ledger"):
        assert untrusted_item in boundary.casefold(), untrusted_item
    # The clause that distinguishes this from "anything local is trusted".
    assert "reachable reliability conditions" in boundary
    assert "Monkeypatching is only a test technique" in boundary


def test_every_round_carries_the_three_scope_tests() -> None:
    """These are mechanical, so they belong in round 1 as much as in round 5.

    Withholding them until the stopping rule engages is what let rounds 1 and 2 spend on a
    pre-existing condition and on a reproduction against an older tree.
    """
    for round_number in (1, 2, 3, 9):
        policy = review_request._review_policy(round_number)
        assert "threat model" in policy.casefold(), round_number
        assert "pre-existing condition" in policy.casefold(), round_number
        assert "Reviewed revision" in policy, round_number
        # A dismissal loses the finding; a backlog row keeps it. The distinction is the
        # whole reason these are safe to apply -- assert the request still asks for it.
        assert "backlog" in policy.casefold(), round_number


def test_branch_causality_is_not_a_flat_pre_existing_code_exemption() -> None:
    """The amplification clause is the point of the rewrite, so pin it.

    A flat "already on the diff base means non-blocking" would have excused M1-308's round-7
    finding: the same unguarded `yaml.safe_load` sat in `config.py` (filed as M0-007) *and*
    in the branch's own new `allowlist.py`. The test asserts the escape hatch is conditional,
    not that the words appear somewhere in the document.
    """
    for round_number in (1, 2, 3, 9):
        policy = review_request._review_policy(round_number)
        causality = policy[policy.index("**Branch causality.**") :].split("\n- ")[0]
        assert "neither depends on it nor materially increases" in causality, round_number
        assert "state the before/after exposure" in causality, round_number


# Distinct 40-character hashes, so an assertion that HEAD and the base are pinned
# *separately* cannot pass on a generator that resolves one and reuses it for both.
TEST_HEAD = "b" * 40
TEST_BASE = "c" * 40
TEST_REVIEWED = "a" * 40


def _render(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    """Assemble a real request with git stubbed out, and return the emitted document.

    Asserting on the constants alone cannot see the order they are placed in, which is the
    only thing a cross-reference depends on.
    """

    def fake_run(*args: str) -> str:
        # Matched exactly rather than by prefix: the generator now asks `rev-parse` two
        # different questions (the branch name, and each revision's commit hash), and a
        # prefix match would answer both with the branch name -- which is what a pinned
        # hash must never be.
        if args == ("git", "rev-parse", "--abbrev-ref", "HEAD"):
            return "chore/under-test\n"
        if args == ("git", "rev-parse", "--verify", "HEAD^{commit}"):
            return TEST_HEAD + "\n"
        if args == ("git", "rev-parse", "--verify", "origin/master^{commit}"):
            return TEST_BASE + "\n"
        if args == ("git", "rev-parse", "--verify", "reviewed-sha^{commit}"):
            return TEST_REVIEWED + "\n"
        if args[:3] == ("git", "merge-base", "--is-ancestor"):
            return ""
        if args[:3] == ("git", "diff", "--name-only"):
            return "src/whiskeyjack_bot/research/exa.py\n"
        if args[:2] == ("git", "diff"):
            return " src/whiskeyjack_bot/research/exa.py | 1 +\n"
        return ""

    monkeypatch.setattr(review_request, "_run", fake_run)
    review_request.main(["M1-305", "--no-verify", *argv])
    return capsys.readouterr().out


def test_the_contracts_cross_references_point_the_right_way(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cross-reference has to match the order the document is actually assembled in.

    The scope tests render in the review-round contract, the boundary they are about renders
    later in the standing conventions, and the output format renders last. An earlier draft
    said "the trust boundary above" from the contract -- pointing at a section the reviewer
    had not read yet. The scope tests are now self-contained, so the fix is asserted as the
    absence of any backward reference from the contract rather than as different wording.
    """
    body = _render(monkeypatch, capsys)
    contract = body.index("Check three scope tests")
    conventions = body.index("**Threat and operational boundary.**")
    output_format = body.index("## Output format")
    assert contract < conventions < output_format

    # Scoped to what precedes the definition: a reference to the boundary "above" is correct
    # under ## Output format, which renders after the conventions, so a blanket ban on the
    # phrase would fail on a document that is right.
    assert "boundary above" not in body[:conventions].casefold()


def test_the_request_pins_head_and_the_diff_base_to_full_hashes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A request that never states its own commit cannot be answered falsifiably.

    Demanding the reviewer's hash (``_output_format``) only closes one end: until the
    request printed its own, a response naming a different commit contradicted nothing in
    the document. M1-308's round 6 was spent exactly there, on a request whose embedded
    diff was demonstrably post-fix.
    """
    body = _render(monkeypatch, capsys)
    assert f"**Request HEAD:** `{TEST_HEAD}`" in body
    assert f"**Pinned diff base:** `{TEST_BASE}`" in body
    assert f"git diff {TEST_BASE}...{TEST_HEAD}" in body
    # The branch name is not a pin. It used to be the only identity the request carried.
    assert "diffed against `origin/master`" not in body


def test_the_embedded_diffs_survive_the_pinning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pinning names the range; it does not replace the code.

    The reviewer is a pasted-context model with no filesystem, so replacing the diff with
    `git diff <range>` would leave it reviewing a diffstat. Asserted because the pinning
    makes that substitution look free.
    """
    body = _render(monkeypatch, capsys, "--round", "2", "--previous-reviewed", "reviewed-sha")
    assert f"# Remediation diff (`git diff {TEST_REVIEWED}..{TEST_HEAD}`)" in body
    assert f"# Full branch diff for context only (`git diff {TEST_BASE}...{TEST_HEAD}`)" in body


def test_the_remediation_section_names_the_pinned_head_not_the_word_head(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "Review the current HEAD" is exactly the instruction a stale round satisfies."""
    body = _render(monkeypatch, capsys, "--round", "2", "--previous-reviewed", "reviewed-sha")
    section = body[body.index("## Previous review and remediation delta") :]
    assert f"The preceding review examined commit `{TEST_REVIEWED}`" in section
    assert f"Review request HEAD\n`{TEST_HEAD}`" in section


def test_the_ancestry_check_uses_the_pinned_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolving HEAD twice is how a request names one revision and validates another."""
    seen: list[tuple[str, ...]] = []

    def fake_run(*args: str) -> str:
        seen.append(args)
        if args == ("git", "rev-parse", "--verify", "reviewed-sha^{commit}"):
            return TEST_REVIEWED + "\n"
        return ""

    monkeypatch.setattr(review_request, "_run", fake_run)
    assert (
        review_request._reviewed_revision(2, "reviewed-sha", head_revision=TEST_HEAD)
        == TEST_REVIEWED
    )
    assert ("git", "merge-base", "--is-ancestor", TEST_REVIEWED, TEST_HEAD) in seen
    assert ("git", "merge-base", "--is-ancestor", TEST_REVIEWED, "HEAD") not in seen


def test_a_head_change_during_the_gates_voids_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gates take minutes; the window they run in is the one that matters.

    A commit landing mid-pytest would otherwise attach a green result to a revision the
    gates never saw, under the hash the request pins.
    """
    heads = iter((TEST_HEAD, "d" * 40))

    def fake_resolve(revision: str, *, label: str) -> str:
        del label
        return next(heads) if revision == "HEAD" else TEST_BASE

    monkeypatch.setattr(review_request, "_resolve_commit", fake_resolve)
    monkeypatch.setattr(review_request, "_require_clean_tree", lambda: None)
    monkeypatch.setattr(review_request, "_verify_gates", lambda: "all pass")

    with pytest.raises(SystemExit) as excinfo:
        review_request.main(["M1-305"])
    assert "HEAD changed while the gates were running" in str(excinfo.value)


def test_the_clean_tree_check_runs_after_the_gates_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before-only leaves the whole gate window unguarded."""
    calls = 0

    def counting_clean_tree() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(review_request, "_resolve_commit", lambda revision, *, label: TEST_HEAD)
    monkeypatch.setattr(review_request, "_require_clean_tree", counting_clean_tree)
    monkeypatch.setattr(review_request, "_verify_gates", lambda: "all pass")
    monkeypatch.setattr(review_request, "_run", lambda *args: "")

    review_request.main(["M1-305"])
    assert calls == 2


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
        # Exact matches: both revisions are now resolved through the same
        # `rev-parse --verify <rev>^{commit}` shape, so a prefix match would answer the
        # reviewed commit and HEAD with the same hash and trip the "no delta" guard.
        if args == ("git", "rev-parse", "--verify", "reviewed-sha^{commit}"):
            return "a" * 40 + "\n"
        if args == ("git", "rev-parse", "--verify", "HEAD^{commit}"):
            return "b" * 40 + "\n"
        raise SystemExit("not an ancestor")

    monkeypatch.setattr(review_request, "_run", fake_run)
    with pytest.raises(SystemExit) as excinfo:
        review_request._reviewed_revision(3, "reviewed-sha")

    assert "must be an ancestor" in str(excinfo.value)
    assert calls[-1] == ("git", "merge-base", "--is-ancestor", "a" * 40, "b" * 40)


def test_previous_review_commit_resolves_to_the_exact_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = "a" * 40

    def fake_run(*args: str) -> str:
        if args == ("git", "rev-parse", "--verify", "reviewed-sha^{commit}"):
            return reviewed + "\n"
        if args == ("git", "rev-parse", "--verify", "HEAD^{commit}"):
            return "b" * 40 + "\n"
        assert args == ("git", "merge-base", "--is-ancestor", reviewed, "b" * 40)
        return ""

    monkeypatch.setattr(review_request, "_run", fake_run)
    assert review_request._reviewed_revision(4, "reviewed-sha") == reviewed
