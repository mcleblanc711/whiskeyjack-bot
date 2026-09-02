"""Derive submission idempotency keys, and read what a key already claimed (M2-702).

``submission_attempts.idempotency_key`` has been ``TEXT NOT NULL UNIQUE`` since migration
``001``, and :func:`lifecycle.record_submission_attempt` has written it since M1-603 --
but nothing in the tree *minted* one, so every caller would have had to invent a key. An
invented key defeats the column it goes in: the constraint stops a duplicate key from
claiming a second live post, it cannot stop two differently-spelled keys for one forecast
from claiming two. This module is the derivation, and it is the reason D23 ("submission is
a separate approved command with idempotency") can be more than a slogan.

**The key material is exactly four values**, per the backlog wording ("derive keys from
tournament, question, forecast version and payload hash"), plus the schema version that
pins the rule::

    {"key_schema_version", "tournament_id", "question_id", "forecast_version",
     "request_payload_sha256"}

Two obvious candidates are **excluded, each for its own reason**:

- ``record_id`` -- a writer-minted UUID. Including it would make the key a fact about when
  a row was written rather than about what is being submitted, so replaying identical work
  would mint a *second* key and claim a second live post. That is precisely the failure
  the key exists to prevent.
- ``forecast_sha256`` -- functionally determined by the triple already here, because
  ``001`` declares ``UNIQUE (question_id, tournament_id, forecast_version)`` and a stored
  version is immutable (D25). It would add no discrimination and a second value that must
  agree; ``approval.py`` is where the hash binding lives.

**The key carries a visible scheme tag** (``wjsub-1-``) ahead of the digest. Every other
hash in this package is a bare 64-hex digest, and this one deliberately is not:
``idempotency_key`` is an append-only column whose values must still be interpretable
after the rule is versioned, and a bare digest tells a later reader nothing about which
rule produced it. The version is *also* inside the hashed payload, so the two cannot drift
apart -- :func:`_assert_prefix_matches_version` fails at import if they do.

**The digest keys on the persisted form, and changing the rule breaks replay.** This is
M1-305's lesson and ``research/packet.py``'s rule verbatim: the canonical rendering is
``json.dumps(..., ensure_ascii=True, sort_keys=True, separators=(",", ":"),
allow_nan=False)``, which is what SQLite stores, so before == after. ``ensure_ascii``
escapes lone surrogates rather than failing to encode them. As with
``hashing.content_sha256`` and ``packet.packet_sha256``, if the rule must ever change it
changes as a **new versioned function alongside this one**, never as an edit to this one:
keys already stored keep their old spelling, and a re-derivation that disagreed with a
stored key would claim a second post for work already done.

The readers are the other half. :func:`attempt_for_key` answers "has this key already been
used?" positively, and :func:`require_key_unused` is the cheap look before minting an
attempt -- so "same payload/key cannot create two attempts" is a typed refusal that names
the collision, rather than a ``sqlite3.IntegrityError`` surfacing from the UNIQUE
constraint after the caller has already decided to post.

**A read is not a claim, and M2-708 is the difference.** ``require_key_unused`` was, until
migration ``010``, the whole guard in front of a live post: two commands could both read
one key as unused, both post, and ``001``'s UNIQUE would refuse the second *row* -- after
its call had been made. The constraint protected the shape of the ledger and not the
platform. :func:`reserve_submission_key` performs the check and the claim as one act,
inside ``lifecycle.transaction``'s ``BEGIN IMMEDIATE``, writing a
``submission_key_reservations`` row that ``010``'s trigger will not duplicate. A key's
state is therefore **derived, never stored**:

    spent     -- a ``submission_attempts`` row exists for it. Terminal.
    reserved  -- a reservation exists with no release row and no attempt row.
    released  -- every reservation for it carries a release row; the key is free again.
    free      -- no reservation at all.

:func:`release_submission_key` is the exit, and it exists because the reservation creates a
state that did not exist before. A key is a pure function of its four inputs, so a claim
with no way out does not block a retry -- it blocks that forecast, permanently, on an
append-only table. ``not_posted`` is the program reporting that it *proved* no post was
made; ``operator_abandoned`` is a person asserting it after checking the platform. Nothing
is released on the happy path: the attempt row spends the reservation.

**Two derivation seams, not one.** :func:`submission_key_for_record` will mint a key for
any stored record including a ``draft``, because that is what a dry run needs -- a dry run
is how an operator sees what *would* be submitted before deciding whether to approve it
(M2-703). :func:`submission_key_for_approved_record` refuses unless the record both holds
an approval **and** is still at ``approved``, and every path leading to a real post goes
through it. They are two functions rather than one function with a flag, for the reason
M1-402 settled: a bound any caller can lift is not a bound.

**What this module does not do.** It does not build the request payload --
``submission_payload.py`` owns that (M2-707, on M1-502/M1-503), and
``request_payload_sha256`` is an *input* here, exactly as the backlog says ("derive keys
from ... and payload hash"). The import direction is why: that module reaches
``forecast.cdf`` and therefore the SDK, and this one is imported by every submission path
including the dry run.

**What it now does, and did not until M2-707.** It checks that a payload *is* the one an
approval meant. Until this item an approval bound to ``forecast_sha256`` alone, so one
approved forecast covered every payload built from it -- a payload that changed without the
forecast changing got a new key and kept the old approval. That was a known, recorded gap
(**decision D33**) and not an oversight: closing it needed the forecast->payload mapping,
which M1-502/M1-503 did not ship. Migration ``011`` adds ``approval_events.payload_sha256``,
:func:`approval.approve` requires it, and
:func:`submission_key_for_approved_record` refuses a ``request_payload_sha256`` that is not
it. See ``docs/M2-NOTES.md``.

Error hygiene follows ``ConfigError``/``LedgerError``/``LifecycleError``/``ApprovalError``:
a :class:`SubmissionError` never echoes a caller-supplied or stored value, sanitizing
raises use ``from None``, and every malformed shape arrives as a :class:`SubmissionError`
rather than a raw ``sqlite3.Error``/``UnicodeEncodeError``.

Purely local: nothing here contacts Metaculus. ``submission.enabled: false`` and
``dry_run: true`` remain the committed defaults -- the gateways are M2-703 and M2-704.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast, get_args

from whiskeyjack_bot.approval import ApprovalError, effective_approval
from whiskeyjack_bot.bounds import (
    MAX_ACTOR_LENGTH,
    MAX_IDENTIFIER_LENGTH,
    MAX_NOTE_LENGTH,
)
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    RefetchOutcome,
    current_status,
    transaction,
)

# Bumping this changes every key, which is the point: a rule change must be visible as a
# different key rather than silently reinterpreting stored ones. It is part of the hashed
# payload *and* of the prefix, so the rule and its declared version cannot drift apart.
KEY_SCHEMA_VERSION = "1.0.0"

# `lifecycle`'s vocabulary, resolved once. Derived with `get_args` rather than restated as
# a literal, so this reader cannot come to accept a member the writer does not emit.
_REFETCH_OUTCOMES: frozenset[str] = frozenset(get_args(RefetchOutcome))

# The visible scheme tag. Written as a literal rather than derived from the version so
# that _assert_prefix_matches_version has something to check; a computed prefix would
# agree with the version by construction and prove nothing.
_KEY_PREFIX = "wjsub-1-"

_HEX_DIGITS: frozenset[str] = frozenset("0123456789abcdef")


# SQLite stores integers as signed 64-bit. A Python int outside that range cannot be
# persisted, so a key derived from one could never be reproduced from the stored row --
# the M1-305 persisted-form rule applied to integers rather than to datetimes.
_INT64_MAX = 2**63 - 1

# 8-character prefix + 64 hex characters. Well inside the writer's 200-character bound.
KEY_LENGTH = len(_KEY_PREFIX) + 64


# Why a key reservation may have been given up. **The membership lives here and not in a
# column CHECK**, deliberately: M2-711 established what a closed column vocabulary costs on
# an append-only table (widening one is a full rebuild), and this is the kind that grows.
# `010` enforces the column's *shape* -- non-blank text, no NUL, bounded -- and this owns
# what the values may be.
#
# The two are not the same claim, which is why `release_submission_key` pairs each with a
# different rule about `released_by`:
#
# - `not_posted`         the program proved no post was made (`MetaculusSubmissionGateway.
#                        post_attempted` is still false when the refusal arrives). There is
#                        no person to name, so naming one is refused.
# - `operator_abandoned` a human checked the platform and asserts nothing landed. This is
#                        the crash-mid-post case, where the program knows nothing at all,
#                        so `released_by` is required -- `approve`'s rule, that an
#                        attribution claim about a person is never inferred.
ReservationReason = Literal["not_posted", "operator_abandoned"]
_RESERVATION_REASONS: frozenset[str] = frozenset(get_args(ReservationReason))

# Visible tags on the two reservation-side identifiers. Written as literals for
# `_KEY_PREFIX`'s reason. Neither can be confused with an idempotency key or an attempt id:
# those are `<tag>-<64 hex>` and these are `<tag><32 hex>` under different tags, which
# `submission_live._assert_identity_spaces_are_distinct` is the standing check on.
_RESERVATION_PREFIX = "wjres-"
_RELEASE_PREFIX = "wjrel-"

# One spelling each, shared by the reader and the writer. Two texts for one refusal is how
# the reader and the claim come to disagree about what "used" means (M1-608, M2-710).
_SPENT_KEY_REFUSAL = (
    "this idempotency key has already been used by a recorded submission attempt; "
    "a second attempt under it would claim a second live post"
)
_RESERVED_KEY_REFUSAL = (
    "this idempotency key is reserved by a submission that has not finished; the "
    "reservation must be released or spent before another may claim it"
)


class SubmissionError(Exception):
    """A key cannot be derived, or the ledger cannot be read.

    Same hygiene rule as :class:`approval.ApprovalError`: the message never echoes a
    caller-supplied field value, a stored value, or a database error's text, and
    sanitizing raises use ``from None`` so an underlying exception cannot reprint a value
    through its text or a rendered traceback.
    """


def _assert_prefix_matches_version() -> None:
    """Fail at import if the visible scheme tag and :data:`KEY_SCHEMA_VERSION` disagree.

    The prefix is the only part of the rule a reader of the stored column can see, and the
    version is the only part a reader of the *hash input* can see. If they can drift, a key
    can advertise a rule it was not built under -- which is worse than no tag at all,
    because the tag would then be actively misleading about an append-only value.

    ``packet._assert_fields_exist`` makes the same argument about its exclusion list, and
    for the same reason it runs at import rather than in a test: a guard that only a test
    enforces is a guard the next module to import this one does not have.
    """
    major = KEY_SCHEMA_VERSION.split(".", 1)[0]
    expected = f"wjsub-{major}-"
    if _KEY_PREFIX != expected:
        # These are this module's own literals, not caller or row content, so naming them
        # is safe -- and it is the only thing that makes the failure fixable.
        raise SubmissionError(
            f"idempotency-key prefix {_KEY_PREFIX!r} does not match KEY_SCHEMA_VERSION "
            f"{KEY_SCHEMA_VERSION!r} (expected {expected!r}); a rule change bumps both"
        )


_assert_prefix_matches_version()


@dataclass(frozen=True)
class AttemptSummary:
    """One stored submission attempt, read back under its idempotency key.

    Constructed only by this module, from a row the database has already accepted -- the
    contract :class:`approval.ApprovalRecord` carries, and every field is re-gated on the
    way out anyway, because values read back out of the ledger are untrusted per
    CLAUDE.md's threat boundary.

    Every field is JSON-native (``str``/``bool``/``None``), so the persisted form used for
    replay comparison is ``json.dumps(dataclasses.asdict(summary), ensure_ascii=True,
    sort_keys=True)`` -- the M1-305 rule that survives the lone surrogates
    ``str.encode('utf-8')`` does not.

    This is deliberately a **summary**, not the full ``SubmissionReceipt``: it carries what
    a caller needs to decide "this key is spent, and here is what it bought", and none of
    the response body, headers or error text. A duplicate-check that hands back a stored
    response body is a leak channel for one line of convenience.

    ``completed_at_utc`` is ``str | None`` because ``001`` declares the column nullable.
    :func:`lifecycle.record_submission_attempt` requires it (M1-603 round 2, finding 5), so
    every row this package writes has one; a ``None`` here means the row came from
    somewhere else.

    ``refetch_outcome`` is ``RefetchOutcome | None`` for the neighbouring reason and a
    different one. ``009`` added the column, so a row written before it holds ``NULL`` and
    no value can be invented for it; and unlike ``completed_at_utc`` that ``None`` is
    *expected* of any attempt this ledger recorded under M2-704. Both it and
    ``verified_by_refetch`` are read here rather than one being derived from the other:
    these are two stored columns, and reporting what the ledger holds is this reader's
    whole job -- the derivation belongs to the writer, where there is one fact to derive
    from.
    """

    attempt_id: str
    forecast_record_id: str
    idempotency_key: str
    requested_at_utc: str
    completed_at_utc: str | None
    request_payload_sha256: str
    success: bool
    verified_by_refetch: bool
    refetch_outcome: RefetchOutcome | None
    created_at_utc: str


@dataclass(frozen=True)
class KeyReservation:
    """One durable claim on an idempotency key, held while a submission is in flight.

    Minted by :func:`reserve_submission_key`, read back by
    :func:`live_reservation_for_key`. Same contract as :class:`AttemptSummary`: constructed
    only by this module, from values the database has already accepted, and re-gated on the
    way out anyway. Every field is JSON-native, so the persisted form used for replay
    comparison is the one that class documents -- ``json.dumps(dataclasses.asdict(...),
    ensure_ascii=True, sort_keys=True)``.

    **A reservation is not an attempt and never becomes one.** It says *this key is spoken
    for*; it says nothing about whether a post happened, which is
    ``submission_attempts``' answer. The two tables are the claim and the call, the same
    way ``submission_attempts`` and ``submission_verifications`` are the call and its
    verification -- and keeping them apart is what lets both stay strictly append-only
    while the state of a key still changes.

    ``reservation_seq`` numbers one key's reservations from 1. A key normally has exactly
    one. It has more when an earlier claim was released without being spent, which is what
    makes a transient pre-post failure recoverable rather than permanent: without a second
    sequence number, a key -- being a pure function of the tournament, question, forecast
    version and payload hash -- could never be claimed again by the work that derived it.
    """

    reservation_id: str
    idempotency_key: str
    forecast_record_id: str
    reservation_seq: int
    reserved_at_utc: str


def canonical_key_json(
    *,
    tournament_id: str,
    question_id: int,
    forecast_version: int,
    request_payload_sha256: str,
) -> str:
    """Return the exact string :func:`submission_key` digests.

    Exposed for the reason ``packet.canonical_packet_json`` and
    ``hashing.normalize_content`` are: a hash that cannot be inspected is a hash whose
    disagreements cannot be explained. Every input is validated here, so this is also the
    single place the accepted domain is defined.
    """
    payload: dict[str, object] = {
        "key_schema_version": KEY_SCHEMA_VERSION,
        "tournament_id": _require_identifier(tournament_id, "tournament_id"),
        "question_id": _require_identifier_int(question_id, "question_id"),
        "forecast_version": _require_identifier_int(forecast_version, "forecast_version"),
        "request_payload_sha256": _require_sha256(request_payload_sha256, "request_payload_sha256"),
    }
    try:
        # ensure_ascii escapes lone surrogates rather than failing to encode them
        # (M1-305 round 2); sort_keys and the compact separators make the rendering
        # canonical; allow_nan=False refuses NaN/Infinity, which json.dumps would
        # otherwise emit as bare `NaN`/`Infinity` -- not valid JSON, and a value SQLite
        # reads back as NULL. The validators above already exclude every one of those, so
        # this cannot currently raise; the guard stays because a hash rule that starts
        # leaking a value the day a field is added is not a rule anyone can rely on.
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        # from None and a constant message: json.dumps names the offending value in both
        # the circular-reference and the out-of-range-float cases.
        raise SubmissionError(
            "the idempotency-key material could not be rendered as canonical JSON "
            "(detail withheld: it can echo the supplied values)"
        ) from None


def submission_key(
    *,
    tournament_id: str,
    question_id: int,
    forecast_version: int,
    request_payload_sha256: str,
) -> str:
    """Return the idempotency key for one submission of one forecast version.

    Pure: no I/O, no SQL, no clock. The same four inputs always give the same key, which
    is what makes a replayed run reuse the key it already spent instead of minting a new
    one -- and what makes a *changed payload* a different key, because
    ``request_payload_sha256`` is part of the material.
    """
    material = canonical_key_json(
        tournament_id=tournament_id,
        question_id=question_id,
        forecast_version=forecast_version,
        request_payload_sha256=request_payload_sha256,
    )
    return _KEY_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()


def submission_key_for_record(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    request_payload_sha256: str,
) -> str:
    """Derive the key for a stored forecast record and a request payload.

    Reads ``tournament_id``/``question_id``/``forecast_version`` from
    ``forecast_records``. Those three columns shipped with ``001``, and
    ``approval.read_forecast_summary`` already reads them; M1-602 owns the *writer*, not
    the columns, which is why this works ahead of it.

    ``request_payload_sha256`` is validated **before** the ledger is touched: it is this
    caller's own parameter, and a caller mistake should be refused without a read, the
    rule M1-303 round 4 settled for billable calls and which costs nothing to keep here.

    Raises when ``record_id`` names no stored record, matching
    ``approval.read_forecast_summary`` and ``lifecycle.current_status``: a caller that
    could not tell "no such record" from "nothing recorded yet" would report the wrong one.
    """
    payload_sha = _require_sha256(request_payload_sha256, "request_payload_sha256")
    identifier = _require_identifier(record_id, "record_id")
    row = _fetch_one(
        conn,
        "SELECT tournament_id, question_id, forecast_version FROM forecast_records "
        "WHERE record_id = ?",
        (identifier,),
    )
    if row is None:
        raise SubmissionError("record_id does not name a stored forecast record")
    return submission_key(
        tournament_id=_stored_text(row[0], "tournament_id"),
        question_id=_stored_int(row[1], "question_id"),
        forecast_version=_stored_int(row[2], "forecast_version"),
        request_payload_sha256=payload_sha,
    )


# M2-707. Module constants for the same reason `_SPENT_KEY_REFUSAL` is one: a refusal an
# operator will act on is asserted by tests and rendered by the CLI, and two spellings of
# one bound is the defect M1-608 and M2-710 were both filed for.
_UNBOUND_APPROVAL_REFUSAL = (
    "the approval in force for this forecast record predates the payload binding "
    "(migration 011) and so authorizes no particular payload; approve the record again to "
    "bind the decision to the payload it authorizes"
)
_UNAUTHORIZED_PAYLOAD_REFUSAL = (
    "this submission payload is not the one the approval in force authorized, so no "
    "submission key may be derived for it; either submit the payload this record derives "
    "or approve the record again"
)


def submission_key_for_approved_record(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    request_payload_sha256: str,
) -> str:
    """Derive the key, and refuse unless an approval is in force for this record.

    The gated seam. :func:`submission_key_for_record` will mint a key for a ``draft`` --
    which is what a dry run needs, because a dry run is how an operator sees what *would*
    be submitted before deciding whether to approve it (M2-703). Every path that leads to
    a real post goes through this one instead, and D23's "submission is a separate
    approved command" is the reason the two are separate functions rather than one
    function with a flag: a bound any caller can lift is not a bound.

    **Two checks, and the second is not redundant.** ``approval.effective_approval``
    answers "was this forecast approved", derived from the lifecycle event so an
    ``approval_events`` row nothing acted on does not count. It does **not** answer "is it
    approved *now*": an approval event is append-only history, and a record carries it
    forever, so a record that has since reached terminal ``failed`` or ``submitted`` still
    reports one. ``lifecycle.current_status`` is the second check, and without it this
    function would mint a key for a record ``record_submission_attempt`` can no longer
    append an event for -- a live post the ledger cannot record, which is the product's
    primary failure mode (cross-model review round 2, reproduced).

    ``approved`` is the only status this admits, and that is exactly the set
    ``lifecycle._LEGAL_TRANSITIONS`` allows ``submitted``/``submission_failed``/
    ``submission_uncertain`` to leave from. An *uncertain* attempt leaves the record at
    ``approved`` and so still passes -- deliberately, per M1-603: whether to make a second
    request while one is unresolved is decided by ``lifecycle.unresolved_uncertainties``,
    not by refusing to derive a key.

    **The third check is M2-707, and it is what closes D33.** Until this item an approval
    bound to ``forecast_sha256`` alone, so one approved forecast covered every payload built
    from it and this function could not tell the payload an operator reviewed from any other
    payload of the same forecast. ``approval_events.payload_sha256`` (migration ``011``)
    now carries the digest of the payload the decision authorized, and
    ``request_payload_sha256`` must equal it. Everything downstream follows from that one
    comparison: the key is derived from the payload hash, so a payload the approval did not
    authorize cannot reach a key, a reservation, or a post.

    **A pre-``011`` approval carries no binding and is refused rather than exempted.** The
    column is nullable because ``ADD COLUMN`` cannot be otherwise, so a ``None`` here is a
    row written before the binding existed -- not a row that authorized every payload. The
    stricter reading is the one that costs a re-approval and not a post nobody reviewed, and
    this project has no live approvals to strand: ``submission.enabled`` is committed false
    and M2-706 has never run.

    **What it still does not establish** is that the stored digest is the payload the record
    *derives* -- that is a question about canonical JSON, the pinned SDK's CDF conversion
    and ``numeric_calibration``, none of which this module or the ledger can see.
    :func:`whiskeyjack_bot.submission_payload.payload_sha256_for_record` establishes it, at
    approve time, and the digest carries it forward. A wrong digest therefore fails closed
    -- the payload an operator submits will not match it -- rather than opening a path.
    """
    payload_sha = _require_sha256(request_payload_sha256, "request_payload_sha256")
    identifier = _require_identifier(record_id, "record_id")
    try:
        approval = effective_approval(conn, identifier)
        status = current_status(conn, identifier)
    except ApprovalError as exc:
        raise _wrap_approval(exc) from None
    except LifecycleError as exc:
        raise _wrap_lifecycle(exc) from None
    if approval is None:
        raise SubmissionError(
            "this forecast record holds no approval in force, so no submission key may be "
            "derived for it; approve the record first"
        )
    if status != "approved":
        # The status is one of `lifecycle.LifecycleStatus`, a closed vocabulary this
        # package defines, so naming it is safe -- and it is what makes the refusal
        # actionable. It is not a stored value in the sense the hygiene rule guards.
        raise SubmissionError(
            f"this forecast record was approved but is no longer awaiting submission "
            f"(it is {status}), so no submission key may be derived for it"
        )
    if approval.payload_sha256 is None:
        raise SubmissionError(_UNBOUND_APPROVAL_REFUSAL)
    if approval.payload_sha256 != payload_sha:
        # Neither digest is printed. One is a stored value and the other is the caller's,
        # so echoing them would let a caller confirm a guess about what was approved --
        # `lifecycle._require_hash_binds` and `approval._record_decision` refuse the
        # analogous forecast-hash mismatch in exactly these terms.
        raise SubmissionError(_UNAUTHORIZED_PAYLOAD_REFUSAL)
    return submission_key_for_record(conn, identifier, request_payload_sha256=payload_sha)


def attempt_for_key(conn: sqlite3.Connection, idempotency_key: str) -> AttemptSummary | None:
    """Return the attempt already recorded under this key, or ``None``.

    The key is validated as a non-blank storable identifier only (:func:`_require_identifier`),
    not against :func:`submission_key`'s own format. A ledger may hold keys minted under an
    earlier schema version, and a reader that refused to look at them would report an unused
    key for one that is spent -- the exact answer that costs a second live post.
    """
    key = _require_identifier(idempotency_key, "idempotency_key")
    row = _fetch_one(
        conn,
        "SELECT attempt_id, forecast_record_id, idempotency_key, requested_at_utc, "
        "completed_at_utc, request_payload_sha256, success, verified_by_refetch, "
        "refetch_outcome, created_at_utc FROM submission_attempts WHERE idempotency_key = ?",
        (key,),
    )
    if row is None:
        return None
    return AttemptSummary(
        attempt_id=_stored_text(row[0], "attempt_id"),
        forecast_record_id=_stored_text(row[1], "forecast_record_id"),
        idempotency_key=_stored_text(row[2], "idempotency_key"),
        requested_at_utc=_stored_text(row[3], "requested_at_utc"),
        completed_at_utc=(None if row[4] is None else _stored_text(row[4], "completed_at_utc")),
        request_payload_sha256=_stored_text(row[5], "request_payload_sha256"),
        success=_stored_flag(row[6], "success"),
        verified_by_refetch=_stored_flag(row[7], "verified_by_refetch"),
        refetch_outcome=_stored_refetch_outcome(row[8]),
        created_at_utc=_stored_text(row[9], "created_at_utc"),
    )


def require_key_unused(conn: sqlite3.Connection, idempotency_key: str) -> None:
    """Refuse if this key is spent **or reserved**; return silently otherwise.

    Two conditions, one answer, because a caller asking "may I claim this key" is asking
    one question. A key is unavailable if a :func:`attempt_for_key` row records a call
    already made under it, and equally if a live :func:`live_reservation_for_key` says a
    submission is in flight holding it. A second guard for the second condition would be
    two spellings of one bound with nothing keeping them in agreement -- the defect this
    project has now filed twice (M1-608, M2-710).

    It is still a **read**, and that is still the honest division. ``001``'s ``UNIQUE`` and
    ``010``'s reservation trigger are what cannot be raced; this says the same thing
    beforehand, as this module's own error, so the ordinary case is an explanation rather
    than a stack trace. What changed with M2-708 is what stands behind it:
    :func:`reserve_submission_key` now performs this check and the claim as one act, so the
    live path no longer *depends* on a read. This remains the cheap look for a caller that
    only wants to know.
    """
    key = _require_identifier(idempotency_key, "idempotency_key")
    # Names no value: the key is derived from a payload hash and a tournament, and echoing
    # it back would let a caller confirm a guess about stored content.
    if attempt_for_key(conn, key) is not None:
        raise SubmissionError(_SPENT_KEY_REFUSAL)
    if live_reservation_for_key(conn, key) is not None:
        raise SubmissionError(_RESERVED_KEY_REFUSAL)


def live_reservation_for_key(
    conn: sqlite3.Connection, idempotency_key: str
) -> KeyReservation | None:
    """Return the reservation currently holding this key, or ``None``.

    "Currently holding" means a ``submission_key_reservations`` row with no
    ``submission_key_releases`` row pointing at it. ``010``'s trigger allows at most one
    such row per key, so the ``ORDER BY``/``LIMIT`` below is not how the answer is decided
    -- it is what keeps this reader **total** against a ledger some other program wrote,
    where the invariant was never enforced. A reader that raised on a ledger holding two
    live reservations would refuse to report the very state an operator needs to see.

    Validates the key as a non-blank storable identifier only, for :func:`attempt_for_key`'s
    reason: a ledger may hold keys minted under an earlier schema version, and a reader that
    refused to look at them would report a free key for one that is held.
    """
    key = _require_identifier(idempotency_key, "idempotency_key")
    row = _fetch_one(
        conn,
        "SELECT reservation_id, idempotency_key, forecast_record_id, reservation_seq, "
        "reserved_at_utc FROM submission_key_reservations r WHERE r.idempotency_key = ? "
        "AND NOT EXISTS ("
        "SELECT 1 FROM submission_key_releases x WHERE x.reservation_id = r.reservation_id"
        ") ORDER BY r.reservation_seq DESC LIMIT 1",
        (key,),
    )
    return None if row is None else _reservation_from_row(row)


def live_reservations_for_record(
    conn: sqlite3.Connection, record_id: str
) -> tuple[KeyReservation, ...]:
    """Every reservation currently held against this forecast record, oldest first.

    :func:`live_reservation_for_key` is keyed by the idempotency key, and the key is a
    pure function of the tournament, question, forecast version and payload hash -- so an
    operator recovering from a crash would have to reproduce the payload byte for byte
    before they could name the thing they are trying to release. ``forecast_record_id`` is
    a column, so this asks the question they can actually answer.

    **Returns a tuple, not one row, deliberately.** ``010``'s trigger constrains one *key*
    to one live reservation; it does not stop one record from holding two under two
    different payload hashes, which is what a second command with a changed payload
    leaves behind. A reader that returned a single row would have to pick, and the picking
    would be invisible to the operator deciding what to release.

    Validates the identifier as a non-blank storable identifier only, for
    :func:`attempt_for_key`'s reason: a ledger may hold rows written under an earlier schema
    version, and a reader that refused to look at them would report a free record for one
    that is held.
    """
    identifier = _require_identifier(record_id, "record_id")
    rows = _fetch_all(
        conn,
        "SELECT reservation_id, idempotency_key, forecast_record_id, reservation_seq, "
        "reserved_at_utc FROM submission_key_reservations r WHERE r.forecast_record_id = ? "
        "AND NOT EXISTS ("
        "SELECT 1 FROM submission_key_releases x WHERE x.reservation_id = r.reservation_id"
        ") ORDER BY r.reservation_seq ASC",
        (identifier,),
    )
    return tuple(_reservation_from_row(row) for row in rows)


def reserve_submission_key(
    conn: sqlite3.Connection,
    *,
    record_id: str,
    idempotency_key: str,
    reserved_at: datetime,
) -> KeyReservation:
    """Claim this key durably. **The check and the claim are one act** (M2-708).

    :func:`require_key_unused` is a read, and until this existed it was the whole guard in
    front of a live post. Two commands could both read one key as unused, both post, and
    ``001``'s ``UNIQUE`` would then refuse the second *row* -- after its call had already
    been made. The constraint protected the shape of the ledger and not the platform, and
    the acceptance criterion asks for the platform: *two concurrent commands for the same
    derived key durably select one poster before any network I/O*.

    Three layers, and it is worth being precise about which one does what:

    - :func:`lifecycle.transaction` is ``BEGIN IMMEDIATE``, so the write lock is taken
      **before** the read below and the read-then-write cannot interleave with another
      writer's. That is what makes the ordinary contended case a clean typed refusal
      rather than a lock upgrade that cannot be retried from inside an open transaction.
    - ``010``'s ``submission_key_reservations_validate_on_insert`` refuses a second live
      reservation, a spent key, and a sequence number that is not the next one. **This is
      the enforcement**, and it is the layer that cannot be raced.
    - ``UNIQUE (idempotency_key, reservation_seq)`` turns any race that does occur into a
      loud failure rather than a silently duplicated claim.

    Exactly the division :func:`lifecycle.transaction` documents for ``event_seq``, and the
    reason the Python checks below are not the guarantee: they are what turn the guarantee
    into a message an operator can act on.

    Every input is validated before the ledger is touched (M1-303 round 4: refuse a caller
    mistake before the spend), and ``reservation_id`` is minted here rather than accepted,
    so no caller can supply one that collides with a row it cannot see.

    **What it does not check** is that ``idempotency_key`` is the key
    :func:`submission_key_for_record` would derive for ``record_id`` -- that needs the
    payload hash, which is the caller's and not stored anywhere this could read. ``010``
    catches the consequence that matters, one key reserved against two different records,
    and :func:`submission_live.post_approved_forecast` derives both values from the same
    row in the same breath.

    A reservation is **not** released on success. The attempt row spends it, and the
    derived state of the key becomes ``spent`` -- which is why nothing here has to be
    undone on the happy path, and why the writer of the attempt row is unchanged.
    """
    if conn.in_transaction:
        # A reservation that is not durable the moment it is made is not a reservation.
        # `lifecycle.transaction` nests as a SAVEPOINT when the caller already holds a
        # transaction, and RELEASE does not commit -- so inside one, this would return a
        # KeyReservation the caller could still erase with a ROLLBACK, after a post it
        # had already authorized. Round 1 reproduced exactly that: one forecast posted
        # twice, with no ledger row for the first call.
        #
        # Refusing is the only honest answer, because the durability is the whole point
        # of the row. It cannot be fixed by committing here either: the enclosing
        # transaction is the caller's, and this is not the layer that may end it.
        raise SubmissionError(
            "a key reservation must be durable the moment it is taken, so it cannot be "
            "made inside a caller's open transaction; commit or roll back first"
        )
    identifier = _require_identifier(record_id, "record_id")
    key = _require_identifier(idempotency_key, "idempotency_key")
    reserved = _require_utc(reserved_at, "reserved_at")
    reservation_id = _RESERVATION_PREFIX + uuid.uuid4().hex
    try:
        with transaction(conn):
            if attempt_for_key(conn, key) is not None:
                raise SubmissionError(_SPENT_KEY_REFUSAL)
            if live_reservation_for_key(conn, key) is not None:
                raise SubmissionError(_RESERVED_KEY_REFUSAL)
            sequence = _next_reservation_seq(conn, key)
            _execute(
                conn,
                "INSERT INTO submission_key_reservations (reservation_id, idempotency_key, "
                "forecast_record_id, reservation_seq, reserved_at_utc, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (reservation_id, key, identifier, sequence, reserved, _utc_text(_utcnow())),
            )
            return KeyReservation(
                reservation_id=reservation_id,
                idempotency_key=key,
                forecast_record_id=identifier,
                reservation_seq=sequence,
                reserved_at_utc=reserved,
            )
    except LifecycleError as exc:
        # Includes the losing side of a contended `BEGIN IMMEDIATE` whose busy timeout
        # expires. That is a refusal like any other here, and it means the same thing:
        # this caller did not get the key, so this caller posts nothing.
        raise _wrap_lifecycle(exc) from None


def release_submission_key(
    conn: sqlite3.Connection,
    reservation: KeyReservation,
    *,
    reason: ReservationReason,
    released_at: datetime,
    released_by: str | None = None,
    note: str | None = None,
) -> None:
    """Give up a reservation that was never spent, so the key can be claimed again.

    **Why this exists.** An atomic reservation creates a state that did not exist before:
    reserved, but with no attempt row. An idempotency key is a pure function of the
    tournament, question, forecast version and payload hash, so a claim with no exit does
    not block a retry -- it blocks that forecast, permanently, on an append-only table.
    Two ways out, and they are different claims (see :data:`ReservationReason`):
    ``not_posted`` is the program reporting that it proved no post was made;
    ``operator_abandoned`` is a person asserting it after checking the platform.

    ``released_by`` is required for the second and refused for the first. Requiring one the
    program would have to invent is ``approve``'s rule -- an attribution claim about a
    person is never inferred from the machine -- and accepting one for ``not_posted`` would
    put a name against a conclusion no person reached.

    Only ``reservation.reservation_id`` is read. Nothing else on the value object reaches
    the row, so there is no second source of truth for the caller to get wrong -- M2-703
    round 1's finding, applied by construction rather than checked afterwards. The key the
    "already spent" test runs against is read back **from the stored row**, not taken from
    the object.

    Refuses, rather than silently doing nothing, when the reservation was consumed by a
    recorded attempt: that reservation was not abandoned, and recording it as abandoned
    would assert something false about an irreversible call. ``010`` refuses it too.
    """
    if type(reservation) is not KeyReservation:
        # Exact type, not isinstance, for `record_submission_attempt`'s reason: a subclass
        # can shadow a field with a property, turning the read below into caller code.
        raise SubmissionError("reservation must be a KeyReservation")
    reservation_id = _require_identifier(reservation.reservation_id, "reservation.reservation_id")
    reason_text = _require_reason(reason)
    released = _require_utc(released_at, "released_at")
    actor = _require_optional_identifier(released_by, "released_by", max_length=MAX_ACTOR_LENGTH)
    note_text = _require_optional_text(note, "note", max_length=MAX_NOTE_LENGTH)
    if reason_text == "not_posted" and actor is not None:
        raise SubmissionError(
            "a not_posted release records that the program proved no post was made, so it "
            "names no person; omit released_by"
        )
    if reason_text != "not_posted" and actor is None:
        raise SubmissionError(
            f"a release with reason {reason_text} is an assertion by a person about what "
            "is on the platform, so released_by is required"
        )
    release_id = _RELEASE_PREFIX + uuid.uuid4().hex
    try:
        with transaction(conn):
            row = _fetch_one(
                conn,
                "SELECT idempotency_key FROM submission_key_reservations WHERE reservation_id = ?",
                (reservation_id,),
            )
            if row is None:
                raise SubmissionError("reservation_id does not name a stored key reservation")
            stored_key = _stored_text(row[0], "idempotency_key")
            if attempt_for_key(conn, stored_key) is not None:
                raise SubmissionError(
                    "this reservation was consumed by a recorded submission attempt and so "
                    "was not abandoned; there is nothing to release"
                )
            if live_reservation_for_key(conn, stored_key) is None:
                raise SubmissionError("this key reservation has already been released")
            _execute(
                conn,
                "INSERT INTO submission_key_releases (release_id, reservation_id, reason, "
                "released_by, note, released_at_utc, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    release_id,
                    reservation_id,
                    reason_text,
                    actor,
                    note_text,
                    released,
                    _utc_text(_utcnow()),
                ),
            )
    except LifecycleError as exc:
        raise _wrap_lifecycle(exc) from None


def _next_reservation_seq(conn: sqlite3.Connection, idempotency_key: str) -> int:
    """The sequence number ``010``'s trigger will accept for this key's next reservation.

    Read inside the caller's ``BEGIN IMMEDIATE``, which is what makes "the next one" still
    true by the time the insert runs -- ``forecast.store``'s argument for reading a version
    head inside the transaction that writes against it.
    """
    row = _fetch_one(
        conn,
        "SELECT max(reservation_seq) FROM submission_key_reservations WHERE idempotency_key = ?",
        (idempotency_key,),
    )
    if row is None or row[0] is None:
        return 1
    return _stored_int(row[0], "reservation_seq") + 1


def _reservation_from_row(row: sqlite3.Row) -> KeyReservation:
    """Gate a stored reservation on the way out; see :class:`AttemptSummary`."""
    return KeyReservation(
        reservation_id=_stored_text(row[0], "reservation_id"),
        idempotency_key=_stored_text(row[1], "idempotency_key"),
        forecast_record_id=_stored_text(row[2], "forecast_record_id"),
        reservation_seq=_stored_int(row[3], "reservation_seq"),
        reserved_at_utc=_stored_text(row[4], "reserved_at_utc"),
    )


def _wrap_lifecycle(exc: LifecycleError) -> SubmissionError:
    """Re-raise the lifecycle layer as this module's own type; see :func:`_wrap_approval`."""
    return SubmissionError(str(exc) or "the ledger refused to report this record's status")


def _wrap_approval(exc: ApprovalError) -> SubmissionError:
    """Re-raise the approval layer as this module's own type, message preserved.

    Deliberate rather than lazy, and the same call ``approval._wrap_lifecycle`` makes:
    ``ApprovalError``'s own contract guarantees its text names no stored or caller-supplied
    value, and its message is the one thing that makes a refusal actionable. Replacing it
    with a constant would satisfy the letter of the module-own-error rule while destroying
    what the operator needs.
    """
    return SubmissionError(str(exc) or "the ledger refused to report this record's approval")


def _require_text(value: object, field: str, *, max_length: int = MAX_IDENTIFIER_LENGTH) -> str:
    """Return ``value`` as storable text, or raise naming only the *field*.

    ``max_length`` defaults to the identifier bound every caller here wants; the free-text
    fields a release carries (``released_by``, ``note``) pass their own, so there is one
    text validator rather than one per bound.

    The type gate is exact (``type(x) is str``) rather than ``isinstance``, and the encode
    probe is the load-bearing one: ``sqlite3`` encodes text parameters as UTF-8, so a lone
    surrogate reaching a query raises a raw ``UnicodeEncodeError`` **quoting the offending
    character** -- both a leak and an error type callers do not handle. ``approval.py`` and
    ``lifecycle.py`` give the same reasoning at their own boundaries.
    """
    if type(value) is not str or not value:
        raise SubmissionError(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise SubmissionError(f"{field} is longer than the {max_length}-character limit")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # from None: UnicodeEncodeError's own message quotes the character it choked on.
        raise SubmissionError(
            f"{field} contains characters that cannot be stored "
            "(detail withheld: it can echo the value)"
        ) from None
    return value


def _require_identifier(
    value: object, field: str, *, max_length: int = MAX_IDENTIFIER_LENGTH
) -> str:
    """Return ``value`` as non-blank, NUL-free storable text, or raise (M2-710).

    :func:`_require_text` already refuses ``''``, but ``'\\n\\t'`` is truthy and would
    reach ``submission_key``/``canonical_key_json`` through it -- minting a key for a
    ``tournament_id`` no ``forecast_records`` row can ever hold, since
    ``006_non_blank_identifiers.sql`` refuses a whitespace-only value at INSERT.
    ``lifecycle._require_identifier`` solved the identical problem for its own writers;
    this mirrors it rather than widening ``_require_text``, for the same reason that
    function stays split there -- blank prose (``released_by``, ``note``) and blank
    identity mean different things, and only identity columns take the stricter check.

    The blank test is ``str.strip()``, matching the character set
    ``006_non_blank_identifiers.sql`` and ``010_submission_key_reservations.sql`` spell out
    in their triggers' ``trim()`` calls.

    **U+0000 is refused outright**, for the same reason ``lifecycle._require_identifier``
    refuses it: SQLite's ``length()`` stops counting at an embedded NUL, so a
    200-character-limit check in a trigger cannot see past one -- a NUL-bearing identifier
    could pass the schema's ceiling and still fail Python's ``len()`` on read-back. Refusing
    the character here removes the one input the two counting functions disagree about.

    Used for every identifier this module derives a key from or looks a row up by --
    ``tournament_id``, ``record_id``, ``idempotency_key``, ``reservation_id`` -- which are
    exactly the columns 006 and 010 guard with the matching trigger clause.

    ``max_length`` exists only so :func:`_require_optional_identifier` can delegate here
    with the actor bound instead of restating the blank rule. It is one spelling of that
    rule on purpose: two definitions of "blank" that nobody compared is the defect this
    whole family of guards descends from, and a second copy would reopen it one refactor
    later.
    """
    text = _require_text(value, field, max_length=max_length)
    if not text.strip():
        raise SubmissionError(f"{field} must not be blank")
    if "\x00" in text:
        raise SubmissionError(f"{field} must not contain a NUL character")
    return text


def _require_identifier_int(value: object, field: str) -> int:
    """Return a positive, storable integer.

    ``type(value) is int`` rather than ``isinstance``: ``bool`` subclasses ``int``, so
    ``isinstance(True, int)`` is true and ``True`` would hash as question 1.

    The upper bound is the persisted-form rule, not arithmetic caution. SQLite stores
    signed 64-bit integers; a Python int beyond that cannot reach a ``forecast_records``
    row, so a key derived from one could never be reproduced by reading the row back --
    it would be a key that works exactly once, which is the opposite of the point.
    """
    if type(value) is not int:
        raise SubmissionError(f"{field} must be an integer")
    if value < 1:
        raise SubmissionError(f"{field} must be a positive integer")
    if value > _INT64_MAX:
        raise SubmissionError(f"{field} is larger than a 64-bit integer and cannot be stored")
    return value


def _require_sha256(value: object, field: str) -> str:
    """Return a 64-character lowercase hex digest, or raise naming only the field.

    Lowercase is part of the rule rather than a courtesy: ``"AB"`` and ``"ab"`` are the
    same digest and must not be two idempotency keys. Normalizing case here would be the
    other way to get that, and it is the wrong way -- an uppercase digest arriving from a
    caller is a bug in the caller, and silently accepting it hides which of two spellings
    the stored key was built from.
    """
    text = _require_text(value, field)
    if len(text) != 64 or not _HEX_DIGITS.issuperset(text):
        raise SubmissionError(f"{field} must be 64 lowercase hexadecimal characters")
    return text


def _require_optional_text(value: object, field: str, *, max_length: int) -> str | None:
    """``None`` passes through; anything else must be storable text. ``lifecycle``'s."""
    return None if value is None else _require_text(value, field, max_length=max_length)


def _require_optional_identifier(value: object, field: str, *, max_length: int) -> str | None:
    """``None`` passes through; anything else must be non-blank, NUL-free text (M2-710).

    For ``released_by``, and the reason it is not on :func:`_require_optional_text` is not
    the identifier/prose split :func:`_require_identifier` describes -- an actor name is
    prose. It is that ``010_submission_key_reservations.sql`` guards this column and does
    not guard ``note``, in as many words: "Nullable, because the program releases its own
    reservation and has no person to name. Present means a claim about a human, and a
    blank one is worse than none."

    So the writer follows the schema column by column rather than by category. ``note``
    stays on :func:`_require_optional_text`, because ``010`` asks only that it be text and
    a stricter writer would refuse input the ledger accepts -- the same two-spellings-of-
    one-bound defect as M2-710 itself, pointed the other way.

    Until this, ``released_by='   '`` passed the writer, reached the INSERT, and came back
    as :func:`_execute`'s "the ledger rejected this write (detail withheld ...)" -- which
    that function's docstring says is only ever the race its trigger exists to catch.
    """
    return None if value is None else _require_identifier(value, field, max_length=max_length)


def _require_reason(value: object) -> str:
    """Gate a value against :data:`ReservationReason`, this module's closed vocabulary.

    The members are named in the refusal. They are this module's own literals, not caller
    or stored content, and naming them is the only thing that makes the failure fixable --
    the same carve-out ``_assert_prefix_matches_version`` makes for its constants.
    """
    if type(value) is not str or value not in _RESERVATION_REASONS:
        raise SubmissionError("reason must be one of " + ", ".join(sorted(_RESERVATION_REASONS)))
    return value


def _stored_text(value: object, field: str) -> str:
    if type(value) is not str:
        raise SubmissionError(
            f"stored {field} is not text (detail withheld: it can echo stored values)"
        )
    return value


def _stored_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise SubmissionError(
            f"stored {field} is not an integer (detail withheld: it can echo stored values)"
        )
    return value


def _stored_refetch_outcome(value: object) -> RefetchOutcome | None:
    """Gate a stored ``refetch_outcome``, which may legitimately be absent (M2-711).

    ``None`` for a row written before ``009`` added the column; otherwise a member of the
    vocabulary, checked rather than trusted -- values read back out of the ledger are
    untrusted per CLAUDE.md's threat boundary, and a value only becomes safe to name in a
    message once it has been proven to belong to a vocabulary this package defines. This
    one is never named either way, because the refusal cannot know which it is looking at.
    """
    if value is None:
        return None
    if type(value) is not str or value not in _REFETCH_OUTCOMES:
        raise SubmissionError(
            "stored refetch_outcome is not one of the recognized values "
            "(detail withheld: it can echo stored values)"
        )
    return cast(RefetchOutcome, value)


def _stored_flag(value: object, field: str) -> bool:
    """Gate a stored 0/1 column. ``001`` CHECKs both, so anything else is a foreign row."""
    if type(value) is not int or value not in (0, 1):
        raise SubmissionError(
            f"stored {field} is not 0 or 1 (detail withheld: it can echo stored values)"
        )
    return value == 1


def _fetch_one(
    conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> sqlite3.Row | None:
    try:
        row = conn.execute(sql, parameters).fetchone()
    except (sqlite3.Error, OverflowError, UnicodeEncodeError, UnicodeDecodeError):
        # from None: the underlying error's text and traceback can carry stored values,
        # and sqlite3's UnicodeEncodeError quotes the character it could not encode.
        # UnicodeDecodeError is here because sqlite3 decodes TEXT at *fetch*, not at
        # execute (M1-306), so a row holding undecodable bytes raises from this line.
        raise SubmissionError(
            "the ledger could not be read (detail withheld: a database message can echo "
            "stored values)"
        ) from None
    return cast("sqlite3.Row | None", row)


def _fetch_all(
    conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> list[sqlite3.Row]:
    """:func:`_fetch_one` for a whole result set, and the same refusal for the same reasons.

    The fetch is inside the ``try`` rather than after it because ``sqlite3`` decodes TEXT
    at fetch time, not at execute (M1-306), so a row holding undecodable bytes raises
    here and not from the statement above it.
    """
    try:
        rows = conn.execute(sql, parameters).fetchall()
    except (sqlite3.Error, OverflowError, UnicodeEncodeError, UnicodeDecodeError):
        # from None: the underlying error's text and traceback can carry stored values.
        raise SubmissionError(
            "the ledger could not be read (detail withheld: a database message can echo "
            "stored values)"
        ) from None
    return cast("list[sqlite3.Row]", rows)


def _execute(conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...]) -> None:
    """Run one INSERT, wrapping every database failure as this module's own error.

    ``lifecycle._insert``'s contract and its reasoning: callers only handle
    :class:`SubmissionError`, so a raw ``sqlite3.Error`` -- including the
    ``IntegrityError`` one of ``010``'s triggers raises -- must not escape, and the
    database's own text is not forwarded, because SQLite naming tables rather than values
    is a property of its formatting today and not a contract this module may rest on.

    Every actionable case is refused with its own message before the statement runs. What
    reaches here is the race the trigger exists to catch, and a caller that loses it has
    still posted nothing.
    """
    try:
        conn.execute(sql, parameters)
    except (sqlite3.Error, OverflowError, UnicodeEncodeError):
        # from None: the underlying error's text and traceback can carry stored values.
        raise SubmissionError(
            "the ledger rejected this write (detail withheld: a database message can echo "
            "stored values)"
        ) from None


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _utc_text(value: datetime) -> str:
    """Render an already-validated aware datetime in ``lifecycle``'s canonical stored form."""
    return _require_aware_utc(value, "timestamp").isoformat(timespec="microseconds")


def _require_aware_utc(value: object, field: str) -> datetime:
    """Return an aware datetime converted to UTC, or raise. Mirrors ``lifecycle``'s.

    Exact type rather than ``isinstance``: a ``datetime`` subclass can override
    ``isoformat()`` and write arbitrary text into a pinned timestamp column.

    The conversion is guarded, and broadly, for the reason ``lifecycle._require_aware_utc``
    gives: ``tzinfo`` is an abstract base class, so ``utcoffset()`` and ``astimezone()`` run
    caller-supplied code on a value that has passed every type gate above. ``except
    Exception`` is the right width precisely because what arbitrary code can raise is not
    enumerable.
    """
    if type(value) is not datetime:
        raise SubmissionError(f"{field} must be a datetime")
    try:
        aware = value.tzinfo is not None and value.utcoffset() is not None
    except Exception:
        raise SubmissionError(
            f"{field} has a timezone that could not be read "
            "(detail withheld: it can echo the value)"
        ) from None
    if not aware:
        raise SubmissionError(f"{field} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except Exception:
        raise SubmissionError(
            f"{field} could not be converted to UTC (detail withheld: it can echo the value)"
        ) from None


def _require_utc(value: object, field: str) -> str:
    """Return an aware datetime as the canonical stored UTC string, or raise.

    ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``: fixed width 32, always UTC, microseconds always
    present. ``010`` pins that exact form on ``reserved_at_utc`` and ``released_at_utc``
    because it compares the two, and a lexicographic comparison against an unknown format
    is a coin toss that reads like a check (003's round-4 finding, on the same columns one
    table over).

    This is the third spelling of one rule -- ``lifecycle._require_utc`` and
    ``submission_gateway._require_aware_utc`` are the others -- and the duplication is the
    error-hygiene rule's price: each module owns the exception type its callers handle.
    What keeps them from drifting is a test that drives all three over the same datetimes
    and asserts the rendered text is equal, which is M2-710's rule (compare the layers over
    one input set) applied to a bound rather than to a validator.
    """
    return _utc_text(_require_aware_utc(value, field))
