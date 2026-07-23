"""Diagnostic value objects for question normalization (M1-203).

A question whose type is deferred in v1 (D21) does not abort a batch and does not
raise to the caller: :func:`~whiskeyjack_bot.questions.normalize.normalize_questions`
skips it and records a :class:`DeferralEvent` on its result, so the batch's
supported questions still normalize.

**These are in-process values, not ledger rows.** Persisting a deferral belongs to
the ledger writers (M1-602): there is no run or tournament context at this layer to
key a row on, and the event exists to be returned to the caller and logged.

The event is built to carry **no unvetted string**. ``reason`` is one of the two
literals below and is **derived**, not accepted from the caller: it follows
deterministically from the canonicalized ``question_type`` (see ``__post_init__``).
``question_type`` is a member of the SDK's own tag enum that v1 does not support, or
``'unknown'``; both identity fields are gated to ``int`` -- a non-integer id becomes
``None`` rather than being carried. That is what lets the event be logged without
widening the no-echo surface the rest of the package maintains.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from forecasting_tools.data_models.questions import QuestionBasicType

from whiskeyjack_bot.config import SupportedQuestionType
from whiskeyjack_bot.questions.model import CanonicalQuestion

# Two reasons rather than one: the ``_KNOWN_SDK_TYPES`` gate renders an unvetted tag
# as 'unknown', which erases the difference between "a type the SDK defines and we
# defer" and "something arbitrary reached the tag slot". The first is routine; the
# second means a question object came from outside the SDK's own models. The
# distinction is recoverable here without echoing the value, and is drawn from the
# canonicalized type alone rather than trusted from the constructor.
DeferralReason = Literal["deferred_v1_type", "unrecognized_type"]

# The exact-type/membership sets ``__post_init__`` canonicalizes against. The SDK's
# own six-value tag enum is the only vocabulary safe to name; anything else renders
# as 'unknown'. ``frozenset`` so membership is a hash lookup, not a linear scan.
_KNOWN_SDK_TYPES: frozenset[str] = frozenset(get_args(QuestionBasicType))
# The v1-supported types (D20). A supported type is *never* deferred, so a
# ``DeferralEvent`` naming one is incoherent and collapses to 'unknown'. Derived from
# config directly -- ``normalize`` imports *from* this module, so importing its
# ``_SUPPORTED_TYPES`` back would be circular; ``config`` imports nothing from
# ``questions``.
_SUPPORTED_TYPES: frozenset[str] = frozenset(get_args(SupportedQuestionType))


@dataclass(frozen=True)
class DeferralEvent:
    """One question skipped because its type is deferred in v1 (D21).

    Identity is best-effort and read defensively: ``question_id`` and ``post_id``
    are ``None`` when the question object does not expose an integer there. They
    are carried at all -- unlike the duplicate-id error, which withholds ids --
    because an operator cannot act on "one question was deferred". The no-echo rule
    guards against a credential surfacing through free-text field values.

    Both the no-echo invariant *and* reason/type coherence are enforced here in
    ``__post_init__``, not only by the ``normalize`` helpers that usually build the
    event: a frozen dataclass's field annotations do not validate, so a value object
    exported from the package must uphold its own guarantees. ``reason`` is **not
    trusted from the caller** -- it is derived from the canonicalized
    ``question_type`` alone, so a contradictory ``(reason, question_type)`` pair
    passed to the constructor cannot survive.

    A coherent deferral describes exactly one thing: a type the SDK vouches for (a
    member of ``_KNOWN_SDK_TYPES``) that v1 does **not** support. Such a type is kept
    with ``reason='deferred_v1_type'``. Everything else -- a non-``str``, a ``str``
    *subclass*, an unvetted tag, or even a *supported* type (which is never deferred)
    -- collapses to ``question_type='unknown'``, ``reason='unrecognized_type'``.

    The type gate is **exact** -- ``type(x) is str``/``int``, not ``isinstance`` --
    because a ``str``/``int`` *subclass* (or an ``IntEnum``) can carry
    attacker-controlled ``__str__``/``__repr__`` whose value slips past a membership
    check and renders through the log or this dataclass's repr. Anything not exactly
    the built-in type degrades to ``'unknown'``/``None``, so a leak is impossible by
    construction rather than by promise; the same coercion (not a raise) keeps a
    diagnostic value from turning into a crash.
    """

    # A ``QuestionBasicType`` member (deferred by v1), or 'unknown' for an unvetted
    # or incoherent tag.
    question_type: str
    # Derived in ``__post_init__`` from ``question_type``; the constructor value is
    # ignored. Defaulted so callers need not supply it.
    reason: DeferralReason = "unrecognized_type"
    question_id: int | None = None
    post_id: int | None = None

    def __post_init__(self) -> None:
        # Reason follows the canonicalized type, so a contradictory pair cannot
        # survive. A coherent deferral is a KNOWN SDK type that v1 does not support;
        # a supported type is never deferred and an unvetted tag was never vouched
        # for, so both collapse to the unknown/unrecognized pair. Exact-type check so
        # only a built-in str -- whose rendering carries no attacker payload -- is
        # ever kept.
        qt = self.question_type
        if type(qt) is str and qt in _KNOWN_SDK_TYPES and qt not in _SUPPORTED_TYPES:
            object.__setattr__(self, "reason", "deferred_v1_type")
        else:
            object.__setattr__(self, "question_type", "unknown")
            object.__setattr__(self, "reason", "unrecognized_type")
        for field in ("question_id", "post_id"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value <= 0):
                object.__setattr__(self, field, None)


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
