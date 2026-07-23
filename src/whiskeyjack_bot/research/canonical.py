"""URL canonicalization and the consolidated URL-validation policy (M1-305).

``canonicalize_url`` derives the ``canonical_url`` that
``UNIQUE(retrieval_run_id, canonical_url, content_sha256)`` (M1-601) keys dedup
on. Adapters (M1-302 AskNews, M1-303 Exa, M1-304 structured router, M1-307 X
agent) call it once per document; until M1-305 they set ``canonical_url`` equal
to ``original_url`` (M1-301 note). ``original_url`` is preserved on the schema,
so canonicalization is never an attribution loss: the as-retrieved URL survives.

**This module owns URL-validation policy beyond bare syntax** (backlog M1-305).
``model._require_http_url`` was kept deliberately minimal after three consecutive
cross-model review regressions came out of extending it (a blanket Unicode-``Cf``
ban broke valid IDN hostnames; the IDNA check that replaced it broke IPv6
literals -- see ``docs/M1-301-NOTES.md``). The lesson banked there governs every
line below: **delegate host classification to the authority** (``ipaddress``
first, ``idna`` only for what is not an IP literal) and **do not speculatively
transform** what a provider sent. So query parameters keep their order and their
bytes, percent-encoding is only case-normalized (never decoded), and the one
lossy step -- dropping a documented tracking-parameter allowlist -- exists solely
because dedup keys on ``canonical_url`` and two reports differing only by a
``utm_*`` tag must collapse.

The gate is *reused*, not re-implemented: ``canonicalize_url`` runs
``model._require_http_url`` before normalizing, so there is one definition of
"is this a URL at all", and a second hand-rolled copy cannot drift from it.

Canonicalization is pure: no network, no wall-clock, string operations only. Its
output is itself a valid ``HttpUrlString`` -- it round-trips the schema's own gate.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import unquote, urlsplit, urlunsplit

import idna

from whiskeyjack_bot.research.model import _require_http_url

# Query keys removed during canonicalization. Analytics/click-tracking tags that
# name the referrer, not the resource: two provider reports of one article differ
# only by these, so keying dedup on a URL that keeps them would never collapse
# them. Everything else is preserved byte-for-byte -- an ordinary query parameter
# can select the resource (``?id=42``), so only this closed, documented set goes.
# Matched case-insensitively against the percent-decoded key.
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "utm_cid",
        "utm_reader",
        "utm_source_platform",
        "gclid",
        "gclsrc",
        "dclid",
        "wbraid",
        "gbraid",
        "fbclid",
        "fb_action_ids",
        "fb_action_types",
        "fb_ref",
        "fb_source",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "igshid",
        "yclid",
        "twclid",
        "ttclid",
        "vero_id",
        "vero_conv",
        "oly_anon_id",
        "oly_enc_id",
        "spm",
    }
)

# Default port per scheme, dropped from the canonical authority.
_DEFAULT_PORT: dict[str, int] = {"http": 80, "https": 443}

# A percent-encoded octet; the hex digits are uppercased (RFC 3986 s6.2.2.1).
# Deliberately only case-normalized, never decoded: decoding an unreserved octet
# is where subtle equivalence bugs live, and this module does not take that risk.
_PERCENT_OCTET_RE = re.compile(r"%[0-9a-fA-F]{2}")


class CanonicalizationError(Exception):
    """A URL could not be canonicalized, with the offending input withheld.

    Same hygiene rule as ``ResearchSchemaError``/``ConfigError``: a URL is row
    content, so the message is a constant that never echoes it. Every raise here
    uses ``from None`` -- ``idna`` and ``urlsplit`` embed the offending value in
    their own exceptions, and a chained ``__cause__`` reprints it in a traceback.
    """


# Mirrors model._BAD_URL: constant, value-free, names no fragment of the input.
_BAD_URL = "not a canonicalizable http(s) URL (offending input withheld)"


def _canonical_host(host: str) -> str:
    """Return the canonical form of a validated host.

    Branched exactly as ``model._require_resolvable_hostname`` branches, for the
    reason recorded there: ``urlsplit().hostname`` returns IP literals as well as
    domain names, and ``idna`` refuses IP literals. So an IP literal answers to
    ``ipaddress`` (compressed, and re-bracketed for IPv6, which is how it must
    reassemble into an authority) and everything else to ``idna`` (IDNA 2008
    A-label, lowercased). ``uts46=True`` folds case/width so a mixed-case IDN host
    canonicalizes; a host that already passed the stricter gate cannot fail here.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass  # Not an IP literal, so it must be a domain name.
    else:
        return f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    try:
        return idna.encode(host, uts46=True).decode("ascii")
    except idna.IDNAError:
        raise CanonicalizationError(_BAD_URL) from None


def _strip_tracking(query: str) -> str:
    """Drop tracking pairs; keep everything else in order, byte-for-byte.

    Splits on the raw ``&`` rather than round-tripping through ``parse_qsl`` /
    ``urlencode`` so a preserved parameter is stored exactly as the provider sent
    it -- re-encoding could alter a value that is part of the resource identity.
    """
    if not query:
        return query
    kept: list[str] = []
    for pair in query.split("&"):
        if not pair:
            continue  # An empty segment ("a&&b") carries nothing; not preserved.
        key = pair.split("=", 1)[0]
        if unquote(key).lower() in _TRACKING_PARAMS:
            continue
        kept.append(pair)
    return "&".join(kept)


def _uppercase_percent_octets(text: str) -> str:
    return _PERCENT_OCTET_RE.sub(lambda m: m.group(0).upper(), text)


def canonicalize_url(url: str) -> str:
    """Return the canonical form of ``url`` for dedup, or raise on non-URLs.

    Reuses ``model._require_http_url`` as the syntactic gate, then normalizes the
    parts that vary cosmetically between reports of one resource: it lowercases
    the scheme, canonicalizes the host (IDN -> A-label, IPv6 compressed), drops
    the default port, drops userinfo and the fragment, uppercases percent-octet
    hex, and strips tracking parameters. Query order and every non-tracking
    parameter are preserved. The result validates as an ``HttpUrlString``.
    """
    try:
        _require_http_url(url)
    except ValueError:
        # from None and a constant message: _require_http_url raises the sanitized
        # model._BAD_URL, but re-raising as this module's own error keeps callers
        # handling a single type, and drops any __cause__ that could reprint input.
        raise CanonicalizationError(_BAD_URL) from None

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    # .hostname is guaranteed present by the gate; lower() is redundant with what
    # urlsplit already does but makes the canonical intent explicit for the reader.
    host = _canonical_host(parts.hostname or "")

    netloc = host
    port = parts.port
    if port is not None and port != _DEFAULT_PORT[scheme]:
        netloc = f"{host}:{port}"

    path = _uppercase_percent_octets(parts.path) or "/"
    query = _uppercase_percent_octets(_strip_tracking(parts.query))

    # Fragment dropped: it is never sent to the server, so it is not part of the
    # resource identity. Userinfo dropped by rebuilding netloc from host alone --
    # it is not resource identity either, and keeping it would write credentials
    # into the stored dedup key (original_url still preserves the as-retrieved URL).
    return urlunsplit((scheme, netloc, path, query, ""))
