"""Provenance-preserving deduplication of research evidence (M1-305).

Collapses documents that are the **same underlying artifact** -- identical
``canonical_url`` *and* identical ``content_sha256`` -- into one, so a forecaster
is not shown, and the ledger is not asked to store, the same article twice. The
key mirrors the ledger's ``UNIQUE(retrieval_run_id, canonical_url,
content_sha256)`` (M1-601) minus the run id: within one run this prevents a
constraint violation, and across a question's runs it is what lets two providers
that both surfaced one article collapse to a single piece of evidence. The scope
of a collapse is the input the caller passes -- one run's documents for strict
per-run semantics, or a question's whole set to dedup across providers.

**Without losing provenance** is the acceptance criterion and the delicate part.
``provenance`` distinguishes a document the pipeline fetched (``direct_api``) from
one a research agent merely reported (``llm_reported``), and the forecaster
prompt's evidence caps read it. When the same artifact arrives both ways, the
survivor must carry the *stronger* claim (``direct_api``): a verified retrieval is
never silently downgraded to a reported one, nor a reported one upgraded to
verified. ``original_url`` is a schema field on whichever document survives, so
the as-retrieved URL is never lost either.

Pure and deterministic: no I/O, first-seen order preserved, ties broken by a
total, timestamp-based rule so the same input always yields the same output.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

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


def dedup_key(document: ResearchDocument) -> tuple[str, str]:
    """The artifact identity a collapse is keyed on: ``(canonical_url, hash)``."""
    return (document.canonical_url, document.content_sha256)


def _prefer(current: ResearchDocument, candidate: ResearchDocument) -> ResearchDocument:
    """Choose the survivor of two same-artifact documents.

    Stronger provenance wins; on equal provenance the earliest ``retrieved_at_utc``
    wins (the first observation of the artifact); a remaining tie keeps the
    first-seen document. Total and order-independent, so the result is stable.
    """
    current_rank = _PROVENANCE_RANK[current.provenance]
    candidate_rank = _PROVENANCE_RANK[candidate.provenance]
    if candidate_rank < current_rank:
        return candidate
    if candidate_rank > current_rank:
        return current
    if candidate.retrieved_at_utc < current.retrieved_at_utc:
        return candidate
    return current


def deduplicate(documents: Iterable[ResearchDocument]) -> DedupResult:
    """Collapse same-artifact documents, preserving the strongest provenance.

    Returns the survivors in first-seen order and the count of duplicates removed.
    """
    survivors: dict[tuple[str, str], ResearchDocument] = {}
    order: list[tuple[str, str]] = []
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
