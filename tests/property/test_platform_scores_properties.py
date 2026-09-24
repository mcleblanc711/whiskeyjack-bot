"""Property tests for platform score extraction and its ledger round trip (M4-803).

The CLAUDE.md pre-review fuzz pass over the one pure function this item adds,
:func:`platform_scores.extract_platform_scores`, and the writer and trigger behind it:

1. It never raises outside ``PlatformScoreError``. The payload is corrupted at one **level**
   per test (the post, the question, ``my_forecasts``, ``score_data``, one score), chosen by
   ``parametrize`` rather than ``sampled_from`` so every level is reached by construction
   (PR-1's lesson: ``sampled_from`` skews to its first member). ``event()`` records whether
   each draw was accepted or which rule refused it.
2. A well-formed payload is copied **bit for bit**: every finite double, including ``-0.0``,
   subnormals and the extremes, through the persisted form (``canonical_json`` then
   ``json.loads``), and :func:`platform_scores.recompute` agrees with it.
3. Through a real ledger: the writer lands the four rows, 017's exact-value clause admits
   them, and the reader re-reads the same number. The clause relies on SQLite parsing a JSON
   number to the double Python does; this is the property that says so for the SQLite under
   test, over arbitrary finite doubles rather than the 140 live values. "The same number" is
   IEEE equality, and bit-identity for every value but one: SQLite's REAL storage drops the
   sign of a negative zero (``-0.0`` reads back ``0.0``; the property found it), which every
   comparison in the program -- the trigger's ``=``, the reader's ``!=`` -- treats as equal.
4. No refusal reprints a value: string values carry a sentinel, and a float canary sits in the
   payload of every refusal.
"""

from __future__ import annotations

import itertools
import json
import math
import sqlite3
import struct
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st
from resolution_rows import SCORE_DATA, post_payload, seed_submitted

from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    read_platform_scores,
    record_platform_scores,
    record_resolution_observation,
)
from whiskeyjack_bot.platform_scores import (
    IMPLEMENTATION_VERSIONS,
    PLATFORM_METRIC_ORDER,
    SCORE_DATA_KEYS,
    PlatformScoreError,
    extract_platform_scores,
    recompute,
)
from whiskeyjack_bot.resolution import canonical_json

QUESTION_ID = 45747
POST_ID = 45556
# A distinctive double: if any refusal message formats a payload number, this is the one it
# would print, and `repr` is how it would print it.
CANARY = 7.123456789012345e-7
SENTINEL = "SENTINEL-value"

FINITE = st.floats(allow_nan=False, allow_infinity=False)
JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**70), max_value=2**70),
    st.floats(),  # NaN and both infinities included: a stored response is untrusted
    st.text(max_size=8).map(lambda text: SENTINEL + text),
)
JSON_VALUES = st.recursive(
    JSON_SCALARS,
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(st.text(max_size=6), inner, max_size=3),
    ),
    max_leaves=8,
)


def _bits(value: float) -> bytes:
    return struct.pack("<d", value)


def _stored_bits(value: float) -> bytes:
    """What a REAL column gives back: every double unchanged, except ``-0.0`` becomes ``0.0``."""
    return _bits(0.0 if value == 0.0 else value)


def _payload(scores: dict[str, float] | None = None) -> dict[str, Any]:
    data: dict[str, object] = dict(SCORE_DATA)
    if scores is not None:
        data.update(scores)
    data["relative_legacy_score"] = CANARY
    return post_payload("numeric", post_id=POST_ID, question_id=QUESTION_ID, score_data=data)


def _refusal(source: object, question_id: object = QUESTION_ID) -> str | None:
    """Run the extractor; return the refusal message, or ``None`` if it accepted."""
    try:
        extract_platform_scores(source, question_id)
    except PlatformScoreError as exc:
        message = str(exc)
        assert SENTINEL not in message
        assert repr(CANARY) not in message and str(CANARY) not in message
        return message
    return None


def _record(message: str | None) -> None:
    event("accepted" if message is None else f"refused: {message.split(':')[0][:48]}")


# ── 1 and 4. never raises outside the module's error type; no value leaks ────


def _set_post(payload: dict[str, Any], value: object) -> object:
    return value


def _set_question(payload: dict[str, Any], value: object) -> object:
    payload["question"] = value
    return payload


def _set_my_forecasts(payload: dict[str, Any], value: object) -> object:
    payload["question"]["my_forecasts"] = value
    return payload


def _set_score_data(payload: dict[str, Any], value: object) -> object:
    payload["question"]["my_forecasts"]["score_data"] = value
    return payload


def _set_one_score(payload: dict[str, Any], value: object) -> object:
    payload["question"]["my_forecasts"]["score_data"]["peer_score"] = value
    return payload


@pytest.mark.parametrize(
    "level",
    [_set_post, _set_question, _set_my_forecasts, _set_score_data, _set_one_score],
    ids=["post", "question", "my_forecasts", "score_data", "one_score"],
)
@given(value=JSON_VALUES)
def test_a_corrupted_level_never_raises_outside_platform_score_error(
    level: Callable[[dict[str, Any], object], object], value: object
) -> None:
    source = level(_payload(), value)
    message = _refusal(source)
    _record(message)
    if message is None:
        # Only the one-score level can be accepted, and only with a finite float there.
        assert level is _set_one_score
        assert type(value) is float and math.isfinite(value)


@given(question_id=st.one_of(JSON_VALUES, st.integers()))
def test_a_malformed_question_id_is_refused_or_names_no_question(question_id: object) -> None:
    message = _refusal(_payload(), question_id)
    _record(message)
    if question_id != QUESTION_ID or type(question_id) is not int:
        assert message is not None


@pytest.mark.parametrize("key", sorted(SCORE_DATA_KEYS.values()))
@given(value=JSON_SCALARS)
def test_each_required_score_refuses_anything_but_a_finite_float(key: str, value: object) -> None:
    payload = _payload()
    payload["question"]["my_forecasts"]["score_data"][key] = value
    message = _refusal(payload)
    _record(message)
    valid = type(value) is float and math.isfinite(value)
    assert (message is None) is valid
    if not valid:
        assert message is not None and key in message


@pytest.mark.parametrize("key", sorted(SCORE_DATA_KEYS.values()))
def test_each_required_score_is_required(key: str) -> None:
    payload = _payload()
    del payload["question"]["my_forecasts"]["score_data"][key]
    assert _refusal(payload) == f"my_forecasts.score_data has no {key}"


@pytest.mark.parametrize("key", sorted(SCORE_DATA_KEYS.values()))
@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_each_required_score_refuses_a_non_finite_float(key: str, value: float) -> None:
    """The scalar strategy reaches this branch about 1% of the time; this reaches it always."""
    payload = _payload()
    payload["question"]["my_forecasts"]["score_data"][key] = value
    assert _refusal(payload) == f"my_forecasts.score_data.{key} must be finite"


@pytest.mark.parametrize("version", sorted(IMPLEMENTATION_VERSIONS.values()))
@pytest.mark.parametrize("metric", PLATFORM_METRIC_ORDER)
def test_recompute_accepts_exactly_the_registered_diagonal(version: str, metric: str) -> None:
    """Every registered version against every metric: read on the diagonal, refused off it."""
    if IMPLEMENTATION_VERSIONS[metric] == version:  # type: ignore[index]
        assert (
            recompute(version, metric, _payload(), QUESTION_ID)
            == (
                SCORE_DATA[SCORE_DATA_KEYS[metric]]  # type: ignore[index]
            )
        )
    else:
        with pytest.raises(PlatformScoreError, match="does not match its implementation_version"):
            recompute(version, metric, _payload(), QUESTION_ID)


# ── 2. copied bit for bit, and replay-stable through the persisted form ──────


SCORES = st.fixed_dictionaries({key: FINITE for key in SCORE_DATA_KEYS.values()})


@given(scores=SCORES)
@example(scores={key: -0.0 for key in SCORE_DATA_KEYS.values()})
@example(scores={key: 5e-324 for key in SCORE_DATA_KEYS.values()})
@example(scores={key: -1.7976931348623157e308 for key in SCORE_DATA_KEYS.values()})
def test_a_well_formed_payload_is_copied_bit_for_bit_and_replays(
    scores: dict[str, float],
) -> None:
    payload = _payload(scores)
    extracted = extract_platform_scores(payload, QUESTION_ID)
    assert [s.metric for s in extracted] == list(PLATFORM_METRIC_ORDER)
    for score in extracted:
        assert _bits(score.value) == _bits(scores[SCORE_DATA_KEYS[score.metric]])
        assert score.implementation_version == IMPLEMENTATION_VERSIONS[score.metric]

    replayed_source = json.loads(canonical_json(payload))
    replayed = extract_platform_scores(replayed_source, QUESTION_ID)
    assert [(s.metric, _bits(s.value)) for s in replayed] == [
        (s.metric, _bits(s.value)) for s in extracted
    ]
    for score in extracted:
        again = recompute(score.implementation_version, score.metric, replayed_source, QUESTION_ID)
        assert _bits(again) == _bits(score.value)
    event("negative zero" if any(_bits(v) == _bits(-0.0) for v in scores.values()) else "plain")


@given(
    version=st.one_of(st.sampled_from(sorted(IMPLEMENTATION_VERSIONS.values())), JSON_SCALARS),
    metric=st.one_of(st.sampled_from(PLATFORM_METRIC_ORDER), JSON_SCALARS),
)
def test_recompute_reads_only_under_a_registered_version_of_its_own_metric(
    version: object, metric: object
) -> None:
    try:
        value = recompute(version, metric, _payload(), QUESTION_ID)
    except PlatformScoreError as exc:
        assert SENTINEL not in str(exc)
        registered = version in IMPLEMENTATION_VERSIONS.values()
        event("refused: unregistered" if not registered else "refused: metric mismatch")
        assert not registered or IMPLEMENTATION_VERSIONS.get(metric) != version  # type: ignore[call-overload]
        return
    event("accepted")
    assert IMPLEMENTATION_VERSIONS[metric] == version  # type: ignore[index]
    assert value == SCORE_DATA[SCORE_DATA_KEYS[metric]]  # type: ignore[index]


# ── 3. through a real ledger, and 017's exact-value clause ───────────────────


@pytest.fixture(scope="module")
def ledger(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    db = tmp_path_factory.mktemp("platform-score-properties") / "ledger.sqlite3"
    initialize_ledger(db)
    connection = connect(db)
    try:
        yield connection
    finally:
        connection.close()


_IDS = itertools.count(1)
OBSERVED = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)
COMPUTED = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)


@given(scores=SCORES, question_type=st.sampled_from(["binary", "numeric"]))
@example(
    scores={key: 1.7541008294647201e-137 for key in SCORE_DATA_KEYS.values()},
    question_type="numeric",
)
@example(
    scores={key: -1.6514842009472972e116 for key in SCORE_DATA_KEYS.values()},
    question_type="numeric",
)
@example(scores={key: -0.0 for key in SCORE_DATA_KEYS.values()}, question_type="numeric")
def test_the_ledger_stores_and_rereads_every_finite_double_exactly(
    ledger: sqlite3.Connection, scores: dict[str, float], question_type: str
) -> None:
    """The two `@example` values are doubles SQLite 3.45.1's JSON parser rounds wrongly
    (measured 2026-09-23); the SQLite the deployed venv ships reads them exactly."""
    n = next(_IDS)
    record = f"rec-{n}"
    question_id, post_id = 100_000 + n, 200_000 + n
    seed_submitted(
        ledger, record, question_id=question_id, post_id=post_id, question_type=question_type
    )
    payload = post_payload(question_type, post_id=post_id, question_id=question_id)
    payload["question"]["my_forecasts"]["score_data"].update(scores)
    record_resolution_observation(
        ledger, record_id=record, source_response=payload, observed_at=OBSERVED
    )
    write = record_platform_scores(ledger, record_id=record, computed_at=COMPUTED)
    assert write.outcome == "appended"
    reread = read_platform_scores(ledger, record)
    assert [(s.metric, _bits(s.value)) for s in reread] == [
        (metric, _stored_bits(scores[SCORE_DATA_KEYS[metric]])) for metric in PLATFORM_METRIC_ORDER
    ]
    stored = ledger.execute(
        "SELECT metric, value FROM score_events WHERE forecast_record_id = ? ORDER BY event_id",
        (record,),
    ).fetchall()
    assert [(row[0], _bits(row[1])) for row in stored] == [
        (s.metric, _bits(s.value)) for s in reread
    ]
    event(question_type)
    event("zero" if 0.0 in scores.values() else "nonzero")


def test_the_property_module_really_crosses_a_file_backed_ledger(
    ledger: sqlite3.Connection,
) -> None:
    """Guards the module fixture: an in-memory ledger would not exercise WAL or the
    migrations' file-level behaviour the deployed worker runs under."""
    row = ledger.execute("PRAGMA database_list").fetchone()
    assert row is not None and row[2] != ""
