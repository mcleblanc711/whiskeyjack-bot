"""Property tests for resolution classification and its ledger writer (M4-801).

The CLAUDE.md pre-review fuzz pass, over the one pure function this item adds and the writer
that persists it:

1. ``classify_resolution`` never raises outside ``ResolutionError``, over structured and
   arbitrary payloads.
2. No refusal -- from the classifier or the writer -- reprints a payload value.
3. An observation replays: through its persisted JSON form, and through a real ledger.
4. The kind a payload was built to have is the kind it classifies as, and ``scorable`` is
   exactly ``kind == "resolved"``. The strategy draws the kind first, so every kind is reached
   by construction rather than by luck (lesson 5's vacuity trap).
5. Appending a sequence of observations is idempotent in exactly one way: a row per change,
   none per repeat, none for a first retraction. The alphabet is five symbols so repeats
   (A, A) and returns (A, B, A) are drawn constantly; both are also pinned as explicit
   examples.
6. ``observation_sha256`` is injective over a deliberately tiny alphabet of valid
   observations, including ``None`` against a value on every optional field (M1-331).
"""

from __future__ import annotations

import copy
import itertools
import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import event, example, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from resolution_rows import RESOLVED_VALUE, kind_payload, post_payload, seed_submitted
from strategies import HOSTILE_TEXT

from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    current_status,
    latest_resolution,
    read_history,
    read_resolution_history,
    record_resolution_observation,
)
from whiskeyjack_bot.resolution import (
    ResolutionError,
    ResolutionObservation,
    canonical_json,
    classify_resolution,
    observation_from_snapshot,
)

TYPES = ("binary", "multiple_choice", "numeric", "discrete")
KINDS = ("resolved", "annulled", "ambiguous", "withheld", "unresolved")
SENTINEL = "LEAKCANARY7"
T0 = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)
_IDS = itertools.count(1)


@pytest.fixture(scope="module")
def ledger(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    db = tmp_path_factory.mktemp("resolution-properties") / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        yield conn
    finally:
        conn.close()


def _fresh_record(conn: sqlite3.Connection, question_type: str = "binary") -> tuple[str, int, int]:
    """A submitted record on its own question and post, in the shared module ledger."""
    n = next(_IDS)
    record_id, question_id, post_id = f"rec-prop-{n}", 500_000 + n, 900_000 + n
    seed_submitted(
        conn, record_id, question_id=question_id, post_id=post_id, question_type=question_type
    )
    return record_id, question_id, post_id


# ── strategies ───────────────────────────────────────────────────────────────

JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**70), max_value=2**70),
    st.floats(allow_nan=True, allow_infinity=True),
    HOSTILE_TEXT,
)
JSON_VALUES = st.recursive(
    JSON_SCALARS,
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(HOSTILE_TEXT, children, max_size=3)
    ),
    max_leaves=8,
)

_DECIMALS = st.one_of(
    st.floats(allow_nan=False, allow_infinity=False).map(repr),
    st.integers(min_value=-(10**6), max_value=10**6).map(str),
)
# Weighted toward values some type accepts. An even mix of valid and hostile draws was
# measured at 90% refused and 0.2% classified as anything but `unresolved`/`withheld`, which
# made the replay property below a test of almost nothing (lesson 5). The hostile branches
# stay, one draw in four, which is still hundreds of hostile examples per run.
_VALID_RESOLUTIONS = st.sampled_from(
    [
        None,
        "yes",
        "no",
        "annulled",
        "ambiguous",
        "above_upper_bound",
        "below_lower_bound",
        "Option Alpha",
        "Option Beta",
        "Other",
    ]
)
RESOLUTION_VALUES = st.one_of(
    _VALID_RESOLUTIONS,
    _VALID_RESOLUTIONS,
    _DECIMALS,
    st.one_of(st.sampled_from(["nan", "inf", "1_0", " 1", ""]), JSON_VALUES),
)
STATUSES = st.one_of(
    st.just("resolved"),
    st.just("resolved"),
    st.sampled_from(["open", "closed", "upcoming"]),
    JSON_VALUES,
)
_VALID_TIMESTAMPS = st.one_of(
    st.sampled_from([None, "2026-09-17T12:00:00Z", "2026-09-17T12:00:00+05:30"]),
    st.datetimes(
        min_value=datetime(1970, 1, 2),
        max_value=datetime(9999, 12, 30),
        timezones=st.just(timezone.utc),
    ).map(lambda moment: moment.isoformat()),
)
TIMESTAMPS = st.one_of(
    _VALID_TIMESTAMPS,
    _VALID_TIMESTAMPS,
    _VALID_TIMESTAMPS,
    st.one_of(st.sampled_from(["2026-09-17T12:00:00", "x"]), JSON_VALUES),
)


_TYPE_RESOLUTIONS: dict[str, st.SearchStrategy[str | None]] = {
    "binary": st.sampled_from([None, "yes", "no", "annulled", "ambiguous"]),
    "multiple_choice": st.sampled_from(
        [None, "Option Alpha", "Option Beta", "Other", "annulled", "ambiguous"]
    ),
    "numeric": st.one_of(
        st.sampled_from([None, "annulled", "ambiguous", "above_upper_bound", "below_lower_bound"]),
        _DECIMALS,
    ),
    "discrete": st.one_of(
        st.sampled_from([None, "annulled", "ambiguous", "above_upper_bound", "below_lower_bound"]),
        _DECIMALS,
    ),
}


@st.composite
def structured_posts(draw: st.DrawFn, *, hostile: bool = True) -> tuple[object, int, str]:
    """A real post fixture with its resolution fields, and sometimes its shape, redrawn.

    ``hostile=False`` draws only values the drawn type accepts and never mutates the shape,
    for the properties about what happens *after* a successful classification. Measured
    with ``hostile=True``: ~90% of draws are refused, which is right for "never raises" and
    would leave a replay property a few dozen examples to say anything about.
    """
    question_type = draw(st.sampled_from(TYPES))
    post: dict[str, Any] = post_payload(question_type, post_id=45556, question_id=45747)
    question = post["question"]
    if not hostile:
        status = draw(st.sampled_from(["resolved", "resolved", "closed", "open"]))
        question["status"] = status
        question["resolution"] = (
            draw(_TYPE_RESOLUTIONS[question_type]) if status == "resolved" else None
        )
        question["actual_resolve_time"] = draw(_VALID_TIMESTAMPS)
        question["resolution_set_time"] = draw(_VALID_TIMESTAMPS)
        return post, 45747, question_type
    question["status"] = draw(STATUSES)
    question["resolution"] = draw(RESOLUTION_VALUES)
    question["actual_resolve_time"] = draw(TIMESTAMPS)
    question["resolution_set_time"] = draw(TIMESTAMPS)
    mutation = draw(
        st.sampled_from(["none"] * 6 + ["drop_key", "retype", "replace_question", "id"])
    )
    if mutation == "drop_key":
        question.pop(draw(st.sampled_from(sorted(question))), None)
    elif mutation == "retype":
        question["type"] = draw(st.sampled_from([*TYPES, "date"]) | JSON_VALUES)
    elif mutation == "replace_question":
        post["question"] = draw(JSON_VALUES)
    elif mutation == "id":
        post["id"] = draw(JSON_VALUES)
    record_type = draw(st.sampled_from([*TYPES, *([question_type] * 8)]))
    return post, 45747, record_type


# ── 1. never raises outside the module's own error type ──────────────────────


@settings(max_examples=400)
@given(structured_posts())
def test_classification_never_raises_outside_resolution_error(
    case: tuple[object, int, str],
) -> None:
    post, question_id, question_type = case
    try:
        observation = classify_resolution(
            post, question_id=question_id, question_type=question_type
        )
    except ResolutionError:
        event("refused")
        return
    event(f"classified {observation.kind}")
    assert isinstance(observation, ResolutionObservation)


@given(JSON_VALUES, st.integers(), JSON_VALUES)
def test_arbitrary_json_never_raises_outside_resolution_error(
    post: object, question_id: int, question_type: object
) -> None:
    try:
        classify_resolution(post, question_id=question_id, question_type=question_type)  # type: ignore[arg-type]
    except ResolutionError:
        pass


@given(JSON_VALUES)
def test_canonical_json_never_raises_outside_resolution_error(payload: object) -> None:
    try:
        text = canonical_json(payload)
    except ResolutionError:
        return
    assert text.isascii()


# ── 2. no value leak ─────────────────────────────────────────────────────────

LEAK_FIELDS = ("resolution", "status", "actual_resolve_time", "resolution_set_time", "type")


@given(
    st.sampled_from(TYPES),
    st.sampled_from(LEAK_FIELDS),
    HOSTILE_TEXT,
    HOSTILE_TEXT,
)
def test_no_refusal_reprints_a_planted_value(
    question_type: str, field: str, before: str, after: str
) -> None:
    """The sentinel is invalid in every field it is planted in, so a refusal is guaranteed.

    That guarantee is the vacuity guard: `pytest.raises` fails the example if the classifier
    ever accepted the planted value, so no example can pass by never reaching a message.
    """
    post = post_payload(question_type, post_id=45556, question_id=45747)
    post["question"][field] = f"{before}{SENTINEL}{after}"
    with pytest.raises(ResolutionError) as caught:
        classify_resolution(post, question_id=45747, question_type=question_type)
    assert SENTINEL not in str(caught.value)
    assert caught.value.__cause__ is None


@settings(max_examples=60)
@given(st.sampled_from(LEAK_FIELDS), HOSTILE_TEXT)
def test_no_writer_refusal_reprints_a_planted_value(
    ledger: sqlite3.Connection, field: str, noise: str
) -> None:
    record_id, question_id, post_id = _fresh_record(ledger)
    post = post_payload("binary", post_id=post_id, question_id=question_id)
    post["question"][field] = f"{noise}{SENTINEL}"
    with pytest.raises(LifecycleError) as caught:
        record_resolution_observation(
            ledger, record_id=record_id, source_response=post, observed_at=T0
        )
    assert SENTINEL not in str(caught.value)
    assert not ledger.in_transaction


# ── 3. replay ────────────────────────────────────────────────────────────────


@settings(max_examples=400)
@given(structured_posts(hostile=False))
def test_an_observation_replays_through_its_persisted_form(case: tuple[object, int, str]) -> None:
    post, question_id, question_type = case
    try:
        observation = classify_resolution(
            post, question_id=question_id, question_type=question_type
        )
    except ResolutionError:
        event("refused")
        return
    event(f"classified {observation.kind}")
    persisted = json.dumps(observation.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)
    replayed = ResolutionObservation.model_validate(json.loads(persisted))
    assert replayed == observation
    assert replayed.observation_sha256 == observation.observation_sha256
    assert observation_from_snapshot(observation.snapshot_json()) == observation


@st.composite
def valid_posts(draw: st.DrawFn) -> tuple[str, str, dict[str, Any]]:
    """A post of a drawn type and kind, with hostile text in fields the classifier ignores."""
    question_type = draw(st.sampled_from(TYPES))
    kind = draw(st.sampled_from(KINDS))
    return (
        question_type,
        kind,
        _decorate(kind_payload(question_type, kind, post_id=1, question_id=1), draw),
    )


def _decorate(post: dict[str, Any], draw: st.DrawFn) -> dict[str, Any]:
    post = copy.deepcopy(post)
    post["title"] = draw(HOSTILE_TEXT)
    post["question"]["description"] = draw(HOSTILE_TEXT)
    post["extra"] = draw(JSON_VALUES.filter(_has_json_form))
    return post


def _has_json_form(value: object) -> bool:
    try:
        canonical_json(value)
    except ResolutionError:
        return False
    return True


def _rekey(post: dict[str, Any], post_id: int, question_id: int) -> dict[str, Any]:
    post = copy.deepcopy(post)
    post["id"] = post_id
    post["question"]["id"] = question_id
    post["question"]["post_id"] = post_id
    return post


@settings(max_examples=150)
@given(valid_posts())
def test_what_the_ledger_stores_replays_to_what_was_classified(
    ledger: sqlite3.Connection, case: tuple[str, str, dict[str, Any]]
) -> None:
    """Crosses the real boundary (lesson 9): SQLite, not a JSON simulation of it."""
    question_type, kind, template = case
    record_id, question_id, post_id = _fresh_record(ledger, question_type)
    post = _rekey(template, post_id, question_id)
    expected = classify_resolution(post, question_id=question_id, question_type=question_type)
    write = record_resolution_observation(
        ledger, record_id=record_id, source_response=post, observed_at=T0
    )
    if kind == "unresolved":
        assert write.outcome == "nothing_to_retract"
        return
    stored = latest_resolution(ledger, record_id)
    assert stored is not None
    assert stored.observation == expected
    assert stored.observation_sha256 == expected.observation_sha256
    source = ledger.execute(
        "SELECT source_response FROM resolution_events WHERE event_id = ?", (stored.event_id,)
    ).fetchone()[0]
    assert source == canonical_json(post)


# ── 4. the partition ─────────────────────────────────────────────────────────


@settings(max_examples=300)
@given(valid_posts())
def test_the_drawn_kind_is_the_classified_kind_and_only_resolved_scores(
    case: tuple[str, str, dict[str, Any]],
) -> None:
    question_type, kind, post = case
    observation = classify_resolution(post, question_id=1, question_type=question_type)
    assert observation.kind == kind
    assert observation.scorable is (kind == "resolved")
    assert (observation.outcome is not None) is (kind == "resolved")
    assert observation.definitive is (kind in ("resolved", "annulled", "ambiguous"))
    if kind == "resolved":
        assert observation.outcome == RESOLVED_VALUE[question_type]


# ── 5. idempotent append ─────────────────────────────────────────────────────

SYMBOLS = ("yes", "no", "annulled", "withheld", "unresolved")


def _symbol_payload(symbol: str, post_id: int, question_id: int) -> dict[str, Any]:
    if symbol == "withheld":
        return kind_payload("binary", "withheld", post_id=post_id, question_id=question_id)
    if symbol == "unresolved":
        return kind_payload("binary", "unresolved", post_id=post_id, question_id=question_id)
    return post_payload("binary", post_id=post_id, question_id=question_id, resolution=symbol)


@settings(max_examples=150)
@given(st.lists(st.sampled_from(SYMBOLS), min_size=1, max_size=7))
@example(["yes", "yes"])
@example(["yes", "annulled", "yes"])
@example(["unresolved", "yes", "unresolved", "unresolved", "no"])
def test_appending_is_a_row_per_change_and_nothing_else(
    ledger: sqlite3.Connection, sequence: list[str]
) -> None:
    record_id, question_id, post_id = _fresh_record(ledger)
    expected: list[str] = []
    for step, symbol in enumerate(sequence):
        write = record_resolution_observation(
            ledger,
            record_id=record_id,
            source_response=_symbol_payload(symbol, post_id, question_id),
            observed_at=T0 + timedelta(minutes=step),
        )
        if not expected and symbol == "unresolved":
            assert write.outcome == "nothing_to_retract"
        elif expected and expected[-1] == symbol:
            assert write.outcome == "unchanged"
        else:
            assert write.outcome == "appended"
            expected.append(symbol)

    history = read_resolution_history(ledger, record_id)
    as_symbols = [r.observation.outcome if r.kind == "resolved" else r.kind for r in history]
    assert as_symbols == expected

    definitive = [s for s in expected if s in ("yes", "no", "annulled")]
    resolved_events = [e for e in read_history(ledger, record_id) if e.event_type == "resolved"]
    assert len(resolved_events) == (1 if definitive else 0)
    assert current_status(ledger, record_id) == ("resolved" if definitive else "submitted")

    # The score guard agrees with the latest row, whatever came before it.
    latest_scorable = bool(expected) and expected[-1] in ("yes", "no")
    try:
        ledger.execute(
            "INSERT INTO score_events (forecast_record_id, metric, value, "
            "implementation_version, computed_at_utc) VALUES (?, 'brier', 0.5, 'v1', ?)",
            (record_id, "2026-09-18T00:00:00.000000+00:00"),
        )
        scored = True
    except sqlite3.IntegrityError:
        scored = False
    assert scored is latest_scorable


# ── 6. the digest is injective over a tiny alphabet ──────────────────────────

_TINY_TIMES = st.sampled_from(
    [None, "2026-09-17T12:00:00.000000+00:00", "2026-09-17T12:00:01.000000+00:00"]
)


@st.composite
def tiny_observations(draw: st.DrawFn) -> ResolutionObservation:
    """A valid observation from a deliberately tiny alphabet, so equal pairs are common.

    `kind` and `outcome` are derived rather than drawn: drawing them independently was
    measured at 98.6% invalid, leaving ~7 examples a run for the property to say anything
    about. The partition decides them, exactly as the classifier would, and the derivation is
    by trying the model rather than by importing the private helper it validates with.
    """
    question_type = draw(st.sampled_from(["binary", "numeric"]))
    status = draw(st.sampled_from(["resolved", "closed"]))
    resolution = (
        None
        if status == "closed"
        else draw(
            st.sampled_from(
                [None, "annulled", "ambiguous"]
                + (["yes", "no"] if question_type == "binary" else ["1.0", "above_upper_bound"])
            )
        )
    )
    fields = {
        "question_id": draw(st.sampled_from([1, 2])),
        "post_id": draw(st.sampled_from([1, 2])),
        "question_type": question_type,
        "platform_status": status,
        "resolution": resolution,
        "actual_resolve_time": draw(_TINY_TIMES),
        "resolution_set_time": draw(_TINY_TIMES),
    }
    for kind in KINDS:
        for outcome in (None, resolution):
            try:
                return ResolutionObservation.model_validate(
                    {**fields, "kind": kind, "outcome": outcome}
                )
            except ValidationError:
                continue
    raise AssertionError("every drawn (status, resolution, type) has exactly one valid kind")


@settings(max_examples=500)
@given(tiny_observations(), tiny_observations())
def test_two_observations_share_a_digest_only_if_they_are_equal(
    first: ResolutionObservation, second: ResolutionObservation
) -> None:
    event("equal pair" if first == second else "distinct pair")
    assert (first.observation_sha256 == second.observation_sha256) is (
        first.model_dump() == second.model_dump()
    )


def test_the_tiny_alphabet_reaches_equal_and_near_equal_pairs() -> None:
    """Vacuity guard for property 6: its alphabet admits both equal and one-field-apart pairs."""
    base = ResolutionObservation.model_validate(
        {
            "question_id": 1,
            "post_id": 1,
            "question_type": "binary",
            "platform_status": "resolved",
            "resolution": "yes",
            "kind": "resolved",
            "outcome": "yes",
            "actual_resolve_time": None,
            "resolution_set_time": None,
        }
    )
    timed = base.model_copy(update={"actual_resolve_time": "2026-09-17T12:00:00.000000+00:00"})
    assert ResolutionObservation.model_validate(timed.model_dump()) == timed
    assert base.observation_sha256 != timed.observation_sha256
    assert (
        base.observation_sha256
        == ResolutionObservation.model_validate(base.model_dump()).observation_sha256
    )


def test_the_property_module_really_crosses_a_file_backed_ledger(
    ledger: sqlite3.Connection,
) -> None:
    (path,) = [row[2] for row in ledger.execute("PRAGMA database_list") if row[1] == "main"]
    assert Path(path).is_file()
