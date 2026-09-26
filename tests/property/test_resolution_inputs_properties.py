"""The pre-post question equality check is order-insensitive, not membership-blind (M1-340).

``submission_policy.resolution_inputs`` is what ``before_post`` compares between the stored
record's question and the live refetch. Two halves, each per field, because the acceptance
criterion has two halves and ignoring a field satisfies only the first:

* a pure reordering of ``question_ids_of_group`` or ``options`` compares equal;
* a real membership change -- a member replaced, added or dropped, or (for group ids, which the
  schema does not require to be unique) a member's *count* changed -- compares unequal.

Every permutation draw is followed by ``assume(permuted != original)`` so an accepted example is
a genuine reorder, and ``event()`` records which kind of membership change each unequal example
exercised so a strategy that stopped reaching one shows in ``--hypothesis-show-statistics``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import assume, event, given, strategies as st

from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalMultipleChoiceQuestion,
)
from whiskeyjack_bot.submission_policy import resolution_inputs

# A narrow range as well as a wide one, so duplicate ids -- which the schema allows -- are drawn
# often enough for the recount case to be reached, not merely possible.
GROUP_IDS = st.lists(
    st.integers(min_value=1, max_value=4) | st.integers(min_value=1, max_value=10**9),
    min_size=2,
    max_size=6,
)
OPTIONS = st.lists(
    st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ ", min_size=1, max_size=8).filter(str.strip),
    min_size=2,
    max_size=6,
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


# --- Half 1: a pure reordering compares equal ----------------------------------------------


@given(GROUP_IDS, st.data())
def test_reordered_group_ids_compare_equal(ids: list[int], data: st.DataObject) -> None:
    permuted = list(data.draw(st.permutations(ids)))
    assume(permuted != ids)
    event("duplicate ids" if len(set(ids)) < len(ids) else "distinct ids")
    assert resolution_inputs(_binary(question_ids_of_group=ids)) == resolution_inputs(
        _binary(question_ids_of_group=permuted)
    )


@given(OPTIONS, st.data())
def test_reordered_options_compare_equal(options: list[str], data: st.DataObject) -> None:
    permuted = list(data.draw(st.permutations(options)))
    assume(permuted != options)
    assert resolution_inputs(_multiple_choice(options=options)) == resolution_inputs(
        _multiple_choice(options=permuted)
    )


# --- Half 2: a real membership change still compares unequal ---------------------------------


@st.composite
def _changed_group_ids(draw: st.DrawFn) -> tuple[list[int], list[int]]:
    ids = draw(GROUP_IDS)
    kind = draw(st.sampled_from(["replaced", "added", "dropped"]))
    changed = list(ids)
    if kind == "replaced":
        index = draw(st.integers(0, len(ids) - 1))
        changed[index] = draw(st.integers(1, 10**9).filter(lambda n: n != ids[index]))
    elif kind == "added":
        changed.append(draw(st.integers(1, 10**9)))
    else:
        changed.pop(draw(st.integers(0, len(ids) - 1)))
    changed = draw(st.permutations(changed)).copy()
    assume(sorted(changed) != sorted(ids))
    event(f"group ids {kind}")
    return ids, changed


@given(_changed_group_ids())
def test_changed_group_membership_compares_unequal(pair: tuple[list[int], list[int]]) -> None:
    ids, changed = pair
    assert resolution_inputs(_binary(question_ids_of_group=ids)) != resolution_inputs(
        _binary(question_ids_of_group=changed)
    )


@st.composite
def _changed_options(draw: st.DrawFn) -> tuple[list[str], list[str]]:
    options = draw(OPTIONS)
    kind = draw(st.sampled_from(["relabelled", "added", "dropped"]))
    changed = list(options)
    if kind == "relabelled":
        index = draw(st.integers(0, len(options) - 1))
        changed[index] = options[index] + "X"
        assume(len(set(changed)) == len(changed))
    elif kind == "added":
        extra = draw(st.text(alphabet="abc", min_size=1, max_size=4))
        changed.append(extra)
    else:
        assume(len(options) > 2)
        changed.pop(draw(st.integers(0, len(options) - 1)))
    changed = draw(st.permutations(changed)).copy()
    event(f"options {kind}")
    return options, changed


@given(_changed_options())
def test_changed_option_membership_compares_unequal(pair: tuple[list[str], list[str]]) -> None:
    options, changed = pair
    assert resolution_inputs(_multiple_choice(options=options)) != resolution_inputs(
        _multiple_choice(options=changed)
    )


@given(
    st.lists(st.integers(1, 10**9), min_size=2, max_size=2, unique=True),
    st.lists(st.integers(1, 10**9), max_size=4),
    st.data(),
)
def test_a_recounted_group_compares_unequal(
    pair: list[int], rest: list[int], data: st.DataObject
) -> None:
    """Same *set* of sibling ids, different multiset: one of ``a``'s two copies becomes ``b``.
    The schema does not make ``question_ids_of_group`` unique, and this is exactly the case a
    ``set()`` comparison would wrongly call unchanged -- constructed rather than filtered for, so
    every example reaches it."""
    a, b = pair
    ids = [a, a, b, *rest]
    changed = list(data.draw(st.permutations([a, b, b, *rest])))
    assert set(ids) == set(changed)
    assert resolution_inputs(_binary(question_ids_of_group=ids)) != resolution_inputs(
        _binary(question_ids_of_group=changed)
    )


def test_a_same_set_different_count_group_is_a_change() -> None:
    """The recount case pinned as an example, so it runs whatever the strategy draws."""
    assert resolution_inputs(_binary(question_ids_of_group=[1, 1, 2])) != resolution_inputs(
        _binary(question_ids_of_group=[1, 2, 2])
    )


def test_a_group_member_and_a_non_group_question_differ() -> None:
    assert resolution_inputs(_binary(question_ids_of_group=[1, 2])) != resolution_inputs(
        _binary(question_ids_of_group=None)
    )


# --- The rest of the contract is unchanged ----------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [("tournament_slugs", ["other"]), ("question_weight", 0.5)],
)
def test_ignored_metadata_stays_ignored(field: str, value: Any) -> None:
    assert resolution_inputs(_binary()) == resolution_inputs(_binary(**{field: value}))


@given(GROUP_IDS)
def test_the_input_question_is_not_mutated_and_the_result_replays(ids: list[int]) -> None:
    """Sorting happens on the dump, never on the stored question -- the record is what a
    forecast was generated from -- and the compared form survives the persisted round-trip."""
    question = _binary(question_ids_of_group=list(ids))
    first = resolution_inputs(question)
    assert question.question_ids_of_group == ids
    replayed = type(question).model_validate(
        json.loads(json.dumps(question.model_dump(mode="json"), ensure_ascii=True, sort_keys=True))
    )
    assert resolution_inputs(replayed) == first
