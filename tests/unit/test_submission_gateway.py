"""M2-703: the submission seam, and the gateway that posts nothing.

Two claims carry the item, and the tests are arranged around them.

*"Makes zero HTTP post calls"* is asserted three ways that fail independently: the module
loads no provider SDK in a fresh interpreter, every ``httpx`` send entry point raises if
touched, and the session-wide socket guard sits under both.

*"Records payload/hash"* is asserted against the **file**, not the return value: the
artifact is read back, its payload re-digested, and the result compared to the hash the
receipt claims. A receipt that agreed with itself would prove nothing.

The third thing under test is the one that is not in the criterion. A dry run must leave
the ledger untouched -- no `submission_attempts` row, no lifecycle event -- because the
idempotency key is UNIQUE and spending it on a rehearsal makes the real post unrecordable.
"""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
import subprocess
import sys
import traceback
from collections.abc import Iterator
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

import pytest

from whiskeyjack_bot.approval import approve
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    SubmissionAttempt,
    current_status,
    record_submission_attempt,
    record_validation,
)
from whiskeyjack_bot.submission import SubmissionError, attempt_for_key, submission_key
from whiskeyjack_bot.submission_gateway import (
    ARTIFACT_SCHEMA_VERSION,
    DryRunSubmissionGateway,
    GatewayError,
    SubmissionGateway,
    SubmissionReceipt,
    SubmissionRequest,
    record_receipt,
    canonical_payload_json,
    dry_run_artifact_path,
    dry_run_attempt_id,
    payload_sha256,
    read_dry_run_artifact,
    write_dry_run_artifact,
)

TS = "2026-08-22T00:00:00.000000+00:00"
FIXED = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
OCCURRED = datetime(2026, 8, 22, 13, 0, tzinfo=timezone.utc)
SHA = "b" * 64

ATTEMPT_ID_RE = re.compile(r"^wjdry-1-[0-9a-f]{64}\Z")

PAYLOAD: dict[str, Any] = {"probability_yes": 0.37, "comment": "none"}


def _key(payload: dict[str, Any] | None = None, *, question_id: int = 100) -> str:
    return submission_key(
        tournament_id="minibench",
        question_id=question_id,
        forecast_version=1,
        request_payload_sha256=payload_sha256(PAYLOAD if payload is None else payload),
    )


def _request(
    payload: dict[str, Any] | None = None,
    *,
    record_id: str = "rec-1",
    question_id: int = 100,
) -> SubmissionRequest:
    body = PAYLOAD if payload is None else payload
    return SubmissionRequest(
        forecast_record_id=record_id,
        question_id=question_id,
        idempotency_key=_key(body, question_id=question_id),
        payload=body,
    )


def _clock(*values: datetime) -> Any:
    """A clock that returns each value in turn, then repeats the last."""
    remaining = list(values)

    def tick() -> datetime:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return tick


def _gateway(root: Path | None = None, *, clock: Any = None) -> DryRunSubmissionGateway:
    return DryRunSubmissionGateway(artifact_root=root, clock=clock or _clock(FIXED))


# --- the seam ------------------------------------------------------------------------


def test_dry_run_gateway_satisfies_the_protocol() -> None:
    gateway: SubmissionGateway = _gateway()
    assert isinstance(gateway.submit(_request()), SubmissionReceipt)


def test_receipt_carries_every_handoff_field() -> None:
    """CODEX_HANDOFF's list, plus `mode` and `artifact_path`, and nothing silently absent."""
    assert set(asdict(_gateway().submit(_request()))) == {
        "mode",
        "attempt_id",
        "forecast_record_id",
        "idempotency_key",
        "requested_at_utc",
        "completed_at_utc",
        "request_payload_sha256",
        "http_status",
        "response_body",
        "response_headers",
        "success",
        "error_type",
        "error_message",
        "verified_by_refetch",
        "refetched_forecast_snapshot",
        "artifact_path",
    }


def test_a_dry_run_claims_no_post_and_invents_no_failure() -> None:
    """`success=True` would be a lie; an `error_type` would be a fabricated cause."""
    receipt = _gateway().submit(_request())
    assert receipt.mode == "dry_run"
    assert receipt.success is False
    assert receipt.verified_by_refetch is False
    assert receipt.http_status is None
    assert receipt.response_body is None
    assert receipt.response_headers is None
    assert receipt.error_type is None
    assert receipt.error_message is None
    assert receipt.refetched_forecast_snapshot is None


def test_the_same_request_and_clock_give_a_byte_identical_receipt() -> None:
    """Determinism is the item's word, so it is asserted on the persisted form."""
    first = _gateway().submit(_request())
    second = _gateway().submit(_request())
    assert first == second
    assert json.dumps(asdict(first), default=str, sort_keys=True) == json.dumps(
        asdict(second), default=str, sort_keys=True
    )


def test_the_attempt_id_is_derived_and_visibly_not_a_submission_key() -> None:
    receipt = _gateway().submit(_request())
    assert ATTEMPT_ID_RE.match(receipt.attempt_id)
    assert receipt.attempt_id == dry_run_attempt_id(receipt.idempotency_key)
    assert receipt.attempt_id != receipt.idempotency_key
    assert not receipt.attempt_id.startswith("wjsub-")
    assert not receipt.idempotency_key.startswith("wjdry-")
    # Inside the ledger's identifier bound -- checked by the writer, not by importing its
    # private constant (M1-303).
    assert len(receipt.attempt_id) <= 200


def test_a_different_payload_gives_a_different_identity() -> None:
    one = _gateway().submit(_request({"probability_yes": 0.37}))
    two = _gateway().submit(_request({"probability_yes": 0.38}))
    assert one.request_payload_sha256 != two.request_payload_sha256
    assert one.idempotency_key != two.idempotency_key
    assert one.attempt_id != two.attempt_id


# --- payload -> hash -----------------------------------------------------------------


def test_the_digest_is_the_digest_of_the_canonical_rendering() -> None:
    import hashlib

    canonical = canonical_payload_json(PAYLOAD)
    assert payload_sha256(PAYLOAD) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert canonical == '{"comment":"none","probability_yes":0.37}'


def test_key_order_does_not_change_the_digest_but_a_value_does() -> None:
    assert payload_sha256({"a": 1, "b": 2}) == payload_sha256({"b": 2, "a": 1})
    assert payload_sha256({"a": 1, "b": 2}) != payload_sha256({"a": 1, "b": 3})


def test_a_lone_surrogate_in_the_payload_hashes_rather_than_raising() -> None:
    """`ensure_ascii=True` escapes it; `str.encode('utf-8')` would not (M1-305 round 2)."""
    canonical = canonical_payload_json({"text": "\ud800"})
    assert canonical.isascii()
    assert json.loads(canonical) == {"text": "\ud800"}
    assert len(payload_sha256({"text": "\ud800"})) == 64


def test_the_receipt_digest_matches_the_payload_it_was_handed() -> None:
    receipt = _gateway().submit(_request())
    assert receipt.request_payload_sha256 == payload_sha256(PAYLOAD)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([1, 2], id="not-an-object"),
        pytest.param("{}", id="json-text-not-an-object"),
        pytest.param({1: "a", "1": "b"}, id="int-key-collapses-two-entries"),
        pytest.param({"a": {2: "b"}}, id="nested-int-key"),
        pytest.param({"a": (1, 2)}, id="tuple-does-not-read-back-as-itself"),
        pytest.param({"a": {1, 2}}, id="set"),
        pytest.param({"a": float("nan")}, id="nan"),
        pytest.param({"a": float("inf")}, id="inf"),
        pytest.param({"a": datetime(2026, 1, 1, tzinfo=timezone.utc)}, id="datetime"),
        pytest.param({"a": b"bytes"}, id="bytes"),
    ],
)
def test_payloads_outside_the_accepted_domain_are_refused(payload: Any) -> None:
    with pytest.raises(GatewayError):
        payload_sha256(payload)


def test_json_would_have_collapsed_an_integer_key_into_a_string_one() -> None:
    """The reason the key rule exists, stated as the behaviour it prevents."""
    assert json.dumps({1: "a", "1": "b"}) in ('{"1": "a", "1": "b"}', '{"1": "b"}')
    with pytest.raises(GatewayError) as excinfo:
        canonical_payload_json({1: "a", "1": "b"})
    assert "coerces" in str(excinfo.value)


def test_a_self_referential_payload_is_refused_by_the_depth_cap() -> None:
    payload: dict[str, Any] = {}
    payload["self"] = payload
    with pytest.raises(GatewayError) as excinfo:
        payload_sha256(payload)
    assert "nests deeper" in str(excinfo.value)


def test_a_shared_subobject_is_not_mistaken_for_a_cycle() -> None:
    """A visited-set would have refused this; the depth cap does not."""
    shared = {"n": 1}
    assert len(payload_sha256({"a": shared, "b": shared})) == 64


def test_a_mapping_whose_items_raise_arrives_as_a_gateway_error() -> None:
    class Hostile(dict[str, Any]):
        def items(self) -> Any:
            raise RuntimeError("secret-payload-value")

    with pytest.raises(GatewayError) as excinfo:
        payload_sha256(Hostile())
    assert "secret-payload-value" not in str(excinfo.value)
    assert "secret-payload-value" not in "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )


# --- zero HTTP -----------------------------------------------------------------------

_PROVIDER_MODULES = ("forecasting_tools", "asknews_sdk", "httpx")

_PROBE = """
import importlib, sys
importlib.import_module({module!r})
print("LOADED:" + ",".join(sorted(m for m in sys.modules if m in {providers!r})))
"""


def test_the_gateway_module_loads_no_http_client_at_all() -> None:
    """Must run in a fresh interpreter: inside pytest the adapter suites have already
    imported httpx, so an in-process assertion would pass or fail for the wrong reason."""
    probe = _PROBE.format(module="whiskeyjack_bot.submission_gateway", providers=_PROVIDER_MODULES)
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    marked = [line for line in result.stdout.splitlines() if line.startswith("LOADED:")]
    assert len(marked) == 1, result.stdout
    assert marked[0] == "LOADED:"


def test_a_dry_run_touches_no_httpx_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the dry-run gateway made an HTTP call")

    monkeypatch.setattr(httpx.Client, "send", explode)
    monkeypatch.setattr(httpx.AsyncClient, "send", explode)
    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx.AsyncClient, "request", explode)
    assert _gateway(tmp_path).submit(_request()).success is False


# --- the ledger is untouched ---------------------------------------------------------


def _seed_draft(
    conn: sqlite3.Connection, record_id: str = "rec-1", *, question_id: int = 100
) -> str:
    """Insert a draft directly: M1-602's record writer does not exist yet.

    `001` declares UNIQUE (question_id, tournament_id, forecast_version), so a second
    record needs its own question rather than a second version of the same one.
    """
    conn.execute(
        "INSERT OR IGNORE INTO research_runs (retrieval_run_id, provider, question_id, "
        "started_at_utc, created_at_utc) VALUES ('run-1', 'asknews', 100, ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO forecast_records ("
        "record_id, question_id, tournament_id, forecast_version, question_type, status, "
        "model_provider, model_name, prompt_version, prompt_sha256, retrieval_run_id, "
        "generated_at_utc, final_prediction_json, record_json, created_at_utc, "
        "forecast_sha256, attempt_id) "
        "VALUES (?, ?, 'minibench', 1, 'binary', 'draft', 'anthropic', 'claude', 'v1', "
        "'abc', 'run-1', ?, '{}', '{}', ?, ?, ?)",
        (record_id, question_id, TS, TS, SHA, f"att-{record_id}"),
    )
    return record_id


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        _seed_draft(conn)
        yield conn
    finally:
        conn.close()


def test_a_dry_run_of_a_draft_records_nothing_and_spends_no_key(
    ledger: sqlite3.Connection, tmp_path: Path
) -> None:
    """The acceptance criterion's real content: a rehearsal must not make the live post
    unrecordable, and `draft` is where a record sits when a dry run is most useful."""
    receipt = _gateway(tmp_path / "artifacts").submit(_request())
    assert current_status(ledger, "rec-1") == "draft"
    assert attempt_for_key(ledger, receipt.idempotency_key) is None
    assert ledger.execute("SELECT count(*) FROM submission_attempts").fetchone()[0] == 0
    assert ledger.execute("SELECT count(*) FROM lifecycle_events").fetchone()[0] == 0
    # And the key is still free for the post that follows.
    assert receipt.artifact_path is not None


# --- the guard on the way into the ledger --------------------------------------------


def test_a_dry_run_receipt_cannot_become_a_submission_attempt(
    ledger: sqlite3.Connection,
) -> None:
    receipt = _gateway().submit(_request())
    with pytest.raises(GatewayError) as excinfo:
        record_receipt(ledger, receipt=receipt, occurred_at=OCCURRED)
    assert "dry_run" in str(excinfo.value)
    assert ledger.execute("SELECT count(*) FROM submission_attempts").fetchone()[0] == 0


def test_the_refusal_is_what_stops_a_rehearsal_killing_the_record(
    ledger: sqlite3.Connection,
) -> None:
    """Without it, a dry-run receipt's (False, False) is `submission_failed` -> terminal
    `failed`: a rehearsal would permanently kill the forecast version it rehearsed."""
    record_validation(ledger, record_id="rec-1", occurred_at=OCCURRED)
    approve(ledger, record_id="rec-1", actor="owner", occurred_at=OCCURRED)
    receipt = _gateway().submit(_request())
    with pytest.raises(GatewayError):
        record_receipt(ledger, receipt=receipt, occurred_at=OCCURRED)
    # Hand the writer what the refusal withheld, and watch the record die.
    record_submission_attempt(
        ledger,
        record_id="rec-1",
        attempt=SubmissionAttempt(
            attempt_id=receipt.attempt_id,
            idempotency_key=receipt.idempotency_key,
            requested_at_utc=receipt.requested_at_utc,
            completed_at_utc=receipt.completed_at_utc,
            request_payload_sha256=receipt.request_payload_sha256,
            success=False,
            verified_by_refetch=False,
        ),
        occurred_at=OCCURRED,
        detail_code="internal_error",
    )
    assert current_status(ledger, "rec-1") == "failed"


def _live(record_id: str = "rec-1", attempt_id: str = "att-live-1") -> SubmissionReceipt:
    return replace(
        _gateway().submit(_request(record_id=record_id)),
        mode="live",
        attempt_id=attempt_id,
        forecast_record_id=record_id,
        success=True,
        verified_by_refetch=True,
        http_status=201,
        response_body='{"ok": true}',
    )


def test_a_live_receipt_is_recorded_against_the_record_it_names(
    ledger: sqlite3.Connection,
) -> None:
    """The bound on every identifier is checked by the writer, not by importing its
    private constant -- which would test the constant, not the writer (M1-303)."""
    record_validation(ledger, record_id="rec-1", occurred_at=OCCURRED)
    approve(ledger, record_id="rec-1", actor="owner", occurred_at=OCCURRED)
    receipt = _live()
    event = record_receipt(ledger, receipt=receipt, occurred_at=OCCURRED)
    assert event.event_type == "submitted"
    assert event.forecast_record_id == "rec-1"
    assert current_status(ledger, "rec-1") == "submitted"
    stored = attempt_for_key(ledger, receipt.idempotency_key)
    assert stored is not None
    assert stored.forecast_record_id == "rec-1"
    assert stored.request_payload_sha256 == receipt.request_payload_sha256


def test_a_receipt_cannot_be_recorded_against_a_different_record(
    ledger: sqlite3.Connection,
) -> None:
    """Round 1's blocking finding, as a regression test.

    Before the fix, `attempt_from_receipt()` handed back a `SubmissionAttempt` -- which
    carries no `forecast_record_id`, and never has -- leaving the caller to re-supply the
    record to `record_submission_attempt()`. A receipt naming `rec-1` recorded against
    `rec-2` succeeded: the attempt row held `rec-2`, `rec-2` advanced to `submitted` on an
    approval that authorized nothing, and `rec-1` stayed `approved`. Append-only, so
    permanently.

    There is no `record_id` parameter to get wrong any more, so the test asserts the
    surface rather than a rejection: the receipt is the only thing that names the record.
    """
    _seed_draft(ledger, "rec-2", question_id=101)
    for record_id in ("rec-1", "rec-2"):
        record_validation(ledger, record_id=record_id, occurred_at=OCCURRED)
        approve(ledger, record_id=record_id, actor="owner", occurred_at=OCCURRED)

    record_receipt(ledger, receipt=_live("rec-1"), occurred_at=OCCURRED)

    assert current_status(ledger, "rec-1") == "submitted"
    assert current_status(ledger, "rec-2") == "approved"
    rows = ledger.execute(
        "SELECT forecast_record_id FROM submission_attempts ORDER BY attempt_id"
    ).fetchall()
    assert [row[0] for row in rows] == ["rec-1"]
    # `record_receipt` takes no record_id: there is no second source of truth to diverge.
    assert "record_id" not in inspect.signature(record_receipt).parameters


def test_the_recorded_row_drops_what_submission_attempts_has_no_column_for(
    ledger: sqlite3.Connection,
) -> None:
    record_validation(ledger, record_id="rec-1", occurred_at=OCCURRED)
    approve(ledger, record_id="rec-1", actor="owner", occurred_at=OCCURRED)
    receipt = replace(_live(), artifact_path="submissions/dry_run/x.json")
    record_receipt(ledger, receipt=receipt, occurred_at=OCCURRED)
    columns = {
        description[0]
        for description in ledger.execute("SELECT * FROM submission_attempts LIMIT 1").description
    }
    assert "mode" not in columns
    assert "artifact_path" not in columns
    stored = attempt_for_key(ledger, receipt.idempotency_key)
    assert stored is not None
    assert stored.attempt_id == "att-live-1"


@pytest.mark.parametrize(
    "receipt",
    [
        pytest.param("not a receipt", id="wrong-type"),
        pytest.param(None, id="none"),
    ],
)
def test_the_writer_refuses_a_foreign_object(ledger: sqlite3.Connection, receipt: Any) -> None:
    with pytest.raises(GatewayError):
        record_receipt(ledger, receipt=receipt, occurred_at=OCCURRED)


def test_the_writer_refuses_an_unrecognized_mode(ledger: sqlite3.Connection) -> None:
    receipt = replace(_gateway().submit(_request()), mode="rehearsal")  # type: ignore[arg-type]
    with pytest.raises(GatewayError) as excinfo:
        record_receipt(ledger, receipt=receipt, occurred_at=OCCURRED)
    assert "rehearsal" not in str(excinfo.value)


def test_a_ledger_refusal_arrives_as_this_modules_error(ledger: sqlite3.Connection) -> None:
    """`record_submission_attempt` raises `LifecycleError`; a caller of this seam handles
    `SubmissionError`. The message is preserved -- `LifecycleError`'s own contract says it
    names no stored or caller-supplied value, and it is what makes a refusal actionable."""
    receipt = _live()  # rec-1 is still `draft`: `submitted` is not legal from there
    with pytest.raises(GatewayError) as excinfo:
        record_receipt(ledger, receipt=receipt, occurred_at=OCCURRED)
    assert str(excinfo.value)
    assert excinfo.value.__cause__ is None


# --- the artifact --------------------------------------------------------------------


def test_the_artifact_layout_has_one_definition() -> None:
    key = _key()
    assert dry_run_artifact_path(question_id=100, idempotency_key=key) == (
        f"submissions/dry_run/100/{key}.json"
    )
    assert _gateway(Path("/nonexistent")) is not None  # constructing writes nothing


def test_the_artifact_records_the_payload_and_a_re_derivable_hash(tmp_path: Path) -> None:
    """Read back from the *file*: a receipt agreeing with itself proves nothing."""
    receipt = _gateway(tmp_path).submit(_request())
    assert receipt.artifact_path is not None
    envelope = read_dry_run_artifact(tmp_path, receipt.artifact_path)
    assert envelope["artifact_schema_version"] == ARTIFACT_SCHEMA_VERSION
    assert envelope["mode"] == "dry_run"
    assert envelope["question_id"] == 100
    assert envelope["request_payload"] == PAYLOAD
    assert payload_sha256(envelope["request_payload"]) == receipt.request_payload_sha256  # type: ignore[arg-type]
    stored = envelope["receipt"]
    assert isinstance(stored, dict)
    assert stored["attempt_id"] == receipt.attempt_id
    assert stored["success"] is False
    assert stored["requested_at_utc"] == "2026-08-22T12:00:00.000000+00:00"
    # The file's own location is the artifact path; writing it inside would be a
    # self-reference a moved artifact silently invalidates.
    assert "artifact_path" not in stored


def test_rerunning_the_same_dry_run_is_a_no_op(tmp_path: Path) -> None:
    first = _gateway(tmp_path).submit(_request())
    assert first.artifact_path is not None
    written = (tmp_path / first.artifact_path).read_bytes()
    second = _gateway(tmp_path).submit(_request())
    assert second == first
    assert (tmp_path / first.artifact_path).read_bytes() == written
    # And no temp file was left behind.
    assert sorted(p.name for p in (tmp_path / first.artifact_path).parent.iterdir()) == [
        Path(first.artifact_path).name
    ]


def test_a_different_body_at_the_same_path_is_never_overwritten(tmp_path: Path) -> None:
    receipt = _gateway(tmp_path).submit(_request())
    assert receipt.artifact_path is not None
    destination = tmp_path / receipt.artifact_path
    destination.write_text('{"tampered": "secret-payload-value"}', encoding="utf-8")
    with pytest.raises(GatewayError) as excinfo:
        _gateway(tmp_path).submit(_request())
    message = str(excinfo.value)
    assert str(destination) in message  # paths are rendered (M1-401 carve-out)
    assert "secret-payload-value" not in message
    assert destination.read_text(encoding="utf-8") == '{"tampered": "secret-payload-value"}'


def test_a_clock_change_alone_makes_the_artifact_disagree(tmp_path: Path) -> None:
    """The receipt is part of the envelope, so a second dry run at a different instant is
    a different file body at the same content-derived path -- and is refused rather than
    silently replacing the first record of the rehearsal."""
    _gateway(tmp_path).submit(_request())
    later = _gateway(tmp_path, clock=_clock(FIXED + timedelta(seconds=1)))
    with pytest.raises(GatewayError):
        later.submit(_request())


def test_an_unwritable_artifact_root_is_refused_with_the_path(tmp_path: Path) -> None:
    blocker = tmp_path / "artifacts"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(GatewayError) as excinfo:
        _gateway(blocker).submit(_request())
    assert str(blocker) in str(excinfo.value)


def test_without_an_artifact_root_the_gateway_touches_the_filesystem_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import tempfile as tempfile_module

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the pure gateway touched the filesystem")

    monkeypatch.setattr(tempfile_module, "mkstemp", explode)
    monkeypatch.setattr(os, "link", explode)
    monkeypatch.setattr(Path, "mkdir", explode)
    receipt = _gateway().submit(_request())
    assert receipt.artifact_path is None


def test_the_writer_refuses_a_receipt_whose_digest_is_not_this_payloads(tmp_path: Path) -> None:
    receipt = replace(_gateway().submit(_request()), request_payload_sha256="a" * 64)
    with pytest.raises(GatewayError) as excinfo:
        write_dry_run_artifact(tmp_path, receipt=receipt, question_id=100, payload=PAYLOAD)
    assert "does not match" in str(excinfo.value)


def test_the_writer_refuses_a_live_receipt(tmp_path: Path) -> None:
    receipt = replace(_gateway().submit(_request()), mode="live")
    with pytest.raises(GatewayError):
        write_dry_run_artifact(tmp_path, receipt=receipt, question_id=100, payload=PAYLOAD)


def test_a_key_that_would_escape_the_artifact_root_is_refused_before_any_io(
    tmp_path: Path,
) -> None:
    request = SubmissionRequest(
        forecast_record_id="rec-1",
        question_id=100,
        idempotency_key="../../etc/passwd",
        payload=PAYLOAD,
    )
    with pytest.raises(GatewayError) as excinfo:
        _gateway(tmp_path).submit(request)
    assert "path component" in str(excinfo.value)
    assert list(tmp_path.iterdir()) == []


# --- reading an artifact back --------------------------------------------------------


def test_the_reader_admits_exactly_what_the_writer_emits(tmp_path: Path) -> None:
    receipt = _gateway(tmp_path).submit(_request())
    assert receipt.artifact_path is not None
    destination = tmp_path / receipt.artifact_path

    destination.write_text('{"artifact_schema_version": "1.0.0", "a": NaN}', encoding="utf-8")
    with pytest.raises(GatewayError) as nan:
        read_dry_run_artifact(tmp_path, receipt.artifact_path)
    assert "non-finite" in str(nan.value)

    destination.write_text('{"artifact_schema_version": "9.9.9"}', encoding="utf-8")
    with pytest.raises(GatewayError) as version:
        read_dry_run_artifact(tmp_path, receipt.artifact_path)
    assert ARTIFACT_SCHEMA_VERSION in str(version.value)

    destination.write_text("[]", encoding="utf-8")
    with pytest.raises(GatewayError):
        read_dry_run_artifact(tmp_path, receipt.artifact_path)

    destination.write_text("{", encoding="utf-8")
    with pytest.raises(GatewayError):
        read_dry_run_artifact(tmp_path, receipt.artifact_path)


def test_the_reader_refuses_a_missing_artifact_naming_the_path(tmp_path: Path) -> None:
    with pytest.raises(GatewayError) as excinfo:
        read_dry_run_artifact(tmp_path, "submissions/dry_run/1/absent.json")
    assert "absent.json" in str(excinfo.value)


def test_the_reader_refuses_undecodable_bytes(tmp_path: Path) -> None:
    relative = "submissions/dry_run/1/broken.json"
    destination = tmp_path / relative
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(GatewayError):
        read_dry_run_artifact(tmp_path, relative)


# --- the accepted domain of the request ----------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("forecast_record_id", ""),
        ("forecast_record_id", "  "),
        ("forecast_record_id", "\n\t"),
        ("forecast_record_id", "a" * 201),
        ("forecast_record_id", "rec\x001"),
        ("forecast_record_id", "rec-\ud800"),
        ("forecast_record_id", 1),
        ("forecast_record_id", None),
        ("question_id", 0),
        ("question_id", -1),
        ("question_id", True),
        ("question_id", 2**63),
        ("question_id", "100"),
        ("question_id", 1.0),
        ("idempotency_key", ""),
        ("idempotency_key", "has/slash"),
        ("idempotency_key", ".hidden"),
        ("idempotency_key", "a" * 129),
    ],
)
def test_a_malformed_request_field_is_refused_as_this_modules_error(field: str, value: Any) -> None:
    request = replace(_request(), **{field: value})
    with pytest.raises(GatewayError):
        _gateway().submit(request)


def test_a_request_subclass_is_refused_rather_than_read() -> None:
    """A subclass can shadow a field with a property, so each read becomes caller code."""

    class Sneaky(SubmissionRequest):
        @property  # type: ignore[misc]
        def payload(self) -> dict[str, Any]:
            raise RuntimeError("secret-payload-value")

    # Built without the generated __init__, which the property would refuse to run: the
    # point is that an instance reaching submit() must be turned away on its type.
    sneaky = Sneaky.__new__(Sneaky)
    object.__setattr__(sneaky, "forecast_record_id", "rec-1")
    object.__setattr__(sneaky, "question_id", 100)
    object.__setattr__(sneaky, "idempotency_key", _key())
    with pytest.raises(GatewayError) as excinfo:
        _gateway().submit(sneaky)
    assert "secret-payload-value" not in str(excinfo.value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(datetime(2026, 8, 22, 12, 0), id="naive"),
        pytest.param("2026-08-22T12:00:00+00:00", id="text"),
        pytest.param(None, id="none"),
    ],
)
def test_a_clock_that_does_not_return_an_aware_datetime_is_refused(value: Any) -> None:
    with pytest.raises(GatewayError):
        DryRunSubmissionGateway(clock=lambda: value).submit(_request())


def test_a_clock_with_a_hostile_timezone_arrives_as_a_gateway_error() -> None:
    class Hostile(tzinfo):
        def utcoffset(self, dt: Any) -> Any:
            raise RuntimeError("secret-clock-value")

    moment = datetime(2026, 8, 22, 12, 0, tzinfo=Hostile())
    with pytest.raises(GatewayError) as excinfo:
        DryRunSubmissionGateway(clock=lambda: moment).submit(_request())
    assert "secret-clock-value" not in str(excinfo.value)


def test_a_clock_that_ran_backwards_is_refused() -> None:
    backwards = _clock(FIXED, FIXED - timedelta(seconds=1))
    with pytest.raises(GatewayError) as excinfo:
        DryRunSubmissionGateway(clock=backwards).submit(_request())
    assert "earlier" in str(excinfo.value)


def test_the_two_readings_bracket_the_request() -> None:
    later = FIXED + timedelta(milliseconds=5)
    receipt = DryRunSubmissionGateway(clock=_clock(FIXED, later)).submit(_request())
    assert receipt.requested_at_utc == FIXED
    assert receipt.completed_at_utc == later


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"artifact_root": "not-a-path"}, id="artifact-root-str"),
        pytest.param({"clock": "not-callable"}, id="clock-not-callable"),
    ],
)
def test_a_malformed_gateway_construction_is_refused(kwargs: Any) -> None:
    with pytest.raises(GatewayError):
        DryRunSubmissionGateway(**kwargs)


def test_every_refusal_is_catchable_as_the_seams_error_type() -> None:
    """`GatewayError` subclasses `SubmissionError`, so a caller handling the submission
    seam handles the gateway too -- and can still tell the two apart."""
    assert issubclass(GatewayError, SubmissionError)
    with pytest.raises(SubmissionError):
        payload_sha256([1])  # type: ignore[arg-type]


# --- the replay guard ----------------------------------------------------------------

# An astral scalar and its UTF-16 surrogate-pair spelling, built from code points so no
# editor or shell can normalize the distinction away. `ensure_ascii=True` escapes both to
# the same two \uXXXX units and `json.loads` recombines them, so they are one string on
# the way back -- which is why the tiebreak in M1-305 keys on the persisted form, and why
# two of them used as *keys* is a payload that cannot be replayed.
SURROGATE_PAIR = chr(0xD83D) + chr(0xDE00)
ASTRAL = chr(0x1F600)


def test_two_keys_that_persist_as_one_are_refused() -> None:
    assert SURROGATE_PAIR != ASTRAL
    with pytest.raises(GatewayError) as excinfo:
        payload_sha256({SURROGATE_PAIR: 1, ASTRAL: 2})
    assert "does not survive its own canonical rendering" in str(excinfo.value)
    assert SURROGATE_PAIR not in str(excinfo.value)


def test_the_same_two_spellings_as_values_are_one_value_and_that_is_correct() -> None:
    """They persist as one scalar, so a replay reproduces one -- the digests must agree."""
    assert payload_sha256({"a": SURROGATE_PAIR}) == payload_sha256({"a": ASTRAL})


def test_a_single_such_key_is_accepted_because_it_round_trips() -> None:
    """The guard is a round-trip test, not a character blocklist: one of them alone is
    fine, and refusing it would reject a payload nothing is wrong with."""
    assert len(payload_sha256({SURROGATE_PAIR: 1})) == 64
    assert len(payload_sha256({chr(0xD800): 1})) == 64


def test_the_reader_refuses_an_envelope_the_writer_could_not_have_emitted(
    tmp_path: Path,
) -> None:
    """Shapes, not just the version: a reader that admits more than its writer produces is
    not reading the format it documents (`research/artifacts.py` round 1, finding 7)."""
    receipt = _gateway(tmp_path).submit(_request())
    assert receipt.artifact_path is not None
    destination = tmp_path / receipt.artifact_path
    intact = json.loads(destination.read_text(encoding="utf-8"))

    for key, value in (
        ("mode", "live"),
        ("mode", "rehearsal"),
        ("question_id", "100"),
        ("request_payload", []),
        ("receipt", "gone"),
    ):
        broken = dict(intact)
        broken[key] = value
        destination.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(GatewayError):
            read_dry_run_artifact(tmp_path, receipt.artifact_path)

    for key in ("mode", "question_id", "request_payload", "receipt"):
        broken = {k: v for k, v in intact.items() if k != key}
        destination.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(GatewayError):
            read_dry_run_artifact(tmp_path, receipt.artifact_path)

    destination.write_text(json.dumps(intact), encoding="utf-8")
    assert read_dry_run_artifact(tmp_path, receipt.artifact_path) == intact
