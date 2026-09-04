"""T-904: the numeric CDF conversion contract, frozen against ``forecasting-tools==0.2.92``.

*"Guard 201-point size, monotonicity, bounds and PMF steps against package upgrades.
Golden edge cases pass on 0.2.92; dependency drift triggers a visible failure."*

``tests/unit/test_forecast_cdf.py`` already discharges the *behavioural* half of that row --
38 tests over the point count, the four bound-flag combinations, the adjacent-PMF cap, ties,
the liveness guard, the wall-clock bound and the no-leak invariance probe. Its own module
docstring names this file as the follow-on, and nothing there is restated here.

**What is missing there, and is the whole of this file, is the drift half.** Every one of
those assertions is expressed against *our* config and *our* guards --
``len(cdf.values) == calibration.expected_cdf_points``,
``max(steps) <= calibration.max_adjacent_pmf``, endpoints against the question's flags. An
upgrade whose ``_standardize_cdf`` produced materially different numbers in the same *shape*
passes all of them, because our guards would clamp or refuse identically. Before this file
there was no record anywhere in the repository of what the pinned package actually emits:
``tests/fixtures/forecasts/numeric_golden.json`` (T-901) is a schema record whose
``final_prediction`` is the nine declared percentiles, not a 201-point array.

**``201`` lives in three places and only one of them is what this row is about.**

- ``config.py``'s ``expected_cdf_points: Literal[201]`` -- moved only by a config edit, and
  the ``Literal`` refuses it.
- ``forecast/cdf.py``'s ``_SDK_DEFAULT_CDF_SIZE`` -- moved only by a source edit.
- **the length ``get_cdf`` actually returns** -- moved by a package upgrade, and nothing in
  the repository could see it move.

The golden pins the third. :func:`test_the_three_sources_of_the_point_count_agree` is what
ties them together, so an upgrade that moved the SDK's resolution produces a failing array
comparison, then a failing three-way comparison once the golden is regenerated, and an owner
decision instead of a silent config bump.

**Exact float equality, no tolerance, and that is deliberate rather than careless.**
``_standardize_cdf`` ends with ``np.round(cdf, 10)``, so the emitted array is snapped to a
``1e-10`` grid, and every value in every frozen case sits at least ``1.4e3`` ulps from a
rounding boundary (``3.7e4`` for the linear cases; the log-scaled one, whose path is the only
one running ``np.log``, is the ``1.4e3``). A tolerance of ``1e-12`` would not notice the
one-ulp perturbation this file's assertions were mutation-tested against, and a contract test
that passes on a drifted version is worse than no contract test.

The fixture is written by ``scripts/regenerate_cdf_golden.py``, which this module
deliberately does not import: the assertion is a live SDK call against a frozen record, not
a generator checked against itself.
"""

from __future__ import annotations

import copy
import importlib.metadata
import json
from collections.abc import Callable
from itertools import pairwise
from math import isfinite
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from forecasting_tools import NumericDistribution, Percentile

from whiskeyjack_bot.config import NumericCalibrationConfig, validate_config_data
from whiskeyjack_bot.forecast import cdf as cdf_module
from whiskeyjack_bot.forecast.cdf import (
    _LOWER_ENDPOINT,
    _NOT_MONOTONE,
    _PERCENTILES_LOC,
    _UPPER_ENDPOINT,
    _WRONG_LENGTH,
    build_numeric_cdf,
    numeric_cdf_or_problems,
)
from whiskeyjack_bot.forecast.schema import NumericForecastResponse, validate_forecast_response
from whiskeyjack_bot.questions.model import CanonicalNumericQuestion

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "forecasts"
GOLDEN_PATH: Final = FIXTURES / "numeric_cdf_golden.json"
SCHEMA_GOLDEN_PATH: Final = FIXTURES / "numeric_golden.json"

GOLDEN: Final[dict[str, Any]] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
CASES: Final[tuple[dict[str, Any], ...]] = tuple(GOLDEN["cases"])
CASE_IDS: Final[tuple[str, ...]] = tuple(case["name"] for case in CASES)

QUESTION_ID: Final = 123
POST_ID: Final = 456

# The properties ``_array_problems`` reports, restated here rather than imported. That is
# the point: this is the *oracle* the drift mutants below are measured against, and an
# oracle that called the code under test could not show that a mutant violates exactly one
# rule. Written from the rule statements in ``forecast/cdf.py``'s docstrings.
_LENGTH: Final = "length"
_UNIT_INTERVAL: Final = "unit_interval"
_MONOTONE: Final = "monotone"
_STEP_CAP: Final = "step_cap"
_LOWER: Final = "lower_endpoint"
_UPPER: Final = "upper_endpoint"


def _committed_calibration() -> NumericCalibrationConfig:
    """The *committed* ``config.example.yaml``, not a hand-built config.

    ``test_forecast_cdf.py``'s reason, unchanged: a test that builds its own config cannot
    notice the committed defaults drifting away from what the operator is actually shipped.
    """
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    calibration = validate_config_data(data).numeric_calibration
    assert isinstance(calibration, NumericCalibrationConfig)
    return calibration


def _distribution(case: dict[str, Any]) -> NumericDistribution:
    """The SDK distribution for one case, built from the fixture's own fields.

    No ``CanonicalNumericQuestion`` and no ``NumericCalibrationConfig`` in the path, so
    nothing this project owns can mask a change in what the package emits. That separation
    is the marginal value of the whole file; :func:`test_our_conversion_returns_the_frozen_array`
    is the one test that deliberately puts our code back in.
    """
    question = case["question"]
    return NumericDistribution(
        declared_percentiles=[
            Percentile(percentile=point["percentile"], value=point["value"])
            for point in case["percentiles"]
        ],
        open_upper_bound=question["open_upper_bound"],
        open_lower_bound=question["open_lower_bound"],
        upper_bound=question["upper_bound"],
        lower_bound=question["lower_bound"],
        zero_point=question["zero_point"],
        cdf_size=question["cdf_size"],
        standardize_cdf=case["distribution"]["standardize_cdf"],
        strict_validation=case["distribution"]["strict_validation"],
    )


def _emitted(case: dict[str, Any]) -> tuple[float, ...]:
    """What the installed package returns for one case, right now."""
    return tuple(point.percentile for point in _distribution(case).get_cdf())


def _frozen(case: dict[str, Any]) -> tuple[float, ...]:
    """What the package returned when the fixture was written."""
    return tuple(case["cdf"])


def _steps(values: tuple[float, ...]) -> list[float]:
    return [second - first for first, second in pairwise(values)]


def _violations(
    values: tuple[float, ...], case: dict[str, Any], calibration: NumericCalibrationConfig
) -> frozenset[str]:
    """Every rule from ``_array_problems`` that ``values`` breaks, as a set of names.

    The independent oracle. Its only job is to let a drift mutant assert that it violates
    **exactly one** rule before asserting which problem the conversion reports -- the
    anti-vacuity guard that stops "the mutant was refused" from standing in for "the mutant
    was refused *for this reason*".
    """
    broken: set[str] = set()
    if len(values) != calibration.expected_cdf_points:
        broken.add(_LENGTH)
    if not all(isfinite(value) and 0.0 <= value <= 1.0 for value in values):
        return frozenset(broken | {_UNIT_INTERVAL})
    if not all(first <= second for first, second in pairwise(values)):
        broken.add(_MONOTONE)
    if any(step > calibration.max_adjacent_pmf for step in _steps(values)):
        broken.add(_STEP_CAP)
    if not case["question"]["open_lower_bound"] and values and values[0] != 0.0:
        broken.add(_LOWER)
    if not case["question"]["open_upper_bound"] and values and values[-1] != 1.0:
        broken.add(_UPPER)
    return frozenset(broken)


# --- the frozen record against the pinned package ----------------------------------


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_the_pinned_sdk_still_emits_the_frozen_arrays(case: dict[str, Any]) -> None:
    """The whole row, in one assertion: the package still says what the record says.

    Exact equality on every one of the 201 values. This is the only assertion in the
    repository that would notice ``_standardize_cdf`` changing its offsets, its cap, its
    scale search or its rounding -- every other CDF test compares the package against
    itself or against our own guards, so both sides drift together.

    Tests the third of the three ``201``s: the length the package actually returns.
    """
    assert _emitted(case) == _frozen(case)


def test_the_golden_was_generated_by_the_installed_package() -> None:
    """A pin move must not leave a stale frozen record quietly passing.

    Distinct from ``tests/unit/test_dependency_pins.py``, which pins ``pyproject.toml``
    against the installed distribution. This pins the **golden's provenance** against the
    installed distribution: the arrays above could, in principle, survive an upgrade
    unchanged, and then nothing would have prompted anyone to look. The failure this
    produces is the prompt.
    """
    installed = importlib.metadata.version(GOLDEN["generated_with"]["package"])
    assert GOLDEN["generated_with"]["version"] == installed, (
        "the golden CDF fixture was generated against "
        f"{GOLDEN['generated_with']['version']} but {installed} is installed; run "
        "`uv run python scripts/regenerate_cdf_golden.py` and read the array diff before "
        "committing it"
    )


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_every_golden_array_has_the_recorded_point_count(case: dict[str, Any]) -> None:
    """The size clause, read out of the record rather than transcribed.

    No assertion in this file spells ``201`` -- the number appears only in prose. It is in
    the fixture because the package put it there, which is what makes the golden the third
    site rather than a fourth copy of the first.
    """
    assert len(case["cdf"]) == case["point_count"]


def test_the_three_sources_of_the_point_count_agree() -> None:
    """The one assertion that ties the three ``201``s together.

    ``config.py``'s ``Literal[201]``, ``forecast/cdf.py``'s ``_SDK_DEFAULT_CDF_SIZE`` and
    the length the package emits are three independent facts that happen to coincide today.
    An upgrade that moved the third fails
    :func:`test_the_pinned_sdk_still_emits_the_frozen_arrays`; regenerating the golden then
    fails *this*, and the config's ``Literal`` refuses the change after that. Three visible
    failures in a row, ending at an owner decision -- which is what "dependency drift
    triggers a visible failure" has to mean for a number that is currently correct in three
    places at once.
    """
    recorded = {case["point_count"] for case in CASES}
    assert len(recorded) == 1, "the cases disagree about the emitted point count"
    assert recorded == {_committed_calibration().expected_cdf_points}
    assert recorded == {cdf_module._SDK_DEFAULT_CDF_SIZE}


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_every_golden_array_is_strictly_increasing(case: dict[str, Any]) -> None:
    """Monotonicity, asserted strictly and on the frozen array.

    ``strictly`` rather than ``non-decreasing``, which is the weaker rule ``cdf.py``
    enforces: the measured minimum step across these seven cases is ``9.94e-05``, four
    orders of magnitude above the ``1e-10`` grid the package rounds to, so strictness is a
    real property of 0.2.92 rather than an accident that a rounding change could erase
    silently. A package that started emitting a flat step would still satisfy our
    conversion and fail here, which is the point of a contract test.

    The existing suite asserts non-decreasing incidentally
    (``test_the_prompts_own_numeric_example_converts``) and never produces ``_NOT_MONOTONE``
    at all; :func:`test_a_drifted_array_is_refused_for_exactly_one_reason` is the other half.
    """
    values = _frozen(case)
    assert all(second > first for first, second in pairwise(values))


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_the_golden_endpoints_follow_the_recorded_bound_flags(case: dict[str, Any]) -> None:
    """A closed bound pins its endpoint exactly; an open one leaves mass outside.

    ``test_forecast_cdf.py`` asserts the same rule over all four flag combinations, and this
    is not a restatement of it: that test asserts the *relation* (``== 0.0``, or ``> 0.0``),
    which any rescaling of the package's offsets would still satisfy. This asserts the
    frozen numbers -- ``0.005945`` and ``0.994055`` are ``_standardize_cdf``'s open-bound
    offsets showing through, and a change to either is a change to how much mass the
    package puts outside a bound.
    """
    values = _frozen(case)
    question = case["question"]
    if question["open_lower_bound"]:
        assert 0.0 < values[0] < 1.0
    else:
        assert values[0] == 0.0
    if question["open_upper_bound"]:
        assert 0.0 < values[-1] < 1.0
    else:
        assert values[-1] == 1.0


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_no_golden_step_exceeds_the_committed_cap(case: dict[str, Any]) -> None:
    """The PMF clause: every frozen array is submittable under the committed cap."""
    assert max(_steps(_frozen(case))) <= _committed_calibration().max_adjacent_pmf


def test_the_concentrated_case_still_saturates_just_below_the_packages_own_cap() -> None:
    """Where the margin under ``max_adjacent_pmf: 0.2`` actually comes from, frozen.

    ``NumericDefaults.get_max_pmf_value`` caps the inbound PMF at ``0.2 * 0.95``, and the
    committed ``0.2`` is satisfiable only because of that wiggle room. The existing suite
    asserts the band ``0.18 < max(steps) <= 0.2``; this pins the number, so a package that
    moved the wiggle factor to ``0.99`` -- still inside that band, still passing every
    existing test -- fails here.
    """
    case = _case("concentrated_saturating")
    assert max(_steps(_frozen(case))) == 0.18999997910000005


def test_the_tie_rewrite_is_visible_in_the_emitted_array() -> None:
    """A repeated value changes the submitted CDF, not only ``percentiles_used``.

    ``_check_and_update_repeating_values`` rewrites a repeated value downward by up to
    ``1e-6`` before any array exists, which crowds two percentile levels onto almost the
    same x-position and nearly doubles the tallest step -- ``1.94x``, measured, which is
    why the ratio below is asserted at ``1.9`` and the exact pair is asserted as well: a
    ratio alone would not notice both arrays moving together. ``test_forecast_cdf.py`` records
    the divergence through ``NumericCdf.percentiles_used``; this records what it did to the
    array that would actually be posted, which is the thing M1-508 has to decide about.
    """
    tied = max(_steps(_frozen(_case("tie_inside_bounds"))))
    untied = max(_steps(_frozen(_case("interior_closed_both"))))
    assert tied > 1.9 * untied
    assert (tied, untied) == (0.0401450109, 0.020675000000000027)


def _case(name: str) -> dict[str, Any]:
    matches = [case for case in CASES if case["name"] == name]
    assert len(matches) == 1, name
    return matches[0]


# --- the same arrays, through this project's own conversion ------------------------


def _response(case: dict[str, Any]) -> NumericForecastResponse:
    """T-901's numeric schema golden, with this case's percentiles substituted.

    The attribution fields are irrelevant to the conversion and reusing the committed schema
    golden is one fewer hand-built payload to drift: what varies between cases is exactly
    the nine declared points, and they come from the CDF golden.
    """
    payload = json.loads(SCHEMA_GOLDEN_PATH.read_text(encoding="utf-8"))
    payload["final_prediction"] = {"percentiles": copy.deepcopy(case["percentiles"])}
    return validate_forecast_response(payload, NumericForecastResponse)


def _question(case: dict[str, Any]) -> CanonicalNumericQuestion:
    question = case["question"]
    return CanonicalNumericQuestion(
        question_id=QUESTION_ID,
        post_id=POST_ID,
        title="How many things?",
        resolution_criteria="Resolves to the number of things.",
        lower_bound=question["lower_bound"],
        upper_bound=question["upper_bound"],
        open_lower_bound=question["open_lower_bound"],
        open_upper_bound=question["open_upper_bound"],
        zero_point=question["zero_point"],
        cdf_size=question["cdf_size"],
    )


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_our_conversion_returns_the_frozen_array(case: dict[str, Any]) -> None:
    """Where the first ``201`` meets the third, on the array a submission would carry.

    ``test_nothing_this_module_returns_is_clamped_sorted_or_padded`` makes the same
    passthrough claim against a **live** ``get_cdf`` call, so our output and the package's
    drift together and the assertion survives. Against the frozen record it cannot: this
    fails if the package changes, if our conversion starts editing the array, or if the two
    stop agreeing about the length.

    The distribution flags are asserted rather than assumed, so a case added later with
    ``standardize_cdf: false`` fails here instead of quietly comparing our output against an
    array the committed config would never have produced.
    """
    calibration = _committed_calibration()
    assert case["distribution"] == {
        "standardize_cdf": calibration.use_forecasting_tools_standardization,
        "strict_validation": calibration.strict_validation,
    }
    cdf = build_numeric_cdf(_response(case), calibration, _question(case))
    assert cdf.values == _frozen(case)
    assert cdf.standardized is calibration.use_forecasting_tools_standardization


# --- drift simulation: what our guards do when the package changes shape -----------

# Each mutator takes the array the package emits and returns the array a drifted package
# might emit instead. Applied twice per case: to the frozen floats, to show the mutant
# breaks exactly one rule, and inside a patched ``get_cdf``, to show which problem the
# conversion then reports. One function for both, so the two cannot disagree.
#
# The mutants are chosen to be *surgical*. Dropping the last point, for instance, would
# break the length rule and the upper-endpoint rule at once, and a test that asserted only
# "it was refused" would pass whichever rule fired.
_Mutator = Callable[[list[float]], list[float]]


def _drop_an_interior_point(values: list[float]) -> list[float]:
    return values[:100] + values[101:]


def _swap_two_interior_values(values: list[float]) -> list[float]:
    mutated = list(values)
    mutated[100], mutated[101] = mutated[101], mutated[100]
    return mutated


def _lift_the_lower_endpoint(values: list[float]) -> list[float]:
    return [values[1], *values[1:]]


def _lower_the_upper_endpoint(values: list[float]) -> list[float]:
    return [*values[:-1], values[-2]]


@pytest.mark.parametrize(
    ("mutate", "broken", "expected"),
    [
        pytest.param(_drop_an_interior_point, _LENGTH, _WRONG_LENGTH, id="200 points"),
        pytest.param(_swap_two_interior_values, _MONOTONE, _NOT_MONOTONE, id="a decrease"),
        pytest.param(_lift_the_lower_endpoint, _LOWER, _LOWER_ENDPOINT, id="cdf[0] off 0.0"),
        pytest.param(_lower_the_upper_endpoint, _UPPER, _UPPER_ENDPOINT, id="cdf[-1] off 1.0"),
    ],
)
def test_a_drifted_array_is_refused_for_exactly_one_reason(
    monkeypatch: pytest.MonkeyPatch, mutate: _Mutator, broken: str, expected: str
) -> None:
    """An upgrade that changed the array's *shape* is caught, and named correctly.

    ``_WRONG_LENGTH``, ``_NOT_MONOTONE``, ``_LOWER_ENDPOINT`` and ``_UPPER_ENDPOINT`` have
    no producing test anywhere in the suite -- they appear only as members of the closed
    message vocabulary in ``tests/property/test_forecast_properties.py``. They are the four
    rules ``cdf.py`` keeps *because* the package might change, so leaving them unexercised
    is leaving the drift guard itself unguarded. (``_STEP_TOO_TALL`` is not here:
    ``test_standardization_off_is_honoured_and_an_uncapped_array_is_refused`` already
    produces it.)

    The condition is simulated at ``get_cdf`` rather than drawn, and that is what the seam
    is for: no percentile set reaches these arrays through 0.2.92, which is precisely the
    claim under test -- a *later* version might. ``test_the_conversion_normalises_negative_
    zero_to_zero`` patches the same seam for the same reason.

    Two assertions carry the weight, and the first is the one that stops this being
    vacuous: the mutant breaks **exactly one** rule of the independent oracle, and the
    conversion reports **exactly** that one problem. Without it, a mutant that happened to
    break two rules would still "pass" against a membership check.
    """
    case = _case("interior_closed_both")
    calibration = _committed_calibration()
    frozen = _frozen(case)

    mutated = tuple(mutate(list(frozen)))
    assert _violations(frozen, case, calibration) == frozenset()
    assert _violations(mutated, case, calibration) == frozenset({broken})

    real = NumericDistribution.get_cdf

    def drifted(self: NumericDistribution) -> list[Percentile]:
        # The x-axis positions are rebuilt rather than carried: ``forecast/cdf.py`` reads
        # only ``point.percentile``, and a dropped point would otherwise need a value
        # invented for it anyway.
        heights = mutate([point.percentile for point in real(self)])
        return [
            Percentile(percentile=height, value=float(index))
            for index, height in enumerate(heights)
        ]

    monkeypatch.setattr(NumericDistribution, "get_cdf", drifted)
    cdf, problems = numeric_cdf_or_problems(_response(case), calibration, _question(case))
    assert cdf is None
    assert problems == [f"{_PERCENTILES_LOC}: {expected}"]


def test_the_oracle_and_the_conversion_agree_on_a_clean_array() -> None:
    """The oracle is not vacuously empty, and not vacuously full.

    ``_violations`` is hand-written from the rule statements rather than imported from
    ``cdf.py``, which is what lets it referee the mutants above. That only works if it
    actually finds things: here it is shown to return an empty set for every frozen array
    and a non-empty one for an array that breaks every rule at once.
    """
    calibration = _committed_calibration()
    for case in CASES:
        assert _violations(_frozen(case), case, calibration) == frozenset()
    closed = _case("interior_closed_both")
    hostile = (0.5, 0.4, 0.9)
    assert _violations(hostile, closed, calibration) == frozenset(
        {_LENGTH, _MONOTONE, _STEP_CAP, _LOWER, _UPPER}
    )
    assert _violations((0.0, float("nan")), closed, calibration) == frozenset(
        {_LENGTH, _UNIT_INTERVAL}
    )


def test_the_frozen_arrays_survive_a_json_round_trip() -> None:
    """The record is exact on disk, not merely close.

    ``json.dumps`` spells a float with ``repr``, which is the shortest string that
    round-trips, so the committed file reproduces every value bit for bit. Said here because
    every assertion above is an exact-equality assertion, and all of them would degrade
    quietly into approximate ones if this were not true.
    """
    for case in CASES:
        assert json.loads(json.dumps(case["cdf"])) == case["cdf"]
