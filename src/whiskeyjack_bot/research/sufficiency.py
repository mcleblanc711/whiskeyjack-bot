"""The stale/insufficient research gate over a whole packet (M1-504).

``research/freshness.py`` (M1-305) tags one *document* fresh or stale against a
cutoff and deliberately stops there -- its own docstring names the packet-level
decision as this item's. This module makes that decision, and stops at the same
boundary in the other direction: it returns a verdict, never a config lookup or a
lifecycle write. Whether ``no_evidence``/``stale_evidence`` fails the run or only
flags it is ``forecast.fail_on_stale_research``/``flag_on_stale_research``, read by
the pipeline call sites that use this module, not by the module itself.

A packet is ``no_evidence`` when it carries zero documents, ``stale_evidence`` when
it carries at least one document and every one of them assesses stale (including
``undatable`` -- an unscored document cannot rescue a packet from ``stale_evidence``
any more than it can be shown ``fresh`` on its own), and ``sufficient`` otherwise. A
single fresh document is enough: ``forecast/attribution.py`` (M1-501) already treats
one document as enough to turn its evidence-conditional rules on, and this gate
keeps that same threshold rather than inventing a second, stricter one for the same
packet.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from whiskeyjack_bot.research.freshness import assess_document
from whiskeyjack_bot.research.packet import ResearchPacket

# Deliberately spelled out with ``lifecycle.FailureCode``'s own two members
# (``no_evidence``, ``stale_evidence``) rather than a fresh vocabulary translated at
# the call site -- a verdict here is passed straight through to
# ``record_failure(detail_code=...)``.
SufficiencyVerdict = Literal["sufficient", "no_evidence", "stale_evidence"]


def assess_sufficiency(packet: ResearchPacket, cutoff: datetime) -> SufficiencyVerdict:
    """Whether ``packet`` holds enough usable evidence to forecast from.

    Pure and deterministic like ``assess_document``: the caller derives ``cutoff``
    with ``freshness_cutoff`` from a reference time and
    ``retrieval.freshness_days_default`` (or a per-question override), so the
    verdict replays identically from stored timestamps.
    """
    if not packet.documents:
        return "no_evidence"
    if all(assess_document(doc, cutoff).state == "stale" for doc in packet.documents):
        return "stale_evidence"
    return "sufficient"
