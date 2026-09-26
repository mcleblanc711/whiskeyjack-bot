"""Activation and replay checks at the live submission boundary (LAUNCH)."""

from __future__ import annotations
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.forecast.record import ForecastRecord, ForecastRecordError
from whiskeyjack_bot.questions.model import CanonicalQuestion
from whiskeyjack_bot.submission_live import (
    ForecastHistory,
    LiveSubmissionError,
    MetaculusPoster,
    _utcnow,
)


# Platform metadata unrelated to the resolution contract: never compared at all.
_IGNORED_METADATA = frozenset({"question_weight", "source_categories", "tournament_slugs"})
# Membership sets compared as multisets (M1-340). Each is classified unordered on the evidence
# questions/canonical.py records (M1-331): group-sibling ids are never indexed against anything,
# and options are matched by label, never position, in forecast/multiple_choice.py and
# submission_payload.py. Sorted rather than ignored, so a changed membership still differs --
# a label renamed, an option added or dropped, a sibling replaced or its count changed.
_UNORDERED_MEMBERSHIP = ("question_ids_of_group", "options")


def resolution_inputs(question: CanonicalQuestion) -> dict[str, Any]:
    """The parts of ``question`` a pre-post refetch must reproduce exactly (M1-340).

    ``model_dump`` in python mode, as the comparison always was, with the ignored metadata
    excluded and each unordered membership list sorted, so the API returning the same
    members in a different order between the record's fetch and the refetch is not read as
    "question resolution inputs changed". Every other field compares exactly as before.
    """
    data = question.model_dump(exclude=set(_IGNORED_METADATA))
    for name in _UNORDERED_MEMBERSHIP:
        members = data.get(name)
        if members is not None:
            data[name] = sorted(members)
    return data


def require_research_artifacts(
    conn: sqlite3.Connection, config: AppConfig, record: ForecastRecord
) -> None:
    from whiskeyjack_bot.research.store import load_packet
    from whiskeyjack_bot.research.packet import packet_sha256
    from whiskeyjack_bot.research.quality import usable_packet, without_future
    from whiskeyjack_bot.research.artifacts import ArtifactError, read_raw_responses
    from whiskeyjack_bot.tournament_state import events, question_fingerprint, StorageFailure

    checkpoints = events(conn, "research_checkpoint", question_fingerprint(record.question))
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
        filtered = usable_packet(
            packet,
            record.question,
            record.generated_at_utc,
            config.retrieval.freshness_days_default,
        )
        if packet_sha256(filtered) == record.research_packet_sha256:
            matched = packet
            break
        # Retain both older reconstruction paths; saved records and hashes are immutable.
        filtered = without_future(packet, record.generated_at_utc)
        if packet_sha256(filtered) == record.research_packet_sha256:
            matched = packet
            break
    if matched is None:
        raise StorageFailure("research packet cannot reproduce its saved hash")
    for run in matched.runs:
        if not run.raw_response_path:
            raise StorageFailure("research replay artifact missing")
        try:
            read_raw_responses(config.storage.artifact_root, run.raw_response_path)
        except ArtifactError:
            raise StorageFailure("research replay artifact missing or invalid") from None


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
            if resolution_inputs(current) != resolution_inputs(record.question):
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
