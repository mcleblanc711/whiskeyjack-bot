"""The package-backed submission gateway, and the commands that reach it (M2-704).

This is the first module in the repository that can cause a live Metaculus post. Everything
upstream of it is already merged and has no caller: M2-701's approval boundary, M2-702's
idempotency keys, M2-703's ``SubmissionGateway`` protocol and ``SubmissionReceipt``, and
M1-603's whole submission vocabulary -- ``submitted`` / ``submission_uncertain`` /
``submission_failed``, ``submission_confirmed`` / ``submission_disconfirmed``,
:func:`lifecycle.record_submission_attempt`, :func:`lifecycle.record_submission_verification`
and :func:`lifecycle.unresolved_uncertainties`. This module is that caller.

The backlog criterion is *"success requires refetch confirmation; uncertain timeout blocks
blind retry"*, and both halves are structural here rather than promised.

**The pinned SDK blind-retries every POST four times, and that is closed here.**
``MetaculusClient._post_question_prediction`` carries ``@retry_with_exponential_backoff()``
(``max_retries=3``) whose ``retry_on_exceptions`` is ``requests.exceptions.RequestException``
-- which ``HTTPError`` subclasses. Measured against ``forecasting-tools==0.2.92``: **four
POSTs on a timeout, four on a 400.** A timed-out post that actually landed is re-posted
three more times, under one idempotency key, with no refetch in between: the blind retry
the criterion forbids, arriving from inside the dependency. ``metaculus/client.py``'s
``SingleAttemptPoster`` is where that is neutralized; this module depends only on the
:class:`MetaculusPoster` protocol below, which promises **one post per call**.

**Nothing here imports ``forecasting_tools``.** The gateway talks to :class:`MetaculusPoster`,
four methods wide. That keeps the SDK's import cost (streamlit, litellm -- the M1-308
lesson) off this path, makes every test a plain object rather than a patched client, and
keeps the transport out of the seam: this module never imports ``requests`` either, and
classifies transport exceptions through public attributes and a closed vocabulary of class
names walked over the MRO. ``tests/unit/test_metaculus_poster.py`` pins that vocabulary
against the real ``requests.exceptions`` classes, so drift is a red build rather than a
silent misclassification -- and ``src/`` gains no dependency, which matters because the
dependency slot is held by M1-311 this wave.

**Order of operations, and why it is this order.** Everything that can refuse refuses
before the post; everything after the post degrades rather than refusing. That is M1-303
round 4's rule ("refuse a caller mistake before the spend") joined to M1-312's ("after the
spend, an artifact failure degrades and the ledger write happens regardless"), and the
boundary between them is the single ``post`` call. A live post the ledger cannot record is
this product's primary failure mode.

**Verification is a before/after comparison, not a timestamp guess.** The gateway fetches
the question *before* posting, keeps the latest of the operator's own forecasts as a
baseline, posts once, then refetches. A confirmation requires a forecast entry that was
not in the baseline **and** whose values match what was posted. The handoff says "refetch
the question and verify ``previous_forecasts`` changed as expected"; the *changed* is why
the baseline exists, and it removes any dependence on this machine's clock agreeing with
Metaculus's. The one deviation from the handoff's literal wording is the field:
``MultipleChoiceQuestion`` never populates ``previous_forecasts`` in the pinned SDK (only
the binary and numeric subclasses override it), so ``api_json["question"]["my_forecasts"]
["history"]`` is read instead -- the one shape that is uniform across all three types. It
is untrusted provider JSON and is parsed defensively into this module's own error type.

**What the receipt can honestly carry.** The public post methods return ``None``, so a
*successful* post yields no status, body or headers at all. A *failed* one does: the SDK
re-raises a bare ``HTTPError`` ``from`` the original, so ``exc.__cause__.response`` is
reachable through public attributes and the status, an allowlisted header subset and a
truncated body are captured from it. That asymmetry -- evidence on failure, none on
success -- is exactly the receipt gap **M2-705** spikes, and this module is the evidence
that spike was waiting for. The SDK's own exception *message* embeds the full response body
and URL and is never stored: ``error_message`` is this module's constant text plus the
exception's type name.

**One state this module cannot record honestly, and it is filed.**
:func:`lifecycle.record_submission_attempt` derives its event from ``(success,
verified_by_refetch)``, and the pair has no member meaning "the post raised *and* the
refetch could not be performed, so the platform state is unknown". ``(False, False)`` is
``submission_failed`` and is terminal, which overclaims. The refetch is retried to make
that cell rare, and when it is still reached the row says so in ``error_message``; **M2-711**
is the filed item for the missing ledger state. It is named here rather than papered over,
because the alternative was writing ``verified_by_refetch=True`` for a refetch that never
happened.

**What this module does not do.** It does not build the payload -- M1-502/M1-503 own that,
and the payload is an input, supplied by ``--payload-file`` until they exist. It does not
close D33: an approval binds to ``forecast_sha256``, so it still cannot check that a
payload *is* the one the approval meant, and **M2-707** remains the filed item. What it can
check, it checks -- the payload's ``question_type`` must equal the record's stored one.
It does not reserve the idempotency key atomically before posting; ``require_key_unused``
is a read and says so, and **M2-708** is that item.

Error hygiene follows the project rule: :class:`LiveSubmissionError` never echoes a
payload, a stored value, or a provider string; sanitizing raises use ``from None``; every
malformed shape arrives as this module's own error type. It subclasses
:class:`submission_gateway.GatewayError` so a caller already handling the seam handles this
too. Filesystem paths are rendered, uniformly, under the settled M1-401 carve-out.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol, cast, get_args

from whiskeyjack_bot.config import AppConfig, SupportedQuestionType
from whiskeyjack_bot.forecast.record import ForecastRecordError
from whiskeyjack_bot.forecast.store import read_forecast_record
from whiskeyjack_bot.lifecycle import (
    FailureCode,
    LifecycleError,
    LifecycleEvent,
    SubmissionVerification,
    record_submission_verification,
    unresolved_uncertainties,
)
from whiskeyjack_bot.submission import (
    SubmissionError,
    require_key_unused,
    submission_key_for_approved_record,
)
from whiskeyjack_bot.submission_gateway import (
    GatewayError,
    SubmissionReceipt,
    SubmissionRequest,
    canonical_payload_json,
    record_receipt,
    write_live_artifact,
)

# Bumping this changes the envelope a reader of `submission_attempts.
# refetched_forecast_snapshot` must understand. It is not the artifact's version, which
# `submission_gateway.ARTIFACT_SCHEMA_VERSION` owns.
VERIFICATION_SCHEMA_VERSION = "1.0.0"

# The visible scheme tag on a derived live attempt id, written as a literal for
# `submission._KEY_PREFIX`'s reason: a computed tag agrees with its version by
# construction and proves nothing. `_assert_identity_spaces_are_distinct` checks the
# property that matters -- it can be confused with neither an idempotency key nor a
# dry-run attempt id, and all three are append-only claims about whether a post happened.
_LIVE_ATTEMPT_PREFIX = "wjlive-1-"

# Matches `lifecycle._MAX_IDENTIFIER` / `_MAX_BODY`, re-spelled for the reason the other
# four modules that restate them give (M1-608 is the item that pins them together). They
# are here so a receipt can be *pre-sanitized* to what the writer will accept: after a post
# has happened, a value the ledger refuses is a live post with no row.
_MAX_IDENTIFIER = 200
_MAX_BODY = 65536

# Metaculus's own bound on a binary forecast, enforced by the SDK's public method as a bare
# `ValueError`. Restated here so the refusal happens *before* the post and arrives as this
# module's error type rather than as an unhandled exception from inside the dependency.
_MIN_PROBABILITY = 0.001
_MAX_PROBABILITY = 0.999

# How far two floats may differ and still count as the same forecast. Small enough that it
# admits only representation noise, not a different forecast. Whether the platform
# round-trips a forecast value exactly is not knowable offline -- see the standing risk in
# docs/M2-NOTES.md; a disagreement reads as `refetch_mismatch` and therefore as uncertain,
# never as a false `submitted`.
_VALUE_TOLERANCE = 1e-9

# A multiple-choice payload must be a distribution. The tolerance is looser than
# `_VALUE_TOLERANCE` because it accumulates over the whole vector.
_CATEGORY_SUM_TOLERANCE = 1e-6

# Upper bound on how many options a multiple-choice payload may carry. Not a platform
# limit: it is what keeps the verification snapshot inside `_MAX_BODY` without a second
# serialization shape, and it refuses a caller mistake (a "distribution" over thousands of
# categories) before the post. Metaculus multiple-choice questions carry a few dozen.
_MAX_CATEGORIES = 64

# Response headers worth keeping, lowercased. The handoff says "allowlisted only"; these
# are the four that explain a failure without carrying content. `Authorization` is a
# *request* header and is not reachable here, but the allowlist is what makes that a
# property of the code rather than of the endpoint.
_ALLOWED_RESPONSE_HEADERS: frozenset[str] = frozenset(
    {"content-type", "date", "retry-after", "x-request-id"}
)

# The refetch is a read and reads may retry -- which is the whole line this module draws
# against the SDK's retry: writes must not, reads may. Retrying it is what makes the
# unrecordable cell M2-711 names rare rather than routine.
_REFETCH_ATTEMPTS = 3
_REFETCH_PAUSE_SECONDS = 2.0

_HEX_DIGITS: frozenset[str] = frozenset("0123456789abcdef")

_SUPPORTED_QUESTION_TYPES: frozenset[str] = frozenset(get_args(SupportedQuestionType))

# The transport's own module. Named as a string rather than imported: importing `requests`
# would make it a declared dependency of this package (the rule `tests/unit/
# test_dependency_pins.py` enforces for idna, asknews and httpx), and the seam should not
# know the transport at all -- it talks to `MetaculusPoster`. The classes below are matched
# by walking the exception's MRO, so an unlisted subclass of a listed class still resolves.
_TRANSPORT_MODULE = "requests.exceptions"

# `ConnectTimeout` is deliberately a *connection* error rather than a timeout, and the
# distinction is the one that matters here: a connect timeout never established a
# connection, so nothing was sent and the platform state is not in doubt. A `ReadTimeout`
# did send and is the genuinely ambiguous case. Both still refetch; only the recorded
# detail_code differs, so a misjudgement here costs audit fidelity and never safety.
_TIMEOUT_NAMES: frozenset[str] = frozenset({"Timeout", "ReadTimeout"})
_CONNECTION_NAMES: frozenset[str] = frozenset(
    {"ConnectionError", "ConnectTimeout", "ProxyError", "SSLError", "ChunkedEncodingError"}
)
_HTTP_NAMES: frozenset[str] = frozenset({"HTTPError", "TooManyRedirects"})

# What went wrong, in this module's own words. A closed vocabulary rather than the
# exception's class name, because the class name is the *dependency's* vocabulary and this
# value is stored in an append-only column.
LiveErrorType = Literal[
    "timeout",
    "connection_error",
    "http_error",
    "request_error",
    "payload_rejected",
    "internal_error",
]

_LIVE_ERROR_TYPES: frozenset[str] = frozenset(get_args(LiveErrorType))

# How each maps into `lifecycle.FailureCode`, which is the closed vocabulary the ledger
# stores. Written as a total mapping so a new member of `LiveErrorType` cannot be added
# without deciding what it means to the ledger; a unit test asserts totality.
_DETAIL_FOR_ERROR: dict[str, FailureCode] = {
    "timeout": "timeout",
    "connection_error": "provider_unavailable",
    "http_error": "http_error",
    "request_error": "provider_error",
    "payload_rejected": "schema_invalid",
    "internal_error": "internal_error",
}

# What a refetch established. Three-valued, unlike `lifecycle.VerificationOutcome`, and the
# third member is the point: "the refetch could not be performed" is not "the forecast is
# absent". Collapsing them is how a lost network connection becomes a permanent claim that
# a live forecast does not exist.
RefetchOutcome = Literal["confirmed", "absent", "mismatched", "unreadable"]

_REFETCH_OUTCOMES: frozenset[str] = frozenset(get_args(RefetchOutcome))


class LiveSubmissionError(GatewayError):
    """A live submission cannot be prepared, performed, verified, or recorded.

    Subclasses :class:`submission_gateway.GatewayError` for the reason that class
    subclasses :class:`submission.SubmissionError`: the caller is already handling the
    submission seam, and a subclass satisfies both halves at once -- ``except
    LiveSubmissionError`` still distinguishes this module, ``except GatewayError`` still
    catches the gateway layer, ``except SubmissionError`` still catches the whole seam.

    Same hygiene rule as its bases: the message never echoes a caller-supplied value, a
    payload, a stored value, or an underlying exception's text, and sanitizing raises use
    ``from None`` so nothing can be reprinted through a cause chain or a rendered
    traceback. Filesystem paths are the M1-401 carve-out and are rendered.
    """


def _assert_identity_spaces_are_distinct() -> None:
    """Fail at import if a live attempt id could be confused with another identifier.

    ``submission_gateway`` already checks its dry-run tag against the idempotency-key tag;
    this adds the third pair. All three are ``<tag>-<64 hex>`` minted from the same
    material, so only the tags keep them apart, and every one of them is an append-only
    claim about whether a live post happened.

    At import rather than in a test, for ``submission._assert_prefix_matches_version``'s
    reason: a guard only a test enforces is a guard the next module to import this one does
    not have.

    It compares **produced identifiers**, not the sibling modules' private prefix
    constants. Importing those to assert against would test the constants rather than the
    minters that use them -- the M1-303 lesson -- and would break the moment either module
    changed how it assembles one.
    """
    from whiskeyjack_bot.submission import KEY_LENGTH, submission_key
    from whiskeyjack_bot.submission_gateway import dry_run_attempt_id

    key = submission_key(
        tournament_id="probe",
        question_id=1,
        forecast_version=1,
        request_payload_sha256="0" * 64,
    )
    probe = _LIVE_ATTEMPT_PREFIX + "0" * 64
    for other in (key, dry_run_attempt_id(key)):
        if probe.startswith(other[: len(_LIVE_ATTEMPT_PREFIX)]) or other.startswith(
            _LIVE_ATTEMPT_PREFIX
        ):
            # A module literal, not caller or row content, so naming it is safe -- and it
            # is the only thing that makes the failure fixable.
            raise LiveSubmissionError(
                f"the live attempt prefix {_LIVE_ATTEMPT_PREFIX!r} is not distinct from "
                "an identifier another minter in this package produces; these identity "
                "spaces must not overlap"
            )
    if len(probe) > _MAX_IDENTIFIER or KEY_LENGTH > _MAX_IDENTIFIER:
        raise LiveSubmissionError(
            f"a derived identifier is longer than the {_MAX_IDENTIFIER}-character "
            "ledger identifier limit"
        )


_assert_identity_spaces_are_distinct()


class MetaculusPoster(Protocol):
    """What this module needs from Metaculus, and nothing more.

    Four members, and the narrowness is the design: ``forecast/generate.py``'s
    ``Forecaster`` protocol makes the same argument. A test double is a plain object with
    four methods instead of a patched SDK client, and this module never imports
    ``forecasting_tools`` -- so *"makes exactly one post call"* is provable by inspection
    of what is imported, the way M2-703's *"makes zero HTTP post calls"* is.

    **``post_*`` must post exactly once.** That is a contract on the implementation, not a
    hope: the pinned SDK's own methods post up to four times (see the module docstring),
    and ``metaculus.client.SingleAttemptPoster`` is the adapter that makes the promise
    true. The signatures below are the SDK's public ones verbatim, so that adapter is a
    pass-through rather than a translation.

    ``get_question_by_post_id`` returns ``object`` deliberately. Typing it as
    ``MetaculusQuestion`` would import the SDK into this module for a value that is read
    entirely through guarded attribute access anyway -- everything it returns is untrusted
    provider data.
    """

    def post_binary_question_prediction(
        self, question_id: int, prediction_in_decimal: float
    ) -> None: ...

    def post_numeric_question_prediction(
        self, question_id: int, cdf_values: list[float]
    ) -> None: ...

    def post_multiple_choice_question_prediction(
        self, question_id: int, options_with_probabilities: dict[str, float]
    ) -> None: ...

    def get_question_by_post_id(self, post_id: int) -> object: ...


@dataclass(frozen=True)
class BinaryPost:
    """A validated binary forecast, ready to hand to the poster."""

    probability_yes: float
    question_type: Literal["binary"] = "binary"


@dataclass(frozen=True)
class NumericPost:
    """A validated numeric forecast: the CDF, as a tuple so it cannot be mutated."""

    continuous_cdf: tuple[float, ...]
    question_type: Literal["numeric"] = "numeric"


@dataclass(frozen=True)
class MultipleChoicePost:
    """A validated multiple-choice forecast, as ordered pairs rather than a dict."""

    probability_yes_per_category: tuple[tuple[str, float], ...]
    question_type: Literal["multiple_choice"] = "multiple_choice"


# A tagged union. Each member carries the ``question_type`` literal it was built for, so
# every dispatch in this module is on that literal and never on the runtime class --
# CLAUDE.md's rule, written for the pinned SDK's ``DiscreteQuestion(NumericQuestion)`` and
# worth keeping even where nothing here subclasses anything. Tuples rather than lists so a
# plan cannot be mutated between validation and the post.
PostPlan = BinaryPost | NumericPost | MultipleChoicePost


def plan_from_payload(payload: Mapping[str, object], *, expected_cdf_points: int) -> PostPlan:
    """Validate a submission payload and return the single post call it authorizes.

    **The plan is derived from the payload's canonical rendering, not from the mapping the
    caller handed in.** :func:`submission_gateway.canonical_payload_json` is called first,
    and the plan is read out of ``json.loads`` of its result -- so what is posted is
    provably what was hashed, and a ``Mapping`` subclass cannot return one value to the
    digest and another to the poster (M1-203's ``_CountingQuestionType`` lesson, closed by
    construction rather than by counting reads).

    **The payload is the Metaculus wire body plus a discriminator.** The keys are the SDK's
    own -- ``probability_yes``, ``continuous_cdf``, ``probability_yes_per_category`` -- so
    M1-502/M1-503 have one shape to emit rather than a private format to translate into.
    ``question_type`` is the discriminator and dispatch is on that literal, never on which
    key happens to be present: CLAUDE.md's rule, and the reason ``DiscreteQuestion``
    subclassing ``NumericQuestion`` has never silently mis-normalized here.

    Exactly two keys are accepted. An unrecognized third is refused rather than ignored,
    because a payload carrying a key the operator expected to be posted, which this module
    silently drops, is a forecast that is not the one anyone reviewed.

    Every bound the SDK's public methods enforce is restated here, and that is deliberate
    duplication: the SDK raises a bare ``ValueError`` from inside the dependency, which is
    an error type callers do not handle, and it raises it at a point this module cannot
    distinguish from a failure that already posted. Refusing here means the refusal happens
    before any network call and arrives as :class:`LiveSubmissionError` (M1-303 round 4).
    """
    return plan_from_canonical_payload(
        _canonical_or_refuse(payload), expected_cdf_points=expected_cdf_points
    )


def _canonical_or_refuse(payload: Mapping[str, object]) -> str:
    """``canonical_payload_json``, with its refusal converted to this module's type."""
    try:
        return canonical_payload_json(payload)
    except LiveSubmissionError:
        raise
    except GatewayError as exc:
        raise _wrap_gateway(exc) from None


def plan_from_canonical_payload(canonical: str, *, expected_cdf_points: int) -> PostPlan:
    """The half of :func:`plan_from_payload` that takes the rendering already in hand.

    :meth:`MetaculusSubmissionGateway.submit` renders the payload once, digests *that*
    string, and derives the plan from the same string. Calling
    :func:`canonical_payload_json` a second time would compare two runs of the same code
    rather than the payload and its hash -- ``write_dry_run_artifact`` states the same rule
    for the same reason.
    """
    if type(expected_cdf_points) is not int or expected_cdf_points < 1:
        raise LiveSubmissionError("expected_cdf_points must be a positive integer")
    if type(canonical) is not str:
        raise LiveSubmissionError("canonical must be the canonical rendering of a payload")
    try:
        parsed = json.loads(canonical)
    except ValueError:
        raise LiveSubmissionError(
            "canonical is not valid JSON (detail withheld: it can echo the payload)"
        ) from None
    if not isinstance(parsed, dict):
        raise LiveSubmissionError("payload must be a JSON object")
    question_type = parsed.get("question_type")
    if type(question_type) is not str or question_type not in _SUPPORTED_QUESTION_TYPES:
        raise LiveSubmissionError(
            "payload.question_type must be one of "
            f"{', '.join(sorted(_SUPPORTED_QUESTION_TYPES))} "
            "(offending value withheld: it is payload content)"
        )
    wire_key = _WIRE_KEY_FOR_TYPE[question_type]
    extra = set(parsed) - {"question_type", wire_key}
    if extra:
        # Names no key: an object key is payload content.
        raise LiveSubmissionError(
            f"a {question_type} payload carries exactly question_type and {wire_key}; "
            "this one carries more, and a key this module would drop is a forecast nobody "
            "reviewed (offending keys withheld)"
        )
    if wire_key not in parsed:
        raise LiveSubmissionError(f"a {question_type} payload must carry {wire_key}")
    value = parsed[wire_key]
    if question_type == "binary":
        return BinaryPost(probability_yes=_require_probability(value, wire_key))
    if question_type == "numeric":
        return NumericPost(
            continuous_cdf=_require_cdf(value, wire_key, expected_cdf_points=expected_cdf_points)
        )
    return MultipleChoicePost(probability_yes_per_category=_require_categories(value, wire_key))


# Which wire key each question type carries. Derived nowhere and written once: it is the
# Metaculus request body's own vocabulary, taken from the pinned SDK's public post methods.
_WIRE_KEY_FOR_TYPE: dict[str, str] = {
    "binary": "probability_yes",
    "numeric": "continuous_cdf",
    "multiple_choice": "probability_yes_per_category",
}


def _require_probability(value: object, field: str) -> float:
    """Return a finite probability inside Metaculus's own bounds, or raise.

    ``bool`` is refused before ``int`` because it subclasses it; ``True`` would otherwise
    become a forecast of 1.0, which is outside the bound anyway but would have been read as
    a number the operator wrote.
    """
    number = _exact_number(value)
    if number is None:
        raise LiveSubmissionError(f"payload.{field} must be a number (offending value withheld)")
    if not math.isfinite(number):
        raise LiveSubmissionError(f"payload.{field} must be a finite number")
    if not _MIN_PROBABILITY <= number <= _MAX_PROBABILITY:
        # The bounds are this module's own literals and the platform's; naming them is what
        # makes the refusal fixable, and neither is payload content.
        raise LiveSubmissionError(
            f"payload.{field} must be between {_MIN_PROBABILITY} and {_MAX_PROBABILITY}, "
            "which is what Metaculus accepts (offending value withheld)"
        )
    return number


def _require_cdf(value: object, field: str, *, expected_cdf_points: int) -> tuple[float, ...]:
    """Return a validated CDF, or raise.

    Three rules, all of them the platform's: the exact point count, every value in
    ``[0, 1]``, and monotonically non-decreasing. The count comes from
    ``forecast.expected_cdf_points``, which ``config.py`` pins to ``Literal[201]`` and
    describes as "a hard error, not a tunable" -- it is passed in rather than restated so
    the value keeps one owner.
    """
    if type(value) is not list:
        raise LiveSubmissionError(f"payload.{field} must be a JSON array")
    if len(value) != expected_cdf_points:
        raise LiveSubmissionError(
            f"payload.{field} must hold exactly {expected_cdf_points} points; "
            f"this one holds {len(value)}"
        )
    points: list[float] = []
    for item in value:
        number = _exact_number(item)
        if number is None:
            raise LiveSubmissionError(
                f"payload.{field} must hold only numbers (offending value withheld)"
            )
        if not math.isfinite(number):
            raise LiveSubmissionError(f"payload.{field} must hold only finite numbers")
        if not 0.0 <= number <= 1.0:
            raise LiveSubmissionError(
                f"payload.{field} must hold only values between 0 and 1 (offending value withheld)"
            )
        points.append(number)
    if any(later < earlier for earlier, later in zip(points, points[1:])):
        raise LiveSubmissionError(
            f"payload.{field} must be monotonically non-decreasing; a CDF that decreases "
            "describes a negative probability"
        )
    return tuple(points)


def _require_categories(value: object, field: str) -> tuple[tuple[str, float], ...]:
    """Return validated multiple-choice probabilities, or raise.

    The sum rule is a **stricter reading** and is recorded as one: the SDK checks nothing
    here and the platform's behaviour for a non-normalized vector is not knowable offline.
    A vector that does not sum to one is a caller mistake with two possible outcomes on the
    platform -- silent renormalization, or rejection -- and neither is something an
    attribution record should have to explain after the fact. Refusing costs nothing,
    because it happens before the post.

    The ordering is the payload's own key order after canonical rendering, which is
    ``sort_keys=True`` and therefore deterministic; the comparison in
    :func:`_comparable_values` does not depend on it.
    """
    if not isinstance(value, dict):
        raise LiveSubmissionError(f"payload.{field} must be a JSON object")
    if not value:
        raise LiveSubmissionError(f"payload.{field} must name at least one option")
    if len(value) > _MAX_CATEGORIES:
        raise LiveSubmissionError(
            f"payload.{field} names more than {_MAX_CATEGORIES} options, which is more "
            "than any Metaculus multiple-choice question carries"
        )
    pairs: list[tuple[str, float]] = []
    for label, probability in value.items():
        if type(label) is not str or not label.strip():
            raise LiveSubmissionError(
                f"payload.{field} keys must be non-blank option labels (offending key withheld)"
            )
        pairs.append((label, _require_probability(probability, field)))
    total = math.fsum(probability for _, probability in pairs)
    if abs(total - 1.0) > _CATEGORY_SUM_TOLERANCE:
        raise LiveSubmissionError(
            f"payload.{field} must be a distribution: its probabilities must sum to 1 "
            f"within {_CATEGORY_SUM_TOLERANCE} (observed sum withheld)"
        )
    return tuple(pairs)


@dataclass(frozen=True)
class ForecastEntry:
    """One of the operator's own forecasts, as the platform reports it.

    ``values`` is Metaculus's ``forecast_values`` verbatim, whose meaning depends on the
    question type: ``[P(no), P(yes)]`` for binary, the CDF for numeric, one probability per
    option in the question's option order for multiple choice.
    """

    start_time: float
    values: tuple[float, ...]


@dataclass(frozen=True)
class ForecastHistory:
    """Every forecast of the operator's that the platform reports for one question.

    An **empty** history and an **unreadable** one are different answers and are never
    collapsed: ``read_my_forecasts`` returns this type for the first and ``None`` for the
    second. Treating a response this module could not parse as "the operator has never
    forecast here" is how a live forecast becomes a permanent claim that it does not exist.
    """

    entries: tuple[ForecastEntry, ...]

    @property
    def latest(self) -> ForecastEntry | None:
        """The entry with the greatest ``start_time``, or ``None`` for an empty history."""
        if not self.entries:
            return None
        return max(self.entries, key=lambda entry: entry.start_time)


def read_my_forecasts(question: object) -> ForecastHistory | None:
    """Extract the operator's own forecast history from a refetched question.

    ``None`` means *unreadable*, never *empty* -- see :class:`ForecastHistory`.

    **This is the deviation from the handoff's literal wording, and it is deliberate.** The
    handoff says to "verify ``previous_forecasts`` changed as expected". In the pinned SDK
    that field is populated only by ``BinaryQuestion`` and ``NumericQuestion``;
    ``MultipleChoiceQuestion`` inherits the base class's ``None`` and never fills it in. A
    verification rule built on it would silently never confirm a multiple-choice
    submission, which is the worst available failure: an honest post recorded as uncertain
    forever. ``api_json["question"]["my_forecasts"]["history"]`` is the shape all three
    subclasses read *from*, so it is the one basis that is uniform.

    Every read is guarded. This is untrusted provider JSON reached through attribute access
    on an object from a third-party package, so ``getattr`` can run arbitrary code and any
    key can hold anything; nothing here may raise, because a caller reaches it *after* a
    post has been made and an exception there would cost the ledger row.

    A **missing** ``my_forecasts`` or a null ``history`` is an empty history rather than an
    unreadable one: that is what the API returns for a question the operator has never
    forecast on, and it is the ordinary case for a first submission.
    """
    try:
        api_json = getattr(question, "api_json", None)
    except Exception:
        return None
    if not isinstance(api_json, dict):
        return None
    inner = api_json.get("question")
    if not isinstance(inner, dict):
        return None
    mine = inner.get("my_forecasts")
    if mine is None:
        return ForecastHistory(())
    if not isinstance(mine, dict):
        return None
    history = mine.get("history")
    if history is None:
        return ForecastHistory(())
    if not isinstance(history, list):
        return None
    entries: list[ForecastEntry] = []
    for item in history:
        if not isinstance(item, dict):
            return None
        start = _finite_number(item.get("start_time"))
        if start is None:
            return None
        raw_values = item.get("forecast_values")
        if not isinstance(raw_values, list):
            return None
        values: list[float] = []
        for candidate in raw_values:
            number = _finite_number(candidate)
            if number is None:
                return None
            values.append(number)
        entries.append(ForecastEntry(start_time=start, values=tuple(values)))
    return ForecastHistory(tuple(entries))


def _finite_number(value: object) -> float | None:
    """Return a finite float, or ``None``. Never raises. ``bool`` is not a number here."""
    number = _exact_number(value)
    return number if number is not None and math.isfinite(number) else None


def _exact_number(value: object) -> float | None:
    """Return ``value`` as a float if it is exactly an ``int`` or ``float``, else ``None``.

    ``type(value) is ...`` rather than ``isinstance``: ``bool`` subclasses ``int``, so
    ``True`` would otherwise be read as the number 1 -- a forecast the operator did not
    write. The branches are spelled out rather than collapsed into ``float(value)`` so the
    exactness of the gate is what the type checker sees too.

    A very large ``int`` raises ``OverflowError`` from ``float()``; that is caught, because
    every caller is either validating a payload before a post or reading provider JSON
    after one, and neither may raise from an unhandled conversion.
    """
    if type(value) is float:
        return value
    if type(value) is int:
        try:
            return float(value)
        except OverflowError:
            return None
    return None


def expected_values(plan: PostPlan) -> tuple[float, ...]:
    """The values a refetch must show, projected out of what was posted.

    One projection per type, and each is the narrowest thing that is comparable against
    ``forecast_values``:

    - **binary** -- the single ``P(yes)``. The platform reports ``[P(no), P(yes)]``, and
      comparing only the value that was sent avoids asserting anything about how the
      complement is computed.
    - **numeric** -- the CDF as sent, element for element.
    - **multiple choice** -- the probabilities **sorted**, compared as a multiset. The
      platform reports one value per option in the *question's* option order, and this
      module never reads the option list: ``MultipleChoiceQuestion.options`` exists, but
      making verification depend on it would add a second thing that must be readable for
      a post to be confirmable. Two options carrying the same probability are
      indistinguishable under a multiset comparison, which is a genuine weakening and is
      the price of not needing the option order; a *different distribution* is still
      caught.
    """
    if plan.question_type == "binary":
        return (plan.probability_yes,)
    if plan.question_type == "numeric":
        return plan.continuous_cdf
    return tuple(sorted(probability for _, probability in plan.probability_yes_per_category))


def observed_values(question_type: str, entry: ForecastEntry) -> tuple[float, ...] | None:
    """The same projection, taken from what the platform reported. ``None`` if it cannot be.

    Keyed on the ``question_type`` string rather than on a plan, because
    :func:`verify_uncertain_attempt` runs this rule from a *stored* snapshot, long after
    the plan that produced it is gone. One definition, two callers -- the alternative was
    two implementations of "did the refetch show what we posted", which is exactly the kind
    of pair that agrees at review time and drifts afterwards.
    """
    if question_type == "binary":
        return (entry.values[1],) if len(entry.values) == 2 else None
    if question_type == "numeric":
        return entry.values
    return tuple(sorted(entry.values))


def values_match(expected: Sequence[float], observed: Sequence[float]) -> bool:
    """Whether two value vectors describe the same forecast, within representation noise."""
    if len(expected) != len(observed):
        return False
    return all(abs(a - b) <= _VALUE_TOLERANCE for a, b in zip(expected, observed))


@dataclass(frozen=True)
class RefetchResult:
    """What one refetch established about a post.

    ``outcome`` is four-valued where ``lifecycle.VerificationOutcome`` is two-valued, and
    the extra members are the point. ``mismatched`` is not ``absent``: something is there
    and it is not what was sent, which is a different thing for an operator to act on.
    ``unreadable`` is not ``absent`` either: a refetch that could not be performed observed
    nothing, and recording that as "the forecast is not there" would let a lost connection
    become a permanent claim about a live forecast.
    """

    outcome: RefetchOutcome
    detail_code: FailureCode | None
    history: ForecastHistory | None


def classify_refetch(
    *,
    question_type: str,
    expected: Sequence[float],
    baseline_latest_start_time: float | None,
    observed: ForecastHistory | None,
) -> RefetchResult:
    """Decide what a refetch established, given the baseline taken before the post.

    Confirmation requires **both** halves: an entry that was not in the baseline, and
    values matching what was posted. The first half is what makes this a test of *this*
    post rather than of any post -- a question the operator had already forecast on would
    otherwise confirm a submission that never landed. The second is what makes it a test of
    this *forecast*.

    "Not in the baseline" is ``start_time`` strictly greater than the baseline's latest.
    Metaculus stamps each forecast with its own start time, so a post that landed advances
    it; comparing against a baseline rather than against this machine's clock is what keeps
    the rule independent of clock agreement between here and the platform.
    """
    if observed is None:
        return RefetchResult("unreadable", "malformed_response", None)
    latest = observed.latest
    if latest is None:
        return RefetchResult("absent", "refetch_missing", observed)
    if baseline_latest_start_time is not None and latest.start_time <= baseline_latest_start_time:
        return RefetchResult("absent", "refetch_missing", observed)
    actual = observed_values(question_type, latest)
    if actual is None or not values_match(expected, actual):
        return RefetchResult("mismatched", "refetch_mismatch", observed)
    return RefetchResult("confirmed", None, observed)


def build_verification_snapshot(
    *,
    question_type: str,
    expected: Sequence[float],
    baseline_entry_count: int,
    baseline_latest_start_time: float | None,
    result: RefetchResult,
) -> str:
    """Render what goes into ``submission_attempts.refetched_forecast_snapshot``.

    **This is the evidence for whatever the row claims**, and it is written so that a later
    refetch can be judged by the same rule without re-reading anything else. It carries the
    baseline the comparison was made against, the values that were expected, the values
    that were seen, and the verdict -- so :func:`verify_uncertain_attempt` reconstructs the
    comparison from the ledger alone, and an auditor can re-run it by hand.

    The rendering is M1-305's rule verbatim, the same spelling every other canonical form
    in this package uses: ``json.dumps(..., ensure_ascii=True, sort_keys=True,
    separators=(",", ":"), allow_nan=False)``. Every value in it is a plain ``float``,
    ``int``, ``str``, ``bool`` or ``None`` assembled here, so the render cannot fail on
    content -- but the guard stays, because this function is called *after* a post and an
    exception here would cost the ledger row.

    The value counts are bounded by :func:`plan_from_payload` (one value for binary,
    ``expected_cdf_points`` for numeric, :data:`_MAX_CATEGORIES` for multiple choice), so
    the rendering cannot approach ``_MAX_BODY``. The fallback below is unreachable given
    those bounds and exists anyway: a snapshot that could not be rendered must degrade to a
    smaller one, never to an exception.
    """
    latest = result.history.latest if result.history is not None else None
    envelope: dict[str, object] = {
        "verification_schema_version": VERIFICATION_SCHEMA_VERSION,
        "question_type": question_type,
        "outcome": result.outcome,
        "expected_values": list(expected),
        "baseline": {
            "entry_count": baseline_entry_count,
            "latest_start_time": baseline_latest_start_time,
        },
        "observed": (
            None
            if result.history is None
            else {
                "entry_count": len(result.history.entries),
                "latest_start_time": None if latest is None else latest.start_time,
                "latest_values": None if latest is None else list(latest.values),
            }
        ),
    }
    rendered = _render_snapshot(envelope)
    if rendered is not None and len(rendered) <= _MAX_BODY:
        return rendered
    reduced = _render_snapshot(
        {
            "verification_schema_version": VERIFICATION_SCHEMA_VERSION,
            "question_type": question_type,
            "outcome": result.outcome,
            "values_omitted": True,
        }
    )
    # Names nothing and cannot fail: every value in the reduced envelope is a literal.
    return reduced if reduced is not None else "{}"


def _render_snapshot(envelope: Mapping[str, object]) -> str | None:
    """Canonical JSON, or ``None``. Never raises -- see :func:`build_verification_snapshot`."""
    try:
        return json.dumps(
            envelope,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        return None


def read_verification_snapshot(snapshot: object) -> dict[str, object]:
    """Parse a stored verification snapshot back, or raise.

    Admits exactly what :func:`build_verification_snapshot` emits, which is why it refuses
    the non-finite JSON constants ``json.loads`` accepts by default: a reader that admits
    more than its writer produces is not reading the format it documents
    (``research/artifacts.py`` round 1, finding 7).
    """
    if type(snapshot) is not str or not snapshot.strip():
        raise LiveSubmissionError("the stored verification snapshot is missing or blank")
    try:
        parsed = json.loads(snapshot, parse_constant=_reject_json_constant)
    except LiveSubmissionError:
        raise
    except ValueError:
        raise LiveSubmissionError("the stored verification snapshot is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise LiveSubmissionError("the stored verification snapshot is not a JSON object")
    version = parsed.get("verification_schema_version")
    if type(version) is not str or not version.strip():
        raise LiveSubmissionError(
            "the stored verification snapshot names no schema version, so it cannot be read"
        )
    if version != VERIFICATION_SCHEMA_VERSION:
        # This module's own literal on one side; the other is named only as "unsupported",
        # because a stored value is content until it is proven to be a member of a
        # vocabulary this module defines.
        raise LiveSubmissionError(
            "the stored verification snapshot was written under an unsupported schema "
            f"version (this build reads {VERIFICATION_SCHEMA_VERSION})"
        )
    return parsed


def _reject_json_constant(token: str) -> object:
    """Refuse ``NaN``/``Infinity``/``-Infinity`` while parsing a stored snapshot."""
    raise LiveSubmissionError(
        "the stored verification snapshot contains a non-finite JSON constant, which this "
        "format does not permit"
    )


def classify_error(exc: BaseException) -> LiveErrorType:
    """Name what went wrong, in this module's own closed vocabulary.

    **Nothing is imported to do this.** ``requests`` is the pinned SDK's transport, not
    this package's dependency, and importing it here would make it one -- the rule
    ``tests/unit/test_dependency_pins.py`` enforces for ``idna``, ``asknews`` and
    ``httpx``. It would also put the transport inside a seam whose whole point is that it
    talks to :class:`MetaculusPoster`. So the exception's own class hierarchy is walked and
    matched by name, restricted to classes declared in the transport's module so that a
    same-named exception from anywhere else cannot be mistaken for one.

    The **MRO** is walked rather than the exception's own class, so a subclass the pinned
    version does not have still resolves to its nearest listed ancestor rather than falling
    through to ``internal_error``. ``tests/unit/test_metaculus_poster.py`` pins the
    vocabulary against the real classes, so a rename in a future release is a red build
    rather than a silent misclassification.

    A bare ``ValueError`` maps to ``payload_rejected``: the SDK's public post methods raise
    one for an out-of-range forecast, *before* reaching the HTTP call.
    :func:`plan_from_payload` restates every one of those bounds so this should be
    unreachable, and it is kept because "unreachable" is a claim about today's SDK.
    """
    for klass in type(exc).__mro__:
        if getattr(klass, "__module__", "") != _TRANSPORT_MODULE:
            continue
        name = klass.__name__
        if name in _TIMEOUT_NAMES:
            return "timeout"
        if name in _CONNECTION_NAMES:
            return "connection_error"
        if name in _HTTP_NAMES:
            return "http_error"
        if name == "RequestException":
            return "request_error"
    if isinstance(exc, ValueError):
        return "payload_rejected"
    return "internal_error"


def http_details(exc: BaseException) -> tuple[int | None, str | None, str | None]:
    """Return ``(http_status, response_body, response_headers)`` from a failed post.

    **The SDK throws the response away and this is how it comes back.**
    ``raise_for_status_with_additional_info`` catches ``requests``' own ``HTTPError`` and
    re-raises a *new* one built from a formatted message, with no ``response=`` -- so
    ``exc.response`` is ``None``. It does chain the original with ``from``, and that one
    carries the response, so ``exc.__cause__.response`` is reachable. Verified by execution
    against ``forecasting-tools==0.2.92``: status ``429`` and its ``Retry-After`` header
    both came back this way.

    Everything is read through ``getattr`` inside a guard and every failure yields
    ``None``: this runs after a post, on an object from a third-party package, and it must
    not be able to raise.

    Headers are **allowlisted**, per the handoff's "allowlisted only". The body is
    truncated to what the ledger column accepts. Neither is the SDK's exception *message*,
    which embeds the full response text and the request URL and is never stored anywhere.
    """
    response = _response_of(exc)
    if response is None:
        return None, None, None
    return _status_of(response), _body_of(response), _headers_of(response)


def _response_of(exc: BaseException) -> object | None:
    for candidate in (exc, getattr(exc, "__cause__", None)):
        if candidate is None:
            continue
        try:
            response = getattr(candidate, "response", None)
        except Exception:
            continue
        if response is not None:
            return cast("object", response)
    return None


def _status_of(response: object) -> int | None:
    try:
        status = getattr(response, "status_code", None)
    except Exception:
        return None
    if type(status) is not int or not 100 <= status <= 599:
        return None
    return status


def _body_of(response: object) -> str | None:
    try:
        text = getattr(response, "text", None)
    except Exception:
        return None
    return storable_text(text, _MAX_BODY)


def _headers_of(response: object) -> str | None:
    try:
        headers = getattr(response, "headers", None)
        items = list(headers.items()) if headers is not None else []
    except Exception:
        return None
    kept: dict[str, str] = {}
    for name, value in items:
        if type(name) is not str or name.lower() not in _ALLOWED_RESPONSE_HEADERS:
            continue
        cleaned = storable_text(value, _MAX_IDENTIFIER)
        if cleaned is not None:
            kept[name.lower()] = cleaned
    if not kept:
        return None
    rendered = _render_snapshot(kept)
    return None if rendered is None or len(rendered) > _MAX_BODY else rendered


def storable_text(value: object, limit: int) -> str | None:
    """Coerce provider text into something the ledger is guaranteed to accept.

    **This is what keeps a completed post recordable.** ``lifecycle`` refuses text that is
    empty, over its column bound, or not UTF-8 encodable, and a refusal at that point is a
    live post with no ledger row -- this product's primary failure mode. So the value is
    cleaned rather than validated: NUL and unpaired surrogates become U+FFFD, the result is
    truncated with a visible ellipsis, and anything that is not text at all, or is blank
    once cleaned, becomes ``None``.

    It never raises, and that is the requirement rather than a nicety.
    """
    if type(value) is not str:
        return None
    cleaned = value.replace("\x00", "�").encode("utf-8", "replace").decode("utf-8", "replace")
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned if cleaned.strip() else None


# What each error type is recorded as, in this module's own words. The exception's own
# message is never used: the SDK's `HTTPError` text embeds the full response body and the
# request URL, and both are content this row must not carry (verified by execution).
_MESSAGE_FOR_ERROR: dict[str, str] = {
    "timeout": "the post timed out; whether Metaculus recorded it is decided by the refetch",
    "connection_error": "the connection to Metaculus failed before a response was received",
    "http_error": "Metaculus rejected the post with an HTTP error status",
    "request_error": "the post failed in the HTTP client",
    "payload_rejected": "the submission payload was rejected before it was sent",
    "internal_error": "the post failed for a reason this gateway does not classify",
}

# Appended when the post failed *and* the refetch could not be performed. That pair has no
# honest encoding in `lifecycle`'s `(success, verified_by_refetch)` -- `(False, False)` is
# `submission_failed`, which is terminal and claims more than is known. **M2-711** is the
# filed item; until it lands the row says so in words.
_UNESTABLISHED_NOTE = (
    " the refetch could not be performed either, so the platform state was not established"
)


@dataclass(frozen=True)
class LiveSubmissionOutcome:
    """A live receipt and the ``detail_code`` the ledger needs alongside it.

    :class:`submission_gateway.SubmissionReceipt` has no ``detail_code`` field and should
    not grow one: the code is a *ledger* vocabulary (``lifecycle.FailureCode``) and the
    receipt is the gateway's sanitized record of a call. Carrying it beside the receipt
    keeps both shapes honest and stops :func:`post_approved_forecast` from having to
    re-derive from a rendered snapshot what this module already knew.
    """

    receipt: SubmissionReceipt
    detail_code: FailureCode | None


class MetaculusSubmissionGateway:
    """A gateway that posts to Metaculus exactly once and verifies by refetch.

    Satisfies :class:`submission_gateway.SubmissionGateway`. Like
    :class:`~submission_gateway.DryRunSubmissionGateway` it holds **no ledger connection**
    and reads **no configuration**: approval, key derivation and the spent-key check all
    happen in :func:`post_approved_forecast`, in front of this, because putting the
    approval boundary inside the thing it gates is not a boundary.

    ``poster`` must post at most once per call -- see :class:`MetaculusPoster`. That is the
    contract ``metaculus.client.SingleAttemptPoster`` exists to keep against a dependency
    that otherwise posts four times.

    ``expected_cdf_points`` is a bound, not a configuration read: ``config.numeric_calibration.
    expected_cdf_points`` is its one owner and :func:`post_approved_forecast` passes it in.

    ``clock`` and ``sleep`` are injected so the receipt is reproducible and so the refetch
    retry costs a test nothing.
    """

    def __init__(
        self,
        *,
        poster: MetaculusPoster,
        expected_cdf_points: int = 201,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        refetch_attempts: int = _REFETCH_ATTEMPTS,
        refetch_pause_seconds: float = _REFETCH_PAUSE_SECONDS,
    ) -> None:
        if poster is None:
            raise LiveSubmissionError("poster is required")
        if type(expected_cdf_points) is not int or expected_cdf_points < 1:
            raise LiveSubmissionError("expected_cdf_points must be a positive integer")
        if clock is not None and not callable(clock):
            raise LiveSubmissionError("clock must be callable")
        if sleep is not None and not callable(sleep):
            raise LiveSubmissionError("sleep must be callable")
        if type(refetch_attempts) is not int or refetch_attempts < 1:
            raise LiveSubmissionError("refetch_attempts must be a positive integer")
        if type(refetch_pause_seconds) not in (int, float) or refetch_pause_seconds < 0:
            raise LiveSubmissionError("refetch_pause_seconds must be a non-negative number")
        self._poster = poster
        self._expected_cdf_points = expected_cdf_points
        self._clock: Callable[[], datetime] = _utcnow if clock is None else clock
        self._sleep: Callable[[float], None] = time.sleep if sleep is None else sleep
        self._refetch_attempts = refetch_attempts
        self._refetch_pause_seconds = float(refetch_pause_seconds)

    def submit(self, request: SubmissionRequest) -> SubmissionReceipt:
        """Post once, verify by refetch, and return the sanitized receipt."""
        return self.submit_with_detail(request).receipt

    def submit_with_detail(self, request: SubmissionRequest) -> LiveSubmissionOutcome:
        """Post once, verify by refetch, and return the receipt with its ledger detail code.

        **Everything before the post can refuse; nothing after it may.** The single
        ``post`` call below is the boundary between M1-303 round 4's rule (refuse a caller
        mistake before the spend) and M1-312's (after the spend, degrade and record
        anyway). Read the sequence with that in mind: validation, the payload render, the
        baseline fetch and the identity check all raise; from the post onwards, every
        failure becomes a field on the receipt.

        The baseline fetch is not decoration. It is what makes the handoff's "verify
        ``previous_forecasts`` **changed**" a comparison rather than a guess, it is what
        catches a question whose ids do not match the record before anything is posted to
        it, and a question whose history cannot be read is one this gateway refuses to post
        to at all -- because success requires confirmation, and a post that could never be
        confirmed would be recorded as uncertain the moment it was made.
        """
        # Exact type for `record_submission_attempt`'s reason: a subclass can shadow a
        # field with a property, turning each read below into caller code that can raise
        # anything, at any point between validation and the post.
        if type(request) is not SubmissionRequest:
            raise LiveSubmissionError("request must be a SubmissionRequest")
        record_id = _require_identifier(request.forecast_record_id, "forecast_record_id")
        question_id = _require_positive_int(request.question_id, "question_id")
        post_id = _require_positive_int(request.post_id, "post_id")
        key = _require_safe_key(request.idempotency_key)
        payload = request.payload

        # Rendered once; the digest and the plan both come from this one string.
        canonical = _canonical_or_refuse(payload)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        plan = plan_from_canonical_payload(canonical, expected_cdf_points=self._expected_cdf_points)

        baseline_question = self._fetch_or_refuse(post_id)
        self._require_matching_identity(baseline_question, question_id=question_id, post_id=post_id)
        self._require_open(baseline_question)
        baseline = read_my_forecasts(baseline_question)
        if baseline is None:
            raise LiveSubmissionError(
                "the question's own forecast history could not be read, so a post to it "
                "could never be confirmed; nothing was posted"
            )

        requested = _require_aware_utc(self._clock(), "clock()")
        error: BaseException | None = None
        try:
            self._post(plan, question_id)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a receipt field
            error = exc
        completed = _require_aware_utc(self._clock(), "clock()")
        if completed < requested:
            # A clock that ran backwards must not cost the row. `record_submission_attempt`
            # refuses a reversed pair outright, and refusing here would leave a completed
            # post unrecordable -- so the pair is clamped and the receipt stays writable.
            completed = requested

        result = self._refetch(post_id, question_id=question_id, plan=plan, baseline=baseline)
        return self._receipt_for(
            record_id=record_id,
            key=key,
            digest=digest,
            plan=plan,
            baseline=baseline,
            result=result,
            requested=requested,
            completed=completed,
            error=error,
        )

    def _post(self, plan: PostPlan, question_id: int) -> None:
        """Make the one post this gateway is allowed to make, dispatching on the literal."""
        if plan.question_type == "binary":
            self._poster.post_binary_question_prediction(question_id, plan.probability_yes)
            return
        if plan.question_type == "numeric":
            self._poster.post_numeric_question_prediction(question_id, list(plan.continuous_cdf))
            return
        self._poster.post_multiple_choice_question_prediction(
            question_id, dict(plan.probability_yes_per_category)
        )

    def _fetch_or_refuse(self, post_id: int) -> object:
        """Fetch the question before posting, or refuse. Nothing has been spent yet."""
        try:
            return self._poster.get_question_by_post_id(post_id)
        except Exception as exc:  # noqa: BLE001 - any transport failure is one refusal here
            # from None and a constant message: the SDK's HTTPError text embeds the full
            # response body and the request URL.
            raise LiveSubmissionError(
                f"the question could not be fetched before posting ({classify_error(exc)}); "
                "nothing was posted"
            ) from None

    def _require_matching_identity(
        self, question: object, *, question_id: int, post_id: int
    ) -> None:
        """Refuse unless the fetched question is the one this record is about.

        **This is what makes carrying both ids safe.** The pinned SDK posts by *question*
        id and fetches by *post* id, and for a group question those differ: expansion
        deep-copies the parent post, so every sibling shares one ``id_of_post`` and only
        ``id_of_question`` tells them apart (M1-202). A mismatched pair would post a
        forecast to the wrong sibling and then confirm it by refetching the post they
        share -- a wrong live forecast, recorded as a verified success.

        Both ids come from the same ``forecast_records`` row, so this is not guarding
        against an operator typing one in. It is the check that turns a value taken on
        trust into a value verified against the platform, which is the difference between
        this and the second source of truth M2-703's round 1 removed.
        """
        actual_question = _int_attribute(question, "id_of_question")
        actual_post = _int_attribute(question, "id_of_post")
        if actual_question is None or actual_post is None:
            raise LiveSubmissionError(
                "the fetched question does not report both of its identifiers, so it "
                "cannot be matched to this record; nothing was posted"
            )
        if actual_question != question_id or actual_post != post_id:
            # Names no number: which question a fetch returned is platform content, and an
            # operator who needs the values has them in the record and in the URL.
            raise LiveSubmissionError(
                "the fetched question is not the one this forecast record names; a group "
                "question's siblings share a post id and differ only by question id, so "
                "posting here would forecast on the wrong question (nothing was posted)"
            )

    def _require_open(self, question: object) -> None:
        """Refuse a question that is not open to forecasts, before anything is posted.

        A closed question rejects the post with an HTTP error, so this saves nothing on the
        platform side -- it saves the *idempotency key*, which is spent by the attempt row
        that failure would produce, and it turns an opaque 4xx into a refusal that says
        what is wrong. The state is read through guarded attribute access and an
        unreadable one is **not** treated as closed: this refuses what it can positively
        establish, and nothing else.
        """
        try:
            state = getattr(question, "state", None)
            rendered = None if state is None else getattr(state, "value", state)
        except Exception:
            return
        if type(rendered) is str and rendered != "open":
            # `state` here is one of the SDK's own closed vocabulary, not free content.
            raise LiveSubmissionError(
                f"the question is {rendered}, not open, so it accepts no forecasts; "
                "nothing was posted"
            )

    def observe(self, post_id: int, *, question_id: int) -> ForecastHistory | None:
        """Refetch one question and return the operator's own forecast history.

        ``None`` means the refetch could not be performed or could not be read -- never
        "there are no forecasts", which is an empty :class:`ForecastHistory`. Collapsing
        those two is how a lost connection becomes a permanent claim that a live forecast
        does not exist.

        **Never raises**, because :meth:`submit_with_detail` calls it after a post has been
        made. :func:`verify_uncertain_attempt` calls the same method for the same rule.
        """
        return self._observe_with_detail(post_id, question_id=question_id)[0]

    def _observe_with_detail(
        self, post_id: int, *, question_id: int
    ) -> tuple[ForecastHistory | None, FailureCode]:
        """The retrying refetch, and why the last failure was.

        Retried, because **reads may retry and writes may not** -- the same line this
        module draws against the SDK's own retry decorator, and what makes the
        unrecordable cell M2-711 names rare rather than routine. Each round is a fresh GET
        and the identity check runs on every one: a refetch that came back describing a
        different question proves nothing about this post.
        """
        detail: FailureCode = "malformed_response"
        for attempt in range(self._refetch_attempts):
            if attempt:
                try:
                    self._sleep(self._refetch_pause_seconds)
                except Exception:  # noqa: BLE001 - an injected sleep must not cost the row
                    pass
            try:
                question = self._poster.get_question_by_post_id(post_id)
            except Exception as exc:  # noqa: BLE001 - classified, never re-raised
                detail = _DETAIL_FOR_ERROR[classify_error(exc)]
                continue
            if (
                _int_attribute(question, "id_of_question") != question_id
                or _int_attribute(question, "id_of_post") != post_id
            ):
                detail = "malformed_response"
                continue
            history = read_my_forecasts(question)
            if history is None:
                detail = "malformed_response"
                continue
            return history, detail
        return None, detail

    def _refetch(
        self, post_id: int, *, question_id: int, plan: PostPlan, baseline: ForecastHistory
    ) -> RefetchResult:
        """Refetch after the post and decide what it established. Never raises."""
        history, detail = self._observe_with_detail(post_id, question_id=question_id)
        if history is None:
            return RefetchResult("unreadable", detail, None)
        return classify_refetch(
            question_type=plan.question_type,
            expected=expected_values(plan),
            baseline_latest_start_time=(
                None if baseline.latest is None else baseline.latest.start_time
            ),
            observed=history,
        )

    def _receipt_for(
        self,
        *,
        record_id: str,
        key: str,
        digest: str,
        plan: PostPlan,
        baseline: ForecastHistory,
        result: RefetchResult,
        requested: datetime,
        completed: datetime,
        error: BaseException | None,
    ) -> LiveSubmissionOutcome:
        """Assemble the receipt. Everything in it is already storable -- see below.

        ``success`` is "the post call returned without raising" and
        ``verified_by_refetch`` is "a refetch confirmed the forecast is there". Both are
        observations, and neither is a judgement about the platform that this module is not
        entitled to make. :func:`lifecycle.record_submission_attempt` turns the pair into
        the event, which is why the event type is never chosen here.

        Every string is passed through :func:`storable_text` and every number through the
        ledger's own bounds *before* the receipt exists, so ``record_receipt`` cannot refuse
        a post that has already happened.
        """
        success = error is None
        verified = result.outcome == "confirmed"
        snapshot = build_verification_snapshot(
            question_type=plan.question_type,
            expected=expected_values(plan),
            baseline_entry_count=len(baseline.entries),
            baseline_latest_start_time=(
                None if baseline.latest is None else baseline.latest.start_time
            ),
            result=result,
        )
        error_type: str | None = None
        error_message: str | None = None
        detail_code: FailureCode | None = None
        status: int | None = None
        body: str | None = None
        headers: str | None = None
        if error is not None:
            classified = classify_error(error)
            error_type = classified
            detail_code = _DETAIL_FOR_ERROR[classified]
            message = _MESSAGE_FOR_ERROR[classified] + f" ({type(error).__name__})"
            if result.outcome == "unreadable":
                message += _UNESTABLISHED_NOTE
            error_message = storable_text(message, _MAX_BODY)
            status, body, headers = http_details(error)
        elif not verified:
            detail_code = result.detail_code
        if not (success and verified) and detail_code is None:
            # Unreachable: `classify_refetch` returns `None` only for `confirmed`, and
            # every error maps through the total `_DETAIL_FOR_ERROR`. It is here because
            # this runs *after* a post, and `record_submission_attempt` refuses an
            # unverified attempt with no detail_code -- which would be a live post the
            # ledger could not record, over a missing enum member.
            detail_code = "internal_error"
        receipt = SubmissionReceipt(
            mode="live",
            attempt_id=live_attempt_id(key),
            forecast_record_id=record_id,
            idempotency_key=key,
            requested_at_utc=requested,
            completed_at_utc=completed,
            request_payload_sha256=digest,
            success=success,
            verified_by_refetch=verified,
            http_status=status,
            response_body=body,
            response_headers=headers,
            error_type=storable_text(error_type, _MAX_IDENTIFIER),
            error_message=error_message,
            refetched_forecast_snapshot=storable_text(snapshot, _MAX_BODY),
        )
        return LiveSubmissionOutcome(receipt=receipt, detail_code=detail_code)


def live_attempt_id(idempotency_key: str) -> str:
    """The deterministic attempt id a live post under this key produces.

    Derived rather than minted as a ``uuid4``, for :func:`submission_gateway.
    dry_run_attempt_id`'s reason and one more of its own: ``001`` declares
    ``idempotency_key`` UNIQUE and :func:`lifecycle._require_no_prior_success` refuses a
    second row for an attempt id that already succeeded, so deriving the id from the key
    makes a duplicate post refusable on *two* independent constraints rather than one.

    It is a hash of the key rather than the key itself so that an attempt id can never be
    pasted into a query against ``submission_attempts.idempotency_key`` and match, and it
    carries its own visible scheme tag so a reader can tell a live identity from a
    rehearsed one without consulting anything else.
    """
    key = _require_identifier(idempotency_key, "idempotency_key")
    return _LIVE_ATTEMPT_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LiveSubmissionRecord:
    """What one live submission left behind: the receipt, its event, and its artifact.

    ``artifact_path`` is ``None`` **exactly when** the artifact could not be written, and
    ``artifact_error`` then says why. M1-312's rule: a result type that cannot represent a
    lie is half the criterion, so there is no state where a path is reported for a file
    that does not exist, and none where a failure is silent.
    """

    receipt: SubmissionReceipt
    event: LifecycleEvent
    artifact_path: str | None
    artifact_error: str | None


def post_approved_forecast(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    payload: Mapping[str, object],
    poster: MetaculusPoster,
    config: AppConfig,
    occurred_at: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> LiveSubmissionRecord:
    """Post one approved forecast, verify it, and record it. **The only door to a live post.**

    Every gate is in front of the post and every one of them refuses without spending
    anything:

    1. **Configuration.** ``submission.enabled`` must be true and ``dry_run`` and
       ``no_submit`` must both be false. All three are committed as the safe values and
       remain so; flipping them is a deliberate operator act, and until M2-704 the
       validator refused even to parse the flipped ones.
    2. **No unresolved uncertainty.** :func:`lifecycle.unresolved_uncertainties` must be
       empty. *This is the acceptance criterion's "uncertain timeout blocks blind retry"*,
       and it is a gate in front of the action rather than a rule about recording one --
       the distinction M1-603 round 4 settled when it withdrew the same check from the
       writer, where it could only stop a post being *recorded*.
    3. **The record.** ``question_id``, ``post_id`` and ``question_type`` all come from the
       one ``forecast_records`` row, so no caller supplies a value that must agree with
       another (M2-703 round 1's finding, applied ahead of time rather than fixed after).
    4. **The payload's type must match the record's.** This is the only part of D33 that is
       checkable today: an approval binds to ``forecast_sha256``, so it still cannot be
       shown that *this* payload is the one the approval meant, and **M2-707** remains the
       filed item. A numeric payload posted against a binary record is caught; a different
       binary payload for the same forecast is not, and that is stated rather than implied.
    5. **Approval, and still awaiting submission.** :func:`submission.
       submission_key_for_approved_record` refuses a record that holds no approval *or* has
       since moved off ``approved`` -- the second check being M2-702 round 2's fix.
    6. **The key is unspent.** :func:`submission.require_key_unused`. It is a read and says
       so; **M2-708** is the item for reserving one atomically.

    After the post, nothing refuses. The artifact is written first and every failure of it
    degrades to a note on the result (M1-312: an artifact failure must never cost a
    recorded spend), and the ledger row is written regardless. If even that fails the error
    names the artifact path, because at that point a live post exists and the operator
    needs the payload that produced it.
    """
    require_live_submission_enabled(config)
    identifier = _require_identifier(record_id, "record_id")

    try:
        outstanding = unresolved_uncertainties(conn, identifier)
    except LifecycleError as exc:
        raise LiveSubmissionError(
            str(exc) or "the ledger refused to report this record's open uncertainties"
        ) from None
    if outstanding:
        # The count is this module's own arithmetic; the attempt ids are stored values and
        # are not named. `verify-submission` is where an operator gets them.
        raise LiveSubmissionError(
            f"this record has {len(outstanding)} submission attempt(s) whose outcome a "
            "refetch has not resolved; posting again would be the blind retry the ledger "
            "exists to prevent -- resolve them with verify-submission first"
        )

    try:
        record = read_forecast_record(conn, identifier)
    except ForecastRecordError as exc:
        raise LiveSubmissionError(
            str(exc) or "this record could not be read back from the ledger"
        ) from None

    canonical = _canonical_or_refuse(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    plan = plan_from_canonical_payload(
        canonical, expected_cdf_points=config.numeric_calibration.expected_cdf_points
    )
    if plan.question_type != record.question_type:
        # Both are members of `config.SupportedQuestionType`, a closed vocabulary this
        # package defines, so naming them is safe and it is what makes this actionable.
        raise LiveSubmissionError(
            f"the payload is a {plan.question_type} forecast and this record is "
            f"{record.question_type}; nothing was posted"
        )

    try:
        key = submission_key_for_approved_record(conn, identifier, request_payload_sha256=digest)
        require_key_unused(conn, key)
    except SubmissionError as exc:
        raise LiveSubmissionError(
            str(exc) or "this record cannot be submitted in its current state"
        ) from None

    gateway = MetaculusSubmissionGateway(
        poster=poster,
        expected_cdf_points=config.numeric_calibration.expected_cdf_points,
        clock=clock,
        sleep=sleep,
    )
    outcome = gateway.submit_with_detail(
        SubmissionRequest(
            forecast_record_id=identifier,
            question_id=record.question_id,
            idempotency_key=key,
            payload=payload,
            post_id=record.post_id,
        )
    )

    # ---- past this line a post has been made; nothing below may refuse it a record ----
    artifact_path, artifact_error = _write_receipt_artifact(
        config.storage.artifact_root,
        receipt=outcome.receipt,
        question_id=record.question_id,
        payload=payload,
        context={
            "forecast_record_id": identifier,
            "question_type": record.question_type,
            "tournament_id": record.tournament_id,
            "forecast_version": record.forecast_version,
            "post_id": record.post_id,
        },
    )
    receipt = replace(outcome.receipt, artifact_path=artifact_path)
    stamped = _utcnow() if occurred_at is None else occurred_at
    try:
        event = record_receipt(
            conn, receipt=receipt, occurred_at=stamped, detail_code=outcome.detail_code
        )
    except GatewayError as exc:
        # `record_receipt` belongs to `submission_gateway` and raises that module's type;
        # this is the one place the wrapper is not used, because the message has to gain
        # the artifact path a bare re-wrap would drop.
        where = (
            f"; the payload and receipt are at {config.storage.artifact_root / artifact_path}"
            if artifact_path is not None
            else "; the artifact could not be written either"
        )
        raise LiveSubmissionError(
            f"a live post was made and the ledger refused to record it ({exc}){where}"
        ) from None
    return LiveSubmissionRecord(
        receipt=receipt,
        event=event,
        artifact_path=artifact_path,
        artifact_error=artifact_error,
    )


def _write_receipt_artifact(
    artifact_root: Path,
    *,
    receipt: SubmissionReceipt,
    question_id: int,
    payload: Mapping[str, object],
    context: Mapping[str, object],
) -> tuple[str | None, str | None]:
    """Write the live artifact, degrading to ``(None, reason)`` on any failure.

    **This is M1-312's rule at its boundary.** Before the post, an artifact that cannot be
    written is a refusal, because nothing has been spent and a submission that cannot
    record itself has simply not happened. After the post, the spend is real and
    irreversible, so an artifact failure must cost the payload record and nothing else --
    the ledger row is what matters and it is written either way.

    ``except Exception`` is the right width here for once: this runs after a live post, and
    what an arbitrary filesystem can raise is not enumerable.
    """
    try:
        return (
            write_live_artifact(
                artifact_root,
                receipt=receipt,
                question_id=question_id,
                payload=payload,
                context=context,
            ),
            None,
        )
    except GatewayError as exc:
        return None, str(exc) or "the submission artifact could not be written"
    except Exception:  # noqa: BLE001 - a post has happened; nothing here may propagate
        return None, "the submission artifact could not be written"


def require_live_submission_enabled(config: AppConfig) -> None:
    """Refuse unless the operator has deliberately turned every safety flag off.

    Public because ``cli._run_submit`` calls it *before* building a poster: constructing
    one reads ``METACULUS_TOKEN``, and an operator running ``submit`` against the committed
    configuration should be told that submission is off rather than that a credential is
    missing. :func:`post_approved_forecast` calls it again as its first act, so the gate
    does not depend on a caller remembering to.

    Three flags rather than one, and they are not redundant: ``enabled`` says a submission
    path may run at all, ``dry_run`` says it rehearses, ``no_submit`` is the belt-and-braces
    kill switch the handoff asks for. Any one of them left at its committed value refuses.
    ``config.py`` already refuses ``enabled: true`` alongside either of the other two, so
    the three cannot be set to a contradictory combination -- this is the runtime half of
    the same rule, stated where the post would otherwise happen.
    """
    if type(config) is not AppConfig:
        raise LiveSubmissionError("config must be an AppConfig")
    submission = config.submission
    if not submission.enabled or submission.dry_run or submission.no_submit:
        raise LiveSubmissionError(
            "live submission is off: it requires submission.enabled: true with "
            "dry_run: false and no_submit: false, and all three ship as the safe values"
        )


def verify_uncertain_attempt(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    attempt_id: str,
    poster: MetaculusPoster,
    occurred_at: datetime | None = None,
    sleep: Callable[[float], None] | None = None,
    refetch_attempts: int = _REFETCH_ATTEMPTS,
    refetch_pause_seconds: float = _REFETCH_PAUSE_SECONDS,
) -> LifecycleEvent:
    """Refetch an uncertain attempt and record what the platform actually shows.

    **This is the way out of an uncertain submission, and without it the gate in
    :func:`post_approved_forecast` never reopens.** ``lifecycle.
    record_submission_verification`` has existed since M1-603 with no caller; this is it.

    It re-runs the *same* comparison the original submission ran, from the snapshot that
    submission stored: the baseline it was measured against, the values that were expected,
    and the question type they are projected through all come out of
    ``submission_attempts.refetched_forecast_snapshot``. There is one implementation of
    "did the refetch show what we posted" (:func:`classify_refetch`) and both callers use
    it, so a later verification cannot judge by a different rule than the attempt did.

    It reads no configuration and makes **no post** -- a GET is the only network call on
    this path, which is why it is safe to run at any time and why it needs no submission
    flags. It is the command an operator runs when a post's outcome is in doubt.

    Two of the four refetch outcomes are refused rather than recorded, and deliberately:

    - **mismatched** -- something is on the platform and it is not what this attempt sent.
      ``lifecycle`` offers only ``confirmed`` and ``absent``, and ``absent`` is terminal;
      recording a mismatch as ``absent`` would kill a forecast version on evidence that
      *a* forecast exists. Which one it is is a human judgement, so the uncertainty is left
      standing -- which keeps the post gate closed, the conservative direction.
    - **unreadable** -- nothing was established. Recording anything here would be inventing
      an observation.
    """
    identifier = _require_identifier(record_id, "record_id")
    attempt = _require_identifier(attempt_id, "attempt_id")
    try:
        outstanding = unresolved_uncertainties(conn, identifier)
    except LifecycleError as exc:
        raise LiveSubmissionError(
            str(exc) or "the ledger refused to report this record's open uncertainties"
        ) from None
    if attempt not in outstanding:
        # Names no stored value: the caller supplied the attempt id and already has it.
        raise LiveSubmissionError(
            "this record holds no unresolved uncertainty for that attempt, so there is "
            "nothing for a refetch to decide"
        )

    snapshot = read_verification_snapshot(_stored_snapshot(conn, identifier, attempt))
    question_type = _snapshot_question_type(snapshot)
    expected = _snapshot_expected_values(snapshot)
    baseline_latest = _snapshot_baseline_start_time(snapshot)

    try:
        record = read_forecast_record(conn, identifier)
    except ForecastRecordError as exc:
        raise LiveSubmissionError(
            str(exc) or "this record could not be read back from the ledger"
        ) from None
    if record.question_type != question_type:
        raise LiveSubmissionError(
            "the stored verification snapshot describes a different question type than "
            "the record does, so it cannot be re-judged"
        )

    gateway = MetaculusSubmissionGateway(
        poster=poster,
        sleep=sleep,
        refetch_attempts=refetch_attempts,
        refetch_pause_seconds=refetch_pause_seconds,
    )
    history = gateway.observe(record.post_id, question_id=record.question_id)
    result = classify_refetch(
        question_type=question_type,
        expected=expected,
        baseline_latest_start_time=baseline_latest,
        observed=history,
    )
    if result.outcome == "unreadable":
        raise LiveSubmissionError(
            "the question could not be refetched, so nothing was established and nothing "
            "was recorded; the attempt stays unresolved"
        )
    if result.outcome == "mismatched":
        raise LiveSubmissionError(
            "the platform holds a forecast that is not the one this attempt sent; that is "
            "not the same as the forecast being absent, and recording it as absent would "
            "end this forecast version on the wrong evidence -- resolve it by hand"
        )

    stamped = _utcnow() if occurred_at is None else occurred_at
    observed_snapshot = build_verification_snapshot(
        question_type=question_type,
        expected=expected,
        baseline_entry_count=_snapshot_baseline_entry_count(snapshot),
        baseline_latest_start_time=baseline_latest,
        result=result,
    )
    verification = SubmissionVerification(
        submission_attempt_id=attempt,
        outcome="confirmed" if result.outcome == "confirmed" else "absent",
        observed_at_utc=stamped,
        refetched_forecast_snapshot=(
            storable_text(observed_snapshot, _MAX_BODY) if result.outcome == "confirmed" else None
        ),
    )
    try:
        return record_submission_verification(
            conn,
            record_id=identifier,
            verification=verification,
            occurred_at=stamped,
            detail_code=None if result.outcome == "confirmed" else result.detail_code,
        )
    except LifecycleError as exc:
        raise LiveSubmissionError(str(exc) or "the ledger refused to record this refetch") from None


def _stored_snapshot(conn: sqlite3.Connection, record_id: str, attempt_id: str) -> object:
    """Read one attempt's stored verification snapshot, scoped to its record."""
    row = _fetch_one(
        conn,
        "SELECT refetched_forecast_snapshot FROM submission_attempts "
        "WHERE attempt_id = ? AND forecast_record_id = ?",
        (attempt_id, record_id),
    )
    if row is None:
        raise LiveSubmissionError(
            "that attempt id names no submission attempt against this forecast record"
        )
    return row[0]


def _snapshot_question_type(snapshot: Mapping[str, object]) -> str:
    value = snapshot.get("question_type")
    if type(value) is not str or value not in _SUPPORTED_QUESTION_TYPES:
        raise LiveSubmissionError(
            "the stored verification snapshot names no question type this build supports"
        )
    return value


def _snapshot_expected_values(snapshot: Mapping[str, object]) -> tuple[float, ...]:
    values = snapshot.get("expected_values")
    if not isinstance(values, list) or not values:
        raise LiveSubmissionError(
            "the stored verification snapshot records no expected values, so this attempt "
            "cannot be re-judged automatically"
        )
    numbers: list[float] = []
    for item in values:
        number = _finite_number(item)
        if number is None:
            raise LiveSubmissionError(
                "the stored verification snapshot's expected values are not all numbers"
            )
        numbers.append(number)
    return tuple(numbers)


def _snapshot_baseline_start_time(snapshot: Mapping[str, object]) -> float | None:
    baseline = snapshot.get("baseline")
    if not isinstance(baseline, dict):
        raise LiveSubmissionError("the stored verification snapshot records no baseline")
    value = baseline.get("latest_start_time")
    if value is None:
        return None
    number = _finite_number(value)
    if number is None:
        raise LiveSubmissionError(
            "the stored verification snapshot's baseline timestamp is not a number"
        )
    return number


def _snapshot_baseline_entry_count(snapshot: Mapping[str, object]) -> int:
    baseline = snapshot.get("baseline")
    if isinstance(baseline, dict):
        count = baseline.get("entry_count")
        if type(count) is int and count >= 0:
            return count
    return 0


def _wrap_gateway(exc: GatewayError) -> LiveSubmissionError:
    """Re-raise the gateway layer as this module's own type, message preserved.

    Found by the property suite, and it was a real escape rather than a technicality:
    ``canonical_payload_json`` and ``record_receipt`` belong to ``submission_gateway`` and
    raise :class:`GatewayError`, which :class:`LiveSubmissionError` *subclasses* -- so
    ``except LiveSubmissionError`` does not catch one. ``cli._run_submit`` catches exactly
    that, so a malformed payload escaped as an unhandled traceback rather than as
    ``refused: ...``. CLAUDE.md's rule is that every malformed shape arrives as the
    module's own error type, and a subclass relationship runs the wrong way to satisfy it.

    The message is carried over, the call ``submission._wrap_approval`` makes and for its
    reason: ``GatewayError``'s own contract guarantees its text names no stored or
    caller-supplied value, and that text is the only thing making a refusal actionable.
    """
    return LiveSubmissionError(str(exc) or "the submission gateway refused this request")


def _fetch_one(
    conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> tuple[object, ...] | None:
    """Read one row, or raise this module's error type. Mirrors ``submission._fetch_one``."""
    try:
        row = conn.execute(sql, parameters).fetchone()
    except (sqlite3.Error, OverflowError, UnicodeEncodeError, UnicodeDecodeError):
        # from None: the underlying error's text and traceback can carry stored values, and
        # sqlite3's UnicodeEncodeError quotes the character it could not encode.
        # UnicodeDecodeError is here because sqlite3 decodes TEXT at *fetch*, not at
        # execute (M1-306), so a row holding undecodable bytes raises from this line.
        raise LiveSubmissionError(
            "the ledger could not be read (detail withheld: a database message can echo "
            "stored values)"
        ) from None
    return None if row is None else tuple(row)


def _int_attribute(subject: object, name: str) -> int | None:
    """Read an integer attribute off an untrusted object, or ``None``. Never raises.

    ``type(value) is int`` rather than ``isinstance``: ``bool`` subclasses ``int``, so a
    question reporting ``True`` for its id would otherwise match question 1.
    """
    try:
        value = getattr(subject, name, None)
    except Exception:
        return None
    return value if type(value) is int else None


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _require_aware_utc(value: object, field: str) -> datetime:
    """Return an aware datetime converted to UTC, or raise. Mirrors ``lifecycle``'s.

    Exact type rather than ``isinstance``: a ``datetime`` subclass can override
    ``isoformat()`` and write arbitrary text into an artifact. The conversion is guarded
    broadly for ``lifecycle._require_aware_utc``'s reason -- ``tzinfo`` is an abstract base
    class, so ``utcoffset()`` and ``astimezone()`` run caller-supplied code on a value that
    has passed every type gate above, and what arbitrary code can raise is not enumerable.
    """
    if type(value) is not datetime:
        raise LiveSubmissionError(f"{field} must be a datetime")
    try:
        aware = value.tzinfo is not None and value.utcoffset() is not None
    except Exception:
        raise LiveSubmissionError(
            f"{field} has a timezone that could not be read "
            "(detail withheld: it can echo the value)"
        ) from None
    if not aware:
        raise LiveSubmissionError(f"{field} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except Exception:
        raise LiveSubmissionError(
            f"{field} could not be converted to UTC (detail withheld: it can echo the value)"
        ) from None


def _require_identifier(value: object, field: str) -> str:
    """Return storable, non-blank identifier text, or raise naming only the *field*.

    ``lifecycle._require_identifier``'s rule, re-spelled for the reason the four other
    modules that restate it give, and pinned to them by **M1-608**: exact ``str`` type,
    non-empty, within the 200-character ledger bound, non-blank under ``str.strip()``, no
    U+0000, UTF-8 encodable. The encode probe is the load-bearing one -- ``sqlite3``
    encodes text parameters as UTF-8, so a lone surrogate reaching a query raises a raw
    ``UnicodeEncodeError`` quoting the offending character.
    """
    if type(value) is not str or not value:
        raise LiveSubmissionError(f"{field} must be a non-empty string")
    if len(value) > _MAX_IDENTIFIER:
        raise LiveSubmissionError(f"{field} is longer than the {_MAX_IDENTIFIER}-character limit")
    if not value.strip():
        raise LiveSubmissionError(f"{field} must not be blank")
    if "\x00" in value:
        raise LiveSubmissionError(f"{field} must not contain a NUL character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # from None: UnicodeEncodeError's own message quotes the character it choked on.
        raise LiveSubmissionError(
            f"{field} contains characters that cannot be stored "
            "(detail withheld: it can echo the value)"
        ) from None
    return value


def _require_safe_key(value: object) -> str:
    """Return an idempotency key that is also safe as a path component.

    Delegates to ``submission_gateway``'s public artifact-path builder rather than
    re-spelling its character rule: the constraint exists because the key becomes a
    directory entry, and the module that owns the layout owns the rule. Every key
    :func:`submission.submission_key` mints passes it.
    """
    from whiskeyjack_bot.submission_gateway import live_artifact_path

    key = _require_identifier(value, "idempotency_key")
    try:
        live_artifact_path(question_id=1, idempotency_key=key)
    except GatewayError as exc:
        raise LiveSubmissionError(str(exc)) from None
    return key


def _require_positive_int(value: object, field: str) -> int:
    """Return a positive, storable integer.

    ``type(value) is int`` rather than ``isinstance``: ``bool`` subclasses ``int``, so
    ``True`` would otherwise become question 1. The upper bound is SQLite's signed 64-bit
    range -- a value beyond it could never reach a ``forecast_records`` row, so a receipt
    naming one could never be matched back to it (the M1-305 persisted-form rule applied to
    integers).
    """
    if type(value) is not int:
        raise LiveSubmissionError(f"{field} must be an integer")
    if value < 1:
        raise LiveSubmissionError(f"{field} must be a positive integer")
    if value > 2**63 - 1:
        raise LiveSubmissionError(f"{field} is larger than a 64-bit integer and cannot be stored")
    return value
