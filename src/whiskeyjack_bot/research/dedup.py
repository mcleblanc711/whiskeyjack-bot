"""Deduplication of research evidence, mirroring the ledger constraint (M1-305).

Collapses documents the ledger would refuse as duplicates: the key is
``(retrieval_run_id, canonical_url, content_sha256)``, **exactly** the ledger's
``UNIQUE(retrieval_run_id, canonical_url, content_sha256)`` (M1-601). Within one
run this prevents a constraint violation from two reports of one article; it
**never collapses across runs**, because two providers (two runs) that both
surface one article are two legitimate ledger rows -- the run id is part of the
attribution, and merging them would erase which run found the evidence. So
cross-run/cross-provider provenance is preserved *by construction*, not by a
merge rule (an earlier cut keyed on ``(canonical_url, content_sha256)`` alone and
lost exactly that -- cross-model review round 1, finding 1).

``provenance`` distinguishes a document the pipeline fetched (``direct_api``) from
one a research agent merely reported (``llm_reported``), and the forecaster
prompt's evidence caps read it. Within a single run it is uniform today, but the
schema does not enforce that, so on an intra-run collision the survivor still
carries the *stronger* claim (``direct_api``) -- a verified retrieval is never
silently downgraded, nor a reported one upgraded. ``original_url`` is a schema
field on whichever document survives, so the as-retrieved URL is never lost.

Pure and deterministic: no I/O, first-seen order preserved, and the survivor of a
collision is a min over a **total** order (so the choice is independent of input
order and replay-stable -- round 1, finding 2).

A presentation-layer "one card per artifact across providers" view is deliberately
*not* built here: it would have to retain every contributing run/provenance rather
than drop them, and belongs to forecast assembly, not this dedup.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from whiskeyjack_bot.research.model import Provenance, ResearchDocument

# Lower rank is the stronger, more-attributable claim and wins a collapse. A
# pipeline-fetched document outranks an agent-reported one; see the module
# docstring for why the direction is never reversed.
_PROVENANCE_RANK: dict[Provenance, int] = {"direct_api": 0, "llm_reported": 1}


@dataclass(frozen=True)
class DedupResult:
    """The collapsed document set and how many duplicates were removed.

    ``collapsed_count`` is the number of documents dropped as duplicates (input
    length minus ``documents`` length); it is exposed so a writer can record an
    auditable dedup counter, in the spirit of ``ResearchRun.posts_dropped_no_url``.
    """

    documents: tuple[ResearchDocument, ...]
    collapsed_count: int


def dedup_key(document: ResearchDocument) -> tuple[str, str, str]:
    """The ledger's dedup identity: ``(retrieval_run_id, canonical_url, hash)``."""
    return (document.retrieval_run_id, document.canonical_url, document.content_sha256)


def _sort_key(document: ResearchDocument) -> tuple[int, datetime, str]:
    """A total order over same-key documents, used to pick the survivor.

    Stronger provenance first, then the earliest ``retrieved_at_utc`` (the first
    observation of the artifact), then the document's full canonical serialization
    -- an arbitrary but total and replay-stable final tiebreak that makes the
    order independent of input order even when two duplicates differ only in a
    non-key field such as ``title``.
    """
    return (
        _PROVENANCE_RANK[document.provenance],
        document.retrieved_at_utc,
        document.model_dump_json(),
    )


def _prefer(current: ResearchDocument, candidate: ResearchDocument) -> ResearchDocument:
    """Return whichever of two same-key documents is smaller in ``_sort_key``."""
    return candidate if _sort_key(candidate) < _sort_key(current) else current


def deduplicate(documents: Iterable[ResearchDocument]) -> DedupResult:
    """Collapse duplicates by the ledger's key, keeping one survivor per key.

    Returns the survivors in first-seen order and the count of duplicates removed.
    """
    survivors: dict[tuple[str, str, str], ResearchDocument] = {}
    order: list[tuple[str, str, str]] = []
    collapsed = 0
    for document in documents:
        key = dedup_key(document)
        if key not in survivors:
            survivors[key] = document
            order.append(key)
        else:
            collapsed += 1
            survivors[key] = _prefer(survivors[key], document)
    return DedupResult(documents=tuple(survivors[key] for key in order), collapsed_count=collapsed)
