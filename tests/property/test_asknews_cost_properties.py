"""Properties of AskNews credit settlement and the failure classifier (M1-336, M1-332).

``credits_microusd`` reads ``usage.credits`` out of provider JSON (untrusted), live and again
out of the ledger. ``classify_failure`` names an arbitrary exception. The back-fill in
``correct_costs`` reads both back from the journal. The properties:

1. ``credits_microusd`` never raises on any JSON, and returns either None or exactly
   ``credits * 25000`` for an exact int count in range -- never a figure the response does
   not carry, so an unknown count is never settled as free or as anything else.
2. It is replay-stable across the persisted form (``canonical`` -> ``json.loads``).
3. ``classify_failure`` is total over generated exception classes, always lands in the
   closed Literal, is decided by the most specific (module, name) match on the MRO, maps
   anything with no SDK or httpx class in its MRO to ``transient``, and no sentinel carried
   by the exception (args, detail, code, response) reaches the run record or any log record.
4. Through a real ledger, the back-fill settles exactly the held AskNews reservations with
   one stored completion carrying a valid count, in their own scope, at the figure a replay
   re-derives; a dry run writes nothing; a second run writes nothing; actual never falls.
5. Every strategy's reach is measured, not assumed (LESSONS.md lesson 5).
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
import sqlite3
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

import httpx
import pytest
import yaml
from asknews_sdk import errors as sdk_errors
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.research.asknews import AskNewsFailure, classify_failure, retrieve_news
from whiskeyjack_bot.research.asknews_cost import credits_microusd
from whiskeyjack_bot.research.durable import begin_call
from whiskeyjack_bot.tournament_state import (
    Budget,
    append,
    budget_context,
    canonical,
    correct_costs,
    events,
    spending,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "SENTINEL-prop-7f3a"

# --- 1-2. credits_microusd ---------------------------------------------------------------

LEAVES = st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=5)
ANY_JSON = st.recursive(
    LEAVES,
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=5), children, max_size=3)
    ),
    max_leaves=8,
)
# A count: mostly in range, often at an edge, sometimes any hostile leaf.
COUNT = st.integers(0, 3).flatmap(
    lambda pick: (
        st.integers(0, 20),
        # Hypothesis shrinks toward small ints, so the range edges are drawn on purpose.
        st.integers(-3, -1) | st.integers(10**6 - 2, 10**6 + 2),
        st.sampled_from([0, 1, 5, 15, 1_000_000, 1_000_001, -1, True, False, 1.0, 1.5]),
        LEAVES,
    )[pick]
)
USAGE = st.integers(0, 2).flatmap(
    lambda pick: (
        st.fixed_dictionaries({"credits": COUNT}),
        st.fixed_dictionaries({}, optional={"credits": COUNT, "other": LEAVES}),
        ANY_JSON,
    )[pick]
)
# The live shape: every stored response so far is this, with 1 or 5 credits. Added after
# measurement: without it the valid branch was reached in 76 of 400 draws, and the ledger
# property settled in two scopes at once in 3 of 150.
LIVE = st.fixed_dictionaries(
    {"usage": st.fixed_dictionaries({"credits": st.integers(0, 20)}), "as_dicts": st.just([])}
)
# `st.one_of` does not weight by repetition (M1-348), so the pick is explicit.
EDGE = st.fixed_dictionaries({"usage": st.fixed_dictionaries({"credits": COUNT})})
RESPONSE = st.integers(0, 5).flatmap(
    lambda pick: (
        LIVE,
        LIVE,
        EDGE,
        st.fixed_dictionaries({"usage": USAGE}),
        st.fixed_dictionaries({}, optional={"usage": USAGE}),
        ANY_JSON,
    )[pick]
)


def _oracle(response: object) -> int | None:
    """The spec, written out: an exact int in [0, 10**6] at $0.025 per credit."""
    if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
        return None
    count = response["usage"].get("credits")
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return count * 25_000 if 0 <= count <= 10**6 else None


def _persistable(value: object) -> bool:
    try:
        canonical(value)
    except ValueError:
        return False
    return True


@given(response=RESPONSE)
def test_the_settled_figure_is_exactly_what_the_count_implies(response: object) -> None:
    found = credits_microusd(response)
    event("settles" if found is not None else "held")
    assert found == _oracle(response)
    assert found is None or type(found) is int


@given(response=RESPONSE)
def test_the_settled_figure_is_replay_stable(response: object) -> None:
    if _persistable(response):
        assert credits_microusd(json.loads(canonical(response))) == credits_microusd(response)


def test_the_response_strategy_reaches_both_branches() -> None:
    hits: Counter[str] = Counter()

    @settings(max_examples=400, database=None, derandomize=True)
    @given(response=RESPONSE)
    def draw(response: object) -> None:
        found = credits_microusd(response)
        hits["settles"] += found is not None
        hits["held"] += found is None
        hits["nonzero"] += bool(found)
        usage = response.get("usage") if isinstance(response, dict) else None
        count = usage.get("credits") if isinstance(usage, dict) else None
        hits["bool_count"] += isinstance(count, bool)
        hits["out_of_range_int"] += type(count) is int and not 0 <= count <= 10**6

    draw()
    assert hits["settles"] >= 100 and hits["held"] >= 100, hits
    assert hits["nonzero"] >= 80 and hits["bool_count"] >= 5, hits
    assert hits["out_of_range_int"] >= 10, hits


# --- 3. classify_failure -----------------------------------------------------------------

TABLE: dict[tuple[str, str], AskNewsFailure] = {
    ("asknews_sdk.errors", "RateLimitExceededError"): "rate_or_quota_limited",
    ("asknews_sdk.errors", "ConcurrencyLimitExceededError"): "rate_or_quota_limited",
    ("asknews_sdk.errors", "ForbiddenError"): "forbidden_or_quota",
    ("asknews_sdk.errors", "UnauthorizedError"): "auth_rejected",
    ("asknews_sdk.errors", "BadRequestError"): "request_rejected",
    ("asknews_sdk.errors", "ResourceNotFoundError"): "request_rejected",
    ("asknews_sdk.errors", "MethodNotAllowed"): "request_rejected",
    ("asknews_sdk.errors", "ValidationError"): "request_rejected",
    ("asknews_sdk.errors", "RequestTimeoutError"): "provider_unavailable",
    ("asknews_sdk.errors", "ServiceUnavailableError"): "provider_unavailable",
    ("asknews_sdk.errors", "APIError"): "provider_error",
    ("httpx", "TimeoutException"): "provider_unavailable",
}
BASES: list[type[BaseException]] = [
    Exception,
    RuntimeError,
    ValueError,
    httpx.ConnectError,
    httpx.ReadTimeout,
    *(getattr(sdk_errors, name) for module, name in TABLE if module == "asknews_sdk.errors"),
]
NAMES = st.sampled_from(sorted({name for _, name in TABLE} | {"QuotaError", "Boom"})) | st.text(
    "abcXYZ_", min_size=1, max_size=8
)
MODULES = st.sampled_from(["asknews_sdk.errors", "httpx", "tests.fake", "asknews_sdk.other"])


def _instance(klass: type[BaseException]) -> BaseException:
    """An instance carrying the sentinel everywhere content could live, bypassing __init__."""
    exc = BaseException.__new__(klass, SENTINEL, {"body": SENTINEL})
    for attribute in ("detail", "code", "response"):
        try:
            object.__setattr__(exc, attribute, SENTINEL)
        except (AttributeError, TypeError):
            pass
    return exc


EXCEPTIONS = st.builds(
    lambda name, module, base, depth: _generated(name, module, base, depth),
    NAMES,
    MODULES,
    st.sampled_from(BASES),
    st.integers(0, 2),
)


def _generated(name: str, module: str, base: type[BaseException], depth: int) -> BaseException:
    klass: type[BaseException] = base
    for level in range(depth):
        klass = type(f"Mid{level}", (klass,), {"__module__": "tests.fake"})
    klass = type(name, (klass,), {"__module__": module})
    return _instance(klass)


def _expected(exc: BaseException) -> AskNewsFailure:
    """The oracle: the first (module, name) the MRO hits in the table, else transient."""
    for klass in type(exc).__mro__:
        found = TABLE.get((klass.__module__, klass.__name__))
        if found is not None:
            return found
    return "transient"


@pytest.fixture(scope="module")
def config(tmp_path_factory: pytest.TempPathFactory) -> AppConfig:
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data["model"]["name"] = "openrouter/test-model"
    data["logging"]["file"] = str(tmp_path_factory.mktemp("logs") / "bot.jsonl")
    return validate_config_data(data)


class _Raising:
    def __init__(self, exc: BaseException) -> None:
        self.news = self
        self.exc = exc

    def search_news(self, **kwargs: Any) -> Any:
        raise self.exc


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(exc=EXCEPTIONS)
def test_every_exception_is_named_in_the_vocabulary_and_leaks_nothing(
    config: AppConfig, exc: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    failure = classify_failure(exc)
    event(f"failure={failure}")
    assert failure in get_args(AskNewsFailure)
    assert failure == _expected(exc)
    if not any(k.__module__ in ("asknews_sdk.errors", "httpx") for k in type(exc).__mro__):
        assert failure == "transient"

    result = retrieve_news(
        _Raising(exc),  # type: ignore[arg-type]
        config,
        question_id=7,
        queries=["q"],
        retrieval_run_id="run-prop",
        now=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )
    assert (result.provider_failed, result.failure, result.calls_attempted) == (True, failure, 1)
    assert result.run.error_summary is not None and f"({failure})" in result.run.error_summary
    assert SENTINEL not in json.dumps(result.run.model_dump(mode="json"))
    assert SENTINEL not in caplog.text


def test_the_exception_strategy_reaches_every_member_and_the_lookalikes() -> None:
    hits: Counter[str] = Counter()

    @settings(max_examples=400, database=None, derandomize=True)
    @given(exc=EXCEPTIONS)
    def draw(exc: BaseException) -> None:
        own = type(exc)
        hits[classify_failure(exc)] += 1
        if (own.__module__, own.__name__) in TABLE:
            hits["own_class_match"] += 1
        elif own.__name__ in {name for _, name in TABLE}:
            hits["lookalike_name"] += 1
        base = own.__mro__[1]
        if (own.__module__, own.__name__) in TABLE and _expected(_instance(base)) != (
            classify_failure(exc)
        ):
            hits["own_overrides_base"] += 1

    draw()
    for member in get_args(AskNewsFailure):
        assert hits[member] >= 5, (member, hits)
    assert hits["lookalike_name"] >= 20 and hits["own_overrides_base"] >= 10, hits


# --- 4. The back-fill, through a real ledger ----------------------------------------------


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("asknews-cost") / "template.sqlite3"
    initialize_ledger(path)
    return path


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("asknews-cost-work")


def _ledger(template: Path, workdir: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    root = workdir / uuid4().hex
    root.mkdir()
    shutil.copy(template, root / "ledger.sqlite3")
    conn = connect(root / "ledger.sqlite3")
    try:
        yield conn, root / "artifacts"
    finally:
        conn.close()
        shutil.rmtree(root)


SCOPES = ("42:33122", "42:33125")
STORED = RESPONSE.filter(_persistable)
CALL = st.tuples(
    st.sampled_from(["asknews", "asknews", "asknews", "exa"]),
    st.integers(0, 5).map(lambda n: (0, 1, 1, 1, 1, 2)[n]),
    STORED,
    st.sampled_from(SCOPES),
)
CALLS = st.lists(CALL, min_size=2, max_size=6)


def _will_settle(provider: str, completions: int, response: object) -> int | None:
    return _oracle(response) if provider == "asknews" and completions == 1 else None


def test_the_ledger_strategy_reaches_settlements_and_refusals_together() -> None:
    hits: Counter[str] = Counter()

    @settings(max_examples=150, database=None, derandomize=True)
    @given(calls=CALLS)
    def draw(calls: list[tuple[str, int, object, str]]) -> None:
        settled = [_will_settle(p, c, r) for p, c, r, _ in calls]
        count = sum(amount is not None for amount in settled)
        hits["with_settlement"] += count > 0
        hits["mixed"] += 0 < count < len(calls)
        hits["two_scopes"] += (
            len({s for (_, _, _, s), a in zip(calls, settled) if a is not None}) > 1
        )

    draw()
    assert hits["with_settlement"] >= 60 and hits["mixed"] >= 40, hits
    assert hits["two_scopes"] >= 15, hits


@settings(max_examples=120)
@given(calls=CALLS)
def test_the_back_fill_is_exact_scoped_replay_stable_and_idempotent(
    template: Path, workdir: Path, calls: list[tuple[str, int, object, str]]
) -> None:
    for conn, root in _ledger(template, workdir):
        expected: dict[str, tuple[str, int]] = {}
        for provider, completions, response, scope in calls:
            budget = Budget(conn, root, scope, 10**12)
            with budget_context(budget):
                call_scope, _ = begin_call(provider, 0.025, {"q": uuid4().hex}, 7, "cutoff")
            assert call_scope is not None
            for _ in range(completions):
                append(
                    conn,
                    "retrieval_completed",
                    call_scope,
                    {"response": response, "actual_cost": None},
                )
            (started,) = events(conn, "retrieval_started", call_scope)
            amount = _will_settle(provider, completions, response)
            if amount is not None:
                expected[started["reservation_id"]] = (scope, amount)
        before = {scope: spending(conn, scope) for scope in SCOPES}
        rows = conn.execute("SELECT count(*) FROM tournament_events").fetchone()[0]

        dry = correct_costs(conn)
        assert {s.reservation_id: (s.scope, s.actual_microusd) for s in dry.asknews} == expected
        assert conn.execute("SELECT count(*) FROM tournament_events").fetchone()[0] == rows

        applied = correct_costs(conn, apply=True)
        assert applied.asknews_written == len(expected)
        for scope in SCOPES:
            mine = {r: a for r, (s, a) in expected.items() if s == scope}
            rows_here = events(conn, "cost_settled", scope)
            assert {row["reservation_id"]: row["actual_microusd"] for row in rows_here} == mine
            for row in rows_here:
                assert (row["basis"], row["backfilled"]) == ("asknews_credits", True)
            actual, held = spending(conn, scope)
            assert actual == before[scope][0] + sum(mine.values()) >= before[scope][0]
            assert held == before[scope][1] - 25_000 * len(mine)

        after = {scope: spending(conn, scope) for scope in SCOPES}
        again = correct_costs(conn, apply=True)
        assert (again.asknews, again.asknews_written) == ((), 0)
        assert {scope: spending(conn, scope) for scope in SCOPES} == after
