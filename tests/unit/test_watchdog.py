"""M1-341: the out-of-process watchdog, read as a contract and run as a measurement.

``deploy/wj-watchdog`` is the only liveness check that survives the program being broken, and
as of this item it is load-bearing for two systemd units rather than one. Five kinds of test:

- **Parity with witnesses outside the script.** The unit names it queries must correspond to
  real files under ``deploy/systemd/``, its own unit must name the tracked script, and its
  throttle must be long against the cadence its own timer declares -- each compared with a file
  the script does not write, so "it watches the resolutions timer" is a comparison rather than a
  restatement of a constant defined here.
- **The criterion, one test per condition.** Disabled, inactive, and a failed last run each
  produce exactly one push naming the schedule, within one watchdog interval.
- **Isolation in both directions.** A stopped resolutions timer must not mute, delay or alter
  the tournament's page, and a dead worker must not mute the resolutions page. They share a
  state file and nothing else.
- **The throttle, driven a day at a time from a fixed instant.** A five-minute watchdog paging
  288 times reports exactly as much as no channel at all.
- **Read-only, and out of reach of AppConfig.** Measured on a real ledger's bytes and on the
  script's own import list, not argued from the source.

The script is loaded by compiling it from source into a fresh module, the idiom
``tests/unit/test_check_backlog.py`` uses and for the reason its comment gives.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from test_deploy_resolutions_unit import only, read_unit, values
from whiskeyjack_bot import tournament_state
from whiskeyjack_bot.ledger import connect, initialize_ledger

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "wj-watchdog"
UNITS = REPO_ROOT / "deploy" / "systemd"
WATCHDOG_SERVICE = UNITS / "whiskeyjack-watchdog.service"
WATCHDOG_TIMER = UNITS / "whiskeyjack-watchdog.timer"

# A fixed instant, never ``utcnow()``. The re-alert window here is rolling rather than
# tumbling, so it has no boundary to straddle -- but T-909 was a test that asserted a page
# count it did not control, and the cheap half of not repeating it is to never start a clock
# at wall-clock time in the first place.
ANCHOR = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


def _load() -> ModuleType:
    # Compiled from source every time, deliberately: importlib's path loading consults
    # __pycache__, whose validation is the source's size plus its mtime at one-second
    # granularity, so a mutation applied, tested and reverted inside one second is served back
    # stale and a surviving mutant reads as a pass. See docs/LESSONS.md lesson 8.
    spec = importlib.util.spec_from_loader("wj_watchdog", loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec"), module.__dict__)
    return module


# ── the fake systemd, and the fake channel ───────────────────────────────────


def healthy_units() -> dict[tuple[str, ...], str]:
    """What `systemctl --user` answers on a host where both schedules are running."""
    return {
        ("is-active", "whiskeyjack-tournament.timer"): "active",
        ("is-enabled", "whiskeyjack-tournament.timer"): "enabled",
        ("is-active", "whiskeyjack-tournament.service"): "inactive",
        ("is-failed", "whiskeyjack-tournament.service"): "inactive",
        ("is-active", "whiskeyjack-resolutions.timer"): "active",
        ("is-enabled", "whiskeyjack-resolutions.timer"): "enabled",
        ("is-failed", "whiskeyjack-resolutions.service"): "inactive",
        ("show", "whiskeyjack-resolutions.service", "-p", "Result", "--value"): "success",
        ("show", "whiskeyjack-resolutions.service", "-p", "ExecMainStatus", "--value"): "0",
    }


@dataclass
class Harness:
    """One loaded watchdog with its systemd, its channel and its clock replaced."""

    module: ModuleType
    units: dict[tuple[str, ...], str]
    pushes: list[dict[str, str]]
    asked: list[tuple[str, ...]] = field(default_factory=list)
    pings: list[datetime] = field(default_factory=list)
    instant: list[datetime] = field(default_factory=lambda: [ANCHOR])
    delivers: list[bool] = field(default_factory=lambda: [True])

    def systemctl(self, *args: str) -> str:
        self.asked.append(args)
        assert args in self.units, f"the watchdog asked an unexpected question: {args}"
        return self.units[args]

    def ping(self) -> None:
        self.pings.append(self.instant[0])

    def push(self, title: str, body: str, *, priority: str, tags: str) -> bool:
        self.pushes.append({"title": title, "body": body, "priority": priority, "tags": tags})
        return self.delivers[0]

    def run(self) -> int:
        return int(self.module.main())

    def titled(self, fragment: str) -> list[dict[str, str]]:
        return [push for push in self.pushes if fragment in push["title"]]

    def state(self) -> dict[str, Any]:
        text = Path(self.module.STATE).read_text(encoding="utf-8")
        loaded = json.loads(text)
        assert isinstance(loaded, dict)
        return loaded


def seeded_ledger(path: Path, *, heartbeat_at: datetime | None) -> None:
    """A real ledger at the real schema, with a heartbeat row written by the real writer.

    Not a hand-rolled table: the watchdog's SELECT has to run against the schema the program
    actually creates, and `tournament_state.append` is what writes the row it reads.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    initialize_ledger(path)
    if heartbeat_at is None:
        return
    connection = connect(path)
    try:
        original = tournament_state.utcnow
        tournament_state.utcnow = lambda: heartbeat_at  # type: ignore[assignment]
        try:
            tournament_state.append(connection, "heartbeat", "worker", {"complete": True})
        finally:
            tournament_state.utcnow = original  # type: ignore[assignment]
    finally:
        connection.close()


@pytest.fixture()
def watchdog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    ledger = tmp_path / "data" / "whiskeyjack_bot.sqlite3"
    seeded_ledger(ledger, heartbeat_at=ANCHOR - timedelta(minutes=2))
    module = _load()
    harness = Harness(module=module, units=healthy_units(), pushes=[])
    monkeypatch.setattr(module, "STATE", tmp_path / "wj-watchdog.json")
    monkeypatch.setattr(module, "LEDGER", ledger)
    monkeypatch.setattr(module, "_systemctl", harness.systemctl)
    monkeypatch.setattr(module, "_push", harness.push)
    monkeypatch.setattr(module, "_ping_deadman", harness.ping)
    monkeypatch.setattr(module, "_now", lambda: harness.instant[0])
    return harness


# ── parity with witnesses outside the script ─────────────────────────────────


def test_every_unit_the_watchdog_names_is_a_unit_this_repo_ships() -> None:
    """A rename cannot leave either check silently pointed at nothing.

    `is-active` on a unit that does not exist answers `inactive`, which is indistinguishable
    from a unit that exists and is stopped. So a typo or a rename in `RESOLUTIONS_UNIT` does
    not fail loudly -- it pages forever about a unit nobody has, and the operator who goes
    looking finds the timer running. The tracked unit files are the witness.
    """
    module = _load()
    for unit in (module.UNIT, module.RESOLUTIONS_UNIT):
        assert (UNITS / f"{unit}.service").is_file(), unit
        assert (UNITS / f"{unit}.timer").is_file(), unit
    assert module.RESOLUTIONS_UNIT != module.UNIT
    # The resolutions unit the watchdog names is the one that actually ingests and scores.
    resolutions = read_unit(UNITS / f"{module.RESOLUTIONS_UNIT}.service")
    assert len(values(resolutions, "Service", "ExecStart")) == 2
    assert only(read_unit(UNITS / f"{module.RESOLUTIONS_UNIT}.timer"), "Timer", "Unit") == (
        f"{module.RESOLUTIONS_UNIT}.service"
    )


def test_the_watchdogs_own_unit_runs_the_tracked_script_and_pages_through_nothing() -> None:
    service = read_unit(WATCHDOG_SERVICE)
    exec_start = only(service, "Service", "ExecStart")
    assert Path(exec_start).name == SCRIPT.name, "the installed name must match the tracked name"
    # Deliberately no OnFailure: a notifier launched by this unit's own failure is not
    # independent evidence about this unit. The script's docstring says so; pin it.
    assert values(service, "Unit", "OnFailure") == []
    timer = read_unit(WATCHDOG_TIMER)
    assert only(timer, "Timer", "Unit") == WATCHDOG_SERVICE.name
    assert only(timer, "Timer", "OnCalendar") == "*:2/5"
    assert only(timer, "Timer", "Persistent") == "true"
    assert only(timer, "Install", "WantedBy") == "timers.target"


def test_the_resolutions_throttle_is_long_against_the_cadence_its_own_timer_declares() -> None:
    """288 pages a day is a muted channel. The bound is read off the timer, not asserted here.

    `OnCalendar=*:2/5` is one run every five minutes, so the re-alert window is what stands
    between a condition that holds until a person acts and a page per run. Two per day is the
    ceiling this asserts; the shipped value is one.
    """
    module = _load()
    calendar = only(read_unit(WATCHDOG_TIMER), "Timer", "OnCalendar")
    minute_field = calendar.split(":")[1]
    cadence = timedelta(minutes=int(minute_field.split("/")[1]))
    runs_per_day = timedelta(days=1) / cadence
    assert runs_per_day == 288
    pages_per_day = timedelta(days=1) / module.RESOLUTIONS_REALERT_AFTER
    assert pages_per_day <= 2, "a stopped timer must not page once per watchdog run"


def test_the_declared_worst_case_run_fits_inside_the_deadline_its_own_unit_declares() -> None:
    """A run systemd kills part-way reports the tournament and never the schedule.

    **Round-1 blocking finding.** Every outward call carries its own timeout, and the
    tournament half is checked first, so if the total budget exceeds `TimeoutStartSec` what
    gets cut is always the resolutions page — the thing this item exists to send. Before
    M1-341 the script made four systemctl queries and at most one push (75s, inside the 120s
    the unit declared); it now makes nine and up to three, which is 190s and was not. The
    deadline is read out of the tracked unit rather than restated here, so the constant and
    the unit cannot drift apart, and the timer's own interval bounds the other end: a hung run
    must be dead before the next one is due.
    """
    module = _load()
    service = read_unit(WATCHDOG_SERVICE)
    deadline = int(only(service, "Service", "TimeoutStartSec"))
    calendar = only(read_unit(WATCHDOG_TIMER), "Timer", "OnCalendar")
    interval = int(calendar.split(":")[1].split("/")[1]) * 60

    assert module.WORST_CASE_SECONDS < deadline, (module.WORST_CASE_SECONDS, deadline)
    assert deadline < interval, "a hung run must not still be alive when the next one starts"
    # And the arithmetic is over the numbers the code actually passes, not over a copy.
    assert module.WORST_CASE_SECONDS == (
        9 * module.SYSTEMCTL_TIMEOUT_SECONDS
        + module.LEDGER_TIMEOUT_SECONDS
        + 2 * module.PUSH_TIMEOUT_SECONDS
        + module.DEADMAN_TIMEOUT_SECONDS
    )


def test_every_outward_call_passes_the_timeout_the_budget_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget is only a bound if the code really passes those numbers.

    **Round-2 blocking finding, and it was mine.** Naming the timeouts as constants, I
    substituted one shared `HTTP_TIMEOUT_SECONDS` into both `urlopen` sites — which silently
    took the dead-man ping from 10 seconds to 15, a change to tournament behaviour this item
    promised not to make, in the same commit whose message claimed every substitution kept its
    value. Nothing observed the timeouts, so nothing caught it. This does: it drives a full run
    with the real `_push` and `_ping_deadman` and records what each outward call was given.
    """
    module = _load()
    seen: dict[str, object] = {}

    class _Completed:
        stdout = "inactive"

    def fake_run(argv: object, **kwargs: Any) -> _Completed:
        seen["systemctl"] = kwargs["timeout"]
        return _Completed()

    ledger = tmp_path / "data" / "l.sqlite3"
    seeded_ledger(ledger, heartbeat_at=ANCHOR - timedelta(hours=3))
    real_connect = sqlite3.connect

    def fake_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        seen["ledger"] = kwargs["timeout"]
        return real_connect(*args, **kwargs)

    class _Response:
        status = 200

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def close(self) -> None:
            return None

    def fake_urlopen(target: Any, timeout: int | None = None) -> _Response:
        # A push sends a Request; the dead-man ping sends a bare URL string.
        seen["push" if hasattr(target, "get_method") else "ping"] = timeout
        return _Response()

    monkeypatch.setattr(module, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(module, "LEDGER", ledger)
    monkeypatch.setattr(module, "_now", lambda: ANCHOR)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("NTFY_TOPIC_URL", "https://ntfy.invalid/wj-fake-topic-0001")

    assert module.main() == 1, "everything is inactive, so both subjects are down"
    assert seen["systemctl"] == module.SYSTEMCTL_TIMEOUT_SECONDS
    assert seen["ledger"] == module.LEDGER_TIMEOUT_SECONDS
    assert seen["push"] == module.PUSH_TIMEOUT_SECONDS

    # The ping only fires on the healthy tournament path, so drive that separately.
    seen.clear()
    monkeypatch.setenv("WJ_HEALTHCHECK_URL", "https://hc.invalid/wj-fake-ping-0001")
    module._ping_deadman()
    assert seen["ping"] == module.DEADMAN_TIMEOUT_SECONDS
    assert module.DEADMAN_TIMEOUT_SECONDS == 10, "the value the vendored script passed"
    assert module.DEADMAN_TIMEOUT_SECONDS != module.PUSH_TIMEOUT_SECONDS, (
        "they are separate constants precisely so one cannot be substituted for the other"
    )


@pytest.mark.parametrize(
    ("stamp", "why"),
    [
        ([1], "a list"),
        (3, "an integer"),
        (1.5, "a float"),
        ({"a": 1}, "a nested object"),
        ("2026-09-17T12:00:00", "naive -- it parses, then fails at the subtraction"),
        (True, "a bool, which is not a str"),
    ],
)
def test_an_unusable_tournament_stamp_does_not_stop_the_resolutions_check(
    watchdog: Harness, stamp: object, why: str
) -> None:
    """**Round-2 blocking finding**, reproduced by execution at `624da85` before the fix.

    The state file is a valid JSON *object* here — round 1's fix does not reach this — and the
    tournament block's own parser guarded only `ValueError` on a value it had truthiness-tested.
    Four shapes escaped as a raw `TypeError` out of `main`, with no resolutions query asked and
    no push attempted.

    The naive-ISO case is the sibling the review did **not** name: it parses perfectly and dies
    at `now - last` instead. Enumerating siblings by execution rather than fixing the one named
    type is M1-308's lesson; all six shapes are driven through a full `main()` run.
    """
    Path(watchdog.module.STATE).write_text(
        json.dumps({"alerting": True, "last_alert": stamp}), encoding="utf-8"
    )
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"

    assert watchdog.run() == 1, why
    assert watchdog.asked.count(("is-active", "whiskeyjack-resolutions.timer")) == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1


@pytest.mark.parametrize(
    ("persisted", "why"),
    [
        ("2026-09-17T11:55:00", "naive -- it parses, then fails at the subtraction"),
        ("20260917", "an integer written into the TEXT column; affinity makes it basic-format"),
        ("not a timestamp", "unparseable"),
        ("", "empty"),
    ],
)
def test_an_unusable_persisted_heartbeat_does_not_stop_the_resolutions_check(
    watchdog: Harness, persisted: str, why: str
) -> None:
    """**Round-3 blocking finding**, reproduced by execution at `e8573e9` before the fix.

    `_last_heartbeat` had a **third** stamp parser, guarding only `ValueError`. `created_at_utc`
    is `TEXT NOT NULL`, but SQLite is dynamically typed and applies TEXT affinity, so the column
    holds whatever a writer put there — and `CLAUDE.md` classifies values read back out of the
    ledger as untrusted in as many words. A naive timestamp parses fine and then raises
    `TypeError` at the caller, out of `main`, taking the resolutions check down with it because
    it runs second.

    Driven against a **real ledger at the real schema**, written by the real writer and then
    updated, because the existing tests seed heartbeats through `tournament_state.append`, which
    always emits an aware timestamp — which is exactly why nothing here saw this for two rounds.
    """
    # An INSERT, not an UPDATE: `012`'s `tournament_events_no_update` trigger refuses the
    # update outright ("tournament events are append-only"), which is worth knowing because it
    # narrows the reachable path to exactly this -- a writer appending a row whose timestamp is
    # not what this reader assumes. Found by execution while writing this test.
    ledger = Path(watchdog.module.LEDGER)
    connection = sqlite3.connect(ledger)
    try:
        connection.execute(
            "INSERT INTO tournament_events(event_id, kind, scope, data, created_at_utc) "
            "VALUES ('later-heartbeat', 'heartbeat', 'worker', '{}', ?)",
            (persisted,),
        )
        connection.commit()
    finally:
        connection.close()
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"

    assert watchdog.run() == 1, why
    assert watchdog.asked.count(("is-active", "whiskeyjack-resolutions.timer")) == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1
    # And the worker page says something true about what it found.
    assert (
        "no usable heartbeat timestamp in the ledger" in watchdog.titled("WORKER DOWN")[0]["body"]
    )


def test_the_problem_codes_and_their_prose_are_one_closed_vocabulary() -> None:
    """Every code the detector can emit has a sentence; the push body indexes by code."""
    module = _load()
    reachable = set()
    for active in ("active", "inactive"):
        for enabled in ("enabled", "disabled"):
            for failed in ("failed", "inactive"):
                reachable.update(module._resolutions_problems(active, enabled, failed))
    assert reachable == set(module.RESOLUTIONS_PROBLEMS)


# ── the criterion, one test per condition ────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "answer", "code"),
    [
        (("is-active", "whiskeyjack-resolutions.timer"), "inactive", "timer_inactive"),
        (("is-enabled", "whiskeyjack-resolutions.timer"), "disabled", "timer_disabled"),
        (("is-failed", "whiskeyjack-resolutions.service"), "failed", "service_failed"),
    ],
)
def test_each_stopped_condition_pages_once_on_the_next_watchdog_run(
    watchdog: Harness, query: tuple[str, ...], answer: str, code: str
) -> None:
    """One run of the watchdog -- five minutes -- is all it takes for each of the three."""
    watchdog.units[query] = answer

    assert watchdog.run() == 1
    pages = watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")
    assert len(pages) == 1
    assert watchdog.module.RESOLUTIONS_PROBLEMS[code] in pages[0]["body"]
    assert "whiskeyjack-resolutions.timer" in pages[0]["body"]
    assert watchdog.state()["resolutions"]["key"] == code
    # The tournament is healthy throughout and says nothing.
    assert watchdog.titled("WORKER DOWN") == []


def test_a_timer_that_is_both_stopped_and_disabled_pages_once_naming_both(
    watchdog: Harness,
) -> None:
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    watchdog.units[("is-enabled", "whiskeyjack-resolutions.timer")] = "disabled"

    assert watchdog.run() == 1
    pages = watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")
    assert len(pages) == 1
    assert watchdog.module.RESOLUTIONS_PROBLEMS["timer_inactive"] in pages[0]["body"]
    assert watchdog.module.RESOLUTIONS_PROBLEMS["timer_disabled"] in pages[0]["body"]
    assert watchdog.state()["resolutions"]["key"] == "timer_disabled,timer_inactive"


def test_the_failed_run_page_says_why_it_failed(watchdog: Harness) -> None:
    """Result and ExecMainStatus reach the body, which is the whole of what they are for."""
    watchdog.units[("is-failed", "whiskeyjack-resolutions.service")] = "failed"
    watchdog.units[("show", "whiskeyjack-resolutions.service", "-p", "Result", "--value")] = (
        "exit-code"
    )
    watchdog.units[
        ("show", "whiskeyjack-resolutions.service", "-p", "ExecMainStatus", "--value")
    ] = "4"

    assert watchdog.run() == 1
    body = watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")[0]["body"]
    assert "result=exit-code exit=4" in body


def test_a_healthy_schedule_is_silent_and_the_run_exits_zero(watchdog: Harness) -> None:
    assert watchdog.run() == 0
    assert watchdog.pushes == []
    assert watchdog.state()["resolutions"] == {}


def test_a_restarted_timer_clears_the_condition_and_says_so_once(watchdog: Harness) -> None:
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1

    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "active"
    assert watchdog.run() == 0
    assert len(watchdog.titled("resolutions schedule recovered")) == 1
    # And the recovery is announced once, not on every later healthy run.
    watchdog.instant[0] += timedelta(minutes=5)
    assert watchdog.run() == 0
    assert len(watchdog.titled("resolutions schedule recovered")) == 1


# ── isolation in both directions ─────────────────────────────────────────────


def test_a_stopped_resolutions_timer_does_not_mute_or_delay_the_worker_page(
    watchdog: Harness,
) -> None:
    """The two subjects share a state file and nothing else.

    Folded into one `problems` list they would share one `last_alert`, and the first fault of
    the day would buy the other one an hour of silence. Driven as two separate faults arriving
    in the same run, with the worker's own page asserted intact.
    """
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    watchdog.units[("is-active", "whiskeyjack-tournament.timer")] = "inactive"

    assert watchdog.run() == 1
    assert len(watchdog.titled("WORKER DOWN")) == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1
    worker_body = watchdog.titled("WORKER DOWN")[0]["body"]
    assert "whiskeyjack-resolutions" not in worker_body


def test_a_dead_worker_does_not_stop_the_resolutions_check_from_running(
    watchdog: Harness,
) -> None:
    """The check that comes second must not be skipped by the first one's early return.

    The day the 2026-09-09 outage repeats is exactly the day the other half must still be
    reported, so this is driven with the worker down and the resolutions timer disabled.
    """
    watchdog.units[("is-enabled", "whiskeyjack-tournament.timer")] = "disabled"
    watchdog.units[("is-enabled", "whiskeyjack-resolutions.timer")] = "disabled"

    assert watchdog.run() == 1
    assert watchdog.asked.count(("is-enabled", "whiskeyjack-resolutions.timer")) == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1


def test_the_worker_recovering_says_nothing_about_a_still_stopped_schedule(
    watchdog: Harness,
) -> None:
    """A shared `alerting` flag would announce "worker recovered" with half the rig stopped."""
    watchdog.units[("is-active", "whiskeyjack-tournament.timer")] = "inactive"
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    assert watchdog.run() == 1

    watchdog.units[("is-active", "whiskeyjack-tournament.timer")] = "active"
    watchdog.instant[0] += timedelta(minutes=5)
    assert watchdog.run() == 1, "the resolutions timer is still stopped"
    assert len(watchdog.titled("worker recovered")) == 1
    assert watchdog.titled("resolutions schedule recovered") == []


# ── the throttle ─────────────────────────────────────────────────────────────


def test_a_stopped_timer_left_stopped_for_a_day_pages_once_not_two_hundred_and_eighty_eight(
    watchdog: Harness,
) -> None:
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"

    for _ in range(288):
        assert watchdog.run() == 1
        watchdog.instant[0] += timedelta(minutes=5)

    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1


def test_a_condition_still_standing_after_the_window_pages_again(watchdog: Harness) -> None:
    """The silence is the window, not the condition going away -- so it must end on its own."""
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    assert watchdog.run() == 1

    watchdog.instant[0] += watchdog.module.RESOLUTIONS_REALERT_AFTER - timedelta(seconds=1)
    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1

    watchdog.instant[0] += timedelta(seconds=1)
    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 2


def test_a_new_fault_inside_the_window_pages_at_once(watchdog: Harness) -> None:
    """A different fault set has never been paged, and must not inherit another's throttle."""
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1

    watchdog.instant[0] += timedelta(minutes=5)
    watchdog.units[("is-failed", "whiskeyjack-resolutions.service")] = "failed"
    assert watchdog.run() == 1
    pages = watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")
    assert len(pages) == 2
    assert watchdog.module.RESOLUTIONS_PROBLEMS["service_failed"] in pages[1]["body"]


def test_a_push_the_channel_refused_is_retried_on_the_next_run(watchdog: Harness) -> None:
    """Only a push that landed stamps the throttle; a dead channel must not buy a day's quiet."""
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    watchdog.delivers[0] = False

    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1
    assert "last_alert" not in watchdog.state()["resolutions"]

    watchdog.instant[0] += timedelta(minutes=5)
    watchdog.delivers[0] = True
    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 2
    assert "last_alert" in watchdog.state()["resolutions"]


def test_a_refused_push_about_a_new_fault_does_not_inherit_the_old_faults_quiet(
    watchdog: Harness,
) -> None:
    """The stamp is carried forward only while the fault set is unchanged.

    **Found by mutation, not by design.** Dropping the `stored.get("key") == key` half of that
    guard survived every other test here, because on the ordinary path a successful push
    overwrites the carried stamp with `now` and nothing is observable. It is observable when the
    push does *not* land: the new fault set would inherit the old set's stamp, be judged inside
    a window it was never paged in, and go unmentioned for up to a day. So the case is a refused
    push arriving exactly at a change of fault set.
    """
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1

    watchdog.instant[0] += timedelta(minutes=5)
    watchdog.units[("is-failed", "whiskeyjack-resolutions.service")] = "failed"
    watchdog.delivers[0] = False
    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 2, "the attempt is made"
    assert "last_alert" not in watchdog.state()["resolutions"], "and it did not land"

    watchdog.instant[0] += timedelta(minutes=5)
    watchdog.delivers[0] = True
    assert watchdog.run() == 1
    pages = watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")
    assert len(pages) == 3, "the new fault is still owed a page that landed"
    assert watchdog.module.RESOLUTIONS_PROBLEMS["service_failed"] in pages[2]["body"]
    assert watchdog.state()["resolutions"]["key"] == "service_failed,timer_inactive"


@pytest.mark.parametrize("contents", ["[]", "null", '"text"', "3", "{", ""])
def test_a_state_file_that_is_not_an_object_still_reports_a_stopped_schedule(
    watchdog: Harness, contents: str
) -> None:
    """**Round-1 blocking finding**, reproduced by execution at `3dda485` before the fix.

    `[]`, `null`, `"text"` and `3` are all valid JSON, so `_load_state`'s bare `json.loads`
    returned them and the first `state.get(...)` raised `AttributeError` out of `main` — with
    no resolutions query asked and no push attempted. It takes down **both** subjects, and it
    does it silently, because the watchdog is the one unit deliberately without an `OnFailure`
    pager. A hand-edited state file is an ordinary operator action.

    `{` and `""` are the shapes that already worked (the parse itself fails); they are drawn
    here so the test is about the top-level *shape*, not about the parse.
    """
    Path(watchdog.module.STATE).write_text(contents, encoding="utf-8")
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"

    assert watchdog.run() == 1
    assert watchdog.asked.count(("is-active", "whiskeyjack-resolutions.timer")) == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1
    assert watchdog.state()["resolutions"]["key"] == "timer_inactive"


@pytest.mark.parametrize(
    "stored",
    ["not a dict", ["not a dict"], {"key": "timer_inactive", "last_alert": "not a timestamp"}],
)
def test_an_unusable_state_file_pages_rather_than_staying_quiet(
    watchdog: Harness, stored: object
) -> None:
    """Every unreadable shape means "no record that anyone was told", and the answer is to tell.

    A person editing this file by hand is the reachable case; the bad direction to fail is
    silence, because a throttle that trusts a corrupt stamp is a throttle that never expires.
    """
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    Path(watchdog.module.STATE).write_text(json.dumps({"resolutions": stored}), encoding="utf-8")

    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1


def test_a_naive_timestamp_in_the_state_file_pages_rather_than_raising(
    watchdog: Harness,
) -> None:
    """`fromisoformat` parses it; subtracting it from an aware `now` is a TypeError."""
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    Path(watchdog.module.STATE).write_text(
        json.dumps({"resolutions": {"key": "timer_inactive", "last_alert": "2026-09-17T12:00:00"}}),
        encoding="utf-8",
    )

    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1


def test_the_dead_man_ping_answers_for_the_worker_and_not_for_the_schedule(
    watchdog: Harness,
) -> None:
    """A stopped resolutions timer must not withhold the ping. Also found by mutation.

    `WJ_HEALTHCHECK_URL` is the only cover for a dark host -- machine off, session gone -- and
    what it reports is "this watchdog is not running at all". Gating it on the resolutions
    check as well would make a stopped timer raise that second alarm too, in an external
    service's wording, about something it is not about. So the gate stays the tournament
    result, and this pins all three cases rather than leaving the choice asserted only in a
    comment. Deleting the ping outright survived every other test in this file.
    """
    assert watchdog.run() == 0
    assert len(watchdog.pings) == 1, "healthy: the watchdog reports itself alive"

    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    watchdog.instant[0] += timedelta(minutes=5)
    assert watchdog.run() == 1
    assert len(watchdog.pings) == 2, "the schedule is stopped; the watchdog is still alive"

    watchdog.units[("is-active", "whiskeyjack-tournament.timer")] = "inactive"
    watchdog.instant[0] += timedelta(minutes=5)
    assert watchdog.run() == 1
    assert len(watchdog.pings) == 2, "the worker is down; the dead-man may fall silent"


# ── the tournament checks, characterized ─────────────────────────────────────


def test_the_tournament_rules_are_what_they_were(watchdog: Harness) -> None:
    """A characterization test, named as one.

    The *parity* evidence that this item changed nothing in the poll's checks is the diff from
    the commit that vendored the script verbatim: the only lines removed are `def main`, two
    `_save_state` calls, one `_load_state` and two integer returns. This pins the rules
    themselves so a later edit has to be deliberate.
    """
    module = watchdog.module
    assert module.UNIT == "whiskeyjack-tournament"
    assert module.STALE_AFTER == timedelta(minutes=20)
    assert module.REALERT_AFTER == timedelta(minutes=60)

    assert watchdog.run() == 0
    assert sorted(query for query in watchdog.asked if "tournament" in query[1]) == [
        ("is-active", "whiskeyjack-tournament.service"),
        ("is-active", "whiskeyjack-tournament.timer"),
        ("is-enabled", "whiskeyjack-tournament.timer"),
        ("is-failed", "whiskeyjack-tournament.service"),
    ]
    assert watchdog.state()["alerting"] is False


def test_a_stale_heartbeat_is_not_a_fault_while_the_poll_is_still_running(
    watchdog: Harness,
) -> None:
    """A poll may legitimately run for tens of minutes; systemd starts no second instance."""
    watchdog.instant[0] = ANCHOR + timedelta(hours=3)
    watchdog.units[("is-active", "whiskeyjack-tournament.service")] = "active"
    assert watchdog.run() == 0
    assert watchdog.pushes == []

    watchdog.units[("is-active", "whiskeyjack-tournament.service")] = "inactive"
    assert watchdog.run() == 1
    assert len(watchdog.titled("WORKER DOWN")) == 1


def test_a_worker_fault_repeats_on_the_tournaments_own_hourly_window(watchdog: Harness) -> None:
    """Sixty minutes, unchanged -- and independent of the resolutions day-long window."""
    watchdog.units[("is-active", "whiskeyjack-tournament.timer")] = "inactive"
    assert watchdog.run() == 1

    watchdog.instant[0] += timedelta(minutes=59)
    assert watchdog.run() == 1
    assert len(watchdog.titled("WORKER DOWN")) == 1

    watchdog.instant[0] += timedelta(minutes=1)
    assert watchdog.run() == 1
    assert len(watchdog.titled("WORKER DOWN")) == 2


# ── read-only, and out of reach of AppConfig ─────────────────────────────────


def test_a_full_run_of_both_checks_leaves_the_ledger_byte_identical(watchdog: Harness) -> None:
    """Measured on the file, not argued from `mode=ro` in the connection string.

    What a read-only open of a WAL database *does* touch, measured rather than assumed: it
    creates a 32 KiB ``-shm`` and a **zero-length** ``-wal`` beside the ledger if they are not
    already there. That is SQLite's read-path shared-memory index, not a ledger write -- no WAL
    frame is appended, and the database file's bytes are identical. On the live host the worker
    already holds both open, so the watchdog creates neither. Asserted here rather than
    excluded, because "the watchdog writes nothing at all" would be a claim this measurement
    does not support.
    """
    ledger = Path(watchdog.module.LEDGER)
    before = hashlib.sha256(ledger.read_bytes()).hexdigest()

    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    watchdog.units[("is-active", "whiskeyjack-tournament.timer")] = "inactive"
    assert watchdog.run() == 1

    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == before
    beside = {path.name: path.stat().st_size for path in ledger.parent.iterdir()}
    assert set(beside) <= {ledger.name, f"{ledger.name}-wal", f"{ledger.name}-shm"}, beside
    assert beside.get(f"{ledger.name}-wal", 0) == 0, "a WAL frame would be a write"


def test_the_resolutions_check_does_not_open_the_ledger_at_all(
    watchdog: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It has no heartbeat rule, so it has no reason to; a connection here would be new risk."""
    opened: list[str] = []
    real_connect = sqlite3.connect

    def spy(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(args[0]))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(watchdog.module.sqlite3, "connect", spy)
    state: dict[str, Any] = {}
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"

    assert watchdog.module._check_resolutions(state) is True
    assert opened == []
    # The tournament check is the one that reads, and it reads read-only.
    assert watchdog.module._check_tournament(state) is False
    assert len(opened) == 1 and "mode=ro" in opened[0]


def test_the_watchdog_imports_nothing_from_the_package_it_watches() -> None:
    """Its value is surviving a broken program, and AppConfig is the thing it must not reach.

    Any new AppConfig field changes `config_sha256` and retires both live activations -- the
    2h33m outage of 2026-09-09. A watchdog that cannot import the package cannot add one.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert "whiskeyjack_bot" not in imported
    # Standard library only: no third-party client, so no venv to be broken either.
    assert imported <= {
        "__future__",
        "datetime",
        "json",
        "os",
        "pathlib",
        "sqlite3",
        "subprocess",
        "sys",
        "urllib",
    }, sorted(imported)


def test_no_push_carries_an_environment_value(
    watchdog: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diagnostics name env vars, never their values (project rule; see CLAUDE.md)."""
    monkeypatch.setenv("NTFY_TOPIC_URL", "https://ntfy.invalid/wj-secret-topic-0001")
    monkeypatch.setenv("WJ_HEALTHCHECK_URL", "https://hc.invalid/wj-secret-ping-0001")
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    watchdog.units[("is-active", "whiskeyjack-tournament.timer")] = "inactive"

    assert watchdog.run() == 1
    assert watchdog.pushes
    for push in watchdog.pushes:
        for field_value in push.values():
            assert "wj-secret-topic-0001" not in field_value
            assert "wj-secret-ping-0001" not in field_value
