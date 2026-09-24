"""M1-513: the spec's probability envelope has exactly one executable declaration in ``src/``.

``config.PROBABILITY_BOUND_FLOOR``/``CEILING`` (``0.001``/``0.999``, CODEX_HANDOFF.md §
Configuration schema) are the envelope that forecast validation *and* submission validation
enforce. Until M1-513, ``submission_live.py`` declared its own ``0.001``/``0.999``, so there
were two sources for one spec number and nothing failed if they drifted. M1-502's risk claim
4 asserted a single source and was true only of the forecast package.

This walks the syntax tree of every module in ``src/`` rather than grepping it, so comments
and docstrings -- which name the envelope in prose all over the forecast package and are not
executable -- do not count, and a float spelled ``1e-3`` or a string built by an f-string
does. Two things fail it:

- a float constant equal to either endpoint anywhere but the two ``config.py`` assignments;
- a non-docstring string constant containing either endpoint's spelling, f-string parts
  included -- the diagnostics that name the envelope must render it from the constants, or
  an envelope change would leave the message stating the old pair.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

from whiskeyjack_bot import config, submission_live
from whiskeyjack_bot.forecast import binary, generate, multiple_choice

SRC: Final = Path(__file__).resolve().parents[2] / "src" / "whiskeyjack_bot"
# Hand-written, not read off the constants: this is what a second declaration would spell.
_ENDPOINTS: Final = (0.001, 0.999)
_SPELLINGS: Final = ("0.001", "0.999")
_DECLARATIONS: Final = {"PROBABILITY_BOUND_FLOOR", "PROBABILITY_BOUND_CEILING"}


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """The ids of every docstring expression: module, class and function first statements."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def _declaration_values(tree: ast.Module) -> set[int]:
    """The ids of the two sanctioned constants, in ``config.py`` only."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in _DECLARATIONS
        ):
            found.add(id(node.value))
    return found


def _second_declarations(source: str, *, is_config: bool) -> list[str]:
    """Every executable spelling of an endpoint in ``source``, as ``line: kind``."""
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    if is_config:
        skip |= _declaration_values(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in skip:
            continue
        value = node.value
        if isinstance(value, float) and value in _ENDPOINTS:
            hits.append(f"{node.lineno}: float {value!r}")
        elif isinstance(value, str) and any(spelling in value for spelling in _SPELLINGS):
            hits.append(f"{node.lineno}: string naming an endpoint")
    # ``ast.walk`` is breadth-first, so the order it finds things in is not line order.
    return sorted(hits, key=lambda hit: int(hit.split(":", 1)[0]))


def test_no_module_in_src_declares_the_envelope_a_second_time() -> None:
    offenders = {
        str(path.relative_to(SRC)): hits
        for path in sorted(SRC.rglob("*.py"))
        if (
            hits := _second_declarations(
                path.read_text(encoding="utf-8"), is_config=path == SRC / "config.py"
            )
        )
    }
    assert offenders == {}


def test_the_scan_sees_each_shape_a_second_declaration_could_take() -> None:
    """Anti-vacuity: the walker is shown to find what it exists to find.

    A walker that skipped every constant would pass the test above against any tree, so
    each shape is planted in a synthetic module and must be reported -- including the two
    that a grep for ``0.001`` would miss (``1e-3``) or wrongly count (a comment, a docstring).
    """
    planted = '''
"""Module docstring naming 0.001 and 0.999 is prose."""
# a comment naming 0.001 is not executable
_MIN = 0.001
_MAX = 999e-3
_ALSO = 1e-3
def f(x):
    """Function docstring 0.999."""
    return f"between 0.001 and {x}"
MESSAGE = "must lie within 0.999"
PROBABILITY_BOUND_FLOOR = 0.001
'''
    hits = _second_declarations(planted, is_config=False)
    assert hits == [
        "4: float 0.001",
        "5: float 0.999",
        "6: float 0.001",
        "9: string naming an endpoint",
        "10: string naming an endpoint",
        "11: float 0.001",
    ]
    # In config.py, and only there, the named assignment is the sanctioned declaration.
    assert "11: float 0.001" not in _second_declarations(planted, is_config=True)


def test_the_constants_are_the_ones_every_consumer_reads() -> None:
    """The single source is the one actually consumed: each module that enforces the
    envelope reaches ``config``'s constants rather than a same-valued copy of them."""
    assert (config.PROBABILITY_BOUND_FLOOR, config.PROBABILITY_BOUND_CEILING) == _ENDPOINTS
    for module in (submission_live, binary, multiple_choice, generate):
        assert module.PROBABILITY_BOUND_FLOOR is config.PROBABILITY_BOUND_FLOOR
        assert module.PROBABILITY_BOUND_CEILING is config.PROBABILITY_BOUND_CEILING
