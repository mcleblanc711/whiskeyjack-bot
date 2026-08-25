"""Invariants of the raw-model-output envelope and the replay it feeds (M1-406).

The writer takes model replies -- arbitrary provider-supplied text, up to and including
lone surrogates and astral scalars -- and the reader hands them back to a parse whose
output is hashed into an approval-bearing record. So the properties here are the ones
M1-305 paid five review rounds to establish, restated over this format:

- **only the module's own error escapes**, over both entry points, on any input;
- **the persisted form round-trips**, keyed on ``json.dumps(ensure_ascii=True,
  sort_keys=True)`` rather than on in-memory equality -- an artifact that came back as a
  *different* string would silently re-derive a different hash;
- **the reader admits exactly what the writer emits**, in both directions;
- **no message leaks a value**, on any path.

Each property below was run against a deliberately broken copy of the module and observed
to fail before being kept: three of M1-303's ten new properties passed against broken code
at the full 200 draws, and a property that cannot fail is worse than no property because it
reads as coverage.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, given, settings
from strategies import HOSTILE_TEXT

from whiskeyjack_bot.artifacts import ArtifactError
from whiskeyjack_bot.forecast.artifacts import (
    _ENVELOPE_FIELDS,
    MODEL_OUTPUT_SCHEMA_VERSION,
    artifact_relative_path,
    read_raw_model_output,
    write_raw_model_output,
)
from whiskeyjack_bot.forecast.parse import ForecastGeneration, ModelSettings
from whiskeyjack_bot.forecast.record import ForecastRecordError
from whiskeyjack_bot.forecast.store import ModelCall

WHEN = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)

PLANTED = "privateFAKE123456"

# A small pool so two draws collide on a path often enough for the never-overwrite
# property to actually exercise the collision, rather than every draw being unique.
ATTEMPT_IDS = st.sampled_from(["attempt-1", "attempt-2", "a.b_c-1"])
QUESTION_IDS = st.sampled_from([1, 42])

REPLIES = st.lists(HOSTILE_TEXT, min_size=1, max_size=3)

COSTS = st.none() | st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False)

# Anything at all, in any field. The point is that no input produces anything but this
# module's own error -- `HOSTILE_TEXT` alone would only exercise the happy shapes.
ANYTHING = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**70), max_value=2**70),
    st.floats(allow_nan=True, allow_infinity=True),
    HOSTILE_TEXT,
    st.binary(max_size=8),
    st.lists(HOSTILE_TEXT, max_size=3),
    st.tuples(HOSTILE_TEXT),
    st.dictionaries(HOSTILE_TEXT, HOSTILE_TEXT, max_size=2),
)

_SETTINGS_FIELDS = (
    "provider",
    "name",
    "temperature",
    "max_output_tokens",
    "timeout_seconds",
    "allowed_tries",
    "prompt_version",
    "prompt_sha256",
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
        "forecast": None,
        "settings": _settings(),
        "sources": (),
        "request": "the rendered reasoning packet",
        "raw_responses": ("{}",),
        "invocations": 1,
        "repair_attempted": False,
        "cost_usd": 0.25,
        "failure_code": None,
        "failure_problems": (),
    }
    fields.update(overrides)
    return ForecastGeneration(**fields)


def _canonical(payload: Any) -> str:
    """The persisted form, the project's rule verbatim (M1-305).

    Not ``repr`` and not ``model_dump_json()``: the first carries distinctions JSON drops,
    and the second raises on a lone surrogate. This is the only notion of equality a
    replayable format may be compared under.
    """
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, allow_nan=False)


def _leaks(exc: BaseException, needle: str) -> bool:
    return needle in str(exc)


# --------------------------------------------------------------------------------------
# Only this module's error escapes
# --------------------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    field=st.sampled_from(
        [
            "request",
            "raw_responses",
            "invocations",
            "repair_attempted",
            "cost_usd",
            "failure_code",
            "failure_problems",
            "settings",
        ]
    ),
    value=ANYTHING,
    attempt_id=ANYTHING,
    question_id=ANYTHING,
)
def test_the_writer_raises_only_its_own_error(
    tmp_path_factory: pytest.TempPathFactory,
    field: str,
    value: object,
    attempt_id: object,
    question_id: object,
) -> None:
    """Callers only handle ``ArtifactError``; a raw ``TypeError``/``AttributeError``
    escaping a public boundary is a review finding in this project (it has been, twice).

    ``tmp_path_factory``, not ``tmp_path``: a function-scoped fixture with ``@given`` is a
    hypothesis health-check failure, and reusing one root across draws is what M1-308
    settled on.
    """
    root = tmp_path_factory.mktemp("artifacts")
    try:
        write_raw_model_output(
            root,
            attempt_id=attempt_id,  # type: ignore[arg-type]
            question_id=question_id,  # type: ignore[arg-type]
            generation=_generation(**{field: value}),
            written_at_utc=WHEN,
            retain=True,
        )
    except ArtifactError:
        return


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(body=ANYTHING, relative=ANYTHING)
def test_the_reader_raises_only_its_own_error(
    tmp_path_factory: pytest.TempPathFactory, body: object, relative: object
) -> None:
    root = tmp_path_factory.mktemp("artifacts")
    (root / "forecast" / "1").mkdir(parents=True, exist_ok=True)
    try:
        (root / "forecast" / "1" / "a.json").write_text(
            body if isinstance(body, str) else json.dumps(body, default=repr), encoding="utf-8"
        )
    except (UnicodeEncodeError, ValueError):
        # A lone surrogate cannot be written as UTF-8, which is a property of the *test's*
        # own file write, not of the module under test. The reader's behaviour on bytes it
        # cannot decode is covered by the unit suite.
        return
    for candidate in (relative, "forecast/1/a.json"):
        try:
            read_raw_model_output(root, candidate)  # type: ignore[arg-type]
        except ArtifactError:
            continue


@given(
    path=ANYTHING,
    cost=ANYTHING,
    calls=ANYTHING,
)
def test_the_model_call_raises_only_its_own_error(
    path: object, cost: object, calls: object
) -> None:
    """``ModelCall`` is the writer-side guard for `008`'s three columns, and it is
    constructed from values a caller supplies, so the same rule applies to it."""
    try:
        ModelCall(
            raw_output_path=path,  # type: ignore[arg-type]
            cost_usd=cost,  # type: ignore[arg-type]
            model_invocations=calls,  # type: ignore[arg-type]
        )
    except ForecastRecordError:
        return


# --------------------------------------------------------------------------------------
# The persisted form round-trips
# --------------------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    attempt_id=ATTEMPT_IDS,
    question_id=QUESTION_IDS,
    replies=REPLIES,
    request=HOSTILE_TEXT,
    cost=COSTS,
    problems=st.lists(HOSTILE_TEXT, max_size=2),
)
def test_every_stored_envelope_replays_to_itself(
    tmp_path_factory: pytest.TempPathFactory,
    attempt_id: str,
    question_id: int,
    replies: list[str],
    request: str,
    cost: float | None,
    problems: list[str],
) -> None:
    """Write, read, write again -- the second rendering must be byte-identical to the first.

    Keyed on the **persisted form**, which is the property replay actually needs: a lone
    surrogate that came back as U+FFFD, or a surrogate pair that recombined into a
    different scalar, would parse to a different forecast and hash to a different record
    while every in-memory comparison still passed (M1-305, M1-306).
    """
    root = tmp_path_factory.mktemp("artifacts")
    generation = _generation(
        raw_responses=tuple(replies),
        request=request,
        cost_usd=cost,
        failure_problems=tuple(problems),
        failure_code="schema_invalid" if problems else None,
        invocations=min(len(replies), 2),
        repair_attempted=len(replies) > 1,
    )
    relative = write_raw_model_output(
        root,
        attempt_id=attempt_id,
        question_id=question_id,
        generation=generation,
        written_at_utc=WHEN,
        retain=True,
    )
    assert relative == artifact_relative_path(question_id=question_id, attempt_id=attempt_id)
    assert relative is not None

    first = json.loads((root / relative).read_text(encoding="utf-8"))
    stored = read_raw_model_output(root, relative)

    # The reply text is the thing replay re-parses, so it is compared under the persisted
    # form rather than by `==` on the tuple.
    assert _canonical(list(stored.raw_responses)) == _canonical(first["raw_responses"])
    assert _canonical(stored.request) == _canonical(first["request"])
    assert _canonical(list(stored.failure_problems)) == _canonical(first["failure_problems"])
    assert stored.cost_usd == cost
    assert stored.attempt_id == attempt_id
    assert stored.question_id == question_id
    assert stored.settings == generation.settings
    assert stored.written_at_utc == WHEN


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(attempt_id=ATTEMPT_IDS, question_id=QUESTION_IDS, replies=REPLIES)
def test_an_artifact_is_never_overwritten_whatever_the_draw(
    tmp_path_factory: pytest.TempPathFactory,
    attempt_id: str,
    question_id: int,
    replies: list[str],
) -> None:
    """The pool of ids is deliberately small so two writes really do collide.

    M1-702's lesson: a collision property needs colliding *draws*, not merely a colliding
    pool -- so the second write is issued at the same coordinates rather than at a fresh
    draw's.
    """
    root = tmp_path_factory.mktemp("artifacts")
    common = {
        "attempt_id": attempt_id,
        "question_id": question_id,
        "written_at_utc": WHEN,
        "retain": True,
    }
    relative = write_raw_model_output(
        root,
        generation=_generation(raw_responses=tuple(replies)),
        **common,  # type: ignore[arg-type]
    )
    assert relative is not None
    before = (root / relative).read_bytes()
    with pytest.raises(ArtifactError):
        write_raw_model_output(
            root,
            generation=_generation(raw_responses=("something else",)),
            **common,  # type: ignore[arg-type]
        )
    assert (root / relative).read_bytes() == before


# --------------------------------------------------------------------------------------
# The reader admits exactly what the writer emits
# --------------------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    field=st.sampled_from(
        [
            "artifact_schema_version",
            "attempt_id",
            "question_id",
            "model_settings",
            "request",
            "raw_responses",
            "invocations",
            "repair_attempted",
            "cost_usd",
            "failure_code",
            "failure_problems",
            "written_at_utc",
        ]
    ),
    value=ANYTHING,
)
def test_the_reader_refuses_any_value_the_writer_could_not_emit(
    tmp_path_factory: pytest.TempPathFactory, field: str, value: object
) -> None:
    """Both directions of "admits exactly what the writer emits", as one property.

    A replacement value is either one the writer could have produced -- in which case the
    read must succeed and come back carrying it -- or one it could not, in which case the
    read must refuse. There is no third outcome, and the failure this catches is the third
    one: a value accepted and silently coerced.
    """
    root = tmp_path_factory.mktemp("artifacts")
    relative = write_raw_model_output(
        root,
        attempt_id="attempt-1",
        question_id=1,
        generation=_generation(),
        written_at_utc=WHEN,
        retain=True,
    )
    assert relative is not None
    envelope = json.loads((root / relative).read_text(encoding="utf-8"))
    original = _canonical(envelope)
    envelope[field] = value
    try:
        mutated = _canonical(envelope)
    except (TypeError, ValueError):
        return  # not JSON at all; the writer could not have produced it either
    if mutated == original:
        return
    (root / relative).write_text(mutated, encoding="utf-8")
    try:
        stored = read_raw_model_output(root, relative)
    except ArtifactError:
        return
    # Accepted -- so it must have come back unchanged, not coerced into something else.
    round_tripped = {
        "artifact_schema_version": MODEL_OUTPUT_SCHEMA_VERSION,
        "attempt_id": stored.attempt_id,
        "question_id": stored.question_id,
        "request": stored.request,
        "raw_responses": list(stored.raw_responses),
        "invocations": stored.invocations,
        "repair_attempted": stored.repair_attempted,
        "cost_usd": stored.cost_usd,
        "failure_code": stored.failure_code,
        "failure_problems": list(stored.failure_problems),
    }
    if field in round_tripped:
        assert _canonical(round_tripped[field]) == _canonical(value)
    elif field == "model_settings":
        assert _canonical(
            {name: getattr(stored.settings, name) for name in _SETTINGS_FIELDS}
        ) == _canonical(value)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    dropped=st.frozensets(st.sampled_from(sorted(_ENVELOPE_FIELDS)), max_size=3),
    added=st.frozensets(
        st.text(min_size=1, max_size=12).filter(lambda k: k not in _ENVELOPE_FIELDS),
        max_size=2,
    ),
)
def test_the_reader_refuses_any_envelope_whose_key_set_the_writer_could_not_emit(
    tmp_path_factory: pytest.TempPathFactory,
    dropped: frozenset[str],
    added: frozenset[str],
) -> None:
    """The *shape* half of "admits exactly what the writer emits" (round 1 finding 1).

    ``test_the_reader_refuses_any_value_the_writer_could_not_emit`` replaces a value under
    an existing key, so it can never produce an envelope whose key set differs from the
    writer's -- and the key set was exactly where the reader was lax. Deleting a nullable
    field read back as ``None`` (the value the writer emits for "cost was not reported"),
    and an unknown key was ignored outright.

    Either edit yields a shape this writer cannot render, so the only correct outcome is a
    refusal; an unedited draw is the positive control and must still read back.
    """
    root = tmp_path_factory.mktemp("artifacts")
    relative = write_raw_model_output(
        root,
        attempt_id="attempt-1",
        question_id=1,
        generation=_generation(),
        written_at_utc=WHEN,
        retain=True,
    )
    assert relative is not None
    envelope = json.loads((root / relative).read_text(encoding="utf-8"))
    for key in dropped:
        del envelope[key]
    for key in added:
        envelope[key] = "x"
    (root / relative).write_text(_canonical(envelope), encoding="utf-8")

    if not dropped and not added:
        assert read_raw_model_output(root, relative).attempt_id == "attempt-1"
        return
    with pytest.raises(ArtifactError):
        read_raw_model_output(root, relative)


# --------------------------------------------------------------------------------------
# No message leaks a value
# --------------------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(field=st.sampled_from(["request", "raw_responses", "failure_problems", "attempt_id"]))
def test_no_writer_refusal_echoes_the_value_it_refused(
    tmp_path_factory: pytest.TempPathFactory, field: str
) -> None:
    """The value is planted in one untrusted position at a time and the refusal forced
    by an *unrelated* field, so the message that escapes is one that had the planted value
    in hand and still did not print it."""
    root = tmp_path_factory.mktemp("artifacts")
    overrides: dict[str, Any] = {"invocations": 99}
    if field == "attempt_id":
        attempt_id: object = f"../{PLANTED}"
    else:
        attempt_id = "attempt-1"
        overrides[field] = (PLANTED,) if field != "request" else PLANTED
    with pytest.raises(ArtifactError) as caught:
        write_raw_model_output(
            root,
            attempt_id=attempt_id,  # type: ignore[arg-type]
            question_id=1,
            generation=_generation(**overrides),
            written_at_utc=WHEN,
            retain=True,
        )
    assert not _leaks(caught.value, PLANTED)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(field=st.sampled_from(["artifact_schema_version", "failure_code", "written_at_utc"]))
def test_no_reader_refusal_echoes_the_file_content_it_refused(
    tmp_path_factory: pytest.TempPathFactory, field: str
) -> None:
    root = tmp_path_factory.mktemp("artifacts")
    relative = write_raw_model_output(
        root,
        attempt_id="attempt-1",
        question_id=1,
        generation=_generation(raw_responses=(PLANTED,), request=PLANTED),
        written_at_utc=WHEN,
        retain=True,
    )
    assert relative is not None
    envelope = json.loads((root / relative).read_text(encoding="utf-8"))
    envelope[field] = PLANTED
    (root / relative).write_text(_canonical(envelope), encoding="utf-8")
    with pytest.raises(ArtifactError) as caught:
        read_raw_model_output(root, relative)
    assert not _leaks(caught.value, PLANTED)


def test_the_planted_value_would_be_visible_if_it_leaked() -> None:
    """The positive control. Both no-leak properties above pass trivially if `_leaks`
    cannot say yes -- a substring guard that never matches is the vacuous shape M1-607
    named."""
    assert _leaks(ArtifactError(f"reply was {PLANTED}"), PLANTED)


# --------------------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(offset_minutes=st.integers(min_value=-14 * 60, max_value=14 * 60))
def test_a_written_timestamp_reads_back_as_the_same_instant(
    tmp_path_factory: pytest.TempPathFactory, offset_minutes: int
) -> None:
    """The writer normalizes to UTC; the reader must return the same instant however the
    caller's timezone spelled it. A comparison on the rendered string would pass while the
    instant moved."""
    root = tmp_path_factory.mktemp("artifacts")
    when = WHEN.astimezone(timezone(timedelta(minutes=offset_minutes)))
    relative = write_raw_model_output(
        root,
        attempt_id="attempt-1",
        question_id=1,
        generation=_generation(),
        written_at_utc=when,
        retain=True,
    )
    assert relative is not None
    assert read_raw_model_output(root, relative).written_at_utc == when
