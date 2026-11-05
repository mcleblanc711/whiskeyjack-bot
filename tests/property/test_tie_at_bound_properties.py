"""Invariants of the message selector (M1-363).

Over generated percentile sets, open and closed bounds, with and without repeats at each bound:

- **the verdict did not move**: the conversion refuses exactly when the steps it is made of
  refuse. The oracle composes those steps directly, so it cannot see the selector;
- **the message is the selector's**: a refused reply that repeats a value at or beyond a bound
  gets the specific text, and every other refusal has none of it.

The expected shape is derived here from the values, independently of the code's helper.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings, strategies as st

from tests.unit.test_forecast_cdf import _calibration, _question, _with_values
from whiskeyjack_bot.forecast.cdf import (
    _TIE_AT_BOUND,
    _array_problems,
    _distribution,
    _standardization_can_converge,
    _values,
    numeric_cdf_or_problems,
)

LOWER, UPPER = 100.0, 1000.0


@st.composite
def _values_with_repeats(draw: Any) -> tuple[float, ...]:
    """Nine non-decreasing values, with a run on a bound drawn often."""
    tail = sorted(
        draw(
            st.lists(
                st.floats(min_value=LOWER - 50, max_value=UPPER + 500, allow_nan=False, width=32),
                min_size=9,
                max_size=9,
            )
        )
    )
    values = [float(value) for value in tail]
    shape = draw(st.integers(0, 4))
    run = draw(st.integers(2, 4))
    if shape == 1:
        values[:run] = [LOWER] * run
    elif shape == 2:
        values[-run:] = [UPPER] * run
    elif shape == 3:
        values[:run] = [LOWER - 10.0] * run
    elif shape == 4:
        middle = values[4]
        values[3 : 3 + run] = [middle] * run
    return tuple(sorted(values))


def _repeats_at_a_bound(values: tuple[float, ...]) -> bool:
    repeated = {value for value in values if values.count(value) > 1}
    return any(value <= LOWER or value >= UPPER for value in repeated)


@settings(deadline=None)
@given(
    values=_values_with_repeats(),
    open_lower=st.booleans(),
    open_upper=st.booleans(),
)
def test_the_selector_picks_a_message_and_never_a_verdict(
    values: tuple[float, ...], open_lower: bool, open_upper: bool
) -> None:
    calibration = _calibration()
    question = _question(
        lower_bound=LOWER,
        upper_bound=UPPER,
        open_lower_bound=open_lower,
        open_upper_bound=open_upper,
    )
    forecast = _with_values(*values)

    converted, problems = numeric_cdf_or_problems(forecast, calibration, question)

    distribution = _distribution(forecast, calibration, question)
    refused_by_the_steps = (
        distribution is None
        or not _standardization_can_converge(distribution)
        or (array := _values(distribution)) is None
        or bool(_array_problems(array, calibration, question))
    )
    assert (converted is None) is refused_by_the_steps
    if converted is None:
        named = any(_TIE_AT_BOUND in problem for problem in problems)
        assert named is _repeats_at_a_bound(values)
    else:
        assert problems == []
