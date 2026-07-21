"""M1-202 acceptance: unpacked fixtures produce one unique internal question per
subquestion and no duplicate IDs.

The criterion is written against a specific trap. Group expansion deep-copies the
*parent post* once per subquestion, so every sibling shares ``post_id``, ``url`` and
the parent's framing fields; only ``question_id`` tells them apart. These tests pin
that ``question_id`` is the identity anchor, that the parent linkage and title
survive, and that a duplicate id is refused at the normalization boundary rather
than at the ledger's unique constraint (which is only reached after a forecast has
been generated).
"""

import json
import traceback
from pathlib import Path
from typing import Any

import pytest
from forecasting_tools.data_models.data_organizer import DataOrganizer
from forecasting_tools.helpers.metaculus_client import MetaculusClient

from whiskeyjack_bot.questions import (
    NormalizationError,
    UnsupportedQuestionTypeError,
    is_group_post,
    normalize_question,
    normalize_questions,
    unpack_group_post,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GROUP_POST = FIXTURES / "api_posts" / "group" / "minibench_group.json"

PLANTED_SECRET = "privateFAKE123456"

# Set at construction time, so two expansions of the same post legitimately differ.
_CONSTRUCTION_TIME_FIELDS = {"date_accessed"}


def raw_group_post() -> dict[str, Any]:
    return json.loads(GROUP_POST.read_text(encoding="utf-8"))


def canonical_group() -> list[Any]:
    return normalize_questions(unpack_group_post(raw_group_post()))


# --- acceptance -------------------------------------------------------------


def test_unpacked_group_yields_one_unique_question_per_subquestion() -> None:
    """The acceptance criterion, stated directly."""
    post = raw_group_post()
    subquestion_count = len(post["group_of_questions"]["questions"])
    canonical = canonical_group()

    assert len(canonical) == subquestion_count
    ids = [q.question_id for q in canonical]
    assert len(set(ids)) == len(ids)
    assert set(ids) == {q["id"] for q in post["group_of_questions"]["questions"]}


def test_siblings_share_the_post_but_not_the_question_id() -> None:
    """Pins the exact collision the criterion is written against.

    If a future refactor keys internal identity on ``post_id`` or ``url``, a whole
    group collapses to a single record. This fails loudly when that happens.
    """
    canonical = canonical_group()

    assert len({q.post_id for q in canonical}) == 1
    assert len({q.url for q in canonical}) == 1
    assert len({q.question_id for q in canonical}) == len(canonical)


def test_parent_framing_overrides_every_subquestion() -> None:
    """The resolution rules live once on the parent and govern every sibling.

    The fixture's first subquestion carries its own description/criteria/fine print
    precisely so this test can prove they are replaced rather than preserved.
    """
    post = raw_group_post()
    group = post["group_of_questions"]
    canonical = canonical_group()

    assert {q.resolution_criteria for q in canonical} == {group["resolution_criteria"]}
    assert {q.fine_print for q in canonical} == {group["fine_print"]}
    assert {q.background_info for q in canonical} == {group["description"]}


def test_explicit_parent_null_overrides_subquestion_value() -> None:
    """An explicit null is an override, not an absence.

    ``unpack_group_post`` skips parent keys that are *absent*, where the SDK would
    raise. It does not skip keys carried explicitly as null: those still overwrite,
    matching the SDK, because the parent stating a field is empty for the whole group
    is not the same as the parent not addressing it at all.

    Pins the corrected claim from review -- the deviation was first documented as
    "never erases a subquestion's own value", which this case disproves.
    """
    post = raw_group_post()
    assert post["group_of_questions"]["questions"][0]["fine_print"], (
        "fixture no longer gives the first subquestion its own fine print"
    )
    post["group_of_questions"]["fine_print"] = None

    assert {q.fine_print for q in unpack_group_post(post)} == {None}


# --- parent identity --------------------------------------------------------


def test_parent_linkage_and_title_survive_unpacking() -> None:
    post = raw_group_post()
    expected_ids = [q["id"] for q in post["group_of_questions"]["questions"]]
    expected_labels = {q["label"] for q in post["group_of_questions"]["questions"]}
    canonical = canonical_group()

    for question in canonical:
        assert question.question_ids_of_group == expected_ids
        assert question.group_parent_title == post["title"]
    assert {q.group_question_option for q in canonical} == expected_labels


def test_group_parent_title_is_load_bearing_not_decorative() -> None:
    """At least one subquestion is unforecastable without the parent title.

    Metaculus titles some subquestions with only their option label ("September
    2026"). Such a title states no question, so the parent's must be carried or the
    forecaster receives a bare period name. The fixture contains one deliberately.
    """
    canonical = canonical_group()

    bare = [q for q in canonical if q.title == q.group_question_option]
    assert bare, "fixture no longer exercises the label-only-title case"
    for question in bare:
        assert question.group_parent_title
        assert question.group_parent_title != question.title


def test_non_group_questions_carry_no_group_fields() -> None:
    """No accidental coupling: the group fields stay None off the group path."""
    post = json.loads((FIXTURES / "api_posts" / "binary_post.json").read_text(encoding="utf-8"))
    canonical = normalize_question(DataOrganizer.get_question_from_post_json(post))

    assert canonical.group_question_option is None
    assert canonical.question_ids_of_group is None
    assert canonical.group_parent_title is None


def test_group_parent_title_is_none_without_a_retained_payload() -> None:
    """A question replayed from a snapshot need not carry the raw post payload.

    The parent title is a best-effort recovery, not a required field: its absence
    must degrade to None rather than raise.
    """
    questions = unpack_group_post(raw_group_post())
    for question in questions:
        question.api_json = {}

    for canonical in normalize_questions(questions):
        assert canonical.question_ids_of_group
        assert canonical.group_parent_title is None


# --- SDK drift --------------------------------------------------------------


def test_our_unpacking_matches_the_pinned_sdk() -> None:
    """Drift alarm for owning ~20 lines the SDK also implements.

    We expand groups ourselves because the SDK's version is a private static method
    on a network-bound client. That is only safe while the two agree, so this pins
    them field-by-field on the same fixture and fails on an SDK bump that changes
    expansion semantics.
    """
    ours = unpack_group_post(raw_group_post())
    theirs = MetaculusClient._unpack_group_question(raw_group_post())

    assert len(ours) == len(theirs)
    for mine, sdk in zip(ours, theirs, strict=True):
        mine_dump, sdk_dump = mine.model_dump(), sdk.model_dump()
        # api_json is the raw post echoed back verbatim by both; comparing it adds
        # nothing and dominates the diff on failure.
        keys = (mine_dump.keys() | sdk_dump.keys()) - _CONSTRUCTION_TIME_FIELDS - {"api_json"}
        for key in keys:
            assert mine_dump.get(key) == sdk_dump.get(key), f"diverged on {key!r}"


# --- duplicate rejection ----------------------------------------------------


def test_duplicate_question_ids_are_refused() -> None:
    """Refused at the boundary, not at the ledger's unique constraint.

    A collision reaching the ledger is only discovered after a forecast has been
    generated and paid for.
    """
    questions = unpack_group_post(raw_group_post())
    questions[1].id_of_question = questions[0].id_of_question

    with pytest.raises(NormalizationError) as excinfo:
        normalize_questions(questions)

    assert "duplicate" in str(excinfo.value)


def test_duplicate_rejection_does_not_echo_ids_or_content() -> None:
    questions = unpack_group_post(raw_group_post())
    questions[1].id_of_question = questions[0].id_of_question
    for question in questions:
        question.question_text = PLANTED_SECRET

    with pytest.raises(NormalizationError) as excinfo:
        normalize_questions(questions)

    rendered = str(excinfo.value) + "".join(traceback.format_exception(excinfo.value))
    assert PLANTED_SECRET not in rendered
    assert str(questions[0].id_of_question) not in rendered


def test_unique_ids_still_normalize() -> None:
    """The guard must not reject the ordinary case."""
    assert len(canonical_group()) == 3


# --- malformed posts --------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.pop("group_of_questions"), id="no_group_block"),
        pytest.param(lambda p: p.__setitem__("group_of_questions", []), id="group_not_a_dict"),
        pytest.param(
            lambda p: p["group_of_questions"].__setitem__("questions", []),
            id="no_subquestions",
        ),
        pytest.param(
            lambda p: p["group_of_questions"].__setitem__("questions", {"id": 1}),
            id="subquestions_not_a_list",
        ),
        pytest.param(
            lambda p: p["group_of_questions"].__setitem__("questions", ["not-a-dict"]),
            id="subquestion_not_a_dict",
        ),
        pytest.param(
            lambda p: p["group_of_questions"]["questions"][1].pop("id"),
            id="subquestion_without_id",
        ),
        pytest.param(
            lambda p: p["group_of_questions"]["questions"][1].pop("type"),
            id="subquestion_without_type",
        ),
        pytest.param(
            lambda p: p["group_of_questions"]["questions"][1].__setitem__("type", "made_up"),
            id="unknown_subquestion_type",
        ),
    ],
)
def test_malformed_group_posts_raise_normalization_error(mutate: Any) -> None:
    """Never a raw KeyError/ValueError/AssertionError.

    Callers handle this package's own error type only; a raw exception escaping is
    the defect class already found twice in review.
    """
    post = raw_group_post()
    mutate(post)

    with pytest.raises(NormalizationError):
        unpack_group_post(post)


def test_malformed_group_post_does_not_echo_content() -> None:
    """The SDK's own ValueError interpolates the offending type string.

    Every field is planted, not only the ones already known to leak: the narrow
    version of this test is what let a High through on M1-301.
    """
    post = raw_group_post()
    post["title"] = PLANTED_SECRET
    post["group_of_questions"]["description"] = PLANTED_SECRET
    post["group_of_questions"]["fine_print"] = PLANTED_SECRET
    post["group_of_questions"]["resolution_criteria"] = PLANTED_SECRET
    for subquestion in post["group_of_questions"]["questions"]:
        subquestion["title"] = PLANTED_SECRET
        subquestion["label"] = PLANTED_SECRET
        subquestion["type"] = PLANTED_SECRET

    with pytest.raises(NormalizationError) as excinfo:
        unpack_group_post(post)

    assert PLANTED_SECRET not in str(excinfo.value)
    assert PLANTED_SECRET not in "".join(traceback.format_exception(excinfo.value))


def test_is_group_post_discriminates() -> None:
    assert is_group_post(raw_group_post())
    assert not is_group_post(
        json.loads((FIXTURES / "api_posts" / "binary_post.json").read_text(encoding="utf-8"))
    )


def test_deferred_subquestion_types_are_refused_by_normalize_not_unpack() -> None:
    """Type policy stays in one place (D21).

    A well-formed date subquestion expands without complaint and is refused
    downstream by ``normalize``, so the reason reported is the real one ("type not
    supported in v1") rather than "malformed group". Refusal still happens before
    any model or submission call.

    The date payload is built out fully rather than by flipping the type string: the
    SDK parses a subquestion against its declared type, so a binary-shaped block
    labelled ``date`` fails at expansion and would test nothing about type policy.
    """
    post = raw_group_post()
    subquestion = post["group_of_questions"]["questions"][1]
    subquestion["type"] = "date"
    subquestion["open_lower_bound"] = True
    subquestion["open_upper_bound"] = True
    # range_min/range_max are floated before being parsed as dates, so they are epoch
    # seconds here: 2026-01-01 and 2026-12-31.
    subquestion["scaling"] = {
        "range_min": 1767225600.0,
        "range_max": 1798761600.0,
        "zero_point": None,
    }

    questions = unpack_group_post(post)
    assert len(questions) == 3

    with pytest.raises(UnsupportedQuestionTypeError) as excinfo:
        normalize_questions(questions)
    assert "date" in str(excinfo.value)
