"""Property tests for the shared ValidationError sanitizer (M0-008).

A purpose-built schema that has every channel the sanitizer has to close: ``extra="forbid"``
at every level, a nested model, a list of models, a ``dict[str, X]`` and a ``dict[int, X]``
whose keys are input, a discriminated union (whose ``union_tag_invalid`` msg quotes the
tag, the M1-602 B1 leak), and an authored validator. A marker is planted in values and in
keys, at the top level and at depth, and must never reach a rendered problem.

``event()`` records which channels a draw actually opened, measured from pydantic's own
error list rather than from what the strategy meant to do, so a run that never reached a
channel shows it.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from hypothesis import event, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from whiskeyjack_bot.validation_errors import authored_error, sanitized_problems

MARKER = "WJLEAKMARKER"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Leaf(_Strict):
    n: int
    tag: str

    @field_validator("tag")
    @classmethod
    def _tag_is_ok(cls, value: str) -> str:
        if value != "ok":
            raise authored_error("prop_tag_not_ok", "tag must be ok")
        return value


class ArmA(_Strict):
    kind: Literal["a"]
    leaf: Leaf


class ArmB(_Strict):
    kind: Literal["b"]
    leaves: list[Leaf]


class Root(_Strict):
    name: str
    child: Annotated[ArmA | ArmB, Field(discriminator="kind")]
    items: list[Leaf]
    table: dict[str, Leaf]
    numbered: dict[int, Leaf]


def _leaf() -> dict[str, Any]:
    return {"n": 1, "tag": "ok"}


def _baseline() -> dict[str, Any]:
    return {
        "name": "root",
        "child": {"kind": "a", "leaf": _leaf()},
        "items": [_leaf(), _leaf()],
        "table": {"k": _leaf()},
        "numbered": {1: _leaf()},
    }


def _containers(payload: dict[str, Any]) -> list[tuple[int, dict[Any, Any]]]:
    """Every mapping in the payload with its depth, including the input-keyed tables."""
    found: list[tuple[int, dict[Any, Any]]] = []
    stack: list[tuple[int, Any]] = [(0, payload)]
    while stack:
        depth, value = stack.pop()
        if isinstance(value, dict):
            found.append((depth, value))
            stack.extend((depth + 1, child) for child in value.values())
        elif isinstance(value, list):
            stack.extend((depth + 1, child) for child in value)
    return found


_STR_MARKERS = st.text(max_size=6).map(lambda tail: MARKER + tail)
# Large enough that no index, count or type slug can contain one by accident.
_INT_MARKERS = st.integers(min_value=10**8, max_value=10**12)

_MUTATIONS = st.sampled_from(
    [
        "extra_str_key",
        "extra_int_key",
        "value",
        "tag_value",
        "union_tag",
        "table_key",
        "int_table_key",
    ]
)


@st.composite
def _planted(draw: st.DrawFn) -> tuple[dict[str, Any], list[str]]:
    payload = _baseline()
    markers: list[str] = []
    for _ in range(draw(st.integers(min_value=1, max_value=4))):
        kind = draw(_MUTATIONS)
        containers = _containers(payload)
        depth, target = draw(st.sampled_from(containers))
        if kind == "extra_str_key":
            key = draw(_STR_MARKERS)
            target[key] = draw(st.one_of(st.integers(), _STR_MARKERS))
            markers.append(key)
        elif kind == "extra_int_key":
            number = draw(_INT_MARKERS)
            target[number] = 1
            markers.append(str(number))
        elif kind == "value":
            # A marker where a model expects an int, a list or a nested object.
            field = draw(st.sampled_from(sorted(str(k) for k in target) or ["n"]))
            value = draw(_STR_MARKERS)
            target[field] = value
            markers.append(value)
        elif kind == "tag_value":
            leaf = payload["items"][0]
            leaf["tag"] = draw(_STR_MARKERS)
            markers.append(leaf["tag"])
        elif kind == "union_tag":
            tag = draw(_STR_MARKERS)
            payload["child"] = {"kind": tag, "leaf": _leaf()}
            markers.append(tag)
        elif kind == "table_key":
            key = draw(_STR_MARKERS)
            payload["table"][key] = {"n": draw(_STR_MARKERS), "tag": "ok"}
            markers.append(key)
        else:
            number = draw(_INT_MARKERS)
            payload["numbered"][number] = {"n": "x", "tag": "ok"}
            markers.append(str(number))
        event(f"planted: {kind} at depth {'0' if depth == 0 else '>=1'}")
    return payload, markers


def _record_channels(exc: ValidationError, markers: list[str]) -> None:
    """Which channels pydantic's own output actually carried a marker through."""
    for err in exc.errors(include_input=False, include_url=False):
        if err["type"] == "union_tag_invalid":
            event("reached: union_tag_invalid (msg quotes the tag)")
        if err["type"] == "extra_forbidden":
            event(f"reached: extra_forbidden at depth {'0' if len(err['loc']) == 1 else '>=1'}")
        if any(isinstance(part, int) and str(part) in markers for part in err["loc"]):
            event("reached: an int input key in loc")
        if any(isinstance(part, str) and MARKER in part for part in err["loc"]):
            event("reached: a str input key in loc")
        if MARKER in err["msg"]:
            event("reached: marker in pydantic msg")
        if err["type"] == "prop_tag_not_ok":
            event("reached: authored error")


@given(_planted())
@settings(max_examples=300, deadline=None)
def test_no_planted_marker_reaches_any_rendered_problem(
    case: tuple[dict[str, Any], list[str]],
) -> None:
    payload, markers = case
    try:
        Root.model_validate(payload)
    except ValidationError as exc:
        _record_channels(exc, markers)
        problems = sanitized_problems(exc, Root)
    else:
        # A mutation can land on a field that tolerates it (a marker string in `name`).
        event("validated")
        return
    assert problems, "a failed validation must render at least one problem"
    rendered = "\n".join(problems)
    for marker in markers:
        assert marker not in rendered, (marker, rendered)
    # Replay-stable: the rendering is plain text that survives the persisted form.
    assert json.loads(json.dumps(problems, ensure_ascii=True)) == problems


@given(_planted())
@settings(max_examples=100, deadline=None)
def test_the_rendering_is_deterministic(case: tuple[dict[str, Any], list[str]]) -> None:
    payload, _ = case
    try:
        Root.model_validate(payload)
    except ValidationError as first:
        try:
            Root.model_validate(payload)
        except ValidationError as second:
            assert sanitized_problems(first, Root) == sanitized_problems(second, Root)


def test_an_authored_sentence_and_the_authored_path_survive() -> None:
    """The companion: withholding is not blanket. A field path the schema declared, a list
    index after a list field, and an authored sentence all come through."""
    payload = _baseline()
    payload["items"][1]["tag"] = "not ok"
    payload["child"] = {"kind": "b", "leaves": [{"n": "x", "tag": "ok"}]}
    try:
        Root.model_validate(payload)
    except ValidationError as exc:
        problems = sanitized_problems(exc, Root)
    else:
        raise AssertionError("expected a validation failure")
    assert "items.1.tag: tag must be ok [prop_tag_not_ok]" in problems
    assert "child.<withheld>.leaves.0.n: int_parsing" in problems
