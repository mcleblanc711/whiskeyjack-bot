"""M1-355: the AskNews sub-cap inside a MiniBench series, its pages and its Exa degrade (D51).

Drafted from the acceptance criterion, one test per clause where a clause is testable:
the enable and its refusals, the stored micro-USD figure, the reservation refusal under, at
and over the sub-cap with held-then-settled accounting, that the refusal is its own reason,
the degrade to Exa that still forecasts the question, both page levels and their throttle,
the status block, and no-leak.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from whiskeyjack_bot import tournament_state
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.tournament import status
from whiskeyjack_bot.tournament_state import (
    AskNewsSubcapReached,
    Budget,
    StorageFailure,
    TournamentError,
    append,
    events,
    series_of,
    series_spending,
)
from tests.unit import test_follow
from tests.unit.test_follow import (
    ACCOUNT,
    FIRST,
    NEXT,
    _config,
    _enable_series,
    activations,
    answer,
)
from tests.unit.test_tournament import _recording

SUBCAP = 1_000_000


@pytest.fixture
def series(tmp_path: Path) -> Any:
    """M1-354's followed-series fixture, re-exposed (a bare import would trip F811)."""
    yield from test_follow.series.__wrapped__(tmp_path)


def _budget(case: Any, project: int = FIRST, *, subcap: int | None = SUBCAP) -> Budget:
    conn, config, *_ = case
    (recorded,) = events(conn, "series", "account")
    return Budget(
        conn,
        config.storage.artifact_root,
        f"{ACCOUNT}:{project}",
        80_000_000,
        series_id=recorded["series_id"],
        series_ceiling=80_000_000,
        asknews_ceiling=subcap,
    )


def _seed(case: Any, project: int, provider: str, micro: int, *, stamped: bool = True) -> str:
    """One held reservation, written the way ``Budget.reserve`` writes it."""
    conn, *_ = case
    (recorded,) = events(conn, "series", "account")
    row: dict[str, Any] = {
        "reservation_id": f"seed-{provider}-{project}-{micro}",
        "provider": provider,
        "estimate_microusd": micro,
    }
    if stamped:
        row["series_id"] = recorded["series_id"]
    append(conn, "cost_reserved", f"{ACCOUNT}:{project}", row)
    return str(row["reservation_id"])


# ── enable ────────────────────────────────────────────────────────────────────


def test_enable_stores_the_sub_cap_in_whole_micro_usd_on_the_series_event(series: Any) -> None:
    conn, *_ = series
    (recorded,) = events(conn, "series", "account")
    assert recorded["asknews_budget_microusd"] == 20_000_000
    assert type(recorded["asknews_budget_microusd"]) is int
    assert series_of(conn, activations(conn)[0]).asknews_budget_microusd == 20_000_000  # type: ignore[union-attr]


def test_a_fractional_sub_cap_is_floored_never_rounded_up(tmp_path: Path) -> None:
    config = _config(tmp_path)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    _enable_series(conn, config, asknews_budget_usd=12.3456789)
    (recorded,) = events(conn, "series", "account")
    assert recorded["asknews_budget_microusd"] == 12_345_678


def test_the_sub_cap_may_equal_the_series_ceiling(tmp_path: Path) -> None:
    config = _config(tmp_path)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    _enable_series(conn, config, asknews_budget_usd=80)
    assert events(conn, "series", "account")[0]["asknews_budget_microusd"] == 80_000_000


@pytest.mark.parametrize(
    "value",
    [None, 0, -1, 80.01, float("nan"), float("inf"), 1e-9],
    ids=["missing", "zero", "negative", "over-series", "nan", "inf", "floors-to-zero"],
)
def test_a_follow_enable_with_a_bad_sub_cap_appends_nothing(tmp_path: Path, value: Any) -> None:
    config = _config(tmp_path)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    with pytest.raises(TournamentError, match="AskNews sub-cap"):
        _enable_series(conn, config, asknews_budget_usd=value)
    assert activations(conn) == [] and events(conn, "series", "account") == []


def test_the_sub_cap_is_checked_against_the_series_ceiling_given_not_the_maximum(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    with pytest.raises(TournamentError, match="AskNews sub-cap"):
        _enable_series(conn, config, series_budget_usd=50, asknews_budget_usd=60)
    assert events(conn, "series", "account") == []


def test_a_pinned_profile_refuses_the_sub_cap(tmp_path: Path) -> None:
    config = _config(tmp_path, follow=None)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    with pytest.raises(TournamentError, match="tournament.follow"):
        tournament_state.enable(
            conn,
            config,
            account_id=ACCOUNT,
            project_id=FIRST,
            starts=tournament_state.utcnow() - timedelta(minutes=1),
            ends=tournament_state.utcnow() + timedelta(days=1),
            asknews_budget_usd=5,
        )
    assert activations(conn) == []


# ── reading the series back ───────────────────────────────────────────────────


def test_a_series_enabled_before_the_sub_cap_reads_back_with_none(series: Any) -> None:
    conn, *_ = series
    legacy = dict(events(conn, "series", "account")[0])
    del legacy["asknews_budget_microusd"]
    assert tournament_state._series_from(legacy).asknews_budget_microusd is None


@pytest.mark.parametrize(
    "stored",
    [None, "20000000", 0, -5, 80_000_001, True, 2.5e7],
    ids=["null", "str", "zero", "negative", "over-ceiling", "bool", "float"],
)
def test_a_malformed_stored_sub_cap_is_a_storage_failure(series: Any, stored: Any) -> None:
    conn, *_ = series
    row = dict(events(conn, "series", "account")[0], asknews_budget_microusd=stored)
    with pytest.raises(StorageFailure, match="cannot read series authorization") as caught:
        tournament_state._series_from(row)
    assert str(stored) not in str(caught.value) or str(stored) in {"0", "True", "None"}


# ── the reservation ───────────────────────────────────────────────────────────


def test_a_reservation_under_the_sub_cap_is_recorded(series: Any) -> None:
    conn, *_ = series
    budget = _budget(series)
    budget.reserve("asknews", 0.5, {})
    assert series_spending(conn, budget.series_id or "", "asknews") == (0, 500_000)


def test_a_reservation_exactly_at_the_sub_cap_is_allowed_and_one_micro_over_is_not(
    series: Any,
) -> None:
    conn, *_ = series
    budget = _budget(series)
    budget.reserve("asknews", 0.999999, {})
    reserved = len(events(conn, "cost_reserved", f"{ACCOUNT}:{FIRST}"))
    with pytest.raises(AskNewsSubcapReached, match="AskNews sub-cap reached"):
        budget.reserve("asknews", 0.000002, {})
    assert len(events(conn, "cost_reserved", f"{ACCOUNT}:{FIRST}")) == reserved
    budget.reserve("asknews", 0.000001, {})
    assert series_spending(conn, budget.series_id or "", "asknews") == (0, SUBCAP)


def test_the_refusal_is_its_own_reason_distinct_from_the_round_and_series_ceilings(
    series: Any,
) -> None:
    conn, config, *_ = series
    (recorded,) = events(conn, "series", "account")
    budget = _budget(series)
    _seed(series, NEXT, "asknews", SUBCAP)
    with pytest.raises(AskNewsSubcapReached) as sub:
        budget.reserve("asknews", 0.01, {})
    assert str(sub.value) == "AskNews sub-cap reached; no provider call made"

    round_budget = Budget(
        conn, config.storage.artifact_root, f"{ACCOUNT}:{FIRST}", 100_000, asknews_ceiling=SUBCAP
    )
    with pytest.raises(TournamentError, match="round budget exhausted") as rnd:
        round_budget.reserve("asknews", 0.2, {})
    assert not isinstance(rnd.value, AskNewsSubcapReached)

    tight = Budget(
        conn,
        config.storage.artifact_root,
        f"{ACCOUNT}:{FIRST}",
        80_000_000,
        series_id=recorded["series_id"],
        series_ceiling=SUBCAP,
        asknews_ceiling=80_000_000,
    )
    with pytest.raises(TournamentError, match="series budget exhausted") as cap:
        tight.reserve("asknews", 0.01, {})
    assert not isinstance(cap.value, AskNewsSubcapReached)


def test_only_asknews_reservations_are_refused_by_the_sub_cap(series: Any) -> None:
    conn, *_ = series
    budget = _budget(series)
    _seed(series, FIRST, "asknews", SUBCAP)
    budget.reserve("exa", 0.05, {})
    budget.reserve("openrouter", 0.3, {})
    with pytest.raises(AskNewsSubcapReached):
        budget.reserve("asknews", 0.000001, {})
    assert series_spending(conn, budget.series_id or "") == (0, SUBCAP + 350_000)


def test_the_sub_cap_counts_asknews_across_projects_and_ignores_other_providers_and_pre_series(
    series: Any,
) -> None:
    conn, *_ = series
    budget = _budget(series, NEXT)
    _seed(series, FIRST, "asknews", 600_000)  # another project in the series: counts
    _seed(series, FIRST, "openrouter", 50_000_000)  # not AskNews: ignored
    _seed(series, NEXT, "asknews", 30_000_000, stamped=False)  # before the series: ignored
    budget.reserve("asknews", 0.4, {})
    with pytest.raises(AskNewsSubcapReached):
        budget.reserve("asknews", 0.000001, {})
    assert series_spending(conn, budget.series_id or "", "asknews") == (0, SUBCAP)


def test_a_settlement_below_the_estimate_frees_room_and_a_held_one_still_counts(
    series: Any,
) -> None:
    conn, *_ = series
    budget = _budget(series)
    first = budget.reserve("asknews", 0.6, {})
    second = budget.reserve("asknews", 0.4, {})
    series_id = budget.series_id or ""
    assert series_spending(conn, series_id, "asknews") == (0, SUBCAP)
    with pytest.raises(AskNewsSubcapReached):
        budget.reserve("asknews", 0.1, {})
    budget.settle_microusd(first, 100_000, basis="asknews_credits")
    assert series_spending(conn, series_id, "asknews") == (100_000, 400_000)
    budget.reserve("asknews", 0.5, {})
    assert series_spending(conn, series_id, "asknews") == (100_000, 900_000)
    budget.settle_microusd(second, 400_000, basis="asknews_credits")
    assert series_spending(conn, series_id, "asknews") == (500_000, 500_000)


def test_a_series_with_no_recorded_sub_cap_refuses_nothing_on_its_account(series: Any) -> None:
    conn, *_ = series
    budget = _budget(series, subcap=None)
    for _ in range(3):
        budget.reserve("asknews", 5.0, {})
    assert series_spending(conn, budget.series_id or "", "asknews") == (0, 15_000_000)


# ── the degrade ───────────────────────────────────────────────────────────────


class _Exa:
    """An Exa transport answering one usable result and recording every request."""

    def __init__(self) -> None:
        self.requests = 0
        self.empty = False
        self.title = ""  # the polled question's own title, set by ``_poll_with_exa``

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests += 1
            # Behind the poll's own clock, as the AskNews fake does: a document dated after
            # the as-of instant reads as not yet published.
            now = (tournament_state.utcnow() - timedelta(seconds=2)).isoformat()
            return httpx.Response(
                200,
                json={
                    # Two sources: the fake model's reply cites `src-001` and `src-002`, and a
                    # packet that lacks the second is refused as schema-invalid.
                    "results": []
                    if self.empty
                    else [
                        {
                            "title": self.title,
                            "url": url,
                            "publishedDate": now,
                            "text": "The agency July data release is scheduled for publication.",
                        }
                        for url in ("https://example.org/first", "https://example.org/second")
                    ],
                    "costDollars": {"total": 0.007},
                },
            )

        return httpx.Client(base_url="https://api.exa.ai", transport=httpx.MockTransport(handler))


def _poll_with_exa(case: Any, exa: _Exa, body: bytes) -> dict[str, Any]:
    from whiskeyjack_bot.tournament import run_once

    conn, config, platform, news, model = case
    platform.open_on = NEXT  # the fixture question belongs to NEXT: poll through the rollover
    exa.title = platform.raw["question"]["title"]
    return run_once(
        conn,
        config,
        client=platform,
        poster=platform,
        news_client=news,
        web_client=exa.client(),
        forecaster=model,
        series_reader=lambda: body,
    )


def test_at_the_sub_cap_the_question_is_still_forecast_on_the_exa_fallback(series: Any) -> None:
    conn, _, platform, news, _ = series
    _seed(
        series, FIRST, "asknews", 20_000_000
    )  # the whole sub-cap, on the project before the rollover
    exa = _Exa()
    result = _poll_with_exa(series, exa, answer(NEXT))
    assert news.calls == 0
    assert exa.requests >= 1
    assert result["heartbeat"]["failures"] == 0 and result["heartbeat"]["processed"] == 1
    assert platform.posts == 1
    qid = platform.raw["question"]["id"]
    assert events(conn, "question_failure", f"{NEXT}:{qid}") == []
    spent = series_spending(conn, events(conn, "series", "account")[0]["series_id"], "asknews")
    assert spent == (0, 20_000_000)


def test_the_primary_run_names_the_sub_cap_and_the_exa_run_is_recorded(series: Any) -> None:
    conn, *_ = series
    _seed(series, FIRST, "asknews", 20_000_000)
    _poll_with_exa(series, _Exa(), answer(NEXT))
    rows = conn.execute(
        "SELECT provider, error_summary, provider_config_json FROM research_runs"
    ).fetchall()
    providers = [row[0] for row in rows]
    assert providers.count("asknews") == 1 and providers.count("exa") == 1
    news = next(row for row in rows if row[0] == "asknews")
    assert "AskNews sub-cap" in news[1] and "provider call failed" not in news[1]
    # Not an outage, so the fallback's recorded reason must not claim one.
    reasons = json.loads(next(row for row in rows if row[0] == "exa")[2])["fallback_reasons"]
    assert "primary_returned_no_documents" in reasons
    assert "primary_provider_failed" not in reasons


def test_when_exa_finds_nothing_either_the_question_is_recorded_evidence_poor_not_failed(
    series: Any,
) -> None:
    """The sub-cap is a skip, not an outage: nothing here is a transient `provider_error`
    to retry, so the empty answer is `no_documents` and the question is forecast."""
    conn, _, platform, *_ = series
    _seed(series, FIRST, "asknews", 20_000_000)
    exa = _Exa()
    exa.empty = True
    result = _poll_with_exa(series, exa, answer(NEXT))
    qid = platform.raw["question"]["id"]
    assert events(conn, "question_failure", f"{NEXT}:{qid}") == []
    assert result["heartbeat"]["failures"] == 0 and platform.posts == 1
    # Scoped to the record, so read by kind rather than by question.
    (marker,) = [
        json.loads(row[0])
        for row in conn.execute("SELECT data FROM tournament_events WHERE kind='evidence_gap'")
    ]
    assert marker["code"] == "evidence_poor"
    assert marker.get("reason") in (None, "no_documents")


def test_a_refused_pass_asks_once_not_once_per_query(
    series: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused reservation ends the AskNews pass; it is not retried for each further query.
    Production derives one query, so this drives ``retrieve_news`` with several directly."""
    from whiskeyjack_bot.research.asknews import retrieve_news
    from whiskeyjack_bot.tournament_state import budget_context

    conn, config, _, news, _ = series
    assert config.retrieval.max_queries_per_question >= 3
    asked: list[str] = []
    original = Budget.reserve

    def counting(self: Budget, provider: str, estimate: float, request: Any) -> str:
        asked.append(provider)
        return original(self, provider, estimate, request)

    monkeypatch.setattr(Budget, "reserve", counting)
    _seed(series, FIRST, "asknews", SUBCAP)
    with budget_context(_budget(series, NEXT)):
        result = retrieve_news(
            news,
            config,
            question_id=91001,
            queries=["first query", "second query", "third query"],
            retrieval_run_id="run-subcap",
            now=tournament_state.utcnow(),
        )
    assert asked == ["asknews"] and news.calls == 0
    assert result.provider_failed is False and result.documents == ()


def test_under_the_sub_cap_asknews_is_used_and_exa_is_not_bought(series: Any) -> None:
    _, _, platform, news, _ = series
    exa = _Exa()
    result = _poll_with_exa(series, exa, answer(NEXT))
    assert news.calls >= 1 and platform.posts == 1
    assert result["heartbeat"]["failures"] == 0
    assert exa.requests == 0


def test_the_round_budget_refusal_still_fails_the_question_rather_than_degrading(
    series: Any,
) -> None:
    """The degrade is for the sub-cap alone: a spent round budget stops AskNews and Exa
    alike, and must stay a failure."""
    conn, _, platform, news, _ = series
    (recorded,) = events(conn, "series", "account")
    append(
        conn,
        "cost_reserved",
        f"{ACCOUNT}:{NEXT}",
        {
            "reservation_id": "round",
            "provider": "openrouter",
            "estimate_microusd": 39_999_999,
            "series_id": recorded["series_id"],
        },
    )
    exa = _Exa()
    result = _poll_with_exa(series, exa, answer(NEXT))
    assert result["heartbeat"]["failures"] == 1 and platform.posts == 0
    assert news.calls == 0 and exa.requests == 0


# ── the pages ─────────────────────────────────────────────────────────────────


def _notifier(case: Any) -> tuple[Any, Any]:
    """A real notifier over a recording transport, with its own empty throttle directory."""
    from tests.unit.test_tournament import _Pushes
    from whiskeyjack_bot.notify import Notifier

    _, config, *_ = case
    pushes = _Pushes()
    notifier = Notifier(
        client=httpx.Client(transport=httpx.MockTransport(pushes)),
        topic_url="https://ntfy.invalid/wj-fake-topic-0001",
        state_root=config.storage.artifact_root / "push-state",
        secret_names=(),
    )
    return notifier, pushes


def test_no_page_below_80_percent_and_one_at_80_that_the_throttle_holds_per_series(
    series: Any,
) -> None:
    from whiskeyjack_bot.notify import notifier_context

    budget = _budget(series)  # sub-cap 1.00
    notifier, pushes = _notifier(series)
    with notifier_context(notifier):
        budget.reserve("asknews", 0.79, {})
        assert pushes.sent == []
        budget.reserve("asknews", 0.01, {})  # 0.80 exactly: the level is inclusive
        assert len(pushes.matching("AskNews sub-cap at 80%")) == 1
        budget.reserve("asknews", 0.05, {})  # still past 80%: a condition, not a new event
        budget.reserve("asknews", 0.05, {})
        assert len(pushes.matching("AskNews sub-cap at 80%")) == 1
        assert pushes.matching("AskNews sub-cap at 100%") == []
    (page,) = pushes.matching("AskNews sub-cap at 80%")
    assert page["priority"] and "tournament status" in page["body"]


def test_the_100_percent_page_fires_once_at_the_refusal_and_not_again_while_it_holds(
    series: Any,
) -> None:
    from whiskeyjack_bot.notify import notifier_context

    budget = _budget(series)
    notifier, pushes = _notifier(series)
    with notifier_context(notifier):
        budget.reserve("asknews", 0.9, {})
        for _ in range(3):
            with pytest.raises(AskNewsSubcapReached):
                budget.reserve("asknews", 0.2, {})
    assert len(pushes.matching("AskNews sub-cap at 100%")) == 1
    (page,) = pushes.matching("AskNews sub-cap at 100%")
    assert "Exa fallback" in page["body"]
    assert len(pushes.matching("AskNews sub-cap at 80%")) == 1


def test_a_second_series_pages_on_its_own_levels(series: Any, tmp_path: Path) -> None:
    from whiskeyjack_bot.notify import notifier_context

    conn, *_ = series
    notifier, pushes = _notifier(series)
    one = _budget(series)
    other = Budget(
        conn,
        one.root,
        f"{ACCOUNT}:{NEXT}",
        80_000_000,
        series_id="b" * 32,
        series_ceiling=80_000_000,
        asknews_ceiling=SUBCAP,
    )
    with notifier_context(notifier):
        one.reserve("asknews", 0.85, {})
        other.reserve("asknews", 0.85, {})
    assert len(pushes.matching("AskNews sub-cap at 80%")) == 2


def test_a_poll_that_reaches_the_sub_cap_pages_and_does_not_report_a_provider_failure(
    series: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, config, *_ = series
    pushes = _recording(monkeypatch, config, config.storage.artifact_root / "push-state")
    _seed(series, FIRST, "asknews", 20_000_000)
    _poll_with_exa(series, _Exa(), answer(NEXT))
    assert len(pushes.matching("AskNews sub-cap at 100%")) == 1
    assert pushes.matching("asknews failed") == []


def test_no_provider_or_series_value_reaches_a_page_or_the_log(
    series: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    conn, config, platform, *_ = series
    pushes = _recording(monkeypatch, config, config.storage.artifact_root / "push-state")
    _seed(series, FIRST, "asknews", 20_000_000)
    with caplog.at_level(logging.DEBUG):
        _poll_with_exa(series, _Exa(), answer(NEXT))
    series_id = events(conn, "series", "account")[0]["series_id"]
    text = json.dumps(pushes.sent) + caplog.text
    for forbidden in (
        series_id,
        platform.raw["question"]["title"],
        "example.org",
        "20000000",
        "seed-asknews",
    ):
        assert forbidden not in text
    assert pushes.matching("AskNews sub-cap")


# ── status ────────────────────────────────────────────────────────────────────


def test_status_reports_the_sub_cap_the_spend_and_what_remains(series: Any) -> None:
    conn, config, *_ = series
    budget = _budget(series, subcap=20_000_000)
    held = budget.reserve("asknews", 5.0, {})
    budget.reserve("openrouter", 9.0, {})
    budget.settle_microusd(held, 3_000_000, basis="asknews_credits")
    budget.reserve("asknews", 2.0, {})
    news = status(conn, config)["series"]["asknews"]
    assert news == {
        "sub_cap_usd": 20,
        "actual_cost_usd": 3,
        "reserved_cost_usd": 2,
        "remaining_budget_usd": 15,
    }


def test_status_says_so_for_a_series_with_no_recorded_sub_cap(series: Any, tmp_path: Path) -> None:
    """The ledger is append-only, so a pre-M1-355 series is rebuilt in a fresh one: the same
    two rows, the series row minus its sub-cap key."""
    conn, config, *_ = series
    legacy = dict(events(conn, "series", "account")[0])
    del legacy["asknews_budget_microusd"]
    path = tmp_path / "legacy" / "ledger.sqlite3"
    path.parent.mkdir()
    initialize_ledger(path)
    old = connect(path)
    append(old, "series", "account", legacy)
    append(old, "activation", "account", activations(conn)[0])
    report = status(old, config)["series"]
    assert report["asknews"] is None
    assert report["ceiling_usd"] == 80
