"""The common attribution fields, and the citations that make them auditable (M1-501).

``forecast/schema.py`` accepts a response whose evidence lists are all empty and whose
citations name documents that were never supplied. It says so at the fields --
``evidence_adjustments``, ``load_bearing_facts`` and ``failure_modes`` carry
``default_factory=list``, so an omitted key parses -- and M1-403's notes defer the rest
here by name: "cross-type attribution presence ... and **resolving ``source_ids``
against the documents actually supplied**. All M1-501's, which owns them for all three
question types."

Two holes, and the second is the one this project exists to prevent. ``forecast/
inputs.py`` mints ``src-001``... over the packet's documents in ``dedup.dedup_key``
order and hands the mapping back as ``ModelInput.sources``, but nothing has ever
checked that a cited id is one of them: a model could cite ``src-009`` against a
five-document packet and the forecast would flow out of ``generate_forecast`` toward
``forecast_records`` as an attribution claim no evidence backs. The first is smaller
and just as unchecked -- ``prompts/forecaster.md`` line 157 asks the model to confirm
"the question ID and type match the input", and only the type is enforced (by which
response model the dispatch selected).

**The rules live on the output path rather than in the schema**, which is M1-403's
placement decision applied to a second item, for the same three reasons: it leaves
``test_a_structurally_invalid_response_is_refused``'s ``"source_ids": []`` payloads and
``schema.py``'s stated scope intact instead of reversing them, it keeps the schema free
of anything a question or a config supplies, and it makes every rule **repairable** --
``forecast.generate`` runs these inside its parse step, so a violation becomes a
sanitized problem the existing one-repair loop feeds back to the model rather than a
wasted billable call.

So :func:`attribution_problems` **returns** problems instead of raising. Its strings are
the shape ``schema._sanitize`` produces -- a schema-authored field path, a colon, a
value-free message -- and its list indices are rendered exactly as ``_sanitize`` renders
them (``str(part)`` for an int ``loc`` part), so ``generate._repair_turn`` and
``generate._classify`` need no special case.

Seven rules. Three of them stand down when the packet supplied no documents at all:

- ``question_id`` must equal the id the request carried.
- ``failure_modes`` must not be empty.
- ``evidence_adjustments`` must not be empty.                        *(evidence-conditional)*
- ``load_bearing_facts`` must not be empty.                          *(evidence-conditional)*
- Every adjustment and every load-bearing fact must cite at least one id.  *(conditional)*
- Every cited id, anywhere, must resolve to a supplied one.
- No ``source_ids`` list may repeat an id (``schema._tags_are_distinct``'s rule).

**Why three of them are conditional.** A packet may legitimately hold no documents --
``research/store.py`` names the state outright, "a question researched and found
nothing" -- and **M1-504** owns the gate over it, with ``forecast.fail_on_stale_research``
and ``forecast.flag_on_stale_research`` as its committed config. Requiring a citation
that cannot exist would fail every such forecast through the repair loop instead: two
billed calls to reject something no model could have supplied, which is the failure
``binary.py::_require_config`` refuses for the inverted-bounds case. The unconditional
half still bites -- citing ``src-001`` when nothing was supplied names evidence that
does not exist -- so nothing passes silently; what M1-501 declines to decide is whether
a no-research forecast may proceed at all.

Three fields the row does **not** name stay optional, and each for a stated reason:
``source_disagreements`` (the prompt's own shared-fields example prints it as ``[]``),
``uncertainty_notes`` (not named by the row), and ``base_rate.source_ids`` -- the prompt
allows a broad prior where no reference class is defensible, and requiring citations
there would fail a reply that followed the prompt exactly. Ids that *are* present in
any of them are still resolved.

**No message renders the supplied ids**, and that is the deliberate difference from
``binary.py``, which renders its configured bounds. The argument there was that
``prompts/forecaster.md`` prints 0.001-0.999 as a literal while config may narrow it, so
a repair turn omitting the actual bound is one no model can satisfy. It does not carry
here: the model already holds the whole id list in its own request, under
``research_documents``, so naming them back buys nothing and grows the message by every
document retrieved. Nothing in this module renders a value.

This module owns :class:`AttributionFieldError`, which **subclasses**
:class:`ForecastSchemaError` -- M1-403 round 1's settled reading, where subclassing is
not a compromise between "one error type per package" and "one per module" but satisfies
both.

**Primitives, not ``ModelInput``.** The natural signature would take the built reasoning
packet, which carries both the question id and the source mapping. It is not taken,
because when M1-501 chose the signature, importing ``forecast.inputs`` reached
``questions.model`` and through it ``forecasting_tools``, ``litellm`` and ``httpx``
(``inputs.py``, filed as M1-204) -- and like ``forecast/schema.py`` and
``forecast/binary.py``, this has to stay reachable from M1-406's replay path with the
provider client not importable at all. The caller maps
``tuple(reference.source_id for reference in model_input.sources)``, which is one line at
the one call site.

That coupling is **gone** -- ``questions/__init__.py``'s re-export block has since been
gutted, and M1-406's import-graph test now pins ``forecast.inputs`` clean alongside this
module. The signature stays as it is anyway: taking primitives is what makes this
module's independence a property of its own interface rather than of another package's
``__init__.py``, which is exactly the thing that changed underneath it once already.
"""

from __future__ import annotations

from collections.abc import Sequence

from whiskeyjack_bot.forecast.schema import (
    ForecastResponse,
    ForecastResponseT,
    ForecastSchemaError,
    _ForecastResponseBase,
)

# The field paths these problems are reported against, spelled as ``schema._sanitize``
# would spell them so the two are indistinguishable in a repair turn. Every one is a
# name this project's schema authored; none is model output.
_QUESTION_ID_LOC = "question_id"
_FAILURE_MODES_LOC = "failure_modes"
_ADJUSTMENTS_LOC = "evidence_adjustments"
_FACTS_LOC = "load_bearing_facts"
_BASE_RATE_SOURCES_LOC = "base_rate.source_ids"

# Every message below is a constant. The offending value is never interpolated, and the
# supplied id set is never rendered either -- see the module docstring for why that
# differs from binary.py.
_QUESTION_ID_MISMATCH = (
    "must be the question this forecast was requested for (offending input withheld)"
)
# The wording ``schema._require_non_blank`` uses for the same idea one level down: a
# field that is present but says nothing is an absent answer wearing a field name.
_MUST_NOT_BE_EMPTY = "must not be empty"
_MUST_CITE_ONE = "must cite at least one source_id supplied in research_documents"
_UNRESOLVED = "must name only source_ids supplied in research_documents (offending input withheld)"
_REPEATED = "must not repeat a source_id"


class AttributionFieldError(ForecastSchemaError):
    """A forecast's attribution fields cannot be used, or were checked against nonsense.

    Subclasses :class:`ForecastSchemaError` for the reason M1-403 round 1 settled: a
    caller handling the forecast package's response failures as one type keeps working
    unchanged -- ``generate._parse`` is exactly such a caller -- while a caller that
    wants *this* module's boundary can name it without importing ``forecast.schema``.

    Carries the same sanitized ``problems`` list as its parent: a field path, a colon
    and a value-free message. Nothing here echoes model output.
    """


def _supplied_ids(source_ids: Sequence[str]) -> frozenset[str]:
    """The set of ids the model was actually given, refusing a caller's wrong shape.

    ``str`` satisfies ``Sequence[str]``, so ``source_ids="src-001"`` type-checks and
    would silently mean "the twelve ids ``s``, ``r``, ``c``, ``-``..." -- the M1-303
    round-4 defect that cost six billable calls in its own module. A generator is
    refused for a quieter version of the same thing: it satisfies no ``Sequence``, and
    one that did would be exhausted by the first membership test.
    """
    if isinstance(source_ids, (str, bytes)) or not isinstance(source_ids, Sequence):
        raise AttributionFieldError(["source_ids: must be a sequence of strings"])
    for value in source_ids:
        # Exact type, not isinstance: a str subclass is unvetted (questions/normalize.py).
        if type(value) is not str:
            raise AttributionFieldError(["source_ids: must be a sequence of strings"])
    return frozenset(source_ids)


def _citation_problems(
    cited: Sequence[str], location: str, supplied: frozenset[str], *, require_one: bool
) -> list[str]:
    """Every problem with one ``source_ids`` list, at most one per rule.

    Aggregated rather than reported per offending id, so neither the text of a problem
    nor **the number of them** varies with the model's output. A per-id list would leak
    how many citations were bad through a channel no leak test that reads only message
    text would see (M1-302's rule that a channel is a channel).
    """
    problems: list[str] = []
    if require_one and not cited:
        problems.append(f"{location}: {_MUST_CITE_ONE}")
    seen: set[str] = set()
    repeated = False
    unresolved = False
    for value in cited:
        if value in seen:
            repeated = True
        seen.add(value)
        if value not in supplied:
            unresolved = True
    if unresolved:
        problems.append(f"{location}: {_UNRESOLVED}")
    if repeated:
        problems.append(f"{location}: {_REPEATED}")
    return problems


def _problems(
    forecast: _ForecastResponseBase, *, question_id: int, source_ids: Sequence[str]
) -> list[str]:
    """The shared body of the two entry points below."""
    if not isinstance(forecast, _ForecastResponseBase):
        # Exact category, not a duck-typed read: a non-response has no fields at all,
        # and that is a caller mistake rather than something to ask the model to fix.
        raise AttributionFieldError(["forecast: must be a forecast response"])
    if type(question_id) is not int:
        # bool is an int subclass, so isinstance would accept True as a question id.
        raise AttributionFieldError(["question_id: must be an int"])
    supplied = _supplied_ids(source_ids)
    # The three evidence rules stand down when there was nothing to cite. See the
    # module docstring: M1-504 owns whether a no-research forecast may proceed at all,
    # and a rule no reply could satisfy costs two billed calls to discover.
    require_citations = bool(supplied)

    problems: list[str] = []
    if forecast.question_id != question_id:
        problems.append(f"{_QUESTION_ID_LOC}: {_QUESTION_ID_MISMATCH}")
    if not forecast.failure_modes:
        problems.append(f"{_FAILURE_MODES_LOC}: {_MUST_NOT_BE_EMPTY}")
    problems.extend(
        _citation_problems(
            forecast.base_rate.source_ids, _BASE_RATE_SOURCES_LOC, supplied, require_one=False
        )
    )
    if require_citations and not forecast.evidence_adjustments:
        problems.append(f"{_ADJUSTMENTS_LOC}: {_MUST_NOT_BE_EMPTY}")
    for index, adjustment in enumerate(forecast.evidence_adjustments):
        problems.extend(
            _citation_problems(
                adjustment.source_ids,
                f"{_ADJUSTMENTS_LOC}.{index}.source_ids",
                supplied,
                require_one=require_citations,
            )
        )
    if require_citations and not forecast.load_bearing_facts:
        problems.append(f"{_FACTS_LOC}: {_MUST_NOT_BE_EMPTY}")
    for index, fact in enumerate(forecast.load_bearing_facts):
        problems.extend(
            _citation_problems(
                fact.source_ids,
                f"{_FACTS_LOC}.{index}.source_ids",
                supplied,
                require_one=require_citations,
            )
        )
    return problems


def attribution_problems(
    forecast: ForecastResponse, *, question_id: int, source_ids: Sequence[str]
) -> list[str]:
    """Every attribution-field and citation problem with one response, any question type.

    An empty list means the response is attributable: it answers the question it was
    asked, it carries the fields M1-501's row names, and every source it cites is one
    the model was given.

    Each string is a schema-authored field path, a colon, and a value-free message --
    safe to log, to store, and to send back to the model as a repair turn.

    Raises :class:`AttributionFieldError` only for a caller mistake (a response of the
    wrong type, a non-``int`` question id, or a source-id sequence of the wrong shape).
    Those are not problems with the model's output and must never become a repair turn.
    """
    return _problems(forecast, question_id=question_id, source_ids=source_ids)


def validate_attribution_fields(
    forecast: ForecastResponseT, *, question_id: int, source_ids: Sequence[str]
) -> ForecastResponseT:
    """Return the response unchanged, or raise with the sanitized problems.

    The entry point for a caller holding a response it cannot repair -- a replay, or a
    validation pass over a stored record. ``forecast.generate`` uses
    :func:`attribution_problems` instead, because inside the attempt loop a problem is a
    repair turn rather than an error.

    Nothing is repaired, dropped or renumbered here. An unresolvable citation is not
    silently removed: a forecast whose evidence list was quietly edited is precisely the
    record the ledger could not stand behind.
    """
    problems = _problems(forecast, question_id=question_id, source_ids=source_ids)
    if problems:
        raise AttributionFieldError(problems)
    return forecast
