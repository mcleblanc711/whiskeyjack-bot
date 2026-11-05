"""Invariants of the message selector (M1-363).

Over generated percentile sets, open and closed bounds, with and without repeats at each bound:

- **the verdict did not move**: the conversion refuses exactly when the steps it is made of
  refuse. The oracle composes those steps directly, so it cannot see the selector;
- **the selector is the written rule**, tested directly as well as through the conversion;
- **the message is the selector's**: a refused reply that repeats a value at or beyond a bound
  gets the specific text, and every other refusal has none of it.

The expected shape is derived here from the values, independently of the code's helper.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import yaml
from hypothesis import example, given, settings, strategies as st

from whiskeyjack_bot.config import NumericCalibrationConfig, validate_config_data
from whiskeyjack_bot.forecast.schema import NumericForecastResponse, validate_forecast_response
from whiskeyjack_bot.questions.model import CanonicalNumericQuestion
from whiskeyjack_bot.forecast.cdf import (
    _TIE_AT_BOUND,
    _array_problems,
    _distribution,
    _repeats_a_value_at_a_bound,
    _standardization_can_converge,
    _values,
    numeric_cdf_or_problems,
)

LOWER, UPPER = 100.0, 1000.0
LEVELS = (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)
REPO_ROOT = Path(__file__).resolve().parents[2]
_PROMPT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")


def _calibration() -> NumericCalibrationConfig:
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    return validate_config_data(data).numeric_calibration


CALIBRATION = _calibration()


def _json_block(heading: str) -> str:
    match = re.search(r"```json\n(.*?)\n```", _PROMPT.split(f"\n## {heading}\n", 1)[1], re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _forecast(values: tuple[float, ...]) -> NumericForecastResponse:
    payload: dict[str, Any] = json.loads(_json_block("Shared fields"))
    payload["model_prior"] = None
    payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    payload["question_id"] = 46395
    payload["question_type"] = "numeric"
    payload["final_prediction"] = {
        "percentiles": [
            {"percentile": level, "value": value}
            for level, value in zip(LEVELS, values, strict=True)
        ]
    }
    return validate_forecast_response(payload, NumericForecastResponse)


@st.composite
def _values_with_repeats(draw: Any) -> tuple[float, ...]:
    """Nine values, weighted toward what the conversion refuses.

    Refused replies are rare among uniform draws, and the assertion about the message is only
    reached on a refusal. So a run sits on or beyond a bound with a tail of random width, which
    is q46395's shape, or eight values sit inside the range with one lone value on a bound,
    which is refused and must not be named: the lone value is not repeated.
    """
    shape = draw(st.integers(0, 2))
    if shape == 0:
        drawn = draw(
            st.lists(
                st.floats(min_value=LOWER - 50, max_value=UPPER + 500, allow_nan=False, width=32),
                min_size=9,
                max_size=9,
            )
        )
        return tuple(sorted(float(value) for value in drawn))
    if shape == 1:
        anchor = draw(st.sampled_from([LOWER, UPPER, LOWER - 10.0, UPPER + 10.0]))
        run = draw(st.integers(2, 9))
        width = draw(st.sampled_from([1.0, 5.0, 30.0, 300.0, 900.0, 3000.0]))
        tail = draw(
            st.lists(st.floats(-1.0, 1.0, allow_nan=False, width=32), min_size=9 - run, max_size=9)
        )
        return tuple(sorted([anchor] * run + [anchor + width * float(t) for t in tail[: 9 - run]]))
    lone = draw(st.sampled_from([LOWER, UPPER]))
    middle = draw(st.sampled_from([500.0, 123.25, 987.5]))
    return tuple(sorted([lone] + [middle] * 8))


def _repeats_at_a_bound(values: tuple[float, ...]) -> bool:
    repeated = {value for value in values if values.count(value) > 1}
    return any(value <= LOWER or value >= UPPER for value in repeated)


@settings(deadline=None)
@example(values=(LOWER,) * 9, open_lower=False, open_upper=False)
@example(values=(UPPER,) * 9, open_lower=False, open_upper=False)
@example(values=(LOWER, *(500.0,) * 8), open_lower=False, open_upper=False)
@example(values=(*(500.0,) * 8, UPPER), open_lower=False, open_upper=False)
@example(values=(500.0,) * 9, open_lower=False, open_upper=False)
@given(
    values=_values_with_repeats(),
    open_lower=st.booleans(),
    open_upper=st.booleans(),
)
def test_the_selector_picks_a_message_and_never_a_verdict(
    values: tuple[float, ...], open_lower: bool, open_upper: bool
) -> None:
    calibration = CALIBRATION
    question = CanonicalNumericQuestion(
        question_id=46395,
        post_id=46166,
        title="How much?",
        resolution_criteria="Resolves to the amount.",
        lower_bound=LOWER,
        upper_bound=UPPER,
        open_lower_bound=open_lower,
        open_upper_bound=open_upper,
        cdf_size=201,
    )
    forecast = _forecast(values)

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


@settings(deadline=None)
@example(values=(*(500.0,) * 7, UPPER, UPPER))
@example(values=(*(500.0,) * 7, UPPER + 10.0, UPPER + 10.0))
@example(values=(LOWER, LOWER, *(500.0,) * 7))
@example(values=(LOWER - 10.0, LOWER - 10.0, *(500.0,) * 7))
@example(values=(LOWER, *(500.0,) * 8))
@example(values=(*(500.0,) * 8, UPPER))
@example(values=(500.0,) * 9)
@given(values=_values_with_repeats())
def test_the_selector_is_exactly_the_written_rule(values: tuple[float, ...]) -> None:
    """Direct, because a refused reply repeating a value at the upper bound was not found in a
    search of the conversion, so the first property cannot reach that half of the rule."""
    question = CanonicalNumericQuestion(
        question_id=46395,
        post_id=46166,
        title="How much?",
        resolution_criteria="Resolves to the amount.",
        lower_bound=LOWER,
        upper_bound=UPPER,
        open_lower_bound=True,
        open_upper_bound=True,
        cdf_size=201,
    )
    assert _repeats_a_value_at_a_bound(_forecast(values), question) is _repeats_at_a_bound(values)
