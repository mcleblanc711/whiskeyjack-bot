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
from pathlib import Path
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from strategies import HOSTILE_TEXT, research_documents, round_trip

from whiskeyjack_bot.forecast.inputs import (
    ForecastInputError,
    build_model_input,
    render_model_input,
)
from whiskeyjack_bot.forecast.schema import (
    BinaryForecastResponse,
    ForecastSchemaError,
    response_model_for,
    validate_forecast_response,
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
