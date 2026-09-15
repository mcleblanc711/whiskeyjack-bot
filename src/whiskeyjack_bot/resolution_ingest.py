"""Fetch resolution state for submitted forecasts and append it to the ledger (M4-801).

``whiskeyjack-bot ingest-resolutions`` is this module's only caller. It reads Metaculus and
writes the ledger; it never posts, never reads a submission flag, and makes no paid call.

**Which records.** Every forecast record whose lifecycle history reached ``submitted`` --
including records already ``resolved`` or ``scored``, because the platform can re-resolve or
retract after the record has moved. A record that was never posted is out of scope, and not
only for tidiness: Metaculus returns a resolution value only for a question the account
predicted on (``docs/openapi.yml``), so polling one would record ``withheld`` forever.

**One GET per post.** Records are grouped by ``post_id``, so a group post whose subquestions
were each forecast is fetched once, and every record reads the same payload observed at the
same instant. The fetch goes through the pinned SDK's ``get_question_by_post_id`` (D03),
whose read retry is kept as shipped -- a GET is idempotent.

**Failures are per question.** A post that cannot be fetched or classified, and a record the
ledger refuses, are reported and skipped; the rest of the run proceeds. ``ingest-resolutions``
exits non-zero if anything was skipped, so a scheduled run cannot fail quietly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from forecasting_tools.helpers.metaculus_client import MetaculusClient

from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    ResolutionWriteOutcome,
    record_resolution_observation,
)
from whiskeyjack_bot.resolution import ResolutionKind

# post_id -> the raw post payload, exactly as the API returned it.
FetchPost = Callable[[int], object]

IngestStatus = Literal["appended", "unchanged", "nothing_to_retract", "failed"]


class ResolutionFetchError(Exception):
    """A Metaculus post could not be fetched, or came back in a shape with no payload.

    The message never carries the SDK's exception text: ``requests`` and the SDK both put
    URLs, response bodies and headers into theirs.
    """


class ResolutionIngestError(Exception):
    """The set of records to resolve could not be read from the ledger."""


@dataclass(frozen=True)
class IngestResult:
    """What happened to one forecast record in one run.

    ``detail`` is a message from this package's own sanitized error types, set only when
    ``status`` is ``failed``; it names rules and fields, never a payload value.
    """

    record_id: str
    question_id: int
    post_id: int | None
    status: IngestStatus
    kind: ResolutionKind | None = None
    scorable: bool | None = None
    moved_to_resolved: bool = False
    detail: str | None = None


def ingest_resolutions(
    conn: sqlite3.Connection,
    fetch_post: FetchPost,
    *,
    question_id: int | None = None,
    clock: Callable[[], datetime] | None = None,
) -> tuple[IngestResult, ...]:
    """Fetch and record the resolution state of every submitted record, or of one question."""
    if question_id is not None and (type(question_id) is not int or question_id < 1):
        raise ResolutionIngestError("question_id must be a positive integer")
    now = clock if clock is not None else _utcnow
    records = _submitted_records(conn, question_id)

    by_post: dict[int, list[tuple[str, int]]] = {}
    results: list[IngestResult] = []
    for record_id, record_question_id, post_id in records:
        if post_id is None:
            results.append(
                IngestResult(
                    record_id=record_id,
                    question_id=record_question_id,
                    post_id=None,
                    status="failed",
                    detail="the forecast record has no post_id to fetch",
                )
            )
            continue
        by_post.setdefault(post_id, []).append((record_id, record_question_id))

    for post_id, members in by_post.items():
        try:
            payload = fetch_post(post_id)
        except ResolutionFetchError as exc:
            for record_id, record_question_id in members:
                results.append(
                    IngestResult(
                        record_id=record_id,
                        question_id=record_question_id,
                        post_id=post_id,
                        status="failed",
                        detail=str(exc),
                    )
                )
            continue
        observed_at = now()
        for record_id, record_question_id in members:
            results.append(
                _record_one(conn, record_id, record_question_id, post_id, payload, observed_at)
            )
    return tuple(results)


def sdk_post_fetcher(client: MetaculusClient) -> FetchPost:
    """Adapt the pinned SDK's ``get_question_by_post_id`` into a :data:`FetchPost`.

    ``unpack_subquestions`` so a group post returns rather than raising; every question the
    SDK builds from one post carries a deep copy of that post as ``api_json``, including the
    full ``group_of_questions``, so the first one is the whole payload and the classifier
    selects the subquestion by id. The SDK's parse runs, and a payload it cannot parse is a
    fetch failure: that is the same payload the forecasting pipeline would have refused.
    """

    def fetch(post_id: int) -> object:
        try:
            fetched = client.get_question_by_post_id(
                post_id, group_question_mode="unpack_subquestions"
            )
        except Exception:
            raise ResolutionFetchError(
                "the Metaculus post could not be fetched or parsed (detail withheld: the "
                "SDK's message can carry URLs and response content)"
            ) from None
        question = fetched[0] if isinstance(fetched, list) and fetched else fetched
        payload = getattr(question, "api_json", None)
        if type(payload) is not dict or not payload:
            raise ResolutionFetchError("the Metaculus post came back with no payload")
        return payload

    return fetch


def _record_one(
    conn: sqlite3.Connection,
    record_id: str,
    question_id: int,
    post_id: int,
    payload: object,
    observed_at: datetime,
) -> IngestResult:
    try:
        write = record_resolution_observation(
            conn, record_id=record_id, source_response=payload, observed_at=observed_at
        )
    except LifecycleError as exc:
        return IngestResult(
            record_id=record_id,
            question_id=question_id,
            post_id=post_id,
            status="failed",
            detail=str(exc),
        )
    outcome: ResolutionWriteOutcome = write.outcome
    stored = write.stored
    return IngestResult(
        record_id=record_id,
        question_id=question_id,
        post_id=post_id,
        status=outcome,
        kind=None if stored is None else stored.kind,
        scorable=None if stored is None else stored.scorable,
        moved_to_resolved=write.event is not None,
    )


def _submitted_records(
    conn: sqlite3.Connection, question_id: int | None
) -> list[tuple[str, int, int | None]]:
    try:
        rows = conn.execute(
            "SELECT f.record_id, f.question_id, f.post_id FROM forecast_records f "
            "WHERE EXISTS (SELECT 1 FROM lifecycle_events e "
            "              WHERE e.forecast_record_id = f.record_id "
            "                AND e.to_status = 'submitted') "
            "  AND (? IS NULL OR f.question_id = ?) "
            "ORDER BY f.post_id, f.question_id, f.record_id",
            (question_id, question_id),
        ).fetchall()
    except sqlite3.Error:
        raise ResolutionIngestError(
            "the ledger's submitted records could not be read (detail withheld: a database "
            "message can echo stored values)"
        ) from None
    records: list[tuple[str, int, int | None]] = []
    for row in rows:
        record_id, record_question_id, post_id = row[0], row[1], row[2]
        if (
            type(record_id) is not str
            or type(record_question_id) is not int
            or (post_id is not None and type(post_id) is not int)
        ):
            raise ResolutionIngestError(
                "a stored forecast record has a malformed identifier (detail withheld: it can "
                "echo stored values)"
            )
        records.append((record_id, record_question_id, post_id))
    return records


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
