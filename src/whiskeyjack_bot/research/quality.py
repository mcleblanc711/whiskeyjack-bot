"""Contemporary, relevant evidence gates for automatic approval (LAUNCH, M1-327).

Two verdicts, deliberately separate since M1-327. :func:`quality_problem` is the
**fatal** one -- a packet with nothing usable in it is nothing to forecast from.
:func:`missing_source_domains` is the **recorded** one: a forecast made without a
document from the resolution authority the question names is still a forecast, and
saying so on the attribution record suits this project better than declining. See that
function for the two live questions that argument was settled on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from whiskeyjack_bot.questions.model import CanonicalQuestion
from whiskeyjack_bot.research.canonical import host_identity
from whiskeyjack_bot.research.model import ResearchDocument
from whiskeyjack_bot.research.packet import ResearchPacket

_STOP = frozenset(
    "will what when where which would should there their before after about between above below more than that this with from have been does into over under number percentage percent price close closing value resolve resolves resolution question".split()
)


def host(url: str) -> str | None:
    """``url``'s host as written, or ``None`` when there is not a readable one.

    The guard is the point (M1-327). ``urlsplit`` raises a bare ``ValueError`` on an
    unparsable authority -- ``urlsplit("https://[abc").hostname`` is
    ``ValueError: Invalid IPv6 URL`` -- and the text :func:`source_domains` scans is a
    question's ``resolution_criteria``/``fine_print``, which arrives from the Metaculus
    API and is untrusted under CLAUDE.md's threat boundary. That raise escaped as a raw
    ``ValueError``, which is a review finding on its own; worse, ``orchestrate.py``
    calls ``source_domains`` before retrieval, so one such string in one question's
    resolution criteria aborted the *retrieval*, not merely this gate. A URL whose host
    cannot be read names no resolution authority, so it is skipped.

    ``ResearchDocument.canonical_url`` cannot reach that branch -- ``HttpUrlString``
    refuses the same string -- but the guard lives on the shared helper rather than on
    the question side alone: which side is schema-protected is a fact about today's
    models, not something either caller should have to know.

    The host is returned **as written**, ``www.`` and all. Stripping here would change
    what ``orchestrate.py`` passes to Exa as ``include_domains`` and what
    ``test_archived_resolution_url_names_the_original_publisher`` pins, neither of which
    this item is about. :func:`comparable_host` owns the strip, at the one place two
    hosts are compared.
    """
    try:
        parsed = urlsplit(url).hostname
    except ValueError:
        return None
    return parsed


def comparable_host(value: str) -> str:
    """One host reduced to the form two spellings of it share.

    Delegated to ``research/canonical.host_identity``, which is where this project's
    knowledge of host spellings already lives. The old rule was ``removeprefix("www.")``
    written out twice inside one boolean expression, and the property pass this item
    required found what it missed on its first run: a question naming
    ``https://results.cik.bg./2026/`` -- one terminal DNS root dot, D32's own example --
    could never match a document from ``results.cik.bg``, because the two were compared
    as strings. That is the same unsatisfiability M1-327 exists to remove, one layer down
    from the branch it was filed against.
    """
    return host_identity(value)


def source_domains(question: CanonicalQuestion) -> tuple[str, ...]:
    text = " ".join(filter(None, [question.resolution_criteria, question.fine_print]))
    domains = set()
    for url in re.findall(r"https?://[^\s<>]+", text):
        url = url.rstrip(").,;")
        # A Wayback URL identifies the archived publisher, not archive.org as
        # today's resolution authority. Keep the archive itself in stored context.
        if host(url) == "web.archive.org":
            embedded = re.search(r"/web/[^/]+/(https?://.+)", url)
            if embedded:
                url = embedded.group(1)
        named = host(url)
        if named:
            domains.add(named)
    return tuple(sorted(domains))


def contemporary(doc: ResearchDocument, now: datetime, days: int) -> bool:
    dates = [d for d in (doc.published_at_utc, doc.updated_at_utc) if d is not None]
    return bool(
        dates
        and all(d <= now for d in dates)
        and max(dates) >= now - timedelta(days=days)
        and doc.retrieved_at_utc <= now
        and (doc.snippet or doc.summary or "").strip()
    )


def relevant(doc: ResearchDocument, question: CanonicalQuestion) -> bool:
    content = " ".join(filter(None, [doc.title, doc.snippet, doc.summary])).lower()
    if "net worth" in question.title.lower() and "net worth" not in content:
        return False
    terms = set(re.findall(r"[a-z0-9]{3,}", question.title.lower())) - _STOP
    return len({t for t in terms if t in content}) >= min(2, len(terms)) and bool(terms)


def usable(doc: ResearchDocument, question: CanonicalQuestion, now: datetime, days: int) -> bool:
    return contemporary(doc, now, days) and relevant(doc, question)


# Deliberately spelled out with ``lifecycle.FailureCode``'s own two members rather than a
# fresh vocabulary translated at the call site -- exactly the reasoning
# ``research/sufficiency.py`` gives for ``SufficiencyVerdict``. A verdict here is passed
# straight through to ``record_pre_forecast_failure(detail_code=...)`` (M1-326).
QualityCode = Literal["no_evidence", "stale_evidence"]


@dataclass(frozen=True)
class QualityProblem:
    """Why this packet cannot be forecast from, in both machine and human form.

    ``code`` exists because M1-326's skip has to classify the verdict, and deriving a
    classification by matching ``message`` would make it depend on wording this project's
    error-hygiene rule may legitimately reword at any time. ``message`` stays the operator's
    half: it names no stored value, and it is what distinguishes the two refusals that share
    a code.
    """

    code: QualityCode
    message: str


def quality_problem(
    packet: ResearchPacket, question: CanonicalQuestion, now: datetime, days: int
) -> QualityProblem | None:
    """The packet's refusal, or ``None`` when it may be forecast from.

    Deterministic in the same sense ``assess_sufficiency`` is: over a stored packet and a
    fixed ``now`` it replays identically. That is precisely why M1-326 refuses to buy
    research again to re-derive it -- a repeat is not a retry, it is the same answer at
    full price.

    **Every branch left here is fatal, and that is now the whole rule.** A third branch
    refused a packet that carried no document from a resolution authority the question
    named; it is :func:`missing_source_domains` since M1-327, and it no longer refuses
    anything. Nothing about the two remaining branches changed, so a stored verdict of
    ``stale_evidence`` or a genuinely empty packet replays exactly as it did.
    """
    useful = [d for d in packet.documents if usable(d, question, now, days)]
    if not useful:
        # Documents that exist but have all aged out are a different operator problem from
        # having nothing, and `stale_evidence` is the member that already means it.
        if packet.documents and not any(contemporary(d, now, days) for d in packet.documents):
            return QualityProblem(
                "stale_evidence", "no contemporary evidence relevant to the question"
            )
        return QualityProblem(
            "no_evidence", "no usable contemporary evidence relevant to the question"
        )
    return None


def missing_source_domains(
    packet: ResearchPacket, question: CanonicalQuestion, now: datetime, days: int
) -> tuple[str, ...]:
    """The resolution authorities the packet has no usable document from (M1-327).

    Empty when the question names none, and empty when at least one usable document
    carries one of them -- the requirement has always been "any of them", so a gap is
    all-or-nothing and the whole named set is what is missing.

    **This used to be a third branch of :func:`quality_problem`, and refusing on it was
    wrong.** It asks a resolution *authority* to appear as a *news publisher*, and the
    only two retrievers this project has are news retrievers. Cup question 45452 names
    ``results.cik.bg``, the Bulgarian election commission's results portal for an
    election on 2026-10-25: it cannot have news coverage before the event it exists to
    report, so 13 usable AskNews documents were discarded on each of 17 retrievals and
    the question was never forecast. It is not a results-portal edge case either -- the
    Cup rehearsal question 43330 names ``forbes.com``, a real news publisher, and was
    refused twice for the same reason, because neither provider happened to return it.

    So the gap is now recorded rather than fatal (owner decision, 2026-09-09). An
    attribution instrument is better served by a forecast that states what evidence it
    lacked than by no forecast at all; ``pipeline_live`` appends it against the record
    as an ``evidence_gap`` event. The first branch of :func:`quality_problem` stays
    fatal, because "we have nothing usable" really is nothing to forecast from.

    Pure and deterministic over a stored packet and a fixed ``now``, exactly as
    :func:`quality_problem` is -- and for the same reason, since M1-326's gate must be
    able to trust that a replay is not a new answer.
    """
    domains = source_domains(question)
    if not domains:
        return ()
    wanted = {comparable_host(named) for named in domains}
    for document in packet.documents:
        if not usable(document, question, now, days):
            continue
        carried = host(document.canonical_url)
        if carried is not None and comparable_host(carried) in wanted:
            return ()
    return domains


def usable_packet(
    packet: ResearchPacket, question: CanonicalQuestion, now: datetime, days: int
) -> ResearchPacket:
    """Select model inputs without modifying retained research or legacy hashing."""
    return ResearchPacket(
        question_id=packet.question_id,
        runs=packet.runs,
        documents=tuple(d for d in packet.documents if usable(d, question, now, days)),
    )


def without_future(packet: ResearchPacket, now: datetime) -> ResearchPacket:
    return ResearchPacket(
        question_id=packet.question_id,
        runs=packet.runs,
        documents=tuple(
            d
            for d in packet.documents
            if all(
                t <= now
                for t in (d.published_at_utc, d.updated_at_utc, d.retrieved_at_utc)
                if t is not None
            )
        ),
    )
