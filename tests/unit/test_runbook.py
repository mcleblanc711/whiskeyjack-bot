"""T-908: `docs/RUNBOOK.md` against the CLI it documents.

The runbook quotes command lines, flags and exit codes, and until this module nothing in
the repository could fail when the program moved underneath it. D-1001's cross-model review
found three false claims of exactly that kind, all three in `_run_submit`'s tail, and one of
them was already contradicted by a merged test -- `test_cli_submit.py`'s
`test_an_uncertain_submission_tells_the_operator_the_next_command` has asserted
`EXIT_REFUSED` for `submission_uncertain` since M2-704. The suite held the fact; nothing
compared it to the document.

Three checks, in the order the item's acceptance criteria name them:

1. every `whiskeyjack-bot` command line the runbook shows parses under the real argparse
   parser, with every flag it names recognized;
2. the exit-code grid the runbook publishes for `submit` is a **total partition** of
   `(success, refetch_outcome, artifact written)` and every one of its sixteen cells is
   driven through the real CLI and compared;
3. every `file.py:symbol` citation resolves to a symbol defined in that file.

**Why the grid is parsed out of the document rather than written here.** D-1001's round-1
remediation fixed a row of that table and was still false; round 2 fixed another row and was
still false. The defect was never a missing row -- the table was keyed on the `result:` line,
which is the lifecycle event type, while the exit is
`(refetch_outcome == "confirmed", artifact_path is not None)`, and those two partitions
cross. A table is a claim about a partition, and per-row verification cannot detect a wrong
one. So the assertions below key on the two conditions the return reads, and the partition
itself is checked: no cell uncovered, no cell covered twice.
"""

from __future__ import annotations

import ast
import re
import shlex
from argparse import Action, ArgumentParser, _SubParsersAction
from pathlib import Path
from typing import Any, Final, get_args

import pytest

from whiskeyjack_bot.cli import EXIT_REFUSED, build_parser, main
from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
from whiskeyjack_bot.lifecycle import RefetchOutcome

# Imported by bare name, not as `tests.unit.test_cli_submit`. Two reasons, and the second is
# the load-bearing one. `tests/unit` is on `sys.path` because pytest's prepend import mode puts
# each test module's own directory there, so the bare name is what pytest itself imports these
# modules as -- the dotted form builds a *second* module object holding a second copy of every
# fixture and constant. And the dotted form only resolves at all because importing the pinned
# SDK side-effects the working directory onto `sys.path`; this module's imports are light enough
# not to trigger it, which is how it was found. `tests/score_rows.py:44` names the same accident.
import test_cli_submit
from test_cli_submit import _install
from test_submission_live import (  # noqa: F401 - the autouse fixture is reused deliberately
    NEW_START,
    PROBABILITY,
    FakePoster,
    FakeQuestion,
    _binary_values,
    _entry,
    isolate_activation_policy_for_gateway_tests,
)

# Re-exported by assignment rather than `from ... import`, because a test below takes each
# of these as a parameter -- which is how pytest requests a fixture -- and ruff reads an
# imported name shadowed by a parameter as F811. The binding pytest collects is the module
# attribute either way.
config_file = test_cli_submit.config_file
record_id = test_cli_submit.record_id
payload_file = test_cli_submit.payload_file

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
RUNBOOK: Final = REPO_ROOT / "docs" / "RUNBOOK.md"
SOURCE_ROOT: Final = REPO_ROOT / "src" / "whiskeyjack_bot"
PROGRAM: Final = "whiskeyjack-bot"


def runbook_text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


# ── fenced blocks ────────────────────────────────────────────────────────────
#
# The runbook's convention is rigid and this module depends on it: a ```bash block is what
# the operator types, and the plain ``` block under it is what the program printed. Three of
# the bash blocks are indented inside list items, so the fence pattern is not anchored to
# column zero -- anchoring it there drops them silently, which is the failure mode this
# module exists to prevent applied to the module itself.

_FENCE = re.compile(r"^[ \t]*```([A-Za-z0-9_+-]*)[ \t]*$")


def fenced_blocks(text: str, language: str) -> list[list[str]]:
    """Every fenced block written in `language`, as lists of stripped lines."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        match = _FENCE.match(line)
        if match is None:
            if current is not None:
                current.append(line.strip())
            continue
        if current is not None:
            blocks.append(current)
            current = None
        elif match.group(1) == language:
            current = []
    assert current is None, "an unterminated fence in docs/RUNBOOK.md"
    return blocks


# ── the documented command lines ─────────────────────────────────────────────

_REDIRECT = re.compile(r"\s*2>\s*/dev/null\s*")


def documented_invocations(text: str) -> list[list[str]]:
    """The argv of every `whiskeyjack-bot` command line the runbook shows.

    Joins `\\`-continued lines, drops a trailing `2>/dev/null`, and treats `[...]` as the
    optional-argument notation it is (`[--question-id ID]`), keeping the contents.

    The program is recognized as **argv[0] after `uv run`**, never as a substring: the
    runbook also says `cd ~/projects/whiskeyjack-bot && git pull --ff-only`, which a grep for
    the name reads as a command and this does not.
    """
    invocations: list[list[str]] = []
    for block in fenced_blocks(text, "bash"):
        pending = ""
        for line in block:
            joined = pending + line
            if joined.endswith("\\"):
                pending = joined[:-1].strip() + " "
                continue
            pending = ""
            candidate = _REDIRECT.sub(" ", joined).replace("[", "").replace("]", "")
            words = shlex.split(candidate)
            if words[:2] == ["uv", "run"]:
                words = words[2:]
            if words[:1] == [PROGRAM]:
                invocations.append(words[1:])
        assert not pending, "a bash block in docs/RUNBOOK.md ends mid-continuation"
    return invocations


# The commands the runbook shows today. A set equality rather than a subset check, and a
# count alongside it: an extractor that quietly stops matching, or a runbook that stops
# showing commands, must fail here rather than pass over an empty loop.
DOCUMENTED_COMMANDS: Final = frozenset(
    {
        "approve",
        "ingest-resolutions",
        "init-ledger",
        "reconcile-submission",
        "release-key",
        "replay",
        "report",
        "run-replay",
        "score",
        "show",
        "submit",
        "tournament",
        "unrecorded-posts",
        "verify-env",
        "verify-submission",
    }
)
DOCUMENTED_INVOCATION_COUNT: Final = 18


def subparsers_of(parser: ArgumentParser) -> dict[str, ArgumentParser]:
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public accessor
        if isinstance(action, _SubParsersAction):
            return dict(action.choices)
    return {}


def options_of(parser: ArgumentParser) -> dict[str, Action]:
    return {
        flag: action
        for action in parser._actions  # noqa: SLF001 - argparse exposes no public accessor
        for flag in action.option_strings
    }


def placeholder_for(action: Action) -> str:
    """A value this flag will accept, built from the flag's own declaration.

    The runbook writes `--question-id ID` and `--record-id "$REC"`, which are placeholders
    and not values; `ID` cannot survive `type=int`. Deriving the substitute from
    `action.type`/`action.choices` means this module carries no table of what each flag
    takes -- so a flag that changes type does not need editing here, and a flag that gains
    `choices` is still exercised with a legal one.
    """
    if action.choices:
        return str(next(iter(action.choices)))
    return "1" if action.type in (int, float) else "x"


def parseable_argv(argv: list[str]) -> list[str]:
    """`argv` with placeholder values replaced, and every name checked as it is walked.

    Descends into nested subparsers (`tournament enable`) rather than assuming the runbook
    only ever shows one level.
    """
    parser = build_parser()
    out: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("-"):
            options = options_of(parser)
            assert token in options, f"{token} is not a flag of `{' '.join(out)}`"
            action = options[token]
            out.append(token)
            if action.nargs != 0:
                index += 1
                assert index < len(argv), f"{token} takes a value and the runbook shows none"
                out.append(placeholder_for(action))
        else:
            choices = subparsers_of(parser)
            assert token in choices, f"`{token}` is not a command of `{' '.join(out) or PROGRAM}`"
            parser = choices[token]
            out.append(token)
        index += 1
    return out


def test_every_command_line_the_runbook_shows_parses() -> None:
    """A renamed command or flag fails here rather than in the next review round."""
    parser = build_parser()
    invocations = documented_invocations(runbook_text())

    assert len(invocations) == DOCUMENTED_INVOCATION_COUNT
    assert {argv[0] for argv in invocations} == DOCUMENTED_COMMANDS

    for argv in invocations:
        parsed = parser.parse_args(parseable_argv(argv))
        assert parsed.command == argv[0]


# ── markdown tables ──────────────────────────────────────────────────────────


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _plain(cell: str) -> str:
    return cell.replace("`", "").replace("*", "").strip()


def markdown_table(text: str, *, header: tuple[str, ...]) -> list[list[str]]:
    """The body rows of the one table whose header cells are `header`.

    Cells are compared with backticks and bold markers removed, so the table can be styled
    without this module caring. Exactly one table must match: two would make the assertion
    below a claim about whichever came first.
    """
    lines = text.splitlines()
    found: list[list[list[str]]] = []
    for index, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        if tuple(_plain(cell).lower() for cell in _cells(line)) != header:
            continue
        rows: list[list[str]] = []
        for body in lines[index + 2 :]:  # +2 skips the |---|---| separator
            if not body.startswith("|"):
                break
            rows.append([_plain(cell) for cell in _cells(body)])
        found.append(rows)
    assert len(found) == 1, f"{len(found)} tables in docs/RUNBOOK.md have the header {header}"
    return found[0]


# ── the `submit` exit-code grid ──────────────────────────────────────────────
#
# A cell is `(success, refetch_outcome, artifact written)` -- the three inputs, not the
# `result:` line. The exit is `EXIT_OK if receipt.verified_by_refetch and
# recorded.artifact_path else EXIT_REFUSED`, and `verified_by_refetch` is
# `refetch_outcome == "confirmed"`, so those three decide it and the event type does not.
# Keying on the event type is what made D-1001's table false twice.

Cell = tuple[bool, str, bool]

GRID_HEADER: Final = ("success", "refetch_outcome", "result:", "artifact", "exit")
ARTIFACT_STATES: Final = {"written": (True,), "not written": (False,), "either": (True, False)}


def grid_universe() -> frozenset[Cell]:
    """Every cell the grid must account for, built from the CLI's own vocabulary.

    `RefetchOutcome` is read rather than written out, so a fifth member makes the runbook's
    table incomplete and fails the partition check below instead of going uncovered.
    """
    return frozenset(
        (success, outcome, artifact)
        for success in (True, False)
        for outcome in get_args(RefetchOutcome)
        for artifact in (True, False)
    )


def _documented_bool(cell: str) -> bool:
    """`True`/`False` as the grid writes them -- never coerced.

    `cell.strip() == "True"` would read a typo, or a word the column does not use, as
    `False`; the partition check would then see a collision rather than the malformed cell
    that caused it. A document this module cannot parse must say so.
    """
    value = cell.strip()
    assert value in {"True", "False"}, f"the grid's `success` column reads {value!r}"
    return value == "True"


def documented_grid(text: str) -> tuple[dict[Cell, tuple[str, int]], list[Cell]]:
    """The runbook's grid expanded to one entry per cell, plus any cell claimed twice."""
    coverage: dict[Cell, tuple[str, int]] = {}
    duplicates: list[Cell] = []
    for row in markdown_table(text, header=GRID_HEADER):
        successes = [_documented_bool(part) for part in row[0].split("/")]
        outcomes = [part.strip() for part in row[1].split("/")]
        artifact_word = row[3].lower()
        assert artifact_word in ARTIFACT_STATES, f"the grid's artifact column reads {row[3]!r}"
        result, artifacts, code = row[2], ARTIFACT_STATES[artifact_word], int(row[4])
        for success in successes:
            for outcome in outcomes:
                for artifact in artifacts:
                    cell = (success, outcome, artifact)
                    if cell in coverage:
                        duplicates.append(cell)
                    coverage[cell] = (result, code)
    return coverage, duplicates


def test_the_documented_grid_is_a_total_partition() -> None:
    """The check per-row verification cannot do.

    D-1001's round-1 remediation fixed a row of this table and the table was still false;
    round 2 fixed another row and it was still false. The defect was the *index*, and adding
    rows cannot reach it. So: every cell of `(success, refetch_outcome, artifact)` claimed
    exactly once -- none missing, none claimed twice.
    """
    coverage, duplicates = documented_grid(runbook_text())

    assert not duplicates, "the runbook's grid claims these cells twice"
    assert set(coverage) == grid_universe()
    # Literal, and not redundant with the equality above: a commit that narrowed
    # `RefetchOutcome` *and* shrank the table to match would satisfy the equality, because
    # both sides would have moved together. This is the witness outside both.
    assert len(coverage) == 16


MISMATCHED_PROBABILITY: Final = 0.9  # deliberately not PROBABILITY (0.37)
_RESULT_LINE = re.compile(r"^result:\s+(\S+) \(success=(True|False), refetch=(\w+)\)$", re.M)
_ARTIFACT_LINE = re.compile(r"^artifact:\s+(.*)$", re.M)


def _after_question(outcome: str) -> FakeQuestion:
    """The question a refetch sees, scripted to produce each `RefetchOutcome`."""
    if outcome == "confirmed":
        return FakeQuestion(history=[_entry(NEW_START, _binary_values(PROBABILITY))])
    if outcome == "absent":
        return FakeQuestion(history=[])
    if outcome == "mismatched":
        return FakeQuestion(history=[_entry(NEW_START, _binary_values(MISMATCHED_PROBABILITY))])
    # `unreadable`: the platform answered with something the snapshot cannot be read out of.
    # The drive `test_submission_live.py`'s M2-711 tests already use.
    return FakeQuestion(api_json="not a dict")


def _refuse_artifact(*_args: Any, **_kwargs: Any) -> str:
    from whiskeyjack_bot.submission_gateway import GatewayError

    raise GatewayError("the submission artifact could not be written")


def run_submit_cell(
    cell: Cell,
    *,
    config_file: Path,
    record_id: str,
    payload_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    """Drive `submit` to one cell of the grid and return `(exit code, stdout)`.

    Two seams, both already faked by `test_cli_submit.py` and `test_submission_live.py`:
    the poster (`build_poster`), which decides whether the POST raises and what a refetch
    then sees, and `write_live_artifact`, whose failure `_write_receipt_artifact` degrades
    to `(None, reason)` by design. That degradation is M1-312's rule at its boundary -- a
    filesystem failure *after* an irreversible spend -- so simulating it is a reachable
    reliability condition, not an invented one.
    """
    from requests.exceptions import Timeout

    success, outcome, artifact = cell
    _install(
        monkeypatch,
        FakePoster(
            after=_after_question(outcome),
            post_error=None if success else Timeout("the POST timed out"),
        ),
    )
    if not artifact:
        monkeypatch.setattr("whiskeyjack_bot.submission_live.write_live_artifact", _refuse_artifact)
    exit_code = main(
        [
            "submit",
            "--config",
            str(config_file),
            "--record-id",
            record_id,
            "--payload-file",
            str(payload_file),
        ]
    )
    return exit_code, capsys.readouterr().out


def observed_cell(stdout: str) -> tuple[Cell, str]:
    """`((success, refetch_outcome, artifact written), result)` as `submit` printed them."""
    result = _RESULT_LINE.search(stdout)
    assert result is not None, f"no `result:` line in:\n{stdout}"
    artifact = _ARTIFACT_LINE.search(stdout)
    assert artifact is not None, f"no `artifact:` line in:\n{stdout}"
    written = not artifact.group(1).startswith("NOT WRITTEN")
    return (result.group(2) == "True", result.group(3), written), result.group(1)


@pytest.mark.parametrize("cell", sorted(grid_universe()), ids=repr)
def test_each_documented_grid_cell_is_what_the_cli_does(
    cell: Cell,
    config_file: Path,
    record_id: str,
    payload_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every cell, driven through the real command and compared to the document.

    The acceptance criteria's three named cases -- recorded uncertainty, recorded failure,
    and a confirmed post whose artifact was not written -- are cells of this grid rather
    than three separate tests, which is the point: a claim about a partition is checked by
    enumerating the inputs, not by re-reading the rows.

    The first assertion is the one that keeps the other from being vacuous. If the scripted
    poster stopped producing the cell this parameter names, every parameter would collapse
    onto whichever cell it did produce and still find a matching row.
    """
    exit_code, stdout = run_submit_cell(
        cell,
        config_file=config_file,
        record_id=record_id,
        payload_file=payload_file,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    driven, result = observed_cell(stdout)
    assert driven == cell, "the fakes did not produce the cell this parameter names"

    documented_result, documented_exit = documented_grid(runbook_text())[0][cell]
    assert result == documented_result
    assert exit_code == documented_exit


INSTRUCTION_PREFIX: Final = "the outcome is unresolved; run "


def documented_instruction_line(text: str) -> list[str]:
    """The runbook's quotation of `submit`'s last line, split at its placeholders.

    Compared segment-by-segment in order rather than as one string, because the document
    writes `<REC>` and `<ATTEMPT>` where the command prints real identifiers. Every segment
    must be non-empty: `str.find("")` answers `0` for any haystack, so an empty one would
    be a piece of the comparison that cannot fail.
    """
    quoted = [line for line in text.splitlines() if line.startswith(INSTRUCTION_PREFIX)]
    assert len(quoted) == 1, "docs/RUNBOOK.md must quote the instruction line exactly once"
    segments = re.split(r"<REC>|<ATTEMPT>", quoted[0])
    assert len(segments) == 3, "the quoted instruction line lost its placeholders"
    assert all(segments), "the quoted instruction line has an empty segment"
    return segments


def test_a_timeout_a_confirming_refetch_and_a_written_artifact_exits_zero(
    config_file: Path,
    record_id: str,
    payload_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cell D-1001 got wrong twice, named by the acceptance criteria.

    The POST raised, the refetch found the forecast on the platform, the artifact was
    written. The outcome is `submission_uncertain` -- the attempt and the platform were
    never observed together -- and the command exits `0` with a `verify-submission` still
    owed. An exit-code table keyed on the `result:` line cannot represent this row, which
    is why rounds 1 and 2 each fixed a row and were each still wrong.

    The instruction line is compared against the runbook's own quotation of it, so `cli.py`
    and the document cannot drift apart either.
    """
    exit_code, stdout = run_submit_cell(
        (False, "confirmed", True),
        config_file=config_file,
        record_id=record_id,
        payload_file=payload_file,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert observed_cell(stdout) == ((False, "confirmed", True), "submission_uncertain")
    assert exit_code == EXIT_OK

    remaining = stdout
    for segment in documented_instruction_line(runbook_text()):
        index = remaining.find(segment)
        assert index >= 0, f"the runbook quotes {segment!r} and `submit` did not print it"
        remaining = remaining[index + len(segment) :]


# ── the exit-code vocabulary and the quoted return ───────────────────────────


def test_the_exit_code_table_lists_the_codes_the_program_has() -> None:
    documented = {int(row[0]) for row in markdown_table(runbook_text(), header=("code", "meaning"))}
    assert documented == {EXIT_OK, EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_REFUSED}


def test_the_quoted_return_is_the_return() -> None:
    """The anchor that replaced `cli.py:735`.

    A line number is invalidated by any edit above it and says nothing about the line it
    names; the text of the return is the claim the section actually makes.
    """
    blocks = fenced_blocks(runbook_text(), "python")
    assert len(blocks) == 1, "docs/RUNBOOK.md quotes source in exactly one python block"

    source = {
        line.strip() for line in (SOURCE_ROOT / "cli.py").read_text(encoding="utf-8").splitlines()
    }
    for quoted in blocks[0]:
        assert quoted in source, f"docs/RUNBOOK.md quotes a line cli.py does not have: {quoted!r}"


# ── source citations ─────────────────────────────────────────────────────────

_CITATION = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_]*\.py):([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`"
)
CITATION_COUNT: Final = 15


def defined_symbols(path: Path) -> set[str]:
    """Top-level names in a module, plus `Class.member` one level in."""
    names: set[str] = set()

    def record(node: ast.AST, prefix: str = "") -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(prefix + node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                prefix + target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(prefix + node.target.id)

    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        record(node)
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                record(member, prefix=f"{node.name}.")
    return names


def test_every_source_citation_resolves() -> None:
    """`file.py:symbol`, not `file.py:line`.

    Ten of the eleven line-number citations this replaced were stale when T-908 was written,
    and one named a function that had moved out of the range entirely. A number is
    invalidated by any edit above the line it names, which is a check that reddens for the
    wrong reason and rots when it is not run at all; a symbol is invalidated only by the
    rename that actually breaks the reference.
    """
    citations = _CITATION.findall(runbook_text())
    assert len(citations) == CITATION_COUNT

    for filename, symbol in citations:
        candidates = sorted(SOURCE_ROOT.rglob(filename))
        assert len(candidates) == 1, f"`{filename}` names {len(candidates)} files under src/"
        assert symbol in defined_symbols(candidates[0]), f"{filename} defines no `{symbol}`"
