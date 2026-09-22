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
import os
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


def rendered(moment: datetime) -> str:
    """A UTC instant the way `systemctl show` renders one under `TZ=UTC LC_ALL=C`.

    Measured on the live host at systemd 255, not invented: `Sat 2026-09-19 12:23:18 UTC`.
    """
    return moment.strftime("%a %Y-%m-%d %H:%M:%S UTC")


# Nine hours before the anchor -- stale enough to page -- rendered the two ways a stamp can be
# unusable while looking right: the host's own local zone (what an un-pinned TZ produces) and no
# zone at all. Anchor-relative on purpose: a future-dated example is refused by the arithmetic,
# so it would pass whether or not the zone is checked.
STALE_IN_LOCAL_ZONE = (ANCHOR - timedelta(hours=9)).strftime("%a %Y-%m-%d %H:%M:%S MDT")
STALE_WITHOUT_ZONE = (ANCHOR - timedelta(hours=9)).strftime("%a %Y-%m-%d %H:%M:%S")

LAST_TRIGGER = ("show", "whiskeyjack-resolutions.timer", "-p", "LastTriggerUSec", "--value")
ACTIVE_ENTER = ("show", "whiskeyjack-resolutions.timer", "-p", "ActiveEnterTimestamp", "--value")


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
        # Fired an hour ago, and active for a month -- a timer doing exactly what it declares.
        LAST_TRIGGER: rendered(ANCHOR - timedelta(hours=1)),
        ACTIVE_ENTER: rendered(ANCHOR - timedelta(days=30)),
    }


@dataclass
class Harness:
    """One loaded watchdog with its systemd, its channel and its clock replaced."""

    module: ModuleType
    units: dict[tuple[str, ...], str]
    pushes: list[dict[str, str]]
    asked: list[tuple[str, ...]] = field(default_factory=list)
    envs: list[dict[str, str] | None] = field(default_factory=list)
    pings: list[datetime] = field(default_factory=list)
    instant: list[datetime] = field(default_factory=lambda: [ANCHOR])
    delivers: list[bool] = field(default_factory=lambda: [True])
    # The Metaculus boundary (M1-347), faked at `_metaculus_get` and nowhere deeper, so the
    # thread, the join, the parsers and the decision are all the real code. Each answer is a
    # parsed payload, or an exception instance to raise in its place.
    project: list[object] = field(default_factory=lambda: [{"id": ACTIVE_PROJECT}])
    posts: list[object] = field(default_factory=lambda: [{"results": [{"status": "open"}]}])
    fetched: list[str] = field(default_factory=list)
    real_metaculus_get: Any = None

    def systemctl(self, *args: str, env: dict[str, str] | None = None) -> str:
        # `env` is recorded, not honoured: what it must CONTAIN is asserted against the real
        # `subprocess.run` in `test_the_timestamp_queries_ask_in_utc_and_the_others_are_unchanged`,
        # which is the only place that can see it.
        self.asked.append(args)
        self.envs.append(env)
        assert args in self.units, f"the watchdog asked an unexpected question: {args}"
        return self.units[args]

    def watch_since(self, moment: datetime, *, last_seen: datetime | None = None) -> None:
        """Seed the watchdog's own observation record (M1-343).

        `moment` is when the unbroken run of observations began. `last_seen` defaults to one
        watchdog interval ago, which is what an unbroken record looks like; passing an older
        instant is how a test says "this host was off in between".
        """
        seen = self.instant[0] - timedelta(minutes=5) if last_seen is None else last_seen
        path = Path(self.module.STATE)
        stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        stored["observed"] = {"since": moment.isoformat(), "last_seen": seen.isoformat()}
        path.write_text(json.dumps(stored), encoding="utf-8")

    def ping(self) -> None:
        self.pings.append(self.instant[0])

    def push(
        self, title: str, body: str, *, priority: str, tags: str, timeout: int | None = None
    ) -> bool:
        self.pushes.append({"title": title, "body": body, "priority": priority, "tags": tags})
        return self.delivers[0]

    def metaculus(self, path: str, deadline: float) -> object:
        self.fetched.append(path)
        answer = self.project[0] if path.startswith("/projects/") else self.posts[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def activate(self, project_id: object) -> None:
        """Append one activation event, newest wins -- what `tournament enable` leaves."""
        seed_activation(Path(self.module.LEDGER), project_id)

    def run(self) -> int:
        return int(self.module.main())

    def titled(self, fragment: str) -> list[dict[str, str]]:
        return [push for push in self.pushes if fragment in push["title"]]

    def state(self) -> dict[str, Any]:
        text = Path(self.module.STATE).read_text(encoding="utf-8")
        loaded = json.loads(text)
        assert isinstance(loaded, dict)
        return loaded


# The project the fixture's live activation is bound to, and what the fake Metaculus says the
# `minibench` slug resolves to unless a test says otherwise. 33125 is the real current series.
ACTIVE_PROJECT = 33125


def seed_activation(path: Path, project_id: object) -> None:
    """An `activation` event through the real journal writer, carrying only what is read.

    `tournament_state.enable` would need a whole AppConfig to reach the same row; the watchdog
    reads one key of it, and `append` is the writer every activation passes through.
    """
    connection = connect(path)
    try:
        tournament_state.append(
            connection, "activation", "account", {"project_id": project_id, "account_id": 1}
        )
    finally:
        connection.close()


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
    seed_activation(ledger, ACTIVE_PROJECT)
    module = _load()
    harness = Harness(module=module, units=healthy_units(), pushes=[])
    harness.real_metaculus_get = module._metaculus_get
    monkeypatch.setattr(module, "STATE", tmp_path / "wj-watchdog.json")
    monkeypatch.setattr(module, "LEDGER", ledger)
    monkeypatch.setattr(module, "_systemctl", harness.systemctl)
    monkeypatch.setattr(module, "_metaculus_get", harness.metaculus)
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
    the unit declared); it made nine and up to three at M1-341, which is 190s and was not, and
    makes eleven at M1-343 (the two timestamp queries the stall rule adds), which is 215s. The
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
    # M1-347 adds the rollover check's wall-clock read bound and its one push.
    assert module.WORST_CASE_SECONDS == (
        11 * module.SYSTEMCTL_TIMEOUT_SECONDS
        + module.LEDGER_TIMEOUT_SECONDS
        + 2 * module.PUSH_TIMEOUT_SECONDS
        + module.DEADMAN_TIMEOUT_SECONDS
        + module.ROLLOVER_FETCH_SECONDS
        + module.ROLLOVER_PUSH_TIMEOUT_SECONDS
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

    ledger_timeouts: list[float] = []
    push_timeouts: dict[str, object] = {}

    def fake_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        ledger_timeouts.append(kwargs["timeout"])
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
        # A push sends a Request; the dead-man ping sends a bare URL string. Pushes are told
        # apart by title, because the rollover check's push passes its own shorter timeout.
        if hasattr(target, "get_method"):
            push_timeouts[target.get_header("Title")] = timeout
        else:
            seen["ping"] = timeout
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
    # The heartbeat read, then the rollover check's activation read -- which is also bounded by
    # its thread join, so it may pass less than the ledger timeout and never more.
    assert ledger_timeouts[0] == module.LEDGER_TIMEOUT_SECONDS
    assert len(ledger_timeouts) == 2 and 0 < ledger_timeouts[1] <= module.LEDGER_TIMEOUT_SECONDS
    # This ledger has no activation, so the rollover check could not run and says so.
    assert push_timeouts == {
        "whiskeyjack: WORKER DOWN": module.PUSH_TIMEOUT_SECONDS,
        "whiskeyjack: RESOLUTIONS SCHEDULE STOPPED": module.PUSH_TIMEOUT_SECONDS,
        "whiskeyjack: rollover check failed": module.ROLLOVER_PUSH_TIMEOUT_SECONDS,
    }

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


def test_a_ledger_directory_the_watchdog_cannot_read_does_not_stop_the_resolutions_check(
    watchdog: Harness,
) -> None:
    """**Round-4 blocking finding**, reproduced by execution at `b08f01b` before the fix.

    `Path.exists()` does not swallow `EACCES` — measured on the system interpreter the unit
    actually runs (3.12.3) — so a ledger directory that loses search permission made it raise
    `PermissionError` from outside `_last_heartbeat`'s `try`. That escaped `main` and took the
    resolutions check with it, because that check runs second. `CLAUDE.md` keeps permission
    failures and unreadable files explicitly in scope as reachable reliability conditions.

    Driven with a **real `chmod 000`** rather than a monkeypatched `exists`, because the claim
    is about what the filesystem does, not about what a stub can be told to do. Restored in a
    `finally` so a failure here cannot leave an unreadable directory behind for pytest's
    cleanup. Skipped as root, who ignores the mode bits.
    """
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions, so the condition is unreachable")
    ledger = Path(watchdog.module.LEDGER)
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"

    ledger.parent.chmod(0o000)
    try:
        assert watchdog.run() == 1
    finally:
        ledger.parent.chmod(0o755)

    assert watchdog.asked.count(("is-active", "whiskeyjack-resolutions.timer")) == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1
    # And the unreadable ledger is itself reported, rather than passed over in silence.
    assert (
        "no usable heartbeat timestamp in the ledger" in watchdog.titled("WORKER DOWN")[0]["body"]
    )


def test_the_problem_codes_and_their_prose_are_one_closed_vocabulary() -> None:
    """Every code the detector can emit has a sentence; the push body indexes by code.

    The enumeration covers **both** detectors, because `RESOLUTIONS_PROBLEMS` is one vocabulary
    shared by them: `_resolutions_problems` over its three answers, and `_timer_stalled` over
    the one code it contributes (M1-343). A code with no sentence is a `KeyError` in the body,
    and a sentence no detector can emit is prose nobody will ever read.
    """
    module = _load()
    reachable = set()
    for active in ("active", "inactive"):
        for enabled in ("enabled", "disabled"):
            for failed in ("failed", "inactive"):
                reachable.update(module._resolutions_problems(active, enabled, failed))
                stale = ANCHOR - timedelta(days=7)
                if module._timer_stalled(active, enabled, stale, stale, stale, ANCHOR):
                    reachable.add("timer_stalled")
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


# ── the stall rule: enabled, active, and not firing (M1-343) ─────────────────
#
# Every test here drives the watchdog through `main()` rather than calling the predicate, so
# what is measured is a page, and every silence is checked to be its OWN guard's silence: the
# setup is one that WOULD page, and the single field under test is the only thing changed.


def stall_window(harness: Harness) -> timedelta:
    window: timedelta = (
        harness.module.RESOLUTIONS_INTERVAL + harness.module.RESOLUTIONS_STALE_MARGIN
    )
    return window


def test_the_interval_the_stall_rule_uses_is_the_cadence_the_timer_declares() -> None:
    """Read off the tracked unit file, so the constant and the schedule cannot drift apart.

    This is the same witness `test_the_resolutions_throttle_is_long_against_the_cadence_its_own
    _timer_declares` uses for the throttle: the number is not restated here, it is derived from
    `OnCalendar=*-*-* 00/6:23:00`, whose hour field `00/6` is what "every six hours" means.
    """
    module = _load()
    timer = read_unit(UNITS / f"{module.RESOLUTIONS_UNIT}.timer")
    calendar = only(timer, "Timer", "OnCalendar")
    hour_field = calendar.split()[1].split(":")[0]
    assert "/" in hour_field, calendar
    assert timedelta(hours=int(hour_field.split("/")[1])) == module.RESOLUTIONS_INTERVAL

    # And the margin is genuinely a margin: bigger than the slack the timer itself declares.
    accuracy = only(timer, "Timer", "AccuracySec")
    assert accuracy.endswith("min"), accuracy
    assert module.RESOLUTIONS_STALE_MARGIN > timedelta(minutes=int(accuracy[: -len("min")]))
    # A stall must be reportable well inside the window that re-pages it, or the first page of
    # a standing stall would arrive after the throttle had already come round.
    assert (
        module.RESOLUTIONS_INTERVAL + module.RESOLUTIONS_STALE_MARGIN
        < module.RESOLUTIONS_REALERT_AFTER
    )


def test_the_runbooks_w1_table_is_the_whole_vocabulary(watchdog: Harness) -> None:
    """A table is a claim about a PARTITION, not a list of examples (T-908's lesson).

    W1's table tells an operator what each line of the page means. The page is built by indexing
    `RESOLUTIONS_PROBLEMS` by code, so a sentence the table is missing is a page an operator
    cannot look up, and a sentence only the table has is a line nothing can ever print. Set
    equality, so both directions fail.
    """
    runbook = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    section = runbook.split("### W1 — `RESOLUTIONS SCHEDULE STOPPED`", 1)[1]
    rows: list[str] = []
    for line in section.splitlines():
        if line.startswith("|"):
            rows.append(line)
        elif rows:
            break  # the FIRST table in W1, not every table further down the runbook
    cells = [row.split("|")[1].strip() for row in rows]
    assert cells[:2] == ["line", "---"], cells[:2]  # the header and its separator
    documented = set(cells[2:])
    assert documented == set(watchdog.module.RESOLUTIONS_PROBLEMS.values())


def test_a_timer_that_has_never_fired_says_nothing(watchdog: Harness) -> None:
    """The criterion's first silence, and it is guard A's -- not the arithmetic's.

    `systemctl show -P LastTriggerUSec` answers the empty string for a timer that has never
    fired (measured, systemd 255). Everything else in this run is set up to page: the watchdog
    has watched for a month, the timer has been active for a month. Only the stamp is missing.
    """
    watchdog.watch_since(ANCHOR - timedelta(days=30))
    watchdog.units[LAST_TRIGGER] = ""

    assert watchdog.run() == 0
    assert watchdog.pushes == []

    # The same run, with the stamp present and stale, pages -- so the silence above was the
    # never-fired guard refusing, and nothing else.
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        ("", "a timer that has never fired, and a unit that does not exist"),
        ("unknown", "what this module substitutes when the systemctl call itself fails"),
        ("n/a", "systemd's own rendering of an unset timestamp"),
        ("infinity", "systemd's own rendering of a value that never elapses"),
        # Stale by nine hours and rendered in a local zone: the SAME instant that pages one
        # row above, so if the zone token stopped being checked this row would page rather than
        # pass. A future-dated example would have been silent for the wrong reason.
        (STALE_IN_LOCAL_ZONE, "a local zone -- what an un-pinned TZ would produce"),
        (STALE_WITHOUT_ZONE, "the same instant with the zone token missing"),
        ("Sat 2026-13-45 99:99:99 UTC", "well-shaped and not a date"),
    ],
)
def test_a_trigger_stamp_this_program_cannot_use_is_silence_not_a_page(
    watchdog: Harness, answer: str, why: str
) -> None:
    """Unreadable means no usable record, and for a stall the honest answer to that is silence.

    The opposite of `_alert_is_due`'s rule, deliberately: an unusable *throttle* stamp means
    nobody has been told and the safe direction is to tell them, while an unusable *trigger*
    stamp is the program not knowing whether anything is wrong at all. Inventing a page from
    that would make a coverage rule with no observed occurrence into a source of false alarms.

    The `MDT` row is the one that pins `_unit_timestamp`'s environment: it is exactly what this
    host's systemd prints without `TZ=UTC`, and it must not parse by accident.
    """
    watchdog.watch_since(ANCHOR - timedelta(days=30))
    watchdog.units[LAST_TRIGGER] = answer

    assert watchdog.run() == 0, why
    assert watchdog.pushes == []


@pytest.mark.parametrize("hours", [0.0, 1.0, 5.9, 6.9])
def test_a_timer_that_fired_inside_its_own_window_says_nothing(
    watchdog: Harness, hours: float
) -> None:
    """Everything up to the interval plus the margin is a timer doing its job."""
    watchdog.watch_since(ANCHOR - timedelta(days=30))
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=hours))

    assert watchdog.run() == 0
    assert watchdog.pushes == []


def test_a_stale_timer_pages_once_and_says_how_long_it_has_been(watchdog: Harness) -> None:
    """The criterion's page: enabled, active, and nine hours without firing."""
    watchdog.watch_since(ANCHOR - timedelta(days=3))
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))

    assert watchdog.run() == 1
    pages = watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")
    assert len(pages) == 1
    body = pages[0]["body"]
    assert watchdog.module.RESOLUTIONS_PROBLEMS["timer_stalled"] in body
    # An age computed here, never the timestamp systemd printed.
    assert "last fired: 9 h ago (interval 6 h + margin 1 h)" in body
    assert rendered(ANCHOR - timedelta(hours=9)) not in body
    assert "systemd-analyze calendar" in body, "the stall has its own thing to look at"
    assert watchdog.state()["resolutions"]["key"] == "timer_stalled"
    # The worker is healthy throughout and says nothing.
    assert watchdog.titled("WORKER DOWN") == []

    # One page, not one per five-minute run.
    watchdog.instant[0] += timedelta(minutes=5)
    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1


def test_a_standing_stall_pages_again_only_after_the_days_throttle(watchdog: Harness) -> None:
    """Driven at the watchdog's own cadence, because the continuity record is at that cadence.

    A test that jumped the clock 24 hours in one step would find the observation record broken
    and prove nothing about the throttle -- which is itself the point of guard D.
    """
    watchdog.watch_since(ANCHOR - timedelta(days=3))
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    # A poll in progress: the tournament's stale-heartbeat exemption, so the worker stays quiet
    # while the clock runs (`test_a_stale_heartbeat_is_not_a_fault_while_the_poll_is_running`).
    watchdog.units[("is-active", "whiskeyjack-tournament.service")] = "active"

    assert watchdog.run() == 1
    # Fifteen minutes is inside the gap tolerance, so the record the stall rule depends on is
    # never broken by the test's own clock -- a 24-hour jump would break it and prove nothing.
    step = timedelta(minutes=15)
    elapsed = timedelta(0)
    while elapsed < watchdog.module.RESOLUTIONS_REALERT_AFTER:
        watchdog.instant[0] += step
        elapsed += step
        watchdog.run()
        if elapsed < watchdog.module.RESOLUTIONS_REALERT_AFTER:
            assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1, elapsed

    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 2
    assert watchdog.titled("WORKER DOWN") == []


def test_a_host_that_was_off_says_nothing_until_the_watchdog_has_watched_the_window(
    watchdog: Harness,
) -> None:
    """The criterion's second silence, and it is guard D's.

    The timer answers a nine-hour-old trigger throughout, so the arithmetic would page on the
    very first run. What holds it is the watchdog's own record: its last observation is three
    days old, which is a gap in the record rather than nine hours of watching a timer fail to
    fire. The page arrives when -- and only when -- it has watched the whole window itself.
    """
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    watchdog.watch_since(ANCHOR - timedelta(days=3), last_seen=ANCHOR - timedelta(days=3))
    watchdog.units[("is-active", "whiskeyjack-tournament.service")] = "active"

    assert watchdog.run() == 0
    assert watchdog.pushes == []

    window = stall_window(watchdog)
    step = timedelta(minutes=15)
    first_page_at: datetime | None = None
    while watchdog.instant[0] < ANCHOR + window + timedelta(hours=1):
        watchdog.instant[0] += step
        watchdog.run()
        if first_page_at is None and watchdog.titled("RESOLUTIONS SCHEDULE STOPPED"):
            first_page_at = watchdog.instant[0]

    # Two-sided: it does page, and not one run before the window it claims to measure.
    assert first_page_at is not None, "the silence must end once the window has been watched"
    assert ANCHOR + window <= first_page_at < ANCHOR + window + step
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1
    assert watchdog.titled("WORKER DOWN") == []


def test_a_break_in_the_watchdogs_own_record_restarts_the_window(watchdog: Harness) -> None:
    """A gap longer than four missed runs is not watching, and a stall may not span one."""
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    watchdog.watch_since(ANCHOR - timedelta(days=3), last_seen=ANCHOR - timedelta(days=3))
    # A poll in progress, so the worker's own staleness rule stays quiet while the clock runs.
    watchdog.units[("is-active", "whiskeyjack-tournament.service")] = "active"
    assert watchdog.run() == 0

    # Just inside the tolerance the record survives, so the window keeps accumulating...
    watchdog.instant[0] += watchdog.module.WATCHDOG_GAP_TOLERANCE
    watchdog.run()
    assert watchdog.state()["observed"]["since"] == ANCHOR.isoformat()

    # ...and one second past it, the record starts again from now.
    watchdog.instant[0] += watchdog.module.WATCHDOG_GAP_TOLERANCE + timedelta(seconds=1)
    watchdog.run()
    assert watchdog.state()["observed"]["since"] == watchdog.instant[0].isoformat()
    assert watchdog.pushes == []


def test_a_failing_timestamp_query_does_not_re_page_a_standing_stall(
    watchdog: Harness,
) -> None:
    """**Round-1 blocking finding**, as the review's own three-run reproduction.

    A stall stops being *detectable* whenever a guard refuses, and two of those guards refuse
    for reasons that say nothing about the timer — the host was off, or the query failed. If an
    undetectable stall drops out of the code set, the THROTTLE KEY changes, and a changed key is
    a new fault set that pages at once. With the service also failed and the `LastTriggerUSec`
    query timing out on one run in three (`_systemctl` answering "unknown", an ordinary local
    failure), the key flapped and the schedule paged three times inside a window that owes one.

    Reproduced by execution at `4232e12` before the fix: one push on the pinned base, three on
    the branch.
    """
    watchdog.watch_since(ANCHOR - timedelta(days=3))
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    watchdog.units[("is-failed", "whiskeyjack-resolutions.service")] = "failed"

    assert watchdog.run() == 1
    assert watchdog.state()["resolutions"]["key"] == "service_failed,timer_stalled"

    for answer in ("unknown", "", "n/a"):
        watchdog.instant[0] += timedelta(minutes=5)
        watchdog.units[LAST_TRIGGER] = answer
        assert watchdog.run() == 1
        # The key is what the throttle is keyed on, so it is the thing that must not move.
        assert watchdog.state()["resolutions"]["key"] == "service_failed,timer_stalled", answer
        assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1, answer

    # And when the query recovers, still one page for one unchanged condition.
    watchdog.instant[0] += timedelta(minutes=5)
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1


def test_a_break_in_the_record_does_not_re_page_or_announce_a_recovery(
    watchdog: Harness,
) -> None:
    """A stall is a standing condition; only the timer firing ends it.

    Guard D refuses after a gap in the watchdog's own record — the host was off — which says
    nothing about the timer. Before the round-1 fix that dropped `timer_stalled` from the code
    set, which both re-paged (a changed key) and, once the code was gone, let a later empty set
    look like a recovery.
    """
    watchdog.watch_since(ANCHOR - timedelta(days=3))
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    watchdog.units[("is-active", "whiskeyjack-tournament.service")] = "active"
    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1

    # The machine is off for half an hour: longer than the gap tolerance, well inside the
    # 24-hour window the page it already sent owes.
    watchdog.instant[0] += timedelta(minutes=30)
    assert watchdog.run() == 1, "the condition still stands; it has not been disproved"
    assert watchdog.state()["observed"]["since"] == watchdog.instant[0].isoformat()
    assert watchdog.state()["resolutions"]["key"] == "timer_stalled"
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1, "one condition, one page"
    assert watchdog.titled("recovered") == []


def test_another_fault_clearing_is_not_a_recovery_while_the_stall_stands(
    watchdog: Harness,
) -> None:
    """The second half of the round-1 finding, and the one that would have misled an operator.

    With the stall's code dropped, the stored key was `service_failed` alone; clearing the
    failed run then emptied the code set and sent `resolutions schedule recovered` about a timer
    that had still not fired. Now the stall stays in the set until the timer fires, so an empty
    set really does mean every fault cleared.
    """
    watchdog.watch_since(ANCHOR - timedelta(days=3))
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    # A poll in progress, so the worker stays quiet while this test runs the clock on.
    watchdog.units[("is-active", "whiskeyjack-tournament.service")] = "active"
    watchdog.units[("is-failed", "whiskeyjack-resolutions.service")] = "failed"
    assert watchdog.run() == 1

    watchdog.instant[0] += timedelta(minutes=35)  # a gap, so guard D can no longer see it
    watchdog.units[("is-failed", "whiskeyjack-resolutions.service")] = "inactive"
    assert watchdog.run() == 1
    assert watchdog.titled("recovered") == []
    assert watchdog.state()["resolutions"]["key"] == "timer_stalled"


def test_only_the_timer_firing_ends_a_carried_stall(watchdog: Harness) -> None:
    """The evidence that ends it, after a gap that made it unverifiable."""
    watchdog.watch_since(ANCHOR - timedelta(days=3))
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    # A poll in progress, so the worker stays quiet while this test runs the clock on.
    watchdog.units[("is-active", "whiskeyjack-tournament.service")] = "active"
    assert watchdog.run() == 1

    watchdog.instant[0] += timedelta(minutes=35)
    assert watchdog.run() == 1, "a gap does not end it"

    watchdog.instant[0] += timedelta(minutes=5)
    watchdog.units[LAST_TRIGGER] = rendered(watchdog.instant[0] - timedelta(minutes=1))
    assert watchdog.run() == 0
    assert len(watchdog.titled("resolutions schedule recovered")) == 1
    assert watchdog.state()["resolutions"] == {}


def test_a_stopped_timer_supersedes_a_carried_stall(watchdog: Harness) -> None:
    """A timer somebody stopped is described by its own code, and the queries are not asked.

    The carried stall must not survive as a phantom beside `timer_inactive`: the three existing
    codes are read off systemd every run and are the stronger statement.
    """
    watchdog.watch_since(ANCHOR - timedelta(days=3))
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    assert watchdog.run() == 1
    assert watchdog.state()["resolutions"]["key"] == "timer_stalled"

    watchdog.instant[0] += timedelta(minutes=5)
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"
    assert watchdog.run() == 1
    assert watchdog.state()["resolutions"]["key"] == "timer_inactive"
    assert watchdog.titled("recovered") == []


def test_a_timer_that_fires_again_says_so_once(watchdog: Harness) -> None:
    """The evidence a recovery needs is the timer firing, and that is what clears the stall."""
    watchdog.watch_since(ANCHOR - timedelta(days=3))
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    assert watchdog.run() == 1

    watchdog.instant[0] += timedelta(minutes=5)
    watchdog.units[LAST_TRIGGER] = rendered(watchdog.instant[0] - timedelta(minutes=1))
    assert watchdog.run() == 0
    assert len(watchdog.titled("resolutions schedule recovered")) == 1
    assert watchdog.state()["resolutions"] == {}

    # Once, not on every later healthy run.
    watchdog.instant[0] += timedelta(minutes=5)
    assert watchdog.run() == 0
    assert len(watchdog.titled("resolutions schedule recovered")) == 1


def test_a_timer_just_restarted_is_recovering_rather_than_stalled(watchdog: Harness) -> None:
    """Guard C, and the false page it exists to prevent.

    `Persistent=true` means a timer started after a stop fires within `AccuracySec`. So the
    minute after an operator runs `systemctl --user enable --now` on a timer that had been
    stopped for a week, its last trigger is a week old and it is not stalled -- it is the fix
    working. Paging then would be a false alarm on the path the runbook tells them to take.
    """
    watchdog.watch_since(ANCHOR - timedelta(days=30))
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(days=7))
    watchdog.units[ACTIVE_ENTER] = rendered(ANCHOR - timedelta(minutes=1))

    assert watchdog.run() == 0
    assert watchdog.pushes == []

    # The only field that changes: the timer has now been active for the whole window.
    watchdog.units[ACTIVE_ENTER] = rendered(ANCHOR - timedelta(hours=8))
    assert watchdog.run() == 1
    assert len(watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")) == 1


@pytest.mark.parametrize("answer", ["", "unknown", STALE_IN_LOCAL_ZONE])
def test_an_activation_stamp_this_program_cannot_use_is_silence_too(
    watchdog: Harness, answer: str
) -> None:
    """Guard C has no opinion it can defend without the stamp, so it refuses."""
    watchdog.watch_since(ANCHOR - timedelta(days=30))
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    watchdog.units[ACTIVE_ENTER] = answer

    assert watchdog.run() == 0
    assert watchdog.pushes == []


def test_a_stopped_timer_is_never_also_reported_stalled(watchdog: Harness) -> None:
    """The criterion is scoped to a timer that is running; a stopped one is already described.

    And the two timestamp queries are not even asked, which is what keeps the declared worst
    case honest: they are on the path where their answer can matter and nowhere else.
    """
    watchdog.watch_since(ANCHOR - timedelta(days=30))
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"

    assert watchdog.run() == 1
    assert watchdog.state()["resolutions"]["key"] == "timer_inactive"
    assert LAST_TRIGGER not in watchdog.asked
    assert ACTIVE_ENTER not in watchdog.asked


def test_a_stalled_timer_whose_last_run_also_failed_pages_once_naming_both(
    watchdog: Harness,
) -> None:
    """The stall is ADDED to the code set, not substituted for it (the stricter reading).

    Two faults are two lines in one page and one throttle key, so the second is new information
    the moment it appears rather than something a standing key buys silence for.
    """
    watchdog.watch_since(ANCHOR - timedelta(days=30))
    watchdog.units[LAST_TRIGGER] = rendered(ANCHOR - timedelta(hours=9))
    watchdog.units[("is-failed", "whiskeyjack-resolutions.service")] = "failed"

    assert watchdog.run() == 1
    pages = watchdog.titled("RESOLUTIONS SCHEDULE STOPPED")
    assert len(pages) == 1
    assert watchdog.module.RESOLUTIONS_PROBLEMS["timer_stalled"] in pages[0]["body"]
    assert watchdog.module.RESOLUTIONS_PROBLEMS["service_failed"] in pages[0]["body"]
    assert watchdog.state()["resolutions"]["key"] == "service_failed,timer_stalled"


def test_the_timestamp_queries_ask_in_utc_and_the_others_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment is the whole of why the stamps parse, so observe it at `subprocess.run`.

    `systemctl show` renders timestamps in the client's local zone and locale, and
    `--timestamp=` does not change that for `show` (measured, systemd 255). The timestamp calls
    therefore pass an environment; **every other call passes `env=None`**, which is what
    `subprocess.run` was already given -- so this is also the witness that M1-341's four
    tournament queries and three resolutions queries are invoked exactly as they were.
    """
    module = _load()
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []
    answers = {
        ("is-active", "whiskeyjack-resolutions.timer"): "active",
        ("is-enabled", "whiskeyjack-resolutions.timer"): "enabled",
    }

    class _Completed:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run(argv: Any, **kwargs: Any) -> _Completed:
        args = tuple(argv[2:])
        calls.append((args, kwargs["env"]))
        return _Completed(answers.get(args, "inactive"))

    ledger = tmp_path / "data" / "l.sqlite3"
    seeded_ledger(ledger, heartbeat_at=ANCHOR - timedelta(minutes=2))
    monkeypatch.setattr(module, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(module, "LEDGER", ledger)
    monkeypatch.setattr(module, "_now", lambda: ANCHOR)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setenv("WJ_WATCHDOG_ENV_WITNESS", "carried-through-0001")

    module.main()

    timestamps = [(args, env) for args, env in calls if args in (LAST_TRIGGER, ACTIVE_ENTER)]
    assert len(timestamps) == 2, [args for args, _ in calls]
    for args, env in timestamps:
        assert env is not None
        assert env["TZ"] == "UTC" and env["LC_ALL"] == "C", args
        # The rest of the environment is carried through: systemctl --user needs the session's
        # own variables (XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS) to reach the user manager.
        assert env["WJ_WATCHDOG_ENV_WITNESS"] == "carried-through-0001"
    others = [
        args for args, env in calls if env is not None and args not in (LAST_TRIGGER, ACTIVE_ENTER)
    ]
    assert others == [], "every other systemctl call must be invoked exactly as it was"
    assert len(calls) > 2


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

    assert watchdog.module._check_resolutions(state, ANCHOR - timedelta(days=30)) is True
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
        "threading",
        "time",
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


# ── M1-347: MiniBench rolling over to a project the activation does not cover ──
#
# 2026-09-21: Metaculus moved MiniBench from 33122 to 33125, the worker kept polling 33122 with
# fresh heartbeats and `discovered: 0`, and 13 questions were lost before a person noticed.
# The fixture's activation and the fake slug both say 33125, which is today's real state.

NEXT_PROJECT = 33130
ROLLED = "MINIBENCH ROLLED OVER"
CHECK_FAILED = "rollover check failed"


def test_the_slug_is_the_pinned_sdks_minibench_id() -> None:
    """The witness outside the script: the SDK the worker polls through names the series.

    If a pinned-SDK bump renamed `CURRENT_MINIBENCH_ID`, the worker and this check would ask
    about two different things, and nothing else would say so.
    """
    from forecasting_tools.helpers.metaculus_client import MetaculusClient

    assert _load().MINIBENCH_SLUG == MetaculusClient.CURRENT_MINIBENCH_ID


def test_a_matching_project_is_quiet_and_asks_metaculus_one_question(watchdog: Harness) -> None:
    assert watchdog.run() == 0
    assert watchdog.pushes == []
    assert watchdog.fetched == ["/projects/tournaments/minibench/"]
    assert watchdog.state()["rollover"] == {}
    assert watchdog.state()["rollover_check"] == {}


def test_a_mismatch_with_an_open_post_pages_once_within_one_run(watchdog: Harness) -> None:
    """The criterion. Killed-mutant (a): a neutered comparison reads every run as a match."""
    watchdog.project[0] = {"id": NEXT_PROJECT}

    assert watchdog.run() == 1
    pages = watchdog.titled(ROLLED)
    assert len(pages) == 1 and len(watchdog.pushes) == 1
    assert pages[0]["priority"] == "urgent"
    assert "docs/RUNBOOK.md P3" in pages[0]["body"]
    # The open-post question is asked about the slug's project, never the activation's.
    assert watchdog.fetched[1] == f"/posts/?tournaments={NEXT_PROJECT}&statuses=open&limit=10"
    # And the worker's own page and throttle are untouched.
    assert watchdog.titled("WORKER DOWN") == []
    assert watchdog.state()["alerting"] is False


def test_a_mismatch_with_nothing_open_is_quiet(watchdog: Harness) -> None:
    """Killed-mutant (b): dropping the open-post condition pages at every series boundary."""
    watchdog.project[0] = {"id": NEXT_PROJECT}
    watchdog.posts[0] = {"results": []}

    assert watchdog.run() == 0
    assert watchdog.pushes == []


def test_closed_posts_in_the_answer_are_not_open_posts(watchdog: Harness) -> None:
    """Counted by each post's own status, not by trusting the `statuses=open` filter."""
    watchdog.project[0] = {"id": NEXT_PROJECT}
    watchdog.posts[0] = {"results": [{"status": "closed"}, {"status": "resolved"}, {}]}

    assert watchdog.run() == 0
    assert watchdog.pushes == []


def test_the_latest_activation_is_the_one_compared(watchdog: Harness) -> None:
    """Newest by `seq`, the row `require_activation` reads -- in both directions."""
    watchdog.activate(33122)
    assert watchdog.run() == 1, "the newest activation is on the old series"
    assert len(watchdog.titled(ROLLED)) == 1

    watchdog.activate(ACTIVE_PROJECT)
    watchdog.instant[0] += timedelta(minutes=5)
    assert watchdog.run() == 0, "re-pointed: the newest activation covers the slug again"


@pytest.mark.parametrize(
    "failure",
    [
        OSError("network is unreachable"),
        TimeoutError(),
        ValueError("Expecting value"),
        RecursionError(),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        RuntimeError("anything else at all"),
    ],
    ids=["oserror", "timeout", "json", "recursion", "decode", "anything"],
)
def test_a_failed_read_is_its_own_fault_never_green_and_never_worker_down(
    watchdog: Harness, failure: BaseException
) -> None:
    """Killed-mutant (c): swallowing the error as green exits 0 and pages nothing."""
    watchdog.project[0] = failure

    assert watchdog.run() == 1
    assert [push["title"] for push in watchdog.pushes] == [f"whiskeyjack: {CHECK_FAILED}"]
    assert watchdog.pushes[0]["priority"] == "default"
    assert "Metaculus did not answer with a usable MiniBench project" in watchdog.pushes[0]["body"]
    assert watchdog.state()["alerting"] is False, "a Metaculus outage is not the worker's"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "33125",
        {},
        {"id": None},
        {"id": "33125"},
        {"id": True},
        {"id": 0},
        {"id": -33125},
        {"id": 33125.0},
        {"id": [33125]},
    ],
    ids=repr,
)
def test_a_malformed_project_answer_is_a_failed_check(watchdog: Harness, payload: object) -> None:
    """Bool-as-int included: `True` is an int to `isinstance`, and it is not project 1."""
    watchdog.project[0] = payload

    assert watchdog.run() == 1
    assert len(watchdog.titled(CHECK_FAILED)) == 1
    assert watchdog.titled(ROLLED) == []


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"results": None}, {"results": "open"}, {"results": [1]}, {"results": [[]]}],
    ids=repr,
)
def test_a_malformed_posts_answer_is_a_failed_check(watchdog: Harness, payload: object) -> None:
    watchdog.project[0] = {"id": NEXT_PROJECT}
    watchdog.posts[0] = payload

    assert watchdog.run() == 1
    assert len(watchdog.titled(CHECK_FAILED)) == 1
    assert "open posts" in watchdog.titled(CHECK_FAILED)[0]["body"]
    assert watchdog.titled(ROLLED) == []


@pytest.mark.parametrize(
    "data",
    [
        '{"project_id": "33125"}',
        '{"project_id": true}',
        '{"project_id": 0}',
        '{"account_id": 1}',
        "[33125]",
        "33125",
    ],
)
def test_an_activation_this_program_cannot_read_is_a_failed_check_not_a_match(
    watchdog: Harness, data: str
) -> None:
    """Values read back out of the ledger are untrusted (CLAUDE.md). An INSERT, as `012` allows."""
    connection = sqlite3.connect(Path(watchdog.module.LEDGER))
    try:
        connection.execute(
            "INSERT INTO tournament_events(event_id, kind, scope, data, created_at_utc) "
            "VALUES ('odd-activation', 'activation', 'account', ?, '2026-09-17T11:00:00+00:00')",
            (data,),
        )
        connection.commit()
    finally:
        connection.close()

    assert watchdog.run() == 1
    assert len(watchdog.titled(CHECK_FAILED)) == 1
    assert "could not be read from the ledger" in watchdog.titled(CHECK_FAILED)[0]["body"]
    assert watchdog.fetched == [], "no Metaculus question is worth asking without the other half"


def test_a_ledger_with_no_activation_is_a_failed_check(
    tmp_path: Path, watchdog: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = tmp_path / "bare" / "ledger.sqlite3"
    seeded_ledger(bare, heartbeat_at=ANCHOR - timedelta(minutes=2))
    monkeypatch.setattr(watchdog.module, "LEDGER", bare)

    assert watchdog.run() == 1
    assert len(watchdog.titled(CHECK_FAILED)) == 1


def test_a_read_that_hangs_is_cut_off_at_the_declared_bound(
    watchdog: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is the thread join, not a socket timeout -- DNS has none at all.

    Measured, not argued: a fetch that blocks far past the bound returns `timed_out` once the
    bound has elapsed. Shrunk to a fraction of a second so the test costs that, not 15 s.
    """
    import threading
    import time

    release = threading.Event()

    def hang(path: str, deadline: float) -> object:
        release.wait(30)
        return {"id": ACTIVE_PROJECT}

    monkeypatch.setattr(watchdog.module, "ROLLOVER_FETCH_SECONDS", 0.3)
    monkeypatch.setattr(watchdog.module, "_metaculus_get", hang)
    started = time.monotonic()
    try:
        assert watchdog.run() == 1
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert elapsed < 5, elapsed
    assert "did not finish within" in watchdog.titled(CHECK_FAILED)[0]["body"]


def test_a_standing_rollover_repeats_hourly_and_not_every_run(watchdog: Harness) -> None:
    watchdog.project[0] = {"id": NEXT_PROJECT}
    for _ in range(12):
        assert watchdog.run() == 1
        watchdog.instant[0] += timedelta(minutes=5)
    assert len(watchdog.titled(ROLLED)) == 1, "an hour of runs, one page"

    assert watchdog.run() == 1
    assert len(watchdog.titled(ROLLED)) == 2, "sixty minutes after the first page"


def test_a_second_rollover_while_the_first_stands_pages_at_once(watchdog: Harness) -> None:
    """The project pair is in the throttle key, so a new pair is new information."""
    watchdog.project[0] = {"id": NEXT_PROJECT}
    assert watchdog.run() == 1
    watchdog.instant[0] += timedelta(minutes=5)
    watchdog.project[0] = {"id": NEXT_PROJECT + 1}
    assert watchdog.run() == 1
    assert len(watchdog.titled(ROLLED)) == 2


def test_a_failed_read_neither_clears_nor_re_pages_a_standing_rollover(watchdog: Harness) -> None:
    """The M1-343 lesson, applied: a code that drops out for an unrelated reason re-keys.

    A Metaculus timeout says nothing about which project MiniBench is on. So the failed read
    pages on its own throttle and leaves the rollover's entry exactly as it was, and when the
    read works again the standing rollover is still inside its hour and stays quiet.
    """
    watchdog.project[0] = {"id": NEXT_PROJECT}
    assert watchdog.run() == 1
    rollover_entry = watchdog.state()["rollover"]

    watchdog.instant[0] += timedelta(minutes=5)
    watchdog.project[0] = TimeoutError()
    assert watchdog.run() == 1
    assert watchdog.state()["rollover"] == rollover_entry
    assert len(watchdog.titled(CHECK_FAILED)) == 1

    watchdog.instant[0] += timedelta(minutes=5)
    watchdog.project[0] = {"id": NEXT_PROJECT}
    assert watchdog.run() == 1
    assert len(watchdog.titled(ROLLED)) == 1, "the same rollover, still inside its hour"
    assert watchdog.state()["rollover_check"] == {}, "a good read clears the failed check"


def test_an_empty_batch_does_not_clear_a_standing_rollover(watchdog: Harness) -> None:
    """Nothing open is not evidence the activation was re-pointed."""
    watchdog.project[0] = {"id": NEXT_PROJECT}
    assert watchdog.run() == 1
    watchdog.instant[0] += timedelta(minutes=5)
    watchdog.posts[0] = {"results": []}
    assert watchdog.run() == 0
    watchdog.instant[0] += timedelta(minutes=5)
    watchdog.posts[0] = {"results": [{"status": "open"}]}
    assert watchdog.run() == 1
    assert len(watchdog.titled(ROLLED)) == 1
    assert [push["title"] for push in watchdog.pushes if "cleared" in push["title"]] == []


def test_a_failing_check_pages_on_its_own_six_hour_window(watchdog: Harness) -> None:
    # Six hours outruns the fixture's heartbeat; a poll in progress holds it still, which is
    # not a worker fault, so the worker half stays healthy and only this subject is exercised.
    watchdog.units[("is-active", "whiskeyjack-tournament.service")] = "active"
    watchdog.project[0] = OSError("down")
    for _ in range(72):
        assert watchdog.run() == 1
        watchdog.instant[0] += timedelta(minutes=5)
    assert len(watchdog.titled(CHECK_FAILED)) == 1, "six hours of runs, one page"
    assert watchdog.run() == 1
    assert len(watchdog.titled(CHECK_FAILED)) == 2
    assert watchdog.titled("WORKER DOWN") == []


def test_re_pointing_the_activation_clears_the_rollover_and_says_so_once(
    watchdog: Harness,
) -> None:
    watchdog.project[0] = {"id": NEXT_PROJECT}
    assert watchdog.run() == 1
    watchdog.activate(NEXT_PROJECT)
    watchdog.instant[0] += timedelta(minutes=5)
    assert watchdog.run() == 0
    watchdog.instant[0] += timedelta(minutes=5)
    assert watchdog.run() == 0
    assert len([push for push in watchdog.pushes if "rollover cleared" in push["title"]]) == 1
    assert watchdog.state()["rollover"] == {}


def test_a_refused_rollover_push_is_retried_on_the_next_run(watchdog: Harness) -> None:
    watchdog.project[0] = {"id": NEXT_PROJECT}
    watchdog.delivers[0] = False
    assert watchdog.run() == 1
    watchdog.delivers[0] = True
    watchdog.instant[0] += timedelta(minutes=5)
    assert watchdog.run() == 1
    assert len(watchdog.titled(ROLLED)) == 2
    assert "last_alert" in watchdog.state()["rollover"]


def test_a_rollover_does_not_mute_or_alter_the_other_two_subjects(watchdog: Harness) -> None:
    watchdog.project[0] = {"id": NEXT_PROJECT}
    watchdog.units[("is-active", "whiskeyjack-tournament.timer")] = "inactive"
    watchdog.units[("is-active", "whiskeyjack-resolutions.timer")] = "inactive"

    assert watchdog.run() == 1
    assert sorted(push["title"] for push in watchdog.pushes) == [
        "whiskeyjack: MINIBENCH ROLLED OVER",
        "whiskeyjack: RESOLUTIONS SCHEDULE STOPPED",
        "whiskeyjack: WORKER DOWN",
    ]


def test_an_unusable_rollover_state_entry_still_pages(watchdog: Harness) -> None:
    for entry in ([], "x", {"key": 3, "last_alert": [1]}, {"last_alert": "2026-09-17T11:00:00"}):
        Path(watchdog.module.STATE).write_text(
            json.dumps({"rollover": entry, "rollover_check": entry}), encoding="utf-8"
        )
        watchdog.pushes.clear()
        watchdog.project[0] = {"id": NEXT_PROJECT}
        assert watchdog.run() == 1, entry
        assert len(watchdog.titled(ROLLED)) == 1, entry


# ── the real HTTP half, against a fake opener ────────────────────────────────


@dataclass
class _Body:
    payload: bytes
    status: int = 200

    def __enter__(self) -> _Body:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.payload if limit < 0 else self.payload[:limit]


def _serve(module: ModuleType, monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[Any]:
    """Replace `build_opener` so `_metaculus_get` runs for real against `body`."""
    requests: list[Any] = []

    class _Opener:
        def __init__(self, *handlers: object) -> None:
            requests.append(("handlers", handlers))

        def open(self, request: Any, timeout: float | None = None) -> _Body:
            requests.append((request, timeout))
            return _Body(body)

    monkeypatch.setattr(module.urllib.request, "build_opener", _Opener)
    return requests


@pytest.mark.parametrize(
    "body",
    [b"not json", b"\xff\xfe", b"[" * 200_000, b'{"id": 33125' + b" " * 1_000_000 + b"}", b""],
    ids=["text", "undecodable", "deep", "oversize", "empty"],
)
def test_a_malformed_body_never_escapes_main(
    watchdog: Harness, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    module = watchdog.module
    monkeypatch.setattr(module, "_metaculus_get", watchdog.real_metaculus_get)
    _serve(module, monkeypatch, body)

    assert watchdog.run() == 1
    assert len(watchdog.titled(CHECK_FAILED)) == 1


def test_the_get_sends_the_token_as_a_header_refuses_redirects_and_stays_in_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    module = _load()
    requests = _serve(module, monkeypatch, b'{"id": 33125}')
    monkeypatch.setenv("METACULUS_TOKEN", "wj-fake-token-0001")

    assert module._metaculus_get("/projects/tournaments/minibench/", time.monotonic() + 15) == {
        "id": 33125
    }
    (_, handlers), (request, timeout) = requests
    assert handlers == (module._NoRedirect,)
    assert module._NoRedirect().redirect_request() is None
    assert request.full_url == "https://www.metaculus.com/api/projects/tournaments/minibench/"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == "Token wj-fake-token-0001"
    # Measured: the default `Python-urllib/3.x` agent is answered 403 even with a valid token.
    assert request.get_header("User-agent") == module.ROLLOVER_USER_AGENT
    assert 0 < timeout <= module.ROLLOVER_FETCH_SECONDS

    with pytest.raises(TimeoutError):
        module._metaculus_get("/projects/tournaments/minibench/", time.monotonic() - 1)


# ── no value read from Metaculus or the ledger reaches a push or stdout ────────


CANARY_PROJECT = 987654321


@pytest.mark.parametrize("scenario", ["rollover", "malformed", "raised"])
def test_no_payload_value_appears_in_any_push_or_on_stdout(
    watchdog: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: str,
) -> None:
    """Neither project id, the count, the token, nor any text Metaculus sent back."""
    monkeypatch.setenv("METACULUS_TOKEN", "wj-secret-token-0001")
    canary = "wj-canary-7f3a"
    if scenario == "rollover":
        watchdog.project[0] = {"id": CANARY_PROJECT, "name": canary, "slug": canary}
        watchdog.posts[0] = {"results": [{"status": "open", "title": canary}] * 7}
    elif scenario == "malformed":
        watchdog.project[0] = {"id": canary, "detail": canary}
    else:
        watchdog.project[0] = RuntimeError(canary)

    assert watchdog.run() == 1
    assert watchdog.pushes
    printed = capsys.readouterr().out
    for text in [printed, *(value for push in watchdog.pushes for value in push.values())]:
        for leaked in (canary, str(CANARY_PROJECT), str(ACTIVE_PROJECT), "wj-secret-token-0001"):
            assert leaked not in text, (leaked, text)
    if scenario == "rollover":
        assert " 7 " not in watchdog.titled(ROLLED)[0]["body"]
