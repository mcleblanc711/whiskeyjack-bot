"""Bounded, explicitly priced Sol requests with auditable effective parameters (LAUNCH)."""

from __future__ import annotations

import os
import asyncio
import math
import json
from typing import Any

import httpx

from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.tournament_state import CURRENT_BUDGET, TournamentError, append, canonical


from whiskeyjack_bot.tournament_state import digest, events
from whiskeyjack_bot.redaction import redact_secrets


class SolClient:
    def __init__(self, config: AppConfig) -> None:
        self.model = config.model.name
        self.config = config
        self.last_cost: float | None = None

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        request = {
            "model": "openai/gpt-5.6-sol",
            "messages": prompt,
            "reasoning": {"effort": "medium", "exclude": True},
            "max_tokens": 6000,
            "provider": {
                "max_price": {"prompt": 2, "completion": 10},
                "require_parameters": True,
                "allow_fallbacks": False,
            },
        }
        budget = CURRENT_BUDGET.get()
        # UTF-8 bytes plus framing conservatively bound text input token count.
        estimate = (len(canonical(request).encode()) + 4096) * 2 / 1_000_000 + 0.06
        cache_scope = f"{budget.scope}:{digest(request)}" if budget else ""
        if budget:
            cached = events(budget.conn, "model_completed", cache_scope)
            if cached:
                self.last_cost = cached[-1]["cost"]
                started = events(budget.conn, "model_started", cache_scope)[-1]
                budget.settle(started["reservation_id"], self.last_cost)
                return str(cached[-1]["content"])
            if events(budget.conn, "model_started", cache_scope):
                raise TournamentError(
                    "a prior model call has an unknown outcome; no repeat purchase"
                )
        reservation = budget.reserve("openrouter", estimate, request) if budget else None
        if budget:
            append(budget.conn, "model_started", cache_scope, {"reservation_id": reservation})
        self.last_cost = None
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
            cost = usage.get("cost")
            if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                self.last_cost = float(cost)
        except Exception:
            raise TournamentError(
                "Sol request failed or was unavailable at the authorized price"
            ) from None
        text = redact_secrets(text, self.config.secret_env_var_names())
        if budget and reservation:
            append(
                budget.conn,
                "model_completed",
                cache_scope,
                {"content": text, "cost": self.last_cost},
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
            budget.settle(reservation, self.last_cost)
        return text
