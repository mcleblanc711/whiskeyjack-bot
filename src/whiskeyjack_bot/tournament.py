"""Sequential, explicitly activated tournament composition and recovery (LAUNCH)."""

from __future__ import annotations

import fcntl
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from whiskeyjack_bot.approval import approve
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.forecast.record import record_sha256
from whiskeyjack_bot.forecast.replay import replay_forecast
from whiskeyjack_bot.forecast.store import read_forecast_record
from whiskeyjack_bot.lifecycle import current_status
from whiskeyjack_bot.metaculus.client import SingleAttemptPoster, build_client
from whiskeyjack_bot.metaculus.snapshots import save_snapshot
from whiskeyjack_bot.pipeline_live import _attempt_question, _build_clients
from whiskeyjack_bot.prompt import load_prompt
from whiskeyjack_bot.questions.normalize import normalize_questions
from whiskeyjack_bot.submission_live import (
    MetaculusSubmissionGateway,
    classify_refetch,
    expected_option_labels,
    expected_values,
    plan_from_payload,
    post_approved_forecast,
    require_live_submission_enabled,
)
from whiskeyjack_bot.submission_payload import authorized_payload
from whiskeyjack_bot.timeouts import phase_timeout
from whiskeyjack_bot.tournament_state import (
    ActivationInactive,
    Budget,
    StorageFailure,
    TournamentError,
    append,
    budget_context,
    events,
    require_activation,
    require_spending_clear,
    spending,
    utcnow,
    witness,
)


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
    plan = plan_from_payload(intent["payload"], expected_cdf_points=201)
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
    return True


def comment_text(conn: sqlite3.Connection, record_id: str) -> str:
    record = read_forecast_record(conn, record_id)
    forecast = record.forecast.model_dump(mode="json")
    # These are the public concise response fields requested by the forecaster schema.
    forecast.pop("as_of_utc", None)
    return (
        f"Whiskeyjack forecast — subquestion {record.question_id}: {record.question.title}\n\n"
        f"Concise forecast and rationale:\n{json.dumps(forecast, ensure_ascii=False, indent=2)}\n\n"
        + "Sources:\n"
        + "\n".join(s.canonical_url for s in record.sources)
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
    data["unresolved"] += data["restored_question_holds"]
    return data


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
        config.model.name != "openrouter/openai/gpt-5.6-sol"
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
    with worker_lock(config.storage.sqlite_path.with_suffix(".worker.lock")):
        client = build_client(config) if client is None else client
        poster = SingleAttemptPoster(client) if poster is None else poster
        account = poster.get_current_user_id()
        project = str(config.metaculus.tournament.id)
        activation = require_activation(conn, config, account_id=account, project_id=project)
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
            return status(conn, config)
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
        prompt = load_prompt(config.forecast.prompt_path, config.forecast.prompt_version)
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
            try:
                require_activation(conn, config, account_id=account, project_id=project)
                if existing:
                    record_id = str(existing[0])
                else:
                    require_spending_clear(conn, budget.scope)
                    # Persist a cutoff for restart recovery; generation requests stay byte-identical.
                    scope = f"{project}:{question.question_id}"
                    checkpoints = events(conn, "question_started", scope)
                    from whiskeyjack_bot.tournament_state import digest

                    fingerprint = digest(question.model_dump(mode="json"))
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
                            {"at": now.isoformat(), "fingerprint": fingerprint},
                        )
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
                        )
                    if (
                        outcome.status == "not_recorded"
                        or outcome.note
                        or outcome.artifact_outcome == "failed"
                    ):
                        raise StorageFailure("forecast or evidence storage failed; worker stopped")
                    if outcome.status != "recorded" or outcome.record_id is None:
                        raise TournamentError("question research or generation failed")
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
                    {"error_type": type(exc).__name__, "at": utcnow().isoformat()},
                )
            heartbeat["at"] = utcnow().isoformat()
            append(conn, "heartbeat", "worker", heartbeat)
        heartbeat["complete"] = True
        heartbeat["at"] = utcnow().isoformat()
        append(conn, "heartbeat", "worker", heartbeat)
        return status(conn, config)
