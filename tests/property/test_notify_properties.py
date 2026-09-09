"""M1-329: the notifier's contract, fuzzed.

Three claims are worth fuzzing rather than sampling, because each has an unbounded input
and a failure mode that a hand-picked example would not reach.

**Nothing but ``NotifyError`` escapes, for any input.** Every one of these calls sits on the
worker's forecast path, so an exception class the caller does not handle is a lost forecast.

**The throttle is a function of the window, not of the object.** The worker is a
``Type=oneshot`` restarted every five minutes, so "same window" and "next window" have to
hold across a fresh process, and the boundary is arithmetic that is easy to get off by one.

**No input reaches a message.** Subjects, titles and bodies are built from ledger content.

**The strategy draws what it needs to reach rather than hoping for it.** The recurring
defect in this project's property tests is a strategy that cannot reach the branch the
assertion is about, so:

- the clock offset is drawn as a *mode* relative to the window (``same``, ``next``,
  ``far``) and derived from it, never as an independent datetime -- two free datetimes over
  any useful range land in different windows almost every draw, and every assertion about
  the throttled arm would then be about a case the strategy never produces;
- the event is drawn from the real vocabulary for the reachable arms and from junk for the
  refusal arm, so neither branch is starved;
- ``docs/M1-NOTES.md`` records the mutation pass proving each assertion is load-bearing.
"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, get_args

import httpx
import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, assume, given, settings

from whiskeyjack_bot import notify
from whiskeyjack_bot.notify import (
    BUDGET_THRESHOLD_PERCENTS,
    NotifyError,
    NotifyEvent,
    Notifier,
    budget_level_crossed,
    describe,
)

# Low-entropy on purpose; see docs/LESSONS.md on the gitleaks full-history scan.
PLANTED = "privateFAKE123456"
FAKE_TOPIC = "https://ntfy.invalid/wj-fake-topic-0001"

_EVENTS: tuple[str, ...] = get_args(NotifyEvent)

# Where the second send's clock sits relative to the first. Derived from the event's own
# window rather than drawn freely, so both the suppressed and the released arm are reached
# on every run.
_OFFSETS = ("same", "next", "far")


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200)


def _sender(
    root: Path, handler: Any = _ok, *, clock: Any = None, secrets: tuple[str, ...] = ()
) -> Notifier:
    return Notifier(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        topic_url=FAKE_TOPIC,
        state_root=root,
        secret_names=secrets,
        clock=clock or (lambda: datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)),
    )


# Arbitrary text, including the shapes that break naive path and header handling.
_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=1,
    max_size=64,
)


@given(
    event=st.sampled_from(_EVENTS),
    subject=_TEXT,
    title=_TEXT,
    body=st.text(max_size=200),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
def test_a_send_never_raises_anything_but_notify_error(
    tmp_path_factory: pytest.TempPathFactory,
    event: str,
    subject: str,
    title: str,
    body: str,
) -> None:
    """The narrow ``except`` is itself the assertion.

    Anything else reaching a call site is a lost forecast, because these calls sit inside
    ``run_once``'s per-question loop and its recovery pass.
    """
    root = tmp_path_factory.mktemp("notify")
    try:
        outcome = _sender(root).send(event, subject=subject, title=title, body=body)
    except NotifyError:
        return
    assert outcome in get_args(notify.NotifyOutcome)


@given(event=_TEXT, subject=_TEXT)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
def test_an_unrecognized_event_never_writes_and_never_echoes(
    tmp_path_factory: pytest.TempPathFactory, event: str, subject: str
) -> None:
    """Refused before any I/O, and the refusal does not repeat the input."""
    assume(event not in _EVENTS)
    root = tmp_path_factory.mktemp("notify")
    handler_calls: list[int] = []

    def counting(request: httpx.Request) -> httpx.Response:
        handler_calls.append(1)
        return httpx.Response(200)

    with pytest.raises(NotifyError) as caught:
        _sender(root, counting).send(
            event, subject=f"{subject}{PLANTED}", title=PLANTED, body=PLANTED
        )
    assert handler_calls == []
    assert not (root / "notifications").exists()
    rendered = "".join(traceback.format_exception(caught.value))
    assert PLANTED not in rendered


@st.composite
def _repeat_cases(draw: st.DrawFn) -> tuple[str, str, str]:
    return draw(st.sampled_from(_EVENTS)), draw(_TEXT), draw(st.sampled_from(_OFFSETS))


@given(case=_repeat_cases())
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
def test_the_throttle_is_decided_by_the_window_and_survives_a_restart(
    tmp_path_factory: pytest.TempPathFactory, case: tuple[str, str, str]
) -> None:
    """Same window suppresses, a later window releases -- across separate processes.

    The second send is made by a **new** ``Notifier`` with a new client, which is what the
    next timer firing actually is. An in-memory guard would pass the first half of this and
    fail the second only in production, 288 times a day.
    """
    event, subject, offset = case
    root = tmp_path_factory.mktemp("notify")
    window = notify._WINDOW_SECONDS[event]
    start = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    # Anchor to the base of the window so "same" is genuinely the same index. Without this
    # a start near a boundary plus a sub-window offset would sometimes cross it, and the
    # suppressed arm would be flaky rather than wrong -- the worst kind.
    start = datetime.fromtimestamp((int(start.timestamp()) // window) * window, timezone.utc)
    later = {
        "same": start + timedelta(seconds=window - 1),
        "next": start + timedelta(seconds=window),
        "far": start + timedelta(seconds=window * 5 + 3),
    }[offset]

    first = _sender(root, clock=lambda: start)
    assert first.send(event, subject=subject, title="t", body="b") == "sent"
    first.close()

    # A brand-new process: nothing in memory carries over.
    second = _sender(root, clock=lambda: later)
    outcome = second.send(event, subject=subject, title="t", body="b")
    second.close()
    assert outcome == ("throttled" if offset == "same" else "sent")


@given(event=st.sampled_from(_EVENTS), subject=_TEXT)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
def test_a_stamp_is_always_one_safe_component_under_the_state_root(
    tmp_path_factory: pytest.TempPathFactory, event: str, subject: str
) -> None:
    """A subject is ledger content and never becomes a path component.

    Hashed rather than sanitized, because hashing is total over every string: scopes carry
    ``:``, and ``require_safe_component`` would refuse those outright.
    """
    root = tmp_path_factory.mktemp("notify")
    assert _sender(root).send(event, subject=subject, title="t", body="b") == "sent"
    stamps = list((root / "notifications").iterdir())
    assert len(stamps) == 1
    name = stamps[0].name
    assert stamps[0].parent == root / "notifications"
    assert stamps[0].resolve().is_relative_to(root.resolve())
    assert "/" not in name and "\\" not in name and ".." not in name
    assert name.startswith(f"{event}-")


@given(title=st.text(max_size=300))
@settings(max_examples=200)
def test_a_title_never_carries_a_header_separator(title: str) -> None:
    """A CR or LF in a header value is the one shape that could split a request."""
    safe = notify._header_safe(title)
    assert safe
    assert not any(c in safe for c in "\r\n\x00")
    assert len(safe) <= 200
    # Any whitespace run collapses, so the result is never merely truncated mid-newline.
    assert safe == " ".join(safe.split())


@given(
    used=st.integers(min_value=0, max_value=10**9),
    ceiling=st.integers(min_value=1, max_value=10**9),
)
@settings(max_examples=200)
def test_a_budget_level_is_always_one_of_the_configured_levels(used: int, ceiling: int) -> None:
    level = budget_level_crossed(used, ceiling)
    assert level is None or level in BUDGET_THRESHOLD_PERCENTS
    if level is not None:
        assert used * 100 >= ceiling * level


@given(
    lower=st.integers(min_value=0, max_value=10**9),
    extra=st.integers(min_value=0, max_value=10**9),
    ceiling=st.integers(min_value=1, max_value=10**9),
)
@settings(max_examples=200)
def test_a_budget_level_never_falls_as_spending_rises(lower: int, extra: int, ceiling: int) -> None:
    """Monotone in ``used``: more spend can only reach a higher level, never a lower one.

    Reported levels are compared as numbers with ``None`` read as zero, because ``None``
    means "below every level" and is the bottom of the same order.
    """
    below = budget_level_crossed(lower, ceiling) or 0
    above = budget_level_crossed(lower + extra, ceiling) or 0
    assert above >= below


@given(
    ceiling=st.integers(min_value=1, max_value=10**9),
    percent=st.sampled_from(BUDGET_THRESHOLD_PERCENTS),
)
@settings(max_examples=200)
def test_each_configured_level_is_reachable(ceiling: int, percent: int) -> None:
    """Guards the vacuity of the two properties above.

    If no ``used`` could reach a level, both would hold trivially for it. Spending exactly
    the level's share of the ceiling must report at least that level.
    """
    reported = budget_level_crossed((ceiling * percent + 99) // 100, ceiling)
    assert reported is not None and reported >= percent


@given(
    pairs=st.lists(
        st.tuples(st.text(alphabet="abcdefghijklmnop_", min_size=1, max_size=12), st.integers()),
        max_size=12,
    )
)
@settings(max_examples=200)
def test_describe_renders_every_pair_exactly_once(pairs: list[tuple[str, int]]) -> None:
    rendered = describe(pairs)
    assert len(rendered.split()) == len(pairs)
    for label, value in pairs:
        assert f"{label}={value}" in rendered


@given(subject=_TEXT, title=_TEXT, body=_TEXT, event=st.sampled_from(_EVENTS))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
def test_a_failed_push_leaks_neither_the_topic_nor_the_message(
    tmp_path_factory: pytest.TempPathFactory,
    caplog: pytest.LogCaptureFixture,
    event: str,
    subject: str,
    title: str,
    body: str,
) -> None:
    """The degrade path is where a leak would be least noticed, so it is fuzzed too."""
    root = tmp_path_factory.mktemp("notify")

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"{FAKE_TOPIC} refused: {PLANTED}")

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        outcome = _sender(root, failing).send(
            event, subject=subject, title=f"{title}{PLANTED}", body=f"{body}{PLANTED}"
        )
    assert outcome == "failed"
    assert FAKE_TOPIC not in caplog.text
    assert PLANTED not in caplog.text
