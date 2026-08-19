"""Shared strategy and replay helpers for the property suite.

These are the "keep myself honest" property tests, not Codex's acceptance layer
(T-901--T-904). They exist because cross-model review kept finding, one round per
property, things a local fuzzer finds in one run: M1-305's survivor tiebreak took
five rounds on a single function -- a lone-surrogate raise, then ``datetime.fold``,
then astral-vs-surrogate-pair spelling -- and each of those is an invariant, not an
example. The strategies below generate exactly those input classes.

A later review found this docstring had been describing coverage the strategies did
not have: the astral/surrogate-pair "pair" was one literal written twice, and
``TIMESTAMPS`` held only ISO strings, which cannot carry ``fold``. Both are now
generated for real. If you weaken a strategy, weaken the claim in the same commit --
a fuzzer that documents properties it never exercises is worse than no fuzzer, because
it is read as evidence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from hypothesis import strategies as st

from whiskeyjack_bot.research.model import (
    ResearchDocument,
    ResearchRun,
    validate_document,
    validate_run,
)

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
            # An astral scalar and its UTF-16 surrogate-pair spelling. Both have to
            # be spelled out: the second used to be another astral literal, so the
            # pair this file claims to generate was the same string twice and the
            # distinction was never produced (cross-model review, round 1).
            # json.dumps(ensure_ascii=True) escapes both to the same two \\uXXXX
            # units and json.loads recombines the pair, so they persist and replay
            # as one scalar -- which is exactly why the tiebreak keys on the
            # persisted form and must treat them as equal.
            "\ud83d\ude00",
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
_TIMESTAMP_STRINGS = st.sampled_from(
    [
        "2026-07-17T00:00:00+00:00",
        "2026-07-17T00:00:00+01:00",
        "2026-07-16T23:00:00+00:00",  # the same instant as the line above
        "2026-07-18T12:30:00+00:00",
    ]
)

# ...and the same instants as aware datetime *objects*, carrying both values of
# ``datetime.fold``.
#
# This exists because the string strategy above cannot produce fold at all: an ISO
# string has nowhere to put it, so the suite was fuzzing everything except the field
# whose round-3 bug the replay-stability property is named after (cross-model review,
# round 1). fold does survive the schema when the value arrives as an object --
# ``_to_utc`` calls ``astimezone(timezone.utc)``, and CPython returns ``self``
# unchanged when the tzinfo is already that instance, so ``fold=1`` reaches the model
# intact. It is then dropped by ``isoformat``, which is the whole point: two documents
# differing only in fold have identical persisted forms and must key identically, or a
# replay picks a different survivor than the live run did.
_FOLDED_DATETIMES = st.builds(
    lambda spec, fold: datetime(*spec, tzinfo=timezone.utc, fold=fold),
    st.sampled_from([(2026, 7, 17, 0, 0), (2026, 7, 16, 23, 0), (2026, 7, 18, 12, 30)]),
    st.sampled_from([0, 1]),
)

TIMESTAMPS = st.one_of(_TIMESTAMP_STRINGS, _FOLDED_DATETIMES)
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
            # Terminal DNS root dots (M1-310). Present here so the *existing*
            # properties -- idempotence, revalidation, own-error-only, no-leak --
            # cover the spelling the canonicalizer now rewrites, not only the
            # new properties written for it.
            "https://bls.gov./x",
            "https://127.0.0.1./a",
            "https://xn--bcher-kva.de./a",
            "https://bücher.de./a",
            # The same dot in its three non-ASCII spellings (review round 1,
            # finding 1). UTS-46 maps these onto `.`, so they were accepted by the
            # gate, survived an ASCII-only strip, and *became* a terminal dot
            # during IDNA encoding -- after the only code that removed one had run.
            "https://bls.gov。/x",
            "https://bls.gov．/x",
            "https://bls.gov｡/x",
            "https://127.0.0.1。/a",
            "https://bücher.de。/a",
            "https://a..b/x",  # refused: an empty label, not a root dot
            "https://a。。b/x",  # refused for the same reason, in the other spelling
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


# Hosts that are valid with or without a terminal root dot, for M1-310. Deliberately
# mixed: a bare domain, a subdomain, an IPv4 literal (the dot is stripped before the
# IP/domain split, so it has to hold there too) and both spellings of one IDN.
ROOT_DOT_HOSTS = st.sampled_from(
    [
        "bls.gov",
        "data.bls.gov",
        "example.org",
        "127.0.0.1",
        "xn--bcher-kva.de",
        "bücher.de",
    ]
)

# The same pool minus the U-label, whose A-label is already in it: these are hosts
# that must stay *distinct* after canonicalization, and `bücher.de` is not distinct
# from `xn--bcher-kva.de` -- it is the same host, which is the point of folding it.
# `notbls.gov` is the suffix coincidence: dropping the dot must not make it a match.
DISTINCT_HOSTS = st.sampled_from(
    ["bls.gov", "data.bls.gov", "notbls.gov", "example.org", "127.0.0.1", "xn--bcher-kva.de"]
)


# Every spelling of "one terminal DNS root dot, or none". The three non-ASCII
# entries are the separators UTS-46 maps onto `.` (U+3002, U+FF0E, U+FF61); the
# gate accepts all three, so all three are spellings of one host and belong in any
# pool that claims to enumerate them. Generating only "" and "." is what let review
# round 1, finding 1 through: the properties below were already asserting the right
# statements, over an input space that could not express the counterexample.
ROOT_DOT_SUFFIXES = st.sampled_from(["", ".", "。", "．", "｡"])


@st.composite
def host_spellings(draw: st.DrawFn) -> tuple[str, str]:
    """One host, returned as two URLs whose root-dot spelling is drawn independently.

    The independence is the entire property. A pair derived from one string carries
    the same spelling on both sides and so holds on the pre-fix code -- the mistake
    ``test_the_two_spellings_of_a_host_select_each_other`` records having made in
    M1-303's round 5, and the reason three of that item's ten new properties proved
    nothing. The *separator* is drawn independently for the same reason, one round
    later: with both sides restricted to ASCII the pair could never straddle the
    ASCII/Unicode boundary, which is exactly where the surviving defect was.
    """
    host = draw(ROOT_DOT_HOSTS)
    path = draw(st.sampled_from(["/", "/report", "/a/b?q=1"]))
    left = f"{host}{draw(ROOT_DOT_SUFFIXES)}"
    right = f"{host}{draw(ROOT_DOT_SUFFIXES)}"
    return f"https://{left}{path}", f"https://{right}{path}"


@st.composite
def research_documents(
    draw: st.DrawFn, text: st.SearchStrategy[str] = HOSTILE_TEXT
) -> ResearchDocument:
    """A schema-valid ResearchDocument over the hostile input classes above.

    ``text`` is a parameter so a property that is only claimed over storable input
    can pass ``ENCODABLE_TEXT`` instead of filtering. Filtering was the first cut and
    hypothesis rejected it as a failed health check: lone surrogates are common
    enough in ``HOSTILE_TEXT`` that most packets were discarded. Narrowing the
    *generator* keeps the surrogate gap visible as its own test rather than hiding
    it inside an ``assume``.
    """
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
        "title": draw(st.none() | text),
        "snippet": draw(st.none() | text),
        "summary": draw(st.none() | text),
        "publisher": draw(st.none() | text),
        "author": draw(st.none() | text),
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


# --- Research runs (M1-306) -------------------------------------------------
#
# The packet hash covers the run as well as its documents, so the run needs the
# same hostile-input treatment the documents already get: queries and provider
# configs are provider- and caller-supplied text that reaches the same JSON
# rendering, and the counters added by 004 have to keep NULL distinct from 0.

# Deliberately small, so two runs in one packet can collide on the fields the
# packet's own validation is supposed to refuse.
QUESTION_IDS = st.sampled_from([1, 42])

# Non-negative and nullable: None is "unmeasured", 0 is "nothing was discarded",
# and 004 keeps them apart on purpose.
COUNTS = st.none() | st.integers(min_value=0, max_value=5)


# JSON that survives the round trip into a TEXT column. Non-finite floats are
# excluded because the schema refuses them (they render as null), which is the
# invariant `_reject_non_finite` exists to hold.
def provider_config_values(
    text: st.SearchStrategy[str] = HOSTILE_TEXT,
) -> st.SearchStrategy[object]:
    """Nested JSON a provider config may hold, over the given text class."""
    return st.recursive(
        st.none()
        | st.booleans()
        | st.integers(min_value=-1000, max_value=1000)
        | st.floats(allow_nan=False, allow_infinity=False, width=32)
        | text,
        lambda children: (
            st.lists(children, max_size=3) | st.dictionaries(text, children, max_size=3)
        ),
        max_leaves=6,
    )


PROVIDER_CONFIG_VALUES = provider_config_values()


@st.composite
def research_runs(draw: st.DrawFn, text: st.SearchStrategy[str] = HOSTILE_TEXT) -> ResearchRun:
    """A schema-valid ResearchRun over the hostile input classes above.

    ``text`` is a parameter for the reason :func:`research_documents` gives."""
    # Drawn together rather than generated and filtered: the schema requires an
    # agent_model and a posts_dropped_no_url from the agent provider, and a run that
    # cannot be built is not a run worth fuzzing.
    provider = draw(st.sampled_from(["asknews", "exa", "structured", "xai_x_search"]))
    if provider == "xai_x_search":
        agent_model: str | None = draw(st.sampled_from(["grok-3", "grok-4-fast"]))
        posts_dropped: int | None = draw(st.integers(min_value=0, max_value=5))
    else:
        agent_model = draw(st.none() | st.sampled_from(["grok-3"]))
        posts_dropped = draw(COUNTS)

    started = draw(TIMESTAMPS)
    payload: dict[str, Any] = {
        "retrieval_run_id": draw(RUN_IDS),
        "question_id": draw(QUESTION_IDS),
        "provider": provider,
        "provider_config": draw(
            st.none() | st.dictionaries(text, provider_config_values(text), max_size=3)
        ),
        "queries": draw(st.lists(text, max_size=3)),
        "started_at_utc": started,
        # None or the start itself: the schema refuses a completion before the start,
        # and drawing an independent instant would mostly generate refusals.
        "completed_at_utc": draw(st.none() | st.just(started)),
        "freshness_cutoff_utc": draw(st.none() | TIMESTAMPS),
        # Excluded from the packet hash, and generated *because* it is: the property
        # that says so can only discriminate if the value actually varies.
        "raw_response_path": draw(st.none() | st.sampled_from(["research/1/a.json", "x.json"])),
        "error_summary": draw(st.none() | text),
        # -0.0 is drawn explicitly. `st.floats(min_value=0, ...)` never produces it,
        # which is why the suite missed that SQLite maps -0.0 to 0.0 on a REAL column
        # while the model preserved the sign -- an ordinary accepted cost that hashed
        # differently before and after persistence (M1-306 review round 1, finding 1).
        # A bound that starts at zero silently excludes one of zero's two spellings.
        "cost_usd": draw(
            st.none()
            | st.sampled_from([0.0, -0.0])
            | st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False)
        ),
        "agent_model": agent_model,
        "posts_dropped_no_url": posts_dropped,
        "documents_dropped": draw(COUNTS),
        "duplicates_collapsed": draw(COUNTS),
    }
    return validate_run(payload)


def persisted_run(run: ResearchRun) -> str:
    """The run exactly as the ledger stores it. See :func:`persisted`."""
    return json.dumps(
        run.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def round_trip_run(run: ResearchRun) -> ResearchRun:
    """Store and reload a run the way replay does."""
    return validate_run(json.loads(persisted_run(run)))
