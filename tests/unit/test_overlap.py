"""Lens-versus-baseline overlap and syndication clusters (M1-356)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from whiskeyjack_bot.research.overlap import (
    TITLE_JACCARD,
    WIRE_JACCARD,
    WIRE_WINDOW,
    Comparable,
    OverlapError,
    classify,
    clusters,
    comparable,
    independent_reports,
    near_duplicate,
    title_tokens,
)

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _item(
    name: str,
    title: str | None,
    *,
    publisher: str | None = None,
    at: datetime | None = T0,
    url: str | None = None,
    digest: str | None = None,
    run: str = "run-lens",
) -> Comparable:
    canonical = url or f"https://example.org/{name}"
    content = digest or name.encode().hex().ljust(64, "0")[:64]
    return Comparable(
        key=(run, canonical, content),
        canonical_url=canonical,
        content_sha256=content,
        publisher=publisher,
        published_at_utc=at,
        english_title=title,
    )


# English titles AskNews gave five language copies of one AFP story in the 2026-10-16 probe
# (countries=["IR"]). A fixture recorded from that probe, never a statistic.
AFP_COPIES = [
    ("en", "Iran's Military Reports Oil Tanker Fire in Gulf of Oman Prohibited Zone", 0),
    ("ar", "Iranian Military Reports Fire on Non-Compliant Tanker in Gulf of Oman", 4),
    ("es", "Iran Claims Oil Tanker Afire in Gulf of Oman After Violating No-Go Zone", 45),
    ("pt", "Iran Claims Tanker Afire in Gulf of Oman After Violating Restricted Zone", 93),
    ("fr", "Iranian Army Reports Tanker Fire in Gulf of Oman Amidst Strait of Hormuz Conflict", -4),
]
AFP_OTHER_STORY = (
    "Iranian Revolutionary Guards Claim Container Ship Sank After Hitting Mine in Hormuz"
)


def _afp() -> list[Comparable]:
    return [
        _item(lang, title, publisher="AFP", at=T0 + timedelta(minutes=minutes))
        for lang, title, minutes in AFP_COPIES
    ]


def test_the_constants_are_the_documented_ones() -> None:
    assert (TITLE_JACCARD, WIRE_JACCARD, WIRE_WINDOW) == (0.6, 0.3, timedelta(hours=12))


def test_five_language_copies_of_one_wire_story_are_one_report() -> None:
    copies = _afp()
    found = clusters(copies)
    assert len(found) == 1
    # Representative is the earliest published: the French copy, 4 minutes before T0.
    assert found[0][0] == copies[4].key
    assert set(found[0]) == {item.key for item in copies}


def test_a_different_story_from_the_same_agency_stays_separate() -> None:
    other = _item("mine", AFP_OTHER_STORY, publisher="AFP", at=T0 - timedelta(days=1))
    assert len(clusters([*_afp(), other])) == 2


def test_the_wire_rule_needs_the_same_publisher() -> None:
    left = _item("a", AFP_COPIES[0][1], publisher="AFP")
    right = _item("b", AFP_COPIES[1][1], publisher="Reuters")
    assert not near_duplicate(left, right)


def test_the_wire_rule_needs_both_times_and_closeness() -> None:
    left = _item("a", AFP_COPIES[0][1], publisher="AFP")
    far = _item("b", AFP_COPIES[1][1], publisher="AFP", at=T0 + WIRE_WINDOW + timedelta(seconds=1))
    undated = _item("c", AFP_COPIES[1][1], publisher="AFP", at=None)
    edge = _item("d", AFP_COPIES[1][1], publisher="AFP", at=T0 + WIRE_WINDOW)
    assert not near_duplicate(left, far)
    assert not near_duplicate(left, undated)
    assert near_duplicate(left, edge)


def test_publisher_comparison_folds_case_and_spacing() -> None:
    left = _item("a", AFP_COPIES[0][1], publisher="AFP")
    right = _item("b", AFP_COPIES[1][1], publisher=" afp ")
    assert near_duplicate(left, right)


def test_the_cross_publisher_threshold_is_inclusive() -> None:
    # Six shared tokens of ten distinct ones is exactly 0.6; four of nine (0.44) is below.
    left = _item("a", "alpha bravo charlie delta echo foxtrot", publisher="X")
    right = _item(
        "b", "alpha bravo charlie delta echo foxtrot golf hotel india juliet", publisher="Y"
    )
    assert title_tokens(left.english_title) <= title_tokens(right.english_title)
    assert near_duplicate(left, right)
    lower = _item("c", "alpha bravo charlie delta kilo lima mike", publisher="Y")
    assert not near_duplicate(left, lower)


def test_classify_assigns_exactly_one_class_with_identity_first() -> None:
    base = [
        _item("b1", "Iran declares the strait closed to shipping", url="https://x.ir/1", run="b"),
        _item("b2", "Unrelated headline about grain prices", digest="ab" * 32, run="b"),
        _item("b3", "Oman foreign minister visits Tehran for talks", run="b"),
    ]
    lens = [
        _item("l1", "Something else", url="https://x.ir/1"),  # same_url
        _item("l2", None, digest="ab" * 32),  # same_content, even untitled
        _item("l3", "Oman Foreign Minister Visits Tehran for Talks Today"),  # republication
        _item("l4", None),  # unassessed: untranslated
        _item("l5", "Iranian navy seizes tanker near Bandar Abbas"),  # novel
        _item("l6", "the and for"),  # only stop words: nothing to compare
    ]
    verdicts = classify(base, lens)
    assert [verdicts[item.key] for item in lens] == [
        "same_url",
        "same_content",
        "republication",
        "unassessed",
        "novel",
        "unassessed",
    ]


def test_an_untranslated_lens_document_is_never_counted_as_new() -> None:
    lens = [_item("fa", None)]
    assert classify([], lens) == {lens[0].key: "unassessed"}
    assert independent_reports([], lens) == ()


def test_independent_reports_count_a_cluster_once_and_drop_held_ones() -> None:
    copies = _afp()
    base = [_item("held", "Iranian navy seizes tanker near Bandar Abbas", run="b")]
    fresh = _item("fresh", "Oman reopens Musandam port after closure")
    held_copy = _item("again", "Iranian Navy Seizes Tanker Near Bandar Abbas")
    reports = independent_reports(base, [*copies, fresh, held_copy])
    assert len(reports) == 2  # the AFP cluster and the fresh story; the held copy adds nothing


def test_a_cluster_with_any_member_already_held_is_not_independent() -> None:
    copies = _afp()
    base = [_item("held", AFP_COPIES[2][1], run="b")]  # the baseline holds the Spanish copy
    assert independent_reports(base, copies) == ()


def test_clusters_are_transitive() -> None:
    # a~b and b~c by the wire rule; a and c alone are below every threshold.
    a = _item("a", "alpha bravo charlie delta", publisher="AFP")
    b = _item("b", "alpha bravo charlie echo foxtrot golf", publisher="AFP")
    c = _item("c", "echo foxtrot golf hotel india juliet", publisher="AFP")
    assert near_duplicate(a, b) and near_duplicate(b, c) and not near_duplicate(a, c)
    assert len(clusters([a, b, c])) == 1


def test_undated_documents_never_represent_a_dated_cluster() -> None:
    dated = _item("d", "alpha bravo charlie", url="https://e.org/z")
    undated = _item("u", "alpha bravo charlie", url="https://e.org/a", at=None)
    assert clusters([undated, dated])[0][0] == dated.key


def test_duplicate_lens_keys_are_refused() -> None:
    item = _item("a", "x y z")
    with pytest.raises(OverlapError):
        clusters([item, item])
    with pytest.raises(OverlapError):
        classify([], [item, item])


@pytest.mark.parametrize("bad", [None, 3, ["not comparable"]])
def test_wrong_shapes_raise_the_module_error(bad: object) -> None:
    with pytest.raises(OverlapError):
        classify([], bad)  # type: ignore[arg-type]
    with pytest.raises(OverlapError):
        clusters(bad)  # type: ignore[arg-type]


def test_comparable_reads_the_document_and_takes_the_title_from_the_caller() -> None:
    from whiskeyjack_bot.research.model import validate_document

    document = validate_document(
        {
            "retrieval_run_id": "run-1",
            "original_url": "https://www.mehrnews.com/news/1",
            "canonical_url": "https://www.mehrnews.com/news/1",
            "retrieved_at_utc": T0,
            "published_at_utc": T0,
            "source_type": "news",
            "provenance": "direct_api",
            "content_sha256": "c" * 64,
            "title": "تنگه هرمز باز است یا بسته؟",
            "publisher": "Mehr",
        }
    )
    built = comparable(document, english_title=None)
    assert built.english_title is None and built.publisher == "Mehr"
    assert built.key == ("run-1", "https://www.mehrnews.com/news/1", "c" * 64)
    with pytest.raises(OverlapError):
        comparable("doc", english_title=None)  # type: ignore[arg-type]
    with pytest.raises(OverlapError):
        comparable(document, english_title=3)  # type: ignore[arg-type]


def test_a_surrogate_pair_title_classifies_the_same_after_replay() -> None:
    """Round 1, B1: the reviewer's counterexample, pinned."""
    import json

    base = _item("base", "Iran tanker fire", run="b")
    paired = _item("lens", "𝐢ran tanker fire")
    replayed = _item("lens", json.loads(json.dumps(paired.english_title, ensure_ascii=True)))
    assert classify([base], [paired]) == classify([base], [replayed])
    assert classify([base], [paired]) == {paired.key: "republication"}
