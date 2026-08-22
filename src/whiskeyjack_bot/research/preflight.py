"""Shared caller-preflight guards for the retrieval adapters (M1-303, M1-309).

Both retrieval adapters (:mod:`whiskeyjack_bot.research.exa`,
:mod:`whiskeyjack_bot.research.asknews`) make billable calls per query, and both build a
:class:`whiskeyjack_bot.research.model.ResearchRun` only at the end of a run. A malformed
caller argument that only ``validate_run`` catches has already paid for every call in the
run before anything notices -- these two checks close that gap by running before any
network use, so a caller mistake is refused as the module's own error and no call happens
at all.

Each check takes the caller's own exception type as ``error`` so this module never becomes
the thing both adapters raise -- per the project's error-hygiene rule, callers only ever
handle their own module's error type. ``research/exa.py`` binds ``error=ExaFallbackError``;
``research/asknews.py`` binds ``error=AskNewsRetrievalError``. The guard logic itself --
what counts as malformed, and why -- lives here exactly once.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone


def string_list(values: Sequence[str], message: str, *, error: type[Exception]) -> list[str]:
    """Return ``values`` as a list of non-blank strings, or raise ``error(message)``.

    ``str`` **satisfies** ``Sequence[str]``, so ``mypy --strict`` cannot catch a caller
    passing one, and iterating it yields characters: a query string like ``"inflation"``
    silently becomes one billable call per character (cross-model review round 4, finding 2,
    on the Exa adapter -- and the identical shape on AskNews's ``queries`` argument is what
    M1-309 was filed for). ``bytes``/``bytearray`` are refused with them -- they iterate to
    ints, not strings, but the mistake is the same shape and the error should be too.

    ``list(values)`` is wrapped because a caller can pass any object at runtime: an
    ``__iter__`` that raises must arrive as ``error(message)`` like every other malformed
    shape, not as whatever it happened to throw.

    ``message`` is a constant chosen by the call site, never caller data.
    """
    if isinstance(values, (str, bytes, bytearray)):
        raise error(message)
    try:
        items = list(values)
    except Exception:
        raise error(message) from None
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise error(message)
    return items


def require_run_metadata(
    *, question_id: int, retrieval_run_id: str, now: datetime, error: type[Exception]
) -> datetime:
    """Refuse caller metadata the run record would reject, and return ``now`` in UTC.

    These three reach ``ResearchRun`` validation only at the *end* of a run, which is far
    too late: a malformed one would let every billable call happen and then raise --
    discarding the record of the spend -- and raise ``ResearchSchemaError``, a sibling
    module's error, not the caller's own (cross-model review round 4, finding 4, on the Exa
    adapter).

    ``question_id`` is gated **more strictly than the schema**, on purpose. ``ResearchRun``
    is not strict about it: pydantic coerces ``"42"`` to ``42`` and ``True`` to ``1``. An
    exact ``int`` closes that coercion at the source -- ``type(...) is int`` rather than
    ``isinstance``, which would admit an ``IntEnum`` (round 5, non-blocking observation 1;
    the same exact-type gate M1-203 uses).

    **Validate-and-return**: the UTC-normalized ``now`` is the value the rest of the run
    should use. Converting only inside final validation let an upper-bound
    ``datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(-timedelta(hours=14)))`` pass every
    preflight, pay for a call, and *then* raise a raw ``OverflowError`` from the conversion
    (round 5, finding 3). Converting once here means the failure lands before the money and
    as the caller's own error, and every later use of the value is already UTC.

    The ``utcoffset()`` gate stays ahead of the conversion and is not redundant with it:
    ``astimezone`` on a *naive* datetime silently assumes local time and succeeds, so it
    cannot be the thing that rejects one.
    """
    if type(question_id) is not int:
        raise error("question_id must be an int (offending input withheld)")
    # `not retrieval_run_id` refuses '' but '\n\t' is truthy, so a whitespace-only run id
    # would pass this gate, pay for every call in the run, and only then fail at the
    # ledger -- the exact shape of round 4's finding 4 (M1-607's non-blank identifier rule).
    if (
        not isinstance(retrieval_run_id, str)
        or not retrieval_run_id.strip()
        or "\x00" in retrieval_run_id
    ):
        raise error(
            "retrieval_run_id must be a non-blank string with no NUL character "
            "(offending input withheld)"
        )
    if not isinstance(now, datetime):
        raise error("now must be a timezone-aware datetime (offending input withheld)")
    try:
        # A caller-supplied tzinfo runs code here: utcoffset() can raise, and a broken one
        # must arrive as the caller's own error like every other malformed shape rather than
        # whatever it happened to throw.
        offset = now.utcoffset()
    except Exception:
        raise error("now must be a timezone-aware datetime (offending input withheld)") from None
    if offset is None:
        raise error("now must be a timezone-aware datetime (offending input withheld)")
    try:
        # Same reasoning as above -- astimezone calls the caller's tzinfo again -- plus
        # OverflowError when the UTC instant falls outside datetime's representable range.
        return now.astimezone(timezone.utc)
    except Exception:
        raise error("now cannot be converted to UTC (offending input withheld)") from None
