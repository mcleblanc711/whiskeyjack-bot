"""Bounded, explicitly priced OpenRouter requests with auditable effective parameters (M1-408).

M1-348 settles a bring-your-own-key call from its upstream figure; see
:func:`whiskeyjack_bot.tournament_state.settled_cost`. M1-367 raises the reasoning effort to
``high`` (D59) and records the effort sent on every billed call's ``model_response``.

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
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Literal, get_args

import httpx

from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.tournament_state import CURRENT_BUDGET, TournamentError, append, canonical


from whiskeyjack_bot.tournament_state import (
    CostBasis,
    ModelOutcomeUnknown,
    StorageFailure,
    digest,
    events,
    settled_cost,
)
from whiskeyjack_bot.redaction import redact_secrets

# The reasoning efforts a priced call may send (M5-806). Closed, so a replay arm cannot name a
# value OpenRouter would forward unvalidated, and so the fidelity gate can try every member when
# it rebuilds a stored call whose effort no record field carries.
ReasoningEffort = Literal["low", "medium", "high"]

# Reasoning effort sent on every priced call (M1-367, D59; was "medium" from Launch to M1-367).
# Code, not an AppConfig field: a new config field retires the live activation, and what
# attribution needs is the value actually sent, which each `model_response` now records.
# Reasoning tokens bill as output and count against `max_tokens`; at medium, 298 live Astra
# responses used at most 1,781 of MAX_OUTPUT_TOKENS (reasoning max 480), all `stop`.
# M5-806 lets a replay arm pass another member of `ReasoningEffort` to `PricedClient`; every
# live caller passes nothing, so this stays the value the worker sends.
REASONING_EFFORT: Final[ReasoningEffort] = "high"

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


# The earliest instant this project can attest each model was in service (M5-806, D62). A replay
# arm is refused for a question that opened before its model's instant, because a model released
# after a question opened may have been trained on its outcome; this is the leak the stored
# packets cannot close. Astra: M1-408 read OpenRouter's public listing on 2026-09-10, so the end
# of that day is the conservative bound (an earlier provider release only makes it stricter).
# Sol is deliberately absent: no date has been recorded for it, so a Sol arm is refused for every
# question rather than admitted on a guess. A model missing here is refused, never defaulted.
MODEL_IN_SERVICE: Final[dict[str, datetime]] = {
    "openrouter/openai/gpt-6-astra": datetime(2026, 9, 11, tzinfo=timezone.utc),
}


def _require_effort(value: object) -> ReasoningEffort:
    if type(value) is not str or value not in get_args(ReasoningEffort):
        raise TournamentError("reasoning effort is not a supported value")
    effort: ReasoningEffort = value  # type: ignore[assignment]
    return effort


def build_request(
    messages: Any, model: PricedModel, *, reasoning_effort: ReasoningEffort = REASONING_EFFORT
) -> dict[str, Any]:
    """The exact OpenRouter body for one call: the registered ID, and `max_price` from the
    registered prices, so OpenRouter refuses to route above what the reservation assumed.

    ``reasoning_effort`` (M5-806) defaults to the live constant, so every existing caller's body
    -- and with it every journal cache scope already written -- is byte-identical. The replay
    harness passes another member of :data:`ReasoningEffort` to run an arm at that effort, and
    the fidelity gate passes each member to rebuild a stored call made before M1-367.
    """
    return {
        "model": model.openrouter_id,
        "messages": messages,
        "reasoning": {"effort": _require_effort(reasoning_effort), "exclude": True},
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


def _refuse_non_finite(literal: str) -> float:
    """``json.loads``'s ``parse_constant`` hook: NaN, Infinity and -Infinity are refused.

    Standard JSON has no such literals, so a body carrying one is malformed. The literal is
    not echoed: the raise is translated to a static message by the caller's ``except``.
    """
    raise ValueError("non-finite number in response body")


def _finite_float(token: str) -> float:
    """``json.loads``'s ``parse_float`` hook: a number token that overflows is refused.

    ``1e999`` is valid JSON and ``float`` turns it into infinity without ever calling
    ``parse_constant`` -- the review-round-1 finding. Same refusal, same static translation.
    """
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("non-finite number in response body")
    return value


class PricedClient:
    def __init__(
        self, config: AppConfig, *, reasoning_effort: ReasoningEffort = REASONING_EFFORT
    ) -> None:
        priced = PRICED_MODELS.get(config.model.name)
        if priced is None:
            raise TournamentError("model is not a registered priced model")
        self.model = config.model.name
        self.priced = priced
        self.config = config
        # M5-806: the effort this client sends. The worker never passes it, so it is
        # REASONING_EFFORT there; checked here so a bad value refuses before any reservation.
        self.reasoning_effort = _require_effort(reasoning_effort)
        self.last_cost: float | None = None

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        request = build_request(prompt, self.priced, reasoning_effort=self.reasoning_effort)
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
                raise ModelOutcomeUnknown(
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
                # Strict, not `response.json()` (M1-350): that accepts the NaN/Infinity
                # literals and overflowing numbers such as `1e999`, and the journal's
                # `canonical()` refuses the non-finite floats they become -- so one anywhere
                # in the body escaped below as a raw ValueError, after `model_completed` was
                # written and before settlement. Refused here, inside the `try`, it is an
                # unknown outcome like any other failed request.
                data = json.loads(
                    response.content,
                    parse_constant=_refuse_non_finite,
                    parse_float=_finite_float,
                )
            text = data["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise ValueError
            usage = data.get("usage", {})
            # M1-348: a BYOK call reports `cost: 0` and bills upstream; see `settled_cost`.
            settled = settled_cost(usage)
            if settled is not None:
                self.last_cost, basis = settled
        except Exception:
            raise ModelOutcomeUnknown(
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
                                # 1.1.0 (M1-367): adds `reasoning_effort`, read off the
                                # request this call actually posted, not the constant.
                                "schema_version": "1.1.0",
                                "id": data.get("id"),
                                "model": data.get("model"),
                                "reasoning_effort": request["reasoning"]["effort"],
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
