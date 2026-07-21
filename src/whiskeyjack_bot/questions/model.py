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
    question_weight: float | None = None
    # Group-parent identity is carried through unchanged so M1-202 can unpack
    # subquestions without losing the parent linkage.
    group_question_option: str | None = None
    question_ids_of_group: list[int] | None = None


class CanonicalBinaryQuestion(_CanonicalQuestionBase):
    qtype: Literal["binary"] = "binary"


class CanonicalMultipleChoiceQuestion(_CanonicalQuestionBase):
    qtype: Literal["multiple_choice"] = "multiple_choice"
    options: list[str] = Field(min_length=1)
    option_is_instance_of: str | None = None


class CanonicalNumericQuestion(_CanonicalQuestionBase):
    qtype: Literal["numeric"] = "numeric"
    lower_bound: float
    upper_bound: float
    open_lower_bound: bool
    open_upper_bound: bool
    zero_point: float | None = None
    # The Metaculus/SDK cdf resolution; agrees with config.expected_cdf_points
    # (Literal[201]). Kept as a plain int here -- calibration-time enforcement
    # of the point count belongs to the validation epic (M1-503), not the model.
    cdf_size: int
    nominal_lower_bound: float | None = None
    nominal_upper_bound: float | None = None

    @model_validator(mode="after")
    def _bounds_ordered(self) -> CanonicalNumericQuestion:
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
