"""M1-406 acceptance: replay makes zero API calls and reproduces the parsed forecast hash.

The criterion is asserted three ways, because each covers something the others cannot:

- :func:`test_replay_reproduces_the_stored_forecast_hash` is the round trip -- persist a
  real generation, then re-derive its record from the stored provider text and compare the
  hashes;
- ``test_the_replay_path_reaches_no_provider_client`` (in ``test_forecast_generate.py``) is
  the zero-calls half, asserted as a property of the **import graph** rather than of a mock
  count, which is how M1-306 settled it: a module that cannot reach an SDK has no call to
  make, whatever a test double is or is not asked;
- the mismatch tests below check that the instrument can say *no*. A verifier that cannot
  fail is not a verifier, and M1-501's lesson is that a one-sided assertion is vacuous
  against exactly the change that breaks the thing.

The persistence half (M1-312's ordering rule, applied to the model call) is exercised with
**real** artifact failures -- a destination that already exists, an unwritable directory --
rather than monkeypatched ones.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import traceback
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from whiskeyjack_bot.artifacts import ArtifactError
from whiskeyjack_bot.config import AppConfig, load_config
from whiskeyjack_bot.forecast.artifacts import artifact_relative_path, write_raw_model_output
from whiskeyjack_bot.forecast.inputs import SourceReference
from whiskeyjack_bot.forecast.parse import ForecastGeneration, ModelSettings
from whiskeyjack_bot.forecast.persist import (
    GenerationPersistence,
    persist_generation,
    persist_raw_output,
)
from whiskeyjack_bot.forecast.record import (
    ForecastRecordDraft,
    ForecastRecordError,
    assign_identity,
    build_forecast_record_draft,
    record_sha256,
)
from whiskeyjack_bot.forecast.numeric import NumericOutputError
from whiskeyjack_bot.forecast.replay import ForecastReplay, replay_forecast
from whiskeyjack_bot.forecast.schema import (
    ForecastResponse,
    response_model_for,
    validate_forecast_response,
)
from whiskeyjack_bot.forecast import store as store_module
from whiskeyjack_bot.forecast.store import (
    ModelCall,
    mint_record_id,
    read_forecast_record,
    read_model_call,
)
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.questions.model import CanonicalNumericQuestion
from whiskeyjack_bot.lifecycle import transaction
from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalMultipleChoiceQuestion,
    CanonicalQuestion,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")

QUESTION_ID = 123
POST_ID = 456
TOURNAMENT = "minibench"
RUN_ID = "run-1"
ATTEMPT = "attempt-1"
GENERATED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
WRITTEN_AT = datetime(2026, 8, 22, 12, 1, tzinfo=timezone.utc)
TIMESTAMP = "2026-08-22T00:00:00.000000+00:00"

PLANTED = "privateFAKE123456"

_ROOT_ONLY = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="an unwritable directory does not stop root",
)


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


def _json_block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block("Binary schema") + "}"),
    }
    payload["question_id"] = QUESTION_ID
    payload.update(overrides)
    return payload


def _response(**overrides: Any) -> ForecastResponse:
    payload = _payload(**overrides)
    return validate_forecast_response(payload, response_model_for(payload["question_type"]))


def _reply(**overrides: Any) -> str:
    """The provider text a model would have returned for :func:`_response`.

    Rendered from the same payload the response is validated from, so the artifact holds a
    reply that really does parse to the stored forecast -- which is the whole thing under
    test. A hand-written near-miss would make every match below accidental.
    """
    return json.dumps(_payload(**overrides))


def _question(**overrides: Any) -> CanonicalQuestion:
    fields: dict[str, Any] = {
        "question_id": QUESTION_ID,
        "post_id": POST_ID,
        "title": "Will the thing happen?",
    }
    fields.update(overrides)
    return CanonicalBinaryQuestion(**fields)


def _sources() -> tuple[SourceReference, ...]:
    return tuple(
        SourceReference(
            source_id=source_id,
            document_id=None,
            canonical_url=f"https://example.test/{source_id}",
            content_sha256="c" * 64,
        )
        for source_id in ("src-001", "src-002")
    )


def _settings(**overrides: Any) -> ModelSettings:
    fields: dict[str, Any] = {
        "provider": "openrouter",
        "name": "openrouter/test-model",
        "temperature": 0.1,
        "max_output_tokens": 2048,
        "timeout_seconds": 60.0,
        "allowed_tries": 2,
        "prompt_version": "1.1.0",
        "prompt_sha256": "b" * 64,
    }
    fields.update(overrides)
    return ModelSettings(**fields)


def _generation(**overrides: Any) -> ForecastGeneration:
    fields: dict[str, Any] = {
        "forecast": _response(),
        "settings": _settings(),
        "sources": _sources(),
        "request": "the rendered reasoning packet",
        "raw_responses": (_reply(),),
        "invocations": 1,
        "repair_attempted": False,
        "cost_usd": 0.25,
        "failure_code": None,
        "failure_problems": (),
    }
    fields.update(overrides)
    return ForecastGeneration(**fields)


def _draft(
    generation: ForecastGeneration,
    attempt_id: str = ATTEMPT,
    question: CanonicalQuestion | None = None,
) -> ForecastRecordDraft:
    return build_forecast_record_draft(
        question=question if question is not None else _question(),
        generation=generation,
        tournament_id=TOURNAMENT,
        attempt_id=attempt_id,
        retrieval_run_id=RUN_ID,
        research_packet_sha256="d" * 64,
        generated_at=GENERATED_AT,
    )


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    root.mkdir()
    return root


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    database = tmp_path / "ledger.sqlite3"
    initialize_ledger(database)
    connection = connect(database)
    with transaction(connection):
        connection.execute(
            "INSERT INTO research_runs "
            "(retrieval_run_id, provider, started_at_utc, created_at_utc, question_id) "
            "VALUES (?, 'exa', ?, ?, ?)",
            (RUN_ID, TIMESTAMP, TIMESTAMP, QUESTION_ID),
        )
    try:
        yield connection
    finally:
        connection.close()


def _config(tmp_path: Path, artifact_root: Path, **overrides: object) -> AppConfig:
    """A real AppConfig off config.example.yaml, rooted in the test's tmp_path.

    Overrides are addressed as ``section__key``, matching ``test_research_persist.py``, so a
    test can flip one flag without restating either section. Replay is **enabled** here
    because every test that does not name the flag is about something else; the committed
    default is `false` and one test asserts that the default refuses.
    """
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["model"]["name"] = "openai/gpt-4o"
    data["retrieval"]["social"]["agent_model"] = "grok-3"
    data["storage"]["artifact_root"] = str(artifact_root)
    data["storage"]["sqlite_path"] = str(tmp_path / "ledger.sqlite3")
    data["forecast"]["replay_saved_model_output"] = True
    for name, value in overrides.items():
        section, _, key = name.partition("__")
        data[section][key] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path)


def _persist(
    conn: sqlite3.Connection,
    config: AppConfig,
    generation: ForecastGeneration | None = None,
    question: CanonicalQuestion | None = None,
) -> GenerationPersistence:
    generation = generation if generation is not None else _generation()
    return persist_generation(
        conn,
        config,
        draft=_draft(generation, question=question),
        generation=generation,
        written_at=WRITTEN_AT,
    )


def _leaks(exc: BaseException) -> bool:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return PLANTED in str(exc) or PLANTED in rendered


# --------------------------------------------------------------------------------------
# The acceptance criterion
# --------------------------------------------------------------------------------------


def test_replay_reproduces_the_stored_forecast_hash(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    config = _config(tmp_path, artifacts)
    stored = _persist(conn, config)
    assert stored.record is not None
    assert stored.artifact_outcome == "written"

    result = replay_forecast(conn, config, record_id=stored.record.record_id)

    assert result.matches
    assert result.replayed_sha256 == result.stored_sha256
    assert result.stored_sha256 == record_sha256(stored.record)
    assert result.problems == ()
    assert result.call == ModelCall(
        raw_output_path=artifact_relative_path(question_id=QUESTION_ID, attempt_id=ATTEMPT),
        cost_usd=0.25,
        model_invocations=1,
    )


MC_OPTIONS = ("Option one", "Option two")


def _mc_overrides() -> dict[str, Any]:
    """``_payload`` overrides that retype the prompt's shared fields as multiple choice."""
    base = json.loads(_json_block("Shared fields"))["base_rate"]
    return {
        "question_type": "multiple_choice",
        # The prompt's own rule, which ``schema.py`` enforces on this response type.
        "model_prior": None,
        "base_rate": {**base, "prior_probability": None},
        "final_prediction": {
            "options": [
                {"option": MC_OPTIONS[0], "probability": 0.6},
                {"option": MC_OPTIONS[1], "probability": 0.4},
            ]
        },
    }


def _mc_question() -> CanonicalMultipleChoiceQuestion:
    return CanonicalMultipleChoiceQuestion(
        question_id=QUESTION_ID,
        post_id=POST_ID,
        title="Which thing happens?",
        options=list(MC_OPTIONS),
    )


def test_a_multiple_choice_record_replays_against_its_stored_option_list(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """M1-404 gave this module a branch, and nothing here reached it.

    Every record built in this file is binary, so ``options`` was ``None`` on every replay
    and the side of the branch that reads the stored question's option list was dead under
    test. This is the happy path across it: a stored multiple-choice record must still
    replay to its own hash.
    """
    config = _config(tmp_path, artifacts)
    overrides = _mc_overrides()
    generation = _generation(forecast=_response(**overrides), raw_responses=(_reply(**overrides),))
    stored = _persist(conn, config, generation, question=_mc_question())
    assert stored.record is not None
    assert stored.record.question_type == "multiple_choice"

    result = replay_forecast(conn, config, record_id=stored.record.record_id)

    assert result.matches, result.problems
    assert result.problems == ()
    assert result.replayed_sha256 == result.stored_sha256


def test_a_replayed_multiple_choice_reply_is_checked_against_the_stored_options(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """The load-bearing negative for that branch.

    Editing the stored reply to name an option the question never supplied must be caught
    *by the option rules*, not merely produce a different hash -- which is what proves the
    replay is running M1-404's checker with the record's own option list rather than
    passing ``None`` and skipping it. A replay that skipped the check would re-parse this
    reply cleanly and report a hash mismatch instead, so the two outcomes are
    distinguishable and this asserts the right one.
    """
    config = _config(tmp_path, artifacts)
    overrides = _mc_overrides()
    generation = _generation(forecast=_response(**overrides), raw_responses=(_reply(**overrides),))
    stored = _persist(conn, config, generation, question=_mc_question())
    assert stored.record is not None
    assert stored.raw_output_path is not None

    path = artifacts / stored.raw_output_path
    envelope = json.loads(path.read_text(encoding="utf-8"))
    edited = json.loads(envelope["raw_responses"][0])
    edited["final_prediction"]["options"][1]["option"] = "An option nobody offered"
    envelope["raw_responses"] = [json.dumps(edited)]
    path.write_text(json.dumps(envelope), encoding="utf-8")

    result = replay_forecast(conn, config, record_id=stored.record.record_id)

    assert not result.matches
    assert result.replayed_sha256 is None
    assert list(result.problems) == [
        "final_prediction.options: must name only options the question supplied "
        "(offending labels withheld)",
        "final_prediction.options: must name every option the question supplied "
        "(offending labels withheld)",
    ]
    assert not any("An option nobody offered" in problem for problem in result.problems)


def test_replay_re_derives_rather_than_reading_the_stored_answer_back(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """The load-bearing negative: a replay whose hash came from the record would match
    whatever the artifact said.

    Editing the stored reply to a *different but still valid* forecast must produce a
    mismatch. If it does not, the comparison is between the record and itself -- exactly
    the vacuous shape M1-501's round-1 lesson names, and the one that would make every
    other test here pass for the wrong reason.
    """
    config = _config(tmp_path, artifacts)
    stored = _persist(conn, config)
    assert stored.record is not None
    assert stored.raw_output_path is not None

    path = artifacts / stored.raw_output_path
    envelope = json.loads(path.read_text(encoding="utf-8"))
    original = json.loads(envelope["raw_responses"][0])
    assert original["final_prediction"]["probability_yes"] != 0.42
    original["final_prediction"]["probability_yes"] = 0.42
    envelope["raw_responses"] = [json.dumps(original)]
    path.write_text(json.dumps(envelope), encoding="utf-8")

    result = replay_forecast(conn, config, record_id=stored.record.record_id)
    assert not result.matches
    assert result.replayed_sha256 is not None
    assert result.replayed_sha256 != result.stored_sha256
    # The stored record is untouched: 003 blocks UPDATE on this table, and nothing here
    # writes anyway. A replay is a verification instrument, never a second writer.
    assert record_sha256(read_forecast_record(conn, stored.record.record_id)) == (
        result.stored_sha256
    )


def _numeric_generation() -> ForecastGeneration:
    """A numeric generation whose stored reply parses cleanly against a valid question."""
    payload = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block("Numeric schema") + "}"),
    }
    payload["question_id"] = QUESTION_ID
    payload["model_prior"] = None
    payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    forecast = validate_forecast_response(payload, response_model_for("numeric"))
    return _generation(forecast=forecast, raw_responses=(json.dumps(payload),))


def _numeric_question(**overrides: Any) -> CanonicalNumericQuestion:
    fields: dict[str, Any] = {
        "question_id": QUESTION_ID,
        "post_id": POST_ID,
        "title": "How many things?",
        "lower_bound": 0.0,
        "upper_bound": 100.0,
        "open_lower_bound": False,
        "open_upper_bound": False,
        "cdf_size": 201,
    }
    fields.update(overrides)
    return CanonicalNumericQuestion(**fields)


def test_a_stored_question_no_percentile_set_could_satisfy_is_a_record_error(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """M1-405 round 1, finding 2: a member checker's error must not escape this boundary.

    ``CanonicalNumericQuestion`` accepts ``zero_point == lower_bound``, so does
    ``ForecastRecordDraft``. The pinned SDK refuses such a question outright, so
    ``numeric._require_question`` raises rather than reporting a repairable problem, and
    ``forecast.generate`` refuses it in a preflight before anything is spent. Since M1-507,
    ``forecast.store.append_forecast_version`` refuses it too, on the ordinary write path --
    it runs the same composed check through a ``ForecastConfig`` it did not have before.

    So the row this test is about -- one that is already stored despite being unsatisfiable,
    which is what makes replay's own re-check the only thing that catches it -- can no
    longer be produced through the writer. It is built and inserted directly instead
    (:func:`assign_identity` plus the writer's own ``_insert``, bypassing
    ``append_forecast_version``'s validation the way a row written before this rule existed
    would have reached the ledger). Replay has no preflight of its own: it is reading a row
    that already exists, reaches ``_parse``, and this module's own docstring says a raw
    ``ForecastSchemaError`` out of a public boundary is a review finding here (it has been,
    three times now). It arrives as ``ForecastRecordError``, the same translation
    ``response_model_for`` already had one line above.
    """
    config = _config(tmp_path, artifacts)
    generation = _numeric_generation()
    draft = build_forecast_record_draft(
        question=_numeric_question(zero_point=0.0),
        generation=generation,
        tournament_id=TOURNAMENT,
        attempt_id=ATTEMPT,
        retrieval_run_id=RUN_ID,
        research_packet_sha256="d" * 64,
        generated_at=GENERATED_AT,
    )
    path = write_raw_model_output(
        artifacts,
        attempt_id=ATTEMPT,
        question_id=QUESTION_ID,
        generation=generation,
        written_at_utc=WRITTEN_AT,
        retain=True,
    )
    assert path is not None
    record = assign_identity(
        draft, record_id=mint_record_id(), forecast_version=1, parent_record_id=None
    )
    with transaction(conn):
        store_module._insert(
            conn,
            record,
            ModelCall(
                raw_output_path=path,
                cost_usd=generation.cost_usd,
                model_invocations=generation.invocations,
            ),
        )

    with pytest.raises(ForecastRecordError) as caught:
        replay_forecast(conn, config, record_id=record.record_id)
    # Exact type, not isinstance: NumericOutputError also subclasses ForecastSchemaError,
    # so an isinstance assertion here would pass on the unfixed code.
    assert type(caught.value) is ForecastRecordError
    assert not isinstance(caught.value, NumericOutputError)
    # ``from None``: the chained cause would re-render the member's problem list.
    assert caught.value.__cause__ is None


def test_the_same_stored_row_replays_when_its_zero_point_is_satisfiable(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """The companion: the refusal above must be about the unsatisfiable pair and nothing
    else, or every log-scaled numeric record would be unreplayable."""
    config = _config(tmp_path, artifacts)
    generation = _numeric_generation()
    draft = build_forecast_record_draft(
        question=_numeric_question(lower_bound=1.0, zero_point=0.5),
        generation=generation,
        tournament_id=TOURNAMENT,
        attempt_id=ATTEMPT,
        retrieval_run_id=RUN_ID,
        research_packet_sha256="d" * 64,
        generated_at=GENERATED_AT,
    )
    stored = persist_generation(
        conn, config, draft=draft, generation=generation, written_at=WRITTEN_AT
    )
    assert stored.record is not None

    result = replay_forecast(conn, config, record_id=stored.record.record_id)
    assert result.matches
    assert result.problems == ()


def test_a_reply_that_no_longer_parses_is_reported_and_not_raised(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """ "we checked and it differs" is an answer; the problems say why."""
    config = _config(tmp_path, artifacts)
    stored = _persist(conn, config)
    assert stored.record is not None and stored.raw_output_path is not None

    path = artifacts / stored.raw_output_path
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["raw_responses"] = ["not json at all"]
    path.write_text(json.dumps(envelope), encoding="utf-8")

    result = replay_forecast(conn, config, record_id=stored.record.record_id)
    assert not result.matches
    assert result.replayed_sha256 is None
    assert result.problems == ("the reply was not a single JSON object",)


def test_the_last_reply_is_the_one_replayed(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """`generate_forecast` returns as soon as a reply parses, so the last one is the one the
    record came from; an earlier one is a malformed reply a repair turn replaced."""
    config = _config(tmp_path, artifacts)
    generation = _generation(
        raw_responses=("not json at all", _reply()), invocations=2, repair_attempted=True
    )
    stored = _persist(conn, config, generation)
    assert stored.record is not None

    result = replay_forecast(conn, config, record_id=stored.record.record_id)
    assert result.matches
    assert result.raw_response_count == 2
    assert result.call.model_invocations == 2


# --------------------------------------------------------------------------------------
# Refusals: "we could not check" is not "we checked and it differs"
# --------------------------------------------------------------------------------------


def test_the_committed_default_refuses_to_replay(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """`forecast.replay_saved_model_output` defaults to false, and honouring it is what
    keeps "we replayed" from being something that happened by accident
    (``research/store.py::replay_research``'s rule)."""
    enabled = _config(tmp_path, artifacts)
    stored = _persist(conn, enabled)
    assert stored.record is not None
    disabled = _config(tmp_path, artifacts, forecast__replay_saved_model_output=False)
    with pytest.raises(ForecastRecordError):
        replay_forecast(conn, disabled, record_id=stored.record.record_id)


def test_a_row_with_no_recorded_artifact_is_refused_not_reported_as_a_mismatch(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    config = _config(tmp_path, artifacts, storage__retain_raw_model_output=False)
    stored = _persist(conn, config)
    assert stored.record is not None
    assert stored.artifact_outcome == "retention_disabled"
    assert read_model_call(conn, stored.record.record_id).raw_output_path is None

    replay_config = _config(tmp_path, artifacts)
    with pytest.raises(ForecastRecordError):
        replay_forecast(conn, replay_config, record_id=stored.record.record_id)


def test_an_artifact_belonging_to_another_attempt_is_refused(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """`raw_output_path` is written once and never corrected, and `008` constrains its
    *shape*, not which file it names. So the envelope's own provenance is compared against
    the row -- otherwise another attempt's text would be re-parsed and its hash reported as
    this record's, the single worst thing this module could do."""
    config = _config(tmp_path, artifacts)
    stored = _persist(conn, config)
    assert stored.record is not None and stored.raw_output_path is not None
    path = artifacts / stored.raw_output_path
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["attempt_id"] = "attempt-2"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ForecastRecordError):
        replay_forecast(conn, config, record_id=stored.record.record_id)


def test_an_unknown_record_and_an_unreadable_artifact_are_refusals(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    config = _config(tmp_path, artifacts)
    with pytest.raises(ForecastRecordError):
        replay_forecast(conn, config, record_id="no-such-record")
    stored = _persist(conn, config)
    assert stored.record is not None and stored.raw_output_path is not None
    (artifacts / stored.raw_output_path).unlink()
    with pytest.raises(ForecastRecordError):
        replay_forecast(conn, config, record_id=stored.record.record_id)


def test_a_replay_result_cannot_claim_a_match_it_did_not_compute() -> None:
    """M1-312's rule: a result type that cannot represent a lie is half the criterion."""
    call = ModelCall(raw_output_path="forecast/1/a.json", cost_usd=0.0, model_invocations=1)
    with pytest.raises(ForecastRecordError):
        ForecastReplay(
            record_id="r",
            stored_sha256="a" * 64,
            replayed_sha256="b" * 64,
            matches=True,
            problems=(),
            call=call,
            raw_response_count=1,
        )
    with pytest.raises(ForecastRecordError):
        ForecastReplay(
            record_id="r",
            stored_sha256="a" * 64,
            replayed_sha256=None,
            matches=False,
            problems=(),
            call=call,
            raw_response_count=1,
        )
    with pytest.raises(ForecastRecordError):
        ForecastReplay(
            record_id="r",
            stored_sha256="a" * 64,
            replayed_sha256="a" * 64,
            matches=True,
            problems=("something",),
            call=call,
            raw_response_count=1,
        )


# --------------------------------------------------------------------------------------
# Persistence: the artifact-first ordering rule (M1-312, applied to the model call)
# --------------------------------------------------------------------------------------


def test_an_artifact_failure_still_records_the_forecast_and_reports_the_loss(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """A real failure, not a monkeypatched one: the destination already exists, which is
    the condition `os.link` refuses.

    The call cost money; losing its evidence is an audit loss and losing its row is losing
    the record that money was spent at all. So the row lands with a NULL path, the cost and
    the invocation count are still recorded, and the loss reaches the caller as a value.
    """
    config = _config(tmp_path, artifacts)
    relative = artifact_relative_path(question_id=QUESTION_ID, attempt_id=ATTEMPT)
    (artifacts / relative).parent.mkdir(parents=True)
    (artifacts / relative).write_text("an artifact already here", encoding="utf-8")

    stored = _persist(conn, config)
    assert stored.record is not None
    assert stored.artifact_outcome == "failed"
    assert stored.raw_output_path is None
    assert stored.artifact_error is not None
    call = read_model_call(conn, stored.record.record_id)
    assert call == ModelCall(raw_output_path=None, cost_usd=0.25, model_invocations=1)
    # And the pre-existing file is untouched: an artifact is never overwritten.
    assert (artifacts / relative).read_text(encoding="utf-8") == "an artifact already here"


@_ROOT_ONLY
def test_an_unwritable_artifact_root_still_records_the_forecast(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    config = _config(tmp_path, artifacts)
    artifacts.chmod(0o500)
    try:
        stored = _persist(conn, config)
    finally:
        artifacts.chmod(0o700)
    assert stored.record is not None
    assert stored.artifact_outcome == "failed"


def test_retention_disabled_is_reported_distinctly_from_a_failure(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """Both leave `raw_output_path` NULL, and an auditor cannot otherwise tell "the operator
    asked us not to keep it" from "we tried and lost it"."""
    config = _config(tmp_path, artifacts, storage__retain_raw_model_output=False)
    stored = _persist(conn, config)
    assert stored.artifact_outcome == "retention_disabled"
    assert stored.artifact_error is None
    assert list(artifacts.rglob("*.json")) == []


def test_a_persistence_result_cannot_hide_an_audit_loss() -> None:
    with pytest.raises(ForecastRecordError):
        GenerationPersistence(
            record=None,
            raw_output_path="forecast/1/a.json",
            artifact_outcome="failed",
            artifact_error="lost",
        )
    with pytest.raises(ForecastRecordError):
        GenerationPersistence(
            record=None, raw_output_path=None, artifact_outcome="written", artifact_error=None
        )
    with pytest.raises(ForecastRecordError):
        GenerationPersistence(
            record=None,
            raw_output_path=None,
            artifact_outcome="retention_disabled",
            artifact_error="lost",
        )


def test_a_failed_generation_keeps_its_evidence_and_gets_no_row(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """The whole reason the artifact is keyed on `attempt_id` rather than `record_id`."""
    config = _config(tmp_path, artifacts)
    failed = _generation(
        forecast=None,
        raw_responses=("not json at all",),
        failure_code="malformed_response",
        failure_problems=("the reply was not a single JSON object",),
    )
    stored = persist_raw_output(
        config,
        attempt_id="attempt-failed",
        question_id=QUESTION_ID,
        generation=failed,
        written_at=WRITTEN_AT,
    )
    assert stored.record is None
    assert stored.artifact_outcome == "written"
    assert stored.raw_output_path is not None
    assert (artifacts / stored.raw_output_path).is_file()
    assert conn.execute("SELECT count(*) FROM forecast_records").fetchone()[0] == 0


def test_the_two_persistence_entry_points_refuse_each_others_generations(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """They differ in whether a row is appended, which is not a thing to get wrong by
    picking the wrong function."""
    config = _config(tmp_path, artifacts)
    with pytest.raises(ForecastRecordError):
        persist_raw_output(
            config,
            attempt_id=ATTEMPT,
            question_id=QUESTION_ID,
            generation=_generation(),
            written_at=WRITTEN_AT,
        )
    failed = _generation(forecast=None, failure_code="malformed_response", failure_problems=("no",))
    with pytest.raises(ForecastRecordError):
        persist_generation(
            conn, config, draft=_draft(_generation()), generation=failed, written_at=WRITTEN_AT
        )


# --------------------------------------------------------------------------------------
# The ModelCall columns and 008's clauses
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("raw_output_path", "/absolute/path.json"),
        ("raw_output_path", "forecast/../../escape.json"),
        ("raw_output_path", "   "),
        ("raw_output_path", "x" * 201),
        ("raw_output_path", 7),
        ("cost_usd", -0.01),
        ("cost_usd", float("nan")),
        ("cost_usd", float("inf")),
        ("cost_usd", True),
        ("cost_usd", "free"),
        ("model_invocations", 0),
        ("model_invocations", 3),
        ("model_invocations", True),
        ("model_invocations", 1.0),
    ],
)
def test_the_model_call_refuses_what_the_schema_refuses(field: str, value: object) -> None:
    with pytest.raises(ForecastRecordError):
        ModelCall(**{field: value})  # type: ignore[arg-type]


def test_the_model_call_accepts_every_shape_the_writer_produces() -> None:
    """The positive control: the parametrization above proves nothing if the valid shapes
    are refused too."""
    assert ModelCall() == ModelCall(None, None, None)
    assert ModelCall(raw_output_path="forecast/1/a.json", cost_usd=0, model_invocations=1)
    assert ModelCall(cost_usd=0.0, model_invocations=2)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("raw_output_path", "/absolute/path.json"),
        ("raw_output_path", "forecast/../escape.json"),
        ("raw_output_path", ""),
        ("raw_output_path", "x" * 201),
        ("cost_usd", -1.0),
        ("model_invocations", 0),
        ("model_invocations", 3),
        ("model_invocations", "two"),
    ],
)
def test_migration_008_refuses_the_row_by_direct_sql(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path, column: str, value: object
) -> None:
    """Reached by **direct SQL**, following `test_forecast_store.py`'s rule for `007`: going
    through the writer would test the writer's refusals a second time, and the schema exists
    precisely for the writer that does not exist yet -- a fixture, a repair script, or the
    second writer nobody has written."""
    config = _config(tmp_path, artifacts)
    stored = _persist(conn, config)
    assert stored.record is not None
    row = dict(
        zip(
            [d[0] for d in conn.execute("SELECT * FROM forecast_records LIMIT 0").description],
            conn.execute("SELECT * FROM forecast_records").fetchone(),
            strict=True,
        )
    )
    row["record_id"] = "another-record"
    row["forecast_version"] = 2
    row["parent_record_id"] = stored.record.record_id
    row["attempt_id"] = "attempt-2"
    row[column] = value
    columns = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            conn.execute(f"INSERT INTO forecast_records ({columns}) VALUES ({placeholders})", row)


def test_migration_008_admits_the_row_the_writer_produces(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """The positive control for the parametrization above. Without it, a trigger clause that
    refused *every* insert would make all eight cases pass."""
    config = _config(tmp_path, artifacts)
    stored = _persist(conn, config)
    assert stored.record is not None
    assert read_model_call(conn, stored.record.record_id).model_invocations == 1


def test_a_pre_008_row_reads_back_with_all_three_columns_null(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    """`ADD COLUMN` cannot add NOT NULL without a default, so an upgraded ledger holds NULLs
    here -- and every `008` clause permits them, by construction."""
    config = _config(tmp_path, artifacts)
    stored = _persist(conn, config, _generation(cost_usd=None))
    assert stored.record is not None
    assert read_model_call(conn, stored.record.record_id) == ModelCall(
        raw_output_path=artifact_relative_path(question_id=QUESTION_ID, attempt_id=ATTEMPT),
        cost_usd=None,
        model_invocations=1,
    )


# --------------------------------------------------------------------------------------
# Hygiene
# --------------------------------------------------------------------------------------


def test_no_refusal_echoes_the_content_it_refused(
    conn: sqlite3.Connection, tmp_path: Path, artifacts: Path
) -> None:
    config = _config(tmp_path, artifacts)
    stored = _persist(conn, config)
    assert stored.record is not None and stored.raw_output_path is not None

    path = artifacts / stored.raw_output_path
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["attempt_id"] = "attempt-2"
    envelope["raw_responses"] = [PLANTED]
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ForecastRecordError) as caught:
        replay_forecast(conn, config, record_id=stored.record.record_id)
    assert not _leaks(caught.value)

    with pytest.raises(ForecastRecordError) as caught:
        replay_forecast(conn, config, record_id=PLANTED)
    assert not _leaks(caught.value)

    with pytest.raises(ForecastRecordError) as caught:
        ModelCall(raw_output_path=f"/{PLANTED}.json")
    assert not _leaks(caught.value)


def test_the_planted_value_would_be_visible_if_it_leaked() -> None:
    assert _leaks(ForecastRecordError(f"stored value was {PLANTED}"))
    assert _leaks(ArtifactError(f"model reply was {PLANTED}"))
