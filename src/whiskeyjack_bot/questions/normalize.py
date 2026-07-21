"""Map pinned-SDK question objects onto the canonical schema (M1-201).

``forecasting_tools`` returns questions as its own Pydantic models
(:class:`~forecasting_tools.data_models.questions.MetaculusQuestion` and
subclasses). :func:`normalize_question` maps a single such object onto the
matching :mod:`whiskeyjack_bot.questions.model` canonical model, and is the one
place the SDK's field names are read -- everything downstream sees only the
canonical schema.

Type dispatch keys on the SDK's ``question_type`` literal rather than
``isinstance``: ``DiscreteQuestion`` subclasses ``NumericQuestion`` in the SDK,
so an ``isinstance(q, NumericQuestion)`` test would silently swallow the
unsupported ``discrete`` type. Only the three v1 types (D20) map; ``date``,
``conditional``, ``discrete`` and anything else are deferred (D21).

Refusal is two-tier (M1-203). :func:`normalize_question` -- the single-question
path, and the type-policy chokepoint -- *raises*
:class:`UnsupportedQuestionTypeError`, so it can never return a question of a
deferred type. :func:`normalize_questions` -- the batch path -- instead *skips*
such a question, records a
:class:`~whiskeyjack_bot.questions.events.DeferralEvent` on its result and logs
it, so one deferred question no longer throws away the normalization of every
supported question fetched alongside it. Everything else still aborts the batch:
D21 defers date and conditional questions, it does not make malformed records
survivable.

Error hygiene matches ``ConfigError``/``SnapshotError``/``LedgerError``: a
:class:`NormalizationError` never echoes field values (a mistakenly stored
secret must not surface), and sanitizing raises use ``from None``.
"""

from __future__ import annotations

import logging
from typing import Any, get_args

from forecasting_tools.data_models.questions import MetaculusQuestion, QuestionBasicType
from pydantic import ValidationError

from whiskeyjack_bot.config import SupportedQuestionType
from whiskeyjack_bot.questions.events import DeferralEvent, NormalizationResult
from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalMultipleChoiceQuestion,
    CanonicalNumericQuestion,
    CanonicalQuestion,
)

logger = logging.getLogger(__name__)

# Derived from the single source of truth in config (D20), so adding a type
# there cannot leave this dispatch silently out of step.
_SUPPORTED_TYPES: frozenset[str] = frozenset(get_args(SupportedQuestionType))
# The SDK's own six-value tag enum. Only a member of this set is safe to name in an
# error message; any other value reached the tag slot from outside the SDK's own
# models and is therefore unvetted content under the no-echo rule.
_KNOWN_SDK_TYPES: frozenset[str] = frozenset(get_args(QuestionBasicType))


class NormalizationError(Exception):
    """A question cannot be mapped onto the canonical schema.

    Same hygiene rule as ``ConfigError``: the message never echoes question
    field values (which can carry a mistakenly pasted secret), and sanitizing
    raises use ``from None`` so a wrapped ``ValidationError`` -- whose own text
    interpolates the offending input -- cannot resurface through the cause chain.
    """


class UnsupportedQuestionTypeError(NormalizationError):
    """The question is a type deferred in v1 (date/conditional/discrete, D21).

    Raised before any model or submission call is made. The message names the
    ``question_type`` tag only when it is one of the SDK's own enum values
    (``_KNOWN_SDK_TYPES``); anything else renders as ``'unknown'``, since an
    arbitrary value in that slot is unvetted content under the no-echo rule.
    """


def _sanitize(exc: ValidationError) -> NormalizationError:
    """Rebuild a ValidationError as a NormalizationError with inputs stripped."""
    problems = [
        f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    ]
    return NormalizationError(
        "cannot normalize question:\n" + "\n".join(f"  - {p}" for p in problems)
    )


def _group_parent_title(q: MetaculusQuestion) -> str | None:
    """The group parent's post title, for a question that is a group member (M1-202).

    Expansion sets a subquestion's ``question_text`` from the subquestion block and
    drops the parent post's title, so a subquestion titled only with its option label
    ("September 2024") carries no statement of what is being asked. The parent title
    survives on the raw post payload the SDK retains, and is lifted back out here.

    Only the title is taken. The payload itself is never carried onto the canonical
    model: it contains the community-prediction aggregations, and the canonical
    question is the forecaster's input boundary (community prediction is never a
    forecaster input in v1).

    Returns ``None`` for a non-group question, and for a group member whose payload
    is absent -- a question rebuilt from a snapshot has no obligation to carry one.
    """
    if not q.question_ids_of_group:
        return None
    payload = getattr(q, "api_json", None)
    if not isinstance(payload, dict):
        return None
    title = payload.get("title")
    # A blank title is no more use than a missing one, and normalizing it here keeps
    # the "is this self-describing" test downstream a simple None check.
    if not isinstance(title, str) or not title.strip():
        return None
    return title


def _safe_attr(q: MetaculusQuestion, attribute: str) -> object:
    """Read one attribute with nothing allowed to escape.

    ``getattr``'s default only suppresses ``AttributeError``; a property whose
    getter raises anything else would escape this module's error boundary. These
    reads happen on the *refusal* path, where an escaping exception would turn a
    clean deferral into a crash -- and the value is unusable either way.
    """
    try:
        return getattr(q, attribute, None)
    except Exception:
        return None


def _type_tag(question_type: object) -> str:
    """Render the type tag for an error message or a diagnostic event.

    One helper for both so the exception text and the event cannot drift: a tag
    outside the SDK's own enum reached that slot from outside the SDK's models and
    is unvetted content under the no-echo rule.
    """
    if isinstance(question_type, str) and question_type in _KNOWN_SDK_TYPES:
        return question_type
    return "unknown"


def _supported_type(q: MetaculusQuestion) -> str | None:
    """The question's type if v1 supports it (D20), else ``None``.

    isinstance before membership: an unhashable tag (a list, say) would raise a raw
    ``TypeError`` out of the frozenset test itself, escaping the error boundary.
    """
    question_type = _safe_attr(q, "question_type")
    if isinstance(question_type, str) and question_type in _SUPPORTED_TYPES:
        return question_type
    return None


def _safe_int(q: MetaculusQuestion, attribute: str) -> int | None:
    """Read one integer identity field, or ``None`` if it is not an integer.

    The int gate is the no-echo guarantee for :class:`DeferralEvent`: a string in
    an id slot -- which could be a mistakenly stored credential -- is dropped
    rather than carried. ``bool`` is excluded explicitly since it subclasses
    ``int``, and a ``True`` identity is a defect rather than an id.
    """
    value = _safe_attr(q, attribute)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _deferral_event(q: MetaculusQuestion) -> DeferralEvent:
    """Describe a question deferred under D21. Reads identity only.

    No content field is touched, so a deferred question reaches no model and no
    submission call -- and nothing that could carry a secret reaches the event.
    """
    tag = _type_tag(_safe_attr(q, "question_type"))
    return DeferralEvent(
        reason="deferred_v1_type" if tag != "unknown" else "unrecognized_type",
        question_type=tag,
        question_id=_safe_int(q, "id_of_question"),
        post_id=_safe_int(q, "id_of_post"),
    )


def _common_fields(q: MetaculusQuestion) -> dict[str, Any]:
    """Read the fields shared by every supported type off the SDK object."""
    return {
        "question_id": q.id_of_question,
        "post_id": q.id_of_post,
        "url": q.page_url,
        "title": q.question_text,
        "background_info": q.background_info,
        "resolution_criteria": q.resolution_criteria,
        "fine_print": q.fine_print,
        "unit_of_measure": q.unit_of_measure,
        "open_time": q.open_time,
        "close_time": q.close_time,
        "scheduled_resolution_time": q.scheduled_resolution_time,
        "tournament_slugs": q.tournament_slugs,
        "question_weight": q.question_weight,
        # Handed over as plain dicts, not constructed SourceCategory models: this
        # function runs inside the field-read fence, which catches only
        # AttributeError/TypeError, so a ValidationError raised here would escape
        # normalize_question entirely. Letting the canonical model build them keeps
        # that failure inside the ValidationError boundary below.
        "source_categories": [
            {"id": category.id, "name": category.name, "slug": category.slug}
            for category in q.categories
        ],
        "group_question_option": q.group_question_option,
        "question_ids_of_group": q.question_ids_of_group,
        "group_parent_title": _group_parent_title(q),
    }


def normalize_question(q: MetaculusQuestion) -> CanonicalQuestion:
    """Map one SDK question onto its canonical model.

    Raises :class:`UnsupportedQuestionTypeError` for deferred types (D21) and
    :class:`NormalizationError` if a supported type fails canonical validation.

    The singular path still *raises* on a deferred type, deliberately: it is the
    type-policy chokepoint, and "an unsupported question can never reach a model"
    is easiest to guarantee when this function cannot return one. Skipping with a
    diagnostic event is a batch policy and lives on :func:`normalize_questions`.
    """
    question_type = _supported_type(q)
    if question_type is None:
        # Refused before any field is read, so an unsupported type can never
        # reach a model or submission call (D21).
        tag = _type_tag(_safe_attr(q, "question_type"))
        raise UnsupportedQuestionTypeError(
            f"question type {tag!r} is not supported in v1 (binary, multiple_choice, numeric only)"
        )

    # Field reads are fenced separately from model construction: a TypeError raised
    # while building a canonical model is our bug, and must stay visible rather than
    # being reported as a malformed input record.
    try:
        fields = _common_fields(q)
        if question_type == "multiple_choice":
            fields.update(
                options=q.options,
                option_is_instance_of=q.option_is_instance_of,
            )
        elif question_type == "numeric":
            fields.update(
                lower_bound=q.lower_bound,
                upper_bound=q.upper_bound,
                open_lower_bound=q.open_lower_bound,
                open_upper_bound=q.open_upper_bound,
                zero_point=q.zero_point,
                cdf_size=q.cdf_size,
                nominal_lower_bound=q.nominal_lower_bound,
                nominal_upper_bound=q.nominal_upper_bound,
            )
    except (AttributeError, TypeError):
        # A question object missing the fields its own type declares. Without
        # this the raw AttributeError escapes to callers that only handle
        # NormalizationError -- the same defect found against SnapshotError in
        # M0-103 review. Constant message + from None: the underlying error can
        # carry the object's repr, and with it stored field values.
        raise NormalizationError(
            f"question object does not expose the fields required for type {question_type!r} "
            "(detail withheld: it can echo question contents)"
        ) from None

    try:
        if question_type == "binary":
            return CanonicalBinaryQuestion(**fields)
        if question_type == "multiple_choice":
            return CanonicalMultipleChoiceQuestion(**fields)
        if question_type == "numeric":
            return CanonicalNumericQuestion(**fields)
    except ValidationError as exc:
        # from None: the ValidationError text echoes the offending input values.
        raise _sanitize(exc) from None
    # Unreachable: question_type was checked against _SUPPORTED_TYPES above.
    raise AssertionError("unreachable: unhandled supported question type")


def normalize_questions(questions: list[MetaculusQuestion]) -> NormalizationResult:
    """Normalize a batch: defer unsupported types, propagate real failures (M1-203).

    A question whose type is deferred in v1 (D21) does **not** abort the batch. It
    is skipped before any field is read, recorded as a
    :class:`~whiskeyjack_bot.questions.events.DeferralEvent` on the result and
    logged at WARNING -- so it makes zero model and zero submission calls while the
    batch's supported questions still normalize.

    **Everything else still aborts.** A *supported*-type question that fails
    canonical validation, and a duplicate ``question_id``, both raise
    :class:`NormalizationError`. D21 defers date and conditional questions; it does
    not make malformed records survivable, and the stricter reading of an ambiguous
    criterion is the project rule.

    ``question_id`` uniqueness (M1-202) is enforced over the **accepted** questions
    only. Group expansion is where that check earns its keep: every subquestion of a
    group is built by deep-copying the parent post, so siblings share ``post_id``,
    ``url`` and the parent's framing fields, and ``question_id`` is the only thing
    telling them apart. A duplicate means either an expansion defect or the same
    question fetched twice, and both would collide on the ledger's
    ``UNIQUE (question_id, tournament_id, forecast_version)`` -- but only after a
    forecast had been generated and paid for. A deferred question has no canonical
    model and never reaches the ledger, so it is not part of that check.
    """
    accepted: list[CanonicalQuestion] = []
    deferrals: list[DeferralEvent] = []

    for question in questions:
        if _supported_type(question) is None:
            event = _deferral_event(question)
            deferrals.append(event)
            # Logged inside the loop so deferrals stay visible even when a later
            # question aborts the batch. Interpolated into the message rather than
            # passed via ``extra``: JsonFormatter builds a fixed payload with no
            # structured-field passthrough, and the message is a field it redacts,
            # so every value here is already inside the redaction path.
            logger.warning(
                "deferring unsupported question type (D21): reason=%s question_type=%s "
                "question_id=%s post_id=%s",
                event.reason,
                event.question_type,
                event.question_id,
                event.post_id,
            )
            continue
        accepted.append(normalize_question(question))

    seen: set[int] = set()
    duplicates = 0
    for canonical in accepted:
        if canonical.question_id in seen:
            duplicates += 1
        seen.add(canonical.question_id)
    if duplicates:
        # Count only. The colliding id is low-risk content, but the no-echo rule is
        # unconditional for an error message and the softer reading of it has been a
        # review finding. (DeferralEvent does carry ids: it is a diagnostic value
        # rather than an error message, and its int gate makes a leak impossible --
        # see events.py.)
        raise NormalizationError(
            f"question batch contains {duplicates} duplicate question id(s) "
            "(ids withheld: an error message never echoes record content)"
        )

    return NormalizationResult(questions=tuple(accepted), deferrals=tuple(deferrals))
