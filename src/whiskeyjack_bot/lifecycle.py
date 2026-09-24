"""Lifecycle state machine and atomic event writers (M1-603, M1-606, M2-713).

A forecast record is written once, as a ``draft``, and is never updated. Every later
state -- validated, approved, submitted, failed, resolved, scored -- exists only as an
appended :data:`lifecycle_events` row, so a record's **current status is derived**: the
``to_status`` of its highest ``event_seq``, or its stored ``status`` while it has no
events. ``003_lifecycle_events.sql`` holds the reasoning and the enforcement.

Not every event moves the record. A rejection and an *uncertain* submission -- one whose
refetch neither confirmed nor refuted the post -- are recorded where they happened and
leave the status where it was, because in both cases the record has not gone anywhere and
moving it would be a claim nothing supports. That is why a submission has three outcomes
here and not two; see :func:`record_submission_attempt`.

An uncertainty is not a resting place, though. What resolves it is a refetch --
:func:`record_submission_verification`, which writes what the platform actually showed and
carries the record to ``submitted`` or to ``failed``. Recording that observation as another
*attempt* would mean claiming a second live post, which is the retry the handoff exists to
block; see :class:`SubmissionVerification`.

A post can also go unrecorded altogether: made, and then the ledger refused the attempt row
or the process died before writing it. :func:`record_submission_reconciliation` is the way
out of that (M2-713). It writes no attempt -- there is no honest receipt to write -- but a
reconciliation row carrying a person's assertion and the program's own confirming refetch,
and the same ``submission_confirmed`` event a verification produces.

Blocking that retry is **not** something this module can do, and round 4 removed the
attempt to. Every writer here runs after the fact it records, so refusing a write cannot
prevent an action -- it can only lose the evidence of one. The rule lives in front of the
request instead: :func:`unresolved_uncertainties` is what a submitter asks *before* posting.

That is what M1-603's acceptance criterion reduces to. "Injected failures cannot leave
an approved/submitted state without its event record" is not a property of the code
below; it is a property of the schema, because there is nowhere else for the state to
be written. What this module owns is the other half -- **atomicity**. An approval is a
row in ``approval_events`` *and* a row in ``lifecycle_events``, a submission is a row in
``submission_attempts`` *and* a row in ``lifecycle_events``, and a caller must never be
able to observe one without the other. Every writer here wraps both in one transaction.

The module deliberately does **not** ship:

- ``approve`` / ``reject`` CLI commands -- M2-701 owns those, and adding them here would
  put a reachable approval path in the tree ahead of its item;
- a score computed for a numeric or discrete question -- D30 forbids a local replica of the
  platform's continuous scores. The resolution writer landed with M4-801
  (:func:`record_resolution_observation`, whose rows ``014_resolution_ingestion.sql``
  constrains), the local Brier/log writer with M4-802 (:func:`record_local_scores`), and the
  platform score writer with M4-803 (:func:`record_platform_scores`, which copies Metaculus's
  own scores out of the stored observation); ``017_platform_score_events.sql`` constrains
  both kinds of score row;
- assembly of the handoff's full canonical record. Approval and submission history is
  joined at read/export time (M1-604, ``show``), never written back into ``record_json``
  -- writing it back would mean updating a stored forecast version, which is the thing
  D25 forbids.

M1-606 adds the other half of the pipeline: a research or generation failure happens
*before* any :data:`forecast_records` row exists, so it cannot be a ``lifecycle_events``
row (see :data:`LifecycleEventType`'s note on ``research_failed``/``generation_failed``).
:func:`record_pre_forecast_failure` writes to ``pipeline_failure_events`` instead, scoped
to a caller-minted ``attempt_id`` rather than a forecast record. ``docs/M1-NOTES.md``'s
"M1-606" section and ``004_pipeline_failure_events.sql`` hold the reasoning; the two write
paths share this module's error type and validators but not a table.

Error hygiene follows ``ConfigError``/``LedgerError``: :class:`LifecycleError` never
echoes a stored value, sanitizing raises use ``from None``, and every malformed shape
arrives as a :class:`LifecycleError` rather than a raw ``sqlite3``/``TypeError``. The
vocabularies below are the one thing safe to name in a message -- they are this module's
own closed literals, not content -- and values read back out of the database are gated
against them before they are allowed anywhere near an error string.

Purely local file I/O: no network access on any path through here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, cast, get_args

from whiskeyjack_bot.bounds import (
    MAX_ACTOR_LENGTH,
    MAX_BODY_LENGTH,
    MAX_IDENTIFIER_LENGTH,
    MAX_NOTE_LENGTH,
)
from whiskeyjack_bot.platform_scores import (
    COMPARISON_BASELINES,
    PLATFORM_METRICS,
    ComparisonBaseline,
    PlatformMetric,
    PlatformScoreError,
    extract_platform_scores,
)
from whiskeyjack_bot.platform_scores import (
    recompute as recompute_platform,
)
from whiskeyjack_bot.redaction import redact_secrets
from whiskeyjack_bot.resolution import (
    ResolutionError,
    ResolutionKind,
    ResolutionObservation,
    canonical_json,
    classify_resolution,
    observation_from_snapshot,
    sha256_text,
)
from whiskeyjack_bot.scoring import (
    LOCAL_METRICS,
    LocalMetric,
    LocalScore,
    ScoreError,
    recompute,
    score_binary,
    score_multiple_choice,
)

# The seven states of 001's `forecast_records.status` CHECK.
LifecycleStatus = Literal[
    "draft", "validated", "approved", "submitted", "failed", "resolved", "scored"
]

# What can happen to a forecast record. Each event type names its own pipeline phase,
# which is why there is no separate `phase` column: it would be a second spelling of
# this one that has to be kept in agreement with it.
#
# Every member is scoped to a *forecast record*, and that is what bounds the list. An
# earlier draft also carried `research_failed` and `generation_failed`; both were
# structurally unreachable, because `forecast_records` (001) requires a non-null
# `final_prediction_json`, `record_json` and `retrieval_run_id`, so no record exists
# until generation has already succeeded. There was no row for those events to attach
# to, and no honest one to invent. They are removed rather than left as an API that can
# only raise "unknown record"; M1-606 owns pre-forecast failures and the attempt-scoped
# identity they need. (GPT review round 1, finding 2.) A research failure already has a
# home in the meantime: `research_runs.error_summary`, per CLAUDE_CODE_PROMPT.md's
# retrieval section.
#
# `submission_uncertain` is the one member that exists because the alternative was a
# false claim. An attempt that posted but whose refetch did not confirm it is neither a
# verified success nor an outright failure; recording it as `submission_failed` moved the
# record to terminal `failed`, so a later confirming refetch had no legal event and the
# ledger would disagree with the platform permanently -- the opposite of the handoff's
# "an uncertain timeout blocks blind retry until a refetch resolves the state". It leaves
# the record `approved`. (GPT review round 2, finding 3.)
#
# **M2-711 widened what reaches it, and deliberately added no member of its own.** A post
# that raised and whose refetch could not be performed is the same lifecycle state as any
# other unconfirmed post -- approved, resolvable by a later refetch, closed against blind
# retry -- so it wanted this event and not a twelfth one. A twelfth would have meant
# widening this Literal's CHECK in `003_lifecycle_events.sql`, which SQLite can only do by
# rebuilding a table whose append-only block triggers would have to be dropped to do it:
# the operation the ledger exists to make impossible. The vocabulary member that case
# genuinely needed went on `submission_attempts.refetch_outcome`, where ADD COLUMN reaches
# it. See :data:`RefetchOutcome` and :func:`record_submission_attempt`. (Owner decision.)
#
# `submission_confirmed` and `submission_disconfirmed` are the two ways back out of that
# state, and they belong to the *refetch* rather than to another attempt. Without them the
# only route to `submitted` ran through a second `submission_attempts` row, which needs a
# new idempotency key (001 declares it UNIQUE) and therefore claims a second live post --
# the blind retry the handoff forbids, reached by way of the mechanism that was supposed
# to prevent it. (GPT review round 3, finding 1.) They cite a `submission_verifications`
# row; see :func:`record_submission_verification`.
LifecycleEventType = Literal[
    "validated",
    "validation_failed",
    "rejected",
    "approved",
    "submitted",
    "submission_uncertain",
    "submission_failed",
    "submission_confirmed",
    "submission_disconfirmed",
    "resolved",
    "scored",
]

# The subset :func:`record_failure` writes: failures that happen once the draft record
# exists but before it is approved, and so carry no detail row. One member today, kept as
# a named alias because M1-606 is expected to widen it, and because it keeps
# `record_failure`'s vocabulary gate honest about which events it will accept. A
# submission failure is not here -- it has an attempt row and is written by
# :func:`record_submission_attempt`.
PipelineFailureEvent = Literal["validation_failed"]

# What the refetch that accompanied a *post* established (M2-711). Four-valued, and it is
# `submission_live.classify_refetch`'s own vocabulary carried across the seam rather than
# spelled again: that function has returned these four members since M2-704, and the ledger
# collapsed them into `verified_by_refetch` alone, which is one bit and loses which.
#
# The two extra members are the ones the pair had nowhere to put. `mismatched` is "something
# newer than the baseline is on the platform and it is not what was sent"; `unreadable` is
# "no observation was made at all". Both used to arrive as `verified_by_refetch=False` and,
# when the post had also raised, as terminal `submission_failed` -- a permanent claim that
# the post did not go through, made on no observation. See
# :func:`record_submission_attempt` for the partition this now decides, and
# `009_submission_refetch_outcome.sql` for the schema half.
#
# Deliberately distinct from :data:`VerificationOutcome` below, which is two-valued and
# stays that way. They answer different questions: this is what an *attempt's own* refetch
# saw, and that is what a *later, standalone* refetch concluded -- and only `confirmed` and
# `absent` are conclusions, which is why `submission_live.verify_uncertain_attempt` refuses
# to record the other two and leaves the uncertainty standing instead.
RefetchOutcome = Literal["confirmed", "absent", "mismatched", "unreadable"]

# What a refetch saw. Two-valued for the reason the migration gives: a refetch that could
# not be *performed* observed nothing and changes no state, so it has no lifecycle event
# to produce and would be a detail row nothing can cite. Like `ApprovalDecision`, the
# member and the event type are deliberately different words -- `confirmed`/`absent`
# describe the platform, `submission_confirmed`/`submission_disconfirmed` describe what
# that does to the record -- so the migration maps one to the other explicitly rather than
# comparing two columns that happen to agree.
VerificationOutcome = Literal["confirmed", "absent"]

# The 001 vocabulary of `approval_events.decision`, reused verbatim: the decision and
# the lifecycle event type are the same word, which is what lets the migration's trigger
# check that a lifecycle event cites an approval row recording the same decision.
ApprovalDecision = Literal["approved", "rejected"]

# Why something failed, or why a submission is unconfirmed. A closed vocabulary, because
# this is the only "reason" the lifecycle log carries and it must be safe to export and
# log without review. Provider text stays in
# `submission_attempts.error_message`/`response_body`, which the event row points at
# rather than copies.
#
# `rejected_by_reviewer` was a member and is deliberately gone. A rejection is a decision,
# not a failure: it lands validated -> validated, its account is the actor and note on the
# `approval_events` row the event cites, and the migration forbids a `detail_code` on it.
# Nothing could ever write the code, so it is removed rather than shipped as dead
# vocabulary in an immutable migration -- the call round 1 made on `research_failed`.
# (Owner decision, round 2 finding 8.)
FailureCode = Literal[
    "provider_error",
    "provider_unavailable",
    "no_evidence",
    "stale_evidence",
    "malformed_response",
    "schema_invalid",
    "calibration_invalid",
    "http_error",
    "timeout",
    "refetch_mismatch",
    "refetch_missing",
    "internal_error",
]

# What can fail before any forecast_records row exists (M1-606). Scoped to an attempt_id
# rather than a forecast record -- see 004_pipeline_failure_events.sql and
# docs/M1-NOTES.md's "M1-606" section for why lifecycle_events cannot hold these instead:
# its event_type is a closed CHECK, and widening one means rebuilding the table.
PreForecastEventType = Literal["research_failed", "generation_failed"]

# FailureCode minus refetch_mismatch/refetch_missing: both describe what a refetch saw of
# an already-posted forecast (submission_verifications), which cannot occur before
# generation has even succeeded once. Spelled out rather than derived, the same style
# PipelineFailureEvent uses for record_failure's narrower vocabulary -- so a future
# addition to FailureCode does not silently become reachable here before anyone decides it
# belongs.
PreForecastFailureCode = Literal[
    "provider_error",
    "provider_unavailable",
    "no_evidence",
    "stale_evidence",
    "malformed_response",
    "schema_invalid",
    "calibration_invalid",
    "http_error",
    "timeout",
    "internal_error",
]

_STATUSES: frozenset[str] = frozenset(get_args(LifecycleStatus))
_EVENT_TYPES: frozenset[str] = frozenset(get_args(LifecycleEventType))
_FAILURE_CODES: frozenset[str] = frozenset(get_args(FailureCode))
_APPROVAL_DECISIONS: frozenset[str] = frozenset(get_args(ApprovalDecision))
_REFETCH_OUTCOMES: frozenset[str] = frozenset(get_args(RefetchOutcome))
_VERIFICATION_OUTCOMES: frozenset[str] = frozenset(get_args(VerificationOutcome))
_PIPELINE_FAILURE_EVENTS: frozenset[str] = frozenset(get_args(PipelineFailureEvent))
_PRE_FORECAST_EVENT_TYPES: frozenset[str] = frozenset(get_args(PreForecastEventType))
_PRE_FORECAST_FAILURE_CODES: frozenset[str] = frozenset(get_args(PreForecastFailureCode))

# The state machine, spelled out here and again as a trigger in
# `003_lifecycle_events.sql`. The duplication is deliberate -- the database is the
# enforcement, this table is the writer -- and `tests/unit/test_lifecycle.py` drives
# every possible (event_type, from_status, to_status) triple through the database and
# asserts the accepted set is exactly this one, so the two cannot drift apart.
#
# `failed` is terminal by omission: a retry is a new forecast *version* (M1-602), not a
# resurrected record. `rejected` is validated -> validated because the seven states have
# no 'rejected' member and a rejected approval must "leave the last valid record intact"
# (CODEX_HANDOFF, pipeline and failure boundaries) -- it records a decision without
# moving the record. `submission_uncertain` is approved -> approved for the reason given
# at its vocabulary member: an unresolved submission must stay somewhere a later refetch
# can still move it, and `approved` is where the record was. The refetch is what moves it
# from there -- `submission_confirmed` to `submitted`, `submission_disconfirmed` to
# terminal `failed`, the same destination a failed post whose refetch *saw* nothing
# reaches and for the same reason: the post is not there, and the retry is a new forecast
# version. (Since M2-711 that is the failed post whose `refetch_outcome` is `absent`; one
# whose refetch was unreadable observed nothing and lands uncertain instead.)
#
# This table is the whole rule again, as of round 4. Round 3 added a history-dependent
# guard on top of it -- no further attempt while an uncertainty stood -- which is not a
# transition rule and, more to the point, not a rule a record of past events can enforce.
# See :func:`unresolved_uncertainties`.
_LEGAL_TRANSITIONS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("validated", "draft", "validated"),
        ("validation_failed", "draft", "failed"),
        ("validation_failed", "validated", "failed"),
        ("rejected", "validated", "validated"),
        ("approved", "validated", "approved"),
        ("submitted", "approved", "submitted"),
        ("submission_uncertain", "approved", "approved"),
        ("submission_failed", "approved", "failed"),
        ("submission_confirmed", "approved", "submitted"),
        ("submission_disconfirmed", "approved", "failed"),
        ("resolved", "submitted", "resolved"),
        ("scored", "resolved", "scored"),
    }
)

# (event_type, from_status) -> to_status. Derived rather than written out a third time.
# The mapping is total over `_LEGAL_TRANSITIONS` and single-valued -- no event type can
# mean two different destinations from the same state -- which a unit test pins by
# comparing sizes.
_DESTINATIONS: dict[tuple[str, str], str] = {
    (event_type, from_status): to_status
    for event_type, from_status, to_status in _LEGAL_TRANSITIONS
}

# Length ceilings, by what the field is. Identifiers and actors are short by nature; an
# operator's note is prose; a provider response body is the one field that can be large
# and is capped where the handoff says the receipt is "size-limited". They live in
# `bounds.py` rather than here (M1-608): five other modules apply the same numbers, and
# until this item they each spelled them again under a comment saying they matched.

_HEX_DIGITS: frozenset[str] = frozenset("0123456789abcdef")

# What SQLite's INTEGER can actually hold. Python's int is unbounded, so this is a real
# boundary rather than a defensive nicety; see _require_optional_int.
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


class LifecycleError(Exception):
    """A lifecycle event cannot be recorded, or the ledger rejected it.

    Same hygiene rule as ``ConfigError``/``LedgerError``: the message never echoes a
    stored value, a caller-supplied field value, or a database error's text, and
    sanitizing raises use ``from None`` so nothing can be reprinted through a cause
    chain or a rendered traceback. Vocabulary members (this module's own literals) are
    named, because they are the only thing that makes a rejection actionable and they
    are not content.
    """


@dataclass(frozen=True)
class LifecycleEvent:
    """One recorded transition, read back from the ledger.

    Constructed only by this module, from a row the database has already accepted, so
    every field has passed the migration's CHECKs and triggers. It carries **no free
    text**: identifiers, closed-vocabulary tags and ISO-8601 timestamps only. That is
    what lets a history be logged or exported without a redaction pass -- the free-text
    detail lives in the row this one points at.

    All fields are JSON-native (``str``/``int``/``None``), so the persisted form used
    for replay comparison is ``json.dumps(dataclasses.asdict(event),
    ensure_ascii=True, sort_keys=True)`` -- the M1-305 rule, which survives the lone
    surrogates that ``str.encode('utf-8')`` does not.
    """

    event_id: int
    forecast_record_id: str
    event_seq: int
    event_type: LifecycleEventType
    from_status: LifecycleStatus
    to_status: LifecycleStatus
    detail_code: FailureCode | None
    approval_event_id: int | None
    submission_attempt_id: str | None
    submission_verification_id: int | None
    resolution_event_id: int | None
    score_event_id: int | None
    submission_reconciliation_id: str | None
    occurred_at_utc: str
    created_at_utc: str


@dataclass(frozen=True)
class SubmissionAttempt:
    """The `submission_attempts` row a submission produced, minus writer-owned metadata.

    This is the **ledger-side** shape. M2-703's ``SubmissionReceipt`` is the gateway's
    return type and is that item's to define; it maps into this. Keeping them separate
    is what stops a persistence concern (column set, size caps) from being decided here
    on behalf of the submission seam, and vice versa.

    ``created_at_utc`` is absent deliberately: it records when the ledger stored the
    row, so only the write path may set it. Letting a caller supply it would let a
    caller backdate its own audit trail -- the rule ``research/model.py`` already states
    for the same column.

    ``completed_at_utc`` is **required**, and was optional in the first cut. The ledger
    only hears about an attempt once it is over -- there is no in-flight row to leave open
    -- and ``submission_attempts`` is append-only, so a receipt written without a
    completion time could never acquire one. (GPT review round 2, finding 5.)

    ``refetch_outcome`` replaced a ``verified_by_refetch: bool`` **field** in M2-711, and
    the replacement is the item rather than a refactor alongside it. The boolean could not
    distinguish "the refetch looked and the forecast is not there" from "the refetch could
    not be performed", so an attempt that raised and could not be checked was recorded as
    terminal ``submission_failed``. The column is still written -- ``001`` declares it NOT
    NULL -- but it is **derived** here, so this class holds one fact once and no caller can
    hand the writer two that disagree. The schema enforces the same equivalence against raw
    SQL (``009_submission_refetch_outcome.sql``), which is what makes the derivation a
    single source of truth rather than a convention.
    """

    attempt_id: str
    idempotency_key: str
    requested_at_utc: datetime
    completed_at_utc: datetime
    request_payload_sha256: str
    success: bool
    refetch_outcome: RefetchOutcome
    http_status: int | None = None
    response_body: str | None = None
    response_headers: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    refetched_forecast_snapshot: str | None = None

    @property
    def verified_by_refetch(self) -> bool:
        """Whether a refetch confirmed the post -- derived, never supplied.

        Kept as a public name because it is the column, the receipt field every existing
        reader asks for, and the word the acceptance criterion uses. Kept as a *property*
        because it is not independent information: exactly one member of
        :data:`RefetchOutcome` is a confirmation, and a second stored copy of that fact is
        a second thing that can be wrong.
        """
        return self.refetch_outcome == "confirmed"


@dataclass(frozen=True)
class SubmissionVerification:
    """A refetch, and what it saw of an attempt whose outcome was left uncertain.

    Deliberately **not** a ``SubmissionAttempt``. An attempt is the record of a request:
    it carries an idempotency key (unique, per 001), a request payload hash and an HTTP
    status, and none of those exist for an observation. Resolving an uncertainty by
    writing a second attempt row meant minting a second key -- which is to say, claiming a
    second live post -- so the thing the handoff asks for ("block retry until refetch
    resolves state") could only be recorded by doing the thing it forbids. (GPT review
    round 3, finding 1.)

    ``outcome`` decides the event: ``confirmed`` carries the record to ``submitted``,
    ``absent`` to terminal ``failed``. The attempt named here must be one this ledger
    recorded as ``submission_uncertain``; an attempt already accounted for as submitted or
    failed is not open to being re-decided by a later refetch.

    ``refetched_forecast_snapshot`` is optional in the type and **required for a
    ``confirmed`` outcome** by both the writer and the schema: a confirmation with nothing
    stored is a claim about the platform with no evidence behind it, and it is the claim
    that moves the record to ``submitted``.

    ``created_at_utc`` is absent for :class:`SubmissionAttempt`'s reason -- it is when the
    ledger stored the row, so only the write path may set it.
    """

    submission_attempt_id: str
    outcome: VerificationOutcome
    observed_at_utc: datetime
    refetched_forecast_snapshot: str | None = None


@dataclass(frozen=True)
class StoredSubmissionAttempt:
    """A ``submission_attempts`` row, read back in full (M1-612).

    Constructed only by this module, from a row the database has already accepted --
    the same contract :class:`LifecycleEvent`/:class:`PreForecastFailure` carry.
    Deliberately omits ``response_body``/``response_headers``/``error_message``/
    ``refetched_forecast_snapshot``: identifying and status fields only, matching how
    ``show`` already summarizes lifecycle detail (a ``detail_code`` and an attempt id,
    never the full row). The raw response is available in full through M1-604's export,
    a different tool for that job.
    """

    attempt_id: str
    forecast_record_id: str
    idempotency_key: str
    requested_at_utc: str
    completed_at_utc: str | None
    request_payload_sha256: str
    http_status: int | None
    success: bool
    error_type: str | None
    refetch_outcome: RefetchOutcome | None


@dataclass(frozen=True)
class StoredSubmissionVerification:
    """A ``submission_verifications`` row, read back in full (M1-612).

    Constructed only by this module, for :class:`StoredSubmissionAttempt`'s reason.
    """

    verification_id: int
    submission_attempt_id: str
    outcome: VerificationOutcome
    observed_at_utc: str


@dataclass(frozen=True)
class SubmissionReconciliation:
    """A post the ledger never recorded, and the evidence that it reached the platform (M2-713).

    **Not a ``SubmissionAttempt``, and it cannot be one.** An attempt row is the receipt of a
    request -- whether the POST returned, when it finished, what the refetch beside it saw --
    and in this state nobody captured that. ``success`` is ``NOT NULL CHECK (0, 1)`` since
    ``001``, so an attempt row would have to guess it, and a guessed ``success`` on an
    append-only table is the claim ``CODEX_HANDOFF.md`` prohibits. A captured artifact does
    not rescue it either: the artifact carries no ``detail_code``, and the one an uncertain
    event needs is not always derivable from what it does carry.

    What a reconciliation holds instead is evidence, from three owners:

    - **the program, before the post** -- ``reservation_id`` (the claim the post was made
      under) and ``intent_event_id`` (the ``forecast_intent`` journal row
      ``submission_policy`` commits immediately ahead of every POST);
    - **the program, now** -- ``refetched_forecast_snapshot``, a refetch whose outcome is
      ``confirmed``, taken at ``refetched_at_utc``;
    - **a person** -- ``observed_by`` and ``note``, both required, for ``approve``'s reason:
      a claim about what someone saw on the platform is never inferred from the machine.

    **There is no ``attempt_id`` field, and that is the round-1 fix.** The row names the
    attempt by the identity :func:`live_attempt_id_for_key` gives a post under the reservation's
    key -- the one its attempt row would have carried -- and the writer derives it from the
    *stored* reservation. Accepting it as a field let a well-formed id for some other key be
    recorded against this post (M2-703's rule: a value the writer can derive is a second source
    of truth if a caller may also supply it). ``artifact_path`` and
    ``artifact_sha256`` pin the captured artifact when one exists -- both or neither -- and
    are never read into the row: the file is evidence the row points at, not a receipt the
    row claims.

    ``created_at_utc`` is absent for :class:`SubmissionAttempt`'s reason, and so is the
    reconciliation's own identifier: the writer mints it, so no caller can supply one that
    collides with a row it cannot see.
    """

    reservation_id: str
    request_payload_sha256: str
    intent_event_id: str
    observed_by: str
    note: str
    refetched_at_utc: datetime
    refetched_forecast_snapshot: str
    artifact_path: str | None = None
    artifact_sha256: str | None = None


@dataclass(frozen=True)
class PreForecastFailure:
    """One recorded pre-forecast failure, read back from the ledger (M1-606).

    Constructed only by this module, from a row the database has already accepted --
    the same contract :class:`LifecycleEvent` carries, and for the same reason: every
    field has passed ``pipeline_failure_events_validate_on_insert``. JSON-native fields
    only, so the persisted form used for replay comparison is the same
    ``json.dumps(dataclasses.asdict(event), ensure_ascii=True, sort_keys=True)`` rule
    :class:`LifecycleEvent` documents.

    ``attempt_id`` is the link acceptance criterion 2 rests on: if the campaign this
    event belongs to later succeeds, the resulting ``forecast_records`` row is stamped
    with this same value at INSERT time (``004_pipeline_failure_events.sql``).
    """

    event_id: int
    attempt_id: str
    event_seq: int
    question_id: int
    tournament_id: str
    event_type: PreForecastEventType
    detail_code: PreForecastFailureCode
    retrieval_run_id: str | None
    occurred_at_utc: str
    created_at_utc: str


# What :func:`record_resolution_observation` did with an observation. ``unchanged`` and
# ``nothing_to_retract`` write nothing: the first is a repeated poll, the second a question
# that is not resolved and never was, which has no state to record.
ResolutionWriteOutcome = Literal["appended", "unchanged", "nothing_to_retract"]

# Where a record must be for a resolution to be recorded against it. The API only unmasks a
# resolution for a question the account predicted on, and in this ledger "predicted" is a
# confirmed post; `resolved` and `scored` are where such a record goes next.
_RESOLVABLE_STATUSES: frozenset[str] = frozenset({"submitted", "resolved", "scored"})

# The largest raw post payload stored as a resolution's source response. A MiniBench post as
# this account sees it is a few kilobytes -- aggregations are masked -- so this bounds a
# pathological response without truncating an ordinary one.
MAX_SOURCE_RESPONSE_LENGTH = 4 * 1024 * 1024


@dataclass(frozen=True)
class StoredResolution:
    """One ``resolution_events`` row, read back and re-verified (M4-801).

    ``observation`` is re-validated from the stored snapshot, and both digests are recomputed
    from the stored text before this is constructed, so what a scorer reads is what was
    hashed. ``scorable`` is the observation's, which the migration also pins on the row.
    """

    event_id: int
    forecast_record_id: str
    observation: ResolutionObservation
    observation_sha256: str
    source_response_sha256: str
    observed_at_utc: str
    ingested_at_utc: str
    # The stored post payload's canonical text, verified against ``source_response_sha256``
    # with everything else here. Carried so a platform score is read from exactly the text
    # that was hashed (M4-803); kept out of the repr because it is the whole payload.
    source_response: str = field(repr=False)

    @property
    def kind(self) -> ResolutionKind:
        return self.observation.kind

    @property
    def scorable(self) -> bool:
        return self.observation.scorable


@dataclass(frozen=True)
class ResolutionWrite:
    """The result of one :func:`record_resolution_observation` call."""

    outcome: ResolutionWriteOutcome
    stored: StoredResolution | None
    event: LifecycleEvent | None


# What :func:`record_local_scores` did. ``unchanged`` and ``not_scorable`` write nothing: the
# first is a repeated run against an observation already scored under every current
# implementation, the second a record with no resolution or whose latest one is not
# ``resolved``.
ScoreWriteOutcome = Literal["appended", "unchanged", "not_scorable"]

# Where a record must be for a local score to be written. `resolved` takes the `scored`
# event; `scored` is a re-resolution or a new implementation version, which appends rows only.
_SCORABLE_STATUSES: frozenset[str] = frozenset({"resolved", "scored"})


@dataclass(frozen=True)
class StoredScore:
    """One local ``score_events`` row, read back (M4-802).

    What :func:`read_local_scores` returns has also been **recomputed**: its value is what the
    implementation named by ``implementation_version`` gives for the stored forecast and the
    cited resolution, exactly.
    """

    event_id: int
    forecast_record_id: str
    resolution_event_id: int
    metric: LocalMetric
    value: float
    implementation_version: str
    computed_at_utc: str


@dataclass(frozen=True)
class ScoreWrite:
    """The result of one :func:`record_local_scores` call.

    ``scores`` holds the rows this call appended (empty unless ``appended``); ``resolution``
    is the latest resolution the call read, or ``None`` if the record has none.
    """

    outcome: ScoreWriteOutcome
    scores: tuple[StoredScore, ...]
    event: LifecycleEvent | None
    resolution: StoredResolution | None


@dataclass(frozen=True)
class StoredPlatformScore:
    """One platform ``score_events`` row, read back (M4-803).

    What :func:`read_platform_scores` returns has also been **re-read**: its value is exactly
    what the cited observation's stored, digest-verified response holds under the path its
    ``implementation_version`` names. ``comparison_baseline`` and the version together are
    the score's source; ``resolution_event_id`` is the evidence.
    """

    event_id: int
    forecast_record_id: str
    resolution_event_id: int
    metric: PlatformMetric
    value: float
    implementation_version: str
    comparison_baseline: ComparisonBaseline
    computed_at_utc: str


@dataclass(frozen=True)
class PlatformScoreWrite:
    """The result of one :func:`record_platform_scores` call; :class:`ScoreWrite`'s shape."""

    outcome: ScoreWriteOutcome
    scores: tuple[StoredPlatformScore, ...]
    event: LifecycleEvent | None
    resolution: StoredResolution | None


def _utcnow() -> datetime:
    """Writer-owned clock. A seam for tests; never a parameter of the public writers."""
    return datetime.now(tz=timezone.utc)


def _require_text(value: object, field: str, *, max_length: int) -> str:
    """Return ``value`` as storable text, or raise naming only the *field*.

    The type gate is exact (``type(x) is str``) rather than ``isinstance``: a ``str``
    subclass can carry an attacker-controlled ``__str__``/``__repr__`` whose value slips
    into a log line or a dataclass repr, which is the same reasoning
    ``questions/events.py`` gives for its gates.

    The encode probe is the important one. A lone surrogate reaches this layer from
    provider JSON, and ``sqlite3`` encodes text parameters as UTF-8 -- so without it a
    writer raises a raw ``UnicodeEncodeError`` **quoting the offending character**,
    which is both a leak and an error type callers do not handle. (The same defect is
    open against ``research/hashing.py``; here it is closed at the boundary.)
    """
    if type(value) is not str or not value:
        raise LifecycleError(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise LifecycleError(f"{field} is longer than the {max_length}-character limit")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # from None: UnicodeEncodeError's own message quotes the character it choked on.
        raise LifecycleError(
            f"{field} contains characters that cannot be stored "
            "(detail withheld: it can echo the value)"
        ) from None
    return value


def _require_optional_text(value: object, field: str, *, max_length: int) -> str | None:
    return None if value is None else _require_text(value, field, max_length=max_length)


def _redact_optional(value: object, secret_env_var_names: Sequence[str]) -> object:
    """Redact a nullable free-text field before it is bounded or stored (M1-605).

    Applied before ``_require_optional_text``'s length check, not after: redaction can only
    grow text (a secret value becomes ``<redacted:NAME>``), and bounding the *pre*-redaction
    text would let a length check clip a marker the writer just produced.

    Passes anything that is not exactly a ``str`` through unchanged -- including ``None`` --
    so a malformed ``attempt`` field still reaches ``_require_optional_text``'s own type gate
    and raises the module's sanitized error, rather than this helper raising a raw
    ``TypeError`` out of a public boundary first.
    """
    return redact_secrets(value, secret_env_var_names) if type(value) is str else value


def _require_identifier(value: object, field: str) -> str:
    """Return ``value`` as a non-blank identifier, or raise (M1-606, widened by M1-607).

    :func:`_require_text` already refuses ``''``, but ``'\\n\\t'`` is truthy and reaches
    storage through it. That is tolerable for prose columns and not for an identifier:
    ``attempt_id`` is the join key linking a failed campaign to the forecast version that
    later succeeds, and ``004_pipeline_failure_events.sql`` refuses a blank one on *both*
    tables. A writer that accepted what the schema refuses would fail at the statement
    with an opaque message; a writer that accepted what the schema accepts on one table
    and not the other would mint an attempt_id no ``forecast_records`` row could ever
    claim, leaving an append-only failure permanently unjoinable.

    The blank test is ``str.strip()``, which is the definition the migration's character
    set was written from -- M1-603's round 5 was exactly these two definitions disagreeing
    (SQLite's one-argument ``trim()`` strips U+0020 alone), and a test asserts they still
    agree over every codepoint Python calls whitespace.

    **U+0000 is refused outright** (M1-606 review round 2, finding B1). SQLite's own
    ``length()`` stops counting at an embedded NUL rather than counting the full Python
    string, so ``004_pipeline_failure_events.sql``'s ``length(...) > 200`` guard cannot see
    past one: a 202-character ``attempt_id`` with a NUL at position 2 reads as
    ``length() == 1`` to the trigger and passes, while this function's own ``len()`` sees
    202 and refuses it on read-back. Refusing the character here (and in both migration
    triggers, via ``instr(..., char(0)) > 0``) closes the mismatch by removing the one
    input the two counting functions disagree about, rather than trying to make SQLite
    count matching Python's definition of length.

    **Now every identifier column's validator, not just this item's (M1-607).** M1-606
    deliberately scoped this function to its own writer, because widening it would have
    changed what already-shipped, already-reviewed writers accept -- a behaviour change to
    merged code smuggled in under a different item -- and filed the widening as its own
    reviewed change instead. ``006_non_blank_identifiers.sql`` is the schema half of it:
    ``record_id``, ``tournament_id``, ``attempt_id``, ``idempotency_key`` and
    ``retrieval_run_id`` now carry the same clause on both layers.

    It stays separate from :func:`_require_text` rather than replacing it, because the two
    kinds of column differ in what blankness *means*. Blank prose -- an ``actor`` note, an
    ``error_message``, a ``response_body`` -- is a thin record; blank identity is an
    unjoinable one. So ``actor`` and the body columns keep :func:`_require_text`, and that
    distinction is the reason both functions exist.
    """
    text = _require_text(value, field, max_length=MAX_IDENTIFIER_LENGTH)
    if not text.strip():
        raise LifecycleError(f"{field} must not be blank")
    if "\x00" in text:
        raise LifecycleError(f"{field} must not contain a NUL character")
    return text


def _require_sha256(value: object, field: str) -> str:
    """Return ``value`` as a 64-character lowercase hex digest, or raise.

    Mirrors the migration's ``length(...) = 64 AND ... NOT GLOB '*[^0-9a-f]*'`` so a
    caller gets a field-level message instead of a constraint violation.
    """
    text = _require_text(value, field, max_length=64)
    if len(text) != 64 or not _HEX_DIGITS.issuperset(text):
        raise LifecycleError(f"{field} must be 64 lowercase hexadecimal characters")
    return text


def _require_bool(value: object, field: str) -> int:
    """Return 0/1 for the ``CHECK (... IN (0, 1))`` integer columns.

    ``bool`` exactly, not "anything truthy" and not ``int``. ``success`` -- with
    ``refetch_outcome`` -- decides whether a submission becomes ``submitted``,
    ``submission_uncertain`` or ``submission_failed``, so a stray ``1`` arriving where a
    ``bool`` was meant would silently promote an unverified post to a verified one -- an
    unearned claim rather than a type error.
    """
    if type(value) is not bool:
        raise LifecycleError(f"{field} must be True or False")
    return 1 if value else 0


def _require_optional_int(value: object, field: str) -> int | None:
    """Return ``value`` as a storable integer, or raise naming only the *field*.

    The range check is not decoration. Python integers are unbounded and SQLite's are
    signed 64-bit, so ``sqlite3`` raises a raw ``OverflowError`` when it binds one that
    does not fit -- and ``OverflowError`` is not a ``sqlite3.Error``, so it sails past
    the wrapper in :func:`_insert` and reaches the caller as an exception type this
    module does not document. Rejecting it here makes it a field-level message instead.
    (GPT review round 1, finding 3.)
    """
    if value is None:
        return None
    if type(value) is not int:
        raise LifecycleError(f"{field} must be an integer")
    if not _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX:
        raise LifecycleError(f"{field} is outside the range the ledger can store")
    return value


def _require_int(value: object, field: str) -> int:
    """Return ``value`` as a storable integer, or raise. The required counterpart of
    :func:`_require_optional_int` -- ``question_id`` has no honest ``None``.
    """
    result = _require_optional_int(value, field)
    if result is None:
        raise LifecycleError(f"{field} must be an integer")
    return result


def _require_http_status(value: object, field: str) -> int | None:
    """Return ``value`` as an HTTP status code, or raise naming only the *field*.

    A status is either absent -- no response arrived -- or a status code. Storing -1, 0 or
    2**63-1 puts a number that no HTTP responder can have produced into an append-only
    receipt, where it is indistinguishable from a real one. :func:`_require_optional_int`
    is the wider gate underneath (a Python int too large to bind raises ``OverflowError``,
    which is not a ``sqlite3.Error``); this narrows it to the range the field means.
    (GPT review round 2, finding 7.)
    """
    status = _require_optional_int(value, field)
    if status is not None and not 100 <= status <= 599:
        raise LifecycleError(f"{field} must be an HTTP status code between 100 and 599")
    return status


def _require_aware_utc(value: object, field: str) -> datetime:
    """Return an aware datetime converted to UTC, or raise.

    Exact type rather than ``isinstance``: a ``datetime`` subclass can override
    ``isoformat()`` and write arbitrary text into a NOT NULL timestamp column, which
    would put unvetted content into a field the ledger's replay ordering depends on.
    (It is also what makes the conversion below safe to call ``isoformat()`` on:
    ``astimezone`` returns the same class it was given.)

    The conversion itself is guarded, and broadly. ``tzinfo`` is an abstract base class,
    so ``value.utcoffset()`` and ``astimezone()`` run *caller-supplied code* on a value
    that has passed every type gate above -- a ``datetime`` carrying a hostile ``tzinfo``
    whose ``utcoffset`` raises will propagate whatever that method raises, message and
    traceback included. ``except Exception`` is the right width precisely because the
    set of exceptions arbitrary code can raise is not enumerable.

    Separate from :func:`_require_utc` so a caller that has to *compare* two timestamps
    can do it on datetimes rather than on their rendered text.
    """
    if type(value) is not datetime:
        raise LifecycleError(f"{field} must be a datetime")
    try:
        aware = value.tzinfo is not None and value.utcoffset() is not None
    except Exception:
        # from None: the tzinfo's own exception text and traceback are attacker-shaped.
        raise LifecycleError(
            f"{field} has a timezone that could not be read "
            "(detail withheld: it can echo the value)"
        ) from None
    if not aware:
        raise LifecycleError(f"{field} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except Exception:
        raise LifecycleError(
            f"{field} could not be converted to UTC (detail withheld: it can echo the value)"
        ) from None


def _utc_text(value: datetime) -> str:
    """Render an aware UTC datetime in the canonical stored form.

    One function, because the form is a contract with ``003_lifecycle_events.sql`` rather
    than a formatting preference: the migration pins this exact shape on the columns it
    orders, so a second rendering anywhere would be refused by our own schema. That is not
    hypothetical -- the attempt writer rendered its two timestamps with a bare
    ``isoformat()`` while :func:`_require_utc` had been made canonical, and every write
    through it failed until both agreed.

    Takes an **already validated** datetime: :func:`_require_aware_utc`'s output or
    :func:`_utcnow`'s. The ``astimezone`` here is a normalization, not a guard -- given a
    caller's raw datetime it would run that caller's ``tzinfo`` code unprotected, which is
    what :func:`_require_aware_utc` exists to wrap.
    """
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _require_utc(value: object, field: str) -> str:
    """Return an aware datetime as a *canonical* ISO-8601 UTC string, or raise.

    ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``: fixed width 32, always UTC, microseconds always
    present. Plain ``isoformat()`` omits the fractional part when it is zero, which makes
    the rendered width vary and the ordering of two stored values depend on which shape
    each happens to have.

    That matters because the schema compares two of these columns, and it can only do so
    exactly by comparing the text: ``julianday()`` is a float day number, so microseconds
    fall below its precision and two instants a microsecond apart compare equal (GPT review
    round 4, finding 3). ``003_lifecycle_events.sql`` pins this exact form on the columns it
    orders; rendering it here for *every* timestamp keeps the stored ledger uniform rather
    than uniform-where-checked.
    """
    return _utc_text(_require_aware_utc(value, field))


def _require_member(value: object, allowed: frozenset[str], field: str) -> str:
    """Gate a value against one of this module's closed vocabularies.

    Used on caller input *and* on values read back out of the database. A stored value
    is only safe to name in a message once it has been proven to be a member of a
    vocabulary this module defines; until then it is content, and the rejection says so
    without printing it.
    """
    if type(value) is not str or value not in allowed:
        raise LifecycleError(
            f"{field} is not one of the recognized values "
            "(detail withheld: it can echo a stored value)"
        )
    return value


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a block atomically, nesting safely inside a caller's own transaction.

    ``BEGIN IMMEDIATE``, not a bare ``BEGIN``. Every writer here reads the record's
    current status and then appends against it; a deferred ``BEGIN`` takes the write
    lock lazily, so two writers can both read "validated", both decide their event is
    seq 2, and only discover the conflict on a lock upgrade that cannot be retried from
    inside an open transaction. Taking the write lock up front serializes the
    read-then-write instead. (``UNIQUE (forecast_record_id, event_seq)`` is the second
    line of defence, and turns any race that does occur into a loud failure rather than
    a silently reordered history.)

    Nested use opens a ``SAVEPOINT`` instead, so M1-602 can write a forecast record and
    its first lifecycle event in one unit without this module either committing early
    or rolling back work it does not own.

    The transaction-control statements are themselves guarded (:func:`_control`). A
    ``COMMIT`` can fail -- a busy timeout, a full disk -- and an unguarded one would both
    escape as a raw ``sqlite3.Error`` and leave the caller holding an open transaction
    that strands every later write on the connection.
    """
    if conn.isolation_level is not None:
        # ledger.connect() sets isolation_level = None so that BEGIN/COMMIT are explicit.
        # Under the default, sqlite3 opens and commits transactions on its own schedule,
        # and "one transaction" below would silently not be one.
        raise LifecycleError(
            "the ledger connection must be in explicit-transaction mode; "
            "open it with whiskeyjack_bot.ledger.connect()"
        )
    if conn.in_transaction:
        savepoint = f"wj_{uuid.uuid4().hex}"
        _control(conn, f"SAVEPOINT {savepoint}")
        try:
            yield
        except BaseException:
            _unwind(conn, f"ROLLBACK TO {savepoint}", f"RELEASE {savepoint}")
            raise
        # The same unwind the exception path uses: ROLLBACK TO leaves the savepoint on the
        # stack, so it takes both statements to undo this block without touching the
        # caller's transaction, which is not ours to roll back or commit.
        _control(
            conn,
            f"RELEASE {savepoint}",
            unwind=(f"ROLLBACK TO {savepoint}", f"RELEASE {savepoint}"),
        )
        return
    _control(conn, "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        _unwind(conn, "ROLLBACK")
        raise
    _control(conn, "COMMIT", unwind=("ROLLBACK",))


def _control(conn: sqlite3.Connection, statement: str, *, unwind: tuple[str, ...] = ()) -> None:
    """Run a transaction-control statement, or fail as this module's own error type.

    ``unwind`` is what has to run before the failure is reported, so the connection is
    never left inside a transaction the caller believes was closed: a failed ``COMMIT``
    rolls back, a failed ``RELEASE`` unwinds to its savepoint. Best-effort by way of
    :func:`_unwind` -- if that fails too the connection is unusable, and the original
    failure is still the one worth reporting.
    """
    try:
        conn.execute(statement)
    except sqlite3.Error:
        _unwind(conn, *unwind)
        # from None: the underlying error's text and traceback can carry stored values.
        # The statement itself is this module's own constant SQL, never caller content,
        # but naming it would say nothing a caller could act on.
        raise LifecycleError(
            "the ledger could not complete this transaction (detail withheld: a database "
            "message can echo stored values)"
        ) from None


def _unwind(conn: sqlite3.Connection, *statements: str) -> None:
    """Best-effort rollback that never replaces the exception being propagated.

    If the rollback itself fails the connection is already unusable, and surfacing that
    instead of the original error would hide why the block failed in the first place.

    Every statement is attempted, including those after one that failed. The two-part
    savepoint unwind is why: ``ROLLBACK TO`` leaves the savepoint on the stack and only
    the paired ``RELEASE`` pops it, so abandoning the sequence at the first failure would
    leak a savepoint onto a connection the caller goes on using.
    """
    for statement in statements:
        try:
            conn.execute(statement)
        except sqlite3.Error:
            continue


def current_status(conn: sqlite3.Connection, record_id: str) -> LifecycleStatus:
    """Return the record's derived current status.

    The ``to_status`` of its highest ``event_seq``, or the status it was created with
    while it has no events. ``forecast_records.status`` is *status at creation* and is
    pinned to ``draft`` by the migration; it is never the answer to "where is this
    record now" once any event exists.
    """
    identifier = _require_identifier(record_id, "record_id")
    row = _fetch_one(
        conn,
        "SELECT to_status FROM lifecycle_events WHERE forecast_record_id = ? "
        "ORDER BY event_seq DESC LIMIT 1",
        (identifier,),
    )
    if row is None:
        row = _fetch_one(
            conn,
            "SELECT status FROM forecast_records WHERE record_id = ?",
            (identifier,),
        )
    if row is None:
        raise LifecycleError("record_id does not name a stored forecast record")
    return cast(LifecycleStatus, _require_member(row[0], _STATUSES, "status"))


def read_history(conn: sqlite3.Connection, record_id: str) -> tuple[LifecycleEvent, ...]:
    """Return every recorded event for a record, in append order.

    ``event_seq`` is contiguous from 1 per record, so a gap in the returned sequence is
    a detectable defect rather than an unremarkable rowid jump.

    An unknown ``record_id`` raises rather than returning an empty history, matching
    :func:`current_status`. The two are the read seam M1-604 and ``show`` build on, and
    a caller that cannot tell "this record has no events yet" from "there is no such
    record" would report the first while looking at the second.
    """
    identifier = _require_identifier(record_id, "record_id")
    _require_stored_record(conn, identifier)
    rows = _fetch_all(
        conn,
        f"SELECT {_EVENT_COLUMNS} FROM lifecycle_events WHERE forecast_record_id = ? "
        "ORDER BY event_seq",
        (identifier,),
    )
    return tuple(_event_from_row(row) for row in rows)


def unresolved_uncertainties(conn: sqlite3.Connection, record_id: str) -> tuple[str, ...]:
    """Attempt ids this record recorded as uncertain that no refetch has resolved yet.

    **Ask this before submitting, not after.** It is the ledger's half of the handoff's
    "an uncertain timeout blocks blind retry until a refetch resolves the state": an empty
    tuple means nothing is outstanding, and a non-empty one names the attempts M2-704 has
    to refetch and pass to :func:`record_submission_verification` first.

    Round 3 tried to enforce that rule at write time instead, in the trigger and in
    :func:`record_submission_attempt`. Both run on a *finished* receipt, so refusing there
    could not stop a second post -- only stop it being recorded, which loses the fact
    instead of preventing the act, and left the attempt row committed with its event
    refused (GPT review round 4, finding 1). A rule about what to do next belongs in front
    of the action; this is that seam, and it is a reader.

    "Unresolved" is: an uncertain event whose record is *still* ``approved``. A refetch
    that resolved one carried the record to ``submitted`` or ``failed``, and no submission
    is legal from either -- so once the record has moved, nothing here is outstanding.
    """
    identifier = _require_identifier(record_id, "record_id")
    if current_status(conn, identifier) != "approved":
        return ()
    rows = _fetch_all(
        conn,
        "SELECT submission_attempt_id FROM lifecycle_events "
        "WHERE forecast_record_id = ? AND event_type = 'submission_uncertain' "
        "ORDER BY event_seq",
        (identifier,),
    )
    return tuple(_stored_text(row[0], "submission_attempt_id") for row in rows)


def record_attempt_id(conn: sqlite3.Connection, record_id: str) -> str | None:
    """The ``attempt_id`` this record's ``forecast_records`` row was stamped with (M1-612).

    ``004_pipeline_failure_events.sql`` adds the column and stamps every new record at
    INSERT time; ``None`` for a pre-004 row. This is the join key
    :func:`read_pipeline_failure_events` needs -- that table has no ``forecast_record_id``
    column at all, because a pre-forecast failure can occur before any record exists.
    """
    identifier = _require_identifier(record_id, "record_id")
    _require_stored_record(conn, identifier)
    row = _fetch_one(
        conn, "SELECT attempt_id FROM forecast_records WHERE record_id = ?", (identifier,)
    )
    if row is None:  # pragma: no cover - _require_stored_record already proved existence
        raise LifecycleError("the recorded forecast record could not be read back")
    return None if row[0] is None else _stored_text(row[0], "attempt_id")


def record_validation(
    conn: sqlite3.Connection, *, record_id: str, occurred_at: datetime
) -> LifecycleEvent:
    """Record that a draft passed validation: ``draft -> validated``.

    The success half of M1-501/M1-504's gate. The failure half is
    :func:`record_failure` with ``event_type='validation_failed'``.
    """
    return _append_event(
        conn,
        record_id=record_id,
        event_type="validated",
        occurred_at_utc=_require_utc(occurred_at, "occurred_at"),
    )


def record_failure(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    event_type: PipelineFailureEvent,
    detail_code: FailureCode,
    occurred_at: datetime,
) -> LifecycleEvent:
    """Record a pre-approval failure of a stored draft: validation, today.

    These carry no detail row -- there is no provider receipt to point at -- so
    ``detail_code`` is the whole account of what went wrong and is required. A failed
    record is terminal: a further attempt is a new forecast version (M1-602), which is
    why there is no transition out of ``failed``.

    ``event_type`` is a one-member vocabulary rather than a fixed literal because the
    events this *cannot* record are the interesting ones: a research or generation
    failure happens before any ``forecast_records`` row exists, so it has no record to
    name here. That is M1-606's problem to solve, with an attempt-scoped identity and
    migration 004; see :data:`LifecycleEventType`.
    """
    _require_member(event_type, _PIPELINE_FAILURE_EVENTS, "event_type")
    _require_member(detail_code, _FAILURE_CODES, "detail_code")
    return _append_event(
        conn,
        record_id=record_id,
        event_type=event_type,
        detail_code=detail_code,
        occurred_at_utc=_require_utc(occurred_at, "occurred_at"),
    )


def record_approval(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    decision: ApprovalDecision,
    actor: str,
    forecast_sha256: str,
    occurred_at: datetime,
    note: str | None = None,
    payload_sha256: str | None = None,
) -> LifecycleEvent:
    """Append an approval decision and its lifecycle event, atomically.

    ``forecast_sha256`` must equal the hash stored on the record: approval binds to an
    exact forecast, and any content change invalidates it (D12/D23). The check is made
    here for a readable failure and again by the migration's trigger, which is the
    binding one -- a writer that forgot to compare cannot get past the database.

    ``payload_sha256`` is the second half of that binding and is **M2-707/D33**: the hash
    of the submission payload the decision authorizes. It is required for ``'approved'``
    and must be absent for ``'rejected'``, which is `011`'s rule and is checked here for
    the same readable-failure reason. What this module does *not* check is that the digest
    is the payload the record actually derives -- that is a question about canonical JSON,
    the pinned SDK's CDF conversion and the calibration configuration, none of which the
    ledger layer can see. :mod:`whiskeyjack_bot.submission_payload` owns the derivation and
    :func:`whiskeyjack_bot.submission.submission_key_for_approved_record` owns the gate.

    ``'rejected'`` leaves the record ``validated``. A rejection records a decision; it
    does not move the record, and the last valid record stays intact.
    """
    _require_member(decision, _APPROVAL_DECISIONS, "decision")
    identifier = _require_identifier(record_id, "record_id")
    actor_text = _require_text(actor, "actor", max_length=MAX_ACTOR_LENGTH)
    digest = _require_sha256(forecast_sha256, "forecast_sha256")
    payload_digest = _require_payload_digest(payload_sha256, decision)
    note_text = _require_optional_text(note, "note", max_length=MAX_NOTE_LENGTH)
    occurred = _require_utc(occurred_at, "occurred_at")

    with transaction(conn):
        _require_hash_binds(conn, identifier, digest)
        approval_id = _insert(
            conn,
            "INSERT INTO approval_events "
            "(forecast_record_id, decision, actor, forecast_sha256, note, created_at_utc, "
            "payload_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                decision,
                actor_text,
                digest,
                note_text,
                _utc_text(_utcnow()),
                payload_digest,
            ),
        )
        return _append_event(
            conn,
            record_id=identifier,
            event_type=decision,
            approval_event_id=approval_id,
            occurred_at_utc=occurred,
        )


def record_submission_attempt(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    attempt: SubmissionAttempt,
    occurred_at: datetime,
    secret_env_var_names: Sequence[str],
    detail_code: FailureCode | None = None,
) -> LifecycleEvent:
    """Append a submission attempt and its lifecycle event, atomically.

    The event type is **derived from the attempt**, not chosen by the caller, and
    ``(success, refetch_outcome)`` partitions into three outcomes rather than two::

        success  refetch_outcome  event
        -------  ---------------  --------------------
        True     confirmed        submitted
        True     absent           submission_uncertain
        True     mismatched       submission_uncertain
        True     unreadable       submission_uncertain
        False    confirmed        submission_uncertain
        False    absent           submission_failed
        False    mismatched       submission_uncertain
        False    unreadable       submission_uncertain

    ``submitted`` is M2-704's "success requires refetch confirmation". Everything that is
    neither a confirmed success nor an observed absence is the handoff's uncertain timeout:
    the post and the platform do not agree, or the platform was never read, and neither is
    a failure. Recording those as ``submission_failed`` moved the record to terminal
    ``failed``, so a later confirming refetch had nowhere to land and blind retry was the
    only thing left -- exactly what the handoff says the ledger must prevent (GPT review
    round 2, finding 3). An uncertain attempt leaves the record ``approved``.

    **M2-711 split the last row of the old table.** 003 read ``(success,
    verified_by_refetch)``, and that pair had no member meaning "the post raised *and* the
    refetch could not be performed"; it landed in ``(False, False)`` with the genuinely
    failed attempt and became terminal ``failed`` -- a permanent claim that the post did
    not go through, made on no observation at all. ``unreadable`` is that case.
    ``mismatched`` moves with it and for the same reason: ``submission_live.
    classify_refetch`` returns it only when an entry *newer than the baseline* is on the
    platform and does not match what was sent, so "nothing is there" is false of it too.
    Which of the two it is is a human judgement, and an uncertain record is where a human
    can still make it.

    A second attempt made while an earlier one is still uncertain **is recorded**, not
    refused. Round 3 refused it here and in the trigger; round 4 withdrew that, because
    this function is handed a receipt for a post that has already happened, and the only
    thing a refusal achieves is a live post with no ledger row. Whether to make that
    request is decided before it is made -- see :func:`unresolved_uncertainties` -- and a
    record may therefore hold more than one uncertain attempt, each with its own event.

    ``detail_code`` is required for both non-verified outcomes and refused for
    ``submitted``.

    ``secret_env_var_names`` is redacted out of ``attempt.response_body``,
    ``attempt.response_headers`` and ``attempt.error_message`` before either is bounded or
    stored (M1-605): those three are raw HTTP text from a live submission attempt, the same
    class of content ``logging_setup.ProviderResponseTextFilter`` already protects in logs,
    and nothing upstream of this writer redacts them. Required rather than defaulted, so a
    caller cannot silently skip redaction by omission.

    Persistence only. Nothing here contacts Metaculus; the gateways that do are M2-703
    and M2-704, and ``submission.enabled``/``dry_run`` remain what they are until then.
    """
    # Exact type, not isinstance -- the same gate every validator below uses, and for a
    # stronger reason. A subclass can override __getattribute__ or shadow a field with a
    # property, so each `attempt.<field>` read here becomes a call into caller-supplied
    # code that can raise anything, from anywhere between the two writes. The field
    # validators cannot help: they only see what the attribute access returns.
    # (GPT review round 1, finding 3.)
    if type(attempt) is not SubmissionAttempt:
        raise LifecycleError("attempt must be a SubmissionAttempt")
    identifier = _require_identifier(record_id, "record_id")
    occurred = _require_utc(occurred_at, "occurred_at")

    attempt_id = _require_identifier(attempt.attempt_id, "attempt.attempt_id")
    success = _require_bool(attempt.success, "attempt.success")
    outcome = _require_member(attempt.refetch_outcome, _REFETCH_OUTCOMES, "attempt.refetch_outcome")
    # The column, derived from the one field that carries the fact. Not read off the
    # dataclass's property: a property is an attribute read like any other, and deriving it
    # here keeps the value bound in this function to the vocabulary member just validated.
    verified = 1 if outcome == "confirmed" else 0

    event_type: LifecycleEventType
    if success == 1 and verified == 1:
        event_type = "submitted"
        if detail_code is not None:
            raise LifecycleError("detail_code is not applicable to a verified submission")
    else:
        # An observed absence after a post that raised is the only failure: everything else
        # is the platform and the post disagreeing, or the platform never being read.
        event_type = (
            "submission_failed" if success == 0 and outcome == "absent" else "submission_uncertain"
        )
        if detail_code is None:
            raise LifecycleError(
                "detail_code is required for an attempt that is not a refetch-verified success"
            )
        _require_member(detail_code, _FAILURE_CODES, "detail_code")

    # Both timestamps as datetimes, so the ordering check below compares instants rather
    # than rendered text. A receipt that finished before it was requested is not a clock
    # curiosity here: `requested_at_utc` is what an idempotency key is reasoned about
    # against, and the row is append-only, so a reversed pair is permanent.
    requested = _require_aware_utc(attempt.requested_at_utc, "attempt.requested_at_utc")
    completed = _require_aware_utc(attempt.completed_at_utc, "attempt.completed_at_utc")
    if completed < requested:
        raise LifecycleError("attempt.completed_at_utc is earlier than attempt.requested_at_utc")

    values = (
        attempt_id,
        identifier,
        _require_identifier(attempt.idempotency_key, "attempt.idempotency_key"),
        _utc_text(requested),
        _utc_text(completed),
        _require_sha256(attempt.request_payload_sha256, "attempt.request_payload_sha256"),
        _require_http_status(attempt.http_status, "attempt.http_status"),
        _require_optional_text(
            _redact_optional(attempt.response_body, secret_env_var_names),
            "attempt.response_body",
            max_length=MAX_BODY_LENGTH,
        ),
        _require_optional_text(
            _redact_optional(attempt.response_headers, secret_env_var_names),
            "attempt.response_headers",
            max_length=MAX_BODY_LENGTH,
        ),
        success,
        _require_optional_text(
            attempt.error_type, "attempt.error_type", max_length=MAX_IDENTIFIER_LENGTH
        ),
        _require_optional_text(
            _redact_optional(attempt.error_message, secret_env_var_names),
            "attempt.error_message",
            max_length=MAX_BODY_LENGTH,
        ),
        verified,
        _require_optional_text(
            attempt.refetched_forecast_snapshot,
            "attempt.refetched_forecast_snapshot",
            max_length=MAX_BODY_LENGTH,
        ),
        outcome,
        _utc_text(_utcnow()),
    )

    with transaction(conn):
        _insert(
            conn,
            "INSERT INTO submission_attempts "
            "(attempt_id, forecast_record_id, idempotency_key, requested_at_utc, "
            "completed_at_utc, request_payload_sha256, http_status, response_body, "
            "response_headers, success, error_type, error_message, verified_by_refetch, "
            "refetched_forecast_snapshot, refetch_outcome, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return _append_event(
            conn,
            record_id=identifier,
            event_type=event_type,
            detail_code=detail_code,
            submission_attempt_id=attempt_id,
            occurred_at_utc=occurred,
        )


_SUBMISSION_ATTEMPT_COLUMNS = (
    "attempt_id, forecast_record_id, idempotency_key, requested_at_utc, "
    "completed_at_utc, request_payload_sha256, http_status, success, error_type, "
    "refetch_outcome"
)


def _submission_attempt_from_row(row: sqlite3.Row) -> StoredSubmissionAttempt:
    """Build the value object from a stored row, gating every vocabulary field."""
    success = row[7]
    if type(success) is not int or success not in (0, 1):
        raise LifecycleError(
            "stored success is not a 0/1 flag (detail withheld: it can echo stored values)"
        )
    return StoredSubmissionAttempt(
        attempt_id=_stored_text(row[0], "attempt_id"),
        forecast_record_id=_stored_text(row[1], "forecast_record_id"),
        idempotency_key=_stored_text(row[2], "idempotency_key"),
        requested_at_utc=_stored_text(row[3], "requested_at_utc"),
        completed_at_utc=(None if row[4] is None else _stored_text(row[4], "completed_at_utc")),
        request_payload_sha256=_stored_text(row[5], "request_payload_sha256"),
        http_status=(None if row[6] is None else _stored_int(row[6], "http_status")),
        success=bool(success),
        error_type=(None if row[8] is None else _stored_text(row[8], "error_type")),
        refetch_outcome=(
            None
            if row[9] is None
            else cast(RefetchOutcome, _require_member(row[9], _REFETCH_OUTCOMES, "refetch_outcome"))
        ),
    )


def read_submission_attempts(
    conn: sqlite3.Connection, record_id: str
) -> tuple[StoredSubmissionAttempt, ...]:
    """Every submission attempt recorded against this record, in request order (M1-612).

    An unknown ``record_id`` raises, for :func:`read_forecast_summary`'s reason.
    """
    identifier = _require_identifier(record_id, "record_id")
    _require_stored_record(conn, identifier)
    rows = _fetch_all(
        conn,
        f"SELECT {_SUBMISSION_ATTEMPT_COLUMNS} FROM submission_attempts "
        "WHERE forecast_record_id = ? ORDER BY requested_at_utc, attempt_id",
        (identifier,),
    )
    return tuple(_submission_attempt_from_row(row) for row in rows)


def record_submission_verification(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    verification: SubmissionVerification,
    occurred_at: datetime,
    detail_code: FailureCode | None = None,
) -> LifecycleEvent:
    """Append a refetch observation and its lifecycle event, atomically.

    This is how an uncertain submission ends. The attempt named by ``verification`` must
    be one this record's history holds a ``submission_uncertain`` event for; what the
    refetch saw then decides where the record goes::

        confirmed  submission_confirmed     approved -> submitted
        absent     submission_disconfirmed  approved -> failed

    As with :func:`record_submission_attempt`, the event type is **derived** and never
    chosen by the caller. ``detail_code`` is required for ``absent`` -- ``refetch_missing``
    is the usual one -- and refused for ``confirmed``, which is a success and carries no
    failure code. ``refetched_forecast_snapshot`` runs the other way: **required for
    ``confirmed``**, because it is the evidence that carries the record to ``submitted``,
    and empty for ``absent``, which saw nothing to store.

    Until this is written the uncertainty is outstanding, and
    :func:`unresolved_uncertainties` keeps saying so -- which is what M2-704 consults
    before deciding to post again. ``submission_disconfirmed`` is terminal, so a genuinely
    lost post is retried as a new forecast version (M1-602), which is what every other
    route to ``failed`` already means.

    Persistence only. Nothing here contacts Metaculus; M2-704 owns the refetch itself.
    """
    # Exact type, for the reason spelled out in record_submission_attempt: a subclass can
    # turn every attribute read below into a call into caller-supplied code.
    if type(verification) is not SubmissionVerification:
        raise LifecycleError("verification must be a SubmissionVerification")
    identifier = _require_identifier(record_id, "record_id")
    occurred = _require_utc(occurred_at, "occurred_at")

    attempt_id = _require_identifier(
        verification.submission_attempt_id, "verification.submission_attempt_id"
    )
    outcome = _require_member(verification.outcome, _VERIFICATION_OUTCOMES, "verification.outcome")
    observed = _require_utc(verification.observed_at_utc, "verification.observed_at_utc")
    snapshot = _require_optional_text(
        verification.refetched_forecast_snapshot,
        "verification.refetched_forecast_snapshot",
        max_length=MAX_BODY_LENGTH,
    )

    event_type: LifecycleEventType
    if outcome == "confirmed":
        event_type = "submission_confirmed"
        if detail_code is not None:
            raise LifecycleError("detail_code is not applicable to a confirmed submission")
        # A confirmation carries the record to `submitted`, and what it saw is the whole
        # of the evidence for that. Round 3 wrote the snapshot column and left it
        # optional, so a confirmation could be recorded on nothing (round 4, finding 2).
        if snapshot is None or not snapshot.strip():
            raise LifecycleError(
                "verification.refetched_forecast_snapshot is required for a confirmed "
                "refetch; a confirmation is only auditable if it stores what it saw"
            )
    else:
        event_type = "submission_disconfirmed"
        if detail_code is None:
            raise LifecycleError(
                "detail_code is required for a refetch that did not find the forecast"
            )
        _require_member(detail_code, _FAILURE_CODES, "detail_code")

    with transaction(conn):
        _require_verifiable_attempt(conn, identifier, attempt_id, observed)
        verification_id = _insert(
            conn,
            "INSERT INTO submission_verifications "
            "(submission_attempt_id, outcome, observed_at_utc, "
            "refetched_forecast_snapshot, created_at_utc) VALUES (?, ?, ?, ?, ?)",
            (attempt_id, outcome, observed, snapshot, _utc_text(_utcnow())),
        )
        return _append_event(
            conn,
            record_id=identifier,
            event_type=event_type,
            detail_code=detail_code,
            submission_verification_id=verification_id,
            occurred_at_utc=occurred,
        )


_SUBMISSION_VERIFICATION_COLUMNS = (
    "verification_id, submission_attempt_id, outcome, observed_at_utc"
)


def _submission_verification_from_row(row: sqlite3.Row) -> StoredSubmissionVerification:
    """Build the value object from a stored row, gating every vocabulary field."""
    return StoredSubmissionVerification(
        verification_id=_stored_int(row[0], "verification_id"),
        submission_attempt_id=_stored_text(row[1], "submission_attempt_id"),
        outcome=cast(
            VerificationOutcome, _require_member(row[2], _VERIFICATION_OUTCOMES, "outcome")
        ),
        observed_at_utc=_stored_text(row[3], "observed_at_utc"),
    )


def read_submission_verifications(
    conn: sqlite3.Connection, record_id: str
) -> tuple[StoredSubmissionVerification, ...]:
    """Every refetch observation recorded against this record's attempts (M1-612).

    ``submission_verifications`` has no ``forecast_record_id`` column -- an observation is
    of an attempt, not of a record -- so this joins through ``submission_attempts`` to
    scope it, the same join :func:`unresolved_uncertainties` avoids needing only because
    it reads the *absence* of a verification rather than one that exists.

    An unknown ``record_id`` raises, for :func:`read_forecast_summary`'s reason.
    """
    identifier = _require_identifier(record_id, "record_id")
    _require_stored_record(conn, identifier)
    rows = _fetch_all(
        conn,
        f"SELECT {_SUBMISSION_VERIFICATION_COLUMNS} FROM submission_verifications "
        "WHERE submission_attempt_id IN "
        "(SELECT attempt_id FROM submission_attempts WHERE forecast_record_id = ?) "
        "ORDER BY observed_at_utc, verification_id",
        (identifier,),
    )
    return tuple(_submission_verification_from_row(row) for row in rows)


# A reconciliation's own identifier, minted by the writer. `wjres-`/`wjrel-` are the
# reservation-side tags (`submission.py`); this is a third tag in the same `<tag><32 hex>`
# shape, in a column of its own, so it is never compared with either.
_RECONCILIATION_PREFIX = "wjrec-"

# The visible scheme tag on a live attempt id. It lives here, with the one derivation below,
# because the reconciliation writer has to derive the id and `submission_live` imports this
# module -- the reverse import would close a cycle. `submission_live.live_attempt_id` delegates
# to :func:`live_attempt_id_for_key`, so there is one rule, and `016` pins the same shape.
LIVE_ATTEMPT_TAG = "wjlive-1-"


def live_attempt_id_for_key(idempotency_key: str) -> str:
    """The deterministic attempt id a live post under this key carries (M2-704, moved here).

    ``submission_live.live_attempt_id`` documents why it is derived rather than minted and
    hashed rather than copied; that function now calls this one. Moved for M2-713's round 1:
    a reconciliation must name the attempt its post carried, and the only honest source for
    that is this derivation applied to the reservation the ledger stored.
    """
    key = _require_identifier(idempotency_key, "idempotency_key")
    return LIVE_ATTEMPT_TAG + hashlib.sha256(key.encode("utf-8")).hexdigest()


def record_submission_reconciliation(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    reconciliation: SubmissionReconciliation,
    occurred_at: datetime,
) -> LifecycleEvent:
    """Record a post the ledger never wrote down, and its ``submission_confirmed`` event (M2-713).

    The state this closes: a forecast live on Metaculus, a key reservation standing, no
    ``submission_attempts`` row and the record still ``approved`` -- because the ledger refused
    the attempt write after the post, or the process died before making it. Nothing else in
    the ledger could move that record without an attempt row, and an attempt row could only
    be written by inventing the receipt nobody captured; see :class:`SubmissionReconciliation`.

    One transaction, two rows: the reconciliation and the event citing it, so no caller can
    observe one without the other -- this module's atomicity contract. The event is
    ``submission_confirmed``, approved -> submitted, the event that already means "a refetch
    confirmed an outcome that was not settled at the time". ``submitted`` is not used: it
    claims a successful, refetch-verified attempt, and none exists.

    **What is enforced, and where.** ``016``'s trigger is the binding check and holds against a
    raw INSERT; the probes below restate it so a caller gets a field-level message rather than
    ``the ledger rejected this write``. ``attempt_id`` is not accepted at all: it is derived
    here, inside the transaction, from the key of the reservation the ledger stored
    (:func:`live_attempt_id_for_key`). SQLite has no sha256, so ``016`` checks only its shape and
    that no attempt row holds it; this writer is the one path that sets it, and it cannot be
    handed a different one.

    Persistence only: the refetch the snapshot records was made by the caller.
    """
    if type(reconciliation) is not SubmissionReconciliation:
        # Exact type, for `record_submission_attempt`'s reason: a subclass can turn each
        # attribute read below into a call into caller-supplied code.
        raise LifecycleError("reconciliation must be a SubmissionReconciliation")
    identifier = _require_identifier(record_id, "record_id")
    occurred = _require_utc(occurred_at, "occurred_at")
    reservation_id = _require_identifier(
        reconciliation.reservation_id, "reconciliation.reservation_id"
    )
    digest = _require_sha256(
        reconciliation.request_payload_sha256, "reconciliation.request_payload_sha256"
    )
    intent_event_id = _require_identifier(
        reconciliation.intent_event_id, "reconciliation.intent_event_id"
    )
    observed_by = _require_assertion_text(
        reconciliation.observed_by, "reconciliation.observed_by", max_length=MAX_ACTOR_LENGTH
    )
    note = _require_assertion_text(
        reconciliation.note, "reconciliation.note", max_length=MAX_NOTE_LENGTH
    )
    refetched_at = _require_utc(reconciliation.refetched_at_utc, "reconciliation.refetched_at_utc")
    snapshot = _require_confirming_snapshot(reconciliation.refetched_forecast_snapshot)
    artifact_path = (
        None
        if reconciliation.artifact_path is None
        else _require_identifier(reconciliation.artifact_path, "reconciliation.artifact_path")
    )
    artifact_sha256 = (
        None
        if reconciliation.artifact_sha256 is None
        else _require_sha256(reconciliation.artifact_sha256, "reconciliation.artifact_sha256")
    )
    if (artifact_path is None) != (artifact_sha256 is None):
        raise LifecycleError(
            "reconciliation.artifact_path and reconciliation.artifact_sha256 are recorded "
            "together or not at all"
        )

    reconciliation_id = _RECONCILIATION_PREFIX + uuid.uuid4().hex
    with transaction(conn):
        attempt_id = _require_reconcilable(
            conn,
            record_id=identifier,
            reservation_id=reservation_id,
            digest=digest,
            intent_event_id=intent_event_id,
            refetched_at=refetched_at,
        )
        _insert(
            conn,
            "INSERT INTO submission_reconciliations (reconciliation_id, reservation_id, "
            "forecast_record_id, attempt_id, request_payload_sha256, intent_event_id, "
            "artifact_path, artifact_sha256, observed_by, note, refetched_at_utc, "
            "refetched_forecast_snapshot, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reconciliation_id,
                reservation_id,
                identifier,
                attempt_id,
                digest,
                intent_event_id,
                artifact_path,
                artifact_sha256,
                observed_by,
                note,
                refetched_at,
                snapshot,
                _utc_text(_utcnow()),
            ),
        )
        return _append_event(
            conn,
            record_id=identifier,
            event_type="submission_confirmed",
            submission_reconciliation_id=reconciliation_id,
            occurred_at_utc=occurred,
        )


def record_resolution_observation(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    source_response: object,
    observed_at: datetime,
) -> ResolutionWrite:
    """Classify a fetched post payload and append it as this record's resolution, atomically.

    The payload is the only input about the question: the observation is **derived** here by
    ``resolution.classify_resolution`` against the record's own question id, post id and type,
    so no caller can hand this writer an observation that disagrees with the evidence stored
    beside it.

    What happens next depends on the latest row already recorded for the record::

        same observation as the latest row             unchanged           nothing written
        `unresolved`, and no row yet                   nothing_to_retract  nothing written
        anything else                                  appended            one row

    An appended ``resolved``/``annulled``/``ambiguous`` observation also appends the
    ``resolved`` lifecycle event (``submitted -> resolved``) if the record is still
    ``submitted``. A ``withheld`` value or a retraction moves nothing, and a record already
    ``resolved`` has no further transition to take: its current resolution is its latest row,
    which is what ``score_events_require_scorable_resolution`` reads.

    The record must be ``submitted``, ``resolved`` or ``scored``. The two skips above are this
    writer's courtesy; ``014_resolution_ingestion.sql`` refuses the same rows against a raw
    INSERT.
    """
    identifier = _require_identifier(record_id, "record_id")
    observed = _require_utc(observed_at, "observed_at")

    with transaction(conn):
        row = _fetch_one(
            conn,
            "SELECT question_id, post_id, question_type FROM forecast_records WHERE record_id = ?",
            (identifier,),
        )
        if row is None:
            raise LifecycleError("record_id does not name a stored forecast record")
        question_id = _stored_int(row[0], "question_id")
        if row[1] is None:
            raise LifecycleError("the forecast record has no post_id to resolve against")
        post_id = _stored_int(row[1], "post_id")
        question_type = _stored_text(row[2], "question_type")

        status = current_status(conn, identifier)
        if status not in _RESOLVABLE_STATUSES:
            raise LifecycleError(
                f"a resolution cannot be recorded for a record whose current status is {status}"
            )

        try:
            observation = classify_resolution(
                source_response, question_id=question_id, question_type=question_type
            )
            source_text = canonical_json(source_response)
        except ResolutionError as exc:
            # The message is resolution.py's own, which names rules and fields only.
            raise LifecycleError(f"the source response cannot be recorded: {exc}") from None
        if observation.post_id != post_id:
            raise LifecycleError("the source response is for a different post than the record")
        if len(source_text) > MAX_SOURCE_RESPONSE_LENGTH:
            raise LifecycleError("the source response is larger than the ledger stores")
        snapshot = observation.snapshot_json()
        digest = sha256_text(snapshot)

        latest = _fetch_one(
            conn,
            "SELECT observation_sha256, observed_at_utc FROM resolution_events "
            "WHERE forecast_record_id = ? ORDER BY event_id DESC LIMIT 1",
            (identifier,),
        )
        if latest is None and observation.kind == "unresolved":
            return ResolutionWrite(outcome="nothing_to_retract", stored=None, event=None)
        if latest is not None:
            if _stored_text(latest[0], "observation_sha256") == digest:
                return ResolutionWrite(outcome="unchanged", stored=None, event=None)
            if _stored_text(latest[1], "observed_at_utc") > observed:
                raise LifecycleError(
                    "observed_at is earlier than the latest observation recorded for this record"
                )

        event_id = _insert(
            conn,
            "INSERT INTO resolution_events "
            "(question_id, forecast_record_id, resolution_snapshot_json, outcome, annulled, "
            "ambiguous, source_response, ingested_at_utc, post_id, question_type, "
            "resolution_kind, scorable, observation_sha256, source_response_sha256, "
            "observed_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                question_id,
                identifier,
                snapshot,
                observation.outcome,
                1 if observation.kind == "annulled" else 0,
                1 if observation.kind == "ambiguous" else 0,
                source_text,
                _utc_text(_utcnow()),
                post_id,
                question_type,
                observation.kind,
                1 if observation.scorable else 0,
                digest,
                sha256_text(source_text),
                observed,
            ),
        )
        event: LifecycleEvent | None = None
        if observation.definitive and status == "submitted":
            event = _append_event(
                conn,
                record_id=identifier,
                event_type="resolved",
                resolution_event_id=event_id,
                occurred_at_utc=observed,
            )
        stored = _read_resolution(conn, "event_id = ?", (event_id,))
        if stored is None:  # pragma: no cover - the row was just inserted in this transaction
            raise LifecycleError("the recorded resolution could not be read back")
        return ResolutionWrite(outcome="appended", stored=stored, event=event)


def latest_resolution(conn: sqlite3.Connection, record_id: str) -> StoredResolution | None:
    """The record's current resolution -- its latest observation -- or ``None`` if it has none.

    This is the read a scorer asks. It is the latest row and not the row the ``resolved``
    lifecycle event cites, because the platform can retract or change a resolution after
    the record has moved; ``score_events`` refuses a score on the same terms.
    """
    identifier = _require_identifier(record_id, "record_id")
    _require_stored_record(conn, identifier)
    return _read_resolution(
        conn,
        "event_id = (SELECT max(event_id) FROM resolution_events WHERE forecast_record_id = ?)",
        (identifier,),
    )


def read_resolution_history(
    conn: sqlite3.Connection, record_id: str
) -> tuple[StoredResolution, ...]:
    """Every recorded observation for a record, in append order."""
    identifier = _require_identifier(record_id, "record_id")
    _require_stored_record(conn, identifier)
    rows = _fetch_all(
        conn,
        f"SELECT {_RESOLUTION_COLUMNS} FROM resolution_events WHERE forecast_record_id = ? "
        "ORDER BY event_id",
        (identifier,),
    )
    return tuple(_resolution_from_row(row) for row in rows)


def record_local_scores(
    conn: sqlite3.Connection, *, record_id: str, computed_at: datetime
) -> ScoreWrite:
    """Compute and append a record's local Brier and log scores, atomically (M4-802).

    The record id is the only input about what is scored. The forecast is read back through
    ``forecast.store.read_forecast_record`` (which re-verifies ``forecast_sha256``) and the
    outcome through :func:`latest_resolution` (which recomputes both digests), so no caller can
    hand this writer a probability or an outcome that disagrees with the ledger.

    What is scored is the record's ``final_prediction``. For binary and multiple choice the
    payload that was approved and posted is a verbatim copy of it
    (``submission_payload._binary_payload``/``_multiple_choice_payload``; D34 binds the post to
    that payload), so there is one subject and not two.

    What happens depends on the latest resolution and on the rows already written::

        no resolution, or latest not `resolved`          not_scorable   nothing written
        every current metric already scored against it   unchanged      nothing written
        otherwise                                        appended       the missing rows

    Appended rows cite the latest resolution. A record still ``resolved`` also takes the
    ``scored`` lifecycle event, linking the first row written (003 lets an event link exactly
    one). A record already ``scored`` -- the platform re-resolved it, or an implementation
    version changed -- gets rows only: 003 has no ``scored -> scored`` transition, and the rows
    are the record of it (M4-804 owns the lifecycle side of a re-resolution).

    ``not_scorable`` is this writer's courtesy. The refusal is the schema's:
    ``014_resolution_ingestion.sql`` refuses a score whose latest resolution is not scorable,
    and ``015_local_score_events.sql`` refuses one that cites a superseded observation.
    """
    identifier = _require_identifier(record_id, "record_id")
    computed = _require_utc(computed_at, "computed_at")

    with transaction(conn):
        _require_stored_record(conn, identifier)
        resolution = latest_resolution(conn, identifier)
        if resolution is None or not resolution.scorable:
            return ScoreWrite(outcome="not_scorable", scores=(), event=None, resolution=resolution)
        status = current_status(conn, identifier)
        if status not in _SCORABLE_STATUSES:
            raise LifecycleError(
                f"a score cannot be recorded for a record whose current status is {status}"
            )

        scores = _local_scores_for(conn, identifier, resolution.observation.outcome)
        existing = {
            (row[0], row[1])
            for row in _fetch_all(
                conn,
                "SELECT metric, implementation_version FROM score_events "
                "WHERE forecast_record_id = ? AND resolution_event_id = ?",
                (identifier, resolution.event_id),
            )
        }
        missing = [
            score
            for score in scores
            if (score.metric, score.implementation_version) not in existing
        ]
        if not missing:
            return ScoreWrite(outcome="unchanged", scores=(), event=None, resolution=resolution)
        if computed < resolution.observed_at_utc:
            raise LifecycleError(
                "computed_at is earlier than the observation the score is computed against"
            )

        event_ids = [
            _insert(
                conn,
                "INSERT INTO score_events (forecast_record_id, metric, value, "
                "implementation_version, comparison_baseline, computed_at_utc, "
                "resolution_event_id) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (
                    identifier,
                    score.metric,
                    score.value,
                    score.implementation_version,
                    computed,
                    resolution.event_id,
                ),
            )
            for score in missing
        ]
        event: LifecycleEvent | None = None
        if status == "resolved":
            event = _append_event(
                conn,
                record_id=identifier,
                event_type="scored",
                score_event_id=event_ids[0],
                occurred_at_utc=computed,
            )
        stored = tuple(
            _score_from_row(row)
            for row in _fetch_all(
                conn,
                f"SELECT {_SCORE_COLUMNS} FROM score_events WHERE event_id IN "
                f"({', '.join('?' for _ in event_ids)}) ORDER BY event_id",
                tuple(event_ids),
            )
        )
        return ScoreWrite(outcome="appended", scores=stored, event=event, resolution=resolution)


def read_local_scores(conn: sqlite3.Connection, record_id: str) -> tuple[StoredScore, ...]:
    """Every local score row for a record, in append order, each recomputed and checked.

    A value read back out of the ledger is untrusted, and a score is only an attribution claim
    while it is what its named implementation gives for the stored forecast and the stored
    outcome. So each row's cited resolution is re-verified (both digests) and the value is
    recomputed by :func:`scoring.recompute` under the row's own ``implementation_version`` and
    compared **exactly**; a mismatch, an unregistered version or a row whose metric and
    version disagree is refused. Platform rows are :func:`read_platform_scores`'.
    """
    identifier = _require_identifier(record_id, "record_id")
    _require_stored_record(conn, identifier)
    metrics = sorted(LOCAL_METRICS)
    rows = _fetch_all(
        conn,
        f"SELECT {_SCORE_COLUMNS} FROM score_events WHERE forecast_record_id = ? "
        f"AND metric IN ({', '.join('?' for _ in metrics)}) ORDER BY event_id",
        (identifier, *metrics),
    )
    if not rows:
        return ()
    binary, options = _prediction_inputs(conn, identifier)
    verified: list[StoredScore] = []
    for row in rows:
        stored = _score_from_row(row)
        resolution = _read_resolution(
            conn,
            "event_id = ? AND forecast_record_id = ?",
            (stored.resolution_event_id, identifier),
        )
        if resolution is None:
            raise LifecycleError("a stored score cites a resolution row this record does not have")
        prediction = binary if stored.metric.endswith("_binary") else options
        try:
            value = recompute(
                stored.implementation_version,
                stored.metric,
                prediction,
                resolution.observation.outcome,
            )
        except ScoreError as exc:
            raise LifecycleError(f"a stored score cannot be recomputed: {exc}") from None
        if value != stored.value:
            raise LifecycleError("a stored score does not match its recomputation")
        verified.append(stored)
    return tuple(verified)


def record_platform_scores(
    conn: sqlite3.Connection, *, record_id: str, computed_at: datetime
) -> PlatformScoreWrite:
    """Copy Metaculus's scores for a record out of its latest observation, atomically (M4-803).

    The record id is the only input. The scores are read from the latest resolution row's
    stored ``source_response`` -- the payload the platform returned when it reported the
    resolution, re-verified against its digest -- for the record's own question id, by
    :func:`platform_scores.extract_platform_scores`. Nothing is fetched and nothing is
    computed: a platform row is the platform's number and cites the evidence it was read
    from, so it replays exactly (D30).

    Every supported question type is recorded (owner decision 2026-09-23), and the outcomes
    are :func:`record_local_scores`' own::

        no resolution, or latest not `resolved`          not_scorable   nothing written
        every current metric already recorded for it     unchanged      nothing written
        otherwise                                        appended       the missing rows

    A scorable observation that carries no readable scores is a :class:`LifecycleError`, not
    ``not_scorable``: the resolution is definite, so a missing score is an attribution gap
    and the run must say so.

    A record still ``resolved`` takes the ``scored`` event, linking the first row written.
    For binary and multiple choice the caller runs :func:`record_local_scores` first, so the
    event keeps linking the local row it always has; for numeric and discrete -- which have
    no local score -- this writer is what moves the record to ``scored``.
    """
    identifier = _require_identifier(record_id, "record_id")
    computed = _require_utc(computed_at, "computed_at")

    with transaction(conn):
        question_id = _require_stored_question_id(conn, identifier)
        resolution = latest_resolution(conn, identifier)
        if resolution is None or not resolution.scorable:
            return PlatformScoreWrite(
                outcome="not_scorable", scores=(), event=None, resolution=resolution
            )
        status = current_status(conn, identifier)
        if status not in _SCORABLE_STATUSES:
            raise LifecycleError(
                f"a score cannot be recorded for a record whose current status is {status}"
            )
        source = _parsed_source_response(resolution)
        try:
            scores = extract_platform_scores(source, question_id)
        except PlatformScoreError as exc:
            # platform_scores.py's messages name rules and its own key names only.
            raise LifecycleError(f"the platform scores cannot be recorded: {exc}") from None
        existing = {
            (row[0], row[1])
            for row in _fetch_all(
                conn,
                "SELECT metric, implementation_version FROM score_events "
                "WHERE forecast_record_id = ? AND resolution_event_id = ?",
                (identifier, resolution.event_id),
            )
        }
        missing = [
            score
            for score in scores
            if (score.metric, score.implementation_version) not in existing
        ]
        if not missing:
            return PlatformScoreWrite(
                outcome="unchanged", scores=(), event=None, resolution=resolution
            )
        if computed < resolution.observed_at_utc:
            raise LifecycleError(
                "computed_at is earlier than the observation the score is computed against"
            )

        event_ids = [
            _insert(
                conn,
                "INSERT INTO score_events (forecast_record_id, metric, value, "
                "implementation_version, comparison_baseline, computed_at_utc, "
                "resolution_event_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    score.metric,
                    score.value,
                    score.implementation_version,
                    score.comparison_baseline,
                    computed,
                    resolution.event_id,
                ),
            )
            for score in missing
        ]
        event: LifecycleEvent | None = None
        if status == "resolved":
            event = _append_event(
                conn,
                record_id=identifier,
                event_type="scored",
                score_event_id=event_ids[0],
                occurred_at_utc=computed,
            )
        stored = tuple(
            _platform_score_from_row(row)
            for row in _fetch_all(
                conn,
                f"SELECT {_SCORE_COLUMNS} FROM score_events WHERE event_id IN "
                f"({', '.join('?' for _ in event_ids)}) ORDER BY event_id",
                tuple(event_ids),
            )
        )
        return PlatformScoreWrite(
            outcome="appended", scores=stored, event=event, resolution=resolution
        )


def read_platform_scores(
    conn: sqlite3.Connection, record_id: str
) -> tuple[StoredPlatformScore, ...]:
    """Every platform score row for a record, in append order, each re-read and checked.

    :func:`read_local_scores`' rule for the platform's numbers: each row's cited resolution is
    re-verified (both digests), its stored response is re-read under the row's own
    ``implementation_version`` for the record's question, and the value is compared
    **exactly**. A mismatch, an unregistered version, a row whose metric and version
    disagree, or a comparison baseline that is not the metric's is refused.
    """
    identifier = _require_identifier(record_id, "record_id")
    _require_stored_record(conn, identifier)
    metrics = sorted(PLATFORM_METRICS)
    rows = _fetch_all(
        conn,
        f"SELECT {_SCORE_COLUMNS} FROM score_events WHERE forecast_record_id = ? "
        f"AND metric IN ({', '.join('?' for _ in metrics)}) ORDER BY event_id",
        (identifier, *metrics),
    )
    if not rows:
        return ()
    question_id = _require_stored_question_id(conn, identifier)
    verified: list[StoredPlatformScore] = []
    for row in rows:
        stored = _platform_score_from_row(row)
        resolution = _read_resolution(
            conn,
            "event_id = ? AND forecast_record_id = ?",
            (stored.resolution_event_id, identifier),
        )
        if resolution is None:
            raise LifecycleError("a stored score cites a resolution row this record does not have")
        source = _parsed_source_response(resolution)
        try:
            value = recompute_platform(
                stored.implementation_version, stored.metric, source, question_id
            )
        except PlatformScoreError as exc:
            raise LifecycleError(f"a stored platform score cannot be re-read: {exc}") from None
        if value != stored.value:
            raise LifecycleError("a stored platform score does not match its cited observation")
        verified.append(stored)
    return tuple(verified)


def _require_stored_question_id(conn: sqlite3.Connection, record_id: str) -> int:
    row = _fetch_one(
        conn, "SELECT question_id FROM forecast_records WHERE record_id = ?", (record_id,)
    )
    if row is None:
        raise LifecycleError("record_id does not name a stored forecast record")
    return _stored_int(row[0], "question_id")


def _parsed_source_response(resolution: StoredResolution) -> object:
    """The stored post payload of a resolution row, parsed.

    ``resolution`` came from :func:`_resolution_from_row`, which verified this exact text
    against ``source_response_sha256``, so what is parsed is what was hashed. ``json.loads``
    is given no hook: a number too large for a double parses to an infinity, which
    ``platform_scores`` refuses as non-finite.
    """
    try:
        return cast(object, json.loads(resolution.source_response))
    except (ValueError, RecursionError):
        raise LifecycleError(
            "a stored resolution source response is not JSON "
            "(detail withheld: it can echo stored values)"
        ) from None


def _prediction_inputs(
    conn: sqlite3.Connection, record_id: str
) -> tuple[object, tuple[tuple[str, float], ...] | None]:
    """The stored forecast's ``final_prediction`` in the shapes ``scoring`` takes.

    Returns ``(probability_yes, None)`` for binary and ``(None, options)`` for multiple
    choice. Imported here rather than at module scope because ``forecast.store`` imports this
    module (``approval.approve`` defers its builder import for the same reason), and so that
    reading a status never loads the forecast schema stack.
    """
    from whiskeyjack_bot.forecast.record import ForecastRecordError
    from whiskeyjack_bot.forecast.schema import (
        BinaryForecastResponse,
        MultipleChoiceForecastResponse,
    )
    from whiskeyjack_bot.forecast.store import read_forecast_record

    try:
        record = read_forecast_record(conn, record_id)
    except ForecastRecordError:
        raise LifecycleError(
            "the forecast record cannot be read back for scoring "
            "(detail withheld: it can echo stored values)"
        ) from None
    # Dispatch on the literal, and re-check the concrete response type: DiscreteQuestion
    # subclasses NumericQuestion, and a stored record is untrusted.
    forecast = record.forecast
    if record.question_type == "binary" and type(forecast) is BinaryForecastResponse:
        return forecast.final_prediction.probability_yes, None
    if (
        record.question_type == "multiple_choice"
        and type(forecast) is MultipleChoiceForecastResponse
    ):
        return None, tuple(
            (entry.option, entry.probability) for entry in forecast.final_prediction.options
        )
    if record.question_type in ("numeric", "discrete"):
        raise LifecycleError(
            f"a {record.question_type} forecast has no local score (its scores are the "
            "platform's: record_platform_scores)"
        )
    raise LifecycleError("the stored forecast does not match its question type")


def _local_scores_for(
    conn: sqlite3.Connection, record_id: str, outcome: str | None
) -> tuple[LocalScore, ...]:
    binary, options = _prediction_inputs(conn, record_id)
    try:
        if options is None:
            return score_binary(binary, outcome)
        return score_multiple_choice(options, outcome)
    except ScoreError as exc:
        # scoring.py's messages name rules only.
        raise LifecycleError(f"the forecast cannot be scored: {exc}") from None


def record_pre_forecast_failure(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    question_id: int,
    tournament_id: str,
    event_type: PreForecastEventType,
    detail_code: PreForecastFailureCode,
    occurred_at: datetime,
    retrieval_run_id: str | None = None,
) -> PreForecastFailure:
    """Append one pre-forecast failure, atomically (M1-606).

    Scoped to ``attempt_id`` rather than a forecast record: no ``forecast_records`` row
    exists yet when a research or generation failure happens -- 001 requires
    ``final_prediction_json``, ``record_json`` and ``retrieval_run_id``, all of which
    exist only once generation has succeeded. ``attempt_id`` is minted once by the caller
    for the whole campaign toward one forecast version and reused across every retry, so
    more than one failure can share it; ``event_seq`` orders them the same way
    ``lifecycle_events.event_seq`` orders a forecast record's history.

    ``retrieval_run_id`` is required for ``'generation_failed'`` (generation only runs
    once research has completed, so there is always a run to cite) and optional for
    ``'research_failed'`` (a failure can occur before any ``research_runs`` row exists).

    If this campaign later succeeds, the resulting ``forecast_records`` row is stamped
    with this same ``attempt_id`` -- that write belongs to M1-602, not this function.
    What this module enforces, in ``004_pipeline_failure_events.sql``, is that an
    ``attempt_id`` already claimed by a successful record cannot also record a failure,
    and that an ``attempt_id``'s question/tournament cannot change once used.
    """
    identifier = _require_identifier(attempt_id, "attempt_id")
    qid = _require_int(question_id, "question_id")
    tid = _require_identifier(tournament_id, "tournament_id")
    _require_member(event_type, _PRE_FORECAST_EVENT_TYPES, "event_type")
    _require_member(detail_code, _PRE_FORECAST_FAILURE_CODES, "detail_code")
    occurred = _require_utc(occurred_at, "occurred_at")
    run_id = _require_optional_text(
        retrieval_run_id, "retrieval_run_id", max_length=MAX_IDENTIFIER_LENGTH
    )
    if event_type == "generation_failed" and run_id is None:
        raise LifecycleError("retrieval_run_id is required for a generation failure")

    with transaction(conn):
        _require_no_prior_success(conn, identifier)
        seq = _next_pipeline_failure_seq(conn, identifier)
        event_id = _insert(
            conn,
            "INSERT INTO pipeline_failure_events "
            "(attempt_id, event_seq, question_id, tournament_id, event_type, "
            "detail_code, retrieval_run_id, occurred_at_utc, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                seq,
                qid,
                tid,
                event_type,
                detail_code,
                run_id,
                occurred,
                _utc_text(_utcnow()),
            ),
        )
        row = _fetch_one(
            conn,
            f"SELECT {_PRE_FORECAST_FAILURE_COLUMNS} FROM pipeline_failure_events "
            "WHERE event_id = ?",
            (event_id,),
        )
        if row is None:  # pragma: no cover - the row was just inserted in this transaction
            raise LifecycleError("the recorded pipeline failure could not be read back")
        return _pre_forecast_failure_from_row(row)


def read_pipeline_failure_events(
    conn: sqlite3.Connection, attempt_id: str
) -> tuple[PreForecastFailure, ...]:
    """Return every recorded pre-forecast failure for an attempt, in append order.

    Unlike :func:`read_history`, an unknown ``attempt_id`` is **not** an error: an
    attempt_id has no independent identity row of its own to be "unknown" against --
    nothing creates one before the first event cites it, unlike a forecast record -- so
    an empty tuple is the honest answer whether the attempt never failed or never
    existed at all.
    """
    identifier = _require_identifier(attempt_id, "attempt_id")
    rows = _fetch_all(
        conn,
        f"SELECT {_PRE_FORECAST_FAILURE_COLUMNS} FROM pipeline_failure_events "
        "WHERE attempt_id = ? ORDER BY event_seq",
        (identifier,),
    )
    return tuple(_pre_forecast_failure_from_row(row) for row in rows)


_EVENT_COLUMNS = (
    "event_id, forecast_record_id, event_seq, event_type, from_status, to_status, "
    "detail_code, approval_event_id, submission_attempt_id, submission_verification_id, "
    "resolution_event_id, score_event_id, occurred_at_utc, created_at_utc, "
    # 016 (M2-713), appended where ADD COLUMN put it: the mapper indexes positionally.
    "submission_reconciliation_id"
)

# Spelled out rather than `SELECT *` for the reason _EVENT_COLUMNS is: the row mapper
# indexes positionally, so the order here is part of the contract and a later ALTER TABLE
# must not be able to silently reorder it. Matches PreForecastFailure's field order.
_PRE_FORECAST_FAILURE_COLUMNS = (
    "event_id, attempt_id, event_seq, question_id, tournament_id, event_type, "
    "detail_code, retrieval_run_id, occurred_at_utc, created_at_utc"
)


def _append_event(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    event_type: LifecycleEventType,
    detail_code: FailureCode | None = None,
    approval_event_id: int | None = None,
    submission_attempt_id: str | None = None,
    submission_verification_id: int | None = None,
    resolution_event_id: int | None = None,
    score_event_id: int | None = None,
    submission_reconciliation_id: str | None = None,
    occurred_at_utc: str,
) -> LifecycleEvent:
    """Append one lifecycle row, in a transaction, and return it as stored.

    ``from_status`` is read here rather than accepted from the caller: a caller that can
    assert its own starting point can skip a state. The destination follows from
    ``(event_type, from_status)`` via :data:`_DESTINATIONS`, so an illegal transition is
    a readable error before any statement runs -- and the migration's trigger re-derives
    the same thing against the row it is actually inserting, which is what makes it
    enforcement rather than agreement.

    The row is read back after insert rather than assembled from the arguments: what is
    returned is then what the ledger holds, including the values its own constraints
    accepted.
    """
    identifier = _require_identifier(record_id, "record_id")
    _require_member(event_type, _EVENT_TYPES, "event_type")

    with transaction(conn):
        from_status = current_status(conn, identifier)
        to_status = _DESTINATIONS.get((event_type, from_status))
        if to_status is None:
            # Both halves are vetted vocabulary members, so naming them is safe and is
            # the only thing that makes this actionable.
            raise LifecycleError(
                f"a {event_type} event is not a legal transition for a record whose "
                f"current status is {from_status}"
            )
        event_seq = _next_seq(conn, identifier)
        event_id = _insert(
            conn,
            "INSERT INTO lifecycle_events "
            "(forecast_record_id, event_seq, event_type, from_status, to_status, "
            "detail_code, approval_event_id, submission_attempt_id, "
            "submission_verification_id, resolution_event_id, score_event_id, "
            "submission_reconciliation_id, occurred_at_utc, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                event_seq,
                event_type,
                from_status,
                to_status,
                detail_code,
                approval_event_id,
                submission_attempt_id,
                submission_verification_id,
                resolution_event_id,
                score_event_id,
                submission_reconciliation_id,
                occurred_at_utc,
                _utc_text(_utcnow()),
            ),
        )
        row = _fetch_one(
            conn,
            f"SELECT {_EVENT_COLUMNS} FROM lifecycle_events WHERE event_id = ?",
            (event_id,),
        )
        if row is None:  # pragma: no cover - the row was just inserted in this transaction
            raise LifecycleError("the recorded lifecycle event could not be read back")
        return _event_from_row(row)


def _require_stored_record(conn: sqlite3.Connection, record_id: str) -> None:
    """Fail readably when an identifier names no stored forecast record."""
    row = _fetch_one(conn, "SELECT 1 FROM forecast_records WHERE record_id = ?", (record_id,))
    if row is None:
        raise LifecycleError("record_id does not name a stored forecast record")


def _require_no_prior_success(conn: sqlite3.Connection, attempt_id: str) -> None:
    """Fail readably when an attempt has already produced a forecast record (M1-606).

    The writer-side twin of ``pipeline_failure_events_validate_on_insert``'s
    prior-success probe. The trigger is the enforcement -- it holds against a raw INSERT
    that never reaches this module -- and this is what turns the same refusal into a
    :class:`LifecycleError` with an actionable message instead of the opaque
    ``the ledger rejected this write`` :func:`_insert` is obliged to raise. Both must
    hold; neither is redundant, for the reason 003 gives about its own paired probes.

    Success and failure are both terminal for one ``attempt_id``: an attempt that
    produced a forecast version cannot afterwards be recorded as having failed, or the
    ledger would show a campaign that both succeeded and did not.
    """
    row = _fetch_one(conn, "SELECT 1 FROM forecast_records WHERE attempt_id = ?", (attempt_id,))
    if row is not None:
        raise LifecycleError("attempt_id has already produced a stored forecast record")


def _require_payload_digest(value: object, decision: str) -> str | None:
    """Gate ``payload_sha256`` against the decision it belongs to (M2-707, migration 011).

    Two rules, and the asymmetry is the design rather than an omission. An approval
    authorizes exactly one submission payload, so it carries that payload's hash; a
    rejection authorizes nothing, so it carries none -- and, more to the point, the payload
    is *derived*, so a record whose numeric CDF will not convert has no payload hash to
    offer and must still be rejectable. Requiring it on one side and forbidding it on the
    other also gives a NULL exactly one meaning per decision: on an approval it is a row
    written before `011`, which is what lets the submission gate refuse those rather than
    guess.

    Both rules are `011`'s trigger clauses restated, for the reason every other validator
    here restates one: a caller gets a field-level message instead of a constraint
    violation.
    """
    if decision == "approved":
        if value is None:
            raise LifecycleError(
                "payload_sha256 is required for an approval: an approval binds to the "
                "submission payload it authorizes"
            )
        return _require_sha256(value, "payload_sha256")
    if value is not None:
        raise LifecycleError(
            "payload_sha256 must be omitted for a rejection: a rejection authorizes no "
            "submission payload"
        )
    return None


def _require_hash_binds(conn: sqlite3.Connection, record_id: str, digest: str) -> None:
    """Fail readably when an approval names a hash the record does not have."""
    row = _fetch_one(
        conn, "SELECT forecast_sha256 FROM forecast_records WHERE record_id = ?", (record_id,)
    )
    if row is None:
        raise LifecycleError("record_id does not name a stored forecast record")
    if row[0] is None:
        raise LifecycleError(
            "this forecast record stores no content hash and so cannot be approved"
        )
    if row[0] != digest:
        # Neither hash is printed: one is a stored value, and printing the other would
        # let a caller confirm a guess against it.
        raise LifecycleError(
            "forecast_sha256 does not match the stored hash of this forecast record; "
            "the forecast changed and any prior approval no longer binds"
        )


def _require_assertion_text(value: object, field: str, *, max_length: int) -> str:
    """Non-blank storable text with no NUL: a person's name, or what they said they saw.

    :func:`_require_text` with :func:`_require_identifier`'s two extra refusals, at a
    caller-chosen bound. An ``approve`` note may be blank or absent; this one may not, because
    the row it sits on exists to record an assertion, and a blank assertion is none. NUL is
    refused for 004's reason -- SQLite's ``length()`` stops at it, so ``016``'s bound would
    not see past one.
    """
    text = _require_text(value, field, max_length=max_length)
    if not text.strip():
        raise LifecycleError(f"{field} must not be blank")
    if "\x00" in text:
        raise LifecycleError(f"{field} must not contain a NUL character")
    return text


def _require_confirming_snapshot(value: object) -> str:
    """Refetch evidence whose recorded outcome is ``confirmed``, or raise.

    ``016``'s clause, restated: the snapshot is the program's half of the evidence and the
    half that carries the record to ``submitted``, so a snapshot recording any other verdict,
    or none, is refused. Parsed only to read one member; the rest is
    ``submission_live.build_verification_snapshot``'s published shape and is not interpreted
    here.
    """
    text = _require_text(
        value, "reconciliation.refetched_forecast_snapshot", max_length=MAX_BODY_LENGTH
    )
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        parsed = None
    if not isinstance(parsed, dict) or parsed.get("outcome") != "confirmed":
        raise LifecycleError(
            "reconciliation.refetched_forecast_snapshot must record a confirming refetch; a "
            "reconciliation is only auditable if it stores what the platform showed"
        )
    return text


def _require_reconcilable(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    reservation_id: str,
    digest: str,
    intent_event_id: str,
    refetched_at: str,
) -> str:
    """Fail readably when a reconciliation could not describe an unrecorded post; return the
    attempt id its post carried, derived from the key on the stored reservation row.

    ``016``'s probes in the trigger's order, each with a message that says what to do. The
    trigger is the enforcement; see :func:`_require_verifiable_attempt` for why both hold.
    Timestamps are compared as text for that function's reason: both sides are the canonical
    fixed-width form.
    """
    _require_stored_record(conn, record_id)
    reservation = _fetch_one(
        conn,
        "SELECT idempotency_key, reserved_at_utc FROM submission_key_reservations "
        "WHERE reservation_id = ? AND forecast_record_id = ?",
        (reservation_id, record_id),
    )
    if reservation is None:
        raise LifecycleError(
            "reconciliation.reservation_id does not name a key reservation held against this "
            "forecast record"
        )
    attempt_id = live_attempt_id_for_key(_stored_text(reservation[0], "idempotency_key"))
    if _fetch_one(
        conn, "SELECT 1 FROM submission_key_releases WHERE reservation_id = ?", (reservation_id,)
    ):
        raise LifecycleError(
            "this reservation was released, which records that nothing was posted under it"
        )
    if _fetch_one(
        conn,
        "SELECT 1 FROM submission_reconciliations WHERE reservation_id = ? OR attempt_id = ?",
        (reservation_id, attempt_id),
    ):
        raise LifecycleError("this post has already been reconciled")
    if _fetch_one(
        conn,
        "SELECT 1 FROM submission_attempts WHERE idempotency_key = ? OR attempt_id = ?",
        (reservation[0], attempt_id),
    ):
        raise LifecycleError(
            "a submission attempt already records the post made under this reservation; "
            "there is nothing unrecorded to reconcile"
        )
    if not _fetch_one(
        conn,
        "SELECT 1 FROM approval_events WHERE forecast_record_id = ? AND decision = 'approved' "
        "AND payload_sha256 = ?",
        (record_id, digest),
    ):
        # Neither digest is printed, `_require_hash_binds`'s rule.
        raise LifecycleError(
            "reconciliation.request_payload_sha256 is not the payload this record's approval "
            "authorized"
        )
    status = current_status(conn, record_id)
    if status != "approved":
        # A vetted vocabulary member, so naming it is safe and actionable.
        raise LifecycleError(
            f"this forecast record is {status}, not awaiting submission, so there is no "
            "unrecorded post to reconcile"
        )
    if unresolved_uncertainties(conn, record_id):
        raise LifecycleError(
            "this record holds a submission attempt whose outcome is unresolved; resolve it "
            "with verify-submission first"
        )
    if not _fetch_one(
        conn,
        "SELECT 1 FROM tournament_events WHERE event_id = ? AND kind = 'forecast_intent' "
        "AND scope = ?",
        (intent_event_id, record_id),
    ):
        raise LifecycleError(
            "reconciliation.intent_event_id does not name this record's durable submission intent"
        )
    if refetched_at < _stored_text(reservation[1], "reserved_at_utc"):
        raise LifecycleError(
            "reconciliation.refetched_at_utc is earlier than the reservation it reconciles"
        )
    return attempt_id


def _require_verifiable_attempt(
    conn: sqlite3.Connection, record_id: str, attempt_id: str, observed_at_utc: str
) -> None:
    """Fail readably when a refetch names an attempt it cannot be resolving.

    Both halves are re-derived by the migration's trigger, which is the binding check.
    The attempt must be one *this* record recorded as uncertain -- which subsumes
    ownership, since the uncertain event names both -- and the observation cannot predate
    the attempt it observes.

    The ordering comparison is left to SQL rather than parsed here, because the stored
    value is text this module did not necessarily write and ``fromisoformat`` on it would
    be one more place a stored value can raise. It compares TEXT, not ``julianday``: a
    float day number cannot represent microseconds, so that comparison called two instants
    a microsecond apart equal (round 4, finding 3). Both sides are the canonical fixed-width
    UTC form :func:`_require_utc` renders and the migration pins, which is what makes a
    text comparison exact.
    """
    row = _fetch_one(
        conn,
        "SELECT 1 FROM lifecycle_events WHERE forecast_record_id = ? "
        "AND submission_attempt_id = ? AND event_type = 'submission_uncertain' LIMIT 1",
        (record_id, attempt_id),
    )
    if row is None:
        raise LifecycleError(
            "this record has no uncertain submission attempt by that identifier, so there "
            "is nothing for a refetch to resolve"
        )
    row = _fetch_one(
        conn,
        "SELECT 1 FROM submission_attempts WHERE attempt_id = ? AND completed_at_utc > ?",
        (attempt_id, observed_at_utc),
    )
    if row is not None:
        raise LifecycleError(
            "verification.observed_at_utc is earlier than the completion of the attempt it verifies"
        )


def _next_seq(conn: sqlite3.Connection, record_id: str) -> int:
    row = _fetch_one(
        conn,
        "SELECT max(event_seq) FROM lifecycle_events WHERE forecast_record_id = ?",
        (record_id,),
    )
    if row is None or row[0] is None:
        return 1
    if type(row[0]) is not int:
        # A non-integer event_seq means the column's affinity was defeated by a writer
        # that bypassed this module; the value itself is stored content and stays unnamed.
        raise LifecycleError(
            "the stored lifecycle sequence is malformed "
            "(detail withheld: it can echo stored values)"
        )
    return row[0] + 1


def _next_pipeline_failure_seq(conn: sqlite3.Connection, attempt_id: str) -> int:
    """:func:`_next_seq` for ``pipeline_failure_events``, scoped to an attempt (M1-606).

    Separate from :func:`_next_seq` rather than parameterized over table and key column:
    the two sequences count different things over different identity spaces, and a shared
    helper taking a table name would put an f-stringed identifier into SQL for no gain.

    Racing writers are not a correctness concern here for the reason they are not one for
    :func:`_next_seq`: ``UNIQUE (attempt_id, event_seq)`` is what actually decides, and a
    loser gets a refused write rather than a duplicated sequence number.
    """
    row = _fetch_one(
        conn,
        "SELECT max(event_seq) FROM pipeline_failure_events WHERE attempt_id = ?",
        (attempt_id,),
    )
    if row is None or row[0] is None:
        return 1
    if type(row[0]) is not int:
        # See _next_seq: affinity is not a type, and the offending value is stored
        # content that this message must not name.
        raise LifecycleError(
            "the stored pipeline failure sequence is malformed "
            "(detail withheld: it can echo stored values)"
        )
    return row[0] + 1


def _event_from_row(row: sqlite3.Row) -> LifecycleEvent:
    """Build the value object from a stored row, gating every vocabulary field."""
    return LifecycleEvent(
        event_id=_stored_int(row[0], "event_id"),
        forecast_record_id=_stored_text(row[1], "forecast_record_id"),
        event_seq=_stored_int(row[2], "event_seq"),
        event_type=cast(LifecycleEventType, _require_member(row[3], _EVENT_TYPES, "event_type")),
        from_status=cast(LifecycleStatus, _require_member(row[4], _STATUSES, "from_status")),
        to_status=cast(LifecycleStatus, _require_member(row[5], _STATUSES, "to_status")),
        detail_code=(
            None
            if row[6] is None
            else cast(FailureCode, _require_member(row[6], _FAILURE_CODES, "detail_code"))
        ),
        approval_event_id=None if row[7] is None else _stored_int(row[7], "approval_event_id"),
        submission_attempt_id=(
            None if row[8] is None else _stored_text(row[8], "submission_attempt_id")
        ),
        submission_verification_id=(
            None if row[9] is None else _stored_int(row[9], "submission_verification_id")
        ),
        resolution_event_id=None
        if row[10] is None
        else _stored_int(row[10], "resolution_event_id"),
        score_event_id=None if row[11] is None else _stored_int(row[11], "score_event_id"),
        submission_reconciliation_id=(
            None if row[14] is None else _stored_text(row[14], "submission_reconciliation_id")
        ),
        occurred_at_utc=_stored_text(row[12], "occurred_at_utc"),
        created_at_utc=_stored_text(row[13], "created_at_utc"),
    )


def _pre_forecast_failure_from_row(row: sqlite3.Row) -> PreForecastFailure:
    """Build the value object from a stored row, gating every vocabulary field (M1-606).

    Every field is re-validated on the way out even though the schema's own ``CHECK``
    constraints accepted it on the way in. That is not belt-and-braces: values read back
    out of the ledger are untrusted per CLAUDE.md's threat boundary, the database file is
    ordinary local state, and a row written by something other than this module is
    exactly the case the ``typeof()``-style guards elsewhere in this file exist for. A
    row that cannot be gated arrives as a :class:`LifecycleError` naming only the field.
    """
    return PreForecastFailure(
        event_id=_stored_int(row[0], "event_id"),
        attempt_id=_stored_text(row[1], "attempt_id"),
        event_seq=_stored_int(row[2], "event_seq"),
        question_id=_stored_int(row[3], "question_id"),
        tournament_id=_stored_text(row[4], "tournament_id"),
        event_type=cast(
            PreForecastEventType, _require_member(row[5], _PRE_FORECAST_EVENT_TYPES, "event_type")
        ),
        detail_code=cast(
            PreForecastFailureCode,
            _require_member(row[6], _PRE_FORECAST_FAILURE_CODES, "detail_code"),
        ),
        retrieval_run_id=(None if row[7] is None else _stored_text(row[7], "retrieval_run_id")),
        occurred_at_utc=_stored_text(row[8], "occurred_at_utc"),
        created_at_utc=_stored_text(row[9], "created_at_utc"),
    )


_RESOLUTION_COLUMNS = (
    "event_id, forecast_record_id, resolution_snapshot_json, source_response, "
    "observation_sha256, source_response_sha256, observed_at_utc, ingested_at_utc, "
    "resolution_kind, scorable, outcome"
)


_SCORE_COLUMNS = (
    "event_id, forecast_record_id, resolution_event_id, metric, value, "
    "implementation_version, comparison_baseline, computed_at_utc"
)


def _score_from_row(row: sqlite3.Row) -> StoredScore:
    """Gate one ``score_events`` row's shape. The value check is :func:`read_local_scores`'s."""
    metric = _require_member(row[3], LOCAL_METRICS, "metric")
    value = row[4]
    if type(value) is not float:
        raise LifecycleError(
            "stored score value is not a real number (detail withheld: it can echo stored values)"
        )
    if row[6] is not None:
        raise LifecycleError("a stored local score carries a comparison baseline")
    return StoredScore(
        event_id=_stored_int(row[0], "event_id"),
        forecast_record_id=_stored_text(row[1], "forecast_record_id"),
        resolution_event_id=_stored_int(row[2], "resolution_event_id"),
        metric=cast(LocalMetric, metric),
        value=value,
        implementation_version=_stored_text(row[5], "implementation_version"),
        computed_at_utc=_stored_text(row[7], "computed_at_utc"),
    )


def _platform_score_from_row(row: sqlite3.Row) -> StoredPlatformScore:
    """Gate one platform ``score_events`` row's shape. The value check is the reader's."""
    metric = cast(PlatformMetric, _require_member(row[3], PLATFORM_METRICS, "metric"))
    value = row[4]
    if type(value) is not float:
        raise LifecycleError(
            "stored score value is not a real number (detail withheld: it can echo stored values)"
        )
    if row[6] != COMPARISON_BASELINES[metric]:
        raise LifecycleError("a stored platform score does not carry its metric's baseline")
    return StoredPlatformScore(
        event_id=_stored_int(row[0], "event_id"),
        forecast_record_id=_stored_text(row[1], "forecast_record_id"),
        resolution_event_id=_stored_int(row[2], "resolution_event_id"),
        metric=metric,
        value=value,
        implementation_version=_stored_text(row[5], "implementation_version"),
        comparison_baseline=COMPARISON_BASELINES[metric],
        computed_at_utc=_stored_text(row[7], "computed_at_utc"),
    )


def _read_resolution(
    conn: sqlite3.Connection, where: str, parameters: tuple[object, ...]
) -> StoredResolution | None:
    row = _fetch_one(
        conn, f"SELECT {_RESOLUTION_COLUMNS} FROM resolution_events WHERE {where}", parameters
    )
    return None if row is None else _resolution_from_row(row)


def _resolution_from_row(row: sqlite3.Row) -> StoredResolution:
    """Rebuild and re-verify a stored resolution.

    The digests are recomputed from the stored text and the snapshot is re-validated,
    because a value read back out of the ledger is untrusted and a score computed from a row
    whose content no longer matches its hash would be attributed to evidence nobody stored.
    """
    snapshot = _stored_text(row[2], "resolution_snapshot_json")
    source = _stored_text(row[3], "source_response")
    observation_digest = _stored_text(row[4], "observation_sha256")
    source_digest = _stored_text(row[5], "source_response_sha256")
    try:
        observation = observation_from_snapshot(snapshot)
        replayed = observation.snapshot_json()
        snapshot_ok = sha256_text(snapshot) == observation_digest and replayed == snapshot
        source_ok = sha256_text(source) == source_digest
    except (ResolutionError, UnicodeEncodeError):
        raise LifecycleError(
            "a stored resolution snapshot is malformed (detail withheld: it can echo stored values)"
        ) from None
    if not snapshot_ok:
        raise LifecycleError("a stored resolution snapshot does not match its recorded digest")
    if not source_ok:
        raise LifecycleError("a stored resolution source response does not match its digest")
    if (
        row[8] != observation.kind
        or row[9] != (1 if observation.scorable else 0)
        or row[10] != observation.outcome
    ):
        raise LifecycleError("a stored resolution row disagrees with its own snapshot")
    return StoredResolution(
        event_id=_stored_int(row[0], "event_id"),
        forecast_record_id=_stored_text(row[1], "forecast_record_id"),
        observation=observation,
        observation_sha256=observation_digest,
        source_response_sha256=source_digest,
        observed_at_utc=_stored_text(row[6], "observed_at_utc"),
        ingested_at_utc=_stored_text(row[7], "ingested_at_utc"),
        source_response=source,
    )


def _stored_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise LifecycleError(
            f"stored {field} is not an integer (detail withheld: it can echo stored values)"
        )
    return value


def _stored_text(value: object, field: str) -> str:
    if type(value) is not str:
        raise LifecycleError(
            f"stored {field} is not text (detail withheld: it can echo stored values)"
        )
    return value


def _insert(conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]) -> int:
    """Execute one INSERT and return its rowid, wrapping every database failure.

    Callers only handle this module's own error type, so a raw ``sqlite3.Error`` --
    including the ``IntegrityError`` a trigger raises -- must not escape. The database's
    text is not forwarded: SQLite's constraint messages name tables and columns rather
    than values today, but that is a property of the engine's formatting and not a
    contract, and this module's guarantee is not allowed to depend on it. The
    actionable cases (illegal transition, unknown record, hash mismatch, malformed
    field) are all raised with their own messages before the statement runs.

    ``OverflowError`` is caught alongside ``sqlite3.Error`` because it is not one:
    ``sqlite3`` raises it while *binding* a Python int too large for a signed 64-bit
    column, before any database code runs. :func:`_require_optional_int` now rejects
    those at the field, so this is the second line -- but the two must both hold, since
    a later writer could pass an integer that never went through that validator.
    """
    try:
        cursor = conn.execute(sql, parameters)
    except (sqlite3.Error, OverflowError):
        # from None: the underlying error's text and traceback can carry stored values.
        raise LifecycleError(
            "the ledger rejected this write (detail withheld: a database message can "
            "echo stored values)"
        ) from None
    rowid = cursor.lastrowid
    if rowid is None:  # pragma: no cover - INSERT always sets lastrowid
        raise LifecycleError("the ledger did not report an identifier for this write")
    return rowid


def _fetch_one(
    conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> sqlite3.Row | None:
    try:
        row = conn.execute(sql, parameters).fetchone()
    except (sqlite3.Error, OverflowError):  # OverflowError: see _insert
        raise LifecycleError(
            "the ledger could not be read (detail withheld: a database message can "
            "echo stored values)"
        ) from None
    return cast("sqlite3.Row | None", row)


def _fetch_all(
    conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> list[sqlite3.Row]:
    try:
        rows = conn.execute(sql, parameters).fetchall()
    except (sqlite3.Error, OverflowError):  # OverflowError: see _insert
        raise LifecycleError(
            "the ledger could not be read (detail withheld: a database message can "
            "echo stored values)"
        ) from None
    return cast("list[sqlite3.Row]", rows)
