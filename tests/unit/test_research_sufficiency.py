"""M1-504: the packet-level sufficiency verdict `research/freshness.py` (M1-305) hands off
to. A packet with no documents is `no_evidence`; one where every document assesses stale is
`stale_evidence`; a single fresh document among any number of stale ones is `sufficient`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from whiskeyjack_bot.research.freshness import freshness_cutoff
from whiskeyjack_bot.research.model import ResearchDocument, ResearchRun
from whiskeyjack_bot.research.packet import ResearchPacket, build_packet
from whiskeyjack_bot.research.sufficiency import assess_sufficiency

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
CUTOFF = freshness_cutoff(NOW, 30)
FRESH_TS = NOW
STALE_TS = NOW - timedelta(days=31)


def _run(question_id: int = 42, run_id: str = "run-1") -> ResearchRun:
    return ResearchRun(
        retrieval_run_id=run_id,
        question_id=question_id,
        provider="asknews",
        started_at_utc=NOW,
    )


def _doc(url: str, run_id: str = "run-1", **overrides: Any) -> ResearchDocument:
    fields: dict[str, Any] = {
        "retrieval_run_id": run_id,
        "original_url": url,
        "canonical_url": url,
        "retrieved_at_utc": NOW,
        "source_type": "news",
        "provenance": "direct_api",
        "content_sha256": "a" * 64,
    }
    fields.update(overrides)
    return ResearchDocument(**fields)


def _packet(*documents: ResearchDocument, question_id: int = 42) -> ResearchPacket:
    return build_packet(question_id, [_run(question_id=question_id)], list(documents))


def test_a_packet_with_no_documents_is_no_evidence() -> None:
    assert assess_sufficiency(_packet(), CUTOFF) == "no_evidence"


def test_a_packet_of_entirely_stale_documents_is_stale_evidence() -> None:
    packet = _packet(
        _doc("https://a.example/1", published_at_utc=STALE_TS),
        _doc("https://a.example/2", published_at_utc=STALE_TS),
    )
    assert assess_sufficiency(packet, CUTOFF) == "stale_evidence"


def test_a_wholly_undatable_packet_is_stale_evidence() -> None:
    # Undatable documents assess `stale` (M1-305's stricter reading), so a packet
    # that carries only undated evidence cannot be told apart from one that is
    # provably old -- neither can support a forecast.
    packet = _packet(_doc("https://a.example/1"))
    assert assess_sufficiency(packet, CUTOFF) == "stale_evidence"


def test_one_fresh_document_among_many_stale_ones_is_sufficient() -> None:
    # Matches M1-501's own threshold: one document is enough to turn its
    # evidence-conditional rules on, so this gate does not invent a stricter bar
    # for the same packet.
    packet = _packet(
        _doc("https://a.example/1", published_at_utc=STALE_TS),
        _doc("https://a.example/2", published_at_utc=STALE_TS),
        _doc("https://a.example/3", published_at_utc=FRESH_TS),
    )
    assert assess_sufficiency(packet, CUTOFF) == "sufficient"


def test_a_packet_of_only_fresh_documents_is_sufficient() -> None:
    packet = _packet(_doc("https://a.example/1", published_at_utc=FRESH_TS))
    assert assess_sufficiency(packet, CUTOFF) == "sufficient"
