"""Properties of the evidence gates (M1-327).

Five functions here are pure and total over their schema-valid inputs -- ``source_domains``,
``contemporary``, ``relevant``, ``usable``, ``quality_problem`` -- and M1-327 adds a sixth,
``missing_source_domains``. CLAUDE.md's fuzz rule applies to all of them, and M1-326's round-1
reviewer already filed their absence as a backlog candidate.

**The property that matters here is satisfiability.** M1-327 exists because the named-source
rule was close to unsatisfiable by construction: it asked a resolution *authority* to appear
in a *news* retrieval, and two live questions (Cup 45452's ``results.cik.bg``, rehearsal
43330's ``forbes.com``) show the two ways that fails. A test that asserts "a packet from the
named source passes" is worth nothing if the strategy cannot build such a packet -- it would
pass on code that never matches anything, which is precisely the code under repair. So
``test_a_document_from_the_named_source_closes_the_gap`` asserts ``usable`` on its own
document before asserting anything about the gap: the vacuity guard sits in the *test*, where
a strategy change cannot quietly remove it (M1-326's notes; the M1-405 registry-dispatch
form of the same trap).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from strategies import ENCODABLE_TEXT, ROOT_DOT_SUFFIXES

from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion
from whiskeyjack_bot.research.canonical import CanonicalizationError, _canonical_host
from whiskeyjack_bot.research.model import ResearchDocument, validate_document, validate_run
from whiskeyjack_bot.research.packet import ResearchPacket
from whiskeyjack_bot.research.quality import (
    comparable_host,
    contemporary,
    host,
    missing_source_domains,
    quality_problem,
    relevant,
    source_domains,
    usable,
)

MARKER = "Qz7leakcanaryZq"
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
DAYS = 14
QUESTION_ID = 45452
RUN_ID = "run-1"

# Titles whose terms `relevant` can actually match: each carries at least one token of
# three or more alphanumerics that is not in `_STOP`, which is what makes `terms`
# non-empty. Asserted rather than assumed, in `_relevance_is_reachable` below.
TITLES = st.sampled_from(
    [
        "Will the example agency publish the July release?",
        "Bulgarian parliamentary election seat count",
        "Forbes billionaire ranking 2026",
    ]
)

# Hosts a question can name, and a document can carry. Mixed on purpose: a bare domain, a
# subdomain, an IPv4 literal and a government portal of the shape 45452 actually names.
HOSTS = st.sampled_from(
    ["results.cik.bg", "forbes.com", "example.org", "data.bls.gov", "127.0.0.1"]
)
WWW = st.sampled_from(["", "www."])

# Resolution-criteria text, including the class that used to make `source_domains` raise.
# `https://[abc` is `ValueError: Invalid IPv6 URL` out of `urlsplit`, and question text
# arrives from the Metaculus API, so this pool is the reachable input and not a hypothetical.
BROKEN_URLS = st.sampled_from(
    [
        "https://[abc",
        "https://[",
        "http://[::1",
        "https://[fe80::1%25eth0]bad",
        "http://[::1]/x",
        "https://example.org/a",
        "https://web.archive.org/web/20240112150444/https://www.forbes.com/profile/x/",
        "https://results.cik.bg/2026/",
        "not a url at all",
        "",
        # Hosts `urlsplit` reads out happily and canonicalization then refuses. These are
        # the second raising class, one layer below the first: `host` returns a string, so
        # the `except ValueError` above is no defence, and `comparable_host` is what raises.
        # See UNCANONICALIZABLE_HOSTS below.
        "https://a..b/x",
        "https://-bad.example/y",
        "https://abc-.com/z",
    ]
)

# Hosts that `urlsplit` yields as an ordinary string but `canonical._canonical_host`
# refuses: an empty label, a leading hyphen, a trailing hyphen, an over-long label, a
# bare root dot and a lone surrogate. Reachable -- this is question text from the
# Metaculus API, and `source_domains` scans it before retrieval -- which is what makes
# `host_identity`'s totality a property and not a nicety. Asserted to actually be
# refused in `test_an_uncanonicalizable_named_host_still_yields_a_verdict`.
UNCANONICALIZABLE_HOSTS = st.sampled_from(
    ["a..b", "-bad.example", "abc-.com", "x" * 70 + ".com", ".", "\ud800.com"]
)
CRITERIA = st.one_of(
    ENCODABLE_TEXT,
    st.builds(lambda a, u, b: f"{a} {u} {b}", ENCODABLE_TEXT, BROKEN_URLS, ENCODABLE_TEXT),
    st.lists(BROKEN_URLS, max_size=3).map(" ".join),
)


def _question(**overrides: Any) -> CanonicalBinaryQuestion:
    payload: dict[str, Any] = {
        "question_id": QUESTION_ID,
        "post_id": QUESTION_ID,
        "title": "Will the example agency publish the July release?",
        "resolution_criteria": None,
        "fine_print": None,
    }
    payload.update(overrides)
    return CanonicalBinaryQuestion(**payload)


def _document(
    url: str, *, text: str, published: datetime | None = NOW - timedelta(days=1)
) -> ResearchDocument:
    return validate_document(
        {
            "retrieval_run_id": RUN_ID,
            "original_url": url,
            "canonical_url": url,
            "retrieved_at_utc": NOW - timedelta(hours=1),
            "published_at_utc": published,
            "source_type": "news",
            "provenance": "direct_api",
            "content_sha256": "a" * 64,
            "title": text,
            "snippet": text,
            "summary": None,
        }
    )


def _packet(documents: tuple[ResearchDocument, ...]) -> ResearchPacket:
    run = validate_run(
        {
            "retrieval_run_id": RUN_ID,
            "question_id": QUESTION_ID,
            "provider": "asknews",
            "queries": ["q"],
            "started_at_utc": NOW - timedelta(hours=1),
        }
    )
    return ResearchPacket(question_id=QUESTION_ID, runs=(run,), documents=documents)


def _persisted(document: ResearchDocument) -> str:
    return json.dumps(
        document.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def _round_trip(document: ResearchDocument) -> ResearchDocument:
    return validate_document(json.loads(_persisted(document)))


@given(criteria=CRITERIA, fine_print=st.none() | CRITERIA, title=TITLES, url=BROKEN_URLS)
def test_no_gate_raises_for_a_schema_valid_question(
    criteria: str, fine_print: str | None, title: str, url: str
) -> None:
    """Totality. Every one of these is a pure verdict function with no error type of its
    own, so "only the module's own error escapes" reads here as "nothing escapes".

    ``source_domains`` did not hold this before M1-327: ``urlsplit("https://[abc")`` raises
    ``ValueError``, question text is untrusted, and ``research/orchestrate.py`` calls
    ``source_domains`` *before* retrieval -- so one such string in one question's resolution
    criteria aborted the retrieval and not merely the gate. Removing the ``except ValueError``
    in ``quality.host`` fails this test.
    """
    question = _question(title=title, resolution_criteria=criteria, fine_print=fine_print)
    packet = _packet((_document("https://example.org/a", text=title),))

    domains = source_domains(question)
    assert all(isinstance(d, str) and d for d in domains)
    assert host(url) is None or isinstance(host(url), str)
    for document in packet.documents:
        assert isinstance(contemporary(document, NOW, DAYS), bool)
        assert isinstance(relevant(document, question), bool)
        assert isinstance(usable(document, question, NOW, DAYS), bool)
    quality_problem(packet, question, NOW, DAYS)
    assert isinstance(missing_source_domains(packet, question, NOW, DAYS), tuple)


@given(
    host_name=HOSTS,
    named_www=WWW,
    carried_www=WWW,
    named_dot=ROOT_DOT_SUFFIXES,
    title=TITLES,
    path=st.sampled_from(["/", "/2026/", "/a/b?q=1"]),
)
@settings(max_examples=200)
def test_a_document_from_the_named_source_closes_the_gap(
    host_name: str,
    named_www: str,
    carried_www: str,
    named_dot: str,
    title: str,
    path: str,
) -> None:
    """**The gate is satisfiable.** The property M1-327 is named after.

    The ``www.`` prefix is drawn independently on each side, following the lesson
    ``strategies.host_spellings`` records: a pair derived from one string carries the same
    spelling on both sides and so holds on code that does no normalization at all, which is
    how three of M1-303's ten new properties proved nothing.

    The two assertions before the claim are the vacuity guard, and they belong here rather
    than in the strategy. A packet whose document is not ``usable`` never reaches the branch
    this test is about, so without them a strategy that stopped producing usable documents
    would leave a green test asserting nothing -- the exact shape of the recurring defect in
    ``docs/LESSONS.md``.
    """
    named = f"https://{named_www}{host_name}{named_dot}{path}"
    carried = f"https://{carried_www}{host_name}/story"
    question = _question(
        title=title, resolution_criteria=f"Resolves to the figure published at {named} ."
    )
    document = _document(carried, text=f"{title} -- the agency published its figures.")
    packet = _packet((document,))

    assert source_domains(question), "the question must actually name a source"
    assert usable(document, question, NOW, DAYS), "the document must reach the branch"

    assert missing_source_domains(packet, question, NOW, DAYS) == ()
    assert quality_problem(packet, question, NOW, DAYS) is None


@given(host_name=HOSTS, other=HOSTS, named_www=WWW, title=TITLES)
def test_a_document_from_elsewhere_leaves_the_gap_and_still_forecasts(
    host_name: str, other: str, named_www: str, title: str
) -> None:
    """The behaviour change itself: a gap is recorded, never refused.

    Restoring ``quality_problem``'s third branch fails the second assertion, which is what
    makes this the pre-fix proof at the unit level.
    """
    question = _question(
        title=title,
        resolution_criteria=f"Resolves per https://{named_www}{host_name}/2026/ .",
    )
    document = _document(f"https://{other}/story", text=f"{title} -- reported today.")
    packet = _packet((document,))
    assert usable(document, question, NOW, DAYS), "the document must reach the branch"

    gap = missing_source_domains(packet, question, NOW, DAYS)
    assert gap == () if comparable_host(other) == comparable_host(host_name) else gap != ()
    assert quality_problem(packet, question, NOW, DAYS) is None, (
        "a missing resolution source is recorded, not refused (M1-327)"
    )


@given(
    criteria=CRITERIA,
    title=TITLES,
    url=st.sampled_from(["https://example.org/a", "https://forbes.com/x"]),
    shape=st.sampled_from(["passing", "empty", "stale", "irrelevant"]),
)
def test_the_verdict_replays_across_the_persisted_form(
    criteria: str, title: str, url: str, shape: str
) -> None:
    """Replay stability. The ledger holds JSON, so a verdict derived from a stored packet
    must equal the verdict derived from the live one, or M1-326's gate declines a question
    on an answer the replay would not reproduce.

    ``shape`` is round 1's non-blocking finding, and it was right. Every packet this
    property built was contemporary and relevant -- the document's text *was* the question's
    title -- so ``quality_problem`` returned ``None`` on all six inputs and the assertion
    below compared ``None == None`` forever. A replay-stability claim that never replays a
    **refusal** says nothing about the two codes M1-326's gate actually stores, which is the
    whole reason the property exists. The four shapes reach every branch: ``None``,
    ``stale_evidence``, and ``no_evidence`` by both routes (nothing retrieved, and retrieved
    but irrelevant).

    ``EXPECTED`` is the vacuity guard, and it is asserted *before* the round-trip. Without it
    a future change to ``_document``'s defaults could quietly collapse the shapes back to one
    verdict and leave this test green while proving nothing again -- the exact way it failed
    the first time.
    """
    question = _question(title=title, resolution_criteria=criteria)
    if shape == "empty":
        live = ()
    elif shape == "stale":
        live = (_document(url, text=title, published=NOW - timedelta(days=DAYS * 40)),)
    elif shape == "irrelevant":
        live = (_document(url, text="wholly unrelated filler prose"),)
    else:
        live = (_document(url, text=title),)
    stored = tuple(_round_trip(document) for document in live)
    assert [_persisted(d) for d in stored] == [_persisted(d) for d in live]

    expected = {
        "passing": None,
        "empty": "no_evidence",
        "stale": "stale_evidence",
        "irrelevant": "no_evidence",
    }[shape]
    first = quality_problem(_packet(live), question, NOW, DAYS)
    assert (first.code if first else None) == expected, (
        f"the {shape!r} packet must reach the {expected!r} branch, or this property "
        "compares one verdict against itself"
    )

    second = quality_problem(_packet(stored), question, NOW, DAYS)
    assert first == second
    assert missing_source_domains(_packet(live), question, NOW, DAYS) == missing_source_domains(
        _packet(stored), question, NOW, DAYS
    )


@given(where=st.sampled_from(["title", "resolution_criteria", "snippet"]), title=TITLES)
def test_no_verdict_message_echoes_question_or_document_content(where: str, title: str) -> None:
    """No value leak. ``QualityProblem.message`` is rendered by the caller and reaches
    ``QuestionOutcome.problems``, so it carries no stored, file or field value.

    ``missing_source_domains`` deliberately *does* return hostnames -- it is the ledger's
    ``evidence_gap`` payload, not a message -- so the claim here is scoped to the message
    path, and the returned domains are asserted to be exactly what the question named
    rather than being asserted absent."""
    marked = f"{title} {MARKER}"
    question = _question(
        title=marked if where == "title" else title,
        resolution_criteria=(
            f"Resolves per https://results.cik.bg/{MARKER}/"
            if where == "resolution_criteria"
            else "Resolves per https://results.cik.bg/2026/"
        ),
    )
    text = f"{MARKER} unrelated" if where == "snippet" else "unrelated"
    problem = quality_problem(
        _packet((_document("https://other.example/x", text=text),)), question, NOW, DAYS
    )
    assert problem is not None, "an irrelevant document is exactly the refusal under test"
    assert MARKER not in problem.message and MARKER not in problem.code


@given(bad=UNCANONICALIZABLE_HOSTS, title=TITLES, carried=HOSTS)
def test_an_uncanonicalizable_named_host_still_yields_a_verdict(
    bad: str, title: str, carried: str
) -> None:
    """``host_identity`` is total, and this is the reachable path that needs it.

    ``host``'s ``except ValueError`` catches only the URLs ``urlsplit`` itself refuses.
    A second class gets past it: ``https://a..b/x`` parses fine and yields the host
    ``a..b``, which ``_canonical_host`` then rejects. Because ``missing_source_domains``
    canonicalizes *the question's* named domains, that raise lands inside a verdict
    function with no error type of its own -- a ``CanonicalizationError`` escaping to
    ``pipeline_live`` from one string of Metaculus-supplied resolution criteria. It is the
    same defect ``host`` was fixed for, one layer down, and removing the ``except
    CanonicalizationError`` fallback in ``canonical.host_identity`` fails this test.

    The first assertion is the vacuity guard: if canonicalization ever starts *accepting*
    these hosts, the strategy stops reaching the fallback and this test must fail loudly
    rather than keep passing on a branch it no longer enters.
    """
    with pytest.raises(CanonicalizationError):
        _canonical_host(bad)

    question = _question(title=title, resolution_criteria=f"Resolves per https://{bad}/x .")
    document = _document(f"https://{carried}/story", text=f"{title} -- reported today.")
    packet = _packet((document,))

    assert comparable_host(bad) == bad.lower().removeprefix("www.")
    assert isinstance(missing_source_domains(packet, question, NOW, DAYS), tuple)
    quality_problem(packet, question, NOW, DAYS)


@given(title=TITLES, carried=HOSTS, text=ENCODABLE_TEXT)
def test_a_question_naming_no_source_reports_no_gap(title: str, carried: str, text: str) -> None:
    """The negative control on the empty-set branch.

    Most questions name no resolution URL at all, so a gap reported here would put an
    ``evidence_gap`` row against essentially every forecast in the ledger and make the
    attribution claim meaningless by dilution. Returning anything but ``()`` from
    ``missing_source_domains``' ``if not domains`` branch fails this test.
    """
    question = _question(title=title, resolution_criteria=text, fine_print=None)
    assume(not source_domains(question))
    packet = _packet((_document(f"https://{carried}/story", text=f"{title} -- today."),))

    assert missing_source_domains(packet, question, NOW, DAYS) == ()


@given(
    host_name=HOSTS,
    title=TITLES,
    spoiler=st.sampled_from(["stale", "irrelevant"]),
)
def test_an_unusable_document_from_the_named_source_leaves_the_gap(
    host_name: str, title: str, spoiler: str
) -> None:
    """Only a **usable** document closes the gap.

    ``missing_source_domains`` filters on ``usable`` for the same reason
    ``quality_problem`` does: a document from the named authority that is two years stale,
    or about something else entirely, is not evidence the forecast rested on. Without the
    filter the ledger would record "we had resolution-source evidence" on the strength of
    a document the forecaster never saw -- an *overstated* attribution claim, which is the
    failure mode this item is least willing to accept.

    The two guards are the vacuity pair: the document must carry the named host (or the
    gap would be open for the trivial reason) and must be unusable (or it would close it).
    """
    named = f"https://{host_name}/2026/"
    if spoiler == "stale":
        document = _document(
            f"https://{host_name}/story",
            text=f"{title} -- reported then.",
            published=NOW - timedelta(days=DAYS * 40),
        )
    else:
        document = _document(f"https://{host_name}/story", text="wholly unrelated filler prose")
    question = _question(title=title, resolution_criteria=f"Resolves per {named} .")
    packet = _packet((document,))

    assert comparable_host(host(document.canonical_url) or "") == comparable_host(host_name), (
        "the document must carry the named host"
    )
    assert not usable(document, question, NOW, DAYS), "the document must be unusable"

    assert missing_source_domains(packet, question, NOW, DAYS) == source_domains(question)


@given(title=TITLES)
def test_relevance_is_reachable_for_every_generated_title(title: str) -> None:
    """The pool above claims each title has matchable terms. A title made entirely of
    `_STOP` words would make `relevant` false for every document and quietly turn the
    satisfiability property into a test of nothing."""
    document = _document("https://example.org/a", text=f"{title} -- reported today.")
    assert relevant(document, _question(title=title))
