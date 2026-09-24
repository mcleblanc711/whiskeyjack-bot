"""Property tests for the attribution report's pure layer (M5-804).

The CLAUDE.md pre-review fuzz pass over what this item adds that is a pure function --
:func:`report.calibration_bin`, :func:`report.summarize_values`, :func:`report.classify` and
:func:`report.build_report`:

1. **Never raises outside ``ReportError``**, including on the doubles that overflow a sum or a
   spread (017 admits any finite double, so they are reachable), on signed zeros and on
   facts that could not come from a valid ledger.
2. **Partitions hold.** The states partition the included records; every single-valued axis
   partitions them; on an overlapping axis a record sits in exactly ``max(1, |values|)``
   groups; the calibration bins partition the calibration population.
3. **Order is irrelevant**: shuffling the facts gives byte-identical canonical output, and the
   bin index is monotone in the probability.
4. **Replay-stable** across the persisted form: ``canonical_json`` -> ``json.loads`` ->
   ``canonical_json``.
5. **No value leaks** into a refusal.

Reach is measured, not assumed: :func:`test_the_strategy_reaches_every_branch` counts states,
refusal kinds, edge probabilities and multi-valued rows over a fixed draw and fails if any
branch the properties are about is starved (the vacuous-property class).
"""

from __future__ import annotations

import json
import math
import random
import sys
from collections import Counter
from typing import Any

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from whiskeyjack_bot.export import canonical_json
from whiskeyjack_bot.platform_scores import COMPARISON_BASELINES, IMPLEMENTATION_VERSIONS
from whiskeyjack_bot.report import (
    AXES,
    CALIBRATION_BIN_EDGES,
    OVERLAPPING_AXES,
    STATES,
    AttributionRow,
    RecordFacts,
    ReportError,
    ResolutionFact,
    ScoreFact,
    build_report,
    calibration_bin,
    classify,
    summarize_values,
)
from whiskeyjack_bot.scoring import IMPLEMENTATION_VERSIONS as LOCAL_VERSIONS

CANARY = "zqx-canary-5150"
TYPES = ("binary", "multiple_choice", "numeric", "discrete")
# Weighted by repetition -- `sampled_from` draws an index uniformly, so a repeated member is
# drawn more often (unlike `st.one_of`, which does not weight by repetition; M1-348). Posted
# statuses and the `resolved` kind dominate, or the scored branches starve (measured below).
STATUSES = ("draft", "validated", "approved", "failed") + ("submitted", "resolved", "scored") * 3
KINDS = ("resolved",) * 4 + ("annulled", "ambiguous", "withheld", "unresolved")
TAGS = ("base_rate", "status_quo", "trend", "inside_view")
TOURNAMENTS = ("33125", "33122") * 3 + ("bot-testing-area", "minibench")

# Metrics each question type is scored on, with their current versions and baselines.
_PLATFORM = tuple(
    (metric, version, COMPARISON_BASELINES[metric])
    for metric, version in IMPLEMENTATION_VERSIONS.items()
)
_LOCAL_BINARY = tuple(
    (metric, LOCAL_VERSIONS[metric], None) for metric in ("local_brier_binary", "local_log_binary")
)
_LOCAL_MC = tuple(
    (metric, LOCAL_VERSIONS[metric], None)
    for metric in ("local_brier_multiclass", "local_log_multiclass")
)
METRICS_FOR = {
    "binary": _LOCAL_BINARY + _PLATFORM,
    "multiple_choice": _LOCAL_MC + _PLATFORM,
    "numeric": _PLATFORM,
    "discrete": _PLATFORM,
}

# Probabilities that sit on, just below and just above every edge, plus the open interval.
_EDGE_ADJACENT = sorted(
    {
        value
        for edge in CALIBRATION_BIN_EDGES
        for value in (edge, math.nextafter(edge, -1.0), math.nextafter(edge, 2.0))
        if 0.0 <= value <= 1.0
    }
)
PROBABILITY = st.one_of(st.sampled_from(_EDGE_ADJACENT), st.floats(0.0, 1.0), st.just(-0.0))

# Score values on one scale with each other so sums and spreads interact (M1-348's lesson),
# plus the extremes that overflow them.
_EXTREME = sys.float_info.max
SCORE_VALUE = st.integers(0, 4).flatmap(
    lambda branch: (
        st.sampled_from([_EXTREME, -_EXTREME, _EXTREME / 2])
        if branch == 0
        else st.sampled_from([0.0, -0.0, 0.1, 1e-300, -1e-300])
        if branch == 1
        else st.floats(-100.0, 100.0)
    )
)


@st.composite
def facts_for(draw: st.DrawFn, record_id: str) -> RecordFacts:
    question_type = draw(st.sampled_from(TYPES))
    status = draw(st.sampled_from(STATUSES))
    kind = draw(st.one_of(st.none(), st.sampled_from(KINDS)))
    resolution = None
    scores: tuple[ScoreFact, ...] = ()
    if kind is not None:
        outcome = draw(st.sampled_from(["yes", "no"])) if question_type == "binary" else "x"
        resolution = ResolutionFact(
            event_id=draw(st.integers(1, 1000)),
            kind=kind,
            outcome=outcome if kind == "resolved" else None,
            observation_sha256="a" * 64,
            source_response_sha256="b" * 64,
            observed_at_utc="2026-09-17T18:00:00.000000+00:00",
        )
        if kind == "resolved":
            chosen = draw(
                st.lists(st.sampled_from(METRICS_FOR[question_type]), unique=True, max_size=6)
            )
            scores = tuple(
                ScoreFact(
                    event_id=index + 1,
                    resolution_event_id=resolution.event_id,
                    metric=metric,
                    implementation_version=version,
                    comparison_baseline=baseline,
                    value=draw(SCORE_VALUE),
                )
                for index, (metric, version, baseline) in enumerate(chosen)
            )
    return RecordFacts(
        record_id=record_id,
        question_id=draw(st.integers(1, 12)),
        post_id=1,
        tournament_id=draw(st.sampled_from(TOURNAMENTS)),
        forecast_version=draw(st.integers(1, 3)),
        parent_record_id=None,
        forecast_sha256="c" * 64,
        generated_at_utc="2026-09-09T12:00:00.000000+00:00",
        question_type=question_type,
        question_domain=draw(st.sampled_from([None, "econ_data", "health"])),
        source_categories=tuple(
            sorted(
                draw(
                    st.sets(
                        st.tuples(st.integers(1, 3), st.sampled_from([None, "a", "b"])),
                        max_size=3,
                    )
                ),
                key=lambda c: (c[0], c[1] or ""),
            )
        ),
        model_provider="openrouter",
        model_name=draw(st.sampled_from(["m1", "m2"])),
        prompt_version="1.2.0",
        prompt_sha256=draw(st.sampled_from(["d" * 64, "e" * 64])),
        reasoning_strategy_tags=tuple(sorted(draw(st.sets(st.sampled_from(TAGS))))),
        evidence_gaps=tuple(
            sorted(draw(st.sets(st.sampled_from(["named_source_absent", "evidence_poor"]))))
        ),
        lifecycle_status=status,
        probability_yes=draw(PROBABILITY) if question_type == "binary" else None,
        resolution=resolution,
        scores=scores,
        stale_score_rows=draw(st.integers(0, 3)),
        model_cost_usd=draw(st.one_of(st.none(), st.floats(0.0, 10.0))),
        model_invocations=None,
    )


@st.composite
def fact_lists(draw: st.DrawFn) -> list[RecordFacts]:
    count = draw(st.integers(0, 12))
    return [draw(facts_for(f"rec-{index:02d}")) for index in range(count)]


def _render(facts: list[RecordFacts]) -> str:
    rows = classify(facts)
    return canonical_json(
        build_report(rows, ledger_schema_version=17, records_sha256="f" * 64), "report"
    )


def _attempt(facts: list[RecordFacts]) -> tuple[tuple[AttributionRow, ...], dict[str, Any]] | None:
    """Classify and build, or None on a ReportError; anything else propagates and fails."""
    try:
        rows = classify(facts)
        return rows, build_report(rows, ledger_schema_version=17, records_sha256="f" * 64)
    except ReportError:
        return None


def _values(axis: str, facts: RecordFacts) -> int:
    """How many groups ``facts`` belongs to on ``axis``, derived from the facts alone."""
    if axis == "source_category":
        return max(1, len({category for category, _ in facts.source_categories}))
    if axis == "reasoning_strategy_tag":
        return max(1, len(set(facts.reasoning_strategy_tags)))
    if axis == "evidence_gap":
        return max(1, len(set(facts.evidence_gaps)))
    return 1


# ── 1-2. never raises outside ReportError; partitions hold ───────────────────


@given(facts=fact_lists())
def test_the_report_never_raises_outside_its_error_and_every_partition_holds(
    facts: list[RecordFacts],
) -> None:
    built = _attempt(facts)
    if built is None:
        event("refused")
        return
    rows, report = built
    included = [row for row in rows if row.included]
    event(f"included: {min(len(included), 3)}")

    population = report["population"]
    assert population["records"] == len(facts)
    assert population["included"] == len(included)
    assert sum(population["states"].values()) == len(included)
    assert set(population["states"]) == set(STATES)

    for axis in report["axes"]:
        total = sum(group["records"] for group in axis["groups"])
        expected = sum(_values(axis["axis"], row.facts) for row in included)
        assert total == expected
        assert axis["overlapping"] is (axis["axis"] in OVERLAPPING_AXES)
        if not axis["overlapping"]:
            assert total == len(included)
        for group in axis["groups"]:
            assert sum(group["states"].values()) == group["records"]
            calibration = group["calibration"]
            assert sum(b["n"] for b in calibration["bins"]) == calibration["n"]
            for cell in group["scores"]:
                assert cell["min"] <= cell["mean"] <= cell["max"]
                assert cell["provenance"] == cell["metric"].split("_", 1)[0]
                assert (cell["comparison_baseline"] is None) == (cell["provenance"] == "local")
                assert cell["small_sample"] is (cell["n"] < 30)
                assert (cell["sample_sd"] is None) == (cell["n"] < 2)
    assert [axis["axis"] for axis in report["axes"]] == list(AXES)


@given(facts=fact_lists())
def test_the_all_group_counts_each_scored_subjects_scores_once(facts: list[RecordFacts]) -> None:
    built = _attempt(facts)
    if built is None or not built[0]:
        return
    rows, report = built
    expected: Counter[str] = Counter(
        score.metric
        for row in rows
        if row.included and row.state == "scored"
        for score in row.facts.scores
    )
    groups = report["axes"][0]["groups"]
    got = Counter({cell["metric"]: cell["n"] for group in groups for cell in group["scores"]})
    assert got == expected
    calibrated = sum(
        1
        for row in rows
        if row.included
        and row.facts.question_type == "binary"
        and row.state in ("scored", "resolved_unscored")
    )
    assert sum(group["calibration"]["n"] for group in groups) == calibrated


@given(facts=fact_lists())
def test_each_question_has_at_most_one_scored_subject(facts: list[RecordFacts]) -> None:
    built = _attempt(facts)
    if built is None:
        return
    rows, _ = built
    posted = [row for row in rows if row.state != "not_posted"]
    subjects = Counter(row.facts.question_id for row in posted if row.state != "superseded")
    assert all(count == 1 for count in subjects.values())
    for row in posted:
        if row.state == "superseded":
            event("superseded")
            later = [
                other
                for other in posted
                if other.facts.question_id == row.facts.question_id
                and (other.facts.forecast_version, other.facts.record_id)
                > (row.facts.forecast_version, row.facts.record_id)
            ]
            assert later


# ── 3-4. order-independence and replay ───────────────────────────────────────


@given(facts=fact_lists(), seed=st.integers(0, 2**32 - 1))
def test_shuffling_the_facts_gives_byte_identical_output(
    facts: list[RecordFacts], seed: int
) -> None:
    try:
        original = _render(facts)
    except ReportError:
        return
    shuffled = list(facts)
    random.Random(seed).shuffle(shuffled)
    assert _render(shuffled) == original


@given(facts=fact_lists())
def test_the_report_is_replay_stable_across_its_persisted_form(facts: list[RecordFacts]) -> None:
    try:
        rendered = _render(facts)
    except ReportError:
        return
    assert canonical_json(json.loads(rendered), "report") == rendered


@given(values=st.lists(SCORE_VALUE, min_size=1, max_size=8), seed=st.integers(0, 2**32 - 1))
def test_a_summary_is_order_independent_and_bounded(values: list[float], seed: int) -> None:
    try:
        summary = summarize_values(values)
    except ReportError:
        event("summary refused")
        return
    shuffled = list(values)
    random.Random(seed).shuffle(shuffled)
    again = summarize_values(shuffled)
    assert canonical_json(summary, "s") == canonical_json(again, "s")
    assert summary["min"] <= summary["mean"] <= summary["max"]
    assert math.copysign(1.0, summary["min"]) == 1.0 or summary["min"] != 0.0
    assert summary["n"] == len(values)


@given(
    values=st.lists(
        st.sampled_from([_EXTREME, -_EXTREME, _EXTREME / 2, 1.0, -0.0]), min_size=2, max_size=5
    )
)
def test_extreme_scores_in_one_cell_arrive_as_report_error(values: list[float]) -> None:
    """Every scored subject carries one of ``values`` in the same platform cell."""
    metric = "platform_spot_peer_score"
    facts = [
        _base(
            record_id=f"rec-{index}",
            question_id=index + 1,
            resolution=ResolutionFact(
                index + 1, "resolved", "x", "a" * 64, "b" * 64, "2026-09-17T18:00:00Z"
            ),
            scores=(
                ScoreFact(1, index + 1, metric, IMPLEMENTATION_VERSIONS[metric], "peer", value),
            ),
        )
        for index, value in enumerate(values)
    ]
    built = _attempt(facts)
    event("refused" if built is None else "summarized")
    if built is not None:
        (cell,) = built[1]["axes"][0]["groups"][0]["scores"]
        assert all(math.isfinite(cell[key]) for key in ("sum", "mean", "min", "max"))


def test_the_mean_of_equal_values_is_that_value() -> None:
    """Three 0.1s: fsum then divide rounds one ulp past 0.1; the clamp puts it back."""
    assert summarize_values([0.1, 0.1, 0.1])["mean"] == 0.1
    assert summarize_values([-0.0, 0.0])["min"] == 0.0
    assert math.copysign(1.0, summarize_values([-0.0, 0.0])["min"]) == 1.0


@pytest.mark.parametrize(
    "values",
    [[_EXTREME, _EXTREME], [-_EXTREME, -_EXTREME], [_EXTREME, -_EXTREME], [math.inf], [math.nan]],
)
def test_an_unrepresentable_summary_is_refused_not_reported(values: list[float]) -> None:
    with pytest.raises(ReportError, match="not"):
        summarize_values(values)


# ── the bins ─────────────────────────────────────────────────────────────────


@given(a=PROBABILITY, b=PROBABILITY)
def test_the_bin_index_is_monotone_and_in_range(a: float, b: float) -> None:
    low, high = sorted((a, b))
    assert 0 <= calibration_bin(low) <= calibration_bin(high) <= len(CALIBRATION_BIN_EDGES) - 2


@pytest.mark.parametrize("index", range(len(CALIBRATION_BIN_EDGES) - 1))
def test_every_edge_opens_its_own_bin_and_the_double_below_it_does_not(index: int) -> None:
    edge = CALIBRATION_BIN_EDGES[index]
    assert calibration_bin(edge) == index
    if index:
        assert calibration_bin(math.nextafter(edge, 0.0)) == index - 1


def test_one_is_in_the_last_bin_and_zeros_in_the_first() -> None:
    assert calibration_bin(1.0) == len(CALIBRATION_BIN_EDGES) - 2
    assert calibration_bin(0.0) == calibration_bin(-0.0) == 0
    assert calibration_bin(0.3) == 3  # the literal 0.3 is the edge's double


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 1.0000000000000002, -1e-300])
def test_a_probability_outside_the_unit_interval_is_refused(value: float) -> None:
    with pytest.raises(ReportError, match=r"\[0, 1\]"):
        calibration_bin(value)


# ── 5. no value leaks ────────────────────────────────────────────────────────


def _base(**overrides: Any) -> RecordFacts:
    fields: dict[str, Any] = {
        "record_id": "rec-1",
        "question_id": 1,
        "post_id": 1,
        "tournament_id": "33125",
        "forecast_version": 1,
        "parent_record_id": None,
        "forecast_sha256": "c" * 64,
        "generated_at_utc": "2026-09-09T12:00:00.000000+00:00",
        "question_type": "numeric",
        "question_domain": None,
        "source_categories": (),
        "model_provider": "openrouter",
        "model_name": "m1",
        "prompt_version": "1.2.0",
        "prompt_sha256": "d" * 64,
        "reasoning_strategy_tags": (),
        "evidence_gaps": (),
        "lifecycle_status": "submitted",
        "probability_yes": None,
        "resolution": None,
        "scores": (),
        "stale_score_rows": 0,
        "model_cost_usd": None,
        "model_invocations": None,
    }
    fields.update(overrides)
    return RecordFacts(**fields)


_RESOLVED = ResolutionFact(1, "resolved", "yes", "a" * 64, "b" * 64, "2026-09-17T18:00:00Z")


@pytest.mark.parametrize(
    "facts",
    [
        _base(lifecycle_status=CANARY),
        _base(resolution=ResolutionFact(1, CANARY, None, "a" * 64, "b" * 64, "t")),
        _base(
            resolution=_RESOLVED,
            scores=(ScoreFact(1, 1, CANARY, f"{CANARY}/1", None, 1.0),),
        ),
        _base(
            resolution=_RESOLVED,
            scores=(
                ScoreFact(
                    1,
                    1,
                    "platform_peer_score",
                    IMPLEMENTATION_VERSIONS["platform_peer_score"],
                    CANARY,
                    1.0,
                ),
            ),
        ),
        _base(question_type="binary", probability_yes=None),
        _base(question_type="binary", probability_yes=5150.0),
        _base(
            question_type="binary",
            probability_yes=0.5,
            resolution=ResolutionFact(1, "resolved", CANARY, "a" * 64, "b" * 64, "t"),
        ),
        _base(probability_yes=0.5),
        _base(model_cost_usd=-5150.0),
        _base(model_cost_usd=math.inf),
        _base(stale_score_rows=-5150),
        _base(resolution=None, scores=(ScoreFact(1, 1, "platform_peer_score", "v", "peer", 1.0),)),
    ],
)
def test_a_refused_fact_never_reaches_the_message(facts: RecordFacts) -> None:
    with pytest.raises(ReportError) as caught:
        classify([facts])
    message = str(caught.value)
    assert CANARY not in message
    assert "5150" not in message


def test_two_facts_for_one_record_are_refused() -> None:
    with pytest.raises(ReportError, match="share one record_id"):
        classify([_base(), _base()])


# ── reach ────────────────────────────────────────────────────────────────────


def test_the_strategy_reaches_every_branch() -> None:
    """Measured: without this the properties above could pass on refusals and empty reports."""
    hits: Counter[str] = Counter()

    @settings(max_examples=400, database=None, derandomize=True)
    @given(facts=fact_lists())
    def draw(facts: list[RecordFacts]) -> None:
        built = _attempt(facts)
        if built is None:
            hits["refused"] += 1
            return
        rows, _ = built
        for row in rows:
            hits[f"state:{row.state}"] += 1
            if not row.included:
                hits["excluded"] += 1
            facts_ = row.facts
            if row.included and len(set(facts_.reasoning_strategy_tags)) > 1:
                hits["multi_tag"] += 1
            if row.included and len({c for c, _ in facts_.source_categories}) > 1:
                hits["multi_category"] += 1
            if row.included and len(facts_.evidence_gaps) > 1:
                hits["multi_gap"] += 1
            p = facts_.probability_yes
            if row.state in ("scored", "resolved_unscored") and p in CALIBRATION_BIN_EDGES:
                hits["calibrated_on_edge"] += 1
            if row.state == "scored" and row.included and len(facts_.scores) > 1:
                hits["scored_with_cells"] += 1

    draw()
    for state in STATES:
        assert hits[f"state:{state}"] >= 20, (state, hits)
    for branch in ("excluded", "multi_tag", "multi_category", "multi_gap"):
        assert hits[branch] >= 20, (branch, hits)
    # An overflowing cell needs two extremes in one cell, which this strategy reaches only
    # rarely; `test_extreme_scores_in_one_cell_arrive_as_report_error` drives it directly.
    assert hits["refused"] >= 1, hits
    assert hits["calibrated_on_edge"] >= 5, hits
    assert hits["scored_with_cells"] >= 20, hits
