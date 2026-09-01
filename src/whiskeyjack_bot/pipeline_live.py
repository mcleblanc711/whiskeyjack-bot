"""One or more snapshot questions, through live retrieval and a live model call (M1-315).

The paid sibling of :mod:`whiskeyjack_bot.pipeline`, and a **separate module on purpose**.
That module's acceptance guarantee is structural -- none of this project's paid or posting
adapters is reachable by importing it, which is how T-903's two zeroes are pinned by an
import graph rather than by a mock count. M1-315's own criterion says the paid composition
must "live outside ``whiskeyjack_bot.pipeline`` or that module's import-graph test [must] be
restated". Restating a guarantee is strictly worse than keeping it, so ``pipeline.py`` is
not edited and not imported *from* here in the direction that would matter: this module may
import that one, never the reverse.

What is claimed for *this* module is narrower and is asserted the same way. It reaches
``research.asknews``, ``research.exa`` and ``forecast.generate`` -- it must, it spends money
through all three -- and it reaches **no submission module and no approval module**, so
``CODEX_HANDOFF.md`` § "Required CLI entry points"'s rule that ``run`` never submits
implicitly holds here for the same structural reason it holds there. One honest caveat,
stated rather than papered over: ``whiskeyjack_bot.metaculus.client`` *is* on this graph,
because ``research/exa.py`` takes ``MissingCredentialError`` from it and that module also
holds ``build_poster``. That coupling predates this branch and is filed as its own row; what
makes "cannot post" true here is the absence of ``submission*`` and ``approval``, plus the
fact that a post requires an approved record and a separate command (D23).

**The order of every decision below is "refuse before the money, report after it."** That is
M1-303 round 4's rule and M1-312's inversion of it, and the boundary between them is the
first billable call:

- Everything checkable is checked before the loop -- config, the two replay switches, the
  run limits, the prompt, and both clients (constructing a client *is* the credential check,
  and doing it once means a missing key fails before question one rather than during
  question three).
- Inside the loop nothing aborts the batch. A question whose research finds nothing, whose
  reply is unusable, or whose row the ledger refuses is recorded as that question's outcome
  and the next question runs. This is the criterion's "a per-question failure records its
  pipeline event and does not abort the batch".

**Retrying a later phase repeats no paid call unless asked** (``CODEX_HANDOFF.md`` §
"Pipeline and failure boundaries"). Research already completed for a question is reused from
the ledger by default; ``refresh_research=True`` pays for it again. That reuse is
deliberately **not** ``replay_research``: that function's ``retrieval.replay_saved_research``
gate means "this run never spends at all", which a live run has already contradicted by
existing. Reuse means something different -- this *phase* is already complete -- so it goes
through :func:`store.load_packet`, which is ungated and reads SQLite and nothing else, and
it is logged and named in the result rather than being a silent saving.

Saved *model output* is not reused here at all. A live run mints a fresh attempt id, so
there is nothing to address a saved reply by; replaying one is ``run-replay --attempt-id``,
which is a different command precisely so that the reader of a command line can tell which
of the two spends money.

Errors are :class:`LiveRunError`. Messages never echo a question title, a document, a model
reply or a stored field value; filesystem paths are the settled M1-401 carve-out.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.forecast.generate import (
    ForecastGenerationError,
    build_forecaster_client,
    generate_forecast,
)
from whiskeyjack_bot.forecast.inputs import ForecastInputError
from whiskeyjack_bot.forecast.persist import ArtifactOutcome, persist_generation, persist_raw_output
from whiskeyjack_bot.forecast.record import (
    ForecastRecordError,
    build_forecast_record_draft,
    record_sha256,
)
from whiskeyjack_bot.forecast.schema import ForecastSchemaError
from whiskeyjack_bot.forecast.store import mint_record_id
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    PreForecastFailureCode,
    record_pre_forecast_failure,
    record_validation,
    transaction,
)
from whiskeyjack_bot.metaculus.client import MissingCredentialError
from whiskeyjack_bot.metaculus.snapshots import SnapshotError, load_snapshot
from whiskeyjack_bot.prompt import PromptError, load_prompt
from whiskeyjack_bot.questions.normalize import NormalizationError, normalize_questions
from whiskeyjack_bot.research.asknews import AskNewsRetrievalError, build_asknews_client
from whiskeyjack_bot.research.exa import ExaFallbackError, build_exa_client
from whiskeyjack_bot.research.orchestrate import (
    OrchestrationError,
    PaidRetrievalError,
    retrieve_for_question,
)
from whiskeyjack_bot.research.packet import packet_sha256
from whiskeyjack_bot.research.store import StoreError, list_retrieval_run_ids, load_packet

if TYPE_CHECKING:
    from whiskeyjack_bot.forecast.parse import ForecastGeneration
    from whiskeyjack_bot.prompt import LoadedPrompt
    from whiskeyjack_bot.questions.model import CanonicalQuestion
    from whiskeyjack_bot.research.packet import ResearchPacket

_LOGGER = logging.getLogger(__name__)

# Why the batch stopped. ``completed`` means it ran out of questions, which is the only
# member that is not a cap biting.
StopReason = Literal["completed", "question_limit", "cost_limit"]

# What became of one question. ``not_recorded`` is the one that writes nothing to the
# ledger: see :func:`_forecast_one` on why a post-generation persistence failure has no
# honest event type to be written as.
QuestionStatus = Literal["recorded", "research_failed", "generation_failed", "not_recorded"]


class LiveRunError(Exception):
    """A live run this module refused to start, or could not set up.

    Deliberately not :class:`whiskeyjack_bot.pipeline.PipelineError`: reusing it would make
    the replay module a dependency of the paid one for a class name, and the two commands
    fail for different reasons. Raised only *before* the per-question loop -- once the loop
    is running, a failure is a question's outcome rather than the batch's.
    """


@dataclass(frozen=True)
class QuestionOutcome:
    """What one question's live attempt produced, successful or not.

    The ``__post_init__`` refuses every pairing that would misdescribe what happened, the
    shape M1-312 gave :class:`PaidRunPersistence` and M1-406 gave
    :class:`GenerationPersistence`: a result object that cannot represent a lie is half of
    "reports the loss to its caller rather than swallowing it".

    ``artifact_outcome`` legitimately carries all three of its members here, which is the
    difference from ``pipeline.ReplayRun`` and is M1-312's rule rather than an oversight:
    for a **paid** attempt the cost and the invocation count are facts whether or not the
    evidence survived, so the row is written regardless. ``run_replay`` holds itself to the
    stricter bar because it spends nothing; this path cannot, and ``pipeline.py`` says so in
    ``_require_retained_output``'s own docstring.
    """

    question_id: int
    status: QuestionStatus
    attempt_id: str
    retrieval_run_ids: tuple[str, ...]
    document_count: int
    research_reused: bool
    record_id: str | None = None
    forecast_version: int | None = None
    forecast_sha256: str | None = None
    research_packet_sha256: str | None = None
    raw_output_path: str | None = None
    artifact_outcome: ArtifactOutcome | None = None
    detail_code: PreForecastFailureCode | None = None
    problems: tuple[str, ...] = ()
    cost_usd: float | None = None
    unpriced_calls: int = 0
    note: str | None = None

    def __post_init__(self) -> None:
        recorded = self.status == "recorded"
        if recorded != (self.record_id is not None):
            raise LiveRunError("a recorded question carries a record id and no other does")
        if recorded != (self.forecast_sha256 is not None):
            raise LiveRunError("a recorded question carries a forecast hash and no other does")
        wrote_event = self.status in ("research_failed", "generation_failed")
        if wrote_event != (self.detail_code is not None):
            raise LiveRunError(
                "a question whose failure was recorded carries a detail code and no other does"
            )
        if recorded and self.artifact_outcome is None:
            raise LiveRunError("a recorded question reports what became of its artifact")
        if self.artifact_outcome is not None and self.status not in (
            "recorded",
            "generation_failed",
        ):
            raise LiveRunError("only a question that reached the model reports an artifact outcome")


@dataclass(frozen=True)
class BatchRun:
    """What one ``run`` invocation did, across every question it attempted.

    ``known_cost_usd`` and ``unpriced_calls`` are two fields rather than one total for the
    reason M1-303 round 3 settled: ``cost_usd is None`` means **unknown, never free**, so a
    single number would have to either invent a zero or refuse to answer. The pair says the
    true thing -- this much is known to have been spent, alongside this many calls nobody
    priced -- and :func:`run_live` never claims the cap bounded more than the first.
    """

    outcomes: tuple[QuestionOutcome, ...]
    stop_reason: StopReason
    known_cost_usd: float
    unpriced_calls: int

    @property
    def records_written(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == "recorded")

    @property
    def failures(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status != "recorded")


def _require_live_configuration(config: AppConfig) -> None:
    """Refuse a configuration this command cannot honestly run under. Before anything.

    One ``isinstance`` at the top, and the message says exactly what it checked. That is
    **M1-316's finding closed in the module it would otherwise have been copied into**:
    ``pipeline.py``'s guards read an attribute and then check the *value's* type while
    printing "config must be an AppConfig", so a ``SimpleNamespace`` satisfies them. I
    copied that shape once already without asking what it verified, which is exactly how the
    class propagates; ``pipeline.py``'s own guards are M1-316's row to fix, not this
    branch's to edit.

    The two replay switches must be **off**. A live run under a replay configuration is a
    contradiction rather than a preference: ``replay_saved_research`` says "this run never
    spends", and every path below this line does. Refusing is also what keeps the two
    commands legible -- a config edit cannot silently turn ``run`` into ``run-replay``.
    """
    if not isinstance(config, AppConfig):
        raise LiveRunError("config must be an AppConfig")
    if config.retrieval.replay_saved_research:
        raise LiveRunError(
            "retrieval.replay_saved_research is enabled; that configuration says this run "
            "must not spend, so the live command refuses it (use `run-replay`)"
        )
    if config.forecast.replay_saved_model_output:
        raise LiveRunError(
            "forecast.replay_saved_model_output is enabled; that configuration says this "
            "run must not spend, so the live command refuses it (use `run-replay`)"
        )
    if config.run_limits.max_parallel_questions != 1:
        # Refused rather than ignored. This loop is sequential, and a configuration asking
        # for parallelism that silently does not happen is a claim the operator would have
        # no way to check. Concurrency is not this item's, and the ledger's write path has
        # never been exercised under it.
        raise LiveRunError(
            "run_limits.max_parallel_questions must be 1; this command forecasts questions "
            "sequentially and will not pretend otherwise"
        )


def _effective_limit(config: AppConfig, limit: int | None) -> int:
    """How many questions this batch may attempt.

    ``run_limits.max_questions`` is the ceiling and ``--limit`` may only lower it. It is the
    first consumer that config field has ever had -- it, ``max_cost_usd`` and
    ``max_parallel_questions`` have been committed since M0 with no reader anywhere in
    ``src/`` -- and a paid batch loop is where it belongs: the committed default is 1, so an
    operator who has not thought about it gets one question, not the whole snapshot.
    """
    ceiling = config.run_limits.max_questions
    if limit is None:
        return ceiling
    if type(limit) is not int or limit < 1:
        raise LiveRunError("limit must be a positive integer")
    if limit > ceiling:
        raise LiveRunError(
            "limit exceeds run_limits.max_questions; raise the configured ceiling "
            "deliberately rather than passing a larger number on the command line"
        )
    return limit


def _select_questions(
    snapshot: Path, *, question_id: int | None
) -> tuple[tuple[CanonicalQuestion, ...], str]:
    """The questions this batch will attempt, with the snapshot's tournament id.

    Reads the snapshot with ``load_snapshot``, never ``metaculus.fetch``'s wrapper: that one
    is ``load_snapshot`` plus a log line but sits beside the live fetcher and imports
    ``metaculus.client``. T-903 recorded that as this project's most expensive defect class
    -- an assertion that cannot fail for the thing it names -- after a deferred,
    function-local import of it passed two of three guards.
    """
    if not isinstance(snapshot, Path):
        raise LiveRunError("snapshot must be a Path")
    if question_id is not None and type(question_id) is not int:
        raise LiveRunError("question_id must be an int")
    try:
        meta, loaded = load_snapshot(snapshot)
    except SnapshotError as exc:
        raise LiveRunError(str(exc)) from None
    _LOGGER.info(
        "loaded %d questions from snapshot %s (tournament %r, fetched %s)",
        len(loaded),
        snapshot,
        meta.tournament_id,
        meta.fetched_at_utc.isoformat(),
    )
    try:
        result = normalize_questions(loaded)
    except NormalizationError as exc:
        raise LiveRunError(str(exc)) from None
    for event in result.deferrals:
        _LOGGER.info("snapshot question deferred: %s", event.reason)

    tournament_id = str(meta.tournament_id)
    if not tournament_id.strip():
        raise LiveRunError("the snapshot records a blank tournament id")

    if question_id is not None:
        for question in result.questions:
            if question.question_id == question_id:
                return (question,), tournament_id
        # No id in the message: a question id is row content (M1-202's precedent).
        raise LiveRunError(
            "the snapshot holds no supported question with that id (offending value withheld)"
        )
    if not result.questions:
        raise LiveRunError("the snapshot holds no supported question to forecast")
    # Every supported question, unsliced. The caller applies the limit, so it can tell a
    # batch that ran out of questions from one a cap stopped -- two different stop reasons
    # that a pre-sliced tuple makes indistinguishable.
    return tuple(result.questions), tournament_id


@dataclass(frozen=True)
class _Research:
    """The research one question will be forecast from, however it was obtained.

    A local type rather than a reused :class:`RetrievalOutcome`, because that one's
    ``__post_init__`` requires every id it names to have a :class:`ProviderRun` beside it --
    it is the report of *calls this process made*, and reuse made none. Bending it to carry
    a reused packet would have meant weakening the one invariant that keeps an orchestration
    report from naming runs it did not perform. (It refused the attempt, which is what that
    invariant is for.)
    """

    packet: ResearchPacket | None
    retrieval_run_ids: tuple[str, ...]
    reused: bool
    provider_failed: bool
    cost_usd: float | None
    unpriced_calls: int


def _research(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    question: CanonicalQuestion,
    now: datetime,
    refresh: bool,
    news_client: Any | None,
    web_client: Any | None,
) -> _Research:
    """Reuse the research already paid for, or pay for it.

    The default is reuse, and the rule it implements is ``CODEX_HANDOFF.md``'s: *"retrying a
    later phase must not repeat an earlier paid call unless explicitly requested."* A rerun
    after a model failure is the ordinary case this exists for -- without it, fixing a
    malformed reply costs the whole retrieval again.

    ``load_packet`` rather than ``replay_research`` for the reason in the module docstring:
    the latter is gated on a switch meaning "this run never spends", which this one does.
    Both read SQLite and issue zero provider calls; only one of them also asserts a mode this
    command is not in.

    ``completed_only`` is left at its default. An open run is a spend record, not evidence --
    reusing one would build a packet whose contents change when that run finishes, which is
    exactly the identity failure ``list_retrieval_run_ids`` was separated from ``load_packet``
    to prevent.
    """
    if not refresh:
        try:
            existing = list_retrieval_run_ids(conn, question_id=question.question_id)
            if existing:
                packet = load_packet(
                    conn, question_id=question.question_id, retrieval_run_ids=existing
                )
                if packet.documents:
                    _LOGGER.info(
                        "reusing %d completed research run(s) for question %d; no provider "
                        "call is made (pass --refresh-research to retrieve again)",
                        len(existing),
                        question.question_id,
                    )
                    return _Research(
                        packet=packet,
                        retrieval_run_ids=existing,
                        reused=True,
                        provider_failed=False,
                        cost_usd=None,
                        unpriced_calls=0,
                    )
        except StoreError as exc:
            # Reuse is an optimisation over paying again, so a ledger that cannot answer
            # "what is already here" degrades to retrieving rather than failing the
            # question. Logged, because silently paying twice is the thing being avoided.
            _LOGGER.warning("could not reuse stored research, retrieving instead: %s", exc)

    outcome = retrieve_for_question(
        conn,
        config,
        question=question,
        now=now,
        news_client=news_client,
        web_client=web_client,
    )
    return _Research(
        packet=outcome.packet,
        retrieval_run_ids=outcome.retrieval_run_ids,
        reused=False,
        provider_failed=outcome.provider_failed,
        cost_usd=outcome.cost_usd,
        unpriced_calls=outcome.unpriced_runs,
    )


def _record_pre_forecast(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    question_id: int,
    tournament_id: str,
    event_type: Literal["research_failed", "generation_failed"],
    detail_code: PreForecastFailureCode,
    retrieval_run_id: str | None,
    occurred_at: datetime,
) -> str | None:
    """Append one pipeline failure event, and report rather than raise if that fails too.

    A ledger that cannot record the failure must not also lose the rest of the batch. The
    returned note is sanitized ``LifecycleError`` text -- it names no stored value -- and it
    reaches the operator through the question's outcome instead of the ledger.
    """
    try:
        record_pre_forecast_failure(
            conn,
            attempt_id=attempt_id,
            question_id=question_id,
            tournament_id=tournament_id,
            event_type=event_type,
            detail_code=detail_code,
            occurred_at=occurred_at,
            retrieval_run_id=retrieval_run_id,
        )
    except LifecycleError as exc:
        _LOGGER.error(
            "could not record the %s event for question %d: %s", event_type, question_id, exc
        )
        return str(exc)
    return None


def _attempt_question(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    question: CanonicalQuestion,
    tournament_id: str,
    prompt: LoadedPrompt,
    now: datetime,
    refresh: bool,
    news_client: Any | None,
    web_client: Any | None,
    forecaster: Any | None,
) -> QuestionOutcome:
    """One question, end to end. **Never raises** -- every failure becomes this question's outcome.

    That is the criterion's "a per-question failure ... does not abort the batch", and it is
    why the exception handling here is broad at the module boundaries rather than at the call
    sites: each of ``OrchestrationError``, ``ForecastGenerationError``, ``ForecastInputError``,
    ``ForecastSchemaError``, ``ForecastRecordError``, ``StoreError`` and ``LifecycleError``
    means "this question did not finish", and none of them means "stop the run".

    **That sentence was true of the persistence boundary and false of the research one until
    round 1 said so**, and the correction is worth keeping visible because the shape recurs:
    ``StoreError`` is caught by name where it is *raised* by name (the record/validation
    block below), while the research phase reaches it through another module -- so listing it
    here read as a guarantee that the ``except`` clause did not provide. Research failures now
    arrive as this composer can handle them: ``PaidRetrievalError`` for a ledger write that
    failed after the provider was billed, its parent ``OrchestrationError`` for a refusal that
    cost nothing. A raw ``StoreError`` from that phase would now be a bug in
    ``retrieve_for_question``'s boundary rather than a hole here.

    **Three failure shapes, three different ledger consequences**, and the differences are the
    substance of this function:

    - *Research found nothing, the provider failed, or the ledger refused a write.* A
      ``research_failed`` event (M1-606). This is that event type's **first production
      writer** -- ``pipeline.py`` writes only ``generation_failed`` -- and
      ``retrieval_run_id`` is optional on it for exactly the case where the refusal happened
      before any ``research_runs`` row existed. When the failure came *after* the spend the
      run id is not optional and is cited: ``PaidRetrievalError`` carries the row
      ``open_run`` put there, and the spend it carries is added to the batch's total, so a
      question that failed after paying is not free in the accounting.
    - *The model answered and the answer was unusable, or the call could not be made.* The
      raw text is written to disk first (``persist_raw_output``: the money bought that text,
      and the artifact is the only trace of what it bought), then a ``generation_failed``
      event carrying ``generation.failure_code`` -- which is already a
      ``PreForecastFailureCode``, so nothing is re-derived here.
    - *The forecast was good and the ledger refused the row.* **Nothing is written**, and
      that is deliberate rather than an omission. ``PreForecastEventType`` has two members
      and neither is true: the generation did not fail. Widening the vocabulary is a
      ``CHECK`` rebuild of an append-only table, which is not a thing to do sideways from a
      composition item, so it is filed as its own row. What survives is the artifact on disk
      (written before the row, M1-406's ordering) and ``status="not_recorded"`` with the
      sanitized reason on the outcome. T-903 settled the principle: recording a failure that
      did not happen puts a false claim in the ledger.
    """
    attempt_id = mint_record_id()
    question_id = question.question_id

    try:
        research = _research(
            conn,
            config,
            question=question,
            now=now,
            refresh=refresh,
            news_client=news_client,
            web_client=web_client,
        )
    except PaidRetrievalError as exc:
        # The ledger refused a write *after* the provider was billed. Caught before its
        # parent class because the two cases differ in everything the ledger and the budget
        # care about: this one names the run row that says money was spent on this question,
        # and carries the spend forward so `run_live`'s accumulated figure is not understated
        # by a question that failed after paying. Round 1 found this shape escaping the batch
        # entirely, which lost the event *and* the remaining questions.
        note = _record_pre_forecast(
            conn,
            attempt_id=attempt_id,
            question_id=question_id,
            tournament_id=tournament_id,
            event_type="research_failed",
            detail_code="internal_error",
            retrieval_run_id=exc.retrieval_run_ids[0],
            occurred_at=now,
        )
        return QuestionOutcome(
            question_id=question_id,
            status="research_failed",
            attempt_id=attempt_id,
            retrieval_run_ids=exc.retrieval_run_ids,
            document_count=0,
            research_reused=False,
            detail_code="internal_error",
            problems=(str(exc),),
            cost_usd=exc.cost_usd,
            unpriced_calls=exc.unpriced_calls,
            note=note,
        )
    except OrchestrationError as exc:
        # Refused before any provider call, so nothing was spent -- but the research phase
        # still did not happen for this question, which is what `research_failed` means.
        # `retrieval_run_id=None` is the documented shape for a failure that precedes any
        # `research_runs` row.
        note = _record_pre_forecast(
            conn,
            attempt_id=attempt_id,
            question_id=question_id,
            tournament_id=tournament_id,
            event_type="research_failed",
            detail_code="internal_error",
            retrieval_run_id=None,
            occurred_at=now,
        )
        return QuestionOutcome(
            question_id=question_id,
            status="research_failed",
            attempt_id=attempt_id,
            retrieval_run_ids=(),
            document_count=0,
            research_reused=False,
            detail_code="internal_error",
            problems=(str(exc),),
            note=note,
        )

    if research.packet is None:
        detail: PreForecastFailureCode = (
            "provider_error" if research.provider_failed else "no_evidence"
        )
        note = _record_pre_forecast(
            conn,
            attempt_id=attempt_id,
            question_id=question_id,
            tournament_id=tournament_id,
            event_type="research_failed",
            detail_code=detail,
            retrieval_run_id=research.retrieval_run_ids[0] if research.retrieval_run_ids else None,
            occurred_at=now,
        )
        return QuestionOutcome(
            question_id=question_id,
            status="research_failed",
            attempt_id=attempt_id,
            retrieval_run_ids=research.retrieval_run_ids,
            document_count=0,
            research_reused=research.reused,
            detail_code=detail,
            cost_usd=research.cost_usd,
            unpriced_calls=research.unpriced_calls,
            note=note,
        )

    run_ids = research.retrieval_run_ids
    try:
        generation: ForecastGeneration = generate_forecast(
            config=config,
            question=question,
            packet=research.packet,
            prompt=prompt,
            tournament_id=tournament_id,
            now=now,
            client=forecaster,
        )
    except (
        ForecastGenerationError,
        ForecastInputError,
        ForecastSchemaError,
        MissingCredentialError,
    ) as exc:
        # Every one of these is raised *before* the spend, by contract. The forecast still
        # did not happen, so it is a recorded generation failure -- with no artifact, because
        # there is no reply to keep.
        note = _record_pre_forecast(
            conn,
            attempt_id=attempt_id,
            question_id=question_id,
            tournament_id=tournament_id,
            event_type="generation_failed",
            detail_code="internal_error",
            retrieval_run_id=run_ids[0],
            occurred_at=now,
        )
        return QuestionOutcome(
            question_id=question_id,
            status="generation_failed",
            attempt_id=attempt_id,
            retrieval_run_ids=run_ids,
            document_count=len(research.packet.documents),
            research_reused=research.reused,
            detail_code="internal_error",
            problems=(str(exc),),
            cost_usd=research.cost_usd,
            unpriced_calls=research.unpriced_calls,
            note=note,
        )

    knowns = [value for value in (research.cost_usd, generation.cost_usd) if value is not None]
    cost = sum(knowns) if knowns else None
    unpriced = research.unpriced_calls + (
        generation.invocations if generation.cost_usd is None else 0
    )
    documents = len(research.packet.documents)

    if generation.forecast is None:
        # `ForecastGeneration` contracts that `forecast is None` exactly when `failure_code`
        # is set, but an `assert` would be stripped under `-O` and a crash is a worse answer
        # than a true-but-vague one. `internal_error` is what an unclassified failure is.
        failure_code: PreForecastFailureCode = generation.failure_code or "internal_error"
        artifact_outcome: ArtifactOutcome = "retention_disabled"
        artifact_path: str | None = None
        try:
            kept = persist_raw_output(
                config,
                attempt_id=attempt_id,
                question_id=question_id,
                generation=generation,
                written_at=now,
            )
            artifact_outcome = kept.artifact_outcome
            artifact_path = kept.raw_output_path
        except ForecastRecordError as exc:
            _LOGGER.error("could not file the failed reply for question %d: %s", question_id, exc)
        note = _record_pre_forecast(
            conn,
            attempt_id=attempt_id,
            question_id=question_id,
            tournament_id=tournament_id,
            event_type="generation_failed",
            detail_code=failure_code,
            retrieval_run_id=run_ids[0],
            occurred_at=now,
        )
        return QuestionOutcome(
            question_id=question_id,
            status="generation_failed",
            attempt_id=attempt_id,
            retrieval_run_ids=run_ids,
            document_count=documents,
            research_reused=research.reused,
            raw_output_path=artifact_path,
            artifact_outcome=artifact_outcome,
            detail_code=failure_code,
            problems=generation.failure_problems,
            cost_usd=cost,
            unpriced_calls=unpriced,
            note=note,
        )

    try:
        draft = build_forecast_record_draft(
            question=question,
            generation=generation,
            tournament_id=tournament_id,
            attempt_id=attempt_id,
            # The packet's first run, oldest first. The column is a single FK while a packet
            # may carry many runs; what pins the *whole* packet is `research_packet_sha256`,
            # a record field and therefore inside `forecast_sha256`. Widening a merged schema
            # is not this item's, and T-903 already filed the tension.
            retrieval_run_id=run_ids[0],
            research_packet_sha256=packet_sha256(research.packet),
            generated_at=now,
        )
        # One unit: the row, its artifact and its validation event, or none of them.
        # T-903's round-1 finding 1, and `lifecycle.transaction` nests as a SAVEPOINT so
        # this caller can have exactly that. **What is deliberately absent is `run_replay`'s
        # refusal of a non-"written" artifact.** For a paid attempt the cost and the
        # invocation count are facts whether or not the evidence survived, so the row is
        # written regardless -- M1-312's rule, and `pipeline._require_retained_output`'s own
        # docstring names this caller as the one that needs it.
        with transaction(conn):
            persisted = persist_generation(
                conn, config, draft=draft, generation=generation, written_at=now
            )
            record = persisted.record
            if record is None:  # pragma: no cover - persist_generation raises instead
                raise ForecastRecordError("the forecast version was not appended")
            record_validation(conn, record_id=record.record_id, occurred_at=now)
    except (ForecastRecordError, StoreError, LifecycleError) as exc:
        _LOGGER.error("could not record the forecast for question %d: %s", question_id, exc)
        return QuestionOutcome(
            question_id=question_id,
            status="not_recorded",
            attempt_id=attempt_id,
            retrieval_run_ids=run_ids,
            document_count=documents,
            research_reused=research.reused,
            cost_usd=cost,
            unpriced_calls=unpriced,
            note=str(exc),
        )

    return QuestionOutcome(
        question_id=question_id,
        status="recorded",
        attempt_id=record.attempt_id,
        retrieval_run_ids=run_ids,
        document_count=documents,
        research_reused=research.reused,
        record_id=record.record_id,
        forecast_version=record.forecast_version,
        forecast_sha256=record_sha256(record),
        research_packet_sha256=record.research_packet_sha256,
        raw_output_path=persisted.raw_output_path,
        artifact_outcome=persisted.artifact_outcome,
        cost_usd=cost,
        unpriced_calls=unpriced,
    )


def _build_clients(
    config: AppConfig,
    *,
    news_client: Any | None,
    web_client: Any | None,
    forecaster: Any | None,
) -> tuple[Any, Any | None, Any | None]:
    """Construct every client once, before the loop. **This is the credential check.**

    Building a client is the only thing that reads a credential, so doing it here means a
    missing ``OPENROUTER_API_KEY`` or ``ASKNEWS_API_KEY`` fails before question one rather
    than during question three, with two questions' worth of retrieval already paid for.
    ``verify-env`` checks the same variables and is not a substitute: it runs at a different
    time, and a variable can be unset between the two.

    **The Exa key is the deliberate exception.** The fallback is optional -- an operator may
    reasonably have no Exa account -- so its absence marks the fallback unavailable and does
    not refuse the run. Probing it once here rather than per question keeps the log honest:
    one line saying the fallback cannot run, instead of one per question saying it again.
    """
    try:
        forecaster_client = (
            forecaster if forecaster is not None else build_forecaster_client(config)
        )
    except (MissingCredentialError, ForecastGenerationError) as exc:
        raise LiveRunError(str(exc)) from None
    try:
        primary = news_client if news_client is not None else build_asknews_client(config)
    except (MissingCredentialError, AskNewsRetrievalError) as exc:
        raise LiveRunError(str(exc)) from None

    fallback: Any | None = web_client
    if fallback is None:
        try:
            fallback = build_exa_client(config)
        except (MissingCredentialError, ExaFallbackError) as exc:
            _LOGGER.info(
                "the fallback provider is unavailable for this run; a question whose "
                "primary provider fails will be recorded as a research failure: %s",
                exc,
            )
            fallback = None
    return forecaster_client, primary, fallback


def run_live(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    snapshot: Path,
    now: datetime,
    question_id: int | None = None,
    limit: int | None = None,
    refresh_research: bool = False,
    news_client: Any | None = None,
    web_client: Any | None = None,
    forecaster: Any | None = None,
) -> BatchRun:
    """Forecast one or more saved questions live, writing one validated record per question.

    **This function spends money.** Its replay-only sibling is
    :func:`whiskeyjack_bot.pipeline.run_replay`, reached by a different command.

    ``now`` is caller-supplied and is used for the **whole batch** rather than re-read per
    question: one invocation is one run, and its rows share its instant. That keeps the batch
    reproducible under test and keeps every ``started_at_utc`` in it comparable. The cost is
    that a long batch stamps its last question with the time the first one started, which is
    immaterial against a freshness window measured in days and is stated rather than hidden.

    **Both configured caps are enforced, and neither claims more than it can.**
    ``run_limits.max_questions`` bounds how many questions are attempted and ``--limit`` may
    only lower it. ``run_limits.max_cost_usd`` is checked against accumulated **known** cost
    before each question after the first -- a cap cannot be checked before the first spend,
    since nothing is known until something is bought.

    That word *known* is load-bearing and is the one place this function is weaker than it
    might look, so it is stated plainly: ``cost_usd is None`` means **unknown, never free**
    (M1-303 round 3), and unknown cost is counted in ``unpriced_calls`` rather than added as
    zero. It is not, however, treated as a reason to stop, and the reason is structural
    rather than a preference: the AskNews adapter records ``cost_usd: None`` on **every** run
    by design ("AskNews reports usage in credits, not currency, and no credit->USD rate is
    configured"), and ``forecast/generate.py`` publishes ``None`` for any model LiteLLM
    cannot price. A rule that stopped on unknown cost would therefore stop every batch after
    its first question and make ``--limit`` unusable. So the cap bounds the spend it can see,
    the rest is counted and reported, and this docstring is the claim: **``max_cost_usd``
    bounds known spend and cannot bound unknown spend.**

    Raises :class:`LiveRunError` for a refusal, always before the first billable call. Once
    the loop starts, nothing raises: each question's failure is its own outcome.
    """
    _require_live_configuration(config)
    if not isinstance(conn, sqlite3.Connection):
        raise LiveRunError("conn must be a sqlite3.Connection")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise LiveRunError("now must be a timezone-aware datetime")
    if type(refresh_research) is not bool:
        raise LiveRunError("refresh_research must be a bool")
    if question_id is not None and limit is not None:
        raise LiveRunError(
            "pass either --question-id or --limit, not both: one names a question and the "
            "other bounds how many are taken from the snapshot in order"
        )

    ceiling = _effective_limit(config, limit)
    available, tournament_id = _select_questions(snapshot, question_id=question_id)
    selected = available[:ceiling]
    try:
        prompt = load_prompt(config.forecast.prompt_path, config.forecast.prompt_version)
    except PromptError as exc:
        raise LiveRunError(str(exc)) from None
    forecaster_client, primary, fallback = _build_clients(
        config, news_client=news_client, web_client=web_client, forecaster=forecaster
    )

    budget = config.run_limits.max_cost_usd
    outcomes: list[QuestionOutcome] = []
    known_cost = 0.0
    unpriced = 0
    stop: StopReason = "question_limit" if len(selected) < len(available) else "completed"

    _LOGGER.info(
        "live run starting: %d question(s) of %d supported in the snapshot, budget %.2f USD",
        len(selected),
        len(available),
        budget,
    )
    for index, question in enumerate(selected):
        if index and known_cost >= budget:
            _LOGGER.warning(
                "stopping before question %d: %.4f USD of known spend has reached "
                "run_limits.max_cost_usd (%.2f)",
                question.question_id,
                known_cost,
                budget,
            )
            stop = "cost_limit"
            break
        outcome = _attempt_question(
            conn,
            config,
            question=question,
            tournament_id=tournament_id,
            prompt=prompt,
            now=now,
            refresh=refresh_research,
            news_client=primary,
            web_client=fallback,
            forecaster=forecaster_client,
        )
        outcomes.append(outcome)
        if outcome.cost_usd is not None:
            known_cost += outcome.cost_usd
        unpriced += outcome.unpriced_calls

    return BatchRun(
        outcomes=tuple(outcomes),
        stop_reason=stop,
        known_cost_usd=known_cost,
        unpriced_calls=unpriced,
    )
