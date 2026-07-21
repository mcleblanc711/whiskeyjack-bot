"""M1-201 acceptance: golden binary, multiple-choice and numeric fixtures
normalize into the canonical schema and retain their resolution fine print.

Deferred types (D21) are refused before any field is read, so an unsupported
question can never reach a model or submission call. Comprehensive valid/invalid
golden records are Codex's T-901; this suite covers the model + mapping only.
"""

import json
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from forecasting_tools.data_models.data_organizer import DataOrganizer
from forecasting_tools.data_models.questions import (
    DateQuestion,
    DiscreteQuestion,
    MetaculusQuestion,
    NumericQuestion,
)
from pydantic import ValidationError

from whiskeyjack_bot.questions import (
    CanonicalBinaryQuestion,
    CanonicalMultipleChoiceQuestion,
    CanonicalNumericQuestion,
    CanonicalQuestionAdapter,
    NormalizationError,
    UnsupportedQuestionTypeError,
    normalize_question,
    normalize_questions,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
API_POSTS = FIXTURES / "api_posts"


def load_fixture_questions() -> list[MetaculusQuestion]:
    posts = sorted(API_POSTS.glob("*_post.json"))
    return [
        DataOrganizer.get_question_from_post_json(json.loads(p.read_text(encoding="utf-8")))
        for p in posts
    ]


def raw_post(name: str) -> dict[str, Any]:
    return json.loads((API_POSTS / f"{name}_post.json").read_text(encoding="utf-8"))


def normalized_by_type() -> dict[str, Any]:
    return {q.qtype: q for q in normalize_questions(load_fixture_questions())}


def test_fixtures_normalize_to_expected_canonical_types() -> None:
    canonical = normalize_questions(load_fixture_questions())
    assert {type(q) for q in canonical} == {
        CanonicalBinaryQuestion,
        CanonicalMultipleChoiceQuestion,
        CanonicalNumericQuestion,
    }
    assert {q.qtype for q in canonical} == {"binary", "multiple_choice", "numeric"}


@pytest.mark.parametrize("name", ["binary", "multiple_choice", "numeric"])
def test_normalization_retains_resolution_fine_print(name: str) -> None:
    """The headline M1-201 acceptance criterion."""
    source = raw_post(name)["question"]
    canonical = normalized_by_type()[name]
    assert canonical.resolution_criteria == source["resolution_criteria"]
    assert canonical.fine_print == source["fine_print"]
    # Guard against the assertion passing on a pair of Nones.
    assert canonical.resolution_criteria
    assert canonical.fine_print


@pytest.mark.parametrize("name", ["binary", "multiple_choice", "numeric"])
def test_identity_and_common_fields_preserved(name: str) -> None:
    post = raw_post(name)
    canonical = normalized_by_type()[name]
    assert canonical.question_id == post["question"]["id"]
    assert canonical.post_id == post["id"]
    assert canonical.title == post["question"]["title"]
    assert canonical.unit_of_measure == post["question"]["unit"]
    assert "minibench" in canonical.tournament_slugs
    assert canonical.question_weight == post["question"]["question_weight"]
    assert canonical.open_time == datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)


def test_multiple_choice_options_preserved() -> None:
    source = raw_post("multiple_choice")["question"]
    canonical = normalized_by_type()["multiple_choice"]
    assert canonical.options == source["options"]
    assert canonical.option_is_instance_of == source["group_variable"]


def test_numeric_bounds_preserved() -> None:
    scaling = raw_post("numeric")["question"]["scaling"]
    canonical = normalized_by_type()["numeric"]
    assert canonical.lower_bound == scaling["range_min"]
    assert canonical.upper_bound == scaling["range_max"]
    assert canonical.zero_point == scaling["zero_point"]
    assert canonical.open_lower_bound is False
    assert canonical.open_upper_bound is True
    assert canonical.nominal_lower_bound == scaling["nominal_min"]
    assert canonical.nominal_upper_bound == scaling["nominal_max"]
    # Agrees with config.expected_cdf_points (Literal[201]).
    assert canonical.cdf_size == 201


def test_group_identity_is_carried_through() -> None:
    """M1-202 unpacks subquestions; the parent linkage must survive M1-201."""
    for sdk, canonical in zip(
        load_fixture_questions(), normalize_questions(load_fixture_questions())
    ):
        assert canonical.group_question_option == sdk.group_question_option
        assert canonical.question_ids_of_group == sdk.question_ids_of_group


def test_canonical_questions_round_trip_through_the_union_adapter() -> None:
    for canonical in normalize_questions(load_fixture_questions()):
        restored = CanonicalQuestionAdapter.validate_python(canonical.model_dump())
        assert restored == canonical
        assert type(restored) is type(canonical)


def test_canonical_models_reject_unknown_fields() -> None:
    payload = normalized_by_type()["binary"].model_dump()
    payload["surprise"] = "extra"
    with pytest.raises(ValidationError):
        CanonicalQuestionAdapter.validate_python(payload)


# --- deferred / unsupported types (D21) ------------------------------------


def _synthetic_date_question() -> DateQuestion:
    return DateQuestion(
        question_text="[SYNTHETIC] When?",
        lower_bound=datetime(2026, 1, 1, tzinfo=timezone.utc),
        upper_bound=datetime(2027, 1, 1, tzinfo=timezone.utc),
        open_lower_bound=False,
        open_upper_bound=False,
    )


def test_date_question_is_rejected() -> None:
    with pytest.raises(UnsupportedQuestionTypeError, match="date"):
        normalize_question(_synthetic_date_question())


def test_discrete_question_is_rejected_despite_subclassing_numeric() -> None:
    """Regression guard for the SDK's inheritance trap.

    ``DiscreteQuestion`` subclasses ``NumericQuestion``, so dispatching on
    ``isinstance(q, NumericQuestion)`` would silently normalize an unsupported
    type as numeric. Dispatch keys on the ``question_type`` tag instead.
    """
    question = DiscreteQuestion(
        question_text="[SYNTHETIC] How many?",
        lower_bound=0.0,
        upper_bound=10.0,
        open_lower_bound=False,
        open_upper_bound=False,
    )
    assert isinstance(question, NumericQuestion)  # the trap is real
    with pytest.raises(UnsupportedQuestionTypeError, match="discrete"):
        normalize_question(question)


@pytest.mark.parametrize("tag", ["conditional", "date", "discrete", "", None, 7])
def test_unsupported_tags_are_refused_without_reading_any_field(tag: object) -> None:
    """A bare tag is enough to refuse: no field access, so no model call."""

    class _OnlyTag:
        question_type = tag

    with pytest.raises(UnsupportedQuestionTypeError):
        normalize_question(_OnlyTag())  # type: ignore[arg-type]


# --- malformed records ------------------------------------------------------

PLANTED_SECRET = "privateFAKE123456"


def fake_sdk_question(**overrides: object) -> SimpleNamespace:
    """A minimal stand-in exposing the SDK attribute names normalize reads."""
    base: dict[str, object] = {
        "question_type": "binary",
        "id_of_question": 91001,
        "id_of_post": 90001,
        "page_url": "https://example.invalid/q/1",
        "question_text": "[SYNTHETIC] Will it?",
        "background_info": None,
        "resolution_criteria": "Resolves YES if it does.",
        "fine_print": "Per the source's own timestamp.",
        "unit_of_measure": None,
        "open_time": None,
        "close_time": None,
        "scheduled_resolution_time": None,
        "tournament_slugs": ["minibench"],
        "question_weight": 1.0,
        "group_question_option": None,
        "question_ids_of_group": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("description", "overrides", "match"),
    [
        ("missing question id", {"id_of_question": None}, "question_id"),
        ("missing post id", {"id_of_post": None}, "post_id"),
        ("non-integer question id", {"id_of_question": "ninety"}, "question_id"),
        ("empty title", {"question_text": ""}, "title"),
        (
            "multiple choice with no options",
            {"question_type": "multiple_choice", "options": [], "option_is_instance_of": None},
            "options",
        ),
        (
            "numeric bounds inverted",
            {
                "question_type": "numeric",
                "lower_bound": 500.0,
                "upper_bound": 0.0,
                "open_lower_bound": False,
                "open_upper_bound": True,
                "zero_point": None,
                "cdf_size": 201,
                "nominal_lower_bound": None,
                "nominal_upper_bound": None,
            },
            "lower_bound",
        ),
    ],
)
def test_malformed_records_raise_normalization_error(
    description: str, overrides: dict[str, object], match: str
) -> None:
    with pytest.raises(NormalizationError, match=match):
        normalize_question(fake_sdk_question(**overrides))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("description", "overrides"),
    [
        ("secret as an invalid question id", {"id_of_question": PLANTED_SECRET}),
        ("secret as an invalid post id", {"id_of_post": PLANTED_SECRET}),
        (
            "secret as invalid multiple-choice options",
            {
                "question_type": "multiple_choice",
                "options": PLANTED_SECRET,
                "option_is_instance_of": None,
            },
        ),
        (
            "secret as an invalid weight",
            {"question_weight": PLANTED_SECRET, "id_of_question": None},
        ),
    ],
)
def test_normalization_errors_never_echo_field_values(
    description: str, overrides: dict[str, object]
) -> None:
    """Same rule as ConfigError/SnapshotError: pydantic's own rendering prints
    the offending input, so a mistakenly stored credential must be stripped from
    both the message and the cause chain."""
    with pytest.raises(NormalizationError) as excinfo:
        normalize_question(fake_sdk_question(**overrides))  # type: ignore[arg-type]
    assert PLANTED_SECRET not in str(excinfo.value), description
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert PLANTED_SECRET not in rendered, description


def test_question_missing_its_type_fields_raises_normalization_error() -> None:
    """A raw AttributeError must not escape to callers.

    Same defect class as the M0-103 review finding against SnapshotError: the
    CLI only handles the module's own error type, so every malformed shape has
    to arrive as one.
    """
    incomplete = fake_sdk_question(question_type="multiple_choice")  # no options attr
    with pytest.raises(NormalizationError, match="does not expose the fields"):
        normalize_question(incomplete)  # type: ignore[arg-type]


def test_missing_field_error_does_not_echo_question_contents() -> None:
    incomplete = fake_sdk_question(
        question_type="numeric",  # numeric bound attrs absent
        resolution_criteria=PLANTED_SECRET,
    )
    with pytest.raises(NormalizationError) as excinfo:
        normalize_question(incomplete)  # type: ignore[arg-type]
    assert PLANTED_SECRET not in str(excinfo.value)
    assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))


def test_unsupported_error_is_a_normalization_error() -> None:
    """Callers may catch the base type to handle every normalization refusal."""
    assert issubclass(UnsupportedQuestionTypeError, NormalizationError)


def test_normalize_questions_propagates_the_first_failure() -> None:
    with pytest.raises(UnsupportedQuestionTypeError):
        normalize_questions([*load_fixture_questions(), _synthetic_date_question()])
