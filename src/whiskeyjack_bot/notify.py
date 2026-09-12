"""Operational push notifications to an ntfy topic (M1-329).

Why this module exists, stated as the incident it is a response to: on 2026-09-08 the
AskNews monthly quota was exhausted and nobody knew for nine hours. The Cup worker
recorded 110 ``question_failure`` rows for one question between 16:38 and 01:40 and
re-bought its research 17 times; the operator learned of it from the vendor's cap email.
Nothing in ``src/``, ``scripts/`` or ``deploy/`` could tell anyone.

Two mechanisms are needed because neither sees both failure shapes. A systemd
``OnFailure`` unit (``deploy/systemd/whiskeyjack-notify@.service``) catches a process that
**dies, times out or exits non-zero** even when this program is broken, but cannot say
why. This module knows *which question and which provider*, but sends nothing if the
process dies first. Neither is a substitute for the other.

Three rules shape everything here.

**A notification never costs a forecast.** Every failure arm degrades: :meth:`Notifier.send`
returns a closed-``Literal`` outcome and does not raise into a caller. The exception from a
failed push is discarded rather than inspected, because an ``httpx`` error quotes the
request and the request URL *is* the credential. **Slow** is bounded separately from
**failed**, and by a different mechanism: ``httpx``'s ``timeout`` bounds each operation and
never their sum, so :func:`_push_deadline` puts a real elapsed-time bound around the push
and hands back whatever is left of the enclosing phase deadline.

**The throttle is durable across processes.** The worker is a ``Type=oneshot`` restarted
every five minutes, so in-memory dedup would page 288 times a day and the channel would be
muted, which is the same outcome as having no channel. The state is a stamp file claimed
through :func:`artifacts.write_new_file`, whose ``os.link`` fails ``EEXIST`` rather than
clobbering and is therefore already a cross-process compare-and-set -- fsynced, reviewed and
property-tested. It is deliberately **not** a ledger table: a notification throttle is
operational state rather than attribution, it would need a migration, and, decisively, the
systemd half has no ledger connection at all. State only the healthy program can reach is
not state an alerting path may depend on.

**Nothing sent here reveals more than the platform already shows.** ntfy is a third party.
``prediction_posted`` carries the question, the post and the posted value -- all public the
moment the forecast lands on Metaculus -- and never a rationale field, which is not public.
Message bodies are passed through :func:`redaction.redact_secrets` before they leave, so a
configured credential cannot ride out in one even by accident.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import Final, Literal, get_args

import httpx

from whiskeyjack_bot.artifacts import write_new_file
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.redaction import redact_secrets
from whiskeyjack_bot.research.transport import apply_connection_retries

_LOGGER = logging.getLogger(__name__)

# The closed event vocabulary. A module-level ``Literal`` alias rather than an
# ``enum.Enum`` per the project convention, validated at runtime with ``get_args`` before
# any I/O -- an unrecognized event falling through would claim a stamp under a name no
# window covers, and the throttle would then be whatever the mapping's iteration order
# happened to be.
NotifyEvent = Literal[
    "question_blocked",
    "provider_failed",
    "budget_threshold",
    "prediction_posted",
    "poll_summary",
    "activation_retired",
]

# What happened to one push. Closed for the same reason the outcome vocabularies in
# ``research/persist.py`` and ``forecast/persist.py`` are closed: a caller that wants to
# log or count the degrade cases must be able to name them, and ``bool`` cannot tell
# "throttled" (working as designed) from "failed" (the push was lost).
NotifyOutcome = Literal["sent", "throttled", "disabled", "failed"]

# How long one event's stamp suppresses the next push of the same subject, in seconds.
# Total over ``NotifyEvent`` and asserted so at import; a missing entry is a silent
# unthrottled event, which is the 288-a-day failure this module exists to avoid.
#
# The windows are per event because the events are not the same kind of thing, and one
# number cannot serve both ends of the range:
#
# - ``question_blocked`` / ``provider_failed`` are incident alerts. 30 minutes matches the
#   worker's own re-attempt cadence, so a genuinely new block pages promptly while a
#   flapping one does not re-page on every five-minute poll.
# - ``budget_threshold`` is keyed by spending scope *and* level, so a day-long window
#   makes it a daily reminder while spend stays above a level, not a per-poll alarm. It is
#   deliberately a reminder rather than a once-ever alert: being at 80% of the ceiling is a
#   condition, not an event, and it stays true until someone acts on it.
# - ``prediction_posted`` already fires at most once per record by construction (it is
#   emitted inside the ``forecast_confirmed`` write guard). The window is a backstop, not
#   the mechanism.
# - ``poll_summary`` is the daily liveness digest -- see :meth:`Notifier.send` for why it
#   is a digest and not a per-poll push.
# - ``activation_retired`` (M1-334) is a condition that holds until an operator re-runs
#   ``tournament enable``: every five-minute poll hits it again. Keyed on the profile's
#   project, so two profiles retiring both page; a day-long window makes one retired profile
#   page once per incident, not 288 times a day.
_WINDOW_SECONDS: Final[dict[str, int]] = {
    "question_blocked": 1800,
    "provider_failed": 1800,
    "budget_threshold": 86400,
    "prediction_posted": 86400,
    "poll_summary": 86400,
    "activation_retired": 86400,
}
assert set(_WINDOW_SECONDS) == set(get_args(NotifyEvent))
assert all(seconds > 0 for seconds in _WINDOW_SECONDS.values())

# Fraction-of-ceiling levels at which ``budget_threshold`` fires, highest first so the
# most severe level a run has crossed is the one reported.
#
# Set against ``actual + held`` -- the exact sum ``tournament_state.Budget.reserve``
# compares to the ceiling before refusing a call -- and not against settled spend alone.
# Measured on the Cup ledger 2026-09-09: AskNews had 43 reservations worth $3.175 and
# **zero** settlements, so ``actual_cost_usd`` for the provider that exhausted the quota
# is permanently $0.00 and a threshold on it can never fire. ``held`` is inflated by that
# same non-settlement, and that is not a reason to discard it: inflated or not, it is the
# number that stops the worker.
#
# Read the levels honestly. On the same ledger the nine-hour incident reached only 31.7%
# of the $10 ceiling, so neither level would have fired during it. This is a budget alarm,
# not a stall alarm; ``question_blocked`` is the one that catches a stall.
BUDGET_THRESHOLD_PERCENTS: Final[tuple[int, ...]] = (80, 50)

# Stamps under this directory, a sibling of ``operations/`` in the artifact root.
# ``tournament_state.check_storage`` globs ``operations/*.json`` and the posting guard
# only, so the restore detector does not see these -- which is correct, because a missing
# notification stamp means a notification may be sent twice, not that spend is unaccounted
# for. ``tests/unit/test_notify.py`` proves that by execution rather than by reading.
_STAMP_SUBDIR: Final = "notifications"

# Stamps older than this are pruned opportunistically on write. They are pure operational
# residue: once a window has passed, its stamp can never suppress anything again.
_STAMP_RETENTION = timedelta(days=7)

# ntfy priorities, as the header value the service expects.
_PRIORITY: Final[dict[str, str]] = {
    "question_blocked": "high",
    "provider_failed": "high",
    "budget_threshold": "high",
    "prediction_posted": "default",
    "poll_summary": "low",
    # Every poll is refusing until an operator acts, so it pages like an incident.
    "activation_retired": "high",
}
assert set(_PRIORITY) == set(get_args(NotifyEvent))


class _PushExpired(BaseException):
    """The elapsed-time deadline for one push fired.

    ``BaseException`` for the same reason ``timeouts._PhaseExpired`` is one: it has to
    escape ``httpx``'s own ``except Exception`` handlers to reach the boundary that
    degrades. It never leaves this module.
    """


# Never hand ``setitimer`` a zero when restoring a live outer timer: zero means *disabled*,
# so an outer phase deadline whose remaining time has already elapsed would be silently
# cancelled rather than fired. This is the smallest positive value that fires promptly.
_MIN_TICK = 1e-6

# Seconds of an enclosing phase deadline that a notification may never borrow. The phase
# bounds in `tournament.py` are 240s and 480s, so this is under half a percent of them, and
# it is what makes "the push finishes strictly before the phase does" true rather than
# nearly true.
_PHASE_MARGIN = 1.0


@contextmanager
def _push_deadline(seconds: float) -> Iterator[None]:
    """Bound the elapsed time of one push, without disturbing an enclosing phase deadline.

    Round 1 finding 1, and the module docstring used to claim this was already handled by
    "a client timeout plus the config cap". That was wrong, and the reviewer was right to
    call it: ``httpx``'s ``timeout`` bounds each *operation* -- connect, write, one read --
    and never the total. A response whose body dribbles in under the read timeout stalls
    for as long as the peer likes. Measured against the pre-fix code: a 0.1s client timeout
    and a 1.5s response returned ``"sent"`` after 1.69s, and inside ``phase_timeout(0.5)``
    a single notification killed the forecasting phase outright.

    ``timeouts.phase_timeout`` cannot simply be nested to fix it. Its ``finally`` runs
    ``setitimer(ITIMER_REAL, 0)``, which cancels whatever timer was already running, so an
    inner use would silently disarm the enclosing 480-second bound -- trading a bug that
    stops one poll for one that never stops it. So this reads the outer timer, borrows from
    it, and puts back what is left:

    The governing rule is that **a notification may never be the reason a phase expires**,
    which decides what happens when the two deadlines disagree. Borrowing the whole of the
    outer's remaining time is not good enough: with 0.5s left on the phase and a 10s push
    budget, letting the outer fire is precisely the reported defect, just relabelled. So the
    push gets ``min(its own budget, outer remaining - _PHASE_MARGIN)`` and is abandoned
    outright when that is not positive. Near the end of a phase the push is squeezed and
    then skipped, which is the correct order of precedence: the forecast is the work, the
    notification is commentary on it.

    - **Not the main thread**: no deadline. ``setitimer`` is main-thread-only, the worker is
      single-threaded, and the per-operation timeouts still apply. Better than raising.
    - **No outer timer**: the push gets its own budget.
    - **Otherwise**: install ours for the squeezed budget, and on the way out restore the
      outer's remaining time less what we consumed. The prior handler is read with
      ``getsignal`` *before* ours is installed (M1-514's lesson: read it after, and the
      handoff window swallows the caller's alarm).
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    try:
        outer_remaining, outer_interval = signal.getitimer(signal.ITIMER_REAL)
    except (ValueError, OSError):  # pragma: no cover - defensive; guarded above
        yield
        return
    budget = seconds
    if outer_remaining > 0:
        budget = min(seconds, outer_remaining - _PHASE_MARGIN)
        if budget <= 0:
            # No room left to notify without eating the phase. Abandoned before the timer
            # is touched, so the outer deadline is left exactly as it was found.
            raise _PushExpired

    def expired(signum: int, frame: FrameType | None) -> None:
        raise _PushExpired

    prior = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, expired)
    started = time.monotonic()
    try:
        signal.setitimer(signal.ITIMER_REAL, budget)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior)
        if outer_remaining > 0:
            # `budget <= outer_remaining - _PHASE_MARGIN`, so this is at least the margin
            # and the `_MIN_TICK` floor is unreachable here; it is kept because "restore a
            # live timer, never disarm it" must hold whatever the arithmetic does.
            left = outer_remaining - (time.monotonic() - started)
            signal.setitimer(signal.ITIMER_REAL, max(left, _MIN_TICK), outer_interval)


class NotifyError(Exception):
    """A notification could not be prepared or recorded.

    Same hygiene rule as every other module error here: the message never echoes a
    stored, file or field value, and sanitizing raises use ``from None`` so an underlying
    exception cannot reprint one through its text or a rendered traceback. Filesystem
    paths are the settled M1-401 carve-out and are rendered.

    It is raised for *caller mistakes* -- an event outside the vocabulary, a subject that
    is not a string -- which are programming errors and should be loud. It is **not**
    raised for a failed push, a slow push, or an unclaimable stamp: those are the
    conditions this module exists to absorb, and they come back as a
    :data:`NotifyOutcome` instead.
    """


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_notify_client(config: AppConfig) -> tuple[httpx.Client, str] | None:
    """Build the client and topic URL, or ``None`` if notifications are not usable.

    ``None`` rather than an exception for both "switched off" and "no topic in the
    environment": a worker whose notifications are unconfigured must still forecast. The
    caller distinguishes them only in the log line, never in behaviour.

    The URL is returned alongside the client rather than becoming its ``base_url``,
    because httpx normalizes a ``base_url`` by appending a trailing slash and then merges
    relative paths against it. An ntfy topic URL is a whole path, not a prefix, and
    ``POST https://host/topic/`` is not the request the operator configured. The URL is
    posted absolutely, so what leaves this process is byte-for-byte what is in the
    environment.
    """
    if not config.notify.enabled:
        return None
    topic_url = os.environ.get(config.notify.topic_url_env)
    if not topic_url:
        # The NAME only. The value is the thing we are protecting.
        _LOGGER.warning(
            "notifications are enabled but %s is unset; no operational alerts will be sent",
            config.notify.topic_url_env,
        )
        return None
    try:
        client = httpx.Client(
            timeout=config.notify.timeout_seconds,
            # Never follow with the credential attached, exactly as the Exa client does:
            # a redirect would replay the topic URL at a host we did not configure.
            follow_redirects=False,
        )
    except Exception:
        # httpx quotes what it could not parse, and that would be the credential.
        _LOGGER.warning("the notification client could not be built")
        return None
    # Connection-failure retries only, never a re-sent request. A documented no-op under
    # ``httpx.MockTransport``, so this does not break the test seam.
    apply_connection_retries(client, 1)
    return client, topic_url


@dataclass(frozen=True)
class Notifier:
    """Sends one operational alert, throttled durably, and never raises into a caller.

    Built by :func:`build_notifier`. ``topic_url`` is a bearer credential, so it is
    excluded from the generated ``__repr__``: a dataclass repr is exactly the kind of
    thing that ends up in a traceback or a debug log. That is belt to the braces of
    registering its variable name in ``secret_env_var_names``, which makes the logging
    filter scrub the value wherever it still appears. ``tests/unit/test_notify.py``
    asserts both, because "the repr is safe" is a claim about generated code.
    """

    client: httpx.Client
    topic_url: str = field(repr=False)
    state_root: Path
    # Total elapsed seconds one push may take, enforced by :func:`_push_deadline`. Distinct
    # from the client's timeout, which bounds each operation and not the sum of them.
    deadline_seconds: float = 10.0
    secret_names: tuple[str, ...] = ()
    clock: Callable[[], datetime] = _utcnow
    # Not part of equality or the repr: it is a cache, not identity.
    _pruned: list[bool] = field(default_factory=list, compare=False, repr=False)

    def close(self) -> None:
        """Release the transport. Best effort: a failed close is not worth an exception."""
        try:
            self.client.close()
        except Exception:
            _LOGGER.debug("the notification client did not close cleanly")

    def send(
        self,
        event: str,
        *,
        subject: str,
        title: str,
        body: str,
    ) -> NotifyOutcome:
        """Push one alert, unless an identical one already went out this window.

        ``event`` must be one of :data:`NotifyEvent` and ``subject`` identifies *what* the
        alert is about -- a question scope, a record id, an activation and level. Together
        they key the throttle, so two different questions blocking within the same window
        both page while one question blocking twice does not.

        Returns rather than raises for every operational outcome. The only exceptions that
        leave this method are :class:`NotifyError` for a caller mistake, and
        ``BaseException`` (the phase timeout's own class) which must not be swallowed.
        """
        if event not in get_args(NotifyEvent):
            # Before any I/O and before the stamp is claimed. The accepted values are this
            # module's own literals, so naming them leaks nothing.
            raise NotifyError(
                f"event must be one of {get_args(NotifyEvent)} (offending input withheld)"
            )
        if type(subject) is not str or not subject:
            raise NotifyError("subject must be a non-empty str (offending input withheld)")
        if not self._claim(event, subject):
            return "throttled"
        return self._post(event, title=title, body=body)

    # -- throttle ---------------------------------------------------------------------

    def _stamp_path(self, event: str, subject: str, now: datetime) -> Path:
        """The stamp for this event, subject and window.

        The subject is hashed rather than sanitized into the filename. Hashing is total
        over every string -- scopes carry ``:``, which ``require_safe_component`` refuses --
        and it means a subject can never become a path component, so no caller can name a
        directory entry with a meaning of its own by choosing a subject.

        The window is a tumbling one: ``floor(epoch / window)``. A sliding window would
        need read-then-write and reintroduce exactly the race ``os.link`` closes. The cost
        is that two pushes can land either side of a boundary; a duplicate alert is a far
        better failure than a lost one or a raced one.
        """
        window = _WINDOW_SECONDS[event]
        index = int(now.timestamp()) // window
        keyed = hashlib.sha256(subject.encode("utf-8", "surrogatepass")).hexdigest()[:32]
        return self.state_root / _STAMP_SUBDIR / f"{event}-{keyed}-{index}.json"

    def _instant(self) -> datetime:
        """Read the injected clock, refusing anything that is not a real instant.

        ``clock`` is caller code and may return anything. A naive datetime would make
        ``timestamp()`` local-dependent, which would silently move every window boundary.
        """
        try:
            now = self.clock()
        except Exception:
            raise NotifyError("the notification clock could not be read") from None
        if type(now) is not datetime or now.tzinfo is None:
            raise NotifyError("the notification clock must return an aware datetime")
        return now

    def _claim(self, event: str, subject: str) -> bool:
        """Claim this window for this event, returning whether we won it.

        ``os.link`` is the compare-and-set: the first process in the window creates the
        stamp, every other process fails ``EEXIST``. Losing that race is what "throttled"
        means, and it survives the process exiting because the stamp is a fsynced file.

        A stamp that cannot be written for any *other* reason -- an unwritable directory,
        a full disk -- fails **closed**: no push. That is the deliberate direction. A
        notifier that cannot record what it sent is a notifier that sends 288 times a day,
        and a muted channel reports nothing at all, whereas a missing alert still leaves
        the daily ``poll_summary`` digest to stop arriving and say so.
        """
        # One clock reading for the whole claim. Reading it again for the payload or the
        # prune would let a window boundary fall between them, so the stamp recording
        # window N could be the one filed under N+1.
        now = self._instant()
        path = self._stamp_path(event, subject, now)
        payload = json.dumps(
            {"event": event, "at": now.isoformat()},
            ensure_ascii=True,
            sort_keys=True,
            allow_nan=False,
        ).encode()
        try:
            write_new_file(path, payload, what="notification stamp", error=NotifyError)
        except NotifyError as exc:
            try:
                collided = path.exists()
            except OSError:
                # Round 1 non-blocking observation. `exists()` is itself I/O and raises
                # `PermissionError` when directory traversal is lost between the write
                # attempt and this check. A raw OSError escaping is a violation of the rule
                # that every malformed shape arrives as the module's own error type, and
                # `emit` containing it downstream does not make it right here.
                collided = False
            if collided:
                # Someone else claimed this window -- the ordinary throttled case, and the
                # only one that is not a problem.
                return False
            # Distinguished from the line above by the destination *not* being there:
            # this was an I/O failure, not a collision. Logged rather than only returned,
            # because a silent alerting path is the condition this module exists to end.
            _LOGGER.warning(
                "notification stamp could not be written, so no alert was sent: %s", exc
            )
            return False
        self._prune(now)
        return True

    def _prune(self, now: datetime) -> None:
        """Delete stamps too old to suppress anything, at most once per process.

        Best effort in every arm. Pruning is housekeeping; failing at it must not cost an
        alert, and must not raise into the caller that was only trying to notify.
        """
        if self._pruned:
            return
        self._pruned.append(True)
        cutoff = now - _STAMP_RETENTION
        try:
            entries = list((self.state_root / _STAMP_SUBDIR).glob("*.json"))
        except OSError:
            return
        for entry in entries:
            try:
                if datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc) < cutoff:
                    entry.unlink()
            except OSError:
                continue

    # -- transport --------------------------------------------------------------------

    def _post(self, event: str, *, title: str, body: str) -> NotifyOutcome:
        """POST the message, absorbing every failure.

        The exception is discarded rather than inspected or logged: ``httpx`` errors quote
        the request, and this request's URL is the bearer credential. What an operator
        needs from the log is that a push was lost and which event it was, and both of
        those are our own literals.
        """
        headers = {
            "Title": _header_safe(redact_secrets(title, self.secret_names)),
            "Priority": _PRIORITY[event],
            "Tags": "rotating_light" if _PRIORITY[event] == "high" else "chart_with_upwards_trend",
        }
        content = redact_secrets(body, self.secret_names).encode("utf-8", "replace")
        try:
            with _push_deadline(self.deadline_seconds):
                response = self.client.post(self.topic_url, content=content, headers=headers)
        except _PushExpired:
            # The elapsed-time bound, not a transport error: nothing about the peer is
            # known and nothing about it is said.
            _LOGGER.warning("the %s notification exceeded its wall-clock bound", event)
            return "failed"
        except BaseException as exc:
            # ``phase_timeout`` raises a BaseException subclass precisely so provider
            # ``except Exception`` handlers cannot swallow it (M1-514). Re-raise it and
            # absorb everything else.
            if not isinstance(exc, Exception):
                raise
            _LOGGER.warning("the %s notification could not be delivered", event)
            return "failed"
        if response.status_code >= 400:
            # The status code is ours to state; the body is the third party's and is not
            # read, let alone logged.
            _LOGGER.warning(
                "the %s notification was rejected with status %d", event, response.status_code
            )
            return "failed"
        return "sent"


def _header_safe(value: str) -> str:
    """Collapse a title to something an HTTP header can carry.

    Control bytes in a header value are the one shape that could split a request, and
    titles are assembled from ledger-derived strings. Nothing here is trusted to be
    single-line just because every current caller writes one.

    Every C0 control and DEL is replaced before the whitespace collapse, not only ``\r``
    and ``\n``. ``str.split()`` alone is not enough and the property pass in
    ``tests/property/test_notify_properties.py`` is what said so: ``NUL`` is not
    whitespace to Python, so it survived the collapse untouched. Non-ASCII characters are
    left alone deliberately -- header framing is byte-oriented and a multi-byte UTF-8
    sequence contains no ``0x0D`` or ``0x0A``, so U+2028 and friends cannot split anything.
    """
    stripped = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in value)
    collapsed = " ".join(stripped.split())
    return collapsed[:200] if collapsed else "whiskeyjack"


def build_notifier(config: AppConfig) -> Notifier | None:
    """Build a notifier for this configuration, or ``None`` if notifications are off.

    ``None`` is the normal, supported state and every call site treats it as "skip",
    so notifications are never load-bearing for a forecast.
    """
    built = build_notify_client(config)
    if built is None:
        return None
    client, topic_url = built
    return Notifier(
        client=client,
        topic_url=topic_url,
        state_root=config.storage.artifact_root,
        deadline_seconds=config.notify.timeout_seconds,
        secret_names=tuple(config.secret_env_var_names()),
    )


# The notifier in force for the current poll, or ``None``. Ambient for the same reason
# ``tournament_state.CURRENT_BUDGET`` is: the places that know a notification is warranted
# -- the fallback decision inside a paid retrieval, the budget check inside a reservation
# -- are several frames below the place that knows how to send one, and threading a
# parameter through them would put notification plumbing in the middle of the money path.
CURRENT_NOTIFIER: ContextVar[Notifier | None] = ContextVar("wj_notifier", default=None)


@contextmanager
def notifier_context(notifier: Notifier | None) -> Iterator[None]:
    """Install ``notifier`` for the duration of the block, and close it on the way out.

    It takes ownership of the client deliberately. The alternative -- a ``try``/``finally``
    around the caller's body -- would mean re-indenting the whole of ``run_once``, which is
    the live money path, to add a resource close. Ownership here keeps that diff at one
    line and puts the close somewhere it cannot be forgotten.
    """
    token = CURRENT_NOTIFIER.set(notifier)
    try:
        yield
    finally:
        CURRENT_NOTIFIER.reset(token)
        if notifier is not None:
            notifier.close()


def emit(event: str, *, subject: str, title: str, body: str) -> NotifyOutcome:
    """Send one alert through the ambient notifier, absorbing everything.

    **This is the only function the call sites use**, so that "a notification never costs
    a forecast" is one function to audit rather than five ``try`` blocks to keep in step.

    Every exception is absorbed here, including :class:`NotifyError` -- which
    :meth:`Notifier.send` raises for a caller mistake. A wrong event name is a bug and
    earns a loud line in the log, but it is never worth losing the forecast that was
    mid-flight when it was reached. ``BaseException`` is deliberately not caught: the
    phase timeout is one, and swallowing it here would defeat the bound that stops a
    wedged poll.

    With no notifier installed -- notifications off, no topic configured, or any code path
    outside a poll -- this returns ``"disabled"`` and does nothing at all.
    """
    notifier = CURRENT_NOTIFIER.get()
    if notifier is None:
        return "disabled"
    try:
        return notifier.send(event, subject=subject, title=title, body=body)
    except Exception:
        # Round 1 finding 2. ``event`` is the caller's argument and is unvalidated on this
        # path -- the exception being handled is usually the vocabulary check rejecting it
        # -- so interpolating it puts a rejected field value straight into a diagnostic,
        # which is the one thing every error message in this project may not do. Named only
        # once it is known to be one of this module's own literals.
        _LOGGER.warning(
            "the %s notification could not be prepared",
            event if event in get_args(NotifyEvent) else "unrecognized",
        )
        return "failed"


def budget_level_crossed(used: int, ceiling: int) -> int | None:
    """The highest configured threshold ``used/ceiling`` has reached, or ``None``.

    Both arguments are micro-USD, the unit the ledger stores. ``used`` is
    ``actual + held``: see :data:`BUDGET_THRESHOLD_PERCENTS` for why held spend counts
    even though AskNews reservations never settle.
    """
    if ceiling <= 0:
        return None
    for percent in BUDGET_THRESHOLD_PERCENTS:
        if used * 100 >= ceiling * percent:
            return percent
    return None


def describe(counts: Sequence[tuple[str, object]]) -> str:
    """Render ``label=value`` pairs as one line for a message body.

    Callers pass their own literals as labels and ledger-derived counts as values, so
    this never sees free text; it exists so the five call sites format alike.
    """
    return " ".join(f"{label}={value}" for label, value in counts)
