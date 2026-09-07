"""Reuse completed paid retrieval calls during recovery; never repeat unknown calls (LAUNCH)."""

from __future__ import annotations

from typing import Any

from whiskeyjack_bot.tournament_state import (
    CURRENT_BUDGET,
    TournamentError,
    append,
    digest,
    events,
)


def begin_call(
    provider: str, estimate: float, request: dict[str, Any], question_id: int, cutoff: str
) -> tuple[str | None, dict[str, Any] | None]:
    budget = CURRENT_BUDGET.get()
    if budget is None:
        return None, None
    scope = f"{budget.scope}:{question_id}:{cutoff}:{digest(request)}"
    completed = events(budget.conn, "retrieval_completed", scope)
    if completed:
        return scope, completed[-1]["response"]
    if events(budget.conn, "retrieval_started", scope):
        raise TournamentError("retrieval outcome is unknown; no repeat purchase")
    reservation = budget.reserve(provider, estimate, request)
    append(budget.conn, "retrieval_started", scope, {"reservation_id": reservation})
    return scope, None


def complete_call(
    scope: str | None, response: dict[str, Any], actual_cost: float | None = None
) -> None:
    budget = CURRENT_BUDGET.get()
    if budget is None or scope is None:
        return
    import json
    from whiskeyjack_bot.redaction import redact_secrets
    from whiskeyjack_bot.tournament_state import canonical

    response = json.loads(redact_secrets(canonical(response), budget.secret_names))
    if not events(budget.conn, "retrieval_completed", scope):
        append(budget.conn, "retrieval_completed", scope, {"response": response})
        started = events(budget.conn, "retrieval_started", scope)[-1]
        budget.settle(started["reservation_id"], actual_cost)
