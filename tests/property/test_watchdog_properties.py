"""M1-341: the watchdog's two pure functions, fuzzed.

``_resolutions_problems`` is a classifier over strings systemd prints, and ``_alert_is_due``
is a decision over a JSON object a person can edit. Both are reached on the path that decides
whether an operator hears that resolution ingestion has stopped, and both are total by
intention rather than by luck -- so the properties are: never raises; the code set is a sorted
subset of one closed vocabulary; the classification is exact rather than approximate; the
throttle is silent only inside its own window and only for a fault set it has already paged.

Every strategy here draws systemd's *own* answers as well as arbitrary text: a strategy that
can only produce `"banana"` never reaches the branch the assertion is about (docs/LESSONS.md,
lesson 9), and the branch that matters here is the one where the string IS `"active"`.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

from hypothesis import given
from hypothesis import strategies as st

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "wj-watchdog"


def _load() -> ModuleType:
    # Compiled from source, never through __pycache__ -- see tests/unit/test_watchdog.py.
    spec = importlib.util.spec_from_loader("wj_watchdog_properties", loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec"), module.__dict__)
    return module


watchdog = _load()

# systemd's real answers, mixed with arbitrary text. `is-enabled` really can answer
# `enabled-runtime`, `static`, `indirect`, `linked` and `masked`; `is-active` really can
# answer `activating` and `deactivating`; and `_systemctl` substitutes "unknown" when the
# call itself fails and "" for a unit that does not exist. All of them must classify.
UNIT_WORDS = st.sampled_from(
    [
        "active",
        "inactive",
        "activating",
        "deactivating",
        "failed",
        "enabled",
        "enabled-runtime",
        "disabled",
        "static",
        "indirect",
        "linked",
        "masked",
        # Measured on systemd 255: `is-enabled` on a unit that does not exist answers this,
        # while `is-active` and `is-failed` both answer `inactive` -- indistinguishable from a
        # unit that exists and is stopped.
        "not-found",
        "unknown",
        "",
        "Active",
        "active ",
    ]
)
# Per-field, and deliberately NOT one shared `ANSWERS` strategy. With all three fields drawn
# uniformly from ~16 words plus arbitrary text, the combination (active, enabled, "failed") has
# probability ~1/32768 and 200 draws never reach it -- so the one branch that distinguishes
# `service_failed` from silence was unreachable and the mutation that deletes that rule survived
# the property suite entirely. Found by mutation (W03, props-only pass); it is the reachability
# form of the vacuity class docs/LESSONS.md calls the top recurring defect. Naming each field's
# pivotal value as its own `st.one_of` branch lifts that combination to roughly 1 draw in 36.
ACTIVE_ANSWERS = st.one_of(st.just("active"), UNIT_WORDS, st.text(max_size=40))
ENABLED_ANSWERS = st.one_of(st.just("enabled"), UNIT_WORDS, st.text(max_size=40))
FAILED_ANSWERS = st.one_of(st.just("failed"), st.just("inactive"), UNIT_WORDS, st.text(max_size=40))

ANCHOR = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


@given(active=ACTIVE_ANSWERS, enabled=ENABLED_ANSWERS, failed=FAILED_ANSWERS)
def test_the_codes_are_always_a_sorted_subset_of_the_closed_vocabulary(
    active: str, enabled: str, failed: str
) -> None:
    codes = watchdog._resolutions_problems(active, enabled, failed)
    assert isinstance(codes, tuple)
    assert set(codes) <= set(watchdog.RESOLUTIONS_PROBLEMS)
    assert list(codes) == sorted(codes)
    assert len(set(codes)) == len(codes)
    # Deterministic: the same three answers always classify the same way.
    assert watchdog._resolutions_problems(active, enabled, failed) == codes


@given(active=ACTIVE_ANSWERS, enabled=ENABLED_ANSWERS, failed=FAILED_ANSWERS)
def test_silence_means_exactly_running_enabled_and_not_failed(
    active: str, enabled: str, failed: str
) -> None:
    """The no-fault case is an equality, not an implication.

    Stated one-sided -- "a fault implies something was wrong" -- this would pass against a
    detector that never fires at all (M1-501's vacuity, docs/LESSONS.md). So both directions.
    """
    quiet = watchdog._resolutions_problems(active, enabled, failed) == ()
    assert quiet == (active == "active" and enabled == "enabled" and failed != "failed")


@given(active=ACTIVE_ANSWERS, enabled=ENABLED_ANSWERS, failed=FAILED_ANSWERS)
def test_every_code_emitted_has_a_sentence_for_the_push_body(
    active: str, enabled: str, failed: str
) -> None:
    """The body indexes RESOLUTIONS_PROBLEMS by code; a code without one is a KeyError."""
    for code in watchdog._resolutions_problems(active, enabled, failed):
        assert watchdog.RESOLUTIONS_PROBLEMS[code]


@given(
    codes=st.lists(st.sampled_from(sorted(watchdog.RESOLUTIONS_PROBLEMS)), unique=True).map(
        lambda values: tuple(sorted(values))
    ),
    stored=st.dictionaries(
        st.sampled_from(["key", "last_alert", "alerting", "unexpected"]),
        st.one_of(
            st.none(),
            st.booleans(),
            st.integers(),
            st.text(max_size=40),
            st.sampled_from(sorted(watchdog.RESOLUTIONS_PROBLEMS)),
            st.just("timer_inactive"),
            st.just(ANCHOR.isoformat()),
            st.just("2026-09-17T12:00:00"),
        ),
        max_size=4,
    ),
    drift=st.integers(min_value=-90_000, max_value=90_000),
)
def test_the_throttle_decision_never_raises_and_is_json_round_trippable(
    codes: tuple[str, ...], stored: dict[str, object], drift: int
) -> None:
    """Total over every shape the state file can hold, including ones a person typed.

    ``stored`` is what came back out of JSON, so it is untrusted shape: the decision must be a
    bool for all of it, and the entry the caller would write back must survive a round trip
    through the file it lives in.
    """
    now = ANCHOR + timedelta(seconds=drift)
    due = watchdog._alert_is_due(codes, stored, now)
    assert isinstance(due, bool)
    if not codes:
        assert due is False
    entry = {"key": watchdog._throttle_key(codes), "last_alert": now.isoformat()}
    assert json.loads(json.dumps(entry)) == entry


@given(
    codes=st.lists(
        st.sampled_from(sorted(watchdog.RESOLUTIONS_PROBLEMS)), unique=True, min_size=1
    ).map(lambda values: tuple(sorted(values))),
    elapsed=st.integers(min_value=0, max_value=200_000),
)
def test_a_paged_fault_set_is_silent_for_its_window_and_never_a_second_longer(
    codes: tuple[str, ...], elapsed: int
) -> None:
    """The bound is two-sided on purpose.

    "Never pages inside the window" alone is satisfied by never paging, which is the failure
    this whole item exists to end. So the same property also asserts that the silence *ends*.
    """
    window = watchdog.RESOLUTIONS_REALERT_AFTER.total_seconds()
    stored = {"key": watchdog._throttle_key(codes), "last_alert": ANCHOR.isoformat()}
    due = watchdog._alert_is_due(codes, stored, ANCHOR + timedelta(seconds=elapsed))
    assert due == (elapsed >= window)


@given(
    paged=st.lists(st.sampled_from(sorted(watchdog.RESOLUTIONS_PROBLEMS)), unique=True).map(
        lambda values: tuple(sorted(values))
    ),
    current=st.lists(
        st.sampled_from(sorted(watchdog.RESOLUTIONS_PROBLEMS)), unique=True, min_size=1
    ).map(lambda values: tuple(sorted(values))),
    elapsed=st.integers(min_value=0, max_value=200_000),
)
def test_a_fault_set_that_has_never_been_paged_pages_whatever_the_stamp_says(
    paged: tuple[str, ...], current: tuple[str, ...], elapsed: int
) -> None:
    """A different set has its own first page owed; it may not inherit another set's quiet."""
    stored = {"key": watchdog._throttle_key(paged), "last_alert": ANCHOR.isoformat()}
    due = watchdog._alert_is_due(current, stored, ANCHOR + timedelta(seconds=elapsed))
    if paged != current:
        assert due is True
    else:
        assert due == (elapsed >= watchdog.RESOLUTIONS_REALERT_AFTER.total_seconds())


@given(
    raw=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=True, allow_infinity=True),
        st.lists(st.integers(), max_size=3),
        st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
        st.text(max_size=40),
        st.datetimes(timezones=st.just(timezone.utc)).map(lambda moment: moment.isoformat()),
        st.datetimes().map(lambda moment: moment.isoformat()),
    )
)
def test_the_stamp_parser_is_total_and_returns_only_aware_instants(raw: object) -> None:
    """`_aware_stamp` is the single parser both throttles use, so it has to be total.

    It is the round-2 fix: the tournament block's own parser guarded only `ValueError` on a
    value it had truthiness-tested, so a list, an int, a float, a dict or a **naive** ISO
    string each escaped as a raw `TypeError` out of `main` and took down both subjects. The
    draw includes aware and naive `isoformat()` output precisely so both halves are reachable
    -- without the aware branch this degenerates into "always None", which a parser that never
    parses anything also satisfies.
    """
    parsed = watchdog._aware_stamp(raw)
    assert parsed is None or isinstance(parsed, datetime)
    if parsed is not None:
        assert parsed.tzinfo is not None
        # Whatever it returns can be subtracted from an aware `now` without raising.
        assert isinstance(ANCHOR - parsed, timedelta)
    if not isinstance(raw, str) or isinstance(raw, bool):
        assert parsed is None


@given(moment=st.datetimes(timezones=st.just(timezone.utc)))
def test_an_aware_stamp_survives_the_round_trip_through_the_state_file(moment: datetime) -> None:
    """What the watchdog writes, it must read back as the same instant.

    `_check_resolutions` stores `_now().isoformat()` and `_alert_is_due` reads it back through
    `_aware_stamp`, with the JSON file in between -- so the throttle is only a 24-hour window
    if that round trip is the identity.
    """
    stored = json.loads(json.dumps({"last_alert": moment.isoformat()}))
    assert watchdog._aware_stamp(stored["last_alert"]) == moment


@given(
    stamp=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=True, allow_infinity=True),
        st.text(max_size=40),
        # Naive, malformed, empty -- and, so the OTHER half of the assertion is reachable at
        # all, two genuinely usable aware stamps. Without them `usable` is False for every
        # draw and the property degenerates into "this always pages", which a throttle that
        # does not work also satisfies. That is the vacuity class docs/LESSONS.md calls the
        # top recurring defect, and it was in this property when it was first written.
        st.just("2026-09-17T12:00:00"),
        st.just("2026-13-45T99:99:99+00:00"),
        st.just(""),
        st.just(ANCHOR.isoformat()),
        st.just((ANCHOR - timedelta(hours=1)).isoformat()),
    )
)
def test_a_stamp_that_cannot_be_read_as_an_aware_instant_pages(stamp: object) -> None:
    """Unreadable means "no record that anyone was told", and the safe answer is to tell.

    The dangerous direction is silence: a throttle that trusts an unparseable or naive stamp
    either raises inside the alerting path or waits forever. Both halves are drawn -- an
    unusable stamp pages, and a usable one inside the window does not.
    """
    codes = ("timer_inactive",)
    stored = {"key": watchdog._throttle_key(codes), "last_alert": stamp}
    usable = isinstance(stamp, str) and not isinstance(stamp, bool) and _parses_as_aware(stamp)
    assert watchdog._alert_is_due(codes, stored, ANCHOR) is not usable


def _parses_as_aware(text: str) -> bool:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None
