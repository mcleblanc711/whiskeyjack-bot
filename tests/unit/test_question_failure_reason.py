"""M1-325: a ``question_failure`` row records why, not only which type.

Until M1-325 the row carried ``{"error_type": "TournamentError", "at": ...}`` for every refusal
inside a question's processing, so the live ledger's 2026-09-22 10:21 row could not say what
refused. The reason is now recorded for a ``TournamentError`` (module-owned and value-free by
contract, which the source scan below holds every raise site to) and withheld for anything
else. ``tournament status`` surfaces it.
"""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from hypothesis import event, given, strategies as st

from test_evidence_poor import _clock, _rows  # type: ignore[import-not-found]
from test_tournament import case, poll  # type: ignore[import-not-found]  # noqa: F401
from whiskeyjack_bot import tournament as whiskeyjack_tournament
from whiskeyjack_bot import tournament_state
from whiskeyjack_bot.lifecycle import PreForecastFailureCode
from whiskeyjack_bot.pipeline_live import QuestionStatus
from whiskeyjack_bot.tournament import (
    RECENT_QUESTION_FAILURES,
    _failed_outcome_reason,
    _question_failure_reason,
    status,
)
from whiskeyjack_bot.tournament_state import TournamentError

__all__ = ["case"]

ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "SENTINEL-7f3a-do-not-log"


def _reasons(conn: Any) -> list[str | None]:
    return [row.get("reason") for row in _rows(conn, "question_failure")]


# --- the three sites the criterion names ------------------------------------------------


def test_five_minutes_remaining_is_recorded(case: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Time runs out during research: the worker's gate passed, the pipeline's then refuses.

    The worker reads its clock once, at the top of the question; the pipeline re-reads it
    before buying a forecast. Research that took long enough is the reachable case, and two
    clocks are how it is simulated.
    """
    conn, _config, platform, _news, _model = case
    clock = _clock(case, monkeypatch)
    close = clock["now"] + timedelta(hours=8)
    later = close - timedelta(minutes=4)
    monkeypatch.setattr(tournament_state, "utcnow", lambda: later)
    assert poll(case)["heartbeat"]["failures"] == 1
    assert _reasons(conn) == ["less than five minutes remain; no new forecast purchased"]
    assert platform.posts == 0


def test_a_quality_gate_refusal_is_recorded_with_its_code(case: Any) -> None:
    """The M1-325 row's own example: the gate refused, and the ledger could not say so."""
    conn, _config, _platform, news, _model = case
    news.stale = True
    assert poll(case)["heartbeat"]["failures"] == 1
    assert _reasons(conn) == ["question research_failed (stale_evidence)"]


def test_an_unresolved_comment_is_recorded(case: Any) -> None:
    conn, _config, platform, _news, _model = case
    platform.lose_comment_response = True
    platform.hide_comments = True
    assert poll(case)["heartbeat"]["failures"] == 1
    assert _reasons(conn) == ["forecast or private comment remains unresolved"]


def test_the_three_reasons_are_distinguishable(case: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """One ledger, three refusals, three different recorded reasons -- and `status` shows them.

    Each is reproduced on a fresh question id so they sit side by side as they would live.
    """
    conn, _config, platform, news, _model = case
    clock = _clock(case, monkeypatch)
    base = platform.raw["question"]["id"]

    news.stale = True
    poll(case)
    news.stale = False

    platform.raw["question"]["id"] = base + 1
    platform.raw["id"] += 1
    real = tournament_state.utcnow
    later = clock["now"] + timedelta(hours=8) - timedelta(minutes=4)
    monkeypatch.setattr(tournament_state, "utcnow", lambda: later)
    poll(case)
    monkeypatch.setattr(tournament_state, "utcnow", real)

    # Last: once anything is posted, the fake platform reports a prior forecast for every
    # question it is asked about, so a question after this one would be skipped.
    platform.raw["question"]["id"] = base + 2
    platform.raw["id"] += 1
    platform.lose_comment_response = platform.hide_comments = True
    poll(case)

    reasons = _reasons(conn)
    assert len(reasons) == len(set(reasons)) == 3
    recent = status(conn, case[1])["recent_question_failures"]
    assert [entry["reason"] for entry in recent] == reasons[::-1], "newest first"
    assert {entry["scope"] for entry in recent} == {
        f"32977:{base}",
        f"32977:{base + 1}",
        f"32977:{base + 2}",
    }
    assert all(entry["error_type"] == "TournamentError" for entry in recent)


def test_any_other_exception_keeps_its_type_only(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A third-party message may quote a request or a body, so it is never recorded."""
    conn, _config, _platform, _news, _model = case

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"upstream said {SENTINEL}")

    monkeypatch.setattr(whiskeyjack_tournament, "_attempt_question", explode)
    assert poll(case)["heartbeat"]["failures"] == 1
    (row,) = _rows(conn, "question_failure")
    assert row["error_type"] == "RuntimeError" and row["reason"] is None
    stored = conn.execute("SELECT data FROM tournament_events").fetchall()
    assert not any(SENTINEL in data for (data,) in stored)
    assert SENTINEL not in json.dumps(status(conn, case[1]))


def test_status_reports_the_newest_few_and_old_rows_without_a_reason(case: Any) -> None:
    conn, config, *_ = case
    assert status(conn, config)["recent_question_failures"] == []
    # A row written before M1-325 has no `reason`; it reports None, which is what it said.
    tournament_state.append(
        conn, "question_failure", "32977:1", {"error_type": "TournamentError", "at": "t0"}
    )
    for index in range(RECENT_QUESTION_FAILURES + 2):
        tournament_state.append(
            conn,
            "question_failure",
            f"32977:{index + 2}",
            {"error_type": "TournamentError", "reason": f"r{index}", "at": f"t{index + 1}"},
        )
    recent = status(conn, config)["recent_question_failures"]
    assert len(recent) == RECENT_QUESTION_FAILURES
    assert recent[0]["reason"] == f"r{RECENT_QUESTION_FAILURES + 1}"
    assert recent[-1]["reason"] == "r2"


def test_status_reads_a_pre_m1_325_row_as_no_reason(case: Any) -> None:
    conn, config, *_ = case
    tournament_state.append(
        conn, "question_failure", "32977:1", {"error_type": "TournamentError", "at": "t0"}
    )
    assert status(conn, config)["recent_question_failures"] == [
        {"scope": "32977:1", "error_type": "TournamentError", "reason": None, "at": "t0"}
    ]


def test_a_malformed_stored_row_is_a_storage_failure(case: Any) -> None:
    conn, config, *_ = case
    with conn:
        conn.execute(
            "INSERT INTO tournament_events(event_id,kind,scope,data,created_at_utc) "
            "VALUES('x','question_failure','32977:1',?, 't')",
            (json.dumps([SENTINEL]),),
        )
    with pytest.raises(tournament_state.StorageFailure) as raised:
        status(conn, config)
    assert SENTINEL not in str(raised.value)


# --- the pure halves ---------------------------------------------------------------------

_STATUSES = list(get_args(QuestionStatus))
_CODES = list(get_args(PreForecastFailureCode))
_VOCABULARY = {
    f"question {s} ({c})"
    for s in [*_STATUSES, "unclassified"]
    for c in [*_CODES, "none", "unclassified"]
}


@given(
    status_=st.sampled_from(_STATUSES) | st.text(),
    code=st.none() | st.sampled_from(_CODES) | st.text(),
)
def test_the_outcome_reason_is_drawn_from_closed_vocabularies(
    status_: str, code: str | None
) -> None:
    """Never raises, never echoes: anything outside the two vocabularies is `unclassified`."""
    event("status in vocabulary" if status_ in _STATUSES else "status foreign")
    event("code none" if code is None else ("code in vocabulary" if code in _CODES else "foreign"))
    outcome = SimpleNamespace(status=status_, detail_code=code)
    reason = _failed_outcome_reason(outcome)  # type: ignore[arg-type]
    assert reason in _VOCABULARY
    if status_ in _STATUSES and (code is None or code in _CODES):
        assert reason == f"question {status_} ({code if code is not None else 'none'})"
    # The replay form: a recorded reason survives the journal's canonical encoding unchanged.
    assert json.loads(tournament_state.canonical({"reason": reason}))["reason"] == reason


@given(message=st.text())
def test_only_a_tournament_error_message_is_recorded(message: str) -> None:
    assert _question_failure_reason(TournamentError(message)) == message
    assert _question_failure_reason(tournament_state.StorageFailure(message)) == message
    for foreign in (RuntimeError(message), ValueError(message), KeyError(message)):
        assert _question_failure_reason(foreign) is None


# --- the contract the recorded message rests on -----------------------------------------

# The two raise sites that translate an already-sanitized error from another module by
# `str(exc)`. Anything else passed to a TournamentError constructor must be a literal.
_TRANSLATIONS = {
    ("tournament.py", "_unrecorded"),  # ReconciliationError
    ("tournament.py", "run_once"),  # PromptError
}
# Helpers whose output is built from closed vocabularies (property-tested above).
_VOCABULARY_HELPERS = {"_failed_outcome_reason"}


def _tournament_error_names() -> set[str]:
    names = set()
    pending = [TournamentError]
    while pending:
        current = pending.pop()
        names.add(current.__name__)
        pending.extend(current.__subclasses__())
    return names


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    owner: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner[child] = node.name  # innermost wins: inner defs are walked later
    return owner


def _is_constant_fstring(node: ast.AST) -> bool:
    return isinstance(node, ast.JoinedStr) and all(
        isinstance(part, ast.Constant)
        or (
            isinstance(part, ast.FormattedValue)
            and isinstance(part.value, ast.Name)
            and part.value.id.isupper()
        )
        for part in node.values
    )


def test_every_tournament_error_message_is_value_free_by_construction() -> None:
    """A source scan over every constructor call of every `TournamentError` subclass.

    `ActivationRetired` takes binding names and validates them against a closed Literal in
    its own `__init__`, so it is checked there rather than here.
    """
    names = _tournament_error_names() - {"ActivationRetired"}
    assert {"TournamentError", "StorageFailure", "ModelOutcomeUnknown"} <= names
    offenders = []
    seen = 0
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id not in names:
                continue
            seen += 1
            where = f"{path.relative_to(ROOT)}:{node.lineno}"
            if node.keywords or len(node.args) != 1:
                offenders.append(where)
                continue
            (arg,) = node.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                continue
            if _is_constant_fstring(arg):
                continue
            if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                if arg.func.id in _VOCABULARY_HELPERS:
                    continue
                if arg.func.id == "str" and (path.name, owner.get(node)) in _TRANSLATIONS:
                    continue
            offenders.append(where)
    assert seen >= 50, "the scan must actually find the raise sites"
    assert offenders == []
