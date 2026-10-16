"""Invariants of lens-versus-baseline overlap and syndication clustering (M1-356).

- **Order independence.** Classes and clusters do not depend on the order documents arrive
  in. The cluster representative is chosen by a total order, so a permutation cannot change
  which copy of a report speaks for it.
- **Partition.** Every lens document is in exactly one cluster, and has exactly one class.
- **Symmetry.** ``near_duplicate(a, b) == near_duplicate(b, a)``; union-find assumes it.
- **Conservatism.** A report is never counted as independent when any copy of it is
  already in the baseline, and an untitled document is never ``novel``.
- **Replay stability.** Building comparables from a document after a store-and-reload
  through the persisted form gives the same classes and clusters.

The generators deliberately draw from a **small** title vocabulary, publisher set and time
grid, so that near-duplicates, wire copies and shared URLs actually occur. A strategy over
free text would almost never produce two titles that overlap, and every property about
clustering would then be about singletons -- the vacuous-strategy defect this project has
paid for repeatedly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from hypothesis import given, strategies as st
from strategies import research_documents

from whiskeyjack_bot.research.model import validate_document
from whiskeyjack_bot.research.overlap import (
    Comparable,
    classify,
    clusters,
    comparable,
    independent_reports,
    near_duplicate,
)

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
_WORDS = ["iran", "tanker", "fire", "gulf", "oman", "navy", "strait", "closed", "talks", "oil"]
_TITLES = st.one_of(
    st.none(),
    st.lists(st.sampled_from(_WORDS), min_size=1, max_size=6).map(" ".join),
)


@st.composite
def _items(draw: st.DrawFn, run: str) -> list[Comparable]:
    count = draw(st.integers(0, 7))
    result: list[Comparable] = []
    for index in range(count):
        url = f"https://e.org/{draw(st.integers(0, 4))}"
        digest = f"{draw(st.integers(0, 4)):064x}"
        result.append(
            Comparable(
                key=(run, url + f"#{index}", digest),
                canonical_url=url,
                content_sha256=digest,
                publisher=draw(st.sampled_from([None, "AFP", "afp", "Reuters"])),
                published_at_utc=draw(
                    st.none() | st.integers(0, 40).map(lambda h: T0 + timedelta(hours=h))
                ),
                english_title=draw(_TITLES),
            )
        )
    return result


@given(_items("run-b"), _items("run-l"), st.randoms(use_true_random=False))
def test_classes_and_clusters_do_not_depend_on_input_order(
    base: list[Comparable], lens: list[Comparable], rnd: object
) -> None:
    shuffled_base, shuffled_lens = list(base), list(lens)
    rnd.shuffle(shuffled_base)  # type: ignore[attr-defined]
    rnd.shuffle(shuffled_lens)  # type: ignore[attr-defined]
    assert classify(base, lens) == classify(shuffled_base, shuffled_lens)
    assert clusters(lens) == clusters(shuffled_lens)
    assert independent_reports(base, lens) == independent_reports(shuffled_base, shuffled_lens)


@given(_items("run-b"), _items("run-l"))
def test_every_lens_document_has_one_class_and_one_cluster(
    base: list[Comparable], lens: list[Comparable]
) -> None:
    verdicts = classify(base, lens)
    assert set(verdicts) == {item.key for item in lens}
    grouped = [key for group in clusters(lens) for key in group]
    assert sorted(grouped) == sorted(item.key for item in lens)


@given(_items("run-l"))
def test_near_duplicate_is_symmetric_and_clusters_respect_it(lens: list[Comparable]) -> None:
    for left in lens:
        for right in lens:
            assert near_duplicate(left, right) == near_duplicate(right, left)
    membership = {key: index for index, group in enumerate(clusters(lens)) for key in group}
    for left in lens:
        for right in lens:
            if near_duplicate(left, right):
                assert membership[left.key] == membership[right.key]


@given(_items("run-b"), _items("run-l"))
def test_no_independent_report_contains_a_held_or_untitled_only_copy(
    base: list[Comparable], lens: list[Comparable]
) -> None:
    verdicts = classify(base, lens)
    for group in independent_reports(base, lens):
        classes = {verdicts[key] for key in group}
        assert "novel" in classes
        assert not classes & {"same_url", "same_content", "republication"}
    for item in lens:
        if item.english_title is None:
            assert verdicts[item.key] != "novel"


@given(st.lists(research_documents(), max_size=5), st.lists(research_documents(), max_size=5))
def test_classification_survives_a_store_and_reload(base_docs: list, lens_docs: list) -> None:  # type: ignore[type-arg]
    def reload(document: object) -> object:
        dumped = json.dumps(
            document.model_dump(mode="json"),  # type: ignore[attr-defined]
            ensure_ascii=True,
            sort_keys=True,
        )
        return validate_document(json.loads(dumped))

    lens_unique = {(d.retrieval_run_id, d.canonical_url, d.content_sha256): d for d in lens_docs}
    lens_docs = list(lens_unique.values())
    before = (
        [comparable(d, english_title=d.title) for d in base_docs],
        [comparable(d, english_title=d.title) for d in lens_docs],
    )
    after = (
        [comparable(reload(d), english_title=reload(d).title) for d in base_docs],  # type: ignore[arg-type,attr-defined]
        [comparable(reload(d), english_title=reload(d).title) for d in lens_docs],  # type: ignore[arg-type,attr-defined]
    )
    assert classify(*before) == classify(*after)
    assert clusters(before[1]) == clusters(after[1])
