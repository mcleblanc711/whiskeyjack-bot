"""Fingerprint stability under unordered API metadata (M1-331).

M1-326's gate skips a question whose recorded research verdict is deterministic, keyed on
``tournament_state.question_fingerprint``. ``tournament_state.canonical()`` sorts dict keys
only, never list elements, so before this item any reordering of a list-valued field between
two polls of the same question changed the fingerprint and made the block silently stop
matching -- the exact re-purchase M1-326 exists to prevent, reintroduced through the key.

Every property below is written per field, not per category, per the acceptance criterion:
a strategy that cannot actually permute a list (0/1 elements, or duplicate elements collapsing
under a permutation) would make the first half of every property vacuous, so each permutation
draw is followed by ``assume(permuted != original)`` -- every accepted example is a genuine
reorder, never an accidental identity permutation.
"""

from __future__ import annotations

from typing import Any

from hypothesis import assume, given, strategies as st

from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalMultipleChoiceQuestion,
    SourceCategory,
)
from whiskeyjack_bot.tournament_state import digest, question_fingerprint

# Short, distinct tokens -- what matters is that draws are unique so a permutation of them is a
# genuine reorder, not their content.
SLUGS = st.lists(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8),
    min_size=2,
    max_size=5,
    unique=True,
)
CATEGORY_IDS = st.lists(
    st.integers(min_value=1, max_value=10_000), min_size=2, max_size=5, unique=True
)
GROUP_IDS = st.lists(st.integers(min_value=1, max_value=10**9), min_size=2, max_size=5, unique=True)
OPTIONS = st.lists(
    st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=8),
    min_size=2,
    max_size=5,
    unique=True,
)


def _binary(**overrides: Any) -> CanonicalBinaryQuestion:
    fields: dict[str, Any] = {"question_id": 1, "post_id": 2, "title": "Will X happen?"}
    fields.update(overrides)
    return CanonicalBinaryQuestion(**fields)


def _multiple_choice(**overrides: Any) -> CanonicalMultipleChoiceQuestion:
    fields: dict[str, Any] = {
        "question_id": 1,
        "post_id": 2,
        "title": "Which will happen?",
        "options": ["Alpha", "Beta"],
    }
    fields.update(overrides)
    return CanonicalMultipleChoiceQuestion(**fields)


def _categories(ids: list[int]) -> list[SourceCategory]:
    return [SourceCategory(id=category_id, name=f"cat-{category_id}") for category_id in ids]


# --- Half 1: permuting an unordered list preserves the fingerprint ------------------------


@given(SLUGS, st.data())
def test_fingerprint_is_invariant_under_tournament_slugs_reordering(
    slugs: list[str], data: st.DataObject
) -> None:
    permuted = list(data.draw(st.permutations(slugs)))
    assume(permuted != slugs)
    original = _binary(tournament_slugs=slugs)
    reordered = _binary(tournament_slugs=permuted)
    assert question_fingerprint(original) == question_fingerprint(reordered)


@given(CATEGORY_IDS, st.data())
def test_fingerprint_is_invariant_under_source_categories_reordering(
    category_ids: list[int], data: st.DataObject
) -> None:
    permuted_ids = list(data.draw(st.permutations(category_ids)))
    assume(permuted_ids != category_ids)
    original = _binary(source_categories=_categories(category_ids))
    reordered = _binary(source_categories=_categories(permuted_ids))
    assert question_fingerprint(original) == question_fingerprint(reordered)


@given(GROUP_IDS, st.data())
def test_fingerprint_is_invariant_under_question_ids_of_group_reordering(
    group_ids: list[int], data: st.DataObject
) -> None:
    permuted = list(data.draw(st.permutations(group_ids)))
    assume(permuted != group_ids)
    original = _binary(question_ids_of_group=group_ids)
    reordered = _binary(question_ids_of_group=permuted)
    assert question_fingerprint(original) == question_fingerprint(reordered)


@given(OPTIONS, st.data())
def test_fingerprint_is_invariant_under_options_reordering(
    options: list[str], data: st.DataObject
) -> None:
    permuted = list(data.draw(st.permutations(options)))
    assume(permuted != options)
    original = _multiple_choice(options=options)
    reordered = _multiple_choice(options=permuted)
    assert question_fingerprint(original) == question_fingerprint(reordered)


# --- Half 2: a materially edited question still re-qualifies ------------------------------


@given(SLUGS, st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8))
def test_fingerprint_changes_when_tournament_slugs_membership_changes(
    slugs: list[str], extra: str
) -> None:
    assume(extra not in slugs)
    original = _binary(tournament_slugs=slugs)
    edited = _binary(tournament_slugs=[*slugs, extra])
    assert question_fingerprint(original) != question_fingerprint(edited)


@given(CATEGORY_IDS, st.integers(min_value=1, max_value=10_000))
def test_fingerprint_changes_when_source_categories_membership_changes(
    category_ids: list[int], extra: int
) -> None:
    assume(extra not in category_ids)
    original = _binary(source_categories=_categories(category_ids))
    edited = _binary(source_categories=_categories([*category_ids, extra]))
    assert question_fingerprint(original) != question_fingerprint(edited)


@given(GROUP_IDS, st.integers(min_value=1, max_value=10**9))
def test_fingerprint_changes_when_question_ids_of_group_membership_changes(
    group_ids: list[int], extra: int
) -> None:
    assume(extra not in group_ids)
    original = _binary(question_ids_of_group=group_ids)
    edited = _binary(question_ids_of_group=[*group_ids, extra])
    assert question_fingerprint(original) != question_fingerprint(edited)


@given(OPTIONS, st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=1, max_size=8))
def test_fingerprint_changes_when_options_membership_changes(
    options: list[str], extra: str
) -> None:
    assume(extra not in options)
    original = _multiple_choice(options=options)
    edited = _multiple_choice(options=[*options, extra])
    assert question_fingerprint(original) != question_fingerprint(edited)


TITLES = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=1, max_size=30).filter(
    lambda s: s.strip()
)


@given(TITLES, TITLES)
def test_fingerprint_changes_when_title_changes(first_title: str, second_title: str) -> None:
    """Sanity check that canonicalization did not make the digest insensitive to
    ordinary scalar fields -- only the four unordered lists are meant to collapse."""
    assume(first_title.strip() != second_title.strip())
    first = _binary(title=first_title)
    second = _binary(title=second_title)
    assert question_fingerprint(first) != question_fingerprint(second)


# --- Fixed point: canonicalization is a no-op on already-canonical input ------------------


@given(SLUGS, CATEGORY_IDS, GROUP_IDS)
def test_fingerprint_matches_todays_formula_on_already_sorted_binary_question(
    slugs: list[str], category_ids: list[int], group_ids: list[int]
) -> None:
    """Proves canonicalization changes nothing for a question whose list order is
    already canonical -- the property-level complement to the live-ledger count in
    docs/M1-NOTES.md showing most recorded questions are already in this state."""
    question = _binary(
        tournament_slugs=sorted(slugs),
        source_categories=_categories(sorted(category_ids)),
        question_ids_of_group=sorted(group_ids),
    )
    assert question_fingerprint(question) == digest(question.model_dump(mode="json"))


@given(OPTIONS)
def test_fingerprint_matches_todays_formula_on_already_sorted_multiple_choice_question(
    options: list[str],
) -> None:
    question = _multiple_choice(options=sorted(options))
    assert question_fingerprint(question) == digest(question.model_dump(mode="json"))
