"""Properties of the discrete CDF conversion (M1-205).

``forecast/cdf.py`` converted numeric questions against two constants: a 201-point length
and ``max_adjacent_pmf``. A discrete question brings its own length -- ``cdf_size`` is
``inbound_outcome_count + 1`` and ranges from 2 up -- and needs its own per-step cap,
because on a grid of sixteen outcomes two adjacent CDF points *are* two outcomes and the
numeric cap refuses ordinary confident forecasts.

Both of those are numbers now reached through a question rather than written down, which
is exactly the shape that fails silently: a wrong length is refused loudly by Metaculus,
but a wrong *cap* just converts a confident forecast into a repair turn and then a
failure, and nothing in the ledger would say why. So the properties below are about the
question's own declaration deciding the outcome, not about any particular number.

The payload is built from ``prompts/forecaster.md`` for the reason
``tests/unit/test_forecast_cdf.py`` gives: a response shape transcribed into a test is one
the prompt can drift away from without anything failing.
"""

from __future__ import annotations

import json
import math
import re
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from whiskeyjack_bot.config import NumericCalibrationConfig, validate_config_data
from whiskeyjack_bot.forecast.cdf import (
    NumericCdfError,
    expected_cdf_points_for,
    numeric_cdf_or_problems,
)
from whiskeyjack_bot.forecast.numeric import DECLARED_PERCENTILE_LEVELS
from whiskeyjack_bot.forecast.schema import NumericForecastResponse, validate_forecast_response
from whiskeyjack_bot.questions.model import CanonicalDiscreteQuestion, CanonicalNumericQuestion

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")

QUESTION_ID = 123
POST_ID = 456

# A secret-shaped marker planted in the question's own text. Nothing this module produces
# may render it: the no-leak rule is about question and response *content*, and a question
# title is content.
SECRET = "s3cr3t-canary-value"


def _json_block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _committed_calibration() -> NumericCalibrationConfig:
    """The *committed* ``config.example.yaml``, not a hand-built calibration.

    The model name is the one placeholder the committed file deliberately refuses to
    validate with (D27: no silent default), so it is filled in here exactly as
    ``tests/unit/test_forecast_cdf.py`` fills it. Everything this module reads --
    the two caps and the point count -- comes from the file.
    """
    raw = json.loads(
        json.dumps(yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8")))
    )
    raw["model"]["name"] = "openrouter/test-model"
    return validate_config_data(raw).numeric_calibration


def _calibration(**overrides: Any) -> NumericCalibrationConfig:
    committed = _committed_calibration()
    return NumericCalibrationConfig(**{**committed.model_dump(), **overrides})


def _response(values: tuple[float, ...]) -> NumericForecastResponse:
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block("Numeric schema") + "}"),
    }
    payload["model_prior"] = None
    payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    payload["question_id"] = QUESTION_ID
    payload["final_prediction"] = {
        "percentiles": [
            {"percentile": level, "value": value}
            for level, value in zip(DECLARED_PERCENTILE_LEVELS, values, strict=True)
        ]
    }
    return validate_forecast_response(payload, NumericForecastResponse)


def _discrete_question(outcomes: int, **overrides: Any) -> CanonicalDiscreteQuestion:
    """A Metaculus-shaped discrete question over ``outcomes`` integers 0..outcomes-1.

    The half-open bounds are not decoration: this is exactly how Metaculus scales a
    discrete question (post 45559 arrives as ``range_min: -0.5, range_max: 15.5``), and a
    test that used integer bounds would be converting a question the platform never sends.
    """
    fields: dict[str, Any] = {
        "question_id": QUESTION_ID,
        "post_id": POST_ID,
        "title": f"How many things, {SECRET}?",
        "resolution_criteria": f"Resolves to the count. {SECRET}",
        "lower_bound": -0.5,
        "upper_bound": outcomes - 0.5,
        "open_lower_bound": False,
        "open_upper_bound": False,
        "cdf_size": outcomes + 1,
    }
    fields.update(overrides)
    return CanonicalDiscreteQuestion(**fields)


@st.composite
def _grids(draw: st.DrawFn) -> tuple[CanonicalDiscreteQuestion, NumericForecastResponse]:
    """A discrete question and a well-formed nine-percentile reply inside its range.

    Outcome counts stay modest so the property suite stays fast; the two real shapes this
    item was built against (17 and 72 points) are pinned as goldens in
    ``tests/unit/test_discrete_cdf_golden.py`` rather than left to a draw to happen upon.
    """
    outcomes = draw(st.integers(min_value=3, max_value=40))
    lower, upper = -0.5, outcomes - 0.5
    values = draw(
        st.lists(
            st.floats(
                min_value=lower,
                max_value=upper,
                allow_nan=False,
                allow_infinity=False,
                width=32,
            ),
            min_size=len(DECLARED_PERCENTILE_LEVELS),
            max_size=len(DECLARED_PERCENTILE_LEVELS),
        )
    )
    ordered = tuple(sorted(values))
    # The SDK refuses a distribution with no spread at all; that is a *reply* problem and
    # `tests/unit/test_forecast_cdf.py` owns it. These properties are about what a
    # convertible reply produces.
    assume(ordered[0] < ordered[-1])
    return _discrete_question(outcomes), _response(ordered)


# The three shape properties below deliberately convert under a cap that cannot filter
# anything (``1.0``). The cap has its own property further down, and leaving it live here
# would make these three quietly conditional on it: under a restrictive cap most draws
# convert to ``None``, the ``assume`` discards them, and the properties either prove
# nothing or fail by assumption-exhaustion rather than by assertion. Watched happen --
# mutating ``_cdf_rules`` to the numeric cap made the monotonicity property fail for that
# reason and not for its own.
_SHAPE_ONLY = {"discrete_max_adjacent_pmf": 1.0}


@given(_grids())
@settings(deadline=None)
def test_a_discrete_cdf_has_exactly_the_length_the_question_declared(
    case: tuple[CanonicalDiscreteQuestion, NumericForecastResponse],
) -> None:
    """The length rule is the question's ``cdf_size``, not a configured constant.

    The vacuity guard is the assertion on ``cdf is not None``: a strategy that only ever
    produced unconvertible replies would make every clause below true about nothing, which
    is this project's most-repeated property defect.
    """
    question, forecast = case
    cdf, problems = numeric_cdf_or_problems(forecast, _calibration(**_SHAPE_ONLY), question)
    assume(cdf is not None)
    assert cdf is not None
    assert problems == []
    assert len(cdf.values) == question.cdf_size
    assert question.cdf_size != _calibration().expected_cdf_points, (
        "a draw equal to the numeric 201 would make this property unable to tell the "
        "question's declaration from the configured constant"
    )


@given(_grids())
@settings(deadline=None)
def test_a_discrete_cdf_is_non_decreasing_and_within_the_unit_interval(
    case: tuple[CanonicalDiscreteQuestion, NumericForecastResponse],
) -> None:
    question, forecast = case
    cdf, _ = numeric_cdf_or_problems(forecast, _calibration(**_SHAPE_ONLY), question)
    assume(cdf is not None)
    assert cdf is not None
    assert all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in cdf.values)
    assert all(first <= second for first, second in pairwise(cdf.values))


@given(_grids())
@settings(deadline=None)
def test_closed_bounds_pin_both_endpoints(
    case: tuple[CanonicalDiscreteQuestion, NumericForecastResponse],
) -> None:
    """Both bounds closed, so the array must start at 0 and end at 1.

    This is the rule the live MiniBench question could not exercise -- post 45559 has an
    open upper bound -- and the reason the bot-testing-area question 43321, whose bounds
    are both closed, is the first target of the live verification.
    """
    question, forecast = case
    assert not question.open_lower_bound and not question.open_upper_bound
    cdf, _ = numeric_cdf_or_problems(forecast, _calibration(**_SHAPE_ONLY), question)
    assume(cdf is not None)
    assert cdf is not None
    assert cdf.values[0] == 0.0
    assert cdf.values[-1] == 1.0


@given(_grids())
@settings(deadline=None)
def test_conversion_never_raises_outside_this_modules_own_error(
    case: tuple[CanonicalDiscreteQuestion, NumericForecastResponse],
) -> None:
    """ "Every malformed shape arrives as the module's own error type", for discrete.

    A raw ``ValueError`` or ``ValidationError`` out of the SDK is a review finding this
    project has taken twice, and the discrete path reaches the same third-party conversion
    by a new route.
    """
    question, forecast = case
    try:
        numeric_cdf_or_problems(forecast, _calibration(), question)
    except NumericCdfError:
        pass  # the module's own type, including its timeout subclass


@given(_grids())
@settings(deadline=None)
def test_no_problem_message_renders_question_or_response_content(
    case: tuple[CanonicalDiscreteQuestion, NumericForecastResponse],
) -> None:
    """Problem strings are safe to log, store and send back as a repair turn."""
    question, forecast = case
    _, problems = numeric_cdf_or_problems(forecast, _calibration(), question)
    joined = " ".join(problems)
    assert SECRET not in joined
    for value in (p.value for p in forecast.final_prediction.percentiles):
        assert repr(value) not in joined


# --- the wiring property ---------------------------------------------------------------
#
# The three above would all still pass if `_cdf_rules` returned the numeric cap for a
# discrete question, because they never build a forecast concentrated enough to reach it.
# This one is the difference, and it is written as a *comparison between the two caps on
# one array* rather than against a hard-coded number, so it cannot drift when either
# default is retuned.


def _confident_reply(outcomes: int) -> NumericForecastResponse:
    """A reply that puts most of its mass on one outcome -- an ordinary discrete forecast."""
    mode = outcomes // 2
    return _response(
        (
            float(mode - 1),
            float(mode - 1),
            float(mode),
            float(mode),
            float(mode),
            float(mode),
            float(mode),
            float(mode + 1),
            float(mode + 1),
        )
    )


def test_the_discrete_cap_admits_a_confident_forecast_the_numeric_cap_refuses() -> None:
    """The item's load-bearing behaviour, and the mutation this suite exists to catch.

    Concentrating probability on the modal outcome is what a confident discrete forecast
    *is*. Under the numeric cap it is refused as a malformed spike, and the refusal is not
    free: it becomes a repair turn, i.e. a second billed model call, before failing anyway.

    Asserted as one array measured against both caps, so the test states the relationship
    rather than a number either default could move away from. If ``_cdf_rules`` is mutated
    to return ``max_adjacent_pmf`` for a discrete question, the first assertion fails.
    """
    outcomes = 16
    question = _discrete_question(outcomes)
    forecast = _confident_reply(outcomes)

    cdf, problems = numeric_cdf_or_problems(forecast, _calibration(), question)
    assert cdf is not None, f"the discrete cap must admit a confident forecast: {problems}"

    steps = [second - first for first, second in pairwise(cdf.values)]
    committed = _committed_calibration()
    assert max(steps) > committed.max_adjacent_pmf, (
        "vacuity guard: this reply must actually exceed the numeric cap, or the test "
        "proves nothing about which cap was applied"
    )
    assert max(steps) <= committed.discrete_max_adjacent_pmf

    # And the same array, converted under the numeric cap, is refused -- so the difference
    # is the cap and not something else about the reply.
    under_numeric = _calibration(discrete_max_adjacent_pmf=committed.max_adjacent_pmf)
    refused, refusals = numeric_cdf_or_problems(forecast, under_numeric, question)
    assert refused is None
    assert any("adjacent" in problem or "concentrates" in problem for problem in refusals)


@pytest.mark.parametrize("cdf_size", [1, 0, -1, 202, 10_000])
def test_a_discrete_cdf_size_outside_the_envelope_is_refused_as_a_caller_mistake(
    cdf_size: int,
) -> None:
    """An unbounded ``cdf_size`` is an unbounded allocation inside the SDK.

    It arrives from the Metaculus scaling block, which the threat model treats as
    untrusted, so the envelope is checked before anything converts -- and as a raise
    rather than a repair turn, because no percentile set could fix it.
    """
    question = _discrete_question(4, cdf_size=cdf_size)
    with pytest.raises(NumericCdfError) as caught:
        numeric_cdf_or_problems(
            _response(tuple(float(i) / 4 for i in range(9))), _calibration(), question
        )
    assert all(SECRET not in problem for problem in caught.value.problems)


def test_the_length_rule_differs_between_the_two_types_for_one_calibration() -> None:
    """``expected_cdf_points_for`` is the single source both the conversion and the
    submission preflight read, so this pins that they cannot disagree per type."""
    calibration = _calibration()
    numeric = CanonicalNumericQuestion(
        question_id=QUESTION_ID,
        post_id=POST_ID,
        title="How many things?",
        lower_bound=0.0,
        upper_bound=100.0,
        open_lower_bound=False,
        open_upper_bound=False,
        cdf_size=201,
    )
    assert expected_cdf_points_for(numeric, calibration) == 201
    assert expected_cdf_points_for(_discrete_question(16), calibration) == 17
    assert expected_cdf_points_for(_discrete_question(71), calibration) == 72
