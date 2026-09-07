"""Read-only platform reconciliation before restored storage may post (LAUNCH)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from whiskeyjack_bot.artifacts import write_new_file
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.lifecycle import transaction
from whiskeyjack_bot.submission_live import MetaculusSubmissionGateway
from whiskeyjack_bot.tournament_state import (
    StorageFailure,
    TournamentError,
    append,
    canonical,
    check_storage,
    events,
    guard_root,
    utcnow,
)


def reconcile_restored(conn: sqlite3.Connection, config: AppConfig, poster: Any) -> dict[str, int]:
    """Import durable witnesses, hold unknown costs/questions, and never POST.

    The external posting guard must be retained on the execution host when restoring
    a backup. It witnesses the maximum history, independently of the backup's age.
    Reconciliation does not authorize a replay of any ambiguous write.
    """
    from whiskeyjack_bot.tournament import worker_lock

    with worker_lock(config.storage.sqlite_path.with_suffix(".worker.lock")):
        account = poster.get_current_user_id()
        active = events(conn, "activation", "account")
        if not active or active[-1]["account_id"] != account:
            raise TournamentError("restored storage account could not be verified")
        guards = sorted(guard_root(conn).glob("*.json"))
        witnesses = conn.execute(
            "SELECT count(*) FROM tournament_events WHERE kind='witness'"
        ).fetchone()[0]
        if witnesses and not guards:
            raise StorageFailure(
                "posting guard is missing; restore the original host guard before reconciling"
            )
        imported = blocked = 0
        gateway = MetaculusSubmissionGateway(poster=poster)
        for path in guards:
            try:
                envelope = json.loads(path.read_text())
                data, scope = envelope["data"], envelope["scope"]
                identifier = envelope["event_id"]
            except (OSError, ValueError, KeyError):
                raise StorageFailure("posting guard is unreadable") from None
            row = conn.execute(
                "SELECT data FROM tournament_events WHERE event_id=?", (identifier,)
            ).fetchone()
            if row is not None:
                if json.loads(row[0]) != data:
                    raise StorageFailure("posting guard disagrees with ledger")
                continue
            if "payload" in data and "question_id" in data:
                if data["account_id"] != account:
                    raise TournamentError("operation witness belongs to another bot")
                observed = gateway.observe(data["post_id"], question_id=data["question_id"])
                if observed is None:
                    raise TournamentError(
                        "platform forecast history is unreadable; restore remains blocked"
                    )
                # Absence is not proof of non-acceptance. Every restored intent blocks
                # that whole question, even if the server currently reports no history.
                append(
                    conn,
                    "restored_question_hold",
                    f"{data['project_id']}:{data['question_id']}",
                    {
                        "record_id": data["record_id"],
                        "observed_entries": len(observed.entries),
                        "account_id": account,
                    },
                )
                blocked += 1
            if "reservation_id" in data and "estimate_microusd" in data:
                known = events(conn, "cost_reserved", scope)
                if not any(e["reservation_id"] == data["reservation_id"] for e in known):
                    append(
                        conn,
                        "cost_reserved",
                        scope,
                        {k: data[k] for k in ("reservation_id", "provider", "estimate_microusd")},
                    )
            with transaction(conn):
                conn.execute(
                    "INSERT INTO tournament_events(event_id,kind,scope,data,created_at_utc) VALUES(?,?,?,?,?)",
                    (identifier, "witness", scope, canonical(data), utcnow().isoformat()),
                )
            write_new_file(
                config.storage.artifact_root / "operations" / path.name,
                path.read_bytes(),
                what="restored witness",
                on_existing="confirm_identical",
                error=StorageFailure,
            )
            imported += 1
        check_storage(conn, config.storage.artifact_root)
        append(
            conn,
            "restore_reconciled",
            "account",
            {
                "account_id": account,
                "imported": imported,
                "held_questions": blocked,
                "at": utcnow().isoformat(),
            },
        )
        return {"imported_witnesses": imported, "held_questions": blocked}
