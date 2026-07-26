"""The shared Claude permission allowlist preserves its outward-action boundary.

Two review rounds have now found the same defect in different clothes: a prefix rule that
quietly reached an outward action the list meant to exclude -- `Bash(uv run *)` matching
`uv run git push` (round 1), then `Bash(scripts/*)` and `Bash(bash .github/scripts/*)`
reaching `finish-item.sh --delete-remote` (round 2). A regression test that covers only
the most recent spelling guards the half of the history that already has everyone's
attention, so both rounds are pinned here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS = REPO_ROOT / ".claude" / "settings.json"

# Round 1: an interpreter takes an arbitrary command as its argument, so a wildcard after
# one allows anything the interpreter can run -- including the git and gh commands
# deliberately left off the list.
INTERPRETER_WILDCARDS = (
    "Bash(uv run *)",
    "Bash(uv run python *)",
    "Bash(uv run python3 *)",
    "Bash(python *)",
    "Bash(python3 *)",
    "Bash(bash *)",
    "Bash(sh *)",
    "Bash(env *)",
    "Bash(xargs *)",
)

# Absent by intent: these are the outward actions the operator is the boundary on.
OUTWARD_COMMANDS = ("git push", "gh pr create", "gh pr merge", "gh release")


def _allowed() -> set[str]:
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    allow = data["permissions"]["allow"]
    assert isinstance(allow, list)
    return set(allow)


def test_script_permissions_are_explicit_not_directory_wildcards() -> None:
    allowed = _allowed()

    assert all("scripts/*" not in rule for rule in allowed)
    assert {
        "Bash(bash .github/scripts/check-migrations.sh)",
        "Bash(bash .github/scripts/check-tracked-artifacts.sh)",
    } <= allowed


@pytest.mark.parametrize("rule", INTERPRETER_WILDCARDS)
def test_no_interpreter_wildcards(rule: str) -> None:
    """Round 1's finding: `uv run *` matched `uv run git push`."""
    assert rule not in _allowed()


def test_no_rule_ends_an_interpreter_with_a_bare_wildcard() -> None:
    """The general shape, so an unlisted interpreter cannot reintroduce the hole.

    A rule whose final token before `*` is an interpreter name allows that interpreter to
    run anything. Spelled out rather than matched loosely: the rules that legitimately end
    in a wildcard name a *script or subcommand* first (`uv run pytest*`).
    """
    interpreters = {"uv run", "python", "python3", "bash", "sh", "env", "xargs", "sudo"}
    for rule in _allowed():
        body = rule.removeprefix("Bash(").removesuffix(")").strip()
        stem = body.removesuffix("*").strip()
        assert stem not in interpreters, f"{rule} allows an interpreter to run anything"


@pytest.mark.parametrize("command", OUTWARD_COMMANDS)
def test_outward_actions_are_absent(command: str) -> None:
    """These prompt every time on purpose; the operator is the boundary, not this file."""
    for rule in _allowed():
        body = rule.removeprefix("Bash(").removesuffix(")").strip()
        assert not body.startswith(command), f"{rule} allows {command!r} without a prompt"


def test_finish_item_is_absent_because_a_prefix_cannot_exclude_its_flag() -> None:
    """`finish-item.sh --delete-remote` pushes a branch deletion, and no prefix rule can
    tell it apart from the safe form -- so the script is listed nowhere."""
    for rule in _allowed():
        assert "finish-item" not in rule
