"""The forecaster's structured response schema (M1-402).

``prompts/forecaster.md`` is the contract: it tells the model to return exactly one
JSON object, prints the shared attribution fields, and prints one ``final_prediction``
shape per ``question_type``. This module is the receiving half of that contract, and
its models are transcribed from the prompt rather than invented alongside it.

**The prompt is the only schema instruction the model is given.** Nothing here is
appended to the message as a generated JSON schema, and no ``response_format`` is
passed to the provider. Both were considered and rejected in M1-402:

- ``GeneralLlm.get_schema_format_instructions_for_pydantic_type`` would put
  instructions in front of the model that ``forecast_records.prompt_sha256`` does not
  attest to. That column exists precisely so a stored forecast can be tied to the
  exact text that produced it; instructions outside the hashed file are an
  attribution hole in the one place built to close it.
- ``response_format`` is forwarded to litellm, which sets ``drop_params = True`` and
  **silently discards** it when a provider does not support it. A guarantee that can
  vanish without a signal is not a guarantee.

So prompt/schema agreement is a real risk, and it is pinned mechanically instead:
``tests/unit/test_forecast_schema.py`` parses the JSON examples out of
``prompts/forecaster.md`` and asserts each one validates here. Editing the prompt's
schema without editing these models fails CI.

Scope, settled with the owner before any code. This module owns everything that is
**not question- or config-dependent**: field names, types, the closed vocabularies,
and the one cross-field rule the prompt states outright (a non-binary question has no
``prior_probability`` and no ``model_prior``). It deliberately does not read the
question's option list, its numeric bounds, or ``forecast.min_probability`` /
``forecast.max_probability`` -- those are ``forecast/multiple_choice.py`` (M1-404),
M1-405 and ``forecast/binary.py`` (M1-403) respectively, and
each is that row's stated acceptance criterion.

Models are strict (``extra="forbid"``, reusing ``config._StrictModel``). Use
:func:`validate_forecast_response` rather than a bare ``model_validate``: model output
is untrusted under CLAUDE.md's threat boundary, and pydantic's own error rendering
echoes the offending input.

This module imports no provider SDK, and that is load-bearing rather than tidy. M1-406
must replay a stored raw response and reproduce the parsed forecast with zero API
calls; M1-306 established that zero-calls is a property of the import graph, not of a
mock count (``tests/unit/test_research_store.py``). A replay path has to reach this
schema without the provider client being importable at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypeVar, get_args

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    Field,
    ValidationError,
    model_validator,
)

from whiskeyjack_bot.config import SupportedQuestionType, _StrictModel

# The version of the *output record* contract, which is not the prompt's version.
# ``prompts/forecaster.md`` carries both: its H1 reads v1.1.0 (the prompt, M1-401)
# while the shared-fields example carries "schema_version": "1.0.0" (this contract).
# M1-401's parser is anchored to line 1 specifically so the two cannot be confused,
# and the same care is needed here: bumping one does not bump the other.
RESPONSE_SCHEMA_VERSION = "1.0.0"

# Which way an adjustment moves the probability of the resolution event.
# ``prompts/forecaster.md`` "Allowed values".
Direction = Literal["up", "down", "mixed", "none"]

# How far it moves it. The prompt's evidence caps are written in this vocabulary --
# an ``unverified_social`` document "may justify a tiny or small adjustment at most"
# -- so the words are load-bearing, not decoration.
Magnitude = Literal["tiny", "small", "medium", "large"]

# The twelve tags the prompt permits, verbatim and in its order.
ReasoningStrategyTag = Literal[
    "base_rate",
    "status_quo",
    "trend",
    "deadline_hazard",
    "inside_view",
    "outside_view",
    "market_signal",
    "institutional_process",
    "historical_analogy",
    "scenario_mixture",
    "measurement_model",
    "source_reconciliation",
]

# Every closed vocabulary above is checked against ``get_args`` rather than a
# restated tuple, the questions/normalize.py idiom: a value added to the alias
# cannot leave a consumer silently out of step.
REASONING_STRATEGY_TAGS: frozenset[str] = frozenset(get_args(ReasoningStrategyTag))

# The prompt's word cap on the one free-text summary that reaches the ledger
# ("rationale_summary must be no more than 120 words"). D24 is the reason it has a
# cap at all: the record stores a concise auditable rationale, never deliberation.
MAX_RATIONALE_WORDS = 120


def _require_non_blank(value: str) -> str:
    """Reject a string that is present but empty.

    A blank required field is an absent answer wearing a field name, and the model
    output is the one place in this pipeline where that distinction is routinely
    tested. Whitespace-only counts as blank: ``str.strip()`` removes the full Unicode
    whitespace set, and there is no SQL layer under this module for a narrower
    ``trim()`` to disagree with (the two-layer failure M1-603 round 5 was about).
    """
    if not value.strip():
        # No value in the message: this is model output.
        raise ValueError("must not be blank")
    return value


NonBlankStr = Annotated[str, AfterValidator(_require_non_blank)]


def _to_utc(value: datetime) -> datetime:
    """Normalize an aware timestamp to UTC, the research/model.py rule."""
    return value.astimezone(timezone.utc)


# Timezone-aware only, normalized to UTC. ``as_of_utc`` is the prompt's hard
# information cutoff, so a naive value is not a cutoff at all.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]


# A probability, structurally. The *configured* bounds -- forecast.min_probability
# and forecast.max_probability -- are M1-403's criterion and are deliberately not
# applied here: this module never reads config.
Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class _ResponsePart(_StrictModel):
    """Base for the nested pieces of a response, so the strictness is stated once."""


class BaseRate(_ResponsePart):
    """The reference class the forecast starts from (prompt, Method step 1)."""

    reference_class: NonBlankStr
    # None for a non-binary question: the prompt says so outright, and the
    # cross-field rule that enforces it lives on the response, which knows the type.
    prior_probability: Probability | None = None
    basis: NonBlankStr
    source_ids: list[NonBlankStr] = Field(default_factory=list)


class EvidenceAdjustment(_ResponsePart):
    """One move away from the prior, with its direction, size and citations."""

    claim: NonBlankStr
    direction: Direction
    magnitude: Magnitude
    source_ids: list[NonBlankStr] = Field(default_factory=list)
    load_bearing: bool


class LoadBearingFact(_ResponsePart):
    """A fact the forecast would change without."""

    claim: NonBlankStr
    source_ids: list[NonBlankStr] = Field(default_factory=list)


class BinaryPrediction(_ResponsePart):
    """``final_prediction`` for a binary question."""

    probability_yes: Probability


class OptionProbability(_ResponsePart):
    """One option of a multiple-choice question and its probability."""

    option: NonBlankStr
    probability: Probability


class MultipleChoicePrediction(_ResponsePart):
    """``final_prediction`` for a multiple-choice question.

    That every *supplied* option appears exactly once and that the probabilities sum
    to 1 within 1e-6 is M1-404's acceptance criterion: both need the question's option
    list, which this module does not read. **That rule now exists**, in
    ``forecast/multiple_choice.py``, reached through ``forecast.validate``; this
    docstring used to stop at "which this module does not read", and a pointer that says
    only where a rule is *not* is how M1-501 lost a round.
    """

    options: list[OptionProbability] = Field(min_length=1)


class PercentilePoint(_ResponsePart):
    """One declared percentile level and its value."""

    percentile: Probability
    value: float = Field(allow_inf_nan=False)


class NumericPrediction(_ResponsePart):
    """``final_prediction`` for a numeric question.

    That the nine levels are exactly the declared ones, non-decreasing and compatible
    with the question's bounds is M1-405's acceptance criterion; all three need the
    question, which this module does not read.
    """

    percentiles: list[PercentilePoint] = Field(min_length=1)


class _ForecastResponseBase(_StrictModel):
    """The attribution fields every response carries, whatever its question type.

    Transcribed from ``prompts/forecaster.md`` "Shared fields". ``question_type`` and
    ``final_prediction`` are declared by each subclass, because those two are what
    makes the response type question-specific.
    """

    schema_version: str
    question_id: int
    as_of_utc: UtcDatetime
    base_rate: BaseRate
    model_prior: Probability | None = None
    status_quo: NonBlankStr
    evidence_adjustments: list[EvidenceAdjustment] = Field(default_factory=list)
    load_bearing_facts: list[LoadBearingFact] = Field(default_factory=list)
    # The prompt shows this list empty and never gives it an item shape. Its two
    # unshaped neighbours in the same object -- failure_modes and uncertainty_notes --
    # are lists of strings by example, so it is read the same way. Recorded as an
    # ambiguity resolved by consistency: giving it an object shape the prompt does not
    # print would fail a model that followed the prompt exactly.
    source_disagreements: list[NonBlankStr] = Field(default_factory=list)
    failure_modes: list[NonBlankStr] = Field(default_factory=list)
    reasoning_strategy_tags: list[ReasoningStrategyTag] = Field(default_factory=list)
    rationale_summary: NonBlankStr
    process_confidence: Probability
    uncertainty_notes: list[NonBlankStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def _schema_version_matches(self) -> _ForecastResponseBase:
        if self.schema_version != RESPONSE_SCHEMA_VERSION:
            # The value is withheld: it is model output like any other field.
            raise ValueError(
                f"schema_version must be {RESPONSE_SCHEMA_VERSION} "
                "(offending input withheld from this message)"
            )
        return self

    @model_validator(mode="after")
    def _tags_are_distinct(self) -> _ForecastResponseBase:
        tags = self.reasoning_strategy_tags
        if len(set(tags)) != len(tags):
            # Constant message: the vocabulary is ours, but which tag repeated is
            # still a fact about the model's output and buys nothing here.
            raise ValueError("reasoning_strategy_tags must not repeat a tag")
        return self

    @model_validator(mode="after")
    def _rationale_within_word_cap(self) -> _ForecastResponseBase:
        if len(self.rationale_summary.split()) > MAX_RATIONALE_WORDS:
            raise ValueError(f"rationale_summary must be at most {MAX_RATIONALE_WORDS} words")
        return self


def _reject_priors(response: _ForecastResponseBase) -> None:
    """The one cross-field rule the prompt states outright.

    "If the question is not binary, ``prior_probability`` and ``model_prior`` must be
    ``null``; describe the reference distribution or option prior in ``basis``."

    It lives on the two non-binary responses rather than on the base, because
    ``question_type`` is declared per subclass -- that field is what makes the response
    type question-specific, so narrowing it on the base would be an override that reads
    as an accident rather than as the design.

    The converse -- that a binary response must *supply* a prior -- is deliberately not
    enforced here, and **it landed in ``forecast/binary.py`` with M1-403**, not in
    M1-501's cross-type checker. This sentence used to name M1-501, because that is where
    M1-402 expected the rule to go; the owner settled it onto the binary output path
    instead, since the rule is binary-specific by construction. Left pointing at M1-501 it
    became a live trap: M1-501's round-1 review read it, looked in ``attribution.py``,
    found no prior check and filed a blocking finding for a rule that has been enforced
    since M1-403. Corrected on the M1-501 branch for that reason.

    ``binary.binary_output_problems`` reports both spellings,
    ``validate.output_problems`` reaches it for every binary response (M1-506; the
    composition was ``generate._output_problems`` when the paragraph above was written),
    and ``test_the_binary_prior_rule_belongs_to_binary_py`` pins the split from M1-501's
    side.
    """
    if response.base_rate.prior_probability is not None or response.model_prior is not None:
        raise ValueError("prior_probability and model_prior must be null for this question type")


class BinaryForecastResponse(_ForecastResponseBase):
    """A binary forecast: the shared fields plus ``probability_yes``."""

    question_type: Literal["binary"]
    final_prediction: BinaryPrediction


class MultipleChoiceForecastResponse(_ForecastResponseBase):
    """A multiple-choice forecast: the shared fields plus one entry per option."""

    question_type: Literal["multiple_choice"]
    final_prediction: MultipleChoicePrediction

    @model_validator(mode="after")
    def _no_priors_for_a_non_binary_question(self) -> MultipleChoiceForecastResponse:
        _reject_priors(self)
        return self


class NumericForecastResponse(_ForecastResponseBase):
    """A numeric forecast: the shared fields plus the declared percentiles."""

    question_type: Literal["numeric"]
    final_prediction: NumericPrediction

    @model_validator(mode="after")
    def _no_priors_for_a_non_binary_question(self) -> NumericForecastResponse:
        _reject_priors(self)
        return self


# The public value type. ``response_model_for`` returns one of its members.
ForecastResponse = BinaryForecastResponse | MultipleChoiceForecastResponse | NumericForecastResponse

ForecastResponseT = TypeVar("ForecastResponseT", bound=_ForecastResponseBase)

# Keyed on the question-type literal, never on isinstance: DiscreteQuestion subclasses
# NumericQuestion in the pinned SDK, so an isinstance test silently normalizes an
# unsupported type as numeric. questions/normalize.py carries the same rule and the
# regression test for it.
_RESPONSE_MODELS: dict[str, type[ForecastResponse]] = {
    "binary": BinaryForecastResponse,
    "multiple_choice": MultipleChoiceForecastResponse,
    "numeric": NumericForecastResponse,
}

# Derived from config's single source of truth (D20) rather than restated -- the
# questions/normalize.py idiom, so a type added there cannot leave this module
# silently out of step. The mapping above still spells each key out, because every
# one needs its own model; what is derived is the *set*, and
# tests/unit/test_forecast_schema.py pins the two equal.
SUPPORTED_RESPONSE_TYPES: frozenset[str] = frozenset(get_args(SupportedQuestionType))


class ForecastSchemaError(Exception):
    """A model response failed validation, with the response withheld.

    Same hygiene rule as ``ResearchSchemaError``/``ConfigError``: pydantic renders the
    offending input in its message, and this input is raw model output.
    """

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid forecast response:\n" + "\n".join(f"  - {p}" for p in problems))


def response_model_for(question_type: str) -> type[ForecastResponse]:
    """The response model for one question type.

    Raises :class:`ForecastSchemaError` for anything outside the v1 vocabulary, so an
    unsupported type cannot fall through to a default and be forecast as the wrong
    shape. The argument is the canonical question's ``qtype`` literal.
    """
    # Exact-type gate before the lookup, not isinstance: a str subclass is unvetted
    # (the questions/normalize.py rule), and an *unhashable* argument -- a list, a
    # dict -- makes dict.get raise a raw TypeError. That escaping is the defect this
    # project has taken as a review finding twice: every malformed shape must arrive
    # as this module's own error. Found by tests/property/test_forecast_properties.py.
    model = _RESPONSE_MODELS.get(question_type) if type(question_type) is str else None
    if model is None:
        # The vocabulary is ours to name; the offending value is not. Both the
        # unsupported-type case and the (test-pinned unreachable) registered-nowhere
        # case arrive here, so neither can escape as a raw KeyError.
        raise ForecastSchemaError(
            [
                "question_type: must be one of "
                + ", ".join(sorted(SUPPORTED_RESPONSE_TYPES))
                + " (offending input withheld)"
            ]
        )
    return model


# Substituted for any error-location part the schema did not author. Matches the
# wording config.py and research/model.py use.
_WITHHELD = "<withheld>"


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    """Every pydantic model reachable from one field annotation."""
    found: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    for arg in get_args(annotation):
        found.extend(_nested_models(arg))
    return found


def _schema_field_names(model: type[BaseModel]) -> frozenset[str]:
    """Field names declared anywhere in ``model`` or a model nested inside it.

    ``research/model.py::_sanitize`` collects only the top-level field names, which is
    right for its two flat models. This response is four levels deep, so the same rule
    applied naively would withhold ``base_rate.prior_probability`` -- a name this
    schema authored -- and turn every nested diagnostic into ``<withheld>.<withheld>``.
    An error nobody can act on is its own failure mode (the M1-401 path carve-out made
    the same argument).

    Widening it stays safe because the set is still *schema-authored only*: an
    unexpected key under ``extra="forbid"`` has that key as its own ``loc``, and a key
    the model invented is in no ``model_fields`` anywhere, so it is still withheld.
    """
    names: set[str] = set()
    seen: set[type[BaseModel]] = set()
    stack: list[type[BaseModel]] = [model]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        names.update(current.model_fields)
        for field in current.model_fields.values():
            stack.extend(_nested_models(field.annotation))
    return frozenset(names)


def _sanitize(exc: ValidationError, model: type[BaseModel]) -> ForecastSchemaError:
    """Render a ValidationError with every model-controlled fragment removed.

    ``include_input=False`` withholds the offending value, but ``loc`` can itself be
    model output: under ``extra="forbid"`` the location of an unexpected key *is* that
    key. So a location part survives only if this schema authored it -- an int list
    index, or a field name declared somewhere in the model tree.

    As in ``research/model.py``, the message text cannot be filtered here: a
    ``ValueError`` raised by any validator becomes ``err["msg"]`` verbatim. The
    companion invariant is on the validators above -- every raise in this module uses
    a constant, value-free message -- and the property suite is the net.
    """
    known = _schema_field_names(model)
    problems = []
    for err in exc.errors(include_input=False, include_url=False):
        parts = [
            str(part) if isinstance(part, int) or part in known else _WITHHELD
            for part in err["loc"]
        ]
        location = ".".join(parts) or "<root>"
        problems.append(f"{location}: {err['msg']}")
    return ForecastSchemaError(problems)


def validate_forecast_response(data: Any, model: type[ForecastResponseT]) -> ForecastResponseT:
    """Validate a model response; raises ForecastSchemaError on failure.

    The sanctioned entry point. Unlike a bare ``model_validate``, its errors never
    echo the model's output -- which is untrusted text that may quote anything the
    provider was sent, including the research packet.
    """
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        # from None: a chained __cause__ re-exposes the raw ValidationError (which
        # echoes inputs) whenever this error reaches a traceback renderer.
        raise _sanitize(exc, model) from None
