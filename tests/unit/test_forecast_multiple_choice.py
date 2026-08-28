"""M1-404 acceptance: every exact option once, with probabilities summing to one.

The criterion names three failures and a tolerance -- *"Unknown/missing/duplicate options
fail validation; tolerance is 1e-6"* -- and the owner settled a fifth rule onto this row:
the per-option bounds ``prompts/forecaster.md`` already prints to the model. Each of the
five is tested in isolation and then together, because a checker that reports only the
first problem it finds costs a repair turn per rule.

The reply under test is the prompt's **own** multiple-choice example, and the question's
option list is read back out of the same block, so the two are a matched pair by
construction. A hand-written fixture could drift away from the prompt and leave every test
here green against a reply no model would send.

The comprehensive valid/invalid golden set is Codex's **T-901**, authored blind from spec.
Nothing here pre-writes it.
"""

import copy
import json
import re
import traceback
from math import nextafter
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
import yaml

from whiskeyjack_bot.config import ForecastConfig, validate_config_data
from whiskeyjack_bot.forecast.binary import BinaryOutputError, binary_output_problems
from whiskeyjack_bot.forecast.multiple_choice import (
    _SUM_TOLERANCE,
    MultipleChoiceOutputError,
    multiple_choice_output_problems,
    validate_multiple_choice_output,
)
from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalMultipleChoiceQuestion,
)
from whiskeyjack_bot.forecast.schema import (
    ForecastSchemaError,
    MultipleChoiceForecastResponse,
    validate_forecast_response,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")

# Low-entropy on purpose: CI scans every branch with gitleaks, so a realistic secret shape
# here fails unrelated PRs. See the M1-301 notes.
SECRET = "privateFAKE123456"

_UNKNOWN = (
    "final_prediction.options: must name only options the question supplied "
    "(offending labels withheld)"
)
_MISSING = (
    "final_prediction.options: must name every option the question supplied "
    "(offending labels withheld)"
)
_DUPLICATE = (
    "final_prediction.options: must name each supplied option at most once "
    "(offending labels withheld)"
)
_SUM = "final_prediction.options: probabilities must sum to 1 within 1e-06 (observed sum withheld)"


def _bounds_problem(low: float, high: float) -> str:
    return (
        f"final_prediction.options: each probability must be between {low!r} and "
        f"{high!r} inclusive (offending input withheld)"
    )


def _committed_forecast_config() -> ForecastConfig:
    """The forecast section of the *committed* config.example.yaml, not a hand-built one.

    ``test_forecast_binary.py``'s helper, for its reason: a test that builds its own bounds
    cannot notice the committed defaults drifting away from the range the prompt prints to
    the model.
    """
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    # The one substitution the committed example demands of every caller (D27).
    data["model"]["name"] = "openrouter/test-model"
    return validate_config_data(data).forecast


def _narrowed(minimum: float, maximum: float) -> ForecastConfig:
    return _committed_forecast_config().model_copy(
        update={"min_probability": minimum, "max_probability": maximum}
    )


def _json_block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _prompt_payload() -> dict[str, Any]:
    """The prompt's shared fields composed with its multiple-choice prediction block."""
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block("Multiple-choice schema") + "}"),
    }
    # The prompt's own rule, which ``schema.py`` enforces on this response type.
    payload["model_prior"] = None
    payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    return payload


# The two labels the prompt's own example answers, and their probabilities (0.55/0.45).
PROMPT_OPTIONS: tuple[str, ...] = tuple(
    entry["option"] for entry in _prompt_payload()["final_prediction"]["options"]
)


def _response(options: list[dict[str, Any]] | None = None) -> MultipleChoiceForecastResponse:
    payload = _prompt_payload()
    if options is not None:
        payload["final_prediction"] = {"options": options}
    forecast = validate_forecast_response(payload, MultipleChoiceForecastResponse)
    assert isinstance(forecast, MultipleChoiceForecastResponse)
    return forecast


def _answers(*pairs: tuple[str, float]) -> list[dict[str, Any]]:
    return [{"option": option, "probability": probability} for option, probability in pairs]


def _question(options: Any = PROMPT_OPTIONS) -> CanonicalMultipleChoiceQuestion:
    """The canonical question this checker reads its option list from.

    It used to be handed the labels as bare primitives. At the M1-404/M1-405 merge the
    checker took the validated question instead, so the option list a test wants is now
    expressed by building the question that carries it -- and every shape the old
    primitive guard refused is refused here, by the model, at the input contract.
    """
    return CanonicalMultipleChoiceQuestion(
        question_id=123,
        post_id=456,
        title="Which option?",
        options=list(options),
    )


def _problems(
    forecast: MultipleChoiceForecastResponse,
    options: Any = PROMPT_OPTIONS,
    config: ForecastConfig | None = None,
) -> list[str]:
    return multiple_choice_output_problems(
        forecast,
        config if config is not None else _committed_forecast_config(),
        _question(options),
    )


A, B = PROMPT_OPTIONS


# --- the acceptance criterion ---------------------------------------------------------


def test_the_prompts_own_example_validates_against_its_own_option_list() -> None:
    """The criterion's happy path, and the one that decides whether this rule is
    satisfiable at all.

    A checker whose rule a compliant reply cannot pass bills two calls per question to
    discover, which is what ``binary._require_config`` refuses for an inverted bounds pair.
    The prompt tells the model exactly this shape, so this must be silent.
    """
    forecast = _response()
    assert _problems(forecast) == []
    assert (
        validate_multiple_choice_output(forecast, _committed_forecast_config(), _question())
        is forecast
    )


def test_an_unknown_option_is_refused() -> None:
    """A label the question never supplied. The reply is otherwise a valid distribution,
    so nothing but this rule may bite."""
    forecast = _response(_answers((A, 0.55), ("Some third option", 0.45)))
    assert _problems(forecast) == [_UNKNOWN, _MISSING]


def test_a_missing_option_is_refused() -> None:
    """A supplied label the reply never names, isolated from the other four rules.

    Three options rather than two: with two, a reply naming one of them cannot be both
    inside [0.001, 0.999] and sum to 1, so the bounds and sum rules would fire as well and
    this would not be a test of *missing*.
    """
    forecast = _response(_answers((A, 0.5), (B, 0.5)))
    assert _problems(forecast, options=(A, B, "A third option")) == [_MISSING]


def test_a_duplicate_option_is_refused() -> None:
    """The same label twice. Both halves of the option set are present, so *unknown* and
    *missing* stay silent and only the duplicate rule bites."""
    forecast = _response(_answers((A, 0.3), (B, 0.4), (A, 0.3)))
    assert _problems(forecast) == [_DUPLICATE]


def test_an_out_of_bounds_probability_is_refused() -> None:
    """The owner's fifth rule, against a **non-default** config -- the case that proves
    ``forecast.min_probability`` has a consumer here rather than a default that happens to
    agree with the prompt."""
    forecast = _response(_answers((A, 0.95), (B, 0.05)))
    assert _problems(forecast, config=_narrowed(0.1, 0.9)) == [_bounds_problem(0.1, 0.9)]
    # ...and the same reply is fine under the committed bounds.
    assert _problems(forecast) == []


def test_a_vector_that_is_not_a_distribution_is_refused() -> None:
    forecast = _response(_answers((A, 0.6), (B, 0.6)))
    assert _problems(forecast) == [_SUM]


def test_the_tolerance_is_applied_where_the_criterion_says() -> None:
    """One ulp on either side of 1e-6, which is as tight as this can truthfully be stated.

    There is **no** probability vector whose distance from 1 is exactly ``1e-6``: neither
    ``1.0 + 1e-6`` nor ``1.0 - 1e-6`` round-trips to that difference, so the boundary case
    a strict-versus-inclusive comparison would disagree about does not exist in the float
    space. These two draws straddle it by a single ulp, which pins the comparison to the
    right place even though nothing can pin its strictness. Recorded in docs/M1-NOTES.md.
    """
    just_inside = (1.0 + _SUM_TOLERANCE) - 0.5
    just_outside = nextafter(1.0 + _SUM_TOLERANCE, 2.0) - 0.5
    assert just_inside != just_outside

    assert _problems(_response(_answers((A, 0.5), (B, just_inside)))) == []
    assert _problems(_response(_answers((A, 0.5), (B, just_outside)))) == [_SUM]


def test_matching_is_exact_string_equality() -> None:
    """*"every exact option"*. Nothing is stripped, case-folded or Unicode-normalized.

    A near-miss is one *unknown* plus one *missing*, not a match: a rule that quietly
    accepted ``" A"`` for ``"A"`` would store a forecast against a label the platform will
    not score, and the disagreement would be invisible in the ledger.
    """
    for near_miss in (f" {A}", f"{A} ", A.upper(), A.lower(), A.replace(" ", " ")):
        if near_miss == A:
            continue
        forecast = _response(_answers((near_miss, 0.55), (B, 0.45)))
        assert _problems(forecast) == [_UNKNOWN, _MISSING], near_miss


# --- the shape of the answer ----------------------------------------------------------


def test_every_problem_is_reported_at_once() -> None:
    """All five rules on one reply, in the criterion's own order.

    Reporting only the first would cost one repair turn per rule, and the loop budgets
    one in total.
    """
    forecast = _response(_answers((A, 0.9995), ("unknown one", 0.9995), ("unknown one", 0.9995)))
    assert _problems(forecast) == [
        _UNKNOWN,
        _MISSING,
        _DUPLICATE,
        _bounds_problem(0.001, 0.999),
        _SUM,
    ]


def test_nothing_is_clamped_dropped_or_renormalized() -> None:
    """M1-502's criterion is that "no arbitrary post-hoc renormalization is hidden", and
    the prompt says "do not clamp mechanically". The response comes back untouched, and a
    refused one is refused rather than repaired."""
    forecast = _response(_answers((A, 0.6), (B, 0.6)))
    before = forecast.model_dump(mode="json")
    problems = _problems(forecast)
    assert problems == [_SUM]
    assert forecast.model_dump(mode="json") == before

    good = _response()
    returned = validate_multiple_choice_output(good, _committed_forecast_config(), _question())
    assert returned is good
    assert [entry.probability for entry in returned.final_prediction.options] == [0.55, 0.45]


def test_the_raising_entry_point_carries_the_same_problems() -> None:
    forecast = _response(_answers((A, 0.6), ("unknown one", 0.6)))
    expected = _problems(forecast)
    assert len(expected) > 1, "more than one problem, or this proves nothing"
    with pytest.raises(MultipleChoiceOutputError) as caught:
        validate_multiple_choice_output(forecast, _committed_forecast_config(), _question())
    assert caught.value.problems == expected
    # Catchable as the package's one response-failure type, BinaryOutputError's argument.
    assert isinstance(caught.value, ForecastSchemaError)


def test_the_verdict_does_not_depend_on_the_order_the_model_answered_in() -> None:
    """``math.fsum`` rather than ``sum``, stated as the property it buys.

    A verdict that moved with answer order would not be one a replay could reproduce, and
    the option list is a set as far as every rule here is concerned.
    """
    pairs = [(A, 0.55), (B, 0.45)]
    assert _problems(_response(_answers(*pairs))) == _problems(
        _response(_answers(*reversed(pairs)))
    )


# --- the leak rules -------------------------------------------------------------------


def test_two_different_offending_label_sets_produce_identical_text() -> None:
    """The leak property as invariance, not as substring absence.

    ``test_forecast_binary.py``'s idiom. A message built by interpolating the offending
    label would differ between these two; a message that merely avoided the *substring*
    could still be built from it.
    """
    first = _problems(_response(_answers(("first bogus label", 0.55), (B, 0.45))))
    second = _problems(_response(_answers(("an entirely different one", 0.55), (B, 0.45))))
    assert first == second
    assert first == [_UNKNOWN, _MISSING]


def test_the_number_of_offending_labels_does_not_change_the_text() -> None:
    """The count is a channel too (M1-302's rule), and it is the one an aggregating
    checker exists to close. One bad label and three produce the same single problem."""
    one = _problems(_response(_answers(("x", 0.5), (B, 0.5))))
    three = _problems(_response(_answers(("x", 0.25), ("y", 0.25), ("z", 0.5))))
    assert one == three == [_UNKNOWN, _MISSING]


def test_no_problem_string_or_traceback_echoes_a_label() -> None:
    """Both the message and the rendered traceback, because a sanitizing raise that
    reprints the value through a chained exception satisfies neither half. The supplied
    labels are checked as well as the answered ones: they reach this function from a
    caller, and M1-501 declines to render those for the same reason."""
    forecast = _response(_answers((SECRET, 0.55), (f"{SECRET}-other", 0.45)))
    problems = _problems(forecast, options=(f"{SECRET}-supplied", f"{SECRET}-supplied-2"))
    assert problems, "the option set must bite"
    assert not any(SECRET in problem for problem in problems)

    with pytest.raises(MultipleChoiceOutputError) as caught:
        validate_multiple_choice_output(
            forecast,
            _committed_forecast_config(),
            _question((f"{SECRET}-supplied", f"{SECRET}-supplied-2")),
        )
    exc = caught.value
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert SECRET not in str(exc)
    assert SECRET not in rendered


# --- caller mistakes, which must never become a repair turn ---------------------------


def test_a_response_of_another_question_type_is_a_caller_mistake() -> None:
    """Exact category, not a duck-typed read. ``validate.output_problems`` keys the table
    on the question-type literal, so this is unreachable through the entry point -- and
    asserted anyway, because a mis-keyed entry must surface as this module's own error
    rather than as a wrong answer."""
    from whiskeyjack_bot.forecast.schema import BinaryForecastResponse, NumericForecastResponse

    payload = {**_prompt_payload()}
    for model, block, question_type in (
        (BinaryForecastResponse, "Binary schema", "binary"),
        (NumericForecastResponse, "Numeric schema", "numeric"),
    ):
        other = {
            **payload,
            **json.loads("{" + _json_block(block) + "}"),
        }
        other["question_type"] = question_type
        if question_type == "binary":
            other["model_prior"] = 0.5
            other["base_rate"] = {**other["base_rate"], "prior_probability": 0.5}
        response = validate_forecast_response(other, model)
        with pytest.raises(MultipleChoiceOutputError) as caught:
            multiple_choice_output_problems(
                response,  # type: ignore[arg-type]
                _committed_forecast_config(),
                _question(),
            )
        assert caught.value.problems == ["forecast: must be a multiple-choice forecast response"]

    with pytest.raises(MultipleChoiceOutputError):
        multiple_choice_output_problems(
            "not a response",  # type: ignore[arg-type]
            _committed_forecast_config(),
            _question(),
        )


@pytest.mark.parametrize(
    ("options", "error_type"),
    [
        ([], "too_short"),
        (["A"], "too_short"),
        (["A", "A"], "value_error"),
        (["A", "  "], "value_error"),
        (["A", 2], "string_type"),
    ],
    ids=["empty", "singleton", "repeated", "blank", "non-str-member"],
)
def test_the_option_shapes_that_would_make_a_rule_lie_are_refused_by_the_question(
    options: Any, error_type: str
) -> None:
    """Where the guarantee lives now, and why this test moved rather than vanished.

    Until the M1-404/M1-405 merge this checker was handed the option list as bare
    primitives with no validator behind them, so it re-checked each of these itself and
    this test asserted its own ``MultipleChoiceOutputError`` for each. **M1-404's round-1
    review found a hole in exactly that arrangement**: five of the model's six invariants
    were mirrored by hand and the sixth, ``min_length=2``, was not, which left a singleton
    option list producing a rule set no reply could satisfy.

    The checker now takes the validated question, so the mirror is gone and the model is
    the single place these hold. The cases are kept, pointed at their real enforcement
    point -- a shape that reaches ``CanonicalMultipleChoiceQuestion`` is refused there, and
    the checker can no longer be handed one. Losing these cases entirely is what would let
    the guarantee quietly weaken.
    """
    with pytest.raises(ValidationError) as caught:
        CanonicalMultipleChoiceQuestion(
            question_id=123, post_id=456, title="Which option?", options=options
        )
    # The error *type*, not a substring of pydantic's prose: the wording is the library's
    # to change and the type is the contract.
    assert [error["type"] for error in caught.value.errors()] == [error_type]


def test_a_question_of_another_type_is_a_caller_mistake() -> None:
    """The one gate ``_require_question`` still makes for itself.

    ``numeric._require_question``'s shape: refuse the argument this module cannot read,
    as this module's own error type rather than as a raw ``AttributeError`` reaching a
    caller. Everything about the *contents* of the option list is the model's job now.
    """
    for question in (
        None,
        "not a question",
        123,
        CanonicalBinaryQuestion(question_id=123, post_id=456, title="Will it?"),
    ):
        with pytest.raises(MultipleChoiceOutputError) as caught:
            multiple_choice_output_problems(
                _response(),
                _committed_forecast_config(),
                question,  # type: ignore[arg-type]
            )
        assert caught.value.problems == ["question: must be a canonical multiple-choice question"]


def test_no_reply_to_a_one_option_question_could_ever_have_passed() -> None:
    """Why a singleton option list is refused at the contract rather than returned.

    M1-404's round 1 found the missing arity guard; this pins the *reason* it mattered, and
    it survives the merge because the reason is about this module's rules rather than about
    where the list comes from. For a one-option list the *sum* and *bounds* rules cover the
    whole line between them and leave no gap: summing to 1 within ``_SUM_TOLERANCE`` forces
    the sole probability to at least ``1 - _SUM_TOLERANCE``, already above
    ``max_probability``. So a returned problem would have asked for a repair no model could
    perform -- the failure ``_require_config`` refuses for an inverted bounds pair.

    Stated against the *committed* bounds, for ``_committed_forecast_config``'s reason: if a
    future config narrowed the gap the two rules would stop covering the line, and this
    assertion is what would notice.
    """
    config = _committed_forecast_config()
    assert config.max_probability < 1.0 - _SUM_TOLERANCE, (
        "the sum and bounds rules no longer cover the line between them, so a one-option "
        "question may have become answerable -- re-derive the argument before relying on it"
    )


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (None, "forecast_config: must be a ForecastConfig"),
        ("not a config", "forecast_config: must be a ForecastConfig"),
    ],
    ids=["none", "str"],
)
def test_a_config_of_the_wrong_type_is_a_caller_mistake(config: Any, expected: str) -> None:
    # Not through ``_problems``: its ``config is None`` default would substitute the real
    # one and quietly turn the ``None`` case into a test of nothing.
    with pytest.raises(MultipleChoiceOutputError) as caught:
        multiple_choice_output_problems(_response(), config, _question())
    assert caught.value.problems == [expected]


def test_this_module_and_binary_refuse_the_same_inverted_config() -> None:
    """The pin on the one deliberate duplication in this module.

    ``_require_config`` and ``_format_bound`` are copied from ``binary.py`` rather than
    shared, because every module here owns its own sanitized exception and a shared helper
    would have to be parameterized by the error type it raises. This asserts the two copies
    agree on both halves -- the refusal and the rendered bound -- so a change to one that
    is not made to the other fails here rather than in a review.
    """
    inverted = _committed_forecast_config().model_copy(
        update={"min_probability": 0.9, "max_probability": 0.1}
    )
    message = "forecast_config: min_probability must be strictly below max_probability"

    with pytest.raises(MultipleChoiceOutputError) as mine:
        _problems(_response(), config=inverted)
    assert mine.value.problems == [message]

    from whiskeyjack_bot.forecast.schema import BinaryForecastResponse

    binary_payload = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block("Binary schema") + "}"),
        # Outside the narrowed pair below, so the bounds half of the comparison is
        # reached. The prompt's own example probability sits inside it.
        "final_prediction": {"probability_yes": 0.9},
    }
    binary = validate_forecast_response(binary_payload, BinaryForecastResponse)
    with pytest.raises(BinaryOutputError) as theirs:
        binary_output_problems(
            binary,
            inverted,  # type: ignore[arg-type]
            CanonicalBinaryQuestion(question_id=123, post_id=456, title="Will it?"),
        )
    assert theirs.value.problems == [message]

    # And the two render a bound the same way, which is the other half of the copy.
    narrowed = _narrowed(0.2, 0.8)
    assert _bounds_problem(0.2, 0.8).endswith(
        "between 0.2 and 0.8 inclusive (offending input withheld)"
    )
    mine_problems = _problems(_response(_answers((A, 0.9), (B, 0.1))), config=narrowed)
    theirs_problems = binary_output_problems(
        binary,
        narrowed,
        CanonicalBinaryQuestion(question_id=123, post_id=456, title="Will it?"),
    )
    assert any("between 0.2 and 0.8 inclusive" in problem for problem in mine_problems)
    assert any("between 0.2 and 0.8 inclusive" in problem for problem in theirs_problems)
