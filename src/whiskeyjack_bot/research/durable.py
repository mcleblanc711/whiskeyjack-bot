"""Reuse completed paid retrieval calls during recovery; never repeat unknown calls (LAUNCH)."""

from __future__ import annotations

from typing import Any

from whiskeyjack_bot.tournament_state import (
    CURRENT_BUDGET,
    CostBasis,
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
        started = events(budget.conn, "retrieval_started", scope)[-1]
        budget.settle(started["reservation_id"], completed[-1].get("actual_cost"))
        return scope, completed[-1]["response"]
    if events(budget.conn, "retrieval_started", scope):
        raise TournamentError("retrieval outcome is unknown; no repeat purchase")
    reservation = budget.reserve(provider, estimate, request)
    append(budget.conn, "retrieval_started", scope, {"reservation_id": reservation})
    return scope, None


def complete_call(
    scope: str | None,
    response: dict[str, Any],
    actual_cost: float | None = None,
    *,
    actual_microusd: int | None = None,
    basis: CostBasis | None = None,
) -> None:
    """Record a completed call once, then settle its reservation.

    ``actual_cost`` is a dollar figure (Exa's ``costDollars.total``), stored on the
    ``retrieval_completed`` row so recovery settles from it. ``actual_microusd`` is an exact
    figure the caller derives from ``response`` itself (AskNews credits, M1-336): it is not
    stored, because the stored response already carries it, and recovery reaches this
    function again with the cached response and derives the same figure. Pass one or
    neither; with both, the exact figure wins. Neither leaves the reservation held.
    """
    budget = CURRENT_BUDGET.get()
    if budget is None or scope is None:
        return
    import json
    from whiskeyjack_bot.redaction import redact_secrets
    from whiskeyjack_bot.tournament_state import canonical

    response = json.loads(redact_secrets(canonical(response), budget.secret_names))
    if not events(budget.conn, "retrieval_completed", scope):
        append(
            budget.conn,
            "retrieval_completed",
            scope,
            {"response": response, "actual_cost": actual_cost},
        )
    started = events(budget.conn, "retrieval_started", scope)[-1]
    if actual_microusd is not None:
        budget.settle_microusd(started["reservation_id"], actual_microusd, basis=basis)
    else:
        budget.settle(started["reservation_id"], actual_cost, basis=basis)
