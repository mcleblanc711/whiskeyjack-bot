"""Raw rows for an unrecorded live post and its reconciliation (M2-713).

Imported by bare name (``from reconciliation_rows import ...``), like ``resolution_rows``:
``tests/`` is on ``sys.path`` through ``tests/conftest.py``.

Raw SQL rather than the production writers, for the reason every schema-level suite gives:
the ledger suites probe ``016_submission_reconciliations.sql``'s triggers directly, and the
export and append-only suites need one row in every table without depending on the writer
under test. The rows are the ones the live path leaves behind -- a record walked to
``approved``, its key reservation, the ``forecast_intent`` journal row ``submission_policy``
commits before the POST -- and nothing else, which is the unrecorded-post state itself.

The attempt id is the one ``submission_live.live_attempt_id`` produces for the key, not a
string shaped like one, so a change to the minter shows up here rather than being papered over.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from whiskeyjack_bot.submission_live import live_attempt_id

RECONCILED_AT = "2026-09-15T12:00:00.000000+00:00"
RESERVED_AT = "2026-09-15T11:00:00.000000+00:00"
CONFIRMING_SNAPSHOT = json.dumps(
    {"outcome": "confirmed", "question_type": "binary", "expected_values": [0.35]},
    sort_keys=True,
    separators=(",", ":"),
)


@dataclass(frozen=True)
class Unrecorded:
    record_id: str
    reservation_id: str
    idempotency_key: str
    attempt_id: str
    intent_event_id: str
    payload_sha256: str


def seed_unrecorded_post(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    question_id: int,
    run_id: str,
    forecast_sha256: str,
    payload_sha256: str,
    tournament_id: str = "minibench",
    ts: str = RESERVED_AT,
) -> Unrecorded:
    """A record at ``approved``, its standing reservation and its submission intent. Raw.

    ``run_id`` must already name a ``research_runs`` row: each suite seeds its own.
    ``question_id`` must not be shared with a record that holds an attempt or a reservation,
    or ``012``'s whole-question guard refuses the reservation.
    """
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, post_id, tournament_id, forecast_version, question_type, "
        "status, model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
        "forecast_sha256, attempt_id) "
        "VALUES (?, ?, ?, ?, 1, 'binary', 'draft', 'anthropic', 'claude', 'v1', 'abc', ?, ?, "
        "'{}', '{}', ?, ?, ?)",
        (
            record_id,
            question_id,
            question_id + 1000,
            tournament_id,
            run_id,
            ts,
            ts,
            forecast_sha256,
            f"att-{record_id}",
        ),
    )
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, from_status, "
        "to_status, occurred_at_utc, created_at_utc) "
        "VALUES (?, 1, 'validated', 'draft', 'validated', ?, ?)",
        (record_id, ts, ts),
    )
    approval_id = conn.execute(
        "INSERT INTO approval_events (forecast_record_id, decision, actor, forecast_sha256, "
        "created_at_utc, payload_sha256) VALUES (?, 'approved', 'chris', ?, ?, ?)",
        (record_id, forecast_sha256, ts, payload_sha256),
    ).lastrowid
    conn.execute(
        "INSERT INTO lifecycle_events (forecast_record_id, event_seq, event_type, from_status, "
        "to_status, approval_event_id, occurred_at_utc, created_at_utc) "
        "VALUES (?, 2, 'approved', 'validated', 'approved', ?, ?, ?)",
        (record_id, approval_id, ts, ts),
    )
    key = f"wjsub-1-{record_id}"
    reservation_id = f"wjres-{record_id}"
    conn.execute(
        "INSERT INTO submission_key_reservations (reservation_id, idempotency_key, "
        "forecast_record_id, reservation_seq, reserved_at_utc, created_at_utc) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (reservation_id, key, record_id, ts, ts),
    )
    intent_event_id = f"tev-intent-{record_id}"
    conn.execute(
        "INSERT INTO tournament_events (event_id, kind, scope, data, created_at_utc) "
        "VALUES (?, 'forecast_intent', ?, ?, ?)",
        (
            intent_event_id,
            record_id,
            json.dumps(
                {
                    "record_id": record_id,
                    "account_id": 42,
                    "project_id": tournament_id,
                    "question_id": question_id,
                    "post_id": question_id + 1000,
                    "payload": {"question_type": "binary", "probability_yes": 0.35},
                    "payload_sha256": payload_sha256,
                    "baseline": [],
                },
                sort_keys=True,
            ),
            ts,
        ),
    )
    return Unrecorded(
        record_id=record_id,
        reservation_id=reservation_id,
        idempotency_key=key,
        attempt_id=live_attempt_id(key),
        intent_event_id=intent_event_id,
        payload_sha256=payload_sha256,
    )


def reconciliation_columns(post: Unrecorded, **overrides: object) -> dict[str, object]:
    """Every column of a valid reconciliation row for ``post``, with any column overridden."""
    columns: dict[str, object] = {
        "reconciliation_id": f"wjrec-{post.record_id}",
        "reservation_id": post.reservation_id,
        "forecast_record_id": post.record_id,
        "attempt_id": post.attempt_id,
        "request_payload_sha256": post.payload_sha256,
        "intent_event_id": post.intent_event_id,
        "artifact_path": None,
        "artifact_sha256": None,
        "observed_by": "chris",
        "note": "saw 35% on the question page",
        "refetched_at_utc": RECONCILED_AT,
        "refetched_forecast_snapshot": CONFIRMING_SNAPSHOT,
        "created_at_utc": RECONCILED_AT,
    }
    columns.update(overrides)
    return columns


def insert_reconciliation(conn: sqlite3.Connection, post: Unrecorded, **overrides: object) -> str:
    """Raw-INSERT a reconciliation row for ``post``; returns its id."""
    columns = reconciliation_columns(post, **overrides)
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO submission_reconciliations ({names}) VALUES ({placeholders})",
        tuple(columns.values()),
    )
    return str(columns["reconciliation_id"])


def insert_reconciled_event(
    conn: sqlite3.Connection, record_id: str, reconciliation_id: str, **overrides: object
) -> None:
    """Raw-INSERT the ``submission_confirmed`` event citing a reconciliation, seq 3."""
    columns: dict[str, object] = {
        "forecast_record_id": record_id,
        "event_seq": 3,
        "event_type": "submission_confirmed",
        "from_status": "approved",
        "to_status": "submitted",
        "submission_reconciliation_id": reconciliation_id,
        "occurred_at_utc": RECONCILED_AT,
        "created_at_utc": RECONCILED_AT,
    }
    columns.update(overrides)
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO lifecycle_events ({names}) VALUES ({placeholders})",
        tuple(columns.values()),
    )


def seed_reconciled_post(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    question_id: int,
    run_id: str,
    forecast_sha256: str,
    payload_sha256: str,
) -> Unrecorded:
    """:func:`seed_unrecorded_post`, reconciled: the row and its event. Raw."""
    post = seed_unrecorded_post(
        conn,
        record_id,
        question_id=question_id,
        run_id=run_id,
        forecast_sha256=forecast_sha256,
        payload_sha256=payload_sha256,
    )
    reconciliation_id = insert_reconciliation(conn, post)
    insert_reconciled_event(conn, record_id, reconciliation_id)
    return post
