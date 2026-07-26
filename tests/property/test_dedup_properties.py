"""Invariants of the dedup survivor rule (M1-305).

Every property here corresponds to a finding a cross-model review round produced by
hand, over four round-trips. Stated as invariants, one local run covers all of them.
"""

from __future__ import annotations

import itertools
import random

from hypothesis import given, strategies as st
from strategies import persisted, research_documents, round_trip

from whiskeyjack_bot.research import ResearchDocument
from whiskeyjack_bot.research.dedup import _sort_key, dedup_key, deduplicate

DOCUMENT_LISTS = st.lists(research_documents(), max_size=6)


@given(research_documents())
def test_keys_never_raise(document: ResearchDocument) -> None:
    """Round 2: the tiebreak raised on a schema-valid document holding a lone
    surrogate, because model_dump_json() cannot encode one. Any document the schema
    accepts must be keyable."""
    assert len(dedup_key(document)) == 3
    assert len(_sort_key(document)) == 3


@given(research_documents(), research_documents())
def test_sort_key_is_a_strict_weak_order(first: ResearchDocument, second: ResearchDocument) -> None:
    """Exactly one of <, >, == holds. A survivor chosen by min() is only well defined
    if the order is total."""
    left, right = _sort_key(first), _sort_key(second)
    assert (left < right) + (right < left) + (left == right) == 1


@given(st.lists(research_documents(), min_size=3, max_size=3))
def test_sort_key_is_transitive(documents: list[ResearchDocument]) -> None:
    first, second, third = sorted(_sort_key(document) for document in documents)
    assert first <= second <= third
    assert first <= third


@given(research_documents(), research_documents())
def test_persisted_equality_implies_key_equality(
    first: ResearchDocument, second: ResearchDocument
) -> None:
    """Round 4: the order is total over *persisted* forms, deliberately not over
    in-memory identity. Two documents that store identically must key identically, or
    the survivor a replay picks can differ from the one the live run picked."""
    if persisted(first) == persisted(second):
        assert _sort_key(first) == _sort_key(second)


@given(DOCUMENT_LISTS)
def test_deduplicate_conserves_documents(documents: list[ResearchDocument]) -> None:
    result = deduplicate(documents)
    assert len(result.documents) + result.collapsed_count == len(documents)
    assert len({dedup_key(document) for document in result.documents}) == len(result.documents)


@given(DOCUMENT_LISTS)
def test_survivor_is_an_input_with_the_minimal_key(documents: list[ResearchDocument]) -> None:
    """The survivor is a member of its own collision group and the minimum of it --
    never a merged or synthesized document."""
    result = deduplicate(documents)
    for survivor in result.documents:
        group = [d for d in documents if dedup_key(d) == dedup_key(survivor)]
        assert persisted(survivor) in {persisted(d) for d in group}
        assert _sort_key(survivor) == min(_sort_key(d) for d in group)


@given(DOCUMENT_LISTS, st.randoms(use_true_random=True))
def test_deduplicate_is_permutation_invariant(
    documents: list[ResearchDocument], rng: random.Random
) -> None:
    """Round 1, finding 2: the survivor must not depend on the order the providers
    happened to return documents in. Output *order* is first-seen by contract; the
    surviving set and the collapsed count are not order-dependent at all."""
    shuffled = list(documents)
    rng.shuffle(shuffled)
    original = deduplicate(documents)
    reordered = deduplicate(shuffled)
    assert original.collapsed_count == reordered.collapsed_count
    assert {persisted(d) for d in original.documents} == {persisted(d) for d in reordered.documents}


@given(DOCUMENT_LISTS)
def test_deduplicate_is_replay_stable(documents: list[ResearchDocument]) -> None:
    """Round 3: replay reconstructs documents from the ledger's JSON, so dedup over
    the stored form must pick the same survivors as dedup over the live objects. This
    is the property the datetime.fold bug violated."""
    live = deduplicate(documents)
    replayed = deduplicate([round_trip(document) for document in documents])
    assert live.collapsed_count == replayed.collapsed_count
    assert [persisted(d) for d in live.documents] == [persisted(d) for d in replayed.documents]


@given(research_documents())
def test_round_trip_is_a_fixed_point(document: ResearchDocument) -> None:
    """Storing an already-stored document changes nothing; otherwise a replay of a
    replay could drift."""
    once = round_trip(document)
    assert persisted(once) == persisted(round_trip(once))


@given(DOCUMENT_LISTS)
def test_dedup_never_merges_across_runs(documents: list[ResearchDocument]) -> None:
    """Round 1, finding 1: the key includes retrieval_run_id because two providers
    surfacing one article are two legitimate ledger rows. Collapsing them would erase
    which run found the evidence."""
    result = deduplicate(documents)
    for first, second in itertools.combinations(result.documents, 2):
        if first.retrieval_run_id != second.retrieval_run_id:
            assert dedup_key(first) != dedup_key(second)
