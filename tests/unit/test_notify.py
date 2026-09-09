"""Operational push notifications (M1-329).

The acceptance criterion this file exists to hold up is "no forecast is ever lost to a
notification". Every degrade arm below therefore asserts two things, not one: that the
push failed *and* that the failure did not escape.

Sockets are blocked three ways in this suite, so every test injects
``httpx.MockTransport``. Fake topic URLs are low-entropy on purpose -- CI scans every
branch with gitleaks, and a realistic secret shape here fails unrelated PRs.
"""

from __future__ import annotations

import copy
import json
import logging
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from whiskeyjack_bot import notify
from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.logging_setup import configure_logging
from whiskeyjack_bot.notify import (
    BUDGET_THRESHOLD_PERCENTS,
    CURRENT_NOTIFIER,
    Notifier,
    NotifyError,
    budget_level_crossed,
    build_notifier,
    emit,
    notifier_context,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Low-entropy on purpose (see the module docstring).
FAKE_TOPIC = "https://ntfy.invalid/wj-fake-topic-0001"
PLANTED = "privateFAKE123456"
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


class _Exchange:
    """A MockTransport handler that records requests and replays canned responses."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "url": str(request.url),
                "body": request.content.decode("utf-8"),
                "headers": dict(request.headers),
            }
        )
        if not self._responses:
            return httpx.Response(200)
        return self._responses[0] if len(self._responses) == 1 else self._responses.pop(0)


def _notifier(
    root: Path,
    handler: Any,
    *,
    clock: Any = None,
    secret_names: tuple[str, ...] = (),
) -> Notifier:
    # ``artifacts`` rather than ``root`` itself: production passes
    # ``config.storage.artifact_root``, and a test whose stamps live somewhere the real
    # notifier would never write cannot see a mistake about where they go.
    return Notifier(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        topic_url=FAKE_TOPIC,
        state_root=root / "artifacts",
        secret_names=secret_names,
        clock=clock or (lambda: NOW),
    )


def _ticking(*values: datetime) -> Any:
    """Return a clock that yields each value once, then repeats the last."""
    remaining = list(values)
    return lambda: remaining.pop(0) if len(remaining) > 1 else remaining[0]


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    data["storage"]["artifact_root"] = str(tmp_path / "artifacts")
    data["notify"] = {"enabled": True, "topic_url_env": "NTFY_TOPIC_URL"}
    return validate_config_data(data)


# ── the request that actually goes out ───────────────────────────────────────


def test_the_topic_url_is_posted_verbatim(tmp_path: Path) -> None:
    """No trailing slash, no path merging.

    httpx normalizes a ``base_url`` by appending ``/`` and then merges relative paths
    against it, which would turn the configured topic into a different URL. The topic is
    a whole path, not a prefix, so it is posted absolutely.
    """
    handler = _Exchange()
    assert (
        _notifier(tmp_path, handler).send("poll_summary", subject="worker", title="t", body="b")
        == "sent"
    )
    assert handler.requests[0]["url"] == FAKE_TOPIC


def test_the_message_carries_the_ntfy_headers(tmp_path: Path) -> None:
    handler = _Exchange()
    _notifier(tmp_path, handler).send(
        "question_blocked", subject="33108:45452", title="blocked", body="why"
    )
    headers = handler.requests[0]["headers"]
    assert headers["title"] == "blocked"
    assert headers["priority"] == "high"
    assert handler.requests[0]["body"] == "why"


def test_a_multiline_title_cannot_split_the_request(tmp_path: Path) -> None:
    """Titles are assembled from ledger-derived strings, so nothing is assumed single-line."""
    handler = _Exchange()
    _notifier(tmp_path, handler).send(
        "poll_summary", subject="worker", title="one\r\nX-Injected: yes\nthree", body="b"
    )
    headers = handler.requests[0]["headers"]
    assert headers["title"] == "one X-Injected: yes three"
    assert "x-injected" not in headers


# ── a failed or slow push degrades rather than blocks ────────────────────────


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(lambda request: httpx.Response(500), id="server_error"),
        pytest.param(lambda request: httpx.Response(404), id="unknown_topic"),
        pytest.param(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("no route")),
            id="connect_error",
        ),
        pytest.param(
            lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("too slow")),
            id="timeout",
        ),
    ],
)
def test_a_failed_push_never_propagates(tmp_path: Path, handler: Any) -> None:
    assert (
        _notifier(tmp_path, handler).send(
            "provider_failed", subject="asknews:1", title="t", body="b"
        )
        == "failed"
    )


def test_a_transport_exception_does_not_leak_the_topic_url(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """httpx errors quote the request, and this request's URL is the credential.

    The exception is discarded rather than inspected, so what an operator gets is our own
    literal plus the event name.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed connecting to {FAKE_TOPIC} with {PLANTED}")

    with caplog.at_level(logging.DEBUG):
        assert (
            _notifier(tmp_path, handler).send(
                "provider_failed", subject="asknews:1", title="t", body="b"
            )
            == "failed"
        )
    assert FAKE_TOPIC not in caplog.text
    assert PLANTED not in caplog.text
    assert "provider_failed" in caplog.text


def test_a_rejected_push_does_not_log_the_third_party_response_body(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    handler = _Exchange(httpx.Response(403, text=f"denied for {PLANTED}"))
    with caplog.at_level(logging.DEBUG):
        assert (
            _notifier(tmp_path, handler).send("poll_summary", subject="worker", title="t", body="b")
            == "failed"
        )
    assert PLANTED not in caplog.text
    assert "403" in caplog.text


def test_a_slow_push_does_not_block(tmp_path: Path, deadline: None) -> None:
    """The ``deadline`` fixture is the point: a regression here is a hang, not a wrong answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("the topic host never answered")

    assert (
        _notifier(tmp_path, handler).send("poll_summary", subject="worker", title="t", body="b")
        == "failed"
    )


def test_emit_absorbs_a_caller_mistake_rather_than_losing_the_caller(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``Notifier.send`` raises for a bad event; ``emit`` -- what call sites use -- does not.

    A wrong event name is a bug worth a loud log line, and is never worth losing the
    forecast that was mid-flight when it was reached.
    """
    handler = _Exchange()
    with notifier_context(_notifier(tmp_path, handler)):
        with pytest.raises(NotifyError):
            CURRENT_NOTIFIER.get().send(  # type: ignore[union-attr]
                "not_an_event", subject="s", title="t", body="b"
            )
        with caplog.at_level(logging.DEBUG):
            assert emit("not_an_event", subject="s", title="t", body="b") == "failed"
    assert handler.requests == []


def test_emit_without_a_notifier_does_nothing_at_all(tmp_path: Path) -> None:
    """The unconfigured path is the common one and must be inert, not merely harmless."""
    assert CURRENT_NOTIFIER.get() is None
    assert emit("poll_summary", subject="worker", title="t", body="b") == "disabled"


def test_a_base_exception_is_not_swallowed(tmp_path: Path) -> None:
    """``phase_timeout`` raises a BaseException subclass precisely so it cannot be eaten."""

    class _Bound(BaseException):
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        raise _Bound("phase timeout")

    with pytest.raises(_Bound):
        _notifier(tmp_path, handler).send("poll_summary", subject="w", title="t", body="b")


# ── the durable throttle ─────────────────────────────────────────────────────


def test_the_second_push_in_a_window_is_throttled(tmp_path: Path) -> None:
    handler = _Exchange()
    sender = _notifier(tmp_path, handler)
    assert sender.send("question_blocked", subject="a", title="t", body="b") == "sent"
    assert sender.send("question_blocked", subject="a", title="t", body="b") == "throttled"
    assert len(handler.requests) == 1


def test_a_different_subject_is_not_throttled(tmp_path: Path) -> None:
    """Two questions blocking in one window are two incidents, not a repeat."""
    handler = _Exchange()
    sender = _notifier(tmp_path, handler)
    assert sender.send("question_blocked", subject="a", title="t", body="b") == "sent"
    assert sender.send("question_blocked", subject="b", title="t", body="b") == "sent"
    assert len(handler.requests) == 2


def test_the_throttle_survives_a_simulated_restart(tmp_path: Path) -> None:
    """The worker is a Type=oneshot restarted every five minutes.

    In-memory dedup would page 288 times a day, so the state must outlive the process.
    The restart is simulated by discarding every in-process object and rebuilding from the
    same directory -- which is exactly what the next timer firing does.
    """
    handler = _Exchange()
    assert (
        _notifier(tmp_path, handler).send("question_blocked", subject="a", title="t", body="b")
        == "sent"
    )

    # A brand-new process: new client, new notifier, same state directory, same window.
    assert (
        _notifier(tmp_path, _Exchange()).send("question_blocked", subject="a", title="t", body="b")
        == "throttled"
    )

    # And it releases once the window has passed, or the throttle would be a mute button.
    window = notify._WINDOW_SECONDS["question_blocked"]
    later = NOW + timedelta(seconds=window + 1)
    fresh = _Exchange()
    assert (
        _notifier(tmp_path, fresh, clock=lambda: later).send(
            "question_blocked", subject="a", title="t", body="b"
        )
        == "sent"
    )
    assert len(fresh.requests) == 1


def test_the_stamp_file_alone_decides_the_throttle(tmp_path: Path) -> None:
    """The file is both necessary and sufficient -- nothing in memory participates.

    The mutation pass is why this test exists. "Rebuild the notifier and send again"
    looked like a restart test and was not one: a throttle held in a *class* attribute
    survives a new instance in the same interpreter, so that mutant passed while being
    exactly the 288-a-day bug the durable throttle exists to prevent. A restart test whose
    only witness is inside the program cannot see the difference.

    So the witness is the filesystem, in both directions: removing the stamp must re-arm
    the push (necessary), and a stamp this process never sent must suppress one
    (sufficient). No in-memory scheme can satisfy both.
    """
    stamps = tmp_path / "artifacts" / "notifications"
    assert (
        _notifier(tmp_path, _Exchange()).send("question_blocked", subject="a", title="t", body="b")
        == "sent"
    )
    stamp = next(stamps.glob("*.json"))
    contents = stamp.read_bytes()

    # Necessary: with the file gone, the same window is open again. An in-memory throttle
    # would still refuse here, because deleting a file tells it nothing.
    stamp.unlink()
    handler = _Exchange()
    assert (
        _notifier(tmp_path, handler).send("question_blocked", subject="a", title="t", body="b")
        == "sent"
    )
    assert len(handler.requests) == 1

    # Sufficient: a stamp put there by something other than a send suppresses one. This is
    # the other process's write, in the only form a test can stage it.
    for existing in stamps.glob("*.json"):
        existing.unlink()
    stamp.write_bytes(contents)
    quiet = _Exchange()
    assert (
        _notifier(tmp_path, quiet).send("question_blocked", subject="a", title="t", body="b")
        == "throttled"
    )
    assert quiet.requests == []


def test_the_window_is_per_event(tmp_path: Path) -> None:
    """A daily digest and a 30-minute incident alert cannot share one number."""
    assert notify._WINDOW_SECONDS["poll_summary"] == 86400
    assert notify._WINDOW_SECONDS["question_blocked"] == 1800
    handler = _Exchange()
    sender = _notifier(tmp_path, handler)
    sender.send("question_blocked", subject="a", title="t", body="b")
    # Same subject, different event: a separate stamp, so it is not suppressed.
    assert sender.send("poll_summary", subject="a", title="t", body="b") == "sent"


def test_every_event_has_a_positive_window() -> None:
    """A missing entry would be a silently unthrottled event -- the 288-a-day failure."""
    from typing import get_args

    assert set(notify._WINDOW_SECONDS) == set(get_args(notify.NotifyEvent))
    assert all(seconds > 0 for seconds in notify._WINDOW_SECONDS.values())
    assert set(notify._PRIORITY) == set(get_args(notify.NotifyEvent))


def test_a_subject_never_becomes_a_path_component(tmp_path: Path) -> None:
    """Subjects carry ``:`` (scopes) and could carry worse; they are hashed, not sanitized."""
    handler = _Exchange()
    sender = _notifier(tmp_path, handler)
    assert (
        sender.send("question_blocked", subject="../../etc/passwd", title="t", body="b") == "sent"
    )
    stamps = list((sender.state_root / "notifications").glob("*.json"))
    assert len(stamps) == 1
    assert "passwd" not in stamps[0].name
    assert ".." not in stamps[0].name


def test_an_unwritable_state_directory_fails_closed_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A notifier that cannot record what it sent would send 288 times a day.

    A muted channel reports nothing at all, so this direction is deliberate. It is logged
    rather than only returned, because a silent alerting path is the condition this module
    exists to end -- and the daily digest going missing is the backstop.
    """
    handler = _Exchange()
    blocked = tmp_path / "artifacts"
    blocked.write_text("not a directory", encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        assert (
            _notifier(tmp_path, handler).send("question_blocked", subject="a", title="t", body="b")
            == "throttled"
        )
    assert handler.requests == []
    assert "stamp could not be written" in caplog.text


def test_a_stamp_records_only_the_event_and_the_instant(tmp_path: Path) -> None:
    """The stamp is operational residue, so it holds nothing worth protecting."""
    _notifier(tmp_path, _Exchange()).send(
        "question_blocked", subject=f"33108:{PLANTED}", title="t", body="b"
    )
    stamp = next((tmp_path / "artifacts" / "notifications").glob("*.json"))
    body = json.loads(stamp.read_text(encoding="utf-8"))
    assert body == {"event": "question_blocked", "at": NOW.isoformat()}
    assert PLANTED not in stamp.read_text(encoding="utf-8")
    assert PLANTED not in stamp.name


def test_one_clock_reading_decides_the_whole_claim(tmp_path: Path) -> None:
    """The stamp path and its payload must not straddle a window boundary.

    Two readings could file the stamp recording window N under N+1, and the throttle
    would then be off by a window whenever a poll landed on the edge.
    """
    window = notify._WINDOW_SECONDS["question_blocked"]
    edge = datetime.fromtimestamp((int(NOW.timestamp()) // window + 1) * window, timezone.utc)
    sender = _notifier(tmp_path, _Exchange(), clock=_ticking(edge - timedelta(seconds=1), edge))
    assert sender.send("question_blocked", subject="a", title="t", body="b") == "sent"
    stamp = next((tmp_path / "artifacts" / "notifications").glob("*.json"))
    index = int(stamp.stem.rsplit("-", 1)[1])
    recorded = datetime.fromisoformat(json.loads(stamp.read_text(encoding="utf-8"))["at"])
    assert int(recorded.timestamp()) // window == index


# ── hostile clocks ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "clock",
    [
        pytest.param(lambda: datetime(2026, 9, 9, 12, 0), id="naive"),
        pytest.param(lambda: "2026-09-09T12:00:00Z", id="not_a_datetime"),
        pytest.param(lambda: None, id="none"),
    ],
)
def test_a_clock_that_is_not_an_instant_is_refused(tmp_path: Path, clock: Any) -> None:
    """A naive datetime would make ``timestamp()`` local-dependent and move every boundary."""
    with pytest.raises(NotifyError):
        _notifier(tmp_path, _Exchange(), clock=clock).send(
            "poll_summary", subject="w", title="t", body="b"
        )


def test_a_raising_clock_does_not_reprint_its_message(tmp_path: Path) -> None:
    def clock() -> datetime:
        raise RuntimeError(f"clock broke: {PLANTED}")

    with pytest.raises(NotifyError) as caught:
        _notifier(tmp_path, _Exchange(), clock=clock).send(
            "poll_summary", subject="w", title="t", body="b"
        )
    assert PLANTED not in str(caught.value)
    # The rendered traceback, per the project convention: ``from None`` sets
    # ``__suppress_context__``, so the original message is not printed even though the
    # object is still chained. That rendering is the channel an operator actually sees.
    assert PLANTED not in "".join(traceback.format_exception(caught.value))


# ── the closed vocabulary ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "event", ["", "nope", "Question_Blocked", "question_blocked ", "poll-summary", "*", "0"]
)
def test_an_event_outside_the_vocabulary_raises_and_writes_nothing(
    tmp_path: Path, event: str
) -> None:
    """Checked before any I/O: no stamp is claimed and nothing is posted.

    An unrecognized event falling through would claim a stamp under a name no window
    covers, and the throttle would then be whatever the mapping happened to contain.
    """
    handler = _Exchange()
    with pytest.raises(NotifyError) as caught:
        _notifier(tmp_path, handler).send(event, subject="s", title="t", body="b")
    # The message states the accepted values -- this module's own literals -- and says the
    # rejected one is withheld.
    assert "offending input withheld" in str(caught.value)
    assert handler.requests == []
    assert not (tmp_path / "artifacts" / "notifications").exists()


def test_a_rejected_event_name_is_not_echoed_back(tmp_path: Path) -> None:
    """The name could be anything a caller built, so it is withheld like any other input."""
    with pytest.raises(NotifyError) as caught:
        _notifier(tmp_path, _Exchange()).send(
            f"blocked_{PLANTED}", subject="s", title="t", body="b"
        )
    assert PLANTED not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("subject", ["", None, 3, b"bytes"])
def test_a_subject_that_is_not_a_non_empty_string_is_refused(tmp_path: Path, subject: Any) -> None:
    handler = _Exchange()
    with pytest.raises(NotifyError):
        _notifier(tmp_path, handler).send("poll_summary", subject=subject, title="t", body="b")
    assert handler.requests == []


# ── secret hygiene ───────────────────────────────────────────────────────────


def test_the_topic_url_is_not_in_the_notifier_repr(tmp_path: Path) -> None:
    """A dataclass repr is exactly the thing that ends up in a traceback or a debug log."""
    sender = _notifier(tmp_path, _Exchange())
    for rendered in (repr(sender), str(sender), f"{sender}", format(sender)):
        assert FAKE_TOPIC not in rendered


def test_configured_logging_redacts_the_topic_url(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The end-to-end proof that registering the NAME is enough to scrub the VALUE."""
    monkeypatch.setenv("NTFY_TOPIC_URL", FAKE_TOPIC)
    configure_logging(config)
    logging.getLogger("httpx").warning("posting to %s", FAKE_TOPIC)
    captured = capsys.readouterr()
    file_text = config.logging.file.read_text(encoding="utf-8")
    assert FAKE_TOPIC not in captured.err
    assert FAKE_TOPIC not in file_text
    assert "<redacted:NTFY_TOPIC_URL>" in file_text


def test_a_secret_in_a_message_body_never_leaves_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ntfy is a third party; a body is redacted before it is sent, not after."""
    monkeypatch.setenv("METACULUS_TOKEN", PLANTED)
    handler = _Exchange()
    _notifier(tmp_path, handler, secret_names=("METACULUS_TOKEN",)).send(
        "poll_summary", subject="w", title=f"t {PLANTED}", body=f"b {PLANTED}"
    )
    sent = handler.requests[0]
    assert PLANTED not in sent["body"]
    assert PLANTED not in json.dumps(sent["headers"])
    assert "<redacted:METACULUS_TOKEN>" in sent["body"]


# ── construction from configuration ──────────────────────────────────────────


def test_no_notifier_when_notifications_are_disabled(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NTFY_TOPIC_URL", FAKE_TOPIC)
    off = config.model_copy(update={"notify": config.notify.model_copy(update={"enabled": False})})
    assert build_notifier(off) is None


def test_no_notifier_when_the_topic_is_unset_and_the_name_is_all_that_is_logged(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("NTFY_TOPIC_URL", raising=False)
    with caplog.at_level(logging.DEBUG):
        assert build_notifier(config) is None
    assert "NTFY_TOPIC_URL" in caplog.text


def test_a_built_notifier_reads_the_topic_from_the_environment(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NTFY_TOPIC_URL", FAKE_TOPIC)
    built = build_notifier(config)
    assert built is not None
    try:
        assert built.topic_url == FAKE_TOPIC
        assert built.state_root == config.storage.artifact_root
        assert "NTFY_TOPIC_URL" in built.secret_names
        assert built.client.timeout.read == config.notify.timeout_seconds
        # Never follow a redirect with the credential attached.
        assert built.client.follow_redirects is False
    finally:
        built.close()


def test_the_context_manager_closes_the_client_it_was_given(tmp_path: Path) -> None:
    sender = _notifier(tmp_path, _Exchange())
    with notifier_context(sender):
        assert CURRENT_NOTIFIER.get() is sender
    assert CURRENT_NOTIFIER.get() is None
    assert sender.client.is_closed


def test_the_context_manager_restores_the_previous_notifier(tmp_path: Path) -> None:
    outer = _notifier(tmp_path, _Exchange())
    token = CURRENT_NOTIFIER.set(outer)
    try:
        with notifier_context(_notifier(tmp_path, _Exchange())):
            pass
        assert CURRENT_NOTIFIER.get() is outer
    finally:
        CURRENT_NOTIFIER.reset(token)


# ── budget thresholds ────────────────────────────────────────────────────────


def test_budget_levels_report_the_highest_one_reached() -> None:
    ceiling = 10_000_000
    assert budget_level_crossed(0, ceiling) is None
    assert budget_level_crossed(4_999_999, ceiling) is None
    assert budget_level_crossed(5_000_000, ceiling) == 50
    assert budget_level_crossed(7_999_999, ceiling) == 50
    assert budget_level_crossed(8_000_000, ceiling) == 80
    assert budget_level_crossed(999_000_000, ceiling) == 80


def test_the_measured_incident_trajectory_never_crosses_a_level() -> None:
    """The honest half of the budget story, from the Cup ledger (M1-329).

    Nine hours of re-purchased research reached $3.175 of a $10 ceiling -- 31.7%. Neither
    configured level fires. This is asserted rather than left as a note because the
    temptation on the next incident will be to lower the thresholds until they catch a
    stall, and a budget alarm tuned to catch stalls is a budget alarm that pages daily.
    ``question_blocked`` is the event that catches a stall.
    """
    assert budget_level_crossed(3_175_000 + 186_000, 10_000_000) is None


def test_a_ceiling_that_is_not_positive_never_reports_a_level() -> None:
    for ceiling in (0, -1):
        assert budget_level_crossed(5_000_000, ceiling) is None


def test_thresholds_are_ordered_highest_first() -> None:
    """``budget_level_crossed`` returns the first match, so the order is the behaviour."""
    assert list(BUDGET_THRESHOLD_PERCENTS) == sorted(BUDGET_THRESHOLD_PERCENTS, reverse=True)
    assert all(0 < percent < 100 for percent in BUDGET_THRESHOLD_PERCENTS)
