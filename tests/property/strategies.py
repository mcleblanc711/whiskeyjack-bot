"""Shared strategy and replay helpers for the property suite.

These are the "keep myself honest" property tests, not Codex's acceptance layer
(T-901--T-904). They exist because cross-model review kept finding, one round per
property, things a local fuzzer finds in one run: M1-305's survivor tiebreak took
five rounds on a single function -- a lone-surrogate raise, then ``datetime.fold``,
then astral-vs-surrogate-pair spelling -- and each of those is an invariant, not an
example. The strategy below generates exactly those input classes.
"""

from __future__ import annotations

import json
from typing import Any

from hypothesis import strategies as st

from whiskeyjack_bot.research import ResearchDocument, validate_document

# Text that has actually broken this code before:
#   - lone surrogates, which arrive intact from provider JSON (json.loads('"\\ud800"'))
#     and make model_dump_json() raise;
#   - astral scalars and their UTF-16 surrogate-pair spelling: distinct Python strings
#     that json.dumps(ensure_ascii=True) -> json.loads collapses into one, which is why
#     the dedup tiebreak keys on the persisted form and not on the object;
#   - NFC/NFD spellings of one grapheme, which the content hash is pinned to collapse;
#   - ordinary and empty text, so the common path stays exercised.
HOSTILE_TEXT = st.one_of(
    st.text(
        st.characters(exclude_categories=["Cc", "Cs"]) | st.characters(categories=["Cs"]),
        max_size=24,
    ),
    st.sampled_from(
        [
            "\ud800",
            "😀",
            "\U0001f600",
            "resumé",
            "resumé",
            "  spaced \n out  ",
            "",
        ]
    ),
)

# The same text minus lone surrogates, for invariants that are only claimed over
# UTF-8-encodable input. Kept as its own strategy rather than folded into a filter, so
# the surrogate gap stays visible as its own test.
ENCODABLE_TEXT = st.one_of(
    st.text(st.characters(exclude_categories=["Cc", "Cs"]), max_size=24),
    st.sampled_from(["😀", "  spaced \n out  ", "", "a\t\nb"]),
)

SURROGATE_TEXT = st.text(st.characters(categories=["Cs"]), min_size=1, max_size=4)

# Small pools, so collisions on the dedup key actually happen instead of every
# generated document being unique.
RUN_IDS = st.sampled_from(["run-1", "run-2"])
URLS = st.sampled_from(
    [
        "https://example.org/a",
        "https://example.org/b",
        "http://example.org:8080/c?q=1",
        "https://[::1]/d",
    ]
)
DIGESTS = st.sampled_from(["a" * 64, "b" * 64, "c" * 64])
TIMESTAMPS = st.sampled_from(
    [
        "2026-07-17T00:00:00+00:00",
        "2026-07-17T00:00:00+01:00",
        "2026-07-16T23:00:00+00:00",  # the same instant as the line above
        "2026-07-18T12:30:00+00:00",
    ]
)
RELIABILITY_TAGS = st.sampled_from(
    ["official_primary", "verified_org", "journalist", "unverified_social"]
)

# URL-shaped inputs for the canonicalizer, valid and not. The invalid ones are the
# cases M1-301's review rounds 2, 4, 5 and 6 turned on.
URL_CANDIDATES = st.one_of(
    URLS,
    st.sampled_from(
        [
            "https://EXAMPLE.org:443/a/b?utm_source=x&q=1#frag",
            "https://user:pass@example.org/a",
            "https://xn--e1afmkfd.xn--p1ai/a",
            "https://نامه‌ای.ir/a",
            "https://example.org/%2f%41",
            "https://example.org",
            "http://example.org:80/",
            "https://[2001:db8::1]:8443/x",
            # Not URLs at all, or URLs this validator refuses:
            "",
            "not a url",
            "/relative/path",
            "ftp://example.org/a",
            "https:///a",
            "https://user@/a",
            "https://:443/a",
            "https://exa mple.org/a",
            "https://example.org/a\tb",
            "https://example.org:99999/a",
            "https://​example.org/a",
            "\ud800",
        ]
    ),
    st.text(max_size=20),
)


@st.composite
def research_documents(draw: st.DrawFn) -> ResearchDocument:
    """A schema-valid ResearchDocument over the hostile input classes above."""
    # Social documents are constrained by the schema (llm_reported plus a tag), so
    # the trio is drawn together rather than generated and filtered.
    source_type = draw(st.sampled_from(["news", "web", "official", "structured", "social"]))
    if source_type == "social":
        provenance = "llm_reported"
        reliability_tag: str | None = draw(RELIABILITY_TAGS)
    else:
        provenance = draw(st.sampled_from(["direct_api", "llm_reported"]))
        reliability_tag = draw(st.none() | RELIABILITY_TAGS)

    payload: dict[str, Any] = {
        "retrieval_run_id": draw(RUN_IDS),
        "original_url": draw(URLS),
        "canonical_url": draw(URLS),
        "retrieved_at_utc": draw(TIMESTAMPS),
        "source_type": source_type,
        "provenance": provenance,
        "content_sha256": draw(DIGESTS),
        "title": draw(st.none() | HOSTILE_TEXT),
        "snippet": draw(st.none() | HOSTILE_TEXT),
        "summary": draw(st.none() | HOSTILE_TEXT),
        "publisher": draw(st.none() | HOSTILE_TEXT),
        "author": draw(st.none() | HOSTILE_TEXT),
        "reliability_tag": reliability_tag,
    }
    return validate_document(payload)


def persisted(document: ResearchDocument) -> str:
    """The document exactly as the ledger stores it.

    Identity for every replay assertion in this package: the ledger holds JSON, so
    two documents are the same evidence if and only if this string matches. The
    in-memory object carries distinctions storage drops (``datetime.fold``,
    surrogate-pair spelling), so comparing objects would assert something stricter
    than replay can preserve -- the M1-305 round-4 lesson.
    """
    return json.dumps(
        document.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def round_trip(document: ResearchDocument) -> ResearchDocument:
    """Store and reload a document the way replay does."""
    return validate_document(json.loads(persisted(document)))
