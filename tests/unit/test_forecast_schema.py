"""M1-402: the forecaster response schema, and its agreement with the prompt.

The load-bearing test in this file is ``test_the_prompts_own_examples_validate``. The
prompt is the *only* schema instruction the model gets -- no generated JSON schema is
appended and no ``response_format`` is sent, for the reasons in
``forecast/schema.py`` -- so nothing but a test stops the prompt and these models from
drifting apart, and the drift would show up as a repair loop burning two calls per
forecast rather than as an error anyone could read.
"""

import json
import re
import traceback
import warnings
from pathlib import Path
from typing import Any, get_args

import pytest

from whiskeyjack_bot.config import SupportedQuestionType
from whiskeyjack_bot.forecast.schema import (
    MAX_RATIONALE_WORDS,
    RESPONSE_SCHEMA_VERSION,
    SUPPORTED_RESPONSE_TYPES,
    BinaryForecastResponse,
    ForecastSchemaError,
    MultipleChoiceForecastResponse,
    NumericForecastResponse,
    response_model_for,
    validate_forecast_response,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")
# Low-entropy on purpose: CI scans every branch with gitleaks, so a realistic secret
# shape here fails unrelated PRs. See the M1-301 notes.
SECRET = "privateFAKE123456"


def _json_block(heading: str) -> str:
    """The first fenced JSON block under one of the prompt's headings."""
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _shared() -> dict[str, Any]:
    return json.loads(_json_block("Shared fields"))


def _prediction(heading: str) -> dict[str, Any]:
    """A per-type block, which is a fragment rather than a whole object."""
    return json.loads("{" + _json_block(heading) + "}")


def binary_payload(**overrides: Any) -> dict[str, Any]:
    payload = {**_shared(), **_prediction("Binary schema")}
    payload.update(overrides)
    return payload


def _leaks(exc: BaseException) -> bool:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return SECRET in str(exc) or SECRET in rendered


# --- the prompt is the schema instruction, so the two must agree -----------------


@pytest.mark.parametrize(
    ("heading", "question_type", "model"),
    [
        ("Binary schema", "binary", BinaryForecastResponse),
        ("Multiple-choice schema", "multiple_choice", MultipleChoiceForecastResponse),
        ("Numeric schema", "numeric", NumericForecastResponse),
    ],
)
def test_the_prompts_own_examples_validate(
    heading: str, question_type: str, model: type[Any]
) -> None:
    """Composing the prompt's shared fields with each of its three prediction blocks
    must produce a value this schema accepts.

    The prompt's shared-fields example is written *for a binary question*: it prints a
    populated ``prior_probability`` and ``model_prior`` and a binary
    ``question_type``. So the non-binary cases null the two priors, which is not a
    workaround -- it is the prompt's own rule ("if the question is not binary,
    ``prior_probability`` and ``model_prior`` must be ``null``") applied, and
    ``test_the_prompt_examples_would_notice_a_drift`` checks that omitting the step
    fails.
    """
    payload = {**_shared(), **_prediction(heading)}
    if question_type != "binary":
        payload["model_prior"] = None
        payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    response = validate_forecast_response(payload, response_model_for(question_type))
    assert isinstance(response, model)
    assert response.question_type == question_type


@pytest.mark.parametrize("heading", ["Multiple-choice schema", "Numeric schema"])
def test_the_prompt_examples_would_notice_a_drift(heading: str) -> None:
    """The composition above passes for a reason, not by construction.

    Without the priors step the same payload must be *rejected*, which is what makes
    the previous test evidence that the schema enforces the prompt's rule rather than
    evidence that it accepts anything.
    """
    payload = {**_shared(), **_prediction(heading)}
    with pytest.raises(ForecastSchemaError):
        validate_forecast_response(payload, response_model_for(payload["question_type"]))


def test_the_prompt_declares_the_schema_version_this_module_pins() -> None:
    assert _shared()["schema_version"] == RESPONSE_SCHEMA_VERSION


def test_every_supported_question_type_has_a_response_model() -> None:
    """Derived from config, never restated: a fourth supported type must not be able
    to reach the forecaster with no model to parse its answer into."""
    assert SUPPORTED_RESPONSE_TYPES == frozenset(get_args(SupportedQuestionType))
    for question_type in SUPPORTED_RESPONSE_TYPES:
        assert response_model_for(question_type).model_fields["question_type"] is not None


def test_an_unsupported_question_type_is_refused_without_echoing_it() -> None:
    with pytest.raises(ForecastSchemaError) as excinfo:
        response_model_for(f"discrete-{SECRET}")
    assert not _leaks(excinfo.value)
    assert "binary, multiple_choice, numeric" in str(excinfo.value)


def test_a_response_cannot_claim_another_question_type() -> None:
    """The prompt's final self-check says the type must match the input; the response
    model for a binary question therefore refuses a numeric answer outright."""
    payload = {**_shared(), **_prediction("Numeric schema")}
    with pytest.raises(ForecastSchemaError):
        validate_forecast_response(payload, BinaryForecastResponse)


# --- structural rules ------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("schema_version", {"schema_version": "2.0.0"}),
        ("blank status_quo", {"status_quo": "   "}),
        ("blank rationale", {"rationale_summary": ""}),
        ("repeated tag", {"reasoning_strategy_tags": ["base_rate", "base_rate"]}),
        ("unknown tag", {"reasoning_strategy_tags": ["vibes"]}),
        (
            "unknown direction",
            {
                "evidence_adjustments": [
                    {
                        "claim": "c",
                        "direction": "sideways",
                        "magnitude": "small",
                        "source_ids": [],
                        "load_bearing": False,
                    }
                ]
            },
        ),
        (
            "unknown magnitude",
            {
                "evidence_adjustments": [
                    {
                        "claim": "c",
                        "direction": "up",
                        "magnitude": "enormous",
                        "source_ids": [],
                        "load_bearing": False,
                    }
                ]
            },
        ),
        ("probability above one", {"final_prediction": {"probability_yes": 1.5}}),
        ("probability not a number", {"final_prediction": {"probability_yes": float("nan")}}),
        ("confidence above one", {"process_confidence": 2.0}),
        ("naive as_of", {"as_of_utc": "2026-07-09T18:00:00"}),
        ("blank source id", {"load_bearing_facts": [{"claim": "f", "source_ids": ["  "]}]}),
        ("extra top-level field", {"extra_field": 1}),
    ],
)
def test_a_structurally_invalid_response_is_refused(label: str, overrides: Any) -> None:
    with pytest.raises(ForecastSchemaError):
        validate_forecast_response(binary_payload(**overrides), BinaryForecastResponse)


def test_the_rationale_word_cap_is_the_prompts_number_and_is_exact() -> None:
    at_cap = binary_payload(rationale_summary=" ".join(["word"] * MAX_RATIONALE_WORDS))
    assert validate_forecast_response(at_cap, BinaryForecastResponse) is not None
    over = binary_payload(rationale_summary=" ".join(["word"] * (MAX_RATIONALE_WORDS + 1)))
    with pytest.raises(ForecastSchemaError):
        validate_forecast_response(over, BinaryForecastResponse)
    assert f"no more than {MAX_RATIONALE_WORDS} words" in PROMPT_TEXT


def test_a_binary_response_may_carry_priors() -> None:
    """The converse of the prompt's rule is deliberately *not* enforced here: whether
    a binary response must supply a prior is a presence requirement over the
    attribution fields, which is M1-501's row. Both spellings validate."""
    assert validate_forecast_response(binary_payload(), BinaryForecastResponse) is not None
    without = binary_payload(model_prior=None)
    without["base_rate"] = {**without["base_rate"], "prior_probability": None}
    assert validate_forecast_response(without, BinaryForecastResponse) is not None


def test_configured_probability_bounds_are_not_applied_here() -> None:
    """0.9995 is outside forecast.max_probability (0.999) and inside this schema.

    Not an oversight: applying the configured bounds is M1-403's stated acceptance
    criterion, and this module reads no config at all. A test pins the boundary so the
    split is visible rather than inferred from an absence.
    """
    payload = binary_payload(final_prediction={"probability_yes": 0.9995})
    assert validate_forecast_response(payload, BinaryForecastResponse) is not None


def test_multiple_choice_option_identity_is_not_checked_here() -> None:
    """Options that duplicate, that sum to well over one, and that name nothing the
    question offered all validate: every one of those needs the question's option
    list, and M1-404 owns them."""
    payload = {**_shared(), **_prediction("Multiple-choice schema")}
    payload["model_prior"] = None
    payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    payload["final_prediction"] = {
        "options": [
            {"option": "Anything", "probability": 0.9},
            {"option": "Anything", "probability": 0.9},
        ]
    }
    assert validate_forecast_response(payload, MultipleChoiceForecastResponse) is not None


def test_numeric_percentile_levels_are_not_checked_here() -> None:
    """Three out-of-order percentiles validate; the nine exact levels and their
    ordering are M1-405's criterion, and both need the question's bounds."""
    payload = {**_shared(), **_prediction("Numeric schema")}
    payload["model_prior"] = None
    payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    payload["final_prediction"] = {
        "percentiles": [
            {"percentile": 0.5, "value": 10.0},
            {"percentile": 0.1, "value": 99.0},
        ]
    }
    assert validate_forecast_response(payload, NumericForecastResponse) is not None


# --- the sanitizer ---------------------------------------------------------------


def test_a_nested_field_name_this_schema_authored_survives_in_the_message() -> None:
    """``research/model.py``'s sanitizer collects only top-level field names, which is
    right for its two flat models. This response is four levels deep, so the same rule
    applied naively renders ``<withheld>.<withheld>`` and the diagnostic is unusable."""
    payload = binary_payload()
    payload["base_rate"] = {**payload["base_rate"], "prior_probability": 5.0}
    with pytest.raises(ForecastSchemaError) as excinfo:
        validate_forecast_response(payload, BinaryForecastResponse)
    assert "base_rate.prior_probability" in str(excinfo.value)


def test_a_key_the_model_invented_is_withheld_at_every_depth() -> None:
    for payload in (
        binary_payload(**{SECRET: 1}),
        binary_payload(base_rate={**_shared()["base_rate"], SECRET: 1}),
        binary_payload(final_prediction={"probability_yes": 0.4, SECRET: 1}),
    ):
        with pytest.raises(ForecastSchemaError) as excinfo:
            validate_forecast_response(payload, BinaryForecastResponse)
        assert not _leaks(excinfo.value)
        assert "<withheld>" in str(excinfo.value)


def test_a_list_index_survives_because_the_schema_authored_it() -> None:
    payload = binary_payload(failure_modes=["fine", "   "])
    with pytest.raises(ForecastSchemaError) as excinfo:
        validate_forecast_response(payload, BinaryForecastResponse)
    assert "failure_modes.1" in str(excinfo.value)


@pytest.mark.parametrize(
    "field",
    ["status_quo", "rationale_summary", "schema_version"],
)
def test_a_planted_secret_in_a_field_value_reaches_no_channel(field: str) -> None:
    """Watches three channels at once, the pattern both adapter suites carry: the
    raised exception, its rendered traceback, and pydantic serializer warnings --
    which embed the offending value in their text and so are an egress channel rather
    than noise."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            validate_forecast_response(binary_payload(**{field: SECRET}), BinaryForecastResponse)
        except ForecastSchemaError as exc:
            assert not _leaks(exc)
    assert all(SECRET not in str(w.message) for w in caught)


def test_the_leak_test_is_watching_a_field_that_can_actually_fail() -> None:
    """``status_quo`` and ``rationale_summary`` accept the planted string, so only the
    ``schema_version`` case above raises at all. Without this the parametrize would
    read as three leak checks when it is one -- the vacuity M1-308 round 4 named."""
    with pytest.raises(ForecastSchemaError):
        validate_forecast_response(binary_payload(schema_version=SECRET), BinaryForecastResponse)
    assert validate_forecast_response(binary_payload(status_quo=SECRET), BinaryForecastResponse)
