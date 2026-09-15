"""Property tests for local Brier and log scores and their ledger writer (M4-802).

The CLAUDE.md pre-review fuzz pass over ``scoring.py`` and ``lifecycle.record_local_scores``:

1. No scoring function raises outside ``ScoreError``, over structured and arbitrary inputs.
2. On every valid input: binary Brier in [0, 1]; multiclass Brier in [0, 2 + 2 * tol]; log
   finite and <= 0 -- or ``ScoreError`` exactly when the realized outcome had probability 0.
3. A multiclass score does not depend on option order, to the bit.
4. A binary forecast and the same forecast as two options agree: log exactly, Brier twice
   (the single-term against the two-category convention) up to the rounding of ``1.0 - p``.
5. Both scores are proper over a grid: the expected score under belief ``q`` is best at
   ``p = q``.
6. No refusal reprints a label, an outcome or a probability.
7. What the writer stores replays: read back through the real ledger and recomputed, and
   through the persisted JSON form. A zero on the realized outcome writes no row at all.

Reach is measured with ``hypothesis.event`` (M4-801's lesson: two properties there were
nearly vacuous as first written).
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import math
import sqlite3
from collections.abc import Callable, Iterator

import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st
from strategies import HOSTILE_TEXT

from score_rows import MC_LABELS, SCORED_AT, seed_resolved
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    StoredScore,
    read_local_scores,
    record_local_scores,
)
from whiskeyjack_bot.scoring import (
    IMPLEMENTATION_VERSIONS,
    ScoreError,
    binary_brier_v1,
    binary_log_v1,
    multiclass_brier_v1,
    multiclass_log_v1,
    recompute,
    score_binary,
    score_multiple_choice,
)

TOLERANCE = 1e-6  # the scorer's sum tolerance, restated: a property must not import it
SENTINEL = "LEAKCANARY7"
_IDS = itertools.count(1)

# ── strategies ───────────────────────────────────────────────────────────────

EDGE_PROBABILITIES = st.sampled_from([0.0, 1.0, 0.001, 0.999, 5e-324, 1.0 - 2.0**-53, 0.5])
PROBABILITIES = st.one_of(EDGE_PROBABILITIES, st.floats(min_value=0.0, max_value=1.0))
BINARY_OUTCOMES = st.sampled_from(["yes", "no"])
LABELS = st.text(st.characters(exclude_categories=["Cs"]), min_size=1, max_size=8)


@st.composite
def distributions(draw: st.DrawFn, min_size: int = 2) -> tuple[tuple[str, float], ...]:
    """Distinct labels with integer weights normalized: sums within ~1e-15 of 1, and zeros
    are drawn often enough that the undefined-log branch is reached."""
    labels = draw(st.lists(LABELS, min_size=min_size, max_size=6, unique=True))
    weights = draw(
        st.lists(
            st.one_of(st.just(0), st.integers(min_value=0, max_value=1000)),
            min_size=len(labels),
            max_size=len(labels),
        ).map(lambda ws: ws if sum(ws) else [1, *ws[1:]])  # mapped, not filtered: no discards
    )
    total = sum(weights)
    return tuple((label, weight / total) for label, weight in zip(labels, weights, strict=True))


ARBITRARY = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**70), max_value=2**70),
        st.floats(allow_nan=True, allow_infinity=True),
        HOSTILE_TEXT,
    ),
    lambda children: st.lists(children, max_size=4) | st.tuples(children, children),
    max_leaves=8,
)


# ── 1. never raises outside ScoreError ───────────────────────────────────────


@given(
    prediction=st.one_of(PROBABILITIES, distributions(min_size=1), ARBITRARY),
    outcome=st.one_of(BINARY_OUTCOMES, LABELS, ARBITRARY),
)
@example(prediction=[("a", 1.0)], outcome="a")  # a list of tuples is a Sequence too
@example(prediction=(("a", 1.0),), outcome="a")
def test_no_scoring_function_raises_outside_its_error_type(
    prediction: object, outcome: object
) -> None:
    refused = 0
    for function in (
        binary_brier_v1,
        binary_log_v1,
        multiclass_brier_v1,
        multiclass_log_v1,
        score_binary,
        score_multiple_choice,
    ):
        try:
            function(prediction, outcome)
        except ScoreError:
            refused += 1
    for version in (*IMPLEMENTATION_VERSIONS.values(), "local_brier_binary/9", outcome):
        try:
            recompute(version, "local_brier_binary", prediction, outcome)
        except ScoreError:
            refused += 1
    event(f"refused {refused} of 12")


# ── 2. bounds on every valid input ───────────────────────────────────────────


@given(p=PROBABILITIES, outcome=BINARY_OUTCOMES)
def test_binary_scores_stay_in_range_or_refuse_only_a_zero(p: float, outcome: str) -> None:
    brier = binary_brier_v1(p, outcome)
    assert 0.0 <= brier <= 1.0
    realized = p if outcome == "yes" else 1.0 - p
    if realized == 0.0:
        event("log undefined: zero on the outcome")
        with pytest.raises(ScoreError, match="undefined"):
            binary_log_v1(p, outcome)
        return
    log = binary_log_v1(p, outcome)
    event("log at 0" if log == 0.0 else "log < 0")
    assert math.isfinite(log) and log <= 0.0


@given(options=distributions(), data=st.data())
def test_multiclass_scores_stay_in_range_or_refuse_only_a_zero(
    options: tuple[tuple[str, float], ...], data: st.DataObject
) -> None:
    label, realized = data.draw(st.sampled_from(options))
    brier = multiclass_brier_v1(options, label)
    assert 0.0 <= brier <= 2.0 + 2 * TOLERANCE
    event("brier >= 1" if brier >= 1.0 else "brier < 1")
    if realized == 0.0:
        event("log undefined: zero on the outcome")
        with pytest.raises(ScoreError, match="undefined"):
            multiclass_log_v1(options, label)
        return
    log = multiclass_log_v1(options, label)
    assert math.isfinite(log) and log <= 0.0


# ── 3. option order does not matter ──────────────────────────────────────────


@given(options=distributions(), data=st.data())
def test_a_multiclass_score_does_not_depend_on_option_order(
    options: tuple[tuple[str, float], ...], data: st.DataObject
) -> None:
    permuted = tuple(data.draw(st.permutations(options)))
    event("order changed" if permuted != options else "order unchanged")
    label = data.draw(st.sampled_from(options))[0]
    assert multiclass_brier_v1(permuted, label) == multiclass_brier_v1(options, label)
    try:
        forward = multiclass_log_v1(options, label)
    except ScoreError:
        with pytest.raises(ScoreError):
            multiclass_log_v1(permuted, label)
        return
    assert multiclass_log_v1(permuted, label) == forward


# ── 4. binary and two-option multiclass agree ────────────────────────────────


@given(p=PROBABILITIES, outcome=BINARY_OUTCOMES)
def test_a_binary_forecast_scores_like_its_two_option_distribution(p: float, outcome: str) -> None:
    two = (("yes", p), ("no", 1.0 - p))
    # Twice, up to the rounding of `1.0 - p`: 0.999 is not exactly 1 - 0.001 in binary, so
    # the second term differs by ~1e-21 there (found by this property, p = 0.001, "no").
    assert math.isclose(
        multiclass_brier_v1(two, outcome),
        2.0 * binary_brier_v1(p, outcome),
        rel_tol=1e-12,
        abs_tol=1e-18,
    )
    try:
        log = binary_log_v1(p, outcome)
    except ScoreError:
        event("both undefined")
        with pytest.raises(ScoreError):
            multiclass_log_v1(two, outcome)
        return
    assert multiclass_log_v1(two, outcome) == log


# ── 5. propriety ─────────────────────────────────────────────────────────────

INTERIOR = st.floats(min_value=0.001, max_value=0.999)


@given(q=INTERIOR, p=INTERIOR)
def test_both_binary_scores_are_proper(q: float, p: float) -> None:
    """E_q[Brier(p)] is least, and E_q[log(p)] greatest, at p = q."""

    def expected(function: Callable[[object, object], float], forecast: float) -> float:
        return q * function(forecast, "yes") + (1.0 - q) * function(forecast, "no")

    event("p == q" if p == q else "p != q")
    slack = 1e-12
    assert expected(binary_brier_v1, q) <= expected(binary_brier_v1, p) + slack
    assert expected(binary_log_v1, q) >= expected(binary_log_v1, p) - slack


# ── 6. no value in any refusal ───────────────────────────────────────────────


@given(
    label=HOSTILE_TEXT.map(lambda text: f"{SENTINEL}{text}"),
    probability=st.one_of(
        st.floats(allow_nan=True, allow_infinity=True), st.just(0.0), st.just(1.7)
    ),
    shape=st.sampled_from(["duplicate", "unpriced", "zero", "range", "sum", "binary"]),
)
def test_no_refusal_reprints_a_label_outcome_or_probability(
    label: str, probability: float, shape: str
) -> None:
    calls = {
        "duplicate": lambda: multiclass_log_v1(((label, 0.5), (label, 0.5)), label),
        "unpriced": lambda: multiclass_brier_v1(((label, 1.0),), f"{label}x"),
        "zero": lambda: multiclass_log_v1(((label, 0.0), ("b", 1.0)), label),
        "range": lambda: multiclass_brier_v1(((label, probability), ("b", 0.5)), label),
        "sum": lambda: multiclass_brier_v1(((label, 0.25), ("b", 0.25)), label),
        "binary": lambda: binary_log_v1(probability, label),
    }
    try:
        calls[shape]()
    except ScoreError as exc:
        event(f"refused {shape}")
        message = str(exc)
        assert SENTINEL not in message
        assert repr(probability) not in message or repr(probability) in ("0.0", "1.0")
        return
    event(f"accepted {shape}")


# ── 7. what is stored replays, through the real ledger ───────────────────────


@pytest.fixture(scope="module")
def ledger(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    db = tmp_path_factory.mktemp("scoring-properties") / "ledger.sqlite3"
    initialize_ledger(db)
    conn = connect(db)
    try:
        yield conn
    finally:
        conn.close()


@st.composite
def resolved_forecasts(draw: st.DrawFn) -> tuple[str, dict[str, object], str]:
    """(question_type, forecast kwargs for seed_resolved, the outcome it resolves to)."""
    if draw(st.booleans()):
        return "binary", {"probability_yes": draw(PROBABILITIES)}, draw(BINARY_OUTCOMES)
    labels = draw(st.permutations(MC_LABELS))
    weights = draw(
        st.lists(st.integers(min_value=0, max_value=50), min_size=3, max_size=3).map(
            lambda ws: ws if sum(ws) else [1, *ws[1:]]
        )
    )
    total = sum(weights)
    options = tuple((label, w / total) for label, w in zip(labels, weights, strict=True))
    return "multiple_choice", {"options": options}, draw(st.sampled_from(MC_LABELS))


@given(case=resolved_forecasts())
def test_a_stored_score_replays_and_a_zero_writes_nothing(
    ledger: sqlite3.Connection, case: tuple[str, dict[str, object], str]
) -> None:
    question_type, forecast, outcome = case
    n = next(_IDS)
    record = seed_resolved(
        ledger,
        f"rec-score-prop-{n}",
        question_id=700_000 + n,
        post_id=800_000 + n,
        question_type=question_type,
        resolution=outcome,
        **forecast,
    )
    if question_type == "binary":
        p = forecast["probability_yes"]
        assert isinstance(p, float)
        realized = p if outcome == "yes" else 1.0 - p
    else:
        options = forecast["options"]
        assert isinstance(options, tuple)
        realized = dict(options)[outcome]

    if realized == 0.0:
        event(f"{question_type}: zero on the outcome")
        with pytest.raises(LifecycleError, match="undefined"):
            record_local_scores(ledger, record_id=record, computed_at=SCORED_AT)
        assert read_local_scores(ledger, record) == ()
        return

    event(f"{question_type}: scored")
    write = record_local_scores(ledger, record_id=record, computed_at=SCORED_AT)
    assert write.outcome == "appended" and len(write.scores) == 2
    assert read_local_scores(ledger, record) == write.scores
    for stored in write.scores:
        persisted = json.dumps(dataclasses.asdict(stored), ensure_ascii=True, sort_keys=True)
        assert StoredScore(**json.loads(persisted)) == stored
    expected = (
        score_binary(forecast["probability_yes"], outcome)
        if question_type == "binary"
        else score_multiple_choice(forecast["options"], outcome)
    )
    assert [(s.metric, s.value, s.implementation_version) for s in write.scores] == [
        (s.metric, s.value, s.implementation_version) for s in expected
    ]
    again = record_local_scores(ledger, record_id=record, computed_at=SCORED_AT)
    assert again.outcome == "unchanged"
