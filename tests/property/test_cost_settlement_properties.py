"""Properties of BYOK cost settlement and the past-spend correction (M1-348).

``settled_cost`` reads OpenRouter's ``usage`` -- provider JSON, untrusted -- and decides what
a model call settles at. ``correct_costs`` reads the same ``usage`` back out of the ledger --
also untrusted -- and corrects a reservation settled at 0. The properties:

1. ``settled_cost`` never raises on any JSON, and whatever it returns is a valid figure the
   ``usage`` actually carries: the upstream figure exactly when ``is_byok`` is exactly
   ``True``, ``usage.cost`` exactly when ``is_byok`` is absent or exactly ``False``, and
   nothing otherwise. So it never settles without a valid figure, and never reads a BYOK
   call's ``cost: 0`` as its price.
2. It is replay-stable across the persisted form (``canonical`` -> ``json.loads``).
3. The strategy reaches every branch, measured rather than assumed (the vacuous-property
   class, LESSONS.md lesson 5).
4. Through a real ledger: a correction run never raises outside ``TournamentError``, writes
   nothing without ``apply``, corrects exactly the reservations settled at 0 whose stored
   ``usage`` carries a valid upstream figure, at the figure a replay re-derives from what
   was stored, and a second run writes nothing.
5. ``spending()``'s actual figure never decreases over any append the writers can make.
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.tournament_state import (
    Budget,
    CostBasis,
    TournamentError,
    append,
    canonical,
    correct_costs,
    events,
    settled_cost,
    spending,
)

# --- Strategies -------------------------------------------------------------------------

# Every JSON leaf, including the ones a naive gate admits: bools (an `int` subclass), huge
# integers (`float()` overflows), NaN and infinities (`json.loads` accepts them), strings.
LEAVES = st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=5)
ANY_JSON = st.recursive(
    LEAVES,
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=5), children, max_size=3)
    ),
    max_leaves=8,
)
# A figure: mostly a valid price, often a hostile leaf.
FIGURE = st.one_of(
    st.floats(min_value=0, max_value=10),
    st.integers(min_value=0, max_value=100),
    st.just(0),
    st.just(1e305),
    LEAVES,
)
IS_BYOK = st.booleans() | LEAVES
COST_DETAILS = st.one_of(
    st.fixed_dictionaries({}, optional={"upstream_inference_cost": FIGURE}),
    st.fixed_dictionaries({"upstream_inference_cost": FIGURE}),
    ANY_JSON,
)
STRUCTURED = st.fixed_dictionaries(
    {},
    optional={"is_byok": IS_BYOK, "cost": FIGURE, "cost_details": COST_DETAILS},
)
VALID_FIGURE = st.floats(min_value=0, max_value=10) | st.integers(min_value=0, max_value=100)
# The live response shape: `is_byok` a real bool, both figures present and mostly valid. Added
# after measurement: STRUCTURED alone reached the valid-BYOK branch in 3 of 400 draws, which
# would have let every property here pass on refusals.
LIVE_SHAPE = st.builds(
    lambda byok, cost, upstream: {
        "is_byok": byok,
        "cost": cost,
        "cost_details": {"upstream_inference_cost": upstream},
    },
    st.booleans(),
    st.one_of(st.just(0), VALID_FIGURE, FIGURE),
    st.one_of(VALID_FIGURE, FIGURE),
)
# An explicit pick, because `st.one_of` does not weight by repetition: listing LIVE_SHAPE twice
# in it was measured at 17 valid-BYOK draws in 400.
USAGE = st.integers(0, 3).flatmap(lambda pick: (LIVE_SHAPE, LIVE_SHAPE, STRUCTURED, ANY_JSON)[pick])


def _valid(value: object) -> bool:
    """The oracle, written independently of the implementation's helper."""
    if type(value) is bool or type(value) not in (int, float):
        return False
    try:
        usd = float(value)  # type: ignore[arg-type]
    except OverflowError:
        return False
    return math.isfinite(usd) and usd >= 0 and math.isfinite(usd * 1_000_000)


def _branch(usage: object) -> str:
    """Which branch the oracle says ``usage`` belongs to."""
    if type(usage) is not dict:
        return "refused"
    byok = usage.get("is_byok", False)
    if byok is True:
        details = usage.get("cost_details")
        if type(details) is dict and _valid(details.get("upstream_inference_cost")):
            return "upstream_byok"
        return "refused"
    if byok is False and _valid(usage.get("cost")):
        return "openrouter"
    return "refused"


def _persistable(usage: object) -> bool:
    try:
        canonical(usage)
    except ValueError:
        return False
    return True


# --- 1-3. The settlement rule -----------------------------------------------------------


@given(usage=USAGE)
def test_the_settled_figure_is_exactly_the_one_the_usage_names(usage: object) -> None:
    result = settled_cost(usage)
    branch = _branch(usage)
    if branch == "refused":
        assert result is None
        return
    assert result is not None and type(usage) is dict
    value, basis = result
    assert basis == branch and basis in get_args(CostBasis)
    assert type(value) is float and math.isfinite(value) and value >= 0
    if basis == "upstream_byok":
        assert usage["is_byok"] is True
        assert value == float(usage["cost_details"]["upstream_inference_cost"])
    else:
        assert usage.get("is_byok", False) is False
        assert value == float(usage["cost"])


@given(usage=USAGE)
def test_the_settled_figure_is_replay_stable(usage: object) -> None:
    if not _persistable(usage):
        return
    assert settled_cost(json.loads(canonical(usage))) == settled_cost(usage)


def test_the_strategy_reaches_every_branch() -> None:
    """Measured: without this, every property above could pass on refusals alone."""
    hits: Counter[str] = Counter()

    @settings(max_examples=400, database=None, derandomize=True)
    @given(usage=USAGE)
    def draw(usage: object) -> None:
        result = settled_cost(usage)
        hits["refused" if result is None else result[1]] += 1
        if type(usage) is dict and usage.get("is_byok") is True and result is None:
            hits["byok_refused"] += 1
        if type(usage) is dict and usage.get("is_byok") is True and usage.get("cost") == 0:
            if result is not None:
                hits["byok_zero_cost_settled_upstream"] += 1

    draw()
    for branch in ("upstream_byok", "openrouter", "refused", "byok_refused"):
        assert hits[branch] >= 25, (branch, hits)
    assert hits["byok_zero_cost_settled_upstream"] >= 5, hits


# --- 4-5. Through a real ledger ---------------------------------------------------------


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("cost-settlement") / "template.sqlite3"
    initialize_ledger(path)
    return path


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("cost-settlement-work")


def _ledger(template: Path, workdir: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    root = workdir / uuid4().hex
    root.mkdir()
    shutil.copy(template, root / "ledger.sqlite3")
    conn = connect(root / "ledger.sqlite3")
    try:
        yield conn, root / "artifacts"
    finally:
        conn.close()
        shutil.rmtree(root)


SCOPE = "42:32977"
# One reservation: what it settles at (None = never settled), and its stored usage (None =
# no model_response, the Exa shape).
SETTLED_AT = st.integers(0, 3).flatmap(
    lambda pick: (st.just(0.0), st.just(0.0), st.none(), st.floats(0, 5))[pick]
)
# The live Astra shape the correction exists for: BYOK, `cost: 0`, a valid upstream figure.
BYOK_VALID = st.builds(
    lambda upstream: {
        "is_byok": True,
        "cost": 0,
        "cost_details": {"upstream_inference_cost": upstream},
    },
    VALID_FIGURE,
)
STORED_USAGE = st.integers(0, 3).flatmap(
    lambda pick: (BYOK_VALID, st.none(), USAGE.filter(_persistable), USAGE.filter(_persistable))[
        pick
    ]
)
RESERVATION = st.tuples(SETTLED_AT, STORED_USAGE)
RESERVATIONS = st.lists(RESERVATION, min_size=1, max_size=4)


def _expected(settled_at: float | None, usage: object) -> int | None:
    """The oracle for one reservation: what a correction should write, or None."""
    found = settled_cost(usage)
    if settled_at != 0.0 or found is None or found[1] != "upstream_byok":
        return None
    amount = math.ceil(found[0] * 1_000_000)
    return amount if amount > 0 else None


def test_the_ledger_strategy_reaches_corrections_and_refusals_together() -> None:
    """Measured: the first RESERVATION strategy had anything to correct in 7 of 150 draws."""
    hits: Counter[str] = Counter()

    @settings(max_examples=150, database=None, derandomize=True)
    @given(reservations=RESERVATIONS)
    def draw(reservations: list[tuple[float | None, object]]) -> None:
        found = [_expected(*reservation) for reservation in reservations]
        corrected = sum(amount is not None for amount in found)
        hits["with_correction"] += corrected > 0
        hits["mixed"] += 0 < corrected < len(found)

    draw()
    assert hits["with_correction"] >= 35 and hits["mixed"] >= 25, hits


def _rows(conn: sqlite3.Connection, kind: str) -> int:
    return int(
        conn.execute("SELECT count(*) FROM tournament_events WHERE kind=?", (kind,)).fetchone()[0]
    )


@settings(max_examples=150)
@given(reservations=RESERVATIONS)
def test_a_correction_is_exact_replay_stable_and_idempotent(
    template: Path, workdir: Path, reservations: list[tuple[float | None, object]]
) -> None:
    for conn, root in _ledger(template, workdir):
        budget = Budget(conn, root, SCOPE, 10**12)
        expected: dict[str, int] = {}
        for settled_at, usage in reservations:
            identifier = budget.reserve("openrouter", 1.0, {})
            if usage is not None:
                append(conn, "model_response", identifier, {"usage": usage})
            budget.settle(identifier, settled_at)
            amount = _expected(settled_at, usage)
            if amount is not None:
                expected[identifier] = amount
        before = spending(conn, SCOPE)
        rows = conn.execute("SELECT count(*) FROM tournament_events").fetchone()[0]

        dry = correct_costs(conn)
        assert {c.reservation_id: c.actual_microusd for c in dry.corrections} == expected
        assert dry.written == 0
        assert conn.execute("SELECT count(*) FROM tournament_events").fetchone()[0] == rows

        applied = correct_costs(conn, apply=True)
        assert applied.written == len(expected) == _rows(conn, "cost_corrected")
        # Replay-stable: each correction is what the rule re-derives from the stored form.
        for row in events(conn, "cost_corrected", SCOPE):
            (response,) = events(conn, "model_response", row["reservation_id"])
            replayed = settled_cost(response["usage"])
            assert replayed is not None and replayed[1] == "upstream_byok"
            assert row["actual_microusd"] == math.ceil(replayed[0] * 1_000_000)
        after = spending(conn, SCOPE)
        assert after == (before[0] + sum(expected.values()), before[1])

        again = correct_costs(conn, apply=True)
        assert again.written == 0 and again.corrections == ()
        assert again.already_corrected == len(expected)
        assert _rows(conn, "cost_corrected") == len(expected)
        assert spending(conn, SCOPE) == after


OPERATION = st.one_of(
    st.tuples(st.just("reserve"), st.just(0), st.none()),
    st.tuples(st.just("settle"), st.integers(0, 5), st.one_of(st.none(), st.floats())),
    st.tuples(st.just("response"), st.integers(0, 5), USAGE.filter(_persistable)),
    st.tuples(st.just("correct"), st.just(0), st.none()),
    st.tuples(st.just("raw_correction"), st.integers(0, 5), st.integers(0, 10**7)),
)


@settings(max_examples=150)
@given(operations=st.lists(OPERATION, max_size=12))
def test_actual_spending_never_decreases_over_appends(
    template: Path, workdir: Path, operations: list[tuple[str, Any, Any]]
) -> None:
    for conn, root in _ledger(template, workdir):
        budget = Budget(conn, root, SCOPE, 10**12)
        reservations: list[str] = []
        actual = 0
        for operation, index, value in operations:
            target = reservations[index % len(reservations)] if reservations else None
            try:
                if operation == "reserve":
                    reservations.append(budget.reserve("openrouter", 1.0, {}))
                elif target is None:
                    continue
                elif operation == "settle":
                    budget.settle(target, value)
                elif operation == "response":
                    append(conn, "model_response", target, {"usage": value})
                elif operation == "correct":
                    correct_costs(conn, apply=True)
                else:
                    append(
                        conn,
                        "cost_corrected",
                        SCOPE,
                        {"reservation_id": target, "actual_microusd": value},
                    )
            except TournamentError:
                pass
            now, _ = spending(conn, SCOPE)
            assert now >= actual
            actual = now
