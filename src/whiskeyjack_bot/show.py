"""Assemble one forecast record's ledger state for the read-only `show` command (M1-611).

Nothing in this module writes to the ledger or contacts a provider. It joins exactly six
existing pure-read functions from three collaborator modules -- no new SQL is written here:

- :func:`approval.read_forecast_summary` -- identity, derived status, content hash.
- :func:`approval.effective_approval` and :func:`approval.approval_history` -- the approval
  currently in force and the full decision trail, each with its ``payload_sha256`` binding.
- :func:`lifecycle.read_history` -- the full lifecycle event history.
- :func:`lifecycle.unresolved_uncertainties` -- attempt ids an operator would otherwise have
  to recover from a live submission artifact or scrollback (``docs/RUNBOOK.md``'s "how to
  find the attempt id if you lost it").
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
    read_history,
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


@dataclass(frozen=True)
class RecordShow:
    """Everything M1-611's acceptance criterion asks a read-only `show` to report.

    Deliberately narrower than the full canonical record CODEX_HANDOFF.md describes
    (question text, model settings, sources, resolution and score events): that join is
    M1-612's stated scope. Both :attr:`effective_approval` and the full
    :attr:`approval_history` are carried, not just the one currently in force -- an
    attribution tool over an append-only ledger should not hide a superseded or rejected
    decision.
    """

    summary: ForecastSummary
    effective_approval: ApprovalRecord | None
    approval_history: tuple[ApprovalRecord, ...]
    lifecycle_history: tuple[LifecycleEvent, ...]
    unresolved_uncertainties: tuple[str, ...]
    standing_reservations: tuple[KeyReservation, ...]


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
    except LifecycleError as exc:
        raise ShowError(str(exc)) from None
    try:
        reservations = live_reservations_for_record(conn, record_id)
    except SubmissionError as exc:
        raise ShowError(str(exc)) from None
    return RecordShow(
        summary=summary,
        effective_approval=approval,
        approval_history=history,
        lifecycle_history=lifecycle,
        unresolved_uncertainties=uncertainties,
        standing_reservations=reservations,
    )
