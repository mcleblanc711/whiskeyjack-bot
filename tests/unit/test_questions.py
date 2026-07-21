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
    Category,
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


@pytest.mark.parametrize(
    "tag",
    ["conditional", "date", "discrete", "", None, 7, [], {}, ["binary"], {"binary": 1}],
)
def test_unsupported_tags_are_refused_without_reading_any_field(tag: object) -> None:
    """A bare tag is enough to refuse: no field access, so no model call.

    The unhashable cases are the regression guard: testing ``tag in frozenset``
    before ``isinstance(tag, str)`` raises a raw ``TypeError`` that escapes the
    module's error boundary entirely.
    """

    class _OnlyTag:
        question_type = tag

    with pytest.raises(UnsupportedQuestionTypeError):
        normalize_question(_OnlyTag())  # type: ignore[arg-type]


def test_unsupported_tag_error_names_only_known_sdk_types() -> None:
    """A tag outside the SDK's own enum is unvetted content, so it is not echoed."""

    class _KnownTag:
        question_type = "conditional"

    class _ForeignTag:
        question_type = PLANTED_SECRET

    with pytest.raises(UnsupportedQuestionTypeError, match="conditional"):
        normalize_question(_KnownTag())  # type: ignore[arg-type]

    with pytest.raises(UnsupportedQuestionTypeError) as excinfo:
        normalize_question(_ForeignTag())  # type: ignore[arg-type]
    assert PLANTED_SECRET not in str(excinfo.value)
    assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))
    assert "unknown" in str(excinfo.value)


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
        "categories": [],
        "group_question_option": None,
        "question_ids_of_group": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_group_identity_is_carried_through() -> None:
    """M1-202 unpacks subquestions; the parent linkage must survive M1-201.

    Uses non-null linkage deliberately: every repo fixture has a null group
    parent, so a fixture-driven check passes even if both fields are mapped to a
    constant ``None``.
    """
    canonical = normalize_question(
        fake_sdk_question(  # type: ignore[arg-type]
            group_question_option="Above 3%",
            question_ids_of_group=[91001, 91002, 91003],
        )
    )
    assert canonical.group_question_option == "Above 3%"
    assert canonical.question_ids_of_group == [91001, 91002, 91003]


# --- source categories (uninterpreted SDK passthrough) ----------------------


def test_source_categories_are_carried_through() -> None:
    """normalize is the only place SDK fields are read, so a dropped category is
    unrecoverable downstream. Slug wins where present; name is the fallback."""
    canonical = normalize_question(
        fake_sdk_question(  # type: ignore[arg-type]
            categories=[
                Category(id=1, name="Economics", slug="economy"),
                Category(id=2, name="Geopolitics", slug=None),
            ]
        )
    )
    assert canonical.source_categories == ["economy", "Geopolitics"]


def test_source_categories_default_to_empty() -> None:
    assert normalize_question(fake_sdk_question()).source_categories == []  # type: ignore[arg-type]


def test_source_categories_survive_the_round_trip() -> None:
    canonical = normalize_question(
        fake_sdk_question(categories=[Category(id=1, name="Health", slug="health")])  # type: ignore[arg-type]
    )
    restored = CanonicalQuestionAdapter.validate_json(canonical.model_dump_json())
    assert restored == canonical


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


# --- schema integrity: finite floats ----------------------------------------


def numeric_kwargs(**overrides: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "question_id": 91001,
        "post_id": 90001,
        "title": "[SYNTHETIC] How many?",
        "lower_bound": 0.0,
        "upper_bound": 100.0,
        "open_lower_bound": False,
        "open_upper_bound": True,
        "cdf_size": 201,
    }
    base.update(overrides)
    return base


NON_FINITE = [float("nan"), float("inf"), float("-inf")]


@pytest.mark.parametrize("value", NON_FINITE)
@pytest.mark.parametrize(
    "field",
    ["lower_bound", "upper_bound", "zero_point", "nominal_lower_bound", "nominal_upper_bound"],
)
def test_non_finite_numeric_fields_are_rejected(field: str, value: float) -> None:
    """NaN slips past ``lower >= upper`` (both comparisons are false) and then
    serializes to JSON null, so the union adapter cannot read the record back."""
    with pytest.raises(ValidationError):
        CanonicalNumericQuestion(**numeric_kwargs(**{field: value}))


@pytest.mark.parametrize("value", NON_FINITE)
def test_non_finite_question_weight_is_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        CanonicalBinaryQuestion(
            question_id=91001, post_id=90001, title="[SYNTHETIC] Will it?", question_weight=value
        )


def test_numeric_question_survives_a_json_round_trip() -> None:
    """The positive half of the finite-float contract."""
    canonical = CanonicalNumericQuestion(
        **numeric_kwargs(zero_point=1.0, nominal_lower_bound=0.0, nominal_upper_bound=100.0)
    )
    restored = CanonicalQuestionAdapter.validate_json(canonical.model_dump_json())
    assert restored == canonical
    assert type(restored) is CanonicalNumericQuestion


# --- schema integrity: multiple-choice option sets --------------------------


def choice_kwargs(options: list[str]) -> dict[str, Any]:
    return {
        "question_id": 91001,
        "post_id": 90001,
        "title": "[SYNTHETIC] Which?",
        "options": options,
    }


@pytest.mark.parametrize(
    ("description", "options"),
    [
        ("duplicate labels", ["A", "B", "A"]),
        ("blank label", ["A", "B", ""]),
        ("whitespace-only label", ["A", "B", "   "]),
        ("single option", ["A"]),
        ("no options", []),
        ("duplicated blank", ["", ""]),
    ],
)
def test_malformed_option_sets_are_rejected(description: str, options: list[str]) -> None:
    """M1-404 must emit every exact option once; duplicates collapse as mapping
    keys and blanks cannot be matched back to a source option."""
    with pytest.raises(ValidationError):
        CanonicalMultipleChoiceQuestion(**choice_kwargs(options))


def test_well_formed_option_set_is_accepted() -> None:
    canonical = CanonicalMultipleChoiceQuestion(**choice_kwargs(["Yes", "No", "Too close"]))
    assert canonical.options == ["Yes", "No", "Too close"]


def test_option_validation_errors_never_echo_the_labels() -> None:
    """Option labels are record content, so the no-echo rule covers them too."""
    with pytest.raises(NormalizationError) as excinfo:
        normalize_question(
            fake_sdk_question(  # type: ignore[arg-type]
                question_type="multiple_choice",
                options=[PLANTED_SECRET, PLANTED_SECRET],
                option_is_instance_of=None,
            )
        )
    assert PLANTED_SECRET not in str(excinfo.value)
    assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))
