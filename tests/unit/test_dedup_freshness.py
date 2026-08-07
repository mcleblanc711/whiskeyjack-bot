"""M1-305: URL canonicalization consolidates the URL policy without regressing
the IDN/IPv6/Cf cases M1-301 fought through, freshness-tagging is deterministic
and flags what it cannot date, and duplicate artifacts collapse without losing
the stronger provenance."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from whiskeyjack_bot.research.canonical import CanonicalizationError, canonicalize_url
from whiskeyjack_bot.research.dedup import deduplicate
from whiskeyjack_bot.research.freshness import (
    assess_document,
    assess_freshness,
    freshness_cutoff,
)
from whiskeyjack_bot.research.hashing import content_sha256
from whiskeyjack_bot.research.model import ResearchDocument, validate_document

TS = "2026-07-17T00:00:00+00:00"
SHA = "a" * 64


def _document(**overrides: object) -> ResearchDocument:
    data: dict[str, object] = {
        "retrieval_run_id": "run-1",
        "original_url": "https://example.org/a",
        "canonical_url": "https://example.org/a",
        "retrieved_at_utc": TS,
        "source_type": "news",
        "provenance": "direct_api",
        "content_sha256": SHA,
    }
    data.update(overrides)
    return validate_document(data)


# --- canonicalization -------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        # The exact case model.py's test_url_validation_does_not_rewrite_the_url
        # preserves verbatim -- canonicalization is where it collapses.
        ("https://example.org:443/a/b?utm_source=x&q=1#frag", "https://example.org/a/b?q=1"),
        # Default ports for both schemes, and an empty path becomes "/".
        ("http://example.org:80/", "http://example.org/"),
        ("https://example.org", "https://example.org/"),
        # Scheme and host lowercase; path case is content and is preserved.
        ("HTTPS://EXAMPLE.ORG/A", "https://example.org/A"),
        # Userinfo is dropped -- not resource identity, and keeps credentials out
        # of the stored dedup key.
        ("https://user:pass@example.org/a", "https://example.org/a"),
        # A non-default port survives.
        ("https://8.8.8.8:8080/a", "https://8.8.8.8:8080/a"),
        # Percent-octet hex is uppercased, never decoded.
        ("https://example.org/%e2%98%83?x=%2f", "https://example.org/%E2%98%83?x=%2F"),
        # Tracking params drop; every other param keeps its place and its bytes.
        (
            "https://example.org/a?a=1&utm_source=x&b=2&fbclid=y&c=3",
            "https://example.org/a?a=1&b=2&c=3",
        ),
        # One terminal DNS root dot goes (M1-310, D32): two spellings of one host
        # were two dedup keys for one page, and never matched for attribution.
        ("https://bls.gov./x", "https://bls.gov/x"),
        ("https://BLS.GOV./x", "https://bls.gov/x"),
        ("https://data.bls.gov./x", "https://data.bls.gov/x"),
        # The dot is removed before the port and the IP/domain split, so it
        # composes with both rather than being a domain-name special case.
        ("https://bls.gov.:443/x", "https://bls.gov/x"),
        ("https://127.0.0.1./a", "https://127.0.0.1/a"),
        ("https://BÜCHER.DE./a", "https://xn--bcher-kva.de/a"),
        # The same dot in the three separators UTS-46 folds onto `.` (review round
        # 1, finding 1). These are the regression pins: an ASCII-only strip ran
        # *before* IDNA created the dot, so each of these produced `bls.gov.` --
        # a non-fixed-point canonical form and a second identity for one page.
        ("https://bls.gov。/x", "https://bls.gov/x"),  # U+3002
        ("https://bls.gov．/x", "https://bls.gov/x"),  # U+FF0E
        ("https://bls.gov｡/x", "https://bls.gov/x"),  # U+FF61
        ("https://BLS.GOV。/x", "https://bls.gov/x"),
        ("https://data.bls.gov。/x", "https://data.bls.gov/x"),
        ("https://bls.gov。:443/x", "https://bls.gov/x"),
        ("https://127.0.0.1。/a", "https://127.0.0.1/a"),
        ("https://BÜCHER.DE。/a", "https://xn--bcher-kva.de/a"),
        # Interior separators fold too -- that is UTS-46's doing, not this
        # module's, and it is why the mapping is delegated rather than re-tabled.
        ("https://bls。gov。/x", "https://bls.gov/x"),
        # IDN host folds to its A-label; IPv6 compresses and re-brackets.
        ("https://MÜNCHEN.DE/a", "https://xn--mnchen-3ya.de/a"),
        ("https://[2001:0db8:0000:0000:0000:0000:0000:0001]:443/a", "https://[2001:db8::1]/a"),
        ("https://[::1]/a", "https://[::1]/a"),
    ],
)
def test_canonicalize_url_normalizes_expected_forms(url: str, expected: str) -> None:
    assert canonicalize_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org:443/a/b?utm_source=x&q=1#frag",
        "https://MÜNCHEN.DE/a",
        "https://نامه‌ای.ir/a",  # Persian ZWNJ
        "https://क्‍ष.com/a",  # Devanagari ZWJ
        "https://[2001:db8::1]:443/a",
        "https://8.8.8.8:8080/a",
        "http://example.org:80/",
        # Both spellings of one host: the dotted one must also reach a fixed point,
        # or the canonical form would depend on how many times it was canonicalized.
        "https://bls.gov./x",
        "https://bls.gov/x",
        "https://127.0.0.1./a",
        # Every separator spelling, because this is the property that broke: the
        # Unicode ones canonicalized to `https://bls.gov./x`, which canonicalized
        # again to something else. One canonicalization must settle it.
        "https://bls.gov。/x",
        "https://bls.gov．/x",
        "https://bls.gov｡/x",
        "https://127.0.0.1。/a",
    ],
)
def test_canonical_output_revalidates_and_is_idempotent(url: str) -> None:
    once = canonicalize_url(url)
    # The output is itself a valid canonical_url: it round-trips the schema gate.
    assert validate_document(_document(canonical_url=once)).canonical_url == once
    # Canonicalizing an already-canonical URL is a fixed point.
    assert canonicalize_url(once) == once


@pytest.mark.parametrize(
    "url",
    [
        # Standards-valid international hostnames model.py accepts: canonicalize
        # must accept them too (policy consolidation, not a second, stricter gate).
        "https://نامه‌ای.ir/a",
        "https://क्‍ष.com/a",
        "https://münchen.de/a",
        "https://例え.jp/a",
        "https://[fe80::1]/a",
        "https://192.168.1.1/a",
    ],
)
def test_canonicalize_accepts_what_the_schema_accepts(url: str) -> None:
    # Agreement direction 1: everything validate_document accepts, canonicalize
    # accepts. (Both are exercised on the same fixtures the M1-301 rounds added.)
    validate_document(_document(original_url=url))
    canonicalize_url(url)


@pytest.mark.parametrize(
    "url",
    [
        # Cf that IDNA refuses everywhere; ZWNJ out of context; a space; a
        # bracketed non-address; and inputs that are not http(s) URLs at all.
        "https://exa​mple.org/a",  # zero-width space
        "https://ex‮ample.org/a",  # right-to-left override
        "https://ab‌cd.com/a",  # ZWNJ in a disallowed context
        "https://exam ple.org/a",  # raw space
        "https://[gg::1]/a",  # bracketed but not an address
        "https://[::1]:99999/a",  # out-of-range port
        "ftp://example.org/a",  # scheme we never retrieve over
        "not a url",
        "/relative/path",
        # M1-310 removes *one* terminal root dot and nothing else: an empty label
        # is still refused, by the shared gate, before canonicalization can run.
        # These are why the strip is one slice and not a loop.
        "https://bls.gov../x",
        "https://.bls.gov/x",
        "https://./x",
        # Same, in and across the folded separators. These are what makes "exactly
        # one, never a loop" a statement about every spelling rather than only the
        # ASCII one: a double separator is an empty label whichever way it is
        # written, so the strip never has a second dot left to consider.
        # Unlike the cases added above, these **pass both pre- and post-fix** --
        # the shared gate refuses them either way. Kept as the bound on the widened
        # fold: it must not start accepting an empty label in a new spelling.
        "https://bls.gov。。/x",
        "https://bls.gov。./x",
        "https://bls.gov.。/x",
        "https://。bls.gov/x",
        "https://。/x",
    ],
)
def test_canonicalize_rejects_what_the_schema_rejects(url: str) -> None:
    # Agreement direction 2: everything the schema refuses, canonicalize refuses
    # too -- and as CanonicalizationError, so callers handle one type.
    from whiskeyjack_bot.research.model import ResearchSchemaError

    with pytest.raises(ResearchSchemaError):
        validate_document(_document(original_url=url))
    with pytest.raises(CanonicalizationError):
        canonicalize_url(url)


@pytest.mark.parametrize(
    "url, expected",
    [
        # Tracking-key removal is the only query transform: empty segments and
        # leading/trailing separators survive, because a query-signing or
        # -dispatching endpoint can distinguish them.
        ("https://example.org/a?x=1&&y=2", "https://example.org/a?x=1&&y=2"),
        ("https://example.org/a?a=1&", "https://example.org/a?a=1&"),
        ("https://example.org/a?&a=1", "https://example.org/a?&a=1"),
        # Tracking is still stripped; the surrounding structure is left intact.
        ("https://example.org/a?utm_source=x&a=1", "https://example.org/a?a=1"),
    ],
)
def test_empty_query_segments_are_preserved(url: str, expected: str) -> None:
    assert canonicalize_url(url) == expected


def test_canonicalization_error_never_echoes_the_url() -> None:
    secret = "hunter2-do-not-print"
    try:
        canonicalize_url(f"https://exa mple.org/{secret}")
    except CanonicalizationError as exc:
        assert secret not in str(exc)
        # from None: no __cause__ to reprint the input through a traceback.
        assert exc.__cause__ is None
    else:  # pragma: no cover - the call must raise
        pytest.fail("expected CanonicalizationError")


# --- freshness --------------------------------------------------------------

CUTOFF = datetime(2026, 7, 1, tzinfo=timezone.utc)
BEFORE = datetime(2026, 6, 1, tzinfo=timezone.utc)
AFTER = datetime(2026, 7, 15, tzinfo=timezone.utc)


def test_freshness_cutoff_is_pure_subtraction() -> None:
    reference = datetime(2026, 7, 31, tzinfo=timezone.utc)
    assert freshness_cutoff(reference, 30) == datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_document_after_cutoff_is_fresh() -> None:
    verdict = assess_freshness(published_at=AFTER, updated_at=None, cutoff=CUTOFF)
    assert verdict.state == "fresh"
    assert verdict.reason == "within_window"
    assert verdict.effective_date == AFTER


def test_document_before_cutoff_is_stale() -> None:
    verdict = assess_freshness(published_at=BEFORE, updated_at=None, cutoff=CUTOFF)
    assert verdict.state == "stale"
    assert verdict.reason == "before_cutoff"


def test_boundary_instant_is_fresh() -> None:
    # The window is "on or after" the cutoff: exactly at the cutoff is fresh.
    verdict = assess_freshness(published_at=CUTOFF, updated_at=None, cutoff=CUTOFF)
    assert verdict.state == "fresh"


def test_updated_at_overrides_published_at_in_both_directions() -> None:
    # A stale publish date rescued by a recent update.
    assert assess_freshness(published_at=BEFORE, updated_at=AFTER, cutoff=CUTOFF).state == "fresh"
    # And a recent publish date superseded by an older update: updated_at is the
    # effective date whenever it is present, not merely when it helps.
    assert assess_freshness(published_at=AFTER, updated_at=BEFORE, cutoff=CUTOFF).state == "stale"


def test_undated_document_is_stale_and_undatable() -> None:
    verdict = assess_freshness(published_at=None, updated_at=None, cutoff=CUTOFF)
    assert verdict.state == "stale"
    assert verdict.reason == "undatable"
    assert verdict.effective_date is None


def test_assess_is_deterministic() -> None:
    a = assess_freshness(published_at=AFTER, updated_at=None, cutoff=CUTOFF)
    b = assess_freshness(published_at=AFTER, updated_at=None, cutoff=CUTOFF)
    assert a == b


def test_assess_document_reads_the_schema_fields() -> None:
    doc = _document(published_at_utc=BEFORE.isoformat())
    assert assess_document(doc, CUTOFF).state == "stale"


# --- deduplication ----------------------------------------------------------


def _hash(text: str) -> str:
    return content_sha256(text)


def _replayed(doc: ResearchDocument) -> ResearchDocument:
    # Cross a REAL json.dumps -> json.loads text boundary, exactly as the ledger
    # stores and a replay reconstructs -- not a bare model_dump(mode="json") handoff,
    # which skips JSON encoding and so misses the normalization the boundary imposes.
    text = json.dumps(doc.model_dump(mode="json"), ensure_ascii=True)
    return validate_document(json.loads(text))


def test_identical_artifacts_collapse() -> None:
    body = _hash("payrolls rose")
    a = _document(content_sha256=body)
    b = _document(content_sha256=body)
    result = deduplicate([a, b])
    assert len(result.documents) == 1
    assert result.collapsed_count == 1


@pytest.mark.parametrize("separator", [".", "。", "．", "｡"])
def test_the_two_spellings_of_one_host_collapse_to_one_document(separator: str) -> None:
    """M1-310's acceptance criterion, at the level it is written about: dedup.

    Two reports of one page, one of them carrying the terminal DNS root dot. Under
    the pre-M1-310 canonical form these were two keys and both rows survived --
    a duplicate ledger row for one artifact inside one run.

    Parametrized over the separators UTS-46 folds (review round 1, finding 1): the
    ASCII case passed while the other three still produced two survivors, which is
    the whole shape of that defect -- a rule that was right about one spelling of
    the thing it was written about.
    """
    body = _hash("payrolls rose")
    dotted_url = f"https://bls.gov{separator}/report"
    dotted = _document(
        original_url=dotted_url,
        canonical_url=canonicalize_url(dotted_url),
        content_sha256=body,
    )
    plain = _document(
        original_url="https://bls.gov/report",
        canonical_url=canonicalize_url("https://bls.gov/report"),
        content_sha256=body,
    )
    result = deduplicate([dotted, plain])
    assert len(result.documents) == 1
    assert result.collapsed_count == 1
    # The survivor is one of the two documents exactly as retrieved, carrying its
    # own original_url next to the shared canonical form: canonicalization decides
    # identity, never attribution, so original_url is never rewritten to what
    # canonicalization produced.
    #
    # *Which* of the two survives is `_sort_key`'s business, not this rule's, and
    # it is genuinely separator-dependent -- the tiebreak orders the persisted
    # JSON, where `ensure_ascii=True` renders U+3002 as a backslash-u escape,
    # so `/` (U+002F) sorts ahead of that backslash (U+005C) while a bare
    # `.` (U+002E) sorts ahead of `/`. Pinning the dotted document as the winner
    # would assert that unrelated order here, and would be wrong for three of
    # these four separators.
    survivor = result.documents[0]
    assert (survivor.original_url, survivor.canonical_url) in {
        (dotted_url, "https://bls.gov/report"),
        ("https://bls.gov/report", "https://bls.gov/report"),
    }


def test_two_spellings_serving_different_content_are_still_two_documents() -> None:
    """The bound on the decision above, and the answer to its main objection.

    ``bls.gov.`` and ``bls.gov`` differ on the wire (Host header, SNI, cookie
    scope), so a virtual-hosting stack *can* serve different pages at them. That
    does not produce a wrong collapse: the key carries ``content_sha256``, so two
    different bodies remain two rows however the host was spelled.
    """
    dotted = _document(
        original_url="https://bls.gov./report",
        canonical_url=canonicalize_url("https://bls.gov./report"),
        content_sha256=_hash("the dotted vhost's page"),
    )
    plain = _document(
        original_url="https://bls.gov/report",
        canonical_url=canonicalize_url("https://bls.gov/report"),
        content_sha256=_hash("a different page"),
    )
    result = deduplicate([dotted, plain])
    assert result.collapsed_count == 0
    assert len(result.documents) == 2


def test_distinct_artifacts_are_not_collapsed_and_keep_order() -> None:
    first = _document(canonical_url="https://example.org/1", content_sha256=_hash("one"))
    second = _document(canonical_url="https://example.org/2", content_sha256=_hash("two"))
    result = deduplicate([first, second])
    assert result.collapsed_count == 0
    assert [d.canonical_url for d in result.documents] == [
        "https://example.org/1",
        "https://example.org/2",
    ]


@pytest.mark.parametrize("reported_first", [True, False])
def test_collapse_keeps_the_stronger_provenance(reported_first: bool) -> None:
    body = _hash("same article, two providers")
    fetched = _document(provenance="direct_api", content_sha256=body)
    reported = _document(
        source_type="social",
        provenance="llm_reported",
        reliability_tag="unverified_social",
        content_sha256=body,
    )
    order = [reported, fetched] if reported_first else [fetched, reported]
    result = deduplicate(order)
    assert len(result.documents) == 1
    # Regardless of arrival order, the survivor is the verified retrieval: a
    # reported claim never silently displaces a fetched one.
    assert result.documents[0].provenance == "direct_api"


def test_equal_provenance_ties_break_to_earliest_retrieval() -> None:
    body = _hash("one artifact, two fetches")
    later = _document(retrieved_at_utc="2026-07-17T12:00:00+00:00", content_sha256=body)
    earlier = _document(retrieved_at_utc="2026-07-17T06:00:00+00:00", content_sha256=body)
    result = deduplicate([later, earlier])
    assert len(result.documents) == 1
    assert result.documents[0].retrieved_at_utc == datetime(2026, 7, 17, 6, tzinfo=timezone.utc)


def test_same_artifact_from_different_runs_is_not_collapsed() -> None:
    # The key is (retrieval_run_id, canonical_url, content_sha256), exactly the
    # ledger's UNIQUE: two providers (two runs) that both surface one article are
    # two legitimate rows, and collapsing them would erase which run found it.
    body = _hash("one article, two providers")
    from_asknews = _document(retrieval_run_id="run-asknews", content_sha256=body)
    from_exa = _document(retrieval_run_id="run-exa", content_sha256=body)
    result = deduplicate([from_asknews, from_exa])
    assert result.collapsed_count == 0
    assert {d.retrieval_run_id for d in result.documents} == {"run-asknews", "run-exa"}


def test_exact_tie_survivor_is_order_independent() -> None:
    # Same key, same provenance, same retrieved_at, differing only in a non-key
    # field: the survivor must not depend on input order (the full-serialization
    # tiebreak makes the selection a min over a total order).
    body = _hash("one artifact, two records")
    a = _document(title="Headline A", content_sha256=body)
    b = _document(title="Headline B", content_sha256=body)
    forward = deduplicate([a, b])
    backward = deduplicate([b, a])
    assert len(forward.documents) == 1
    assert forward.documents[0].title == backward.documents[0].title


def test_dedup_survivor_is_replay_stable() -> None:
    # Equal UTC instants that differ only in datetime.fold compare equal, so the
    # tiebreak decides. fold is carried in memory but dropped by JSON/isoformat,
    # so a tiebreak keyed on the in-memory form would pick a different survivor
    # before vs after a store->replay round-trip. The canonical-JSON key must not.
    body = _hash("one artifact, two records with different fold")
    fold0 = datetime(2026, 7, 17, tzinfo=timezone.utc, fold=0)
    fold1 = datetime(2026, 7, 17, tzinfo=timezone.utc, fold=1)
    a = _document(retrieved_at_utc=fold0, snippet="A", content_sha256=body)
    b = _document(retrieved_at_utc=fold1, snippet="B", content_sha256=body)

    def survivor(docs: list[ResearchDocument]) -> str | None:
        return deduplicate(docs).documents[0].snippet

    before = survivor([a, b])
    after = survivor([_replayed(a), _replayed(b)])
    assert before == after
    # And it stays order-independent both before and after persistence.
    assert survivor([b, a]) == before
    assert survivor([_replayed(b), _replayed(a)]) == after


def test_dedup_tiebreak_is_surrogate_safe() -> None:
    # A text field may hold an unpaired surrogate (schema-valid; e.g. from provider
    # JSON). The tiebreak must not raise on it -- model_dump_json() would, and would
    # leak the character -- and must still be order-independent.
    body = _hash("surrogate in the title")
    a = _document(title="\ud800", content_sha256=body)
    b = _document(title="\ud801", content_sha256=body)
    forward = deduplicate([a, b])
    backward = deduplicate([b, a])
    assert len(forward.documents) == 1
    assert forward.documents[0].title == backward.documents[0].title


def test_json_equivalent_titles_collapse_and_persist_identically() -> None:
    # An astral scalar and its UTF-16 surrogate-pair spelling are distinct Python
    # strings but the same persisted document: json.loads recombines the pair, so
    # both round-trip to the one scalar. The tiebreak keys on the persisted form, so
    # they collide -- correctly -- and whichever the collapse returns, its persisted
    # form is the same in either input order (replay-stable). Injectivity over
    # in-memory identity is deliberately not a goal; matching persisted identity is.
    body = _hash("astral scalar vs its surrogate-pair spelling")
    astral = _document(title=chr(0x1F600), content_sha256=body)
    pair = _document(title=chr(0xD83D) + chr(0xDE00), content_sha256=body)
    assert astral.title != pair.title  # distinct in memory
    assert len(deduplicate([astral, pair]).documents) == 1  # collide by design

    forward = deduplicate([astral, pair]).documents[0]
    backward = deduplicate([pair, astral]).documents[0]
    assert _replayed(forward).title == _replayed(backward).title == chr(0x1F600)
