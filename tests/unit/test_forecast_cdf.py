"""M1-503 acceptance: the 201-point CDF, and the PMF constraint the package does not check.

*"Exactly 201 monotone values for normal numeric questions; maintained PMF constraint
passes."* Two clauses, and the second is the one worth being careful about.

Reading ``numeric_report.py`` casually suggests the pinned SDK already enforces the PMF
cap: ``_check_distribution_too_tall`` exists, it compares every adjacent step against
``NumericDefaults.get_max_pmf_value``, and ``get_cdf`` re-validates its own output.
``test_the_sdk_does_not_check_the_pmf_cap_on_its_own_output`` shows by execution that it
does not -- the re-validation constructs a ``NumericDistribution`` without ``cdf_size``,
and the cap is gated on ``len(percentiles) == self.cdf_size``, which is false for every
length when that field is ``None``. So the cap is genuinely this module's, and the test
that says so is written as a construction rather than as a description, because the pin
can move.

The comprehensive golden set and the package-drift guard are Codex's **T-904**, authored
from spec. This file builds its payloads out of ``prompts/forecaster.md`` and its config
out of the committed ``config.example.yaml``, the ``test_forecast_numeric.py`` approach and
for its reason: a test that builds its own fixture cannot notice the committed defaults
drifting away from what the model is actually told and actually configured with.
"""

import copy
import json
import re
import signal
import threading
import time
from itertools import pairwise
from math import copysign, isfinite, nextafter
from pathlib import Path
from typing import Any

import pytest
import yaml
from forecasting_tools import NumericDistribution, Percentile
from forecasting_tools.data_models.questions import NumericQuestion

from whiskeyjack_bot.config import (
    MAX_CONVERSION_TIMEOUT_SECONDS,
    NumericCalibrationConfig,
    validate_config_data,
)
from whiskeyjack_bot.forecast import cdf as cdf_module
from whiskeyjack_bot.forecast.cdf import (
    _NOT_CONVERTIBLE,
    _NOT_WELL_FORMED,
    NumericCdfError,
    NumericCdfTimeoutError,
    build_numeric_cdf,
    numeric_cdf_or_problems,
)
from whiskeyjack_bot.forecast.numeric import DECLARED_PERCENTILE_LEVELS, numeric_output_problems
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

# The prompt's own example, read once so the values a test perturbs are the values the
# model is shown. ``test_forecast_numeric.py`` reads the same block for the same reason.
PROMPT_VALUES = (10.0, 12.0, 14.0, 18.0, 24.0, 31.0, 38.0, 42.0, 50.0)


def _committed_config() -> Any:
    """The *committed* config.example.yaml, not a hand-built one."""
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    return validate_config_data(data)


def _calibration(**overrides: Any) -> NumericCalibrationConfig:
    committed = _committed_config().numeric_calibration
    if not overrides:
        return committed
    return committed.model_copy(update=overrides)


def _forecast_config() -> Any:
    return _committed_config().forecast


def _json_block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block("Numeric schema") + "}"),
    }
    payload["model_prior"] = None
    payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    payload["question_id"] = QUESTION_ID
    payload.update(overrides)
    return payload


def _response(**overrides: Any) -> NumericForecastResponse:
    return validate_forecast_response(_payload(**overrides), NumericForecastResponse)


def _with_values(*values: float) -> NumericForecastResponse:
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


def _convert(
    forecast: NumericForecastResponse | None = None,
    calibration: NumericCalibrationConfig | None = None,
    **question_overrides: Any,
) -> tuple[Any, list[str]]:
    return numeric_cdf_or_problems(
        forecast if forecast is not None else _response(),
        calibration if calibration is not None else _calibration(),
        _question(**question_overrides),
    )


def _steps(values: tuple[float, ...]) -> list[float]:
    return [second - first for first, second in pairwise(values)]


# --- the acceptance criterion, clause by clause ------------------------------------


def test_the_prompts_own_numeric_example_converts() -> None:
    """The reply the prompt shows the model produces a submittable array.

    The end-to-end statement of the row. If this fails, either the prompt's example or the
    committed calibration has drifted away from something the pinned SDK will convert, and
    a model that followed the instructions exactly would be sent a repair turn.
    """
    cdf, problems = _convert()
    assert problems == []
    assert cdf is not None
    assert len(cdf.values) == 201
    assert all(first <= second for first, second in pairwise(cdf.values))


def test_exactly_the_configured_number_of_points() -> None:
    """201, read from the config rather than written here.

    ``expected_cdf_points`` is a ``Literal[201]``, so this cannot currently differ -- but
    the array's length is compared against the *config* in ``cdf.py``, and a test that
    hardcoded 201 on both sides would pass even if that comparison were deleted.
    """
    calibration = _calibration()
    cdf, problems = _convert()
    assert problems == []
    assert cdf is not None
    assert len(cdf.values) == calibration.expected_cdf_points


@pytest.mark.parametrize(
    ("open_lower", "open_upper"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_the_endpoints_follow_the_bound_flags(open_lower: bool, open_upper: bool) -> None:
    """A closed bound pins its endpoint; an open one does not.

    All four combinations, because the rule is keyed on the two flags separately and a test
    of the closed/closed case alone would pass for an implementation that applied either
    rule unconditionally. The open cases assert the *negation* -- mass is left outside the
    bound -- which is what makes them discriminating rather than decorative.
    """
    cdf, problems = _convert(open_lower_bound=open_lower, open_upper_bound=open_upper)
    assert problems == []
    assert cdf is not None
    if open_lower:
        assert cdf.values[0] > 0.0
    else:
        assert cdf.values[0] == 0.0
    if open_upper:
        assert cdf.values[-1] < 1.0
    else:
        assert cdf.values[-1] == 1.0


def test_every_adjacent_step_is_within_the_configured_cap() -> None:
    """The PMF constraint, on the array that would actually be posted."""
    calibration = _calibration()
    cdf, problems = _convert()
    assert problems == []
    assert cdf is not None
    assert max(_steps(cdf.values)) <= calibration.max_adjacent_pmf


def test_a_concentrated_reply_saturates_just_below_the_cap() -> None:
    """The interesting case for the cap, and the one that shows where the margin comes from.

    Nine values inside a fifth of a unit on a 0-100 question is as concentrated as a
    percentile set gets. The SDK's standardization caps the inbound PMF at
    ``0.2 * 0.95 = 0.19`` -- the wiggle room its own validator leaves -- so the array comes
    back at the cap rather than over it. Pinned because it is the whole reason the
    committed ``max_adjacent_pmf: 0.2`` is satisfiable at all: a config that narrowed it
    below 0.19 would refuse a concentrated but perfectly reasonable forecast, which is the
    unsatisfiable-rule failure M1-404's round-1 finding was about.
    """
    concentrated = _with_values(23.9, 23.95, 23.98, 23.99, 24.0, 24.01, 24.02, 24.05, 24.1)
    cdf, problems = _convert(concentrated)
    assert problems == []
    assert cdf is not None
    steps = _steps(cdf.values)
    assert 0.18 < max(steps) <= _calibration().max_adjacent_pmf


def test_the_sdk_does_not_check_the_pmf_cap_on_its_own_output() -> None:
    """Why the cap above is this module's rule and not the package's.

    ``get_cdf`` re-validates its result by constructing a ``NumericDistribution`` from it,
    and that distribution is built **without ``cdf_size``**. ``_check_distribution_too_tall``
    is gated on ``len(percentiles) == self.cdf_size``, which is false for every length once
    that field is ``None``. Asserted by handing the SDK an array that violates the cap and
    watching it accept it -- a description of the source would agree with any reading of
    it, including a wrong one.

    If a pin move starts enforcing the cap there, this fails and the redundancy in
    ``cdf.py`` becomes a deliberate belt-and-braces rather than a load-bearing rule.
    """
    tall = [0.0, 0.5, 1.0]
    accepted = NumericDistribution(
        declared_percentiles=[
            Percentile(percentile=height, value=value)
            for height, value in zip(tall, (0.0, 50.0, 100.0), strict=True)
        ],
        open_upper_bound=False,
        open_lower_bound=False,
        upper_bound=100.0,
        lower_bound=0.0,
        zero_point=None,
        cdf_size=None,
    )
    steps = _steps(tuple(point.percentile for point in accepted.declared_percentiles))
    assert max(steps) > _calibration().max_adjacent_pmf


def test_a_reply_the_percentile_checker_accepts_can_still_fail_the_conversion() -> None:
    """The case that puts this gate inside the attempt loop.

    Nine equal values are non-decreasing, inside the bounds and exactly the declared
    levels, so ``numeric_output_problems`` returns nothing: the reply followed
    ``prompts/forecaster.md`` to the letter. The conversion refuses it -- the resulting CDF
    is flat over most of its range, and ``get_cdf``'s own re-validation raises. Without
    this gate that reply is billed, parsed, recorded and approved, and fails inside the SDK
    at submission time.

    Both halves are asserted. A test of the refusal alone would pass if M1-405 had started
    refusing ties, which would make this gate redundant rather than load-bearing.
    """
    flat = _with_values(*([24.0] * 9))
    assert numeric_output_problems(flat, _forecast_config(), _question()) == []
    cdf, problems = _convert(flat)
    assert cdf is None
    assert len(problems) == 1
    assert problems[0].startswith("final_prediction.percentiles: ")


def test_a_cdf_size_the_config_does_not_expect_is_a_refusal_not_a_problem() -> None:
    """No reply could fix it, so it must never become a repair turn.

    ``binary._require_config``'s rule. A question declaring another resolution converts to
    an array of that length, which ``submission_live._require_cdf`` refuses -- after the
    forecast has been billed, recorded and approved.
    """
    with pytest.raises(NumericCdfError) as excinfo:
        _convert(cdf_size=101)
    assert excinfo.value.problems == [
        "question: cdf_size must equal numeric_calibration.expected_cdf_points "
        "(offending input withheld)"
    ]


def test_a_question_whose_zero_point_is_not_below_its_lower_bound_is_a_refusal() -> None:
    """``_check_log_scaled_fields`` runs unconditionally, so no percentile set survives it.

    ``numeric._require_question`` refuses the same question for the same reason. The
    duplication is deliberate: a caller reaching this module without the composed
    validation would otherwise be handed an unsatisfiable repair turn.
    """
    with pytest.raises(NumericCdfError):
        _convert(zero_point=0.0)


# --- the deviation from the row's wording, pinned rather than asserted -------------


def test_the_field_mapping_is_the_one_from_question_would_have_made() -> None:
    """``NumericDistribution.from_question`` is not used, so its mapping is pinned here.

    The row says to use it. It takes an SDK ``NumericQuestion``, and that object does not
    survive ``questions/normalize.py`` -- the ledger stores a ``CanonicalNumericQuestion``,
    so reaching ``from_question`` would mean fabricating an SDK question purely to have its
    fields read straight back out. It also cannot honour
    ``numeric_calibration.strict_validation``, which it never passes.

    So the mapping is ours, and this is what makes that safe rather than merely stated: the
    SDK builds a distribution its own way, this module builds one its way, and every field
    is compared. A pin move that renamed a field, changed a default or started deriving one
    of them differently fails here instead of silently producing a different CDF.

    ``strict_validation`` is excluded from the comparison and named as the one deliberate
    difference; ``is_date`` is excluded because this project has no date questions and
    ``from_question`` sets it from an ``isinstance`` check on an SDK type.
    """
    question = _question()
    percentiles = [
        Percentile(percentile=level, value=value)
        for level, value in zip(DECLARED_PERCENTILE_LEVELS, PROMPT_VALUES, strict=True)
    ]
    theirs = NumericDistribution.from_question(
        percentiles,
        NumericQuestion(
            question_text=question.title,
            upper_bound=question.upper_bound,
            lower_bound=question.lower_bound,
            open_upper_bound=question.open_upper_bound,
            open_lower_bound=question.open_lower_bound,
            zero_point=question.zero_point,
            cdf_size=question.cdf_size,
        ),
    )
    cdf, problems = _convert()
    assert problems == []
    assert cdf is not None
    # Same inputs, same output array -- the strongest form of the claim.
    assert cdf.values == tuple(point.percentile + 0.0 for point in theirs.get_cdf())
    for field in (
        "open_upper_bound",
        "open_lower_bound",
        "upper_bound",
        "lower_bound",
        "zero_point",
        "cdf_size",
        "standardize_cdf",
    ):
        assert getattr(theirs, field) == getattr(
            _question_distribution_fields(question, _calibration()), field
        ), field


def _question_distribution_fields(
    question: CanonicalNumericQuestion, calibration: NumericCalibrationConfig
) -> NumericDistribution:
    """This module's mapping, rebuilt here so the test compares it rather than describing it."""
    return NumericDistribution(
        declared_percentiles=[
            Percentile(percentile=level, value=value)
            for level, value in zip(DECLARED_PERCENTILE_LEVELS, PROMPT_VALUES, strict=True)
        ],
        open_upper_bound=question.open_upper_bound,
        open_lower_bound=question.open_lower_bound,
        upper_bound=question.upper_bound,
        lower_bound=question.lower_bound,
        zero_point=question.zero_point,
        cdf_size=question.cdf_size,
        standardize_cdf=calibration.use_forecasting_tools_standardization,
        strict_validation=calibration.strict_validation,
    )


# --- the two calibration knobs actually reach the SDK ------------------------------


def test_standardization_off_is_honoured_and_an_uncapped_array_is_refused() -> None:
    """``use_forecasting_tools_standardization: false`` is passed through, not ignored.

    Standardization is what caps the inbound PMF at ``0.19``; with it off nothing does, and
    a concentrated reply comes back over the configured ``0.2``. That is refused as a
    problem rather than posted, which is the whole reason the knob can be honoured at all:
    the operator may turn the SDK's normalization off, and the array is still checked
    against this project's own cap before anything is submitted.

    The same reply converts cleanly with the committed default, which is what makes this a
    test of the knob rather than of the reply.
    """
    concentrated = _with_values(23.9, 23.95, 23.98, 23.99, 24.0, 24.01, 24.02, 24.05, 24.1)
    assert _convert(concentrated)[1] == []
    cdf, problems = _convert(
        concentrated, calibration=_calibration(use_forecasting_tools_standardization=False)
    )
    assert cdf is None
    assert problems == [
        "final_prediction.percentiles: the converted CDF concentrates more probability "
        "between two adjacent points than the configured maximum; widen the spread "
        "between adjacent percentile values (offending input withheld)"
    ]


def test_strict_validation_off_leaves_a_repeated_value_alone() -> None:
    """The other knob, pinned the same way: through an observable difference in output.

    ``_check_and_update_repeating_values`` runs only under ``strict_validation``. With it
    on, a tie is nudged and :attr:`NumericCdf.adjusted` says so; with it off, the declared
    values reach ``get_cdf`` untouched. A test that only asserted the default would pass
    for an implementation that hardcoded it.
    """
    tie = _with_values(10.0, 10.0, 14.0, 18.0, 24.0, 31.0, 38.0, 42.0, 50.0)
    strict, _ = _convert(tie)
    lenient, _ = _convert(tie, calibration=_calibration(strict_validation=False))
    assert strict is not None and lenient is not None
    assert strict.adjusted is True
    assert lenient.adjusted is False
    assert lenient.percentiles_used == tuple(
        (point.percentile, point.value) for point in tie.final_prediction.percentiles
    )


# --- M1-508: the tie is converted, and the divergence is observable ----------------


def test_a_tie_is_converted_and_the_adjustment_is_recorded_not_hidden() -> None:
    """What this row owes M1-508: the difference is readable, not silent.

    ``prompts/forecaster.md`` says "non-decreasing" twice, so two equal values are a reply
    that followed the prompt, and M1-405 accepts them deliberately. The pinned SDK then
    builds the CDF from values this project did not produce -- it rewrites each repeated
    value, downward by up to ``1e-6`` inside the bounds. Against "nothing is clamped" that
    is a real divergence, and M1-508 is the row that decides what it may cost.

    What is settled here is only that it is **visible**: ``percentiles_used`` is what the
    conversion actually used and it differs from what the model returned. The values
    themselves are not logged.

    Also pins the correction this branch makes to two prose claims: the SDK does *not*
    mutate the caller's list in place, so the response is still exactly what it was.
    """
    declared = (10.0, 10.0, 14.0, 18.0, 24.0, 31.0, 38.0, 42.0, 50.0)
    tie = _with_values(*declared)
    before = tie.model_dump(mode="json")
    cdf, problems = _convert(tie)
    assert problems == []
    assert cdf is not None
    assert cdf.adjusted is True
    used = tuple(value for _, value in cdf.percentiles_used)
    assert used != declared
    # Only the repeated value moved, and it moved down, inside the bounds.
    assert used[2:] == declared[2:]
    assert used[0] < 10.0 and used[1] < 10.0
    assert used[0] != used[1]
    # The response object the ledger will hash is untouched.
    assert tie.model_dump(mode="json") == before


def test_a_reply_with_no_repeated_value_reports_no_adjustment() -> None:
    """The other half, so ``adjusted`` is not a constant."""
    cdf, problems = _convert()
    assert problems == []
    assert cdf is not None
    assert cdf.adjusted is False
    assert cdf.percentiles_used == tuple(
        zip(DECLARED_PERCENTILE_LEVELS, PROMPT_VALUES, strict=True)
    )


# --- the liveness guard: inputs the pinned SDK never returns from ------------------

# Both captured by fuzzing the conversion under a worker-thread deadline, then reduced to
# literals so the regression costs no draws. Neither is exotic: each is a reply
# ``numeric_output_problems`` accepts, and ``prompts/forecaster.md`` asks for
# "non-decreasing" values, so a tie is a compliant answer rather than a malformed one.
_HANGS_TIE_REWRITTEN_ONTO_BOUND = (
    (
        1.9,
        1.9,
        16.689279064930158,
        40.76687708555057,
        86.62698362886371,
        89.04730534332211,
        101.4345590075338,
        148.1522332384087,
        148.1522332384087,
    ),
    {
        "lower_bound": 2.0,
        "upper_bound": 100.0,
        "open_lower_bound": True,
        "open_upper_bound": True,
        "zero_point": 1.5,
    },
)
_HANGS_LOG_SCALED_SPAN = (
    (
        -3.971733705368532e-181,
        -3.971733705368532e-181,
        -3.971733705368532e-181,
        10.367256550384665,
        10.780095059151328,
        10.780095059151328,
        17.119124079549202,
        45.35618087933484,
        63.51437691644283,
    ),
    {
        "lower_bound": 0.0,
        "upper_bound": 100.0,
        "open_lower_bound": True,
        "open_upper_bound": True,
        "zero_point": -1.0,
    },
)


@pytest.mark.parametrize(
    ("values", "question_fields"),
    [
        pytest.param(*_HANGS_TIE_REWRITTEN_ONTO_BOUND, id="tie-rewritten-onto-open-bound"),
        pytest.param(*_HANGS_LOG_SCALED_SPAN, id="log-scaled-span"),
    ],
)
@pytest.mark.usefixtures("deadline")
def test_an_input_that_does_not_terminate_in_the_sdk_is_refused_first(
    values: tuple[float, ...], question_fields: dict[str, Any]
) -> None:
    """The guard, stated as the thing it prevents rather than as the check it performs.

    ``_standardize_cdf`` searches for a scale with ``while capped_sum(hi) < 1.0: hi *= 1.2``.
    With a negative interior PMF that sum falls without bound instead of rising, ``hi``
    reaches ``inf``, ``inf * 1.2`` is still ``inf``, and ``capped_sum(inf)`` is ``-inf``,
    which is still below ``1.0``. The loop has no exit. It does not raise and it is not
    slow: it never returns, and nothing on the forecast path imposes a deadline.

    Both parametrized inputs reached it with the **committed** defaults, and both are
    replies ``numeric_output_problems`` accepts -- asserted below rather than asserted
    about, because "M1-405 would have caught it" is exactly the belief that would make this
    guard look redundant.

    **M1-514 replaced this test's worker-thread harness with the ``deadline`` fixture.**
    The harness existed because a plain call could not express "this must terminate": a
    regression hung the process until CI killed it, and the failure read as an
    infrastructure problem rather than as this assertion. It cannot be used any more, and
    the reason is the point of M1-514 -- ``forecast.cdf`` now refuses to convert on a thread
    where it cannot install its own bound, so the harness would measure its own refusal.
    ``tests/conftest.py``'s ``deadline`` fixture is the replacement and is strictly better:
    it is an outer ``SIGALRM`` the production bound suspends and restores, so a regression
    in *either* the guard or the bound fails loudly here rather than hanging.
    """
    assert (
        numeric_output_problems(
            _with_values(*values), _forecast_config(), _question(**question_fields)
        )
        == []
    )
    cdf, problems = numeric_cdf_or_problems(
        _with_values(*values), _calibration(), _question(**question_fields)
    )
    assert cdf is None
    assert problems == [
        "final_prediction.percentiles: the declared percentiles do not produce a "
        "well-formed cumulative distribution; return strictly increasing values spread "
        "more widely across the question's range (detail withheld: the conversion echoes "
        "the values it refused)"
    ]


def test_the_guard_is_skipped_when_standardization_is_off() -> None:
    """With ``standardize_cdf`` false the hanging loop is never reached, so probing twice
    would only double the conversion cost.

    The same input is refused either way -- the unstandardized path raises where the
    standardized one hangs -- so this pins the *cost* decision, not the outcome.
    """
    values, fields = _HANGS_TIE_REWRITTEN_ONTO_BOUND
    cdf, problems = numeric_cdf_or_problems(
        _with_values(*values),
        _calibration(use_forecasting_tools_standardization=False),
        _question(**fields),
    )
    assert cdf is None
    assert problems == [f"final_prediction.percentiles: {_NOT_CONVERTIBLE}"]


def test_a_well_formed_reply_still_converts_and_pays_only_one_extra_probe() -> None:
    """The guard must not refuse anything the conversion would have accepted.

    ``prompts/forecaster.md``'s own example is the control: it converted before the guard
    existed and must convert after it, or the liveness fix has been bought by rejecting
    legitimate forecasts.
    """
    cdf, problems = _convert()
    assert problems == []
    assert cdf is not None
    assert len(cdf.values) == 201


# --- the wall-clock bound: what the guard above cannot promise (M1-514) -------------
#
# The guard is keyed on the one mechanism M1-503 had evidence for. This section is about
# the input nobody has found: the conversion is cut off in bounded time and recorded as a
# failure, whatever the reason it did not return.
#
# Every test here neuters the guard rather than deleting it, and uses one of the two real
# non-terminating literals above. A synthetic "SDK that sleeps" would pass against a bound
# that only catches slowness; only the genuine loop -- which does not raise, is not slow,
# and never returns -- distinguishes a real cutoff from a plausible-looking one.


@pytest.fixture
def guard_neutered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make M1-503's fast path let the two known hanging inputs through.

    ``WHERE 0`` on a trigger rather than ``DROP TRIGGER`` -- M1-607's pattern. Deleting the
    guard would change which code the rest of the file exercises; forcing its answer to
    "this converges" leaves everything else identical and puts the SDK into the loop that
    genuinely does not terminate.
    """
    monkeypatch.setattr(cdf_module, "_standardization_can_converge", lambda distribution: True)


@pytest.mark.parametrize(
    ("values", "question_fields"),
    [
        pytest.param(*_HANGS_TIE_REWRITTEN_ONTO_BOUND, id="tie-rewritten-onto-open-bound"),
        pytest.param(*_HANGS_LOG_SCALED_SPAN, id="log-scaled-span"),
    ],
)
@pytest.mark.usefixtures("guard_neutered", "deadline")
def test_a_conversion_that_does_not_terminate_is_cut_off(
    values: tuple[float, ...], question_fields: dict[str, Any]
) -> None:
    """The acceptance criterion: cut off in bounded time, not left to stop the run.

    With the fast path neutered these inputs reach ``_standardize_cdf`` and the scale search
    never returns. The assertion is that the call comes back *at all*, and comes back near
    the configured bound rather than near the SDK's own runtime -- a conversion that
    returned in 14ms would mean the guard was still doing the work and this test was
    measuring nothing.

    The ``deadline`` fixture is the outer watchdog: if the bound regresses this fails as an
    assertion at 10s instead of hanging until CI kills the job.
    """
    started = time.monotonic()
    with pytest.raises(NumericCdfTimeoutError) as excinfo:
        numeric_cdf_or_problems(
            _with_values(*values),
            _calibration(conversion_timeout_seconds=0.5),
            _question(**question_fields),
        )
    elapsed = time.monotonic() - started
    assert excinfo.value.problems == [
        "final_prediction.percentiles: the numeric CDF conversion did not complete within "
        "the configured wall-clock bound (offending input withheld)"
    ]
    # Bracketed on both sides. The lower bound is what says the SDK really entered the
    # non-terminating loop rather than being refused early by something else.
    assert 0.5 <= elapsed < 5.0, elapsed


@pytest.mark.usefixtures("guard_neutered", "deadline")
def test_a_timeout_is_not_laundered_into_a_repair_turn() -> None:
    """``_Expired`` must derive from ``BaseException``, and this is the test that says so.

    ``_distribution``, ``_values`` and ``_standardization_can_converge`` each swallow
    ``Exception`` on purpose, to turn an SDK refusal into a repair turn without letting the
    SDK's value-quoting text escape. An expiry raised as an ordinary exception is caught by
    exactly those handlers and comes back as ``_NOT_CONVERTIBLE``: the run continues, the
    model is asked to repair something no reply could fix, a second call is billed, and the
    ledger records ``schema_invalid`` for a conversion that never terminated.

    Nothing about that failure is visible from the outside except the message, which is why
    this asserts the message rather than the class. Flipping ``_Expired`` to ``Exception``
    must fail here.
    """
    values, fields = _HANGS_TIE_REWRITTEN_ONTO_BOUND
    with pytest.raises(NumericCdfError) as excinfo:
        numeric_cdf_or_problems(
            _with_values(*values),
            _calibration(conversion_timeout_seconds=0.5),
            _question(**fields),
        )
    assert type(excinfo.value) is NumericCdfTimeoutError
    assert f"final_prediction.percentiles: {_NOT_CONVERTIBLE}" not in excinfo.value.problems


@pytest.mark.usefixtures("deadline")
def test_the_guard_is_still_the_fast_path_and_the_bound_is_the_backstop() -> None:
    """Both inputs are refused *deterministically*, without spending the deadline.

    This is the half of the acceptance criterion that asks which of the two survives and
    why. The guard does, as the fast path: it costs one interpolation and produces a repair
    turn the model can act on, where the bound costs the whole configured deadline and
    produces a terminal ``timeout``. Asserted by giving the conversion a bound it would
    obviously blow through if the guard were not there -- the test above proves it does.
    """
    for values, fields in (_HANGS_TIE_REWRITTEN_ONTO_BOUND, _HANGS_LOG_SCALED_SPAN):
        started = time.monotonic()
        cdf, problems = numeric_cdf_or_problems(
            _with_values(*values),
            _calibration(conversion_timeout_seconds=5.0),
            _question(**fields),
        )
        assert cdf is None
        assert problems == [f"final_prediction.percentiles: {_NOT_WELL_FORMED}"]
        assert time.monotonic() - started < 1.0


@pytest.mark.usefixtures("deadline")
def test_an_ordinary_conversion_is_not_falsely_timed_out() -> None:
    """A bound that refused the shipped workload would be a different kind of bug.

    The committed default, the committed prompt example, and every bound-flag combination
    -- the same control ``test_a_well_formed_reply_still_converts_and_pays_only_one_extra_probe``
    applies to the guard.
    """
    assert _committed_config().numeric_calibration.conversion_timeout_seconds == 10.0
    for open_lower in (True, False):
        for open_upper in (True, False):
            cdf, problems = _convert(
                open_lower_bound=open_lower,
                open_upper_bound=open_upper,
                **({"zero_point": -1.0} if open_lower else {}),
            )
            assert problems == [], (open_lower, open_upper, problems)
            assert cdf is not None


@pytest.mark.usefixtures("deadline")
def test_an_outer_deadline_comes_back_with_the_elapsed_time_deducted() -> None:
    """The bound suspends an outer timer; it must not cancel or restart it.

    ``forecast.cdf`` installs a process-global ``SIGALRM`` handler and interval timer for
    the duration of one conversion, so it owes the caller both back. Restoring the handler
    alone would leave the outer deadline disarmed -- silently, since nothing fires -- and
    restoring the timer at its original value would push the caller's deadline out by
    however long the conversion took, every time.

    ``tests/conftest.py``'s own ``deadline`` fixture is an outer ``SIGALRM``, so this is not
    hypothetical: without the restore, the whole file's watchdog stops working after the
    first numeric conversion.
    """
    fired: list[int] = []
    previous = signal.signal(signal.SIGALRM, lambda signum, frame: fired.append(signum))
    signal.setitimer(signal.ITIMER_REAL, 5.0)
    try:
        time.sleep(0.5)
        assert _convert()[1] == []
        remaining, interval = signal.setitimer(signal.ITIMER_REAL, 0.0)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)
    assert fired == [], "the caller's deadline must not fire during the conversion"
    assert signal.getsignal(signal.SIGALRM) is not None
    # Still armed, and advanced rather than reset: strictly less than the 4.5s that would
    # remain if the conversion were free, strictly more than nothing.
    assert 0.0 < remaining < 4.5, remaining
    assert interval == 0.0


@pytest.mark.usefixtures("deadline")
def test_a_thread_that_cannot_install_the_bound_refuses_rather_than_running_unbounded() -> None:
    """Fail closed. A bound that is silently absent is worse than one that is loudly missing.

    ``signal.signal`` raises ``ValueError`` off the main thread of the main interpreter, so
    the deadline cannot be installed there. The alternative to refusing would be to convert
    anyway, which restores exactly the stopped run this item exists to prevent -- and does
    it invisibly, on whichever thread a future ``run_limits.max_parallel_questions > 1``
    happens to use.

    The response here is the *well-formed* one: the refusal is about the thread, not the
    reply, so it must fire for input that would otherwise convert cleanly.
    """
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = numeric_cdf_or_problems(_response(), _calibration(), _question())
        except NumericCdfError as exc:
            outcome["problems"] = exc.problems

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(20.0)
    assert not worker.is_alive(), "the refusal must be immediate, not a conversion that ran"
    assert "result" not in outcome
    assert outcome["problems"] == [
        "final_prediction.percentiles: the numeric CDF conversion cannot be bounded in "
        "time on this thread; it is refused rather than run unbounded"
    ]


@pytest.mark.parametrize("value", [60.001, 120.0, 86400.0])
def test_a_bound_above_the_ceiling_is_refused_at_load(value: float) -> None:
    """A bound any config can lift is not a bound -- ``MAX_MODEL_INVOCATIONS``' argument.

    ``conversion_timeout_seconds: 86400`` would leave the non-terminating scale search
    running for a day, which is the stopped run with extra steps. Refused where it fails
    earliest: at config load, and therefore at ``verify-env``.
    """
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    data["numeric_calibration"]["conversion_timeout_seconds"] = value
    with pytest.raises(Exception):
        validate_config_data(data)


def test_the_committed_bound_is_inside_the_ceiling() -> None:
    """A ceiling that refused the shipped config would be a different kind of bug."""
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert (
        0
        < data["numeric_calibration"]["conversion_timeout_seconds"]
        <= MAX_CONVERSION_TIMEOUT_SECONDS
    )
    assert MAX_CONVERSION_TIMEOUT_SECONDS == 60.0


@pytest.mark.usefixtures("deadline")
def test_the_ceiling_is_refused_again_at_the_conversion_site() -> None:
    """``model_construct`` bypasses validation, so reaching here means an ``AppConfig``
    assembled some other way -- the reason ``generate.py`` repeats ``MAX_MODEL_INVOCATIONS``
    at the site that spends money. A config object carries no memory of which validator
    built it, and this is the bound that decides how long a hang may stop the run for.
    """
    committed = _calibration()
    unbounded = NumericCalibrationConfig.model_construct(
        **{**committed.model_dump(), "conversion_timeout_seconds": 86400.0}
    )
    assert unbounded.conversion_timeout_seconds == 86400.0
    with pytest.raises(NumericCdfError) as excinfo:
        numeric_cdf_or_problems(_response(), unbounded, _question())
    assert excinfo.value.problems == [
        "numeric_calibration: conversion_timeout_seconds is outside the wall-clock bound "
        "the conversion may be given (offending input withheld)"
    ]


# --- message hygiene ---------------------------------------------------------------


def test_two_different_unconvertible_replies_produce_identical_text() -> None:
    """Invariance, not substring absence.

    ``hostile not in message`` is the draft this project has had to correct three times: it
    fails on a value that happens to be a substring of something the message renders for an
    innocent reason, and it passes for a message that leaks a *transformed* copy. The
    correct shape is that two different planted values in the same place produce
    byte-identical output.

    Both replies here are flat and therefore unconvertible, at opposite ends of the range,
    so the same rule fires for both and any difference in the text would be a leak.
    """
    low = _convert(_with_values(*([24.0] * 9)))[1]
    high = _convert(_with_values(*([71.5] * 9)))[1]
    assert low == high
    assert low != []


def test_two_different_refused_questions_produce_identical_text() -> None:
    """The same invariance on the raising path, where the value is a *question* field.

    Question fields come from Metaculus payloads, which CLAUDE.md classes as untrusted, and
    these strings reach the persisted raw-output envelope through
    ``ForecastGeneration.failure_problems``. M1-405 took a rendered bound as a round-1
    blocking finding for exactly this reason.
    """

    def refusal(**overrides: Any) -> list[str]:
        with pytest.raises(NumericCdfError) as excinfo:
            _convert(**overrides)
        return excinfo.value.problems

    assert refusal(cdf_size=101) == refusal(cdf_size=999_983)
    assert refusal(zero_point=-1.0, lower_bound=-1.0) == refusal(
        zero_point=-500.0, lower_bound=-500.0
    )


def test_the_invariance_probe_would_notice_a_leak() -> None:
    """The guard on the two tests above, which pass trivially against a constant.

    Renders the planted values the way a leaking message would and asserts the two
    renderings differ -- so "identical output" is a fact about the messages rather than
    about the plantings being indistinguishable.
    """
    assert repr(24.0) != repr(71.5)
    assert repr(101) != repr(999_983)


def test_no_problem_or_refusal_renders_a_number_from_the_input() -> None:
    """A second, independent reading of the same rule.

    The declared levels are the one category of number this module's siblings *do* name --
    they are this project's own constant, transcribed from the hashed prompt. This module
    names none at all: every message is a constant, and nothing reached through
    ``forecast``, ``question`` or the SDK is interpolated anywhere.
    """
    planted = {"24.0", "71.5", "101", "100.0", "0.2", "201"}
    emitted: list[str] = []
    emitted.extend(_convert(_with_values(*([24.0] * 9)))[1])
    for overrides in ({"cdf_size": 101}, {"zero_point": 0.0}):
        with pytest.raises(NumericCdfError) as excinfo:
            _convert(**overrides)
        emitted.extend(excinfo.value.problems)
    concentrated = _with_values(23.9, 23.95, 23.98, 23.99, 24.0, 24.01, 24.02, 24.05, 24.1)
    emitted.extend(
        _convert(
            concentrated, calibration=_calibration(use_forecasting_tools_standardization=False)
        )[1]
    )
    assert emitted
    for message in emitted:
        assert not any(token in message for token in planted), message


# --- caller mistakes arrive as this module's own error type ------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda: numeric_cdf_or_problems("not a response", _calibration(), _question()),
            id="response of the wrong category",
        ),
        pytest.param(
            lambda: numeric_cdf_or_problems(
                validate_forecast_response(
                    {
                        **_payload(),
                        "question_type": "binary",
                        "final_prediction": {"probability_yes": 0.4},
                        "model_prior": 0.4,
                        "base_rate": {**_payload()["base_rate"], "prior_probability": 0.4},
                    },
                    response_model_for("binary"),
                ),
                _calibration(),
                _question(),
            ),
            id="a binary response",
        ),
        pytest.param(
            lambda: numeric_cdf_or_problems(_response(), None, _question()),
            id="calibration of the wrong category",
        ),
        pytest.param(
            lambda: numeric_cdf_or_problems(_response(), _forecast_config(), _question()),
            id="the sibling config section",
        ),
        pytest.param(
            lambda: numeric_cdf_or_problems(
                _response(),
                _calibration(),
                CanonicalBinaryQuestion(
                    question_id=QUESTION_ID, post_id=POST_ID, title="Will X happen?"
                ),
            ),
            id="a binary question",
        ),
        pytest.param(
            lambda: numeric_cdf_or_problems(
                _response(),
                _calibration(),
                CanonicalMultipleChoiceQuestion(
                    question_id=QUESTION_ID,
                    post_id=POST_ID,
                    title="Which one?",
                    options=["a", "b"],
                ),
            ),
            id="a multiple-choice question",
        ),
        pytest.param(
            lambda: numeric_cdf_or_problems(_response(), _calibration(), None),
            id="no question at all",
        ),
        pytest.param(
            lambda: build_numeric_cdf("not a response", _calibration(), _question()),
            id="the raiser, response of the wrong category",
        ),
    ],
)
def test_every_refusal_path_raises_this_modules_own_type_exactly(call: Any) -> None:
    """A raw ``AttributeError``, ``TypeError`` or ``pydantic.ValidationError`` escaping is
    the defect this project has taken as a review finding twice. Every malformed shape
    arrives as ``NumericCdfError``.

    ``type(...) is`` rather than ``isinstance``: ``NumericCdfError`` subclasses
    ``ForecastSchemaError`` and so do four sibling errors, so an ``isinstance`` assertion
    would pass for a ``NumericOutputError`` leaking out of the module next door.
    """
    with pytest.raises(ForecastSchemaError) as excinfo:
        call()
    assert type(excinfo.value) is NumericCdfError
    assert excinfo.value.problems


# --- the returned array is the SDK's, unedited -------------------------------------


def test_the_pair_agrees_and_the_raiser_returns_the_same_cdf() -> None:
    """One conversion, two entry points, no second code path to drift."""
    returned, problems = _convert()
    raised = build_numeric_cdf(_response(), _calibration(), _question())
    assert problems == []
    assert returned == raised


def test_the_raiser_carries_exactly_the_problems_the_other_half_returns() -> None:
    flat = _with_values(*([24.0] * 9))
    _, problems = _convert(flat)
    with pytest.raises(NumericCdfError) as excinfo:
        build_numeric_cdf(flat, _calibration(), _question())
    assert excinfo.value.problems == problems


def test_nothing_this_module_returns_is_clamped_sorted_or_padded() -> None:
    """The array is the SDK's own, value for value.

    The one transformation in the path is ``-0.0 -> 0.0``, which changes no value, and the
    SDK's standardization, which is disclosed on ``standardized`` rather than hidden. If
    this module ever started nudging an array to satisfy its own cap, this fails.
    """
    cdf, _ = _convert()
    assert cdf is not None
    theirs = _question_distribution_fields(_question(), _calibration()).get_cdf()
    assert cdf.values == tuple(point.percentile for point in theirs)
    assert cdf.standardized is _calibration().use_forecasting_tools_standardization


def test_the_unit_interval_rule_is_unreachable_through_the_pinned_sdk() -> None:
    """Said rather than faked, because a draw that reaches it does not exist.

    ``Percentile``'s own validator refuses a ``percentile`` below 0, above 1 or NaN, and an
    infinity fails the same two comparisons -- so every value ``get_cdf`` can return is
    already a finite number in ``[0, 1]``. The check in ``cdf.py`` is kept because it is the
    contract ``submission_live._require_cdf`` enforces on the way back in, and a pin move
    that loosened ``Percentile`` would otherwise produce an array refused *after* a human
    approved the forecast.

    Asserting the SDK's guarantee is what makes that reasoning falsifiable: if a pin move
    makes any of these constructible, this fails and the check stops being redundant.
    """
    for hostile in (-0.1, 1.1, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(Exception):
            Percentile(percentile=hostile, value=1.0)
    cdf, _ = _convert()
    assert cdf is not None
    assert all(isfinite(value) and 0.0 <= value <= 1.0 for value in cdf.values)


def test_the_conversion_normalises_negative_zero_to_zero(monkeypatch: Any) -> None:
    """A representation fix in the producer, not a change to the hash rule.

    ``docs/M2-NOTES.md`` names this as a standing risk that "becomes reachable when
    M1-503's CDF arrays exist": ``0.0`` and ``-0.0`` compare equal but render differently,
    so they hash differently, so two arrays an operator would call equal derive two
    idempotency keys and two submissions. M2-NOTES declined to normalize floats *inside*
    the replay-critical hash rule; this is the producer declining to emit the spelling.

    **Not reachable through 0.2.92** -- ``_standardize_cdf`` pins a closed lower bound to a
    positive ``0.0`` -- so the condition is simulated at the seam rather than drawn. The
    monkeypatch stands in for a float edge or a pin move, and the claim it supports is
    only "the normalization is applied", never "the SDK emits this today".
    """
    real = NumericDistribution.get_cdf

    def signed_zero_first(self: NumericDistribution) -> list[Percentile]:
        points = real(self)
        return [Percentile(percentile=-0.0, value=points[0].value), *points[1:]]

    monkeypatch.setattr(NumericDistribution, "get_cdf", signed_zero_first)
    cdf, problems = _convert()
    assert problems == []
    assert cdf is not None
    assert copysign(1.0, cdf.values[0]) == 1.0


def test_the_negative_zero_normalisation_changes_no_other_value() -> None:
    """``+ 0.0`` is the identity on every float that is not ``-0.0``."""
    for value in (0.0, 1.0, 0.5, 5e-05, nextafter(0.0, 1.0), 0.19, 1 / 3):
        assert value + 0.0 == value
        assert copysign(1.0, value + 0.0) == copysign(1.0, value)
    assert copysign(1.0, -0.0) == -1.0
    assert copysign(1.0, -0.0 + 0.0) == 1.0
