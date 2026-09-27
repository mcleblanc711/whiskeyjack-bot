"""M1-354: the follow guard and its two parsers, fuzzed.

**The parsers are total.** ``parse_series_project`` reads a Metaculus answer and
``_series_from`` reads a series row back out of the ledger; both inputs are untrusted. The
first never raises; the second raises only ``StorageFailure``, with a message that names no
value.

**``rebound`` exactly when every guard holds.** Not "the verdict the code's own order
computes" -- an oracle restating that order proves only that the code agrees with itself
(M1-347's round-1 lesson). The property is independent of the order: the verdict is
``rebound`` iff all eleven conditions hold, and otherwise it names a condition that really
failed.

**The strategy reaches every verdict.** Each condition is drawn as a boolean and the inputs
are *built* from it, never drawn freely and filtered, so the all-true case -- the only one
that binds a project -- is drawn about one time in twelve rather than never.
``test_the_strategy_reaches_every_verdict`` proves it for each code with ``find``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, get_args

import hypothesis.strategies as st
import pytest
from hypothesis import find, given, settings

from whiskeyjack_bot.follow import FollowVerdict, SeriesProject, parse_series_project, verdict
from whiskeyjack_bot.tournament_state import (
    Series,
    StorageFailure,
    _series_from,
    canonical,
)

NOW = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)
BOUND = {"config_sha256": "c" * 64, "prompt_sha256": "p" * 64}

# The eleven conditions, each named by the verdict that reports it failing.
CONDITIONS = (
    "no_series",
    "account_mismatch",
    "series_disabled",
    "series_expired",
    "bindings_moved",
    "project_unreadable",
    "current",
    "not_the_series",
    "not_newer",
    "not_ongoing",
    "series_exhausted",
)


@dataclass(frozen=True)
class Drawn:
    holds: dict[str, bool]
    arguments: dict[str, Any]


@st.composite
def drawn(draw: st.DrawFn) -> Drawn:
    # Most conditions hold most of the time, so every single failure AND the all-hold case
    # are both common. `current` is the one whose "holding" means the project moved.
    holds = {name: draw(st.booleans() | st.just(True)) for name in CONDITIONS}
    current = draw(st.integers(min_value=2, max_value=10**6))
    ceiling = draw(st.integers(min_value=1, max_value=80_000_000))
    series = Series(
        series_id="s" * 32,
        account_id=7,
        follow="minibench",
        budget_microusd=ceiling,
        project_budget_microusd=ceiling,
        ends=NOW + timedelta(seconds=draw(st.integers(1, 10**7)))
        if holds["series_expired"]
        else NOW - timedelta(seconds=draw(st.integers(0, 10**7))),
        config_sha256=BOUND["config_sha256"],
        prompt_sha256=BOUND["prompt_sha256"] if holds["bindings_moved"] else "q" * 64,
    )
    step = draw(st.integers(min_value=1, max_value=1000))
    if not holds["current"]:
        project_id = current
    elif holds["not_newer"]:
        project_id = current + step
    else:
        project_id = max(1, current - step)
    close = (
        NOW + timedelta(seconds=draw(st.integers(1, 10**7)))
        if draw(st.booleans()) or holds["not_ongoing"]
        else NOW - timedelta(seconds=draw(st.integers(0, 10**7)))
    )
    ongoing = True if holds["not_ongoing"] else draw(st.booleans())
    if not holds["not_ongoing"] and ongoing and close > NOW:
        ongoing = False
    project = SeriesProject(
        project_id=project_id,
        slug="minibench" if holds["not_the_series"] else draw(st.sampled_from(["aibq3", ""])),
        close=close,
        ongoing=ongoing,
    )
    used = (
        draw(st.integers(0, ceiling - 1))
        if holds["series_exhausted"]
        else draw(st.integers(ceiling, 10 * ceiling))
    )
    arguments = {
        "series": series if holds["no_series"] else None,
        "disabled": not holds["series_disabled"],
        "account_id": 7 if holds["account_mismatch"] else 8,
        "bound": BOUND,
        "now": NOW,
        "current_project": current,
        "project": project if holds["project_unreadable"] else None,
        "series_used": used,
    }
    return Drawn(holds, arguments)


@settings(max_examples=500)
@given(drawn())
def test_rebound_exactly_when_every_guard_holds(case: Drawn) -> None:
    got = verdict(**case.arguments)
    assert got in get_args(FollowVerdict)
    if all(case.holds.values()):
        assert got == "rebound"
    else:
        assert got != "rebound"
        assert got in {name for name, held in case.holds.items() if not held}


@pytest.mark.parametrize("expected", [*CONDITIONS, "rebound"])
def test_the_strategy_reaches_every_verdict(expected: str) -> None:
    found = find(
        drawn(),
        lambda case: verdict(**case.arguments) == expected,
        settings=settings(max_examples=5000, database=None),
    )
    assert verdict(**found.arguments) == expected


_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=12,
)
# A valid value for each field, drawn most of the time, so the accept branch is common; junk
# (any JSON, a naive or unparsable date, a bool id) the rest. The first draft drew every
# field from one mixed strategy and `find` showed the accept branch was never reached.
_VALID: dict[str, st.SearchStrategy[Any]] = {
    "id": st.integers(min_value=1, max_value=10**6),
    "slug": st.text(max_size=12),
    "close_date": st.datetimes(timezones=st.just(timezone.utc)).map(datetime.isoformat),
    "is_ongoing": st.booleans(),
}
_JUNK = st.one_of(
    _JSON,
    st.integers(max_value=0),
    st.sampled_from(["2026-10-09T00:00:00", "not a date", True, False]),
)


@st.composite
def answers(draw: st.DrawFn) -> bytes:
    shape = draw(st.sampled_from(["fields", "fields", "json", "bytes"]))
    if shape == "bytes":
        return draw(st.binary(max_size=200))
    if shape == "json":
        return json.dumps(draw(_JSON)).encode()
    payload = {
        key: draw(valid if draw(st.integers(0, 5)) else _JUNK)
        for key, valid in _VALID.items()
        if draw(st.integers(0, 7))
    }
    return json.dumps(payload).encode()


@settings(max_examples=500)
@given(answers())
def test_the_answer_parser_is_total_and_exact(body: bytes) -> None:
    parsed = parse_series_project(body)
    if parsed is None:
        return
    payload = json.loads(body)
    assert type(parsed.project_id) is int and parsed.project_id == payload["id"] > 0
    assert type(parsed.slug) is str and parsed.slug == payload["slug"]
    assert type(parsed.ongoing) is bool and parsed.ongoing is payload["is_ongoing"]
    assert parsed.close.tzinfo is not None
    assert parsed.close == datetime.fromisoformat(payload["close_date"])


def test_the_answer_parser_reaches_its_accept_branch() -> None:
    found = find(answers(), lambda body: parse_series_project(body) is not None)
    assert parse_series_project(found) is not None


# Built per key like `answers`: a valid value most of the time, junk otherwise. One mixed
# strategy for every key never assembled a whole valid row, which `find` showed.
_ROW_VALID: dict[str, st.SearchStrategy[Any]] = {
    "series_id": st.text(max_size=8),
    "account_id": st.integers(min_value=-5, max_value=10**8),
    "follow": st.text(max_size=8),
    "budget_microusd": st.integers(min_value=-5, max_value=10**8),
    "project_budget_microusd": st.integers(min_value=-5, max_value=10**8),
    "ends": st.datetimes(timezones=st.just(timezone.utc)).map(datetime.isoformat)
    | st.sampled_from(["2026-10-20T00:00:00", "never"]),
    "config_sha256": st.text(max_size=8),
    "prompt_sha256": st.text(max_size=8),
}


@st.composite
def series_rows(draw: st.DrawFn) -> Any:
    if not draw(st.integers(0, 4)):
        return draw(_JSON)
    return {
        key: draw(valid if draw(st.integers(0, 7)) else _JSON)
        for key, valid in _ROW_VALID.items()
        if draw(st.integers(0, 9))
    }


@settings(max_examples=500)
@given(series_rows())
def test_a_series_row_parses_or_fails_as_storage_without_a_value(row: Any) -> None:
    try:
        parsed = _series_from(row)
    except StorageFailure as refused:
        assert str(refused) == "cannot read series authorization"
        assert refused.__suppress_context__ or refused.__cause__ is None
        return
    assert parsed.ends.tzinfo is not None
    assert parsed.budget_microusd > 0 and parsed.project_budget_microusd > 0
    assert type(parsed.account_id) is int


@given(
    st.integers(min_value=1, max_value=10**9),
    st.integers(min_value=1, max_value=80_000_000),
    st.integers(min_value=1, max_value=80_000_000),
    st.integers(min_value=0, max_value=10**8),
)
def test_a_series_row_survives_the_persisted_form(
    account: int, ceiling: int, project: int, seconds: int
) -> None:
    row = {
        "series_id": "s" * 32,
        "account_id": account,
        "follow": "minibench",
        "budget_microusd": ceiling,
        "project_budget_microusd": project,
        "ends": (NOW + timedelta(seconds=seconds)).isoformat(),
        **BOUND,
    }
    assert _series_from(json.loads(canonical(row))) == _series_from(row)


def test_the_series_row_strategy_reaches_its_accept_branch() -> None:
    def accepted(row: Any) -> bool:
        try:
            _series_from(row)
        except StorageFailure:
            return False
        return True

    assert accepted(
        find(series_rows(), accepted, settings=settings(max_examples=5000, database=None))
    )
