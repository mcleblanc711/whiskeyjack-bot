"""Assemble one forecast record's canonical ledger history for `show` (M1-611/M1-612).

Nothing in this module writes to the ledger or contacts a provider. It joins existing
pure-read functions from three collaborator modules -- no new SQL is written here; new
SQL for a table with no existing per-record reader (``submission_attempts``,
``submission_verifications``) lives in :mod:`lifecycle`, which already owns every write
to those tables:

- :func:`approval.read_forecast_summary` -- identity, derived status, content hash.
- :func:`approval.effective_approval` and :func:`approval.approval_history` -- the approval
  currently in force and the full decision trail, each with its ``payload_sha256`` binding.
- :func:`lifecycle.read_history` -- the full lifecycle event history.
- :func:`lifecycle.unresolved_uncertainties` -- attempt ids an operator would otherwise have
  to recover from a live submission artifact or scrollback (``docs/RUNBOOK.md``'s "how to
  find the attempt id if you lost it").
- :func:`lifecycle.record_attempt_id`, :func:`lifecycle.read_submission_attempts`,
  :func:`lifecycle.read_submission_verifications`, :func:`lifecycle.read_resolution_history`,
  :func:`lifecycle.read_local_scores`, :func:`lifecycle.read_pipeline_failure_events` --
  M1-612's join: every linked approval, submission, verification, lifecycle, pre-forecast
  failure, resolution and score event, merged below into one chronological order.
- :func:`submission.live_reservations_for_record` -- standing key reservations, otherwise
  visible only as a side effect of a `submit`/`release-key` refusal.

Each collaborator's own sanitized error is re-raised here as :class:`ShowError`, mirroring
`submission.py`'s own `_wrap_lifecycle`/`_wrap_approval` pattern: a caller of this module
handles one exception type, and every message it carries is already value-free because the
function that raised it is.

Purely local file I/O: nothing here contacts Metaculus, AskNews or Exa.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from whiskeyjack_bot.approval import (
    ApprovalError,
    ApprovalRecord,
    ForecastSummary,
    approval_history,
    effective_approval,
    read_forecast_summary,
)
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    LifecycleEvent,
    PreForecastFailure,
    StoredResolution,
    StoredScore,
    StoredSubmissionAttempt,
    StoredSubmissionVerification,
    read_history,
    read_local_scores,
    read_pipeline_failure_events,
    read_resolution_history,
    read_submission_attempts,
    read_submission_verifications,
    record_attempt_id,
    unresolved_uncertainties,
)
from whiskeyjack_bot.submission import KeyReservation, SubmissionError, live_reservations_for_record


class ShowError(Exception):
    """A record's ledger state cannot be assembled read-only.

    Same hygiene rule as every other module's own error type: the message never echoes a
    stored or caller-supplied value. Each instance is built from a collaborator's own
    already-sanitized message, preserved rather than replaced with a constant -- the same
    reasoning :class:`approval.ApprovalError`'s docstring gives for wrapping
    :class:`lifecycle.LifecycleError`.
    """


HistoryEntryKind = Literal[
    "approval",
    "submission_attempt",
    "submission_verification",
    "lifecycle",
    "pre_forecast_failure",
    "resolution",
    "score",
]


@dataclass(frozen=True)
class HistoryEntry:
    """One event from any of the seven categories M1-612's acceptance criterion names.

    Exactly one of the payload fields is set, chosen by :attr:`kind`; the rest are
    ``None``. A tagged union rather than seven separate lists is what lets
    :func:`merge_canonical_history` treat every category alike -- tag, timestamp, sort --
    without a bespoke case for each one.
    """

    kind: HistoryEntryKind
    occurred_at_utc: str
    approval: ApprovalRecord | None = None
    submission_attempt: StoredSubmissionAttempt | None = None
    submission_verification: StoredSubmissionVerification | None = None
    lifecycle_event: LifecycleEvent | None = None
    pre_forecast_failure: PreForecastFailure | None = None
    resolution: StoredResolution | None = None
    score: StoredScore | None = None


def merge_canonical_history(
    *,
    approvals: tuple[ApprovalRecord, ...],
    submission_attempts: tuple[StoredSubmissionAttempt, ...],
    submission_verifications: tuple[StoredSubmissionVerification, ...],
    lifecycle_events: tuple[LifecycleEvent, ...],
    pre_forecast_failures: tuple[PreForecastFailure, ...],
    resolutions: tuple[StoredResolution, ...],
    scores: tuple[StoredScore, ...],
) -> tuple[HistoryEntry, ...]:
    """Merge seven independently-ordered event streams into one chronological order.

    Not every category has a linked ``lifecycle_events`` row to hang off of:
    ``record_resolution_observation`` only appends one on a record's first
    ``submitted -> resolved`` transition (a later re-resolution while already
    ``resolved`` writes a bare ``resolution_events`` row), and
    ``record_local_scores`` links only the first row of a re-scoring batch. Merging the
    seven tables directly, rather than following ``lifecycle_events``' link columns, is
    what keeps those un-linked rows from silently vanishing from the record's history.

    Pure and total: tag each row by kind, concatenate category by category (each
    category already comes back from its own reader in that category's own
    chronological order), then a single stable sort by ``occurred_at_utc``. Stability is
    what keeps two same-instant rows in their original per-category order without a
    bespoke tiebreak -- Python's ``sorted`` guarantees it -- and every timestamp compared
    here is ``lifecycle._utc_text``'s fixed-width canonical form
    (``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``), which lifecycle.py already pins on every
    column that matters for ordering: comparing them lexicographically is exact, unlike
    the julianday() comparison that loses sub-microsecond ordering.
    """
    entries = [
        *(HistoryEntry("approval", a.occurred_at_utc, approval=a) for a in approvals),
        *(
            HistoryEntry("submission_attempt", a.requested_at_utc, submission_attempt=a)
            for a in submission_attempts
        ),
        *(
            HistoryEntry("submission_verification", v.observed_at_utc, submission_verification=v)
            for v in submission_verifications
        ),
        *(
            HistoryEntry("lifecycle", e.occurred_at_utc, lifecycle_event=e)
            for e in lifecycle_events
        ),
        *(
            HistoryEntry("pre_forecast_failure", f.occurred_at_utc, pre_forecast_failure=f)
            for f in pre_forecast_failures
        ),
        *(HistoryEntry("resolution", r.observed_at_utc, resolution=r) for r in resolutions),
        *(HistoryEntry("score", s.computed_at_utc, score=s) for s in scores),
    ]
    return _stable_chronological(entries)


def _stable_chronological(entries: list[HistoryEntry]) -> tuple[HistoryEntry, ...]:
    """The pure sort/tiebreak this module's fuzz pass (M1-612) is against.

    Factored out of :func:`merge_canonical_history` so it can be exercised directly with
    synthetic, minimal :class:`HistoryEntry` values -- the merge itself needs no real
    ``ApprovalRecord``/``StoredResolution``/etc. payloads to prove: only ``kind`` and
    ``occurred_at_utc`` matter to the ordering it produces.
    """
    return tuple(sorted(entries, key=lambda entry: entry.occurred_at_utc))


@dataclass(frozen=True)
class RecordShow:
    """Everything the `show` acceptance criteria ask a read-only command to report.

    :attr:`canonical_history` is M1-612's join: every linked approval, submission,
    verification, lifecycle, pre-forecast failure, resolution and score event, in one
    chronological order (:func:`merge_canonical_history`). The per-category tuples
    beside it are kept too, both because M1-611 already exposed some of them and because
    a caller wanting only one category's detail should not have to filter the merged
    view to get it. Both :attr:`effective_approval` and the full :attr:`approval_history`
    are carried, not just the one currently in force -- an attribution tool over an
    append-only ledger should not hide a superseded or rejected decision.
    """

    summary: ForecastSummary
    effective_approval: ApprovalRecord | None
    approval_history: tuple[ApprovalRecord, ...]
    lifecycle_history: tuple[LifecycleEvent, ...]
    unresolved_uncertainties: tuple[str, ...]
    standing_reservations: tuple[KeyReservation, ...]
    submission_attempts: tuple[StoredSubmissionAttempt, ...]
    submission_verifications: tuple[StoredSubmissionVerification, ...]
    resolution_history: tuple[StoredResolution, ...]
    score_history: tuple[StoredScore, ...]
    pre_forecast_failures: tuple[PreForecastFailure, ...]
    canonical_history: tuple[HistoryEntry, ...]


def assemble_show(conn: sqlite3.Connection, record_id: str) -> RecordShow:
    """Join one record's status, approval, lifecycle and submission state, read-only.

    :func:`approval.read_forecast_summary` runs first and is what actually validates
    ``record_id`` names a stored record; everything after it assumes that record exists.
    """
    try:
        summary = read_forecast_summary(conn, record_id)
        approval = effective_approval(conn, record_id)
        history = approval_history(conn, record_id)
    except ApprovalError as exc:
        raise ShowError(str(exc)) from None
    try:
        lifecycle = read_history(conn, record_id)
        uncertainties = unresolved_uncertainties(conn, record_id)
        attempts = read_submission_attempts(conn, record_id)
        verifications = read_submission_verifications(conn, record_id)
        resolutions = read_resolution_history(conn, record_id)
        scores = read_local_scores(conn, record_id)
        attempt_id = record_attempt_id(conn, record_id)
        pre_forecast_failures = (
            () if attempt_id is None else read_pipeline_failure_events(conn, attempt_id)
        )
    except LifecycleError as exc:
        raise ShowError(str(exc)) from None
    try:
        reservations = live_reservations_for_record(conn, record_id)
    except SubmissionError as exc:
        raise ShowError(str(exc)) from None
    canonical = merge_canonical_history(
        approvals=history,
        submission_attempts=attempts,
        submission_verifications=verifications,
        lifecycle_events=lifecycle,
        pre_forecast_failures=pre_forecast_failures,
        resolutions=resolutions,
        scores=scores,
    )
    return RecordShow(
        summary=summary,
        effective_approval=approval,
        approval_history=history,
        lifecycle_history=lifecycle,
        unresolved_uncertainties=uncertainties,
        standing_reservations=reservations,
        submission_attempts=attempts,
        submission_verifications=verifications,
        resolution_history=resolutions,
        score_history=scores,
        pre_forecast_failures=pre_forecast_failures,
        canonical_history=canonical,
    )
