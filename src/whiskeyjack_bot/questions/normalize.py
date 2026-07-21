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
``conditional``, ``discrete`` and anything else are refused with
:class:`UnsupportedQuestionTypeError` (D21). Turning that refusal into a logged
diagnostic event -- rather than an exception the caller must catch -- is M1-203.

Error hygiene matches ``ConfigError``/``SnapshotError``/``LedgerError``: a
:class:`NormalizationError` never echoes field values (a mistakenly stored
secret must not surface), and sanitizing raises use ``from None``.
"""

from __future__ import annotations

from typing import Any, get_args

from forecasting_tools.data_models.questions import MetaculusQuestion, QuestionBasicType
from pydantic import ValidationError

from whiskeyjack_bot.config import SupportedQuestionType
from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalMultipleChoiceQuestion,
    CanonicalNumericQuestion,
    CanonicalQuestion,
)

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
        # Slug where the SDK supplies one (it is optional on Category), else the
        # required name. Carried uninterpreted -- see the field comment in model.py.
        "source_categories": [category.slug or category.name for category in q.categories],
        "group_question_option": q.group_question_option,
        "question_ids_of_group": q.question_ids_of_group,
    }


def normalize_question(q: MetaculusQuestion) -> CanonicalQuestion:
    """Map one SDK question onto its canonical model.

    Raises :class:`UnsupportedQuestionTypeError` for deferred types (D21) and
    :class:`NormalizationError` if a supported type fails canonical validation.
    """
    question_type = getattr(q, "question_type", None)
    # isinstance before membership: an unhashable tag (a list, say) would raise a raw
    # TypeError out of the frozenset test itself, escaping the boundary below.
    if not isinstance(question_type, str) or question_type not in _SUPPORTED_TYPES:
        # Refused before any field is read, so an unsupported type can never
        # reach a model or submission call (D21).
        tag = (
            question_type
            if isinstance(question_type, str) and question_type in _KNOWN_SDK_TYPES
            else "unknown"
        )
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


def normalize_questions(questions: list[MetaculusQuestion]) -> list[CanonicalQuestion]:
    """Normalize a list of SDK questions; propagates the first failure."""
    return [normalize_question(q) for q in questions]
