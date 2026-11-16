"""The local log score for numeric and discrete forecasts against hand calculations (M4-809).

Every expected value is written out by hand, never produced by calling the implementation
(the T-904 lesson). Natural logs come from ``bc -l`` at scale 25, so the oracle does not share
Python's libm with the code under test. The platform's own numbers are the second oracle:
``tests/fixtures/scoring/continuous_platform_scores.json`` holds CDFs this account posted, the
scaling Metaculus reported for each question, the resolution, and the spot baseline score
Metaculus computed -- a witness outside this program.
"""

from __future__ import annotations

import array
import collections
import json
import math
from pathlib import Path
from typing import Any

import pytest

from whiskeyjack_bot import scoring
from whiskeyjack_bot.scoring import (
    ABOVE_UPPER_BOUND,
    BELOW_LOWER_BOUND,
    CONTINUOUS_LOG_VERSION,
    IMPLEMENTATION_VERSIONS,
    LOCAL_METRICS,
    ContinuousScore,
    ScoreError,
    continuous_log_v1,
    peer_points_per_nat,
    platform_baseline_points,
    recompute,
    score_continuous,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "scoring" / "continuous_platform_scores.json"

# ln(x) from `echo "scale=25; l(x)" | bc -l`.
LN_0_05 = -2.9957322735539909934352235
LN_0_2 = -1.6094379124341003746007593
LN_0_25 = -1.3862943611198906188344642

# A five-point CDF over four inbound buckets, both bounds open. Its PMF, by hand:
#   below 0.05 | 0.20 | 0.25 | 0.25 | 0.20 | above 0.05
CDF = [0.05, 0.25, 0.5, 0.75, 0.95]
LINEAR: dict[str, Any] = {
    "range_min": 0.0,
    "range_max": 100.0,
    "zero_point": None,
    "open_lower_bound": True,
    "open_upper_bound": True,
    "inbound_outcome_count": 4,
}


_DEFAULT_CDF: Any = object()


def _score(resolution: object, cdf: object = _DEFAULT_CDF, **overrides: object) -> ContinuousScore:
    chosen = CDF if cdf is _DEFAULT_CDF else cdf
    return score_continuous(chosen, resolution, **{**LINEAR, **overrides})


# ── the bucket a resolution lands in ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("resolution", "bucket", "log_mass"),
    [
        # 30 is 0.3 of the range; 0.3 * 4 = 1.2, so the second inbound bucket (0.25 to 0.5).
        ("30", 2, LN_0_25),
        ("30.0", 2, LN_0_25),
        ("3e1", 2, LN_0_25),
        ("60", 3, LN_0_25),
        ("10", 1, LN_0_2),
        ("90", 4, LN_0_2),
        # Strictly outside the range, as a number.
        ("-5", 0, LN_0_05),
        ("100.5", 5, LN_0_05),
        (BELOW_LOWER_BOUND, 0, LN_0_05),
        (ABOVE_UPPER_BOUND, 5, LN_0_05),
    ],
)
def test_a_resolution_scores_the_mass_of_its_bucket(
    resolution: str, bucket: int, log_mass: float
) -> None:
    score = _score(resolution)
    assert score.bucket == bucket
    assert score.bucket_count == 6
    assert score.log_mass == pytest.approx(log_mass, rel=1e-12)
    assert score.implementation_version == CONTINUOUS_LOG_VERSION
    assert continuous_log_v1(CDF, resolution, **LINEAR) == score.log_mass


@pytest.mark.parametrize(
    ("resolution", "bucket", "log_mass"),
    [
        # Exactly on an inner edge: the platform puts the outcome in the LOWER bucket.
        ("25", 1, LN_0_2),
        ("50", 2, LN_0_25),
        ("75", 3, LN_0_25),
        # Exactly the lower bound is inbound (bucket 1), never "below" (bucket 0).
        ("0", 1, LN_0_2),
        ("-0.0", 1, LN_0_2),
        # Exactly the upper bound is the last inbound bucket, never "above".
        ("100", 4, LN_0_2),
    ],
)
def test_a_resolution_on_a_bucket_edge_belongs_to_the_lower_bucket(
    resolution: str, bucket: int, log_mass: float
) -> None:
    score = _score(resolution)
    assert score.bucket == bucket
    assert score.log_mass == pytest.approx(log_mass, rel=1e-12)


def test_the_two_live_edge_cases_land_where_the_platform_put_them() -> None:
    """q45747 and q45962, the two records a floor-plus-one rule scores in the wrong bucket.

    215.65 in [190, 235] is 0.57 of the range and 0.57 * 200 computes to 114.00000000000003;
    70250 in [65000, 95000] is 0.175 and 0.175 * 200 is exactly 35.0. The platform's spot
    baseline scores are only reproduced from buckets 114 and 35.
    """
    cases = {case["question_id"]: case for case in _fixture_cases()}
    for question_id, bucket in ((45747, 114), (45962, 35)):
        case = cases[question_id]
        assert _score_case(case).bucket == bucket


def test_a_closed_bound_with_no_mass_outside_it_cannot_score_an_outcome_there() -> None:
    closed = [0.0, 0.25, 0.5, 0.75, 1.0]
    with pytest.raises(ScoreError, match="probability 0"):
        _score(BELOW_LOWER_BOUND, closed, open_lower_bound=False, open_upper_bound=False)
    with pytest.raises(ScoreError, match="probability 0"):
        _score("101", closed, open_lower_bound=False, open_upper_bound=False)
    # An integer 0 and 1 are what the platform stores at a closed bound.
    assert _score("30", [0, 0.25, 0.5, 0.75, 1], open_lower_bound=False).bucket == 2


def test_the_default_inbound_count_is_the_platforms_two_hundred() -> None:
    cdf = [index / 200 for index in range(201)]
    score = score_continuous(
        cdf,
        "50",
        range_min=0,
        range_max=100,
        zero_point=None,
        open_lower_bound=False,
        open_upper_bound=False,
    )
    # 50 is the edge between buckets 100 and 101: the lower one.
    assert (score.bucket, score.bucket_count) == (100, 202)


# ── log-scaled questions ─────────────────────────────────────────────────────

# zero_point 0 below the range [1, 100]: deriv_ratio = (100 - 0) / (1 - 0) = 100, and the
# value at unscaled location u is 100 ** u. From `echo "scale=30; e(0.6*l(100))" | bc -l`.
VALUE_AT_0_6_ZERO_BELOW = "15.848931924611134852"
# zero_point 110 above the range [10, 100]: deriv_ratio = (100 - 110) / (10 - 110) = 0.1, and
# the value at u is 10 + 90 * (0.1 ** u - 1) / (0.1 - 1). bc gives, for u = 0.3:
VALUE_AT_0_3_ZERO_ABOVE = "59.881276637272771499"


def test_a_log_scale_with_its_zero_point_below_the_range() -> None:
    scale = {"range_min": 1.0, "range_max": 100.0, "zero_point": 0.0}
    # u = 0.6, and 0.6 * 4 = 2.4: the third inbound bucket.
    assert _score(VALUE_AT_0_6_ZERO_BELOW, **scale).bucket == 3
    # The same value on a linear scale is 0.15 of the range: the first inbound bucket.
    assert _score(VALUE_AT_0_6_ZERO_BELOW, range_min=1.0, range_max=100.0).bucket == 1
    # 10 is u = 0.5 on this scale, an edge: the lower bucket.
    assert _score("10", **scale).bucket == 2
    assert _score("1", **scale).bucket == 1
    assert _score("100", **scale).bucket == 4
    assert _score("0.5", **scale).bucket == 0
    assert _score("250", **scale).bucket == 5
    # At or beyond the zero point the log scale has no location at all; it is below.
    assert _score("0", **scale).bucket == 0
    assert _score("-3", **scale).bucket == 0


def test_a_log_scale_with_its_zero_point_above_the_range() -> None:
    scale = {"range_min": 10.0, "range_max": 100.0, "zero_point": 110.0}
    # u = 0.3, and 0.3 * 4 = 1.2: the second inbound bucket.
    assert _score(VALUE_AT_0_3_ZERO_ABOVE, **scale).bucket == 2
    # Linear, the same value is 0.554 of the range: the third inbound bucket.
    assert _score(VALUE_AT_0_3_ZERO_ABOVE, range_min=10.0, range_max=100.0).bucket == 3
    assert _score("5", **scale).bucket == 0
    assert _score("105", **scale).bucket == 5
    # At or beyond a zero point above the range, the outcome is above.
    assert _score("110", **scale).bucket == 5
    assert _score("400", **scale).bucket == 5


def test_a_value_one_float_outside_a_bound_is_outside_it_on_every_scale() -> None:
    """Found by the property pass: unscaling first rounds such a value onto the bound.

    On the log scale below, ``(value - low) * (ratio - 1) + width`` loses a difference of
    3.6e-229 against a width of 1, so the location computes to exactly 0 and the value would
    be scored in the first inbound bucket. The platform writes a value below ``range_min``
    as ``below_lower_bound``; the comparison is made directly.
    """
    tiny = {"range_min": 3.566930108161601e-229, "range_max": 1.0, "zero_point": -1.0}
    assert _score("0.0", **tiny).bucket == 0
    assert _score("3.566930108161601e-229", **tiny).bucket == 1
    for scale in (
        {"range_min": 1.0, "range_max": 100.0, "zero_point": None},
        {"range_min": 1.0, "range_max": 100.0, "zero_point": 0.0},
        {"range_min": 10.0, "range_max": 100.0, "zero_point": 110.0},
    ):
        low, high = scale["range_min"], scale["range_max"]
        assert isinstance(low, float) and isinstance(high, float)
        assert _score(repr(math.nextafter(low, -math.inf)), **scale).bucket == 0
        assert _score(repr(low), **scale).bucket == 1
        assert _score(repr(high), **scale).bucket == 4
        assert _score(repr(math.nextafter(high, math.inf)), **scale).bucket == 5


# ── the platform's own scores ────────────────────────────────────────────────


def _fixture_cases() -> list[dict[str, Any]]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases = document["cases"]
    assert isinstance(cases, list)
    return cases


def _score_case(case: dict[str, Any]) -> ContinuousScore:
    return score_continuous(
        case["cdf"],
        case["resolution"],
        range_min=case["range_min"],
        range_max=case["range_max"],
        zero_point=case["zero_point"],
        open_lower_bound=case["open_lower_bound"],
        open_upper_bound=case["open_upper_bound"],
        inbound_outcome_count=case["inbound_outcome_count"],
    )


def test_the_fixture_covers_what_the_row_asks_for() -> None:
    cases = _fixture_cases()
    assert len(cases) >= 30
    kinds = {case["question_type"] for case in cases}
    assert kinds == {"numeric", "discrete"}
    assert any(case["resolution"] == BELOW_LOWER_BOUND for case in cases)
    assert {(c["open_lower_bound"], c["open_upper_bound"]) for c in cases} >= {
        (True, True),
        (False, True),
        (False, False),
    }
    assert {45747, 45962} <= {case["question_id"] for case in cases}
    # Numbers only: a case holds no text beyond its type and its resolution string.
    for case in cases:
        assert set(case) == {
            "question_id",
            "question_type",
            "cdf",
            "range_min",
            "range_max",
            "zero_point",
            "open_lower_bound",
            "open_upper_bound",
            "inbound_outcome_count",
            "resolution",
            "platform_spot_baseline_score",
        }


def test_every_fixture_case_reproduces_the_platforms_spot_baseline_score() -> None:
    worst = 0.0
    for case in _fixture_cases():
        points = platform_baseline_points(_score_case(case))
        worst = max(worst, abs(points - case["platform_spot_baseline_score"]))
    # Measured 4.3e-14 over all 105 live records on 2026-11-15; the tolerance leaves room
    # for a libm that rounds the last bit differently, and nothing else.
    assert worst < 1e-9


# ── the platform's baseline scale and the paired identity ────────────────────


def test_the_baseline_scale_by_hand() -> None:
    # In range, two open bounds: baseline (1 - 0.05 * 2) / 4 = 0.225.
    # `echo "scale=25; 100*(l(0.25)-l(0.9/4))/2" | bc -l`
    assert platform_baseline_points(_score("30")) == pytest.approx(
        5.2680257828913150613750500, rel=1e-12
    )
    # Out of range: baseline 0.05, and this forecast put exactly 0.05 there.
    assert platform_baseline_points(_score(BELOW_LOWER_BOUND)) == pytest.approx(0.0, abs=1e-12)
    # The same above the upper bound: only a property reached that bucket's baseline before.
    assert platform_baseline_points(_score(ABOVE_UPPER_BOUND)) == pytest.approx(0.0, abs=1e-12)
    assert _score(ABOVE_UPPER_BOUND).out_of_range and _score(BELOW_LOWER_BOUND).out_of_range
    assert not _score("30").out_of_range
    # One open bound: baseline (1 - 0.05) / 4 = 0.2375; the mass in bucket 1 is 0.2.
    one_open = _score("10", [0.0, 0.2, 0.45, 0.7, 0.95], open_lower_bound=False)
    assert platform_baseline_points(one_open) == pytest.approx(
        -8.5925128463329611170049500, rel=1e-12
    )
    # No open bound: baseline 1 / 4.
    closed = _score("10", [0.0, 0.2, 0.5, 0.8, 1.0], open_lower_bound=False, open_upper_bound=False)
    assert platform_baseline_points(closed) == pytest.approx(
        -11.1571775657104877883147550, rel=1e-12
    )


def _platform_spot_peer(mine: float, others: list[float], *, continuous: bool) -> float:
    """Metaculus's spot peer score, written from ``scoring/score_math.py`` and not from here.

    ``100 * (n / (n - 1)) * ln(p / gm)`` with the geometric mean over all ``n`` forecasters,
    this one included, halved for a continuous question.
    """
    everyone = [mine, *others]
    count = len(everyone)
    geometric_mean = math.exp(sum(math.log(p) for p in everyone) / count)
    points = 100.0 * (count / (count - 1)) * math.log(mine / geometric_mean)
    return points / 2.0 if continuous else points


@pytest.mark.parametrize("question_type", ["numeric", "discrete", "binary", "multiple_choice"])
def test_two_of_our_forecasts_differ_in_peer_score_by_the_factor_times_their_log_difference(
    question_type: str,
) -> None:
    continuous = question_type in ("numeric", "discrete")
    others = [0.31, 0.02, 0.44, 0.17, 0.09]
    first, second = 0.25, 0.2
    difference = _platform_spot_peer(first, others, continuous=continuous) - _platform_spot_peer(
        second, others, continuous=continuous
    )
    factor = peer_points_per_nat(question_type)
    assert factor == (50.0 if continuous else 100.0)
    assert difference == pytest.approx(factor * (LN_0_25 - LN_0_2), rel=1e-12)


def test_the_identity_holds_for_two_cdfs_on_one_question() -> None:
    sharper = [0.02, 0.12, 0.62, 0.88, 0.98]  # 0.5 on the bucket 30 falls in
    first, second = _score("30"), _score("30", sharper)
    others = [0.3, 0.05, 0.4]
    difference = _platform_spot_peer(
        math.exp(first.log_mass), others, continuous=True
    ) - _platform_spot_peer(math.exp(second.log_mass), others, continuous=True)
    assert difference == pytest.approx(
        peer_points_per_nat("numeric") * (first.log_mass - second.log_mass), rel=1e-12
    )
    # And the baseline scale differs by the same amount: both arms share one baseline.
    assert platform_baseline_points(first) - platform_baseline_points(second) == pytest.approx(
        difference, rel=1e-12
    )


def test_peer_points_per_nat_refuses_a_type_it_does_not_know() -> None:
    for unknown in ("date", "", "Numeric", None, 3):
        with pytest.raises(ScoreError):
            peer_points_per_nat(unknown)


# ── not a stored metric (AC4) ────────────────────────────────────────────────


def test_the_continuous_score_is_not_a_stored_metric() -> None:
    """017's trigger closes the ``score_events`` vocabulary; this score is not in it."""
    assert CONTINUOUS_LOG_VERSION not in IMPLEMENTATION_VERSIONS.values()
    assert CONTINUOUS_LOG_VERSION.split("/")[0] not in LOCAL_METRICS
    with pytest.raises(ScoreError):
        recompute(CONTINUOUS_LOG_VERSION, "local_log_continuous", CDF, "30")
    assert not hasattr(scoring, "CONTINUOUS_METRICS")


# ── refusals ─────────────────────────────────────────────────────────────────

CANARY = 0.123456789
CANARY_TEXT = "LEAKCANARY7"


@pytest.mark.parametrize(
    "cdf",
    [
        None,
        "0.1,0.2",
        (x for x in CDF),
        {0.05, 0.25},
        [0.05, 0.25, 0.5, 0.75],  # one point short
        [0.05, 0.25, 0.5, 0.75, 0.95, 0.99],  # one too many
        [0.05, 0.25, 0.5, 0.45, 0.95],  # decreases
        [0.05, 0.25, 0.5, 0.75, 1.5],
        [-0.1, 0.25, 0.5, 0.75, 0.95],
        [0.05, 0.25, math.nan, 0.75, 0.95],
        [0.05, 0.25, math.inf, 0.75, 0.95],
        [0.05, 0.25, "0.5", 0.75, 0.95],
        [0.05, 0.25, None, 0.75, 0.95],
        [False, 0.25, 0.5, 0.75, True],
        [0.05, 0.25, 0.5, 0.75, 10**400],
    ],
)
def test_a_malformed_cdf_is_refused(cdf: object) -> None:
    with pytest.raises(ScoreError):
        _score("30", cdf)


@pytest.mark.parametrize(
    "resolution",
    [
        None,
        30,
        30.0,
        b"30",
        "",
        " 30",
        "30 ",
        "3_0",
        "nan",
        "inf",
        "-Infinity",
        "1e999",
        "0x1p3",
        "٣٠",  # Arabic-Indic digits, which float() accepts
        "annulled",
        "ambiguous",
        "yes",
        "Below_Lower_Bound",
        "3" * 65,
    ],
)
def test_a_malformed_resolution_is_refused(resolution: object) -> None:
    with pytest.raises(ScoreError):
        _score(resolution)


@pytest.mark.parametrize(
    "overrides",
    [
        {"range_min": None},
        {"range_max": "100"},
        {"range_min": True},
        {"range_min": math.nan},
        {"range_max": math.inf},
        {"range_min": 100.0, "range_max": 100.0},
        {"range_min": 100.0, "range_max": 0.0},
        {"range_min": -1.7e308, "range_max": 1.7e308},  # the width overflows
        {"zero_point": "0"},
        {"zero_point": math.nan},
        {"zero_point": 0.0},  # on the lower bound
        {"zero_point": 100.0},  # on the upper bound
        {"zero_point": 50.0},  # inside the range
        {"zero_point": -1e300},  # so far away that the scale's ratio is exactly 1
        {"range_max": 1.7e308, "zero_point": -1.7e308},  # the ratio overflows
        {"open_lower_bound": 1},
        {"open_upper_bound": None},
        {"open_lower_bound": "true"},
        {"inbound_outcome_count": 0},
        {"inbound_outcome_count": -4},
        {"inbound_outcome_count": 4.0},
        {"inbound_outcome_count": True},
        {"inbound_outcome_count": "4"},
        {"inbound_outcome_count": 5},  # does not match the CDF
        {"inbound_outcome_count": 10_001},  # beyond what this module scores
    ],
)
def test_malformed_scaling_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ScoreError):
        _score("30", **overrides)


class _ListSubclass(list[float]):
    """A list in every way but its exact type."""


@pytest.mark.parametrize(
    "container",
    [
        _ListSubclass(CDF),
        dict.fromkeys(CDF),
        collections.deque(CDF),
        array.array("d", CDF),
        frozenset(CDF),
        range(5),
    ],
)
def test_a_container_of_the_right_length_that_is_not_a_list_or_tuple_is_refused(
    container: object,
) -> None:
    """Five items each, so the length rule passes and only the type rule can refuse.

    A mutation run found the gap: every other malformed container in this file also had the
    wrong length, so a type rule loosened to "anything with a length" survived.
    """
    assert len(container) == len(CDF)  # type: ignore[arg-type]
    with pytest.raises(ScoreError, match="cdf must be a list of numbers"):
        _score("30", container)
    # The two exact types are both accepted and agree.
    assert _score("30", tuple(CDF)) == _score("30", list(CDF))


def test_a_count_beyond_the_limit_is_refused_even_with_a_cdf_that_would_match_it() -> None:
    """Found by the same run: with a five-point CDF the length rule refused first."""
    inbound = 10_001
    cdf = [(index + 1) / (inbound + 2) for index in range(inbound + 1)]
    with pytest.raises(ScoreError, match="inbound_outcome_count"):
        _score("30", cdf, inbound_outcome_count=inbound)


def test_a_count_of_zero_is_refused_even_with_a_cdf_that_would_match_it() -> None:
    """The length check alone would pass a one-point CDF for a count of zero."""
    with pytest.raises(ScoreError, match="inbound_outcome_count"):
        _score("30", [0.5], inbound_outcome_count=0)


def test_the_upper_bound_of_a_log_scale_whose_ratio_vanishes_against_one() -> None:
    """Zero point 1e-20 above the upper bound of ``[-1, 0]``: the ratio is 1e-20, not zero.

    ``ratio - 1.0`` is then exactly ``-1.0``, so at the upper bound the platform's argument
    ``(value - low) * (ratio - 1) + width`` is ``-1 + 1``: zero, whose logarithm does not
    exist (``math.log`` raises ``ValueError``; numpy gives ``-inf``). Mathematically it is
    ``width * ratio`` and the location is 1: the last inbound bucket.
    """
    scale = {"range_min": -1.0, "range_max": 0.0, "zero_point": 1e-20}
    assert _score("0", **scale).bucket == 4
    assert _score("-0.0", **scale).bucket == 4
    # ln(0.5) / ln(1e-20) is about 0.015 of the range: the first inbound bucket.
    assert _score("-0.5", **scale).bucket == 1
    assert _score("-1", **scale).bucket == 1
    assert _score("-1.5", **scale).bucket == 0
    assert _score("0.5", **scale).bucket == 5


def test_a_log_scale_whose_ratio_underflows_to_zero_is_refused() -> None:
    """A zero point one denormal above the upper bound of a range 1e308 wide.

    The resolution is inside the range, so the refusal has to come from the scale: an
    outside value would be placed by comparison before the scale was ever used.
    """
    with pytest.raises(ScoreError, match="usable log scale"):
        _score("-5", range_min=-1e308, range_max=0.0, zero_point=5e-324)


@pytest.mark.parametrize(
    ("low", "high", "zero_point", "value"),
    [
        # Each unscales, by the platform's formula in floats, to the location in the comment.
        (3.0, 6.0, 6000000000000006.0, "5.979337069251501"),  # 1.2
        (0.0, 1.3, 6500000000000001.0, "1.048452576641855"),  # 1.5
        (12.5, 18.0, 2.7500000000000016e16, "17.058704579668582"),  # 2.0
        (12.5, 112.5, 5.000000000000001e17, "71.05618635076388"),  # 4.0
    ],
)
def test_a_value_inside_the_range_is_inbound_however_the_log_scale_rounds(
    low: float, high: float, zero_point: float, value: str
) -> None:
    """A zero point about 1e15 range-widths away: the two logarithms cancel to a few ulps.

    Unclamped, these locations index a bucket the CDF does not have (an ``IndexError``, not
    a ``ScoreError``) or the "above" bucket for a value that is inside the range.
    """
    assert low < float(value) < high
    score = _score(value, range_min=low, range_max=high, zero_point=zero_point)
    assert 1 <= score.bucket <= 4


def test_exactly_the_upper_bound_is_the_last_inbound_bucket_at_the_largest_count() -> None:
    """The platform has a branch for a location of exactly 1; the epsilon covers it here.

    ``int(N + 1 - 1e-10)`` is ``N`` while one ulp of ``N + 1`` is below the epsilon, which
    holds with room to spare at the largest count this module accepts.
    """
    inbound = 10_000
    cdf = [(index + 1) / (inbound + 2) for index in range(inbound + 1)]
    for resolution, bucket in (
        ("100", inbound),
        ("0", 1),
        ("50", inbound // 2),
        ("100.5", inbound + 1),
    ):
        score = _score(resolution, cdf, inbound_outcome_count=inbound)
        assert score.bucket == bucket


def test_no_refusal_reprints_a_value() -> None:
    """Canaries, because a substring check on an arbitrary draw cannot tell a leak (M1-346)."""
    attempts: list[tuple[object, object, dict[str, object]]] = [
        ([0.05, 0.25, CANARY, 0.2, 0.95], "30", {}),  # decreases at the canary
        ([0.05, 0.25, 0.5, 0.75, 1.0 + CANARY], "30", {}),
        ([0.05, 0.25, 0.5, 0.75], "30", {}),
        (CDF, CANARY_TEXT, {}),
        (CDF, "30", {"range_min": 100.0 + CANARY, "range_max": CANARY}),
        (CDF, "30", {"zero_point": 50.0 + CANARY}),
        (CDF, "30", {"inbound_outcome_count": 123456789}),
        ([0.0, CANARY, 0.5, 0.75, 1.0], BELOW_LOWER_BOUND, {}),
        (CDF, "30", {"open_lower_bound": CANARY_TEXT}),
    ]
    for cdf, resolution, overrides in attempts:
        with pytest.raises(ScoreError) as caught:
            _score(resolution, cdf, **overrides)
        message = str(caught.value)
        assert "123456789" not in message
        assert CANARY_TEXT not in message
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ or caught.value.__context__ is None
