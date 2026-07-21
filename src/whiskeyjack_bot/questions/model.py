"""Canonical internal question schema (M1-201).

The bot fetches questions as the pinned ``forecasting-tools`` SDK's Pydantic
models (:mod:`forecasting_tools.data_models.questions`). Those models track the
SDK and can shift under us. This module defines a **stable internal schema**
that the rest of Milestone 1 -- retrieval, forecast generation, validation and
the ledger writers -- depends on instead, so a SDK bump cannot ripple through
the whole pipeline. :mod:`whiskeyjack_bot.questions.normalize` maps the SDK
objects onto these models.

Scope is fixed by decisions D20 (support binary, multiple-choice and numeric in
v1) and D21 (defer date and conditional). Only the three supported types have a
canonical model here; rejecting the deferred types is
:mod:`whiskeyjack_bot.questions.normalize`'s job (and, as a diagnostic event,
M1-203's).

The models are strict (``extra="forbid"``, reusing ``config._StrictModel``) so a
malformed record fails validation loudly -- that is the M1-201 acceptance
contract: golden binary, multiple-choice and numeric fixtures validate and
retain their resolution fine print.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from whiskeyjack_bot.config import SupportedQuestionType, _StrictModel

# Pydantic accepts NaN and +/-infinity for a bare ``float``, but ``model_dump_json``
# serializes them as JSON ``null`` -- which then fails to validate back, breaking the
# round-trip the discriminated union promises. Every canonical float is finite.
_Finite = Annotated[float, Field(allow_inf_nan=False)]


class SourceCategory(_StrictModel):
    """One Metaculus category, carried through with its identity intact.

    Ours, not the SDK's ``Category``, so a SDK bump cannot reach downstream code
    -- the same reason the question models exist. Kept to the identity triple:
    ``emoji`` is presentational and ``description`` is free text that would widen
    the no-echo surface for no downstream gain.

    ``id`` is the only stable identifier: a slug can be renamed, and a slug is
    optional while a name is not. Collapsing the three to one string loses that --
    ``Category(id=17, name="Economics", slug="economy")`` and
    ``Category(id=18, name="economy", slug=None)`` are different categories that a
    ``slug or name`` mapping renders identically.
    """

    id: int
    name: str = Field(min_length=1)
    slug: str | None = None


class _CanonicalQuestionBase(_StrictModel):
    """Fields shared by every supported question type.

    Field names are canonical (ours), not the SDK's; the mapping from SDK
    attribute names lives in :mod:`whiskeyjack_bot.questions.normalize`.
    """

    qtype: SupportedQuestionType
    question_id: int
    post_id: int
    url: str | None = None
    title: str = Field(min_length=1)
    background_info: str | None = None
    # The resolution fine print is the headline retention target of M1-201.
    resolution_criteria: str | None = None
    fine_print: str | None = None
    unit_of_measure: str | None = None
    open_time: datetime | None = None
    close_time: datetime | None = None
    scheduled_resolution_time: datetime | None = None
    tournament_slugs: list[str] = Field(default_factory=list)
    question_weight: _Finite | None = None
    # Uninterpreted passthrough of the SDK's ``categories``. NOT the project's domain
    # tag: that taxonomy lives in config/x_accounts.yaml (econ_data, space_launch, ...)
    # and has no mechanical mapping from Metaculus categories. Carried here only
    # because normalize.py is the single place SDK fields are read, so anything dropped
    # here is unrecoverable downstream without a re-fetch. Deriving a domain tag from
    # this belongs to M1-307 / the forecast record (M1-602) -- which is also why the
    # identity is kept whole rather than flattened to one label per category.
    source_categories: list[SourceCategory] = Field(default_factory=list)
    # Group-parent identity is carried through unchanged so M1-202 can unpack
    # subquestions without losing the parent linkage.
    group_question_option: str | None = None
    question_ids_of_group: list[int] | None = None


class CanonicalBinaryQuestion(_CanonicalQuestionBase):
    qtype: Literal["binary"] = "binary"


class CanonicalMultipleChoiceQuestion(_CanonicalQuestionBase):
    qtype: Literal["multiple_choice"] = "multiple_choice"
    # At least two: a one-option multiple-choice question cannot be forecast, and
    # M1-404 must emit "every exact option once with probabilities summing to one" --
    # which is unrepresentable if labels collapse as mapping keys. The option set is
    # therefore constrained here, at the input contract, rather than downstream.
    options: list[str] = Field(min_length=2)
    option_is_instance_of: str | None = None

    @model_validator(mode="after")
    def _options_are_labelled_and_distinct(self) -> CanonicalMultipleChoiceQuestion:
        # Do not echo the labels: mirror the project-wide rule that a validation
        # message never reprints record content.
        if any(not option.strip() for option in self.options):
            raise ValueError("multiple-choice options must not be blank")
        if len(set(self.options)) != len(self.options):
            raise ValueError("multiple-choice options must be distinct")
        return self


class CanonicalNumericQuestion(_CanonicalQuestionBase):
    qtype: Literal["numeric"] = "numeric"
    lower_bound: _Finite
    upper_bound: _Finite
    open_lower_bound: bool
    open_upper_bound: bool
    zero_point: _Finite | None = None
    # The Metaculus/SDK cdf resolution; agrees with config.expected_cdf_points
    # (Literal[201]). Kept as a plain int here -- calibration-time enforcement
    # of the point count belongs to the validation epic (M1-503), not the model.
    cdf_size: int
    nominal_lower_bound: _Finite | None = None
    nominal_upper_bound: _Finite | None = None

    @model_validator(mode="after")
    def _bounds_ordered(self) -> CanonicalNumericQuestion:
        # NaN is refused by the field's allow_inf_nan=False before this runs, so the
        # comparison cannot be silently false for a non-finite bound.
        if self.lower_bound >= self.upper_bound:
            # Do not echo the bound values: mirror the project-wide rule that a
            # validation message never reprints record content.
            raise ValueError("numeric lower_bound must be strictly less than upper_bound")
        return self


# Discriminated union: pydantic selects the subclass by the ``qtype`` tag, so a
# serialized canonical question round-trips back to the right type.
CanonicalQuestion = Annotated[
    CanonicalBinaryQuestion | CanonicalMultipleChoiceQuestion | CanonicalNumericQuestion,
    Field(discriminator="qtype"),
]

# Adapter for validating/round-tripping a raw dict against the union (the models
# themselves are validated directly when constructed by ``normalize``).
CanonicalQuestionAdapter: TypeAdapter[
    CanonicalBinaryQuestion | CanonicalMultipleChoiceQuestion | CanonicalNumericQuestion
] = TypeAdapter(CanonicalQuestion)
