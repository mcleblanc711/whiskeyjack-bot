"""Durable activation, operation journal, and prepaid round limits (LAUNCH).

One ledger belongs to one bot account. Unknown charges retain their full reservation.
Journal files intentionally survive SQLite restore; a mismatch blocks further writes
until the operator reconciles the restored database against the platform.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from whiskeyjack_bot.artifacts import write_new_file
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.lifecycle import LifecycleError, transaction
from whiskeyjack_bot.notify import budget_level_crossed, emit


class TournamentError(Exception):
    """A safe refusal, with external content withheld."""


class ActivationInactive(TournamentError):
    """An ordinary disabled or out-of-window activation, not invalid storage."""


class StorageFailure(TournamentError):
    """Stop the worker; continuing could lose evidence or spend."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical(data: Any) -> str:
    return json.dumps(
        data, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(data: Any) -> str:
    return hashlib.sha256(canonical(data).encode()).hexdigest()


def bindings(config: AppConfig) -> dict[str, str]:
    try:
        prompt = config.forecast.prompt_path.read_bytes()
    except OSError:
        raise StorageFailure("cannot read activation prompt") from None
    return {
        "config_sha256": digest(config.model_dump(mode="json")),
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
    }


def events(conn: sqlite3.Connection, kind: str, scope: str) -> list[dict[str, Any]]:
    try:
        return [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT data FROM tournament_events WHERE kind=? AND scope=? ORDER BY seq",
                (kind, scope),
            )
        ]
    except (sqlite3.Error, ValueError):
        raise StorageFailure("cannot read tournament journal") from None


def append(conn: sqlite3.Connection, kind: str, scope: str, data: dict[str, Any]) -> str:
    identifier = uuid4().hex
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO tournament_events(event_id,kind,scope,data,created_at_utc) "
                "VALUES(?,?,?,?,?)",
                (identifier, kind, scope, canonical(data), utcnow().isoformat()),
            )
    except (sqlite3.Error, LifecycleError):
        raise StorageFailure("cannot commit tournament journal") from None
    return identifier


def guard_root(conn: sqlite3.Connection) -> Path:
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2]).parent / ".posting-guard"


def check_storage(conn: sqlite3.Connection, root: Path) -> None:
    """Compare durable spend/write witnesses against restored SQLite state."""
    try:
        for path in [*(root / "operations").glob("*.json"), *guard_root(conn).glob("*.json")]:
            envelope = json.loads(path.read_text())
            row = conn.execute(
                "SELECT data FROM tournament_events WHERE event_id=?", (envelope["event_id"],)
            ).fetchone()
            if row is None or json.loads(row[0]) != envelope["data"]:
                raise StorageFailure("storage restore detected; platform reconciliation required")
        for row in conn.execute("SELECT event_id,data FROM tournament_events WHERE kind='witness'"):
            path = root / "operations" / f"{row[0]}.json"
            if not path.is_file() or not (guard_root(conn) / path.name).is_file():
                raise StorageFailure("operation artifact missing; platform reconciliation required")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        raise StorageFailure("cannot verify operation journal and artifacts") from None


def witness(conn: sqlite3.Connection, root: Path, scope: str, data: dict[str, Any]) -> str:
    identifier = append(conn, "witness", scope, data)
    envelope = canonical(
        {"schema_version": "1.1.0", "event_id": identifier, "scope": scope, "data": data}
    ).encode()
    for destination in (
        root / "operations" / f"{identifier}.json",
        guard_root(conn) / f"{identifier}.json",
    ):
        write_new_file(destination, envelope, what="operation witness", error=StorageFailure)
    return identifier


def enable(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    account_id: int,
    project_id: int,
    starts: datetime,
    ends: datetime,
    budget_usd: float = 20.0,
) -> str:
    now = utcnow()
    if (
        type(account_id) is not int
        or account_id <= 0
        or type(project_id) is not int
        or project_id <= 0
        or starts.tzinfo is None
        or ends.tzinfo is None
        or starts >= ends
        or ends <= now
        or not 0 < budget_usd <= 20
    ):
        raise TournamentError("invalid activation identity, window, or budget (maximum USD 20)")
    if config.metaculus.tournament.use_sdk_current_id or str(config.metaculus.tournament.id) != str(
        project_id
    ):
        raise TournamentError("activation requires the concrete configured project ID")
    if config.environment != "production" and project_id != 32977:
        raise TournamentError("testing profiles may activate only project 32977")
    prior = events(conn, "activation", "account")
    if any(a["account_id"] != account_id for a in prior):
        raise TournamentError("this ledger is already bound to another bot account")
    check_storage(conn, config.storage.artifact_root)
    data = {
        "activation_id": uuid4().hex,
        "account_id": account_id,
        "project_id": project_id,
        "starts": starts.isoformat(),
        "ends": ends.isoformat(),
        "budget_microusd": math.floor(budget_usd * 1_000_000),
        **bindings(config),
    }
    append(conn, "activation", "account", data)
    return str(data["activation_id"])


def disable(conn: sqlite3.Connection) -> None:
    active = events(conn, "activation", "account")
    if active:
        append(conn, "disabled", active[-1]["activation_id"], {})


def require_activation(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    account_id: int,
    project_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    active = events(conn, "activation", "account")
    check_storage(conn, config.storage.artifact_root)
    if not active:
        raise ActivationInactive("tournament activation is disabled")
    data = active[-1]
    instant = now or utcnow()
    if (
        data["account_id"] != account_id
        or str(data["project_id"]) != project_id
        or str(config.metaculus.tournament.id) != project_id
        or config.metaculus.tournament.use_sdk_current_id
        or (config.environment != "production" and project_id != "32977")
        or any(data[k] != v for k, v in bindings(config).items())
    ):
        raise TournamentError("activation account, destination, configuration, or prompt changed")
    if events(conn, "disabled", data["activation_id"]) or not datetime.fromisoformat(
        data["starts"]
    ) <= instant < datetime.fromisoformat(data["ends"]):
        raise ActivationInactive("tournament activation is disabled or outside its validity window")
    return data


def spending(conn: sqlite3.Connection, scope: str) -> tuple[int, int]:
    reserved = events(conn, "cost_reserved", scope)
    settled = {
        e["reservation_id"]: e["actual_microusd"] for e in events(conn, "cost_settled", scope)
    }
    actual = sum(settled.values())
    held = sum(e["estimate_microusd"] for e in reserved if e["reservation_id"] not in settled)
    return actual, held


@contextmanager
def storage_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Keep transaction failures fatal through provider exception handling."""
    try:
        with transaction(conn):
            yield
    except (sqlite3.Error, LifecycleError):
        raise StorageFailure("cannot commit spending transaction; worker stopped") from None


def require_spending_clear(conn: sqlite3.Connection, scope: str) -> None:
    if events(conn, "restored_spending_hold", scope):
        raise TournamentError(
            "restored spending outcome is unknown; spending hold blocks purchases"
        )


@dataclass
class Budget:
    conn: sqlite3.Connection
    root: Path
    scope: str
    ceiling: int
    secret_names: tuple[str, ...] = ()

    def reserve(self, provider: str, estimate: float, request: Any) -> str:
        if not math.isfinite(estimate) or estimate <= 0:
            raise TournamentError("billable call has no conservative cost bound")
        amount = math.ceil(estimate * 1_000_000)
        identifier = uuid4().hex
        # The budget level this reservation reached, if any (M1-329). Computed inside the
        # transaction, where the numbers are already in hand and consistent, but reported
        # outside it: this block holds BEGIN IMMEDIATE, and an HTTP POST inside it would
        # serialize every other process's budget check behind a third party's latency.
        crossed: int | None = None
        try:
            # BEGIN IMMEDIATE serializes budget checks across processes and restarts.
            with storage_transaction(self.conn):
                require_spending_clear(self.conn, self.scope)
                actual, held = spending(self.conn, self.scope)
                if actual + held + amount > self.ceiling:
                    # 100 rather than a configured level: the ceiling is not a threshold
                    # that was crossed, it is the refusal itself, and it is the one budget
                    # condition that has already stopped a paid call from happening.
                    crossed = 100
                    raise TournamentError("round budget exhausted; no provider call made")
                crossed = budget_level_crossed(actual + held + amount, self.ceiling)
                append(
                    self.conn,
                    "cost_reserved",
                    self.scope,
                    {
                        "reservation_id": identifier,
                        "provider": provider,
                        "estimate_microusd": amount,
                    },
                )
        finally:
            # `finally` so the exhaustion refusal above is reported too -- that is the
            # alert an operator most needs, and it is only reachable on the raising path.
            if crossed is not None:
                emit(
                    "budget_threshold",
                    subject=f"{self.scope}-{crossed}",
                    title=f"whiskeyjack: budget at {crossed}%",
                    body=(
                        f"Spending for {self.scope} has reached {crossed}% of its "
                        f"activation ceiling. Reserved spend counts toward this and "
                        f"AskNews reservations never settle, so this tracks what will "
                        f"stop the worker, not what has been billed. "
                        + (
                            "The ceiling is reached: paid calls are being refused."
                            if crossed == 100
                            else "Check `tournament status` for the split."
                        )
                    ),
                )
        from whiskeyjack_bot.redaction import redact_secrets

        request = json.loads(redact_secrets(canonical(request), self.secret_names))
        witness(
            self.conn,
            self.root,
            self.scope,
            {
                "reservation_id": identifier,
                "provider": provider,
                "estimate_microusd": amount,
                "request": request,
            },
        )
        return identifier

    def settle(self, identifier: str, actual: float | None) -> None:
        if actual is None or not math.isfinite(actual) or actual < 0:
            return
        with storage_transaction(self.conn):
            # Recovery may reach this after completion was committed but settlement
            # was interrupted. Serialize the check and append across processes.
            if events(self.conn, "cost_settled_id", identifier):
                return
            append(
                self.conn,
                "cost_settled",
                self.scope,
                {"reservation_id": identifier, "actual_microusd": math.ceil(actual * 1_000_000)},
            )
            append(self.conn, "cost_settled_id", identifier, {})


CURRENT_BUDGET: ContextVar[Budget | None] = ContextVar("tournament_budget", default=None)


@contextmanager
def budget_context(budget: Budget) -> Iterator[None]:
    token = CURRENT_BUDGET.set(budget)
    try:
        yield
    finally:
        CURRENT_BUDGET.reset(token)
