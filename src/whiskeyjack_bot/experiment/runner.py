"""Run a replay arm: select, gate, generate, persist (M5-806, AC1-AC9; D62).

One run takes an arm over its control records, record by record and within each record sample by
sample, and appends one row per (record, sample) to the experiment store. The live ledger is read
through the caller's read-only connection and is never written.

**The control is re-run, never read from the live record** (D62): live forecasts before 2026-11-11
were made at medium effort and older prompts, so an arm's control is its own identity-transform
arm under the arm's own model, effort and prompt.

**Spend.** Every model call is reserved before it is made, through the priced client, against the
arm's ceiling. Each (arm, record, sample) has its own budget scope -- the priced client's replay
cache is keyed by scope and request digest, so two samples of one request under one scope would
be one purchase served twice -- and every scope of the arm reserves under one series whose ceiling
is the command-line ceiling. The ceiling is therefore cumulative across every run of the arm
(owner decision, 2026-11-17). Billed calls and cost are read back out of the store's journal,
never from the generation's own counters, which include replayed cache hits (M1-315's lesson).

**Resuming.** A rerun skips every (record, sample) that has a row and buys nothing for it. A
sample whose call was bought but whose row was never written -- a crash between the two, or a
ceiling reached at the repair turn -- is replayed from the journal's cache for free on the next
run, which then buys only what is still missing. A call whose outcome is unknown is recorded as
such and never bought again; the priced client refuses to.

**Where the store may live.** ``Budget.reserve`` writes a witness under the budget root and under
``<store directory>/.posting-guard``. The live worker's ``check_storage`` stops it on any file in
*its* guard directory that its own journal lacks, so a store beside the live ledger would stop the
worker on the next poll. A store in the live ledger's directory is refused, and so is a store
whose files directory is the live artifact root.

**Never submittable** (AC7). Nothing here imports approval, submission or the posting path, and
nothing writes to the live ledger: ``tests/unit/test_experiment_harness.py`` measures the import
graph out of process.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal
from uuid import uuid4

from whiskeyjack_bot.artifacts import ArtifactError
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.credentials import MissingCredentialError
from whiskeyjack_bot.experiment.arm_store import (
    ArmSample,
    SampleOutcome,
    append_arm_sample,
    begin_run,
    bind_arm,
    canonical_json,
    completed_samples,
    finish_run,
    open_store,
    record_fidelity,
    store_lock,
)
from whiskeyjack_bot.experiment.errors import ExperimentError
from whiskeyjack_bot.experiment.fidelity import (
    ControlInput,
    GateVerdict,
    check_record,
    journal_index,
    prompt_archive,
)
from whiskeyjack_bot.experiment.spec import (
    MAX_EXPERIMENT_CEILING_USD,
    LoadedArmSpec,
    load_arm_prompt,
)
from whiskeyjack_bot.experiment.transforms import (
    NO_LABELLERS,
    Labellers,
    apply_transform,
    arm_documents,
    kept_packet,
    require_labellers,
    transformed_model_input,
)
from whiskeyjack_bot.forecast.artifacts import write_raw_model_output
from whiskeyjack_bot.forecast.generate import (
    Forecaster,
    ForecastGenerationError,
    generate_forecast,
)
from whiskeyjack_bot.forecast.inputs import ForecastInputError, ModelInput, render_model_input
from whiskeyjack_bot.forecast.parse import ForecastGeneration
from whiskeyjack_bot.forecast.priced import (
    MAX_OUTPUT_TOKENS,
    MODEL_IN_SERVICE,
    PricedClient,
)
from whiskeyjack_bot.forecast.record import (
    ForecastRecord,
    ForecastRecordError,
    assign_identity,
    build_forecast_record_draft,
)
from whiskeyjack_bot.forecast.store import mint_record_id
from whiskeyjack_bot.ledger import LEDGER_SCHEMA_VERSION
from whiskeyjack_bot.prompt import LoadedPrompt
from whiskeyjack_bot.report import ReportError, classify, read_facts
from whiskeyjack_bot.research.packet import packet_sha256
from whiskeyjack_bot.tournament_state import (
    Budget,
    BudgetExhausted,
    ModelOutcomeUnknown,
    TournamentError,
    budget_context,
    events,
    series_spending,
    spending,
)

# The population an arm draws from (SOURCE-ALPHA "Data"): the attribution report's own rule --
# the latest posted version of each question outside the test tournaments -- restricted to a
# question whose latest resolution is a real one.
_POPULATION_STATES: Final = frozenset({"scored", "resolved_unscored"})

# The priced call's fixed timeout, as `tournament.run_once` requires it.
_PRICED_TIMEOUT_SECONDS: Final = 120

StopReason = Literal["complete", "ceiling", "journal_refused", "dry_run"]


@dataclass(frozen=True)
class PreparedArm:
    """An arm ready to run: its spec, prompt, generation config and settings binding."""

    loaded: LoadedArmSpec
    prompt: LoadedPrompt
    # The arm's generation config: the operator's, with the arm's model and prompt, and storage
    # pointed at the experiment store's files so nothing it might write lands in the live tree.
    config: AppConfig
    # The operator's config as loaded. Everything that *reads* live state -- the fidelity gate's
    # artifact root, the store-location guard -- uses this one, never ``config``.
    live_config: AppConfig
    settings_sha256: str
    labellers: Labellers


@dataclass(frozen=True)
class RunReport:
    """What one run did, reported to the operator and persisted beside the run."""

    arm_sha256: str
    run_id: str | None
    stop_reason: StopReason
    selected: int
    passed_gate: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    skipped_existing: int = 0
    not_applicable: int = 0
    question_refused: int = 0
    forecasts: int = 0
    generation_failed: int = 0
    outcome_unknown: int = 0
    arm_spent_microusd: int = 0
    arm_held_microusd: int = 0

    def counts(self) -> dict[str, object]:
        return {
            "selected": self.selected,
            "passed_gate": self.passed_gate,
            "excluded": dict(sorted(self.excluded.items())),
            "skipped_existing": self.skipped_existing,
            "not_applicable": self.not_applicable,
            "question_refused": self.question_refused,
            "forecasts": self.forecasts,
            "generation_failed": self.generation_failed,
            "outcome_unknown": self.outcome_unknown,
            "arm_spent_microusd": self.arm_spent_microusd,
            "arm_held_microusd": self.arm_held_microusd,
        }


def store_files_dir(store_path: Path) -> Path:
    """The store's own directory for raw artifacts and spend witnesses: ``<store>-files``."""
    return store_path.with_name(store_path.stem + "-files")


def require_store_location(store_path: Path, live_path: Path, config: AppConfig) -> None:
    """Refuse a store whose witnesses would land where the live worker checks its own."""
    try:
        store = store_path.resolve()
        live = live_path.resolve()
        live_artifacts = config.storage.artifact_root.resolve()
        files = store_files_dir(store).resolve()
    except (OSError, RuntimeError, ValueError):
        raise ExperimentError(f"cannot resolve experiment store path {store_path}") from None
    if store == live or store.parent == live.parent:
        raise ExperimentError(
            f"experiment store {store_path} must not be in the live ledger's directory: its "
            "spend witnesses would land in the live posting guard and stop the worker"
        )
    if files == live_artifacts or store.parent == live_artifacts:
        raise ExperimentError(
            f"experiment store {store_path} must not write into the live artifact root"
        )


def settings_sha256(config: AppConfig) -> str:
    """The generation settings an arm binds to, beyond its spec.

    Everything ``generate_forecast`` reads from configuration except what the spec already names
    (model name, prompt) and what is not a setting (the credential variable's name): the model
    call's bounds, the forecast bounds and output rules, the numeric calibration, and the
    freshness window the fidelity gate rebuilds packets with.
    """
    model = config.model.model_dump(mode="json", exclude={"name", "api_key_env"})
    forecast = config.forecast.model_dump(mode="json", exclude={"prompt_path", "prompt_version"})
    payload = {
        "model": model,
        "forecast": forecast,
        "numeric_calibration": config.numeric_calibration.model_dump(mode="json"),
        "freshness_days_default": config.retrieval.freshness_days_default,
    }
    return hashlib.sha256(canonical_json(payload).encode("ascii")).hexdigest()


def prepare_arm(
    loaded: LoadedArmSpec,
    config: AppConfig,
    *,
    files_root: Path | None,
    labellers: Labellers = NO_LABELLERS,
) -> PreparedArm:
    """Check everything that can be checked before a record is touched, and build the config.

    The model preconditions are the ones ``tournament.run_once`` enforces for a priced model,
    repeated here because the harness builds its client directly rather than through
    ``build_forecaster_client``: a temperature, a token bound other than the one the request
    sends, a timeout or a try count the priced call does not honour would each make the stored
    settings claim something the call did not do.
    """
    require_labellers(loaded.spec.transform, labellers)
    prompt = load_arm_prompt(loaded, config)
    model = config.model
    if (
        model.temperature is not None
        or model.max_output_tokens != MAX_OUTPUT_TOKENS
        or model.timeout_seconds != _PRICED_TIMEOUT_SECONDS
        or model.allowed_tries > 2
    ):
        raise ExperimentError(
            "the configured model settings are not the priced call's "
            "(temperature null, max_output_tokens 6000, timeout 120, at most 2 tries)"
        )
    if model.provider != loaded.spec.model.split("/", 1)[0]:
        raise ExperimentError("the configured model provider does not match the arm's model")
    forecast = config.forecast.model_copy(
        update={"prompt_path": loaded.prompt_path, "prompt_version": loaded.spec.prompt.version}
    )
    updates: dict[str, object] = {
        "model": model.model_copy(update={"name": loaded.spec.model}),
        "forecast": forecast,
    }
    if files_root is not None:
        updates["storage"] = config.storage.model_copy(
            update={
                "artifact_root": files_root,
                "sqlite_path": files_root / "never-a-ledger.sqlite3",
            }
        )
    arm_config = config.model_copy(update=updates)
    return PreparedArm(
        loaded=loaded,
        prompt=prompt,
        config=arm_config,
        live_config=config,
        settings_sha256=settings_sha256(config),
        labellers=labellers,
    )


def build_arm_client(prepared: PreparedArm) -> PricedClient:
    """The priced client for the arm, after the credential check ``build_forecaster_client`` does.

    The check must come first: inside ``PricedClient.invoke`` a missing key surfaces only after
    the reservation and ``model_started`` are written, which leaves that sample's scope refusing
    every later purchase as an unknown outcome.
    """
    name = prepared.config.model.api_key_env
    if not os.environ.get(name):
        raise MissingCredentialError(name)
    try:
        return PricedClient(prepared.config, reasoning_effort=prepared.loaded.spec.reasoning_effort)
    except TournamentError:
        raise ExperimentError("the arm's priced client could not be built") from None


def population(live: sqlite3.Connection) -> tuple[str, ...]:
    """The control records an arm may draw from, in ``record_id`` (UUIDv7, time) order."""
    try:
        ids = [
            str(row[0])
            for row in live.execute(
                "SELECT record_id FROM forecast_records ORDER BY record_id"
            ).fetchall()
        ]
        rows = classify([read_facts(live, record_id) for record_id in ids])
    except (ReportError, sqlite3.Error):
        raise ExperimentError("cannot read the live ledger's population") from None
    return tuple(
        row.facts.record_id
        for row in rows
        if row.exclusion is None and row.state in _POPULATION_STATES
    )


def select(live: sqlite3.Connection, loaded: LoadedArmSpec) -> tuple[str, ...]:
    """The arm's records: the population filtered by the spec's selection, in record order."""
    selection = loaded.spec.selection
    chosen: list[str] = []
    try:
        for record_id in population(live):
            facts = read_facts(live, record_id)
            if selection.question_types is not None and (
                facts.question_type not in selection.question_types
            ):
                continue
            if (
                selection.tournaments is not None
                and facts.tournament_id not in selection.tournaments
            ):
                continue
            if selection.record_ids is not None and record_id not in selection.record_ids:
                continue
            chosen.append(record_id)
            if selection.limit is not None and len(chosen) >= selection.limit:
                break
    except (ReportError, sqlite3.Error):
        raise ExperimentError("cannot read the live ledger's population") from None
    return tuple(chosen)


def _gate(
    live: sqlite3.Connection,
    records: Sequence[str],
    prepared: PreparedArm,
    prompt_dirs: Sequence[Path],
) -> list[GateVerdict]:
    prompts = prompt_archive(prompt_dirs)
    journal = journal_index(live)
    in_service = MODEL_IN_SERVICE[prepared.loaded.spec.model]
    return [
        check_record(
            live,
            record_id,
            config=prepared.live_config,
            prompts=prompts,
            journal=journal,
            in_service_from=in_service,
        )
        for record_id in records
    ]


def check_arm(
    *,
    prepared: PreparedArm,
    live: sqlite3.Connection,
    prompt_dirs: Sequence[Path],
) -> RunReport:
    """Selection and the fidelity gate only: opens no store, builds no client, spends nothing."""
    records = select(live, prepared.loaded)
    verdicts = _gate(live, records, prepared, prompt_dirs)
    excluded: Counter[str] = Counter(
        str(verdict.reason) for verdict in verdicts if verdict.reason is not None
    )
    return RunReport(
        arm_sha256=prepared.loaded.sha256,
        run_id=None,
        stop_reason="dry_run",
        selected=len(records),
        passed_gate=sum(1 for verdict in verdicts if verdict.control is not None),
        excluded=dict(excluded),
    )


def _after_as_of(model_input: ModelInput, as_of: datetime) -> bool:
    """Whether any document the model would be shown is dated after the control's cutoff."""
    for document in model_input.packet.research_documents:
        for moment in (document.published_at, document.updated_at, document.retrieved_at):
            if moment is not None and moment > as_of:
                return True
    return False


def _spend(
    store: sqlite3.Connection, scope: str
) -> tuple[int, int, int, tuple[str, ...], str | None]:
    """``(billed calls, actual, held, reported models, effort sent)`` for one sample's scope."""
    try:
        reserved = events(store, "cost_reserved", scope)
        actual, held = spending(store, scope)
        models: list[str] = []
        effort: str | None = None
        for reservation in reserved:
            identifier = reservation.get("reservation_id")
            if type(identifier) is not str:
                raise ExperimentError("a stored reservation has no id")
            for response in events(store, "model_response", identifier):
                model = response.get("model")
                if type(model) is str and model not in models:
                    models.append(model)
                sent = response.get("reasoning_effort")
                if type(sent) is str:
                    effort = sent
    except TournamentError:
        raise ExperimentError("cannot read the experiment store's spend journal") from None
    return len(reserved), actual, held, tuple(models), effort


def _fixed(chosen: ModelInput) -> Callable[[ModelInput], ModelInput]:
    """A transform that returns the already-built arm input; ``generate_forecast`` re-checks it
    against the base it built itself, which also confirms the gate's base and its are one."""

    def transform(_base: ModelInput) -> ModelInput:
        return chosen

    return transform


def _record(
    control: ControlInput, generation: ForecastGeneration, attempt_id: str
) -> ForecastRecord | None:
    record = control.record
    try:
        draft = build_forecast_record_draft(
            question=control.question,
            generation=generation,
            tournament_id=record.tournament_id,
            attempt_id=attempt_id,
            retrieval_run_id=record.retrieval_run_id,
            research_packet_sha256=record.research_packet_sha256,
            generated_at=record.generated_at_utc,
        )
        return assign_identity(
            draft, record_id=mint_record_id(), forecast_version=1, parent_record_id=None
        )
    except ForecastRecordError:
        return None


def run_arm(
    *,
    prepared: PreparedArm,
    client: Forecaster,
    live: sqlite3.Connection,
    live_path: Path,
    store_path: Path,
    ceiling_usd: int,
    prompt_dirs: Sequence[Path],
) -> RunReport:
    """Run the arm over its records under the ceiling; returns what was done."""
    if type(ceiling_usd) is not int or not 0 < ceiling_usd <= MAX_EXPERIMENT_CEILING_USD:
        raise ExperimentError(
            f"the ceiling must be a whole number of USD from 1 to {MAX_EXPERIMENT_CEILING_USD}"
        )
    if client.model != prepared.config.model.name:
        raise ExperimentError("the client does not address the arm's model")
    require_store_location(store_path, live_path, prepared.live_config)
    ceiling = ceiling_usd * 1_000_000
    files = store_files_dir(store_path)
    spec = prepared.loaded.spec
    arm = prepared.loaded.sha256
    secrets = tuple(prepared.config.secret_env_var_names())

    with store_lock(store_path):
        store = open_store(store_path)
        try:
            records = bind_arm(
                store,
                arm_sha256=arm,
                spec_json=prepared.loaded.spec_json,
                settings_sha256=prepared.settings_sha256,
                selection=select(live, prepared.loaded),
            )
            run_id = uuid4().hex
            begin_run(
                store,
                run_id=run_id,
                arm_sha256=arm,
                live_ledger_path=live_path,
                live_schema_version=LEDGER_SCHEMA_VERSION,
                ceiling_microusd=ceiling,
            )
            verdicts = _gate(live, records, prepared, prompt_dirs)
            record_fidelity(
                store,
                run_id,
                [(v.record_id, v.reason, v.control_effort) for v in verdicts],
            )
            done = completed_samples(store, arm)
            counts: Counter[str] = Counter()
            excluded: Counter[str] = Counter(
                str(v.reason) for v in verdicts if v.reason is not None
            )
            stop: StopReason = "complete"

            for verdict in verdicts:
                control = verdict.control
                if control is None:
                    continue
                if stop != "complete":
                    break
                record = control.record
                documents = arm_documents(control.model_input, control.packet)
                for sample_index in range(spec.samples):
                    if (record.record_id, sample_index) in done:
                        counts["skipped_existing"] += 1
                        continue
                    result = apply_transform(
                        spec.transform,
                        documents,
                        question_id=record.question_id,
                        sample_index=sample_index,
                        labellers=prepared.labellers,
                    )
                    if not result.applicable:
                        counts["not_applicable"] += 1
                        continue
                    transformed = transformed_model_input(control.model_input, result.documents)
                    if _after_as_of(transformed, record.generated_at_utc):
                        excluded["document_after_as_of"] += 1
                        continue
                    try:
                        request = render_model_input(transformed)
                    except ForecastInputError:
                        counts["question_refused"] += 1
                        continue
                    scope = f"experiment:{arm}:{record.record_id}:{sample_index}"
                    budget = Budget(
                        conn=store,
                        root=files,
                        scope=scope,
                        ceiling=ceiling,
                        secret_names=secrets,
                        series_id=arm,
                        series_ceiling=ceiling,
                    )
                    attempt_id = mint_record_id()
                    generation: ForecastGeneration | None = None
                    outcome: SampleOutcome
                    try:
                        with budget_context(budget):
                            generation = generate_forecast(
                                config=prepared.config,
                                question=control.question,
                                packet=control.packet,
                                prompt=prepared.prompt,
                                tournament_id=record.tournament_id,
                                now=record.generated_at_utc,
                                client=client,
                                model_input_transform=_fixed(transformed),
                            )
                    except BudgetExhausted:
                        stop = "ceiling"
                        break
                    except ModelOutcomeUnknown:
                        outcome = "outcome_unknown"
                    except TournamentError:
                        stop = "journal_refused"
                        break
                    except (ForecastGenerationError, MissingCredentialError):
                        counts["question_refused"] += 1
                        continue

                    raw_path: str | None = None
                    arm_record: ForecastRecord | None = None
                    failure: str | None = None
                    if generation is not None:
                        try:
                            raw_path = write_raw_model_output(
                                files,
                                attempt_id=attempt_id,
                                question_id=record.question_id,
                                generation=generation,
                                written_at_utc=datetime.now(timezone.utc),
                                retain=True,
                                secret_env_var_names=secrets,
                            )
                        except ArtifactError:
                            # The persist.py rule: a call that cost money is still recorded.
                            raw_path = None
                        if generation.forecast is not None:
                            arm_record = _record(control, generation, attempt_id)
                        if arm_record is not None:
                            outcome = "forecast"
                        else:
                            outcome = "generation_failed"
                            failure = generation.failure_code or "record_invalid"
                    billed, actual, held, models, effort = _spend(store, scope)
                    append_arm_sample(
                        store,
                        live,
                        ArmSample(
                            sample_id=uuid4().hex,
                            arm_sha256=arm,
                            run_id=run_id,
                            control_record_id=record.record_id,
                            control_forecast_sha256=control.forecast_sha256,
                            sample_index=sample_index,
                            attempt_id=attempt_id,
                            outcome=outcome,
                            failure_code=failure,
                            kept_packet_sha256=packet_sha256(
                                kept_packet(control.packet, result.documents)
                            ),
                            request_sha256=hashlib.sha256(request.encode("ascii")).hexdigest(),
                            record=arm_record,
                            raw_output_path=raw_path,
                            cost_microusd=actual if held == 0 else None,
                            held_microusd=held,
                            billed_calls=billed,
                            model_invocations=None
                            if generation is None
                            else generation.invocations,
                            reported_models=models,
                            reasoning_effort_sent=effort,
                            created_at_utc=datetime.now(timezone.utc),
                        ),
                    )
                    counts[
                        {
                            "forecast": "forecasts",
                            "generation_failed": "generation_failed",
                            "outcome_unknown": "outcome_unknown",
                        }[outcome]
                    ] += 1

            try:
                spent, held_total = series_spending(store, arm)
            except TournamentError:
                raise ExperimentError("cannot read the arm's spend") from None
            report = RunReport(
                arm_sha256=arm,
                run_id=run_id,
                stop_reason=stop,
                selected=len(records),
                passed_gate=sum(1 for verdict in verdicts if verdict.control is not None),
                excluded=dict(excluded),
                skipped_existing=counts["skipped_existing"],
                not_applicable=counts["not_applicable"],
                question_refused=counts["question_refused"],
                forecasts=counts["forecasts"],
                generation_failed=counts["generation_failed"],
                outcome_unknown=counts["outcome_unknown"],
                arm_spent_microusd=spent,
                arm_held_microusd=held_total,
            )
            finish_run(store, run_id, stop_reason=stop, counts=report.counts())
            return report
        finally:
            store.close()


def render_report(report: RunReport) -> str:
    """The operator's summary: counts and spend only, never a record's content."""
    lines = [
        f"arm {report.arm_sha256} run {report.run_id or '(dry run)'}: {report.stop_reason}",
        json.dumps(report.counts(), sort_keys=True, indent=2),
    ]
    return "\n".join(lines)
