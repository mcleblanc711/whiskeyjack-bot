"""Persisting one forecast attempt: its raw model output, then its ledger row (M1-406).

:mod:`whiskeyjack_bot.forecast.artifacts` and :mod:`whiskeyjack_bot.forecast.store` are
separate primitives, and each takes or returns a path without an opinion about how the
other went. This module is the ordering rule between them, executed rather than described:
**write the artifact first, and if that fails, append the ledger row anyway with
``raw_output_path`` NULL.**

This is M1-312's ``research/persist.py`` applied to the model call, and the argument
transfers unchanged. By the time a caller holds a finished ``ForecastGeneration``, one or
two billable model invocations have already happened. The artifact is *evidence that the
row came from text a provider really returned*; the row is the record. Losing the evidence
is an audit loss. Losing the row is losing the record that money was spent at all. So every
decision here resolves in favour of the forecast staying recorded, with the loss **reported
to the caller** rather than swallowed -- :class:`GenerationPersistence` cannot be
constructed in a shape that hides one.

**Ordering, and why it is not the other way round.** The artifact write is attempted first
because the ledger row must record the path the artifact actually landed at, and a path
recorded before the write could name a file that never appeared -- on an append-only table
that can never be corrected. The reverse failure is benign and deliberately not repaired:
an artifact written whose ledger write then fails leaves a file with no row, which is inert,
and the artifact is *never* deleted to tidy it up.

**A failed generation is persisted too, and has no row.** ``ForecastGeneration.forecast`` is
``None`` exactly when ``failure_code`` is set, and ``build_forecast_record_draft`` refuses to
build a record for one -- that case belongs in ``pipeline_failure_events`` (M1-606), not in
the table that holds successes. But the calls still cost money and their text is still the
only evidence of what the model said, so :func:`persist_raw_output` writes the artifact for a
failed attempt on its own. The result is a file with no row pointing at it, which is the same
benign direction as above. This module does not write the failure event: that table is
M1-606's and scoping this branch to it would be two writers for one failure.

**Retention is resolved here, once.** ``storage.retain_raw_model_output`` is the operator's
switch; ``forecast/artifacts.py`` still reads no config and is still handed an explicit flag.
Unlike the retrieval side there is only one flag to combine, and the asymmetry is
deliberate rather than an oversight: ``retrieval.retain_raw_responses`` exists because
retrieval has a per-provider retention question, and the forecast path has one model.

**Error contract:** the only exception a caller handles is ``ForecastRecordError``, reused
from :mod:`whiskeyjack_bot.forecast.record` rather than minting a fourth type for a module
that adds no failure mode of its own -- ``forecast/store.py`` reuses it for the same reason,
and ``research/persist.py`` reuses ``StoreError``. An artifact failure never raises out of
here by design. Both wrapped errors are already sanitized: neither ``ForecastRecordError``
nor ``ArtifactError`` echoes a stored value, a model reply or a prompt, and filesystem paths
are the settled M1-401 carve-out.

Purely local file I/O and SQLite: no network access on any path through here.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args

from whiskeyjack_bot.artifacts import ArtifactError
from whiskeyjack_bot.forecast.artifacts import write_raw_model_output
from whiskeyjack_bot.forecast.parse import ForecastGeneration
from whiskeyjack_bot.forecast.record import ForecastRecord, ForecastRecordDraft, ForecastRecordError
from whiskeyjack_bot.forecast.store import ModelCall, append_forecast_version

if TYPE_CHECKING:
    from whiskeyjack_bot.config import AppConfig

_LOGGER = logging.getLogger(__name__)

# What became of the raw artifact. `retention_disabled` is not a failure -- it is the
# configured flag's meaning -- and keeping it distinct from `failed` is the whole point of
# reporting an outcome rather than just a path: both leave `raw_output_path` NULL, and a
# caller auditing the ledger cannot otherwise tell "the operator asked us not to keep it"
# from "we tried and lost it". The vocabulary is `research/persist.py`'s, restated rather
# than imported: the two modules agree today, and a shared Literal would make one item's
# decision to add a fourth outcome silently rewrite the other's contract.
ArtifactOutcome = Literal["written", "retention_disabled", "failed"]


@dataclass(frozen=True)
class GenerationPersistence:
    """What :func:`persist_generation` did, including what it failed to do.

    ``artifact_error`` carries the sanitized :class:`ArtifactError` text when the write
    failed, and is the report the acceptance criterion asks for: an audit loss reaches the
    caller as a value rather than as a log line nobody reads.

    The constructor refuses the combinations that would misreport what happened, so the only
    way to return "the artifact was written" is to hold the path it was written to. A result
    type that cannot represent a lie is half the criterion (M1-312).
    """

    record: ForecastRecord | None
    raw_output_path: str | None
    artifact_outcome: ArtifactOutcome
    artifact_error: str | None

    def __post_init__(self) -> None:
        if self.artifact_outcome not in get_args(ArtifactOutcome):
            raise ForecastRecordError("artifact_outcome is not one of the known outcomes")
        written = self.raw_output_path is not None
        failed = self.artifact_error is not None
        if written != (self.artifact_outcome == "written"):
            raise ForecastRecordError("a written artifact has a path and an unwritten one has none")
        if failed != (self.artifact_outcome == "failed"):
            raise ForecastRecordError("a failed artifact write reports its error and no other does")


def _resolve_retention(config: AppConfig) -> tuple[bool, Path]:
    """Return ``(retain, artifact_root)`` from the operator's switch.

    The root is type-checked rather than trusted, so a caller that hands over something
    ``AppConfig``-shaped but not an ``AppConfig`` arrives as this module's error instead of a
    raw ``TypeError`` from ``artifact_root / relative`` two calls down.
    """
    try:
        retain = config.storage.retain_raw_model_output
        root = config.storage.artifact_root
    except AttributeError:
        raise ForecastRecordError("config must be an AppConfig") from None
    if not isinstance(root, Path) or type(retain) is not bool:
        raise ForecastRecordError("config must be an AppConfig")
    return retain, root


def _write_artifact(
    config: AppConfig,
    *,
    attempt_id: str,
    question_id: int,
    generation: ForecastGeneration,
    written_at: datetime,
) -> tuple[str | None, ArtifactOutcome, str | None]:
    """Attempt the artifact write; never raise ``ArtifactError`` out of it.

    Returns ``(path, outcome, error)``. Every ``ArtifactError`` degrades to
    ``("failed", text)`` -- the M1-312 rule, and the inversion of M1-303's refuse-before-
    billing rule, which only holds *before* the spend. Here the spend has happened.

    The error text is logged at WARNING as well as returned: it is already sanitized (an
    ``ArtifactError`` never echoes a model reply, and paths are the M1-401 carve-out), and a
    caller that discards the value should still leave a trace that evidence was lost.
    """
    retain, root = _resolve_retention(config)
    try:
        path = write_raw_model_output(
            root,
            attempt_id=attempt_id,
            question_id=question_id,
            generation=generation,
            written_at_utc=written_at,
            retain=retain,
        )
    except ArtifactError as exc:
        _LOGGER.warning("raw model output artifact could not be written: %s", exc)
        return None, "failed", str(exc)
    if path is None:
        return None, "retention_disabled", None
    return path, "written", None


def persist_raw_output(
    config: AppConfig,
    *,
    attempt_id: str,
    question_id: int,
    generation: ForecastGeneration,
    written_at: datetime,
) -> GenerationPersistence:
    """Write the artifact for an attempt that produced no forecast, and record no row.

    The failed-generation half of this module. ``record`` is ``None`` in every result,
    which is not a degraded success: a generation with a ``failure_code`` has no forecast to
    store, and its ledger home is ``pipeline_failure_events`` (M1-606), written by whoever
    owns the attempt. What this function guarantees is that the text the money bought is on
    disk before that happens.

    Refuses a *successful* generation rather than silently writing an artifact nobody will
    link: :func:`persist_generation` is that path, and the two differ in whether a row is
    appended, which is not a detail to get wrong by picking the wrong function.
    """
    _require_generation(generation)
    if generation.forecast is not None:
        raise ForecastRecordError(
            "persist_raw_output is for an attempt that produced no forecast; a successful "
            "generation is persisted with persist_generation so its row is appended"
        )
    path, outcome, error = _write_artifact(
        config,
        attempt_id=attempt_id,
        question_id=question_id,
        generation=generation,
        written_at=written_at,
    )
    return GenerationPersistence(
        record=None, raw_output_path=path, artifact_outcome=outcome, artifact_error=error
    )


def persist_generation(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    draft: ForecastRecordDraft,
    generation: ForecastGeneration,
    written_at: datetime,
) -> GenerationPersistence:
    """Write the artifact, then append the forecast version -- in that order, regardless.

    ``draft`` is what :func:`whiskeyjack_bot.forecast.record.build_forecast_record_draft`
    returned for this same ``generation``; the attempt id and question id the artifact is
    keyed by are read off it rather than taken as parameters, because a second source of
    truth for which attempt this is could disagree with the row being written (M2-703's
    lesson: remove the parameter, do not check for a mismatch).

    Raises :class:`ForecastRecordError` only for a caller mistake or a ledger refusal --
    both of which mean no row was appended. An artifact failure is never a raise: the
    result carries ``artifact_outcome="failed"`` and the row is appended with a NULL path.
    """
    _require_generation(generation)
    if generation.forecast is None:
        raise ForecastRecordError(
            "a generation that produced no forecast has no record to persist; use "
            "persist_raw_output and record the failure as a pipeline failure event"
        )
    if not isinstance(draft, ForecastRecordDraft):
        raise ForecastRecordError("draft must be a ForecastRecordDraft")

    path, outcome, error = _write_artifact(
        config,
        attempt_id=draft.attempt_id,
        question_id=draft.question_id,
        generation=generation,
        written_at=written_at,
    )
    # The cost and the invocation count come from the generation, not from the artifact:
    # they are recorded even when the artifact write failed, because they are what the call
    # cost and that fact does not depend on whether the evidence survived.
    record = append_forecast_version(
        conn,
        draft=draft,
        call=ModelCall(
            raw_output_path=path,
            cost_usd=generation.cost_usd,
            model_invocations=generation.invocations,
        ),
    )
    return GenerationPersistence(
        record=record, raw_output_path=path, artifact_outcome=outcome, artifact_error=error
    )


def _require_generation(generation: object) -> ForecastGeneration:
    if not isinstance(generation, ForecastGeneration):
        raise ForecastRecordError("generation must be a ForecastGeneration")
    return generation
