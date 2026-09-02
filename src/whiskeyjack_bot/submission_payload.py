"""The submission payload one stored forecast authorizes (M2-707).

This is the forecast->payload mapping M1-502 and M1-503 both deliberately left for this
item -- *"M1-502 shipped validation without a payload builder ... M2-707 is where the
mapping and the approval binding land together"* (``docs/M1-NOTES.md``, M1-503's
"Rejected -- building the submission payload"). Until it existed, an approval could bind
only to ``forecast_sha256``, so one approved forecast covered every payload built from it
and ``submission.submission_key_for_approved_record`` had nothing to compare a payload
against. That is decision **D33**, and this module is the half of closing it that computes
a value; ``approval.approve`` stores it and ``submission_key_for_approved_record`` enforces
it.

**The payload is the Metaculus wire body plus a discriminator**, exactly as
``submission_live.plan_from_payload`` already defines it: ``question_type`` and one of
``probability_yes`` / ``continuous_cdf`` / ``probability_yes_per_category``, and nothing
else. That vocabulary is not re-declared here -- :data:`submission_live._WIRE_KEY_FOR_TYPE`
stays its single owner, read through :func:`submission_live.plan_from_payload`, because a
second copy is the defect M2-703 removed a parameter to avoid.

**Every payload this module returns is put through ``plan_from_payload`` before it is
returned.** That is what makes an approval bind to a *postable* payload rather than merely
to a derivable one: the alternative is an operator approving something the live path will
refuse at the last gate, which is a refusal arriving after a human decision instead of
before it. It costs one canonical rendering and no I/O.

**Where this module may not be imported from**, and both directions are load-bearing:

* **Not from ``approval.py``.** ``submission.py`` imports ``approval``, so an approval
  module that reached a builder importing ``submission`` would close an import cycle. That
  is why :func:`approval.approve` takes ``payload_sha256`` as a parameter rather than
  computing it, and why the two gates that make a wrong value harmless live downstream.
* **Not from ``submission_live.py``.** That module states, and rests design on, *"Nothing
  here imports ``forecasting_tools``"* -- four-method-wide :class:`MetaculusPoster` protocol,
  no transport, no SDK import cost on the one live path. The numeric branch below **is**
  ``NumericDistribution`` (via ``forecast.cdf``), so importing this from there would end a
  documented guarantee in order to duplicate a check the hash comparison at the key seam
  already makes. The derivation is established once, at approve time, and carried forward
  by the stored digest.

So the only importer is ``cli.py``, which already reaches the SDK and already imports
lazily inside its command functions.

**Dispatch is on the ``question_type`` literal**, never on ``isinstance`` and never on
which prediction field happens to be populated -- CLAUDE.md's rule, and the reason
``DiscreteQuestion`` subclassing ``NumericQuestion`` has never silently mis-normalized
here. The record's own validators already require ``question_type`` to agree across the
row, the question and the response, but a record read back out of the ledger is untrusted
input under the threat boundary, so each branch re-checks the exact concrete types it
needs and refuses rather than assuming.

**No message renders a value.** Not a probability, not an option label, not a percentile,
and above all not the SDK's own text: every failure inside ``NumericDistribution`` arrives
as a ``pydantic.ValidationError`` interpolating the percentiles it refused, which is why
``forecast.cdf`` fences every SDK call and emits value-free problem strings. Those strings
are safe to pass through; nothing else is.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from whiskeyjack_bot.config import NumericCalibrationConfig, SupportedQuestionType
from whiskeyjack_bot.forecast.cdf import build_numeric_cdf
from whiskeyjack_bot.forecast.record import ForecastRecord
from whiskeyjack_bot.forecast.schema import (
    BinaryForecastResponse,
    ForecastSchemaError,
    MultipleChoiceForecastResponse,
    NumericForecastResponse,
)
from whiskeyjack_bot.questions.model import CanonicalNumericQuestion
from whiskeyjack_bot.submission import SubmissionError
from whiskeyjack_bot.submission_gateway import GatewayError, canonical_payload_json
from whiskeyjack_bot.submission_live import LiveSubmissionError, plan_from_payload

__all__ = [
    "AuthorizedPayload",
    "PayloadBuildError",
    "authorized_payload",
    "build_submission_payload",
    "payload_sha256_for_record",
]


class PayloadBuildError(SubmissionError):
    """A stored forecast cannot be turned into a submission payload.

    Subclasses :class:`submission.SubmissionError` for the reason
    :class:`submission_gateway.GatewayError` does: every caller of this module is already
    handling that type, and a second unrelated exception class at the same seam would be a
    distinction no caller makes. The hygiene rule is unchanged -- the message never echoes
    a prediction, an option label, a percentile, a question field or an SDK error's text,
    and sanitizing raises use ``from None`` so an underlying exception cannot reprint one
    through a rendered traceback.
    """


def build_submission_payload(
    record: ForecastRecord, *, calibration: NumericCalibrationConfig
) -> dict[str, object]:
    """Return the Metaculus request body this stored forecast authorizes.

    Deterministic in ``(record, calibration)`` and free of I/O, the clock and the ledger,
    because :func:`payload_sha256_for_record`'s digest is what an approval binds to and a
    binding to a value that could come back different is not a binding. The one
    non-obvious input is ``calibration``: a numeric payload is a *conversion* of the
    stored percentiles, not a copy of them, so changing ``numeric_calibration`` changes
    the payload a record derives. That is deliberate and it fails safe -- the rebuilt
    payload hashes differently, so the approval stops binding and the operator is asked to
    approve again rather than posting an array nobody reviewed.

    ``record.question`` and ``record.forecast`` both travel inside ``record_json``, so the
    conversion runs against the question the forecast was *made* against rather than
    against whatever a snapshot says today.
    """
    if type(calibration) is not NumericCalibrationConfig:
        raise PayloadBuildError("calibration must be a NumericCalibrationConfig")
    if type(record) is not ForecastRecord:
        raise PayloadBuildError("record must be a stored ForecastRecord")
    question_type = record.question_type
    if question_type == "binary":
        payload = _binary_payload(record)
    elif question_type == "multiple_choice":
        payload = _multiple_choice_payload(record)
    elif question_type == "numeric":
        payload = _numeric_payload(record, calibration)
    else:  # pragma: no cover - `SupportedQuestionType` is closed and the record validates it
        raise PayloadBuildError(_UNSUPPORTED_TYPE)
    _require_postable(payload, calibration)
    return payload


@dataclass(frozen=True)
class AuthorizedPayload:
    """The payload a stored forecast authorizes, its exact bytes, and their digest.

    Three fields rather than one, and they travel together for the reason
    :func:`submission_live.plan_from_canonical_payload` exists: ``sha256`` is taken over
    ``canonical``, and ``canonical`` is the rendering of ``payload``, so what an operator is
    shown, what an approval binds to and what is posted are provably the same bytes. A
    caller that rendered a second time to print it would be comparing two runs of one
    function instead -- the second-source-of-truth defect M2-703 removed a parameter to
    avoid, and the rule ``write_dry_run_artifact`` states for the same pairing.
    """

    payload: dict[str, object]
    canonical: str
    sha256: str


def authorized_payload(
    record: ForecastRecord, *, calibration: NumericCalibrationConfig
) -> AuthorizedPayload:
    """Build the payload this record authorizes, render it once, and digest that rendering.

    The seam ``cli.py`` uses for both ``approve`` (which binds a decision to ``sha256``)
    and ``submit`` (which posts ``payload`` when no ``--payload-file`` is given), so the two
    commands cannot disagree about what a record authorizes.
    """
    payload = build_submission_payload(record, calibration=calibration)
    canonical = _render(payload)
    return AuthorizedPayload(
        payload=payload,
        canonical=canonical,
        sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def payload_sha256_for_record(
    record: ForecastRecord, *, calibration: NumericCalibrationConfig
) -> str:
    """Return the digest of the payload this record authorizes.

    The value :func:`approval.approve` stores and
    :func:`submission.submission_key_for_approved_record` compares against. A thin
    delegate to :func:`authorized_payload` rather than a second derivation, for a caller
    that wants only the digest.
    """
    return authorized_payload(record, calibration=calibration).sha256


# --- per-type builders ----------------------------------------------------------------
#
# One per member of `SupportedQuestionType`, reached by the literal above. Each re-checks
# the concrete response type it needs: the record's `_one_question` validator already
# requires `question_type` to agree across the row, the question and the response, but a
# record reconstructed from a stored `record_json` is untrusted under CLAUDE.md's threat
# boundary, and "the validator would have caught it" is not something a payload builder
# gets to assume about a value that decides what is posted.


def _binary_payload(record: ForecastRecord) -> dict[str, object]:
    forecast = record.forecast
    if type(forecast) is not BinaryForecastResponse:
        raise PayloadBuildError(_MISMATCHED_RESPONSE)
    return {
        "question_type": "binary",
        "probability_yes": forecast.final_prediction.probability_yes,
    }


def _multiple_choice_payload(record: ForecastRecord) -> dict[str, object]:
    forecast = record.forecast
    if type(forecast) is not MultipleChoiceForecastResponse:
        raise PayloadBuildError(_MISMATCHED_RESPONSE)
    categories: dict[str, float] = {}
    for entry in forecast.final_prediction.options:
        if entry.option in categories:
            # Refused rather than collapsed. The wire shape is a JSON object keyed on the
            # label, so two entries sharing one label would silently become one -- a
            # payload carrying fewer options than the forecast it claims to be, whose
            # probabilities then no longer sum to one and which an operator would have
            # approved without ever seeing the dropped entry. `multiple_choice_output_
            # problems` rules this out on the generation path; this is the same rule at
            # the point where dropping it would be irreversible. The label is not named:
            # it is provider-derived question content.
            raise PayloadBuildError(
                "this forecast names one multiple-choice option twice, so its payload "
                "would silently carry fewer options than the forecast does "
                "(offending label withheld: it is question content)"
            )
        categories[entry.option] = entry.probability
    return {
        "question_type": "multiple_choice",
        "probability_yes_per_category": categories,
    }


def _numeric_payload(
    record: ForecastRecord, calibration: NumericCalibrationConfig
) -> dict[str, object]:
    forecast = record.forecast
    question = record.question
    if type(forecast) is not NumericForecastResponse:
        raise PayloadBuildError(_MISMATCHED_RESPONSE)
    if type(question) is not CanonicalNumericQuestion:
        raise PayloadBuildError(_MISMATCHED_QUESTION)
    try:
        cdf = build_numeric_cdf(forecast, calibration, question)
    except ForecastSchemaError as exc:
        # The parent rather than `NumericCdfError`, so this stays total against the
        # package's whole response-failure vocabulary: `build_numeric_cdf` raises only
        # `NumericCdfError` today, and a reader who has to re-derive that from a listing of
        # every `raise` in `forecast/cdf.py` is a reader one refactor away from an
        # unhandled sibling escaping a public boundary. Both carry the same sanitized
        # `problems` list.
        #
        # Those problem strings are value-free by construction
        # (`forecast/cdf.py`: "No message renders a value"), so passing them through is
        # what makes the refusal actionable -- an operator learns that the percentiles
        # need widening rather than that "something failed". `from None` all the same: the
        # pydantic ValidationError underneath it interpolates the values it refused.
        raise PayloadBuildError(
            "this forecast's percentiles do not convert into a submittable CDF, so there "
            f"is no payload for an approval to bind to: {'; '.join(exc.problems)}"
        ) from None
    return {
        "question_type": "numeric",
        "continuous_cdf": list(cdf.values),
    }


# --- shared refusals and helpers ------------------------------------------------------

_MISMATCHED_RESPONSE = (
    "this record's stored response is not the type its question_type names, so no payload "
    "can be derived from it"
)
_MISMATCHED_QUESTION = (
    "this record's stored question is not the type its question_type names, so no payload "
    "can be derived from it"
)
_UNSUPPORTED_TYPE = "this record's question_type is not one this project can submit"


def _require_postable(payload: Mapping[str, object], calibration: NumericCalibrationConfig) -> None:
    """Refuse a payload the live path would refuse, here rather than after an approval.

    :func:`submission_live.plan_from_payload` is the complete account of what Metaculus
    accepts, and it runs immediately before the post. Running it again here is not a second
    rule -- it is the same one, moved in front of the human decision, so an approval can
    only ever bind to a payload that would actually be accepted. Every bound it enforces
    (probability range, the exact CDF point count, monotonicity, the category sum) is one
    the generation path already checked, so reaching a refusal here means the stored record
    and the current configuration disagree, which is exactly what an operator needs told
    before approving and not after.
    """
    try:
        plan_from_payload(payload, expected_cdf_points=calibration.expected_cdf_points)
    except LiveSubmissionError as exc:
        raise PayloadBuildError(
            f"the payload derived from this record is not one Metaculus would accept: {exc}"
        ) from None


def _render(payload: Mapping[str, object]) -> str:
    """The exact string the digest is taken over.

    :func:`submission_gateway.canonical_payload_json` and nothing else: the rule that
    "changing this rendering breaks replay and changes every idempotency key derived from a
    payload" only holds while there is one implementation of it, and this module is a new
    place that rule could have been quietly forked.
    """
    try:
        return canonical_payload_json(payload)
    except GatewayError as exc:  # pragma: no cover - `_require_postable` renders it first
        raise PayloadBuildError(str(exc)) from None


# Named so the closed vocabulary this module dispatches over is checkable from the outside
# rather than only from a reading of the branches above. `tests/unit/test_submission_
# payload.py` asserts it equals `get_args(SupportedQuestionType)`: a fourth supported type
# added to `config.py` with no branch here must be a red build, not a payload that falls
# through to `_UNSUPPORTED_TYPE` at approval time.
BUILDABLE_QUESTION_TYPES: frozenset[SupportedQuestionType] = frozenset(
    {"binary", "multiple_choice", "numeric"}
)
