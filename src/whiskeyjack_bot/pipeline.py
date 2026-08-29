"""One saved question, through replayed research and a saved model reply, to one record (T-903).

Every stage below this module already existed and none of them was reachable. ``M1-306``
shipped research replay, ``M1-406`` shipped model-output replay, ``M1-602`` shipped the
version writer, and ``M1-506`` shipped the composed output validation -- and
``generate_forecast``, ``persist_generation``, ``normalize_questions`` and
``lifecycle.record_validation`` had, between them, **no caller reachable from the CLI** --
the precise claim, because ``build_model_input`` does have one production caller in
``forecast/generate.py`` and "zero production callers" would have been false of it. Each item
said so in its own notes: ``research/persist.py`` that "the composed entry point belongs with
the retrieval orchestrator that item does not ship", ``docs/M1-NOTES.md`` that "the ``run`` /
``replay --record-id`` CLI wiring" was deferred and "this slice is library-only". This module
is that wiring, and it is what makes T-903's acceptance criterion a sentence about a command
rather than about a test harness:

    One command, one saved question, research + model replay -> one complete validated
    ledger record, zero provider calls, zero submission calls, reproducible forecast hash.

**Replay only, and that is the scope of the item rather than a limitation of the design.**
There is no live paid path here. What that buys is stated carefully below, because the
obvious stronger version of it is false and a test asserting it would have been vacuous.

*Zero submission calls is structural.* No submission module, no approval module and no
Metaculus poster is on this module's import graph, so there is no code here to post with.
``CODEX_HANDOFF.md`` § "Required CLI entry points" asks that ``run`` never submit implicitly;
a module with no submission code on its path is the strongest available form of that.

*Zero provider calls is structural at the layer that can spend, and no further.* None of
this project's own paid adapters is reachable -- not ``research.asknews``, ``research.exa``,
``forecast.generate`` or ``metaculus.client`` -- which is what "no call can be made" means
here, and it is what :func:`_select_question`'s comment is about. It is **not** true that no
provider SDK is imported: ``metaculus/snapshots.py`` and ``questions/normalize.py`` both
import ``forecasting_tools``, because a saved snapshot holds serialized SDK question objects
and normalization dispatches on their types, and that SDK in turn drags ``httpx``,
``openai``, ``litellm`` and ``asknews_sdk`` onto the graph transitively. So the forbidden-set
test that ``research/store.py`` can run -- where the SDK is genuinely absent -- cannot be
copied here, and writing one that named those packages would fail for a reason unrelated to
whether a call can happen, while one that omitted them would assert nothing. The check that
does bite is the whiskeyjack-layer one, plus the suite-wide socket block under which the
whole acceptance run executes.

The live ``run`` is **M1-315**, filed with this item, for the same reason M1-312 was separate
from M1-306: composing paid calls is a different risk from composing free ones, and that row
is also where ``--limit`` and the batch loop go.

**A saved reply is an input, not a lookup.** ``CODEX_HANDOFF.md`` § "Pipeline and failure
boundaries" writes the stage as ``retrieve/replay -> forecast/replay -> validate -> persist
draft``, so the saved model output enters this run the way the saved research packet does --
addressed by ``(question_id, attempt_id)``, before any record exists. That is
:func:`whiskeyjack_bot.forecast.replay.replay_generation`, and it is why
``replay_forecast``'s record-keyed reader could not be reused: that one verifies a row that
is already in the ledger.

**The rebuilt request is compared byte-for-byte against the stored one, and a difference is
a refusal.** This is the load-bearing check in the module. A saved reply is an answer to a
specific reasoning packet; replaying it against research it never saw would write an
attribution claim the reply does not support -- documents cited in a record the model was
never shown. So the packet is rebuilt from the replayed research, rendered through the same
``render_model_input`` the generating call used, and required to equal the stored request
exactly. ``as_of_utc`` is recovered from the stored request rather than taken from the clock
precisely so the comparison can be exact instead of approximate.

Approval and submission remain separate commands (D23).

Errors are :class:`PipelineError`, this module's own, and every underlying module's error
type is translated into it at the one boundary below -- ``ConfigError``, ``SnapshotError``,
``NormalizationError``, ``StoreError``, ``ForecastRecordError``, ``ForecastInputError``,
``ForecastSchemaError``, ``PromptError`` and ``LifecycleError`` all arrive as one type,
because a caller distinguishing "the snapshot is unreadable" from "the ledger refused the
row" would be handling a distinction the operator cannot act on differently. Messages never
echo stored, file or field values; filesystem paths are the settled M1-401 carve-out.

Purely local file I/O and SQLite: no network access on any path through here.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from whiskeyjack_bot.forecast.inputs import (
    ForecastInputError,
    build_model_input,
    render_model_input,
)
from whiskeyjack_bot.forecast.parse import ForecastGeneration, _classify, _parse
from whiskeyjack_bot.forecast.persist import ArtifactOutcome, persist_generation
from whiskeyjack_bot.forecast.record import (
    ForecastRecordError,
    build_forecast_record_draft,
    record_sha256,
)
from whiskeyjack_bot.forecast.replay import replay_generation
from whiskeyjack_bot.forecast.schema import ForecastSchemaError, response_model_for
from whiskeyjack_bot.forecast.store import mint_record_id
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    record_pre_forecast_failure,
    record_validation,
    transaction,
)
from whiskeyjack_bot.metaculus.snapshots import SnapshotError, load_snapshot
from whiskeyjack_bot.prompt import PromptError, load_prompt
from whiskeyjack_bot.questions.normalize import NormalizationError, normalize_questions
from whiskeyjack_bot.research.packet import packet_sha256
from whiskeyjack_bot.research.store import StoreError, list_retrieval_run_ids, replay_research

if TYPE_CHECKING:
    from whiskeyjack_bot.config import AppConfig
    from whiskeyjack_bot.forecast.artifacts import StoredModelOutput
    from whiskeyjack_bot.forecast.inputs import ModelInput
    from whiskeyjack_bot.questions.model import CanonicalQuestion
    from whiskeyjack_bot.research.packet import ResearchPacket

_LOGGER = logging.getLogger(__name__)


class PipelineError(Exception):
    """A replay run this module refused to attempt, or could not complete.

    Every message is a constant or names only a filesystem path: no question text, no
    document body, no model reply and no stored field value reaches it. Raised in place of
    every underlying module's own error type -- see the module docstring on why the
    distinction is not one a caller can act on.
    """


class ForecastRejected(PipelineError):
    """The saved reply was read, and did not survive parsing or output validation.

    Distinct from its base class because it is the one failure that is *about the forecast*
    rather than about the run: the artifact was found, the packet was rebuilt, the request
    matched, and the reply still produced no usable forecast. It is also the only failure
    this module records in the ledger -- see :func:`run_replay` -- so a caller needs to be
    able to tell "nothing was written" from "a ``generation_failed`` event was written".

    ``problems`` is ``forecast.schema``'s sanitized list: field paths and validator
    messages, safe to print and to store. It never carries the offending value.
    """

    def __init__(self, message: str, *, attempt_id: str, problems: tuple[str, ...]) -> None:
        super().__init__(message)
        self.attempt_id = attempt_id
        self.problems = problems


@dataclass(frozen=True)
class ReplayRun:
    """What one replay run wrote. Every field is read back off the persisted record.

    ``artifact_outcome`` rather than a bare optional path, and it is
    :mod:`whiskeyjack_bot.forecast.persist`'s three-member vocabulary carried through
    unchanged: M1-312's rule is that a run stays recorded when its artifact write fails, and
    ``retention_disabled`` and ``failed`` both leave ``raw_output_path`` NULL. Collapsing
    them into a boolean here would re-create exactly the ambiguity that item added the
    outcome to remove -- "the operator asked us not to keep it" is not "we tried and lost
    it". The constructor refuses any pairing that would misreport which happened.

    **A returned ``ReplayRun`` always carries ``"written"``**, because :func:`run_replay`
    refuses the other two rather than appending a record whose evidence is not on disk
    (round-1 finding 2). The field and the ``__post_init__`` pairing check stay anyway: the
    vocabulary is the persist layer's and this type reports it rather than re-deriving it,
    and a check that is currently unfalsifiable from outside is what makes the refusal above
    provable rather than assumed. M1-315's paid run is the caller that will legitimately see
    the other members.
    """

    record_id: str
    question_id: int
    tournament_id: str
    forecast_version: int
    attempt_id: str
    forecast_sha256: str
    research_packet_sha256: str
    retrieval_run_ids: tuple[str, ...]
    source_count: int
    raw_output_path: str | None
    artifact_outcome: ArtifactOutcome
    replayed_attempt_id: str

    def __post_init__(self) -> None:
        if (self.raw_output_path is not None) != (self.artifact_outcome == "written"):
            raise PipelineError(
                "a run records an artifact path exactly when the artifact was written"
            )


def _require_replay_enabled(config: AppConfig) -> None:
    """Both replay switches, checked together and before anything is read.

    ``retrieval.replay_saved_research`` and ``forecast.replay_saved_model_output`` are
    checked again inside ``replay_research`` and ``replay_generation``; this is the
    ``research/exa.py`` rule applied to a switch rather than a credential. A caller that
    reached the second gate having silently satisfied only the first would have loaded a
    snapshot and opened a ledger for a run that was never going to be permitted, and the
    committed default for both is ``false`` -- the pair is the operator's statement that
    this is a replay, and reading it once, up front, is what makes the refusal legible.
    """
    try:
        research = config.retrieval.replay_saved_research
        model_output = config.forecast.replay_saved_model_output
    except AttributeError:
        raise PipelineError("config must be an AppConfig") from None
    if type(research) is not bool or type(model_output) is not bool:
        raise PipelineError("config must be an AppConfig")
    if not research:
        raise PipelineError(
            "retrieval.replay_saved_research is disabled; refusing to replay saved research"
        )
    if not model_output:
        raise PipelineError(
            "forecast.replay_saved_model_output is disabled; refusing to replay saved model output"
        )


def _require_retained_output(config: AppConfig) -> None:
    """Refuse a replay whose record could not be replayed in turn (round-1 finding 2).

    ``storage.retain_raw_model_output`` defaults to ``True`` and the validator accepts
    ``False``. With it off, :func:`persist_generation` returns ``artifact_outcome=
    "retention_disabled"`` and appends the row with a NULL ``raw_output_path`` -- which is
    the right behaviour *there*, and deliberately so: that is M1-312's rule, and it is the
    inversion of the refuse-before-billing rule, which only holds before the spend. A paid
    attempt's cost and invocation count are facts whether or not the evidence survived, so
    the row is written regardless and M1-315 will want exactly that.

    This command is the other case. Nothing has been spent, and the record it exists to
    produce is one that ``replay --record-id`` can re-derive a hash from -- which it cannot
    do without the artifact the record's own completeness test requires. So the strictness
    lives here rather than in the writer: refusing in ``persist_generation`` would take
    M1-312's decision away from the paid path that needs it.

    Checked up front, before the snapshot is loaded, for :func:`_require_replay_enabled`'s
    reason: a refusal an operator can act on is one that arrives before the work, not one
    that arrives after a row was nearly appended.
    """
    try:
        retain = config.storage.retain_raw_model_output
    except AttributeError:
        raise PipelineError("config must be an AppConfig") from None
    if type(retain) is not bool:
        raise PipelineError("config must be an AppConfig")
    if not retain:
        raise PipelineError(
            "storage.retain_raw_model_output is disabled; this command would append a "
            "record no replay could re-derive, so it refuses instead"
        )


def _select_question(snapshot: Path, question_id: int) -> tuple[CanonicalQuestion, str]:
    """Load the snapshot, normalize it, and return the one question asked for.

    Returns the snapshot's tournament id alongside it: the two come from one file and
    reading them separately would let a run attribute a question to a tournament the
    snapshot does not place it in.

    A deferred question (M1-203) is not an error for the batch and is not a match either --
    it has no canonical model, so it cannot be forecast and it is reported as "not found"
    with the deferrals logged. Ids are not rendered in the message: a question id is row
    content, M1-202's precedent.

    **Reads the snapshot with ``load_snapshot``, not with
    ``metaculus.fetch.fetch_open_questions_fixture``**, and the difference is the module
    docstring's second zero rather than a style preference. That function is
    ``load_snapshot`` plus one log line, but it lives beside the live fetcher and so imports
    ``metaculus.client`` -- meaning a ``run`` that called it would load the live Metaculus
    API client onto the path of the one command whose claim is that no client of ours is
    reachable from it. It was a deferred, function-local import, which is worse: the
    module-level graph would have looked clean while the executed path pulled the client in,
    and the test asserting the claim would have passed without ever touching the code that
    breaks it. That is this project's most expensive defect class -- an assertion that cannot
    fail for the thing it names -- so the import is at module scope and the log line is here.
    """
    if type(question_id) is not int:
        raise PipelineError("question_id must be an int")
    if not isinstance(snapshot, Path):
        raise PipelineError("snapshot must be a Path")
    try:
        meta, loaded = load_snapshot(snapshot)
    except SnapshotError as exc:
        # Message preserved: SnapshotError names no content, and the path it does name is
        # the only thing that makes an unreadable snapshot actionable (M1-401 carve-out).
        raise PipelineError(str(exc)) from None
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
        raise PipelineError(str(exc)) from None
    for event in result.deferrals:
        _LOGGER.info("snapshot question deferred: %s", event.reason)

    tournament_id = str(meta.tournament_id)
    if not tournament_id.strip():
        raise PipelineError("the snapshot records a blank tournament id")
    for question in result.questions:
        if question.question_id == question_id:
            return question, tournament_id
    raise PipelineError(
        "the snapshot holds no supported question with that id (offending value withheld)"
    )


def _replayed_packet(
    conn: sqlite3.Connection, config: AppConfig, *, question_id: int
) -> tuple[ResearchPacket, tuple[str, ...]]:
    """Every completed run stored for the question, replayed as one packet.

    Discovery and assembly are separate calls because ``research/store.py`` separated them
    on purpose -- a packet built from "every row currently sharing a question" has no stable
    identity. The ids are returned with the packet so the run can report exactly which runs
    it read, which is what makes the packet reproducible afterwards.

    An empty set is refused here rather than passed on: ``replay_research`` would refuse it
    too, but its message is about a packet and this one can say the actionable thing, which
    is that the ledger holds no completed research for the question.
    """
    try:
        run_ids = list_retrieval_run_ids(conn, question_id=question_id)
        if not run_ids:
            raise PipelineError(
                "the ledger holds no completed research run for that question; there is "
                "nothing to replay (offending value withheld)"
            )
        packet = replay_research(conn, config, question_id=question_id, retrieval_run_ids=run_ids)
    except StoreError as exc:
        raise PipelineError(str(exc)) from None
    return packet, run_ids


def _as_of_from_request(request: str) -> datetime:
    """Recover the ``as_of_utc`` the stored request was rendered with.

    The alternative was to compare the rebuilt request to the stored one with ``as_of_utc``
    excluded, and that is strictly worse: it turns an exact equality into a hand-maintained
    list of fields that are allowed to differ, and every future field added to
    ``ForecastModelInput`` would join that list by default rather than by decision.
    Recovering the one value that provably cannot be re-derived keeps the comparison total.

    Never raises anything but :class:`PipelineError`, and never renders the request: it is
    the rendered reasoning packet and carries the question text and every document.
    """
    if type(request) is not str:
        raise PipelineError("the stored request is not text")
    try:
        payload = json.loads(request)
    except Exception:
        # Broad and scoped to the one call, the M1-308 round-7 rule: json.loads raises
        # more than JSONDecodeError once the input is not what it should be.
        raise PipelineError("the stored request is not valid JSON") from None
    if not isinstance(payload, dict):
        raise PipelineError("the stored request is not a JSON object")
    raw = payload.get("as_of_utc")
    if type(raw) is not str:
        raise PipelineError("the stored request records no as_of_utc timestamp")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise PipelineError("the stored request's as_of_utc is not an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise PipelineError("the stored request's as_of_utc is not UTC")
    return parsed


def _require_settings_agree(stored: StoredModelOutput, config: AppConfig) -> None:
    """Refuse a saved reply produced under a different model or a different prompt.

    Four fields, and deliberately not the other four. ``provider``, ``name``,
    ``prompt_version`` and ``prompt_sha256`` become NOT NULL columns on the record -- they
    are the attribution claim that a particular model, reading a particular prompt, produced
    this forecast -- so a run that persisted them from an artifact the current configuration
    contradicts would write a claim nothing in the ledger could later detect.

    ``temperature``, ``max_output_tokens``, ``timeout_seconds`` and ``allowed_tries`` are
    call parameters. They shaped the reply when it was made and cannot change what it says
    now that it exists, so requiring them to agree would refuse a replay for a reason that
    provably cannot affect the answer. The record still stores the artifact's values for all
    eight, because those are what the call was actually made with.

    ``prompt_sha256`` is recomputed from the file on disk rather than trusted from config,
    which is the check that catches an edited prompt: the version can stay ``1.1.0`` while
    the bytes change, and M1-401 exists because those are four different hashes.
    """
    try:
        prompt = load_prompt(config.forecast.prompt_path, config.forecast.prompt_version)
    except PromptError as exc:
        raise PipelineError(str(exc)) from None
    if (
        stored.settings.provider != config.model.provider
        or stored.settings.name != config.model.name
    ):
        raise PipelineError(
            "the saved reply was produced by a different model than model.provider/model.name "
            "names; refusing to attribute it to the configured one (values withheld)"
        )
    if stored.settings.prompt_version != prompt.version:
        raise PipelineError(
            "the saved reply was produced under a different forecaster prompt version "
            "(values withheld)"
        )
    if stored.settings.prompt_sha256 != prompt.sha256:
        raise PipelineError(
            "the forecaster prompt on disk is not the one that produced the saved reply; "
            "its version is unchanged but its bytes are not (hashes withheld)"
        )


def _rebuilt_input(
    *,
    question: CanonicalQuestion,
    packet: ResearchPacket,
    tournament_id: str,
    stored: StoredModelOutput,
) -> ModelInput:
    """Rebuild the reasoning packet and require it to render to the stored bytes exactly.

    The module docstring says why this is the load-bearing check. Two things fall out of it
    being an equality rather than a subset test: the ``source_ids`` the reply cites are
    provably the ones ``forecast.inputs`` minted for *this* packet, so the attribution rules
    ``_parse`` runs next are checking real citations; and the replayed research is provably
    the research the model saw, so ``research_packet_sha256`` on the record is a true claim
    rather than an assumption.
    """
    as_of = _as_of_from_request(stored.request)
    try:
        model_input = build_model_input(
            question=question, packet=packet, tournament_id=tournament_id, as_of=as_of
        )
        rendered = render_model_input(model_input)
    except ForecastInputError as exc:
        raise PipelineError(str(exc)) from None
    if rendered != stored.request:
        raise PipelineError(
            "the reasoning packet rebuilt from the replayed research does not match the "
            "one the saved reply answered; refusing to attribute that reply to this "
            "research (rendered packets withheld: they carry question and document text)"
        )
    return model_input


def run_replay(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    question_id: int,
    attempt_id: str,
    snapshot: Path,
    now: datetime,
) -> ReplayRun:
    """Run one saved question through replayed research and a saved reply. No API calls.

    ``attempt_id`` names the *saved* attempt whose reply is replayed. The record this writes
    is stamped with a **freshly minted** attempt id instead, and that is a decision rather
    than an oversight: ``idx_forecast_records_attempt_id`` is a partial unique index and
    ``004``'s trigger cross-checks ``pipeline_failure_events``, so reusing the saved id would
    collide the moment that attempt also has a record -- which is the ordinary case, since
    the artifact usually came from a run that produced one. Minting keeps "every record names
    its own artifact" true, at the cost of ``persist_generation`` writing a second copy of the
    reply under the new id. That copy is honest: this run is a new attempt, and what it
    consumed is what it stores. ``replayed_attempt_id`` on the result records where it came
    from.

    ``now`` is caller-supplied rather than read from the clock, matching ``lifecycle``'s
    ``occurred_at`` parameters and ``build_forecast_record_draft``'s ``generated_at``. It
    becomes the record's ``generated_at_utc`` and the lifecycle event's ``occurred_at``, and
    it is deliberately *not* the request's ``as_of_utc`` -- that is the model's claim about
    the world, this is the pipeline's record of when the run happened.

    **The only failure written to the ledger is a reply that produced no forecast**, as a
    ``generation_failed`` pipeline event (M1-606), because that is the only one where the
    pipeline actually attempted something and it did not work. Every other refusal here --
    replay disabled, no such question, no completed research, a missing artifact, a model or
    prompt that disagrees, a request that does not match -- writes nothing, because none of
    them is a fact about a forecast attempt and recording one would put a failure in the
    ledger that never happened.

    Raises :class:`PipelineError`, or :class:`ForecastRejected` for that one recorded case.
    """
    _require_replay_enabled(config)
    _require_retained_output(config)
    if type(attempt_id) is not str:
        raise PipelineError("attempt_id must be a string")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise PipelineError("now must be a timezone-aware datetime")

    question, tournament_id = _select_question(snapshot, question_id)
    packet, run_ids = _replayed_packet(conn, config, question_id=question_id)

    try:
        stored = replay_generation(config, question_id=question_id, attempt_id=attempt_id)
    except ForecastRecordError as exc:
        raise PipelineError(str(exc)) from None

    _require_settings_agree(stored, config)
    model_input = _rebuilt_input(
        question=question, packet=packet, tournament_id=tournament_id, stored=stored
    )
    source_ids = tuple(source.source_id for source in model_input.sources)

    try:
        model = response_model_for(question.qtype)
        # `_parse` runs `forecast.validate.output_problems` -- M1-506's composed entry
        # point -- inside itself, so this single call *is* the validation stage: the
        # cross-type attribution rules and the type-specific checks both run here, against
        # the configured bounds in force right now. A second `validate_output` pass over the
        # result would re-run the identical checks and could only ever agree.
        forecast, problems = _parse(
            stored.raw_responses[-1],
            model,
            config.forecast,
            question=question,
            source_ids=source_ids,
        )
    except ForecastSchemaError as exc:
        # `forecast/replay.py` takes this same handler for the same reason: `_parse` reaches
        # the type-specific checkers, and those refuse a *question* they cannot check a
        # forecast against -- numeric's is a `zero_point` at or above `lower_bound`, which
        # `CanonicalNumericQuestion` accepts. `generate_forecast` refuses that in a preflight
        # this path does not have, and `_parse`'s own docstring says a caller added later
        # owes the same handler.
        raise PipelineError(str(exc)) from None

    minted = mint_record_id()
    if forecast is None:
        detail = _classify(problems)
        try:
            record_pre_forecast_failure(
                conn,
                attempt_id=minted,
                question_id=question_id,
                tournament_id=tournament_id,
                event_type="generation_failed",
                detail_code=detail,
                occurred_at=now,
                retrieval_run_id=run_ids[0],
            )
        except LifecycleError as exc:
            raise PipelineError(str(exc)) from None
        raise ForecastRejected(
            "the saved reply produced no usable forecast; the failure is recorded",
            attempt_id=minted,
            problems=tuple(problems),
        )

    generation = ForecastGeneration(
        forecast=forecast,
        settings=stored.settings,
        sources=model_input.sources,
        request=stored.request,
        raw_responses=stored.raw_responses,
        invocations=stored.invocations,
        repair_attempted=stored.repair_attempted,
        cost_usd=stored.cost_usd,
        failure_code=None,
        failure_problems=(),
    )
    try:
        draft = build_forecast_record_draft(
            question=question,
            generation=generation,
            tournament_id=tournament_id,
            attempt_id=minted,
            # The packet's first run, and the ids come back oldest-first. The column is a
            # single FK while a packet may carry many runs; what pins the *whole* packet is
            # `research_packet_sha256`, which is a record field and therefore inside
            # `forecast_sha256`. Widening a merged, reviewed schema is not this item's, and
            # the tension is filed as its own row rather than fixed sideways.
            retrieval_run_id=run_ids[0],
            research_packet_sha256=packet_sha256(packet),
            generated_at=now,
        )
    except ForecastRecordError as exc:
        raise PipelineError(str(exc)) from None

    # One unit: the row, its artifact and its validation event, or none of them (round-1
    # finding 1). The first draft of this committed the row and then opened a second
    # transaction for the event, and argued that a draft with no validation event was a
    # legible state the ledger could hold because `003` blocks UPDATE and DELETE on an
    # appended row. That argument conflated two different things. Append-only forbids
    # *mutating a row that exists*; it says nothing about whether a row should have been
    # appended in the first place, and an insert that never commits is not a mutation. So an
    # ordinary local failure -- a busy timeout, a full disk -- between the two writes left a
    # permanent orphan draft, and the retry appended v2 beside it rather than completing v1.
    # `forecast/store.py` says so itself: `lifecycle.transaction` nests as a SAVEPOINT
    # precisely "so a caller that wants the record and its first event in one unit can have
    # it without this module deciding that on its behalf". This is that caller.
    try:
        with transaction(conn):
            persisted = persist_generation(
                conn, config, draft=draft, generation=generation, written_at=now
            )
            record = persisted.record
            if record is None:  # pragma: no cover - persist_generation raises instead
                raise PipelineError("the forecast version was not appended")
            if persisted.artifact_outcome != "written":
                # Inside the transaction on purpose: raising here rolls the row back. The
                # artifact write happens before the insert, so what survives is at worst an
                # orphaned file, which is harmless under `forecast/persist.py`'s convention
                # and is the trace that the attempt happened. What must not survive is the
                # inverse -- a record claiming a replayable forecast whose evidence is not
                # on disk. `_require_retained_output` catches the configured case up front;
                # this catches a write that was permitted and then failed.
                why = persisted.artifact_error
                raise PipelineError(
                    "the raw model output artifact was not written, so this record could "
                    "not be replayed; nothing was appended" + (f" ({why})" if why else "")
                )
            record_validation(conn, record_id=record.record_id, occurred_at=now)
    except (ForecastRecordError, LifecycleError) as exc:
        raise PipelineError(str(exc)) from None

    return ReplayRun(
        record_id=record.record_id,
        question_id=record.question_id,
        tournament_id=record.tournament_id,
        forecast_version=record.forecast_version,
        attempt_id=record.attempt_id,
        forecast_sha256=record_sha256(record),
        research_packet_sha256=record.research_packet_sha256,
        retrieval_run_ids=run_ids,
        source_count=len(model_input.sources),
        raw_output_path=persisted.raw_output_path,
        artifact_outcome=persisted.artifact_outcome,
        replayed_attempt_id=attempt_id,
    )
