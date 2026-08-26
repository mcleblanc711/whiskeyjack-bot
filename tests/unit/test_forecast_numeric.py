"""M1-405 acceptance: the nine declared percentiles, ordered and compatible with bounds.

*"Percentile levels are exact; values are finite, ordered and compatible with question
bounds."* Four clauses, tested four different ways on purpose:

- **exact levels** against the prompt's own fenced JSON, so the tuple in ``numeric.py``
  and the levels the model is actually told to return cannot drift apart;
- **finite** is *not* re-tested here as a rule of this module, because it is not one --
  ``schema.PercentilePoint.value`` carries ``allow_inf_nan=False`` and refuses a non-finite
  value before any checker sees it. It is pinned as a schema fact instead
  (``test_a_non_finite_value_never_reaches_this_module``), which is a test that can fail;
  a duplicate check here could only be tested through ``model_construct``, and a property
  that cannot be reached from any real input is the vacuity class this project has paid
  for more than any other;
- **ordered** at the boundary between equal and decreasing, both directions;
- **compatible with question bounds** at each bound exactly, one ulp outside it, and with
  the bound open as well as closed -- the open case being the one that proves the rule is
  keyed on ``open_lower_bound``/``open_upper_bound`` rather than applied unconditionally.

The comprehensive valid/invalid golden set is Codex's **T-901**, authored blind from spec.
This file builds its payloads out of ``prompts/forecaster.md`` rather than a new fixture,
the ``test_forecast_validate.py`` approach: for a rule whose whole content is "what the
prompt told the model to do", the prompt is the fixture.
"""

import copy
import json
import re
from math import nextafter
from pathlib import Path
from typing import Any

import pytest
import yaml

from whiskeyjack_bot.config import ForecastConfig, validate_config_data
from whiskeyjack_bot.forecast.numeric import (
    DECLARED_PERCENTILE_LEVELS,
    NumericOutputError,
    numeric_output_problems,
    validate_numeric_output,
)
from whiskeyjack_bot.forecast.schema import (
    ForecastSchemaError,
    NumericForecastResponse,
    response_model_for,
    validate_forecast_response,
)
from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalMultipleChoiceQuestion,
    CanonicalNumericQuestion,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")

QUESTION_ID = 123
POST_ID = 456


def _committed_forecast_config() -> ForecastConfig:
    """The forecast section of the *committed* config.example.yaml, not a hand-built one.

    ``test_forecast_binary.py``'s helper, for its reason: a test that builds its own config
    cannot notice the committed defaults drifting away from what the prompt prints to the
    model.
    """
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    return validate_config_data(data).forecast


def _json_block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _payload(**overrides: Any) -> dict[str, Any]:
    """The prompt's shared fields composed with its numeric prediction block."""
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block("Numeric schema") + "}"),
    }
    # The prompt's own rule: a non-binary response nulls both priors.
    payload["model_prior"] = None
    payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    payload["question_id"] = QUESTION_ID
    payload.update(overrides)
    return payload


def _response(**overrides: Any) -> NumericForecastResponse:
    forecast = validate_forecast_response(_payload(**overrides), NumericForecastResponse)
    return forecast


def _with_values(*values: float) -> NumericForecastResponse:
    """A response carrying the declared levels and the supplied values, in order."""
    assert len(values) == len(DECLARED_PERCENTILE_LEVELS)
    return _response(
        final_prediction={
            "percentiles": [
                {"percentile": level, "value": value}
                for level, value in zip(DECLARED_PERCENTILE_LEVELS, values, strict=True)
            ]
        }
    )


def _question(**overrides: Any) -> CanonicalNumericQuestion:
    fields: dict[str, Any] = {
        "question_id": QUESTION_ID,
        "post_id": POST_ID,
        "title": "How many things?",
        "resolution_criteria": "Resolves to the number of things.",
        "lower_bound": 0.0,
        "upper_bound": 100.0,
        "open_lower_bound": False,
        "open_upper_bound": False,
        "cdf_size": 201,
    }
    fields.update(overrides)
    return CanonicalNumericQuestion(**fields)


def _problems(forecast: NumericForecastResponse, **question_overrides: Any) -> list[str]:
    return numeric_output_problems(
        forecast, _committed_forecast_config(), _question(**question_overrides)
    )


# --- the acceptance criterion, clause by clause ------------------------------------


def test_the_prompts_own_numeric_example_passes() -> None:
    """The criterion's baseline: a reply that followed the prompt exactly is accepted.

    The prompt prints 10..50 against a question this file bounds at 0..100 closed, so every
    clause is exercised and none bites. A rule that failed the prompt's own example would be
    a rule no model could satisfy.
    """
    assert _problems(_response()) == []


def test_the_declared_levels_are_the_prompts_own() -> None:
    """The drift guard. ``numeric.py`` transcribes the levels; this proves the transcription.

    Parsed out of the fenced JSON rather than restated, so an edit to ``prompts/forecaster.md``
    that changes a level without changing the tuple fails here. That is the whole reason the
    module does not read the prompt at import time -- the check belongs in a test, not on the
    replay path.
    """
    printed = tuple(
        point["percentile"]
        for point in json.loads("{" + _json_block("Numeric schema") + "}")["final_prediction"][
            "percentiles"
        ]
    )
    assert DECLARED_PERCENTILE_LEVELS == printed
    assert len(DECLARED_PERCENTILE_LEVELS) == 9


@pytest.mark.parametrize(
    "levels",
    [
        pytest.param(DECLARED_PERCENTILE_LEVELS[:-1], id="one missing"),
        pytest.param((*DECLARED_PERCENTILE_LEVELS, 0.995), id="one extra"),
        pytest.param(
            (0.05, 0.01, *DECLARED_PERCENTILE_LEVELS[2:]),
            id="right set, wrong order",
        ),
        pytest.param(
            (0.01, 0.01, *DECLARED_PERCENTILE_LEVELS[2:]),
            id="a duplicate",
        ),
        pytest.param(
            (0.02, *DECLARED_PERCENTILE_LEVELS[1:]),
            id="a level the prompt does not print",
        ),
    ],
)
def test_the_levels_must_be_exactly_the_declared_ones(levels: tuple[float, ...]) -> None:
    """Count, membership, order and duplication, all through the one tuple comparison."""
    forecast = _response(
        final_prediction={
            "percentiles": [
                {"percentile": level, "value": 10.0 + index} for index, level in enumerate(levels)
            ]
        }
    )
    problems = _problems(forecast)
    assert [p for p in problems if "declared levels" in p] != []


def test_a_level_written_another_way_is_the_same_double() -> None:
    """``0.10``, ``0.1`` and ``1e-1`` parse to one value, so exact equality is not brittle.

    This is why the levels are compared exactly rather than within a tolerance: the only
    thing a tolerance would buy is accepting levels the prompt does not print.
    """
    written = json.loads("[0.10, 0.1, 1e-1]")
    assert written[0] == written[1] == written[2] == DECLARED_PERCENTILE_LEVELS[2]


def test_a_non_finite_value_never_reaches_this_module() -> None:
    """*"values are finite"* is the schema's rule, and this is where it is pinned.

    ``numeric.py`` deliberately does not re-check it: ``PercentilePoint.value`` carries
    ``allow_inf_nan=False``, so a non-finite value is refused before any checker is called,
    and a duplicate check here would be unreachable from every real input.
    """
    for bad in ("Infinity", "-Infinity", "NaN"):
        with pytest.raises(ForecastSchemaError):
            validate_forecast_response(
                _payload(
                    final_prediction={
                        "percentiles": [
                            {"percentile": level, "value": json.loads(bad) if index else 1.0}
                            for index, level in enumerate(DECLARED_PERCENTILE_LEVELS)
                        ]
                    }
                ),
                NumericForecastResponse,
            )


# --- ordering ------------------------------------------------------------------------


def test_equal_neighbouring_values_are_accepted() -> None:
    """Non-decreasing, which is the prompt's word in both places it states the rule.

    A tie is what the SDK's ``_check_percentiles_increasing`` also tolerates. Refusing it
    would spend the one budgeted repair call rejecting a reply that followed the prompt.
    """
    assert _problems(_with_values(10, 12, 14, 18, 24, 24, 38, 42, 50)) == []
    assert _problems(_with_values(10, 10, 10, 10, 10, 10, 10, 10, 10)) == []


def test_one_decreasing_step_anywhere_is_refused() -> None:
    ordered = [10.0, 12.0, 14.0, 18.0, 24.0, 31.0, 38.0, 42.0, 50.0]
    for index in range(len(ordered) - 1):
        swapped = list(ordered)
        swapped[index], swapped[index + 1] = swapped[index + 1], swapped[index]
        problems = _problems(_with_values(*swapped))
        assert [p for p in problems if "non-decreasing" in p] != [], index


def test_the_ordering_rule_is_about_values_not_levels() -> None:
    """A descending value list with correct levels gets the ordering problem, not the level
    one -- the two rules are independent and both are reported."""
    problems = _problems(_with_values(50, 42, 38, 31, 24, 18, 14, 12, 10))
    assert [p for p in problems if "declared levels" in p] == []
    assert [p for p in problems if "non-decreasing" in p] != []


# --- bounds --------------------------------------------------------------------------


def test_a_value_exactly_on_a_closed_bound_is_accepted() -> None:
    """Inclusive on both ends, the SDK's own reading of a closed bound: ``get_cdf`` puts
    ``cdf[-1] = P(outcome <= upper_bound)``, so the bound itself is representable."""
    assert _problems(_with_values(0, 12, 14, 18, 24, 31, 38, 42, 100)) == []


def test_one_ulp_outside_a_closed_bound_is_refused() -> None:
    below = nextafter(0.0, -1.0)
    above = nextafter(100.0, float("inf"))
    low_problems = _problems(_with_values(below, 12, 14, 18, 24, 31, 38, 42, 50))
    assert [p for p in low_problems if "lower_bound" in p] != []
    high_problems = _problems(_with_values(10, 12, 14, 18, 24, 31, 38, 42, above))
    assert [p for p in high_problems if "upper_bound" in p] != []


def test_an_open_bound_constrains_nothing_here() -> None:
    """The clause that proves the rule reads ``open_lower_bound``/``open_upper_bound``.

    The tail limits for an open bound are the SDK's ``_check_too_far_from_bounds``, which
    runs only under ``numeric_calibration.strict_validation`` -- a config this checker is
    not handed. M1-503 owns them.
    """
    far_below = _with_values(-1e6, 12, 14, 18, 24, 31, 38, 42, 50)
    assert _problems(far_below, open_lower_bound=True) == []
    assert _problems(far_below) != []
    far_above = _with_values(10, 12, 14, 18, 24, 31, 38, 42, 1e6)
    assert _problems(far_above, open_upper_bound=True) == []
    assert _problems(far_above) != []


def test_no_message_renders_a_question_field_value() -> None:
    """Round 1's blocking finding, asserted as **invariance** rather than substring absence.

    The first cut rendered the bounds, reasoning from ``binary.py`` that a repair turn which
    does not name them is one no model can aim at. That argument is binary's alone: the
    prompt prints ``0.001``-``0.999`` as a literal config may narrow, so a binary model does
    not know its effective bound, while ``forecast/inputs.py`` puts every numeric bound into
    the model's own request. Question fields come from Metaculus payloads, which CLAUDE.md
    classes as untrusted, and these strings reach ``ForecastGeneration.failure_problems`` and
    from there the persisted artifact.

    Invariance, not ``"20.0" not in message``: the messages name ``lower_bound`` and
    ``zero_point``, so short digit runs are substrings of nothing here but the level list,
    and a substring test would pass or fail for unrelated reasons (M1-303's trap). Two
    questions that trigger the same rules with wildly different numbers must produce
    byte-identical output.
    """
    forecast = _with_values(-1e9, 12, 14, 18, 24, 31, 38, 42, 1e9)
    first = _problems(forecast, lower_bound=20.0, upper_bound=40.0)
    second = _problems(
        forecast, lower_bound=3.141592653589793, upper_bound=98765.4321, zero_point=2.5
    )
    assert first, "both draws must trigger the bound rules or this proves nothing"
    # The zero-point rule fires only on the second, so compare the two bound problems that
    # both draws share rather than the whole list.
    shared = [p for p in second if "zero_point" not in p]
    assert first == shared
    for rendered in first + second:
        for leaked in ("20.0", "40.0", "3.14159", "98765", "2.5", "1e+09", "1000000000"):
            assert leaked not in rendered, rendered


def test_the_messages_still_distinguish_the_rule_they_report() -> None:
    """The companion to the invariance above: value-free must not mean uninformative.

    A checker whose every message was one constant would satisfy the leak property
    perfectly and tell a reader nothing. Each rule names the field of the question it is
    about -- names this project's canonical model authored -- so the five are distinct and
    a repair turn says which one was broken.
    """
    low = _problems(_with_values(-5, 12, 14, 18, 24, 31, 38, 42, 50))
    high = _problems(_with_values(10, 12, 14, 18, 24, 31, 38, 42, 500))
    zero = _problems(
        _with_values(1, 12, 14, 18, 24, 31, 38, 42, 50),
        lower_bound=2.0,
        zero_point=1.5,
        open_lower_bound=True,
    )
    order = _problems(_with_values(50, 42, 38, 31, 24, 18, 14, 12, 10))
    levels = _problems(
        _response(final_prediction={"percentiles": [{"percentile": 0.5, "value": 24.0}]})
    )
    rendered = [low, high, zero, order, levels]
    assert all(len(group) == 1 for group in rendered), rendered
    assert len({group[0] for group in rendered}) == 5


def test_the_rendered_levels_round_trip_back_to_the_levels_exactly() -> None:
    """``repr``, not a fixed precision -- M1-403's ``_format_bound`` argument, retargeted.

    Round 1 removed the rendered bounds, which is what that argument originally protected
    here. It still applies to the one thing this module does render: a level printed as
    ``0.010`` would be a level the model could aim at and still miss. Asserted by parsing
    the tokens back out of the message, not by comparing against ``repr`` -- a test compared
    against ``repr`` agrees with any renderer that happens to be ``repr``, including a wrong
    one.

    The mutation that motivated this in round 1 (``repr`` -> ``f"{v:.2f}"``) no longer has a
    bound to survive on, and ``.2f`` happens to round-trip for all nine of these levels. So
    the sharper mutation is the one this kills: a renderer that truncates.
    """
    problems = _problems(
        _response(final_prediction={"percentiles": [{"percentile": 0.5, "value": 24.0}]})
    )
    assert len(problems) == 1
    tokens = re.findall(r"[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?", problems[0])
    parsed = [value for value in (_parses_as(token) for token in tokens) if value is not None]
    # The nine levels appear in order, as a contiguous run; the leading "9" is the count.
    assert parsed[0] == float(len(DECLARED_PERCENTILE_LEVELS))
    assert tuple(parsed[1:]) == DECLARED_PERCENTILE_LEVELS


def _parses_as(token: str) -> float | None:
    try:
        return float(token)
    except ValueError:
        return None


def test_two_different_out_of_bounds_values_produce_identical_text() -> None:
    """The model's value never reaches the message; only the question's bound does."""
    first = _problems(_with_values(-5, 12, 14, 18, 24, 31, 38, 42, 50))
    second = _problems(_with_values(-9999.5, 12, 14, 18, 24, 31, 38, 42, 50))
    assert first == second
    assert "9999" not in " ".join(first)


# --- the zero point ------------------------------------------------------------------


def test_a_value_below_the_zero_point_is_refused() -> None:
    """``NumericDistribution._check_log_scaled_fields``, which runs unconditionally.

    It is here rather than in M1-503 for exactly that reason: it does not depend on
    ``numeric_calibration``, so it is true of the percentile set for any configuration.
    """
    forecast = _with_values(1, 12, 14, 18, 24, 31, 38, 42, 50)
    assert _problems(forecast, lower_bound=0.5, zero_point=0.25) == []
    problems = _problems(forecast, lower_bound=2.0, zero_point=1.5)
    assert [p for p in problems if "zero_point" in p] != []


def test_a_value_exactly_on_the_zero_point_is_accepted() -> None:
    """Inclusive: the SDK raises on ``value < zero_point``, not on equality.

    The lower bound is opened so this isolates the zero-point rule -- 1.5 sits below the
    question's ``lower_bound`` of 2.0, and with a *closed* lower bound that would be a
    second, different problem.
    """
    forecast = _with_values(1.5, 12, 14, 18, 24, 31, 38, 42, 50)
    assert _problems(forecast, lower_bound=2.0, zero_point=1.5, open_lower_bound=True) == []
    just_below = _with_values(nextafter(1.5, 0.0), 12, 14, 18, 24, 31, 38, 42, 50)
    problems = _problems(just_below, lower_bound=2.0, zero_point=1.5, open_lower_bound=True)
    assert [p for p in problems if "zero_point" in p] != []


def test_a_question_whose_zero_point_is_not_below_its_lower_bound_is_a_caller_mistake() -> None:
    """Not a repair turn: no percentile set could satisfy it, and the SDK refuses it too.

    ``forecast.generate`` refuses the same question before anything is spent, so the repair
    loop is never entered for it.
    """
    with pytest.raises(NumericOutputError) as caught:
        _problems(_response(), lower_bound=1.0, zero_point=1.0)
    assert caught.value.problems == [
        "question: zero_point must be strictly below lower_bound for a log-scaled question"
    ]
    with pytest.raises(NumericOutputError):
        _problems(_response(), lower_bound=1.0, zero_point=2.0)


# --- caller mistakes -----------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda: numeric_output_problems(
                "not a response",  # type: ignore[arg-type]
                _committed_forecast_config(),
                _question(),
            ),
            id="response of the wrong category",
        ),
        pytest.param(
            lambda: numeric_output_problems(
                validate_forecast_response(
                    {
                        **_payload(),
                        "question_type": "binary",
                        "final_prediction": {"probability_yes": 0.4},
                        "model_prior": 0.4,
                        "base_rate": {**_payload()["base_rate"], "prior_probability": 0.4},
                    },
                    response_model_for("binary"),
                ),  # type: ignore[arg-type]
                _committed_forecast_config(),
                _question(),
            ),
            id="a binary response",
        ),
        pytest.param(
            lambda: numeric_output_problems(
                _response(),
                None,  # type: ignore[arg-type]
                _question(),
            ),
            id="config of the wrong category",
        ),
        pytest.param(
            lambda: numeric_output_problems(
                _response(),
                _committed_forecast_config(),
                CanonicalBinaryQuestion(  # type: ignore[arg-type]
                    question_id=QUESTION_ID, post_id=POST_ID, title="Will X happen?"
                ),
            ),
            id="a binary question",
        ),
        pytest.param(
            lambda: numeric_output_problems(
                _response(),
                _committed_forecast_config(),
                CanonicalMultipleChoiceQuestion(  # type: ignore[arg-type]
                    question_id=QUESTION_ID,
                    post_id=POST_ID,
                    title="Which one?",
                    options=["a", "b"],
                ),
            ),
            id="a multiple-choice question",
        ),
        pytest.param(
            lambda: numeric_output_problems(
                _response(),
                _committed_forecast_config(),
                None,  # type: ignore[arg-type]
            ),
            id="no question at all",
        ),
    ],
)
def test_every_refusal_path_raises_this_modules_own_type_exactly(call: Any) -> None:
    """A raw ``AttributeError`` or ``TypeError`` escaping is the defect this project has
    taken as a review finding twice. Every malformed shape arrives as ``NumericOutputError``.
    """
    with pytest.raises(NumericOutputError) as caught:
        call()
    assert type(caught.value) is NumericOutputError
    # And it is still catchable as the package's one response-failure type.
    assert isinstance(caught.value, ForecastSchemaError)


def test_the_config_is_accepted_and_not_read() -> None:
    """Stated in the docstring, asserted here: no ``ForecastConfig`` field changes the verdict.

    If a later item gives numeric a config-dependent rule, this test is the one that has to
    be edited to say so -- which is the point of having it.
    """
    forecast = _response()
    narrow = _committed_forecast_config().model_copy(
        update={"min_probability": 0.4, "max_probability": 0.6}
    )
    assert numeric_output_problems(forecast, narrow, _question()) == numeric_output_problems(
        forecast, _committed_forecast_config(), _question()
    )


# --- the returning/raising pair -------------------------------------------------------


def test_the_pair_agrees_and_returns_the_same_object() -> None:
    forecast = _response()
    config = _committed_forecast_config()
    assert validate_numeric_output(forecast, config, _question()) is forecast


def test_validate_raises_with_exactly_the_problems_the_other_half_returns() -> None:
    forecast = _with_values(50, 42, 38, 31, 24, 18, 14, 12, -10)
    config = _committed_forecast_config()
    with pytest.raises(NumericOutputError) as caught:
        validate_numeric_output(forecast, config, _question())
    assert caught.value.problems == numeric_output_problems(forecast, config, _question())
    assert len(caught.value.problems) > 1


def test_nothing_is_clamped_sorted_or_padded() -> None:
    """The response comes back byte-identical; no rule here repairs anything.

    ``prompts/forecaster.md`` says "do not clamp mechanically" and M1-502's criterion is that
    "no arbitrary post-hoc renormalization is hidden". The pinned SDK *does* nudge repeated
    values inside ``NumericDistribution``; that is filed against M1-503, which owns the
    conversion where it happens.
    """
    forecast = _with_values(0, 0, 14, 18, 24, 31, 38, 42, 100)
    before = forecast.model_dump(mode="json")
    assert validate_numeric_output(forecast, _committed_forecast_config(), _question()) is forecast
    assert forecast.model_dump(mode="json") == before
