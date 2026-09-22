"""BYOK cost settlement and the past-spend correction (M1-348); all providers are fake.

Every GPT-6 Astra response carries ``usage.is_byok: true``, ``usage.cost: 0`` and the real
charge in ``usage.cost_details.upstream_inference_cost``. Before M1-348 the priced client
read ``usage.cost`` alone, so 43 calls and $6.03 settled at 0 and the $40 guard saw
nothing. The response shape below is the live one, copied from the ledger's last Astra
``model_response`` with only the numbers kept.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import httpx
import pytest
import yaml

from test_tournament import case as tournament_case  # type: ignore[import-not-found]
from whiskeyjack_bot.cli import main
from whiskeyjack_bot.config import validate_config_data
from whiskeyjack_bot.forecast import generate
from whiskeyjack_bot.forecast.priced import PRICED_MODELS, PricedClient, build_request
from whiskeyjack_bot.tournament import status
from whiskeyjack_bot.tournament_state import (
    Budget,
    StorageFailure,
    append,
    budget_context,
    correct_costs,
    digest,
    events,
    settled_cost,
    spending,
)

ASTRA = "openrouter/openai/gpt-6-astra"
SCOPE = "42:32977"
UPSTREAM = 0.18273
PROMPT = [{"role": "user", "content": "the one prompt"}]


def byok_usage(**changes: Any) -> dict[str, Any]:
    """The live Astra ``usage`` shape; ``changes`` overrides keys, None deletes one."""
    usage: dict[str, Any] = {
        "completion_tokens": 1138,
        "cost": 0,
        "cost_details": {
            "upstream_inference_completions_cost": 0.0569,
            "upstream_inference_cost": UPSTREAM,
            "upstream_inference_prompt_cost": 0.12583,
        },
        "is_byok": True,
        "prompt_tokens": 10067,
        "total_tokens": 11205,
    }
    for key, value in changes.items():
        if value is None:
            usage.pop(key, None)
        else:
            usage[key] = value
    return usage


@pytest.fixture
def case(tmp_path: Any) -> Any:
    yield from tournament_case.__wrapped__(tmp_path)


@pytest.fixture(autouse=True)
def quiet_cli_logging(monkeypatch: Any) -> None:
    monkeypatch.setattr("whiskeyjack_bot.logging_setup.configure_logging", lambda config: None)


def config_file(config: Any, tmp_path: Any) -> str:
    """The operator YAML for ``config`` (as ``test_launch_findings`` writes it)."""
    path = tmp_path / "operator.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    return str(path)


def _astra(config: Any) -> Any:
    data = config.model_dump(mode="json")
    data["model"]["name"] = ASTRA
    return validate_config_data(data)


def _answer_with(monkeypatch: Any, config: Any, usage: Any) -> list[int]:
    """Answer every OpenRouter call with ``usage``; return a one-element call counter."""
    calls = [0]

    def respond(request: Any) -> httpx.Response:
        calls[0] += 1
        body: dict[str, Any] = {"choices": [{"message": {"content": "ok"}}]}
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, json=body)

    original = httpx.AsyncClient
    monkeypatch.setenv(config.model.api_key_env, "test-secret")
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    return calls


def _invoke(conn: Any, config: Any) -> PricedClient:
    client = PricedClient(config)
    budget = Budget(conn, config.storage.artifact_root, SCOPE, 10_000_000)
    with budget_context(budget):
        assert asyncio.run(client.invoke(PROMPT)) == "ok"
    return client


# --- The settlement rule ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (byok_usage(), (UPSTREAM, "upstream_byok")),
        # The non-BYOK (Sol) shape: cost == upstream, and cost is what settles.
        (byok_usage(is_byok=False, cost=UPSTREAM), (UPSTREAM, "openrouter")),
        ({"cost": 0.01}, (0.01, "openrouter")),
        ({"cost": 0}, (0.0, "openrouter")),
        (byok_usage(cost=0.5), (UPSTREAM, "upstream_byok")),
    ],
    ids=["byok", "sol", "no-is_byok", "real-zero", "byok-ignores-cost"],
)
def test_the_settled_figure_follows_is_byok(usage: Any, expected: Any) -> None:
    assert settled_cost(usage) == expected


@pytest.mark.parametrize(
    "usage",
    [
        # Trap 1 of the brief: every one of these must stay unknown, never read as free.
        byok_usage(cost_details=None),
        byok_usage(cost_details={"upstream_inference_cost": None}),
        byok_usage(cost_details={}),
        byok_usage(cost_details={"upstream_inference_cost": "0.18"}),
        byok_usage(cost_details={"upstream_inference_cost": -0.01}),
        byok_usage(cost_details={"upstream_inference_cost": math.nan}),
        byok_usage(cost_details={"upstream_inference_cost": math.inf}),
        byok_usage(cost_details={"upstream_inference_cost": True}),
        byok_usage(cost_details={"upstream_inference_cost": 10**400}),
        byok_usage(cost_details={"upstream_inference_cost": 1e305}),
        byok_usage(cost_details=[UPSTREAM]),
        byok_usage(is_byok="true"),
        byok_usage(is_byok=1),
        byok_usage(is_byok=0),
        {"is_byok": None, "cost": 0.01},
        {"cost": "0.01"},
        {"cost": True},
        {"cost": -1},
        {},
        None,
        [],
        "usage",
    ],
)
def test_an_unknown_cost_never_settles(usage: Any) -> None:
    assert settled_cost(usage) is None


# --- The priced client ------------------------------------------------------------------


def test_a_byok_call_settles_at_the_upstream_figure(case: Any, monkeypatch: Any) -> None:
    conn, config, *_ = case
    config = _astra(config)
    _answer_with(monkeypatch, config, byok_usage())
    client = _invoke(conn, config)
    assert client.last_cost == UPSTREAM
    (settled,) = events(conn, "cost_settled", SCOPE)
    assert settled["actual_microusd"] == math.ceil(UPSTREAM * 1_000_000) == 182_730
    assert settled["basis"] == "upstream_byok"
    scope = f"{SCOPE}:{digest(build_request(PROMPT, PRICED_MODELS[ASTRA]))}"
    (completed,) = events(conn, "model_completed", scope)
    assert completed["cost"] == UPSTREAM and completed["cost_basis"] == "upstream_byok"
    assert spending(conn, SCOPE) == (182_730, 0)


def test_a_non_byok_call_records_the_openrouter_basis(case: Any, monkeypatch: Any) -> None:
    conn, config, *_ = case
    _answer_with(monkeypatch, config, {"cost": 0.25, "is_byok": False})
    _invoke(conn, config)
    (settled,) = events(conn, "cost_settled", SCOPE)
    assert settled == {
        "reservation_id": settled["reservation_id"],
        "actual_microusd": 250_000,
        "basis": "openrouter",
    }


@pytest.mark.parametrize(
    "usage",
    [byok_usage(cost_details=None), byok_usage(is_byok="yes"), None],
    ids=["byok-no-upstream", "malformed-is_byok", "no-usage"],
)
def test_an_unknown_byok_cost_leaves_the_reservation_held(
    case: Any, monkeypatch: Any, usage: Any
) -> None:
    conn, config, *_ = case
    config = _astra(config)
    _answer_with(monkeypatch, config, usage)
    client = _invoke(conn, config)
    assert client.last_cost is None
    assert events(conn, "cost_settled", SCOPE) == []
    actual, held = spending(conn, SCOPE)
    assert actual == 0 and held > 0


def _seed_pre_fix_call(conn: Any, config: Any, *, response: bool) -> str:
    """Journal a pre-M1-348 Astra call whose settlement was interrupted.

    ``model_completed`` carries the BYOK ``0.0`` and no ``cost_basis``, exactly as every
    past Astra call does; ``response`` says whether the ``model_response`` append landed.
    """
    budget = Budget(conn, config.storage.artifact_root, SCOPE, 10_000_000)
    request = build_request(PROMPT, PRICED_MODELS[ASTRA])
    reservation = budget.reserve("openrouter", 1.0, request)
    scope = f"{SCOPE}:{digest(request)}"
    append(conn, "model_started", scope, {"reservation_id": reservation})
    append(conn, "model_completed", scope, {"content": "ok", "cost": 0.0})
    if response:
        append(conn, "model_response", reservation, {"usage": byok_usage(), "content": "ok"})
    return reservation


def test_replay_of_a_pre_fix_call_settles_from_its_stored_response(
    case: Any, monkeypatch: Any
) -> None:
    conn, config, *_ = case
    config = _astra(config)
    calls = _answer_with(monkeypatch, config, byok_usage())
    reservation = _seed_pre_fix_call(conn, config, response=True)
    client = _invoke(conn, config)
    assert calls == [0], "replay must not buy the call again"
    assert client.last_cost == UPSTREAM
    (settled,) = events(conn, "cost_settled", SCOPE)
    assert settled["reservation_id"] == reservation
    assert settled["actual_microusd"] == 182_730 and settled["basis"] == "upstream_byok"


def test_replay_of_a_pre_fix_zero_with_no_response_stays_held(case: Any, monkeypatch: Any) -> None:
    conn, config, *_ = case
    config = _astra(config)
    calls = _answer_with(monkeypatch, config, byok_usage())
    _seed_pre_fix_call(conn, config, response=False)
    client = _invoke(conn, config)
    assert calls == [0]
    assert client.last_cost is None
    assert events(conn, "cost_settled", SCOPE) == []
    assert spending(conn, SCOPE) == (0, 1_000_000)


def test_a_malformed_journal_on_replay_is_a_storage_failure(case: Any, monkeypatch: Any) -> None:
    conn, config, *_ = case
    config = _astra(config)
    _answer_with(monkeypatch, config, byok_usage())
    scope = f"{SCOPE}:{digest(build_request(PROMPT, PRICED_MODELS[ASTRA]))}"
    append(conn, "model_started", scope, {"not_a_reservation": 1})
    append(conn, "model_completed", scope, {"content": "ok", "cost": 0.0})
    with pytest.raises(StorageFailure) as raised:
        _invoke(conn, config)
    assert str(raised.value) == "cannot read tournament journal"


class _Manager:
    """A cost manager that saw spend the priced client did not report."""

    current_usage = 0.5

    def __enter__(self) -> _Manager:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Client:
    def __init__(self, last_cost: float | None) -> None:
        self.last_cost = last_cost

    async def invoke(self, messages: Any) -> str:
        return "text"


@pytest.mark.parametrize(("reported", "expected"), [(0.0, 0.0), (0.3, 0.3), (None, 0.5)])
def test_a_reported_zero_is_not_replaced_by_the_package_counter(
    monkeypatch: Any, reported: float | None, expected: float
) -> None:
    monkeypatch.setattr(generate, "MonetaryCostManager", _Manager)
    assert asyncio.run(generate._invoke_once(_Client(reported), [])) == ("text", expected)


# --- The correction ---------------------------------------------------------------------


def _settle(conn: Any, config: Any, amount: float, usage: Any, *, scope: str = SCOPE) -> str:
    """A reservation settled at ``amount``, with a stored response carrying ``usage``."""
    budget = Budget(conn, config.storage.artifact_root, scope, 100_000_000)
    reservation = budget.reserve("openrouter", 1.0, {"prompt": "x"})
    if usage is not None:
        append(conn, "model_response", reservation, {"usage": usage, "content": "ok"})
    budget.settle(reservation, amount)
    return reservation


def _corrected(conn: Any) -> int:
    return int(
        conn.execute(
            "SELECT count(*) FROM tournament_events WHERE kind='cost_corrected'"
        ).fetchone()[0]
    )


def test_correct_costs_is_a_dry_run_until_applied_then_idempotent(case: Any) -> None:
    conn, config, *_ = case
    byok = _settle(conn, config, 0.0, byok_usage())
    _settle(conn, config, 0.0, None)  # an Exa-shaped settlement at 0: no response
    _settle(conn, config, 0.0, byok_usage(cost_details=None))  # BYOK, no figure
    _settle(conn, config, 0.0, {"cost": 0, "is_byok": False})  # genuinely billed 0
    _settle(conn, config, 0.25, {"cost": 0.25, "is_byok": False})
    before = spending(conn, SCOPE)
    assert before == (250_000, 0)

    dry = correct_costs(conn)
    assert dry.as_dict(applied=False) == {
        "applied": False,
        "reservations": 1,
        "total_usd": 0.18273,
        "already_corrected": 0,
        "refused_no_upstream_figure": 3,
        "written": 0,
    }
    assert [c.reservation_id for c in dry.corrections] == [byok]
    assert _corrected(conn) == 0 and spending(conn, SCOPE) == before

    applied = correct_costs(conn, apply=True)
    assert applied.written == 1 and _corrected(conn) == 1
    assert spending(conn, SCOPE) == (250_000 + 182_730, 0)
    (row,) = events(conn, "cost_corrected", SCOPE)
    assert row == {"reservation_id": byok, "actual_microusd": 182_730, "basis": "upstream_byok"}

    again = correct_costs(conn, apply=True)
    assert (again.written, again.already_corrected, again.corrections) == (0, 1, ())
    assert _corrected(conn) == 1, "a second run must write no second correction"
    assert spending(conn, SCOPE) == (432_730, 0)


def test_the_write_rechecks_the_guard_inside_its_transaction(case: Any, monkeypatch: Any) -> None:
    """A second run that lands between this run's plan and its write writes nothing twice."""
    from whiskeyjack_bot import tournament_state
    from whiskeyjack_bot.ledger import connect

    conn, config, *_ = case
    _settle(conn, config, 0.0, byok_usage())
    real = tournament_state._correction
    raced = []

    def plan_then_race(conn: Any, scope: str, identifier: str) -> Any:
        found = real(conn, scope, identifier)
        if not raced:
            monkeypatch.setattr(tournament_state, "_correction", real)
            with connect(config.storage.sqlite_path) as other:
                raced.append(correct_costs(other, apply=True).written)
        return found

    monkeypatch.setattr(tournament_state, "_correction", plan_then_race)
    report = correct_costs(conn, apply=True)
    assert raced == [1]
    assert len(report.corrections) == 1 and report.written == 0
    assert _corrected(conn) == 1


def test_a_correction_replaces_the_settlement_and_never_adds_to_it(case: Any) -> None:
    conn, config, *_ = case
    zero = _settle(conn, config, 0.0, None)
    paid = _settle(conn, config, 0.005, None)
    assert spending(conn, SCOPE) == (5_000, 0)
    append(conn, "cost_corrected", SCOPE, {"reservation_id": zero, "actual_microusd": 3_000})
    assert spending(conn, SCOPE) == (8_000, 0)
    # Over a nonzero settlement: the larger figure, never the sum.
    append(conn, "cost_corrected", SCOPE, {"reservation_id": paid, "actual_microusd": 3_000})
    assert spending(conn, SCOPE) == (8_000, 0)
    # A duplicate correction row does not count twice.
    append(conn, "cost_corrected", SCOPE, {"reservation_id": zero, "actual_microusd": 3_000})
    assert spending(conn, SCOPE) == (8_000, 0)


def test_a_correction_with_no_settlement_is_ignored_and_the_estimate_stays_held(
    case: Any,
) -> None:
    conn, config, *_ = case
    budget = Budget(conn, config.storage.artifact_root, SCOPE, 10_000_000)
    held = budget.reserve("openrouter", 1.0, {})
    append(conn, "cost_corrected", SCOPE, {"reservation_id": held, "actual_microusd": 7})
    assert spending(conn, SCOPE) == (0, 1_000_000)


def test_status_reports_actual_spend_including_corrections(case: Any) -> None:
    conn, config, *_ = case
    _settle(conn, config, 0.0, byok_usage())
    assert status(conn, config)["actual_cost_usd"] == 0
    correct_costs(conn, apply=True)
    assert status(conn, config)["actual_cost_usd"] == 0.18273


@pytest.mark.parametrize(
    "row",
    [
        {"actual_microusd": 1},
        {"reservation_id": "r", "actual_microusd": "1"},
        {"reservation_id": "r", "actual_microusd": True},
        {"reservation_id": "r", "actual_microusd": -1},
        {"reservation_id": 7, "actual_microusd": 1},
    ],
)
def test_a_malformed_stored_correction_is_a_sanitized_refusal(case: Any, row: Any) -> None:
    conn, *_ = case
    append(conn, "cost_corrected", SCOPE, row)
    with pytest.raises(StorageFailure) as raised:
        spending(conn, SCOPE)
    assert str(raised.value) == "cannot read tournament spending"
    assert raised.value.__suppress_context__ or raised.value.__cause__ is None


def test_a_malformed_stored_settlement_refuses_the_correction_run(case: Any) -> None:
    conn, *_ = case
    append(conn, "cost_settled", SCOPE, {"reservation_id": "r"})
    with pytest.raises(StorageFailure, match="^cannot read tournament spending$"):
        correct_costs(conn)


# --- The command ------------------------------------------------------------------------


def test_the_command_dry_runs_read_only_then_applies(case: Any, tmp_path: Any, capsys: Any) -> None:
    conn, config, *_ = case
    _settle(conn, config, 0.0, byok_usage())
    rows = conn.execute("SELECT count(*) FROM tournament_events").fetchone()[0]
    args = ["tournament", "correct-costs", "--config", config_file(config, tmp_path)]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["applied"], report["reservations"], report["written"]) == (False, 1, 0)
    assert conn.execute("SELECT count(*) FROM tournament_events").fetchone()[0] == rows

    assert main([*args, "--apply"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["applied"], report["written"]) == (True, 1)
    assert _corrected(conn) == 1


@pytest.mark.parametrize("apply", [False, True])
def test_the_command_never_creates_a_missing_ledger(case: Any, tmp_path: Any, apply: bool) -> None:
    config = case[1]
    config = config.model_copy(
        update={
            "storage": config.storage.model_copy(
                update={"sqlite_path": tmp_path / "missing.sqlite"}
            )
        }
    )
    args = ["tournament", "correct-costs", "--config", config_file(config, tmp_path)]
    assert main([*args, "--apply"] if apply else args) != 0
    assert not config.storage.sqlite_path.exists()
