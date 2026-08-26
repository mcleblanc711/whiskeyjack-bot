"""The reasoning packet handed to the forecaster model (M1-402).

``prompts/forecaster.md`` section "Inputs" enumerates what the model receives. This
module builds exactly that and nothing else, from a canonical question (M1-201/202)
and a research packet (M1-306). Every extra field would be extra tokens and one more
piece of unvetted text in a transcript the ledger has to stand behind, so the list is
treated as a contract rather than a suggestion; the two deliberate departures from it
are named below and in ``docs/M1-401-NOTES.md``.

**The community prediction cannot leak through here, structurally.** D22 and the v1
hard constraints forbid it as a model input, and ``CanonicalQuestion`` carries no such
field to render -- M1-202 lifted only the group parent's *title* out of the raw post
payload for exactly this reason. So this is not a filter that could be forgotten; it
is an absence. ``tests/unit/test_forecast_inputs.py`` asserts it anyway, because an
absence nobody checks is one a later field addition can end.

**``source_id`` is minted here**, because nothing upstream has one. The prompt asks
for documents carrying "a stable ``source_id``" and cites them as ``src-001``;
``ResearchDocument`` has no such field, and ``document_id`` is a writer-minted UUID
that ``packet_sha256`` deliberately excludes -- so it identifies *when a row was
written*, not what the evidence is, and is the wrong citation token. Ids are assigned
over the documents sorted by ``dedup.dedup_key``, which is the ledger's own
``UNIQUE (retrieval_run_id, canonical_url, content_sha256)`` and the same total order
``packet_sha256`` sorts by. That makes the mapping a function of packet *identity*
rather than of the caller's bookkeeping: ``ResearchPacket`` keeps its tuples in
supplied order on purpose, and replay reads documents back ordered by
``canonical_url, content_sha256``, so anything keyed on the tuple order would assign
different ids to the same evidence on replay.

This module imports no provider SDK, **and that is now measured rather than hedged.**
When M1-402 wrote this paragraph it said the opposite, and correctly so at the time:
``questions/__init__.py`` re-exported every submodule, so importing
``whiskeyjack_bot.questions.model`` pulled in ``normalize`` and through it
``forecasting_tools``, ``asknews_sdk``, ``litellm``, ``httpx`` and ``streamlit``. That
block has since been gutted -- the same fix M1-308 round 4 applied to
``research/__init__.py`` -- and a fresh interpreter importing this module now loads none
of them.

M1-406 turned that from a nice property into a load-bearing one: ``forecast/replay.py``
names :class:`SourceReference` at runtime, so a re-export block reappearing in
``questions/__init__.py`` would put a provider client back on the replay path. It is
asserted out of process by
``tests/unit/test_forecast_generate.py::test_the_response_schema_reaches_no_provider_client``
rather than described here, because a fact a replay path depends on belongs in the
assertion. **M1-204's acceptance criteria appear already met on master** -- observed while
building M1-406, left as a backlog row for its owner to verify and flip rather than
flipped from another item's branch.

``forecast.schema`` *is* clean, and that one is load-bearing rather than tidy: M1-406
must replay a stored response without the provider client being reachable at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import Field

from whiskeyjack_bot.config import SupportedQuestionType, _StrictModel
from whiskeyjack_bot.questions.model import CanonicalQuestion, _CanonicalQuestionBase
from whiskeyjack_bot.research.dedup import dedup_key
from whiskeyjack_bot.research.model import ResearchDocument
from whiskeyjack_bot.research.packet import ResearchPacket

# Zero-padded to three digits, matching the prompt's own ``src-001``. Three digits
# covers retrieval.max_queries_per_question * max_documents_per_query many times over;
# a run that somehow exceeded it would widen the field rather than wrap, so the ids
# stay distinct either way.
_SOURCE_ID_TEMPLATE = "src-{index:03d}"

# The fields the prompt lists conditionally -- "``options`` for multiple-choice
# questions", numeric bounds "when applicable". They are dropped from the rendered
# message when they do not apply, so "when applicable" is literally true rather than
# approximated with a null. Every *unconditionally* listed field keeps its null: for
# those, "we looked and there is none" and "we did not send it" are different facts,
# and a forecaster that cannot tell them apart should be the more cautious of the two
# for the wrong reason.
_CONDITIONAL_FIELDS = frozenset(
    {
        "options",
        "lower_bound",
        "upper_bound",
        "open_lower_bound",
        "open_upper_bound",
        "zero_point",
    }
)


class ForecastInputError(Exception):
    """A reasoning packet could not be built from the supplied arguments.

    Same hygiene rule as ``ConfigError``/``ResearchSchemaError``: the message is a
    constant and never echoes a question, a document or a caller value.
    """


class ForecastDocumentInput(_StrictModel):
    """One piece of evidence as the model sees it.

    Field names are the prompt's, not the ledger's. Three fields the "Inputs" list
    does not name are carried anyway, because the prompt's *General rules* read them:
    an ``unverified_social`` document "may justify a tiny or small adjustment at most"
    and an ``llm_reported`` one is capped unless corroborated. A rule the model is
    told to apply against fields it is never shown is not a rule.
    """

    source_id: str = Field(min_length=1)
    # The canonical URL, not the as-retrieved one: it is this document's identity in
    # the ledger and the key the source mapping resolves through. ``original_url``
    # stays recoverable from the row, so nothing is lost by not sending it.
    url: str = Field(min_length=1)
    title: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    retrieved_at: datetime
    # One field, because the prompt asks for one ("a short evidence summary"), and the
    # two providers populate different columns: AskNews fills ``summary``, Exa fills
    # ``snippet`` and leaves ``summary`` None. Sending only one of them would hand the
    # model evidence with no content from whichever provider it did not name.
    evidence_summary: str | None = None
    source_type: str
    provenance: str
    reliability_tag: str | None = None


class ForecastModelInput(_StrictModel):
    """The whole reasoning packet, in the prompt's vocabulary.

    Two name bridges from the canonical question, both pinned by tests: the prompt
    says ``question_text`` where the model says ``title``, and ``question_type`` where
    it says ``qtype``.
    """

    as_of_utc: datetime
    question_id: int
    post_id: int
    # The canonical question carries ``tournament_slugs`` (a list), never an id, so
    # this is supplied by the caller and validated rather than derived.
    tournament_id: str = Field(min_length=1)
    question_type: SupportedQuestionType
    question_text: str = Field(min_length=1)
    background_info: str | None = None
    resolution_criteria: str | None = None
    fine_print: str | None = None
    open_time: datetime | None = None
    close_time: datetime | None = None
    scheduled_resolution_time: datetime | None = None
    # Departure from the "Inputs" list, deliberate. M1-202 lifts the parent title out
    # of the group post precisely so "the forecaster always receives what is actually
    # being asked": an unpacked subquestion's own title can be a bare option label
    # ("September 2024"), which is not self-describing. Omitting it to keep the list
    # literal would mean forecasting a question the model cannot see. Recorded as a
    # candidate for the prompt's next version bump, which this branch does not make.
    group_parent_title: str | None = None
    # Multiple-choice only.
    options: list[str] | None = None
    # Numeric only.
    lower_bound: float | None = None
    upper_bound: float | None = None
    open_lower_bound: bool | None = None
    open_upper_bound: bool | None = None
    zero_point: float | None = None
    # The second departure from the "Inputs" list, and the same argument as the
    # first. The prompt asks a numeric question for percentile *values*; a value has
    # no meaning without its unit, and the bounds it must respect are printed in that
    # unit. Sending the bounds while withholding what they measure invites a forecast
    # on the wrong scale. Also a candidate for the prompt's next version bump.
    unit_of_measure: str | None = None
    research_documents: list[ForecastDocumentInput] = Field(default_factory=list)


@dataclass(frozen=True)
class SourceReference:
    """What one ``source_id`` in the model's output resolves back to."""

    source_id: str
    document_id: str | None
    canonical_url: str
    content_sha256: str


@dataclass(frozen=True)
class ModelInput:
    """A built reasoning packet plus the mapping needed to audit its citations.

    The mapping is returned rather than stored: M1-406 and M1-602 own persistence,
    and a citation nobody can resolve is the attribution loss this project exists to
    prevent.
    """

    packet: ForecastModelInput
    sources: tuple[SourceReference, ...]


def _require_aware_utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ForecastInputError(f"{label} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _document_input(document: ResearchDocument, source_id: str) -> ForecastDocumentInput:
    return ForecastDocumentInput(
        source_id=source_id,
        url=document.canonical_url,
        title=document.title,
        publisher=document.publisher,
        published_at=document.published_at_utc,
        updated_at=document.updated_at_utc,
        retrieved_at=document.retrieved_at_utc,
        evidence_summary=document.summary if document.summary is not None else document.snippet,
        source_type=document.source_type,
        provenance=document.provenance,
        reliability_tag=document.reliability_tag,
    )


def build_model_input(
    *,
    question: CanonicalQuestion,
    packet: ResearchPacket,
    tournament_id: str,
    as_of: datetime,
) -> ModelInput:
    """Build the reasoning packet for one question; raises :class:`ForecastInputError`.

    Arguments are checked before anything is built, and the checks are repeated at the
    spending site in ``forecast.generate`` for the reason ``research/exa.py`` repeats
    its own: a value carries no memory of which caller validated it.
    """
    if not isinstance(question, _CanonicalQuestionBase):
        raise ForecastInputError("question must be a canonical question")
    if not isinstance(packet, ResearchPacket):
        raise ForecastInputError("packet must be a ResearchPacket")
    if packet.question_id != question.question_id:
        # No ids in the message: a question id is row content (M1-202's precedent).
        raise ForecastInputError("packet must belong to the question being forecast")
    if type(tournament_id) is not str or not tournament_id.strip():
        raise ForecastInputError("tournament_id must be a non-blank string")
    as_of_utc = _require_aware_utc(as_of, "as_of")

    ordered = sorted(packet.documents, key=dedup_key)
    sources = tuple(
        SourceReference(
            source_id=_SOURCE_ID_TEMPLATE.format(index=index),
            document_id=document.document_id,
            canonical_url=document.canonical_url,
            content_sha256=document.content_sha256,
        )
        for index, document in enumerate(ordered, start=1)
    )
    documents = [
        _document_input(document, reference.source_id)
        for document, reference in zip(ordered, sources, strict=True)
    ]

    fields: dict[str, Any] = {
        "as_of_utc": as_of_utc,
        "question_id": question.question_id,
        "post_id": question.post_id,
        "tournament_id": tournament_id,
        "question_type": question.qtype,
        "question_text": question.title,
        "background_info": question.background_info,
        "resolution_criteria": question.resolution_criteria,
        "fine_print": question.fine_print,
        "open_time": question.open_time,
        "close_time": question.close_time,
        "scheduled_resolution_time": question.scheduled_resolution_time,
        "group_parent_title": question.group_parent_title,
        "unit_of_measure": question.unit_of_measure,
        "research_documents": documents,
    }
    # Dispatch on the question-type literal, never isinstance -- the CLAUDE.md rule
    # that exists because DiscreteQuestion subclasses NumericQuestion in the pinned
    # SDK. CanonicalQuestion is a discriminated union on that literal, so the branches
    # below need no isinstance narrowing at all: mypy --strict resolves the subtype
    # from the tag, which is the same fact the dispatch rule rests on.
    if question.qtype == "multiple_choice":
        fields["options"] = list(question.options)
    elif question.qtype == "numeric":
        fields["lower_bound"] = question.lower_bound
        fields["upper_bound"] = question.upper_bound
        fields["open_lower_bound"] = question.open_lower_bound
        fields["open_upper_bound"] = question.open_upper_bound
        fields["zero_point"] = question.zero_point

    try:
        built = ForecastModelInput(**fields)
    except Exception:
        # Deliberately broad and scoped to this one call, the M1-308 round-7 rule:
        # a pydantic failure here is a bug in this function rather than bad user
        # input, but its message would echo the question's own text.
        raise ForecastInputError(
            "the reasoning packet could not be built (detail withheld: it can echo "
            "question or document content)"
        ) from None
    return ModelInput(packet=built, sources=sources)


def render_model_input(model_input: ModelInput) -> str:
    """Render a reasoning packet as the exact bytes sent to the model.

    The persisted-form rule from M1-305/M1-306, applied to a message rather than a
    hash and for the same reason: ``model_dump(mode="json")`` then ``json.dumps`` with
    ``ensure_ascii``, ``sort_keys`` and compact separators is canonical, so the same
    question and packet render the same bytes on replay and a stored request can be
    compared byte for byte with a regenerated one.

    ``ensure_ascii=True`` also escapes a lone surrogate instead of failing to encode
    it -- reachable here, because provider text reaches these fields through the
    research document. ``allow_nan=False`` refuses NaN and Infinity, which
    ``json.dumps`` would otherwise emit as bare tokens no JSON reader accepts.

    ``warnings=False``: a pydantic serializer warning embeds the offending value in
    its text and reaches stderr and captured logs, so it is an egress channel rather
    than noise (M1-302 round 1, and ``pyproject.toml`` makes any such warning a suite
    failure).
    """
    if not isinstance(model_input, ModelInput):
        raise ForecastInputError("model_input must be a ModelInput")
    try:
        dumped = model_input.packet.model_dump(mode="json", warnings=False)
        payload = {
            name: value
            for name, value in dumped.items()
            if not (name in _CONDITIONAL_FIELDS and value is None)
        }
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except Exception:
        raise ForecastInputError(
            "the reasoning packet could not be rendered as canonical JSON "
            "(detail withheld: it can echo question or document content)"
        ) from None
