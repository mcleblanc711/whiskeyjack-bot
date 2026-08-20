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
used?" positively, and :func:`require_key_unused` is what M2-703/M2-704 call before
minting an attempt -- so "same payload/key cannot create two attempts" is a typed refusal
that names the collision, rather than a ``sqlite3.IntegrityError`` surfacing from the
UNIQUE constraint after the caller has already decided to post.

**Two derivation seams, not one.** :func:`submission_key_for_record` will mint a key for
any stored record including a ``draft``, because that is what a dry run needs -- a dry run
is how an operator sees what *would* be submitted before deciding whether to approve it
(M2-703). :func:`submission_key_for_approved_record` refuses unless the record both holds
an approval **and** is still at ``approved``, and every path leading to a real post goes
through it. They are two functions rather than one function with a flag, for the reason
M1-402 settled: a bound any caller can lift is not a bound.

**What this module does not do.** It does not build the request payload -- M1-502/M1-503
own that, and ``request_payload_sha256`` is an *input* here, exactly as the backlog says
("derive keys from ... and payload hash"). Nor does it check that a payload *is* the one
an approval meant. An approval binds to ``forecast_sha256``, so one approved forecast
covers every payload built from it: a payload that changed without the forecast changing
gets a new key and keeps the old approval. That is a **known, recorded gap**, not an
oversight -- it cannot be closed without the forecast->payload mapping, which does not
exist yet. **Decision D33** records the reading; **M2-707** is the filed item; M2-704 is
where the check lands. See ``docs/M2-NOTES.md``.

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
from dataclasses import dataclass
from typing import cast

from whiskeyjack_bot.approval import ApprovalError, effective_approval
from whiskeyjack_bot.lifecycle import LifecycleError, current_status

# Bumping this changes every key, which is the point: a rule change must be visible as a
# different key rather than silently reinterpreting stored ones. It is part of the hashed
# payload *and* of the prefix, so the rule and its declared version cannot drift apart.
KEY_SCHEMA_VERSION = "1.0.0"

# The visible scheme tag. Written as a literal rather than derived from the version so
# that _assert_prefix_matches_version has something to check; a computed prefix would
# agree with the version by construction and prove nothing.
_KEY_PREFIX = "wjsub-1-"

_HEX_DIGITS: frozenset[str] = frozenset("0123456789abcdef")

# Matches `lifecycle._MAX_IDENTIFIER` and `approval._MAX_IDENTIFIER` (M1-608 is the item
# that pins the three together). It exists here to keep a hostile value away from
# sqlite3's parameter binding, not to restate the writer's field rules.
_MAX_IDENTIFIER = 200

# SQLite stores integers as signed 64-bit. A Python int outside that range cannot be
# persisted, so a key derived from one could never be reproduced from the stored row --
# the M1-305 persisted-form rule applied to integers rather than to datetimes.
_INT64_MAX = 2**63 - 1

# 8-character prefix + 64 hex characters. Well inside the writer's 200-character bound.
KEY_LENGTH = len(_KEY_PREFIX) + 64


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
    """

    attempt_id: str
    forecast_record_id: str
    idempotency_key: str
    requested_at_utc: str
    completed_at_utc: str | None
    request_payload_sha256: str
    success: bool
    verified_by_refetch: bool
    created_at_utc: str


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
        "tournament_id": _require_text(tournament_id, "tournament_id"),
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
    identifier = _require_text(record_id, "record_id")
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

    **What it still does not establish** is that ``request_payload_sha256`` is the payload
    that approval meant: an approval binds to ``forecast_sha256``, and one approved
    forecast covers every payload built from it. Closing that needs the forecast->payload
    mapping, which lives in M1-502/M1-503 and does not exist yet; **M2-707** is the filed
    item and M2-704 is where the check lands. See ``docs/M2-NOTES.md`` and decision D33.
    """
    payload_sha = _require_sha256(request_payload_sha256, "request_payload_sha256")
    identifier = _require_text(record_id, "record_id")
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
    return submission_key_for_record(conn, identifier, request_payload_sha256=payload_sha)


def attempt_for_key(conn: sqlite3.Connection, idempotency_key: str) -> AttemptSummary | None:
    """Return the attempt already recorded under this key, or ``None``.

    The key is validated as *storable text* only, not against
    :func:`submission_key`'s own format. A ledger may hold keys minted under an earlier
    schema version, and a reader that refused to look at them would report an unused key
    for one that is spent -- the exact answer that costs a second live post.
    """
    key = _require_text(idempotency_key, "idempotency_key")
    row = _fetch_one(
        conn,
        "SELECT attempt_id, forecast_record_id, idempotency_key, requested_at_utc, "
        "completed_at_utc, request_payload_sha256, success, verified_by_refetch, "
        "created_at_utc FROM submission_attempts WHERE idempotency_key = ?",
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
        created_at_utc=_stored_text(row[8], "created_at_utc"),
    )


def require_key_unused(conn: sqlite3.Connection, idempotency_key: str) -> None:
    """Refuse if this key has already been spent; return silently otherwise.

    This is the guard M2-703 and M2-704 call *before* deciding to post. ``001``'s UNIQUE
    constraint is what actually makes a second attempt impossible, and it stays the
    enforcement -- but it fires as a ``sqlite3.IntegrityError`` at the write, which is
    after a gateway has already made its decision and, on the live path, possibly its
    call. This says the same thing beforehand, as this module's own error.

    It is a **read**, so it is not by itself a race-free claim: two processes could both
    see an unused key. That is deliberate and it is the honest division -- the UNIQUE
    constraint is the one that cannot be raced, and callers must still let it decide. The
    guard exists to turn the ordinary case into an explanation rather than a stack trace.
    """
    if attempt_for_key(conn, idempotency_key) is not None:
        # Names no value: the key is derived from a payload hash and a tournament, and
        # echoing it back would let a caller confirm a guess about stored content.
        raise SubmissionError(
            "this idempotency key has already been used by a recorded submission attempt; "
            "a second attempt under it would claim a second live post"
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


def _require_text(value: object, field: str) -> str:
    """Return ``value`` as storable text, or raise naming only the *field*.

    The type gate is exact (``type(x) is str``) rather than ``isinstance``, and the encode
    probe is the load-bearing one: ``sqlite3`` encodes text parameters as UTF-8, so a lone
    surrogate reaching a query raises a raw ``UnicodeEncodeError`` **quoting the offending
    character** -- both a leak and an error type callers do not handle. ``approval.py`` and
    ``lifecycle.py`` give the same reasoning at their own boundaries.
    """
    if type(value) is not str or not value:
        raise SubmissionError(f"{field} must be a non-empty string")
    if len(value) > _MAX_IDENTIFIER:
        raise SubmissionError(f"{field} is longer than the {_MAX_IDENTIFIER}-character limit")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # from None: UnicodeEncodeError's own message quotes the character it choked on.
        raise SubmissionError(
            f"{field} contains characters that cannot be stored "
            "(detail withheld: it can echo the value)"
        ) from None
    return value


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
