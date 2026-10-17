"""What an AskNews call costs, in integer micro-USD, from its own ``usage.credits`` (M1-336, M1-337).

AskNews bills in credits and its ``SearchResponse.usage`` reports ``credits: int`` with no
dollar figure, so before M1-336 an AskNews reservation was never settled and its estimate
was the permanent charge. This module is the one place a credit count becomes money.

**The rate is configuration** (M1-353, D41, D54): ``retrieval.primary.microusd_per_credit``,
the provider it prices, read by :func:`microusd_per_credit`. It was a module constant while
33125 was live, because any new ``AppConfig`` field changes ``config_sha256`` and retires the
activation. The owner confirmed the figure on 2026-09-23: $0.025 per credit, the
pay-as-you-go rate, which is the field's default and what the committed profiles state.

**Integer micro-USD throughout, never a float dollar.** ``math.ceil(3 * 0.025 * 1_000_000)``
is 75001, not 75000: 35,044 of the credit counts 0..100000 misround through a float rate,
always upward by one micro-USD. A settlement must be exactly what the provider's figure
implies, so the conversion is ``credits * microusd_per_credit`` in integers.

Deliberately free of ``asknews_sdk``: :func:`whiskeyjack_bot.tournament_state.correct_costs`
converts stored responses with :func:`credits_microusd` and must not import the SDK to do it.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from whiskeyjack_bot.config import AppConfig

# What one "latest news" search is billed, per the owner's endpoint table (2026-09-23):
# news search 1 credit. M1-352 retired the 5-credit historical pass, so this is the only
# AskNews call the adapter makes.
CREDITS_PER_NEWS_CALL: Final = 1

# Far above any real search (the owner's most expensive endpoint is 15 credits) and far
# below anything that makes the micro-USD figure unwieldy. A count above it is not a bill
# this pipeline could have run up; it is unknown, not a figure to settle.
MAX_CREDITS: Final = 1_000_000


def microusd_per_credit(config: AppConfig) -> int:
    """The configured AskNews rate. AskNews is only ever ``retrieval.primary``
    (``research/orchestrate.py`` refuses any other primary), so that is where it lives.

    Validation fills an AskNews provider's rate with the default when it is omitted, so the
    ``None`` branch is reached only by a config built with ``model_copy`` (which skips
    validators) -- and then it is that same default, never a second figure.
    """
    from whiskeyjack_bot.config import ASKNEWS_DEFAULT_MICROUSD_PER_CREDIT

    rate = config.retrieval.primary.microusd_per_credit
    return ASKNEWS_DEFAULT_MICROUSD_PER_CREDIT if rate is None else rate


def news_call_estimate_usd(rate: int) -> float:
    """The reservation for one news call, in the dollars ``begin_call`` takes (M1-337).

    ``Budget.reserve`` scales by 1e6 and ceils, and ``n / 1_000_000 * 1_000_000`` is not exact
    for every integer ``n``, so this is the smallest float that ceils back to exactly
    ``CREDITS_PER_NEWS_CALL * rate`` micro-USD: never a micro-USD short of what one call
    bills, and never one over. A test pins that for every rate the field admits.
    """
    exact = CREDITS_PER_NEWS_CALL * rate
    estimate = exact / 1_000_000
    while math.ceil(estimate * 1_000_000) > exact:
        estimate = math.nextafter(estimate, 0.0)
    while math.ceil(estimate * 1_000_000) < exact:
        estimate = math.nextafter(estimate, math.inf)
    return estimate


def credits_microusd(response: object, *, microusd_per_credit: int) -> int | None:
    """The micro-USD an AskNews response's ``usage.credits`` bills, or None if unknown.

    ``response`` is the ``model_dump(mode="json")`` of a ``SearchResponse`` -- as the adapter
    holds it, and as ``retrieval_completed`` stores it. Only an exact ``int`` in
    ``[0, MAX_CREDITS]`` converts. ``bool`` is an ``int`` subclass and ``True`` would read as
    one credit; a float, a string, a negative, a missing ``usage`` block or a missing
    ``credits`` key is unknown. The caller leaves an unknown call's reservation held at its
    full estimate: unknown is never free.

    Total over arbitrary JSON: it never raises, and reads nothing but the two keys.
    """
    if type(response) is not dict:
        return None
    usage = response.get("usage")
    if type(usage) is not dict:
        return None
    credits = usage.get("credits")
    if type(credits) is not int or not 0 <= credits <= MAX_CREDITS:
        return None
    return credits * microusd_per_credit
