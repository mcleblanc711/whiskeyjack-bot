"""M1-363: a value repeated at a bound is told so, and the refusal rule does not move.

q46395 (project 33132, 2026-10-20): the reply put its 0.01 to 0.25 percentiles on the open
lower bound with a tail to the upper bound. The conversion refused it, correctly, and the repair
turn carried only the generic text, so the model returned the same percentiles and the question
was lost. The payload is frozen in ``numeric_post_q46395_tie_at_bound.json``; the percentiles are
the stored reply's (``docs/M1-NOTES.md`` records both sources).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from forecasting_tools.data_models.data_organizer import DataOrganizer

from tests.unit.test_forecast_cdf import (
    _calibration,
    _question,
    _with_values,
)
from tests.unit.test_forecast_generate import (
    _generate,
    _Model,
    _packet,
    numeric_reply,
)
from tests.unit.test_forecast_generate import config as generate_config
from tests.unit.test_forecast_generate import prompt as generate_prompt
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.forecast.cdf import (
    _TIE_AT_BOUND,
    NumericCdf,
    numeric_cdf_or_problems,
)
from whiskeyjack_bot.prompt import LoadedPrompt
from whiskeyjack_bot.questions.model import CanonicalNumericQuestion
from whiskeyjack_bot.questions.normalize import normalize_questions

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = ROOT / "tests" / "fixtures" / "api_posts" / "numeric_post_q46395_tie_at_bound.json"
LEVELS = (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)
# The reply stored for q46395, identical on both live calls.
STORED_REPLY = (7085.0, 7085.0, 7085.0, 7085.0, 10656.0, 30000.0, 200000.0, 650000.0, 2000000.0)
LOC = "final_prediction.percentiles"


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    built: AppConfig = generate_config.__wrapped__(tmp_path)  # type: ignore[attr-defined]
    return built


@pytest.fixture
def prompt(config: AppConfig) -> LoadedPrompt:
    loaded: LoadedPrompt = generate_prompt.__wrapped__(config)  # type: ignore[attr-defined]
    return loaded


def _q46395() -> CanonicalNumericQuestion:
    raw = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    question = normalize_questions([DataOrganizer.get_question_from_post_json(raw)]).questions[0]
    assert isinstance(question, CanonicalNumericQuestion)
    assert (question.lower_bound, question.upper_bound) == (7085.0, 2000000.0)
    assert question.open_lower_bound and question.open_upper_bound
    return question


def test_the_stored_q46395_reply_receives_the_specific_message() -> None:
    converted, problems = numeric_cdf_or_problems(
        _with_values(*STORED_REPLY), _calibration(), _q46395()
    )
    assert converted is None
    assert problems == [f"{LOC}: {_TIE_AT_BOUND}"]


def test_the_message_renders_no_value_and_no_bound() -> None:
    """A bound is Metaculus payload, so ``forecast/numeric.py``'s rule covers it (AC3)."""
    assert not any(character.isdigit() for character in _TIE_AT_BOUND)
    assert "lower_bound" in _TIE_AT_BOUND and "just inside" in _TIE_AT_BOUND


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("narrow tail, four on the bound", (7085, 7085, 7085, 7085, 7200, 7500, 8000, 8500, 9500)),
        ("narrow tail, two on the bound", (7085, 7085, 7100, 7150, 7200, 7500, 8000, 8500, 9500)),
        ("four just inside the bound", (7086, 7086, 7086, 7086, 10656, 30000, 200000, 650000, 2e6)),
        (
            "four interior ties",
            (7200, 7500, 9000, 9000, 9000, 9000, 200000, 650000, 1500000),
        ),
        (
            "three on the open upper bound",
            (7200, 7500, 9000, 20000, 50000, 200000, 2e6, 2e6, 2e6),
        ),
    ],
)
def test_the_probed_shapes_that_convert_still_convert(name: str, values: tuple[float, ...]) -> None:
    """The row's offline probes: the selector picks a message, never a verdict."""
    converted, problems = numeric_cdf_or_problems(_with_values(*values), _calibration(), _q46395())
    assert problems == [], name
    assert isinstance(converted, NumericCdf)
    assert len(converted.values) == 201


def test_two_values_at_a_closed_lower_bound_on_a_discrete_grid_still_convert() -> None:
    raw = json.loads(
        (ROOT / "tests/fixtures/api_posts/discrete_shapes/post_45775.json").read_text("utf-8")
    )
    question: Any = normalize_questions([DataOrganizer.get_question_from_post_json(raw)]).questions[
        0
    ]
    question = question.model_copy(update={"open_lower_bound": False})
    low, high = question.lower_bound, question.upper_bound
    span = high - low
    fractions = (0.0, 0.0, 0.1, 0.25, 0.4, 0.55, 0.7, 0.8, 0.95)
    converted, problems = numeric_cdf_or_problems(
        _with_values(*(low + span * fraction for fraction in fractions)),
        _calibration(),
        question,
    )
    assert problems == []
    assert isinstance(converted, NumericCdf)


def test_a_refusal_with_no_repeat_at_a_bound_keeps_the_existing_text() -> None:
    """Nine equal values well inside the range: repeated, but not at a bound (AC3)."""
    converted, problems = numeric_cdf_or_problems(
        _with_values(*[24.0] * 9), _calibration(), _question()
    )
    assert converted is None
    assert problems == [
        f"{LOC}: the declared percentiles do not produce a well-formed cumulative distribution; "
        "return strictly increasing values spread more widely across the question's range "
        "(detail withheld: the conversion echoes the values it refused)"
    ]


def test_the_q46395_shape_then_a_compliant_reply_is_a_forecast_in_two_billed_calls(
    config: AppConfig, prompt: LoadedPrompt
) -> None:
    """AC6. Counts calls on the client, and reads the message out of the repair turn itself."""

    def reply(values: tuple[float, ...]) -> str:
        return numeric_reply(
            question_id=46395,
            final_prediction={
                "percentiles": [
                    {"percentile": level, "value": value}
                    for level, value in zip(LEVELS, values, strict=True)
                ]
            },
        )

    compliant = (7086.0, 7090.0, 7200.0, 7500.0, 10656.0, 30000.0, 200000.0, 650000.0, 2e6)
    client = _Model(reply(STORED_REPLY), reply(compliant))
    result = _generate(client, config, prompt, question=_q46395(), packet=_packet(46395))

    assert len(client.calls) == 2
    assert _TIE_AT_BOUND in client.calls[1][-1]["content"]
    assert result.repair_attempted is True
    assert result.failure_code is None
    assert result.forecast is not None


@pytest.mark.parametrize(
    "path",
    [
        ROOT / "prompts" / "forecaster-tournament.md",
        ROOT / "prompts" / "forecaster-tournament-lens.md",
    ],
)
def test_both_tournament_prompts_state_the_two_rules(path: Path) -> None:
    """AC1, for the arm that runs and for the shadow arm."""
    text = path.read_text(encoding="utf-8")
    assert (
        "Do not repeat a percentile value at the question's `lower_bound` or `upper_bound`" in text
    )
    assert "strictly increasing values just inside it" in text
    assert "`load_bearing_facts` must hold at least one entry when research documents" in text
    assert "may rest on secondary reporting if the claim says so" in text
