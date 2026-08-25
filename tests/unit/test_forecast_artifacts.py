"""The raw-model-output envelope on disk (M1-406).

Two properties carry most of these tests, and both are M1-306's, learned there at the cost
of a review round:

- **the reader admits exactly what the writer can emit.** A reader that accepts more is not
  reading the format it documents (round 1, finding 7), and every field it waves through is
  a way for an unattributable blob to be reported as evidence.
- **an artifact is never overwritten**, and that is enforced by ``os.link`` rather than by a
  check that can be raced.

Failures are driven by **real** conditions wherever one exists -- a destination that already
exists, an unwritable directory, an attempt id the layout refuses -- rather than by
monkeypatching, following ``test_research_persist.py``.
"""

from __future__ import annotations

import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from whiskeyjack_bot.artifacts import ArtifactError
from whiskeyjack_bot.forecast.artifacts import (
    MODEL_OUTPUT_SCHEMA_VERSION,
    artifact_relative_path,
    read_raw_model_output,
    write_raw_model_output,
)
from whiskeyjack_bot.forecast.parse import ForecastGeneration, ModelSettings

WHEN = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
QUESTION = 42
ATTEMPT = "attempt-1"

# Low-entropy on purpose: a realistic-looking key would trip the repository's gitleaks
# full-history scan on every unrelated PR (docs/LESSONS.md).
PLANTED = "privateFAKE123456"

_ROOT_ONLY = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="an unwritable directory does not stop root",
)


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    root.mkdir()
    return root


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
        "forecast": None,
        "settings": _settings(),
        "sources": (),
        "request": "the rendered reasoning packet",
        "raw_responses": ('{"probability": 0.5}',),
        "invocations": 1,
        "repair_attempted": False,
        "cost_usd": 0.25,
        "failure_code": None,
        "failure_problems": (),
    }
    fields.update(overrides)
    return ForecastGeneration(**fields)


def _write(artifacts: Path, retain: bool = True, **overrides: Any) -> str | None:
    return write_raw_model_output(
        artifacts,
        attempt_id=overrides.pop("attempt_id", ATTEMPT),
        question_id=overrides.pop("question_id", QUESTION),
        generation=_generation(**overrides),
        written_at_utc=WHEN,
        retain=retain,
    )


def _envelope(artifacts: Path, relative: str) -> dict[str, Any]:
    loaded = json.loads((artifacts / relative).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _rewrite(artifacts: Path, relative: str, envelope: dict[str, Any]) -> None:
    (artifacts / relative).write_text(json.dumps(envelope), encoding="utf-8")


def _leaks(exc: BaseException) -> bool:
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return PLANTED in str(exc) or PLANTED in rendered


# --------------------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------------------


def test_a_written_artifact_reads_back_field_for_field(artifacts: Path) -> None:
    relative = _write(
        artifacts, raw_responses=("first", "second"), invocations=2, repair_attempted=True
    )
    assert relative == artifact_relative_path(question_id=QUESTION, attempt_id=ATTEMPT)
    assert relative is not None

    stored = read_raw_model_output(artifacts, relative)
    assert stored.attempt_id == ATTEMPT
    assert stored.question_id == QUESTION
    assert stored.settings == _settings()
    assert stored.request == "the rendered reasoning packet"
    assert stored.raw_responses == ("first", "second")
    assert stored.invocations == 2
    assert stored.repair_attempted is True
    assert stored.cost_usd == 0.25
    assert stored.failure_code is None
    assert stored.failure_problems == ()
    assert stored.written_at_utc == WHEN


def test_a_failed_generation_is_a_storable_artifact(artifacts: Path) -> None:
    """The whole point of keying on `attempt_id`: a call that bought nothing usable still
    bought something, and its text is the only evidence of what the model said."""
    relative = _write(
        artifacts,
        raw_responses=("not json at all",),
        failure_code="malformed_response",
        failure_problems=("the reply was not a single JSON object",),
        cost_usd=None,
    )
    assert relative is not None
    stored = read_raw_model_output(artifacts, relative)
    assert stored.failure_code == "malformed_response"
    assert stored.failure_problems == ("the reply was not a single JSON object",)
    # None means unknown, not free (M1-303) -- and it survives the round trip as None
    # rather than collapsing to 0.0, which would read as a free call.
    assert stored.cost_usd is None


def test_a_lone_surrogate_in_a_model_reply_round_trips(artifacts: Path) -> None:
    """`ensure_ascii=True` is load-bearing, not tidy.

    A lone surrogate reaches provider text, `str.encode("utf-8")` raises on one, and
    `hashing.content_sha256` still does (the open defect in CLAUDE.md's gotchas). Escaping
    it is what lets the same characters come back -- and M1-306 found the harder half:
    a surrogate *pair* round-trips into a **different** string, so the assertion is on
    equality of the whole reply, not on the write succeeding.
    """
    reply = "before \ud800 after"
    relative = _write(artifacts, raw_responses=(reply,))
    assert relative is not None
    assert read_raw_model_output(artifacts, relative).raw_responses == (reply,)


def test_retention_off_writes_nothing_and_records_nothing(artifacts: Path) -> None:
    assert _write(artifacts, retain=False) is None
    assert list(artifacts.rglob("*.json")) == []


# --------------------------------------------------------------------------------------
# Never overwritten
# --------------------------------------------------------------------------------------


def test_an_artifact_is_never_overwritten(artifacts: Path) -> None:
    relative = _write(artifacts)
    assert relative is not None
    original = (artifacts / relative).read_bytes()
    with pytest.raises(ArtifactError):
        _write(artifacts, raw_responses=("a different reply",))
    assert (artifacts / relative).read_bytes() == original


def test_a_refused_write_leaves_no_temp_file_behind(artifacts: Path) -> None:
    relative = _write(artifacts)
    assert relative is not None
    with pytest.raises(ArtifactError):
        _write(artifacts, raw_responses=("a different reply",))
    directory = (artifacts / relative).parent
    assert sorted(entry.name for entry in directory.iterdir()) == [f"{ATTEMPT}.json"]


@_ROOT_ONLY
def test_an_unwritable_directory_is_this_modules_error(artifacts: Path) -> None:
    (artifacts / "forecast").mkdir()
    (artifacts / "forecast").chmod(0o500)
    try:
        with pytest.raises(ArtifactError):
            _write(artifacts)
    finally:
        (artifacts / "forecast").chmod(0o700)


# --------------------------------------------------------------------------------------
# The writer's refusals
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attempt_id",
    ["", "../escape", "a/b", "with space", ".leading-dot", "x" * 129, 7, None],
)
def test_an_attempt_id_that_is_not_one_path_component_is_refused(
    artifacts: Path, attempt_id: object
) -> None:
    with pytest.raises(ArtifactError):
        write_raw_model_output(
            artifacts,
            attempt_id=attempt_id,  # type: ignore[arg-type]
            question_id=QUESTION,
            generation=_generation(),
            written_at_utc=WHEN,
            retain=True,
        )
    assert list(artifacts.rglob("*.json")) == []


def test_a_bare_string_of_replies_is_refused_rather_than_stored_per_character(
    artifacts: Path,
) -> None:
    """`str` satisfies `Sequence[str]`, so mypy cannot catch this one.

    M1-303 round 4 is the precedent, where the same shape cost six billable calls; here it
    would write one 'reply' per character into the permanent record of what the model said.
    """
    with pytest.raises(ArtifactError):
        _write(artifacts, raw_responses="one reply")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("invocations", 3),
        ("invocations", -1),
        ("invocations", "1"),
        ("repair_attempted", 1),
        ("cost_usd", float("nan")),
        ("cost_usd", float("inf")),
        ("cost_usd", -0.5),
        ("failure_code", "not_a_known_code"),
        ("failure_problems", "a problem"),
        ("raw_responses", (b"bytes",)),
        ("request", 7),
    ],
)
def test_the_writer_refuses_a_shape_it_cannot_honestly_store(
    artifacts: Path, field: str, value: object
) -> None:
    with pytest.raises(ArtifactError):
        _write(artifacts, **{field: value})
    assert list(artifacts.rglob("*.json")) == []


def test_the_writer_refuses_settings_it_cannot_attribute(artifacts: Path) -> None:
    """Settings are this envelope's provenance: they say which model, at which temperature,
    under which prompt, produced the text below them. An envelope without them is a blob."""
    with pytest.raises(ArtifactError):
        _write(artifacts, settings=None)
    with pytest.raises(ArtifactError):
        _write(artifacts, settings=_settings(provider="   "))


def test_a_naive_timestamp_is_refused(artifacts: Path) -> None:
    with pytest.raises(ArtifactError):
        write_raw_model_output(
            artifacts,
            attempt_id=ATTEMPT,
            question_id=QUESTION,
            generation=_generation(),
            written_at_utc=datetime(2026, 8, 24, 12, 0),
            retain=True,
        )


# --------------------------------------------------------------------------------------
# The reader admits exactly what the writer emits
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "artifact_schema_version",
        "attempt_id",
        "question_id",
        "model_settings",
        "request",
        "raw_responses",
        "invocations",
        "repair_attempted",
        "failure_problems",
        "written_at_utc",
    ],
)
def test_the_reader_refuses_an_envelope_missing_any_required_field(
    artifacts: Path, field: str
) -> None:
    """M1-306 round 1, finding 7, as a regression on this envelope.

    The first cut of the retrieval reader checked the version and the bodies and returned,
    which made an artifact carrying neither run id, question, provider nor timestamp a
    valid one. Every field here is required, and the test is parametrized off the field
    list rather than sampling it so that adding a field without validating it fails.
    """
    relative = _write(artifacts)
    assert relative is not None
    envelope = _envelope(artifacts, relative)
    del envelope[field]
    _rewrite(artifacts, relative, envelope)
    with pytest.raises(ArtifactError):
        read_raw_model_output(artifacts, relative)


def test_the_reader_would_notice_a_vacuous_parametrization(artifacts: Path) -> None:
    """The positive control for the test above.

    A negative test needs one (M1-602's lesson: three vacuous tests were found that way).
    If the untouched envelope did not read back, every case above would pass for the wrong
    reason.
    """
    relative = _write(artifacts)
    assert relative is not None
    _rewrite(artifacts, relative, _envelope(artifacts, relative))
    assert read_raw_model_output(artifacts, relative).attempt_id == ATTEMPT


def test_the_reader_refuses_extra_settings_fields(artifacts: Path) -> None:
    """Set equality, not a subset check.

    An extra key is as much a shape this writer cannot emit as a missing one, and reading
    it back as valid would hide exactly the version skew the schema version exists to make
    visible -- M1-501's lesson about one-sided assertions, applied to a key set.
    """
    relative = _write(artifacts)
    assert relative is not None
    envelope = _envelope(artifacts, relative)
    envelope["model_settings"]["reasoning_effort"] = "high"
    _rewrite(artifacts, relative, envelope)
    with pytest.raises(ArtifactError):
        read_raw_model_output(artifacts, relative)


def test_the_reader_refuses_another_schema_version(artifacts: Path) -> None:
    relative = _write(artifacts)
    assert relative is not None
    envelope = _envelope(artifacts, relative)
    envelope["artifact_schema_version"] = "2.0.0"
    _rewrite(artifacts, relative, envelope)
    with pytest.raises(ArtifactError) as caught:
        read_raw_model_output(artifacts, relative)
    # The expected version is this module's own literal and is named; the found one is
    # file content and is withheld, exactly as load_snapshot does.
    assert MODEL_OUTPUT_SCHEMA_VERSION in str(caught.value)
    assert "2.0.0" not in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("invocations", 99),
        ("invocations", 0.5),
        ("repair_attempted", "yes"),
        ("cost_usd", -1),
        ("failure_code", "not_a_known_code"),
        ("raw_responses", "one reply"),
        ("raw_responses", [1, 2]),
        ("failure_problems", "a problem"),
        ("question_id", "42"),
        ("attempt_id", "../escape"),
        ("written_at_utc", "2026-08-24T12:00:00"),
        ("written_at_utc", "not a timestamp"),
    ],
)
def test_the_reader_refuses_a_value_the_writer_could_not_have_produced(
    artifacts: Path, field: str, value: object
) -> None:
    relative = _write(artifacts)
    assert relative is not None
    envelope = _envelope(artifacts, relative)
    envelope[field] = value
    _rewrite(artifacts, relative, envelope)
    with pytest.raises(ArtifactError):
        read_raw_model_output(artifacts, relative)


def test_the_reader_refuses_a_non_finite_json_constant(artifacts: Path) -> None:
    """`json.loads` accepts NaN/Infinity by default and the writer refuses to emit them.

    Reachable rather than hypothetical: a hand-edited or third-party-written envelope is an
    ordinary local file, and a reader that admits more than its writer can emit would hand
    a Python float back as though it had round-tripped (M1-306 round 1, finding 7).
    """
    relative = _write(artifacts)
    assert relative is not None
    (artifacts / relative).write_text(
        json.dumps(_envelope(artifacts, relative)).replace('"cost_usd": 0.25', '"cost_usd": NaN'),
        encoding="utf-8",
    )
    with pytest.raises(ArtifactError):
        read_raw_model_output(artifacts, relative)


def test_an_unreadable_or_malformed_artifact_is_this_modules_error(artifacts: Path) -> None:
    with pytest.raises(ArtifactError):
        read_raw_model_output(artifacts, "forecast/42/nothing-here.json")
    (artifacts / "forecast" / "42").mkdir(parents=True, exist_ok=True)
    (artifacts / "forecast" / "42" / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ArtifactError):
        read_raw_model_output(artifacts, "forecast/42/bad.json")
    (artifacts / "forecast" / "42" / "list.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ArtifactError):
        read_raw_model_output(artifacts, "forecast/42/list.json")


# --------------------------------------------------------------------------------------
# Hygiene
# --------------------------------------------------------------------------------------


def test_no_refusal_echoes_the_content_it_refused(artifacts: Path) -> None:
    """Every escaping message and traceback, over both entry points.

    The value is planted in each untrusted position in turn -- a model reply, the request,
    a failure problem, an identifier -- because a leak is a property of the *channel*, and
    M1-302's lesson is that covering one channel proves nothing about the others.
    """
    for overrides in (
        {"raw_responses": (PLANTED,) * 3, "invocations": 9},
        {"request": PLANTED, "cost_usd": float("nan")},
        {"failure_problems": (PLANTED,), "failure_code": "nope"},
        {"settings": _settings(name=PLANTED, allowed_tries="two")},
    ):
        with pytest.raises(ArtifactError) as caught:
            _write(artifacts, **overrides)
        assert not _leaks(caught.value)

    with pytest.raises(ArtifactError) as caught:
        write_raw_model_output(
            artifacts,
            attempt_id=f"../{PLANTED}",
            question_id=QUESTION,
            generation=_generation(),
            written_at_utc=WHEN,
            retain=True,
        )
    assert not _leaks(caught.value)

    relative = _write(artifacts, raw_responses=(PLANTED,))
    assert relative is not None
    envelope = _envelope(artifacts, relative)
    envelope["artifact_schema_version"] = PLANTED
    _rewrite(artifacts, relative, envelope)
    with pytest.raises(ArtifactError) as caught:
        read_raw_model_output(artifacts, relative)
    assert not _leaks(caught.value)


def test_the_planted_value_would_be_visible_if_it_leaked() -> None:
    """The positive control for the test above: `_leaks` must be able to say yes."""
    assert _leaks(ArtifactError(f"model reply was {PLANTED}"))
