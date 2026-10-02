"""M1-354: following MiniBench across a rollover under an owner-enabled series (D51).

Drafted from the acceptance criterion, one test per clause where a clause is testable:
the series enable and its refusals, the rebind and each guard that refuses it, a read that
fails or times out, the series ceiling at reservation, replay of a rebind from the stored
answer, the watchdog's view before and after, and that no Metaculus value reaches a push.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from forecasting_tools.data_models.data_organizer import DataOrganizer

from whiskeyjack_bot import follow as follow_module
from whiskeyjack_bot import tournament_state
from whiskeyjack_bot.config import validate_config_data
from whiskeyjack_bot.follow import (
    follow,
    parse_series_project,
    read_series_project,
    replay_rebind,
)
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.tournament import run_once, status
from whiskeyjack_bot.tournament_state import (
    Budget,
    TournamentError,
    append,
    bound_project,
    disable,
    enable,
    events,
    require_activation,
    retired_bindings,
    series_of,
    series_spending,
    utcnow,
)
from tests.unit.test_pipeline_live import config as base_config
from tests.unit.test_tournament import ROOT, Model, News, Platform, _recording

FIRST = 33125
NEXT = 33130
ACCOUNT = 42


class SeriesPlatform(Platform):
    """The launch fake, answering for whichever project it is asked about."""

    def __init__(self, raw: dict[str, Any]) -> None:
        super().__init__(raw)
        self.polled: list[int] = []
        self.open_on: int | None = None

    def get_all_open_questions_from_tournament(self, project: int, **kwargs: Any) -> list[Any]:
        self.polled.append(project)
        if project != self.open_on:
            return []
        return [DataOrganizer.get_question_from_post_json(copy.deepcopy(self.raw))]


def answer(
    project_id: object = NEXT,
    *,
    slug: object = "minibench",
    close: object = None,
    ongoing: object = True,
) -> bytes:
    """A Metaculus project answer, shaped like the one measured on 2026-09-27."""
    return json.dumps(
        {
            "id": project_id,
            "slug": slug,
            "name": "MiniBench",
            "close_date": close
            if close is not None
            else (utcnow() + timedelta(days=10)).isoformat(),
            "is_ongoing": ongoing,
        }
    ).encode()


def _config(
    tmp_path: Path,
    *,
    environment: str = "production",
    follow: object = "minibench",
    project: int = FIRST,
) -> Any:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = base_config.__wrapped__(tmp_path)
    data = config.model_dump(mode="json")
    prompt = tmp_path / "forecaster.md"
    prompt.write_bytes(config.forecast.prompt_path.read_bytes())
    data["forecast"]["prompt_path"] = str(prompt)
    data["environment"] = environment
    data["metaculus"]["tournament"].update(id=project, use_sdk_current_id=False, follow=follow)
    data["model"].update(
        name=Model.model, max_output_tokens=6000, timeout_seconds=120, temperature=None
    )
    data["submission"].update(
        enabled=True, dry_run=False, no_submit=False, post_private_reasoning_comment=True
    )
    data["retrieval"]["primary"]["retries"] = 0
    data["retrieval"]["fallback"]["retries"] = 0
    return validate_config_data(data)


def _enable_series(conn: Any, config: Any, **overrides: Any) -> str:
    arguments: dict[str, Any] = {
        "account_id": ACCOUNT,
        "project_id": FIRST,
        "starts": utcnow() - timedelta(minutes=1),
        "ends": utcnow() + timedelta(days=5),
        "budget_usd": 40,
        "series_budget_usd": 80,
        "asknews_budget_usd": 20,
        "series_ends": utcnow() + timedelta(days=30),
    }
    arguments.update(overrides)
    return enable(conn, config, **arguments)


@pytest.fixture
def series(tmp_path: Path) -> Any:
    config = _config(tmp_path)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    raw = json.loads((ROOT / "tests/fixtures/api_posts/binary_post.json").read_text())
    raw["projects"]["default_project"]["id"] = NEXT
    raw["question"]["scheduled_close_time"] = (utcnow() + timedelta(hours=2)).isoformat()
    platform, news, model = SeriesPlatform(raw), News(raw), Model(raw)
    _enable_series(conn, config)
    yield conn, config, platform, news, model
    conn.close()


def poll(case: Any, body: Any = None, *, reader: Any = None) -> dict[str, Any]:
    conn, config, platform, news, model = case
    read = reader or (lambda: answer(FIRST) if body is None else body)
    return run_once(
        conn,
        config,
        client=platform,
        poster=platform,
        news_client=news,
        web_client=object(),
        forecaster=model,
        series_reader=read,
    )


def activations(conn: Any) -> list[dict[str, Any]]:
    return events(conn, "activation", "account")


# ── enable ────────────────────────────────────────────────────────────────────


def test_enable_records_the_series_and_binds_the_first_project_as_the_owner(series: Any) -> None:
    conn, config, *_ = series
    (recorded,) = events(conn, "series", "account")
    (first,) = activations(conn)
    assert recorded["follow"] == "minibench"
    assert recorded["account_id"] == ACCOUNT
    assert recorded["budget_microusd"] == 80_000_000
    assert recorded["project_budget_microusd"] == 40_000_000
    assert first["series_id"] == recorded["series_id"]
    assert first["bound_by"] == "owner"
    assert first["project_id"] == FIRST
    assert tournament_state.MAX_SERIES_BUDGET_USD == 80
    assert bound_project(conn, config) == str(FIRST)


@pytest.mark.parametrize(
    "overrides",
    [
        {"series_budget_usd": None},
        {"series_ends": None},
        {"series_budget_usd": 80.01},
        {"series_budget_usd": 0},
        {"budget_usd": 50, "series_budget_usd": 40},
        {"series_ends": utcnow() - timedelta(minutes=1)},
        {"ends": utcnow() + timedelta(days=40)},
    ],
    ids=[
        "no-ceiling",
        "no-end",
        "ceiling-over-80",
        "ceiling-zero",
        "project-over-series",
        "series-ended",
        "window-past-series",
    ],
)
def test_a_follow_enable_outside_its_limits_appends_nothing(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    config = _config(tmp_path)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    with pytest.raises(TournamentError):
        _enable_series(conn, config, **overrides)
    assert activations(conn) == [] and events(conn, "series", "account") == []


def test_a_pinned_profile_refuses_series_options(tmp_path: Path) -> None:
    config = _config(tmp_path, follow=None)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    with pytest.raises(TournamentError, match="tournament.follow"):
        _enable_series(conn, config)
    assert activations(conn) == []


def test_a_testing_profile_cannot_follow(tmp_path: Path) -> None:
    """On the testing project, so the only refusal left is the production one. (The first
    draft kept id 33125, was refused as a testing profile off 32977, and the mutation pass
    showed the follow-mode refusal itself was never reached.)"""
    config = _config(tmp_path, environment="test", project=32977)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    with pytest.raises(TournamentError, match="production profile"):
        _enable_series(conn, config, project_id=32977)
    assert activations(conn) == []


@pytest.mark.parametrize(
    "tournament",
    [
        {"id": "minibench", "follow": "minibench"},
        {"id": 0, "follow": "minibench"},
        {"id": FIRST, "follow": "minibench", "use_sdk_current_id": True},
        {"id": FIRST, "follow": "aibq3"},
    ],
    ids=["slug-id", "zero-id", "sdk-alias", "unknown-series"],
)
def test_follow_config_needs_a_concrete_project_and_a_known_series(
    tmp_path: Path, tournament: dict[str, Any]
) -> None:
    from whiskeyjack_bot.config import ConfigError

    data = _config(tmp_path).model_dump(mode="json")
    data["metaculus"]["tournament"] = tournament
    with pytest.raises(ConfigError):
        validate_config_data(data)


# ── the steady state ──────────────────────────────────────────────────────────


def test_a_poll_with_the_slug_unmoved_polls_the_bound_project_and_binds_nothing(
    series: Any,
) -> None:
    conn, _, platform, *_ = series
    poll(series, answer(FIRST))
    assert platform.polled == [FIRST]
    assert len(activations(conn)) == 1


def test_a_pinned_profile_never_reads_the_slug(tmp_path: Path) -> None:
    config = _config(tmp_path, follow=None)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    enable(
        conn,
        config,
        account_id=ACCOUNT,
        project_id=FIRST,
        starts=utcnow() - timedelta(minutes=1),
        ends=utcnow() + timedelta(days=1),
    )
    raw = json.loads((ROOT / "tests/fixtures/api_posts/binary_post.json").read_text())
    platform = SeriesPlatform(raw)

    def never() -> bytes:
        raise AssertionError("a pinned profile read the slug")

    poll((conn, config, platform, News(raw), Model(raw)), reader=never)
    assert platform.polled == [FIRST]
    assert follow(conn, config, account_id=ACCOUNT, read=never) == "no_series"


# ── the rebind ────────────────────────────────────────────────────────────────


def test_a_rollover_is_followed_and_the_same_poll_forecasts_on_the_new_project(
    series: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, config, platform, *_ = series
    pushes = _recording(monkeypatch, config, config.storage.artifact_root / "push-state")
    platform.open_on = NEXT
    close = utcnow() + timedelta(days=10)
    body = answer(NEXT, close=close.isoformat())
    before = utcnow()
    result = poll(series, body)
    after = utcnow()

    owner, bound = activations(conn)
    assert bound["project_id"] == NEXT
    assert bound["bound_by"] == f"policy:follow-v1:{owner['series_id']}"
    assert bound["series_id"] == owner["series_id"]
    assert bound["previous_activation_id"] == owner["activation_id"]
    assert before <= datetime.fromisoformat(bound["starts"]) <= after
    assert bound["ends"] == close.isoformat()
    assert bound["budget_microusd"] == 40_000_000
    assert bound["config_sha256"] == owner["config_sha256"]
    evidence = config.storage.artifact_root / bound["evidence"]["path"]
    assert evidence.read_bytes() == body
    assert bound["evidence"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert replay_rebind(conn, config, bound)

    assert platform.polled == [NEXT]
    assert result["project_id"] == NEXT
    assert result["heartbeat"]["processed"] == 1 and platform.posts == 1
    assert result["series"]["bound_by"] == bound["bound_by"]
    (notice,) = pushes.matching("followed")
    assert notice["priority"] == "default"


def test_the_window_ends_at_the_series_end_when_the_project_outlives_it(series: Any) -> None:
    conn, config, *_ = series
    (recorded,) = events(conn, "series", "account")
    poll(series, answer(NEXT, close=(utcnow() + timedelta(days=90)).isoformat()))
    assert activations(conn)[-1]["ends"] == recorded["ends"]


def test_the_next_poll_after_a_rebind_is_the_steady_state(series: Any) -> None:
    conn, _, platform, *_ = series
    poll(series, answer(NEXT))
    poll(series, answer(NEXT))
    assert len(activations(conn)) == 2
    assert platform.polled == [NEXT, NEXT]


# ── each guard ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (answer(NEXT, slug="aibq3"), "not_the_series"),
        (answer(FIRST - 1), "not_newer"),
        (answer(NEXT, ongoing=False), "not_ongoing"),
        (answer(NEXT, close=(utcnow() - timedelta(minutes=1)).isoformat()), "not_ongoing"),
        (b"<html>maintenance</html>", "project_unreadable"),
        (answer(True), "project_unreadable"),
        (answer(NEXT, close="2026-10-09T00:00:00"), "project_unreadable"),
        (b"{" * 100_000, "project_unreadable"),
        # Valid JSON padded past the limit: refused for its size, not its content. (The
        # first draft padded with bare spaces, which fails to parse anyway, and the mutation
        # pass showed removing the size check survived it.)
        (answer(NEXT) + b" " * follow_module.FOLLOW_RESPONSE_LIMIT, "project_unreadable"),
    ],
    ids=[
        "other-series",
        "older-project",
        "not-ongoing",
        "closed",
        "not-json",
        "bool-id",
        "naive-close",
        "deep-nesting",
        "oversize",
    ],
)
def test_a_refused_answer_binds_nothing_and_the_poll_carries_on(
    series: Any, body: bytes, expected: str
) -> None:
    conn, config, platform, *_ = series
    assert follow(conn, config, account_id=ACCOUNT, read=lambda: body) == expected
    poll(series, body)
    assert len(activations(conn)) == 1
    assert platform.polled == [FIRST]


@pytest.mark.parametrize(
    "failure",
    [OSError("unreachable"), TournamentError("series project read failed"), ValueError, None],
    ids=["network", "refused", "value", "not-bytes"],
)
def test_a_failed_read_never_stops_the_poll(series: Any, failure: object) -> None:
    conn, config, platform, *_ = series

    def read() -> Any:
        if failure is None:
            return "33130"
        raise failure  # type: ignore[misc]

    assert follow(conn, config, account_id=ACCOUNT, read=read) == "project_unreadable"
    poll(series, reader=read)
    assert len(activations(conn)) == 1
    assert platform.polled == [FIRST]


def test_a_read_that_hangs_is_cut_off_by_the_wall_clock_bound(
    series: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, config, *_ = series
    monkeypatch.setattr(follow_module, "FOLLOW_READ_SECONDS", 1)

    def slow() -> bytes:
        time.sleep(5)
        return answer(NEXT)

    started = time.monotonic()
    assert follow(conn, config, account_id=ACCOUNT, read=slow) == "project_unreadable"
    assert time.monotonic() - started < 4
    assert len(activations(conn)) == 1


def test_another_account_is_refused(series: Any) -> None:
    conn, config, *_ = series
    assert follow(conn, config, account_id=ACCOUNT + 1, read=lambda: answer()) == (
        "account_mismatch"
    )
    assert len(activations(conn)) == 1


def test_a_disabled_series_binds_nothing(series: Any) -> None:
    conn, config, *_ = series
    disable(conn)
    (series_id,) = [row["series_id"] for row in events(conn, "series", "account")]
    assert events(conn, "disabled", series_id) == [{}]
    assert follow(conn, config, account_id=ACCOUNT, read=lambda: answer()) == "series_disabled"
    with pytest.raises(TournamentError):
        poll(series, answer())
    assert len(activations(conn)) == 1


def test_a_disable_that_fails_partway_commits_neither_event(
    series: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 1 (M1-354): the activation's `disabled` committed before the series', so an
    ordinary I/O failure on the second write left the series able to rebind. Both events now
    commit together or not at all."""
    conn, config, *_ = series
    (series_id,) = [row["series_id"] for row in events(conn, "series", "account")]
    real = tournament_state.append

    def failing(conn: Any, kind: str, scope: str, data: dict[str, Any]) -> str:
        if kind == "disabled" and scope == series_id:
            raise tournament_state.StorageFailure("cannot commit tournament journal")
        return real(conn, kind, scope, data)

    monkeypatch.setattr(tournament_state, "append", failing)
    with pytest.raises(tournament_state.StorageFailure):
        disable(conn)
    monkeypatch.setattr(tournament_state, "append", real)
    (owner,) = activations(conn)
    assert events(conn, "disabled", owner["activation_id"]) == []
    assert events(conn, "disabled", series_id) == []
    disable(conn)
    assert follow(conn, config, account_id=ACCOUNT, read=lambda: answer()) == "series_disabled"


def test_an_enable_that_fails_partway_commits_neither_the_series_nor_the_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sibling of the partial disable: `enable` writes the series and its first
    activation, and a series with no activation (or the reverse) must never be left."""
    config = _config(tmp_path)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    real = tournament_state.append

    def failing(conn: Any, kind: str, scope: str, data: dict[str, Any]) -> str:
        if kind == "activation":
            raise tournament_state.StorageFailure("cannot commit tournament journal")
        return real(conn, kind, scope, data)

    monkeypatch.setattr(tournament_state, "append", failing)
    with pytest.raises(tournament_state.StorageFailure):
        _enable_series(conn, config)
    assert events(conn, "series", "account") == [] and activations(conn) == []


def test_an_expired_series_binds_nothing(series: Any) -> None:
    conn, config, *_ = series
    later = utcnow() + timedelta(days=31)
    assert (
        follow(conn, config, account_id=ACCOUNT, read=lambda: answer(), now=later)
        == "series_expired"
    )
    assert len(activations(conn)) == 1


def test_the_series_end_and_the_close_date_are_exclusive_at_the_instant(series: Any) -> None:
    """Both boundaries, exactly: at the series end nothing binds, and a project that closes
    at this instant is not ongoing. A strict comparison survived the first mutation pass."""
    conn, config, *_ = series
    (recorded,) = events(conn, "series", "account")
    end = datetime.fromisoformat(recorded["ends"])
    assert follow(conn, config, account_id=ACCOUNT, read=lambda: answer(), now=end) == (
        "series_expired"
    )
    now = utcnow()
    closing = answer(NEXT, close=now.isoformat())
    assert follow(conn, config, account_id=ACCOUNT, read=lambda: closing, now=now) == (
        "not_ongoing"
    )
    assert len(activations(conn)) == 1


def test_a_changed_prompt_is_outside_the_owner_authorization(series: Any) -> None:
    conn, config, *_ = series
    config.forecast.prompt_path.write_bytes(b"a different prompt")
    assert follow(conn, config, account_id=ACCOUNT, read=lambda: answer()) == "bindings_moved"
    assert len(activations(conn)) == 1


def test_an_exhausted_series_binds_nothing(series: Any) -> None:
    conn, config, *_ = series
    (series_id,) = [row["series_id"] for row in events(conn, "series", "account")]
    append(
        conn,
        "cost_reserved",
        f"{ACCOUNT}:{FIRST}",
        {
            "reservation_id": "spent-everything",
            "provider": "openrouter",
            "estimate_microusd": 80_000_000,
            "series_id": series_id,
        },
    )
    assert follow(conn, config, account_id=ACCOUNT, read=lambda: answer()) == "series_exhausted"
    assert len(activations(conn)) == 1


def test_an_owner_enable_during_the_read_supersedes_the_rebind(series: Any) -> None:
    """A reachable race: the owner re-enables while a poll is between its read and append."""
    conn, config, *_ = series

    def read() -> bytes:
        _enable_series(conn, config)
        return answer()

    assert follow(conn, config, account_id=ACCOUNT, read=read) == "superseded"
    assert [row["bound_by"] for row in activations(conn)] == ["owner", "owner"]


# ── the series ceiling ────────────────────────────────────────────────────────


def test_the_series_ceiling_spans_projects_and_ignores_spend_before_the_series(
    series: Any,
) -> None:
    conn, config, *_ = series
    (recorded,) = events(conn, "series", "account")
    series_id = recorded["series_id"]
    # Spend on the first project before the series existed: not the series' to count.
    append(
        conn,
        "cost_reserved",
        f"{ACCOUNT}:{FIRST}",
        {"reservation_id": "before", "provider": "openrouter", "estimate_microusd": 70_000_000},
    )
    first = Budget(
        conn,
        config.storage.artifact_root,
        f"{ACCOUNT}:{FIRST}",
        80_000_000,
        series_id=series_id,
        series_ceiling=1_000_000,
    )
    second = Budget(
        conn,
        config.storage.artifact_root,
        f"{ACCOUNT}:{NEXT}",
        80_000_000,
        series_id=series_id,
        series_ceiling=1_000_000,
    )
    one = first.reserve("openrouter", 0.5, {})
    second.reserve("asknews", 0.4, {})
    assert series_spending(conn, series_id) == (0, 900_000)
    reserved = len(events(conn, "cost_reserved", f"{ACCOUNT}:{NEXT}"))
    with pytest.raises(TournamentError, match="series budget exhausted"):
        second.reserve("asknews", 0.2, {})
    assert len(events(conn, "cost_reserved", f"{ACCOUNT}:{NEXT}")) == reserved
    first.settle(one, 0.1)
    assert series_spending(conn, series_id) == (100_000, 400_000)
    second.reserve("asknews", 0.2, {})
    assert series_spending(conn, series_id) == (100_000, 600_000)


def test_a_poll_stamps_every_reservation_with_its_series(series: Any) -> None:
    conn, _, platform, *_ = series
    platform.open_on = NEXT
    poll(series, answer(NEXT))
    (recorded,) = events(conn, "series", "account")
    rows = events(conn, "cost_reserved", f"{ACCOUNT}:{NEXT}")
    assert rows and all(row["series_id"] == recorded["series_id"] for row in rows)
    assert sum(series_spending(conn, recorded["series_id"])) > 0


def test_a_series_ceiling_reached_mid_poll_refuses_the_purchase(series: Any) -> None:
    """The series spend sits on ANOTHER project, so the project's own $40 is untouched and
    only the series ceiling can refuse. (The first draft seeded it on the polled project
    and passed on the per-project refusal, which is the vacuity this project keeps finding.)
    """
    conn, _, platform, *_ = series
    (recorded,) = events(conn, "series", "account")
    append(
        conn,
        "cost_reserved",
        f"{ACCOUNT}:{NEXT}",
        {
            "reservation_id": "nearly-everything",
            "provider": "openrouter",
            "estimate_microusd": 79_999_000,
            "series_id": recorded["series_id"],
        },
    )
    platform.open_on = FIRST
    result = poll(series, answer(FIRST))
    assert platform.posts == 0
    assert result["heartbeat"]["failures"] == 1
    reasons = [
        row.get("reason")
        for row in events(conn, "question_failure", f"{FIRST}:{platform.raw['question']['id']}")
    ]
    assert reasons == ["series budget exhausted; no provider call made"]
    assert events(conn, "cost_reserved", f"{ACCOUNT}:{FIRST}") == []
    assert result["remaining_budget_usd"] == 40
    assert result["series"]["remaining_budget_usd"] == pytest.approx(0.001)


# ── bindings, status, disable ─────────────────────────────────────────────────


def test_a_policy_bound_project_is_the_destination_of_a_following_config(series: Any) -> None:
    conn, config, *_ = series
    poll(series, answer(NEXT))
    bound = activations(conn)[-1]
    assert retired_bindings(bound, config, account_id=ACCOUNT, project_id=str(NEXT)) == ()
    assert require_activation(conn, config, account_id=ACCOUNT, project_id=str(NEXT)) == bound
    assert "destination" in retired_bindings(
        bound, config, account_id=ACCOUNT, project_id=str(FIRST)
    )


def test_a_pinned_config_still_retires_on_a_moved_project(series: Any, tmp_path: Path) -> None:
    conn, _, *_ = series
    poll(series, answer(NEXT))
    bound = activations(conn)[-1]
    pinned = _config(tmp_path / "pinned", follow=None)
    assert "destination" in retired_bindings(
        bound, pinned, account_id=ACCOUNT, project_id=str(NEXT)
    )


def test_an_activation_without_a_series_is_no_destination_for_a_following_config(
    series: Any,
) -> None:
    conn, config, *_ = series
    owner = dict(activations(conn)[0])
    owner.pop("series_id")
    assert "destination" in retired_bindings(
        owner, config, account_id=ACCOUNT, project_id=str(FIRST)
    )


def test_status_reports_the_series(series: Any) -> None:
    conn, config, *_ = series
    report = status(conn, config)
    assert report["enabled"] is True
    assert report["series"]["ceiling_usd"] == 80
    assert report["series"]["remaining_budget_usd"] == 80
    assert report["series"]["disabled"] is False
    assert report["series"]["bound_by"] == "owner"


def test_a_series_row_the_activation_names_but_the_journal_lacks_stops_the_worker(
    series: Any,
) -> None:
    conn, config, *_ = series
    orphan = dict(activations(conn)[0], series_id="0" * 32)
    with pytest.raises(tournament_state.StorageFailure):
        series_of(conn, orphan)


# ── replay ────────────────────────────────────────────────────────────────────


def test_replay_refuses_an_altered_or_missing_answer(series: Any) -> None:
    conn, config, *_ = series
    poll(series, answer(NEXT))
    bound = activations(conn)[-1]
    evidence = config.storage.artifact_root / bound["evidence"]["path"]
    assert replay_rebind(conn, config, bound)
    assert not replay_rebind(conn, config, dict(bound, project_id=NEXT + 1))
    assert not replay_rebind(
        conn, config, dict(bound, evidence=dict(bound["evidence"], path="../../etc/passwd"))
    )
    # The same project, re-serialized: every re-derived fact still holds, so only the hash
    # can refuse it. (Changing the project id instead let a removed hash check survive.)
    original = evidence.read_bytes()
    evidence.write_bytes(json.dumps(json.loads(original), indent=2).encode())
    assert not replay_rebind(conn, config, bound)
    evidence.write_bytes(original)
    # A readable file with the right hash outside `follow/`: only the path shape refuses it.
    outside = config.storage.artifact_root / "outside.json"
    outside.write_bytes(original)
    assert not replay_rebind(
        conn, config, dict(bound, evidence=dict(bound["evidence"], path="outside.json"))
    )
    assert replay_rebind(conn, config, bound)
    evidence.write_bytes(answer(NEXT + 1))
    assert not replay_rebind(conn, config, bound)
    evidence.unlink()
    assert not replay_rebind(conn, config, bound)


def test_the_stored_answer_round_trips_to_the_same_project(series: Any) -> None:
    body = answer(NEXT)
    parsed = parse_series_project(body)
    assert parsed is not None and parse_series_project(json.dumps(json.loads(body)).encode()) == (
        parsed
    )


# ── no leak ───────────────────────────────────────────────────────────────────


def test_no_metaculus_or_series_value_reaches_a_push_or_the_log(
    series: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    conn, config, platform, *_ = series
    pushes = _recording(monkeypatch, config, config.storage.artifact_root / "push-state")
    caplog.set_level(logging.DEBUG, logger="whiskeyjack_bot.tournament")
    poll(series, answer(NEXT))
    poll(series, answer(NEXT + 7, slug="leaky-slug-value"))
    (recorded,) = events(conn, "series", "account")
    forbidden = [str(NEXT), str(NEXT + 7), str(FIRST), recorded["series_id"], "leaky-slug-value"]
    rendered = [entry["title"] + entry["body"] for entry in pushes.matching("followed")]
    rendered += [
        record.getMessage() for record in caplog.records if "follow" in record.getMessage()
    ]
    assert rendered
    assert not [text for text in rendered for value in forbidden if value in text]


# ── the watchdog ──────────────────────────────────────────────────────────────


def test_the_watchdog_pages_on_a_refused_follow_and_reads_ok_after_a_rebind(
    series: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback clause: a guard refusal leaves M1-347's page firing, unchanged, and a
    successful rebind makes the next watchdog run read OK -- against this very ledger."""
    from tests.unit.test_watchdog import _load

    _, config, *_ = series
    watchdog = _load()
    monkeypatch.setattr(watchdog, "LEDGER", config.storage.sqlite_path)

    def metaculus(path: str, deadline: float) -> object:
        if path.startswith("/projects/"):
            return {"id": NEXT}
        return {"results": [{"status": "open"}]}

    monkeypatch.setattr(watchdog, "_metaculus_get", metaculus)
    poll(series, answer(NEXT, slug="aibq3"))
    refused = watchdog._rollover_observation(time.monotonic() + 30)
    assert refused == (FIRST, NEXT, 1)
    assert watchdog._is_rollover(*refused)
    poll(series, answer(NEXT))
    assert watchdog._rollover_observation(time.monotonic() + 30) == (NEXT, NEXT, None)


# ── the real read ─────────────────────────────────────────────────────────────


def _transport(status_code: int, body: bytes = b"", seen: list[httpx.Request] | None = None) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        headers = {"Location": "https://elsewhere.invalid/"} if 300 <= status_code < 400 else {}
        return httpx.Response(status_code, content=body, headers=headers)

    return httpx.MockTransport(handle)


def test_the_read_asks_for_the_slug_with_a_token_and_an_explicit_agent(
    series: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, config, *_ = series
    monkeypatch.setenv(config.metaculus.token_env, "wj-fake-token-0001")
    seen: list[httpx.Request] = []
    body = answer(NEXT)
    assert read_series_project(config, "minibench", transport=_transport(200, body, seen)) == body
    (request,) = seen
    assert str(request.url).endswith("/projects/tournaments/minibench/")
    assert request.headers["authorization"] == "Token wj-fake-token-0001"
    assert request.headers["user-agent"] == "whiskeyjack-bot"


@pytest.mark.parametrize("status_code", [301, 403, 404, 500])
def test_the_read_refuses_anything_but_a_200_and_never_follows_a_redirect(
    series: Any, monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _, config, *_ = series
    monkeypatch.setenv(config.metaculus.token_env, "wj-fake-token-0001")
    seen: list[httpx.Request] = []
    with pytest.raises(TournamentError) as refused:
        read_series_project(config, "minibench", transport=_transport(status_code, b"x", seen))
    assert len(seen) == 1
    assert "wj-fake-token-0001" not in str(refused.value)


def test_the_read_refuses_an_oversize_answer_and_a_missing_token(
    series: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, config, *_ = series
    monkeypatch.delenv(config.metaculus.token_env, raising=False)
    with pytest.raises(TournamentError, match="token"):
        read_series_project(config, "minibench", transport=_transport(200, answer()))
    monkeypatch.setenv(config.metaculus.token_env, "wj-fake-token-0001")
    huge = b" " * (follow_module.FOLLOW_RESPONSE_LIMIT + 1)
    with pytest.raises(TournamentError, match="too large"):
        read_series_project(config, "minibench", transport=_transport(200, huge))
