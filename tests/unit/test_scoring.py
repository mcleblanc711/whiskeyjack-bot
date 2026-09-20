"""Local Brier and log scores against hand calculations (M4-802).

Every expected value below is written out by hand, never produced by calling the
implementation (the T-904 lesson): Brier values are exact decimal arithmetic shown in the
comment beside them, and every natural log was computed with ``bc -l`` at scale 25, so the
oracle does not share Python's libm with the code under test. Comparisons use a relative
tolerance of 1e-12: an IEEE double carries ~16 significant digits, and ``1.0 - 0.7`` is
``0.30000000000000004``, one ulp from the decimal the hand calculation uses.
"""

from __future__ import annotations

import math
from typing import get_args

import pytest

from whiskeyjack_bot import scoring
from whiskeyjack_bot.scoring import (
    BINARY_METRICS,
    IMPLEMENTATION_VERSIONS,
    LOCAL_METRICS,
    MULTICLASS_METRICS,
    LocalMetric,
    LocalScore,
    ScoreError,
    binary_brier_v1,
    binary_log_v1,
    multiclass_brier_v1,
    multiclass_log_v1,
    recompute,
    score_binary,
    score_multiple_choice,
)

# ln(x) from `echo "scale=25; l(x)" | bc -l`.
LN_0_5 = -0.6931471805599453094172321
LN_0_001 = -6.9077552789821370520539743
LN_0_999 = -0.0010005003335835335001429
LN_0_7 = -0.3566749439387323789126387
LN_0_3 = -1.2039728043259359926227462
LN_0_28 = -1.2729656758128874440961659
LN_0_02 = -3.9120230054281460586187507
LN_0_25 = -1.3862943611198906188344642
LN_0_4 = -0.9162907318741550651835272
LN_0_6 = -0.5108256237659906832055140
# -324*l(10) + l(4.9406564584124654): the smallest positive subnormal double.
LN_SUBNORMAL = -744.440071921381262322560767793659
# -53*l(2): 1.0 - (1 - 2**-53) is exactly 2**-53 (Sterbenz).
LN_2_POW_MINUS_53 = -36.736800569677101399113302437274


def close(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, rel_tol=1e-12, abs_tol=0.0)


# ── binary, by hand ──────────────────────────────────────────────────────────

BINARY_BRIER_CASES = [
    # (p, outcome, (p - o)^2 worked by hand)
    (0.7, "yes", 0.09),  # (0.7 - 1)^2 = 0.3^2
    (0.7, "no", 0.49),  # 0.7^2
    (0.5, "yes", 0.25),
    (0.5, "no", 0.25),
    (0.4, "yes", 0.36),  # 0.6^2
    (0.4, "no", 0.16),
    (0.001, "yes", 0.998001),  # 0.999^2 -- the live floor, wrong way round
    (0.001, "no", 0.000001),  # 0.001^2
    (0.999, "yes", 0.000001),
    (0.999, "no", 0.998001),
    (1.0, "yes", 0.0),
    (0.0, "no", 0.0),
    (0.0, "yes", 1.0),  # the worst binary Brier is exactly 1, and it is finite
    (1.0, "no", 1.0),
]


@pytest.mark.parametrize(("p", "outcome", "expected"), BINARY_BRIER_CASES)
def test_binary_brier_matches_the_hand_calculation(p: float, outcome: str, expected: float) -> None:
    actual = binary_brier_v1(p, outcome)
    assert close(actual, expected) if expected else actual == 0.0


BINARY_LOG_CASES = [
    # (p, outcome, ln of the probability given to what happened)
    (0.5, "yes", LN_0_5),
    (0.5, "no", LN_0_5),
    (0.7, "yes", LN_0_7),
    (0.7, "no", LN_0_3),
    (0.4, "yes", LN_0_4),
    (0.4, "no", LN_0_6),
    (0.001, "yes", LN_0_001),
    (0.001, "no", LN_0_999),
    (0.999, "yes", LN_0_999),
    (0.999, "no", LN_0_001),
    (1.0, "yes", 0.0),
    (0.0, "no", 0.0),
]


@pytest.mark.parametrize(("p", "outcome", "expected"), BINARY_LOG_CASES)
def test_binary_log_is_the_natural_log_of_what_happened(
    p: float, outcome: str, expected: float
) -> None:
    actual = binary_log_v1(p, outcome)
    assert close(actual, expected) if expected else actual == 0.0


def test_the_log_base_is_e_not_2_or_10() -> None:
    """ln(0.5) is -0.693...; log2 would give -1 and log10 -0.301. Pinned separately."""
    assert close(binary_log_v1(0.5, "yes"), LN_0_5)
    assert not close(binary_log_v1(0.5, "yes"), -1.0)


def test_the_binary_brier_is_the_single_term_convention() -> None:
    """(p - o)^2, not the two-category sum, which is twice it: 0.18 would be that one."""
    assert close(binary_brier_v1(0.7, "yes"), 0.09)
    assert not close(binary_brier_v1(0.7, "yes"), 0.18)


# ── extreme probabilities ────────────────────────────────────────────────────


def test_the_smallest_positive_probability_gives_a_finite_log_score() -> None:
    subnormal = math.ulp(0.0)
    assert subnormal == 5e-324
    actual = binary_log_v1(subnormal, "yes")
    assert math.isfinite(actual) and close(actual, LN_SUBNORMAL)
    assert close(multiclass_log_v1((("a", subnormal), ("b", 1.0)), "a"), LN_SUBNORMAL)


def test_the_largest_probability_below_one_gives_a_finite_log_score_on_no() -> None:
    p = 1.0 - 2.0**-53
    assert 1.0 - p == 2.0**-53
    actual = binary_log_v1(p, "no")
    assert math.isfinite(actual) and close(actual, LN_2_POW_MINUS_53)


@pytest.mark.parametrize(("p", "outcome"), [(0.0, "yes"), (1.0, "no")])
def test_a_zero_probability_on_what_happened_is_refused_not_clamped(p: float, outcome: str) -> None:
    """The one input with no finite log score. A clamp would report a number nobody forecast."""
    with pytest.raises(ScoreError, match="undefined"):
        binary_log_v1(p, outcome)
    with pytest.raises(ScoreError, match="undefined"):
        score_binary(p, outcome)
    # Brier stays defined at the same input, and is its maximum.
    assert binary_brier_v1(p, outcome) == 1.0


def test_a_zero_probability_on_the_realized_option_is_refused_not_clamped() -> None:
    with pytest.raises(ScoreError, match="undefined"):
        multiclass_log_v1((("a", 0.0), ("b", 1.0)), "a")
    assert multiclass_brier_v1((("a", 0.0), ("b", 1.0)), "a") == 2.0


# ── multiple choice, by hand ─────────────────────────────────────────────────

# The live ledger's one multiple-choice forecast (bot-testing-area question 43331).
LIVE_SHAPE = (("Democrats", 0.7), ("Republicans", 0.28), ("Other", 0.02))

MULTICLASS_CASES = [
    # (options, outcome, Brier worked by hand, ln of the realized option)
    # 0.3^2 + 0.28^2 + 0.02^2 = 0.09 + 0.0784 + 0.0004
    (LIVE_SHAPE, "Democrats", 0.1688, LN_0_7),
    # 0.7^2 + 0.72^2 + 0.02^2 = 0.49 + 0.5184 + 0.0004
    (LIVE_SHAPE, "Republicans", 1.0088, LN_0_28),
    # 0.7^2 + 0.28^2 + 0.98^2 = 0.49 + 0.0784 + 0.9604
    (LIVE_SHAPE, "Other", 1.5288, LN_0_02),
    # 0.75^2 + 3 * 0.25^2 = 0.5625 + 0.1875
    (tuple((label, 0.25) for label in "ABCD"), "C", 0.75, LN_0_25),
    # 0.999^2 + 0.999^2: the worst a forecast inside the live bounds can do on two options
    ((("A", 0.001), ("B", 0.999)), "A", 1.996002, LN_0_001),
    # 0.001^2 + 0.001^2
    ((("A", 0.001), ("B", 0.999)), "B", 0.000002, LN_0_999),
]


@pytest.mark.parametrize(("options", "outcome", "brier", "log"), MULTICLASS_CASES)
def test_multiclass_scores_match_the_hand_calculation(
    options: tuple[tuple[str, float], ...], outcome: str, brier: float, log: float
) -> None:
    assert close(multiclass_brier_v1(options, outcome), brier)
    assert close(multiclass_log_v1(options, outcome), log)


def test_the_realized_option_is_found_by_label_not_by_position() -> None:
    """The platform reorders options (a refetch reports a `label_order`); the score must not.

    Reversing the list moves "Other" from last to first. A scorer that took the outcome's
    index in one ordering and read the other would score "Other" as "Democrats" (0.1688).
    """
    reordered = tuple(reversed(LIVE_SHAPE))
    assert close(multiclass_brier_v1(reordered, "Other"), 1.5288)
    assert close(multiclass_log_v1(reordered, "Other"), LN_0_02)


def test_a_two_option_multiclass_brier_is_twice_the_binary_brier() -> None:
    """The two conventions, related by hand: (p-1)^2 + (1-p)^2 = 2(1-p)^2 for a yes."""
    binary = binary_brier_v1(0.7, "yes")
    multiclass = multiclass_brier_v1((("yes", 0.7), ("no", 0.3)), "yes")
    assert close(binary, 0.09) and close(multiclass, 0.18)


# ── what the scorer writes ───────────────────────────────────────────────────


def test_score_binary_writes_every_binary_metric_in_order() -> None:
    scores = score_binary(0.7, "no")
    assert [score.metric for score in scores] == list(BINARY_METRICS)
    assert [score.implementation_version for score in scores] == [
        "local_brier_binary/1",
        "local_log_binary/1",
    ]
    assert close(scores[0].value, 0.49) and close(scores[1].value, LN_0_3)


def test_score_multiple_choice_writes_every_multiclass_metric_in_order() -> None:
    scores = score_multiple_choice(LIVE_SHAPE, "Republicans")
    assert [score.metric for score in scores] == list(MULTICLASS_METRICS)
    assert [score.implementation_version for score in scores] == [
        "local_brier_multiclass/1",
        "local_log_multiclass/1",
    ]
    assert close(scores[0].value, 1.0088) and close(scores[1].value, LN_0_28)
    assert all(type(score) is LocalScore for score in scores)


def test_no_metric_name_can_be_read_as_a_metaculus_score() -> None:
    """D30 by analogy: every name says local; none says baseline, peer or metaculus."""
    for metric in get_args(LocalMetric):
        assert metric.startswith("local_")
        assert not any(word in metric for word in ("baseline", "peer", "metaculus", "relative"))


# ── versions ─────────────────────────────────────────────────────────────────


def test_every_metric_has_exactly_one_current_version_named_for_it() -> None:
    """015's trigger requires `implementation_version` to start with `<metric>/`."""
    assert set(IMPLEMENTATION_VERSIONS) == LOCAL_METRICS == set(get_args(LocalMetric))
    assert set(BINARY_METRICS) | set(MULTICLASS_METRICS) == LOCAL_METRICS
    for metric, version in IMPLEMENTATION_VERSIONS.items():
        assert version.startswith(f"{metric}/") and len(version) > len(metric) + 1
        assert scoring._IMPLEMENTATIONS[version][0] == metric


# The golden table for each registered version. A version's table is never edited: a change
# to what a function returns is a new version string with a new table, and the old version
# stays registered so rows written under it still recompute. Keyed by the version string so
# a behaviour change that kept its version fails here.
GOLDEN: dict[str, list[tuple[object, str, float]]] = {
    "local_brier_binary/1": [(0.7, "yes", 0.09), (0.001, "yes", 0.998001)],
    "local_log_binary/1": [(0.7, "no", LN_0_3), (0.999, "no", LN_0_001)],
    "local_brier_multiclass/1": [(LIVE_SHAPE, "Other", 1.5288)],
    "local_log_multiclass/1": [(LIVE_SHAPE, "Republicans", LN_0_28)],
}


def test_every_registered_version_has_a_golden_table_and_still_meets_it() -> None:
    assert set(GOLDEN) == set(scoring._IMPLEMENTATIONS)
    for version, cases in GOLDEN.items():
        metric = scoring._IMPLEMENTATIONS[version][0]
        for prediction, outcome, expected in cases:
            assert close(recompute(version, metric, prediction, outcome), expected), version


def test_recompute_refuses_an_unregistered_version_and_a_mismatched_metric() -> None:
    with pytest.raises(ScoreError, match="not a registered"):
        recompute("local_brier_binary/999", "local_brier_binary", 0.5, "yes")
    with pytest.raises(ScoreError, match="not a registered"):
        recompute(None, "local_brier_binary", 0.5, "yes")
    with pytest.raises(ScoreError, match="does not match"):
        recompute("local_brier_binary/1", "local_log_binary", 0.5, "yes")


# ── refusals, and no value in any message ────────────────────────────────────

PROBE_PROBABILITY = 0.123456789
PROBE_LABEL = "Zanzibar-probe-label"


@pytest.mark.parametrize(
    "probability",
    [True, 1, None, "0.5", float("nan"), float("inf"), -float("inf"), -0.001, 1.001],
)
def test_a_probability_that_is_not_a_finite_float_in_range_is_refused(probability: object) -> None:
    for function in (binary_brier_v1, binary_log_v1):
        with pytest.raises(ScoreError):
            function(probability, "yes")
    with pytest.raises(ScoreError):
        multiclass_brier_v1((("a", probability), ("b", 0.5)), "a")


@pytest.mark.parametrize("outcome", ["Yes", "YES", " yes", "", None, 1, "annulled", "true"])
def test_a_binary_outcome_other_than_yes_or_no_is_refused(outcome: object) -> None:
    for function in (binary_brier_v1, binary_log_v1):
        with pytest.raises(ScoreError):
            function(0.5, outcome)


@pytest.mark.parametrize(
    ("options", "outcome", "rule"),
    [
        ((), "a", "must not be empty"),
        ("ab", "a", "sequence of"),
        ({"a": 1.0}, "a", "sequence of"),
        ((["a", 1.0],), "a", "sequence of"),
        ((("a", 0.5, "x"),), "a", "sequence of"),
        (((1, 1.0),), "a", "label must be"),
        ((("", 1.0),), "a", "label must be"),
        ((("a", 0.5), ("a", 0.5)), "a", "more than once"),
        ((("a", 0.5), ("b", 0.4)), "a", "sum to 1"),
        ((("a", 0.5), ("b", 0.500002)), "a", "sum to 1"),
        ((("a", 0.5), ("b", 0.5)), "c", "not an option this forecast priced"),
        ((("a", 0.5), ("b", 0.5)), "", "outcome must be"),
    ],
)
def test_a_malformed_multiclass_input_is_refused_by_rule(
    options: object, outcome: object, rule: str
) -> None:
    for function in (multiclass_brier_v1, multiclass_log_v1):
        with pytest.raises(ScoreError, match=rule):
            function(options, outcome)


def test_a_distribution_inside_the_tolerance_is_scored_without_renormalizing() -> None:
    """0.5 + 0.5000005 is inside 1e-6. The Brier uses the numbers as stored:
    (0.5-1)^2 + 0.5000005^2 = 0.25 + 0.25000050000025 = 0.50000050000025."""
    assert close(multiclass_brier_v1((("a", 0.5), ("b", 0.5000005)), "a"), 0.50000050000025)


def test_no_refusal_message_carries_a_probability_label_or_outcome() -> None:
    probes = [
        lambda: binary_log_v1(PROBE_PROBABILITY, PROBE_LABEL),
        lambda: multiclass_brier_v1(((PROBE_LABEL, PROBE_PROBABILITY),), "a"),
        lambda: multiclass_log_v1(((PROBE_LABEL, 0.5), ("b", 0.5)), f"{PROBE_LABEL}-other"),
        lambda: multiclass_log_v1(((PROBE_LABEL, 0.5), (PROBE_LABEL, 0.5)), PROBE_LABEL),
        lambda: multiclass_log_v1(((PROBE_LABEL, 0.0), ("b", 1.0)), PROBE_LABEL),
        lambda: recompute(PROBE_LABEL, "local_log_binary", PROBE_PROBABILITY, "yes"),
    ]
    for probe in probes:
        with pytest.raises(ScoreError) as excinfo:
            probe()
        message = str(excinfo.value)
        assert PROBE_LABEL not in message and "Zanzibar" not in message
        assert "0.123" not in message and "123456789" not in message


def test_the_sum_refusal_renders_the_tolerance_and_not_the_forecast() -> None:
    """M1-346: *"sum to 1 within 1e-06"* carries `_SUM_TOLERANCE`, not a probability.

    The string is invariant to the probabilities passed in, which is what makes it a
    constant and not a leak -- and it is why a no-leak check written as
    ``repr(probability) not in message`` fails on a drawn ``1e-06`` for a reason that has
    nothing to do with leaking. That failure reddened a *required* gate on branches
    touching neither `scoring.py` nor the property (2 of 40 fresh runs of
    `tests/property/test_scoring_properties.py::test_no_refusal_reprints_a_label_outcome_or_probability`,
    2026-09-20), so the property now draws canary probabilities. This pins the fact that
    made it necessary: if the tolerance ever stops being rendered, or starts being rendered
    beside the value, the canary set has to be re-checked against the new text.
    """
    messages = []
    for probability in (1e-06, PROBE_PROBABILITY):
        with pytest.raises(ScoreError) as excinfo:
            multiclass_brier_v1(((PROBE_LABEL, probability), ("b", 0.5)), PROBE_LABEL)
        messages.append(str(excinfo.value))
    colliding, canary = messages
    assert colliding == canary  # same text for two different probabilities: a constant
    assert "option probabilities must sum to 1 within 1e-06" == colliding
    assert repr(1e-06) in colliding  # the tolerance's own repr, character for character
    assert repr(PROBE_PROBABILITY) not in canary
