"""Durable activation, operation journal, and prepaid round limits (LAUNCH; M1-348).

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
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO tournament_events(event_id,kind,scope,data,created_at_utc) "
                "VALUES(?,?,?,?,?)",
                (identifier, kind, scope, canonical(journal_form(data)), utcnow().isoformat()),
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
# to GPT-6 Astra at 5x Sol's prices. It is a code constant rather than configuration on
# purpose: an activation binds to config_sha256, and a paid-call limit living in the same file
# it authorizes would let one edit both raise the limit and re-authorize under it.
MAX_ACTIVATION_BUDGET_USD: Final = 40


def enable(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    account_id: int,
    project_id: int,
    starts: datetime,
    ends: datetime,
    budget_usd: float = 20.0,
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
    prior = events(conn, "activation", "account")
    if any(a["account_id"] != account_id for a in prior):
        raise TournamentError("this ledger is already bound to another bot account")
    check_storage(conn, config.storage.artifact_root)
    data = {
        "activation_id": uuid4().hex,
        "account_id": account_id,
        "project_id": project_id,
        "starts": starts.isoformat(),
        "ends": ends.isoformat(),
        "budget_microusd": math.floor(budget_usd * 1_000_000),
        **bindings(config),
    }
    append(conn, "activation", "account", data)
    return str(data["activation_id"])


def disable(conn: sqlite3.Connection) -> None:
    active = events(conn, "activation", "account")
    if active:
        append(conn, "disabled", active[-1]["activation_id"], {})


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
    moved: list[RetiredBinding] = []
    if activation.get("account_id") != account_id:
        moved.append("account")
    if (
        str(activation.get("project_id")) != project_id
        or str(config.metaculus.tournament.id) != project_id
        or config.metaculus.tournament.use_sdk_current_id
        or (config.environment != "production" and project_id != "32977")
    ):
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
                crossed = budget_level_crossed(actual + held + amount, self.ceiling)
                append(
                    self.conn,
                    "cost_reserved",
                    self.scope,
                    {
                        "reservation_id": identifier,
                        "provider": provider,
                        "estimate_microusd": amount,
                    },
                )
        finally:
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
