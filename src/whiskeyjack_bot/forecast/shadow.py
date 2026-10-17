"""The shadow experiment arm: a never-submittable forecast from baseline plus lens evidence (M1-359).

D53's Phase 2. In the same worker pass as the forecast the worker submits (the **control**),
after the local-source lens has run, this generates a second forecast for the same question at
the same cutoff, with the same model and settings, from the control's baseline research plus
the lens's runs, under the lens-aware prompt (``prompts/forecaster-tournament-lens.md``). The
paired report (M5-805) scores the two against each other.

**It can never be approved, reserved or posted, and that is structural.** Every path that can
do any of those starts from a ``forecast_records`` row: ``approval.approve`` and
``submission.submission_key_for_record`` read one, and ``approval_events``,
``lifecycle_events``, ``submission_key_reservations``, ``submission_attempts`` and
``submission_reconciliations`` each have a foreign key to one. A shadow forecast is written to
``shadow_forecasts`` (migration 020) and nowhere else, and 020's
``forecast_records_refuse_shadow_id`` stops a shadow id ever becoming a forecast record. This
module imports nothing that approves or posts (``tests/unit/test_shadow_forecast.py`` measures
that out of process).

**The control is never touched.** The worker calls :func:`run_shadow_forecast` only after the
control is recorded, approved and posted, outside the question's ``try``, under its own time
bound, and catches every exception it raises (``tournament._collect_shadow_forecast``). Nothing
here writes to a table the control's lifecycle reads.

**Its model call is priced like the control's.** ``generate_forecast`` goes through the same
``PricedClient`` the worker built, inside the active ``Budget``'s context, so the call
reserves and settles under the activation's and the series' ceilings like any other.

**What it is shown.** The combined packet is the baseline runs the control's research
checkpoint names plus the lens runs that completed, through the control's own freshness filter
(``research.quality.usable_packet``), with each document's locale row rendered
(``inputs.build_model_input(include_locales=True)``). A lens checkpoint that retained no
document that survives that filter buys nothing: the model would see the control's evidence
under a different prompt, which measures the prompt, not the lens.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.forecast.generate import Forecaster, generate_forecast
from whiskeyjack_bot.forecast.parse import ForecastGeneration
from whiskeyjack_bot.forecast.persist import write_generation_artifact
from whiskeyjack_bot.forecast.record import (
    ForecastRecord,
    ForecastRecordError,
    assign_identity,
    build_forecast_record_draft,
    canonical_record_json,
    record_from_json,
    record_sha256,
)
from whiskeyjack_bot.forecast.schema import ForecastSchemaError
from whiskeyjack_bot.forecast.store import mint_record_id
from whiskeyjack_bot.forecast.validate import validate_output
from whiskeyjack_bot.lifecycle import LifecycleError, transaction
from whiskeyjack_bot.prompt import load_prompt
from whiskeyjack_bot.questions.model import CanonicalQuestion
from whiskeyjack_bot.research.packet import (
    PACKET_LOCALES_SCHEMA_VERSION,
    PACKET_SCHEMA_VERSION,
    ResearchPacket,
    packet_sha256,
)
from whiskeyjack_bot.research.quality import usable_packet
from whiskeyjack_bot.research.store import load_packet

# The journal event every attempt appends, whatever happened, scoped like the lens's
# checkpoint: by the question fingerprint.
SHADOW_CHECKPOINT_KIND = "shadow_forecast_checkpoint"

# What a shadow attempt came to. Closed, and the only values the checkpoint's ``outcome`` takes.
ShadowOutcome = Literal["written", "no_lens_evidence", "generation_failed"]


class ShadowForecastError(Exception):
    """A shadow forecast could not be built or stored.

    The module's own error, for the project rule that callers handle one type. Messages are
    constants: none echoes a document, a model reply or a stored value.
    """


@dataclass(frozen=True)
class ShadowResult:
    """What one shadow attempt did, for the worker's checkpoint."""

    outcome: ShadowOutcome
    shadow_forecast_id: str | None = None
    retrieval_run_ids: tuple[str, ...] = ()
    lens_documents: int = 0
    failure_code: str | None = None
    cost_usd: float | None = None
    model_invocations: int = 0


@dataclass(frozen=True)
class StoredShadowForecast:
    """One ``shadow_forecasts`` row, read back."""

    shadow_forecast_id: str
    control_record_id: str
    question_id: int
    tournament_id: str
    local_lens_checkpoint_id: str
    research_packet_sha256: str
    packet_schema_version: str
    retrieval_run_ids: tuple[str, ...]
    prompt_version: str
    prompt_sha256: str
    record: ForecastRecord
    record_sha256: str
    raw_output_path: str | None
    cost_usd: float | None
    model_invocations: int


def lens_run_ids(lens_checkpoint: dict[str, Any]) -> tuple[str, ...]:
    """The lens runs a shadow may read: those that completed and retained a document.

    ``LensOutcome.retrieval_run_ids`` includes an ``interrupted`` run, whose row was opened but
    may never have completed, so the checkpoint's ``calls`` are read instead. Malformed
    entries are skipped rather than trusted: this is a value read back out of the journal.
    """
    found: list[str] = []
    calls = lens_checkpoint.get("calls")
    for call in calls if type(calls) is list else []:
        if type(call) is not dict:
            continue
        run_id, documents = call.get("retrieval_run_id"), call.get("documents")
        if (
            type(run_id) is str
            and run_id
            and type(documents) is int
            and documents > 0
            and call.get("interrupted") is False
            # A reason string when the planned call was not made; None when it was.
            and call.get("skipped") is None
            and run_id not in found
        ):
            found.append(run_id)
    return tuple(found)


def _baseline_run_ids(lens_checkpoint: dict[str, Any]) -> tuple[str, ...]:
    runs = lens_checkpoint.get("baseline_run_ids")
    if type(runs) is not list or not runs or any(type(run) is not str for run in runs):
        raise ShadowForecastError("the lens checkpoint names no baseline runs")
    return tuple(runs)


def run_shadow_forecast(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    question: CanonicalQuestion,
    tournament_id: str,
    now: datetime,
    control_record_id: str,
    lens_checkpoint_id: str,
    lens_checkpoint: dict[str, Any],
    forecaster: Forecaster,
) -> ShadowResult:
    """Generate and store the shadow forecast paired with ``control_record_id``.

    Raises :class:`ShadowForecastError` for anything that stops it before or after the model
    call; ``generate_forecast``'s own refusals and the priced client's ``ModelOutcomeUnknown``
    propagate unchanged, and the worker catches every one of them. A generation that
    produced no forecast is not a raise: its reply is filed as an artifact (it was paid for)
    and the result says ``generation_failed``.
    """
    shadow = config.retrieval.local_lens.shadow
    baseline = _baseline_run_ids(lens_checkpoint)
    lens = tuple(run for run in lens_run_ids(lens_checkpoint) if run not in baseline)
    if not lens:
        return ShadowResult("no_lens_evidence")
    runs = (*baseline, *lens)
    try:
        combined = load_packet(conn, question_id=question.question_id, retrieval_run_ids=runs)
    except Exception:
        # research.store's StoreError, or a sqlite3 error under it: either way the stored
        # runs cannot be read back, and the message could name a run.
        raise ShadowForecastError("the combined research packet could not be loaded") from None
    packet = usable_packet(combined, question, now, config.retrieval.freshness_days_default)
    lens_documents = sum(1 for document in packet.documents if document.retrieval_run_id in lens)
    if lens_documents == 0:
        return ShadowResult("no_lens_evidence", retrieval_run_ids=runs)

    try:
        prompt = load_prompt(
            shadow.prompt_path,
            shadow.prompt_version,
            min_probability=config.forecast.min_probability,
            max_probability=config.forecast.max_probability,
        )
    except Exception:
        raise ShadowForecastError("the shadow prompt could not be loaded") from None
    # The shadow's prompt pair, for the shadow call only. `model_copy` skips validation, which
    # is safe here: both fields were validated where they were read, on
    # `ShadowForecastConfig`, and `generate_forecast` re-checks the version pair itself.
    shadow_config = config.model_copy(
        update={
            "forecast": config.forecast.model_copy(
                update={"prompt_path": shadow.prompt_path, "prompt_version": shadow.prompt_version}
            )
        }
    )
    generation = generate_forecast(
        config=shadow_config,
        question=question,
        packet=packet,
        prompt=prompt,
        tournament_id=tournament_id,
        now=now,
        client=forecaster,
        render_locales=True,
    )
    shadow_forecast_id = mint_record_id()
    raw_output_path = write_generation_artifact(
        config,
        attempt_id=shadow_forecast_id,
        question_id=question.question_id,
        generation=generation,
        written_at=now,
    )
    if generation.forecast is None:
        return ShadowResult(
            "generation_failed",
            retrieval_run_ids=runs,
            lens_documents=lens_documents,
            failure_code=generation.failure_code or "internal_error",
            cost_usd=generation.cost_usd,
            model_invocations=generation.invocations,
        )
    record = _shadow_record(
        generation,
        question=question,
        tournament_id=tournament_id,
        shadow_forecast_id=shadow_forecast_id,
        retrieval_run_id=runs[0],
        packet=packet,
        now=now,
        config=shadow_config,
    )
    append_shadow_forecast(
        conn,
        record=record,
        control_record_id=control_record_id,
        local_lens_checkpoint_id=lens_checkpoint_id,
        packet=packet,
        retrieval_run_ids=runs,
        raw_output_path=raw_output_path,
        cost_usd=generation.cost_usd,
        model_invocations=generation.invocations,
    )
    return ShadowResult(
        "written",
        shadow_forecast_id=shadow_forecast_id,
        retrieval_run_ids=runs,
        lens_documents=lens_documents,
        cost_usd=generation.cost_usd,
        model_invocations=generation.invocations,
    )


def _shadow_record(
    generation: ForecastGeneration,
    *,
    question: CanonicalQuestion,
    tournament_id: str,
    shadow_forecast_id: str,
    retrieval_run_id: str,
    packet: ResearchPacket,
    now: datetime,
    config: AppConfig,
) -> ForecastRecord:
    """The shadow forecast in the control's canonical record form.

    Built by the control's own pure builders, so one parser reads both arms, and checked by
    the control's own output gate (M1-506) -- a shadow is held to the same validity rule as
    the forecast it is compared with. Version 1 and no parent: a shadow is not in a chain.
    """
    try:
        draft = build_forecast_record_draft(
            question=question,
            generation=generation,
            tournament_id=tournament_id,
            attempt_id=shadow_forecast_id,
            retrieval_run_id=retrieval_run_id,
            research_packet_sha256=packet_sha256(packet),
            generated_at=now,
        )
        validate_output(
            draft.forecast,
            config.forecast,
            question=draft.question,
            source_ids=[source.source_id for source in draft.sources],
        )
        return assign_identity(
            draft, record_id=shadow_forecast_id, forecast_version=1, parent_record_id=None
        )
    except (ForecastRecordError, ForecastSchemaError):
        raise ShadowForecastError(
            "the shadow forecast did not form a valid record (detail withheld: it can echo "
            "the model's reply)"
        ) from None


def append_shadow_forecast(
    conn: sqlite3.Connection,
    *,
    record: ForecastRecord,
    control_record_id: str,
    local_lens_checkpoint_id: str,
    packet: ResearchPacket,
    retrieval_run_ids: tuple[str, ...],
    raw_output_path: str | None,
    cost_usd: float | None,
    model_invocations: int,
) -> None:
    """Insert one ``shadow_forecasts`` row. 020's trigger checks the pairing it claims."""
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO shadow_forecasts (shadow_forecast_id, control_record_id, question_id, "
                "tournament_id, local_lens_checkpoint_id, research_packet_sha256, "
                "packet_schema_version, retrieval_run_ids_json, prompt_version, prompt_sha256, "
                "record_json, record_sha256, raw_output_path, cost_usd, model_invocations, "
                "created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.record_id,
                    control_record_id,
                    record.question_id,
                    record.tournament_id,
                    local_lens_checkpoint_id,
                    record.research_packet_sha256,
                    PACKET_LOCALES_SCHEMA_VERSION if packet.locales else PACKET_SCHEMA_VERSION,
                    json.dumps(list(retrieval_run_ids)),
                    record.model_settings.prompt_version,
                    record.model_settings.prompt_sha256,
                    canonical_record_json(record),
                    record_sha256(record),
                    raw_output_path,
                    cost_usd,
                    model_invocations,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except (sqlite3.Error, ForecastRecordError, LifecycleError):
        raise ShadowForecastError("the shadow forecast row was refused by the ledger") from None


def read_shadow_forecasts(
    conn: sqlite3.Connection, *, control_record_id: str | None = None
) -> tuple[StoredShadowForecast, ...]:
    """Every shadow row, or those paired with one control, oldest first."""
    query = (
        "SELECT shadow_forecast_id, control_record_id, question_id, tournament_id, "
        "local_lens_checkpoint_id, research_packet_sha256, packet_schema_version, "
        "retrieval_run_ids_json, prompt_version, prompt_sha256, record_json, record_sha256, "
        "raw_output_path, cost_usd, model_invocations FROM shadow_forecasts"
    )
    arguments: tuple[str, ...] = ()
    if control_record_id is not None:
        query += " WHERE control_record_id = ?"
        arguments = (control_record_id,)
    try:
        rows = conn.execute(query + " ORDER BY created_at_utc, shadow_forecast_id", arguments)
        return tuple(_stored(row) for row in rows.fetchall())
    except sqlite3.Error:
        raise ShadowForecastError("the shadow forecasts could not be read") from None


def _stored(row: tuple[Any, ...]) -> StoredShadowForecast:
    try:
        runs = json.loads(row[7])
        if type(runs) is not list or any(type(run) is not str for run in runs):
            raise ValueError
        return StoredShadowForecast(
            shadow_forecast_id=row[0],
            control_record_id=row[1],
            question_id=row[2],
            tournament_id=row[3],
            local_lens_checkpoint_id=row[4],
            research_packet_sha256=row[5],
            packet_schema_version=row[6],
            retrieval_run_ids=tuple(runs),
            prompt_version=row[8],
            prompt_sha256=row[9],
            record=record_from_json(row[10]),
            record_sha256=row[11],
            raw_output_path=row[12],
            cost_usd=row[13],
            model_invocations=row[14],
        )
    except (ValueError, TypeError, ForecastRecordError):
        raise ShadowForecastError("a stored shadow forecast could not be read back") from None
