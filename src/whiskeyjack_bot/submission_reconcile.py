"""Reconcile a live post the ledger never recorded (M2-713).

:func:`submission_live.post_approved_forecast` posts, refetches, writes the artifact and only
then writes the ``submission_attempts`` row. Anything that stops it in between leaves a
forecast live on Metaculus with no attempt row, a key reservation standing and the record
still ``approved``:

- **the ledger refused the write** -- a busy lock (another process holding ``BEGIN
  IMMEDIATE`` past ``ledger._BUSY_TIMEOUT_MS``), an I/O error. The artifact was written
  first, so the program's receipt is on disk, and ``submit`` says ``a live post was made and
  the ledger refused to record it``;
- **the process died during the refetch** -- Ctrl-C, systemd's start timeout, the OOM
  killer. No artifact, no row, and no message at all. On the live worker the next poll's
  recovery loop even confirms the forecast in the tournament journal and pushes "forecast
  confirmed", while the lifecycle ledger never hears of it.

``release-key`` is the wrong way out of both -- the post landed, so releasing invites a
duplicate -- and until this module nothing else existed.

**What a reconciliation rests on**, and why each piece is required rather than sufficient:

- **Program-written evidence from before the post.** The approval's ``payload_sha256`` (the
  payload a decision authorized, M2-707), the key :func:`submission.
  submission_key_for_approved_record` derives from it, the unreleased reservation holding that
  key (M2-708), and the ``forecast_intent`` journal row ``submission_policy``'s ``before_post``
  commits immediately ahead of every POST, whose payload must re-hash to that digest. Nothing
  here accepts a digest, a key or a payload from the operator: a caller-supplied digest can
  never establish what a record derives (M2-707's lesson).
- **The program's own observation, now.** One GET, through the same
  :meth:`~submission_live.MetaculusSubmissionGateway.observe` and
  :func:`~submission_live.classify_refetch` every other refetch uses, from the account the
  intent names. Only ``confirmed`` proceeds.
- **A person's assertion.** ``observed_by`` and ``note`` are required and have no default, for
  ``approve``'s reason: the program cannot claim a human looked at the platform.

A captured artifact is **pinned, not copied**: its path and the sha256 of its bytes go on the
row, after the artifact is checked to describe this key, attempt, record, question and
payload. Its receipt fields are never read into the ledger -- see
:class:`lifecycle.SubmissionReconciliation` for why an attempt row cannot honestly be written.

Every refusal writes nothing. Everything that can be refused locally is refused before the
poster is touched, and the write happens in one ``BEGIN IMMEDIATE`` that re-derives the whole
evidence set first, so a state that changed while the platform was being read is refused
rather than recorded.

Error hygiene is the project's: :class:`ReconciliationError` names no stored or
caller-supplied value, sanitizing raises use ``from None``, and every refusal from the modules
this one composes arrives as a :class:`ReconciliationError`. Filesystem paths are rendered
(the M1-401 carve-out).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from whiskeyjack_bot.approval import ApprovalError, effective_approval
from whiskeyjack_bot.bounds import MAX_ACTOR_LENGTH, MAX_IDENTIFIER_LENGTH, MAX_NOTE_LENGTH
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.forecast.record import ForecastRecordError
from whiskeyjack_bot.forecast.store import read_forecast_record
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    LifecycleEvent,
    SubmissionReconciliation,
    current_status,
    record_submission_reconciliation,
    transaction,
    unresolved_uncertainties,
)
from whiskeyjack_bot.submission import (
    SubmissionError,
    attempt_for_key,
    key_is_reconciled,
    live_reservation_for_key,
    submission_key_for_approved_record,
)
from whiskeyjack_bot.submission_gateway import (
    GatewayError,
    live_artifact_path,
    parse_submission_artifact,
    payload_sha256,
)
from whiskeyjack_bot.submission_live import (
    LiveSubmissionError,
    MetaculusPoster,
    MetaculusSubmissionGateway,
    build_verification_snapshot,
    classify_refetch,
    expected_option_labels,
    expected_points_for_record,
    expected_values,
    live_attempt_id,
    plan_from_payload,
)


class ReconciliationError(Exception):
    """A post cannot be reconciled, or the evidence for it does not hold.

    Same hygiene rule as :class:`submission.SubmissionError`: the message never echoes a
    caller-supplied field value, a stored value, or a database error's text, and sanitizing
    raises use ``from None``.
    """


@dataclass(frozen=True)
class UnrecordedPost:
    """The program-written evidence that a post was made and never recorded.

    Everything here was derived from ledger rows and files this program wrote before or during
    the post; nothing came from the operator. :func:`find_unrecorded_post` builds it, and
    :func:`reconcile_unrecorded_post` builds it a second time inside its write transaction and
    refuses unless the two are equal -- which is why it is a value object with equality, and
    why it is never passed in.

    ``payload_json`` is the intent's payload in canonical form rather than a mapping, so the
    object is hashable, immutable and compares by the bytes that were digested.
    """

    record_id: str
    question_id: int
    post_id: int
    question_type: str
    reservation_id: str
    reserved_at_utc: str
    idempotency_key: str
    attempt_id: str
    request_payload_sha256: str
    payload_json: str
    intent_event_id: str
    account_id: int
    artifact_path: str | None
    artifact_sha256: str | None


@dataclass(frozen=True)
class SubmissionIntent:
    """What a ``forecast_intent`` journal row says was about to be posted."""

    account_id: int
    payload: dict[str, object]
    payload_sha256: str


def find_unrecorded_post(
    conn: sqlite3.Connection, config: AppConfig, record_id: str
) -> UnrecordedPost:
    """Assemble the evidence that this record's post was made and never recorded, or refuse.

    Local only: ledger reads and one artifact read, no network. Each refusal says why and what
    to do, and in the order an operator can act on:

    1. the record must exist and be ``approved`` -- anything else has not been authorized, or
       has already been accounted for;
    2. no unresolved uncertainty -- the post gate refuses to post past one, and
       ``verify-submission`` is its way out;
    3. an approval in force bound to a payload (``011``), and the key it derives;
    4. no attempt row under that key, and no reconciliation of it -- the post is recorded;
    5. an unreleased reservation holding that key -- without one nothing was claimed, so
       nothing was posted;
    6. exactly one ``forecast_intent`` for the record, agreeing with the record and the
       approval -- without one this command never reached the POST;
    7. the artifact, when it exists, must describe this post; when it does not, the
       reconciliation records that no receipt was captured.
    """
    identifier = _require_identifier(record_id, "record_id")
    try:
        record = read_forecast_record(conn, identifier)
    except ForecastRecordError as exc:
        raise ReconciliationError(
            str(exc) or "this record could not be read back from the ledger"
        ) from None
    try:
        status = current_status(conn, identifier)
        outstanding = unresolved_uncertainties(conn, identifier)
    except LifecycleError as exc:
        raise ReconciliationError(str(exc) or "the ledger could not report this record") from None
    if status != "approved":
        # A member of lifecycle's closed vocabulary, so naming it is safe and actionable.
        raise ReconciliationError(
            f"this forecast record is {status}, not awaiting submission, so there is no "
            "unrecorded post to reconcile"
        )
    if outstanding:
        raise ReconciliationError(
            f"this record has {len(outstanding)} submission attempt(s) whose outcome a refetch "
            "has not resolved; resolve them with verify-submission first"
        )

    try:
        approval = effective_approval(conn, identifier)
    except ApprovalError as exc:
        raise ReconciliationError(str(exc) or "this record's approval could not be read") from None
    if approval is None or approval.payload_sha256 is None:
        raise ReconciliationError(
            "this record holds no approval bound to a payload, so no post under it could have "
            "been authorized and there is nothing to reconcile"
        )
    digest = approval.payload_sha256
    try:
        key = submission_key_for_approved_record(conn, identifier, request_payload_sha256=digest)
        if attempt_for_key(conn, key) is not None:
            raise ReconciliationError(
                "a submission attempt already records this record's post; there is nothing "
                "unrecorded to reconcile"
            )
        if key_is_reconciled(conn, key):
            raise ReconciliationError("this record's post has already been reconciled")
        reservation = live_reservation_for_key(conn, key)
    except SubmissionError as exc:
        raise ReconciliationError(str(exc) or "this record's key could not be read") from None
    if reservation is None:
        raise ReconciliationError(
            "no key reservation is standing for the payload this record's approval authorized, "
            "so nothing was claimed and nothing was posted under it; there is nothing to "
            "reconcile"
        )

    intent_event_id, intent_data = _read_intent_row(conn, identifier)
    intent = read_intent(
        intent_data,
        record_id=identifier,
        question_id=record.question_id,
        post_id=record.post_id,
        tournament_id=record.tournament_id,
    )
    if intent.payload_sha256 != digest:
        # Neither digest is printed: one is stored, and printing the other would let a caller
        # confirm a guess against it.
        raise ReconciliationError(
            "the durable submission intent names a different payload than this record's "
            "approval authorized, so it does not describe a post under this key"
        )
    try:
        plan = plan_from_payload(
            intent.payload, expected_cdf_points=expected_points_for_record(record, config)
        )
    except LiveSubmissionError as exc:
        raise ReconciliationError(
            str(exc) or "the intent's payload is not one this build can compare"
        ) from None
    if plan.question_type != record.question_type:
        raise ReconciliationError(
            "the durable submission intent's payload is for a different question type than "
            "this record"
        )

    try:
        attempt_id = live_attempt_id(key)
    except LiveSubmissionError as exc:
        raise ReconciliationError(
            str(exc) or "this post's attempt id could not be derived"
        ) from None
    artifact_path, artifact_sha256 = _pin_artifact(
        config,
        question_id=record.question_id,
        key=key,
        record_id=identifier,
        attempt_id=attempt_id,
        digest=digest,
    )
    return UnrecordedPost(
        record_id=identifier,
        question_id=record.question_id,
        post_id=record.post_id,
        question_type=record.question_type,
        reservation_id=reservation.reservation_id,
        reserved_at_utc=reservation.reserved_at_utc,
        idempotency_key=key,
        attempt_id=attempt_id,
        request_payload_sha256=digest,
        payload_json=_canonical(intent.payload),
        intent_event_id=intent_event_id,
        account_id=intent.account_id,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
    )


def reconcile_unrecorded_post(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    record_id: str,
    observed_by: str,
    note: str,
    poster: MetaculusPoster,
    occurred_at: datetime | None = None,
    sleep: Callable[[float], None] | None = None,
    refetch_attempts: int = 3,
    refetch_pause_seconds: float = 2.0,
) -> LifecycleEvent:
    """Record that this record's post reached the platform and was never written down.

    **Posts nothing.** One identity read and one refetch are the only network calls, which is
    why no submission flag is consulted -- ``verify-submission``'s reasoning.

    In order: the connection must own its transaction (the record must be durable when this
    returns, and the network read must not happen inside a write lock); the person's assertion
    is validated; :func:`find_unrecorded_post` assembles and checks the evidence; the poster
    must be the account the intent names; the refetch must be ``confirmed``. Then, inside one
    ``BEGIN IMMEDIATE``, the evidence is assembled again and must be unchanged, and
    :func:`lifecycle.record_submission_reconciliation` writes the row and its
    ``submission_confirmed`` event.

    The three refetch outcomes that are not a confirmation are refused and say where to go:
    ``absent`` means the platform does not show this account's forecast, so the person's
    assertion and the platform disagree and ``release-key`` is the path once they have checked;
    ``mismatched`` is runbook L3; ``unreadable`` is retryable.
    """
    if conn.in_transaction:
        raise ReconciliationError(
            "a reconciliation cannot be made inside a caller's open transaction: it must be "
            "durable when this returns, and the platform read must not hold a write lock. "
            "Commit or roll back first"
        )
    if type(config) is not AppConfig:
        raise ReconciliationError("config must be an AppConfig")
    actor = _require_assertion(observed_by, "observed_by", max_length=MAX_ACTOR_LENGTH)
    note_text = _require_assertion(note, "note", max_length=MAX_NOTE_LENGTH)
    evidence = find_unrecorded_post(conn, config, record_id)

    try:
        account = poster.get_current_user_id()
    except Exception:  # noqa: BLE001 - any transport failure is one refusal here
        # from None and a constant message: an SDK error's text embeds the response body.
        raise ReconciliationError(
            "the authenticated account could not be read, so the platform read would not be "
            "known to be this account's; nothing was recorded"
        ) from None
    if type(account) is not int or account != evidence.account_id:
        raise ReconciliationError(
            "the configured token is not the account that made this post, so its forecasts "
            "are not the ones to compare; nothing was recorded"
        )

    payload = json.loads(evidence.payload_json)
    try:
        plan = plan_from_payload(
            payload,
            expected_cdf_points=expected_points_for_record(
                read_forecast_record(conn, evidence.record_id), config
            ),
        )
        gateway = MetaculusSubmissionGateway(
            poster=poster,
            sleep=sleep,
            refetch_attempts=refetch_attempts,
            refetch_pause_seconds=refetch_pause_seconds,
        )
    except (LiveSubmissionError, ForecastRecordError) as exc:
        raise ReconciliationError(str(exc) or "the refetch could not be prepared") from None
    history = gateway.observe(evidence.post_id, question_id=evidence.question_id)
    expected = expected_values(plan)
    labels = expected_option_labels(plan)
    # Baseline `None`: the intent records `baseline: []`, and the gateway refuses to post to a
    # question the account has already forecast on, so every entry the account holds there is
    # newer than the baseline this post was measured against.
    result = classify_refetch(
        question_type=plan.question_type,
        expected=expected,
        baseline_latest_start_time=None,
        observed=history,
        expected_labels=labels,
    )
    if result.outcome == "unreadable":
        raise ReconciliationError(
            "the question could not be refetched, so nothing was established and nothing was "
            "recorded; run this again later"
        )
    if result.outcome == "absent":
        raise ReconciliationError(
            "the platform shows no forecast from this account on the question, which "
            "contradicts the assertion that the post landed; nothing was recorded. If you have "
            "checked Metaculus and it is not there, the post did not land and release-key is "
            "the way out"
        )
    if result.outcome == "mismatched":
        raise ReconciliationError(
            "the platform holds a forecast that is not the payload this record's approval "
            "authorized; that is not this post, and nothing was recorded -- resolve it by hand"
        )

    snapshot = build_verification_snapshot(
        question_type=plan.question_type,
        expected=expected,
        baseline_entry_count=0,
        baseline_latest_start_time=None,
        result=result,
        expected_labels=labels,
    )
    if _snapshot_omits_values(snapshot):
        # `build_verification_snapshot`'s own rule: its evidence-free fallback must never back a
        # confirmation, because it names nothing a reader could check the verdict against.
        raise ReconciliationError(
            "the confirming refetch could not be stored with the values it saw, so it would "
            "back a confirmation it cannot show; nothing was recorded"
        )
    stamped = _utcnow() if occurred_at is None else occurred_at
    reconciliation = SubmissionReconciliation(
        reservation_id=evidence.reservation_id,
        attempt_id=evidence.attempt_id,
        request_payload_sha256=evidence.request_payload_sha256,
        intent_event_id=evidence.intent_event_id,
        observed_by=actor,
        note=note_text,
        refetched_at_utc=stamped,
        refetched_forecast_snapshot=snapshot,
        artifact_path=evidence.artifact_path,
        artifact_sha256=evidence.artifact_sha256,
    )
    try:
        with transaction(conn):
            if find_unrecorded_post(conn, config, evidence.record_id) != evidence:
                raise ReconciliationError(
                    "the ledger or the artifact changed while the platform was being read; "
                    "nothing was recorded -- run this again"
                )
            return record_submission_reconciliation(
                conn,
                record_id=evidence.record_id,
                reconciliation=reconciliation,
                occurred_at=stamped,
            )
    except LifecycleError as exc:
        raise ReconciliationError(
            str(exc) or "the ledger refused to record this reconciliation"
        ) from None


def unrecorded_posts(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Record ids that look like an unrecorded post: read-only, no network, oldest first.

    A candidate holds a ``forecast_intent`` and a reservation that is unreleased and unspent --
    no attempt under its key, no reconciliation of it. That is the shape both routes into this
    state leave, including the one that prints nothing. It is a list of places to look, not a
    verdict: a process killed between the intent and the POST leaves the same shape with
    nothing posted, which is why reconciling one still needs a person and a confirming refetch,
    and why the other answer is ``release-key``.
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT r.forecast_record_id FROM submission_key_reservations r "
            "WHERE NOT EXISTS (SELECT 1 FROM submission_key_releases x "
            "                  WHERE x.reservation_id = r.reservation_id) "
            "  AND NOT EXISTS (SELECT 1 FROM submission_attempts a "
            "                  WHERE a.idempotency_key = r.idempotency_key) "
            "  AND NOT EXISTS (SELECT 1 FROM submission_reconciliations c "
            "                  WHERE c.reservation_id = r.reservation_id) "
            "  AND EXISTS (SELECT 1 FROM tournament_events t WHERE t.kind = 'forecast_intent' "
            "              AND t.scope = r.forecast_record_id) "
            "ORDER BY r.reserved_at_utc, r.forecast_record_id"
        ).fetchall()
    except (sqlite3.Error, UnicodeDecodeError):
        raise ReconciliationError(
            "the ledger could not be read (detail withheld: a database message can echo stored "
            "values)"
        ) from None
    identifiers: list[str] = []
    for row in rows:
        if type(row[0]) is not str:
            raise ReconciliationError(
                "a stored reservation names a record that is not text (detail withheld: it can "
                "echo stored values)"
            )
        identifiers.append(row[0])
    return tuple(identifiers)


def read_intent(
    data: object, *, record_id: str, question_id: int, post_id: int, tournament_id: str
) -> SubmissionIntent:
    """Parse a ``forecast_intent`` journal row's ``data`` and check it describes this record.

    Journal rows are read back out of the ledger, so they are untrusted: the row is parsed
    defensively, every field is checked by exact type, and a row that disagrees with the record
    about its identity is refused rather than trusted. ``baseline`` must be the empty list the
    policy writes, because the refetch below is measured against no baseline.

    Pure, and never raises anything but :class:`ReconciliationError`; nothing in a message is
    read from the row.
    """
    if type(data) is not str:
        raise ReconciliationError("the durable submission intent is not stored as text")
    try:
        parsed = json.loads(data, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ReconciliationError("the durable submission intent is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise ReconciliationError("the durable submission intent is not a JSON object")
    if (
        # Exact types for the integers only: JSON gives `7.0 == 7` and `true == 1`, whereas a
        # parsed JSON string can only equal a `str`.
        parsed.get("record_id") != record_id
        or type(parsed.get("question_id")) is not int
        or parsed.get("question_id") != question_id
        or type(parsed.get("post_id")) is not int
        or parsed.get("post_id") != post_id
        or parsed.get("project_id") != tournament_id
    ):
        raise ReconciliationError(
            "the durable submission intent does not describe this record's question"
        )
    account = parsed.get("account_id")
    if type(account) is not int or account < 1:
        raise ReconciliationError("the durable submission intent names no account")
    baseline = parsed.get("baseline")
    if type(baseline) is not list or baseline:
        raise ReconciliationError(
            "the durable submission intent records a baseline this reconciliation cannot "
            "measure against"
        )
    digest = parsed.get("payload_sha256")
    if type(digest) is not str or len(digest) != 64 or set(digest) - set("0123456789abcdef"):
        raise ReconciliationError("the durable submission intent names no payload digest")
    payload = parsed.get("payload")
    if not isinstance(payload, dict):
        raise ReconciliationError("the durable submission intent holds no payload")
    try:
        rendered = payload_sha256(payload)
    except GatewayError:
        raise ReconciliationError(
            "the durable submission intent's payload cannot be rendered canonically"
        ) from None
    if rendered != digest:
        raise ReconciliationError(
            "the durable submission intent's payload does not hash to the digest it records"
        )
    return SubmissionIntent(account_id=account, payload=payload, payload_sha256=digest)


def check_artifact_binds(
    envelope: Mapping[str, object],
    *,
    record_id: str,
    question_id: int,
    key: str,
    attempt_id: str,
    digest: str,
) -> None:
    """Refuse unless a parsed live artifact describes exactly this post.

    ``parse_submission_artifact`` checked the envelope's shape; this checks its identity. The
    artifact is pinned by hash, so it must be the one this post wrote -- same key, attempt,
    record, question, and a payload that re-hashes to the digest the approval bound. A file at
    the right path that disagrees is conflicting evidence, and it is refused rather than
    skipped: skipping it would record "no receipt was captured" beside one that was.

    Pure, and never raises anything but :class:`ReconciliationError`.
    """
    receipt = envelope.get("receipt")
    if not isinstance(receipt, Mapping):
        raise ReconciliationError("the submission artifact holds no receipt")
    if (
        receipt.get("mode") != "live"
        or receipt.get("idempotency_key") != key
        or receipt.get("attempt_id") != attempt_id
        or receipt.get("forecast_record_id") != record_id
        or receipt.get("request_payload_sha256") != digest
        or type(envelope.get("question_id")) is not int
        or envelope.get("question_id") != question_id
    ):
        raise ReconciliationError(
            "the submission artifact at this post's path does not describe this post"
        )
    payload = envelope.get("request_payload")
    if not isinstance(payload, dict):
        raise ReconciliationError("the submission artifact holds no request payload")
    try:
        rendered = payload_sha256(payload)
    except GatewayError:
        raise ReconciliationError(
            "the submission artifact's payload cannot be rendered canonically"
        ) from None
    if rendered != digest:
        raise ReconciliationError(
            "the submission artifact's payload is not the one this record's approval authorized"
        )


def _read_intent_row(conn: sqlite3.Connection, record_id: str) -> tuple[str, object]:
    """The record's one ``forecast_intent`` row, as ``(event_id, data)``, or refuse.

    ``submission_policy`` refuses a second intent for a question, so more than one for a record
    is a journal this program did not write and is refused rather than chosen between.
    """
    try:
        rows = conn.execute(
            "SELECT event_id, data FROM tournament_events "
            "WHERE kind = 'forecast_intent' AND scope = ? ORDER BY seq",
            (record_id,),
        ).fetchall()
    except (sqlite3.Error, UnicodeDecodeError):
        raise ReconciliationError(
            "the tournament journal could not be read (detail withheld: a database message can "
            "echo stored values)"
        ) from None
    if not rows:
        raise ReconciliationError(
            "this record holds no durable submission intent, so no command reached the post "
            "for it; if a reservation is standing, nothing was posted and release-key is the "
            "way out once you have checked"
        )
    if len(rows) > 1:
        raise ReconciliationError(
            "this record holds more than one durable submission intent, which the submission "
            "policy cannot produce; resolve it by hand"
        )
    event_id = rows[0][0]
    if type(event_id) is not str or not event_id.strip() or len(event_id) > MAX_IDENTIFIER_LENGTH:
        raise ReconciliationError(
            "the durable submission intent's identifier is malformed (detail withheld: it can "
            "echo stored values)"
        )
    return event_id, rows[0][1]


def _pin_artifact(
    config: AppConfig,
    *,
    question_id: int,
    key: str,
    record_id: str,
    attempt_id: str,
    digest: str,
) -> tuple[str | None, str | None]:
    """``(relative path, sha256 of its bytes)`` for this post's artifact, or ``(None, None)``.

    Absent is an answer: the process stopped before writing it, and the reconciliation records
    that no receipt was captured. Present-but-unreadable is not -- a file the command cannot
    read might be the receipt, so it is refused until it can be read, rather than recorded as
    missing. The bytes are read once; the digest and the validation are of the same bytes.
    """
    try:
        relative = live_artifact_path(question_id=question_id, idempotency_key=key)
    except GatewayError as exc:
        raise ReconciliationError(str(exc) or "this post's artifact path is malformed") from None
    path = config.storage.artifact_root / relative
    if not os.path.lexists(path):
        return None, None
    try:
        body = path.read_bytes()
    except OSError:
        raise ReconciliationError(
            f"cannot read the submission artifact {path}; it may hold this post's receipt, so "
            "nothing was recorded -- fix its permissions and run this again"
        ) from None
    try:
        text = body.decode("utf-8")
        envelope = parse_submission_artifact(text, path, expected_mode="live")
    except UnicodeDecodeError:
        raise ReconciliationError(f"the submission artifact is not UTF-8 text: {path}") from None
    except GatewayError as exc:
        raise ReconciliationError(str(exc) or "the submission artifact is malformed") from None
    check_artifact_binds(
        envelope,
        record_id=record_id,
        question_id=question_id,
        key=key,
        attempt_id=attempt_id,
        digest=digest,
    )
    return relative, hashlib.sha256(body).hexdigest()


def _snapshot_omits_values(snapshot: str) -> bool:
    try:
        parsed = json.loads(snapshot)
    except (ValueError, RecursionError):
        return True
    return not isinstance(parsed, dict) or parsed.get("values_omitted") is True


def _require_identifier(value: object, field: str) -> str:
    """Non-blank storable identifier text, or raise naming only the *field*."""
    if type(value) is not str or not value:
        raise ReconciliationError(f"{field} must be a non-empty string")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ReconciliationError(
            f"{field} is longer than the {MAX_IDENTIFIER_LENGTH}-character limit"
        )
    if not value.strip():
        raise ReconciliationError(f"{field} must not be blank")
    if "\x00" in value:
        raise ReconciliationError(f"{field} must not contain a NUL character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # from None: UnicodeEncodeError's own message quotes the character it choked on.
        raise ReconciliationError(
            f"{field} contains characters that cannot be stored "
            "(detail withheld: it can echo the value)"
        ) from None
    return value


def _require_assertion(value: object, field: str, *, max_length: int) -> str:
    """A person's name or note: non-blank, storable, bounded, no NUL.

    Checked here, before any ledger read or network call, so a missing assertion refuses
    without spending anything. ``lifecycle`` checks the same thing again at its own bounds,
    and ``016`` again at the schema.
    """
    if type(value) is not str or not value.strip():
        raise ReconciliationError(
            f"{field} is required: a reconciliation records what a person saw on the platform, "
            "and the program cannot supply that"
        )
    if "\x00" in value:
        raise ReconciliationError(f"{field} must not contain a NUL character")
    if len(value) > max_length:
        raise ReconciliationError(f"{field} is longer than the {max_length}-character limit")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ReconciliationError(
            f"{field} contains characters that cannot be stored "
            "(detail withheld: it can echo the value)"
        ) from None
    return value


def _canonical(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _reject_constant(token: str) -> object:
    raise ValueError("non-finite JSON constant")


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
