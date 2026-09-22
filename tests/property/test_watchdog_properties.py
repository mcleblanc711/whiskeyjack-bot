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


# ── the stall rule's three new pure functions (M1-343) ───────────────────────
#
# `_parse_unit_timestamp` reads a string systemd rendered, `_observe` reads a JSON object a
# person can edit, and `_timer_stalled` decides over both. Every one of them sits on the path
# that decides whether an operator hears that ingestion has stopped firing, so the properties
# are the same three the file already asserts of the others -- total, exact rather than
# approximate, and silent only where silence is the contract -- plus one this rule needs of its
# own: **each guard is decisive on its own**, because a stall refused by the arithmetic when it
# should have been refused by the never-fired guard is a test that passes for the wrong reason.

WINDOW = watchdog.RESOLUTIONS_INTERVAL + watchdog.RESOLUTIONS_STALE_MARGIN
# Older than the window, and younger than it: both halves of every guard must be reachable.
STALE_ENOUGH = ANCHOR - WINDOW - timedelta(hours=2)
RECENT = ANCHOR - timedelta(minutes=30)
STAMPS = st.one_of(
    st.none(),
    st.just(STALE_ENOUGH),
    st.just(RECENT),
    st.just(ANCHOR - WINDOW),  # the boundary itself
    st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2030, 1, 1),
        timezones=st.just(timezone.utc),
    ),
)
# What `systemctl show` can hand the parser, including the local-zone rendering an un-pinned TZ
# would produce -- the one string that looks exactly right and must not parse.
RENDERINGS = st.one_of(
    st.just(""),
    st.just("unknown"),
    st.just("n/a"),
    st.just("infinity"),
    st.just("Sat 2026-09-19 12:23:18 UTC"),
    st.just("Sat 2026-09-19 12:23:18 MDT"),
    st.just("Sat 2026-09-19 12:23:18"),
    st.just("Sat 2026-13-45 99:99:99 UTC"),
    st.just("   Sat  2026-09-19   12:23:18   UTC  "),
    st.datetimes(timezones=st.just(timezone.utc)).map(
        lambda moment: moment.strftime("%a %Y-%m-%d %H:%M:%S UTC")
    ),
    st.text(max_size=40),
)


@given(raw=RENDERINGS)
def test_the_unit_timestamp_parser_is_total_and_returns_only_aware_instants(raw: str) -> None:
    parsed = watchdog._parse_unit_timestamp(raw)
    assert parsed is None or isinstance(parsed, datetime)
    if parsed is not None:
        assert parsed.tzinfo is not None
        assert isinstance(ANCHOR - parsed, timedelta)
        # Only a UTC rendering parses: an abbreviated local zone is a value this program cannot
        # place on the timeline, and reading `MDT` as UTC would be wrong by hours in silence.
        assert raw.split()[-1] == "UTC"


@given(
    # Bounded to years systemd's own four-digit `%Y` rendering round-trips: a year before 1000
    # renders three digits and does not read back, which is a shape no host clock produces and
    # which the parser correctly refuses rather than guesses at.
    moment=st.datetimes(
        min_value=datetime(1000, 1, 1),
        max_value=datetime(9999, 12, 31),
        timezones=st.just(timezone.utc),
    ).map(lambda m: m.replace(microsecond=0))
)
def test_what_systemd_renders_is_what_the_parser_reads_back(moment: datetime) -> None:
    """The round trip is the identity, at the one-second resolution systemd prints.

    Measured against the live host before it was asserted here: the real
    `LastTriggerUSec` of `whiskeyjack-resolutions.timer` read back as the instant
    `systemctl --user list-timers` reports for it.
    """
    rendered = moment.strftime("%a %Y-%m-%d %H:%M:%S UTC")
    assert watchdog._parse_unit_timestamp(rendered) == moment


@given(
    active=ACTIVE_ANSWERS,
    enabled=ENABLED_ANSWERS,
    last_trigger=STAMPS,
    active_since=STAMPS,
    observed_since=st.one_of(st.just(STALE_ENOUGH), st.just(RECENT), st.just(ANCHOR - WINDOW)),
    drift=st.integers(min_value=0, max_value=200_000),
)
def test_the_stall_decision_is_total_and_monotone_in_time(
    active: str,
    enabled: str,
    last_trigger: datetime | None,
    active_since: datetime | None,
    observed_since: datetime,
    drift: int,
) -> None:
    """Never raises, always a bool, and a stall does not un-stall as the clock runs.

    Monotonicity is the property that makes the throttle meaningful: every age this decides on
    grows with `now`, so a condition reported once stays reported until something about the
    timer changes, rather than flickering across the boundary run by run.
    """
    stalled = watchdog._timer_stalled(
        active, enabled, last_trigger, active_since, observed_since, ANCHOR
    )
    assert isinstance(stalled, bool)
    later = watchdog._timer_stalled(
        active,
        enabled,
        last_trigger,
        active_since,
        observed_since,
        ANCHOR + timedelta(seconds=drift),
    )
    if stalled:
        assert later is True


@given(
    guard=st.sampled_from(
        ["active", "enabled", "never_fired", "fired_recently", "restarted", "unwatched"]
    ),
    unusable=st.booleans(),
    other=UNIT_WORDS,
)
def test_every_guard_refuses_a_stall_on_its_own(guard: str, unusable: bool, other: str) -> None:
    """Break one condition at a time, from a case that really does stall.

    The base case is asserted to stall first, so this cannot degenerate into "a detector that
    never fires refuses everything" -- the vacuity class docs/LESSONS.md calls the top recurring
    defect, and the reason this file draws each pivotal value as its own branch.
    """
    case = {
        "timer_active": "active",
        "timer_enabled": "enabled",
        "last_trigger": STALE_ENOUGH,
        "active_since": STALE_ENOUGH,
        "observed_since": STALE_ENOUGH,
    }
    assert watchdog._timer_stalled(now=ANCHOR, **case) is True, "the base case must stall"

    if guard == "active":
        case["timer_active"] = other if other != "active" else "inactive"
    elif guard == "enabled":
        case["timer_enabled"] = other if other != "enabled" else "disabled"
    elif guard == "never_fired":
        case["last_trigger"] = None
    elif guard == "fired_recently":
        case["last_trigger"] = RECENT
    elif guard == "restarted":
        case["active_since"] = None if unusable else RECENT
    else:
        case["observed_since"] = RECENT

    assert watchdog._timer_stalled(now=ANCHOR, **case) is False, guard


@given(
    stored=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.text(max_size=20),
        st.lists(st.integers(), max_size=3),
        st.dictionaries(
            st.sampled_from(["since", "last_seen", "unexpected"]),
            st.one_of(
                st.none(),
                st.integers(),
                st.text(max_size=30),
                st.just("2026-09-17T12:00:00"),
                st.just(ANCHOR.isoformat()),
                st.just((ANCHOR - timedelta(days=3)).isoformat()),
                st.just((ANCHOR - timedelta(minutes=5)).isoformat()),
                st.just((ANCHOR + timedelta(days=1)).isoformat()),
            ),
            max_size=3,
        ),
    )
)
def test_the_observation_record_is_total_and_never_claims_more_than_it_has(
    stored: object,
) -> None:
    """Untrusted shape in, a usable instant out, and never one in the future.

    The direction that matters: this may lose a window it had, never invent one. A `since` this
    program cannot read, or one that post-dates `now`, means the record starts here -- which
    costs a page it might have sent and can never manufacture one.
    """
    state: dict = {"observed": stored}
    since = watchdog._observe(state, ANCHOR)
    assert isinstance(since, datetime) and since.tzinfo is not None
    assert since <= ANCHOR
    # What it wrote back is what it will read next run: JSON, and the same parser.
    written = json.loads(json.dumps(state["observed"]))
    assert watchdog._aware_stamp(written["since"]) == since
    assert watchdog._aware_stamp(written["last_seen"]) == ANCHOR


@given(
    gap=st.integers(min_value=0, max_value=7200), age=st.integers(min_value=0, max_value=200_000)
)
def test_the_record_survives_a_missed_run_and_not_a_gap(gap: int, age: int) -> None:
    """Two-sided on the tolerance: it accumulates inside it, and restarts outside it."""
    since = ANCHOR - timedelta(seconds=age)
    last_seen = ANCHOR - timedelta(seconds=gap)
    if last_seen < since:
        since = last_seen
    state = {"observed": {"since": since.isoformat(), "last_seen": last_seen.isoformat()}}
    kept = watchdog._observe(state, ANCHOR)
    assert kept == (since if gap <= watchdog.WATCHDOG_GAP_TOLERANCE.total_seconds() else ANCHOR)


@given(
    key=st.one_of(
        st.none(),
        st.integers(),
        st.text(max_size=30),
        st.just("timer_stalled"),
        st.just("service_failed,timer_stalled"),
        st.just("timer_disabled,timer_inactive"),
    ),
    last_trigger=STAMPS,
)
def test_only_a_stored_stall_can_leave_a_recovery_unverified(
    key: object, last_trigger: datetime | None
) -> None:
    """Total over an edited state file, and silent about the other three codes.

    Those three are read straight off systemd, so their absence IS the recovery. Only
    `timer_stalled` can stop being reported for a reason that says nothing about the timer.
    """
    unverified = watchdog._stall_unverified({"key": key}, last_trigger, ANCHOR)
    assert isinstance(unverified, bool)
    carries_stall = isinstance(key, str) and "timer_stalled" in key.split(",")
    if not carries_stall:
        assert unverified is False
    else:
        assert unverified == (last_trigger is None or ANCHOR - last_trigger >= WINDOW)


# ── M1-347: the rollover check's parsers and its decision ─────────────────────
#
# Three parsers over untrusted shape -- two Metaculus answers and one ledger column -- and one
# decision. The strategies name the VALID shapes as their own branches (a positive int id, a
# `results` list of objects, an activation row that carries one), because arbitrary JSON almost
# never produces them, and a parser property whose draws never reach the accept branch passes
# against a parser that accepts nothing.

JSON_LEAVES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False),
    st.text(max_size=20),
)
JSON_VALUES = st.recursive(
    JSON_LEAVES,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=12,
)
PROJECT_IDS = st.one_of(
    st.integers(min_value=1, max_value=2**63),
    st.integers(max_value=0),
    st.booleans(),
    st.integers(min_value=1).map(str),
    st.integers(min_value=1).map(float),
    st.none(),
)
PROJECT_ANSWERS = st.one_of(
    st.fixed_dictionaries({"id": PROJECT_IDS}, optional={"slug": JSON_VALUES}),
    JSON_VALUES,
)
STATUS_WORDS = st.sampled_from(["open", "closed", "resolved", "upcoming", "Open", "open "])
# `status` is required in the first branch and drawn mostly from real vocabulary, so a list of
# posts that ALL carry a string status -- the only shape that is counted -- is common rather than
# the product of several independent coin flips. The malformed statuses (`None`, `[]`, a bool,
# absent) come from the leaves and from the bare-JSON branch.
POSTS = st.one_of(
    st.fixed_dictionaries(
        {"status": st.one_of(STATUS_WORDS, JSON_LEAVES)},
        optional={"id": JSON_LEAVES},
    ),
    st.fixed_dictionaries({}, optional={"id": JSON_LEAVES}),
    JSON_VALUES,
)
POSTS_ANSWERS = st.one_of(
    # Well-formed answers as their own branch: without it ~6% of draws reach a counted list and
    # ~1% one with an open post (measured over 2000 draws), so the counting assertion would rest
    # on a handful of examples per run.
    st.fixed_dictionaries(
        {"results": st.lists(st.fixed_dictionaries({"status": STATUS_WORDS}), max_size=6)}
    ),
    st.fixed_dictionaries({"results": st.lists(POSTS, max_size=6)}, optional={"next": JSON_VALUES}),
    st.fixed_dictionaries({"results": JSON_VALUES}),
    JSON_VALUES,
)


@given(payload=PROJECT_ANSWERS)
def test_the_project_parser_is_total_and_accepts_exactly_a_positive_int_id(
    payload: object,
) -> None:
    got = watchdog._project_id(payload)
    identifier = payload.get("id") if isinstance(payload, dict) else None
    valid = type(identifier) is int and identifier > 0
    assert got == (identifier if valid else None)
    assert got is None or type(got) is int


@given(payload=POSTS_ANSWERS)
def test_the_open_post_count_is_total_and_counts_only_posts_that_say_open(
    payload: object,
) -> None:
    got = watchdog._open_post_count(payload)
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not all(
        isinstance(post, dict) and type(post.get("status")) is str for post in results
    ):
        assert got is None
        return
    assert got == len([post for post in results if post.get("status") == "open"])
    assert 0 <= got <= len(results)


@given(
    raw=st.one_of(
        PROJECT_IDS.map(lambda value: json.dumps({"project_id": value, "account_id": 1})),
        JSON_VALUES.map(json.dumps),
        st.text(max_size=40),
        JSON_VALUES,
        st.just("[" * 5000),
    )
)
def test_the_activation_parser_is_total_and_never_reads_a_non_int_as_a_project(
    raw: object,
) -> None:
    got = watchdog._activation_project_id(raw)
    assert got is None or (type(got) is int and got > 0)
    if got is not None:
        assert isinstance(raw, str)
        assert json.loads(raw)["project_id"] == got


@given(
    activation=st.integers(min_value=1, max_value=10**6),
    slug=st.integers(min_value=1, max_value=10**6),
    open_posts=st.one_of(st.none(), st.integers(min_value=0, max_value=100)),
    same=st.booleans(),
)
def test_a_rollover_is_exactly_a_mismatch_with_something_open(
    activation: int, slug: int, open_posts: int | None, same: bool
) -> None:
    """`same` lifts the match case off its ~1-in-a-million natural rate."""
    slug = activation if same else slug
    expected = activation != slug and open_posts is not None and open_posts >= 1
    assert watchdog._is_rollover(activation, slug, open_posts) is expected


@given(
    a=st.tuples(st.integers(min_value=1), st.integers(min_value=1)),
    b=st.tuples(st.integers(min_value=1), st.integers(min_value=1)),
)
def test_the_rollover_key_names_the_pair_and_only_the_pair(
    a: tuple[int, int], b: tuple[int, int]
) -> None:
    assert (watchdog._rollover_key(*a) == watchdog._rollover_key(*b)) is (a == b)
    assert watchdog._rollover_key(*a).startswith("rollover:")
    assert watchdog._rollover_key(*a) != "rollover_check_failed"


@given(
    key=st.sampled_from(["rollover:33125->33130", "rollover_check_failed", "timer_inactive"]),
    stored_key=st.one_of(
        st.sampled_from(["rollover:33125->33130", "rollover_check_failed"]), JSON_LEAVES
    ),
    stamp=st.one_of(st.just(ANCHOR.isoformat()), JSON_LEAVES, st.just("2026-09-17T12:00:00")),
    window_minutes=st.sampled_from([60, 360, 1440]),
    elapsed=st.integers(min_value=0, max_value=200_000),
)
def test_the_shared_throttle_rule_is_total_and_silent_only_for_its_own_key_in_window(
    key: str, stored_key: object, stamp: object, window_minutes: int, elapsed: int
) -> None:
    window = timedelta(minutes=window_minutes)
    stored = {"key": stored_key, "last_alert": stamp}
    due = watchdog._key_is_due(key, stored, ANCHOR + timedelta(seconds=elapsed), window)
    if stored_key != key or stamp != ANCHOR.isoformat():
        assert due is True
    else:
        assert due == (elapsed >= window.total_seconds())
