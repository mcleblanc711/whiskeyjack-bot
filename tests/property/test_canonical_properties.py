"""Invariants of URL canonicalization and content hashing (M1-301, M1-305).

Both are pure functions on arbitrary provider-supplied strings, and both sit on the
dedup key, so a raise or an instability here is a failed retrieval or a broken replay
rather than a cosmetic bug.
"""

from __future__ import annotations

import re
import unicodedata

import pytest
from hypothesis import given
from strategies import ENCODABLE_TEXT, SURROGATE_TEXT, URL_CANDIDATES

from whiskeyjack_bot.research.canonical import _BAD_URL, CanonicalizationError, canonicalize_url
from whiskeyjack_bot.research.hashing import content_sha256, normalize_content
from whiskeyjack_bot.research.model import validate_document

HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


def _canonicalize(url: str) -> str | None:
    """Canonicalize, or None if the input is not a URL. Any other exception escaping
    this helper is the finding: callers only handle CanonicalizationError."""
    try:
        return canonicalize_url(url)
    except CanonicalizationError:
        return None


@given(URL_CANDIDATES)
def test_canonicalize_raises_only_its_own_error(url: str) -> None:
    """M1-301 round 2: urlsplit's own ValueError escaped this path once, and that
    exception embeds the offending netloc -- a raw exception type is both a caller
    contract break and a leak."""
    _canonicalize(url)


@given(URL_CANDIDATES)
def test_canonicalize_is_idempotent(url: str) -> None:
    """The canonical form is a fixed point. Without this, the dedup key depends on how
    many times a URL happened to pass through the canonicalizer."""
    once = _canonicalize(url)
    if once is None:
        return
    assert _canonicalize(once) == once


@given(URL_CANDIDATES)
def test_canonical_output_is_a_storable_url(url: str) -> None:
    """The docstring claims the result validates as an HttpUrlString. If it did not,
    canonicalization would produce documents the schema then rejects."""
    canonical = _canonicalize(url)
    if canonical is None:
        return
    document = validate_document(
        {
            "retrieval_run_id": "run-1",
            "original_url": canonical,
            "canonical_url": canonical,
            "retrieved_at_utc": "2026-07-17T00:00:00+00:00",
            "source_type": "news",
            "provenance": "direct_api",
            "content_sha256": "a" * 64,
        }
    )
    assert document.canonical_url == canonical


@given(URL_CANDIDATES)
def test_rejection_never_echoes_the_input(url: str) -> None:
    """A URL is row content, and a diagnostic naming what was wrong with it is a
    channel for echoing it. The message is one constant string, always.

    Asserted as equality with that constant, not as "no substring of the input appears
    in the message": a message that is input-independent by construction cannot leak,
    and the substring form only produces false alarms when generated text happens to
    overlap the constant ('not a url' shares 'not ' with it)."""
    try:
        canonicalize_url(url)
    except CanonicalizationError as error:
        assert str(error) == _BAD_URL


@given(ENCODABLE_TEXT)
def test_content_hash_is_hex64_and_stable(text: str) -> None:
    digest = content_sha256(text)
    assert HEX64.match(digest)
    assert digest == content_sha256(text)


@given(ENCODABLE_TEXT)
def test_normalization_is_idempotent(text: str) -> None:
    """The digest is pinned to the normalized form, so normalization has to be a fixed
    point or re-hashing stored content would drift."""
    once = normalize_content(text)
    assert normalize_content(once) == once


@given(ENCODABLE_TEXT)
def test_hash_collapses_unicode_spelling_and_whitespace_runs(text: str) -> None:
    """The two collapses the rule promises: NFC/NFD spellings of one grapheme, and any
    run of whitespace. Providers disagree on both for identical article text."""
    assert content_sha256(text) == content_sha256(unicodedata.normalize("NFD", text))
    assert content_sha256(text) == content_sha256(text.replace(" ", "  \t "))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OPEN DEFECT, found by this suite: content_sha256 raises a raw "
        "UnicodeEncodeError on a lone surrogate, and that exception's message quotes "
        "the offending character. Lone surrogates are reachable -- json.loads('\"\\\\ud800\"') "
        "returns one and ResearchDocument accepts it in title/snippet/summary -- so an "
        "adapter hashing provider text can crash with an unsanitized error. Needs an "
        "owner decision (reject the document via ResearchError, or encode with "
        "surrogatepass); not fixed on the workflow-hardening branch that found it."
    ),
)
@given(SURROGATE_TEXT)
def test_content_hash_handles_every_string_the_schema_accepts(text: str) -> None:
    assert HEX64.match(content_sha256(text))
