"""Follow a Metaculus project series across rollovers, under an owner-enabled series (M1-354).

Metaculus runs MiniBench as a series of projects, and the ``minibench`` slug moves from one
to the next (33122 -> 33125 on 2026-09-21). An activation binds one concrete project, so
before this item every rollover waited on the owner to re-point it, and each question is
open for about three hours: M1-347 made the rollover page, but a page answered in the
morning still loses the night's questions.

D51 moves that decision to the worker, inside limits the owner sets once. ``tournament
enable`` on a profile with ``metaculus.tournament.follow`` records a *series*: the account,
the followed slug, a ceiling across every project the series binds, and an end. At the start
of each poll :func:`follow` asks Metaculus what the slug resolves to, and when it has moved it
appends a new activation itself -- attributed to the series policy, not the owner -- only if
every guard in :func:`verdict` holds. A guard that refuses appends nothing, and M1-347's
``MINIBENCH ROLLED OVER`` page is the fallback, unchanged: the watchdog still compares the
slug with the newest activation, and that is exactly the comparison a refusal leaves unequal.

The Metaculus answer is untrusted. It is parsed by a total function, stored byte for byte as
an artifact, and bound into the activation by its sha256, so a rebind can be replayed from
the ledger (:func:`replay_rebind`). No value from it is ever rendered in a message.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import uuid4

from whiskeyjack_bot.artifacts import write_new_file
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.notify import emit
from whiskeyjack_bot.timeouts import phase_timeout
from whiskeyjack_bot.tournament_state import (
    Series,
    StorageFailure,
    TournamentError,
    append,
    bindings,
    events,
    series_disabled,
    series_of,
    series_spending,
    storage_transaction,
    utcnow,
)

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    import httpx

_LOGGER = logging.getLogger("whiskeyjack_bot.tournament")

# What one follow attempt concluded. Closed, and every member but ``rebound`` appends
# nothing. ``current`` is the steady state: the slug is still on the bound project.
FollowVerdict = Literal[
    "rebound",
    "current",
    "no_series",
    "account_mismatch",
    "series_disabled",
    "series_expired",
    "bindings_moved",
    "project_unreadable",
    "not_the_series",
    "not_newer",
    "not_ongoing",
    "series_exhausted",
    "superseded",
]

# One wall-clock bound over the whole read, enforced by ``phase_timeout`` (an interval timer,
# because httpx's timeout is per operation and does not bound a slow trickle). Spent at most
# once per poll, before discovery, against the tournament unit's TimeoutStartSec of 2400 s.
FOLLOW_READ_SECONDS: Final = 20
# A project answer is ~3 KB (measured 2026-09-27). Anything past this is not one.
FOLLOW_RESPONSE_LIMIT: Final = 1_000_000
# Metaculus 403s urllib's default User-Agent even with a valid token (measured 2026-09-22),
# so the agent is always explicit.
FOLLOW_USER_AGENT: Final = "whiskeyjack-bot"
# The one shape an evidence path may take. The path is read back out of the ledger, so it is
# checked before it is joined to the artifact root.
_EVIDENCE_PATH = re.compile(r"follow/[0-9a-f]{32}\.json")


@dataclass(frozen=True)
class SeriesProject:
    """The four fields of a Metaculus project answer a rebind depends on."""

    project_id: int
    slug: str
    close: datetime
    ongoing: bool


def parse_series_project(body: bytes) -> SeriesProject | None:
    """The project a Metaculus ``/projects/tournaments/<slug>/`` answer describes, or None.

    Pure and total over arbitrary bytes: it never raises. Exact types throughout, so a
    ``bool`` never reads as an id and a string never reads as ongoing. A close date without
    a zone is refused rather than assumed UTC.
    """
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if type(payload) is not dict:
        return None
    project_id = payload.get("id")
    slug = payload.get("slug")
    close = payload.get("close_date")
    ongoing = payload.get("is_ongoing")
    if (
        type(project_id) is not int
        or project_id <= 0
        or type(slug) is not str
        or type(close) is not str
        or type(ongoing) is not bool
    ):
        return None
    try:
        when = datetime.fromisoformat(close)
    except ValueError:
        return None
    if when.tzinfo is None or when.utcoffset() is None:
        return None
    return SeriesProject(project_id, slug, when, ongoing)


def precondition(
    *,
    series: Series | None,
    disabled: bool,
    account_id: int,
    bound: dict[str, str],
    now: datetime,
) -> FollowVerdict | None:
    """The refusals that need no Metaculus read, or None when a read is worth making. Pure.

    ``bindings_moved`` is the owner's authorization being scoped: a series was enabled for
    one configuration and one prompt, and a worker running anything else may not extend it
    to a new project -- that needs ``tournament enable`` again.
    """
    if series is None:
        return "no_series"
    if series.account_id != account_id:
        return "account_mismatch"
    if disabled:
        return "series_disabled"
    if now >= series.ends:
        return "series_expired"
    if bound != {
        "config_sha256": series.config_sha256,
        "prompt_sha256": series.prompt_sha256,
    }:
        return "bindings_moved"
    return None


def verdict(
    *,
    series: Series | None,
    disabled: bool,
    account_id: int,
    bound: dict[str, str],
    now: datetime,
    current_project: int,
    project: SeriesProject | None,
    series_used: int,
) -> FollowVerdict:
    """Whether to bind ``project``. Pure; ``rebound`` only when every guard holds.

    ``series_used`` is the series' actual plus held micro-USD. A series with nothing left
    binds nothing: a new activation could not buy anything, and leaving the slug unequal to
    the newest activation keeps M1-347's page firing, which is the owner's cue.
    """
    refused = precondition(
        series=series, disabled=disabled, account_id=account_id, bound=bound, now=now
    )
    if refused is not None:
        return refused
    assert series is not None
    if project is None:
        return "project_unreadable"
    if project.project_id == current_project:
        return "current"
    if project.slug != series.follow:
        return "not_the_series"
    if project.project_id < current_project:
        return "not_newer"
    if not project.ongoing or project.close <= now:
        return "not_ongoing"
    if series_used >= series.budget_microusd:
        return "series_exhausted"
    return "rebound"


def read_series_project(
    config: AppConfig, slug: str, *, transport: httpx.BaseTransport | None = None
) -> bytes:
    """One read-only GET of ``/projects/tournaments/<slug>/``. Raises on anything but a 200.

    Redirects are refused, not followed: the token is a header, and a redirect to another
    host would carry it (M1-347 measured the only redirect this path takes -- the missing
    trailing slash -- and the URL here has it). The body is bounded while it streams.
    ``transport`` is the test seam; the suite blocks sockets.
    """
    import httpx

    token = os.environ.get(config.metaculus.token_env, "").strip()
    if not token:
        raise TournamentError("series project read needs the Metaculus token")
    url = f"{config.metaculus.base_url.rstrip('/')}/projects/tournaments/{slug}/"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Token {token}",
        "User-Agent": FOLLOW_USER_AGENT,
    }
    timeout = min(float(config.metaculus.request_timeout_seconds), float(FOLLOW_READ_SECONDS))
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code != 200:
                    raise TournamentError("series project read failed")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > FOLLOW_RESPONSE_LIMIT:
                        raise TournamentError("series project answer is too large")
    except httpx.HTTPError:
        raise TournamentError("series project read failed") from None
    return bytes(body)


def follow(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    account_id: int,
    read: Callable[[], bytes],
    now: datetime | None = None,
) -> FollowVerdict:
    """Bind the followed slug's project if it moved and every guard holds. Once per poll.

    A failed, slow or malformed read is ``project_unreadable`` and never stops the poll: the
    worker goes on polling the project it is bound to, and the watchdog's
    ``rollover_check_failed`` covers the blind spot. A malformed *ledger* row still stops it
    (:class:`StorageFailure`), as everywhere else: the authorization cannot be shown.
    """
    tournament = config.metaculus.tournament
    if tournament.follow is None:
        return "no_series"
    instant = now or utcnow()
    active = events(conn, "activation", "account")
    if not active:
        return _logged("no_series")
    activation = active[-1]
    if type(activation) is not dict:
        raise StorageFailure("cannot read tournament activation")
    series = series_of(conn, activation)
    disabled = series is not None and series_disabled(conn, series)
    bound = bindings(config)
    refused = precondition(
        series=series, disabled=disabled, account_id=account_id, bound=bound, now=instant
    )
    if refused is not None:
        return _logged(refused)
    assert series is not None
    current = activation.get("project_id")
    if type(current) is not int:
        raise StorageFailure("cannot read tournament activation")
    body: bytes | None
    try:
        with phase_timeout(FOLLOW_READ_SECONDS):
            body = read()
    except Exception:
        body = None
    if type(body) is not bytes or len(body) > FOLLOW_RESPONSE_LIMIT:
        body = None
    project = None if body is None else parse_series_project(body)
    used = sum(series_spending(conn, series.series_id))
    code = verdict(
        series=series,
        disabled=disabled,
        account_id=account_id,
        bound=bound,
        now=instant,
        current_project=current,
        project=project,
        series_used=used,
    )
    if code == "rebound":
        assert body is not None and project is not None
        code = _rebind(
            conn,
            config,
            activation=activation,
            series=series,
            project=project,
            body=body,
            bound=bound,
            now=instant,
        )
    return _logged(code)


def _rebind(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    activation: dict[str, Any],
    series: Series,
    project: SeriesProject,
    body: bytes,
    bound: dict[str, str],
    now: datetime,
) -> FollowVerdict:
    """Store the answer, then append the policy's activation. Artifact first, then ledger.

    The re-check inside the transaction is against another writer -- the owner running
    ``tournament enable`` or ``disable`` while a poll is between its read and this append --
    and it appends nothing if the newest activation is no longer the one the verdict was
    about. An evidence file whose append never happened is an orphan, never a claim.
    """
    evidence = f"follow/{uuid4().hex}.json"
    write_new_file(
        config.storage.artifact_root / evidence,
        body,
        what="series project evidence",
        error=StorageFailure,
    )
    data: dict[str, Any] = {
        "activation_id": uuid4().hex,
        "account_id": series.account_id,
        "project_id": project.project_id,
        "starts": now.isoformat(),
        "ends": min(project.close, series.ends).isoformat(),
        "budget_microusd": series.project_budget_microusd,
        "series_id": series.series_id,
        "follow": series.follow,
        "bound_by": f"policy:follow-v1:{series.series_id}",
        "previous_activation_id": activation.get("activation_id"),
        "evidence": {
            "path": evidence,
            "sha256": hashlib.sha256(body).hexdigest(),
            "read_at": now.isoformat(),
        },
        **bound,
    }
    with storage_transaction(conn):
        latest = events(conn, "activation", "account")
        if not latest or latest[-1] != activation or series_disabled(conn, series):
            return "superseded"
        append(conn, "activation", "account", data)
    # No value from Metaculus or the ledger in either string: not the project ids, not the
    # series id. The subject is hashed into a stamp filename and never transmitted.
    emit(
        "series_followed",
        subject=str(data["activation_id"]),
        title="whiskeyjack: MiniBench followed to a new project",
        body=(
            "MiniBench moved to a new Metaculus project and the worker bound it under the "
            "series you enabled. It polls the new project from now on; nothing else changed."
            "\n\nCheck: whiskeyjack-bot tournament status --config config/tournament.yaml"
        ),
    )
    return "rebound"


def replay_rebind(conn: sqlite3.Connection, config: AppConfig, activation: dict[str, Any]) -> bool:
    """Whether a policy-bound activation follows from the answer it stored. Reads only.

    Re-derives, from the stored bytes alone, every guard that depended on Metaculus: the
    slug, the project being newer than the one it replaced, being ongoing, and the window
    the activation was given. The ledger-side guards (account, series state, spend) are
    facts of the journal at that moment and are not re-derived. Anything unreadable or
    inconsistent is False, never an exception, except a malformed series row, which is a
    storage failure everywhere.
    """
    evidence = activation.get("evidence")
    if type(evidence) is not dict:
        return False
    path, sha, read_at = evidence.get("path"), evidence.get("sha256"), evidence.get("read_at")
    if (
        type(path) is not str
        or not _EVIDENCE_PATH.fullmatch(path)
        or type(sha) is not str
        or type(read_at) is not str
    ):
        return False
    try:
        body = (config.storage.artifact_root / path).read_bytes()
        at = datetime.fromisoformat(read_at)
    except (OSError, ValueError):
        return False
    if hashlib.sha256(body).hexdigest() != sha or at.tzinfo is None:
        return False
    series = series_of(conn, activation)
    project = parse_series_project(body)
    if series is None or project is None:
        return False
    previous = [
        row
        for row in events(conn, "activation", "account")
        if type(row) is dict
        and row.get("activation_id") == activation.get("previous_activation_id")
    ]
    if len(previous) != 1 or type(previous[0].get("project_id")) is not int:
        return False
    return (
        project.project_id == activation.get("project_id")
        and project.slug == series.follow
        and project.project_id > previous[0]["project_id"]
        and project.ongoing
        and project.close > at
        and min(project.close, series.ends).isoformat() == activation.get("ends")
    )


def _logged(code: FollowVerdict) -> FollowVerdict:
    """One JSONL line per attempt that did anything but find the slug where it was.

    Codes only -- never a project id, a series id or a Metaculus value. ``current`` is the
    steady state of every poll and would be 288 lines a day of nothing.
    """
    if code == "rebound":
        _LOGGER.info("series follow: %s", code)
    elif code != "current":
        _LOGGER.warning("series follow: %s", code)
    return code
