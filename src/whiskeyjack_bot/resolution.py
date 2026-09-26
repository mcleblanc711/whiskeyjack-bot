"""Resolution observations: classify what Metaculus shows about a question's outcome (M4-801).

Pure: no network, no ledger. :func:`classify_resolution` reads one raw post payload -- the
``api_json`` the pinned SDK keeps, or a fixture -- and returns a :class:`ResolutionObservation`.
``lifecycle.record_resolution_observation`` persists one; ``resolution_ingest`` fetches and
drives both.

**The raw payload, not the SDK's typed view.** ``MetaculusQuestion.typed_resolution`` is
lossy in ways that matter to a score: an unrecognized string falls through unchanged,
``float()`` accepts ``"nan"`` and ``"infinity"``, and anything ``pendulum`` can parse becomes a
datetime. Each of those would turn a malformed outcome into a plausible one. The classifier
reads ``question.resolution`` itself and accepts only the shapes each supported type can
have, dispatching on the ledger's ``question_type`` literal -- never ``isinstance``, because
``DiscreteQuestion`` subclasses ``NumericQuestion``.

**Five kinds, one scorable.** ``resolved`` carries an outcome and is the only kind a score
may be computed from. ``annulled`` and ``ambiguous`` are the platform's cancellations.
``withheld`` is status ``resolved`` with a null value: Metaculus masks resolution values from
an account that did not predict on the question (``docs/openapi.yml``, "All Authenticated
Accounts"), and every one of ~270 resolved questions sampled with this project's bot token on
2026-09-14 came back that way. ``unresolved`` is a question whose status is not ``resolved``;
it is only worth recording after an earlier observation, where it means the platform
retracted a resolution. ``014_resolution_ingestion.sql`` enforces the same partition.

Error hygiene: :class:`ResolutionError` names fields and this module's own vocabulary, never a
value from the payload, and every sanitizing raise uses ``from None``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Final, Literal, get_args

from pydantic import ConfigDict, Field, ValidationError, model_validator

from whiskeyjack_bot.config import SupportedQuestionType, _StrictModel
from whiskeyjack_bot.validation_errors import sanitized_problems

OBSERVATION_SCHEMA_VERSION: Final = "1.0.0"

ResolutionKind = Literal["resolved", "annulled", "ambiguous", "withheld", "unresolved"]

# The platform's question statuses (the SDK's ``QuestionState``), spelled here so an unknown
# one is a refusal rather than an enum error raised from inside the SDK.
PlatformStatus = Literal["upcoming", "open", "closed", "resolved"]

RESOLUTION_KINDS: Final[frozenset[str]] = frozenset(get_args(ResolutionKind))
SCORABLE_KINDS: Final[frozenset[str]] = frozenset({"resolved"})
# The kinds that carry a record from `submitted` to `resolved`. A withheld value or a
# retraction is an observation about the platform, not a resolution of the forecast.
DEFINITIVE_KINDS: Final[frozenset[str]] = frozenset({"resolved", "annulled", "ambiguous"})

_PLATFORM_STATUSES: Final[frozenset[str]] = frozenset(get_args(PlatformStatus))
_SUPPORTED_TYPES: Final[frozenset[str]] = frozenset(get_args(SupportedQuestionType))
_CANCELLED: Final[frozenset[str]] = frozenset({"annulled", "ambiguous"})
# The SDK's ``OutOfBoundsResolution``: a continuous outcome known to lie outside the range.
_OUT_OF_BOUNDS: Final[frozenset[str]] = frozenset({"above_upper_bound", "below_lower_bound"})
_BINARY_OUTCOMES: Final[frozenset[str]] = frozenset({"yes", "no"})
# A decimal the way Metaculus stores one (``str(float)``): no sign but minus, no whitespace, no
# underscores, no ``nan``/``inf`` spellings -- all of which ``float()`` alone would accept.
_DECIMAL: Final = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?", re.ASCII)
_CANONICAL_UTC: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00", re.ASCII
)
_SQLITE_INT_MAX: Final = 2**63 - 1


class ResolutionError(Exception):
    """A resolution payload or observation is malformed, inconsistent or unsupported.

    The message names the field or rule, never a value: the payload is untrusted platform
    content, and a label or resolution string is exactly what a message must not reprint.
    """


class ResolutionObservation(_StrictModel):
    """One observation of one question's resolution state, as persisted and hashed.

    Every field is JSON-native, so the persisted form -- ``model_dump(mode="json")`` through
    :func:`canonical_json` -- re-validates to an equal model and the same
    :attr:`observation_sha256`. Timestamps are canonical UTC text rather than ``datetime``
    for that reason: a ``datetime`` carries ``fold``, which ISO-8601 cannot represent (the
    M1-305/M1-306 replay defect).

    The validator repeats the classifier's partition, so a stored snapshot that is read back
    and re-validated is held to the same rules that produced it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0.0"] = OBSERVATION_SCHEMA_VERSION
    question_id: int = Field(ge=1, le=_SQLITE_INT_MAX)
    post_id: int = Field(ge=1, le=_SQLITE_INT_MAX)
    question_type: SupportedQuestionType
    platform_status: PlatformStatus
    resolution: str | None
    kind: ResolutionKind
    outcome: str | None
    actual_resolve_time: str | None
    resolution_set_time: str | None

    @model_validator(mode="after")
    def _partition(self) -> ResolutionObservation:
        for name in ("actual_resolve_time", "resolution_set_time"):
            value = getattr(self, name)
            if value is not None and _CANONICAL_UTC.fullmatch(value) is None:
                raise ValueError(f"{name} must be a canonical UTC timestamp")
        if self.resolution is not None:
            _require_encodable(self.resolution, "resolution")
        expected_kind, expected_outcome = _kind_for(
            self.platform_status, self.resolution, self.question_type, options=None
        )
        if self.kind != expected_kind:
            raise ValueError("kind does not follow from platform_status and resolution")
        if self.outcome != expected_outcome:
            raise ValueError("outcome does not follow from kind and resolution")
        return self

    @property
    def scorable(self) -> bool:
        return self.kind in SCORABLE_KINDS

    @property
    def definitive(self) -> bool:
        return self.kind in DEFINITIVE_KINDS

    def snapshot_json(self) -> str:
        """The persisted form: what ``resolution_snapshot_json`` stores and the hash covers."""
        return canonical_json(self.model_dump(mode="json"))

    @property
    def observation_sha256(self) -> str:
        return sha256_text(self.snapshot_json())


def canonical_json(payload: object) -> str:
    """Serialize deterministically as ASCII: sorted keys, no whitespace, no NaN.

    ``ensure_ascii=True`` is what makes the result storable and hashable whatever the payload
    holds: a lone surrogate from provider JSON becomes a ``\\ud800`` escape rather than a
    ``UnicodeEncodeError`` at the SQLite bind or at ``encode("utf-8")`` (M1-305's rule).
    """
    try:
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise ResolutionError(
            "payload cannot be serialized canonically (non-JSON value, non-finite number, "
            "or nesting too deep)"
        ) from None


def sha256_text(text: str) -> str:
    """Digest of canonical ASCII text. Callers only pass :func:`canonical_json` output."""
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def classify_resolution(
    post: object, *, question_id: int, question_type: str
) -> ResolutionObservation:
    """Classify one question's resolution state from a raw Metaculus post payload.

    ``question_id`` and ``question_type`` come from the forecast record being resolved, not
    from the payload: the payload is checked *against* them. For a group post the subquestion
    is selected by ``question_id``. Raises :class:`ResolutionError` for every malformed,
    inconsistent or unsupported shape.
    """
    if type(question_id) is not int or not 1 <= question_id <= _SQLITE_INT_MAX:
        raise ResolutionError("question_id must be a positive integer")
    if type(question_type) is not str or question_type not in _SUPPORTED_TYPES:
        raise ResolutionError("question_type is not a supported question type")
    if type(post) is not dict:
        raise ResolutionError("post payload must be a JSON object")
    post_id = post.get("id")
    if type(post_id) is not int:
        raise ResolutionError("post payload id must be an integer")

    question = select_question(post, question_id)
    if question.get("type") != question_type:
        raise ResolutionError("payload question type does not match the forecast record")

    status = question.get("status")
    if type(status) is not str or status not in _PLATFORM_STATUSES:
        raise ResolutionError("question status is not a recognized platform status")
    if "resolution" not in question:
        # A missing key is a changed payload shape, not a masked value; reading it as
        # `withheld` would hide the change behind a kind that looks routine.
        raise ResolutionError("question payload has no resolution field")
    resolution = question["resolution"]
    if resolution is not None and type(resolution) is not str:
        raise ResolutionError("question resolution must be text or null")
    if resolution is not None:
        _require_encodable(resolution, "question resolution")

    options = _options(question)
    _options_required(question_type, options)
    kind, outcome = _kind_for(status, resolution, question_type, options=options)
    try:
        return ResolutionObservation(
            question_id=question_id,
            post_id=post_id,
            question_type=question_type,  # type: ignore[arg-type]  # gated above
            platform_status=status,  # type: ignore[arg-type]  # gated above
            resolution=resolution,
            kind=kind,
            outcome=outcome,
            actual_resolve_time=_utc_or_none(
                question.get("actual_resolve_time"), "actual_resolve_time"
            ),
            resolution_set_time=_utc_or_none(
                question.get("resolution_set_time"), "resolution_set_time"
            ),
        )
    except ValidationError as exc:
        raise ResolutionError(_sanitized(exc)) from None


def observation_from_snapshot(snapshot: object) -> ResolutionObservation:
    """Re-validate a stored ``resolution_snapshot_json`` (replay and the ledger reader)."""
    if type(snapshot) is not str:
        raise ResolutionError("resolution snapshot must be text")
    try:
        payload = json.loads(snapshot)
    except (ValueError, RecursionError):
        raise ResolutionError("resolution snapshot is not valid JSON") from None
    try:
        return ResolutionObservation.model_validate(payload)
    except ValidationError as exc:
        raise ResolutionError(_sanitized(exc)) from None


def select_question(post: dict[str, object], question_id: int) -> dict[str, object]:
    """The question object for ``question_id`` in a post payload: top-level, or one group member.

    Public because ``platform_scores`` reads the platform's scores out of the same stored
    payload and must select the same question the classifier did (M4-803). Raises
    :class:`ResolutionError` when the question is absent or listed twice.
    """
    question = post.get("question")
    if (
        type(question) is dict
        and question.get("id") == question_id
        and type(question.get("id")) is int
    ):
        return question
    group = post.get("group_of_questions")
    if type(group) is dict:
        members = group.get("questions")
        if type(members) is not list:
            raise ResolutionError("group post has no question list")
        matches = [
            member
            for member in members
            if type(member) is dict
            and type(member.get("id")) is int
            and member.get("id") == question_id
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ResolutionError("group post lists the forecast's question more than once")
    raise ResolutionError("post payload does not contain the forecast's question")


def _options(question: dict[str, object]) -> frozenset[str] | None:
    """Every label a multiple-choice question has ever had, or ``None`` if unreadable.

    ``all_options_ever`` rather than ``options``: Metaculus can add options to an open
    question, and the resolving label must be judged against the set the question had.
    """
    for key in ("all_options_ever", "options"):
        labels = question.get(key)
        if type(labels) is list and labels and all(type(label) is str for label in labels):
            return frozenset(labels)
    return None


def _kind_for(
    status: str,
    resolution: str | None,
    question_type: str,
    *,
    options: frozenset[str] | None,
) -> tuple[ResolutionKind, str | None]:
    """The partition itself, shared by the classifier and the model validator.

    ``options`` is ``None`` from the validator, which has no payload to check a label
    against; the classifier always passes what the question lists.
    """
    if status != "resolved":
        if resolution is not None:
            raise _Inconsistent("a question that is not resolved carries a resolution")
        return "unresolved", None
    if resolution is None:
        return "withheld", None
    if resolution in _CANCELLED:
        return ("annulled" if resolution == "annulled" else "ambiguous"), None
    if question_type == "binary":
        if resolution not in _BINARY_OUTCOMES:
            raise _Inconsistent("binary resolution is not yes or no")
    elif question_type == "multiple_choice":
        if resolution in _OUT_OF_BOUNDS or not resolution:
            raise _Inconsistent("multiple-choice resolution is not an option label")
        if options is not None and resolution not in options:
            raise _Inconsistent("multiple-choice resolution is not one of the question's options")
    elif question_type in ("numeric", "discrete"):
        if resolution not in _OUT_OF_BOUNDS:
            if _DECIMAL.fullmatch(resolution) is None or not math.isfinite(float(resolution)):
                raise _Inconsistent("continuous resolution is not a finite decimal")
    else:
        raise _Inconsistent("question type is not supported")
    return "resolved", resolution


class _Inconsistent(ResolutionError, ValueError):
    """Raised inside the shared partition; a ``ValueError`` so pydantic wraps it too."""


def _options_required(question_type: str, options: frozenset[str] | None) -> None:
    if question_type == "multiple_choice" and options is None:
        raise ResolutionError("multiple-choice question payload has no option labels")


def _utc_or_none(value: object, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ResolutionError(f"{field} must be text or null")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ResolutionError(f"{field} is not an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResolutionError(f"{field} must carry a timezone")
    try:
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (OverflowError, ValueError):
        raise ResolutionError(f"{field} is outside the representable range") from None


def _require_encodable(text: str, field: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise _Inconsistent(f"{field} is not valid Unicode text") from None


def _sanitized(exc: ValidationError) -> str:
    """Field locations and rule names only; pydantic's own text interpolates the input.

    The shared rendering (M0-008). A location part the schema did not author is now
    replaced by ``<withheld>`` rather than dropped, so a path keeps its shape.
    """
    parts = sanitized_problems(exc, ResolutionObservation)
    return "resolution observation is invalid (" + "; ".join(parts) + ")"
