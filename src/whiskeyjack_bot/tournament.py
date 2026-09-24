"""Sequential, explicitly activated tournament composition and recovery (LAUNCH)."""

from __future__ import annotations

import fcntl
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, get_args
from uuid import uuid4

from whiskeyjack_bot.approval import approve
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.forecast.priced import PRICED_MODELS
from whiskeyjack_bot.forecast.record import record_sha256
from whiskeyjack_bot.forecast.replay import replay_forecast
from whiskeyjack_bot.forecast.store import read_forecast_record
from whiskeyjack_bot.lifecycle import PreForecastFailureCode, current_status
from whiskeyjack_bot.metaculus.client import SingleAttemptPoster, build_client
from whiskeyjack_bot.metaculus.snapshots import save_snapshot
from whiskeyjack_bot.notify import build_notifier, describe, emit, notifier_context
from whiskeyjack_bot.pipeline_live import (
    QuestionOutcome,
    QuestionStatus,
    _attempt_question,
    _build_clients,
)
from whiskeyjack_bot.prompt import PromptError, load_prompt
from whiskeyjack_bot.questions.normalize import normalize_questions
from whiskeyjack_bot.submission_live import (
    MetaculusSubmissionGateway,
    classify_refetch,
    expected_points_for_record,
    expected_option_labels,
    expected_values,
    plan_from_payload,
    post_approved_forecast,
    require_live_submission_enabled,
)
from whiskeyjack_bot.submission_payload import authorized_payload
from whiskeyjack_bot.submission_reconcile import (
    ReconciliationError,
    UnrecordedConfirmedPost,
    unrecorded_confirmed_posts,
)
from whiskeyjack_bot.timeouts import phase_timeout
from whiskeyjack_bot.tournament_state import (
    ActivationInactive,
    ActivationRetired,
    Budget,
    ModelOutcomeUnknown,
    StorageFailure,
    TournamentError,
    append,
    budget_context,
    events,
    question_fingerprint,
    require_activation,
    require_spending_clear,
    spending,
    utcnow,
    witness,
)


# Which recorded failures a second attempt could actually change (M1-326).
#
# Deterministic verdicts are re-derived identically from the same question and the same
# stored evidence, so re-buying research to reach one again is not a retry -- it is the
# same answer at full price. `research/quality.py`'s verdict and
# `research/sufficiency.py`'s are both explicitly pure over a stored packet and a fixed
# `now`; `schema_invalid`/`calibration_invalid` are a model response failing a fixed
# schema. The transient half is genuinely worth another attempt: a provider, a socket or
# an unlucky sample.
#
# Split, rather than gating on "any failure", because a provider outage must not
# permanently retire a question -- and gated on the question FINGERPRINT, so an edited
# question is a new question and qualifies again.
DETERMINISTIC_FAILURE_CODES: Final = frozenset(
    {"no_evidence", "stale_evidence", "schema_invalid", "calibration_invalid"}
)

# A transient failure is worth retrying, but not without end: MiniBench question 45754
# spent 10 `generation_failed/internal_error` attempts and 45764 six `schema_invalid`
# ones, and generation runs *after* retrieval is billed, so an unbounded transient retry
# is the same money pump wearing a different label.
MAX_TRANSIENT_ATTEMPTS: Final = 3


@contextmanager
def worker_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TournamentError("a tournament worker is already running") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _confirmed(conn: sqlite3.Connection, record_id: str) -> bool:
    return bool(events(conn, "forecast_confirmed", record_id))


def _attempts(
    conn: sqlite3.Connection, scope: str, fingerprint: str, activation: dict[str, Any]
) -> int:
    """Counted attempts at this question under this activation (M1-326/M1-327's counter)."""
    return sum(
        1
        for event in events(conn, "question_started", scope)
        if event.get("fingerprint") == fingerprint
        and event.get("activation_id") == activation["activation_id"]
    )


def _awaiting_retry(
    conn: sqlite3.Connection,
    scope: str,
    fingerprint: str,
    activation: dict[str, Any],
    now: datetime,
) -> bool:
    """Whether the current checkpoint window already ended in a provider failure (M1-349).

    The window is the one the attempt path below pins ``now`` to: the latest
    ``question_started`` for this fingerprint, if it is at most 1800 s old. Once it has
    expired this is False, so the next poll appends a fresh ``question_started`` and the
    retry is a counted attempt.
    """
    checkpoints = events(conn, "question_started", scope)
    if not checkpoints or checkpoints[-1].get("fingerprint") != fingerprint:
        return False
    started = checkpoints[-1]["at"]
    if (now - datetime.fromisoformat(started)).total_seconds() > 1800:
        return False
    return any(
        event.get("started_at") == started
        and event.get("fingerprint") == fingerprint
        and event.get("activation_id") == activation["activation_id"]
        for event in events(conn, "retry_wait", scope)
    )


def _paces_retry(status: str, detail_code: str | None) -> bool:
    """Whether a failed attempt waits out its checkpoint window before a counted retry.

    Research (M1-349, unchanged): only ``research_failed/provider_error``.

    Generation (M1-351): every ``generation_failed`` code that is not a deterministic
    verdict -- ``internal_error``, ``timeout``, ``provider_error``, ``malformed_response``,
    ``http_error``, ``provider_unavailable``. The deterministic ones (``schema_invalid``,
    ``calibration_invalid``) block instead, below. A code outside the vocabulary paces too:
    waiting is the cheap direction, and blocking is the path that needs a proof.

    **This does not change what a retry costs**, and that was measured before it was
    written. With the production ``PricedClient`` an in-window re-attempt was already free:
    a failed call leaves ``model_started`` with no completion and the same request is refused,
    and a completed call is replayed from the journal. Live question 45754's ten
    ``internal_error`` rows are two billed calls, one per window. What the re-attempt did cost
    was a failure row, a failed poll (exit 1, ``OnFailure``) and a duplicate
    ``generation_failed`` row every five minutes -- the row inflation that made two attempts
    look like ten.
    """
    if status == "research_failed":
        return detail_code == "provider_error"
    if status == "generation_failed":
        return detail_code not in DETERMINISTIC_FAILURE_CODES
    return False


def _append_retry_wait(
    conn: sqlite3.Connection,
    scope: str,
    fingerprint: str,
    activation: dict[str, Any],
    window_start: datetime,
) -> None:
    """The row ``_awaiting_retry`` reads (M1-349), written from one place for both callers."""
    append(
        conn,
        "retry_wait",
        scope,
        {
            "at": utcnow().isoformat(),
            "fingerprint": fingerprint,
            "activation_id": activation["activation_id"],
            "started_at": window_start.isoformat(),
        },
    )


_QUESTION_STATUSES: Final = frozenset(get_args(QuestionStatus))
_PRE_FORECAST_CODES: Final = frozenset(get_args(PreForecastFailureCode))


def _failed_outcome_reason(outcome: QuestionOutcome) -> str:
    """The recorded reason for an attempt that produced no usable record (M1-325).

    Built from two closed vocabularies and nothing else, so it is value-free by construction;
    anything outside them renders as ``unclassified`` rather than being echoed. Before this
    every such failure -- a quality-gate refusal, a sufficiency-gate refusal, a failed
    generation -- recorded the same sentence, and the ledger could not say which.
    """
    status = outcome.status if outcome.status in _QUESTION_STATUSES else "unclassified"
    code = outcome.detail_code
    detail = code if code in _PRE_FORECAST_CODES else ("none" if code is None else "unclassified")
    return f"question {status} ({detail})"


def _question_failure_reason(exc: Exception) -> str | None:
    """What a ``question_failure`` row may say about ``exc`` beyond its type (M1-325).

    A ``TournamentError`` message is module-owned: every raise site passes a literal or a
    translation of an already-sanitized error, which ``test_question_failure_reason``'s
    source scan holds every raise site to. Any other type is withheld -- a third-party
    message can quote a request, a response body or a header.
    """
    return str(exc) if isinstance(exc, TournamentError) else None


def _notify_blocked(scope: str, *, reason: str, detail: str | None) -> None:
    """Report that a question will not be attempted again under this activation (M1-329).

    Called from the two places that *append* ``question_blocked``, never from the read
    gate that skips an already-blocked question: the gate runs on every poll, so alerting
    there would re-page every five minutes for a verdict recorded once.

    This is the alert the 2026-09-08 incident actually needed. Measured on the Cup ledger:
    question 45452 failed with ``no_evidence``, which is a deterministic code, so under
    today's code this fires on the first attempt -- about four minutes in, against the nine
    hours it took the vendor's cap email to arrive.
    """
    emit(
        "question_blocked",
        subject=f"{scope}-{reason}",
        title=f"whiskeyjack: question blocked ({reason})",
        body=(
            f"{scope} will not be attempted again under this activation. "
            f"reason={reason} detail={detail or 'none'}. "
            f"Re-running `tournament enable` clears the verdict and costs one retry."
        ),
    )


def _unrecorded(conn: sqlite3.Connection) -> tuple[UnrecordedConfirmedPost, ...]:
    """The M1-342 read, as this module's error type.

    One translation point rather than one per call site: `status` and `run_once` both ask, a
    caller of either handles `TournamentError` and nothing else, and `ReconciliationError` is
    already sanitized so there is nothing to strip -- only a type to change.
    """
    try:
        return unrecorded_confirmed_posts(conn)
    except ReconciliationError as exc:
        raise TournamentError(str(exc)) from None


def _notify_unrecorded_posts(found: Sequence[UnrecordedConfirmedPost]) -> None:
    """Page for each forecast the platform shows and the lifecycle ledger has not recorded
    (M1-342).

    **Fired from the condition, not from the transition**, which is the opposite of
    :func:`_notify_blocked` above and deliberately so. The two alerts are different shapes:
    ``question_blocked`` reports a verdict *recorded once*, so alerting at the read gate that
    replays it would re-page for a decision already taken. This reports a disagreement between
    the platform and the attribution ledger that **holds until an operator acts**, exactly as
    ``activation_retired`` does, and the day-long window keyed on the record is what turns a
    five-minute poll into one page a day per record rather than 288.

    The transition -- ``reconcile_forecast``'s first-confirmation guard -- was the alternative,
    and it fires exactly once for the life of the record. Three things follow from that and all
    three are worse: a record already confirmed before this shipped could never page; a page
    missed at 04:00 is the only page there will ever be; and "the alert clears after
    reconciliation" becomes unfalsifiable, because a transition hook is silent on the second
    poll whether or not anyone reconciled anything.

    Writes nothing and posts nothing. The body names the record and its question -- the platform
    already shows the forecast publicly, and a record id is what ``reconcile-submission`` takes
    -- and no rationale, no digest and no config value. The title is static for M1-334's reason:
    it is transmitted, and the throttle subject is not.
    """
    for entry in found:
        emit(
            "unrecorded_post",
            subject=entry.record_id,
            title="whiskeyjack: a live forecast is missing from the ledger",
            body=(
                f"A forecast is live on the platform and the lifecycle ledger has not "
                f"recorded the post, so it is never resolved or scored. "
                f"record={entry.record_id} question={entry.question_id}. "
                f"Check the question on Metaculus, then record it with `reconcile-submission` "
                f"(runbook L4). Nothing will retry this on its own."
            ),
        )


def _notify_poll_summary(data: dict[str, Any]) -> None:
    """Send the daily liveness digest (M1-329).

    A push per poll would be 288 a day and the channel would be muted, which is the same
    as having no channel; the throttle window for this event is 24 hours, so this is a
    digest. Its job is not to report a poll -- it is to be *missed*. The systemd
    ``OnFailure`` unit fires when a unit fails, and this module fires when the code
    notices something; neither says anything at all when the timer is disabled, the user
    session ends, or the machine is off. A digest that stops arriving is the only signal
    that covers those.
    """
    heartbeat = data.get("heartbeat") or {}
    emit(
        "poll_summary",
        subject="worker",
        title="whiskeyjack: daily worker digest",
        body=describe(
            [
                ("discovered", heartbeat.get("discovered", 0)),
                ("processed", heartbeat.get("processed", 0)),
                ("skipped", heartbeat.get("skipped", 0)),
                ("failures", heartbeat.get("failures", 0)),
                ("blocked", heartbeat.get("blocked", 0)),
                ("confirmed_total", data.get("forecast_confirmed", 0)),
                ("unresolved", data.get("unresolved", 0)),
                ("actual_usd", round(float(data.get("actual_cost_usd", 0.0)), 4)),
                ("reserved_usd", round(float(data.get("reserved_cost_usd", 0.0)), 4)),
                ("remaining_usd", round(float(data.get("remaining_budget_usd", 0.0)), 4)),
            ]
        ),
    )


def reconcile_forecast(
    conn: sqlite3.Connection, config: AppConfig, poster: Any, record_id: str
) -> bool:
    intents = events(conn, "forecast_intent", record_id)
    if not intents:
        return False
    intent = intents[-1]
    if poster.get_current_user_id() != intent["account_id"]:
        raise TournamentError("forecast reconciliation account changed")
    gateway = MetaculusSubmissionGateway(poster=poster)
    observed = gateway.observe(intent["post_id"], question_id=intent["question_id"])
    if observed is None:
        return False
    # The record's question decides the length, not a literal 201 (M1-205 round 1). A
    # discrete forecast reconciled against 201 refuses its own stored payload, so a posted
    # forecast would never reach `forecast_confirmed` and would be retried as unresolved
    # for as long as the intent stands.
    plan = plan_from_payload(
        intent["payload"],
        expected_cdf_points=expected_points_for_record(
            read_forecast_record(conn, record_id), config
        ),
    )
    result = classify_refetch(
        question_type=plan.question_type,
        expected=expected_values(plan),
        baseline_latest_start_time=None,
        observed=observed,
        expected_labels=expected_option_labels(plan),
    )
    if result.outcome != "confirmed":
        return False
    if not _confirmed(conn, record_id):
        append(
            conn,
            "forecast_confirmed",
            record_id,
            {
                "account_id": intent["account_id"],
                "post_id": intent["post_id"],
                "question_id": intent["question_id"],
                "payload_sha256": intent["payload_sha256"],
            },
        )
        # Inside the guard, not after `return True` below (M1-329). This branch is
        # "confirmed for the first time"; the function itself runs on every poll that
        # re-reconciles an already-confirmed record, so a hook at the return would page
        # every five minutes for a forecast that landed days ago.
        #
        # CONFIRMED means what `classify_refetch` above returned, not what the POST
        # returned: the payload was refetched from the platform and matched. And it is
        # emitted after the ledger row is committed, so the ledger never lags a push.
        #
        # The body carries the question, the post and the payload hash -- all of which the
        # platform already shows publicly -- and no rationale field. ntfy is a third party;
        # the posted value is public the moment it lands, the reasoning is not, and it
        # never becomes public.
        emit(
            "prediction_posted",
            subject=record_id,
            title=f"whiskeyjack: forecast confirmed on question {intent['question_id']}",
            body=(
                f"A forecast is live and verified by refetch. "
                f"question={intent['question_id']} post={intent['post_id']} "
                f"payload_sha256={intent['payload_sha256'][:12]}"
            ),
        )
    return True


EVIDENCE_POOR_NOTICE: Final = (
    "Evidence: none retrieved. No research documents were retrieved for this question, so "
    "this forecast rests on a base rate and the model's prior alone.\n\n"
)


def is_evidence_poor(conn: sqlite3.Connection, record: Any) -> bool:
    """Whether ``record`` carries a valid M1-349 evidence-poor marker.

    The marker is an ``evidence_gap`` row with code ``evidence_poor``, scoped to the record
    and bound to its ``forecast_sha256``. A marker whose hash is not this record's is
    refused rather than ignored: it describes some other content, and reading it as
    absent would let the record be published as though it had evidence.
    """
    markers = [
        event
        for event in events(conn, "evidence_gap", record.record_id)
        if event.get("code") == "evidence_poor"
    ]
    if not markers:
        return False
    digest = record_sha256(record)
    if any(event.get("forecast_sha256") != digest for event in markers):
        raise TournamentError("evidence-poor marker does not match the forecast hash")
    return True


def comment_text(conn: sqlite3.Connection, record_id: str) -> str:
    record = read_forecast_record(conn, record_id)
    forecast = record.forecast.model_dump(mode="json")
    # These are the public concise response fields requested by the forecaster schema.
    forecast.pop("as_of_utc", None)
    # M1-349: said in the comment, above the rationale, because the rationale itself is
    # model output inside `forecast_sha256` and replayed byte for byte (M1-406), so it
    # cannot be rewritten after the fact. A record without the marker is unchanged.
    notice = EVIDENCE_POOR_NOTICE if is_evidence_poor(conn, record) else ""
    return (
        f"Whiskeyjack forecast — subquestion {record.question_id}: {record.question.title}\n\n"
        + notice
        + f"Concise forecast and rationale:\n{json.dumps(forecast, ensure_ascii=False, indent=2)}\n\n"
        + "Sources:\n"
        + ("\n".join(s.canonical_url for s in record.sources) or "(none)")
        + f"\n\nModel: {record.model_settings.name}; prompt: {record.model_settings.prompt_version}"
        + f" ({record.model_settings.prompt_sha256})\n"
        + f"Record: [whiskeyjack:{record_id}]\nForecast SHA256: {record_sha256(record)}"
    )


def _matching_comments(
    comments: list[dict[str, Any]], *, account_id: int, post_id: int, marker: str
) -> list[dict[str, Any]]:
    matches = []
    for comment in comments:
        author = comment.get("author")
        owner = author.get("id") if isinstance(author, dict) else author
        post = comment.get("on_post")
        post = post.get("id") if isinstance(post, dict) else post
        if (
            owner == account_id
            and post == post_id
            and comment.get("is_private") is True
            and marker in str(comment.get("text", ""))
            and type(comment.get("id")) is int
        ):
            matches.append(comment)
    return matches


def complete_comment(
    conn: sqlite3.Connection,
    config: AppConfig,
    poster: Any,
    record_id: str,
    *,
    allow_post: bool = True,
) -> bool:
    if not _confirmed(conn, record_id):
        return False
    if events(conn, "comment_confirmed", record_id):
        return True
    record = read_forecast_record(conn, record_id)
    account = poster.get_current_user_id()
    require_activation(conn, config, account_id=account, project_id=record.tournament_id)
    marker = f"[whiskeyjack:{record_id}]"
    matches = _matching_comments(
        poster.list_my_comments(record.post_id, account),
        account_id=account,
        post_id=record.post_id,
        marker=marker,
    )
    intents = events(conn, "comment_intent", record_id)
    receipts = events(conn, "comment_receipt", record_id)
    if len(matches) > 1:
        raise TournamentError("duplicate comment markers require operator reconciliation")
    if matches:
        if receipts and receipts[-1]["comment_id"] != matches[0]["id"]:
            raise TournamentError("comment receipt and refetch disagree")
        append(
            conn,
            "comment_confirmed",
            record_id,
            {
                "comment_id": matches[0]["id"],
                "account_id": account,
                "post_id": record.post_id,
                "is_private": True,
                "marker": marker,
            },
        )
        return True
    if intents or not allow_post:
        # A missing result in an immediate or delayed GET does not prove no creation.
        return False
    text = comment_text(conn, record_id)
    intent = {
        "account_id": account,
        "post_id": record.post_id,
        "text": text,
        "is_private": True,
        "marker": marker,
    }
    witness(conn, config.storage.artifact_root, record_id, intent)
    append(conn, "comment_intent", record_id, intent)
    try:
        response = poster.create_private_comment(record.post_id, text)
    except Exception:
        return False
    if isinstance(response, dict) and type(response.get("id")) is int:
        append(conn, "comment_receipt", record_id, {"comment_id": response["id"]})
    # Success is exclusively the verified read, never a 2xx or printed message.
    return complete_comment(conn, config, poster, record_id)


def status(conn: sqlite3.Connection, config: AppConfig) -> dict[str, Any]:
    activations = events(conn, "activation", "account")
    data: dict[str, Any] = {
        "enabled": False,
        "refusal_reason": None,
        "spending_held": False,
        "restored_spending_holds": 0,
        "heartbeat": None,
        "forecast_confirmed": 0,
        "comment_completed": 0,
        "unresolved": 0,
        "unrecorded_posts": 0,
    }
    heartbeats = events(conn, "heartbeat", "worker")
    if heartbeats:
        data["heartbeat"] = heartbeats[-1]
    if activations:
        active = activations[-1]
        actual, held = spending(conn, f"{active['account_id']}:{active['project_id']}")
        data.update(
            project_id=active["project_id"],
            account_id=active["account_id"],
            restored_spending_holds=len(
                events(
                    conn, "restored_spending_hold", f"{active['account_id']}:{active['project_id']}"
                )
            ),
            actual_cost_usd=actual / 1_000_000,
            reserved_cost_usd=held / 1_000_000,
            remaining_budget_usd=max(0, active["budget_microusd"] - actual - held) / 1_000_000,
        )
    # This is a local configuration/storage check, not live credential verification.
    try:
        require_activation(
            conn,
            config,
            account_id=activations[-1]["account_id"] if activations else 0,
            project_id=str(config.metaculus.tournament.id),
        )
        data["enabled"] = True
    except ActivationInactive:
        pass
    except TournamentError as exc:
        data["refusal_reason"] = str(exc)
    data["spending_held"] = bool(data["restored_spending_holds"])
    if data["spending_held"]:
        data["enabled"] = False
        data["refusal_reason"] = data["refusal_reason"] or (
            "restored spending outcome is unknown; spending hold blocks purchases"
        )
    for kind, key in (
        ("forecast_confirmed", "forecast_confirmed"),
        ("comment_confirmed", "comment_completed"),
    ):
        data[key] = conn.execute(
            "SELECT count(DISTINCT scope) FROM tournament_events WHERE kind=?", (kind,)
        ).fetchone()[0]
    data["unresolved"] = conn.execute(
        "SELECT count(DISTINCT scope) FROM tournament_events i WHERE kind IN ('forecast_intent','comment_intent') "
        "AND NOT EXISTS (SELECT 1 FROM tournament_events c WHERE c.scope=i.scope AND c.kind='comment_confirmed')"
    ).fetchone()[0]
    data["restored_question_holds"] = conn.execute(
        "SELECT count(DISTINCT scope) FROM tournament_events WHERE kind='restored_question_hold'"
    ).fetchone()[0]
    # Forecasts made without a document from a resolution authority the question named
    # (M1-327). Counted by scope, which is the record id, so this is a count of forecasts
    # and not of gaps. Reported rather than held against anything: it is not a failure and
    # deliberately does not touch `unresolved`.
    data["evidence_gaps"] = conn.execute(
        "SELECT count(DISTINCT scope) FROM tournament_events WHERE kind='evidence_gap'"
    ).fetchone()[0]
    # Forecasts the journal confirms are live while the lifecycle ledger has not recorded the
    # post (M1-342). Reported, like `evidence_gaps` and deliberately not added to `unresolved`:
    # `unresolved` is the poll's own exit code (`cli.py` returns 1 for it), and a systemd unit
    # that fails every five minutes over a condition only a person can clear would page through
    # `whiskeyjack-notify@` on every poll and mark the timer's unit failed for days. The page
    # for this is `_notify_unrecorded_posts`, throttled; this field is the standing count.
    data["unrecorded_posts"] = len(_unrecorded(conn))
    data["unresolved"] += data["restored_question_holds"]
    data["recent_question_failures"] = _recent_question_failures(conn)
    return data


RECENT_QUESTION_FAILURES: Final = 5


def _recent_question_failures(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The newest ``question_failure`` rows, newest first, as ``status`` reports them (M1-325).

    The read path the criterion asks for: why a question failed, without a sqlite query.
    Rows written before M1-325 carry no ``reason`` and report ``None``, which is what they
    recorded. Only the named fields are read back, and a row that is not an object is
    reported as a storage failure rather than guessed at -- the journal is the ledger's.
    """
    try:
        rows = conn.execute(
            "SELECT scope, data FROM tournament_events WHERE kind='question_failure' "
            "ORDER BY seq DESC LIMIT ?",
            (RECENT_QUESTION_FAILURES,),
        ).fetchall()
        found: list[dict[str, Any]] = []
        for scope, raw in rows:
            data = json.loads(raw)
            if type(data) is not dict:
                raise ValueError
            found.append(
                {
                    "scope": scope,
                    "error_type": data.get("error_type"),
                    "reason": data.get("reason"),
                    "at": data.get("at"),
                }
            )
    except (sqlite3.Error, ValueError):
        raise StorageFailure("cannot read tournament journal") from None
    return found


def run_once(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    client: Any | None = None,
    poster: Any | None = None,
    news_client: Any | None = None,
    web_client: Any | None = None,
    forecaster: Any | None = None,
    question_id: int | None = None,
) -> dict[str, Any]:
    require_live_submission_enabled(config)
    if not config.submission.post_private_reasoning_comment:
        raise TournamentError("tournament operation requires private reasoning comments")
    if (
        config.model.name not in PRICED_MODELS
        or config.model.temperature is not None
        or config.model.max_output_tokens != 6000
        or config.model.timeout_seconds != 120
        or config.model.allowed_tries > 2
        or config.retrieval.primary.retries
        or config.retrieval.fallback.retries
        or config.retrieval.max_documents_per_query > 8
    ):
        raise TournamentError(
            "tournament model, research bounds, or no-retry policy is not configured"
        )
    with (
        worker_lock(config.storage.sqlite_path.with_suffix(".worker.lock")),
        notifier_context(build_notifier(config)),
    ):
        client = build_client(config) if client is None else client
        poster = SingleAttemptPoster(client) if poster is None else poster
        account = poster.get_current_user_id()
        project = str(config.metaculus.tournament.id)
        try:
            activation = require_activation(conn, config, account_id=account, project_id=project)
        except ActivationRetired as retired:
            # M1-334. The notifier is already installed here (since M1-329), so this is
            # the ordinary `emit` and degrades exactly as every other alert does: a failed
            # or slow push is absorbed and never changes the refusal below. Names the moved
            # bindings, never a digest or a config value.
            #
            # The project is the throttle *subject* only, which keeps one page per profile:
            # `Notifier` sha256s the subject into a stamp filename and never transmits it.
            # The title is static because it is transmitted, and the project ID is a config
            # value (round 1). Which profile it was is in the operator log and the refusal.
            emit(
                "activation_retired",
                subject=project,
                title="whiskeyjack: activation retired",
                body=(
                    f"Every poll is refusing: {', '.join(retired.changed)} changed since "
                    "the activation was enabled. Nothing is bought or posted until "
                    "`tournament enable` is re-run for this profile."
                ),
            )
            raise
        heartbeat: dict[str, Any] = {
            "at": utcnow().isoformat(),
            "discovered": 0,
            "processed": 0,
            "skipped": 0,
            "failures": 0,
            "complete": False,
        }
        append(conn, "heartbeat", "worker", heartbeat)
        spending_held = bool(events(conn, "restored_spending_hold", f"{account}:{project}"))
        # Resume external phases even after a question has closed or left discovery.
        pending = conn.execute(
            "SELECT DISTINCT scope FROM tournament_events WHERE kind='forecast_intent'"
        ).fetchall()
        for row in pending:
            if events(conn, "comment_confirmed", row[0]):
                continue
            intent = events(conn, "forecast_intent", row[0])[-1]
            if str(intent["project_id"]) != project:
                continue
            try:
                with phase_timeout(240):
                    if not reconcile_forecast(conn, config, poster, row[0]) or not complete_comment(
                        conn, config, poster, row[0], allow_post=not spending_held
                    ):
                        heartbeat["failures"] += 1
            except (StorageFailure, sqlite3.Error, OSError):
                raise StorageFailure("recovery storage failed; worker stopped") from None
            except Exception:
                heartbeat["failures"] += 1
        if spending_held:
            heartbeat.update(
                failures=heartbeat["failures"] + 1, complete=True, at=utcnow().isoformat()
            )
            append(conn, "heartbeat", "worker", heartbeat)
            # One of run_once's two exits. A digest hook on only the normal one would go
            # quiet in exactly the state that most warrants a digest: a spending hold.
            held_summary = status(conn, config)
            _notify_unrecorded_posts(_unrecorded(conn))
            _notify_poll_summary(held_summary)
            return held_summary
        questions = client.get_all_open_questions_from_tournament(
            int(project), group_question_mode="unpack_subquestions"
        )
        snapshot = config.storage.artifact_root / "snapshots" / f"poll-{uuid4().hex}.json"
        save_snapshot(
            snapshot,
            questions,
            tournament_id=int(project),
            group_question_mode="unpack_subquestions",
            source="live",
            fetched_at_utc=utcnow(),
        )
        normalized = normalize_questions(questions)
        heartbeat["discovered"] = len(questions)
        ordered = sorted(normalized.questions, key=lambda q: q.close_time or utcnow())
        budget = Budget(
            conn,
            config.storage.artifact_root,
            f"{account}:{project}",
            activation["budget_microusd"],
            tuple(config.secret_env_var_names()),
        )
        try:
            prompt = load_prompt(
                config.forecast.prompt_path,
                config.forecast.prompt_version,
                min_probability=config.forecast.min_probability,
                max_probability=config.forecast.max_probability,
            )
        except PromptError as exc:
            # M1-407. This is the live worker's only prompt load, and it sits after the
            # snapshot fetch but ahead of ``clients``, so every refusal here lands before
            # the first billable call. Translated to this module's error type rather than
            # left to propagate: ``PromptError`` is already sanitized, but a caller of
            # ``run_once`` handles ``TournamentError``, and until M1-407 this was the one
            # ``load_prompt`` call site with no translation at all.
            raise TournamentError(str(exc)) from None
        clients: tuple[Any, Any, Any] | None = None
        for question in ordered:
            if question_id is not None and question.question_id != question_id:
                continue
            now = utcnow()
            if question.close_time is None or question.close_time <= now + timedelta(minutes=5):
                heartbeat["skipped"] += 1
                continue
            if events(conn, "restored_question_hold", f"{project}:{question.question_id}"):
                heartbeat["skipped"] += 1
                continue
            # The M1-326 gate, placed with the other skips and so ahead of every provider
            # call AND ahead of the Metaculus refetch below. A question whose recorded
            # verdict cannot change is declined here for free; before this, the verdict was
            # re-derived by re-buying the research it was derived from, every 30 minutes,
            # for as long as the question stayed open.
            scope = f"{project}:{question.question_id}"
            fingerprint = question_fingerprint(question)
            # Both counters are scoped to the CURRENT activation as well as to the
            # fingerprint (M1-327). The fingerprint alone was not enough, and the gap was
            # not hypothetical: `question_started` predates M1-326 -- it was written by
            # the launch-readiness *checkpoint*, which was never an attempt counter -- so
            # the Cup ledger carries 17 of them for question 45452 under one fingerprint
            # against a cap of 3. Read that history as attempts and the first poll after
            # this change retires the question as `transient_attempts_exhausted` before
            # the gate M1-327 exists to fix ever runs. Rows written before this change
            # carry no `activation_id` and so match no activation, which is the intended
            # reading of them: they recorded that a question was started, and claimed
            # nothing about how many times it had been tried.
            #
            # It also gives a verdict an expiry. A deterministic block says "this cannot
            # change", which is only ever true of a fixed question *and fixed code*; when
            # the code that produced the verdict changes, the operator needs a way to
            # retire the verdicts it invalidated. `tournament enable` is that gesture --
            # already required after any config change, already recorded, already the
            # explicit "go" -- and re-running it costs at most one re-purchase per blocked
            # question per activation, under the same budget guard as any other purchase.
            blocked = [
                event
                for event in events(conn, "question_blocked", scope)
                if event.get("fingerprint") == fingerprint
                and event.get("activation_id") == activation["activation_id"]
            ]
            if blocked:
                heartbeat["skipped"] += 1
                heartbeat["blocked"] = heartbeat.get("blocked", 0) + 1
                continue
            attempts = _attempts(conn, scope, fingerprint, activation)
            if attempts >= MAX_TRANSIENT_ATTEMPTS:
                # Round 1 finding B1: exhaustion skipped the question but recorded no
                # `question_blocked` row, so the exhausted question was visible only as an
                # aggregate heartbeat count and never identifiable in the ledger. Appended
                # at the transition and exactly once: the `blocked` check above runs first
                # on every later poll, so this branch is not reached again.
                append(
                    conn,
                    "question_blocked",
                    scope,
                    {
                        "at": utcnow().isoformat(),
                        "fingerprint": fingerprint,
                        "activation_id": activation["activation_id"],
                        "reason": "transient_attempts_exhausted",
                        "detail_code": None,
                        "attempts": attempts,
                    },
                )
                _notify_blocked(scope, reason="transient_attempts_exhausted", detail=None)
                heartbeat["skipped"] += 1
                heartbeat["exhausted"] = heartbeat.get("exhausted", 0) + 1
                continue
            if _awaiting_retry(conn, scope, fingerprint, activation, now):
                # M1-349: this checkpoint window already bought research that a provider
                # failed to deliver. Declined for free until the window expires, when the
                # next poll starts a new, counted attempt (see `retry_wait` below).
                heartbeat["skipped"] += 1
                heartbeat["retry_wait"] = heartbeat.get("retry_wait", 0) + 1
                continue
            existing = conn.execute(
                "SELECT record_id FROM forecast_records WHERE question_id=? AND tournament_id=? "
                "ORDER BY forecast_version DESC LIMIT 1",
                (question.question_id, project),
            ).fetchone()
            if existing and events(conn, "forecast_intent", existing[0]):
                heartbeat["skipped"] += 1
                continue
            prior = MetaculusSubmissionGateway(poster=poster).observe(
                question.post_id, question_id=question.question_id
            )
            if prior is None:
                heartbeat["failures"] += 1
                continue
            if prior.entries:
                heartbeat["skipped"] += 1
                continue
            if heartbeat["processed"] >= config.run_limits.max_questions:
                break
            heartbeat["processed"] += 1
            # The checkpoint window this attempt runs in, once one is established below;
            # None on the recovery path, which buys nothing and so is never paced.
            window_start: datetime | None = None
            try:
                require_activation(conn, config, account_id=account, project_id=project)
                if existing:
                    record_id = str(existing[0])
                else:
                    require_spending_clear(conn, budget.scope)
                    # Persist a cutoff for restart recovery; generation requests stay byte-identical.
                    # `scope` and `fingerprint` are the ones the M1-326 gate above already
                    # computed -- recomputing them here would let the gate and the
                    # checkpoint disagree about what "the same question" means.
                    checkpoints = events(conn, "question_started", scope)
                    if (
                        checkpoints
                        and checkpoints[-1]["fingerprint"] == fingerprint
                        and (now - datetime.fromisoformat(checkpoints[-1]["at"])).total_seconds()
                        <= 1800
                    ):
                        now = datetime.fromisoformat(checkpoints[-1]["at"])
                    else:
                        append(
                            conn,
                            "question_started",
                            scope,
                            {
                                "at": now.isoformat(),
                                "fingerprint": fingerprint,
                                "activation_id": activation["activation_id"],
                            },
                        )
                    window_start = now
                    if clients is None:
                        clients = _build_clients(
                            config,
                            news_client=news_client,
                            web_client=web_client,
                            forecaster=forecaster,
                        )
                    with budget_context(budget), phase_timeout(480):
                        outcome = _attempt_question(
                            conn,
                            config,
                            question=question,
                            tournament_id=project,
                            prompt=prompt,
                            now=now,
                            refresh=False,
                            forecaster=clients[0],
                            news_client=clients[1],
                            web_client=clients[2],
                            # M1-349: an empty retrieval becomes an evidence-poor base-rate
                            # forecast rather than a block -- at once when no provider
                            # failed, and on the LAST transient attempt when one did.
                            # Counted after the checkpoint above, so it includes this one.
                            evidence_poor_fallback=True,
                            final_attempt=_attempts(conn, scope, fingerprint, activation)
                            >= MAX_TRANSIENT_ATTEMPTS,
                        )
                    if _paces_retry(outcome.status, outcome.detail_code):
                        # M1-349 (research) and M1-351 (generation): pace the retry. Inside
                        # one checkpoint window `now` is pinned and no `question_started`
                        # is appended, so without this every 5-minute poll re-ran the
                        # attempt and none of them counted as one. The gate above reads
                        # this row and declines the question until the window expires, so
                        # each retry is one counted attempt.
                        _append_retry_wait(conn, scope, fingerprint, activation, now)
                    if (
                        outcome.status == "not_recorded"
                        or outcome.note
                        or outcome.artifact_outcome == "failed"
                    ):
                        raise StorageFailure("forecast or evidence storage failed; worker stopped")
                    if (
                        outcome.detail_code in DETERMINISTIC_FAILURE_CODES
                        and outcome.status != "recorded"
                    ):
                        # Record the verdict as gate state before re-raising. The question
                        # still fails this poll exactly as it did before M1-326; what
                        # changes is that the next poll can decline it without buying the
                        # evidence again. The fingerprint travels with it, so an edited
                        # question is not covered by an older question's verdict.
                        append(
                            conn,
                            "question_blocked",
                            scope,
                            {
                                "at": utcnow().isoformat(),
                                "fingerprint": fingerprint,
                                "activation_id": activation["activation_id"],
                                "reason": "deterministic_verdict",
                                "detail_code": outcome.detail_code,
                            },
                        )
                        _notify_blocked(
                            scope,
                            reason="deterministic_verdict",
                            detail=outcome.detail_code,
                        )
                    if outcome.status != "recorded" or outcome.record_id is None:
                        raise TournamentError(_failed_outcome_reason(outcome))
                    record_id = outcome.record_id
                record = read_forecast_record(conn, record_id)
                if record.question != question:
                    raise TournamentError("saved forecast has changed question inputs")
                replay_config = config.model_copy(
                    update={
                        "forecast": config.forecast.model_copy(
                            update={"replay_saved_model_output": True}
                        )
                    }
                )
                if not replay_forecast(conn, replay_config, record_id=record_id).matches:
                    raise StorageFailure("saved forecast cannot be replayed")
                # M1-349: read before approval, so a marker bound to other content stops the
                # question before anything is posted, not after the forecast is up and only
                # the comment is left to refuse. Its value is not needed here.
                is_evidence_poor(conn, record)
                if current_status(conn, record_id) == "validated":
                    approve(
                        conn,
                        record_id=record_id,
                        actor=f"policy:launch-v1:{activation['activation_id']}",
                        occurred_at=utcnow(),
                        calibration=config.numeric_calibration,
                        expected_sha256=record_sha256(record),
                        note="Automatic approval under explicit round activation",
                    )
                authorized = authorized_payload(record, calibration=config.numeric_calibration)
                result = post_approved_forecast(
                    conn,
                    config=config,
                    record_id=record_id,
                    payload=authorized.payload,
                    poster=poster,
                    occurred_at=utcnow(),
                )
                if result.artifact_error:
                    raise StorageFailure("submission artifact failed; worker stopped")
                if not reconcile_forecast(conn, config, poster, record_id) or not complete_comment(
                    conn, config, poster, record_id
                ):
                    raise TournamentError("forecast or private comment remains unresolved")
            except StorageFailure:
                raise
            except (sqlite3.Error, OSError):
                raise StorageFailure("storage failed; worker stopped") from None
            except Exception as exc:
                heartbeat["failures"] += 1
                append(
                    conn,
                    "question_failure",
                    f"{project}:{question.question_id}",
                    {
                        "error_type": type(exc).__name__,
                        "reason": _question_failure_reason(exc),
                        "at": utcnow().isoformat(),
                    },
                )
                if isinstance(exc, ModelOutcomeUnknown) and window_start is not None:
                    # M1-351: a priced model call that failed, or whose outcome is unknown,
                    # escapes `_attempt_question` as this error rather than as an outcome.
                    # The same request is already refused for free for the rest of the
                    # window (`PricedClient`'s repeat guard), so pacing it spends nothing
                    # differently; what it stops is a failed poll every five minutes.
                    _append_retry_wait(conn, scope, fingerprint, activation, window_start)
            heartbeat["at"] = utcnow().isoformat()
            append(conn, "heartbeat", "worker", heartbeat)
        heartbeat["complete"] = True
        heartbeat["at"] = utcnow().isoformat()
        append(conn, "heartbeat", "worker", heartbeat)
        summary = status(conn, config)
        # After the recovery loop, so a record that reaches this state during *this* poll pages
        # in this poll rather than the next one, and at both of `run_once`'s exits for the
        # reason the digest is: a hook on only the normal exit goes quiet under a spending hold.
        _notify_unrecorded_posts(_unrecorded(conn))
        _notify_poll_summary(summary)
        return summary
