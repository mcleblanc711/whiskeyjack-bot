"""Properties of the forecast record, its hash and its version chain (M1-602).

CLAUDE.md asks four things of any hash or canonicalizer: it never raises outside its
module's own error type, it defines a total order wherever one is claimed, it is
replay-stable across the persisted form, and no message leaks a value. The fifth block
here is this item's own acceptance criterion, generalized: over any number of appends the
chain is contiguous, singly linked, and every earlier record's stored bytes are unchanged.

The round trip is through **real SQLite**, not a simulated one. M1-305 round 1 is the
reason: the JSON-only simulation is precisely the half that behaves, and the defect it
could not see was in the half that does not.
"""

from __future__ import annotations

import json
import re
import sqlite3
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from strategies import HOSTILE_TEXT  # type: ignore[import-not-found]

from whiskeyjack_bot.forecast.parse import ForecastGeneration, ModelSettings
from whiskeyjack_bot.forecast.inputs import SourceReference
from whiskeyjack_bot.forecast.record import (
    ForecastRecord,
    ForecastRecordError,
    assign_identity,
    build_forecast_record_draft,
    canonical_final_prediction_json,
    canonical_record_json,
    record_from_json,
    record_sha256,
)
from whiskeyjack_bot.forecast.schema import BinaryForecastResponse, validate_forecast_response
from whiskeyjack_bot.forecast.store import (
    append_forecast_version,
    latest_forecast_version,
    mint_record_id,
    read_forecast_record,
)
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import transaction
from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion

QUESTION_ID = 123
POST_ID = 456
TOURNAMENT = "minibench"
RUN_ID = "run-1"
TIMESTAMP = "2026-08-22T00:00:00.000000+00:00"
GENERATED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

# Small pools on purpose. A collision property needs colliding *draws*, not merely a
# colliding pool: with free text every generated record is unique and "different records
# hash differently" passes without ever exercising the equal case (M2-702's lesson).
_PROBABILITIES = st.sampled_from([0.0, 1.0, 0.37, 0.5, 0.001, 0.999])
_DOMAINS = st.sampled_from([None, "econ_data", "space_launch"])

# A UTF-16 surrogate pair is the one input a record may not hold: it is escaped and then
# *recombined* into a single scalar by the persisted form, so it comes back out of the
# ledger as a different string. `record.py` refuses one, so the round-trip properties below
# are claimed over text without one -- kept as its own strategy rather than folded into a
# filter inside each test, so the gap stays visible as its own property
# (`test_a_surrogate_pair_is_always_refused`) instead of quietly disappearing.
_SURROGATE_PAIR_RE = re.compile("[\ud800-\udbff][\udc00-\udfff]")
STORABLE_TEXT = HOSTILE_TEXT.filter(lambda value: _SURROGATE_PAIR_RE.search(value) is None)
PAIRED_TEXT = st.builds(
    lambda prefix, suffix: prefix + "\ud83d\ude00" + suffix,
    st.text(max_size=8),
    st.text(max_size=8),
)

# Planted rather than searched for. A no-leak assertion that looks for the *generated*
# value is vacuous for short draws -- "0" appears in any traceback -- which is M1-607's
# finding about substring-based leak checks. A distinctive marker at the *front* of the
# value survives truncation and cannot appear by accident.
LEAK_MARKER = "WJLEAKMARKER"


def _response(**overrides: Any) -> BinaryForecastResponse:
    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "question_id": QUESTION_ID,
        "question_type": "binary",
        "as_of_utc": "2026-08-22T11:00:00+00:00",
        "base_rate": {
            "reference_class": "similar announcements",
            "prior_probability": 0.3,
            "basis": "twelve prior cycles",
            "source_ids": ["src-001"],
        },
        "model_prior": 0.3,
        "status_quo": "nothing has been announced",
        "evidence_adjustments": [
            {
                "claim": "a schedule slipped",
                "direction": "down",
                "magnitude": "small",
                "source_ids": ["src-002"],
                "load_bearing": True,
            }
        ],
        "load_bearing_facts": [{"claim": "the window closes in March", "source_ids": ["src-002"]}],
        "source_disagreements": [],
        "failure_modes": ["the announcement is delayed past the window"],
        "reasoning_strategy_tags": ["base_rate"],
        "rationale_summary": "The base rate dominates and the evidence moves it slightly down.",
        "process_confidence": 0.6,
        "uncertainty_notes": [],
        "final_prediction": {"probability_yes": 0.37},
    }
    payload.update(overrides)
    return validate_forecast_response(payload, BinaryForecastResponse)


@st.composite
def records(draw: st.DrawFn, *, text: st.SearchStrategy[str] = STORABLE_TEXT) -> ForecastRecord:
    """One record, with untrusted text in the fields that really carry it.

    ``resolution_criteria`` is Metaculus text and ``rationale_summary`` is model output;
    both are bare-``str``-shaped in their schemas, so both accept everything ``HOSTILE_TEXT``
    produces. A constrained field like ``question.title`` would reject most of it before
    this module ever saw it, so feeding hostile text there would test pydantic instead.
    """
    criteria = draw(text)
    rationale = draw(text)
    domain = draw(_DOMAINS)
    probability = draw(_PROBABILITIES)
    version = draw(st.integers(min_value=1, max_value=4))

    question = CanonicalBinaryQuestion(
        question_id=QUESTION_ID,
        post_id=POST_ID,
        title="Will the thing happen?",
        resolution_criteria=criteria or None,
    )
    forecast = _response(
        # `rationale or ...` is not enough: `NonBlankStr` refuses anything blank *after
        # stripping*, and a whitespace-only draw is truthy, so `records()` could raise
        # `ForecastSchemaError` while generating an example. Reported as a non-blocking
        # observation in round 2 and reproduced by execution (`" "`, `"\t"` and `"\n\t"`
        # all reach the schema and are refused).
        #
        # Substituted only for the blank family, never `.strip()`-ed: every draw the schema
        # accepts is fed **as written**, because normalizing the input is how a property
        # stops testing the thing it was written for (M1-303).
        rationale_summary=rationale if rationale.strip() else "a rationale",
        final_prediction={"probability_yes": probability},
    )
    generation = ForecastGeneration(
        forecast=forecast,
        settings=ModelSettings(
            provider="openrouter",
            name="openrouter/test-model",
            temperature=0.1,
            max_output_tokens=2048,
            timeout_seconds=60.0,
            allowed_tries=2,
            prompt_version="1.1.0",
            prompt_sha256="b" * 64,
        ),
        sources=tuple(
            SourceReference(
                source_id=source_id,
                document_id=None,
                canonical_url=f"https://example.test/{source_id}",
                content_sha256="c" * 64,
            )
            for source_id in ("src-001", "src-002")
        ),
        request="packet",
        raw_responses=("{}",),
        invocations=1,
        repair_attempted=False,
        cost_usd=None,
        failure_code=None,
        failure_problems=(),
    )
    draft = build_forecast_record_draft(
        question=question,
        generation=generation,
        tournament_id=TOURNAMENT,
        attempt_id=f"attempt-{draw(st.integers(min_value=1, max_value=3))}",
        retrieval_run_id=RUN_ID,
        research_packet_sha256="d" * 64,
        generated_at=GENERATED_AT,
        question_domain=domain,
    )
    return assign_identity(
        draft,
        record_id="01a02000-0000-7000-8000-%012d" % draw(st.integers(min_value=1, max_value=9)),
        forecast_version=version,
        parent_record_id=None if version == 1 else "01a02000-0000-7000-8000-00000000dead",
    )


def _leaked(exc: BaseException, *values: str) -> str | None:
    rendered = str(exc) + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    for value in values:
        if value and value in rendered:
            return value
    return None


# --------------------------------------------------------------------------------------
# 1. Never raises outside the module's own error type
# --------------------------------------------------------------------------------------


@given(record=records())
def test_rendering_never_raises_anything_but_this_modules_error(record: ForecastRecord) -> None:
    try:
        canonical_record_json(record)
        canonical_final_prediction_json(record)
        record_sha256(record)
    except ForecastRecordError:
        pass


@given(
    text=st.text(max_size=200)
    | st.builds(
        json.dumps,
        st.recursive(
            st.none() | st.booleans() | st.integers() | st.text(max_size=8),
            lambda children: (
                st.lists(children, max_size=3)
                | st.dictionaries(st.text(max_size=5), children, max_size=3)
            ),
            max_leaves=8,
        ),
    )
)
def test_reading_arbitrary_stored_text_never_raises_anything_else(text: str) -> None:
    try:
        record_from_json(text)
    except ForecastRecordError:
        pass


# --------------------------------------------------------------------------------------
# 2. A total order, where one is claimed
# --------------------------------------------------------------------------------------


@given(count=st.integers(min_value=2, max_value=200))
@settings(max_examples=25)
def test_minted_ids_are_a_strict_total_order_in_creation_order(count: int) -> None:
    """``mint_record_id`` claims creation order; a claimed order gets a property.

    Strict, not merely non-decreasing: two equal record ids would be one primary key for
    two forecasts. Minted with no pause, so the same-millisecond case -- the one a
    counter-less UUIDv7 gets wrong -- is the case being generated.
    """
    minted = [mint_record_id() for _ in range(count)]
    assert len(set(minted)) == count
    assert all(earlier < later for earlier, later in zip(minted, minted[1:]))


# --------------------------------------------------------------------------------------
# 3. Replay stability across the persisted form
# --------------------------------------------------------------------------------------


@given(record=records())
def test_a_record_replays_to_itself_and_to_its_own_hash(record: ForecastRecord) -> None:
    text = canonical_record_json(record)
    restored = record_from_json(text)
    assert restored == record
    assert canonical_record_json(restored) == text
    assert record_sha256(restored) == record_sha256(record)


@given(left=records(), right=records())
def test_the_hash_keys_on_the_persisted_form_and_nothing_else(
    left: ForecastRecord, right: ForecastRecord
) -> None:
    """Equal persisted bytes iff equal hash -- both directions, over a colliding pool.

    The ``==`` direction is the one that needs colliding draws to mean anything, which is
    why the strategy samples from small pools. The ``!=`` direction is the injectivity
    claim the ledger rests on: a changed record is a changed hash, so an approval bound to
    the old one stops verifying.
    """
    same_bytes = canonical_record_json(left) == canonical_record_json(right)
    assert same_bytes == (record_sha256(left) == record_sha256(right))


@given(record=records())
def test_the_final_prediction_column_is_a_slice_of_the_record_bytes(
    record: ForecastRecord,
) -> None:
    nested = json.loads(canonical_record_json(record))["forecast"]["final_prediction"]
    assert canonical_final_prediction_json(record) == json.dumps(
        nested, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


# --------------------------------------------------------------------------------------
# 4. No value leak
# --------------------------------------------------------------------------------------


# Every message this module and its writer can produce, as prefixes. Closing the set is
# M1-607's answer to the vacuity of substring leak checks: a message that is not on this
# list is a message nobody reviewed for what it echoes, and the test says so.
_ALLOWED_MESSAGE_PREFIXES = (
    "stored record_json is not text",
    "stored record_json is not valid JSON",
    "stored record_json is not a JSON object",
    "stored record_json does not match the record schema (",
    "stored record_json does not round-trip to itself;",
    "the forecast record could not be assembled",
    "the forecast record could not be serialized",
    "the forecast record could not be rendered as canonical JSON",
    "the final prediction could not be rendered as canonical JSON",
    "the forecast record carries no final prediction",
    "a generation that produced no forecast",
    "generation must be a ForecastGeneration",
    "record must be a ForecastRecord",
    "draft must be a ForecastRecordDraft",
    "invalid forecast response:",
    "<record>",  # the field-path prefix of the replayability refusal
    # Round 1, finding B2. Written to lead with a constant rather than with the field path
    # so one prefix closes the whole family -- a message shaped `f"{path} cannot ..."` would
    # have needed one prefix per projected column, which is a list that drifts.
    "a record field cannot be stored in a text column of the ledger:",
)


@given(criteria=STORABLE_TEXT, rationale=STORABLE_TEXT, stored=st.text(max_size=120))
def test_no_message_repeats_the_content_it_refused(
    criteria: str, rationale: str, stored: str
) -> None:
    """Two assertions, because either one alone is worth less than it looks.

    *The marker.* Each untrusted value is prefixed with ``LEAK_MARKER`` and the marker is
    what must not appear. Searching for the generated value itself is what M1-607 found to
    be vacuous: a one-character draw appears in any traceback, so the property fails on
    noise and passes on nothing. A distinctive marker at the front of the value survives
    the truncation pydantic applies to a long input, so an echo of any prefix still trips.

    *The closed set.* A marker check only proves that **these** values did not appear. The
    message set is closed as well, so a new refusal path cannot be added without either
    appearing on the list or failing this test -- which is the half that keeps the guard
    honest as the module changes.

    Checked on the rendered traceback too: a chained exception's text reaches a log even
    when the message this project wrote is clean, which is what ``from None`` is for.
    """
    marked_criteria = LEAK_MARKER + criteria
    marked_rationale = LEAK_MARKER + rationale
    marked_stored = LEAK_MARKER + stored

    question = CanonicalBinaryQuestion(
        question_id=QUESTION_ID,
        post_id=POST_ID,
        title="Will the thing happen?",
        resolution_criteria=marked_criteria,
    )
    generation = ForecastGeneration(
        forecast=_response(rationale_summary=marked_rationale),
        settings=ModelSettings(
            provider="openrouter",
            name="openrouter/test-model",
            temperature=0.1,
            max_output_tokens=2048,
            timeout_seconds=60.0,
            allowed_tries=2,
            prompt_version="1.1.0",
            prompt_sha256="b" * 64,
        ),
        # A source the response does not cite, so the attribution gate has something to say.
        sources=(
            SourceReference(
                source_id="src-001",
                document_id=None,
                canonical_url="https://example.test/1",
                content_sha256="c" * 64,
            ),
        ),
        request="packet",
        raw_responses=("{}",),
        invocations=1,
        repair_attempted=False,
        cost_usd=None,
        failure_code=None,
        failure_problems=(),
    )

    def _build() -> Any:
        return build_forecast_record_draft(
            question=question,
            generation=generation,
            tournament_id=TOURNAMENT,
            attempt_id="attempt-1",
            retrieval_run_id=RUN_ID,
            research_packet_sha256="d" * 64,
            generated_at=GENERATED_AT,
            question_domain=marked_criteria,
        )

    attempts: list[Any] = [
        _build,
        lambda: record_from_json(marked_stored),
        lambda: record_from_json(json.dumps({"resolution_criteria": marked_criteria})),
        # The marker as a *key*, not a value. Round 1's finding B1 had a second half the
        # first version of this property could not see: under `extra="forbid"` the offending
        # key is the error's `loc`, and this property only ever planted the marker in
        # values. A leak channel a property does not feed is a leak channel it does not test.
        lambda: record_from_json(json.dumps({marked_criteria: 1})),
        lambda: record_sha256(_build()),
    ]
    for attempt in attempts:
        try:
            attempt()
        except ForecastRecordError as exc:
            assert _leaked(exc, LEAK_MARKER) is None
            assert str(exc).startswith(_ALLOWED_MESSAGE_PREFIXES), str(exc)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"escaped as {type(exc).__name__}") from exc


@given(text=PAIRED_TEXT)
def test_a_surrogate_pair_is_always_refused(text: str) -> None:
    """The gap ``STORABLE_TEXT`` filters out, asserted rather than left implicit.

    A record holding a surrogate pair is escaped and then *recombined* by the persisted
    form, so it reads back as a different string. Refused rather than rewritten, and the
    corruption is demonstrated on the rendering rule itself so this stays meaningful if the
    guard moves.
    """
    assert json.loads(json.dumps(text, ensure_ascii=True)) != text
    question = CanonicalBinaryQuestion(
        question_id=QUESTION_ID,
        post_id=POST_ID,
        title="Will the thing happen?",
        resolution_criteria=text,
    )
    generation = ForecastGeneration(
        forecast=_response(),
        settings=ModelSettings(
            provider="openrouter",
            name="openrouter/test-model",
            temperature=0.1,
            max_output_tokens=2048,
            timeout_seconds=60.0,
            allowed_tries=2,
            prompt_version="1.1.0",
            prompt_sha256="b" * 64,
        ),
        sources=tuple(
            SourceReference(
                source_id=source_id,
                document_id=None,
                canonical_url=f"https://example.test/{source_id}",
                content_sha256="c" * 64,
            )
            for source_id in ("src-001", "src-002")
        ),
        request="packet",
        raw_responses=("{}",),
        invocations=1,
        repair_attempted=False,
        cost_usd=None,
        failure_code=None,
        failure_problems=(),
    )
    with pytest.raises(ForecastRecordError, match="surrogate pair"):
        build_forecast_record_draft(
            question=question,
            generation=generation,
            tournament_id=TOURNAMENT,
            attempt_id="attempt-1",
            retrieval_run_id=RUN_ID,
            research_packet_sha256="d" * 64,
            generated_at=GENERATED_AT,
        )


@given(rationale=st.sampled_from(["", " ", "\t", "\n\t", "\u00a0", "a real rationale"]))
@settings(max_examples=10)
def test_the_record_strategy_never_raises_while_generating(rationale: str) -> None:
    """The premise every property above rests on: a draw builds a record.

    Round 2's non-blocking observation. `NonBlankStr` refuses anything blank after
    stripping, so the blank family has to be substituted rather than merely falsy-checked --
    otherwise a rare whitespace draw turns a required gate red for a reason that has nothing
    to do with the code under test. Asserted directly on the substitution rather than left
    to the odds of the other properties drawing one.
    """
    kept = rationale if rationale.strip() else "a rationale"
    assert _response(rationale_summary=kept).rationale_summary == kept


def test_the_leak_detector_is_not_inert() -> None:
    """A no-leak property is worth exactly what its detector is worth (M1-308)."""
    assert _leaked(ForecastRecordError(f"saw {LEAK_MARKER}"), LEAK_MARKER) == LEAK_MARKER
    assert _leaked(ForecastRecordError("clean"), LEAK_MARKER) is None


# --------------------------------------------------------------------------------------
# 5. The version chain -- this item's acceptance criterion, generalized
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ledger_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Module-scoped, because ``@given`` plus a function-scoped ``tmp_path`` is a
    hypothesis health-check failure -- the same directory would be reused for every
    example while the fixture claimed otherwise (M1-308 round 5)."""
    return tmp_path_factory.mktemp("forecast-ledgers")


def _fresh_ledger(root: Path, name: str) -> sqlite3.Connection:
    database = root / f"{name}.sqlite3"
    initialize_ledger(database)
    connection = connect(database)
    with transaction(connection):
        connection.execute(
            "INSERT INTO research_runs "
            "(retrieval_run_id, provider, started_at_utc, created_at_utc, question_id) "
            "VALUES (?, 'exa', ?, ?, ?)",
            (RUN_ID, TIMESTAMP, TIMESTAMP, QUESTION_ID),
        )
    return connection


def _draft_for(index: int, *, criteria: str) -> Any:
    question = CanonicalBinaryQuestion(
        question_id=QUESTION_ID,
        post_id=POST_ID,
        title="Will the thing happen?",
        resolution_criteria=criteria or None,
    )
    generation = ForecastGeneration(
        forecast=_response(final_prediction={"probability_yes": min(0.999, 0.1 * index)}),
        settings=ModelSettings(
            provider="openrouter",
            name="openrouter/test-model",
            temperature=0.1,
            max_output_tokens=2048,
            timeout_seconds=60.0,
            allowed_tries=2,
            prompt_version="1.1.0",
            prompt_sha256="b" * 64,
        ),
        sources=tuple(
            SourceReference(
                source_id=source_id,
                document_id=None,
                canonical_url=f"https://example.test/{source_id}",
                content_sha256="c" * 64,
            )
            for source_id in ("src-001", "src-002")
        ),
        request="packet",
        raw_responses=("{}",),
        invocations=1,
        repair_attempted=False,
        cost_usd=None,
        failure_code=None,
        failure_problems=(),
    )
    return build_forecast_record_draft(
        question=question,
        generation=generation,
        tournament_id=TOURNAMENT,
        attempt_id=f"attempt-{index}",
        retrieval_run_id=RUN_ID,
        research_packet_sha256="d" * 64,
        generated_at=GENERATED_AT,
    )


@given(
    versions=st.integers(min_value=1, max_value=6),
    criteria=st.sampled_from(["", "Resolves YES if it happens.", "\ud800", "résumé", "😀"]),
    seed=st.integers(min_value=0, max_value=10**9),
)
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_appending_versions_never_moves_an_earlier_one(
    ledger_root: Path, versions: int, criteria: str, seed: int
) -> None:
    """The criterion as a property: n appends, and nothing already stored changes.

    Compared as raw bytes (``CAST(... AS BLOB)``), because the claim is about the bytes on
    disk. Two decoded strings comparing equal cannot see a re-encoding, and the surrogate
    cases in this strategy are exactly the inputs where the two differ.
    """
    try:
        conn = _fresh_ledger(ledger_root, f"chain-{seed}")
    except Exception:  # pragma: no cover - a name collision across examples
        assume(False)
        raise

    def stored_bytes(record_id: str) -> tuple[Any, ...]:
        row = conn.execute(
            "SELECT CAST(record_json AS BLOB), CAST(final_prediction_json AS BLOB), "
            "forecast_sha256, forecast_version, parent_record_id, created_at_utc "
            "FROM forecast_records WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        assert row is not None
        return tuple(row)

    try:
        snapshots: dict[str, tuple[Any, ...]] = {}
        appended: list[ForecastRecord] = []
        for index in range(1, versions + 1):
            record = append_forecast_version(conn, draft=_draft_for(index, criteria=criteria))
            for record_id, snapshot in snapshots.items():
                assert stored_bytes(record_id) == snapshot, "an earlier version moved"
            snapshots[record.record_id] = stored_bytes(record.record_id)
            appended.append(record)

        assert [r.forecast_version for r in appended] == list(range(1, versions + 1))
        assert [r.parent_record_id for r in appended] == [None] + [
            r.record_id for r in appended[:-1]
        ]
        assert [r.record_id for r in appended] == sorted(r.record_id for r in appended)
        for record in appended:
            assert read_forecast_record(conn, record.record_id) == record
            assert stored_bytes(record.record_id)[0] == canonical_record_json(record).encode()
        head = latest_forecast_version(conn, question_id=QUESTION_ID, tournament_id=TOURNAMENT)
        assert head == appended[-1]
    finally:
        conn.close()
