"""Invariants of the question-only refusal (M1-364).

``forecast.generate.question_refusal`` answers, before anything is bought, the question
``generate_forecast`` would otherwise answer only after retrieval: is there any reply this
configuration could accept for this question? ``pipeline_live`` acts on that answer by not
buying research, and the tournament worker by recording a deterministic verdict, so a wrong
answer in either direction costs something real -- a refusal it should not make loses a
forecast, and one it misses buys research for a question nothing can answer.

The properties this module owns:

- **agreement with the spending site**: over generated questions of every supported type and
  generated configurations, ``question_refusal`` refuses exactly when ``generate_forecast``
  raises before its first model call, and with the same message. The oracle is the spending
  site's own raise, not a second statement of the rules;
- **totality**: it returns a value for every canonical question; it does not raise;
- **no value leak** for a bounded question: a refusal renders none of the question's numbers
  (``forecast/numeric.py``'s rule for Metaculus-supplied values). The multiple-choice refusal
  names its option count and the configured pair by M1-512's decision, so it is excluded.

Branches are weighted with ``st.integers`` rather than ``st.one_of`` (which does not weight by
repetition), so both the refusing and the accepting side of every rule are drawn often.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from hypothesis import example, given, settings, strategies as st

from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.forecast.generate import (
    ForecastGenerationError,
    generate_forecast,
    question_refusal,
)
from whiskeyjack_bot.prompt import load_prompt
from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalDiscreteQuestion,
    CanonicalMultipleChoiceQuestion,
    CanonicalNumericQuestion,
)
from whiskeyjack_bot.research.model import ResearchDocument, ResearchRun
from whiskeyjack_bot.research.packet import ResearchPacket, build_packet

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_NAME = "openrouter/test-model"
NOW = datetime(2026, 10, 5, 8, 5, tzinfo=timezone.utc)
TYPES = ("binary", "multiple_choice", "numeric", "discrete")


def _base_config() -> AppConfig:
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = MODEL_NAME
    return validate_config_data(data)


BASE = _base_config()
PROMPT = load_prompt(
    REPO_ROOT / "prompts" / "forecaster.md",
    BASE.forecast.prompt_version,
    min_probability=0.001,
    max_probability=0.999,
)


def _configured(supported: list[str], minimum: float, maximum: float) -> AppConfig:
    return BASE.model_copy(
        update={
            "forecast": BASE.forecast.model_copy(
                update={
                    "supported_question_types": supported,
                    "min_probability": minimum,
                    "max_probability": maximum,
                }
            )
        }
    )


def _packet(question_id: int) -> ResearchPacket:
    run = ResearchRun(
        retrieval_run_id="run-1", question_id=question_id, provider="asknews", started_at_utc=NOW
    )
    document = ResearchDocument(
        retrieval_run_id="run-1",
        original_url="https://a.example/x",
        canonical_url="https://a.example/x",
        retrieved_at_utc=NOW,
        source_type="news",
        provenance="direct_api",
        content_sha256=hashlib.sha256(b"x").hexdigest(),
        summary="a summary",
    )
    return build_packet(question_id, [run], [document])


class _Counting:
    """A forecaster that records calls and returns an unusable reply.

    What is observed is only *whether* the model was reached, so the reply's content is
    irrelevant; an unparseable one keeps each draw to two cheap calls.
    """

    model = MODEL_NAME

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        self.calls += 1
        return "not json"


# --- strategies ----------------------------------------------------------------------

# Values chosen to be distinctive in text, so the leak check below cannot pass by accident
# of a short number also appearing in some unrelated word.
_LOWERS = st.sampled_from([-713.25, 0.0, 79.495, 4021.75, 7085.0])
_WIDTHS = st.sampled_from([0.5, 2.01, 913.5, 1992915.0])


@st.composite
def _bounded(draw: Any, qtype: str) -> CanonicalNumericQuestion | CanonicalDiscreteQuestion:
    lower = draw(_LOWERS)
    upper = lower + draw(_WIDTHS)
    # zero_point: absent / strictly below lower (satisfiable) / at lower / above lower.
    zero_case = draw(st.integers(0, 3))
    zero_point = {
        0: None,
        1: lower - 37.5,
        2: lower,
        3: lower + 0.125,
    }[zero_case]
    # cdf_size: weighted toward the edges that matter -- the configured 201, the
    # q46095 202 and the first size above it, the envelope's floor 2 and its neighbours, and
    # arbitrary sizes.
    size_case = draw(st.integers(0, 6))
    cdf_size = {
        0: 201,
        1: 202,
        2: draw(st.integers(-3, 3)),
        3: draw(st.integers(4, 200)),
        4: draw(st.integers(204, 400)),
        5: 16,
        6: 203,
    }[size_case]
    model = CanonicalNumericQuestion if qtype == "numeric" else CanonicalDiscreteQuestion
    return model(
        question_id=46095,
        post_id=45913,
        title="How much?",
        lower_bound=lower,
        upper_bound=upper,
        open_lower_bound=draw(st.booleans()),
        open_upper_bound=draw(st.booleans()),
        zero_point=zero_point,
        cdf_size=cdf_size,
    )


@st.composite
def _question(draw: Any) -> Any:
    kind = draw(st.integers(0, 3))
    if kind == 0:
        return CanonicalBinaryQuestion(question_id=46095, post_id=45913, title="Will X?")
    if kind == 1:
        count = draw(st.integers(2, 40))
        return CanonicalMultipleChoiceQuestion(
            question_id=46095,
            post_id=45913,
            title="Which X?",
            options=[f"Option {index}" for index in range(count)],
        )
    return draw(_bounded("numeric" if kind == 2 else "discrete"))


@st.composite
def _config(draw: Any) -> AppConfig:
    # Every type supported most of the time, so the type rule does not mask the others.
    if draw(st.integers(0, 3)):
        supported = list(TYPES)
    else:
        supported = draw(st.lists(st.sampled_from(TYPES), min_size=1, max_size=4, unique=True))
    # Narrowed pairs inside the spec envelope and inside the prompt's declared range, so
    # neither of those config-level preflights is what refuses (M1-512's construction).
    minimum = draw(st.sampled_from([0.001, 0.01, 0.05]))
    maximum = draw(st.sampled_from([0.4, 0.5, 0.95, 0.999]))
    return _configured(supported, minimum, maximum)


def _spending_site(config: AppConfig, question: Any) -> tuple[str | None, int]:
    """What ``generate_forecast`` does: ``(refusal message or None, model calls made)``."""
    client = _Counting()
    try:
        generate_forecast(
            config=config,
            question=question,
            packet=_packet(question.question_id),
            prompt=PROMPT,
            tournament_id="33129",
            now=NOW,
            client=client,
        )
    except ForecastGenerationError as exc:
        assert client.calls == 0, "every ForecastGenerationError is raised before the spend"
        return str(exc), 0
    return None, client.calls


# --- properties ----------------------------------------------------------------------


@settings(deadline=None)
@given(config=_config(), question=_question())
@example(
    config=BASE,
    question=CanonicalDiscreteQuestion(
        question_id=46095,
        post_id=45913,
        title="q46095's grid",
        lower_bound=79.495,
        upper_bound=81.505,
        open_lower_bound=True,
        open_upper_bound=True,
        cdf_size=202,
    ),
)
@example(
    config=_configured(list(TYPES), 0.05, 0.95),
    question=CanonicalMultipleChoiceQuestion(
        question_id=46095,
        post_id=45913,
        title="Which X?",
        options=[f"Option {index}" for index in range(21)],
    ),
)
@example(
    config=_configured(["multiple_choice"], 0.001, 0.999),
    question=CanonicalBinaryQuestion(question_id=46095, post_id=45913, title="Will X?"),
)
@example(
    config=BASE,
    question=CanonicalNumericQuestion(
        question_id=46095,
        post_id=45913,
        title="the configured grid, which must reach the model",
        lower_bound=0.0,
        upper_bound=100.0,
        open_lower_bound=False,
        open_upper_bound=False,
        cdf_size=201,
    ),
)
def test_question_refusal_refuses_exactly_what_the_spending_site_refuses(
    config: AppConfig, question: Any
) -> None:
    refusal = question_refusal(config, question)
    message, calls = _spending_site(config, question)
    if refusal is None:
        assert message is None, "the spending site refused a question the preflight passed"
        assert calls > 0
    else:
        assert message == refusal.message, "a refusal the spending site does not make"
        # The recorded code, derived independently of the module: a type this configuration
        # does not enable has no response schema at all; every other question-only refusal
        # is about what the configured output can express. Asserting membership in the pair
        # alone let both swaps of the mapping survive the mutation pass.
        unsupported = question.qtype not in config.forecast.supported_question_types
        assert refusal.detail_code == ("schema_invalid" if unsupported else "calibration_invalid")


@settings(deadline=None)
@given(config=_config(), qtype=st.sampled_from(["numeric", "discrete"]), data=st.data())
def test_a_bounded_refusal_renders_none_of_the_questions_numbers(
    config: AppConfig, qtype: str, data: Any
) -> None:
    question = data.draw(_bounded(qtype))
    refusal = question_refusal(config, question)
    if refusal is None:
        return
    rendered = [repr(question.lower_bound), repr(question.upper_bound), str(question.cdf_size)]
    if question.zero_point is not None:
        rendered.append(repr(question.zero_point))
    for needle in rendered:
        assert needle not in refusal.message
    # Stronger than the needles, and what makes this not depend on how a float is printed:
    # no bounded or type refusal carries a digit at all.
    assert not any(character.isdigit() for character in refusal.message)


@settings(deadline=None)
@given(
    qtype=st.sampled_from(["numeric", "discrete"]),
    cdf_size=st.one_of(
        st.sampled_from([-1, 0, 1, 2, 3, 200, 201, 202, 203, 204]),
        st.integers(-10, 1_000),
    ),
)
def test_the_grid_decision_is_exactly_the_written_rule(qtype: str, cdf_size: int) -> None:
    """M1-365 AC5. The oracle is the rule written out here, not the code's own constant.

    A discrete question may declare 2 to 202 points; a numeric one exactly 201. The spending
    site agreement above cannot catch a moved bound, because both callers read the same one.
    """
    model: Any = CanonicalNumericQuestion if qtype == "numeric" else CanonicalDiscreteQuestion
    question = model(
        question_id=46095,
        post_id=45913,
        title="How much?",
        lower_bound=79.5,
        upper_bound=281.5,
        open_lower_bound=True,
        open_upper_bound=True,
        zero_point=None,
        cdf_size=cdf_size,
    )
    allowed = cdf_size == 201 if qtype == "numeric" else 2 <= cdf_size <= 202
    refusal = question_refusal(BASE, question)
    assert (refusal is None) is allowed
    if refusal is not None:
        assert not any(character.isdigit() for character in refusal.message)
