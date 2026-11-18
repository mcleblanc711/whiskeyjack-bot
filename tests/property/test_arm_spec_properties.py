"""Property tests for the arm spec and its hash (M5-806, AC2).

The CLAUDE.md pre-review fuzz pass over the pure surface ``experiment/spec.py`` adds:

1. **Never raises outside ``ExperimentError``**, on arbitrary JSON values and on arbitrary file
   bytes.
2. **Replay-stable.** A valid spec's canonical JSON parses back to a spec with the same hash, and
   the hash does not depend on the input file's key order, whitespace or number spelling.
3. **One arm, one spelling.** A set-valued field written unsorted or with a repeat is refused, so
   two files that mean the same arm cannot carry two hashes.
4. **No value leaks** into a refusal: a canary planted in any string field, or as an unknown key,
   never reaches the message.

Reach is measured: ``test_the_strategy_reaches_every_transform_kind`` fails if a kind is starved.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from whiskeyjack_bot.experiment.errors import ExperimentError
from whiskeyjack_bot.experiment.spec import (
    arm_sha256,
    canonical_spec_json,
    load_arm_spec,
    parse_arm_spec,
)

CANARY = "zqx-canary-8806"
ASTRA = "openrouter/openai/gpt-6-astra"
KINDS = (
    "identity",
    "no_research",
    "drop_provider",
    "mask_publisher",
    "shuffle_order",
    "random_subset",
    "drop_cluster",
    "drop_publisher_class",
    "collapse_syndication",
)

labels = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._-]{0,20}", fullmatch=True)
seeds = st.integers(min_value=0, max_value=2**63 - 1)


def _sorted_unique(values: list[Any]) -> list[Any]:
    return sorted(set(values))


@st.composite
def transforms(draw: st.DrawFn) -> dict[str, Any]:
    kind = draw(st.sampled_from(KINDS))
    spec: dict[str, Any] = {"kind": kind}
    if kind == "drop_provider":
        providers = draw(
            st.lists(st.sampled_from(["asknews", "exa", "structured", "xai_x_search"]), min_size=1)
        )
        spec["providers"] = _sorted_unique(providers)
    elif kind == "shuffle_order":
        spec["seed"] = draw(seeds)
    elif kind == "random_subset":
        spec["seed"] = draw(seeds)
        spec["inclusion_probability"] = draw(
            st.floats(min_value=0.001, max_value=0.999, allow_nan=False)
        )
        spec["unit"] = draw(st.sampled_from(["document", "cluster"]))
        if spec["unit"] == "cluster":
            spec["labeller"] = draw(labels)
    elif kind in ("drop_cluster", "collapse_syndication"):
        spec["labeller"] = draw(labels)
    elif kind == "drop_publisher_class":
        spec["labeller"] = draw(labels)
        spec["classes"] = _sorted_unique(draw(st.lists(labels, min_size=1, max_size=4)))
    return spec


@st.composite
def specs(draw: st.DrawFn) -> dict[str, Any]:
    selection: dict[str, Any] = {}
    if draw(st.booleans()):
        selection["question_types"] = _sorted_unique(
            draw(
                st.lists(
                    st.sampled_from(["binary", "multiple_choice", "numeric", "discrete"]),
                    min_size=1,
                )
            )
        )
    if draw(st.booleans()):
        selection["tournaments"] = _sorted_unique(draw(st.lists(labels, min_size=1, max_size=3)))
    if draw(st.booleans()):
        selection["record_ids"] = _sorted_unique(draw(st.lists(labels, min_size=1, max_size=3)))
    if draw(st.booleans()):
        selection["limit"] = draw(st.integers(min_value=1, max_value=10_000))
    return {
        "spec_schema_version": "1.0.0",
        "name": draw(labels),
        "model": ASTRA,
        "reasoning_effort": draw(st.sampled_from(["low", "medium", "high"])),
        "prompt": {
            "path": draw(st.from_regex(r"[a-z][a-z/_-]{0,30}\.md", fullmatch=True)),
            "version": f"{draw(st.integers(0, 99))}.{draw(st.integers(0, 99))}."
            f"{draw(st.integers(0, 99))}",
            "sha256": draw(st.from_regex(r"[0-9a-f]{64}", fullmatch=True)),
        },
        "transform": draw(transforms()),
        "samples": draw(st.integers(min_value=1, max_value=10)),
        "selection": selection,
    }


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=12), children, max_size=5)
    ),
    max_leaves=25,
)


@settings(deadline=None)
@given(specs())
def test_a_valid_spec_hash_is_stable_across_the_persisted_form(data: dict[str, Any]) -> None:
    spec = parse_arm_spec(data)
    persisted = canonical_spec_json(spec)
    again = parse_arm_spec(json.loads(persisted))
    assert canonical_spec_json(again) == persisted
    assert arm_sha256(again) == arm_sha256(spec)


@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(specs(), st.randoms(use_true_random=False), st.integers(min_value=0, max_value=4))
def test_the_hash_ignores_key_order_whitespace_and_number_spelling(
    tmp_path: Path, data: dict[str, Any], rng: Any, indent: int
) -> None:
    def shuffled(value: Any) -> Any:
        if isinstance(value, dict):
            items = list(value.items())
            rng.shuffle(items)
            return {key: shuffled(item) for key, item in items}
        if isinstance(value, list):
            return [shuffled(item) for item in value]
        return value

    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text(json.dumps(data), encoding="utf-8")
    text = json.dumps(shuffled(data), indent=indent or None)
    if "inclusion_probability" in data["transform"]:
        value = data["transform"]["inclusion_probability"]
        text = text.replace(
            json.dumps(value), f"{value!r}0" if "e" not in repr(value) else repr(value)
        )
    second.write_text(text, encoding="utf-8")
    assert load_arm_spec(first).sha256 == load_arm_spec(second).sha256


@settings(deadline=None)
@given(specs(), st.data())
def test_an_unsorted_or_repeated_set_is_refused(data: dict[str, Any], draw: st.DataObject) -> None:
    candidates = [
        (["transform", "providers"], data["transform"].get("providers")),
        (["transform", "classes"], data["transform"].get("classes")),
        (["selection", "question_types"], data["selection"].get("question_types")),
        (["selection", "tournaments"], data["selection"].get("tournaments")),
        (["selection", "record_ids"], data["selection"].get("record_ids")),
    ]
    present = [(path, values) for path, values in candidates if values]
    if not present:
        return
    path, values = draw.draw(st.sampled_from(present))
    broken = [*values, values[0]] if len(values) == 1 else list(reversed(values))
    edited = json.loads(json.dumps(data))
    edited[path[0]][path[1]] = broken
    try:
        parse_arm_spec(edited)
    except ExperimentError as exc:
        assert "sorted and without repeats" in str(exc)
    else:
        raise AssertionError("a non-canonical set was accepted")


@settings(deadline=None)
@given(json_values)
def test_arbitrary_json_is_refused_only_as_an_experiment_error(value: Any) -> None:
    try:
        parse_arm_spec(value)
    except ExperimentError:
        pass


@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.binary(max_size=200))
def test_arbitrary_file_bytes_are_refused_only_as_an_experiment_error(
    tmp_path: Path, raw: bytes
) -> None:
    path = tmp_path / "spec.json"
    path.write_bytes(raw)
    try:
        load_arm_spec(path)
    except ExperimentError as exc:
        assert raw.decode("latin-1") not in str(exc) or len(raw) < 3


def _string_paths(value: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    if isinstance(value, dict):
        return [p for key, item in value.items() for p in _string_paths(item, (*prefix, key))]
    if isinstance(value, list):
        return [
            p for index, item in enumerate(value) for p in _string_paths(item, (*prefix, index))
        ]
    return [prefix] if isinstance(value, (str, int, float)) and prefix else []


@settings(deadline=None)
@given(specs(), st.data())
def test_a_planted_canary_never_reaches_a_refusal(
    data: dict[str, Any], draw: st.DataObject
) -> None:
    edited = json.loads(json.dumps(data))
    if draw.draw(st.booleans()):
        target = edited
        for part in draw.draw(st.sampled_from([("selection",), ("transform",), ("prompt",), ()])):
            target = target[part]
        target[CANARY] = CANARY
    else:
        path = draw.draw(st.sampled_from(_string_paths(edited)))
        target = edited
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = CANARY
    try:
        parse_arm_spec(edited)
    except ExperimentError as exc:
        assert CANARY not in str(exc)
        assert exc.__cause__ is None


def test_the_strategy_reaches_every_transform_kind() -> None:
    seen: Counter[str] = Counter()

    @settings(max_examples=300, deadline=None, database=None)
    @given(specs())
    def record(data: dict[str, Any]) -> None:
        seen[data["transform"]["kind"]] += 1

    record()
    assert set(seen) == set(KINDS), seen


@settings(deadline=None)
@given(specs(), st.sampled_from(["name", "effort", "samples", "path", "version", "selection"]))
def test_every_field_of_the_spec_is_inside_the_hash(data: dict[str, Any], field: str) -> None:
    """The whole spec is hashed: the label and the prompt path included (a stated decision)."""
    edited = json.loads(json.dumps(data))
    if field == "name":
        edited["name"] = data["name"] + "x" if len(data["name"]) < 64 else "y"
    elif field == "effort":
        edited["reasoning_effort"] = {"low": "medium", "medium": "high", "high": "low"}[
            data["reasoning_effort"]
        ]
    elif field == "samples":
        edited["samples"] = data["samples"] % 10 + 1
    elif field == "path":
        edited["prompt"]["path"] = data["prompt"]["path"] + "x"
    elif field == "version":
        major = int(data["prompt"]["version"].split(".")[0])
        edited["prompt"]["version"] = f"{major + 1}.0.0"
    else:
        edited["selection"] = {**data["selection"], "limit": data["selection"].get("limit", 0) + 1}
    assert arm_sha256(parse_arm_spec(edited)) != arm_sha256(parse_arm_spec(data))


# Weighted toward the shapes the property is about. Plain `st.text()` reaches a NUL so rarely
# that the property passed against the code it was written to catch (measured on ae05693).
_PATH_PIECES = st.sampled_from(["a", "b.md", "/", "..", ".", " ", "\x00", "\ud800", "é", "\\"])
awkward_paths = st.lists(_PATH_PIECES | st.text(max_size=4), min_size=1, max_size=12).map("".join)


def test_the_path_strategy_reaches_nul_and_surrogates() -> None:
    seen: Counter[str] = Counter()

    @settings(max_examples=200, deadline=None, database=None)
    @given(awkward_paths)
    def record(value: str) -> None:
        seen["nul"] += "\x00" in value
        seen["surrogate"] += "\ud800" in value

    record()
    assert seen["nul"] >= 5 and seen["surrogate"] >= 5, seen


@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(specs(), awkward_paths.filter(bool))
def test_any_prompt_path_is_resolved_or_refused_as_an_experiment_error(
    tmp_path: Path, data: dict[str, Any], prompt_path: str
) -> None:
    """Review round 1, B1: the path is text the filesystem may not be able to take.

    The valid-spec strategy draws ordinary file names; this one draws any text -- NUL, lone
    surrogates, separators, ``..`` -- and requires a loaded spec's prompt path to be a
    resolved ``Path`` or the load to refuse as the module's own error.
    """
    edited = json.loads(json.dumps(data))
    edited["prompt"]["path"] = prompt_path
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(edited), encoding="utf-8")
    try:
        loaded = load_arm_spec(path)
    except ExperimentError as exc:
        assert exc.__cause__ is None
    else:
        assert loaded.prompt_path.is_absolute()
