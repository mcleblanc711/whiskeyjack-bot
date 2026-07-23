"""M1-305: URL canonicalization consolidates the URL policy without regressing
the IDN/IPv6/Cf cases M1-301 fought through, freshness-tagging is deterministic
and flags what it cannot date, and duplicate artifacts collapse without losing
the stronger provenance."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from whiskeyjack_bot.research import (
    CanonicalizationError,
    ResearchDocument,
    assess_document,
    assess_freshness,
    canonicalize_url,
    content_sha256,
    deduplicate,
    freshness_cutoff,
    validate_document,
)

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
    ],
)
def test_canonicalize_rejects_what_the_schema_rejects(url: str) -> None:
    # Agreement direction 2: everything the schema refuses, canonicalize refuses
    # too -- and as CanonicalizationError, so callers handle one type.
    from whiskeyjack_bot.research import ResearchSchemaError

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


def test_identical_artifacts_collapse() -> None:
    body = _hash("payrolls rose")
    a = _document(content_sha256=body)
    b = _document(content_sha256=body)
    result = deduplicate([a, b])
    assert len(result.documents) == 1
    assert result.collapsed_count == 1


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
