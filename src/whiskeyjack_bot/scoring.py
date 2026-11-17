"""Local Brier and log scores for binary and multiple-choice forecasts (M4-802), and a
local log score for numeric and discrete forecasts (M4-809).

Pure: no ledger, no clock, no network, no SDK. :func:`lifecycle.record_local_scores` reads
a verified forecast record and its latest scorable resolution and calls this module;
``015_local_score_events.sql`` constrains the rows that result.

**These are local scores, not Metaculus scores.** Metaculus's own scoring
(``Metaculus/metaculus`` ``scoring/score_math.py``) has no Brier score at all, and its
log-based scores are the *baseline* (``100 * ln(2p) / ln 2`` for binary) and *peer*
(``100 * ln(p / geometric_mean)``) transforms, taken over the forecast's time-weighted
coverage. Nothing here reproduces either, and every metric name carries a ``local_`` prefix
so a row cannot be read as one (D30's prohibition, applied by analogy; D36).

The definitions, for a forecast resolved to outcome ``k``:

- ``local_brier_binary``: ``(p - o) ** 2``, where ``p`` is the forecast probability of
  *yes* and ``o`` is 1 for *yes* and 0 for *no*. The single-term convention, in [0, 1] --
  not the two-category sum, which is twice this.
- ``local_log_binary``: ``ln(p)`` for *yes*, ``ln(1.0 - p)`` for *no*. Natural log, in
  (-inf, 0]. ``1.0 - p`` is how the platform itself derives the *no* mass it stores.
- ``local_brier_multiclass``: ``sum_i (f_i - [i == k]) ** 2`` over the forecast's options,
  in [0, 2] for a distribution.
- ``local_log_multiclass``: ``ln(f_k)``.

The outcome is matched to an option **by label**, never by position: options are unordered
membership (M1-331), and the platform reorders them (a refetch reports a ``label_order``).

**Extreme probabilities are refused, never clamped.** The logarithm of every positive double
is finite (the smallest subnormal gives about -744.44), so the only unsafe input is a
probability of exactly zero on the realized outcome, and that is a :class:`ScoreError`. A
clamp would make the function total by changing the score it reports, which is the thing a
versioned implementation exists to prevent. The refusal is unreachable for a posted forecast
-- the configured probability bounds are at least 0.001 and the platform accepts nothing
outside [0.001, 0.999] -- so it guards a malformed stored value, not a real one.

**Every implementation is versioned.** :data:`IMPLEMENTATION_VERSIONS` holds the version
string each metric writes to ``score_events.implementation_version``. A change to what a
function returns for any input is a new version string, and the old function stays
registered in :data:`_IMPLEMENTATIONS` so a stored row can still be recomputed and checked.

**Numeric and discrete forecasts (M4-809)** are scored by :func:`score_continuous`: the
natural log of the probability the forecast's CDF puts on the bucket the question resolved
in, by the platform's own bucket rule (``utils/the_math/formulas.py`` and
``scoring/score_math.py``, read at ``7b47f09``). It is **not a stored metric** -- it is not in
:data:`LocalMetric` and writes no ``score_events`` row -- because it exists for forecasts
that were never posted (experiment arms), which Metaculus never scores.
:func:`platform_baseline_points` puts it on the platform's spot baseline scale so it can be
checked against the platform's number, and :func:`peer_points_per_nat` states what a
difference between two of this account's forecasts is worth in spot peer points.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, get_args

LocalMetric = Literal[
    "local_brier_binary",
    "local_log_binary",
    "local_brier_multiclass",
    "local_log_multiclass",
]

LOCAL_METRICS: Final[frozenset[str]] = frozenset(get_args(LocalMetric))

# The metrics each question type is scored on, in write order. The first is the row a
# `scored` lifecycle event links (003 lets one event link exactly one score row).
BINARY_METRICS: Final[tuple[LocalMetric, ...]] = ("local_brier_binary", "local_log_binary")
MULTICLASS_METRICS: Final[tuple[LocalMetric, ...]] = (
    "local_brier_multiclass",
    "local_log_multiclass",
)

IMPLEMENTATION_VERSIONS: Final[Mapping[LocalMetric, str]] = MappingProxyType(
    {
        "local_brier_binary": "local_brier_binary/1",
        "local_log_binary": "local_log_binary/1",
        "local_brier_multiclass": "local_brier_multiclass/1",
        "local_log_multiclass": "local_log_multiclass/1",
    }
)

# The same number as `forecast/multiple_choice.py`'s `_SUM_TOLERANCE` and
# `submission_live._CATEGORY_SUM_TOLERANCE`, for a third reason: it bounds what this module
# will *score*. Independent constants, the way those two already are -- this module may not
# import either (one is private to generation, the other reaches the submission stack).
# A vector outside it is refused, never renormalized: renormalizing would score a forecast
# nobody made.
_SUM_TOLERANCE: Final = 1e-6

_BINARY_OUTCOMES: Final[Mapping[str, float]] = MappingProxyType({"yes": 1.0, "no": 0.0})


class ScoreError(Exception):
    """A forecast or outcome cannot be scored.

    The message names the rule, never a value: a probability, an option label and an outcome
    are all content read back out of the ledger.
    """


@dataclass(frozen=True)
class LocalScore:
    """One computed score, as ``score_events`` stores it."""

    metric: LocalMetric
    value: float
    implementation_version: str


def _require_probability(value: object, field: str) -> float:
    # Exact type: a bool is an int is not a probability, and an int never reaches here from
    # the validated forecast schema, which stores floats.
    if type(value) is not float:
        raise ScoreError(f"{field} must be a float")
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ScoreError(f"{field} must be a finite probability in [0, 1]")
    return value


def _require_outcome(value: object) -> str:
    if type(value) is not str or not value:
        raise ScoreError("outcome must be a non-empty string")
    return value


def _binary_inputs(probability_yes: object, outcome: object) -> tuple[float, float]:
    p = _require_probability(probability_yes, "probability_yes")
    label = _require_outcome(outcome)
    if label not in _BINARY_OUTCOMES:
        raise ScoreError("a binary outcome must be yes or no")
    return p, _BINARY_OUTCOMES[label]


def _multiclass_inputs(options: object, outcome: object) -> tuple[tuple[float, ...], int]:
    """Validate a distribution and return its probabilities and the realized option's index.

    The index is into the caller's own ordering, found by label; nothing downstream reads a
    position as meaning anything else.
    """
    label = _require_outcome(outcome)
    if not isinstance(options, Sequence) or isinstance(options, (str, bytes)):
        raise ScoreError("options must be a sequence of (label, probability) pairs")
    seen: set[str] = set()
    probabilities: list[float] = []
    realized: int | None = None
    for index, entry in enumerate(options):
        if type(entry) is not tuple or len(entry) != 2:
            raise ScoreError("options must be a sequence of (label, probability) pairs")
        option, probability = entry
        if type(option) is not str or not option:
            raise ScoreError("an option label must be a non-empty string")
        if option in seen:
            raise ScoreError("an option label appears more than once")
        seen.add(option)
        probabilities.append(_require_probability(probability, "an option probability"))
        if option == label:
            realized = index
    if not probabilities:
        raise ScoreError("options must not be empty")
    # fsum is correctly rounded, so the verdict does not depend on option order.
    if abs(math.fsum(probabilities) - 1.0) > _SUM_TOLERANCE:
        raise ScoreError(f"option probabilities must sum to 1 within {_SUM_TOLERANCE}")
    if realized is None:
        raise ScoreError("the outcome is not an option this forecast priced")
    return tuple(probabilities), realized


def _log(probability: float) -> float:
    if probability == 0.0:
        raise ScoreError("the log score is undefined: the realized outcome had probability 0")
    return math.log(probability)


def binary_brier_v1(probability_yes: object, outcome: object) -> float:
    """``(p - o) ** 2``: the single-term binary Brier score, in [0, 1]."""
    p, o = _binary_inputs(probability_yes, outcome)
    return (p - o) ** 2


def binary_log_v1(probability_yes: object, outcome: object) -> float:
    """``ln(p)`` for yes, ``ln(1.0 - p)`` for no. Refuses a zero on the realized outcome."""
    p, o = _binary_inputs(probability_yes, outcome)
    return _log(p if o == 1.0 else 1.0 - p)


def multiclass_brier_v1(options: object, outcome: object) -> float:
    """``sum_i (f_i - [i == k]) ** 2`` over the forecast's options, matched by label."""
    probabilities, realized = _multiclass_inputs(options, outcome)
    # fsum again: the score of a forecast must not depend on the order its options were
    # stored in, and a naive left-to-right sum can differ by an ulp across permutations.
    return math.fsum(
        (f - (1.0 if index == realized else 0.0)) ** 2 for index, f in enumerate(probabilities)
    )


def multiclass_log_v1(options: object, outcome: object) -> float:
    """``ln(f_k)`` for the realized option ``k``, matched by label."""
    probabilities, realized = _multiclass_inputs(options, outcome)
    return _log(probabilities[realized])


# Version string -> (metric, function). Append-only: a retired version keeps its entry so a
# row written under it can still be recomputed. A test pins that every current version is
# registered under its own metric.
_BinaryScorer = Callable[[object, object], float]
_IMPLEMENTATIONS: Final[Mapping[str, tuple[LocalMetric, _BinaryScorer]]] = MappingProxyType(
    {
        "local_brier_binary/1": ("local_brier_binary", binary_brier_v1),
        "local_log_binary/1": ("local_log_binary", binary_log_v1),
        "local_brier_multiclass/1": ("local_brier_multiclass", multiclass_brier_v1),
        "local_log_multiclass/1": ("local_log_multiclass", multiclass_log_v1),
    }
)


def _score(
    metrics: Sequence[LocalMetric], prediction: object, outcome: object
) -> tuple[LocalScore, ...]:
    scores: list[LocalScore] = []
    for metric in metrics:
        version = IMPLEMENTATION_VERSIONS[metric]
        _, function = _IMPLEMENTATIONS[version]
        scores.append(
            LocalScore(
                metric=metric, value=function(prediction, outcome), implementation_version=version
            )
        )
    return tuple(scores)


def score_binary(probability_yes: object, outcome: object) -> tuple[LocalScore, ...]:
    """Every current binary metric, in :data:`BINARY_METRICS` order."""
    return _score(BINARY_METRICS, probability_yes, outcome)


def score_multiple_choice(options: object, outcome: object) -> tuple[LocalScore, ...]:
    """Every current multiclass metric, in :data:`MULTICLASS_METRICS` order.

    ``options`` is a sequence of ``(label, probability)`` pairs.
    """
    return _score(MULTICLASS_METRICS, options, outcome)


def recompute(
    implementation_version: object, metric: object, prediction: object, outcome: object
) -> float:
    """Recompute one stored score with the implementation that wrote it.

    Refuses a version this build does not register, and a version registered under a
    different metric: a row whose metric and version disagree was not written by this module.
    """
    if type(implementation_version) is not str or implementation_version not in _IMPLEMENTATIONS:
        raise ScoreError("implementation_version is not a registered local score version")
    registered_metric, function = _IMPLEMENTATIONS[implementation_version]
    if metric != registered_metric:
        raise ScoreError("the metric does not match its implementation_version")
    return function(prediction, outcome)


# ── numeric and discrete forecasts (M4-809) ──────────────────────────────────
#
# Not a stored metric. `score_events.metric` is a closed vocabulary (017/018), and nothing
# here is in `LocalMetric`, `IMPLEMENTATION_VERSIONS` or `_IMPLEMENTATIONS`: this score is
# computed for forecasts that were never posted (experiment arms, M5-806 and M1-614), which
# have no `score_events` row to write. Storing it for posted records is a vocabulary
# migration and a row of its own.

CONTINUOUS_LOG_VERSION: Final = "local_log_continuous/1"

BELOW_LOWER_BOUND: Final = "below_lower_bound"
ABOVE_UPPER_BOUND: Final = "above_upper_bound"

# `Question.get_inbound_outcome_count`: a numeric question with no count of its own has 200.
_DEFAULT_INBOUND_OUTCOME_COUNT: Final = 200
# The count is untrusted payload and sizes the CDF this module walks, so it is bounded. The
# largest a live question has carried is 200. The bucket rule's epsilon stops registering
# once half an ulp of the count reaches it, near one million; this bound is a hundredfold
# inside that, so the rule needs no separate branch for a resolution exactly on the upper
# bound.
_MAX_INBOUND_OUTCOME_COUNT: Final = 10_000
# `unscaled_location_to_bucket_index`'s own constant: a resolution exactly on a bucket edge
# belongs to the lower bucket.
_BUCKET_EPSILON: Final = 1e-10
# The platform's baseline mass for an outcome outside an open bound.
_OUT_OF_RANGE_BASELINE: Final = 0.05
# An ASCII decimal, optionally in scientific notation. `float()` alone also accepts "nan",
# "inf", "1_0", surrounding whitespace and non-ASCII digits, none of which is a resolution.
_DECIMAL: Final = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_MAX_RESOLUTION_LENGTH: Final = 64

# What one nat of local log score is worth in spot peer points, for two forecasts on one
# question: the platform's `100 * (n / (n - 1)) * ln(p / geometric_mean)` is exactly
# `100 * (ln p - mean of the others' ln p)`, so the others cancel in a difference; numeric,
# date and discrete scores are then halved.
_PEER_POINTS_PER_NAT: Final[Mapping[str, float]] = MappingProxyType(
    {"binary": 100.0, "multiple_choice": 100.0, "numeric": 50.0, "discrete": 50.0}
)


@dataclass(frozen=True)
class ContinuousScore:
    """The local log score of a numeric or discrete forecast, and where it was read.

    ``log_mass`` is the natural log of the probability the CDF puts on the resolved bucket.
    ``bucket`` indexes the platform's PMF: 0 is below the lower bound, ``bucket_count - 1``
    is above the upper bound, and the buckets between are inbound.
    """

    log_mass: float
    bucket: int
    bucket_count: int
    open_bounds: int
    implementation_version: str

    @property
    def out_of_range(self) -> bool:
        return self.bucket in (0, self.bucket_count - 1)


def _require_number(value: object, field: str) -> float:
    # Exact types: the platform stores a closed bound's CDF end and a whole-number range as
    # JSON integers, so an int is a number here; a bool is not.
    if type(value) is float:
        number = value
    elif type(value) is int:
        try:
            number = float(value)
        except OverflowError:
            raise ScoreError(f"{field} must be a finite number") from None
    else:
        raise ScoreError(f"{field} must be a finite number")
    if not math.isfinite(number):
        raise ScoreError(f"{field} must be a finite number")
    return number


def _require_flag(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ScoreError(f"{field} must be a boolean")
    return value


def _inbound_outcome_count(value: object) -> int:
    if value is None:
        return _DEFAULT_INBOUND_OUTCOME_COUNT
    if type(value) is not int or not 1 <= value <= _MAX_INBOUND_OUTCOME_COUNT:
        raise ScoreError(
            "inbound_outcome_count must be a positive integer within the scorer's limit, or absent"
        )
    return value


def _require_cdf(cdf: object, inbound: int) -> tuple[float, ...]:
    if not isinstance(cdf, (list, tuple)) or type(cdf) not in (list, tuple):
        raise ScoreError("cdf must be a list of numbers")
    if len(cdf) != inbound + 1:
        raise ScoreError("cdf must have one more point than the question has inbound outcomes")
    points = tuple(_require_number(point, "a cdf point") for point in cdf)
    previous = 0.0
    for point in points:
        if not 0.0 <= point <= 1.0:
            raise ScoreError("a cdf point must be a probability in [0, 1]")
        if point < previous:
            raise ScoreError("cdf must not decrease")
        previous = point
    return points


def _log_scale_ratio(low: float, high: float, zero_point: float) -> float:
    """How many times farther from the zero point the far bound is than the near one.

    Above 1 on a usable scale. With the zero point below the range this is the platform's
    ``deriv_ratio``; with it above the range, the reciprocal of that.
    """
    if zero_point < low:
        return (high - zero_point) / (low - zero_point)
    return (zero_point - low) / (zero_point - high)


def _log_location(offset: float, width: float, ratio: float) -> float:
    """Where a point ``offset`` from the bound nearest the zero point sits: 0 there, 1 across.

    ``ln(1 + (offset / width) * (ratio - 1)) / ln(ratio)``: the platform's
    ``(ln(offset * (ratio - 1) + width) - ln(width)) / ln(ratio)`` with the width divided out
    before the logarithm is taken instead of subtracted after. It is the same number, and
    the platform's form does not survive floats in two places where this one does:

    - its product overflows when the ratio and the offset are both very large (range
      ``[1, 1e200]`` with the zero point at 0 and a value of 1e150 is a product of 1e350);
      here ``offset / width`` is at most 1 and the ratio is finite;
    - its two logarithms cancel when the zero point is far from the range, leaving an error
      that grows with the distance (1.5e-5 of the range at 1e9 range-widths, 1.5% at 1e12,
      nothing meaningful by 1e14); here ``log1p`` takes the small quantity itself.

    Both terms are ``log1p`` of a non-negative number and the first is no larger than the
    second, so the result is in ``[0, 1]`` but for an ulp of rounding.
    """
    span = ratio - 1.0
    return math.log1p(offset / width * span) / math.log1p(span)


def _unscaled_location(value: float, low: float, high: float, zero_point: float | None) -> float:
    """Where a value inside ``[low, high]`` sits in the range: 0 at ``low``, 1 at ``high``.

    The platform's ``scaled_location_to_unscaled_location``, as written on a linear scale.

    A log scale is measured from the bound nearest its zero point, in the form
    :func:`_log_location` gives. The platform always measures from ``low``. With the zero
    point above the range that is the far bound, and its sum
    ``(value - low) * (ratio - 1) + width`` then cancels: with the zero point 1e-200 above
    ``[-1, 0]``, every value within 1e-16 of the upper bound computes to the upper bound,
    whatever its location. Measured from ``high`` the same quantity is one minus
    :func:`_log_location`, and nothing cancels.

    Not clamped. The caller has already placed a value outside the range, so the result is
    in ``[0, 1]`` but for an ulp, which :func:`_bucket_index` absorbs at both ends.
    """
    width = high - low
    if zero_point is None:
        return (value - low) / width
    ratio = _log_scale_ratio(low, high, zero_point)
    if zero_point < low:
        return _log_location(value - low, width, ratio)
    return 1.0 - _log_location(high - value, width, ratio)


def _bucket_index(location: float, inbound: int) -> int:
    """The platform's ``unscaled_location_to_bucket_index`` for a location in ``[0, 1]``.

    ``max(int(u * N + 1 - 1e-10), 1)``. The epsilon is the platform's: a resolution exactly on
    a bucket edge belongs to the lower bucket, which also puts exactly the upper bound
    (``u == 1``) in the last inbound bucket for every count this module accepts. The clamp
    to 1 keeps exactly the lower bound inbound, never "below". The same two rules place a
    location an ulp outside ``[0, 1]``: an ulp past 1 times the largest count is 2e-12,
    inside the epsilon, and anything below 0 is bucket 1.
    """
    return max(int(location * inbound + 1.0 - _BUCKET_EPSILON), 1)


def _resolved_bucket(
    resolution: object, low: float, high: float, zero_point: float | None, inbound: int
) -> int:
    if type(resolution) is not str or not resolution:
        raise ScoreError("resolution must be a non-empty string")
    # The markers go straight to their buckets. The platform maps them to `range_min - 1`
    # and `range_max + 1` and unscales those, which on a log scale can be a NaN.
    if resolution == BELOW_LOWER_BOUND:
        return 0
    if resolution == ABOVE_UPPER_BOUND:
        return inbound + 1
    if len(resolution) > _MAX_RESOLUTION_LENGTH or _DECIMAL.fullmatch(resolution) is None:
        raise ScoreError("resolution must be a decimal number or a bound marker")
    value = float(resolution)
    if not math.isfinite(value):
        raise ScoreError("resolution must be a finite number")
    # Outside the range is decided by comparison, on every scale. It is how the platform
    # itself writes a resolution (`scaled_location_to_string_location` turns a value below
    # `range_min` into the marker), and unscaling first would let a log scale round a value
    # just outside a bound onto it.
    if value < low:
        return 0
    if value > high:
        return inbound + 1
    return _bucket_index(_unscaled_location(value, low, high, zero_point), inbound)


def score_continuous(
    cdf: object,
    resolution: object,
    *,
    range_min: object,
    range_max: object,
    zero_point: object,
    open_lower_bound: object,
    open_upper_bound: object,
    inbound_outcome_count: object = None,
) -> ContinuousScore:
    """Score a numeric or discrete forecast's CDF against its resolution.

    ``cdf`` is the forecast as the platform holds it: ``inbound_outcome_count + 1``
    non-decreasing probabilities, ``cdf[0]`` the mass below the lower bound and
    ``1 - cdf[-1]`` the mass above the upper. ``resolution`` is the platform's resolution
    string: a decimal number, or one of the two bound markers. The remaining arguments are
    the question's ``scaling`` block and its bound flags, all untrusted payload.

    Refuses, never clamps: a resolved bucket with no mass has no log score.
    """
    low = _require_number(range_min, "range_min")
    high = _require_number(range_max, "range_max")
    if not high > low or not math.isfinite(high - low):
        raise ScoreError("range_max must exceed range_min by a finite amount")
    zero: float | None = None
    if zero_point is not None:
        zero = _require_number(zero_point, "zero_point")
        if low <= zero <= high:
            raise ScoreError("zero_point must lie outside the question's range")
        # The ratio the scale is evaluated with. Infinite when the bounds' distances from the
        # zero point differ by more than a float holds; 1 when the zero point is so far away
        # (about 1e16 range-widths) that they do not differ at all, and the logarithm that
        # divides is zero.
        ratio = _log_scale_ratio(low, high, zero)
        if not math.isfinite(ratio) or not ratio > 1.0:
            raise ScoreError("zero_point does not define a usable log scale")
    lower_open = _require_flag(open_lower_bound, "open_lower_bound")
    upper_open = _require_flag(open_upper_bound, "open_upper_bound")
    inbound = _inbound_outcome_count(inbound_outcome_count)
    points = _require_cdf(cdf, inbound)
    # A closed bound pins its end of the CDF: no outcome exists outside it, so mass there is
    # not a forecast of this question. Exact, as `forecast/cdf.py` builds it and as every
    # closed bound the platform has returned holds it. An open bound is not checked the other
    # way: no mass outside one is still a forecast of this question, and an outcome there is
    # refused below as probability 0.
    if not lower_open and points[0] != 0.0:
        raise ScoreError("cdf must start at 0 when the lower bound is closed")
    if not upper_open and points[-1] != 1.0:
        raise ScoreError("cdf must end at 1 when the upper bound is closed")
    bucket = _resolved_bucket(resolution, low, high, zero, inbound)
    if bucket == 0:
        mass = points[0]
    elif bucket == inbound + 1:
        mass = 1.0 - points[-1]
    else:
        mass = points[bucket] - points[bucket - 1]
    if not mass > 0.0:
        raise ScoreError("the log score is undefined: the resolved bucket had probability 0")
    return ContinuousScore(
        log_mass=math.log(mass),
        bucket=bucket,
        bucket_count=inbound + 2,
        open_bounds=int(lower_open) + int(upper_open),
        implementation_version=CONTINUOUS_LOG_VERSION,
    )


def continuous_log_v1(
    cdf: object,
    resolution: object,
    *,
    range_min: object,
    range_max: object,
    zero_point: object,
    open_lower_bound: object,
    open_upper_bound: object,
    inbound_outcome_count: object = None,
) -> float:
    """``ln`` of the mass on the resolved bucket: :func:`score_continuous`'s ``log_mass``."""
    return score_continuous(
        cdf,
        resolution,
        range_min=range_min,
        range_max=range_max,
        zero_point=zero_point,
        open_lower_bound=open_lower_bound,
        open_upper_bound=open_upper_bound,
        inbound_outcome_count=inbound_outcome_count,
    ).log_mass


def platform_baseline_points(score: ContinuousScore) -> float:
    """``score`` on Metaculus's spot baseline scale, for checking against the platform.

    ``100 * ln(mass / baseline) / 2``, where the baseline mass is 0.05 for an outcome outside
    a bound and ``(1 - 0.05 * open_bounds)`` spread evenly over the inbound buckets otherwise.
    A comparison aid: nothing stores this number.
    """
    if score.out_of_range:
        baseline = _OUT_OF_RANGE_BASELINE
    else:
        baseline = (1.0 - _OUT_OF_RANGE_BASELINE * score.open_bounds) / (score.bucket_count - 2)
    return 100.0 * (score.log_mass - math.log(baseline)) / 2.0


def peer_points_per_nat(question_type: object) -> float:
    """Spot peer points per nat of local log score, for two forecasts on one question.

    100 for binary and multiple choice, 50 for numeric and discrete. Multiply a difference
    in local log score by this to get the difference in spot peer score.
    """
    if type(question_type) is not str or question_type not in _PEER_POINTS_PER_NAT:
        raise ScoreError("question_type has no peer scale this module knows")
    return _PEER_POINTS_PER_NAT[question_type]
