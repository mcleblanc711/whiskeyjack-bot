"""One question's paid retrieval: primary, fallback, and the rows that record both (M1-315).

M1-306 shipped persistence and replay and deliberately shipped no orchestration -- "the
cross-provider orchestration that ``docs/M1-303-NOTES.md`` assigns here ... is a follow-up
row, not this branch", because "it is a paid-call policy surface and does not belong in a
persistence review". No row was ever filed, so M1-315 absorbs it: a live run cannot exist
without it, and every primitive it needs has been merged and reviewed for weeks with no
production caller.

**Every call into this module spends money.** That is the module's boundary and the reason
it is separate from :mod:`whiskeyjack_bot.pipeline_live`: deciding *whether* research is
needed is the run composer's job, and by the time control reaches here the decision has
been made. Nothing here consults a cache, and there is deliberately no "skip if we already
have some" branch -- a function that sometimes bills and sometimes does not is one whose
callers stop knowing which happened.

The shape follows from *when* each failure can occur, which is M1-312's argument one layer
up:

- **Before the spend, a caller mistake is refused** (M1-303 round 4). ``derive_queries``
  and the run metadata are checked first, and the adapters check them again at their own
  boundary, because a value carries no memory of which caller validated it.
- **The run row is opened before the first billable call** (:func:`store.open_run`). That
  is what the two-phase shape in ``research/store.py`` exists for and nothing had used it:
  if the process dies mid-call, the ledger still says money was spent on this question.
- **After the spend, nothing refuses.** A provider that fails, an artifact that cannot be
  written, a fallback whose credential is missing -- each is recorded and reported, never
  raised, because the calls are already paid for and raising would discard the record of
  the spend. :func:`persist_paid_run` already owns the artifact-first/ledger-regardless
  half of that; this module owns the provider-sequencing half.

**The fallback is never silent.** :func:`exa.decide_fallback` is the only thing that can
authorize an Exa call, its reasons are passed through to :func:`exa.retrieve_web`
unmodified, and they are persisted on the run. This module adds no trigger of its own.

``official_source_required`` is passed ``False``: v1 config has no field that expresses a
per-question official-source requirement, and inventing one here would put a paid-call
trigger in a module that cannot say where it came from. M1-304's structured-source router
is where that flag is supposed to originate; filed rather than guessed.

Errors are :class:`OrchestrationError`, and every underlying module's type arrives as one
at the boundaries below -- ``AskNewsRetrievalError``, ``ExaFallbackError``, ``StoreError``,
``ResearchSchemaError`` and ``MissingCredentialError``. Messages never echo a question
title, a query, a document or a provider body; filesystem paths reach the result object
through :func:`persist_paid_run`'s already-sanitized report and are the settled M1-401
carve-out.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.metaculus.client import MissingCredentialError
from whiskeyjack_bot.questions.model import _CanonicalQuestionBase
from whiskeyjack_bot.research.asknews import (
    AskNewsRetrieval,
    AskNewsRetrievalError,
    build_asknews_client,
    retrieve_news,
)
from whiskeyjack_bot.research.exa import (
    ExaFallbackError,
    ExaRetrieval,
    build_exa_client,
    decide_fallback,
    retrieve_web,
)
from whiskeyjack_bot.research.model import ResearchSchemaError, validate_run
from whiskeyjack_bot.research.persist import ArtifactOutcome, persist_paid_run
from whiskeyjack_bot.research.preflight import require_run_metadata
from whiskeyjack_bot.research.store import (
    StoreError,
    load_packet,
    open_run,
    with_retrieval_counts,
)

if TYPE_CHECKING:
    from whiskeyjack_bot.questions.model import CanonicalQuestion
    from whiskeyjack_bot.research.exa import FallbackReason
    from whiskeyjack_bot.research.model import ResearchDocument, ResearchRun
    from whiskeyjack_bot.research.packet import ResearchPacket

_LOGGER = logging.getLogger(__name__)

# Minted here because nothing in the tree minted one: both adapters *take* a
# ``retrieval_run_id`` and neither invents it, so the first composed caller owes it. The
# shape is ``submission.py``'s (`_RESERVATION_PREFIX + uuid4().hex`) and it satisfies
# ``artifacts._SAFE_COMPONENT_RE`` by construction -- a run id becomes a path component,
# and M1-312 records what an id that does not (``run/1``) costs: a lost artifact.
_RUN_ID_PREFIX = "run-"


class OrchestrationError(Exception):
    """Paid retrieval this module refused to attempt.

    Raised **before any billable call**, with one named exception: its subclass
    :class:`PaidRetrievalError`, which reports a ledger write that failed after the money
    was gone. Apart from that, once a provider has been invoked every failure is reported
    on :class:`RetrievalOutcome` instead, because the money is spent either way and an
    exception would discard the record of it.

    A caller that catches this type catches both, which is the point of the subclass: the
    batch must not abort either way, and only a caller that wants the spend figures needs
    to tell them apart.

    Messages are constants or name a filesystem path; no question text, query, document or
    provider body reaches one.
    """


class PaidRetrievalError(OrchestrationError):
    """A ledger failure that happened **after** the calls were billed (M1-315 round 1).

    A subclass rather than a flag, because the composer above must be able to tell two
    things apart that :class:`OrchestrationError` alone collapses:

    - *a caller mistake, refused before the first billable call* -- nothing was spent, no
      ``research_runs`` row exists, and the question's failure event cites no run; and
    - *a ledger that refused a write after the money was gone* -- a row was opened, a
      provider was billed, and both facts have to survive into the batch's accounting or
      the run's cost is understated and the failure event cites nothing.

    Round 1 of this item's review found the second case escaping the batch entirely: the
    composer caught only ``OrchestrationError`` around the research phase while
    :func:`_record` propagated a raw ``StoreError``, so an ordinary transient SQLite write
    failure after AskNews had returned aborted the whole run and wrote no event. Converting
    here rather than widening the caller's ``except`` is what this project's error-hygiene
    rule asks for -- a caller handles the module's own error type -- and it is also the only
    place that still knows which runs were opened and what they cost.

    ``retrieval_run_ids`` is never empty: the primary run row is opened before the first
    billable call, so there is always at least one row that names this question and says
    money was spent on it. ``cost_usd`` and ``unpriced_calls`` keep M1-303 round 3's
    distinction -- ``None`` is *unknown, never free*. A fallback pass whose own recording
    failed contributes no figure, because its cost never reached this frame; that is stated
    rather than guessed at, and with both committed providers reporting no currency figure
    at all it costs nothing today.
    """

    def __init__(
        self,
        message: str,
        *,
        retrieval_run_ids: tuple[str, ...],
        cost_usd: float | None,
        unpriced_calls: int,
    ) -> None:
        super().__init__(message)
        self.retrieval_run_ids = retrieval_run_ids
        self.cost_usd = cost_usd
        self.unpriced_calls = unpriced_calls


@dataclass(frozen=True)
class ProviderRun:
    """One provider pass, as it was recorded.

    ``artifact_outcome`` is :mod:`whiskeyjack_bot.research.persist`'s three-member
    vocabulary carried through unchanged rather than collapsed to a bool: M1-312 added the
    third member precisely so an auditor can tell "the operator asked us not to keep it"
    from "we tried and lost it", and re-flattening it here would undo that one layer up.
    """

    retrieval_run_id: str
    provider: str
    documents_retained: int
    provider_failed: bool
    artifact_outcome: ArtifactOutcome
    artifact_error: str | None
    cost_usd: float | None
    fallback_reasons: tuple[FallbackReason, ...]

    def __post_init__(self) -> None:
        if (self.artifact_error is not None) != (self.artifact_outcome == "failed"):
            raise OrchestrationError("a failed artifact write reports its error and no other does")


@dataclass(frozen=True)
class RetrievalOutcome:
    """What one question's paid retrieval retrieved, and what it cost to find out.

    ``packet`` is ``None`` exactly when no run retained a document. That is not an error
    here -- a question with no evidence is a fact about the question, and the composer
    above turns it into a ``research_failed`` event -- but it is why the packet is optional
    rather than empty: :class:`ResearchPacket` would accept an empty document tuple, and an
    empty packet is indistinguishable from research that found nothing, which is the exact
    ambiguity ``replay_research`` refuses to return.

    ``cost_usd`` sums only the runs that reported a figure, and ``unpriced_runs`` counts
    the ones that did not. They are two fields rather than one optional total because
    ``None`` means *unknown, never free* (M1-303 round 3) and a single optional total would
    force a caller to choose between "no cost" and "no answer". The pair lets a caller say
    the true thing: this much is known to have been spent, across this many priced calls,
    with this many unpriced ones alongside.
    """

    question_id: int
    packet: ResearchPacket | None
    retrieval_run_ids: tuple[str, ...]
    runs: tuple[ProviderRun, ...]
    document_count: int
    cost_usd: float | None
    unpriced_runs: int

    def __post_init__(self) -> None:
        if (self.packet is None) != (self.document_count == 0):
            raise OrchestrationError("a packet is returned exactly when a document was retained")
        if len(self.retrieval_run_ids) != len(self.runs):
            raise OrchestrationError("every recorded run is named in retrieval_run_ids")

    @property
    def provider_failed(self) -> bool:
        """True when the *last* provider to run failed, i.e. nothing recovered after it."""
        return bool(self.runs) and self.runs[-1].provider_failed


def _mint_run_id() -> str:
    return _RUN_ID_PREFIX + uuid.uuid4().hex


def _require_storable(text: str, message: str) -> str:
    """Keep this function total against text SQLite cannot bind.

    **This is a totality backstop, not a live defence, and the distinction is worth being
    exact about because the first version of this docstring got it wrong.** The claim it
    made was that a lone surrogate reaches a question title from a snapshot file, since
    ``"\\ud800"`` is valid JSON and decodes to one. The first half is true and the
    conclusion is false: pydantic's ``str`` refuses a lone surrogate outright
    (``string_unicode``), so ``CanonicalQuestion`` cannot hold one and the *validated* path
    can never reach this check. The property suite found that by generating one and failing
    to build the question -- which is the good outcome, and the reason the property strategy
    is built through the real model rather than a stub.

    What is left is the contract: :func:`derive_queries` is public and promises that only
    :class:`OrchestrationError` escapes it, for any object that satisfies its type gate. An
    object from ``model_construct`` or ``model_copy(update=...)`` satisfies that gate and
    skips validation, so without this the promise would be false for one input class -- and
    the failure would be a raw ``UnicodeEncodeError`` from the sqlite3 binding layer, which
    *quotes the offending character*, after every call in the run had been paid for.

    Cheap, total, and honestly labelled. It is not evidence that the condition is reachable.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise OrchestrationError(message) from None
    return text


def derive_queries(question: CanonicalQuestion) -> tuple[str, ...]:
    """The retrieval queries for one question. Pure, deterministic, and deliberately few.

    **Minimal on purpose.** ``retrieval.max_queries_per_question`` is 6 and AskNews bills
    two calls per query, so query construction is the largest single lever on what a run
    costs. The alternatives were weighed and rejected: asking the model to expand the
    question into search queries spends a billable call to decide how to spend billable
    calls, and hand-written keyword heuristics over a question title are guesses this
    project cannot evaluate offline. What is left is what the question actually says.

    So: the title, and -- when the question is a group sibling (M1-202) -- the parent title
    joined to it, *first*, because unpacking leaves sibling titles like "Democratic" whose
    meaning lives entirely in the parent. Both are emitted when they differ, since the
    combined form is the more complete question and the bare title is the more precise
    search term.

    Richer construction is filed as its own row rather than invented here. The adapters
    apply ``max_queries_per_question`` themselves, so this returns everything it derived
    and does not read config -- which is what keeps it a pure function worth a property
    pass.
    """
    if not isinstance(question, _CanonicalQuestionBase):
        raise OrchestrationError("question must be a canonical question")
    title = " ".join(question.title.split())
    if not title:
        # `CanonicalQuestion.title` is `min_length=1`, which a lone space satisfies.
        raise OrchestrationError("the question has no title to search on")
    _require_storable(title, "the question title cannot be stored (offending value withheld)")

    queries: list[str] = []
    parent = question.group_parent_title
    if parent is not None:
        collapsed = " ".join(parent.split())
        if collapsed:
            _require_storable(
                collapsed, "the group parent title cannot be stored (offending value withheld)"
            )
            queries.append(f"{collapsed} {title}")
    if title not in queries:
        queries.append(title)
    return tuple(queries)


def _opening_run(
    *, retrieval_run_id: str, question_id: int, provider: str, started_at: datetime
) -> ResearchRun:
    """The row written before the first billable call.

    Carries exactly the four columns :func:`store.complete_run` matches on, and nothing
    else: ``queries``, ``provider_config`` and the counters are the adapter's to report,
    and writing a guess at them now would mean the completed row silently replaced a claim
    this module made up.
    """
    try:
        return validate_run(
            {
                "retrieval_run_id": retrieval_run_id,
                "question_id": question_id,
                "provider": provider,
                "started_at_utc": started_at,
            }
        )
    except ResearchSchemaError as exc:
        raise OrchestrationError(str(exc)) from None


def _record(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    run: ResearchRun,
    documents: tuple[ResearchDocument, ...],
    raw_responses: tuple[dict[str, Any], ...],
    documents_dropped: int,
    duplicates_collapsed: int,
    provider_failed: bool,
    fallback_reasons: tuple[FallbackReason, ...],
    written_at: datetime,
) -> ProviderRun:
    """Complete the opened run and file its artifact. Never raises for an artifact loss.

    A :class:`StoreError` *does* propagate, and that asymmetry is deliberate: an artifact
    loss costs the evidence copy while the run stays recorded, whereas a ledger refusal
    means the run was never recorded at all. The first degrades because M1-312 says the
    spend must stay on the books; the second cannot degrade, because there is nothing left
    to degrade *onto*.

    It propagates only as far as :func:`retrieve_for_question`, which is this module's
    public boundary and converts it to :class:`PaidRetrievalError`. That conversion is what
    makes "the caller turns it into a per-question failure without aborting the batch" true;
    before round 1 this docstring asserted that outcome while the raw ``StoreError`` escaped
    a caller that could not catch it.
    """
    counted = with_retrieval_counts(
        run, documents_dropped=documents_dropped, duplicates_collapsed=duplicates_collapsed
    )
    persistence = persist_paid_run(
        conn,
        config,
        counted,
        documents,
        raw_responses=raw_responses,
        written_at_utc=written_at,
        run_opened=True,
    )
    _LOGGER.info(
        "recorded %s run %s for question %d: %d document(s), artifact %s",
        counted.provider,
        counted.retrieval_run_id,
        counted.question_id,
        len(persistence.document_ids),
        persistence.artifact_outcome,
    )
    return ProviderRun(
        retrieval_run_id=counted.retrieval_run_id,
        provider=counted.provider,
        documents_retained=len(persistence.document_ids),
        provider_failed=provider_failed,
        artifact_outcome=persistence.artifact_outcome,
        artifact_error=persistence.artifact_error,
        cost_usd=counted.cost_usd,
        fallback_reasons=fallback_reasons,
    )


def _asknews_client(config: AppConfig, injected: Any | None) -> Any:
    """The primary client, built here when one was not supplied. Before any spend."""
    if injected is not None:
        return injected
    try:
        return build_asknews_client(config)
    except MissingCredentialError as exc:
        raise OrchestrationError(str(exc)) from None
    except AskNewsRetrievalError as exc:
        raise OrchestrationError(str(exc)) from None


def _fallback_pass(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    question_id: int,
    queries: tuple[str, ...],
    reasons: tuple[FallbackReason, ...],
    now_utc: datetime,
    injected: Any | None,
) -> ProviderRun | None:
    """Run the Exa fallback, or report why it could not run. Never raises for that.

    Everything in here happens **after** the primary provider has been billed, so the
    refuse-before-billing rule has already been spent and its inverse applies: a fallback
    that cannot run leaves the question with the evidence the primary found, which is worse
    than having both and strictly better than discarding the primary run as well. A missing
    ``EXA_API_KEY`` is the ordinary case -- the fallback is optional and an operator may
    never have configured one -- so it is logged at INFO and reported, not raised.
    """
    run_id = _mint_run_id()
    try:
        client = injected if injected is not None else build_exa_client(config)
    except (MissingCredentialError, ExaFallbackError) as exc:
        _LOGGER.info("fallback retrieval unavailable, keeping the primary run: %s", exc)
        return None

    opening = _opening_run(
        retrieval_run_id=run_id,
        question_id=question_id,
        provider="exa",
        started_at=now_utc,
    )
    open_run(conn, opening)
    try:
        retrieval: ExaRetrieval = retrieve_web(
            client,
            config,
            question_id=question_id,
            queries=list(queries),
            retrieval_run_id=run_id,
            now=now_utc,
            fallback_reasons=list(reasons),
        )
    except ExaFallbackError as exc:
        # `retrieve_web` refuses before its own network use, so nothing was billed *here*
        # -- but the primary run above was, and the opened row is now a run that will never
        # complete. That is the honest state: it records that a fallback was attempted and
        # did not happen, which is exactly what an open row means.
        _LOGGER.warning("fallback retrieval refused the call, keeping the primary run: %s", exc)
        return None
    return _record(
        conn,
        config,
        run=retrieval.run,
        documents=retrieval.documents,
        raw_responses=retrieval.raw_responses,
        documents_dropped=retrieval.documents_dropped,
        duplicates_collapsed=retrieval.duplicates_collapsed,
        provider_failed=retrieval.provider_failed,
        fallback_reasons=retrieval.fallback_reasons,
        written_at=now_utc,
    )


def retrieve_for_question(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    question: CanonicalQuestion,
    now: datetime,
    news_client: Any | None = None,
    web_client: Any | None = None,
) -> RetrievalOutcome:
    """Retrieve evidence for one question, live and paid, and record every call.

    **This function always spends.** Deciding whether it should be called is
    :mod:`whiskeyjack_bot.pipeline_live`'s job; see the module docstring.

    ``now`` is normalized to UTC once, by :func:`preflight.require_run_metadata`, and that
    value is what the opened row, the adapter and the artifact all carry. **The reason is
    the preflight, not the arithmetic, and the difference is worth stating because the first
    version of this docstring claimed the arithmetic.** That claim was: since
    :func:`store.complete_run` matches an opened row on
    ``(question_id, provider, started_at_utc)`` and the adapters normalize ``now``
    themselves, a non-UTC ``now`` normalized twice would produce two different stored texts
    and the completion would match no row after the calls were billed. It cannot.
    ``ResearchRun.started_at_utc`` is ``UtcDatetime``, an ``AwareDatetime`` with
    ``AfterValidator(_to_utc)``, so **the model normalizes every spelling to the same
    instant** and the two agree however many times the value is converted. A mutation
    handing the adapter the raw ``now`` survived the suite, which is how that was found.

    What calling it here does buy, and is the actual reason: ``require_run_metadata`` is the
    *preflight* -- it refuses a naive ``now``, a non-exact-``int`` question id and a ``now``
    so early that the freshness subtraction overflows -- and it must run before the run id
    reaches a billable call, not inside the adapter after this function has already opened a
    row. Reusing its return value is then simply not throwing away a value already computed.

    Raises :class:`OrchestrationError` for a caller mistake, always before the first
    billable call. After that the only thing that still raises is the ledger, as
    :class:`PaidRetrievalError` carrying the runs opened and the spend known -- everything
    else is reported on the result. Round 1 of this item found that region propagating a
    raw ``StoreError``, which aborted the composer's batch; the conversion is here rather
    than in the caller because this is the frame that still knows what was bought.
    """
    if not isinstance(config, AppConfig):
        raise OrchestrationError("config must be an AppConfig")
    if not isinstance(conn, sqlite3.Connection):
        raise OrchestrationError("conn must be a sqlite3.Connection")
    if config.retrieval.primary.provider != "asknews":
        # Not a gap: `research/exa.py` is structurally the *fallback* provider -- it
        # refuses to run against a config that does not name it as such, and it requires a
        # non-empty reason list saying why the primary was left. There is no honest way to
        # spell "Exa, as the primary, for no reason", so a config asking for one is refused
        # here rather than half-supported.
        raise OrchestrationError(
            "only 'asknews' is supported as retrieval.primary.provider; the Exa adapter is "
            "the fallback and refuses to run without a reason the primary was left"
        )

    queries = derive_queries(question)
    question_id = question.question_id
    primary_run_id = _mint_run_id()
    now_utc = require_run_metadata(
        question_id=question_id,
        retrieval_run_id=primary_run_id,
        now=now,
        error=OrchestrationError,
    )
    client = _asknews_client(config, news_client)

    opening = _opening_run(
        retrieval_run_id=primary_run_id,
        question_id=question_id,
        provider="asknews",
        started_at=now_utc,
    )
    try:
        open_run(conn, opening)
    except StoreError as exc:
        raise OrchestrationError(str(exc)) from None

    # --- past this line nothing refuses ---------------------------------------
    primary: AskNewsRetrieval = retrieve_news(
        client,
        config,
        question_id=question_id,
        queries=list(queries),
        retrieval_run_id=primary_run_id,
        now=now_utc,
    )
    # The money is gone from here on, so the only remaining failure is the ledger's, and it
    # arrives as this module's own type carrying what was spent -- never as a raw
    # `StoreError` escaping into the composer, which is what round 1 found aborting the
    # batch. `_fallback_pass`'s own `open_run` is inside this region for the same reason:
    # it, too, runs after the primary has been billed.
    runs: list[ProviderRun] = []
    try:
        runs.append(
            _record(
                conn,
                config,
                run=primary.run,
                documents=primary.documents,
                raw_responses=primary.raw_responses,
                documents_dropped=primary.documents_dropped,
                duplicates_collapsed=primary.duplicates_collapsed,
                provider_failed=primary.provider_failed,
                fallback_reasons=(),
                written_at=now_utc,
            )
        )

        decision = decide_fallback(
            primary_failed=primary.provider_failed,
            primary_documents=len(primary.documents),
            # No config field expresses this and this module will not invent one; see the
            # module docstring. M1-304's router is where it belongs.
            official_source_required=False,
        )
        if decision.should_run:
            _LOGGER.info(
                "falling back to the secondary provider for question %d: %s",
                question_id,
                ", ".join(decision.reasons),
            )
            fallback = _fallback_pass(
                conn,
                config,
                question_id=question_id,
                queries=queries,
                reasons=decision.reasons,
                now_utc=now_utc,
                injected=web_client,
            )
            if fallback is not None:
                runs.append(fallback)

        run_ids = tuple(entry.retrieval_run_id for entry in runs)
        documents = sum(entry.documents_retained for entry in runs)
        priced = [entry.cost_usd for entry in runs if entry.cost_usd is not None]
        packet = (
            load_packet(conn, question_id=question_id, retrieval_run_ids=run_ids)
            if documents
            else None
        )
    except StoreError as exc:
        # `or (primary_run_id,)`: when the primary's own recording is what failed there is
        # no completed run to name, but `open_run` above already put that row in the ledger
        # and it names this question -- which is exactly what `004`'s ownership trigger
        # requires of a cited run, and the only durable statement that this question cost
        # money.
        billed = [entry.cost_usd for entry in runs if entry.cost_usd is not None]
        billed.extend(value for value in (primary.run.cost_usd,) if not runs and value is not None)
        raise PaidRetrievalError(
            str(exc),
            retrieval_run_ids=tuple(entry.retrieval_run_id for entry in runs) or (primary_run_id,),
            cost_usd=sum(billed) if billed else None,
            unpriced_calls=(len(runs) or 1) - len(billed),
        ) from None
    return RetrievalOutcome(
        question_id=question_id,
        packet=packet,
        retrieval_run_ids=run_ids,
        runs=tuple(runs),
        document_count=documents,
        cost_usd=sum(priced) if priced else None,
        unpriced_runs=len(runs) - len(priced),
    )
