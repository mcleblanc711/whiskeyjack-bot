"""The submission seam, and the gateway that posts nothing (M2-703).

``CODEX_HANDOFF.md`` asks for a :class:`SubmissionGateway` protocol *owned by this
repository* returning a sanitized :class:`SubmissionReceipt`, with two implementations:
``DryRunSubmissionGateway``, which never contacts Metaculus and returns a deterministic
receipt, and ``MetaculusSubmissionGateway`` (M2-704). This module is the seam plus the
first of the two.

**A dry run does not write a ``submission_attempts`` row, and it must not.** Two
independent reasons, either one fatal:

- ``001`` declares ``idempotency_key TEXT NOT NULL UNIQUE``. A dry-run row spends the key
  the real submission needs, so the live post that follows could never be recorded. A live
  post the ledger cannot record is this product's primary failure mode -- the same one
  M1-603 round 4 withdrew its retry block over.
- :func:`lifecycle.record_submission_attempt` *always* appends a lifecycle event, and
  derives its type from ``(success, verified_by_refetch)``. ``(True, True)`` is
  ``submitted``, which would be a lie about a post that never happened; every other pair
  is ``submission_uncertain`` or ``submission_failed``, and ``submission_failed`` moves
  the record to terminal ``failed``. There is no honest event for "nothing was posted",
  and ``_LEGAL_TRANSITIONS`` admits none of them from ``draft`` -- which is exactly where
  a record sits when a dry run is most useful.

So the backlog's *"dry run records payload/hash"* is satisfied by a **file**, not a row:
the receipt and the payload it hashed are written under ``storage.artifact_root``, and the
ledger is untouched. :func:`attempt_from_receipt` is the only door from a receipt into the
ledger and it **refuses a dry-run receipt**, so "a dry run can never be recorded as a
submission attempt" is a tested guard rather than a property of absence.

**Deterministic** means what it says. Given the same :class:`SubmissionRequest` and the
same clock readings, :meth:`DryRunSubmissionGateway.submit` returns a byte-identical
receipt: ``attempt_id`` is derived from the idempotency key rather than minted as a
``uuid4``, and the clock is injected. The derived id also carries its own visible scheme
tag (``wjdry-1-``) for :data:`submission.KEY_SCHEMA_VERSION`'s reason -- a reader must be
able to tell a dry-run identity from a live one without consulting anything else.

**The payload hash is computed here, from the payload the receipt was handed.** It is
never accepted alongside the payload, because a receipt that could claim a digest for a
payload it never saw is not evidence of anything. The rendering is M1-305's rule verbatim,
the same spelling :func:`submission.canonical_key_json` and ``research/packet.py`` use:
``json.dumps(..., ensure_ascii=True, sort_keys=True, separators=(",", ":"),
allow_nan=False)``, then SHA-256 of its UTF-8 encoding. ``ensure_ascii`` escapes lone
surrogates rather than failing to encode them, which is also what makes the artifact
writable. ``research.hashing.content_sha256`` is deliberately *not* reused: it collapses
whitespace runs, which is meaning-preserving for article prose and structurally wrong for
a JSON body.

**What this module does not do.** It does not build the payload -- M1-502/M1-503 own that,
and the payload is an input here. It does not derive the idempotency key: that needs a
connection, and the choice between :func:`submission.submission_key_for_record` (which
admits a ``draft``, because that is what a dry run needs) and
:func:`submission.submission_key_for_approved_record` is the caller's to make, for the
reason M1-402 settled -- a bound any caller can lift is not a bound. It does not call
:func:`submission.require_key_unused` either; that is the caller's check before deciding
to post, and keeping this module free of a ledger connection is what makes both the
determinism claim and the zero-network claim provable rather than asserted.

Error hygiene follows the project rule: :class:`GatewayError` never echoes a
caller-supplied or stored value, sanitizing raises use ``from None``, and every malformed
shape arrives as a :class:`GatewayError`. It subclasses :class:`submission.SubmissionError`
so a caller already handling the submission seam's error type handles this one too.
Filesystem paths *are* rendered, uniformly with ``config.py``/``ledger.py``/
``research/artifacts.py`` under the settled M1-401 carve-out.

Purely local: nothing here contacts Metaculus, and nothing here imports a network client.
``submission.enabled: false``, ``dry_run: true`` and ``no_submit: true`` remain the
committed defaults, and this module reads no configuration -- a gateway that posts nothing
is legal under all three, and relaxing that gate is M2-704's change to make.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol, get_args

from whiskeyjack_bot.lifecycle import SubmissionAttempt
from whiskeyjack_bot.submission import KEY_LENGTH, SubmissionError, submission_key

# Bumping this changes the envelope a reader must understand; it is not the payload's
# schema, which belongs to whatever built the payload.
ARTIFACT_SCHEMA_VERSION = "1.0.0"

# Which gateway produced a receipt. A closed vocabulary rather than a bool, because
# `attempt_from_receipt` dispatches on it and M2-704 adds no third member by accident:
# `get_args` below is what a new member has to pass through.
GatewayMode = Literal["dry_run", "live"]

_GATEWAY_MODES: frozenset[str] = frozenset(get_args(GatewayMode))

# The visible scheme tag on a derived dry-run attempt id, written as a literal for the
# reason `submission._KEY_PREFIX` is: a computed tag agrees with its version by
# construction and proves nothing. `_assert_prefix_is_distinct` checks the one property
# that matters -- it cannot be confused with a submission key.
_DRY_RUN_ATTEMPT_PREFIX = "wjdry-1-"

# Where dry-run artifacts live under `storage.artifact_root`. Two components, so that
# `submissions/live/...` (M2-704) and `research/...` (M1-306) each keep their own
# namespace and a reader can tell what a file is from its path alone.
_SUBMISSIONS_SUBDIR = "submissions"
_DRY_RUN_SUBDIR = "dry_run"

# An idempotency key becomes a path component, so it is constrained to characters that
# cannot escape the artifact root or name a directory entry with a meaning of its own.
# Copied from `research/artifacts._SAFE_RUN_ID_RE` and for its stated reason: this refuses
# a *caller mistake* before any I/O, not an attack -- the operator is not the adversary
# (CLAUDE.md's threat boundary). Every key `submission.submission_key` mints passes it.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Matches `lifecycle._MAX_IDENTIFIER`, `approval._MAX_IDENTIFIER` and
# `submission._MAX_IDENTIFIER`. Re-spelled rather than imported for the reason those three
# already are -- a private constant imported to assert against tests the constant, not the
# writer that enforces it (M1-303) -- and **M1-608 is the filed item that pins them
# together.** A test puts a receipt through `lifecycle.record_submission_attempt` rather
# than comparing the numbers.
_MAX_IDENTIFIER = 200

# SQLite stores signed 64-bit integers, and `question_id` reaches `forecast_records`
# through every caller of this seam. Same reasoning as `submission._INT64_MAX`.
_INT64_MAX = 2**63 - 1

_HEX_DIGITS: frozenset[str] = frozenset("0123456789abcdef")

# How deep a payload may nest. This is not a size preference: it is what makes the
# validator below safe to write recursively. A self-referential payload has no finite
# depth, so the cap refuses it without a visited-set -- and a visited-set would have had to
# choose between refusing a *shared* sub-object (legal JSON, built by any sane payload
# builder that reuses a constant) and tracking identity per path. The cap also keeps
# `json.dumps` clear of its own recursion limit, so a RecursionError cannot arrive from
# the C encoder with a traceback nobody sanitized.
_MAX_PAYLOAD_DEPTH = 64


class GatewayError(SubmissionError):
    """A receipt cannot be produced, recorded, or converted into a ledger attempt.

    Subclasses :class:`submission.SubmissionError` deliberately. The module-owns-its-error
    rule exists so a caller never has to handle a foreign type; here the caller is already
    handling the submission seam, and a subclass satisfies both halves at once -- ``except
    GatewayError`` still distinguishes this module, and ``except SubmissionError`` still
    catches the whole seam.

    Same hygiene rule as its base: the message never echoes a caller-supplied value, a
    payload, or an underlying exception's text, and sanitizing raises use ``from None`` so
    nothing can be reprinted through a cause chain or a rendered traceback. Filesystem
    paths are the M1-401 carve-out and are rendered.
    """


def _assert_prefix_is_distinct() -> None:
    """Fail at import if a dry-run attempt id could be mistaken for a submission key.

    The two derived identifiers are both ``<tag>-<64 hex>`` and both are minted from the
    same material, so the *only* thing keeping them apart is that their tags differ. If
    they ever agreed, a dry-run attempt id would read as a spent idempotency key and a
    spent key would read as a dry run -- and both are append-only claims about whether a
    live post happened.

    At import rather than in a test, for ``submission._assert_prefix_matches_version``'s
    reason: a guard only a test enforces is a guard the next module to import this one
    does not have.
    """
    probe = submission_key(
        tournament_id="probe",
        question_id=1,
        forecast_version=1,
        request_payload_sha256="0" * 64,
    )
    if probe.startswith(_DRY_RUN_ATTEMPT_PREFIX) or _DRY_RUN_ATTEMPT_PREFIX.startswith(
        probe[: len(_DRY_RUN_ATTEMPT_PREFIX)]
    ):
        # Module literals, not caller or row content, so naming them is safe -- and it is
        # the only thing that makes the failure fixable.
        raise GatewayError(
            f"the dry-run attempt prefix {_DRY_RUN_ATTEMPT_PREFIX!r} is not distinct from "
            "the idempotency-key prefix; the two identity spaces must not overlap"
        )
    if len(_DRY_RUN_ATTEMPT_PREFIX) + 64 > _MAX_IDENTIFIER or KEY_LENGTH > _MAX_IDENTIFIER:
        raise GatewayError(
            "a derived identifier is longer than the "
            f"{_MAX_IDENTIFIER}-character ledger identifier limit"
        )


_assert_prefix_is_distinct()


@dataclass(frozen=True)
class SubmissionRequest:
    """What a gateway is asked to submit.

    Four values, and each is here because *any* gateway needs it: the record the receipt
    attributes to, the question a live post addresses, the key that makes the post
    idempotent, and the payload itself. ``tournament_id`` and ``forecast_version`` are not
    here -- they are already inside the key (``submission.canonical_key_json``), and a
    second spelling of a value that must agree with the key is a value that can disagree
    with it.

    **Not validated at construction.** ``payload`` is a :class:`Mapping`, which the caller
    can mutate afterwards, so a constructor check would be a bound that decays between
    construction and use. :meth:`DryRunSubmissionGateway.submit` validates what it reads,
    when it reads it, and reads each field exactly once.
    """

    forecast_record_id: str
    question_id: int
    idempotency_key: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class SubmissionReceipt:
    """The sanitized record of one submission, real or rehearsed.

    The handoff's field list, plus two this module adds:

    - ``mode`` -- which gateway produced it. Without it a dry-run receipt is
      indistinguishable from a live attempt that failed, and :func:`attempt_from_receipt`
      would have nothing to refuse on.
    - ``artifact_path`` -- where the receipt was written, **relative to**
      ``storage.artifact_root``, so a record stays readable after the artifact directory
      moves or is opened on another machine (``research/artifacts.py``'s rule).

    Deliberately **not** :class:`lifecycle.SubmissionAttempt`, which that class's own
    docstring already anticipates. Keeping them separate is what stops a persistence
    concern (column set, size caps) from being decided on behalf of the submission seam,
    and vice versa; :func:`attempt_from_receipt` is the mapping, and it is one-way.

    Field order mirrors ``SubmissionAttempt`` so the mapping reads as a transcription.
    ``created_at_utc`` is absent for that class's reason: it records when the *ledger*
    stored the row, so only the write path may set it.
    """

    mode: GatewayMode
    attempt_id: str
    forecast_record_id: str
    idempotency_key: str
    requested_at_utc: datetime
    completed_at_utc: datetime
    request_payload_sha256: str
    success: bool
    verified_by_refetch: bool
    http_status: int | None = None
    response_body: str | None = None
    response_headers: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    refetched_forecast_snapshot: str | None = None
    artifact_path: str | None = None


class SubmissionGateway(Protocol):
    """What a submission gateway is, from the pipeline's side.

    One member, narrow on purpose -- ``forecast/generate.py``'s ``Forecaster`` protocol
    makes the same argument: a test double implements one method instead of subclassing a
    class whose constructor reaches for a network client.

    The protocol says nothing about *when* a post is legal. Approval (M2-701), key
    derivation and the spent-key check (M2-702) all happen before a gateway is called, and
    putting them behind this method would put the approval boundary inside the thing the
    boundary exists to gate.
    """

    def submit(self, request: SubmissionRequest) -> SubmissionReceipt: ...


def canonical_payload_json(payload: Mapping[str, object]) -> str:
    """Return the exact string :func:`payload_sha256` digests.

    Exposed for ``packet.canonical_packet_json``'s reason: a hash that cannot be inspected
    is a hash whose disagreements cannot be explained. Every input is validated here, so
    this is also the single place the accepted payload domain is defined.

    The accepted domain is defined as *"survives its own canonical rendering"* rather
    than as a list of characters to look for -- see the guard below, and
    :func:`_require_json_object` for the structural half of the same rule.

    **Changing this rendering breaks replay** and, worse, changes every idempotency key
    derived from a payload -- so a re-run over identical work would claim a second live
    post. If the rule must ever change it changes as a new versioned function alongside
    this one, never as an edit to it. The same sentence appears over
    ``hashing.content_sha256``, ``packet.packet_sha256`` and ``submission.submission_key``.
    """
    validated = _require_json_object(payload, "payload")
    try:
        # ensure_ascii escapes lone surrogates rather than failing to encode them (M1-305
        # round 2), which is also what makes the artifact writable; sort_keys and the
        # compact separators make the rendering canonical; allow_nan=False refuses
        # NaN/Infinity, which json.dumps would otherwise emit as bare tokens that are not
        # JSON. The validator above already excludes every one of those, so this cannot
        # currently raise -- the guard stays because a hash rule that starts leaking a
        # value the day a type is admitted is not a rule anyone can rely on.
        rendered = json.dumps(
            validated,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        # The replay guard, and it is not belt-and-braces. `ensure_ascii=True` escapes an
        # astral scalar and its UTF-16 surrogate-pair spelling to the *same* two \uXXXX
        # units, and `json.loads` recombines them -- so two distinct Python strings used as
        # two object *keys* render as one key, and one of the two entries is silently gone
        # on the way back. That is a payload whose digest describes something the artifact
        # does not contain, which is exactly the claim this hash exists to make. Rendering
        # the parse and comparing is a total test for it: the accepted domain is defined as
        # "survives its own canonical rendering", not as a list of characters to look for.
        # (M1-305's surrogate-pair lesson, applied to keys rather than to values.)
        #
        # Defining it that way is what makes it total, and the property suite proved the
        # point by finding a *second* mechanism the blocklist version would have missed:
        # `sort_keys` orders by the Python string, so a key that reparses to a different
        # scalar can sort into a different position without colliding with anything --
        # {U+D83D U+DE00: null, U+D83E: null} renders in one order and reparses into the
        # other. Both are the same defect, and neither is a character you could look for.
        reparsed = json.dumps(
            json.loads(rendered),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        # from None and a constant message: json.dumps names the offending value in the
        # circular-reference and out-of-range-float cases alike.
        raise GatewayError(
            "the submission payload could not be rendered as canonical JSON "
            "(detail withheld: it can echo the payload)"
        ) from None
    if reparsed != rendered:
        # Names neither rendering: both are the payload.
        raise GatewayError(
            "the submission payload does not survive its own canonical rendering, so a "
            "replay could not reproduce it; two object keys most likely differ only in "
            "how they spell one character (detail withheld: it can echo the payload)"
        )
    return rendered


def payload_sha256(payload: Mapping[str, object]) -> str:
    """Return the lowercase hex SHA-256 of a payload's canonical rendering.

    This is the value that goes into ``submission_attempts.request_payload_sha256`` and
    into :func:`submission.submission_key`'s material, so a changed payload is a different
    key by construction.

    Pure: no I/O, no clock, no ledger.
    """
    return hashlib.sha256(canonical_payload_json(payload).encode("utf-8")).hexdigest()


def dry_run_attempt_id(idempotency_key: str) -> str:
    """Return the deterministic attempt id a dry run of this key produces.

    Derived rather than minted. A ``uuid4`` would make two dry runs of identical work
    produce different receipts, which is the one thing a *deterministic* receipt is for --
    an operator has to be able to re-run a dry run and see that nothing changed. Because
    the key is already unique per (tournament, question, version, payload), so is this.

    It is a hash of the key rather than the key itself so that an attempt id can never be
    pasted into a query against ``submission_attempts.idempotency_key`` and match.
    """
    key = _require_identifier(idempotency_key, "idempotency_key")
    return _DRY_RUN_ATTEMPT_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()


def dry_run_artifact_path(*, question_id: int, idempotency_key: str) -> str:
    """The path a dry-run artifact is stored at, relative to ``storage.artifact_root``.

    Exposed so a reader can resolve a stored path without restating the layout, and so the
    layout has exactly one definition -- ``research/artifacts.artifact_relative_path``'s
    reason, and the same shape.
    """
    question = _require_question_id(question_id, "question_id")
    key = _require_safe_key(idempotency_key)
    return f"{_SUBMISSIONS_SUBDIR}/{_DRY_RUN_SUBDIR}/{question}/{key}.json"


def attempt_from_receipt(receipt: SubmissionReceipt) -> SubmissionAttempt:
    """Convert a **live** receipt into the row :func:`lifecycle.record_submission_attempt` takes.

    The only door from a receipt into the ledger, and it refuses a dry-run receipt. That
    refusal is load-bearing, not decorative: a dry-run receipt carries ``success=False``
    and ``verified_by_refetch=False``, which the writer reads as ``submission_failed`` and
    which moves the record to terminal ``failed``. A rehearsal would permanently kill the
    forecast version it was rehearsing.

    ``mode`` is checked rather than trusted-by-convention for M1-402's reason: a bound any
    caller can lift is not a bound. There is deliberately no ``force`` parameter.

    ``artifact_path`` and ``mode`` are dropped: both describe how the receipt was produced
    and recorded, not what was posted, and ``submission_attempts`` has no column for
    either.

    The remaining fields are **transcribed, not re-validated**, and that is deliberate:
    ``record_submission_attempt`` validates every one of them against the schema it is
    about to write, and a second set of rules here could only either agree (and be dead
    code that still has to be kept in step) or disagree (and refuse a row the ledger would
    accept, or worse, accept one it would not). One writer, one rule -- the reason M1-608
    exists rather than three copies of an identifier bound.
    """
    # Exact type, not isinstance, and for `record_submission_attempt`'s stronger reason
    # (round 1, finding 3): a subclass can shadow a field with a property, so each read
    # below would become a call into caller-supplied code that can raise anything.
    if type(receipt) is not SubmissionReceipt:
        raise GatewayError("receipt must be a SubmissionReceipt")
    mode = _require_mode(receipt.mode)
    if mode != "live":
        # `mode` is a member of this module's closed vocabulary, so naming it is safe and
        # it is what makes the refusal actionable.
        raise GatewayError(
            f"a {mode} receipt records no live post and cannot be written to the ledger as "
            "a submission attempt"
        )
    return SubmissionAttempt(
        attempt_id=receipt.attempt_id,
        idempotency_key=receipt.idempotency_key,
        requested_at_utc=receipt.requested_at_utc,
        completed_at_utc=receipt.completed_at_utc,
        request_payload_sha256=receipt.request_payload_sha256,
        success=receipt.success,
        verified_by_refetch=receipt.verified_by_refetch,
        http_status=receipt.http_status,
        response_body=receipt.response_body,
        response_headers=receipt.response_headers,
        error_type=receipt.error_type,
        error_message=receipt.error_message,
        refetched_forecast_snapshot=receipt.refetched_forecast_snapshot,
    )


class DryRunSubmissionGateway:
    """A gateway that produces a receipt and posts nothing.

    Satisfies :class:`SubmissionGateway`. It has no ledger connection, no HTTP client and
    no configuration -- which is not minimalism, it is what makes the two claims in the
    backlog criterion checkable. *"Makes zero HTTP post calls"* is provable by inspection
    because this module imports nothing that can make one; *"records payload/hash"* is the
    artifact, and it is written from the payload this call hashed rather than from
    anything a caller asserted about it.

    ``artifact_root`` is ``storage.artifact_root``, and passing ``None`` makes
    :meth:`submit` pure -- no filesystem access at all. Both are legitimate: a caller
    exercising the seam wants the pure form, and an operator rehearsing a submission wants
    the file. Configuration is not read here, so retention is the caller's decision and is
    explicit, which is ``research/artifacts.py``'s rule for the same reason.

    ``clock`` is injected so a replayed dry run is reproducible. It is the only impure
    input to the receipt; everything else is a function of the request.
    """

    def __init__(
        self,
        *,
        artifact_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if artifact_root is not None and not isinstance(artifact_root, Path):
            raise GatewayError("artifact_root must be a Path or None")
        if clock is not None and not callable(clock):
            raise GatewayError("clock must be callable")
        self._artifact_root = artifact_root
        self._clock: Callable[[], datetime] = _utcnow if clock is None else clock

    def submit(self, request: SubmissionRequest) -> SubmissionReceipt:
        """Return the deterministic receipt for a submission that was not made.

        ``success`` is ``False`` and ``verified_by_refetch`` is ``False``, and neither is a
        placeholder. ``success=True`` would be a claim that a post went through; the
        record's whole purpose is that such a claim is only ever made by something that
        actually posted. Every HTTP, refetch and error field is ``None`` for the mirror
        reason: a dry run is not a failure either, so inventing an ``error_type`` for it
        would put a fabricated cause into an audit record.

        The two clock readings bracket the *request* -- which is to say, nothing -- rather
        than the artifact write. A receipt's timestamps describe the post it reports on,
        and the bookkeeping that follows is not part of it. They are validated the way any
        untrusted datetime is: ``clock`` is caller-supplied code and can return anything.
        """
        # Exact type for `record_submission_attempt`'s reason: a subclass can shadow a
        # field with a property, turning each read below into caller code that can raise
        # anything, at any point between validation and the receipt.
        if type(request) is not SubmissionRequest:
            raise GatewayError("request must be a SubmissionRequest")
        # Read each field exactly once (M1-203's `_CountingQuestionType` lesson): a Mapping
        # subclass or a property could otherwise return one value to the validator and
        # another to the hash.
        record_id = _require_identifier(request.forecast_record_id, "forecast_record_id")
        question_id = _require_question_id(request.question_id, "question_id")
        # The path-safe rule, not the plain identifier rule, and unconditionally -- even
        # when no artifact will be written. A gateway that accepted a key it could not
        # record would have an accepted domain that depended on a constructor argument,
        # so the pure form would mint receipts the recording form refuses. The bound has
        # to be the same one either way.
        key = _require_safe_key(request.idempotency_key)
        payload = request.payload

        digest = payload_sha256(payload)
        requested = _require_aware_utc(self._clock(), "clock()")
        completed = _require_aware_utc(self._clock(), "clock()")
        if completed < requested:
            # The pair is what an idempotency key is reasoned about against, and
            # `lifecycle.record_submission_attempt` refuses a reversed one outright. A
            # clock that ran backwards is refused here so the receipt never carries it.
            raise GatewayError("the clock returned a completion time earlier than the request time")

        receipt = SubmissionReceipt(
            mode="dry_run",
            attempt_id=dry_run_attempt_id(key),
            forecast_record_id=record_id,
            idempotency_key=key,
            requested_at_utc=requested,
            completed_at_utc=completed,
            request_payload_sha256=digest,
            success=False,
            verified_by_refetch=False,
        )
        if self._artifact_root is None:
            return receipt
        path = write_dry_run_artifact(
            self._artifact_root, receipt=receipt, question_id=question_id, payload=payload
        )
        return replace(receipt, artifact_path=path)


def write_dry_run_artifact(
    artifact_root: Path,
    *,
    receipt: SubmissionReceipt,
    question_id: int,
    payload: Mapping[str, object],
) -> str:
    """Write one dry run's payload and receipt; return the path to record.

    Returns the **relative** path, for ``research/artifacts.py``'s reason: a recorded path
    must survive the artifact directory moving, or being opened on another machine.

    The envelope holds the payload *verbatim* alongside the digest the receipt carries, so
    an operator can see what would have been posted and re-derive the hash from the file
    rather than taking the receipt's word for it. That re-derivation is the point of
    writing the payload at all.

    **Re-running a dry run is a no-op, not an error.** ``research/artifacts.py`` refuses an
    existing destination outright, and is right to: a retrieval artifact records a paid
    call, so a second one at the same path is a collision. Here the path is derived from
    the idempotency key, which is derived from the payload -- so an existing file means
    the identical dry run was performed before, and refusing it would make the one mode
    whose entire purpose is repeatability un-repeatable. The bytes are compared, and only
    a *disagreement* raises: identical content returns the path having written nothing.

    ``question_id`` is a parameter rather than a receipt field, and that is the deliberate
    half. The handoff's receipt carries no question, and adding one would put a value in an
    audit record that has to agree with the idempotency key while nothing can check that it
    does -- the key is a digest, so the question inside it is not recoverable from it. The
    path needs a question, so the *caller* supplies it, and the receipt keeps only what a
    submission actually produced. What **can** be checked is checked: the digest is
    re-derived from the payload handed in and must equal the one the receipt claims.

    Raises :class:`GatewayError` rather than swallowing a write failure. Unlike M1-312's
    paid path there is nothing to degrade *to* -- no call was made and no money was spent,
    so a dry run that could not record itself has simply not happened, and reporting
    success for it would be the lie the whole module is arranged against.
    """
    if type(receipt) is not SubmissionReceipt:
        raise GatewayError("receipt must be a SubmissionReceipt")
    if not isinstance(artifact_root, Path):
        raise GatewayError("artifact_root must be a Path")
    mode = _require_mode(receipt.mode)
    if mode != "dry_run":
        raise GatewayError(f"a {mode} receipt is not written to the dry-run artifact tree")

    key = _require_safe_key(receipt.idempotency_key)
    question = _require_question_id(question_id, "question_id")
    # Rendered once and digested from that rendering: hashing a second render of the same
    # mapping would compare two results of code that ran twice, not the file and its hash.
    canonical = canonical_payload_json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != _require_sha256(receipt.request_payload_sha256, "receipt.request_payload_sha256"):
        # The receipt would otherwise attribute this payload's file to a different
        # payload's digest -- a stored claim that is simply false. Names neither value.
        raise GatewayError(
            "the receipt's request_payload_sha256 does not match the payload supplied "
            "with it (detail withheld: it can echo the payload)"
        )

    envelope = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "mode": mode,
        "question_id": question,
        # The payload goes in as the already-canonical text, parsed back, so the file and
        # the digest cannot disagree about which rendering was hashed.
        "request_payload": json.loads(canonical),
        "receipt": _receipt_envelope(receipt),
    }
    try:
        body = json.dumps(envelope, ensure_ascii=True, sort_keys=True, allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError, RecursionError):
        raise GatewayError(
            "the dry-run artifact could not be rendered as JSON "
            "(detail withheld: it can echo the payload)"
        ) from None

    relative = dry_run_artifact_path(question_id=question, idempotency_key=key)
    _write_or_confirm(artifact_root / relative, body)
    return relative


def read_dry_run_artifact(artifact_root: Path, relative_path: str) -> dict[str, object]:
    """Read one dry-run artifact back, or raise :class:`GatewayError`.

    Admits exactly what the writer can emit, which is why it refuses the non-finite JSON
    constants ``json.loads`` accepts by default: a reader that admits more than its writer
    produces is not reading the format it documents (``research/artifacts.py`` round 1,
    finding 7).
    """
    if not isinstance(artifact_root, Path):
        raise GatewayError("artifact_root must be a Path")
    if type(relative_path) is not str or not relative_path.strip():
        raise GatewayError("relative_path must be a non-blank string")
    path = artifact_root / relative_path
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise GatewayError(f"cannot read dry-run artifact {path}") from None
    try:
        envelope = json.loads(text, parse_constant=_reject_json_constant)
    except ValueError:
        raise GatewayError(f"dry-run artifact is not valid JSON: {path}") from None
    if not isinstance(envelope, dict):
        raise GatewayError(f"dry-run artifact is not a JSON object: {path}")
    version = envelope.get("artifact_schema_version")
    if type(version) is not str or not version.strip():
        raise GatewayError(f"dry-run artifact schema version is missing or malformed: {path}")
    if version != ARTIFACT_SCHEMA_VERSION:
        # The version is this module's own literal on one side; the other is named only as
        # "unsupported", because a stored value is content until it is proven to be a
        # member of a vocabulary this module defines.
        raise GatewayError(
            f"dry-run artifact was written under an unsupported schema version "
            f"(this build reads {ARTIFACT_SCHEMA_VERSION}): {path}"
        )
    # The structural half of "admit exactly what the writer emits". Shapes only: the
    # values inside are content this module does not interpret, and re-deriving the digest
    # is the caller's check to make against the receipt it holds.
    if _require_mode(envelope.get("mode")) != "dry_run":
        raise GatewayError(f"dry-run artifact does not record a dry run: {path}")
    if type(envelope.get("question_id")) is not int:
        raise GatewayError(f"dry-run artifact question_id is missing or malformed: {path}")
    if not isinstance(envelope.get("request_payload"), dict):
        raise GatewayError(f"dry-run artifact request_payload is missing or malformed: {path}")
    if not isinstance(envelope.get("receipt"), dict):
        raise GatewayError(f"dry-run artifact receipt is missing or malformed: {path}")
    return envelope


def _receipt_envelope(receipt: SubmissionReceipt) -> dict[str, object]:
    """Render a receipt as a JSON-native dict for the artifact.

    ``artifact_path`` is omitted: the file's own location *is* that value, and writing it
    into the file would be a self-reference that a moved artifact silently invalidates.

    Timestamps use ``lifecycle``'s canonical form -- fixed width, always UTC, microseconds
    always present -- rather than a bare ``isoformat()``, which omits the fractional part
    when it is zero and makes the rendered width vary. The ledger pins that form on the
    columns it orders; an artifact rendering the same instants differently would be a
    second spelling of a value the two records are meant to be comparable on.
    """
    return {
        "mode": receipt.mode,
        "attempt_id": receipt.attempt_id,
        "forecast_record_id": receipt.forecast_record_id,
        "idempotency_key": receipt.idempotency_key,
        "requested_at_utc": _utc_text(receipt.requested_at_utc),
        "completed_at_utc": _utc_text(receipt.completed_at_utc),
        "request_payload_sha256": receipt.request_payload_sha256,
        "success": receipt.success,
        "verified_by_refetch": receipt.verified_by_refetch,
        "http_status": receipt.http_status,
        "response_body": receipt.response_body,
        "response_headers": receipt.response_headers,
        "error_type": receipt.error_type,
        "error_message": receipt.error_message,
        "refetched_forecast_snapshot": receipt.refetched_forecast_snapshot,
    }


def _reject_json_constant(token: str) -> object:
    """Refuse ``NaN``/``Infinity``/``-Infinity`` while parsing an artifact."""
    raise GatewayError(
        "dry-run artifact contains a non-finite JSON constant, which this format does not permit"
    )


def _write_or_confirm(destination: Path, body: bytes) -> None:
    """Create ``destination`` with ``body``, atomically; accept an identical existing file.

    A temp file in the destination's own directory is written and fsynced, then
    ``os.link`` moves it into place -- ``link`` fails with ``EEXIST`` rather than
    replacing, so "never overwrite" is atomic against a concurrent writer instead of a
    check that can be raced. ``os.replace`` would have been the usual atomic rename and is
    exactly wrong here: it clobbers. This is ``research/artifacts._write_new_file``'s
    mechanism; it is re-spelled rather than imported because that function is private to
    its module and raises ``ArtifactError``, and a shared home for it is a refactor of
    merged code that belongs to its own item, not this one.

    The one behavioural difference is what happens on ``EEXIST``. There it is always an
    error; here the existing bytes are compared, because the destination name is derived
    from the payload and an identical file means the identical dry run was performed
    before. Only a disagreement raises -- and a disagreement at a content-derived path
    means something outside this module wrote there.

    The failure mode is a stray temp file, never a half-written artifact.
    """
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise GatewayError(
            f"cannot create dry-run artifact directory {destination.parent}"
        ) from None
    handle, temp_name = -1, ""
    try:
        handle, temp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
        with os.fdopen(handle, "wb") as stream:
            handle = -1  # fdopen took ownership; the finally below must not close it twice.
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_name, destination)
        except FileExistsError:
            _confirm_identical(destination, body)
    except OSError:
        raise GatewayError(f"cannot write dry-run artifact {destination}") from None
    finally:
        if handle != -1:
            os.close(handle)
        if temp_name:
            # The link either succeeded (the content now has two names) or did not (the
            # temp file is garbage). Either way the temp name goes.
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _confirm_identical(destination: Path, body: bytes) -> None:
    """Accept an existing artifact whose bytes match; raise if they do not."""
    try:
        existing = destination.read_bytes()
    except OSError:
        raise GatewayError(
            f"a dry-run artifact already exists at {destination} and could not be read back "
            "to confirm it records the same dry run"
        ) from None
    if existing != body:
        # Names neither body: both are payload-derived content.
        raise GatewayError(
            f"a different dry-run artifact already exists at {destination} and is never "
            "overwritten (detail withheld: it can echo the payload)"
        )


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _utc_text(value: datetime) -> str:
    """Render an already-validated aware datetime in ``lifecycle``'s canonical stored form."""
    return _require_aware_utc(value, "timestamp").isoformat(timespec="microseconds")


def _require_aware_utc(value: object, field: str) -> datetime:
    """Return an aware datetime converted to UTC, or raise. Mirrors ``lifecycle``'s.

    Exact type rather than ``isinstance``: a ``datetime`` subclass can override
    ``isoformat()`` and write arbitrary text into the artifact.

    The conversion is guarded, and broadly, for the reason ``lifecycle._require_aware_utc``
    gives: ``tzinfo`` is an abstract base class, so ``utcoffset()`` and ``astimezone()``
    run caller-supplied code on a value that has passed every type gate above. ``except
    Exception`` is the right width precisely because what arbitrary code can raise is not
    enumerable.
    """
    if type(value) is not datetime:
        raise GatewayError(f"{field} must be a datetime")
    try:
        aware = value.tzinfo is not None and value.utcoffset() is not None
    except Exception:
        raise GatewayError(
            f"{field} has a timezone that could not be read "
            "(detail withheld: it can echo the value)"
        ) from None
    if not aware:
        raise GatewayError(f"{field} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except Exception:
        raise GatewayError(
            f"{field} could not be converted to UTC (detail withheld: it can echo the value)"
        ) from None


def _require_mode(value: object) -> str:
    """Gate a value against :data:`GatewayMode`, this module's closed vocabulary."""
    if type(value) is not str or value not in _GATEWAY_MODES:
        raise GatewayError(
            "mode is not one of the recognized gateway modes "
            "(detail withheld: it can echo the value)"
        )
    return value


def _require_identifier(value: object, field: str) -> str:
    """Return storable, non-blank identifier text, or raise naming only the *field*.

    The rule is ``lifecycle._require_identifier``'s, re-spelled for
    ``submission._require_text``'s stated reason and pinned to it by M1-608: exact ``str``
    type, non-empty, within the 200-character ledger bound, non-blank under
    ``str.strip()``, no U+0000, and UTF-8 encodable.

    The encode probe is the load-bearing one. ``sqlite3`` encodes text parameters as UTF-8,
    so a lone surrogate reaching a query raises a raw ``UnicodeEncodeError`` **quoting the
    offending character** -- both a leak and an error type callers do not handle. Refusing
    it here means a receipt can never carry a value the ledger will choke on later.
    """
    if type(value) is not str or not value:
        raise GatewayError(f"{field} must be a non-empty string")
    if len(value) > _MAX_IDENTIFIER:
        raise GatewayError(f"{field} is longer than the {_MAX_IDENTIFIER}-character limit")
    if not value.strip():
        raise GatewayError(f"{field} must not be blank")
    if "\x00" in value:
        raise GatewayError(f"{field} must not contain a NUL character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # from None: UnicodeEncodeError's own message quotes the character it choked on.
        raise GatewayError(
            f"{field} contains characters that cannot be stored "
            "(detail withheld: it can echo the value)"
        ) from None
    return value


def _require_safe_key(value: object) -> str:
    """Return an idempotency key that is safe to use as a path component.

    Stricter than :func:`_require_identifier`, because this one becomes a directory entry:
    a key carrying a separator would write outside the tree the recorded relative paths
    are resolved in. Every key :func:`submission.submission_key` mints passes -- the
    constraint refuses a *caller mistake*, not an attacker (CLAUDE.md's threat boundary).
    """
    key = _require_identifier(value, "idempotency_key")
    if not _SAFE_KEY_RE.match(key):
        raise GatewayError(
            "idempotency_key must be 1-128 characters of [A-Za-z0-9._-] starting "
            "alphanumeric: it becomes a path component (offending input withheld)"
        )
    return key


def _require_question_id(value: object, field: str) -> int:
    """Return a positive, storable integer.

    ``type(value) is int`` rather than ``isinstance``: ``bool`` subclasses ``int``, so
    ``True`` would otherwise become question 1 -- and, here, the directory ``1``.

    The upper bound is the persisted-form rule. SQLite stores signed 64-bit integers, so a
    Python int beyond that cannot reach a ``forecast_records`` row and a receipt naming one
    could never be matched back to it.
    """
    if type(value) is not int:
        raise GatewayError(f"{field} must be an integer")
    if value < 1:
        raise GatewayError(f"{field} must be a positive integer")
    if value > _INT64_MAX:
        raise GatewayError(f"{field} is larger than a 64-bit integer and cannot be stored")
    return value


def _require_sha256(value: object, field: str) -> str:
    """Return a 64-character lowercase hex digest, or raise naming only the field.

    Lowercase is part of the rule rather than a courtesy: ``"AB"`` and ``"ab"`` are the
    same digest and must not be two idempotency keys.
    """
    text = _require_identifier(value, field)
    if len(text) != 64 or not _HEX_DIGITS.issuperset(text):
        raise GatewayError(f"{field} must be 64 lowercase hexadecimal characters")
    return text


def _require_json_object(value: object, field: str) -> dict[str, object]:
    """Return a payload validated as a JSON object, or raise naming only the *field*.

    The accepted domain is exactly what survives a write-then-read round trip through the
    canonical rendering, because the digest of that rendering is what a replayed run has to
    reproduce:

    - **Objects** must be mappings whose keys are exactly ``str``. This is the rule with
      teeth. ``json.dumps`` silently *coerces* ``int``/``float``/``bool``/``None`` keys to
      strings, so ``{1: "a", "1": "b"}`` renders as one key and one of the two values is
      gone -- a payload the operator wrote and the receipt does not describe.
    - **Arrays** must be ``list``. A ``tuple`` renders identically and reads back as a
      ``list``, so a payload holding one is not equal to the payload a replay reconstructs.
      Refusing it keeps "the artifact is what was hashed" true of the objects too, not only
      of the bytes.
    - **Scalars** are ``str``, exact ``int``, finite ``float``, ``bool`` and ``None``.
      ``bool`` is checked before ``int`` because it subclasses it. Non-finite floats are
      refused here so the failure names the field rather than arriving from ``json.dumps``
      as a message quoting the value.

    ``str`` values are **not** required to be UTF-8 encodable: ``ensure_ascii=True``
    escapes a lone surrogate into the canonical text rather than failing on it, so the
    rendering, the digest and the artifact all survive one (M1-305 round 2). That is the
    difference between payload content and an identifier, which does reach ``sqlite3``.

    Depth is capped at :data:`_MAX_PAYLOAD_DEPTH`, which is what makes this safe to write
    recursively -- see the constant.
    """
    if not isinstance(value, Mapping):
        raise GatewayError(f"{field} must be a JSON object (a mapping)")
    return _validate_object(value, field, 0)


def _validate_object(value: Mapping[object, object], field: str, depth: int) -> dict[str, object]:
    if depth > _MAX_PAYLOAD_DEPTH:
        raise GatewayError(
            f"{field} nests deeper than the {_MAX_PAYLOAD_DEPTH}-level limit "
            "(a self-referential payload reaches this first)"
        )
    result: dict[str, object] = {}
    try:
        items = list(value.items())
    except Exception:
        # A Mapping is caller-supplied code: items() can raise anything.
        raise GatewayError(
            f"{field} could not be read as a mapping (detail withheld: it can echo the payload)"
        ) from None
    for key, item in items:
        if type(key) is not str:
            # Names no key: an object key is payload content.
            raise GatewayError(
                f"{field} contains an object key that is not a string; JSON silently "
                "coerces such keys and can collapse two entries into one "
                "(offending key withheld)"
            )
        result[key] = _validate_value(item, field, depth + 1)
    return result


def _validate_value(value: object, field: str, depth: int) -> object:
    if depth > _MAX_PAYLOAD_DEPTH:
        raise GatewayError(
            f"{field} nests deeper than the {_MAX_PAYLOAD_DEPTH}-level limit "
            "(a self-referential payload reaches this first)"
        )
    if value is None or type(value) is bool or type(value) is str or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise GatewayError(
                f"{field} contains a non-finite number, which JSON cannot represent "
                "(offending value withheld)"
            )
        return value
    if type(value) is list:
        return [_validate_value(item, field, depth + 1) for item in value]
    if isinstance(value, Mapping):
        return _validate_object(value, field, depth + 1)
    # Deliberately names the *type*, not the value: a type name is not payload content,
    # and it is the only thing that makes this actionable.
    raise GatewayError(
        f"{field} contains a {type(value).__name__}, which is not a JSON value; "
        "objects must be mappings and arrays must be lists"
    )
