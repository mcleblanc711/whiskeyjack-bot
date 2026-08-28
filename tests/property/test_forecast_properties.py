"""Property tests for the forecast generation seam (M1-402).

Four invariants, and each one is checked against the pre-feature behaviour before
being trusted -- ``docs/LESSONS.md`` #5 and #9: a property that cannot fail is worse
than no property, because it is read as evidence.

The payload strategy is the part worth reading. Generating arbitrary dicts would
almost never produce something the response schema gets far enough into to exercise a
validator, so ``forecast_payloads`` starts from the *prompt's own valid example* and
mutates it. That is what makes "never raises outside its own error type" a claim about
the validators rather than about the first type check.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from fractions import Fraction
from math import nextafter
from pathlib import Path
from typing import Any

import pytest
from hypothesis import assume, event, given
from hypothesis import strategies as st
from strategies import HOSTILE_TEXT, research_documents, round_trip

from whiskeyjack_bot.forecast.inputs import (
    ForecastInputError,
    build_model_input,
    render_model_input,
)
from whiskeyjack_bot.config import ForecastConfig
from whiskeyjack_bot.forecast.attribution import (
    AttributionFieldError,
    attribution_problems,
    validate_attribution_fields,
)
from whiskeyjack_bot.forecast.binary import binary_output_problems
from whiskeyjack_bot.forecast.multiple_choice import (
    _SUM_TOLERANCE,
    MultipleChoiceOutputError,
    multiple_choice_output_problems,
)
from whiskeyjack_bot.forecast.schema import (
    BinaryForecastResponse,
    ForecastResponse,
    ForecastSchemaError,
    MultipleChoiceForecastResponse,
    response_model_for,
    validate_forecast_response,
)
from whiskeyjack_bot.forecast.validate import (
    _TYPE_CHECKERS,
    ForecastOutputError,
    output_problems,
    validate_output,
)
from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion
from whiskeyjack_bot.research.dedup import dedup_key
from whiskeyjack_bot.research.model import ResearchDocument, ResearchRun
from whiskeyjack_bot.research.packet import PacketError, build_packet

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
RUN_ID = "run-1"


def _json_block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None
    return match.group(1)


VALID_PAYLOAD: dict[str, Any] = {
    **json.loads(_json_block("Shared fields")),
    **json.loads("{" + _json_block("Binary schema") + "}"),
}

# Every leaf the mutations may write. Floats include the values that have broken this
# codebase before: NaN and the infinities cannot round-trip through JSON, and -0.0
# survives it while comparing equal to 0.0 (M1-306).
HOSTILE_VALUES = st.one_of(
    HOSTILE_TEXT,
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**6), max_value=10**6),
    st.sampled_from([float("nan"), float("inf"), float("-inf"), -0.0, 0.0, 1.0, 1.5]),
    st.lists(HOSTILE_TEXT, max_size=3),
    st.dictionaries(HOSTILE_TEXT, HOSTILE_TEXT, max_size=3),
)

_TOP_FIELDS = sorted(VALID_PAYLOAD)
_NESTED = {"base_rate": sorted(VALID_PAYLOAD["base_rate"]), "final_prediction": ["probability_yes"]}


@st.composite
def forecast_payloads(draw: st.DrawFn) -> dict[str, Any]:
    """A near-valid response payload, mutated with hostile values."""
    payload = json.loads(json.dumps(VALID_PAYLOAD))
    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        action = draw(st.sampled_from(["replace", "delete", "extra", "nested", "extra_nested"]))
        if action == "replace":
            payload[draw(st.sampled_from(_TOP_FIELDS))] = draw(HOSTILE_VALUES)
        elif action == "delete":
            payload.pop(draw(st.sampled_from(_TOP_FIELDS)), None)
        elif action == "extra":
            payload[draw(HOSTILE_TEXT)] = draw(HOSTILE_VALUES)
        elif action == "nested":
            parent = draw(st.sampled_from(sorted(_NESTED)))
            if isinstance(payload.get(parent), dict):
                payload[parent][draw(st.sampled_from(_NESTED[parent]))] = draw(HOSTILE_VALUES)
        else:
            parent = draw(st.sampled_from(sorted(_NESTED)))
            if isinstance(payload.get(parent), dict):
                payload[parent][draw(HOSTILE_TEXT)] = draw(HOSTILE_VALUES)
    return payload


# A mutation *plan*, applied twice with two different markers. See
# test_a_validation_message_never_varies_with_the_value_that_failed for why the leak
# property is written this way rather than as a substring check.
_MARKER_ACTIONS = (
    [("replace", field) for field in _TOP_FIELDS]
    + [("in_list", field) for field in _TOP_FIELDS]
    + [("extra", "")]
    + [("nested", f"{parent}.{field}") for parent, fields in _NESTED.items() for field in fields]
    + [("extra_nested", parent) for parent in _NESTED]
)


def _apply(plan: list[tuple[str, str]], marker: str) -> dict[str, Any]:
    payload = json.loads(json.dumps(VALID_PAYLOAD))
    for action, target in plan:
        if action == "replace":
            payload[target] = marker
        elif action == "in_list":
            payload[target] = [marker]
        elif action == "extra":
            payload[marker] = marker
        elif action == "nested":
            parent, field = target.split(".", 1)
            if isinstance(payload.get(parent), dict):
                payload[parent][field] = marker
        elif action == "extra_nested" and isinstance(payload.get(target), dict):
            payload[target][marker] = marker
    return payload


def _verdict(payload: dict[str, Any]) -> str:
    """The whole observable outcome: acceptance, or the exact refusal text."""
    try:
        validate_forecast_response(payload, BinaryForecastResponse)
    except ForecastSchemaError as exc:
        return f"rejected: {exc}"
    return "accepted"


# --- 1. only this module's error type escapes ------------------------------------


@given(forecast_payloads())
def test_validation_never_raises_outside_its_own_error_type(payload: dict[str, Any]) -> None:
    """Model output is untrusted, and every malformed shape must arrive as
    ForecastSchemaError. A raw AttributeError/KeyError/ValueError escaping is a review
    finding in this project -- it has been, twice, and this property found a third:
    ``response_model_for`` reached ``dict.get`` with an unhashable argument."""
    try:
        result = validate_forecast_response(payload, BinaryForecastResponse)
    except ForecastSchemaError:
        return
    assert isinstance(result, BinaryForecastResponse)


@given(st.one_of(HOSTILE_TEXT, st.none(), st.integers(), st.lists(HOSTILE_TEXT, max_size=2)))
def test_response_model_for_never_raises_outside_its_own_error_type(value: Any) -> None:
    try:
        response_model_for(value)
    except ForecastSchemaError:
        return


@given(forecast_payloads())
def test_validation_is_deterministic_over_the_persisted_form(payload: dict[str, Any]) -> None:
    """The same payload, stored and reloaded, must reach the same verdict.

    ``ensure_ascii`` escapes lone surrogates rather than failing to encode them, and
    it is the rendering the ledger actually holds -- so a schema whose verdict changed
    across this boundary would accept a response that could not be replayed.
    """
    reloaded = json.loads(json.dumps(payload, ensure_ascii=True, sort_keys=True, allow_nan=True))

    def verdict(data: dict[str, Any]) -> str:
        try:
            validate_forecast_response(data, BinaryForecastResponse)
        except ForecastSchemaError:
            return "rejected"
        return "accepted"

    assert verdict(payload) == verdict(reloaded)


# --- 2. no generated value reaches a message -------------------------------------


@given(st.lists(st.sampled_from(_MARKER_ACTIONS), min_size=1, max_size=3))
def test_a_validation_message_never_varies_with_the_value_that_failed(
    plan: list[tuple[str, str]],
) -> None:
    """The leak property, written as invariance rather than as a substring check.

    The obvious form -- "the generated value does not appear in the message" -- cannot
    be made to work over hostile text: a draw can be a single character, and ``"0"``
    is a substring of essentially every string this repository produces, so the test
    fails for reasons that have nothing to do with leaking. That is the same trap as
    the ``tmp_path`` substring assertions M1-308 round 5 had to unpick.

    So the same mutation is applied twice with two different markers, and the whole
    observable outcome must be byte-identical. A message that echoed any part of the
    offending value could not satisfy that, and neither could one that named which of
    two spellings it saw. It fails against a sanitizer built on
    ``include_input=True``, which is what makes it evidence.
    """
    first = _apply(plan, "AAAAAAAAAA")
    second = _apply(plan, "ZZZZZZZZZZ")
    assume(first != second)
    assert _verdict(first) == _verdict(second)


@given(st.lists(st.sampled_from(_MARKER_ACTIONS), min_size=1, max_size=2))
def test_the_invariance_property_can_actually_see_a_difference(
    plan: list[tuple[str, str]],
) -> None:
    """The property above passes trivially if every plan produced the same payload.

    This is its vacuity guard: the two markers must really reach the document, so a
    plan whose mutations all landed would be visible as a difference in the payloads
    themselves. Without it, ``assume(first != second)`` could be filtering everything.
    """
    first = _apply(plan, "AAAAAAAAAA")
    second = _apply(plan, "ZZZZZZZZZZ")
    assert first != second or all(action == "nested" for action, _ in plan)


# --- 3. the rendered request survives the storage boundary -----------------------


def _question() -> CanonicalBinaryQuestion:
    return CanonicalBinaryQuestion(question_id=42, post_id=7, title="Will X happen?")


def _run() -> ResearchRun:
    return ResearchRun(
        retrieval_run_id=RUN_ID, question_id=42, provider="asknews", started_at_utc=NOW
    )


def _on_one_run(documents: list[ResearchDocument]) -> list[ResearchDocument]:
    """Re-key documents onto one run and drop dedup-key collisions.

    Re-keying is what makes a packet buildable at all; dropping collisions is what the
    packet constructor requires. Both are done *before* the property so a discarded
    draw is not mistaken for a passing one.
    """
    moved = [d.model_copy(update={"retrieval_run_id": RUN_ID}) for d in documents]
    seen: set[tuple[str, str, str]] = set()
    kept = []
    for document in moved:
        key = dedup_key(document)
        if key not in seen:
            seen.add(key)
            kept.append(document)
    return kept


@given(st.lists(research_documents(), min_size=1, max_size=4))
def test_the_rendered_request_is_stable_across_the_storage_boundary(
    documents: list[ResearchDocument],
) -> None:
    """The property that decides whether a stored forecast can be re-derived.

    The comparison crosses the *real* boundary -- every document is stored and
    reloaded the way replay does it -- because M1-305 and M1-306 both found the same
    trap: an in-memory form carries distinctions (``datetime.fold``, surrogate-pair
    spelling) that JSON drops, so a check that never went through storage asserts
    something stricter than replay can preserve, and passes while replay breaks.
    """
    kept = _on_one_run(documents)
    reloaded = _on_one_run([round_trip(d) for d in kept])
    # A round trip can collapse two distinct in-memory documents onto one persisted
    # form. That is the storage boundary doing exactly what M1-305 documented, not a
    # failure of this property, and the packet is then genuinely a different packet.
    assume(len(reloaded) == len(kept))

    before = build_model_input(
        question=_question(),
        packet=build_packet(42, [_run()], kept),
        tournament_id="minibench",
        as_of=NOW,
    )
    after = build_model_input(
        question=_question(),
        packet=build_packet(42, [_run()], reloaded),
        tournament_id="minibench",
        as_of=NOW,
    )
    assert render_model_input(before) == render_model_input(after)
    assert before.sources == after.sources


@given(st.lists(research_documents(), min_size=1, max_size=4), st.integers(0, 23))
def test_source_ids_are_a_bijection_independent_of_the_supplied_order(
    documents: list[ResearchDocument], rotation: int
) -> None:
    """The ordering claim, stated as a property.

    ``ResearchPacket`` keeps its tuples in supplied order on purpose, so nothing but
    this makes a stored citation resolvable after replay reads the documents back in
    the ledger's order instead.
    """
    kept = _on_one_run(documents)
    shift = rotation % len(kept)
    rotated = kept[shift:] + kept[:shift]

    built = build_model_input(
        question=_question(),
        packet=build_packet(42, [_run()], kept),
        tournament_id="minibench",
        as_of=NOW,
    )
    other = build_model_input(
        question=_question(),
        packet=build_packet(42, [_run()], rotated),
        tournament_id="minibench",
        as_of=NOW,
    )
    ids = [s.source_id for s in built.sources]
    assert len(set(ids)) == len(kept)
    assert ids == [f"src-{i:03d}" for i in range(1, len(kept) + 1)]
    assert built.sources == other.sources
    # Each id addresses the document it was minted for, in the ledger's own order.
    ordered = sorted(kept, key=dedup_key)
    assert [s.canonical_url for s in built.sources] == [d.canonical_url for d in ordered]


@given(st.lists(research_documents(), min_size=1, max_size=3))
def test_building_a_request_never_raises_outside_its_own_error_type(
    documents: list[ResearchDocument],
) -> None:
    kept = _on_one_run(documents)
    try:
        packet = build_packet(42, [_run()], kept)
    except PacketError:
        return
    try:
        rendered = render_model_input(
            build_model_input(
                question=_question(), packet=packet, tournament_id="minibench", as_of=NOW
            )
        )
    except ForecastInputError:
        return
    # ensure_ascii is what makes a lone surrogate renderable at all; a request that is
    # not pure ASCII could not have been escaped and would fail to encode on the wire.
    assert rendered.isascii()
    assert json.loads(rendered)["question_id"] == 42


def _refuse_with_wrong_question(packet: Any) -> ForecastInputError:
    with pytest.raises(ForecastInputError) as excinfo:
        build_model_input(
            question=CanonicalBinaryQuestion(question_id=99, post_id=7, title="Other?"),
            packet=packet,
            tournament_id="minibench",
            as_of=NOW,
        )
    return excinfo.value


def _would_reprint_a_cause(exc: BaseException) -> bool:
    """Whether a traceback renderer would print something this module did not write.

    The ``from None`` invariant, checked directly rather than by reading the rendered
    traceback: an implicit ``__context__`` is printed unless suppressed, and the
    underlying exception is exactly the thing that echoes values.
    """
    if exc.__cause__ is not None:
        return True
    return exc.__context__ is not None and not exc.__suppress_context__


# The empty-packet refusal is the same refusal with no evidence in scope at all, so
# its message is the value-free baseline every other draw must reproduce exactly.
BASELINE_REFUSAL = str(_refuse_with_wrong_question(build_packet(42, [_run()], [])))


@given(st.lists(research_documents(), min_size=1, max_size=3))
def test_a_refusal_message_is_a_constant_whatever_the_evidence_was(
    documents: list[ResearchDocument],
) -> None:
    """Stronger than "the drawn value is not in the message", and the only version
    that works.

    A generated value can be a single character, and ``"0"`` is a substring of every
    traceback this repository produces -- so a substring assertion over hostile text
    fails for a reason that has nothing to do with leaking. The invariant the module
    actually claims is that the message is a *constant*: if it is byte-identical to
    the message produced with no documents at all, no document can have reached it.
    Same trap as the ``tmp_path`` substring assertions in M1-308 round 5.
    """
    kept = _on_one_run(documents)
    try:
        packet = build_packet(42, [_run()], kept)
    except PacketError:
        return
    error = _refuse_with_wrong_question(packet)
    assert str(error) == BASELINE_REFUSAL
    assert not _would_reprint_a_cause(error)


# --- 5. the configured probability bounds (M1-403) --------------------------------


def _forecast_config(minimum: float, maximum: float) -> ForecastConfig:
    """A ForecastConfig with the drawn bounds and everything else at its committed value."""
    return ForecastConfig(
        supported_question_types=["binary", "multiple_choice", "numeric"],
        min_probability=minimum,
        max_probability=maximum,
        community_prediction_policy="log_after_forecast_do_not_use_as_input",
        replay_saved_model_output=False,
        fail_on_stale_research=False,
        flag_on_stale_research=True,
        prompt_path="prompts/forecaster.md",
        prompt_version="1.1.0",
    )


def _binary_response(
    probability: float, *, prior: bool = True, model_prior: bool = True
) -> BinaryForecastResponse:
    payload = json.loads(json.dumps(VALID_PAYLOAD))
    payload["final_prediction"] = {"probability_yes": probability}
    if not prior:
        payload["base_rate"]["prior_probability"] = None
    if not model_prior:
        payload["model_prior"] = None
    return validate_forecast_response(payload, BinaryForecastResponse)


# The schema's own field names, walked from the models rather than imported from
# ``schema._schema_field_names``: a property that asserts against the constant the
# implementation uses passes whatever that constant says (M1-303's lesson).
def _resolves_through_the_schema(location: str, response_model: Any = None) -> bool:
    model: Any = response_model if response_model is not None else BinaryForecastResponse
    for part in location.split("."):
        fields = getattr(model, "model_fields", None)
        if fields is None or part not in fields:
            return False
        annotation = fields[part].annotation
        model = next(
            (
                candidate
                for candidate in (annotation, *getattr(annotation, "__args__", ()))
                if hasattr(candidate, "model_fields")
            ),
            None,
        )
    return True


# 0.001 and 0.999 are the only values ForecastConfig admits at its edges, so the
# strategy is drawn inside them and the pair is ordered rather than assumed apart.
BOUNDS = st.lists(
    st.floats(min_value=0.001, max_value=0.999, allow_nan=False, allow_infinity=False),
    min_size=2,
    max_size=2,
    unique=True,
).map(sorted)

PROBABILITIES = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


@st.composite
def bounds_and_probability(draw: st.DrawFn) -> tuple[float, float, float]:
    """Drawn bounds, and a probability that lands *on* them often enough to matter.

    A free draw over ``PROBABILITIES`` is what this property used first, and the
    mutation harness caught it out: an off-by-one-ulp boundary error survived, because
    hitting ``probability == low`` exactly is measure-zero over a continuous float
    strategy. The single earlier catch was hypothesis's boundary heuristics being lucky,
    which is precisely the "passes for the wrong reason" shape ``docs/LESSONS.md`` #5 is
    about. The interesting bug lives at the boundary, so the strategy is made to go
    there.
    """
    low, high = draw(BOUNDS)
    probability = draw(
        st.one_of(
            st.sampled_from(
                [
                    low,
                    high,
                    nextafter(low, 0.0),
                    nextafter(high, 1.0),
                    nextafter(low, 1.0),
                    nextafter(high, 0.0),
                ]
            ),
            PROBABILITIES,
        )
    )
    return low, high, max(0.0, min(1.0, probability))


@given(bounds_and_probability(), st.booleans(), st.booleans())
def test_the_bounds_check_accepts_exactly_the_probabilities_inside_them(
    drawn: tuple[float, float, float], prior: bool, model_prior: bool
) -> None:
    """The discriminating post-condition, not a smoke test.

    Written as an ``iff`` over the *whole* rule -- an in-bounds probability with a
    missing prior is still refused -- because "returns a list" and "sometimes refuses"
    are both satisfied by code that is wrong at the boundary.
    """
    low, high, probability = drawn
    forecast = _binary_response(probability, prior=prior, model_prior=model_prior)
    problems = binary_output_problems(forecast, _forecast_config(low, high))
    expected = (low <= probability <= high) and prior and model_prior
    assert (problems == []) is expected


@given(PROBABILITIES, BOUNDS, st.booleans(), st.booleans())
def test_the_bounds_check_never_raises_for_a_valid_response_and_a_valid_config(
    probability: float, bounds: list[float], prior: bool, model_prior: bool
) -> None:
    """Every malformed shape must arrive as this project's own error type; a *well*
    formed one must arrive as data. Nothing in this function may raise at all."""
    low, high = bounds
    forecast = _binary_response(probability, prior=prior, model_prior=model_prior)
    problems = binary_output_problems(forecast, _forecast_config(low, high))
    assert all(isinstance(problem, str) for problem in problems)


@given(PROBABILITIES, BOUNDS, st.booleans(), st.booleans())
def test_every_problem_is_reported_at_a_location_the_schema_authored(
    probability: float, bounds: list[float], prior: bool, model_prior: bool
) -> None:
    """A repair turn's field paths must be resolvable, and must be *ours*.

    ``schema._sanitize`` withholds any location part the schema did not declare, so a
    problem this module invents at some other path would be the one field reference in
    a repair turn that means nothing to the reader of a stored failure.
    """
    low, high = bounds
    forecast = _binary_response(probability, prior=prior, model_prior=model_prior)
    for problem in binary_output_problems(forecast, _forecast_config(low, high)):
        location, separator, message = problem.partition(": ")
        assert separator == ": "
        assert message
        assert _resolves_through_the_schema(location)


@given(PROBABILITIES, PROBABILITIES, BOUNDS)
def test_a_bounds_problem_never_varies_with_the_probability_that_failed(
    first: float, second: float, bounds: list[float]
) -> None:
    """Invariance, not substring absence -- the M1-402 leak-property shape.

    "The value does not appear in the message" is unwritable over probabilities: the
    message renders the configured bounds, so ``"0."`` and most short digit runs are
    substrings of it for reasons that have nothing to do with the model's value. Two
    different offending values producing byte-identical text is the claim that
    discriminates.
    """
    low, high = bounds
    assume(not low <= first <= high)
    assume(not low <= second <= high)
    config = _forecast_config(low, high)
    assert binary_output_problems(_binary_response(first), config) == binary_output_problems(
        _binary_response(second), config
    )


@given(BOUNDS, BOUNDS)
def test_the_invariance_property_can_see_the_bounds_change(
    first: list[float], second: list[float]
) -> None:
    """The companion check: the message above is invariant in the model's value and
    *not* invariant in the config's, which is what makes a repair turn actionable."""
    assume(first != second)
    outside = 0.0 if 0.0 < first[0] and 0.0 < second[0] else 1.0
    assume(not first[0] <= outside <= first[1])
    assume(not second[0] <= outside <= second[1])
    forecast = _binary_response(outside)
    left = binary_output_problems(forecast, _forecast_config(*first))
    right = binary_output_problems(forecast, _forecast_config(*second))
    assert left and right
    assert left != right


# --- 6. the attribution fields and their citations (M1-501) -----------------------

# Small pools, so a citation that resolves and one that does not are both drawn often
# rather than by luck. ``src-009`` is never supplied.
_SUPPLIABLE = ["src-001", "src-002", "src-003"]
_CITABLE = [*_SUPPLIABLE, "src-009"]

_ADJUSTMENT = VALID_PAYLOAD["evidence_adjustments"][0]
_FACT = VALID_PAYLOAD["load_bearing_facts"][0]
_VALID_QUESTION_ID = VALID_PAYLOAD["question_id"]

CITATIONS = st.lists(st.sampled_from(_CITABLE), max_size=2)


def _attribution_response(
    *,
    base_ids: list[str],
    adjustment_ids: list[list[str]],
    fact_ids: list[list[str]],
    failure_modes: list[str],
    question_id: int,
) -> BinaryForecastResponse:
    payload = json.loads(json.dumps(VALID_PAYLOAD))
    payload["question_id"] = question_id
    payload["base_rate"]["source_ids"] = base_ids
    payload["evidence_adjustments"] = [{**_ADJUSTMENT, "source_ids": ids} for ids in adjustment_ids]
    payload["load_bearing_facts"] = [{**_FACT, "source_ids": ids} for ids in fact_ids]
    payload["failure_modes"] = failure_modes
    return validate_forecast_response(payload, BinaryForecastResponse)


@st.composite
def attribution_cases(draw: st.DrawFn) -> tuple[BinaryForecastResponse, int, tuple[str, ...]]:
    """A response whose every attribution field was drawn, and the ids it is checked
    against. Every rule M1-501 states is reachable from this strategy."""
    supplied = tuple(draw(st.lists(st.sampled_from(_SUPPLIABLE), max_size=3, unique=True)))
    response = _attribution_response(
        base_ids=draw(CITATIONS),
        adjustment_ids=draw(st.lists(CITATIONS, max_size=2)),
        fact_ids=draw(st.lists(CITATIONS, max_size=2)),
        failure_modes=draw(st.lists(st.just("a failure mode"), max_size=2)),
        question_id=draw(st.sampled_from([_VALID_QUESTION_ID, _VALID_QUESTION_ID + 1])),
    )
    return response, draw(st.just(_VALID_QUESTION_ID)), supplied


HOSTILE_SOURCE_IDS = st.one_of(
    st.lists(HOSTILE_TEXT, max_size=3),
    st.tuples(HOSTILE_TEXT),
    HOSTILE_TEXT,
    st.none(),
    st.integers(),
    st.lists(st.integers(), max_size=2),
    st.dictionaries(HOSTILE_TEXT, HOSTILE_TEXT, max_size=2),
)


@given(attribution_cases(), HOSTILE_SOURCE_IDS, st.one_of(HOSTILE_TEXT, st.none(), st.floats()))
def test_attribution_never_raises_outside_its_own_error_type(
    case: tuple[BinaryForecastResponse, int, tuple[str, ...]],
    source_ids: Any,
    question_id: Any,
) -> None:
    """Every malformed shape must arrive as this module's own error type. A raw
    TypeError out of a membership test against a non-container, or an AttributeError
    from a response that is not one, is the defect this project has taken as a review
    finding twice."""
    response, _, _ = case
    for arguments in (
        {"question_id": _VALID_QUESTION_ID, "source_ids": source_ids},
        {"question_id": question_id, "source_ids": ["src-001"]},
        {"question_id": question_id, "source_ids": source_ids},
    ):
        try:
            problems = attribution_problems(response, **arguments)
        except AttributionFieldError:
            continue
        assert all(isinstance(problem, str) for problem in problems)


@given(st.one_of(HOSTILE_TEXT, st.none(), st.integers(), st.lists(HOSTILE_TEXT, max_size=2)))
def test_attribution_refuses_anything_that_is_not_a_response(value: Any) -> None:
    for entry_point in (attribution_problems, validate_attribution_fields):
        try:
            entry_point(value, question_id=_VALID_QUESTION_ID, source_ids=["src-001"])
        except AttributionFieldError:
            continue
        raise AssertionError("a non-response was accepted")


def _attribution_location_resolves(location: str, response_model: Any = None) -> bool:
    """``_resolves_through_the_schema`` with list indices dropped.

    ``schema._sanitize`` renders an int ``loc`` part as ``str(part)``, so a nested
    problem reads ``evidence_adjustments.0.source_ids``. The index is the schema's own
    rendering rather than a field name, and the rest must still be one.

    ``response_model`` is M1-404's: the composed properties below now draw both binary and
    multiple-choice responses, and ``final_prediction.options`` is a field only one of the
    two models declares. Walking every location against ``BinaryForecastResponse`` would
    have made this assertion fail for a correct problem -- or, worse, pass one that names
    a field the *actual* response type never had.
    """
    parts = [part for part in location.split(".") if not part.isdigit()]
    return _resolves_through_the_schema(".".join(parts), response_model)


@given(attribution_cases())
def test_every_attribution_problem_is_reported_at_a_location_the_schema_authored(
    case: tuple[BinaryForecastResponse, int, tuple[str, ...]],
) -> None:
    """A problem at a location the schema never declared is a location the model
    invented, which is the leak ``schema._sanitize`` exists to prevent."""
    response, question_id, supplied = case
    for problem in attribution_problems(response, question_id=question_id, source_ids=supplied):
        location, separator, message = problem.partition(": ")
        assert separator == ": ", problem
        assert message
        assert _attribution_location_resolves(location), problem


@given(
    st.lists(st.sampled_from(_SUPPLIABLE), min_size=1, max_size=3, unique=True),
    st.sampled_from(["base_rate", "evidence_adjustments", "load_bearing_facts"]),
)
def test_an_attribution_problem_never_varies_with_the_cited_id_that_failed(
    supplied: list[str], field: str
) -> None:
    """The leak property, written as invariance rather than as a substring check --
    M1-403's shape, and for the reason restated there: a short marker is a substring of
    text this module renders for unrelated reasons.

    Two different invented citations, in the same place, must produce byte-identical
    output. A message echoing any part of the id could not satisfy that.
    """

    def verdict(invented: str) -> str:
        response = _attribution_response(
            base_ids=[invented] if field == "base_rate" else list(supplied[:1]),
            adjustment_ids=[[invented] if field == "evidence_adjustments" else list(supplied[:1])],
            fact_ids=[[invented] if field == "load_bearing_facts" else list(supplied[:1])],
            failure_modes=["a failure mode"],
            question_id=_VALID_QUESTION_ID,
        )
        return "\n".join(
            attribution_problems(response, question_id=_VALID_QUESTION_ID, source_ids=supplied)
        )

    first = verdict("AAAAAAAAAA")
    second = verdict("ZZZZZZZZZZ")
    assert first == second
    # Vacuity guard: neither invented id was supplied, so there must *be* a problem.
    assert first


@given(st.sampled_from(_SUPPLIABLE), st.sampled_from(_SUPPLIABLE))
def test_the_invariance_property_can_see_the_supplied_set_change(cited: str, other: str) -> None:
    """The twin of the property above, and the pair is the point.

    Invariance alone is satisfied by a constant string, which would pass every leak
    check while making the checker useless. This says the verdict is *not* invariant in
    what was supplied: the same citation is a problem when it was not supplied and no
    problem when it was.
    """
    assume(cited != other)
    response = _attribution_response(
        base_ids=[cited],
        adjustment_ids=[[cited]],
        fact_ids=[[cited]],
        failure_modes=["a failure mode"],
        question_id=_VALID_QUESTION_ID,
    )
    assert attribution_problems(response, question_id=_VALID_QUESTION_ID, source_ids=[cited]) == []
    assert attribution_problems(response, question_id=_VALID_QUESTION_ID, source_ids=[other])


@given(attribution_cases())
def test_the_evidence_rules_stand_down_exactly_when_nothing_was_supplied(
    case: tuple[BinaryForecastResponse, int, tuple[str, ...]],
) -> None:
    """The conditional stated as a biconditional, in both directions at once.

    M1-504 owns whether a no-research forecast may proceed; M1-501 owns only that the
    citation rules do not fail a reply no model could have given. An implementation
    that dropped the condition, or inverted it, breaks this in one direction or the
    other -- which a one-sided example test cannot see.
    """
    response, question_id, supplied = case
    problems = attribution_problems(response, question_id=question_id, source_ids=supplied)
    conditional = {
        "evidence_adjustments: must not be empty" in problems,
        "load_bearing_facts: must not be empty" in problems,
        any("must cite at least one source_id" in problem for problem in problems),
    }
    if not supplied:
        assert conditional == {False}
    # The unconditional half never stands down, whatever was supplied.
    assert ("failure_modes: must not be empty" in problems) == (not response.failure_modes)


# The seven rules restated in the test's own words, from the *drawn* inputs rather than
# from the implementation. Every message is written out here as a literal: importing the
# module's constants would make this property pass whatever those constants say, which is
# M1-303's lesson and the reason `_resolves_through_the_schema` walks `model_fields` too.
#
# It duplicates logic, deliberately. The failure mode a truth table catches is not "the
# rule is written wrongly twice" -- the unit suite pins each message against a literal for
# that -- it is "a rule quietly stopped firing", which every one-sided property misses. The
# first mutation run proved the point: with only the one-sided conditional property here,
# removing R1, R3, R4, R5 or R7 outright escaped every property in this file.
def _expected_attribution_problems(
    response: BinaryForecastResponse, question_id: int, supplied: tuple[str, ...]
) -> set[str]:
    available = set(supplied)
    require_one = bool(available)
    expected: set[str] = set()
    if response.question_id != question_id:
        expected.add(
            "question_id: must be the question this forecast was requested for "
            "(offending input withheld)"
        )
    if not response.failure_modes:
        expected.add("failure_modes: must not be empty")

    lists: list[tuple[str, list[str], bool]] = [
        ("base_rate.source_ids", list(response.base_rate.source_ids), False)
    ]
    if require_one and not response.evidence_adjustments:
        expected.add("evidence_adjustments: must not be empty")
    for index, adjustment in enumerate(response.evidence_adjustments):
        lists.append(
            (f"evidence_adjustments.{index}.source_ids", list(adjustment.source_ids), require_one)
        )
    if require_one and not response.load_bearing_facts:
        expected.add("load_bearing_facts: must not be empty")
    for index, fact in enumerate(response.load_bearing_facts):
        lists.append((f"load_bearing_facts.{index}.source_ids", list(fact.source_ids), require_one))

    for location, cited, needs_one in lists:
        if needs_one and not cited:
            expected.add(
                f"{location}: must cite at least one source_id supplied in research_documents"
            )
        if any(value not in available for value in cited):
            expected.add(
                f"{location}: must name only source_ids supplied in research_documents "
                "(offending input withheld)"
            )
        if len(set(cited)) != len(cited):
            expected.add(f"{location}: must not repeat a source_id")
    return expected


@given(attribution_cases())
def test_the_problem_set_is_exactly_what_the_seven_rules_specify(
    case: tuple[BinaryForecastResponse, int, tuple[str, ...]],
) -> None:
    """The truth table, enumerated mechanically -- M1-308 round 5's remedy.

    Equality, not containment: a rule that stopped firing fails it, and so does a rule
    that fires where it should not. The one-sided properties around it each say something
    this cannot (invariance, location shape, replay stability); what this adds is that
    every rule is still there at all.
    """
    response, question_id, supplied = case
    problems = attribution_problems(response, question_id=question_id, source_ids=supplied)
    assert set(problems) == _expected_attribution_problems(response, question_id, supplied)
    # No duplicates: the set comparison above would not see one.
    assert len(problems) == len(set(problems))


@given(attribution_cases())
def test_the_truth_table_property_is_not_vacuous(
    case: tuple[BinaryForecastResponse, int, tuple[str, ...]],
) -> None:
    """The guard on the property above: the strategy must reach both verdicts.

    A truth table asserted only over inputs that produce no problems is a test that the
    checker returns an empty list. ``attribution_cases`` draws ``src-009``, empty lists,
    repeats and a mismatched question id, so both halves are reachable -- and hypothesis
    reports the split rather than this test asserting a rate it cannot know.
    """
    response, question_id, supplied = case
    problems = attribution_problems(response, question_id=question_id, source_ids=supplied)
    event("problems found" if problems else "no problems")
    assert isinstance(problems, list)


@given(attribution_cases())
def test_attribution_is_stable_across_the_storage_boundary(
    case: tuple[BinaryForecastResponse, int, tuple[str, ...]],
) -> None:
    """The verdict must be a function of the *persisted* form, M1-305's rule.

    ``model_dump(mode="json")`` renders exactly what the ledger stores, and M1-602 will
    validate stored records rather than in-memory ones. A checker whose answer changed
    across that boundary would pass every test that never went through the ledger.
    """
    response, question_id, supplied = case
    persisted = json.dumps(
        response.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, allow_nan=False
    )
    reloaded = validate_forecast_response(json.loads(persisted), BinaryForecastResponse)
    assert attribution_problems(
        reloaded, question_id=question_id, source_ids=supplied
    ) == attribution_problems(response, question_id=question_id, source_ids=supplied)


# --- 8. the multiple-choice option set and its distribution (M1-404) ---------------
#
# Five rules on one function, so the discriminating property is the *whole truth table*
# rather than "sometimes refuses". Every predicate below is restated independently of the
# implementation -- the sum one exactly, in ``Fraction``, rather than by calling the same
# ``math.fsum`` the source calls.

_MC_LABELS = ["Option A", "Option B", "Option C", "Option D"]
# A label the strategy may answer that no draw can supply, so *unknown* is reachable.
_MC_INTRUDER = "Option Z"
_MC_PROBABILITIES = [0.0, 0.001, 0.1, 0.2, 0.25, 0.3, 0.5, 0.7, 0.9, 0.999, 1.0]
_MC_BOUNDS = [(0.001, 0.999), (0.1, 0.9), (0.2, 0.5)]


def _even_shares(supplied: tuple[str, ...]) -> list[tuple[str, float]]:
    """Every supplied option once, sharing 1 equally.

    Inside every pair in ``_MC_BOUNDS`` for every size the strategy draws, so this branch
    is reliably a *valid* case. Without it the truth table below would be one-sided in
    practice: a freely drawn vector essentially never sums to 1, which is LESSONS #9's
    trap -- a property that looks discriminating because it is green.
    """
    share = 1.0 / len(supplied)
    return [(option, share) for option in supplied]


@st.composite
def option_cases(
    draw: st.DrawFn,
) -> tuple[tuple[str, ...], list[tuple[str, float]], ForecastConfig]:
    """A question's option list, one reply's answers, and the bounds they are checked
    against. Every one of the five rules is reachable, and so is the empty verdict."""
    supplied = tuple(
        draw(st.lists(st.sampled_from(_MC_LABELS), min_size=2, max_size=4, unique=True))
    )
    low, high = draw(st.sampled_from(_MC_BOUNDS))
    config = _forecast_config(low, high)
    if draw(st.booleans()):
        answers = _even_shares(supplied)
        if draw(st.booleans()):
            # Re-ordered, so the valid branch also exercises order-independence.
            answers = list(reversed(answers))
        return supplied, answers, config
    answers = draw(
        st.lists(
            st.tuples(
                st.sampled_from([*_MC_LABELS, _MC_INTRUDER]),
                st.sampled_from(_MC_PROBABILITIES),
            ),
            min_size=1,
            max_size=5,
        )
    )
    return supplied, answers, config


def _mc_response(answers: list[tuple[str, float]]) -> MultipleChoiceForecastResponse:
    payload = json.loads(json.dumps(VALID_PAYLOAD))
    payload["question_type"] = "multiple_choice"
    payload["model_prior"] = None
    payload["base_rate"]["prior_probability"] = None
    payload["final_prediction"] = {
        "options": [{"option": option, "probability": p} for option, p in answers]
    }
    reloaded = validate_forecast_response(payload, MultipleChoiceForecastResponse)
    assert isinstance(reloaded, MultipleChoiceForecastResponse)
    return reloaded


def _expected_option_problems(
    supplied: tuple[str, ...], answers: list[tuple[str, float]], config: ForecastConfig
) -> list[str]:
    """The five rules, restated independently of the implementation.

    The sum predicate is computed in :class:`~fractions.Fraction`, which is the *exact*
    total of the drawn floats rather than a second call to the rounding routine the source
    uses. That is the only one of the five where restating the rule in the test could
    otherwise have meant copying the implementation.
    """
    answered = [option for option, _ in answers]
    distinct = set(answered)
    low, high = config.min_probability, config.max_probability
    problems: list[str] = []
    if not distinct <= set(supplied):
        problems.append(_MC_UNKNOWN)
    if not set(supplied) <= distinct:
        problems.append(_MC_MISSING)
    if len(distinct) != len(answered):
        problems.append(_MC_DUPLICATE)
    if any(not low <= p <= high for _, p in answers):
        problems.append(
            f"final_prediction.options: each probability must be between {low!r} and "
            f"{high!r} inclusive (offending input withheld)"
        )
    if abs(sum(Fraction(p) for _, p in answers) - 1) > Fraction(_SUM_TOLERANCE):
        problems.append(_MC_SUM)
    return problems


_MC_UNKNOWN = (
    "final_prediction.options: must name only options the question supplied "
    "(offending labels withheld)"
)
_MC_MISSING = (
    "final_prediction.options: must name every option the question supplied "
    "(offending labels withheld)"
)
_MC_DUPLICATE = (
    "final_prediction.options: must name each supplied option at most once "
    "(offending labels withheld)"
)
_MC_SUM = (
    "final_prediction.options: probabilities must sum to 1 within 1e-06 (observed sum withheld)"
)


@given(option_cases())
def test_the_option_rules_accept_exactly_the_valid_set(
    case: tuple[tuple[str, ...], list[tuple[str, float]], ForecastConfig],
) -> None:
    """The whole truth table, not a smoke test.

    Equality with the independently derived list rather than ``(problems == []) is ok``:
    the weaker form is green for a checker that reports the wrong rule, and with five
    rules on one field that is the likelier defect.
    """
    supplied, answers, config = case
    exact = sum(Fraction(p) for _, p in answers)
    # The float comparison in the source and the exact one here can only disagree within
    # about an ulp of the tolerance, and no drawn vector lands there. Excluded rather than
    # left to make this property flaky for a reason unrelated to the rule.
    assume(abs(abs(exact - 1) - Fraction(_SUM_TOLERANCE)) > Fraction(1, 10**12))

    problems = multiple_choice_output_problems(_mc_response(answers), config, options=supplied)
    assert problems == _expected_option_problems(supplied, answers, config)


@given(option_cases())
def test_every_option_rule_is_reachable_from_the_strategy(
    case: tuple[tuple[str, ...], list[tuple[str, float]], ForecastConfig],
) -> None:
    """The anti-vacuity guard for the section.

    A truth-table property drawn only from cases where one cell is reachable proves
    nothing about the other four. Hypothesis reports the split rather than this test
    asserting a rate it cannot know.
    """
    supplied, answers, config = case
    problems = multiple_choice_output_problems(_mc_response(answers), config, options=supplied)
    event("verdict: clean" if not problems else "verdict: refused")
    for name, message in (
        ("unknown", _MC_UNKNOWN),
        ("missing", _MC_MISSING),
        ("duplicate", _MC_DUPLICATE),
        ("sum", _MC_SUM),
    ):
        if message in problems:
            event(f"rule bites: {name}")
    if any("each probability must be between" in problem for problem in problems):
        event("rule bites: bounds")


@given(option_cases())
def test_the_option_verdict_does_not_depend_on_the_order_answered(
    case: tuple[tuple[str, ...], list[tuple[str, float]], ForecastConfig],
) -> None:
    """Reversing the reply's option order must not change a single problem.

    Every rule here is about a set or a total, so a verdict that moved with order would
    not be one a replay of the stored response could reproduce.
    """
    supplied, answers, config = case
    forward = multiple_choice_output_problems(_mc_response(answers), config, options=supplied)
    backward = multiple_choice_output_problems(
        _mc_response(list(reversed(answers))), config, options=supplied
    )
    assert forward == backward


@given(option_cases())
def test_the_option_verdict_is_stable_across_the_storage_boundary(
    case: tuple[tuple[str, ...], list[tuple[str, float]], ForecastConfig],
) -> None:
    """M1-305's rule. M1-507 will run this over a record on its way into the ledger, so
    the answer has to be a function of the *persisted* form."""
    supplied, answers, config = case
    response = _mc_response(answers)
    persisted = json.dumps(
        response.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, allow_nan=False
    )
    reloaded = validate_forecast_response(json.loads(persisted), MultipleChoiceForecastResponse)
    assert multiple_choice_output_problems(
        reloaded, config, options=supplied
    ) == multiple_choice_output_problems(response, config, options=supplied)


@given(option_cases())
def test_every_option_problem_is_reported_at_a_location_the_schema_authored(
    case: tuple[tuple[str, ...], list[tuple[str, float]], ForecastConfig],
) -> None:
    supplied, answers, config = case
    for problem in multiple_choice_output_problems(_mc_response(answers), config, options=supplied):
        location, separator, _ = problem.partition(": ")
        assert separator == ": ", problem
        assert _resolves_through_the_schema(location, MultipleChoiceForecastResponse), problem


HOSTILE_OPTIONS = st.one_of(
    st.lists(HOSTILE_TEXT, max_size=3),
    st.tuples(HOSTILE_TEXT),
    HOSTILE_TEXT,
    st.none(),
    st.integers(),
    st.lists(st.integers(), max_size=2),
    st.dictionaries(HOSTILE_TEXT, HOSTILE_TEXT, max_size=2),
)


@given(option_cases(), HOSTILE_OPTIONS, st.one_of(HOSTILE_TEXT, st.none(), st.floats()))
def test_multiple_choice_never_raises_outside_its_own_error_type(
    case: tuple[tuple[str, ...], list[tuple[str, float]], ForecastConfig],
    options: Any,
    config: Any,
) -> None:
    """Every malformed shape must arrive as this module's own error type.

    A raw ``TypeError`` out of a membership test against a non-container, or an
    ``AttributeError`` from a config that is not one, is the defect this project has taken
    as a review finding twice.
    """
    _, answers, valid_config = case
    response = _mc_response(answers)
    for arguments in (
        {"forecast_config": valid_config, "options": options},
        {"forecast_config": config, "options": _MC_LABELS[:2]},
        {"forecast_config": config, "options": options},
    ):
        try:
            problems = multiple_choice_output_problems(
                response,
                arguments["forecast_config"],
                options=arguments["options"],
            )
        except MultipleChoiceOutputError:
            continue
        assert all(isinstance(problem, str) for problem in problems)


@given(
    option_cases(),
    st.one_of(
        st.none(),
        st.just(()),
        st.tuples(HOSTILE_TEXT),
        st.lists(HOSTILE_TEXT, min_size=1, max_size=1),
    ),
)
def test_an_option_list_of_fewer_than_two_always_raises_rather_than_returning(
    case: tuple[tuple[str, ...], list[tuple[str, float]], ForecastConfig],
    options: Any,
) -> None:
    """Arity is a caller mistake, never a repairable problem. Round-1 review, 2026-08-28.

    ``HOSTILE_OPTIONS`` above could already *draw* a one-element list, and the property it
    feeds passed before this branch's fix and after it -- because that property asserts
    only that nothing escapes ``MultipleChoiceOutputError``, and a returned list of
    problems satisfies it just as well as a raise. The strategy reached the branch; the
    assertion was not about it. That is this project's top recurring defect class, so the
    fix is a property whose assertion *is* about arity rather than a wider strategy.

    Fewer than two supplied options cannot be answered at all -- the sum and bounds rules
    cover the line between them -- so returning a problem would ask for an unsatisfiable
    repair at two billed calls per question.
    """
    _, answers, config = case
    response = _mc_response(answers)
    with pytest.raises(MultipleChoiceOutputError) as caught:
        multiple_choice_output_problems(response, config, options=options)
    # When the shape is otherwise well-formed, arity is the reason given -- not a rule
    # problem wearing an exception, and not a message that names the drawn label.
    if isinstance(options, (list, tuple)) and all(type(v) is str for v in options):
        expected = (
            "options: must not be empty"
            if not options
            else "options: must supply at least two options for a multiple-choice question"
        )
        assert caught.value.problems == [expected]


@given(st.lists(st.sampled_from(_MC_LABELS), min_size=2, max_size=3, unique=True))
def test_an_option_problem_never_varies_with_the_label_that_failed(supplied: list[str]) -> None:
    """The leak property as **invariance**, not as a substring check.

    The substring form is the trap this file has already walked into once: a short drawn
    marker is a substring of text these messages render for unrelated reasons. Two
    different invented labels, answered in the same place, must produce byte-identical
    output -- which no message built by interpolating the label could do.
    """
    config = _forecast_config(0.001, 0.999)

    def verdict(invented: str) -> str:
        answers = [(invented, 0.5), (supplied[0], 0.5)]
        return "\n".join(
            multiple_choice_output_problems(_mc_response(answers), config, options=tuple(supplied))
        )

    first = verdict("AAAAAAAAAA")
    second = verdict("ZZZZZZZZZZ")
    assert first == second
    # Vacuity guard: neither invented label was supplied, so there must *be* a problem.
    assert first


@given(
    st.lists(st.sampled_from(_MC_LABELS), min_size=3, max_size=4, unique=True),
    st.integers(min_value=1, max_value=3),
)
def test_an_option_problem_never_varies_with_how_many_labels_failed(
    supplied: list[str], count: int
) -> None:
    """The count is a channel too (M1-302's rule), and it is the one an aggregating
    checker exists to close: one bad label and three must read identically.

    The reply names one supplied option and ``count`` invented ones, sharing 1 equally, so
    only *unknown* and *missing* are ever in play -- the other three rules stay silent for
    every count and cannot mask the invariance being asserted.
    """
    config = _forecast_config(0.001, 0.999)
    share = 1.0 / (count + 1)
    answers = [(supplied[0], share)] + [(f"invented {index}", share) for index in range(count)]
    problems = multiple_choice_output_problems(
        _mc_response(answers), config, options=tuple(supplied)
    )
    assert problems == [_MC_UNKNOWN, _MC_MISSING]


# --- 7. the composed output-validation entry point (M1-506) -----------------------
#
# The members are fuzzed above, one section each. What is new here is the *composition*,
# and it has its own failure modes: a layer that stops being reached, an order that is not
# stable, a raise that carries half the account, and a verdict that changes across the
# ledger boundary M1-507 will validate over.


_MC_OPTIONS = ("Option A", "Option B", "Option C")

# One answer list per rule the multiple-choice layer states, plus one that satisfies all
# of them. Sampled rather than freely generated for M1-306's reason (LESSONS #9): a
# strategy that drew arbitrary label/probability pairs would produce an *unknown* option
# on nearly every draw and would essentially never produce a duplicate or a valid
# distribution, so the properties below would be about one cell of the table.
_MC_ANSWERS: list[list[tuple[str, float]]] = [
    # Exactly right: every supplied option once, summing to 1, inside the widest bounds.
    [("Option A", 0.5), ("Option B", 0.3), ("Option C", 0.2)],
    # Unknown, and therefore missing as well.
    [("Option A", 0.5), ("Option D", 0.5)],
    # Missing alone: the other two are named and the vector is still a distribution.
    [("Option A", 0.5), ("Option B", 0.5)],
    # Duplicate alone: all three supplied labels appear, one of them twice, summing to 1.
    [("Option A", 0.25), ("Option B", 0.5), ("Option C", 0.15), ("Option A", 0.1)],
    # Sum alone.
    [("Option A", 0.5), ("Option B", 0.3), ("Option C", 0.5)],
    # Bounds alone under the committed pair: 0.0 is schema-valid and below min_probability.
    [("Option A", 1.0), ("Option B", 0.0), ("Option C", 0.0)],
]


def _as_multiple_choice(
    response: BinaryForecastResponse, answers: list[tuple[str, float]]
) -> MultipleChoiceForecastResponse:
    """The same attribution case, retyped as a multiple-choice reply.

    Built from the binary draw rather than from a second strategy so the attribution layer
    is drawn identically for both types -- the composition properties are about the
    *composition*, and two strategies would let the two halves drift apart.
    """
    payload = response.model_dump(mode="json")
    payload["question_type"] = "multiple_choice"
    # The prompt's own rule, which ``schema.py`` enforces on this response type.
    payload["model_prior"] = None
    payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    payload["final_prediction"] = {
        "options": [{"option": option, "probability": p} for option, p in answers]
    }
    reloaded = validate_forecast_response(payload, MultipleChoiceForecastResponse)
    assert isinstance(reloaded, MultipleChoiceForecastResponse)
    return reloaded


_ComposedCase = tuple[
    ForecastResponse, int, tuple[str, ...], ForecastConfig, tuple[str, ...] | None
]


@st.composite
def composed_cases(draw: st.DrawFn) -> _ComposedCase:
    """An attribution case, the bounds the type-specific layer is checked against, and the
    question's option list.

    Both layers must be *reachable* from one strategy or the composition properties are
    vacuous -- the defect class this project has paid for more than any other. The bounds
    are drawn wide and narrow so the type-specific layer is silent on some draws and biting
    on others, and ``test_the_composition_is_reached_on_both_sides`` is the event-tagged
    proof that both happen.

    **M1-404 widened this to draw both registered types.** Until this row the strategy drew
    only ``BinaryForecastResponse``, so every composition property was a statement about
    the one type whose entry was not ``None`` -- and the multiple-choice entry could have
    been registered wrong without a single one of them failing. The fifth element is the
    option list the entry point now pairs with the response type.
    """
    response, question_id, supplied = draw(attribution_cases())
    low, high = draw(
        st.sampled_from(
            [
                # The committed bounds, and the widest ``ForecastConfig`` permits --
                # it refuses anything outside [0.001, 0.999] at load, so 0.0/1.0 is not
                # a config an operator can produce.
                (0.001, 0.999),
                (0.002, 0.998),
                # Narrow enough to exclude the valid payload's probability, so the binary
                # layer contributes a problem to a response the attribution layer may well
                # be happy with.
                (0.9, 0.95),
                (0.001, 0.002),
            ]
        )
    )
    config = _forecast_config(low, high)
    if draw(st.booleans()):
        return response, question_id, supplied, config, None
    answers = draw(st.sampled_from(_MC_ANSWERS))
    return _as_multiple_choice(response, answers), question_id, supplied, config, _MC_OPTIONS


def _composed(case: _ComposedCase) -> list[str]:
    response, question_id, supplied, config, options = case
    return output_problems(
        response, config, question_id=question_id, source_ids=supplied, options=options
    )


@given(composed_cases())
def test_the_composition_is_reached_on_both_sides(
    case: _ComposedCase,
) -> None:
    """The anti-vacuity guard for every property in this section.

    A composition property that only ever draws responses the type-specific layer is
    silent about proves nothing about the composition. This tags the four cells and the
    others below are only meaningful because this one shows they are populated.
    """
    response, question_id, supplied, config, options = case
    attribution_half = attribution_problems(response, question_id=question_id, source_ids=supplied)
    checker = _TYPE_CHECKERS[response.question_type]
    assert checker is not None, response.question_type
    type_half = checker(response, config, options=options)
    event(f"question type: {response.question_type}")
    event(f"attribution bites: {bool(attribution_half)}")
    event(f"type-specific bites: {bool(type_half)}")


@given(composed_cases())
def test_the_composition_is_exactly_its_two_layers_in_order(
    case: _ComposedCase,
) -> None:
    """Equality with the concatenation, not a subset or a set.

    The entry point exists so a caller need not know the member list; that is only true if
    it returns *everything* the members do. A subset assertion would be green for an entry
    point that had quietly dropped a layer -- which is the exact regression M1-404 and
    M1-405 could introduce when they register their checkers.
    """
    response, question_id, supplied, config, options = case
    checker = _TYPE_CHECKERS[response.question_type]
    expected = attribution_problems(response, question_id=question_id, source_ids=supplied)
    if checker is not None:
        expected = expected + checker(response, config, options=options)
    assert _composed(case) == expected


@given(composed_cases())
def test_the_pair_agrees_on_every_draw(
    case: _ComposedCase,
) -> None:
    """``validate_output`` raises iff ``output_problems`` is non-empty, and carries the
    whole list.

    The two halves of the pair are a single rule stated twice; a caller that switched
    between them and got a different verdict would have no way to tell which was right.
    """
    response, question_id, supplied, config, options = case
    problems = _composed(case)
    try:
        returned = validate_output(
            response, config, question_id=question_id, source_ids=supplied, options=options
        )
    except ForecastOutputError as exc:
        assert problems != []
        assert exc.problems == problems
    else:
        assert problems == []
        # Nothing is clamped, repaired or renumbered: the same object comes back.
        assert returned is response


@given(composed_cases())
def test_the_composed_verdict_is_stable_across_the_storage_boundary(
    case: _ComposedCase,
) -> None:
    """M1-305's rule, applied to the composition rather than to one member.

    M1-507 will run this entry point over a record on its way into the ledger, so its
    answer has to be a function of the *persisted* form. A composition whose verdict
    changed across ``model_dump(mode="json")`` would pass every test that never went
    through the ledger and refuse a record the generating path accepted.
    """
    response, question_id, supplied, config, options = case
    persisted = json.dumps(
        response.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, allow_nan=False
    )
    # Reloaded through the model the response's own type selects, not through a constant:
    # this strategy draws two types now, and re-validating a multiple-choice payload as a
    # binary response would fail here for a reason that has nothing to do with the claim.
    reloaded = validate_forecast_response(
        json.loads(persisted), response_model_for(response.question_type)
    )
    assert output_problems(
        reloaded, config, question_id=question_id, source_ids=supplied, options=options
    ) == _composed(case)


@given(composed_cases(), HOSTILE_SOURCE_IDS, st.one_of(HOSTILE_TEXT, st.none(), st.floats()))
def test_the_entry_point_never_raises_outside_the_packages_error_type(
    case: _ComposedCase,
    source_ids: Any,
    question_id: Any,
) -> None:
    """Every malformed shape must arrive as a ``ForecastSchemaError``.

    Deliberately asserted against the *parent* type rather than ``ForecastOutputError``
    alone: the composition's contract is that a caller writing one ``except`` clause
    catches every route through it, including the member modules' own caller-mistake
    errors. A raw ``TypeError`` or ``AttributeError`` escaping is the defect this project
    has taken as a review finding twice.
    """
    response, _, _, config, options = case
    for arguments in (
        {"question_id": _VALID_QUESTION_ID, "source_ids": source_ids},
        {"question_id": question_id, "source_ids": ["src-001"]},
        {"question_id": question_id, "source_ids": source_ids},
    ):
        try:
            problems = output_problems(response, config, options=options, **arguments)
        except ForecastSchemaError:
            continue
        assert all(isinstance(problem, str) for problem in problems)


@given(
    st.one_of(HOSTILE_TEXT, st.none(), st.integers(), st.lists(HOSTILE_TEXT, max_size=2)),
    BOUNDS,
)
def test_the_entry_point_refuses_anything_that_is_not_a_response(
    value: Any, bounds: list[float]
) -> None:
    low, high = bounds
    config = _forecast_config(low, high)
    for entry_point in (output_problems, validate_output):
        try:
            entry_point(
                value,
                config,
                question_id=_VALID_QUESTION_ID,
                source_ids=["src-001"],
                options=None,
            )
        except ForecastSchemaError:
            continue
        raise AssertionError("a non-response was accepted")


@given(composed_cases())
def test_every_composed_problem_is_reported_at_a_location_the_schema_authored(
    case: _ComposedCase,
) -> None:
    """A problem at a location the schema never declared is a location the model invented.

    The members each assert this for themselves; the composition is asserted separately
    because concatenating two safe lists is not the only thing an entry point could do --
    one that wrapped, summarised or re-prefixed its members' strings would satisfy both
    members and still produce a path a reader of a stored failure cannot resolve.
    """
    response = case[0]
    model = response_model_for(response.question_type)
    for problem in _composed(case):
        location, separator, _ = problem.partition(": ")
        assert separator == ": ", problem
        assert _attribution_location_resolves(location, model), problem


@given(
    st.lists(st.sampled_from(_SUPPLIABLE), min_size=1, max_size=3, unique=True),
    st.sampled_from(["base_rate", "evidence_adjustments", "load_bearing_facts"]),
    BOUNDS,
)
def test_a_composed_problem_never_varies_with_the_cited_id_that_failed(
    supplied: list[str], field: str, bounds: list[float]
) -> None:
    """The leak property for the composition, written as **invariance** rather than as a
    substring check.

    The shape is M1-403's and M1-501's, and the reason is restated at
    ``test_an_attribution_problem_never_varies_with_the_cited_id_that_failed``: a short
    marker is a substring of text these modules render for unrelated reasons. Drafting
    this one as ``hostile not in message`` failed on a drawn ``"_"``, which appears in
    ``source_ids`` and ``research_documents`` -- the exact trap that comment describes,
    walked into and then out of.

    Two different invented citations, in the same place, must produce byte-identical
    composed output. An entry point echoing any part of the id -- in a member's message or
    in wrapping of its own -- could not satisfy that.
    """
    low, high = bounds
    config = _forecast_config(low, high)

    def verdict(invented: str) -> str:
        response = _attribution_response(
            base_ids=[invented] if field == "base_rate" else list(supplied[:1]),
            adjustment_ids=[[invented] if field == "evidence_adjustments" else list(supplied[:1])],
            fact_ids=[[invented] if field == "load_bearing_facts" else list(supplied[:1])],
            failure_modes=["a failure mode"],
            question_id=_VALID_QUESTION_ID,
        )
        return "\n".join(
            output_problems(
                response,
                config,
                question_id=_VALID_QUESTION_ID,
                source_ids=supplied,
                options=None,
            )
        )

    first = verdict("AAAAAAAAAA")
    second = verdict("ZZZZZZZZZZ")
    assert first == second
    # Vacuity guard: neither invented id was supplied, so there must *be* a problem.
    assert first


@given(composed_cases())
def test_an_unregistered_question_type_is_refused_rather_than_passed(
    case: _ComposedCase,
) -> None:
    """A type the table does not cover must not validate as though it had no rules.

    Unreachable through the schema, and asserted anyway: this is the branch that decides
    whether a fourth question type added to config fails loudly or forecasts silently
    unchecked, and a ``.get()`` default would have made it the latter.
    """
    response, question_id, supplied, config, options = case
    mutated = response.model_copy()
    object.__setattr__(mutated, "question_type", "date")
    try:
        output_problems(
            mutated, config, question_id=question_id, source_ids=supplied, options=options
        )
    except ForecastOutputError as exc:
        assert exc.problems == [
            "question_type: must be one of binary, multiple_choice, numeric "
            "(offending input withheld)"
        ]
    else:
        raise AssertionError("an unregistered question type was validated")
