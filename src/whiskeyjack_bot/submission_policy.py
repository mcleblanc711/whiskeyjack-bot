"""Activation and replay checks at the live submission boundary (LAUNCH)."""

from __future__ import annotations
import sqlite3
from collections.abc import Callable, Mapping
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.forecast.record import ForecastRecord, ForecastRecordError
from whiskeyjack_bot.submission_live import (
    ForecastHistory,
    LiveSubmissionError,
    MetaculusPoster,
    _utcnow,
)


def require_research_artifacts(
    conn: sqlite3.Connection, config: AppConfig, record: ForecastRecord
) -> None:
    from whiskeyjack_bot.research.store import load_packet
    from whiskeyjack_bot.research.packet import packet_sha256
    from whiskeyjack_bot.research.quality import without_future
    from whiskeyjack_bot.tournament_state import digest, events, StorageFailure

    checkpoints = events(
        conn, "research_checkpoint", digest(record.question.model_dump(mode="json"))
    )
    candidates = [tuple(c["run_ids"]) for c in checkpoints]
    if not candidates:
        runs = {record.retrieval_run_id}
        for source in record.sources:
            row = conn.execute(
                "SELECT retrieval_run_id FROM research_documents WHERE document_id=?",
                (source.document_id,),
            ).fetchone()
            if row:
                runs.add(row[0])
        candidates = [tuple(sorted(runs))]
    matched = None
    for run_ids in candidates:
        packet = load_packet(conn, question_id=record.question_id, retrieval_run_ids=run_ids)
        if packet_sha256(packet) == record.research_packet_sha256:
            matched = packet
            break
        filtered = without_future(packet, record.generated_at_utc)
        if packet_sha256(filtered) == record.research_packet_sha256:
            matched = packet
            break
    if matched is None:
        raise StorageFailure("research packet cannot reproduce its saved hash")
    for run in matched.runs:
        if (
            not run.raw_response_path
            or not (config.storage.artifact_root / run.raw_response_path).is_file()
        ):
            raise StorageFailure("research replay artifact missing")


def prepare_live_policy(
    conn: sqlite3.Connection,
    config: AppConfig,
    poster: MetaculusPoster,
    record: ForecastRecord,
    payload: Mapping[str, object],
    digest: str,
) -> Callable[[object, ForecastHistory], None]:
    identifier = record.record_id
    from whiskeyjack_bot.tournament_state import (
        TournamentError,
        StorageFailure,
        append,
        events,
        require_activation,
        witness,
    )
    from whiskeyjack_bot.forecast.replay import replay_forecast
    from whiskeyjack_bot.questions.normalize import normalize_questions

    if events(conn, "restored_question_hold", f"{record.tournament_id}:{record.question_id}"):
        raise LiveSubmissionError("restored submission intent holds this question; no repeat post")
    prior_intent = conn.execute(
        "SELECT 1 FROM tournament_events WHERE kind='forecast_intent' "
        "AND json_extract(data,'$.question_id')=? AND json_extract(data,'$.project_id')=? LIMIT 1",
        (record.question_id, record.tournament_id),
    ).fetchone()
    if prior_intent:
        raise LiveSubmissionError(
            "this question already has a durable submission intent; reconcile without posting"
        )
    try:
        try:
            account_id = poster.get_current_user_id()
        except Exception:
            raise LiveSubmissionError("authenticated bot identity could not be verified") from None
        activation = require_activation(
            conn, config, account_id=account_id, project_id=record.tournament_id
        )
        require_research_artifacts(conn, config, record)
        replay_config = config.model_copy(
            update={
                "forecast": config.forecast.model_copy(update={"replay_saved_model_output": True})
            }
        )
        if not replay_forecast(conn, replay_config, record_id=identifier).matches:
            raise LiveSubmissionError("forecast replay does not match; nothing was posted")
    except StorageFailure:
        raise
    except (TournamentError, ForecastRecordError) as exc:
        raise LiveSubmissionError(str(exc)) from None

    def before_post(question: object, baseline: ForecastHistory) -> None:
        try:
            require_activation(
                conn,
                config,
                account_id=poster.get_current_user_id(),
                project_id=record.tournament_id,
            )
            raw = getattr(question, "api_json", {})
            inner = raw.get("question", {}) if isinstance(raw, dict) else {}
            if record.question_type == "numeric":
                scaling = inner.get("scaling", {})
                if (
                    not isinstance(scaling, dict)
                    or not {"range_min", "range_max", "zero_point", "inbound_outcome_count"}
                    <= scaling.keys()
                    or any(
                        type(inner.get(k)) is not bool
                        for k in ("open_lower_bound", "open_upper_bound")
                    )
                ):
                    raise LiveSubmissionError(
                        "live numeric bounds are unreadable; nothing was posted"
                    )
            normalized = normalize_questions([question]).questions
            if len(normalized) != 1:
                raise LiveSubmissionError("live question cannot be normalized")
            current = normalized[0]
            # Ignore only platform metadata unrelated to the resolution contract.
            ignored = {"question_weight", "source_categories", "tournament_slugs"}
            if current.model_dump(exclude=ignored) != record.question.model_dump(exclude=ignored):
                raise LiveSubmissionError("question resolution inputs changed; nothing was posted")
            closing = current.close_time
            if closing is None or closing <= _utcnow():
                raise LiveSubmissionError(
                    "question deadline is missing or elapsed; nothing was posted"
                )
            api = getattr(question, "api_json", {})
            projects = api.get("projects", {})
            memberships = [
                p
                for values in projects.values()
                for p in (values if isinstance(values, list) else [values])
                if isinstance(p, dict)
            ]
            if not any(str(p.get("id")) == record.tournament_id for p in memberships):
                raise LiveSubmissionError("live question is not in the activated project")
            intent = {
                "record_id": identifier,
                "account_id": account_id,
                "project_id": record.tournament_id,
                "question_id": record.question_id,
                "post_id": record.post_id,
                "payload": payload,
                "payload_sha256": digest,
                "baseline": [],
                "activation_id": activation["activation_id"],
            }
            witness(conn, config.storage.artifact_root, identifier, intent)
            append(conn, "forecast_intent", identifier, intent)
        except StorageFailure:
            raise
        except TournamentError as exc:
            raise LiveSubmissionError(str(exc)) from None

    return before_post
