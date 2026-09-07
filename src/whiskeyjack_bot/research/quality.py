"""Contemporary, relevant evidence gates for automatic approval (LAUNCH)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from whiskeyjack_bot.questions.model import CanonicalQuestion
from whiskeyjack_bot.research.model import ResearchDocument
from whiskeyjack_bot.research.packet import ResearchPacket

_STOP = frozenset(
    "will what when where which would should there their before after about between above below more than that this with from have been does into over under number percentage percent price close closing value resolve resolves resolution question".split()
)


def source_domains(question: CanonicalQuestion) -> tuple[str, ...]:
    text = " ".join(filter(None, [question.resolution_criteria, question.fine_print]))
    domains = set()
    for url in re.findall(r"https?://[^\s<>]+", text):
        url = url.rstrip(").,;")
        # A Wayback URL identifies the archived publisher, not archive.org as
        # today's resolution authority. Keep the archive itself in stored context.
        if urlsplit(url).hostname == "web.archive.org":
            embedded = re.search(r"/web/[^/]+/(https?://.+)", url)
            if embedded:
                url = embedded.group(1)
        host = urlsplit(url).hostname
        if host:
            domains.add(host)
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


def quality_problem(
    packet: ResearchPacket, question: CanonicalQuestion, now: datetime, days: int
) -> str | None:
    useful = [d for d in packet.documents if usable(d, question, now, days)]
    if not useful:
        return "no usable contemporary evidence relevant to the question"
    domains = source_domains(question)
    if domains and not any(
        (urlsplit(d.canonical_url).hostname or "").removeprefix("www.")
        in {host.removeprefix("www.") for host in domains}
        for d in useful
    ):
        return "missing contemporary evidence from the named resolution source"
    return None


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
