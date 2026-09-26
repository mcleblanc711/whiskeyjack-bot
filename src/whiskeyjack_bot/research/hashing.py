"""Content hashing for research documents (M1-301).

``content_sha256`` is the single definition of a document's content identity.
It participates in ``UNIQUE(retrieval_run_id, canonical_url, content_sha256)``
(M1-601) and, through it, in the research-packet hash that replay reproduces.

**Changing the normalization rule below breaks replay**: previously stored
documents keep their old digests, so a re-run over the same evidence would
produce different hashes, defeat the dedup constraint and invalidate the
attribution claim that a replayed forecast saw the same sources. If the rule
must ever change, it changes as a new versioned function alongside this one,
never as an edit to this one.

The primitive lives here rather than in each adapter so that AskNews (M1-302),
Exa (M1-303), the structured router (M1-304) and the X agent (M1-307) cannot
drift into per-provider hashing of the same article.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# The pinned normalization rule. Each step exists to make the digest stable
# across cosmetically different renderings of identical content:
#
# 1. Unicode NFC -- providers disagree on composed vs decomposed accents, so
#    "resumé" and "resumé" must not hash differently.
# 2. Collapse every run of whitespace (including newlines and tabs) to a single
#    space -- reflowed or re-wrapped article text is the same content.
# 3. Strip leading/trailing whitespace.
# 4. Encode UTF-8, then SHA-256.
#
# Deliberately NOT normalized: letter case and punctuation. Both can carry
# meaning in a quoted statement, and an adapter must not be able to collapse two
# genuinely different claims into one document.
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def normalize_content(text: str) -> str:
    """Apply the pinned normalization rule; exposed for tests and diagnostics."""
    return _WHITESPACE_RUN_RE.sub(" ", unicodedata.normalize("NFC", text)).strip()


class ContentHashError(ValueError):
    """``text`` has no UTF-8 form, so it has no content identity (D49).

    The only such text is one carrying a lone surrogate (``\\ud800`` with no pair),
    which ``json.loads`` returns for provider JSON and ``ResearchDocument`` accepts. The
    pinned rule's step 4 cannot encode it. Before D49 the raw ``UnicodeEncodeError``
    escaped, and its message quotes the offending character. The owner chose to
    **reject** the document rather than hash it with ``surrogatepass``: nothing else in
    the pipeline has to carry surrogate-bearing provider text afterwards, and no existing
    digest changes, because such input never had one.

    A ``ValueError`` subclass on purpose. Both adapters already catch ``ValueError`` when
    they build a document and drop it, counted, so the live behaviour is unchanged and
    only the error's hygiene is new. The message is a constant, raised ``from None``.
    """


def content_sha256(text: str) -> str:
    """Return the lowercase hex SHA-256 of ``text`` under the pinned rule.

    Raises :class:`ContentHashError` (D49) for text with no UTF-8 form.
    """
    try:
        encoded = normalize_content(text).encode("utf-8")
    except UnicodeEncodeError:
        raise ContentHashError(
            "document text is not valid Unicode (a lone surrogate); content withheld"
        ) from None
    return hashlib.sha256(encoded).hexdigest()
