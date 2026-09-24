"""M1-512: ``multiple_choice.admits_a_distribution`` against the checker it predicts.

The function answers one question before any billable call: can *any* reply over ``n``
options pass ``multiple_choice_output_problems``' bounds and sum rules under this config?
It is a pure function feeding a refusal, so it gets the CLAUDE.md pre-review pass:

- **totality** -- any count and any config raise only ``MultipleChoiceOutputError``;
- **no value leak** -- a refusal's message never varies with the value that failed;
- **the claim itself, both directions**, against the *checker* rather than a restatement of
  it: when admitted, a concrete reply passes the checker; when refused, the three vectors
  that bound every admissible one (all at the floor, all at the ceiling, uniform) are all
  refused by the checker;
- **a second, exact statement** of the same condition in :class:`~fractions.Fraction`,
  ``n * min <= 1 + tol`` and ``n * max >= 1 - tol``, asserted wherever it is decisive.

``replay-stability`` is vacuous here in the usual sense -- the inputs are an ``int`` and a
config, and nothing is persisted -- so it is stated as determinism across a config that has
been dumped and re-validated, which is the form a replayed run would hand it.

Strategy reach is measured with ``event``: counts are drawn *at* each side's edge
(``floor(1/min)``, ``floor(1/min) + 1``, ``ceil(1/max) - 1``, ``ceil(1/max)``), because a
count drawn uniformly almost never lands where the two answers part. Edges above 250 options
are not drawn -- the committed default's is 1000, and building a thousand-option reply per
draw costs minutes -- so a floor below 0.004 is exercised only on its admitted side here;
``tests/unit/test_forecast_generate.py`` refuses the row's own 21-at-0.05 example.
"""

from __future__ import annotations

import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from hypothesis import assume, event, given, settings
from hypothesis import strategies as st

from whiskeyjack_bot.config import ForecastConfig
from whiskeyjack_bot.forecast.multiple_choice import (
    _SUM_TOLERANCE,
    MultipleChoiceOutputError,
    admits_a_distribution,
    multiple_choice_output_problems,
)
from whiskeyjack_bot.forecast.schema import (
    MultipleChoiceForecastResponse,
    validate_forecast_response,
)
from whiskeyjack_bot.questions.model import CanonicalMultipleChoiceQuestion

GOLDEN: Final = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "forecasts"
        / "multiple_choice_golden.json"
    ).read_text(encoding="utf-8")
)
_COUNT_MESSAGE: Final = "question: option count must be an int of at least 2"
_REFUSALS: Final = {
    _COUNT_MESSAGE,
    "forecast_config: min_probability and max_probability must lie within 0.001 and 0.999 "
    "inclusive (configured pair withheld)",
    "forecast_config: min_probability must be strictly below max_probability",
}


def _config(low: float, high: float) -> ForecastConfig:
    """A *validated* config with the drawn pair (``ForecastConfig`` runs its validators)."""
    return ForecastConfig(
        supported_question_types=["binary", "multiple_choice", "numeric"],
        min_probability=low,
        max_probability=high,
        community_prediction_policy="log_after_forecast_do_not_use_as_input",
        replay_saved_model_output=False,
        fail_on_stale_research=False,
        flag_on_stale_research=True,
        prompt_path="prompts/forecaster.md",
        prompt_version="1.1.0",
    )


def _question(count: int) -> CanonicalMultipleChoiceQuestion:
    return CanonicalMultipleChoiceQuestion(
        question_id=123,
        post_id=456,
        title="Which option?",
        options=[f"Option {index}" for index in range(count)],
    )


def _reply(count: int, probability: float) -> MultipleChoiceForecastResponse:
    """Every option at ``probability`` -- the checker is asked about the vector, not labels."""
    payload = json.loads(json.dumps(GOLDEN))
    payload["final_prediction"] = {
        "options": [
            {"option": f"Option {index}", "probability": probability} for index in range(count)
        ]
    }
    reloaded = validate_forecast_response(payload, MultipleChoiceForecastResponse)
    assert isinstance(reloaded, MultipleChoiceForecastResponse)
    return reloaded


_LOWS: Final = [0.001, 0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 1 / 3, 0.3]
_HIGHS: Final = [0.999, 0.9, 0.6, 0.5, 0.4, 0.34, 1 / 3, 0.3, 0.26]


@st.composite
def _cases(draw: Any) -> tuple[int, float, float]:
    if draw(st.integers(0, 3)) == 0:
        # Inside the sum tolerance's own band: one side of the pair placed a fraction of
        # ``_SUM_TOLERANCE`` past (or short of) exactly ``1 / count``. Where a reply that
        # misses 1 by less than the tolerance is still accepted, so an answer that ignored
        # the tolerance -- "the witness sums to exactly 1" -- is wrong only here, and a
        # uniform draw essentially never lands here (the ``ignore-tolerance`` mutant
        # survived the first version of this strategy).
        count = draw(st.integers(3, 200))
        offset = draw(st.sampled_from([0.4, 0.9, 1.1, 3.0])) * _SUM_TOLERANCE
        if draw(st.booleans()):
            return count, (1 + offset) / count, 0.999
        return count, 0.001, (1 - offset) / count
    low = draw(st.one_of(st.sampled_from(_LOWS), st.floats(0.001, 0.5)))
    high = draw(st.one_of(st.sampled_from(_HIGHS), st.floats(0.001, 0.999)))
    assume(low < high)
    edges = [
        math.floor(1 / low),
        math.floor(1 / low) + 1,
        math.ceil(1 / high) - 1,
        math.ceil(1 / high),
    ]
    count = draw(
        st.one_of(
            st.sampled_from([edge for edge in edges if 2 <= edge <= 250] or [2]),
            st.integers(2, 40),
        )
    )
    return count, low, high


def _exact(count: int, low: float, high: float) -> tuple[Fraction, Fraction]:
    """How far past each side's limit the pair is, in exact arithmetic (<= 0 is inside)."""
    tolerance = Fraction(_SUM_TOLERANCE)
    return (
        Fraction(count) * Fraction(low) - (1 + tolerance),
        (1 - tolerance) - Fraction(count) * Fraction(high),
    )


@settings(max_examples=300, deadline=None)
@given(_cases())
def test_the_answer_is_the_checkers_answer_on_the_bounding_vectors(
    case: tuple[int, float, float],
) -> None:
    count, low, high = case
    config = _config(low, high)
    question = _question(count)
    admitted = admits_a_distribution(count, config)
    event(f"admitted={admitted}")
    uniform = min(max(1 / count, low), high)
    if admitted:
        assert multiple_choice_output_problems(_reply(count, uniform), config, question) == []
    else:
        for probability in {low, high, uniform}:
            assert multiple_choice_output_problems(_reply(count, probability), config, question)


@settings(max_examples=500, deadline=None)
@given(_cases())
def test_the_answer_matches_the_exact_condition_wherever_it_is_decisive(
    case: tuple[int, float, float],
) -> None:
    """A second statement of the rule, in exact arithmetic. Within ``1e-12`` of an edge the
    float sum the checker computes and the exact sum can round to different sides, and
    there the checker (the property above) is the authority; everywhere else they agree."""
    count, low, high = case
    over_floor, under_ceiling = _exact(count, low, high)
    margin = Fraction(1, 10**12)
    decisive = abs(over_floor) > margin and abs(under_ceiling) > margin
    admitted = admits_a_distribution(count, _config(low, high))
    side = "floor" if over_floor > 0 else "ceiling" if under_ceiling > 0 else "inside"
    event(f"{side}; decisive={decisive}")
    if decisive:
        assert admitted == (over_floor <= 0 and under_ceiling <= 0)


_ANY_COUNT = st.one_of(
    st.integers(-3, 3), st.integers(), st.booleans(), st.floats(), st.text(max_size=3), st.none()
)
_ANY_BOUND = st.one_of(st.floats(), st.floats(0.0, 1.0), st.integers(-1, 2))
# Half the draws are a coherent triple -- a real count, including one no list could hold,
# and an in-envelope ordered pair -- so the answering branch is reached rather than left to
# three independent draws all happening to be valid (1.5% when it was).
_COHERENT = st.tuples(
    st.one_of(st.integers(2, 60), st.just(10**400)),
    st.sampled_from([(0.001, 0.999), (0.05, 0.95), (0.3, 0.5), (0.001, 0.4)]),
).map(lambda drawn: (drawn[0], *drawn[1]))


@given(st.one_of(_COHERENT, st.tuples(_ANY_COUNT, _ANY_BOUND, _ANY_BOUND)))
def test_totality_and_a_constant_refusal(case: tuple[Any, Any, Any]) -> None:
    """Only this module's error, and no refusal message carries the value that failed.

    The config is built by ``model_copy``, the path that skips ``ForecastConfig``'s
    validators, so out-of-envelope, inverted, NaN and non-float pairs all reach
    ``_require_config``. ``10**400`` is in the strategy because a count is a plain ``int``
    with no upper bound: the answer must neither overflow a float nor iterate that many
    times (the first version summed ``n`` copies and would not have returned).
    """
    count, low, high = case
    config = _config(0.001, 0.999).model_copy(
        update={"min_probability": low, "max_probability": high}
    )
    try:
        result = admits_a_distribution(count, config)
    except MultipleChoiceOutputError as exc:
        event("refused")
        # A closed vocabulary: every refusal is one of three constant strings, so none can
        # carry the count or the pair that failed.
        assert set(exc.problems) <= _REFUSALS
        return
    event(f"answered {result}; huge={count == 10**400}")
    assert type(result) is bool


@settings(deadline=None)
@given(_cases())
def test_the_answer_survives_the_config_being_persisted_and_reloaded(
    case: tuple[int, float, float],
) -> None:
    count, low, high = case
    config = _config(low, high)
    reloaded = ForecastConfig.model_validate(
        json.loads(json.dumps(config.model_dump(mode="json"), ensure_ascii=True, sort_keys=True))
    )
    assert admits_a_distribution(count, reloaded) == admits_a_distribution(count, config)
