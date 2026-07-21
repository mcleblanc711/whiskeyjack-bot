"""M1-201 acceptance: golden binary, multiple-choice and numeric fixtures
normalize into the canonical schema and retain their resolution fine print.

Deferred types (D21) are refused before any field is read, so an unsupported
question can never reach a model or submission call. Comprehensive valid/invalid
golden records are Codex's T-901; this suite covers the model + mapping only.
"""

import dataclasses
import json
import logging
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

from whiskeyjack_bot.logging_setup import JsonFormatter
from whiskeyjack_bot.questions import (
    CanonicalBinaryQuestion,
    CanonicalMultipleChoiceQuestion,
    CanonicalNumericQuestion,
    CanonicalQuestionAdapter,
    NormalizationError,
    NormalizationResult,
    SourceCategory,
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
    return {q.qtype: q for q in normalize_questions(load_fixture_questions()).questions}


def test_fixtures_normalize_to_expected_canonical_types() -> None:
    canonical = normalize_questions(load_fixture_questions()).questions
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


def test_common_fields_are_read_from_the_object_not_hardcoded() -> None:
    """Every fixture shares one tournament slug, weight and open time, so the
    fixture-driven assertions above would still pass against constants."""
    canonical = normalize_question(
        fake_sdk_question(  # type: ignore[arg-type]
            tournament_slugs=["aibq4", "minibench-practice"],
            question_weight=0.25,
            open_time=datetime(2031, 2, 3, 9, 30, tzinfo=timezone.utc),
            close_time=datetime(2031, 4, 5, 18, 0, tzinfo=timezone.utc),
            scheduled_resolution_time=datetime(2031, 5, 6, 12, 0, tzinfo=timezone.utc),
            unit_of_measure="GW",
            page_url="https://example.invalid/q/4242",
            background_info="[SYNTHETIC] background",
        )
    )
    assert canonical.tournament_slugs == ["aibq4", "minibench-practice"]
    assert canonical.question_weight == 0.25
    assert canonical.open_time == datetime(2031, 2, 3, 9, 30, tzinfo=timezone.utc)
    assert canonical.close_time == datetime(2031, 4, 5, 18, 0, tzinfo=timezone.utc)
    assert canonical.scheduled_resolution_time == datetime(2031, 5, 6, 12, 0, tzinfo=timezone.utc)
    assert canonical.unit_of_measure == "GW"
    assert canonical.url == "https://example.invalid/q/4242"
    assert canonical.background_info == "[SYNTHETIC] background"


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
    for canonical in normalize_questions(load_fixture_questions()).questions:
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
        # Read only for a group member, to recover the parent post's title (M1-202).
        "api_json": {},
    }
    # Type-specific attrs are legitimately absent from the base (normalize reads them
    # only for their own type), so they are allowed per-type rather than globally: a
    # global allowlist still admits `fake_sdk_question(options=[...])` on the default
    # binary type, where nothing reads `options` and the override is vacuous.
    type_specific: dict[object, set[str]] = {
        "multiple_choice": {"options", "option_is_instance_of"},
        "numeric": {
            "lower_bound",
            "upper_bound",
            "open_lower_bound",
            "open_upper_bound",
            "zero_point",
            "cdf_size",
            "nominal_lower_bound",
            "nominal_upper_bound",
        },
    }
    qtype = overrides.get("question_type", base["question_type"])
    known = base.keys() | type_specific.get(qtype, set())
    # Without this an override naming a canonical field instead of the SDK attribute
    # (``url`` for ``page_url``, say) silently sets an attribute nothing reads, and the
    # test passes against the default value it meant to replace.
    unknown = overrides.keys() - known
    assert not unknown, (
        f"override(s) not read by normalize for question_type={qtype!r}: {sorted(unknown)}"
    )
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
    unrecoverable downstream. The full identity triple is preserved."""
    canonical = normalize_question(
        fake_sdk_question(  # type: ignore[arg-type]
            categories=[
                Category(id=1, name="Economics", slug="economy"),
                Category(id=2, name="Geopolitics", slug=None),
            ]
        )
    )
    assert canonical.source_categories == [
        SourceCategory(id=1, name="Economics", slug="economy"),
        SourceCategory(id=2, name="Geopolitics", slug=None),
    ]


def test_distinct_categories_stay_distinguishable() -> None:
    """Regression guard for the round-2 finding: a ``slug or name`` mapping renders
    these two different categories identically, so downstream classification could
    apply the first one's mapping to the second."""
    first, second = (
        normalize_question(fake_sdk_question(categories=[category]))  # type: ignore[arg-type]
        for category in (
            Category(id=17, name="Economics", slug="economy"),
            Category(id=18, name="economy", slug=None),
        )
    )
    assert first.source_categories != second.source_categories
    assert first.source_categories[0].id != second.source_categories[0].id


def test_source_categories_ignore_presentational_sdk_fields() -> None:
    """emoji/description are deliberately not carried; the identity triple is."""
    canonical = normalize_question(
        fake_sdk_question(  # type: ignore[arg-type]
            categories=[
                Category(id=3, name="Health", slug="health", emoji="🩺", description="Long text.")
            ]
        )
    )
    assert canonical.source_categories == [SourceCategory(id=3, name="Health", slug="health")]


def test_source_categories_default_to_empty() -> None:
    assert normalize_question(fake_sdk_question()).source_categories == []  # type: ignore[arg-type]


def test_source_categories_survive_the_round_trip() -> None:
    canonical = normalize_question(
        fake_sdk_question(  # type: ignore[arg-type]
            categories=[
                Category(id=1, name="Health", slug="health"),
                Category(id=2, name="Science", slug=None),
            ]
        )
    )
    restored = CanonicalQuestionAdapter.validate_json(canonical.model_dump_json())
    assert restored == canonical
    assert restored.source_categories[0].id == 1


def test_malformed_category_arrives_as_a_normalization_error() -> None:
    """SourceCategory is built by the canonical model, inside the ValidationError
    fence -- constructing it during the field read would escape the boundary."""
    with pytest.raises(NormalizationError, match="source_categories"):
        normalize_question(
            fake_sdk_question(  # type: ignore[arg-type]
                categories=[SimpleNamespace(id="not-an-int", name="Economics", slug=None)]
            )
        )


def test_category_validation_errors_never_echo_the_category() -> None:
    with pytest.raises(NormalizationError) as excinfo:
        normalize_question(
            fake_sdk_question(  # type: ignore[arg-type]
                categories=[SimpleNamespace(id=PLANTED_SECRET, name=PLANTED_SECRET, slug=None)]
            )
        )
    assert PLANTED_SECRET not in str(excinfo.value)
    assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))


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


def test_a_deferred_type_does_not_abort_the_batch() -> None:
    """One deferred question no longer discards the batch it arrived in (M1-203).

    Before M1-203 the batch propagated the first failure, so a single date question
    in a tournament pull threw away the normalization of every supported question
    fetched alongside it.
    """
    result = normalize_questions([*load_fixture_questions(), _synthetic_date_question()])

    assert len(result.questions) == 3
    assert len(result.deferrals) == 1
    assert result.deferrals[0].question_type == "date"
    assert result.deferrals[0].reason == "deferred_v1_type"


def test_a_malformed_supported_question_still_aborts_the_batch() -> None:
    """D21 defers date and conditional; it does not make malformed records survivable.

    The stricter reading of the M1-203 criterion: only a deferred *type* is skipped.
    A numeric question missing the bounds its own type declares is a real defect, and
    silently dropping it would hide it behind a diagnostic that says "deferred".
    """
    malformed = fake_sdk_question(question_type="numeric")

    with pytest.raises(NormalizationError, match="does not expose the fields"):
        normalize_questions([_synthetic_date_question(), malformed])


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


# --- deferral events (M1-203) -----------------------------------------------

# Every attribute normalize reads for *content*, as opposed to identity. The
# deferral path may read id_of_question/id_of_post; touching anything here means a
# deferred question got further into normalization than D21 allows.
_CONTENT_ATTRS = (
    "page_url",
    "question_text",
    "background_info",
    "resolution_criteria",
    "fine_print",
    "unit_of_measure",
    "open_time",
    "close_time",
    "scheduled_resolution_time",
    "tournament_slugs",
    "question_weight",
    "categories",
    "group_question_option",
    "question_ids_of_group",
    "api_json",
    "options",
    "option_is_instance_of",
    "lower_bound",
    "upper_bound",
    "open_lower_bound",
    "open_upper_bound",
    "zero_point",
    "cdf_size",
    "nominal_lower_bound",
    "nominal_upper_bound",
)


class _ContentFieldRead(BaseException):
    """Raised by a tripwire attribute. Deliberately **not** an ``Exception``.

    ``normalize._safe_attr`` swallows ``Exception`` by design, so a tripwire raising
    ``AssertionError`` would be caught and the test would pass vacuously the moment
    someone routed a content read through that helper. Deriving from
    ``BaseException`` puts the tripwire outside every ``except Exception`` in the
    module under test, which is the only way it can actually fail the build.
    """


def tripwire_question(tag: object) -> object:
    """A question whose every content attribute raises when read.

    The explicit form of a guarantee the ``_OnlyTag`` tests hold only by accident:
    an object exposing just a tag proves "nothing crashed", not "nothing was read".
    Here identity is readable and content is armed, so the distinction is tested
    rather than assumed.
    """

    def _boom(self: object) -> object:
        raise _ContentFieldRead("normalization read a content field of a deferred question")

    namespace: dict[str, object] = {
        "question_type": tag,
        "id_of_question": 91001,
        "id_of_post": 90001,
    }
    for name in _CONTENT_ATTRS:
        namespace[name] = property(_boom)
    return type("_Tripwire", (), namespace)()


def test_deferral_carries_integer_identity() -> None:
    """An operator cannot act on "one question was deferred"; the event says which."""
    result = normalize_questions([fake_sdk_question(question_type="date")])  # type: ignore[list-item]

    assert len(result.questions) == 0
    (event,) = result.deferrals
    assert event.question_id == 91001
    assert event.post_id == 90001


def test_deferral_withholds_non_integer_identity() -> None:
    """The int gate is what makes carrying identity safe under the no-echo rule.

    A credential mistakenly stored in an id slot is dropped rather than carried, so
    the event holds no unvetted string by construction rather than by promise.
    """
    result = normalize_questions(
        [
            fake_sdk_question(  # type: ignore[list-item]
                question_type="date",
                id_of_question=PLANTED_SECRET,
                id_of_post=PLANTED_SECRET,
            )
        ]
    )

    (event,) = result.deferrals
    assert event.question_id is None
    assert event.post_id is None
    assert PLANTED_SECRET not in repr(event)


def test_deferral_event_names_only_known_sdk_types() -> None:
    """Same gate as the error message, and the reason records that it fired.

    Collapsing an unvetted tag to 'unknown' would otherwise erase the difference
    between a type the SDK defines and something arbitrary in the tag slot.
    """

    class _ForeignTag:
        question_type = PLANTED_SECRET
        id_of_question = 91001
        id_of_post = 90001

    result = normalize_questions([_ForeignTag()])  # type: ignore[list-item]

    (event,) = result.deferrals
    assert event.question_type == "unknown"
    assert event.reason == "unrecognized_type"
    assert PLANTED_SECRET not in repr(event)


def test_deferral_reads_no_content_field() -> None:
    """Zero model and zero submission calls, enforced at the field-access level."""
    result = normalize_questions([tripwire_question("date")])  # type: ignore[list-item]

    assert len(result.questions) == 0
    assert result.deferrals[0].question_type == "date"


def test_deferral_log_record_is_not_a_leak_vector() -> None:
    """The event is logged, and the rendered record carries no question content.

    Rendered through the real ``JsonFormatter`` rather than asserting on
    ``caplog.text``: the formatter is what production writes, and the values are
    interpolated into the message precisely because that field is redacted.
    """
    question = fake_sdk_question(
        question_type="date",
        resolution_criteria=PLANTED_SECRET,
        question_text=PLANTED_SECRET,
    )

    logger = logging.getLogger("whiskeyjack_bot.questions.normalize")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        normalize_questions([question])  # type: ignore[list-item]
    finally:
        logger.removeHandler(handler)

    (record,) = [r for r in records if r.levelno == logging.WARNING]
    rendered = JsonFormatter([]).format(record)
    payload = json.loads(rendered)

    assert payload["level"] == "WARNING"
    assert "91001" in payload["message"]
    assert "date" in payload["message"]
    assert PLANTED_SECRET not in rendered


def test_duplicate_check_ignores_deferred_questions() -> None:
    """A deferred question has no canonical model and never reaches the ledger.

    The uniqueness check exists to protect the ledger's
    ``UNIQUE (question_id, tournament_id, forecast_version)``; a deferred question
    sharing an id with an accepted one is not a collision.
    """
    accepted = fake_sdk_question(question_type="binary", id_of_question=91001)
    deferred = fake_sdk_question(question_type="date", id_of_question=91001)

    result = normalize_questions([accepted, deferred])  # type: ignore[list-item]

    assert len(result.questions) == 1
    assert len(result.deferrals) == 1


def test_normalization_result_is_frozen() -> None:
    """A value object, per the project convention for internal results."""
    result = NormalizationResult(questions=(), deferrals=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.questions = ()  # type: ignore[misc]
