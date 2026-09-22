"""Bounded, explicitly priced OpenRouter requests with auditable effective parameters (M1-408).

M1-348 settles a bring-your-own-key call from its upstream figure; see
:func:`whiskeyjack_bot.tournament_state.settled_cost`.

Launch shipped this as a Sol-only client, with the model ID, the OpenRouter ``max_price`` and
the budget reservation estimate each hard-coded from Sol's prices. M1-408 moves the tournament
forecaster to GPT-6 Astra (5x Sol's prices), and the three numbers have to move together:
a ``max_price`` raised without the estimate would reserve a fifth of what a call can bill,
and the budget ceiling is what stops the worker. So a model is admitted only through
:data:`PRICED_MODELS`, and ``max_price`` and the estimate are both *derived* from its one
pair of registered prices -- there is no second place to forget to update.

The registry is closed on purpose. ``tournament.run_once`` refuses a model outside it, so a
config naming an unregistered model fails before any purchase rather than falling through to
``GeneralLlm``, which reserves nothing against the tournament budget.
"""

from __future__ import annotations

import os
import asyncio
import json
from dataclasses import dataclass
from typing import Any, Final

import httpx

from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.tournament_state import CURRENT_BUDGET, TournamentError, append, canonical


from whiskeyjack_bot.tournament_state import (
    CostBasis,
    StorageFailure,
    digest,
    events,
    settled_cost,
)
from whiskeyjack_bot.redaction import redact_secrets

# Output ceiling per call. `tournament.run_once` also requires model.max_output_tokens to
# equal this, so the reservation below and the request cannot disagree about it.
MAX_OUTPUT_TOKENS: Final = 6000


@dataclass(frozen=True)
class PricedModel:
    """One admissible model: its OpenRouter ID and its prices in USD per million tokens.

    The prices are the **ceiling** OpenRouter may route at (`max_price`), not an estimate of
    the bill, so they are the right basis for a reservation: the actual cost settles it down.
    """

    openrouter_id: str
    prompt_usd_per_mtok: float
    completion_usd_per_mtok: float


# Keyed by the full LiteLLM name `model.name` carries. Prices read from OpenRouter's public
# /api/v1/models listing on 2026-09-10: Sol 2/10, Astra 10/50. Astra has pricier endpoints
# (Azure 11/55, a 20/100 tier); max_price 10/50 excludes them rather than paying more.
# Written as ints deliberately: the request renders `"max_price":{"prompt":2,...}` exactly as
# Launch's literal did, so a Sol request's digest -- and with it every `model_started` /
# `model_completed` cache scope already in a live ledger -- is unchanged by this refactor.
PRICED_MODELS: Final[dict[str, PricedModel]] = {
    "openrouter/openai/gpt-5.6-sol": PricedModel("openai/gpt-5.6-sol", 2, 10),
    "openrouter/openai/gpt-6-astra": PricedModel("openai/gpt-6-astra", 10, 50),
}


def build_request(messages: Any, model: PricedModel) -> dict[str, Any]:
    """The exact OpenRouter body for one call: the registered ID, and `max_price` from the
    registered prices, so OpenRouter refuses to route above what the reservation assumed."""
    return {
        "model": model.openrouter_id,
        "messages": messages,
        "reasoning": {"effort": "medium", "exclude": True},
        "max_tokens": MAX_OUTPUT_TOKENS,
        "provider": {
            "max_price": {
                "prompt": model.prompt_usd_per_mtok,
                "completion": model.completion_usd_per_mtok,
            },
            "require_parameters": True,
            "allow_fallbacks": False,
        },
    }


def reservation_estimate_usd(request: dict[str, Any], model: PricedModel) -> float:
    """Upper-bound the cost of one request at the model's registered prices.

    UTF-8 bytes of the whole request plus 4096 of framing conservatively bound the input
    token count (a token is never less than a byte of text), and the output is bounded by
    ``MAX_OUTPUT_TOKENS``. For Sol this is **bit-identical** to Launch's hard-coded
    ``(bytes + 4096) * 2 / 1_000_000 + 0.06``: the two terms keep that expression's order of
    operations, and ``6000 * 10 / 1_000_000`` rounds to the same double as the literal
    ``0.06``. Summing over one division drifts in the last place: measured, 61,894 of the
    first 300,000 request sizes.
    """
    input_bound = len(canonical(request).encode()) + 4096
    return (
        input_bound * model.prompt_usd_per_mtok / 1_000_000
        + MAX_OUTPUT_TOKENS * model.completion_usd_per_mtok / 1_000_000
    )


def _replayed_cost(
    conn: Any, reservation_id: str, completed: dict[str, Any]
) -> tuple[float, CostBasis] | None:
    """What a cached call settles at on replay (M1-348): re-derived, never trusted as stored.

    The stored ``model_response`` for the reservation carries the provider's ``usage``, so
    replay settles through the same rule a fresh call does. That matters for every GPT-6
    Astra call made before M1-348: its ``model_completed.cost`` is the BYOK ``0.0``, and
    re-settling that would make an interrupted settlement read as free. A completion with
    no stored response (a crash between the two appends) is trusted only if it carries a
    ``cost_basis``, which only a post-M1-348 writer records; a pre-M1-348 ``0.0`` with no
    basis is unknown, and its reservation stays held.
    """
    responses = events(conn, "model_response", reservation_id)
    if responses:
        response = responses[-1]
        return settled_cost(response.get("usage") if type(response) is dict else None)
    basis, cost = completed.get("cost_basis"), completed.get("cost")
    if basis == "upstream_byok":
        found = settled_cost({"is_byok": True, "cost_details": {"upstream_inference_cost": cost}})
    elif basis == "openrouter":
        found = settled_cost({"is_byok": False, "cost": cost})
    else:
        found = None
    return found


class PricedClient:
    def __init__(self, config: AppConfig) -> None:
        priced = PRICED_MODELS.get(config.model.name)
        if priced is None:
            raise TournamentError("model is not a registered priced model")
        self.model = config.model.name
        self.priced = priced
        self.config = config
        self.last_cost: float | None = None

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        request = build_request(prompt, self.priced)
        budget = CURRENT_BUDGET.get()
        estimate = reservation_estimate_usd(request, self.priced)
        cache_scope = f"{budget.scope}:{digest(request)}" if budget else ""
        if budget:
            cached = events(budget.conn, "model_completed", cache_scope)
            if cached:
                starts = events(budget.conn, "model_started", cache_scope)
                started = starts[-1] if starts else None
                reservation_id = started.get("reservation_id") if type(started) is dict else None
                if type(reservation_id) is not str or type(cached[-1]) is not dict:
                    raise StorageFailure("cannot read tournament journal")
                replayed = _replayed_cost(budget.conn, reservation_id, cached[-1])
                self.last_cost = None if replayed is None else replayed[0]
                budget.settle(
                    reservation_id,
                    self.last_cost,
                    basis=None if replayed is None else replayed[1],
                )
                return str(cached[-1]["content"])
            if events(budget.conn, "model_started", cache_scope):
                raise TournamentError(
                    "a prior model call has an unknown outcome; no repeat purchase"
                )
        reservation = budget.reserve("openrouter", estimate, request) if budget else None
        if budget:
            append(budget.conn, "model_started", cache_scope, {"reservation_id": reservation})
        self.last_cost = None
        basis: CostBasis | None = None
        try:
            async with asyncio.timeout(120), httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {os.environ[self.config.model.api_key_env]}"
                    },
                    json=request,
                )
                response.raise_for_status()
                data = response.json()
            text = data["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise ValueError
            usage = data.get("usage", {})
            # M1-348: a BYOK call reports `cost: 0` and bills upstream; see `settled_cost`.
            settled = settled_cost(usage)
            if settled is not None:
                self.last_cost, basis = settled
        except Exception:
            raise TournamentError(
                "priced model request failed or was unavailable at the authorized price"
            ) from None
        text = redact_secrets(text, self.config.secret_env_var_names())
        if budget and reservation:
            append(
                budget.conn,
                "model_completed",
                cache_scope,
                {"content": text, "cost": self.last_cost, "cost_basis": basis},
            )
            # Deliberately exclude reasoning/provider internals and all headers.
            append(
                budget.conn,
                "model_response",
                reservation,
                json.loads(
                    redact_secrets(
                        canonical(
                            {
                                "schema_version": "1.0.0",
                                "id": data.get("id"),
                                "model": data.get("model"),
                                "usage": usage,
                                "finish_reason": data["choices"][0].get("finish_reason"),
                                "content": text,
                            }
                        ),
                        self.config.secret_env_var_names(),
                    )
                ),
            )
            budget.settle(reservation, self.last_cost, basis=basis)
        return text
