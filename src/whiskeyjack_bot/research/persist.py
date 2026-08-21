"""Persisting one paid retrieval run: its raw artifact, then its ledger rows (M1-312).

``research.artifacts`` and ``research.store`` are separate primitives, and M1-306 shipped
them without a composition on purpose -- the composed entry point belongs with the
retrieval orchestrator that item does not ship. What it left behind was an ordering rule
living only in a docstring: **write the artifact first, and if that fails, persist the
ledger row anyway with ``raw_response_path`` NULL.** This module is that rule, executed.

The rule exists because of when this function is called. By the time a caller holds a
finished ``AskNewsRetrieval``/``ExaRetrieval``, up to ``max_queries_per_question * 2``
billable calls have already been made. An artifact is *evidence that the rows came from a
real provider response*; the rows are the record. Losing the evidence is an audit loss.
Losing the row is losing the record that money was spent at all. So every decision here
resolves in favour of the run staying recorded, with the loss **reported to the caller**
rather than swallowed -- :class:`PaidRunPersistence` cannot be constructed in a shape that
hides one.

**Ordering, and why it is not the other way round.** The artifact write is attempted first
because the ledger row must record the path the artifact actually landed at, and a path
recorded before the write could name a file that never appeared. The reverse failure is
benign and deliberately not repaired: an artifact written whose ledger write then fails
leaves a file with no row, which is inert, and the artifact is *never* deleted to tidy it
up -- see the module docstring of :mod:`whiskeyjack_bot.research.artifacts` on why an
artifact is not overwritten either.

**Retention is resolved here, once.** ``retrieval.retain_raw_responses`` and
``storage.retain_raw_research`` are two flags that both mean "keep raw research", and
nothing had ever combined them. Either one off means no file is written and no path is
recorded. ``artifacts.py`` still reads no config and is still handed an explicit flag; this
module is the one place that knows what the operator's two switches mean together.

**Error contract:** the only exception a caller handles is :class:`StoreError`, reused from
:mod:`whiskeyjack_bot.research.store` rather than minting a third type for a module that
adds no failure mode of its own. An artifact failure never raises out of here by design,
and the input refusals below are the store's own refusals applied one call earlier, so a
separate exception type would fragment one contract for no gain. Both wrapped errors are
already sanitized: neither ``StoreError`` nor ``ArtifactError`` echoes a stored value, a
query or a provider body, and filesystem paths are the settled M1-401 carve-out.

Purely local file I/O and SQLite: no network access on any path through here.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from whiskeyjack_bot.research.artifacts import ArtifactError, write_raw_responses
from whiskeyjack_bot.research.model import ResearchDocument, ResearchRun
from whiskeyjack_bot.research.store import StoreError, complete_run, persist_retrieval

if TYPE_CHECKING:
    from whiskeyjack_bot.config import AppConfig

_LOGGER = logging.getLogger(__name__)

# What became of the raw artifact. `retention_disabled` is not a failure -- it is the
# configured default's meaning -- and keeping it distinct from `failed` is the whole point
# of reporting an outcome rather than just a path: both leave `raw_response_path` NULL, and
# a caller auditing the ledger cannot otherwise tell "the operator asked us not to keep it"
# from "we tried and lost it".
ArtifactOutcome = Literal["written", "retention_disabled", "failed"]


@dataclass(frozen=True)
class PaidRunPersistence:
    """What :func:`persist_paid_run` did, including what it failed to do.

    ``artifact_error`` carries the sanitized :class:`ArtifactError` text when the write
    failed, and is the report the acceptance criterion asks for: an audit loss reaches the
    caller as a value rather than as a log line nobody reads.

    The constructor refuses the combinations that would misreport what happened, so the
    only way to return "the artifact was written" is to hold the path it was written to.
    """

    document_ids: tuple[str, ...]
    raw_response_path: str | None
    artifact_outcome: ArtifactOutcome
    artifact_error: str | None

    def __post_init__(self) -> None:
        if self.artifact_outcome not in get_args(ArtifactOutcome):
            raise StoreError("artifact_outcome is not one of the known outcomes")
        written = self.raw_response_path is not None
        failed = self.artifact_error is not None
        if written != (self.artifact_outcome == "written"):
            raise StoreError("a written artifact has a path and an unwritten one has none")
        if failed != (self.artifact_outcome == "failed"):
            raise StoreError("a failed artifact write reports its error and no other does")


def _resolve_retention(config: AppConfig) -> tuple[bool, Path]:
    """Return ``(retain, artifact_root)`` from the two flags that both mean "keep raw".

    ``retrieval.retain_raw_responses`` and ``storage.retain_raw_research`` are combined
    with ``and``: either one off means the operator asked for no raw research kept, and
    honouring the narrower of two switches is the reading that cannot store something an
    operator turned off. Nothing else in the project had ever combined them.

    The root is type-checked rather than trusted, so a caller that hands over something
    AppConfig-shaped but not an ``AppConfig`` arrives as this module's error instead of a
    raw ``TypeError`` from ``artifact_root / relative`` two calls down.
    """
    try:
        retain = config.retrieval.retain_raw_responses and config.storage.retain_raw_research
        root = config.storage.artifact_root
    except AttributeError:
        raise StoreError("config must be an AppConfig") from None
    if not isinstance(root, Path):
        raise StoreError("config must be an AppConfig")
    return bool(retain), root


def _validated_run(run: object) -> ResearchRun:
    """Refuse the run shapes this composition cannot honour, before any I/O.

    Every refusal here loses a paid run's row, which is the outcome this module exists to
    avoid, so the list is short and each entry earns its place by being a contradiction
    rather than a mishap:

    - not a ``ResearchRun``, or no ``completed_at_utc``: both ledger writers refuse it
      anyway (:func:`store.persist_retrieval`, :func:`store.complete_run`), and failing
      before the artifact write at least leaves no orphan file behind.
    - already carrying ``raw_response_path``: the store ignores the model's field and
      writes the argument instead, so accepting one would silently discard a caller's
      claim about where its evidence lives. This function mints that path.

    Everything else degrades rather than refuses -- see :func:`persist_paid_run`.
    """
    if not isinstance(run, ResearchRun):
        raise StoreError("run must be a ResearchRun")
    if run.completed_at_utc is None:
        raise StoreError(
            "persist_paid_run requires a run carrying completed_at_utc; a run whose calls "
            "have not finished is recorded with store.open_run"
        )
    if run.raw_response_path is not None:
        raise StoreError(
            "persist_paid_run mints raw_response_path and refuses a run that already "
            "carries one; the stored row would take this call's path, not the run's"
        )
    return run


def persist_paid_run(
    conn: sqlite3.Connection,
    config: AppConfig,
    run: ResearchRun,
    documents: Iterable[ResearchDocument],
    *,
    raw_responses: Sequence[dict[str, Any]],
    written_at_utc: datetime | None = None,
    run_opened: bool = False,
) -> PaidRunPersistence:
    """Store one paid run's raw artifact and its ledger rows, in that order.

    The run and its documents are committed **even when the artifact write fails**, with
    ``raw_response_path`` NULL and the failure reported in the returned
    :class:`PaidRunPersistence`. That is the item's acceptance criterion, and it is why
    :class:`ArtifactError` is caught here and nowhere re-raised.

    ``run_opened`` selects which ledger write happens and has no safe default to guess at:
    false takes :func:`store.persist_retrieval` (one INSERT of run and documents), true
    takes :func:`store.complete_run` (the UPDATE of a row opened before the billable
    calls, which is the shape that keeps spend attributable if the process dies mid-run).
    Guessing wrong would either insert a duplicate or complete the wrong row, so it is
    checked with ``type(...) is not bool`` rather than for truthiness -- M1-306's round-2
    ``completed_only=None`` returned an open run by taking the false branch of a
    truthiness test, which is the same defect one argument over.

    The artifact's identity is taken from the run itself -- ``retrieval_run_id``,
    ``question_id``, ``provider`` -- and not accepted separately, so the envelope and the
    row it is filed under cannot disagree. ``written_at_utc`` defaults to now.

    Raises :class:`StoreError` for the refusals in :func:`_validated_run`, for a
    non-bool ``run_opened``, and for any ledger failure. **Nothing else raises**: an
    ``ArtifactError`` of any kind -- an ordinary I/O failure, a destination that already
    exists, a caller mistake such as a run id the artifact layout will not accept -- is
    reported, not raised, because the calls are already paid for and refusing would trade
    a lost artifact for a lost run.
    """
    if type(run_opened) is not bool:
        raise StoreError("run_opened must be a bool")
    validated = _validated_run(run)
    retain, artifact_root = _resolve_retention(config)
    timestamp = datetime.now(tz=timezone.utc) if written_at_utc is None else written_at_utc

    path: str | None = None
    outcome: ArtifactOutcome = "retention_disabled"
    artifact_error: str | None = None
    if retain:
        try:
            path = write_raw_responses(
                artifact_root,
                retrieval_run_id=validated.retrieval_run_id,
                question_id=validated.question_id,
                provider=validated.provider,
                raw_responses=raw_responses,
                written_at_utc=timestamp,
                # Not `retain=retain`: retention was decided above, and passing the flag
                # through would leave two places that can turn the write off.
                retain=True,
            )
            outcome = "written"
        except ArtifactError as exc:
            artifact_error = str(exc)
            outcome = "failed"
            # The message is ArtifactError's own and carries no provider body; a path is
            # operator-supplied configuration under the M1-401 carve-out. Logged as well
            # as returned because an audit loss that only a caller inspects is one an
            # operator can miss entirely.
            _LOGGER.warning(
                "retrieval artifact was not written; recording the run without it: %s",
                artifact_error,
            )

    if run_opened:
        document_ids = complete_run(conn, validated, documents, raw_response_path=path)
    else:
        document_ids = persist_retrieval(conn, validated, documents, raw_response_path=path)
    return PaidRunPersistence(
        document_ids=document_ids,
        raw_response_path=path,
        artifact_outcome=outcome,
        artifact_error=artifact_error,
    )
