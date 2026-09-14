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
"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

from whiskeyjack_bot.resolution import canonical_json, classify_resolution, sha256_text

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "api_posts"
OBSERVED_AT = "2026-09-17T18:00:00.000000+00:00"

_TYPE_FIXTURES = {
    "binary": "binary_post.json",
    "numeric": "numeric_post.json",
    "discrete": "discrete_post.json",
    "multiple_choice": "multiple_choice_post.json",
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
) -> dict[str, Any]:
    """A committed post fixture re-identified and re-resolved.

    ``resolution="__scorable__"`` stands for "the type's own scorable value", so callers
    that only need *a* resolved post do not repeat the table above.
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
