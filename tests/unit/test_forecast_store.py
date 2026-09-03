"""Appending forecast versions to the ledger (M1-602).

The acceptance criterion -- "updating a question appends v2; v1 remains byte-identical" --
is asserted on the raw bytes of the stored column, not on a re-parsed value: a reader that
round-trips is not evidence that the stored bytes never moved.

Migration ``007``'s clauses are exercised by **direct SQL**. A test that reached them
through :func:`append_forecast_version` would be testing the writer's refusals a second
time; the schema exists precisely for the writer that does not exist yet.
"""

from __future__ import annotations

import itertools
import json
import re
import sqlite3
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from whiskeyjack_bot.approval import read_forecast_summary
from whiskeyjack_bot.forecast.parse import ForecastGeneration, ModelSettings
from whiskeyjack_bot.forecast.inputs import SourceReference
from whiskeyjack_bot.forecast.record import (
    ForecastRecordDraft,
    ForecastRecordError,
    assign_identity,
    build_forecast_record_draft,
    canonical_final_prediction_json,
    canonical_record_json,
    record_sha256,
)
from whiskeyjack_bot.forecast.schema import (
    ForecastResponse,
    response_model_for,
    validate_forecast_response,
)
from whiskeyjack_bot.forecast import store as store_module
from whiskeyjack_bot.forecast.store import (
    append_forecast_version,
    latest_forecast_version,
    mint_record_id,
    read_forecast_record,
)
from whiskeyjack_bot.forecast.validate import output_problems
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    current_status,
    record_approval,
    record_validation,
    transaction,
)
from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalNumericQuestion,
    CanonicalQuestion,
)

from tests.unit.records import FORECAST_CONFIG

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")

SECRET = "privateFAKE123456"

QUESTION_ID = 123
POST_ID = 456
TOURNAMENT = "minibench"
RUN_ID = "run-1"
# A second run, so a `retrieval_run_id` mismatch can be a *different* value that still
# satisfies the foreign key. Without it that parametrized case reads back cleanly and
# proves nothing -- which is what `test_a_coherent_raw_row_reads_back` caught.
RUN_ID_OTHER = "run-2"
GENERATED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
TIMESTAMP = "2026-08-22T00:00:00.000000+00:00"


def _json_block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _response(**overrides: Any) -> ForecastResponse:
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block("Binary schema") + "}"),
    }
    payload["question_id"] = QUESTION_ID
    payload.update(overrides)
    return validate_forecast_response(payload, response_model_for(payload["question_type"]))


def _numeric_response(**overrides: Any) -> ForecastResponse:
    """The prompt's own numeric example, validated by the real validator (M1-507)."""
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block("Numeric schema") + "}"),
    }
    payload["question_id"] = QUESTION_ID
    # The shared block's priors are binary-only; numeric refuses them.
    payload["model_prior"] = None
    payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    payload.update(overrides)
    return validate_forecast_response(payload, response_model_for(payload["question_type"]))


def _disordered_percentiles() -> list[dict[str, float]]:
    """The prompt's own nine levels, with the last two values swapped.

    Schema-valid -- ``schema.NumericPrediction`` does not check ordering -- and refused by
    ``numeric.numeric_output_problems``'s non-decreasing rule, which is the point: a response
    ``forecast.generate`` would refuse and a direct caller of the writer could otherwise
    persist.
    """
    percentiles: list[dict[str, float]] = json.loads("{" + _json_block("Numeric schema") + "}")[
        "final_prediction"
    ]["percentiles"]
    percentiles[-1]["value"], percentiles[-2]["value"] = (
        percentiles[-2]["value"],
        percentiles[-1]["value"],
    )
    return percentiles


def _question(**overrides: Any) -> CanonicalQuestion:
    fields: dict[str, Any] = {
        "question_id": QUESTION_ID,
        "post_id": POST_ID,
        "title": "Will the thing happen?",
    }
    fields.update(overrides)
    return CanonicalBinaryQuestion(**fields)


def _numeric_question(**overrides: Any) -> CanonicalNumericQuestion:
    fields: dict[str, Any] = {
        "question_id": QUESTION_ID,
        "post_id": POST_ID,
        "title": "How many things?",
        "resolution_criteria": "Resolves to the number of things.",
        "lower_bound": 0.0,
        "upper_bound": 100.0,
        "open_lower_bound": False,
        "open_upper_bound": False,
        "cdf_size": 201,
    }
    fields.update(overrides)
    return CanonicalNumericQuestion(**fields)


def _sources(*source_ids: str) -> tuple[SourceReference, ...]:
    return tuple(
        SourceReference(
            source_id=source_id,
            document_id=None,
            canonical_url=f"https://example.test/{source_id}",
            content_sha256="c" * 64,
        )
        for source_id in (source_ids or ("src-001", "src-002"))
    )


def _generation(**overrides: Any) -> ForecastGeneration:
    fields: dict[str, Any] = {
        "forecast": _response(),
        "settings": ModelSettings(
            provider="openrouter",
            name="openrouter/test-model",
            temperature=0.1,
            max_output_tokens=2048,
            timeout_seconds=60.0,
            allowed_tries=2,
            prompt_version="1.1.0",
            prompt_sha256="b" * 64,
        ),
        "sources": _sources(),
        "request": "the rendered reasoning packet",
        "raw_responses": ("{}",),
        "invocations": 1,
        "repair_attempted": False,
        "cost_usd": None,
        "failure_code": None,
        "failure_problems": (),
    }
    fields.update(overrides)
    return ForecastGeneration(**fields)


def _draft(attempt_id: str = "attempt-1", **overrides: Any) -> ForecastRecordDraft:
    fields: dict[str, Any] = {
        "question": _question(),
        "generation": _generation(),
        "tournament_id": TOURNAMENT,
        "attempt_id": attempt_id,
        "retrieval_run_id": RUN_ID,
        "research_packet_sha256": "d" * 64,
        "generated_at": GENERATED_AT,
    }
    fields.update(overrides)
    return build_forecast_record_draft(**fields)


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    database = tmp_path / "ledger.sqlite3"
    initialize_ledger(database)
    connection = connect(database)
    with transaction(connection):
        for run_id in (RUN_ID, RUN_ID_OTHER):
            connection.execute(
                "INSERT INTO research_runs "
                "(retrieval_run_id, provider, started_at_utc, created_at_utc, question_id) "
                "VALUES (?, 'exa', ?, ?, ?)",
                (run_id, TIMESTAMP, TIMESTAMP, QUESTION_ID),
            )
    return connection


def _raw(connection: sqlite3.Connection, record_id: str) -> tuple[Any, ...]:
    """The stored bytes, as bytes.

    ``CAST(... AS BLOB)`` rather than the TEXT the driver decodes: the criterion is about
    the bytes on disk, and a comparison of two decoded strings cannot see a re-encoding.
    """
    row = connection.execute(
        "SELECT CAST(record_json AS BLOB), CAST(final_prediction_json AS BLOB), "
        "forecast_sha256, forecast_version, parent_record_id, status, created_at_utc "
        "FROM forecast_records WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def _leaks(exc: BaseException) -> bool:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return SECRET in str(exc) or SECRET in rendered


# --------------------------------------------------------------------------------------
# The acceptance criterion
# --------------------------------------------------------------------------------------


def test_updating_a_question_appends_v2_and_v1_remains_byte_identical(
    conn: sqlite3.Connection,
) -> None:
    v1 = append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft("attempt-1"))
    before = _raw(conn, v1.record_id)

    v2 = append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft("attempt-2"))

    assert (v1.forecast_version, v1.parent_record_id) == (1, None)
    assert (v2.forecast_version, v2.parent_record_id) == (2, v1.record_id)
    assert v2.record_id != v1.record_id
    assert _raw(conn, v1.record_id) == before
    assert conn.execute("SELECT COUNT(*) FROM forecast_records").fetchone()[0] == 2


def test_a_long_chain_is_contiguous_and_singly_linked(conn: sqlite3.Connection) -> None:
    records = [
        append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft(f"attempt-{i}"))
        for i in range(1, 6)
    ]
    assert [record.forecast_version for record in records] == [1, 2, 3, 4, 5]
    assert [record.parent_record_id for record in records] == [None] + [
        record.record_id for record in records[:-1]
    ]


def test_every_earlier_version_is_untouched_by_every_later_one(conn: sqlite3.Connection) -> None:
    """The criterion generalized past v1: *no* stored version moves, ever.

    Snapshotting only v1 would pass on a writer that rewrote v2 when v3 landed.
    """
    stored: dict[str, tuple[Any, ...]] = {}
    for index in range(1, 5):
        record = append_forecast_version(
            conn, forecast_config=FORECAST_CONFIG, draft=_draft(f"attempt-{index}")
        )
        for record_id, snapshot in stored.items():
            assert _raw(conn, record_id) == snapshot
        stored[record.record_id] = _raw(conn, record.record_id)


def test_each_question_and_tournament_has_its_own_chain(conn: sqlite3.Connection) -> None:
    first = append_forecast_version(
        conn, forecast_config=FORECAST_CONFIG, draft=_draft("attempt-1")
    )
    other_question = append_forecast_version(
        conn,
        forecast_config=FORECAST_CONFIG,
        draft=_draft(
            "attempt-2",
            question=_question(question_id=999),
            generation=_generation(forecast=_response(question_id=999)),
        ),
    )
    other_tournament = append_forecast_version(
        conn, forecast_config=FORECAST_CONFIG, draft=_draft("attempt-3", tournament_id="other-cup")
    )
    assert first.forecast_version == 1
    assert (other_question.forecast_version, other_question.parent_record_id) == (1, None)
    assert (other_tournament.forecast_version, other_tournament.parent_record_id) == (1, None)


def test_a_record_is_born_a_draft_and_moves_only_through_lifecycle_events(
    conn: sqlite3.Connection,
) -> None:
    record = append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft())
    assert _raw(conn, record.record_id)[5] == "draft"
    assert current_status(conn, record.record_id) == "draft"
    record_validation(conn, record_id=record.record_id, occurred_at=GENERATED_AT)
    assert current_status(conn, record.record_id) == "validated"


def test_the_stored_hash_is_what_an_approval_binds_to(conn: sqlite3.Connection) -> None:
    """The writer's hash has to satisfy ``003``'s trigger, not merely look like a hash.

    ``approval_events_bind_forecast_hash_on_insert`` compares the decision's hash against
    the stored one and aborts on a mismatch, so an approval that lands is proof the two
    agree. This is the one test that ties M1-602's hash to M1-603's binding.
    """
    record = append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft())
    record_validation(conn, record_id=record.record_id, occurred_at=GENERATED_AT)
    summary = read_forecast_summary(conn, record.record_id)
    assert summary.forecast_sha256 == record_sha256(record)
    record_approval(
        conn,
        record_id=record.record_id,
        decision="approved",
        actor="owner",
        forecast_sha256=record_sha256(record),
        # M2-707: `011` requires it. Shape only -- this test is about the *forecast* hash
        # being what an approval binds to, and `test_submission_payload.py` is where the
        # derived digest is checked against a real payload.
        payload_sha256="d" * 64,
        occurred_at=GENERATED_AT,
    )
    assert current_status(conn, record.record_id) == "approved"


def test_the_writer_composes_inside_a_callers_transaction(conn: sqlite3.Connection) -> None:
    """One unit of work: the record and its first lifecycle event, or neither.

    ``lifecycle.transaction`` nests as a SAVEPOINT for exactly this, and M1-603's notes
    name this item as the reason. The rollback half is what makes it worth asserting --
    a nested writer that quietly committed would leave the record behind.
    """
    with pytest.raises(RuntimeError):
        with transaction(conn):
            record = append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft())
            record_validation(conn, record_id=record.record_id, occurred_at=GENERATED_AT)
            raise RuntimeError("the caller's own failure, after both writes")
    assert conn.execute("SELECT COUNT(*) FROM forecast_records").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 0


# --------------------------------------------------------------------------------------
# Refusals, and that a refusal writes nothing
# --------------------------------------------------------------------------------------


def test_a_response_citing_a_source_it_was_never_given_is_never_stored(
    conn: sqlite3.Connection,
) -> None:
    """M1-501's gate, run one moment before the row becomes uncorrectable."""
    with pytest.raises(ForecastRecordError) as excinfo:
        append_forecast_version(
            conn,
            forecast_config=FORECAST_CONFIG,
            draft=_draft(generation=_generation(sources=_sources("src-009"))),
        )
    assert conn.execute("SELECT COUNT(*) FROM forecast_records").fetchone()[0] == 0
    assert not conn.in_transaction
    # The sanitized problem list is preserved: it is the whole account of the refusal.
    assert "source_id" in str(excinfo.value)


def test_the_writer_refuses_what_generate_refuses_binary_bounds(
    conn: sqlite3.Connection,
) -> None:
    """M1-507's acceptance criterion, the binary half.

    ``0.9995`` is a structurally valid probability -- ``Probability`` only requires
    ``[0, 1]`` -- and outside ``FORECAST_CONFIG``'s ``min_probability``/``max_probability``
    envelope, so ``binary.binary_output_problems`` refuses it. Driven through
    :func:`forecast.validate.output_problems`, exactly what ``forecast.generate`` runs, and
    through :func:`append_forecast_version`: the two paths' accepted sets must agree.
    """
    out_of_bounds = _response(final_prediction={"probability_yes": 0.9995})
    problems = output_problems(
        out_of_bounds, FORECAST_CONFIG, question=_question(), source_ids=["src-001", "src-002"]
    )
    assert problems, "generate's composed check accepted a probability outside the envelope"

    with pytest.raises(ForecastRecordError) as excinfo:
        append_forecast_version(
            conn,
            forecast_config=FORECAST_CONFIG,
            draft=_draft(generation=_generation(forecast=out_of_bounds)),
        )
    assert conn.execute("SELECT COUNT(*) FROM forecast_records").fetchone()[0] == 0
    assert "0.9995" not in str(excinfo.value)


def test_the_writer_refuses_what_generate_refuses_numeric_ordering(
    conn: sqlite3.Connection,
) -> None:
    """M1-507's acceptance criterion, the numeric half.

    The declared nine levels with the last two values swapped -- schema-valid, since
    ``schema.NumericPrediction`` does not check ordering, and refused by
    ``numeric.numeric_output_problems``'s non-decreasing rule. Driven through
    :func:`forecast.validate.output_problems`, exactly what ``forecast.generate`` runs, and
    through :func:`append_forecast_version`: the two paths' accepted sets must agree.
    """
    question = _numeric_question()
    malformed = _numeric_response(final_prediction={"percentiles": _disordered_percentiles()})
    problems = output_problems(
        malformed, FORECAST_CONFIG, question=question, source_ids=["src-001", "src-002"]
    )
    assert problems, "generate's composed check accepted a non-decreasing percentile set"

    with pytest.raises(ForecastRecordError) as excinfo:
        append_forecast_version(
            conn,
            forecast_config=FORECAST_CONFIG,
            draft=_draft(question=question, generation=_generation(forecast=malformed)),
        )
    assert conn.execute("SELECT COUNT(*) FROM forecast_records").fetchone()[0] == 0
    assert "42.0" not in str(excinfo.value)
    assert "38.0" not in str(excinfo.value)


def test_an_already_appended_record_cannot_be_appended_again(conn: sqlite3.Connection) -> None:
    record = append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft())
    with pytest.raises(ForecastRecordError):
        append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=record)
    assert conn.execute("SELECT COUNT(*) FROM forecast_records").fetchone()[0] == 1


def test_one_attempt_succeeds_at_most_once(conn: sqlite3.Connection) -> None:
    """``004``'s partial unique index, met by the writer rather than described by it."""
    append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft("attempt-1"))
    with pytest.raises(ForecastRecordError) as excinfo:
        append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft("attempt-1"))
    assert "attempt_id" in str(excinfo.value)
    assert conn.execute("SELECT COUNT(*) FROM forecast_records").fetchone()[0] == 1
    assert not conn.in_transaction


def test_a_forecast_record_cannot_claim_an_attempt_from_another_question(
    conn: sqlite3.Connection,
) -> None:
    """``004``'s identity-stability probe, reached through the real writer."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO pipeline_failure_events "
            "(attempt_id, question_id, tournament_id, event_type, detail_code, "
            "event_seq, occurred_at_utc, created_at_utc) "
            "VALUES ('attempt-1', 999, ?, 'research_failed', 'provider_error', 1, ?, ?)",
            (TOURNAMENT, TIMESTAMP, TIMESTAMP),
        )
    with pytest.raises(ForecastRecordError):
        append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft("attempt-1"))
    assert conn.execute("SELECT COUNT(*) FROM forecast_records").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("call", "match"),
    [
        pytest.param(
            lambda c: append_forecast_version(
                object(), forecast_config=FORECAST_CONFIG, draft=_draft()
            ),
            "Connection",
            id="not_a_connection",
        ),
        pytest.param(
            lambda c: append_forecast_version(c, forecast_config=FORECAST_CONFIG, draft={"a": 1}),
            "Draft",
            id="not_a_draft",
        ),
        pytest.param(lambda c: read_forecast_record(c, 42), "string", id="record_id_not_text"),
        pytest.param(
            lambda c: latest_forecast_version(c, question_id="1", tournament_id=TOURNAMENT),
            "int",
            id="question_id_not_int",
        ),
        pytest.param(
            lambda c: latest_forecast_version(c, question_id=1, tournament_id=7),
            "string",
            id="tournament_not_text",
        ),
    ],
)
def test_caller_mistakes_arrive_as_this_modules_error(
    conn: sqlite3.Connection, call: Any, match: str
) -> None:
    with pytest.raises(ForecastRecordError, match=match):
        call(conn)


def test_a_connection_not_opened_by_ledger_connect_is_refused(tmp_path: Path) -> None:
    """``transaction()`` requires explicit-transaction mode; the refusal is carried through.

    "open it with ledger.connect()" is the one thing that makes this actionable, which is
    why the message is preserved rather than replaced with a constant.
    """
    database = tmp_path / "ledger.sqlite3"
    initialize_ledger(database)
    raw = sqlite3.connect(database)
    with pytest.raises(ForecastRecordError, match="ledger.connect"):
        append_forecast_version(raw, forecast_config=FORECAST_CONFIG, draft=_draft())


def test_no_refusal_echoes_the_content_it_refused(conn: sqlite3.Connection) -> None:
    attempts: list[Any] = [
        lambda: append_forecast_version(
            conn,
            forecast_config=FORECAST_CONFIG,
            draft=_draft(
                question=_question(title=SECRET),
                generation=_generation(sources=_sources("src-009")),
            ),
        ),
        lambda: append_forecast_version(
            conn,
            forecast_config=FORECAST_CONFIG,
            draft=_draft(attempt_id=SECRET, tournament_id="x" * 201),
        ),
        lambda: read_forecast_record(conn, SECRET),
    ]
    raised = 0
    for attempt in attempts:
        try:
            attempt()
        except Exception as exc:  # noqa: BLE001 - the point is that nothing leaks
            raised += 1
            assert isinstance(exc, ForecastRecordError), attempt
            assert not _leaks(exc), attempt
    assert raised == len(attempts), "a refusal path stopped refusing; the guard is now vacuous"
    assert _leaks(ForecastRecordError(f"echoing {SECRET}")), "the leak detector is inert"


# --------------------------------------------------------------------------------------
# Reading back
# --------------------------------------------------------------------------------------


def test_a_stored_record_reads_back_as_what_was_returned(conn: sqlite3.Connection) -> None:
    v1 = append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft("attempt-1"))
    v2 = append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft("attempt-2"))
    assert read_forecast_record(conn, v1.record_id) == v1
    assert read_forecast_record(conn, v2.record_id) == v2


def test_an_unknown_record_id_is_refused_and_an_empty_chain_is_reported(
    conn: sqlite3.Connection,
) -> None:
    """ "No such record" and "nothing recorded yet" are different answers."""
    with pytest.raises(ForecastRecordError, match="does not name"):
        read_forecast_record(conn, "01a02000-0000-7000-8000-00000000dead")
    assert latest_forecast_version(conn, question_id=QUESTION_ID, tournament_id=TOURNAMENT) is None
    record = append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft())
    head = latest_forecast_version(conn, question_id=QUESTION_ID, tournament_id=TOURNAMENT)
    assert head == record


_RAW_SERIAL = itertools.count(1)


def _insert_raw(connection: sqlite3.Connection, **overrides: Any) -> str:
    """A row written straight past the writer, to test what the *reader* refuses.

    **Coherent unless an override makes it otherwise**, and that is the whole point. The
    first version of this helper wrote `attempt_id` as a fresh `raw-<uuid>` while the record
    it built said `attempt-1`, so every row it produced already contradicted its own
    `record_json` -- and every test asserting "the reader refuses a row where column X
    disagrees" passed without column X mattering at all. Caught while adding round 1's
    finding-B3 cases; `test_a_coherent_raw_row_reads_back` is the control that keeps it
    caught.

    The serial gives each call a distinct `attempt_id` -- `004` indexes it UNIQUE -- while
    keeping the record and the column agreeing on it.
    """
    attempt_id = f"raw-attempt-{next(_RAW_SERIAL)}"
    record = assign_identity(
        _draft(attempt_id),
        record_id=mint_record_id(),
        forecast_version=1,
        parent_record_id=None,
    )
    values: dict[str, Any] = {
        "record_id": record.record_id,
        "question_id": record.question_id,
        "post_id": record.post_id,
        "tournament_id": record.tournament_id,
        "forecast_version": record.forecast_version,
        "parent_record_id": None,
        "question_type": record.question_type,
        "question_domain": None,
        "status": "draft",
        "model_provider": record.model_settings.provider,
        "model_name": record.model_settings.name,
        "prompt_version": record.model_settings.prompt_version,
        "prompt_sha256": record.model_settings.prompt_sha256,
        "retrieval_run_id": record.retrieval_run_id,
        "generated_at_utc": GENERATED_AT.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"),
        "final_prediction_json": canonical_final_prediction_json(record),
        "record_json": canonical_record_json(record),
        "created_at_utc": TIMESTAMP,
        "forecast_sha256": record_sha256(record),
        "attempt_id": record.attempt_id,
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    with transaction(connection):
        connection.execute(
            f"INSERT INTO forecast_records ({columns}) VALUES ({placeholders})", values
        )
    return str(values["record_id"])


def test_a_coherent_raw_row_reads_back(conn: sqlite3.Connection) -> None:
    """The control for every refusal below.

    Without it, a `_insert_raw` that contradicted its own record in some column nobody named
    would make all of them pass while testing nothing -- which is exactly what the first
    version of that helper did. This asserts the premise the refusals rest on.
    """
    record_id = _insert_raw(conn)
    assert read_forecast_record(conn, record_id).record_id == record_id


def test_a_row_whose_hash_disagrees_with_its_record_is_refused(conn: sqlite3.Connection) -> None:
    record_id = _insert_raw(conn, forecast_sha256="e" * 64)
    with pytest.raises(ForecastRecordError, match="forecast_sha256"):
        read_forecast_record(conn, record_id)


@pytest.mark.parametrize(
    "column",
    [
        "question_id",
        "post_id",
        "tournament_id",
        "question_type",
        "question_domain",
        "model_provider",
        "model_name",
        "prompt_version",
        "prompt_sha256",
        "retrieval_run_id",
        "generated_at_utc",
        "final_prediction_json",
        "attempt_id",
    ],
)
def test_a_row_whose_columns_disagree_with_its_record_is_refused(
    conn: sqlite3.Connection, column: str
) -> None:
    """``001`` stores identity twice and only this comparison keeps the copies in step.

    Round 1, finding B3: the reader compared five identity columns, so a row whose
    ``question_type`` column read ``numeric`` while its ``record_json`` described a binary
    forecast came back as binary -- while ``approval.read_forecast_summary``, which reads
    the column, reported numeric. Two public readers, incompatible attribution, one
    immutable record. Every projected column is parametrized here rather than the one the
    finding named, because a subset is how the first version got it wrong.
    """
    stored: dict[str, Any] = {
        "question_id": 999,
        "post_id": 999,
        "tournament_id": "other-cup",
        "question_type": "numeric",
        "question_domain": "econ_data",
        "model_provider": "anthropic",
        "model_name": "some-other-model",
        "prompt_version": "9.9.9",
        "prompt_sha256": "f" * 64,
        "retrieval_run_id": RUN_ID_OTHER,
        "generated_at_utc": "2020-01-01T00:00:00.000000+00:00",
        "final_prediction_json": '{"probability_yes":0.99}',
        "attempt_id": "att-contradiction",
    }
    record_id = _insert_raw(conn, **{column: stored[column]})
    with pytest.raises(ForecastRecordError, match="columns"):
        read_forecast_record(conn, record_id)


def test_the_reader_checks_every_column_the_writer_derives(conn: sqlite3.Connection) -> None:
    """One projection, used to write the row and to check it coming back.

    The failure mode this guards is drift: a column added to the INSERT and not to the
    comparison would silently reopen finding B3 for that column. Asserted as **set
    equality** against `forecast_records`' own column list, minus the two the writer owns
    (`status`, pinned to `'draft'`; `created_at_utc`, which is not part of the record).
    """
    table_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(forecast_records)").fetchall()
    }
    assert set(store_module._PROJECTED_COLUMNS) | set(store_module._WRITER_OWNED_COLUMNS) == (
        table_columns
    )
    assert set(store_module._INSERT_COLUMNS) == table_columns


def test_the_chain_refuses_to_grow_past_the_largest_storable_version(
    conn: sqlite3.Connection,
) -> None:
    """Round 1, finding B5, as a regression.

    `001`-`006` accepted a `forecast_version` of `2**63-1` and `007` deliberately adds no
    backfill probe, so an upgraded ledger can still hold one. Incrementing it produced
    `2**63`, which sqlite3 refuses at bind time with a raw `OverflowError` -- a raw
    exception out of a public boundary, not this module's error.

    The trigger is dropped to seed the legacy head, simulating the ledger `007`'s own
    no-probe decision leaves reachable.
    """
    with transaction(conn):
        conn.execute("DROP TRIGGER forecast_records_require_draft_on_insert")
    _insert_raw(conn, forecast_version=2**63 - 1, attempt_id="att-legacy-max")
    with pytest.raises(ForecastRecordError, match="largest version"):
        append_forecast_version(conn, forecast_config=FORECAST_CONFIG, draft=_draft("attempt-new"))
    assert conn.execute("SELECT COUNT(*) FROM forecast_records").fetchone()[0] == 1
    assert not conn.in_transaction


def test_a_row_whose_record_json_is_not_canonical_is_refused(conn: sqlite3.Connection) -> None:
    record = assign_identity(
        _draft(), record_id=mint_record_id(), forecast_version=1, parent_record_id=None
    )
    pretty = json.dumps(json.loads(canonical_record_json(record)), indent=2)
    record_id = _insert_raw(conn, record_id=record.record_id, record_json=pretty)
    with pytest.raises(ForecastRecordError):
        read_forecast_record(conn, record_id)


# --------------------------------------------------------------------------------------
# Migration 007, exercised by direct SQL
# --------------------------------------------------------------------------------------


def _direct_insert(connection: sqlite3.Connection, **overrides: Any) -> None:
    try:
        _insert_raw(connection, **overrides)
    finally:
        if connection.in_transaction:  # pragma: no cover - transaction() unwinds already
            connection.execute("ROLLBACK")


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        pytest.param({"forecast_version": 0}, "at least 1", id="version_zero"),
        pytest.param({"forecast_version": -1}, "at least 1", id="version_negative"),
        pytest.param({"forecast_version": 1.5}, "at least 1", id="version_not_an_integer"),
        pytest.param({"record_json": "not json"}, "record_json", id="record_json_not_json"),
        pytest.param({"record_json": "[1]"}, "record_json", id="record_json_not_an_object"),
        pytest.param({"record_json": ""}, "record_json", id="record_json_blank"),
        pytest.param({"record_json": b"\x00\xff"}, "record_json", id="record_json_blob"),
        pytest.param(
            {"final_prediction_json": "0.37"}, "final_prediction_json", id="prediction_scalar"
        ),
        pytest.param({"final_prediction_json": ""}, "final_prediction_json", id="prediction_blank"),
        pytest.param({"question_type": "discrete"}, "question_type", id="unsupported_type"),
        pytest.param({"question_type": "date"}, "question_type", id="deferred_type"),
        pytest.param(
            {"forecast_version": 1, "parent_record_id": "01a02000-0000-7000-8000-00000000dead"},
            "root of a chain",
            id="v1_with_a_parent",
        ),
    ],
)
def test_migration_007_refuses_an_incoherent_row(
    conn: sqlite3.Connection, overrides: dict[str, Any], match: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=match):
        _direct_insert(conn, **overrides)
    assert conn.execute("SELECT COUNT(*) FROM forecast_records").fetchone()[0] == 0


def test_migration_007_refuses_a_later_version_with_no_parent(conn: sqlite3.Connection) -> None:
    _insert_raw(conn)
    with pytest.raises(sqlite3.IntegrityError, match="must name the record it supersedes"):
        _direct_insert(conn, forecast_version=2, parent_record_id=None)


@pytest.mark.parametrize(
    ("overrides", "note"),
    [
        pytest.param({"forecast_version": 3}, "skips a version", id="skips_a_version"),
        pytest.param(
            {"forecast_version": 2, "question_id": 999}, "another question", id="another_question"
        ),
        pytest.param(
            {"forecast_version": 2, "tournament_id": "other-cup"},
            "another tournament",
            id="another_tournament",
        ),
    ],
)
def test_migration_007_refuses_a_parent_that_is_not_the_previous_version(
    conn: sqlite3.Connection, overrides: dict[str, Any], note: str
) -> None:
    """The foreign key proves the parent exists; only this clause says *which* parent.

    Each case below satisfies ``REFERENCES forecast_records (record_id)`` perfectly.
    """
    parent = _insert_raw(conn)
    with pytest.raises(sqlite3.IntegrityError, match="immediately preceding version"):
        _direct_insert(conn, parent_record_id=parent, **overrides)
    assert conn.execute("SELECT COUNT(*) FROM forecast_records").fetchone()[0] == 1


def test_migration_007_refuses_a_row_that_is_its_own_parent(conn: sqlite3.Connection) -> None:
    """No separate clause: at BEFORE INSERT the row's own id is not in the table yet."""
    record_id = mint_record_id()
    with pytest.raises(sqlite3.IntegrityError, match="immediately preceding version"):
        _direct_insert(conn, record_id=record_id, forecast_version=2, parent_record_id=record_id)


def test_migration_007_still_carries_every_earlier_migrations_clause(
    conn: sqlite3.Connection,
) -> None:
    """``007`` recreates ``006``'s trigger; a dropped clause would be silent otherwise.

    One case per rule ``003``, ``004`` and ``006`` put on this trigger, because the
    recreate-by-the-same-name pattern's failure mode is losing a clause, not raising.
    """
    for overrides, match in [
        ({"record_id": "   "}, "record_id"),
        ({"record_id": b"\x01"}, "record_id"),
        ({"tournament_id": "\n\t"}, "tournament_id"),
        ({"status": "approved"}, "status draft"),
        ({"forecast_sha256": "not-a-hash"}, "forecast_sha256"),
        ({"forecast_sha256": None}, "forecast_sha256"),
        ({"attempt_id": None}, "attempt_id"),
        ({"attempt_id": "  "}, "attempt_id"),
    ]:
        with pytest.raises(sqlite3.IntegrityError, match=match):
            _direct_insert(conn, **overrides)
    assert conn.execute("SELECT COUNT(*) FROM forecast_records").fetchone()[0] == 0


# --------------------------------------------------------------------------------------
# record_id minting
# --------------------------------------------------------------------------------------


def test_a_minted_id_is_a_uuidv7() -> None:
    parsed = uuid.UUID(mint_record_id())
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122
    # The 48-bit timestamp is the current millisecond, not a random draw.
    minted_ms = parsed.int >> 80
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    assert abs(minted_ms - now_ms) < 5000


def test_minted_ids_are_distinct_and_sort_in_creation_order() -> None:
    """Time-ordering is the reason UUIDv7 was chosen over ``uuid4`` (M1-601's deferral).

    Minted in a tight loop **on purpose**, with no sleep. A plain UUIDv7 orders only across
    milliseconds, so a test that paused between draws would pass against an implementation
    with no counter at all -- and the case this project actually produces is the other one,
    a chain appended inside a single millisecond. The first version of ``mint_record_id``
    had no counter and this assertion is what found it.

    Compared as strings, because that is how they are stored and how ``ORDER BY record_id``
    would compare them: the hyphenated rendering is fixed-width lowercase hex, so
    lexicographic order over the text is the order over the integer.
    """
    minted = [mint_record_id() for _ in range(2000)]
    assert len(set(minted)) == len(minted)
    assert minted == sorted(minted)


def test_minted_ids_stay_ordered_inside_one_millisecond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clock frozen, so every id lands in the same millisecond by construction.

    A tight loop is *usually* inside one millisecond; frozen, it always is. Simulating a
    reachable condition, which is what CLAUDE.md permits a monkeypatch to do -- 4096 ids in
    one millisecond is beyond this pipeline, but a stopped or coarse clock is not.
    """
    monkeypatch.setattr(store_module.time, "time_ns", lambda: 1_756_000_000_000_000_000)
    minted = [mint_record_id() for _ in range(5000)]
    assert len(set(minted)) == len(minted)
    assert minted == sorted(minted)
    # More ids than the 12-bit counter holds, so the rollover branch really ran.
    assert len({uuid.UUID(value).int >> 80 for value in minted}) > 1


def test_a_backwards_clock_does_not_produce_a_duplicate_or_an_inversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An NTP correction or a resumed laptop moves the clock back; the ids do not.

    ``record_id`` is a primary key before it is a timestamp, so the stored millisecond is
    held and the counter advances rather than the other way round.
    """
    readings = iter([2_000_000_000_000_000_000] + [1_000_000_000_000_000_000] * 200)
    monkeypatch.setattr(store_module.time, "time_ns", lambda: next(readings))
    minted = [mint_record_id() for _ in range(100)]
    assert len(set(minted)) == len(minted)
    assert minted == sorted(minted)
