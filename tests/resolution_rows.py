"""Resolution fixtures shared by the ledger, lifecycle, export and property suites (M4-801).

Imported by bare name (``from resolution_rows import ...``): ``tests/`` is on ``sys.path``
because of ``tests/conftest.py``, whereas ``tests.unit.*`` only resolves once some heavy
dependency has put the working directory there -- which a light module like
``test_lifecycle.py`` never imports.

``014_resolution_ingestion.sql`` constrains every ``resolution_events`` row, and a
``score_events`` row now needs a scorable resolution behind it. Suites that seed one row of
every table used to write a bare ``(question_id, forecast_record_id, ingested_at_utc)``; this
is the smallest row that satisfies the new contract, built from a real observation so the
snapshot, the indexed columns and both digests agree the way the writer makes them agree.

The post payloads are the committed ``tests/fixtures/api_posts`` files with the question's
status and resolution replaced. Values follow the shapes Metaculus documents in
``docs/openapi.yml`` (``resolution: "no"``, ``resolution: "77289125.94957079"``).

``my_forecasts.score_data`` follows what the live API returned on 2026-09-23 (M4-803): the
account's seven scores for a question it predicted that resolved to a value, and ``{}`` for
everything else -- the live annulled observation and the committed real withheld payload
(``withheld_minibench_45321.json``) both carry ``{}``. The values are synthetic.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from whiskeyjack_bot.lifecycle import (
    SubmissionAttempt,
    record_approval,
    record_submission_attempt,
    record_validation,
)
from whiskeyjack_bot.resolution import canonical_json, classify_resolution, sha256_text

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "api_posts"
OBSERVED_AT = "2026-09-17T18:00:00.000000+00:00"
SUBMITTED_AT = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
RECORD_SHA = "b" * 64
PAYLOAD_SHA = "d" * 64
_TS = "2026-09-10T00:00:00.000000+00:00"

_TYPE_FIXTURES = {
    "binary": "binary_post.json",
    "numeric": "numeric_post.json",
    "discrete": "discrete_post.json",
    "multiple_choice": "multiple_choice_post.json",
}

# `question.my_forecasts.score_data` for a question the account predicted on that resolved to a
# value. The keys are the live ones; the numbers are made up.
SCORE_DATA: dict[str, float] = {
    "baseline_score": 12.653108159476442,
    "peer_score": 2.4154150938466965,
    "coverage": 0.3078232610225677,
    "relative_legacy_score": 0.03767701797265986,
    "weighted_coverage": 0.3078232610225677,
    "spot_peer_score": 7.812678563619664,
    "spot_baseline_score": -41.105107253570395,
}

# One scorable value per supported type.
RESOLVED_VALUE = {
    "binary": "yes",
    "numeric": "77289125.94957079",
    "discrete": "3.0",
    "multiple_choice": "Option Beta",
}


def post_payload(
    question_type: str,
    *,
    post_id: int,
    question_id: int,
    status: str = "resolved",
    resolution: str | None = "__scorable__",
    actual_resolve_time: str | None = "2026-09-17T12:00:00Z",
    resolution_set_time: str | None = "2026-09-17T16:30:00.123456Z",
    score_data: object = "__auto__",
) -> dict[str, Any]:
    """A committed post fixture re-identified and re-resolved.

    ``resolution="__scorable__"`` stands for "the type's own scorable value", so callers
    that only need *a* resolved post do not repeat the table above. ``score_data="__auto__"``
    is :data:`SCORE_DATA` when the question resolved to a value and ``{}`` otherwise; pass
    anything else to plant that exact value.
    """
    with (FIXTURES / _TYPE_FIXTURES[question_type]).open(encoding="utf-8") as handle:
        post: dict[str, Any] = json.load(handle)
    post = copy.deepcopy(post)
    post["id"] = post_id
    question = post["question"]
    question["id"] = question_id
    question["post_id"] = post_id
    question["status"] = status
    question["resolution"] = (
        RESOLVED_VALUE[question_type] if resolution == "__scorable__" else resolution
    )
    question["actual_resolve_time"] = actual_resolve_time
    question["resolution_set_time"] = resolution_set_time
    if score_data == "__auto__":
        scored = status == "resolved" and question["resolution"] not in (
            None,
            "annulled",
            "ambiguous",
        )
        score_data = dict(SCORE_DATA) if scored else {}
    question["my_forecasts"]["score_data"] = score_data
    return post


def kind_payload(
    question_type: str, kind: str, *, post_id: int, question_id: int
) -> dict[str, Any]:
    """A post whose classification is ``kind``."""
    if kind == "resolved":
        return post_payload(question_type, post_id=post_id, question_id=question_id)
    if kind in ("annulled", "ambiguous"):
        return post_payload(
            question_type, post_id=post_id, question_id=question_id, resolution=kind
        )
    if kind == "withheld":
        return post_payload(
            question_type, post_id=post_id, question_id=question_id, resolution=None
        )
    if kind == "unresolved":
        return post_payload(
            question_type,
            post_id=post_id,
            question_id=question_id,
            status="closed",
            resolution=None,
            actual_resolve_time=None,
            resolution_set_time=None,
        )
    raise ValueError(kind)


def resolution_columns(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    kind: str = "resolved",
    observed_at_utc: str = OBSERVED_AT,
    payload: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Every column of a valid resolution row for ``record_id``, ready for a raw INSERT."""
    question_id, post_id, question_type = conn.execute(
        "SELECT question_id, post_id, question_type FROM forecast_records WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    source = (
        payload
        if payload is not None
        else kind_payload(question_type, kind, post_id=post_id, question_id=question_id)
    )
    observation = classify_resolution(source, question_id=question_id, question_type=question_type)
    snapshot = observation.snapshot_json()
    source_text = canonical_json(source)
    return {
        "question_id": question_id,
        "forecast_record_id": record_id,
        "resolution_snapshot_json": snapshot,
        "outcome": observation.outcome,
        "annulled": 1 if observation.kind == "annulled" else 0,
        "ambiguous": 1 if observation.kind == "ambiguous" else 0,
        "source_response": source_text,
        "ingested_at_utc": observed_at_utc,
        "post_id": post_id,
        "question_type": question_type,
        "resolution_kind": observation.kind,
        "scorable": 1 if observation.scorable else 0,
        "observation_sha256": sha256_text(snapshot),
        "source_response_sha256": sha256_text(source_text),
        "observed_at_utc": observed_at_utc,
    }


def insert_resolution_row(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    kind: str = "resolved",
    observed_at_utc: str = OBSERVED_AT,
    **overrides: object,
) -> int:
    """Raw-INSERT a resolution row that satisfies 014, with any column overridden."""
    columns = resolution_columns(conn, record_id, kind=kind, observed_at_utc=observed_at_utc)
    columns.update(overrides)
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO resolution_events ({names}) VALUES ({placeholders})",
        tuple(columns.values()),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def seed_record(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    question_id: int,
    post_id: int | None,
    question_type: str = "binary",
    tournament_id: str = "33122",
) -> str:
    """A draft forecast record, raw, with the run it cites. Returns ``record_id``."""
    conn.execute(
        "INSERT OR IGNORE INTO research_runs (retrieval_run_id, provider, question_id, "
        "started_at_utc, created_at_utc) VALUES ('run-resolution', 'asknews', ?, ?, ?)",
        (question_id, _TS, _TS),
    )
    conn.execute(
        "INSERT INTO forecast_records (record_id, question_id, post_id, tournament_id, "
        "forecast_version, question_type, status, model_provider, model_name, prompt_version, "
        "prompt_sha256, retrieval_run_id, generated_at_utc, final_prediction_json, record_json, "
        "created_at_utc, forecast_sha256, attempt_id) VALUES (?, ?, ?, ?, 1, ?, 'draft', "
        "'openrouter', 'model', 'v1', 'abc', 'run-resolution', ?, '{}', '{}', ?, ?, ?)",
        (
            record_id,
            question_id,
            post_id,
            tournament_id,
            question_type,
            _TS,
            _TS,
            RECORD_SHA,
            f"att-{record_id}",
        ),
    )
    return record_id


def walk_to_submitted(conn: sqlite3.Connection, record_id: str) -> None:
    """Carry a draft to ``submitted`` through the production writers."""
    record_validation(conn, record_id=record_id, occurred_at=SUBMITTED_AT)
    record_approval(
        conn,
        record_id=record_id,
        decision="approved",
        actor="policy:test",
        forecast_sha256=RECORD_SHA,
        payload_sha256=PAYLOAD_SHA,
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
            request_payload_sha256=PAYLOAD_SHA,
            success=True,
            refetch_outcome="confirmed",
        ),
        occurred_at=SUBMITTED_AT + timedelta(minutes=2),
        secret_env_var_names=(),
    )


def seed_submitted(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    question_id: int,
    post_id: int | None,
    question_type: str = "binary",
) -> str:
    seed_record(
        conn, record_id, question_id=question_id, post_id=post_id, question_type=question_type
    )
    walk_to_submitted(conn, record_id)
    return record_id


def walk_to_submitted_raw(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    forecast_sha256: str = RECORD_SHA,
    payload_sha256: str = PAYLOAD_SHA,
) -> None:
    """:func:`walk_to_submitted`'s ledger, written by raw INSERTs.

    For a ledger *below* the current schema. This build's writers name every column the
    current schema has, so they cannot write to a ledger a previous build produced -- since
    M2-713's 016, `lifecycle._append_event` names `submission_reconciliation_id`, which a v14
    ledger does not have. `test_lifecycle.py`'s `_seed_v8_ledger` is raw for the same reason.
    """
    ts = _TS
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, from_status, "
        "to_status, occurred_at_utc, created_at_utc) "
        "VALUES (?, 1, 'validated', 'draft', 'validated', ?, ?)",
        (record_id, ts, ts),
    )
    approval_id = conn.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "created_at_utc, payload_sha256) VALUES (?, 'approved', 'policy:test', ?, ?, ?)",
        (record_id, forecast_sha256, ts, payload_sha256),
    ).lastrowid
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, from_status, "
        "to_status, approval_event_id, occurred_at_utc, created_at_utc) "
        "VALUES (?, 2, 'approved', 'validated', 'approved', ?, ?, ?)",
        (record_id, approval_id, ts, ts),
    )
    conn.execute(
        "INSERT INTO submission_attempts (attempt_id, forecast_record_id, idempotency_key, "
        "requested_at_utc, completed_at_utc, request_payload_sha256, success, "
        "verified_by_refetch, refetch_outcome, created_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 1, 'confirmed', ?)",
        (f"attempt-{record_id}", record_id, f"key-{record_id}", ts, ts, payload_sha256, ts),
    )
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, from_status, "
        "to_status, submission_attempt_id, occurred_at_utc, created_at_utc) "
        "VALUES (?, 3, 'submitted', 'approved', 'submitted', ?, ?, ?)",
        (record_id, f"attempt-{record_id}", ts, ts),
    )


def seed_submitted_raw(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    question_id: int,
    post_id: int | None,
    question_type: str = "binary",
) -> str:
    """:func:`seed_submitted`, raw throughout; see :func:`walk_to_submitted_raw`."""
    seed_record(
        conn, record_id, question_id=question_id, post_id=post_id, question_type=question_type
    )
    walk_to_submitted_raw(conn, record_id)
    return record_id


def resolve_raw(
    conn: sqlite3.Connection, record_id: str, *, observed_at_utc: str = OBSERVED_AT
) -> int:
    """A scorable resolution row and its ``resolved`` event, raw; see :func:`walk_to_submitted_raw`."""
    resolution_id = insert_resolution_row(conn, record_id, observed_at_utc=observed_at_utc)
    seq = conn.execute(
        "SELECT max(event_seq) FROM lifecycle_events WHERE forecast_record_id = ?", (record_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, from_status, "
        "to_status, resolution_event_id, occurred_at_utc, created_at_utc) "
        "VALUES (?, ?, 'resolved', 'submitted', 'resolved', ?, ?, ?)",
        (record_id, seq + 1, resolution_id, observed_at_utc, observed_at_utc),
    )
    return resolution_id


_LOCAL_BRIER = {"binary": "local_brier_binary", "multiple_choice": "local_brier_multiclass"}


def insert_score_row(conn: sqlite3.Connection, record_id: str, **overrides: object) -> int:
    """Raw-INSERT a local score row that satisfies 014 and 015, with any column overridden.

    ``015_local_score_events.sql`` requires the row to cite the record's latest resolution
    row, a local metric for the record's question type, a version named for that metric, no
    comparison baseline and a computation time no earlier than the observation. Suites that
    seed several rows for one record vary ``implementation_version`` (``local_brier_binary/2``
    and so on), because the UNIQUE index allows one value per observation, metric and version.
    """
    question_type = conn.execute(
        "SELECT question_type FROM forecast_records WHERE record_id = ?", (record_id,)
    ).fetchone()[0]
    latest = conn.execute(
        "SELECT max(event_id), max(observed_at_utc) FROM resolution_events "
        "WHERE forecast_record_id = ?",
        (record_id,),
    ).fetchone()
    metric = _LOCAL_BRIER.get(question_type, "local_brier_binary")
    columns: dict[str, object] = {
        "forecast_record_id": record_id,
        "metric": metric,
        "value": 0.25,
        "implementation_version": f"{metric}/1",
        "computed_at_utc": latest[1] if latest[1] is not None else OBSERVED_AT,
        "resolution_event_id": latest[0],
    }
    columns.update(overrides)
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO score_events ({names}) VALUES ({placeholders})", tuple(columns.values())
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid
