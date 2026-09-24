"""Record local and platform scores for resolved forecasts (M4-802, M4-803).

``whiskeyjack-bot score`` is this module's only caller. It reads and writes the ledger and
nothing else: no network, no paid call, no submission path, and no import that reaches one.

**Which records.** Every forecast record with at least one resolution observation, or the one
record named. Each gets two writers, in this order, each in its own transaction:

- **Local** (M4-802). Binary and multiple-choice records are handed to
  :func:`lifecycle.record_local_scores`, which decides from the ledger whether the latest
  observation is scorable and what is already written. Numeric and discrete records are
  reported ``out_of_scope`` and never scored locally (D30).
- **Platform** (M4-803). Every record is handed to :func:`lifecycle.record_platform_scores`,
  which copies Metaculus's own scores out of the stored observation. Local runs first so that,
  for binary and multiple choice, the ``scored`` event keeps linking the local row it always
  has; for numeric and discrete the platform writer is what moves the record to ``scored``.

**Failures are per record.** A record the writer refuses is reported and skipped; the rest of
the run proceeds. ``score`` exits non-zero if anything failed, so a scheduled run cannot fail
quietly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, get_args

from whiskeyjack_bot.config import SupportedQuestionType
from whiskeyjack_bot.lifecycle import LifecycleError, record_local_scores, record_platform_scores

ScoreStatus = Literal["appended", "unchanged", "not_scorable", "out_of_scope", "failed"]

# The question types this item scores locally, by the `forecast_records.question_type`
# literal. The writer re-derives the type from the verified record; this is only the filter.
_LOCALLY_SCORED_TYPES = frozenset({"binary", "multiple_choice"})
# Gated before a stored type is put in a result the CLI prints.
_SUPPORTED_TYPES = frozenset(get_args(SupportedQuestionType))


class ScoreRecordsError(Exception):
    """The set of records to score could not be read, or the record named is not stored."""


@dataclass(frozen=True)
class ScoreResult:
    """What happened to one forecast record in one run.

    ``status``/``rows_appended`` are the local writer's and ``platform_status``/
    ``platform_rows_appended`` the platform writer's. ``detail`` is a message from this
    package's own sanitized error types, set only when either status is ``failed`` (the local
    one first); it names rules and fields, never a stored value. ``moved_to_scored`` is true
    when either writer moved the record.
    """

    record_id: str
    question_id: int
    question_type: str
    status: ScoreStatus
    platform_status: ScoreStatus
    rows_appended: int = 0
    platform_rows_appended: int = 0
    moved_to_scored: bool = False
    detail: str | None = None

    @property
    def failed(self) -> bool:
        return self.status == "failed" or self.platform_status == "failed"


def score_records(
    conn: sqlite3.Connection,
    *,
    record_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> tuple[ScoreResult, ...]:
    """Score every record with a resolution observation, or the one record named."""
    if record_id is not None and (type(record_id) is not str or not record_id):
        raise ScoreRecordsError("record_id must be a non-empty string")
    now = clock if clock is not None else _utcnow
    results: list[ScoreResult] = []
    for identifier, question_id, question_type in _candidate_records(conn, record_id):
        details: list[str] = []
        status: ScoreStatus = "out_of_scope"
        rows = 0
        moved = False
        if question_type in _LOCALLY_SCORED_TYPES:
            try:
                write = record_local_scores(conn, record_id=identifier, computed_at=now())
            except LifecycleError as exc:
                status = "failed"
                details.append(str(exc))
            else:
                status = write.outcome
                rows = len(write.scores)
                moved = write.event is not None
        platform_status: ScoreStatus
        platform_rows = 0
        try:
            platform = record_platform_scores(conn, record_id=identifier, computed_at=now())
        except LifecycleError as exc:
            platform_status = "failed"
            details.append(str(exc))
        else:
            platform_status = platform.outcome
            platform_rows = len(platform.scores)
            moved = moved or platform.event is not None
        results.append(
            ScoreResult(
                record_id=identifier,
                question_id=question_id,
                question_type=question_type,
                status=status,
                platform_status=platform_status,
                rows_appended=rows,
                platform_rows_appended=platform_rows,
                moved_to_scored=moved,
                detail="; ".join(details) if details else None,
            )
        )
    return tuple(results)


def _candidate_records(
    conn: sqlite3.Connection, record_id: str | None
) -> list[tuple[str, int, str]]:
    try:
        if record_id is not None:
            if (
                conn.execute(
                    "SELECT 1 FROM forecast_records WHERE record_id = ?", (record_id,)
                ).fetchone()
                is None
            ):
                # The identifier is not echoed: it is operator input that named nothing.
                raise ScoreRecordsError("record_id does not name a stored forecast record")
        rows = conn.execute(
            "SELECT f.record_id, f.question_id, f.question_type FROM forecast_records f "
            "WHERE (? IS NULL AND EXISTS (SELECT 1 FROM resolution_events r "
            "                             WHERE r.forecast_record_id = f.record_id)) "
            "   OR f.record_id = ? "
            "ORDER BY f.question_id, f.record_id",
            (record_id, record_id),
        ).fetchall()
    except sqlite3.Error:
        raise ScoreRecordsError(
            "the ledger's resolved records could not be read (detail withheld: a database "
            "message can echo stored values)"
        ) from None
    records: list[tuple[str, int, str]] = []
    for row in rows:
        identifier, question_id, question_type = row[0], row[1], row[2]
        if (
            type(identifier) is not str
            or type(question_id) is not int
            or question_type not in _SUPPORTED_TYPES
        ):
            raise ScoreRecordsError(
                "a stored forecast record has a malformed identifier (detail withheld: it can "
                "echo stored values)"
            )
        records.append((identifier, question_id, question_type))
    return records


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
