"""Durable activation, operation journal, and prepaid round limits (LAUNCH; M1-348; M1-354).

One ledger belongs to one bot account. Unknown charges retain their full reservation.
Journal files intentionally survive SQLite restore; a mismatch blocks further writes
until the operator reconciles the restored database against the platform.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal, get_args
from uuid import uuid4

from whiskeyjack_bot.artifacts import write_new_file
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.lifecycle import LifecycleError, transaction
from whiskeyjack_bot.notify import budget_level_crossed, emit
from whiskeyjack_bot.questions.canonical import canonicalize_for_fingerprint
from whiskeyjack_bot.questions.model import CanonicalQuestion


class TournamentError(Exception):
    """A safe refusal, with external content withheld."""


class ActivationInactive(TournamentError):
    """An ordinary disabled or out-of-window activation, not invalid storage."""


class AskNewsSubcapReached(TournamentError):
    """A reservation was refused because it would take AskNews spend over the series sub-cap.

    Its own type so research can degrade to the Exa fallback on exactly this refusal (M1-355,
    D51) and never on the round or series ceilings, which stop the worker. The message is a
    constant.
    """


class StorageFailure(TournamentError):
    """Stop the worker; continuing could lose evidence or spend."""


class ModelOutcomeUnknown(TournamentError):
    """A priced model call was started and never recorded a completion (M1-350/M1-351).

    The journal holds a ``model_started`` with no ``model_completed``, the reservation stays
    held because what was billed is unknown, and ``PricedClient`` refuses to buy the same
    request again. A subclass rather than a message, so ``tournament.run_once`` can pace the
    retry to the checkpoint window without parsing wording the error-hygiene rule may
    legitimately change.
    """


# The activation bindings a retirement can name (M1-334). Names only: an operator told
# *which* binding moved can act on it, and none of them is a value, a digest or config
# content, so naming them is inside the project's no-value-echo rule.
RetiredBinding = Literal["account", "destination", "configuration", "prompt"]


class ActivationRetired(TournamentError):
    """The activation no longer matches this worker: something it was bound to moved.

    Unlike :class:`ActivationInactive` -- disabled, or outside its window, which is an
    ordinary resting state -- this needs an operator, because nothing but `tournament
    enable` clears it. The 2026-09-09 outage was this, silent for 2h33m: a config field
    added in a merge moved `config_sha256`, and every poll refused with an exit code and no
    cause. `changed` carries which bindings moved, in `RetiredBinding` order.
    """

    def __init__(self, changed: tuple[RetiredBinding, ...]) -> None:
        if not changed or any(key not in get_args(RetiredBinding) for key in changed):
            raise ValueError("ActivationRetired needs at least one known binding name")
        self.changed = changed
        super().__init__(
            f"activation retired: {', '.join(changed)} changed; re-run tournament enable"
        )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical(data: Any) -> str:
    return json.dumps(
        data, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(data: Any) -> str:
    return hashlib.sha256(canonical(data).encode()).hexdigest()


def question_fingerprint(question: CanonicalQuestion) -> str:
    """The one formula every M1-326/M1-327 call site must share (M1-331).

    ``digest(question.model_dump(mode="json"))`` alone is unstable: ``canonical`` sorts dict
    keys, never list elements, and every list-valued field on ``CanonicalQuestion`` is an
    unordered membership set carried through from the SDK with no sort applied -- see
    ``questions/canonical.py`` for which fields and why. Canonicalizing here, once, is what
    keeps a question whose API-returned list order merely changed between polls from silently
    missing its own recorded verdict and being re-researched at full price.
    """
    return digest(canonicalize_for_fingerprint(question))


def bindings(config: AppConfig) -> dict[str, str]:
    try:
        prompt = config.forecast.prompt_path.read_bytes()
    except OSError:
        raise StorageFailure("cannot read activation prompt") from None
    return {
        "config_sha256": digest(config.model_dump(mode="json")),
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
    }


def events(conn: sqlite3.Connection, kind: str, scope: str) -> list[dict[str, Any]]:
    try:
        return [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT data FROM tournament_events WHERE kind=? AND scope=? ORDER BY seq",
                (kind, scope),
            )
        ]
    except (sqlite3.Error, ValueError):
        raise StorageFailure("cannot read tournament journal") from None


def journal_form(data: dict[str, Any]) -> Any:
    """What the journal stores for `data`: every string redacted of configured secrets.

    Central for every `append` caller (M1-613). Redaction used to be applied per call site
    (the `cost_reserved` request, the model content), so any other caller journaling
    provider text would have persisted it, and M1-604's export carries `data` verbatim.
    Exposed because `witness` must write this exact form to its files: `check_storage`
    compares the file's `data` with the row's for equality, so the two must never be derived
    separately.
    """
    from whiskeyjack_bot.redaction import redact_leaves, registered_secret_env_var_names

    return redact_leaves(data, registered_secret_env_var_names())


def append(conn: sqlite3.Connection, kind: str, scope: str, data: dict[str, Any]) -> str:
    identifier = uuid4().hex
    # Serialized before the transaction opens (M1-338), so a payload that cannot be
    # journaled never reaches the INSERT and persists nothing. Every shape that fails here
    # arrives as StorageFailure with the payload withheld. Before M1-338 a cycle escaped as
    # a raw ValueError('Circular reference detected'), and it has siblings:
    # - NaN or Infinity, which ``allow_nan=False`` refuses (ValueError);
    # - an object with no JSON form (TypeError);
    # - int and str keys in one mapping, which ``sort_keys`` cannot order (TypeError);
    # - nesting deeper than ``json.dumps`` recurses (RecursionError).
    # Their texts can name a value (a key, a type or an object repr), so none is rendered.
    try:
        row = canonical(journal_form(data))
    except (ValueError, TypeError, RecursionError):
        raise StorageFailure(
            "cannot serialize tournament journal payload (payload withheld)"
        ) from None
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO tournament_events(event_id,kind,scope,data,created_at_utc) "
                "VALUES(?,?,?,?,?)",
                (identifier, kind, scope, row, utcnow().isoformat()),
            )
    except (sqlite3.Error, LifecycleError):
        raise StorageFailure("cannot commit tournament journal") from None
    return identifier


def guard_root(conn: sqlite3.Connection) -> Path:
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2]).parent / ".posting-guard"


def check_storage(conn: sqlite3.Connection, root: Path) -> None:
    """Compare durable spend/write witnesses against restored SQLite state."""
    try:
        for path in [*(root / "operations").glob("*.json"), *guard_root(conn).glob("*.json")]:
            envelope = json.loads(path.read_text())
            row = conn.execute(
                "SELECT data FROM tournament_events WHERE event_id=?", (envelope["event_id"],)
            ).fetchone()
            if row is None or json.loads(row[0]) != envelope["data"]:
                raise StorageFailure("storage restore detected; platform reconciliation required")
        for row in conn.execute("SELECT event_id,data FROM tournament_events WHERE kind='witness'"):
            path = root / "operations" / f"{row[0]}.json"
            if not path.is_file() or not (guard_root(conn) / path.name).is_file():
                raise StorageFailure("operation artifact missing; platform reconciliation required")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        raise StorageFailure("cannot verify operation journal and artifacts") from None


def witness(conn: sqlite3.Connection, root: Path, scope: str, data: dict[str, Any]) -> str:
    identifier = append(conn, "witness", scope, data)
    # The same journal form `append` stored, not the raw `data`: `check_storage` requires
    # the two to be equal, and a secret redacted in one but not the other would read as a
    # restored ledger and stop the worker.
    envelope = canonical(
        {
            "schema_version": "1.1.0",
            "event_id": identifier,
            "scope": scope,
            "data": journal_form(data),
        }
    ).encode()
    for destination in (
        root / "operations" / f"{identifier}.json",
        guard_root(conn) / f"{identifier}.json",
    ):
        write_new_file(destination, envelope, what="operation witness", error=StorageFailure)
    return identifier


# The hard maximum for one activation's spending ceiling, in USD. Launch shipped 20; M1-408
# raised it to 40 on the owner's explicit authorization (2026-09-11), when the forecaster moved
# to GPT-6 Astra at 5x Sol's prices; M1-354 raised it to 80 under D51 (2026-09-27), with the
# funding grant. It is a code constant rather than configuration on purpose: an activation
# binds to config_sha256, and a paid-call limit living in the same file it authorizes would
# let one edit both raise the limit and re-authorize under it.
MAX_ACTIVATION_BUDGET_USD: Final = 80
# The hard maximum for a followed series' ceiling (M1-354, D51): the owner's approval covers
# USD 80 across every project one series binds, counted from the moment it is enabled. A
# code constant for the same reason as the one above.
MAX_SERIES_BUDGET_USD: Final = 80


# The one level, short of the refusal itself, at which the AskNews sub-cap pages (M1-355,
# D51: "an 80% page ... at 100% the worker degrades"). Not `notify.BUDGET_THRESHOLD_PERCENTS`,
# which also carries 50 for the ceilings: the owner asked for 80 and 100, not three.
ASKNEWS_PAGE_PERCENT: Final = 80


@dataclass(frozen=True)
class Series:
    """One owner-enabled series authorization, as the journal stores it (M1-354).

    Read back out of the ledger, so every field is checked on the way in: a malformed row is
    a :class:`StorageFailure`, never a guess about what the owner authorized.
    """

    series_id: str
    account_id: int
    follow: str
    budget_microusd: int
    project_budget_microusd: int
    ends: datetime
    config_sha256: str
    prompt_sha256: str
    # M1-355 (D51): the AskNews share of ``budget_microusd``. ``None`` only for a series
    # enabled before M1-355, whose journal row has no such key: it carries no sub-cap, and
    # ``tournament status`` says so rather than inventing one.
    asknews_budget_microusd: int | None = None


def _series_from(data: object) -> Series:
    fields = data if type(data) is dict else {}
    ends: datetime | None = None
    raw_ends = fields.get("ends")
    if type(raw_ends) is str:
        try:
            ends = datetime.fromisoformat(raw_ends)
        except ValueError:
            ends = None
    if (
        type(fields.get("series_id")) is not str
        or type(fields.get("account_id")) is not int
        or type(fields.get("follow")) is not str
        or type(fields.get("budget_microusd")) is not int
        or fields["budget_microusd"] <= 0
        or type(fields.get("project_budget_microusd")) is not int
        or fields["project_budget_microusd"] <= 0
        or ends is None
        or ends.tzinfo is None
        or type(fields.get("config_sha256")) is not str
        or type(fields.get("prompt_sha256")) is not str
    ):
        raise StorageFailure("cannot read series authorization")
    subcap = fields.get("asknews_budget_microusd")
    if "asknews_budget_microusd" in fields and (
        type(subcap) is not int or subcap <= 0 or subcap > fields["budget_microusd"]
    ):
        raise StorageFailure("cannot read series authorization")
    return Series(
        series_id=fields["series_id"],
        account_id=fields["account_id"],
        follow=fields["follow"],
        budget_microusd=fields["budget_microusd"],
        project_budget_microusd=fields["project_budget_microusd"],
        ends=ends,
        config_sha256=fields["config_sha256"],
        prompt_sha256=fields["prompt_sha256"],
        asknews_budget_microusd=subcap,
    )


def series_of(conn: sqlite3.Connection, activation: dict[str, Any]) -> Series | None:
    """The series an activation was bound under, or None for a pinned activation.

    An activation that names a series the journal does not hold is a storage failure: the
    authorization it claims cannot be shown.
    """
    identifier = activation.get("series_id")
    if identifier is None:
        return None
    if type(identifier) is not str:
        raise StorageFailure("cannot read series authorization")
    matches = [row for row in events(conn, "series", "account") if type(row) is dict]
    found = [row for row in matches if row.get("series_id") == identifier]
    if len(found) != 1:
        raise StorageFailure("cannot read series authorization")
    return _series_from(found[0])


def series_disabled(conn: sqlite3.Connection, series: Series) -> bool:
    return bool(events(conn, "disabled", series.series_id))


def bound_project(conn: sqlite3.Connection, config: AppConfig) -> str:
    """The project this profile polls: the configured one, or a followed series' latest.

    A pinned profile polls exactly `metaculus.tournament.id`, as before M1-354. A following
    profile polls whatever project its newest activation binds -- the owner's first, or one
    the series policy bound since -- so a rollover changes no configuration and retires
    nothing. With no activation yet it is the configured id, which `require_activation`
    then refuses as inactive.
    """
    if config.metaculus.tournament.follow is None:
        return str(config.metaculus.tournament.id)
    active = events(conn, "activation", "account")
    if not active:
        return str(config.metaculus.tournament.id)
    project = active[-1].get("project_id") if type(active[-1]) is dict else None
    if type(project) is not int:
        raise StorageFailure("cannot read tournament activation")
    return str(project)


def enable(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    account_id: int,
    project_id: int,
    starts: datetime,
    ends: datetime,
    budget_usd: float = 20.0,
    series_budget_usd: float | None = None,
    series_ends: datetime | None = None,
    asknews_budget_usd: float | None = None,
) -> str:
    now = utcnow()
    if (
        type(account_id) is not int
        or account_id <= 0
        or type(project_id) is not int
        or project_id <= 0
        or starts.tzinfo is None
        or ends.tzinfo is None
        or starts >= ends
        or ends <= now
        or not 0 < budget_usd <= MAX_ACTIVATION_BUDGET_USD
    ):
        raise TournamentError(
            "invalid activation identity, window, or budget "
            f"(maximum USD {MAX_ACTIVATION_BUDGET_USD})"
        )
    if config.metaculus.tournament.use_sdk_current_id or str(config.metaculus.tournament.id) != str(
        project_id
    ):
        raise TournamentError("activation requires the concrete configured project ID")
    if config.environment != "production" and project_id != 32977:
        raise TournamentError("testing profiles may activate only project 32977")
    follow = config.metaculus.tournament.follow
    if follow is None and (
        series_budget_usd is not None or series_ends is not None or asknews_budget_usd is not None
    ):
        raise TournamentError("series options require a profile with tournament.follow set")
    if follow is not None:
        # M1-354 (D51). All or nothing: a following profile without a series would bind
        # nothing past its first project, and silently.
        if config.environment != "production":
            raise TournamentError("following a series requires the production profile")
        if (
            series_budget_usd is None
            or series_ends is None
            or series_ends.tzinfo is None
            or not 0 < series_budget_usd <= MAX_SERIES_BUDGET_USD
            or budget_usd > series_budget_usd
            or series_ends <= now
            or ends > series_ends
        ):
            raise TournamentError(
                "invalid series ceiling or end "
                f"(maximum USD {MAX_SERIES_BUDGET_USD}; the project budget and window must "
                "fit inside the series)"
            )
        # M1-355 (D51): the AskNews share is set by the owner at enable and is never
        # defaulted. At most the series ceiling; a whole number of micro-USD above zero.
        if (
            asknews_budget_usd is None
            or not 0 < asknews_budget_usd <= series_budget_usd
            or math.floor(asknews_budget_usd * 1_000_000) <= 0
        ):
            raise TournamentError(
                "invalid AskNews sub-cap (a required amount above zero, no larger than the "
                "series ceiling)"
            )
    prior = events(conn, "activation", "account")
    if any(a["account_id"] != account_id for a in prior):
        raise TournamentError("this ledger is already bound to another bot account")
    check_storage(conn, config.storage.artifact_root)
    bound = bindings(config)
    data: dict[str, Any] = {
        "activation_id": uuid4().hex,
        "account_id": account_id,
        "project_id": project_id,
        "starts": starts.isoformat(),
        "ends": ends.isoformat(),
        "budget_microusd": math.floor(budget_usd * 1_000_000),
        **bound,
    }
    if follow is not None:
        assert series_budget_usd is not None and series_ends is not None
        assert asknews_budget_usd is not None
        series = {
            "series_id": uuid4().hex,
            "account_id": account_id,
            "follow": follow,
            "budget_microusd": math.floor(series_budget_usd * 1_000_000),
            "project_budget_microusd": data["budget_microusd"],
            "asknews_budget_microusd": math.floor(asknews_budget_usd * 1_000_000),
            "ends": series_ends.isoformat(),
            **bound,
        }
        data.update(series_id=series["series_id"], follow=follow, bound_by="owner")
        with storage_transaction(conn):
            append(conn, "series", "account", series)
            append(conn, "activation", "account", data)
    else:
        append(conn, "activation", "account", data)
    return str(data["activation_id"])


def disable(conn: sqlite3.Connection) -> None:
    active = events(conn, "activation", "account")
    if active:
        # M1-354: disabling a followed project also stops its series, or the next rollover
        # would bind a fresh, enabled activation behind the owner's back. One transaction
        # (round 1): committed one at a time, a failure on the second write left the
        # activation disabled and the series still able to rebind.
        series = active[-1].get("series_id") if type(active[-1]) is dict else None
        with storage_transaction(conn):
            append(conn, "disabled", active[-1]["activation_id"], {})
            if type(series) is str:
                append(conn, "disabled", series, {})


def retired_bindings(
    activation: dict[str, Any], config: AppConfig, *, account_id: int, project_id: str
) -> tuple[RetiredBinding, ...]:
    """Which of the activation's bindings no longer hold, compared key by key.

    The same four conditions `require_activation` refused on as one boolean before M1-334,
    split so a refusal can say which moved. A stored activation missing a binding key
    counts as moved rather than raising `KeyError`: journal rows are read back from the
    ledger and are untrusted.
    """
    computed = bindings(config)
    tournament = config.metaculus.tournament
    moved: list[RetiredBinding] = []
    if activation.get("account_id") != account_id:
        moved.append("account")
    if tournament.follow is None:
        destination_moved = (
            str(activation.get("project_id")) != project_id
            or str(tournament.id) != project_id
            or tournament.use_sdk_current_id
            or (config.environment != "production" and project_id != "32977")
        )
    else:
        # M1-354: a following profile's configured id is only where the series started.
        # The destination holds when the activation was bound under a series that follows
        # what this config follows -- by the owner or by the series policy -- and it is the
        # project being polled.
        destination_moved = (
            str(activation.get("project_id")) != project_id
            or activation.get("follow") != tournament.follow
            or type(activation.get("series_id")) is not str
            or tournament.use_sdk_current_id
            or config.environment != "production"
        )
    if destination_moved:
        moved.append("destination")
    if activation.get("config_sha256") != computed["config_sha256"]:
        moved.append("configuration")
    if activation.get("prompt_sha256") != computed["prompt_sha256"]:
        moved.append("prompt")
    return tuple(moved)


def require_activation(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    account_id: int,
    project_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    active = events(conn, "activation", "account")
    check_storage(conn, config.storage.artifact_root)
    if not active:
        raise ActivationInactive("tournament activation is disabled")
    data = active[-1]
    instant = now or utcnow()
    # M1-334: the resting states are checked *before* retirement, not after. Disabled and
    # out-of-window are deliberate operator states, and they stay silent even when a binding
    # has also moved -- otherwise deploying a config change against a disabled profile, or
    # any poll after a window closes, would page about a profile nobody is running.
    if events(conn, "disabled", data["activation_id"]) or not datetime.fromisoformat(
        data["starts"]
    ) <= instant < datetime.fromisoformat(data["ends"]):
        raise ActivationInactive("tournament activation is disabled or outside its validity window")
    changed = retired_bindings(data, config, account_id=account_id, project_id=project_id)
    if changed:
        raise ActivationRetired(changed)
    return data


def _amounts(rows: list[dict[str, Any]], key: str) -> list[tuple[str, int]]:
    """``(reservation_id, amount)`` pairs from journal rows, or a sanitized refusal.

    The rows are read back out of the ledger and are untrusted: a malformed one used to
    escape :func:`spending` as a raw ``KeyError``/``TypeError`` (M1-348 adds a reader, which
    makes this new surface). Neither the amount nor the identifier is echoed.
    """
    pairs: list[tuple[str, int]] = []
    for row in rows:
        identifier = row.get("reservation_id") if type(row) is dict else None
        amount = row.get(key) if type(row) is dict else None
        if type(identifier) is not str or type(amount) is not int or amount < 0:
            raise StorageFailure("cannot read tournament spending")
        pairs.append((identifier, amount))
    return pairs


def spending(conn: sqlite3.Connection, scope: str) -> tuple[int, int]:
    """``(actual, held)`` micro-USD for one budget scope.

    A ``cost_corrected`` event (M1-348) **replaces** the figure its reservation settled at;
    it never adds to it. Applied as the larger of the two, so no append can lower actual
    spend: a correction is only ever written over a settlement of 0, where the two readings
    agree, and a second correction row for one reservation (the guard prevents it, a
    restored ledger might not) cannot count twice. A correction with no matching settlement
    is ignored -- its reservation is still held at the full estimate, which already
    over-counts it.
    """
    reserved = _amounts(events(conn, "cost_reserved", scope), "estimate_microusd")
    settled = dict(_amounts(events(conn, "cost_settled", scope), "actual_microusd"))
    for identifier, amount in _amounts(events(conn, "cost_corrected", scope), "actual_microusd"):
        if identifier in settled:
            settled[identifier] = max(settled[identifier], amount)
    actual = sum(settled.values())
    held = sum(amount for identifier, amount in reserved if identifier not in settled)
    return actual, held


def series_spending(
    conn: sqlite3.Connection, series_id: str, provider: str | None = None
) -> tuple[int, int]:
    """``(actual, held)`` micro-USD for every reservation made under one series (M1-354).

    With ``provider``, only that provider's reservations (M1-355: the AskNews share). The
    provider is stamped on the ``cost_reserved`` row itself, so the filter reads it there
    and settlements follow by reservation id.

    A reservation belongs to a series when :meth:`Budget.reserve` stamped its id on it, so
    spend made before the series was enabled -- on the same project -- is not counted, which
    is D51's "counted from enablement". Settlements and corrections apply exactly as
    :func:`spending` applies them; they carry no series, and are matched by reservation.
    """
    rows = [data for _, data in _journal_rows(conn, "cost_reserved")]
    mine = [
        row
        for row in rows
        if type(row) is dict
        and row.get("series_id") == series_id
        and (provider is None or row.get("provider") == provider)
    ]
    reserved = _amounts(mine, "estimate_microusd")
    ids = {identifier for identifier, _ in reserved}
    settled = {
        identifier: amount
        for identifier, amount in _amounts(
            [data for _, data in _journal_rows(conn, "cost_settled")], "actual_microusd"
        )
        if identifier in ids
    }
    for identifier, amount in _amounts(
        [data for _, data in _journal_rows(conn, "cost_corrected")], "actual_microusd"
    ):
        if identifier in settled:
            settled[identifier] = max(settled[identifier], amount)
    actual = sum(settled.values())
    held = sum(amount for identifier, amount in reserved if identifier not in settled)
    return actual, held


@contextmanager
def storage_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Keep transaction failures fatal through provider exception handling."""
    try:
        with transaction(conn):
            yield
    except (sqlite3.Error, LifecycleError):
        raise StorageFailure("cannot commit spending transaction; worker stopped") from None


def require_spending_clear(conn: sqlite3.Connection, scope: str) -> None:
    if events(conn, "restored_spending_hold", scope):
        raise TournamentError(
            "restored spending outcome is unknown; spending hold blocks purchases"
        )


# Where a settled cost came from. `openrouter` is `usage.cost`, what OpenRouter bills;
# `upstream_byok` is `usage.cost_details.upstream_inference_cost`, what the upstream provider
# bills a bring-your-own-key call (M1-348). On a BYOK call `usage.cost` is 0: the charge lands
# on the owner's own key, so reading it settled every GPT-6 Astra call as free.
# `asknews_credits` is an AskNews response's `usage.credits` at the per-credit rate in
# `research/asknews_cost.py` (M1-336): AskNews reports credits, never dollars.
CostBasis = Literal["openrouter", "upstream_byok", "asknews_credits"]


def _microusd(usd: float) -> int | None:
    """Whole micro-USD, rounded up, or None if the figure cannot be represented."""
    scaled = usd * 1_000_000
    return math.ceil(scaled) if math.isfinite(scaled) else None


def _valid_usd(value: object) -> float | None:
    """A finite, non-negative USD figure that converts to micro-USD, or None.

    Exact types: ``bool`` is an ``int`` subclass, so ``True`` would otherwise read as $1.
    A huge JSON integer makes ``float()`` raise ``OverflowError``, and a huge finite float
    overflows once scaled to micro-USD; both are unknown, not free.
    """
    if type(value) is int:
        try:
            usd = float(value)
        except OverflowError:
            return None
    elif type(value) is float:
        usd = value
    else:
        return None
    if not math.isfinite(usd) or usd < 0 or _microusd(usd) is None:
        return None
    return usd


def settled_cost(usage: object) -> tuple[float, CostBasis] | None:
    """The cost a model call settles at, and its basis, from OpenRouter's ``usage``.

    ``is_byok`` exactly ``True``: the upstream figure, never ``usage.cost`` (which is the 0
    that made BYOK read as free). ``is_byok`` absent or exactly ``False``: ``usage.cost``.
    Anything else -- ``is_byok`` of any other type, a missing or malformed figure -- is
    None, and the caller leaves the reservation held at its full estimate, so an unknown
    cost never reads as free. Total over arbitrary JSON: it never raises.
    """
    if type(usage) is not dict:
        return None
    byok = usage.get("is_byok", False)
    if type(byok) is not bool:
        return None
    if byok:
        details = usage.get("cost_details")
        if type(details) is not dict:
            return None
        upstream = _valid_usd(details.get("upstream_inference_cost"))
        return None if upstream is None else (upstream, "upstream_byok")
    cost = _valid_usd(usage.get("cost"))
    return None if cost is None else (cost, "openrouter")


@dataclass(frozen=True)
class CostCorrection:
    """One reservation settled at 0 whose stored response carries its real upstream cost."""

    scope: str
    reservation_id: str
    actual_microusd: int


@dataclass(frozen=True)
class AskNewsSettlement:
    """One held AskNews reservation whose stored response carries its ``usage.credits``."""

    scope: str
    reservation_id: str
    actual_microusd: int


@dataclass(frozen=True)
class CorrectionReport:
    """What ``tournament correct-costs`` found, and (with ``--apply``) wrote.

    Two passes. M1-348's corrects a model reservation settled at 0 whose response carries
    an upstream BYOK figure (``cost_corrected``). M1-336's settles a *held* AskNews
    reservation from its stored response's ``usage.credits`` (``cost_settled``).
    """

    corrections: tuple[CostCorrection, ...]
    already_corrected: int
    refused: int
    written: int
    asknews: tuple[AskNewsSettlement, ...] = ()
    asknews_refused: int = 0
    asknews_written: int = 0

    def as_dict(self, *, applied: bool) -> dict[str, Any]:
        return {
            "applied": applied,
            "reservations": len(self.corrections),
            "total_usd": sum(c.actual_microusd for c in self.corrections) / 1_000_000,
            "already_corrected": self.already_corrected,
            "refused_no_upstream_figure": self.refused,
            "written": self.written,
            "asknews_settlements": len(self.asknews),
            "asknews_total_usd": sum(a.actual_microusd for a in self.asknews) / 1_000_000,
            "asknews_refused_no_credits": self.asknews_refused,
            "asknews_written": self.asknews_written,
        }


def _correction(conn: sqlite3.Connection, scope: str, identifier: str) -> CostCorrection | None:
    """The correction for one reservation settled at 0, or None if it has no valid figure.

    Reads only the stored ``model_response`` for the reservation -- no network call. Only
    an ``upstream_byok`` figure corrects: a non-BYOK response settled at 0 was billed 0,
    and a correction must be exactly what :func:`settled_cost` would settle today, which is
    what makes it replay-stable. Exactly one response, or it is refused.
    """
    responses = events(conn, "model_response", identifier)
    if len(responses) != 1 or type(responses[0]) is not dict:
        return None
    settled = settled_cost(responses[0].get("usage"))
    if settled is None or settled[1] != "upstream_byok":
        return None
    amount = _microusd(settled[0])
    if amount is None or amount == 0:
        return None
    return CostCorrection(scope, identifier, amount)


def correct_costs(conn: sqlite3.Connection, *, apply: bool = False) -> CorrectionReport:
    """Correct every reservation settled at 0 that has a valid upstream BYOK figure (M1-348).

    Dry run unless ``apply``. Append-only: each correction is a ``cost_corrected`` event in
    the settlement's own budget scope, which :func:`spending` applies over the settlement,
    plus a ``cost_corrected_id`` guard scoped by reservation. The guard is checked again
    inside the writing transaction, so a second run -- or two at once -- writes nothing
    new. A reservation settled at 0 with no valid upstream figure is counted as refused and
    never written: a correction only ever records a figure the provider reported.
    """
    rows = _journal_rows(conn, "cost_settled")
    corrections: list[CostCorrection] = []
    seen: set[str] = set()
    already = refused = 0
    for scope, data in rows:
        if type(scope) is not str:
            raise StorageFailure("cannot read tournament spending")
        ((identifier, amount),) = _amounts([data], "actual_microusd")
        if amount != 0 or identifier in seen:
            continue
        seen.add(identifier)
        if events(conn, "cost_corrected_id", identifier):
            already += 1
            continue
        correction = _correction(conn, scope, identifier)
        if correction is None:
            refused += 1
        else:
            corrections.append(correction)
    written = 0
    if apply:
        for correction in corrections:
            with storage_transaction(conn):
                if events(conn, "cost_corrected_id", correction.reservation_id):
                    continue
                append(
                    conn,
                    "cost_corrected",
                    correction.scope,
                    {
                        "reservation_id": correction.reservation_id,
                        "actual_microusd": correction.actual_microusd,
                        "basis": "upstream_byok",
                    },
                )
                append(conn, "cost_corrected_id", correction.reservation_id, {})
                written += 1
    settlements, asknews_refused = _asknews_settlements(conn)
    asknews_written = 0
    if apply:
        for settlement in settlements:
            with storage_transaction(conn):
                if events(conn, "cost_settled_id", settlement.reservation_id):
                    continue
                append(
                    conn,
                    "cost_settled",
                    settlement.scope,
                    {
                        "reservation_id": settlement.reservation_id,
                        "actual_microusd": settlement.actual_microusd,
                        "basis": "asknews_credits",
                        "backfilled": True,
                    },
                )
                append(conn, "cost_settled_id", settlement.reservation_id, {})
                asknews_written += 1
    return CorrectionReport(
        tuple(corrections),
        already,
        refused,
        written,
        settlements,
        asknews_refused,
        asknews_written,
    )


def _journal_rows(conn: sqlite3.Connection, kind: str) -> list[tuple[Any, Any]]:
    """Every ``(scope, data)`` of one kind, in journal order."""
    try:
        return [
            (row[0], json.loads(row[1]))
            for row in conn.execute(
                "SELECT scope,data FROM tournament_events WHERE kind=? ORDER BY seq", (kind,)
            )
        ]
    except (sqlite3.Error, ValueError):
        raise StorageFailure("cannot read tournament journal") from None


def _asknews_settlements(
    conn: sqlite3.Connection,
) -> tuple[tuple[AskNewsSettlement, ...], int]:
    """Every held AskNews reservation with a stored credit count, and how many had none (M1-336).

    Before M1-336 no AskNews reservation settled, so each was held at its full estimate for
    good. The response that billed it is already in the journal: ``retrieval_started`` names
    the reservation and its call scope, and that scope's ``retrieval_completed`` holds the
    response, ``usage.credits`` included. This reads only the journal -- no network call --
    and converts with the same :func:`credits_microusd` the adapter settles with today, so a
    back-filled settlement is exactly the one a live call would have written.

    Refused, and left held: a reservation with no single ``retrieval_started`` naming it, a
    call scope without exactly one ``retrieval_completed`` (the outcome is unknown), or a
    response whose credit count is missing or malformed. Unknown is never free.
    """
    from whiskeyjack_bot.research.asknews_cost import credits_microusd

    started: dict[str, list[str]] = {}
    for scope, data in _journal_rows(conn, "retrieval_started"):
        identifier = data.get("reservation_id") if type(data) is dict else None
        if type(scope) is str and type(identifier) is str:
            started.setdefault(identifier, []).append(scope)
    found: list[AskNewsSettlement] = []
    seen: set[str] = set()
    refused = 0
    for scope, data in _journal_rows(conn, "cost_reserved"):
        identifier = data.get("reservation_id") if type(data) is dict else None
        if type(scope) is not str or type(identifier) is not str:
            raise StorageFailure("cannot read tournament spending")
        if data.get("provider") != "asknews" or identifier in seen:
            continue
        seen.add(identifier)
        if events(conn, "cost_settled_id", identifier):
            continue
        calls = started.get(identifier, [])
        completed = events(conn, "retrieval_completed", calls[0]) if len(calls) == 1 else []
        amount = (
            credits_microusd(completed[0].get("response"))
            if len(completed) == 1 and type(completed[0]) is dict
            else None
        )
        if amount is None:
            refused += 1
            continue
        found.append(AskNewsSettlement(scope, identifier, amount))
    return tuple(found), refused


@dataclass
class Budget:
    conn: sqlite3.Connection
    root: Path
    scope: str
    ceiling: int
    secret_names: tuple[str, ...] = ()
    # M1-354: the series this budget's activation was bound under, and its ceiling. Every
    # reservation is stamped with the id and refused past the ceiling, in the same
    # transaction as the per-project check.
    series_id: str | None = None
    series_ceiling: int = 0
    # M1-355: the AskNews share of the series ceiling, or None for a series with no sub-cap
    # recorded (enabled before M1-355) and for a pinned activation.
    asknews_ceiling: int | None = None

    def reserve(self, provider: str, estimate: float, request: Any) -> str:
        if not math.isfinite(estimate) or estimate <= 0:
            raise TournamentError("billable call has no conservative cost bound")
        amount = math.ceil(estimate * 1_000_000)
        identifier = uuid4().hex
        # The budget level this reservation reached, if any (M1-329). Computed inside the
        # transaction, where the numbers are already in hand and consistent, but reported
        # outside it: this block holds BEGIN IMMEDIATE, and an HTTP POST inside it would
        # serialize every other process's budget check behind a third party's latency.
        crossed: int | None = None
        series_crossed: int | None = None
        asknews_crossed: list[int] = []
        try:
            # BEGIN IMMEDIATE serializes budget checks across processes and restarts.
            with storage_transaction(self.conn):
                require_spending_clear(self.conn, self.scope)
                actual, held = spending(self.conn, self.scope)
                if actual + held + amount > self.ceiling:
                    # 100 rather than a configured level: the ceiling is not a threshold
                    # that was crossed, it is the refusal itself, and it is the one budget
                    # condition that has already stopped a paid call from happening.
                    crossed = 100
                    raise TournamentError("round budget exhausted; no provider call made")
                reservation: dict[str, Any] = {
                    "reservation_id": identifier,
                    "provider": provider,
                    "estimate_microusd": amount,
                }
                if self.series_id is not None:
                    series_actual, series_held = series_spending(self.conn, self.series_id)
                    series_used = series_actual + series_held + amount
                    if series_used > self.series_ceiling:
                        series_crossed = 100
                        raise TournamentError("series budget exhausted; no provider call made")
                    series_crossed = budget_level_crossed(series_used, self.series_ceiling)
                    reservation["series_id"] = self.series_id
                    if provider == "asknews" and self.asknews_ceiling is not None:
                        news_actual, news_held = series_spending(
                            self.conn, self.series_id, "asknews"
                        )
                        news_used = news_actual + news_held + amount
                        if news_used > self.asknews_ceiling:
                            # Its own refusal and its own exception: research degrades to
                            # the Exa fallback on this one and on no other.
                            asknews_crossed = [100]
                            raise AskNewsSubcapReached(
                                "AskNews sub-cap reached; no provider call made"
                            )
                        # Independent, not exclusive: one reservation from below 80% to
                        # exactly the sub-cap crosses both levels, and both pages are owed.
                        if news_used * 100 >= self.asknews_ceiling * ASKNEWS_PAGE_PERCENT:
                            asknews_crossed = [ASKNEWS_PAGE_PERCENT]
                        if news_used >= self.asknews_ceiling:
                            # Landing exactly on the sub-cap is accepted but is 100% spent:
                            # the page cannot wait for a later, refused attempt.
                            asknews_crossed.append(100)
                crossed = budget_level_crossed(actual + held + amount, self.ceiling)
                append(self.conn, "cost_reserved", self.scope, reservation)
        finally:
            for level in asknews_crossed if self.series_id is not None else ():
                # M1-355. Beside the series' own page, on its own subject so the two
                # throttle independently; constants and the level only in the text.
                emit(
                    "budget_threshold",
                    subject=f"series-{self.series_id}-asknews-{level}",
                    title=f"whiskeyjack: AskNews sub-cap at {level}%",
                    body=(
                        f"AskNews spending across the followed MiniBench series has reached "
                        f"{level}% of its sub-cap. Reserved spend counts toward "
                        f"this. "
                        + (
                            "The sub-cap is reached: research continues on the Exa fallback."
                            if level == 100
                            else "Check `tournament status` for the split."
                        )
                    ),
                )
            if series_crossed is not None and self.series_id is not None:
                # The series' own threshold, beside the project's. The subject carries the
                # series id so each series pages on its own levels; the id is never sent.
                emit(
                    "budget_threshold",
                    subject=f"series-{self.series_id}-{series_crossed}",
                    title=f"whiskeyjack: series budget at {series_crossed}%",
                    body=(
                        f"Spending across the followed MiniBench series has reached "
                        f"{series_crossed}% of its ceiling. Reserved spend counts toward "
                        f"this. "
                        + (
                            "The ceiling is reached: paid calls are being refused."
                            if series_crossed == 100
                            else "Check `tournament status` for the split."
                        )
                    ),
                )
            # `finally` so the exhaustion refusal above is reported too -- that is the
            # alert an operator most needs, and it is only reachable on the raising path.
            if crossed is not None:
                emit(
                    "budget_threshold",
                    subject=f"{self.scope}-{crossed}",
                    title=f"whiskeyjack: budget at {crossed}%",
                    body=(
                        f"Spending for {self.scope} has reached {crossed}% of its "
                        f"activation ceiling. Reserved spend counts toward this -- a "
                        f"call whose cost is not yet known is held at its full estimate "
                        f"-- so this tracks what will stop the worker, not only what has "
                        f"been billed. "
                        + (
                            "The ceiling is reached: paid calls are being refused."
                            if crossed == 100
                            else "Check `tournament status` for the split."
                        )
                    ),
                )
        from whiskeyjack_bot.redaction import redact_secrets

        request = json.loads(redact_secrets(canonical(request), self.secret_names))
        witness(
            self.conn,
            self.root,
            self.scope,
            {
                "reservation_id": identifier,
                "provider": provider,
                "estimate_microusd": amount,
                "request": request,
            },
        )
        return identifier

    def settle(
        self, identifier: str, actual: float | None, *, basis: CostBasis | None = None
    ) -> None:
        """Settle a reservation down to what was billed; an unknown cost leaves it held.

        ``basis`` names where a model call's figure came from (M1-348); research providers
        settle with ``None``, because neither OpenRouter vocabulary member describes them.
        """
        if actual is None or not math.isfinite(actual) or actual < 0:
            return
        amount = _microusd(actual)
        if amount is None:
            return
        self.settle_microusd(identifier, amount, basis=basis)

    def settle_microusd(
        self, identifier: str, amount: int, *, basis: CostBasis | None = None
    ) -> None:
        """Settle a reservation at an exact micro-USD figure (M1-336).

        For a provider whose bill is an integer count at a fixed rate (AskNews credits), a
        float dollar round trip is not exact: ``ceil(0.075 * 1e6)`` is 75001. Refuses (leaves
        the reservation held) anything but an exact non-negative ``int``.
        """
        if type(amount) is not int or amount < 0:
            return
        with storage_transaction(self.conn):
            # Recovery may reach this after completion was committed but settlement
            # was interrupted. Serialize the check and append across processes.
            if events(self.conn, "cost_settled_id", identifier):
                return
            append(
                self.conn,
                "cost_settled",
                self.scope,
                {"reservation_id": identifier, "actual_microusd": amount, "basis": basis},
            )
            append(self.conn, "cost_settled_id", identifier, {})


CURRENT_BUDGET: ContextVar[Budget | None] = ContextVar("tournament_budget", default=None)


@contextmanager
def budget_context(budget: Budget) -> Iterator[None]:
    token = CURRENT_BUDGET.set(budget)
    try:
        yield
    finally:
        CURRENT_BUDGET.reset(token)
