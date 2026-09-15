"""Scorable forecast records shared by the score ledger, CLI and property suites (M4-802).

Imported by bare name (``from score_rows import ...``), like ``resolution_rows``. Unlike that
module's ``seed_record``, which writes ``'{}'`` for ``record_json`` and so suits raw trigger
tests only, every record here is a **real** ``ForecastRecord``: ``lifecycle.record_local_scores``
reads the forecast back through ``read_forecast_record``, which refuses a placeholder.

A record is carried to ``submitted`` through the production writers with its own stored
``forecast_sha256``, then resolved with ``record_resolution_observation`` from a committed
post fixture. The multiple-choice fixture's labels are ``Option Alpha``, ``Option Beta`` and
``Other``, and it resolves ``Option Beta`` unless told otherwise.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from whiskeyjack_bot.forecast.generate import ForecastGeneration, ModelSettings
from whiskeyjack_bot.forecast.inputs import SourceReference
from whiskeyjack_bot.forecast.record import ForecastRecord, build_forecast_record_draft
from whiskeyjack_bot.forecast.schema import response_model_for, validate_forecast_response
from whiskeyjack_bot.forecast.store import _projection
from whiskeyjack_bot.lifecycle import (
    ResolutionWrite,
    SubmissionAttempt,
    record_approval,
    record_resolution_observation,
    record_submission_attempt,
    record_validation,
)
from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalMultipleChoiceQuestion,
)

from resolution_rows import post_payload, resolve_raw, walk_to_submitted_raw

# Self-contained rather than importing `tests.unit.records`: that package resolves only once
# the SDK's import has put the working directory on sys.path, a side effect a helper should
# not lean on. The record is still built by the real draft assembler and written through
# `store._projection`, so its columns cannot drift from what `read_forecast_record` checks.
_PROMPT = (Path(__file__).resolve().parents[1] / "prompts" / "forecaster.md").read_text(
    encoding="utf-8"
)
_GENERATED_AT = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

MC_LABELS = ("Option Alpha", "Option Beta", "Other")
SUBMITTED_AT = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
RESOLVED_AT = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)
SCORED_AT = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
# For a command that stamps `computed_at` with the real clock: an observation that is already
# in the past whenever the suite runs, and still after SUBMITTED_AT.
PAST_OBSERVATION = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
_CREATED = "2026-09-10T00:00:00.000000+00:00"


def _json_block(heading: str) -> str:
    body = _PROMPT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _generation(forecast: Any) -> ForecastGeneration:
    return ForecastGeneration(
        forecast=forecast,
        settings=ModelSettings(
            provider="openrouter",
            name="openrouter/test-model",
            temperature=0.1,
            max_output_tokens=2048,
            timeout_seconds=60.0,
            allowed_tries=2,
            prompt_version="1.1.0",
            prompt_sha256="b" * 64,
        ),
        sources=tuple(
            SourceReference(
                source_id=source_id,
                document_id=None,
                canonical_url=f"https://example.test/{source_id}",
                content_sha256="c" * 64,
            )
            for source_id in ("src-001", "src-002")
        ),
        request="the rendered reasoning packet",
        raw_responses=("{}",),
        invocations=1,
        repair_attempted=False,
        cost_usd=None,
        failure_code=None,
        failure_problems=(),
    )


def _response(question_type: str, question_id: int, final_prediction: dict[str, Any]) -> Any:
    heading = "Binary schema" if question_type == "binary" else "Multiple-choice schema"
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block(heading) + "}"),
    }
    payload["question_id"] = question_id
    if question_type != "binary":
        payload["model_prior"] = None
        payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    payload["final_prediction"] = final_prediction
    return validate_forecast_response(payload, response_model_for(question_type))


def seed_forecast(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    question_id: int,
    post_id: int,
    question_type: str = "binary",
    probability_yes: float = 0.7,
    options: tuple[tuple[str, float], ...] = (
        ("Option Alpha", 0.2),
        ("Option Beta", 0.5),
        ("Other", 0.3),
    ),
) -> str:
    """Store a real draft record and return its ``forecast_sha256``."""
    if question_type == "binary":
        question: Any = CanonicalBinaryQuestion(
            question_id=question_id, post_id=post_id, title="Will the thing happen?"
        )
        forecast = _response("binary", question_id, {"probability_yes": probability_yes})
    else:
        question = CanonicalMultipleChoiceQuestion(
            question_id=question_id,
            post_id=post_id,
            title="Which one happens?",
            options=[label for label, _ in options],
        )
        forecast = _response(
            "multiple_choice",
            question_id,
            {"options": [{"option": label, "probability": p} for label, p in options]},
        )
    conn.execute(
        "INSERT OR IGNORE INTO research_runs (retrieval_run_id, provider, question_id, "
        "started_at_utc, created_at_utc) VALUES ('run-score', 'asknews', ?, ?, ?)",
        (question_id, _CREATED, _CREATED),
    )
    draft = build_forecast_record_draft(
        question=question,
        generation=_generation(forecast),
        tournament_id="minibench",
        attempt_id=f"att-{record_id}",
        retrieval_run_id="run-score",
        research_packet_sha256="d" * 64,
        generated_at=_GENERATED_AT,
    )
    record = ForecastRecord(
        **draft.model_dump(), record_id=record_id, forecast_version=1, parent_record_id=None
    )
    projected = _projection(record)
    columns = ", ".join((*projected, "status", "created_at_utc"))
    placeholders = ", ".join("?" for _ in range(len(projected) + 2))
    conn.execute(
        f"INSERT INTO forecast_records ({columns}) VALUES ({placeholders})",
        (*projected.values(), "draft", _CREATED),
    )
    return str(projected["forecast_sha256"])


def walk_to_submitted(conn: sqlite3.Connection, record_id: str, forecast_sha256: str) -> None:
    record_validation(conn, record_id=record_id, occurred_at=SUBMITTED_AT)
    record_approval(
        conn,
        record_id=record_id,
        decision="approved",
        actor="policy:test",
        forecast_sha256=forecast_sha256,
        payload_sha256="d" * 64,
        occurred_at=SUBMITTED_AT + timedelta(minutes=1),
    )
    record_submission_attempt(
        conn,
        record_id=record_id,
        attempt=SubmissionAttempt(
            attempt_id=f"attempt-{record_id}",
            idempotency_key=f"key-{record_id}",
            requested_at_utc=SUBMITTED_AT + timedelta(minutes=2),
            completed_at_utc=SUBMITTED_AT + timedelta(minutes=2),
            request_payload_sha256="d" * 64,
            success=True,
            refetch_outcome="confirmed",
        ),
        occurred_at=SUBMITTED_AT + timedelta(minutes=2),
        secret_env_var_names=(),
    )


def resolve(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    resolution: str | None = "__scorable__",
    status: str = "resolved",
    observed_at: datetime = RESOLVED_AT,
) -> ResolutionWrite:
    """Record one observation of the record's question through the production writer."""
    question_id, post_id, question_type = conn.execute(
        "SELECT question_id, post_id, question_type FROM forecast_records WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    payload = post_payload(
        question_type,
        post_id=post_id,
        question_id=question_id,
        status=status,
        resolution=resolution,
        actual_resolve_time=None if status != "resolved" else "2026-09-17T12:00:00Z",
        resolution_set_time=None if status != "resolved" else "2026-09-17T16:30:00.123456Z",
    )
    return record_resolution_observation(
        conn, record_id=record_id, source_response=payload, observed_at=observed_at
    )


def seed_resolved(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    question_id: int,
    post_id: int,
    question_type: str = "binary",
    resolution: str | None = "__scorable__",
    observed_at: datetime = RESOLVED_AT,
    **forecast: Any,
) -> str:
    """A real record, posted and resolved. Returns ``record_id``."""
    digest = seed_forecast(
        conn,
        record_id,
        question_id=question_id,
        post_id=post_id,
        question_type=question_type,
        **forecast,
    )
    walk_to_submitted(conn, record_id, digest)
    resolve(conn, record_id, resolution=resolution, observed_at=observed_at)
    return record_id


def seed_resolved_raw(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    question_id: int,
    post_id: int,
    question_type: str = "binary",
) -> str:
    """:func:`seed_resolved` for a ledger below the current schema: a real record, then raw rows.

    The record is still the real draft (``seed_forecast`` writes it raw already), so a scorer
    run after the upgrade reads a genuine forecast. The events are raw because this build's
    writers cannot write to an older schema; see ``resolution_rows.walk_to_submitted_raw``.
    """
    digest = seed_forecast(
        conn, record_id, question_id=question_id, post_id=post_id, question_type=question_type
    )
    walk_to_submitted_raw(conn, record_id, forecast_sha256=digest, payload_sha256="d" * 64)
    resolve_raw(conn, record_id)
    return record_id


__all__ = [
    "MC_LABELS",
    "PAST_OBSERVATION",
    "RESOLVED_AT",
    "SCORED_AT",
    "resolve",
    "seed_forecast",
    "seed_resolved",
    "walk_to_submitted",
]
