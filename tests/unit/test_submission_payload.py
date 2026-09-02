"""M2-707: the payload a stored forecast authorizes, and the digest an approval binds to.

The derivation half of D33. `tests/unit/test_submission.py` owns the gate that compares a
payload's digest against the one an approval stored; this file owns the question that gate
could not ask until now -- *what payload does this record derive?*

Three claims are under test, and they are the backlog's own words.

**"A submission payload that does not derive from the approved forecast is refused before
any post."** The last test here is that sentence executed end to end: one record, one
approval bound to the payload it derives, and a second payload of the same forecast that
cannot reach a submission key. Everything above it is what makes that comparison mean
something -- if the derivation were not deterministic, or not the same rendering the
gateway hashes, the gate would be comparing two runs of one function.

**The payload is the Metaculus wire body plus a discriminator, and nothing else.** Each of
the three type tests asserts the whole mapping by equality rather than checking that the
expected keys are present, because a payload carrying a key nobody reviewed is exactly the
failure `plan_from_payload` refuses at the post.

**A refusal is a `PayloadBuildError` and says nothing about the values it refused.** The
module reaches the pinned SDK's CDF conversion, whose own errors interpolate the
percentiles they refused; the last block here plants values and asserts none of them
reaches a message or a rendered traceback.

Fixtures are built out of `prompts/forecaster.md` and the committed `config.example.yaml`,
the `test_forecast_cdf.py` approach and for its reason: a test that builds its own fixture
cannot notice the committed defaults drifting away from what the model is actually told and
actually configured with.
"""

from __future__ import annotations

import copy
import json
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from whiskeyjack_bot.approval import approve
from whiskeyjack_bot.config import (
    NumericCalibrationConfig,
    SupportedQuestionType,
    validate_config_data,
)
from whiskeyjack_bot.forecast.cdf import build_numeric_cdf
from whiskeyjack_bot.forecast.generate import ForecastGeneration, ModelSettings
from whiskeyjack_bot.forecast.inputs import SourceReference
from whiskeyjack_bot.forecast.numeric import DECLARED_PERCENTILE_LEVELS
from whiskeyjack_bot.forecast.record import ForecastRecord, build_forecast_record_draft
from whiskeyjack_bot.forecast.schema import (
    ForecastResponse,
    response_model_for,
    validate_forecast_response,
)
from whiskeyjack_bot.forecast.store import append_forecast_version
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import record_validation
from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalMultipleChoiceQuestion,
    CanonicalNumericQuestion,
    CanonicalQuestion,
)
from whiskeyjack_bot.submission import SubmissionError, submission_key_for_approved_record
from whiskeyjack_bot.submission_gateway import canonical_payload_json, payload_sha256
from whiskeyjack_bot.submission_live import plan_from_payload
from whiskeyjack_bot.submission_payload import (
    BUILDABLE_QUESTION_TYPES,
    AuthorizedPayload,
    PayloadBuildError,
    authorized_payload,
    build_submission_payload,
    payload_sha256_for_record,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")

QUESTION_ID = 123
POST_ID = 456
TOURNAMENT = "minibench"
RUN_ID = "run-1"
RECORD_ID = "rec-1"
TS = "2026-08-22T00:00:00.000000+00:00"
GENERATED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
OCCURRED = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

# Planted into a record's content, so that a message or traceback which echoes a stored
# value has something unmistakable to echo. A percentile cannot carry a string, so the leak
# test plants a distinctive *number* -- one far outside the question's declared range, so
# that the conversion refuses it and the pinned SDK's own `ValidationError` is holding it
# at the moment this module re-raises. See `test_no_refusal_echoes_a_value_it_refused`.
LEAK_VALUE = 12345.6789


# ── fixtures built from the committed prompt and configuration ───────────────


def _committed_config() -> Any:
    """The *committed* config.example.yaml, not a hand-built one."""
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    return validate_config_data(data)


def _calibration(**overrides: Any) -> NumericCalibrationConfig:
    committed = _committed_config().numeric_calibration
    if not overrides:
        return committed
    return committed.model_copy(update=overrides)


CALIBRATION = _calibration()


def _json_block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _response(schema: str, **overrides: Any) -> ForecastResponse:
    """One example response from `prompts/forecaster.md`, validated by the real validator."""
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block(schema) + "}"),
    }
    payload["question_id"] = QUESTION_ID
    if schema != "Binary schema":
        # The shared block's priors are binary-only; every other type refuses them.
        payload["model_prior"] = None
        payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    payload.update(overrides)
    return validate_forecast_response(payload, response_model_for(payload["question_type"]))


def _generation(forecast: ForecastResponse) -> ForecastGeneration:
    return ForecastGeneration(
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
        request="the rendered reasoning packet",
        raw_responses=("{}",),
        invocations=1,
        repair_attempted=False,
        cost_usd=None,
        failure_code=None,
        failure_problems=(),
    )


def _draft(question: CanonicalQuestion, forecast: ForecastResponse, attempt_id: str) -> Any:
    return build_forecast_record_draft(
        question=question,
        generation=_generation(forecast),
        tournament_id=TOURNAMENT,
        attempt_id=attempt_id,
        retrieval_run_id=RUN_ID,
        research_packet_sha256="d" * 64,
        generated_at=GENERATED_AT,
    )


def _record(
    question: CanonicalQuestion, forecast: ForecastResponse, *, attempt_id: str = "attempt-1"
) -> ForecastRecord:
    """A stored record, built through the real draft assembler and given an identity.

    Identity is assigned here rather than by `store.append_forecast_version`, because
    everything except the last test is about a pure function of a record and has no reason
    to open a ledger.
    """
    draft = _draft(question, forecast, attempt_id)
    return ForecastRecord(
        **draft.model_dump(), record_id=RECORD_ID, forecast_version=1, parent_record_id=None
    )


def _binary_question(**overrides: Any) -> CanonicalBinaryQuestion:
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


def _binary_record(**overrides: Any) -> ForecastRecord:
    return _record(_binary_question(), _response("Binary schema", **overrides))


def _multiple_choice_record(**overrides: Any) -> ForecastRecord:
    forecast = _response("Multiple-choice schema", **overrides)
    labels = [entry.option for entry in forecast.final_prediction.options]  # type: ignore[union-attr]
    return _record(_multiple_choice_question(labels), forecast)


def _multiple_choice_question(labels: list[str]) -> CanonicalMultipleChoiceQuestion:
    return CanonicalMultipleChoiceQuestion(
        question_id=QUESTION_ID,
        post_id=POST_ID,
        title="Which one happens?",
        options=labels,
    )


def _numeric_record(question: CanonicalNumericQuestion | None = None, **overrides: Any):
    return _record(
        question if question is not None else _numeric_question(),
        _response("Numeric schema", **overrides),
    )


def _every_type() -> list[ForecastRecord]:
    """One record of each supported type, in the order `SupportedQuestionType` declares."""
    return [_binary_record(), _multiple_choice_record(), _numeric_record()]


# ── what each type derives ───────────────────────────────────────────────────


def test_a_binary_record_derives_the_wire_body_and_nothing_else() -> None:
    """Two keys, and the probability is the record's own -- read back, not written twice.

    Asserted by equality rather than by checking that the two expected keys are present: a
    third key is exactly what `plan_from_payload` refuses at the post, and a builder that
    added one would otherwise pass a subset assertion.
    """
    record = _binary_record()
    payload = build_submission_payload(record, calibration=CALIBRATION)
    assert payload == {
        "question_type": "binary",
        "probability_yes": record.forecast.final_prediction.probability_yes,
    }


def test_a_multiple_choice_record_derives_one_entry_per_option() -> None:
    """Every option the forecast holds, keyed by its exact label.

    The label is the platform's own string and the mapping key is how Metaculus reads it,
    so the assertion is over the question's option list rather than over a literal: a
    builder that lower-cased, stripped or reordered a label would post a distribution
    against options that do not exist.
    """
    record = _multiple_choice_record()
    payload = build_submission_payload(record, calibration=CALIBRATION)
    entries = record.forecast.final_prediction.options
    assert payload == {
        "question_type": "multiple_choice",
        "probability_yes_per_category": {entry.option: entry.probability for entry in entries},
    }
    assert list(payload["probability_yes_per_category"]) == record.question.options


def test_a_numeric_record_derives_the_converted_cdf() -> None:
    """The array is `forecast.cdf`'s output, not a second conversion written here.

    M1-503 owns the conversion and this module owns the payload, so the assertion compares
    against `build_numeric_cdf` rather than against 201 transcribed floats -- a transcript
    would pass against a builder that stopped calling the converter at all.
    """
    record = _numeric_record()
    payload = build_submission_payload(record, calibration=CALIBRATION)
    expected = build_numeric_cdf(record.forecast, CALIBRATION, record.question)
    assert payload == {"question_type": "numeric", "continuous_cdf": list(expected.values)}
    assert len(payload["continuous_cdf"]) == CALIBRATION.expected_cdf_points


def test_every_supported_question_type_has_a_builder() -> None:
    """A fourth type added to `config.py` with no branch here must be a red build.

    Without this, such a type would fall through to `_UNSUPPORTED_TYPE` -- discovered by an
    operator at approval time, on a real record, instead of here.
    """
    assert BUILDABLE_QUESTION_TYPES == frozenset(get_args(SupportedQuestionType))
    for record in _every_type():
        assert record.question_type in BUILDABLE_QUESTION_TYPES


def test_every_derived_payload_is_one_the_live_path_accepts() -> None:
    """The postability claim, stated over all three types rather than argued in prose.

    `plan_from_payload` is the complete account of what Metaculus accepts and it runs
    immediately before the post; if it refused what this module returns, an approval would
    bind to a payload that could never be submitted.
    """
    for record in _every_type():
        payload = build_submission_payload(record, calibration=CALIBRATION)
        plan = plan_from_payload(payload, expected_cdf_points=CALIBRATION.expected_cdf_points)
        assert plan is not None


# ── one derivation, one rendering, one digest ────────────────────────────────


def test_the_payload_the_bytes_and_the_digest_are_the_same_payload() -> None:
    """What an operator is shown, what an approval binds to and what is posted agree.

    The three fields travel together precisely so that no caller renders a second time to
    print one of them. Asserting the relationship here is what makes that guarantee
    checkable from outside the module: the digest is over `canonical`, and `canonical` is
    the gateway's rendering of `payload` -- the same function `submission_key` derives its
    material from, so the approval binds to the bytes the key is built from.
    """
    for record in _every_type():
        authorized = authorized_payload(record, calibration=CALIBRATION)
        assert isinstance(authorized, AuthorizedPayload)
        assert authorized.canonical == canonical_payload_json(authorized.payload)
        assert authorized.sha256 == payload_sha256(authorized.payload)
        assert json.loads(authorized.canonical) == authorized.payload
        assert payload_sha256_for_record(record, calibration=CALIBRATION) == authorized.sha256


def test_the_derivation_is_deterministic_in_the_record_and_the_calibration() -> None:
    """A binding to a value that could come back different is not a binding.

    Two derivations from one record, and a third from a record rebuilt out of its own
    persisted JSON -- the form the digest actually has to survive, since what
    `submission_key_for_approved_record` compares is derived from a row read back out of
    the ledger and not from the object the approval was taken against.
    """
    for record in _every_type():
        first = payload_sha256_for_record(record, calibration=CALIBRATION)
        second = payload_sha256_for_record(record, calibration=CALIBRATION)
        assert first == second
        replayed = ForecastRecord.model_validate(
            json.loads(json.dumps(record.model_dump(mode="json"), sort_keys=True))
        )
        assert payload_sha256_for_record(replayed, calibration=CALIBRATION) == first


def test_changing_the_calibration_changes_the_payload_a_record_derives() -> None:
    """The one non-obvious input, and the reason it fails safe.

    A numeric payload is a *conversion* of the stored percentiles rather than a copy of
    them, so `numeric_calibration` is part of what a record derives. That is deliberate:
    the rebuilt payload hashes differently, the approval stops binding, and the operator is
    asked to approve again rather than posting an array nobody reviewed. Asserted through
    the digest, because the digest is the thing the approval actually holds.

    Binary and multiple-choice are asserted *unchanged* by the same knob. Without that half
    the test would pass against a builder that mixed the calibration into every digest,
    which would strand two approvals for a setting neither payload depends on.
    """
    unstandardized = _calibration(use_forecasting_tools_standardization=False)
    numeric = _numeric_record()
    assert payload_sha256_for_record(
        numeric, calibration=unstandardized
    ) != payload_sha256_for_record(numeric, calibration=CALIBRATION)
    for record in (_binary_record(), _multiple_choice_record()):
        assert payload_sha256_for_record(
            record, calibration=unstandardized
        ) == payload_sha256_for_record(record, calibration=CALIBRATION)


# ── refusals ─────────────────────────────────────────────────────────────────


def test_the_refusal_type_is_one_every_caller_already_handles() -> None:
    """`PayloadBuildError` is a `SubmissionError`, and `cli.py` rests on that.

    Both callers of this module catch `SubmissionError`; a second unrelated exception class
    at the same seam would be a distinction no caller makes, and the CLI would let it
    escape as a traceback.
    """
    assert issubclass(PayloadBuildError, SubmissionError)


@pytest.mark.parametrize(
    "record",
    [None, "rec-1", 3, 0.5, object(), {"question_type": "binary"}],
    ids=["none", "text", "int", "float", "object", "mapping"],
)
def test_a_thing_that_is_not_a_stored_record_arrives_as_this_modules_error(record: object) -> None:
    """The project rule: every malformed shape arrives as the module's own error type.

    A raw `AttributeError` escaping a public boundary has been a review finding twice
    (CLAUDE.md), and this boundary is reached from a CLI command that catches
    `SubmissionError` and nothing else.
    """
    with pytest.raises(PayloadBuildError):
        build_submission_payload(record, calibration=CALIBRATION)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "calibration",
    [None, "identity", 201, object(), {"expected_cdf_points": 201}],
    ids=["none", "text", "int", "object", "mapping"],
)
def test_a_thing_that_is_not_a_calibration_arrives_as_this_modules_error(
    calibration: object,
) -> None:
    """Checked for a binary record, which never reads the calibration to build its payload.

    That is the point: the parameter decides what the *numeric* branch converts, so a
    builder that only validated it on the path that uses it would accept nonsense for two
    of the three types and refuse it for the third.
    """
    with pytest.raises(PayloadBuildError):
        build_submission_payload(_binary_record(), calibration=calibration)  # type: ignore[arg-type]


def test_a_record_whose_stored_response_is_not_its_question_type_is_refused() -> None:
    """A record read back out of the ledger is untrusted, and this is what that buys.

    The record's own validator requires `question_type` to agree across the row, the
    question and the response -- so this shape is reached by rewriting a validated record
    rather than by building an invalid one. "The validator would have caught it" is not
    something a payload builder gets to assume about the value that decides what is posted.
    """
    mismatched = _binary_record().model_copy(update={"forecast": _numeric_record().forecast})
    assert mismatched.question_type == "binary"
    with pytest.raises(PayloadBuildError, match="stored response is not the type"):
        build_submission_payload(mismatched, calibration=CALIBRATION)


def test_a_record_whose_stored_question_is_not_its_question_type_is_refused() -> None:
    """The numeric branch reads the *question* too -- its bounds decide the conversion."""
    mismatched = _numeric_record().model_copy(update={"question": _binary_question()})
    assert mismatched.question_type == "numeric"
    with pytest.raises(PayloadBuildError, match="stored question is not the type"):
        build_submission_payload(mismatched, calibration=CALIBRATION)


def test_a_multiple_choice_option_named_twice_is_refused_rather_than_collapsed() -> None:
    """The wire shape is keyed on the label, so a duplicate would silently drop an option.

    A payload carrying fewer options than the forecast it claims to be is one whose
    probabilities no longer sum to one and whose missing entry an operator would have
    approved without ever seeing. Refused at the point where dropping it is irreversible.
    """
    record = _multiple_choice_record()
    prediction = record.forecast.final_prediction
    duplicated = prediction.model_construct(options=[prediction.options[0], prediction.options[0]])
    forecast = record.forecast.model_construct(
        **{**record.forecast.__dict__, "final_prediction": duplicated}
    )
    with pytest.raises(
        PayloadBuildError, match="names one multiple-choice option twice"
    ) as excinfo:
        build_submission_payload(
            record.model_copy(update={"forecast": forecast}), calibration=CALIBRATION
        )
    assert prediction.options[0].option not in str(excinfo.value)


def test_percentiles_that_do_not_convert_have_no_payload_to_bind_to() -> None:
    """A numeric record can stop deriving a payload, and that is a refusal, not a crash.

    `build_numeric_cdf` raises on a percentile set the SDK cannot turn into a distribution.
    Caught as the parent `ForecastSchemaError` rather than `NumericCdfError` so the branch
    stays total against the package's whole response-failure vocabulary, and re-raised with
    the converter's own sanitized problem strings, which is what makes the refusal
    actionable: the operator learns the percentiles need widening rather than that
    "something failed".
    """
    flat = _numeric_record(
        final_prediction={
            "percentiles": [
                {"percentile": level, "value": 50.0} for level in DECLARED_PERCENTILE_LEVELS
            ]
        }
    )
    with pytest.raises(PayloadBuildError, match="do not convert into a submittable CDF") as excinfo:
        build_submission_payload(flat, calibration=CALIBRATION)
    assert "percentiles" in str(excinfo.value)


def test_a_record_and_a_calibration_that_disagree_are_refused_before_the_approval() -> None:
    """The stored question's `cdf_size` against the configured point count.

    Not a hypothetical: the question travels inside `record_json`, so a record forecast
    under one configuration and approved under another is exactly the disagreement an
    operator needs told about *before* deciding, which is the whole reason the derivation
    happens at approve time rather than at post time.
    """
    with pytest.raises(PayloadBuildError, match="do not convert into a submittable CDF"):
        build_submission_payload(
            _numeric_record(_numeric_question(cdf_size=101)), calibration=CALIBRATION
        )


def test_a_payload_metaculus_would_refuse_is_refused_here_instead() -> None:
    """`_require_postable` is live, and these are the two ways to reach it.

    Both are records the *generation* path would never emit and the *record* model happily
    holds: the response schema admits a probability of 0.0, and the option-sum rule lives
    in `forecast/multiple_choice.py` rather than in the response model. Either would be
    refused at the last gate before the post -- which is a refusal arriving after a human
    decision instead of before it, and moving it is what this function is for.
    """
    extreme = _binary_record(final_prediction={"probability_yes": 0.0})
    with pytest.raises(PayloadBuildError, match="not one Metaculus would accept"):
        build_submission_payload(extreme, calibration=CALIBRATION)

    labels = [entry.option for entry in _multiple_choice_record().forecast.final_prediction.options]
    skewed = _multiple_choice_record(
        final_prediction={
            "options": [
                {"option": labels[0], "probability": 0.5},
                {"option": labels[1], "probability": 0.2},
            ]
        }
    )
    with pytest.raises(PayloadBuildError, match="not one Metaculus would accept"):
        build_submission_payload(skewed, calibration=CALIBRATION)


def test_no_refusal_echoes_a_value_it_refused() -> None:
    """Not in the message and not in a rendered traceback.

    The traceback half is what `from None` exists for, and it is the half that matters
    here: every failure inside `NumericDistribution` arrives as a pydantic
    `ValidationError` interpolating the percentiles it refused, so a `raise ... from exc`
    would leak the record's content through a printed stack even with a clean message.
    """
    planted = _numeric_record(
        final_prediction={
            "percentiles": [
                {"percentile": level, "value": LEAK_VALUE + index}
                for index, level in enumerate(DECLARED_PERCENTILE_LEVELS)
            ]
        }
    )
    with pytest.raises(PayloadBuildError) as excinfo:
        build_submission_payload(planted, calibration=CALIBRATION)
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert str(LEAK_VALUE) not in str(excinfo.value)
    assert str(LEAK_VALUE) not in rendered
    # The question's own bounds are content too, and the SDK's message names them.
    assert str(planted.question.upper_bound) not in rendered
    # And nothing chains through, which is the `from None` half stated structurally. The
    # search above cannot see it: `forecast/cdf.py` sanitizes its own message first, so a
    # `raise ... from exc` here would still render clean today. What this module is asked to
    # keep is that no underlying exception travels with its refusal at all -- measured, by
    # replacing every `from None` in the module with `from exc` and watching only this line
    # go red.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None or excinfo.value.__suppress_context__


# ── the acceptance criterion, end to end ─────────────────────────────────────


def test_only_the_payload_the_record_derives_can_reach_a_submission_key(tmp_path: Path) -> None:
    """*"A submission payload that does not derive from the approved forecast is refused
    before any post."*

    Through the real writers and the real gate: a record appended by
    `store.append_forecast_version`, approved with the digest this module derives, and then
    asked for a submission key twice -- once for the payload it derives and once for a
    payload of the same forecast that nobody approved. The second is refused *at the key*,
    which is above the reservation and far above the post, so nothing downstream is
    reachable without it.

    The unauthorized payload is a real derivation under a different calibration rather than
    an invented digest, because that is the case D33 was actually about: a payload that
    changed without the forecast changing. Its `forecast_sha256` is identical; only the
    payload differs.
    """
    database = tmp_path / "ledger.sqlite3"
    initialize_ledger(database)
    connection = connect(database)
    try:
        connection.execute(
            "INSERT INTO research_runs (retrieval_run_id, provider, question_id, "
            "started_at_utc, created_at_utc) VALUES (?, 'exa', ?, ?, ?)",
            (RUN_ID, QUESTION_ID, TS, TS),
        )
        stored = append_forecast_version(
            connection, draft=_draft(_numeric_question(), _response("Numeric schema"), "attempt-1")
        )
        record_validation(connection, record_id=stored.record_id, occurred_at=OCCURRED)

        authorized = authorized_payload(stored, calibration=CALIBRATION)
        unauthorized = authorized_payload(
            stored, calibration=_calibration(use_forecasting_tools_standardization=False)
        )
        assert unauthorized.sha256 != authorized.sha256

        approved = approve(
            connection,
            record_id=stored.record_id,
            actor="chris",
            occurred_at=OCCURRED,
            calibration=CALIBRATION,
        )
        # M2-707 round 1: `approve` derived this itself. `authorized` is the independent
        # derivation -- built here, from the record object this test holds, rather than from
        # the row `approve` read back -- so the equality is a claim about the writer's
        # wiring and not a restatement of one call.
        assert approved.decision.payload_sha256 == authorized.sha256
        assert approved.authorized.sha256 == authorized.sha256

        key = submission_key_for_approved_record(
            connection, stored.record_id, request_payload_sha256=authorized.sha256
        )
        assert key
        with pytest.raises(SubmissionError, match="not the one the approval in force authorized"):
            submission_key_for_approved_record(
                connection, stored.record_id, request_payload_sha256=unauthorized.sha256
            )
    finally:
        connection.close()
