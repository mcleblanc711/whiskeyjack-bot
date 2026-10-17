"""What the local-source lens has collected, and what it added (M1-358, D53).

Read-only: one pass over the ``local_lens_checkpoint`` journal and the runs, documents and
locale rows each one names. No provider call, no write. This is Phase 1's evidence -- whether
the lens finds **local publishers** the baseline did not -- and the input M5-805's paired
report will extend with scores once M1-359's shadow arm exists. It makes no claim about
forecast accuracy; Phase 1 has no arm to compare.

Per question it reports whether the lens triggered (planned any call), what it bought
(calls, requests, cost, elapsed time), what it found (documents, and their publisher origin
relative to the targeted locale), and what was new against the baseline, using
``research/overlap.py``'s conservative rules: a cluster of copies is one report, a report any
copy of which the baseline already held adds nothing, and an untranslated document is
``unassessed`` rather than ``novel``.

**What "English title" means here.** Overlap compares English titles. An AskNews document's
stored ``title`` is AskNews's English title, and a lens locale row carries it explicitly; an
Exa document's title is whatever the page said, so an Exa lens document with no translation
is compared on nothing and reported ``unassessed``. Baseline documents are compared on their
stored title -- which for AskNews is English and for Exa is usually English, an approximation
stated here rather than hidden in the number.

**Cost** is read from the checkpoint, which records each call's cost as the lens saw it:
Exa's ``costDollars`` (also on its run), and for AskNews the response's own
``usage.credits`` at the pinned per-credit rate -- the figure the budget settled from,
M1-336 -- because an AskNews run row carries no cost. A call that made requests but has no
usable cost is counted in ``cost_unknown_calls`` and contributes nothing to ``cost_usd``:
the total is then a floor, and says so, rather than reading an unknown as free.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from whiskeyjack_bot.research.model import DocumentLocale, ResearchDocument
from whiskeyjack_bot.research.overlap import (
    Comparable,
    classify,
    clusters,
    comparable,
    independent_reports,
)
from whiskeyjack_bot.research.store import (
    StoreError,
    load_documents,
    load_locales,
    load_run,
)


class LensReportError(Exception):
    """The report could not be built. Never echoes stored content."""


@dataclass(frozen=True)
class QuestionLens:
    """One question's lens checkpoint, summarized."""

    question_id: int
    at: str
    error_type: str | None
    triggered: bool
    calls: int
    requests: int
    cost_usd: float
    cost_unknown_calls: int
    elapsed_seconds: float | None
    lens_documents: int
    origins: dict[str, int]
    overlap: dict[str, int]
    reports: int
    independent_reports: int
    local_independent_reports: int


@dataclass(frozen=True)
class LensReport:
    questions: tuple[QuestionLens, ...]
    totals: dict[str, Any] = field(default_factory=dict)


def _checkpoints(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT data FROM tournament_events WHERE kind = 'local_lens_checkpoint' ORDER BY seq"
        ).fetchall()
    except sqlite3.Error:
        raise LensReportError("the ledger could not be read (detail withheld)") from None
    found: list[dict[str, Any]] = []
    for (data,) in rows:
        try:
            event = json.loads(data)
        except (TypeError, ValueError):
            raise LensReportError("a lens checkpoint is not valid JSON") from None
        if not isinstance(event, dict):
            raise LensReportError("a lens checkpoint is not a JSON object")
        found.append(event)
    return found


def _amount(value: object) -> float | None:
    """A non-negative finite number read back from a checkpoint, else ``None``.

    Checkpoint values are untrusted ledger reads. ``bool`` is excluded (it is an ``int``), and
    an ``int`` too large for a float is ``None`` rather than an ``OverflowError`` escaping the
    report's error contract (round 1, B2).
    """
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _id_list(value: object) -> list[str]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise LensReportError("a lens checkpoint names its runs in an unexpected shape")
    return list(value)


def _documents(conn: sqlite3.Connection, run_ids: list[str]) -> list[ResearchDocument]:
    return [document for run_id in run_ids for document in load_documents(conn, run_id)]


def _summarize(conn: sqlite3.Connection, event: dict[str, Any]) -> QuestionLens:
    question_id = event.get("question_id")
    if type(question_id) is not int:
        raise LensReportError("a lens checkpoint has no integer question_id")
    baseline_ids = _id_list(event.get("baseline_run_ids", []))
    lens_ids = _id_list(event.get("lens_run_ids", []))
    calls = event.get("calls", [])
    if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
        raise LensReportError("a lens checkpoint lists its calls in an unexpected shape")

    baseline = [
        comparable(document, english_title=document.title)
        for document in _documents(conn, baseline_ids)
    ]
    lens: list[Comparable] = []
    origin_of: dict[tuple[str, str, str], str] = {}
    origins: Counter[str] = Counter()
    for run_id in lens_ids:
        run = load_run(conn, run_id)
        locales: dict[tuple[str, str, str], DocumentLocale] = {
            locale.key(): locale for locale in load_locales(conn, run_id)
        }
        for document in load_documents(conn, run_id):
            row = locales.get(
                (document.retrieval_run_id, document.canonical_url, document.content_sha256)
            )
            if row is not None:
                english = row.english_title
                origins[row.publisher_origin] += 1
            else:
                english = document.title if run.provider == "asknews" else None
                origins["no_locale_row"] += 1
            item = comparable(document, english_title=english)
            lens.append(item)
            origin_of[item.key] = row.publisher_origin if row is not None else "no_locale_row"

    verdicts = classify(baseline, lens)
    groups = clusters(lens)
    independent = independent_reports(baseline, lens)
    requests = sum(
        call["requests"]
        for call in calls
        if type(call.get("requests")) is int and call["requests"] >= 0
    )
    cost = 0.0
    unknown = 0
    for call in calls:
        value = _amount(call.get("cost_usd"))
        if value is not None:
            cost += value
        elif call.get("interrupted") is True or (
            type(call.get("requests")) is int and call["requests"] > 0
        ):
            # Made requests, or was cut off mid-call (round 1, B1): billed or not, unknown.
            unknown += 1
    elapsed = _amount(event.get("elapsed_seconds"))
    error = event.get("error_type")
    return QuestionLens(
        question_id=question_id,
        at=str(event.get("at", "")),
        error_type=error if isinstance(error, str) else None,
        triggered=bool(calls),
        calls=len(calls),
        requests=requests,
        cost_usd=round(cost, 6),
        cost_unknown_calls=unknown,
        elapsed_seconds=elapsed,
        lens_documents=len(lens),
        origins=dict(sorted(origins.items())),
        overlap=dict(sorted(Counter(verdicts.values()).items())),
        reports=len(groups),
        independent_reports=len(independent),
        local_independent_reports=sum(
            1 for group in independent if any(origin_of[key] == "local" for key in group)
        ),
    )


def lens_report(conn: sqlite3.Connection) -> LensReport:
    """Summarize every lens checkpoint; raises :class:`LensReportError`."""
    if not isinstance(conn, sqlite3.Connection):
        raise LensReportError("conn must be a sqlite3.Connection")
    try:
        questions = tuple(_summarize(conn, event) for event in _checkpoints(conn))
    except StoreError as exc:
        raise LensReportError(f"stored research could not be read: {exc}") from None
    totals: dict[str, Any] = {
        "checkpoints": len(questions),
        "failed": sum(1 for q in questions if q.error_type is not None),
        "triggered": sum(1 for q in questions if q.triggered),
        "calls": sum(q.calls for q in questions),
        "requests": sum(q.requests for q in questions),
        "cost_usd": round(sum(q.cost_usd for q in questions), 6),
        "cost_unknown_calls": sum(q.cost_unknown_calls for q in questions),
        "lens_documents": sum(q.lens_documents for q in questions),
        "independent_reports": sum(q.independent_reports for q in questions),
        "local_independent_reports": sum(q.local_independent_reports for q in questions),
    }
    return LensReport(questions=questions, totals=totals)
