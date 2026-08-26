"""M1-506 acceptance: one composed output-validation entry point, and it cannot fall behind.

The criterion has three clauses and each is asserted by something different.

*"One public entry point returns every output problem for a response of any supported
question type"* is a property of :func:`output_problems`, tested per question type against
the prompt's own example payloads.

*"``generate._output_problems`` is defined in terms of it rather than beside it, so the two
cannot diverge"* is asserted **structurally**: the private function is gone. A test that
compared the two lists would only prove they agree today; asserting the absence of a second
implementation is what makes divergence impossible. See
``test_the_parse_path_has_no_second_composition``.

*"A test fails if a supported question type has a checker the entry point does not reach"*
is the coverage pair below, and it is worth being precise about which failure each half
catches, because they are different:

- ``test_every_supported_type_has_an_explicit_registry_entry`` catches **a supported type
  with no entry** -- a fourth question type added to ``config.SupportedQuestionType``
  reaching the forecaster with nothing here having decided what validates it.
- ``test_no_output_checker_in_the_package_is_unreachable`` catches **a checker that exists
  and is not wired in** -- M1-404 writing the option-set checker and leaving
  ``_TYPE_CHECKERS["multiple_choice"]`` at ``None``. This is the half the criterion is
  really about, and it is the half a registry-totality assertion alone would miss.

Both are discriminating **today**, which is why M1-506 leads the wave rather than following
M1-404/M1-405: two of the three supported types are in exactly the state the first must
catch, and the one checker that exists is in exactly the state the second must find.
"""

from __future__ import annotations

import copy
import inspect
import json
import pkgutil
import re
import traceback
from importlib import import_module
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

import whiskeyjack_bot.forecast as forecast_package
from whiskeyjack_bot.config import ForecastConfig, SupportedQuestionType, validate_config_data
from whiskeyjack_bot.forecast.attribution import AttributionFieldError, attribution_problems
from whiskeyjack_bot.forecast.binary import BinaryOutputError, binary_output_problems
from whiskeyjack_bot.forecast.schema import (
    BinaryForecastResponse,
    ForecastResponse,
    ForecastSchemaError,
    response_model_for,
    validate_forecast_response,
)
from whiskeyjack_bot.forecast.validate import (
    _TYPE_CHECKERS,
    ForecastOutputError,
    output_problems,
    validate_output,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")

# Low-entropy on purpose: CI scans every branch with gitleaks, so a realistic secret
# shape here fails unrelated PRs. See the M1-301 notes.
SECRET = "privateFAKE123456"

QUESTION_ID = 123
# What the prompt's own shared-fields example cites: src-001 in ``base_rate``, src-002 in
# ``evidence_adjustments`` and ``load_bearing_facts``.
PROMPT_SOURCES = ("src-001", "src-002")

HEADINGS: dict[str, str] = {
    "binary": "Binary schema",
    "multiple_choice": "Multiple-choice schema",
    "numeric": "Numeric schema",
}


def _committed_forecast_config() -> ForecastConfig:
    """The forecast section of the *committed* config.example.yaml, not a hand-built one.

    ``test_forecast_binary.py``'s helper, for its reason: a test that builds its own bounds
    cannot notice the committed defaults drifting away from the range the prompt prints to
    the model.
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


def _payload(question_type: str = "binary", **overrides: Any) -> dict[str, Any]:
    """The prompt's shared fields composed with one of its three prediction blocks."""
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block(HEADINGS[question_type]) + "}"),
    }
    if payload["question_type"] != "binary":
        # The prompt's own rule: a non-binary response nulls both priors.
        payload["model_prior"] = None
        payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    payload["question_id"] = QUESTION_ID
    payload.update(overrides)
    return payload


def _response(question_type: str = "binary", **overrides: Any) -> ForecastResponse:
    payload = _payload(question_type, **overrides)
    return validate_forecast_response(payload, response_model_for(payload["question_type"]))


def _problems(
    forecast: ForecastResponse,
    config: ForecastConfig | None = None,
    sources: tuple[str, ...] = PROMPT_SOURCES,
) -> list[str]:
    return output_problems(
        forecast,
        config if config is not None else _committed_forecast_config(),
        question_id=QUESTION_ID,
        source_ids=sources,
    )


# --- the coverage pair: the acceptance criterion's third clause ---------------------


def test_every_supported_type_has_an_explicit_registry_entry() -> None:
    """A supported question type with **no entry** in the table is the failure this half
    catches.

    Derived from config's single source of truth (D20), never restated: adding a fourth
    member to ``SupportedQuestionType`` without deciding what validates its output fails
    here rather than reaching the forecaster with the type-specific layer silently empty.

    ``None`` is a legitimate entry and says so -- "supported, and no type-specific checks
    yet" is a decision M1-404 and M1-405 will each change one line of. What is refused is
    the *absence* of a decision.
    """
    assert set(_TYPE_CHECKERS) == set(get_args(SupportedQuestionType))
    # Both directions, the ``test_lifecycle.py`` argument: an entry for a type config does
    # not support is as much a defect as a missing one, and a subset assertion in either
    # direction alone would be green for one of them.
    assert set(get_args(SupportedQuestionType)) == set(_TYPE_CHECKERS)


def _checkers_defined_in_the_forecast_package() -> dict[str, Any]:
    """Every ``*_output_problems`` callable this package defines, by qualified name.

    Walks the package rather than reading a list, because a list is the thing that goes
    stale -- the whole defect M1-506 exists to close is a caller having to keep its own
    copy of what the checkers are.

    ``func.__module__ == module.__name__`` so a checker imported into a second module is
    counted once, at its definition site.
    """
    found: dict[str, Any] = {}
    for info in pkgutil.iter_modules(forecast_package.__path__):
        module = import_module(f"{forecast_package.__name__}.{info.name}")
        for name, obj in vars(module).items():
            if name.startswith("_") or not name.endswith("_output_problems"):
                continue
            if not inspect.isfunction(obj) or obj.__module__ != module.__name__:
                continue
            found[f"{obj.__module__}.{name}"] = obj
    return found


def test_no_output_checker_in_the_package_is_unreachable() -> None:
    """A checker that **exists and is not registered** is the failure this half catches,
    and it is the one the acceptance criterion is really about.

    M1-404 and M1-405 each write a type-specific checker and each change one line of
    ``_TYPE_CHECKERS``. If either writes the checker and forgets the line, the type
    silently keeps validating as though it had no type-specific rules -- which is exactly
    the "reading one checker in isolation, the rule looks absent" seam this item closes,
    reappearing from the other side.

    Discovery is by the ``*_output_problems`` naming convention, which every type-specific
    checker in this package follows. The entry point falls outside it for free -- it is
    named ``output_problems``, with no type prefix and so no leading underscore before
    ``output`` -- so no special case is needed to exclude it, and
    ``test_the_discovery_walk_would_notice_a_checker`` pins that. The convention is
    load-bearing here and is declared as such in ``docs/M1-NOTES.md``: a checker named off
    it escapes this test.
    """
    defined = _checkers_defined_in_the_forecast_package()
    registered = {checker for checker in _TYPE_CHECKERS.values() if checker is not None}
    unreachable = {name for name, func in defined.items() if func not in registered}
    assert unreachable == set(), sorted(unreachable)


def test_the_discovery_walk_would_notice_a_checker() -> None:
    """The test above passes trivially if the walk finds nothing. Pin what it finds today.

    ``test_forecast_generate.py::test_the_probe_would_notice_a_regression`` makes the same
    argument about its import probe: an assertion over an empty set is green for the wrong
    reason, and the cheapest guard is to name the one member that must be in it.
    """
    defined = _checkers_defined_in_the_forecast_package()
    assert "whiskeyjack_bot.forecast.binary.binary_output_problems" in defined
    assert defined["whiskeyjack_bot.forecast.binary.binary_output_problems"] is (
        binary_output_problems
    )
    # And the entry point is not itself discovered as a checker -- ``output_problems``
    # does not end in ``_output_problems``. That is what keeps the test above from
    # needing a special case, so it is asserted rather than assumed.
    assert "whiskeyjack_bot.forecast.validate.output_problems" not in defined
    assert not any(func is output_problems for func in defined.values())


def test_the_parse_path_has_no_second_composition() -> None:
    """*"defined in terms of it rather than beside it, so the two cannot diverge."*

    Asserted as an absence. ``forecast.parse`` composed the checkers privately until
    M1-506; a test comparing that function's output against this one's would prove only
    that they agree at the moment it runs. There is no second implementation to disagree.
    """
    import whiskeyjack_bot.forecast.parse as parse_module

    assert not hasattr(parse_module, "_output_problems")
    assert parse_module.output_problems is output_problems
    # And nothing else in the package composes the members behind the entry point's back.
    for info in pkgutil.iter_modules(forecast_package.__path__):
        if info.name in {"validate", "attribution", "binary"}:
            continue
        module = import_module(f"{forecast_package.__name__}.{info.name}")
        assert attribution_problems not in vars(module).values(), info.name
        assert binary_output_problems not in vars(module).values(), info.name


# --- the entry point, per question type --------------------------------------------


@pytest.mark.parametrize("question_type", sorted(HEADINGS))
def test_the_prompts_own_example_passes_for_every_supported_type(question_type: str) -> None:
    """One entry point, every supported question type, no caller-side dispatch."""
    assert _problems(_response(question_type)) == []


@pytest.mark.parametrize("question_type", sorted(HEADINGS))
def test_the_cross_type_rules_reach_every_supported_type(question_type: str) -> None:
    """M1-501's rules are not binary's, and the composition must not quietly make them so.

    The type-specific layer is empty for two of the three types today; if the attribution
    layer were reached only via that table, those two would validate nothing at all.
    """
    bad = _response(question_type, failure_modes=[], question_id=QUESTION_ID + 1)
    assert sorted(_problems(bad)) == sorted(
        [
            "question_id: must be the question this forecast was requested for "
            "(offending input withheld)",
            "failure_modes: must not be empty",
        ]
    )


def test_the_type_specific_rules_reach_binary() -> None:
    """M1-403's bounds rule, through the composed entry point rather than directly."""
    config = _committed_forecast_config().model_copy(
        update={"min_probability": 0.4, "max_probability": 0.6}
    )
    problems = _problems(_response("binary"), config)
    assert problems == [
        "final_prediction.probability_yes: must be between 0.4 and 0.6 inclusive "
        "(offending input withheld)"
    ]


def test_the_composition_runs_attribution_first_then_the_type_specific_layer() -> None:
    """The order is part of the contract: it is what a repair turn renders, and it is the
    order the two layers are documented in.

    Both layers are made to fail at once, and the composed list must be exactly the
    concatenation -- not a set, not a sort. A response that violates both is the only draw
    that can tell an ordered composition from an unordered one.
    """
    config = _committed_forecast_config().model_copy(
        update={"min_probability": 0.4, "max_probability": 0.6}
    )
    forecast = _response("binary", failure_modes=[])
    attribution_half = attribution_problems(
        forecast, question_id=QUESTION_ID, source_ids=PROMPT_SOURCES
    )
    binary_half = binary_output_problems(forecast, config)
    assert attribution_half and binary_half, "both halves must bite for this to discriminate"
    assert _problems(forecast, config) == attribution_half + binary_half


def test_a_priorless_binary_forecast_is_refused_by_the_entry_point() -> None:
    """The composition M1-501's round-1 finding was actually about, asserted publicly.

    The finding read ``attribution.py`` alone, found no prior check and reported a hole;
    the rule was in ``binary.py`` all along. A caller of this entry point cannot make that
    mistake, which is the whole of M1-506.
    """
    payload = _payload("binary")
    payload["model_prior"] = None
    payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    forecast = validate_forecast_response(payload, BinaryForecastResponse)
    assert _problems(forecast) == [
        "base_rate.prior_probability: must be supplied for a binary question",
        "model_prior: must be supplied for a binary question",
    ]


# --- the pair: returning and raising ------------------------------------------------


@pytest.mark.parametrize("question_type", sorted(HEADINGS))
def test_validate_output_returns_a_clean_response_unchanged(question_type: str) -> None:
    """Nothing is clamped, repaired or renumbered -- the same object comes back."""
    forecast = _response(question_type)
    returned = validate_output(
        forecast,
        _committed_forecast_config(),
        question_id=QUESTION_ID,
        source_ids=PROMPT_SOURCES,
    )
    assert returned is forecast


def test_validate_output_raises_with_exactly_the_problems_the_other_half_returns() -> None:
    """The two halves of the pair agree, and the raise carries the **whole** list.

    Deliberately one ``ForecastOutputError`` rather than whichever member checker fired
    first: the complete list is the entire account of why the response was refused, which
    is ``store.py::_require_attributable``'s argument for carrying the text through.
    """
    config = _committed_forecast_config().model_copy(
        update={"min_probability": 0.4, "max_probability": 0.6}
    )
    forecast = _response("binary", failure_modes=[])
    expected = _problems(forecast, config)
    assert len(expected) > 1, "one problem from each layer, or this proves nothing"

    with pytest.raises(ForecastOutputError) as caught:
        validate_output(forecast, config, question_id=QUESTION_ID, source_ids=PROMPT_SOURCES)
    assert caught.value.problems == expected
    # Catchable as the package's one response-failure type, BinaryOutputError's argument.
    assert isinstance(caught.value, ForecastSchemaError)


# --- the error boundary --------------------------------------------------------------


def test_an_unregistered_question_type_raises_this_modules_error() -> None:
    """A response whose ``question_type`` no entry covers must not pass every check in
    silence.

    Unreachable through the schema -- ``question_type`` is a validated ``Literal`` on every
    response model -- and asserted anyway, because "every malformed shape arrives as the
    module's own error type" is a rule this project has taken as a review finding twice.
    A ``.get()`` default would have made this case return ``[]``.
    """
    forecast = _response("binary")
    # Bypasses pydantic deliberately: the point is the table's behaviour, not the schema's.
    object.__setattr__(forecast, "question_type", "date")
    with pytest.raises(ForecastOutputError) as caught:
        _problems(forecast)
    assert caught.value.problems == [
        "question_type: must be one of binary, multiple_choice, numeric (offending input withheld)"
    ]


@pytest.mark.parametrize(
    "question_type",
    [None, 123, ["binary"], {"binary": 1}],
    ids=["none", "int", "unhashable-list", "unhashable-dict"],
)
def test_a_malformed_question_type_cannot_escape_as_a_raw_error(question_type: Any) -> None:
    """An unhashable value makes ``dict.get`` raise a raw ``TypeError``; the exact-type
    gate is what stops it. ``schema.response_model_for`` carries the same guard, found by
    the property suite."""
    forecast = _response("binary")
    object.__setattr__(forecast, "question_type", question_type)
    with pytest.raises(ForecastOutputError):
        _problems(forecast)


def test_a_caller_mistake_arrives_as_a_member_modules_error_not_a_raw_one() -> None:
    """The member checkers keep their own boundaries, and both subclass
    ``ForecastSchemaError`` -- so a caller handling this package's response failures as one
    type catches every route through the entry point."""
    with pytest.raises(AttributionFieldError):
        output_problems(
            "not a response",  # type: ignore[arg-type]
            _committed_forecast_config(),
            question_id=QUESTION_ID,
            source_ids=PROMPT_SOURCES,
        )
    with pytest.raises(BinaryOutputError):
        output_problems(
            _response("binary"),
            None,  # type: ignore[arg-type]
            question_id=QUESTION_ID,
            source_ids=PROMPT_SOURCES,
        )
    assert issubclass(AttributionFieldError, ForecastSchemaError)
    assert issubclass(BinaryOutputError, ForecastSchemaError)
    assert issubclass(ForecastOutputError, ForecastSchemaError)


def test_no_problem_string_and_no_traceback_echoes_the_response() -> None:
    """The project-wide rule, asserted on the composed path as well as the members.

    Both the message and the rendered traceback, because a sanitizing raise that reprints
    the value through a chained exception satisfies neither half.
    """
    forecast = _response(
        "binary",
        load_bearing_facts=[{"claim": SECRET, "source_ids": [SECRET]}],
    )
    problems = _problems(forecast)
    assert problems, "the unresolvable citation must bite"
    assert not any(SECRET in problem for problem in problems)

    with pytest.raises(ForecastOutputError) as caught:
        validate_output(
            forecast,
            _committed_forecast_config(),
            question_id=QUESTION_ID,
            source_ids=PROMPT_SOURCES,
        )
    exc = caught.value
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert SECRET not in str(exc)
    assert SECRET not in rendered
