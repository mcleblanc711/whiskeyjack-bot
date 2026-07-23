"""Deterministic freshness-tagging of research evidence (M1-305).

Marks a document fresh or stale relative to a question-specific window. The two
acceptance words are load-bearing: **deterministic** means the verdict is a pure
function of timestamps the caller supplies -- it never reads the wall clock, so a
replay of a stored forecast reproduces the same tags -- and **flagged** means this
module only *tags*. Whether a stale document fails the run or merely annotates it
is ``forecast.fail_on_stale_research`` / ``flag_on_stale_research``, and that gate
is M1-504 (which depends on this item). Splitting them keeps the epic boundary:
tagging is evidence about the document, gating is policy about the forecast.

The window is expressed as a cutoff instant. A caller derives it with
``freshness_cutoff`` from a reference time (e.g. the run's ``started_at_utc`` or
the question snapshot time) and a day count (``retrieval.freshness_days_default``,
or a per-question override), then asks ``assess_freshness`` per document. A
document's effective date is ``updated_at_utc`` when present, else
``published_at_utc``; ``retrieved_at_utc`` is deliberately not used -- it records
when *we* fetched the document, not how old its content is, and a fresh fetch of
stale content is still stale evidence.

An **undated** document (neither published nor updated) is tagged ``stale`` with
reason ``undatable``: it cannot be shown to fall within the window, and the
stricter reading (CLAUDE.md ambiguity rule 4) flags what it cannot prove rather
than letting undated evidence pass unchecked where M1-504 could never catch it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from whiskeyjack_bot.research.model import ResearchDocument

# Fresh: effective date is at or after the cutoff. Stale: before it, or undatable.
FreshnessState = Literal["fresh", "stale"]

# Why a verdict landed where it did. ``within_window`` and ``before_cutoff`` are
# dated outcomes; ``undatable`` is the no-timestamp case, kept distinct so a
# consumer (M1-504) can tell "we checked and it is old" from "we could not check".
FreshnessReason = Literal["within_window", "before_cutoff", "undatable"]


@dataclass(frozen=True)
class FreshnessVerdict:
    """The freshness of one document against one cutoff.

    A value object, not stored: freshness is derived at forecast time from
    timestamps the schema already carries, so it is recomputed on replay rather
    than persisted (there is no freshness column on ``research_documents``).
    """

    state: FreshnessState
    reason: FreshnessReason
    cutoff: datetime
    # None exactly when reason is ``undatable``; otherwise the date compared.
    effective_date: datetime | None


def freshness_cutoff(reference: datetime, days: int) -> datetime:
    """Return the oldest instant a document may be dated and still count as fresh.

    Pure subtraction: the caller owns ``reference`` (never ``datetime.now()`` in
    this module) so the window, and therefore every verdict against it, is
    reproducible on replay. ``days`` is validated at the config boundary
    (``retrieval.freshness_days_default`` is ``ge=1``); this function does not
    re-police it.
    """
    return reference - timedelta(days=days)


def assess_freshness(
    published_at: datetime | None,
    updated_at: datetime | None,
    cutoff: datetime,
) -> FreshnessVerdict:
    """Tag a document fresh or stale against ``cutoff``, deterministically.

    Effective date is ``updated_at`` when present, else ``published_at`` -- the
    most recent evidence that the content is current. Undated -> ``stale`` /
    ``undatable``. Otherwise the boundary is inclusive at the cutoff: a document
    dated exactly at ``cutoff`` is ``fresh`` (the window is "on or after"), and
    only a strictly earlier date is ``stale`` / ``before_cutoff``.
    """
    effective_date = updated_at if updated_at is not None else published_at
    if effective_date is None:
        return FreshnessVerdict(
            state="stale", reason="undatable", cutoff=cutoff, effective_date=None
        )
    if effective_date < cutoff:
        return FreshnessVerdict(
            state="stale", reason="before_cutoff", cutoff=cutoff, effective_date=effective_date
        )
    return FreshnessVerdict(
        state="fresh", reason="within_window", cutoff=cutoff, effective_date=effective_date
    )


def assess_document(document: ResearchDocument, cutoff: datetime) -> FreshnessVerdict:
    """``assess_freshness`` reading the two timestamp fields off a document."""
    return assess_freshness(document.published_at_utc, document.updated_at_utc, cutoff)
