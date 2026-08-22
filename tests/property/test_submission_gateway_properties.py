"""Property tests for the dry-run gateway, its payload hash and its artifact (M2-703).

The CLAUDE.md pre-review fuzz pass for a hash/canonicalizer/validator: never raises
outside the module's own error type, replay-stability across the persisted form, a stated
identity claim, and no value leak in any message *or rendered traceback*. Three are
specific to this item:

**Determinism is the item's word, so it is a property and not an example.** The backlog
says the gateway returns "a deterministic receipt"; the fuzz asserts that over generated
requests, not over the one payload a unit test happened to pick.

**No-leak is asserted by closing the message set, not by substring search.** A property
that checks "the input does not appear in the message" passes vacuously whenever the draw
is a single common character, which is most draws -- M1-607's lesson. The set of messages
this module can produce is finite and written down below; every refusal must be a member.

**The accepted domain is exactly what round-trips.** `canonical_payload_json` refuses a
payload whose own rendering does not reparse to itself, so the replay property below is a
claim about every accepted input rather than about the ones the strategies remembered to
generate.

Every property here was re-run against a deliberately weakened module and confirmed to
fail first; three of M1-303's ten new properties passed against the pre-fix tree
(docs/LESSONS.md, lesson 5).
"""

from __future__ import annotations

import itertools
import json
import re
import traceback
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from strategies import HOSTILE_TEXT

from whiskeyjack_bot.submission import submission_key
from whiskeyjack_bot.submission_gateway import (
    DryRunSubmissionGateway,
    GatewayError,
    SubmissionRequest,
    attempt_from_receipt,
    canonical_payload_json,
    dry_run_artifact_path,
    dry_run_attempt_id,
    payload_sha256,
    read_dry_run_artifact,
)

FIXED = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

# Anything at all, including the shapes the accepted domain excludes: non-string object
# keys, tuples, sets, bytes, datetimes, non-finite floats and self-similar nesting. The
# point is that *none* of them may escape as anything but a GatewayError.
ANY_VALUE = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats()
    | HOSTILE_TEXT
    | st.binary(max_size=8)
    | st.datetimes(timezones=st.just(timezone.utc)),
    lambda children: (
        st.lists(children, max_size=4)
        | st.tuples(children)
        | st.frozensets(st.integers(), max_size=3)
        | st.dictionaries(HOSTILE_TEXT | st.integers() | st.none(), children, max_size=4)
    ),
    max_leaves=12,
)
ANY_PAYLOAD = st.dictionaries(HOSTILE_TEXT | st.integers() | st.none(), ANY_VALUE, max_size=4)

# Inside the accepted domain: JSON-native only, string keys only.
JSON_VALUE = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(HOSTILE_TEXT, children, max_size=4)
    ),
    max_leaves=12,
)
JSON_PAYLOAD = st.dictionaries(HOSTILE_TEXT, JSON_VALUE, max_size=5)

IDENTIFIERS = st.text(
    st.characters(min_codepoint=48, max_codepoint=122, categories=["Ll", "Lu", "Nd"]),
    min_size=1,
    max_size=16,
)

# Every message `payload_sha256` can produce, as patterns that capture nothing from the
# input. Two interpolate: `{field}`, which is this module's own literal, and a Python type
# name, which comes from a class definition rather than from payload content.
_MESSAGE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^payload must be a JSON object \(a mapping\)$",
        r"^payload could not be read as a mapping \(detail withheld: it can echo the payload\)$",
        r"^payload contains an object key that is not a string; JSON silently coerces such "
        r"keys and can collapse two entries into one \(offending key withheld\)$",
        r"^payload nests deeper than the \d+-level limit "
        r"\(a self-referential payload reaches this first\)$",
        r"^payload contains a non-finite number, which JSON cannot represent "
        r"\(offending value withheld\)$",
        r"^payload contains a [A-Za-z_][A-Za-z0-9_]*, which is not a JSON value; objects "
        r"must be mappings and arrays must be lists$",
        r"^the submission payload could not be rendered as canonical JSON "
        r"\(detail withheld: it can echo the payload\)$",
        r"^the submission payload does not survive its own canonical rendering, so a replay "
        r"could not reproduce it; two object keys most likely differ only in how they spell "
        r"one character \(detail withheld: it can echo the payload\)$",
    )
)


def _accepted(payload: Any) -> str:
    """The canonical rendering, or discard the draw.

    `HOSTILE_TEXT` generates surrogates, and a *pair* of keys built from them can be
    outside the accepted domain -- the module refuses those by design (see
    `canonical_payload_json`'s replay guard). The properties below are claims about every
    **accepted** payload, so a refusal is a draw to discard, not a failure. The refusal
    itself is under test in `test_every_refusal_is_one_of_the_messages_this_module_can_produce`.
    """
    try:
        return canonical_payload_json(payload)
    except GatewayError:
        assume(False)
        raise  # unreachable; assume(False) raises


def _persisted(value: Any) -> str:
    """The M1-305 persisted form: what SQLite and the artifact both actually hold."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


# --- 1. nothing escapes as a foreign error type --------------------------------------


@given(payload=ANY_PAYLOAD)
def test_the_payload_hash_raises_only_this_modules_error(payload: Any) -> None:
    try:
        digest = payload_sha256(payload)
    except GatewayError:
        return
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


@given(
    record_id=IDENTIFIERS,
    question_id=st.integers(),
    payload=ANY_PAYLOAD,
)
def test_submit_raises_only_this_modules_error(
    record_id: str, question_id: int, payload: Any
) -> None:
    request = SubmissionRequest(
        forecast_record_id=record_id,
        question_id=question_id,
        idempotency_key="wjsub-1-" + "0" * 64,
        payload=payload,
    )
    try:
        receipt = DryRunSubmissionGateway(clock=lambda: FIXED).submit(request)
    except GatewayError:
        return
    assert receipt.mode == "dry_run"
    assert receipt.success is False


@given(receipt_mode=HOSTILE_TEXT)
def test_the_ledger_guard_raises_only_this_modules_error(receipt_mode: str) -> None:
    base = DryRunSubmissionGateway(clock=lambda: FIXED).submit(
        SubmissionRequest(
            forecast_record_id="rec-1",
            question_id=1,
            idempotency_key="wjsub-1-" + "0" * 64,
            payload={"a": 1},
        )
    )
    assume(receipt_mode != "live")
    with pytest.raises(GatewayError):
        attempt_from_receipt(replace(base, mode=receipt_mode))  # type: ignore[arg-type]


# --- 2. replay stability across the persisted form -----------------------------------


@given(payload=JSON_PAYLOAD)
def test_an_accepted_payload_survives_its_own_persisted_form(payload: Any) -> None:
    """Byte-identical through render -> parse -> render, which is what a replay does.

    This is the property the whole hash rests on: the digest is of the canonical text, and
    the artifact holds the parse of that text, so a payload that rendered differently on
    the way back would give a stored file the receipt's hash does not describe.
    """
    canonical = _accepted(payload)
    assert canonical.isascii()
    assert canonical_payload_json(json.loads(canonical)) == canonical
    assert payload_sha256(json.loads(canonical)) == payload_sha256(payload)


@given(payload=JSON_PAYLOAD)
def test_the_receipt_survives_the_persisted_form(payload: Any) -> None:
    _accepted(payload)
    receipt = DryRunSubmissionGateway(clock=lambda: FIXED).submit(
        SubmissionRequest(
            forecast_record_id="rec-1",
            question_id=1,
            idempotency_key="wjsub-1-" + "0" * 64,
            payload=payload,
        )
    )
    assert _persisted(asdict(receipt)) == _persisted(json.loads(_persisted(asdict(receipt))))


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=40)
@given(payload=JSON_PAYLOAD)
def test_the_artifact_reads_back_as_the_payload_that_was_hashed(
    tmp_path_factory: pytest.TempPathFactory, payload: Any
) -> None:
    """`@given` with a function-scoped `tmp_path` is a hypothesis health-check failure
    (M1-308): the directory is created once and shared across every draw. A factory gives
    each draw its own."""
    _accepted(payload)
    root = tmp_path_factory.mktemp("artifacts")
    receipt = DryRunSubmissionGateway(artifact_root=root, clock=lambda: FIXED).submit(
        SubmissionRequest(
            forecast_record_id="rec-1",
            question_id=1,
            idempotency_key=submission_key(
                tournament_id="minibench",
                question_id=1,
                forecast_version=1,
                request_payload_sha256=payload_sha256(payload),
            ),
            payload=payload,
        )
    )
    assert receipt.artifact_path is not None
    envelope = read_dry_run_artifact(root, receipt.artifact_path)
    stored = envelope["request_payload"]
    # Re-derived from the file, not taken from the receipt: a receipt that agreed with
    # itself would prove nothing about what was recorded.
    assert payload_sha256(stored) == receipt.request_payload_sha256  # type: ignore[arg-type]
    assert canonical_payload_json(stored) == canonical_payload_json(payload)  # type: ignore[arg-type]


# --- 3. identity: determinism, and two identity spaces that cannot overlap -----------


@given(
    record_id=IDENTIFIERS,
    question_id=st.integers(min_value=1, max_value=2**62),
    payload=JSON_PAYLOAD,
)
def test_the_receipt_is_a_function_of_the_request_and_the_clock(
    record_id: str, question_id: int, payload: Any
) -> None:
    _accepted(payload)
    request = SubmissionRequest(
        forecast_record_id=record_id,
        question_id=question_id,
        idempotency_key=submission_key(
            tournament_id="minibench",
            question_id=question_id,
            forecast_version=1,
            request_payload_sha256=payload_sha256(payload),
        ),
        payload=payload,
    )
    first = DryRunSubmissionGateway(clock=lambda: FIXED).submit(request)
    second = DryRunSubmissionGateway(clock=lambda: FIXED).submit(request)
    assert first == second
    assert _persisted(asdict(first)) == _persisted(asdict(second))


@given(key=st.text(st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=20))
def test_a_dry_run_identity_can_never_be_read_as_a_submission_key(key: str) -> None:
    attempt_id = dry_run_attempt_id(key)
    real = submission_key(
        tournament_id="minibench",
        question_id=1,
        forecast_version=1,
        request_payload_sha256="0" * 64,
    )
    assert attempt_id != real
    assert not attempt_id.startswith(real[:8])
    assert not real.startswith(attempt_id[:8])
    # Derived, so a replayed dry run reuses it rather than minting a second identity.
    assert dry_run_attempt_id(key) == attempt_id


# The pool is chosen so a field-boundary smear is *reachable*: `{"a": 1, "b": 2}` and
# `{"ab": 12}` concatenate to the same characters, and a rendering that joined its parts
# without delimiters would pass an unconstrained fuzz pass while failing here.
_INJECTIVITY_POOL: tuple[dict[str, Any], ...] = (
    {},
    {"a": 1},
    {"a": 1.0},
    {"a": "1"},
    {"a": True},
    {"a": None},
    {"a": [1]},
    {"a": {"b": 1}},
    {"a": 1, "b": 2},
    {"ab": 12},
    {"a": 12, "b": 1},
    {"a": 1, "b": 21},
    {"a1": "b2"},
    {"a": "1,b:2"},
)


@pytest.mark.parametrize("left, right", list(itertools.combinations(_INJECTIVITY_POOL, 2)), ids=str)
def test_two_different_accepted_payloads_render_differently(
    left: dict[str, Any], right: dict[str, Any]
) -> None:
    """SHA-256 non-collision is not testable and asserting it would be theatre. What
    decides whether two submissions share a key is whether their *material* differs."""
    assert canonical_payload_json(left) != canonical_payload_json(right)
    assert payload_sha256(left) != payload_sha256(right)


# --- 4. no value leaks, in a message or a rendered traceback -------------------------


@given(payload=ANY_PAYLOAD)
def test_every_refusal_is_one_of_the_messages_this_module_can_produce(payload: Any) -> None:
    """Closing the set, not searching for a substring. A substring assertion passes
    vacuously whenever the draw is one common character, which is most draws (M1-607)."""
    try:
        payload_sha256(payload)
    except GatewayError as exc:
        message = str(exc)
        assert any(pattern.match(message) for pattern in _MESSAGE_PATTERNS), message
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        # `from None` everywhere: no cause chain can reprint a value the message withheld.
        assert exc.__cause__ is None
        assert "During handling of the above exception" not in rendered


@given(payload=ANY_PAYLOAD, root=IDENTIFIERS)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=40)
def test_an_artifact_refusal_names_a_path_and_nothing_else(
    tmp_path_factory: pytest.TempPathFactory, payload: Any, root: str
) -> None:
    """Filesystem paths are the settled M1-401 carve-out and *are* rendered; nothing
    else in a write failure may be."""
    directory = tmp_path_factory.mktemp("artifacts") / root
    gateway = DryRunSubmissionGateway(artifact_root=directory, clock=lambda: FIXED)
    request = SubmissionRequest(
        forecast_record_id="rec-1",
        question_id=1,
        idempotency_key="wjsub-1-" + "0" * 64,
        payload=payload,
    )
    try:
        receipt = gateway.submit(request)
    except GatewayError as exc:
        message = str(exc)
        assert (
            any(pattern.match(message) for pattern in _MESSAGE_PATTERNS)
            or str(directory) in message
        ), message
        return
    assert receipt.artifact_path is not None
    assert (directory / receipt.artifact_path).is_file()


# --- 5. the layout has one definition ------------------------------------------------


@given(question_id=st.integers(min_value=1, max_value=2**62))
def test_the_artifact_path_stays_inside_the_root(question_id: int) -> None:
    key = submission_key(
        tournament_id="minibench",
        question_id=question_id,
        forecast_version=1,
        request_payload_sha256="0" * 64,
    )
    relative = dry_run_artifact_path(question_id=question_id, idempotency_key=key)
    resolved = (Path("/root") / relative).resolve()
    assert resolved.is_relative_to(Path("/root"))
    assert relative.startswith("submissions/dry_run/")


@given(key=HOSTILE_TEXT)
def test_a_key_that_is_not_path_safe_never_produces_a_path(key: str) -> None:
    try:
        relative = dry_run_artifact_path(question_id=1, idempotency_key=key)
    except GatewayError:
        return
    assert re.fullmatch(r"submissions/dry_run/1/[A-Za-z0-9][A-Za-z0-9._-]*\.json", relative)


def test_a_clock_reading_pair_is_never_reversed_in_a_receipt() -> None:
    """The pair is what an idempotency key is reasoned about against, and the row it maps
    into is append-only, so a reversed pair would be permanent."""
    readings = [FIXED, FIXED - timedelta(microseconds=1)]
    with pytest.raises(GatewayError):
        DryRunSubmissionGateway(clock=lambda: readings.pop(0)).submit(
            SubmissionRequest(
                forecast_record_id="rec-1",
                question_id=1,
                idempotency_key="wjsub-1-" + "0" * 64,
                payload={"a": 1},
            )
        )
