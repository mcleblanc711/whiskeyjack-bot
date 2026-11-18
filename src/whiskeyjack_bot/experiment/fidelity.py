"""The fidelity gate and the leakage guard of a replay arm (M5-806, AC3 and AC6; D56, D62).

**Fidelity.** Before an arm spends anything on a control record, this module rebuilds that
record's request from the stored research runs and compares it with what the live call stored. A
record that does not rebuild byte for byte is excluded under a reason code. That is what makes a
replay a replay: the arm's model is shown the evidence the live model was shown, and nothing
else differs except the arm's one controlled change.

The rebuild is D56's recipe, the one ``submission_policy.require_research_artifacts`` executes
before an approval, reimplemented here because that module is on the posting path and AC7 forbids
the import:

1. the full run ids come from the ``research_checkpoint`` journal event whose first run is the
   record's ``retrieval_run_id`` (the record stores only the first);
2. ``load_packet`` over those runs, then the worker's freshness filter
   (``research.quality.usable_packet``) at the record's own instant, must reproduce the record's
   ``research_packet_sha256``. Only that rule is accepted: it has been the worker's since before
   the first tournament record, and the two older reconstruction paths ``submission_policy``
   still tries exist for records this population does not contain;
3. ``build_model_input`` at the record's ``as_of`` and ``render_model_input``, redacted the way
   the artifact writer redacts, must equal the raw-output artifact's ``request`` byte for byte;
4. **the stricter reading of "under the record's own prompt and settings"**: the full OpenRouter
   body -- the prompt bytes the record's ``prompt_sha256`` names, the rebuilt user message, and
   ``forecast.priced.build_request`` at some member of the effort vocabulary -- must digest to a
   ``model_started`` scope in the live journal under the record's own project. The effort that
   matches is the control's effort, measured rather than assumed (every record before M1-367 ran
   at medium). The prompt bytes come from the configured prompt directory and its ``archive/``,
   which holds byte-identical copies of the tournament prompts the live records were made under.

Measured on the live ledger on 2026-11-17: all 291 non-test records pass all four.

**Leakage.** A question that opened before the arm model's registered in-service instant is
refused: the model may have been trained on its outcome, which is the one leak stored packets do
not close (D62). A question with no recorded open time is refused too, because the rule cannot
be applied to it.

Everything here reads the live ledger through the caller's read-only connection and nothing else.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, get_args

from whiskeyjack_bot.artifacts import ArtifactError
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.experiment.errors import ExperimentError
from whiskeyjack_bot.forecast.artifacts import read_raw_model_output
from whiskeyjack_bot.forecast.inputs import (
    ForecastInputError,
    ModelInput,
    build_model_input,
    render_model_input,
)
from whiskeyjack_bot.forecast.priced import PRICED_MODELS, ReasoningEffort, build_request
from whiskeyjack_bot.forecast.record import ForecastRecord, ForecastRecordError, record_sha256
from whiskeyjack_bot.forecast.store import read_forecast_record, read_model_call
from whiskeyjack_bot.questions.model import CanonicalQuestion
from whiskeyjack_bot.redaction import redact_secrets
from whiskeyjack_bot.research.packet import PacketError, ResearchPacket, packet_sha256
from whiskeyjack_bot.research.quality import usable_packet
from whiskeyjack_bot.research.store import StoreError, load_packet
from whiskeyjack_bot.tournament_state import TournamentError, digest, events, question_fingerprint

GateReason = Literal[
    "model_not_in_service",
    "open_time_unknown",
    "record_unreadable",
    "no_checkpoint",
    "checkpoint_ambiguous",
    "packet_hash_mismatch",
    "request_unbuildable",
    "artifact_unreadable",
    "artifact_mismatch",
    "request_mismatch",
    "model_unpriced",
    "prompt_unavailable",
    "call_not_reproduced",
    "effort_disagrees",
]
"""Why a control record is excluded from an arm. Closed; the report counts each."""

GATE_REASONS: Final[tuple[str, ...]] = get_args(GateReason)

# The largest prompt file indexed into the archive, in bytes; the live prompts are under 40 KB.
_MAX_PROMPT_BYTES: Final = 1_048_576


@dataclass(frozen=True)
class ControlInput:
    """A control record that passed the gate, with everything an arm needs to replay it."""

    record: ForecastRecord
    forecast_sha256: str
    question: CanonicalQuestion
    packet: ResearchPacket
    model_input: ModelInput
    request: str
    control_effort: ReasoningEffort
    retrieval_run_ids: tuple[str, ...]


@dataclass(frozen=True)
class GateVerdict:
    """One control record's verdict: a ``control`` when it passed, a ``reason`` when it did not."""

    record_id: str
    reason: GateReason | None
    control: ControlInput | None
    control_effort: ReasoningEffort | None = None


def prompt_archive(directories: Sequence[Path]) -> dict[str, str]:
    """Every ``*.md`` file directly in ``directories``, keyed by the sha256 of its bytes.

    The key is the file's identity, so the file name is irrelevant and two copies of one prompt
    are one entry. A file that cannot be read or decoded is refused, with its path: a prompt
    archive the gate cannot fully read would turn into silent ``prompt_unavailable`` exclusions.
    """
    found: dict[str, str] = {}
    for directory in directories:
        try:
            paths = sorted(path for path in directory.glob("*.md") if path.is_file())
        except OSError:
            raise ExperimentError(f"cannot list prompt directory {directory}") from None
        for path in paths:
            try:
                data = path.read_bytes()
            except OSError:
                raise ExperimentError(f"cannot read prompt {path}") from None
            if len(data) > _MAX_PROMPT_BYTES:
                raise ExperimentError(f"prompt {path} is larger than {_MAX_PROMPT_BYTES} bytes")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise ExperimentError(f"prompt {path} is not valid UTF-8") from None
            found[hashlib.sha256(data).hexdigest()] = text
    return found


def journal_index(live: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    """Every live ``model_started`` request digest, with the budget scopes it was bought under.

    A model call's cache scope is ``{budget scope}:{digest}`` and the budget scope is
    ``{account}:{project}``, so the digest is everything after the last colon. Built in one pass
    rather than queried per record by suffix, which no index serves.
    """
    index: dict[str, list[str]] = {}
    try:
        rows = live.execute(
            "SELECT scope FROM tournament_events WHERE kind = 'model_started' ORDER BY seq"
        ).fetchall()
    except sqlite3.Error:
        raise ExperimentError("cannot read the live journal") from None
    for (scope,) in rows:
        if type(scope) is not str or ":" not in scope:
            continue
        prefix, _, found = scope.rpartition(":")
        if len(found) == 64 and all(c in "0123456789abcdef" for c in found):
            index.setdefault(found, []).append(prefix)
    return {key: tuple(value) for key, value in index.items()}


def leakage_reason(question: CanonicalQuestion, in_service_from: datetime) -> GateReason | None:
    """``model_not_in_service`` or ``open_time_unknown`` for a question the arm may not see."""
    opened = question.open_time
    if opened is None:
        return "open_time_unknown"
    if opened < in_service_from:
        return "model_not_in_service"
    return None


def _checkpoints(live: sqlite3.Connection, record: ForecastRecord) -> list[tuple[str, ...]]:
    """Distinct run-id lists of the record's question whose first run is the record's run."""
    try:
        saved = events(live, "research_checkpoint", question_fingerprint(record.question))
    except TournamentError:
        raise ExperimentError("cannot read the live journal's research checkpoints") from None
    found: list[tuple[str, ...]] = []
    for checkpoint in saved:
        run_ids = checkpoint.get("run_ids") if type(checkpoint) is dict else None
        if (
            type(run_ids) is not list
            or not run_ids
            or any(type(run) is not str for run in run_ids)
            or run_ids[0] != record.retrieval_run_id
        ):
            continue
        candidate = tuple(run_ids)
        if candidate not in found:
            found.append(candidate)
    return found


def _effort_recorded(live: sqlite3.Connection, scope: str) -> str | None:
    """The effort the live ``model_response`` recorded for this call, where its schema has one."""
    try:
        started = events(live, "model_started", scope)
        reservation = started[-1].get("reservation_id") if started else None
        if type(reservation) is not str:
            return None
        for response in events(live, "model_response", reservation):
            effort = response.get("reasoning_effort") if type(response) is dict else None
            if type(effort) is str:
                return effort
    except TournamentError:
        raise ExperimentError("cannot read the live journal's model responses") from None
    return None


def check_record(
    live: sqlite3.Connection,
    record_id: str,
    *,
    config: AppConfig,
    prompts: Mapping[str, str],
    journal: Mapping[str, tuple[str, ...]],
    in_service_from: datetime,
) -> GateVerdict:
    """The leakage guard, then the four fidelity checks, for one control record."""
    try:
        record = read_forecast_record(live, record_id)
        forecast_sha256 = record_sha256(record)
        call = read_model_call(live, record_id)
    except (ForecastRecordError, sqlite3.Error):
        return GateVerdict(record_id, "record_unreadable", None)

    leak = leakage_reason(record.question, in_service_from)
    if leak is not None:
        return GateVerdict(record_id, leak, None)

    candidates = _checkpoints(live, record)
    if not candidates:
        return GateVerdict(record_id, "no_checkpoint", None)
    days = config.retrieval.freshness_days_default
    matches: list[tuple[tuple[str, ...], ResearchPacket]] = []
    for run_ids in candidates:
        try:
            packet = load_packet(live, question_id=record.question_id, retrieval_run_ids=run_ids)
            filtered = usable_packet(packet, record.question, record.generated_at_utc, days)
            if packet_sha256(filtered) == record.research_packet_sha256:
                matches.append((run_ids, filtered))
        except (StoreError, PacketError):
            continue
    if not matches:
        return GateVerdict(record_id, "packet_hash_mismatch", None)
    if len(matches) > 1:
        return GateVerdict(record_id, "checkpoint_ambiguous", None)
    run_ids, filtered = matches[0]

    try:
        model_input = build_model_input(
            question=record.question,
            packet=filtered,
            tournament_id=record.tournament_id,
            as_of=record.generated_at_utc,
        )
        request = render_model_input(model_input)
    except ForecastInputError:
        return GateVerdict(record_id, "request_unbuildable", None)

    if call.raw_output_path is None:
        return GateVerdict(record_id, "artifact_unreadable", None)
    try:
        stored = read_raw_model_output(config.storage.artifact_root, call.raw_output_path)
    except ArtifactError:
        return GateVerdict(record_id, "artifact_unreadable", None)
    if stored.attempt_id != record.attempt_id or stored.question_id != record.question_id:
        return GateVerdict(record_id, "artifact_mismatch", None)
    if redact_secrets(request, config.secret_env_var_names()) != stored.request:
        return GateVerdict(record_id, "request_mismatch", None)

    priced = PRICED_MODELS.get(record.model_settings.name)
    if priced is None:
        return GateVerdict(record_id, "model_unpriced", None)
    system = prompts.get(record.model_settings.prompt_sha256)
    if system is None:
        return GateVerdict(record_id, "prompt_unavailable", None)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": request}]
    found: list[tuple[ReasoningEffort, str]] = []
    for effort in get_args(ReasoningEffort):
        body_digest = digest(build_request(messages, priced, reasoning_effort=effort))
        for prefix in journal.get(body_digest, ()):
            if prefix.rpartition(":")[2] == record.tournament_id:
                found.append((effort, f"{prefix}:{body_digest}"))
    if len({effort for effort, _ in found}) != 1:
        return GateVerdict(record_id, "call_not_reproduced", None)
    control_effort, scope = found[0]
    recorded = _effort_recorded(live, scope)
    if recorded is not None and recorded != control_effort:
        return GateVerdict(record_id, "effort_disagrees", None, control_effort)

    return GateVerdict(
        record_id,
        None,
        ControlInput(
            record=record,
            forecast_sha256=forecast_sha256,
            question=record.question,
            packet=filtered,
            model_input=model_input,
            request=request,
            control_effort=control_effort,
            retrieval_run_ids=run_ids,
        ),
        control_effort,
    )
