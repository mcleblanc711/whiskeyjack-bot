"""What an AskNews call costs, in integer micro-USD, from its own ``usage.credits`` (M1-336, M1-337).

AskNews bills in credits and its ``SearchResponse.usage`` reports ``credits: int`` with no
dollar figure, so before M1-336 an AskNews reservation was never settled and its estimate
was the permanent charge. This module is the one place a credit count becomes money.

**The rate is a module constant, not configuration (D41).** Any new ``AppConfig`` field
changes ``config_sha256`` and retires the live MiniBench activation, so M1-353 (Deferred)
moves it to config after 33125 ends. The owner confirmed the figure on 2026-09-23: $0.025 per
credit, the pay-as-you-go rate.

**Integer micro-USD throughout, never a float dollar.** ``math.ceil(3 * 0.025 * 1_000_000)``
is 75001, not 75000: 35,044 of the credit counts 0..100000 misround through a float rate,
always upward by one micro-USD. A settlement must be exactly what the provider's figure
implies, so the conversion is ``credits * MICROUSD_PER_CREDIT`` in integers.

Deliberately free of ``asknews_sdk``: :func:`whiskeyjack_bot.tournament_state.correct_costs`
converts stored responses with :func:`credits_microusd` and must not import the SDK to do it.
"""

from __future__ import annotations

from typing import Final

# $0.025 per credit (owner, 2026-09-23; D41).
MICROUSD_PER_CREDIT: Final = 25_000

# What one "latest news" search is billed, per the owner's endpoint table (2026-09-23):
# news search 1 credit. M1-352 retired the 5-credit historical pass, so this is the only
# AskNews call the adapter makes.
CREDITS_PER_NEWS_CALL: Final = 1

# The reservation for one news call, in the dollars `begin_call` takes. Exactly
# 25000 micro-USD once `Budget.reserve` scales and ceils it; a test pins that, because the
# float round trip is exact for this count and not for every count.
NEWS_CALL_ESTIMATE_USD: Final = CREDITS_PER_NEWS_CALL * MICROUSD_PER_CREDIT / 1_000_000

# Far above any real search (the owner's most expensive endpoint is 15 credits) and far
# below anything that makes the micro-USD figure unwieldy. A count above it is not a bill
# this pipeline could have run up; it is unknown, not a figure to settle.
MAX_CREDITS: Final = 1_000_000


def credits_microusd(response: object) -> int | None:
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
    return credits * MICROUSD_PER_CREDIT
