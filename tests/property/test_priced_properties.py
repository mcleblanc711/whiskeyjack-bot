"""Properties of the priced client's request builder and reservation estimate (M1-408).

The estimate is what the tournament budget reserves before a paid model call, so it is the
number that decides when the worker stops. The properties: it never raises on any message
text the forecaster can send and is always a valid reservation (finite, positive); it is
**bit-identical** to Launch's hard-coded Sol formula, so the refactor moved no live number;
it never decreases as the request grows; and Astra's is at least 5x Sol's for the same
messages, because its prices are 5x on both sides.
"""

from __future__ import annotations

import math
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from whiskeyjack_bot.forecast.priced import (
    PRICED_MODELS,
    build_request,
    reservation_estimate_usd,
)
from whiskeyjack_bot.tournament_state import canonical

from strategies import HOSTILE_TEXT  # type: ignore[import-not-found]

SOL = PRICED_MODELS["openrouter/openai/gpt-5.6-sol"]
ASTRA = PRICED_MODELS["openrouter/openai/gpt-6-astra"]

MESSAGES = st.lists(
    st.fixed_dictionaries({"role": st.sampled_from(["system", "user"]), "content": HOSTILE_TEXT}),
    min_size=1,
    max_size=3,
)


def _launch_sol_estimate(request: dict[str, Any]) -> float:
    """Launch's formula, verbatim from the pre-M1-408 `forecast/sol.py`."""
    return (len(canonical(request).encode()) + 4096) * 2 / 1_000_000 + 0.06


@given(messages=MESSAGES)
def test_every_registered_estimate_is_a_valid_reservation(messages: list[Any]) -> None:
    """`Budget.reserve` refuses a non-finite or non-positive bound, so this is the contract."""
    for model in PRICED_MODELS.values():
        estimate = reservation_estimate_usd(build_request(messages, model), model)
        assert math.isfinite(estimate) and estimate > 0


@given(messages=MESSAGES)
def test_the_sol_estimate_is_bit_identical_to_launch(messages: list[Any]) -> None:
    """Equality of floats, deliberately: the refactor must move no live number at all."""
    request = build_request(messages, SOL)
    assert reservation_estimate_usd(request, SOL) == _launch_sol_estimate(request)


@given(messages=MESSAGES, extra=HOSTILE_TEXT)
def test_the_estimate_never_decreases_as_the_request_grows(messages: list[Any], extra: str) -> None:
    for model in PRICED_MODELS.values():
        smaller = reservation_estimate_usd(build_request(messages, model), model)
        grown = [*messages, {"role": "user", "content": extra}]
        assert reservation_estimate_usd(build_request(grown, model), model) >= smaller


@given(messages=MESSAGES)
def test_astra_reserves_at_least_five_times_sol_for_the_same_messages(
    messages: list[Any],
) -> None:
    sol = reservation_estimate_usd(build_request(messages, SOL), SOL)
    astra = reservation_estimate_usd(build_request(messages, ASTRA), ASTRA)
    assert astra >= 5 * sol * (1 - 1e-12)


@given(messages=MESSAGES)
def test_the_request_carries_the_registered_id_and_prices(messages: list[Any]) -> None:
    for model in PRICED_MODELS.values():
        request = build_request(messages, model)
        assert request["model"] == model.openrouter_id
        assert request["provider"]["max_price"] == {
            "prompt": model.prompt_usd_per_mtok,
            "completion": model.completion_usd_per_mtok,
        }
        assert request["provider"]["allow_fallbacks"] is False
        assert request["messages"] is messages


@given(messages=MESSAGES)
def test_a_sol_request_is_byte_identical_to_launchs_literal(messages: list[Any]) -> None:
    """The request's digest keys every `model_started`/`model_completed` cache scope in a
    live ledger. Identical bytes mean a Sol call made before this refactor is still found
    as the same call after it -- neither re-bought nor orphaned mid-outcome."""
    launch = {
        "model": "openai/gpt-5.6-sol",
        "messages": messages,
        "reasoning": {"effort": "medium", "exclude": True},
        "max_tokens": 6000,
        "provider": {
            "max_price": {"prompt": 2, "completion": 10},
            "require_parameters": True,
            "allow_fallbacks": False,
        },
    }
    assert canonical(build_request(messages, SOL)) == canonical(launch)
