"""Properties for the generation failure path (M1-350, M1-324).

M1-350: whatever JSON body OpenRouter returns, a non-finite literal anywhere in it reaches the
caller as ``ModelOutcomeUnknown`` -- never a raw ``ValueError`` -- with a static message, the
reservation held and no ``model_completed`` written. The strategy's reach is measured: it
is each of ``usage.cost``, deeper in ``usage``, and outside ``usage``, one run apiece.

M1-324: whatever value a malformed reply carries, the problems ``_parse`` returns -- which are
exactly what the no-forecast log line renders -- never contain it. Reach is measured too: the
failure branch, the schema half and the post-schema checks must each be hit.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import uuid
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

import httpx
import pytest
from hypothesis import event, given, settings, strategies as st

from whiskeyjack_bot.config import validate_config_data
from whiskeyjack_bot.forecast.parse import _parse
from whiskeyjack_bot.forecast.priced import PricedClient
from whiskeyjack_bot.forecast.schema import (
    ForecastSchemaError,
    response_model_for,
    validate_forecast_response,
)
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.tournament_state import (
    Budget,
    ModelOutcomeUnknown,
    budget_context,
    spending,
)


def _unit_helpers() -> ModuleType:
    """``tests/unit/test_pipeline_live.py``, loaded by path under its bare name.

    ``from tests.unit ...`` resolves in some property modules only because a third-party
    import earlier in them appends the working directory to ``sys.path``; this module makes
    no such import, and must not depend on one. The bare name is the one the unit suite
    imports it under, so a session that already loaded it reuses that module.
    """
    existing = sys.modules.get("test_pipeline_live")
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parents[1] / "unit" / "test_pipeline_live.py"
    spec = importlib.util.spec_from_file_location("test_pipeline_live", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_pipeline_live"] = module
    spec.loader.exec_module(module)
    return module


_helpers = _unit_helpers()
BINARY: int = _helpers.BINARY
base_config: Any = _helpers.config
questions: Any = _helpers.questions
reply_for: Any = _helpers.reply_for

SCOPE = "42:32977"
_MARKER = "__non_finite_literal__"
_LITERALS = ("NaN", "Infinity", "-Infinity")

_leaves = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(10**6), 10**6),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=8),
)
_trees = st.recursive(
    _leaves,
    lambda inner: st.one_of(
        st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=5), inner, max_size=3)
    ),
    max_leaves=8,
)


def _splice(tree: Any, path: list[str]) -> Any:
    """``tree`` with the marker at ``path`` (dict keys), creating dicts on the way."""
    if not path:
        return _MARKER
    base = dict(tree) if isinstance(tree, dict) else {}
    base[path[0]] = _splice(base.get(path[0]), path[1:])
    return base


_PLACEMENTS = ("usage.cost", "usage.nested", "outside")


@st.composite
def _bodies(draw: st.DrawFn, where: str) -> tuple[bytes, str]:
    usage = draw(st.dictionaries(st.text(max_size=5), _trees, max_size=3))
    extra = draw(_trees)
    body: dict[str, Any] = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": usage,
        "extra": extra,
    }
    if where == "usage.cost":
        body["usage"] = dict(usage, cost=_MARKER)
    elif where == "usage.nested":
        depth = draw(st.lists(st.text(min_size=1, max_size=4), min_size=1, max_size=3))
        body["usage"] = _splice(usage, ["cost_details", *depth])
    else:
        body["extra"] = _splice(extra, [draw(st.text(min_size=1, max_size=4))])
    literal = draw(st.sampled_from(_LITERALS))
    text = json.dumps(body).replace(json.dumps(_MARKER), literal, 1)
    return text.encode(), where


@pytest.mark.parametrize("where", _PLACEMENTS)
def test_a_non_finite_number_anywhere_is_an_unknown_outcome(tmp_path: Path, where: str) -> None:
    """One run per placement: ``sampled_from`` inside one run skews toward its first member
    (measured: 85/9/26 of 120), so reach is guaranteed by construction instead."""
    config = base_config.__wrapped__(tmp_path)
    data = config.model_dump(mode="json")
    data["model"].update(name="openrouter/openai/gpt-6-astra", temperature=None)
    config = validate_config_data(data)
    config.storage.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    current: dict[str, bytes] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=current["body"])

    original = httpx.AsyncClient
    reached: Counter[str] = Counter()

    @given(drawn=_bodies(where))
    @settings(max_examples=40, deadline=None)
    def check(drawn: tuple[bytes, str]) -> None:
        body, placed = drawn
        assert placed == where
        reached[placed] += 1
        event(placed)
        current["body"] = body
        # A fresh prompt per example, so no example meets another's durable guard.
        prompt = [{"role": "user", "content": uuid.uuid4().hex}]
        budget = Budget(conn, config.storage.artifact_root, SCOPE, 10**12)
        before = spending(conn, SCOPE)[1]
        with budget_context(budget):
            try:
                asyncio.run(PricedClient(config).invoke(prompt))
            except ModelOutcomeUnknown as exc:
                message = str(exc)
            else:
                raise AssertionError("a non-finite body was accepted")
        assert message == "priced model request failed or was unavailable at the authorized price"
        for literal in (*_LITERALS, "nan", "inf"):
            assert literal not in message
        assert spending(conn, SCOPE)[1] > before, "the reservation stays held"
        completed = conn.execute(
            "SELECT count(*) FROM tournament_events WHERE kind='model_completed'"
        ).fetchone()[0]
        assert completed == 0

    with (
        mock.patch.object(
            httpx,
            "AsyncClient",
            lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
        ),
        mock.patch.dict("os.environ", {config.model.api_key_env: "test-secret"}),
    ):
        try:
            check()
        finally:
            conn.close()
    assert reached[where] >= 20, reached


# --- M1-324: a value in a malformed reply never reaches the logged problems -------------

SENTINEL = "SENTINEL-9b2e"
_QUESTION = questions()[BINARY]
_REPLY = json.loads(reply_for(_QUESTION))
_MODEL = response_model_for(_QUESTION.qtype)
_SOURCES = ("src-001", "src-002")


def _paths(value: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    if isinstance(value, dict):
        found = [prefix] if prefix else []
        for key, item in value.items():
            found.extend(_paths(item, (*prefix, key)))
        return found
    if isinstance(value, list):
        found = [prefix]
        for index, item in enumerate(value):
            found.extend(_paths(item, (*prefix, index)))
        return found
    return [prefix]


_REPLY_PATHS = [path for path in _paths(_REPLY) if path and path != ("question_type",)]
_POISON = st.sampled_from(
    [
        SENTINEL,
        SENTINEL * 300,
        [SENTINEL],
        {"value": SENTINEL},
        {SENTINEL: 1},
        f"{SENTINEL}\ud800",
        f"src-{SENTINEL}",
    ]
)


def _get(source: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        source = source[key]
    return source


def _set(target: Any, path: tuple[Any, ...], value: Any) -> None:
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def _schema_admits_text(path: tuple[Any, ...]) -> bool:
    """Whether a free-text sentinel at ``path`` still passes the schema.

    Derived rather than listed: ids, timestamps and enums refuse free text, and the point of
    the post-schema run is to reach the checks that run *after* the schema.
    """
    payload = copy.deepcopy(_REPLY)
    _set(payload, path, f"The wire said {SENTINEL}.")
    try:
        validate_forecast_response(dict(payload, question_type=_QUESTION.qtype), _MODEL)
    except ForecastSchemaError:
        return False
    return True


_STRING_PATHS = [
    path
    for path in _REPLY_PATHS
    if isinstance(_get(_REPLY, path), str) and _schema_admits_text(path)
]


def _check_reply(config: Any, payload: dict[str, Any], reached: Counter[str]) -> None:
    forecast, problems = _parse(
        json.dumps(payload),
        _MODEL,
        config.forecast,
        question=_QUESTION,
        source_ids=_SOURCES,
    )
    if forecast is not None:
        reached["accepted"] += 1
        event("accepted")
        return
    reached["refused"] += 1
    stamped = dict(payload, question_type=_QUESTION.qtype)
    try:
        validate_forecast_response(stamped, _MODEL)
        kind = "post-schema"
    except ForecastSchemaError:
        kind = "schema"
    reached[kind] += 1
    event(kind)
    assert problems, "a refusal always carries at least one problem"
    for problem in problems:
        assert SENTINEL not in problem


def test_a_reply_value_never_reaches_the_schema_problems_the_log_renders(
    tmp_path: Path,
) -> None:
    """Any poison, anywhere: mostly refused by the schema, whose messages are the risk."""
    config = base_config.__wrapped__(tmp_path)
    reached: Counter[str] = Counter()

    @given(
        paths=st.lists(st.sampled_from(_REPLY_PATHS), min_size=1, max_size=3),
        poison=_POISON,
        extra_key=st.booleans(),
    )
    @settings(max_examples=300, deadline=None)
    def check(paths: list[tuple[Any, ...]], poison: Any, extra_key: bool) -> None:
        payload = copy.deepcopy(_REPLY)
        for path in paths:
            try:
                _set(payload, path, copy.deepcopy(poison))
            except (KeyError, IndexError, TypeError):
                continue  # an earlier poison already replaced this path's parent
        if extra_key:
            payload["unexpected"] = SENTINEL
        _check_reply(config, payload, reached)

    check()
    assert reached["schema"] >= 150, reached


def test_a_reply_value_never_reaches_the_post_schema_problems_the_log_renders(
    tmp_path: Path,
) -> None:
    """A plain sentinel string in string fields, and a prediction outside the configured
    bounds: schema-valid, so the post-schema checks are the ones that refuse it, with the
    sentinel aboard. Split from the run above because there it was reached 8-20 times in
    300 -- too thin a margin for a bar in a required gate."""
    config = base_config.__wrapped__(tmp_path)
    reached: Counter[str] = Counter()

    @given(
        paths=st.lists(st.sampled_from(_STRING_PATHS), min_size=1, max_size=3),
        text=st.sampled_from([SENTINEL, f"The wire said {SENTINEL}.", f"{SENTINEL}\ud800"]),
    )
    @settings(max_examples=100, deadline=None)
    def check(paths: list[tuple[Any, ...]], text: str) -> None:
        payload = copy.deepcopy(_REPLY)
        payload["final_prediction"] = {"probability_yes": 0.9999}
        for path in paths:
            _set(payload, path, text)
        _check_reply(config, payload, reached)

    check()
    assert reached["post-schema"] >= 50, reached
