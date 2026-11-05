"""M1-365: a discrete question with 201 outcomes converts and plans at 202 points (D57).

Both payloads are live: q46095 (post 45913, project 33129) and q46623 (post 46390, project
33135). Each was refused for ``cdf_size`` 202 and lost its question. The numbers asserted here
are written out rather than read from the code, so a moved bound fails them.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from forecasting_tools.data_models.data_organizer import DataOrganizer

from tests.unit.test_forecast_cdf import _calibration, _with_values
from tests.unit.test_forecast_generate import config  # noqa: F401 (a shared fixture)
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.forecast.cdf import (
    NumericCdf,
    NumericCdfError,
    expected_cdf_points_for,
    numeric_cdf_or_problems,
)
from whiskeyjack_bot.forecast.generate import question_refusal
from whiskeyjack_bot.questions.model import CanonicalDiscreteQuestion, CanonicalNumericQuestion
from whiskeyjack_bot.questions.normalize import normalize_questions
from whiskeyjack_bot.submission_live import (
    DiscretePost,
    LiveSubmissionError,
    plan_from_payload,
)

ROOT = Path(__file__).resolve().parents[2]
SHAPES = ROOT / "tests" / "fixtures" / "api_posts" / "discrete_shapes"

# Metaculus's rules for 201 inbound outcomes, written out: 0.2 * 200 / 201 and 0.01 / 201.
MAX_STEP = 0.2 * 200 / 201
MIN_STEP = 0.01 / 201


def _live(post: str) -> CanonicalDiscreteQuestion:
    raw = json.loads((SHAPES / post).read_text(encoding="utf-8"))
    question = normalize_questions([DataOrganizer.get_question_from_post_json(raw)]).questions[0]
    assert isinstance(question, CanonicalDiscreteQuestion)
    return question


def _percentile_set(question: CanonicalDiscreteQuestion, shape: str) -> tuple[float, ...]:
    """Nine increasing values, as fractions of this question's own range."""
    fractions = {
        "wide": (0.10, 0.18, 0.22, 0.28, 0.35, 0.43, 0.50, 0.56, 0.70),
        "tight": (0.31, 0.32, 0.33, 0.34, 0.35, 0.36, 0.37, 0.38, 0.39),
        "upper tail": (0.75, 0.84, 0.88, 0.91, 0.94, 0.97, 1.03, 1.10, 1.30),
        "lower tail": (-0.15, -0.09, -0.04, 0.02, 0.05, 0.10, 0.15, 0.20, 0.27),
    }[shape]
    low, high = question.lower_bound, question.upper_bound
    return tuple(low + (high - low) * fraction for fraction in fractions)


LIVE_POSTS = ["post_45913.json", "post_46390.json"]


@pytest.mark.parametrize("post", LIVE_POSTS)
def test_the_live_payloads_are_the_201_outcome_shape(post: str) -> None:
    """The fixtures are the case: without this the rest would pass on a coarser grid."""
    assert _live(post).cdf_size == 202


@pytest.mark.parametrize("post", LIVE_POSTS)
def test_a_live_payload_is_no_longer_refused(config: AppConfig, post: str) -> None:
    assert question_refusal(config, _live(post)) is None
    assert expected_cdf_points_for(_live(post), _calibration()) == 202


@pytest.mark.parametrize("post", LIVE_POSTS)
@pytest.mark.parametrize("shape", ["wide", "tight", "upper tail", "lower tail"])
def test_a_live_payload_converts_to_202_values_within_the_platforms_rules(
    post: str, shape: str
) -> None:
    question = _live(post)
    converted, problems = numeric_cdf_or_problems(
        _with_values(*_percentile_set(question, shape)), _calibration(), question
    )
    assert problems == []
    assert isinstance(converted, NumericCdf)
    assert len(converted.values) == 202
    steps = [after - before for before, after in pairwise(converted.values)]
    assert max(steps) <= MAX_STEP
    assert min(steps) >= MIN_STEP


def _converted(post: str) -> list[float]:
    question = _live(post)
    converted, _ = numeric_cdf_or_problems(
        _with_values(*_percentile_set(question, "wide")), _calibration(), question
    )
    assert isinstance(converted, NumericCdf)
    return list(converted.values)


@pytest.mark.parametrize("post", LIVE_POSTS)
def test_the_submission_plan_takes_202_values(post: str) -> None:
    cdf = _converted(post)
    plan = plan_from_payload(
        {"question_type": "discrete", "continuous_cdf": cdf}, expected_cdf_points=202
    )
    assert isinstance(plan, DiscretePost)
    assert len(plan.continuous_cdf) == 202


@pytest.mark.parametrize("length", [201, 203])
def test_the_submission_plan_refuses_any_other_length(length: int) -> None:
    cdf = _converted("post_46390.json")
    wrong = cdf[:201] if length == 201 else [*cdf, cdf[-1]]
    assert len(wrong) == length
    with pytest.raises(LiveSubmissionError):
        plan_from_payload(
            {"question_type": "discrete", "continuous_cdf": wrong}, expected_cdf_points=202
        )


def _numeric_declaring(cdf_size: int) -> CanonicalNumericQuestion:
    return CanonicalNumericQuestion(
        question_id=46623,
        post_id=46390,
        title="How many?",
        lower_bound=1219.5,
        upper_bound=1420.5,
        open_lower_bound=True,
        open_upper_bound=True,
        cdf_size=cdf_size,
    )


def test_a_numeric_question_declaring_202_is_still_refused(config: AppConfig) -> None:
    """The widening is discrete-only: numeric stays pinned to exactly 201."""
    question: Any = _numeric_declaring(202)
    assert question_refusal(config, question) is not None
    with pytest.raises(NumericCdfError):
        numeric_cdf_or_problems(
            _with_values(1240, 1255, 1262, 1275, 1290, 1305, 1320, 1332, 1360),
            _calibration(),
            question,
        )
