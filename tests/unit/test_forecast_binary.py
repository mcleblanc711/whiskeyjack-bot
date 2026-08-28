"""M1-403 acceptance: the golden binary output validates within the configured bounds.

The criterion has two clauses and they are tested differently on purpose. *"Validates
within configured bounds"* is a property of the checker, so it is tested at the
boundary, one ulp outside it, and under a **non-default** config -- that last one is the
case that proves ``forecast.min_probability`` has a consumer rather than a default that
happens to agree with the prompt. *"Includes base rate, adjustments and failure modes"*
is a property of the golden output, so it is asserted on the fixture.

The comprehensive valid/invalid golden set is Codex's **T-901**, authored blind from
spec. This is the one fixture M1-403's own row names, following the M1-201 precedent.
"""

import copy
import json
from math import nextafter
from pathlib import Path
from typing import Any

import pytest
import yaml

from whiskeyjack_bot.config import ForecastConfig, validate_config_data
from whiskeyjack_bot.forecast.binary import (
    BinaryOutputError,
    binary_output_problems,
    validate_binary_output,
)
from whiskeyjack_bot.forecast.schema import (
    BinaryForecastResponse,
    ForecastSchemaError,
    MultipleChoiceForecastResponse,
    NumericForecastResponse,
    validate_forecast_response,
)
from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalNumericQuestion,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GOLDEN_PATH = FIXTURES / "forecasts" / "binary_golden.json"


def _committed_forecast_config() -> ForecastConfig:
    """The forecast section of the *committed* config.example.yaml, not a hand-built one.

    A test that builds its own bounds cannot notice the committed defaults drifting away
    from the range the prompt prints to the model.
    """
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    # The one substitution the committed example demands of every caller (D27: the
    # model name ships as a placeholder that fails validation on purpose). Nothing in
    # the forecast section is touched -- that is the whole point of reading this file.
    data["model"]["name"] = "openrouter/test-model"
    return validate_config_data(data).forecast


def _bounds(minimum: float, maximum: float) -> ForecastConfig:
    config = _committed_forecast_config()
    return config.model_copy(update={"min_probability": minimum, "max_probability": maximum})


def _question(**overrides: Any) -> CanonicalBinaryQuestion:
    """The canonical question M1-405 put on every checker's signature.

    ``binary_output_problems`` does not read it -- it has no rule a question decides -- but
    it gates it, so the argument is supplied here for the same reason a config is: a checker
    that refuses the wrong shape has to be handed the right one to test anything else.
    """
    fields: dict[str, Any] = {
        "question_id": 123,
        "post_id": 456,
        "title": "Will the thing happen?",
        "resolution_criteria": "Resolves YES if the thing happens.",
    }
    fields.update(overrides)
    return CanonicalBinaryQuestion(**fields)


@pytest.fixture()
def golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _response(golden: dict[str, Any], **overrides: Any) -> BinaryForecastResponse:
    payload = {**golden, **overrides}
    return validate_forecast_response(payload, BinaryForecastResponse)


def _with_probability(golden: dict[str, Any], probability: float) -> BinaryForecastResponse:
    return _response(golden, final_prediction={"probability_yes": probability})


def test_the_golden_output_validates_within_the_configured_bounds(golden: dict[str, Any]) -> None:
    """The acceptance criterion, both halves, against the committed defaults."""
    forecast = _response(golden)
    assert binary_output_problems(forecast, _committed_forecast_config(), _question()) == []
    assert validate_binary_output(forecast, _committed_forecast_config(), _question()) is forecast


def test_the_golden_output_includes_base_rate_adjustments_and_failure_modes(
    golden: dict[str, Any],
) -> None:
    """The criterion's second clause, asserted on the fixture rather than the checker.

    Requiring these of *every* response is M1-501's row -- it owns the attribution-field
    presence rules across all three question types. What M1-403 owes is a golden output
    that has them, and a fixture nobody checks is one a later edit can hollow out.
    """
    forecast = _response(golden)
    assert forecast.base_rate.reference_class.strip()
    assert forecast.base_rate.basis.strip()
    assert forecast.base_rate.source_ids
    assert forecast.evidence_adjustments
    assert any(adjustment.load_bearing for adjustment in forecast.evidence_adjustments)
    assert forecast.load_bearing_facts
    assert forecast.failure_modes
    assert forecast.status_quo.strip()


def test_the_golden_output_cites_the_source_id_form_the_pipeline_mints(
    golden: dict[str, Any],
) -> None:
    """Every citation is a ``src-NNN`` of the form ``inputs._SOURCE_ID_TEMPLATE`` produces.

    Resolving citations against the documents actually supplied is **M1-501's**; this is
    only the weaker claim that the golden output speaks the pipeline's own vocabulary,
    so it stays usable as the input to that item's tests.
    """
    forecast = _response(golden)
    cited = {
        source_id
        for holder in (*forecast.evidence_adjustments, *forecast.load_bearing_facts)
        for source_id in holder.source_ids
    } | set(forecast.base_rate.source_ids)
    assert cited
    for source_id in cited:
        assert source_id.startswith("src-")
        assert source_id[4:].isdigit()
        assert len(source_id[4:]) == 3


@pytest.mark.parametrize("probability", [0.001, 0.5, 0.999])
def test_the_configured_bounds_are_inclusive(golden: dict[str, Any], probability: float) -> None:
    """The prompt's own wording: "between 0.001 and 0.999 **inclusive**"."""
    forecast = _with_probability(golden, probability)
    assert binary_output_problems(forecast, _committed_forecast_config(), _question()) == []


@pytest.mark.parametrize(
    "probability",
    [
        nextafter(0.001, 0.0),
        nextafter(0.999, 1.0),
        0.0,
        1.0,
        0.9995,
    ],
)
def test_a_probability_outside_the_configured_bounds_is_refused(
    golden: dict[str, Any], probability: float
) -> None:
    """One ulp outside each end is enough; 0.9995 is the value the schema accepts.

    ``tests/unit/test_forecast_schema.py::test_configured_probability_bounds_are_not_
    applied_here`` asserts 0.9995 validates against the schema. This is the other half of
    that boundary: it validates there and is refused here.
    """
    forecast = _with_probability(golden, probability)
    problems = binary_output_problems(forecast, _committed_forecast_config(), _question())
    assert len(problems) == 1
    assert problems[0].startswith("final_prediction.probability_yes: ")


def test_the_bound_is_configured_and_not_the_prompts_literal(golden: dict[str, Any]) -> None:
    """The test that proves ``forecast.min_probability`` has a consumer.

    0.02 is inside the range ``prompts/forecaster.md`` prints to the model (0.001-0.999)
    and outside a config that narrows it. If the check were reading the prompt's literal
    rather than the config, this would pass.
    """
    forecast = _with_probability(golden, 0.02)
    assert binary_output_problems(forecast, _committed_forecast_config(), _question()) == []
    assert binary_output_problems(forecast, _bounds(0.05, 0.95), _question()) != []


def test_the_bound_problem_names_the_configured_numbers(golden: dict[str, Any]) -> None:
    """A repair turn that does not state the actual bound is one no model can satisfy.

    The prompt hard-codes 0.001-0.999 while config is free to narrow it, so a model told
    only "out of bounds" has nothing to aim at. The bounds are operator configuration,
    which is the M1-401 carve-out's category; the model's own value stays withheld --
    see the next test.
    """
    forecast = _with_probability(golden, 0.02)
    problems = binary_output_problems(forecast, _bounds(0.05, 0.95), _question())
    assert "0.05" in problems[0]
    assert "0.95" in problems[0]


def test_two_different_out_of_bounds_values_produce_identical_text(
    golden: dict[str, Any],
) -> None:
    """The leak property as invariance, not as substring absence.

    "The value does not appear in the message" cannot be written over probabilities:
    ``"0."`` is a substring of every bound this project renders, so the assertion fails
    for reasons unrelated to leaking (the M1-308 round-5 trap, and the same reason
    M1-402's leak properties are written this way). Two different offending values
    producing byte-identical text is the claim that actually discriminates.
    """
    config = _committed_forecast_config()
    first = binary_output_problems(_with_probability(golden, 0.99951), config, _question())
    second = binary_output_problems(_with_probability(golden, 0.99997), config, _question())
    assert first == second
    assert "0.99951" not in first[0]
    assert "0.99997" not in first[0]


@pytest.mark.parametrize(
    ("label", "overrides", "expected"),
    [
        (
            "no base-rate prior",
            {"base_rate": "drop_prior"},
            "base_rate.prior_probability",
        ),
        ("no model prior", {"model_prior": None}, "model_prior"),
    ],
)
def test_a_binary_response_must_supply_its_prior(
    golden: dict[str, Any], label: str, overrides: dict[str, Any], expected: str
) -> None:
    """The one presence rule M1-403 owns, and it is binary-specific by construction.

    ``schema.py`` enforces the prompt's stated converse -- a non-binary response must
    leave both priors null -- and M1-402 recorded deferring this direction. Enforcing
    only the converse leaves binary the single question type where the prior a binary
    forecast is built from is optional.
    """
    payload = dict(golden)
    if overrides.get("base_rate") == "drop_prior":
        payload["base_rate"] = {**golden["base_rate"], "prior_probability": None}
    else:
        payload.update(overrides)
    forecast = validate_forecast_response(payload, BinaryForecastResponse)
    problems = binary_output_problems(forecast, _committed_forecast_config(), _question())
    assert [problem.split(":")[0] for problem in problems] == [expected]


def test_every_problem_is_reported_at_once(golden: dict[str, Any]) -> None:
    """Three problems arrive as three strings, not as the first one found.

    A repair turn that reveals one defect per billed call cannot converge inside the
    one repair ``forecast.generate`` budgets.
    """
    payload = dict(golden)
    payload["base_rate"] = {**golden["base_rate"], "prior_probability": None}
    payload["model_prior"] = None
    payload["final_prediction"] = {"probability_yes": 0.9995}
    forecast = validate_forecast_response(payload, BinaryForecastResponse)
    assert len(binary_output_problems(forecast, _committed_forecast_config(), _question())) == 3


def test_nothing_is_clamped(golden: dict[str, Any]) -> None:
    """``prompts/forecaster.md``: "do not clamp mechanically".

    The refused response is returned to no one, and the accepted one comes back
    identical -- a coerced probability is a number the ledger cannot attribute to the
    model, which is what M1-502's "no arbitrary post-hoc renormalization is hidden"
    forbids.
    """
    forecast = _with_probability(golden, 0.5)
    returned = validate_binary_output(forecast, _committed_forecast_config(), _question())
    assert returned is forecast
    assert returned.final_prediction.probability_yes == 0.5
    with pytest.raises(BinaryOutputError):
        validate_binary_output(
            _with_probability(golden, 0.9995), _committed_forecast_config(), _question()
        )


def test_the_raising_entry_point_carries_the_same_problems(golden: dict[str, Any]) -> None:
    forecast = _with_probability(golden, 0.9995)
    config = _committed_forecast_config()
    with pytest.raises(BinaryOutputError) as caught:
        validate_binary_output(forecast, config, _question())
    assert caught.value.problems == binary_output_problems(forecast, config, _question())


@pytest.mark.parametrize(
    ("question_type", "response_type", "final_prediction"),
    [
        (
            "multiple_choice",
            MultipleChoiceForecastResponse,
            {"options": [{"option": "Yes", "probability": 1.0}]},
        ),
        (
            "numeric",
            NumericForecastResponse,
            {"percentiles": [{"percentile": 0.5, "value": 1.0}]},
        ),
    ],
)
def test_a_response_of_another_question_type_is_a_caller_mistake(
    golden: dict[str, Any],
    question_type: str,
    response_type: type[Any],
    final_prediction: dict[str, Any],
) -> None:
    """Not a repair turn: asking the model to fix a dispatch bug of ours is nonsense.

    The multiple-choice option set is M1-404's and the numeric percentiles are M1-405's;
    neither is approximated here, and neither response reaches this checker in
    ``forecast.generate`` because the dispatch is keyed on the question-type literal.
    """
    payload = {
        **golden,
        "question_type": question_type,
        # The prompt's own rule, which ``schema.py`` enforces: a non-binary response
        # carries no priors. Without this the payload would not validate at all and the
        # test would never reach the checker.
        "model_prior": None,
        "base_rate": {**golden["base_rate"], "prior_probability": None},
        "final_prediction": final_prediction,
    }
    forecast = validate_forecast_response(payload, response_type)
    with pytest.raises(BinaryOutputError) as caught:
        binary_output_problems(forecast, _committed_forecast_config(), _question())
    assert caught.value.problems == ["forecast: must be a binary forecast response"]


def test_a_config_that_admits_no_probability_is_refused(golden: dict[str, Any]) -> None:
    """An inverted pair would fail every forecast through the repair loop.

    ``ForecastConfig`` refuses it at load, so this can only arrive from a config
    assembled some other way -- ``model_copy`` here, which skips validation exactly as an
    arbitrary assembly would. ``forecast.generate`` refuses it in its preflight, before
    anything is spent; this is the net under that.
    """
    forecast = _response(golden)
    with pytest.raises(BinaryOutputError):
        binary_output_problems(forecast, _bounds(0.9, 0.1), _question())
    with pytest.raises(BinaryOutputError):
        binary_output_problems(forecast, _bounds(0.5, 0.5), _question())


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(None, id="none"),
        pytest.param(0.5, id="a float"),
        pytest.param("question", id="a string"),
        pytest.param(
            CanonicalNumericQuestion(
                question_id=123,
                post_id=456,
                title="How many things?",
                lower_bound=0.0,
                upper_bound=100.0,
                open_lower_bound=False,
                open_upper_bound=False,
                cdf_size=201,
            ),
            id="a numeric question",
        ),
    ],
)
def test_a_question_of_the_wrong_type_arrives_as_this_projects_error(
    golden: dict[str, Any], bad: Any
) -> None:
    """M1-405 put the question on this signature; this is what binary spends it on.

    The parameter is not read by any rule here, so the only thing that could make it
    load-bearing is the gate -- and a gate nothing tests is a gate that can be deleted
    without a failure. ``validate.output_problems`` checks the *pairing* centrally before
    the lookup, which is why this one only has to know its own type.
    """
    with pytest.raises(BinaryOutputError) as caught:
        binary_output_problems(_response(golden), _committed_forecast_config(), bad)
    assert caught.value.problems == ["question: must be a canonical binary question"]


def test_the_question_is_accepted_and_not_read(golden: dict[str, Any]) -> None:
    """Stated in the module docstring, asserted here: no field of the question changes the
    verdict. If a later item gives binary a question-dependent rule, this test is the one
    that has to be edited to say so."""
    forecast = _with_probability(golden, 0.9995)
    config = _committed_forecast_config()
    assert binary_output_problems(forecast, config, _question()) == binary_output_problems(
        forecast,
        config,
        _question(question_id=999, post_id=1, title="Something else entirely?"),
    )


@pytest.mark.parametrize("bad", [None, 0.5, "config", {"min_probability": 0.1}])
def test_a_config_of_the_wrong_type_arrives_as_this_projects_error(
    golden: dict[str, Any], bad: Any
) -> None:
    """Every malformed shape arrives as the module's own error type, never an
    AttributeError -- the rule this project has taken as a review finding twice."""
    with pytest.raises(BinaryOutputError):
        binary_output_problems(_response(golden), bad, _question())


def test_the_committed_defaults_still_match_the_range_the_prompt_prints() -> None:
    """A canary, not a constraint. Nothing cross-checks the two (filed as M1-407).

    If the committed defaults are ever narrowed, this fails and points at the prompt --
    which cannot be edited without bumping its version and re-pinning its digest.
    """
    config = _committed_forecast_config()
    prompt = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")
    assert "between 0.001 and 0.999 inclusive" in prompt
    assert (config.min_probability, config.max_probability) == (0.001, 0.999)


def test_this_modules_errors_are_its_own_type_and_still_catch_as_the_packages() -> None:
    """Round-1 finding: the module must own the error type its callers handle.

    The exact-type half is the finding. The subclass half is why the fix costs nothing:
    ``generate._parse`` catches ``ForecastSchemaError`` and is unchanged, so both
    readings of the project's error rule hold at once rather than trading off.
    """
    assert issubclass(BinaryOutputError, ForecastSchemaError)
    assert BinaryOutputError is not ForecastSchemaError


@pytest.mark.parametrize(
    ("label", "call"),
    [
        (
            "out of bounds",
            lambda golden, config: validate_binary_output(
                _with_probability(golden, 0.9995), config, _question()
            ),
        ),
        (
            "a response of another question type",
            lambda golden, config: binary_output_problems("not a response", config, _question()),
        ),
        (
            "a config of the wrong type",
            lambda golden, config: binary_output_problems(_response(golden), None, _question()),
        ),
        (
            "an inverted bounds pair",
            lambda golden, config: binary_output_problems(
                _response(golden), _bounds(0.9, 0.1), _question()
            ),
        ),
        (
            "a question of another type",
            lambda golden, config: binary_output_problems(_response(golden), config, None),
        ),
    ],
)
def test_every_refusal_path_raises_this_modules_own_type_exactly(
    golden: dict[str, Any], label: str, call: Any
) -> None:
    """Exact type, not ``isinstance``: inheriting from the package error must not be a
    way for the parent to be raised directly on one path and go unnoticed."""
    with pytest.raises(ForecastSchemaError) as caught:
        call(golden, _committed_forecast_config())
    assert type(caught.value) is BinaryOutputError
    assert caught.value.problems
    # Still value-free: the sanitized problem list is inherited, not re-invented.
    assert all(": " in problem for problem in caught.value.problems)


# --- M1-502 round 1: the spec's probability envelope, re-checked away from ForecastConfig ---
#
# The multiple-choice half of this carries the full account
# (``test_forecast_multiple_choice.py``). Repeated here because ``_bounds`` above is the
# helper that demonstrates the hole: ``model_copy(update=...)`` skips the field validators,
# and ``_require_config`` re-checked only ``low < high``.


@pytest.mark.parametrize(
    "minimum,maximum",
    [(0.0, 0.999), (0.001, 1.0), (0.0, 1.0), (0.0005, 0.999), (0.001, 0.9995)],
)
def test_a_config_outside_the_spec_envelope_is_refused(
    golden: dict[str, Any], minimum: float, maximum: float
) -> None:
    with pytest.raises(BinaryOutputError) as caught:
        binary_output_problems(
            _with_probability(golden, 0.5), _bounds(minimum, maximum), _question()
        )
    (problem,) = caught.value.problems
    assert problem == (
        "forecast_config: min_probability and max_probability must lie within "
        "0.001 and 0.999 inclusive (configured pair withheld)"
    )


def test_the_envelope_check_does_not_render_the_configured_pair(golden: dict[str, Any]) -> None:
    with pytest.raises(BinaryOutputError) as caught:
        binary_output_problems(_with_probability(golden, 0.5), _bounds(0.0004, 0.9996), _question())
    message = " ".join(caught.value.problems)
    assert "0.0004" not in message
    assert "0.9996" not in message


def test_the_envelope_ends_themselves_are_accepted(golden: dict[str, Any]) -> None:
    assert (
        binary_output_problems(_with_probability(golden, 0.5), _bounds(0.001, 0.999), _question())
        == []
    )
