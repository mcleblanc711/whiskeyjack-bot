"""Diagnostic value objects for question normalization (M1-203).

A question whose type is deferred in v1 (D21) does not abort a batch and does not
raise to the caller: :func:`~whiskeyjack_bot.questions.normalize.normalize_questions`
skips it and records a :class:`DeferralEvent` on its result, so the batch's
supported questions still normalize.

**These are in-process values, not ledger rows.** Persisting a deferral belongs to
the ledger writers (M1-602): there is no run or tournament context at this layer to
key a row on, and the event exists to be returned to the caller and logged.

The event is built to carry **no unvetted string**. ``reason`` is one of the two
literals below, ``question_type`` is a member of the SDK's own tag enum or
``'unknown'``, and both identity fields are gated to ``int`` -- a non-integer id
becomes ``None`` rather than being carried. That is what lets the event be logged
without widening the no-echo surface the rest of the package maintains.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from whiskeyjack_bot.questions.model import CanonicalQuestion

# Two reasons rather than one: the ``_KNOWN_SDK_TYPES`` gate renders an unvetted tag
# as 'unknown', which erases the difference between "a type the SDK defines and we
# defer" and "something arbitrary reached the tag slot". The first is routine; the
# second means a question object came from outside the SDK's own models. The
# distinction is recoverable here without echoing the value.
DeferralReason = Literal["deferred_v1_type", "unrecognized_type"]


@dataclass(frozen=True)
class DeferralEvent:
    """One question skipped because its type is deferred in v1 (D21).

    Identity is best-effort and read defensively: ``question_id`` and ``post_id``
    are ``None`` when the question object does not expose an integer there. They
    are carried at all -- unlike the duplicate-id error, which withholds ids --
    because an operator cannot act on "one question was deferred". The no-echo rule
    guards against a credential surfacing through free-text field values; the
    ``int`` gate in ``normalize._safe_int`` makes that impossible here by
    construction rather than by promise.
    """

    reason: DeferralReason
    # A ``QuestionBasicType`` member, or 'unknown' for an unvetted tag.
    question_type: str
    question_id: int | None = None
    post_id: int | None = None


@dataclass(frozen=True)
class NormalizationResult:
    """The outcome of normalizing a batch: what was accepted, and what was deferred.

    A value object rather than a ``(questions, deferrals)`` tuple, deliberately:
    tuple-returning invites ``canonical, _ = normalize_questions(...)``, which
    discards the deferrals in one character -- the exact silence M1-203 exists to
    remove. Named access makes dropping them a choice.
    """

    questions: tuple[CanonicalQuestion, ...]
    deferrals: tuple[DeferralEvent, ...] = ()
