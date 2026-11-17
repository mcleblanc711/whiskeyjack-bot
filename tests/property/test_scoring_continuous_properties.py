"""Property tests for the local log score of numeric and discrete forecasts (M4-809).

The CLAUDE.md pre-review fuzz pass over ``scoring.score_continuous``:

1. Nothing but ``ScoreError`` escapes, over structured and arbitrary inputs, and every
   refusal's text is one of a fixed set of sentences written out here -- so no message can
   carry a value, whatever the value was.
2. On every valid input the score is the natural log of the mass an independently derived
   PMF puts on the returned bucket, or a refusal exactly when that mass is zero.
3. The resolved value lies inside the bucket it was assigned, checked by mapping the bucket's
   edges back through the platform's *inverse* scaling, which this module does not contain.
   On every log scale the bucket also holds the value's exact location, computed in
   60-digit decimal arithmetic.
4. Buckets are ordered as resolutions are: a larger value never lands in an earlier bucket.
5. One representative resolution per bucket recovers the whole distribution: the masses sum
   to one.
6. The score rises with the mass on the resolved bucket and with nothing else.
7. The persisted form replays: JSON out and back gives the same score to the bit, and an
   integer 0 or 1 where the platform stores one scores as the float does.
8. The platform's baseline scale, restated from its source, agrees with the helper.
9. A closed bound refuses exactly when the CDF puts mass outside it, whatever else is true.

Reach is measured with ``hypothesis.event``: scale kind and size, bucket kind, closed bounds,
whether the platform's own product overflowed, and refusals.

Review round 1 found two defects this file could not reach. The bound flags were drawn
independently of the CDF's endpoints, so no property could say what a closed bound means;
and every scale was within 1e6 of zero, so no product could overflow. ``valid_inputs`` now
builds the CDF from the flags and property 9 draws them apart on purpose. Scales come in
five sizes: the original one, a zero point up to 1e15 range-widths away, the size of a real
question, anywhere a float reaches, and the corner where the platform's own product
overflows. And the check that found nothing was in value space with a slack of a millionth
of the range, which on a wide log scale is wider than most buckets; the exact-location
property is in location space, to a billionth of a bucket.
"""

from __future__ import annotations

import json
import math
from decimal import Context, Decimal
from fractions import Fraction

import pytest
from hypothesis import assume, event, given
from hypothesis import strategies as st

from whiskeyjack_bot.scoring import (
    ABOVE_UPPER_BOUND,
    BELOW_LOWER_BOUND,
    ContinuousScore,
    ScoreError,
    peer_points_per_nat,
    platform_baseline_points,
    score_continuous,
)

# Every sentence the continuous scorer may refuse with, restated: a property must not import
# them. A message outside this set fails property 1, which is how a value in a message would
# be caught without knowing what the value was.
REFUSALS = frozenset(
    {
        "range_min must be a finite number",
        "range_max must be a finite number",
        "zero_point must be a finite number",
        "a cdf point must be a finite number",
        "range_max must exceed range_min by a finite amount",
        "zero_point must lie outside the question's range",
        "zero_point does not define a usable log scale",
        "open_lower_bound must be a boolean",
        "open_upper_bound must be a boolean",
        "inbound_outcome_count must be a positive integer within the scorer's limit, or absent",
        "cdf must be a list of numbers",
        "cdf must have one more point than the question has inbound outcomes",
        "a cdf point must be a probability in [0, 1]",
        "cdf must not decrease",
        "cdf must start at 0 when the lower bound is closed",
        "cdf must end at 1 when the upper bound is closed",
        "resolution must be a non-empty string",
        "resolution must be a decimal number or a bound marker",
        "resolution must be a finite number",
        "the log score is undefined: the resolved bucket had probability 0",
        "question_type has no peer scale this module knows",
    }
)

LOWER_ENDPOINT = "cdf must start at 0 when the lower bound is closed"
UPPER_ENDPOINT = "cdf must end at 1 when the upper bound is closed"
ZERO_MASS = "the log score is undefined: the resolved bucket had probability 0"

# ── strategies ───────────────────────────────────────────────────────────────

INBOUND = st.integers(min_value=1, max_value=24)

# A location in [0, 1], for mapping back to a value. Plain `st.floats(0, 1)` favours 0 and
# small values, and on a log scale that is the first bucket two times in three (measured).
# The slice is drawn as an integer, so every part of the range is reached.
LOCATIONS = st.builds(
    lambda piece, within: (piece + within) / 32,
    st.integers(min_value=0, max_value=31),
    st.floats(min_value=0.0, max_value=1.0),
)


@st.composite
def cdfs(
    draw: st.DrawFn, inbound: int, *, lower_open: bool = True, upper_open: bool = True
) -> list[float]:
    """A valid CDF, built from the masses of its ``inbound + 2`` buckets.

    A closed bound has no mass outside it, so its end of the CDF is exactly 0 or exactly 1.
    About a quarter of the other masses are zero, so empty buckets, flat stretches and open
    bounds with nothing outside them are all drawn, without being the common case.
    """
    mass = st.one_of(
        st.floats(min_value=0.01, max_value=1.0),
        st.floats(min_value=0.01, max_value=1.0),
        st.floats(min_value=1e-9, max_value=1.0),
        st.just(0.0),
    )
    masses = draw(st.lists(mass, min_size=inbound + 2, max_size=inbound + 2))
    if not lower_open:
        masses[0] = 0.0
    if not upper_open:
        masses[-1] = 0.0
    total = math.fsum(masses)
    assume(total > 0.0)
    points: list[float] = []
    running = 0.0
    for value in masses[:-1]:
        running = min(running + value / total, 1.0)
        points.append(running)
    if masses[-1] == 0.0:
        points[-1] = 1.0
    return points


def _moderate_scale(draw: st.DrawFn, kind: str) -> tuple[float, float, float | None]:
    """Within 1e6 of zero, with the zero point up to 1e9 range-widths away."""
    low = draw(st.floats(min_value=-1e6, max_value=1e6))
    high = low + draw(st.floats(min_value=1e-3, max_value=1e6))
    if kind == "linear":
        return low, high, None
    gap = draw(st.floats(min_value=1e-3, max_value=1e6))
    return low, high, low - gap if kind == "zero_below" else high + gap


def _far_scale(draw: st.DrawFn, kind: str) -> tuple[float, float, float | None]:
    """Within 1e6 of zero, with the zero point up to 1e15 range-widths away.

    A log scale this far from its zero point is the linear scale to one part in the distance.
    The platform's expression loses a digit of location for every decade of that distance.
    """
    low = draw(st.floats(min_value=-1e6, max_value=1e6))
    width = draw(st.floats(min_value=1e-3, max_value=1e6))
    if kind == "linear":
        return low, low + width, None
    gap = width * 10.0 ** draw(st.floats(min_value=0.0, max_value=15.0))
    return low, low + width, low - gap if kind == "zero_below" else low + width + gap


# Decades of range width, and decades of log ratio, for a scale built around its zero point.
# "ordinary" is the size of a real question. "extreme" is anywhere a float reaches.
# "overflowing" is where the platform's product `offset * (ratio - 1)` exceeds a float for
# most values in the range, which is review round 1's B2.
_SIZES: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {
    "ordinary": ((-3.0, 6.0), (0.0, 9.0)),
    "extreme": ((-280.0, 280.0), (0.0, 300.0)),
    "overflowing": ((160.0, 280.0), (150.0, 300.0)),
}
SIZES = ("moderate", "far", *_SIZES)


def _sized_scale(draw: st.DrawFn, kind: str, size: str) -> tuple[float, float, float | None]:
    """A scale of a given size whose zero point is within one range-width of the nearest bound.

    That is a log ratio of 2 or more, up to 1e300.
    """
    (low_decade, high_decade), (low_ratio, high_ratio) = _SIZES[size]
    decade = draw(st.floats(min_value=low_decade, max_value=high_decade))
    width = 10.0**decade
    sign = draw(st.sampled_from([0.0, 1.0, -1.0]))
    offset = 10.0 ** draw(st.floats(min_value=-9.0, max_value=9.0))
    if kind == "linear":
        low = sign * width * offset
        return low, low + width, None
    # The nearest bound's distance from the zero point: at most the width, at least 1e-300.
    ratio_decade = draw(st.floats(min_value=low_ratio, max_value=min(high_ratio, decade + 300.0)))
    gap = width * 10.0**-ratio_decade
    zero = sign * gap * offset
    if kind == "zero_below":
        return zero + gap, zero + gap + width, zero
    return zero - gap - width, zero - gap, zero


KINDS = ("linear", "zero_below", "zero_above")


@st.composite
def scalings(
    draw: st.DrawFn, *, kinds: tuple[str, ...] = KINDS, sizes: tuple[str, ...] = SIZES
) -> dict[str, object]:
    kind = draw(st.sampled_from(kinds))
    size = draw(st.sampled_from(sizes))
    if size == "moderate":
        low, high, zero = _moderate_scale(draw, kind)
    elif size == "far":
        low, high, zero = _far_scale(draw, kind)
    else:
        low, high, zero = _sized_scale(draw, kind, size)
    assume(math.isfinite(low) and math.isfinite(high) and high > low)
    assume(math.isfinite(high - low))
    if zero is not None:
        assume(not low <= zero <= high)
        # The scorer's own usability rule, restated: the far bound's distance from the zero
        # point over the near bound's is finite and above 1.
        near, far = (low - zero, high - zero) if zero < low else (zero - high, zero - low)
        assume(math.isfinite(far / near) and far / near > 1.0)
    event(f"scale: {kind}, {size}")
    return {
        "range_min": low,
        "range_max": high,
        "zero_point": zero,
        "open_lower_bound": draw(st.booleans()),
        "open_upper_bound": draw(st.booleans()),
    }


@st.composite
def valid_inputs(
    draw: st.DrawFn, *, markers: bool = True, min_inbound: int = 1
) -> tuple[list[float], str, dict[str, object]]:
    """A CDF, a resolution and a scaling the scorer accepts unless the resolved bucket is empty.

    The CDF is built from the bound flags: a closed bound gets no mass outside it.
    """
    inbound = draw(st.integers(min_value=min_inbound, max_value=24))
    scaling = {**draw(scalings()), "inbound_outcome_count": inbound}
    low, high = scaling["range_min"], scaling["range_max"]
    assert isinstance(low, float) and isinstance(high, float)
    width = high - low
    outside = (max(low - width, -1.7e308), min(high + width, 1.7e308))
    numbers = [
        st.floats(min_value=outside[0], max_value=outside[1]).map(repr),
        # Inside the range, where every inbound bucket is.
        st.floats(min_value=low, max_value=high).map(repr),
        # Exactly on a bucket edge, where the platform's rule is decided.
        st.integers(min_value=0, max_value=inbound).map(
            lambda edge: repr(low + width * edge / inbound)
        ),
        # A hair outside a bound, where a log scale's rounding would pull a value onto it.
        st.sampled_from(
            [repr(math.nextafter(low, -math.inf)), repr(math.nextafter(high, math.inf))]
        ),
        # A location drawn evenly and mapped back to a value. On a log scale the floats
        # between the bounds crowd one end, and this is what reaches the buckets at the other.
        LOCATIONS.map(lambda location: repr(min(max(_value_at(location, scaling), low), high))),
    ]
    choices = [st.one_of(numbers), st.sampled_from([BELOW_LOWER_BOUND, ABOVE_UPPER_BOUND])]
    resolution = draw(st.one_of(choices if markers else choices[:1]))
    cdf = draw(
        cdfs(
            inbound,
            lower_open=scaling["open_lower_bound"] is True,
            upper_open=scaling["open_upper_bound"] is True,
        )
    )
    return cdf, resolution, scaling


def _open(scaling: dict[str, object]) -> dict[str, object]:
    """The same scaling with both bounds open, for a CDF that has mass outside both."""
    return {**scaling, "open_lower_bound": True, "open_upper_bound": True}


JUNK = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(),
    st.text(max_size=12),
    st.binary(max_size=6),
    st.lists(st.one_of(st.floats(), st.integers(), st.none(), st.text(max_size=3)), max_size=6),
    st.tuples(st.floats(), st.floats()),
    st.dictionaries(st.text(max_size=2), st.floats(), max_size=2),
)


# ── independent oracles ──────────────────────────────────────────────────────


def _pmf(cdf: list[float]) -> list[float]:
    """The platform's ``get_pmf``, restated from ``questions/models.py``."""
    pmf = [cdf[0]]
    for index in range(1, len(cdf)):
        pmf.append(cdf[index] - cdf[index - 1])
    pmf.append(1 - cdf[-1])
    return pmf


# Sixty digits: far more than a float's sixteen, so the oracle's own rounding is not in play.
_EXACT = Context(prec=60)


def _decimal(ratio: Fraction) -> Decimal:
    return _EXACT.divide(Decimal(ratio.numerator), Decimal(ratio.denominator))


def _exact_scale(scaling: dict[str, object]) -> tuple[Fraction, Fraction, Fraction]:
    """``range_min``, ``zero_point`` and the platform's ``deriv_ratio``, as exact rationals.

    A float is a rational, and a difference or quotient of rationals is exact: on a scale
    whose bounds are 1e300 apart or 1e-200 from the zero point nothing overflows or cancels.
    """
    low, high, zero = scaling["range_min"], scaling["range_max"], scaling["zero_point"]
    assert isinstance(low, float) and isinstance(high, float) and isinstance(zero, float)
    exact_low, exact_zero = Fraction(low), Fraction(zero)
    return exact_low, exact_zero, (Fraction(high) - exact_zero) / (exact_low - exact_zero)


def _exact_location(value: float, scaling: dict[str, object]) -> Decimal:
    """The platform's ``scaled_location_to_unscaled_location`` on a log scale, exactly.

    Its ``(ln((x - min) * (ratio - 1) + (max - min)) - ln(max - min)) / ln(ratio)`` is
    ``ln((x - zero) / (min - zero)) / ln(ratio)``, since ``ratio - 1`` is
    ``(max - min) / (min - zero)``. The distances are exact rationals; only the two logarithms
    are rounded, to sixty digits.
    """
    low, zero, ratio = _exact_scale(scaling)
    distance = _decimal((Fraction(value) - zero) / (low - zero))
    return _EXACT.divide(distance.ln(_EXACT), _decimal(ratio).ln(_EXACT))


def _value_at(location: float, scaling: dict[str, object]) -> float:
    """The platform's ``unscaled_location_to_scaled_location``: the inverse of what is tested.

    On a log scale its ``min + (max - min) * (ratio ** u - 1) / (ratio - 1)`` is
    ``zero + (min - zero) * ratio ** u``, evaluated in sixty-digit decimals so that the
    oracle places a bucket edge correctly on scales where floats would not.
    """
    low, high, zero = scaling["range_min"], scaling["range_max"], scaling["zero_point"]
    assert isinstance(low, float) and isinstance(high, float)
    if zero is None:
        return low + (high - low) * location
    exact_low, exact_zero, ratio = _exact_scale(scaling)
    grown = _EXACT.power(_decimal(ratio), Decimal(location))
    distance = _EXACT.multiply(_decimal(exact_low - exact_zero), grown)
    return float(_EXACT.add(_decimal(exact_zero), distance))


def _try(cdf: object, resolution: object, scaling: dict[str, object]) -> ContinuousScore | None:
    try:
        return score_continuous(cdf, resolution, **scaling)
    except ScoreError as refusal:
        assert str(refusal) in REFUSALS
        return None


# ── 1. only ScoreError, and only fixed sentences ─────────────────────────────


@given(
    cdf=JUNK,
    resolution=JUNK,
    range_min=JUNK,
    range_max=JUNK,
    zero_point=JUNK,
    open_lower_bound=JUNK,
    open_upper_bound=JUNK,
    inbound_outcome_count=JUNK,
)
def test_nothing_but_a_fixed_score_error_escapes_arbitrary_input(
    cdf: object,
    resolution: object,
    range_min: object,
    range_max: object,
    zero_point: object,
    open_lower_bound: object,
    open_upper_bound: object,
    inbound_outcome_count: object,
) -> None:
    try:
        score_continuous(
            cdf,
            resolution,
            range_min=range_min,
            range_max=range_max,
            zero_point=zero_point,
            open_lower_bound=open_lower_bound,
            open_upper_bound=open_upper_bound,
            inbound_outcome_count=inbound_outcome_count,
        )
    except ScoreError as refusal:
        assert str(refusal) in REFUSALS
        assert refusal.__cause__ is None
        event("refused")
    else:
        event("scored")


@given(valid=valid_inputs(), field=st.sampled_from(["cdf", "resolution", "scaling"]), junk=JUNK)
def test_one_junk_argument_among_valid_ones_refuses_with_a_fixed_sentence(
    valid: tuple[list[float], str, dict[str, object]], field: str, junk: object
) -> None:
    """Junk in one place at a time, so each guard is reached behind valid neighbours."""
    cdf, resolution, scaling = valid
    if field == "cdf":
        outcome = _try(junk, resolution, scaling)
    elif field == "resolution":
        outcome = _try(cdf, junk, scaling)
    else:
        key = sorted(scaling)[hash(repr(junk)) % len(scaling)]
        outcome = _try(cdf, resolution, {**scaling, key: junk})
    event(f"{field}: {'scored' if outcome else 'refused'}")


@given(text=st.text(max_size=20))
def test_peer_points_per_nat_knows_four_types_and_refuses_the_rest(text: str) -> None:
    known = {"binary": 100.0, "multiple_choice": 100.0, "numeric": 50.0, "discrete": 50.0}
    if text in known:
        assert peer_points_per_nat(text) == known[text]
    else:
        with pytest.raises(ScoreError) as refusal:
            peer_points_per_nat(text)
        assert str(refusal.value) in REFUSALS


# ── 2. the score is the log of the bucket's mass ─────────────────────────────


@given(valid=valid_inputs())
def test_the_score_is_the_log_of_the_mass_on_the_returned_bucket(
    valid: tuple[list[float], str, dict[str, object]],
) -> None:
    cdf, resolution, scaling = valid
    inbound = scaling["inbound_outcome_count"]
    assert isinstance(inbound, int)
    pmf = _pmf(cdf)
    try:
        score = score_continuous(cdf, resolution, **scaling)
    except ScoreError as refusal:
        # The only refusal a valid input can meet: nothing on the resolved bucket.
        assert str(refusal) == ZERO_MASS
        event("refused: zero mass")
        return
    assert score.bucket_count == inbound + 2 == len(pmf)
    assert 0 <= score.bucket <= inbound + 1
    assert pmf[score.bucket] > 0.0
    assert score.log_mass == math.log(pmf[score.bucket])
    assert math.isfinite(score.log_mass) and score.log_mass <= 0.0
    assert score.out_of_range == (score.bucket in (0, inbound + 1))
    flags = (scaling["open_lower_bound"], scaling["open_upper_bound"])
    assert score.open_bounds == sum(1 for flag in flags if flag is True)
    kind = "below" if score.bucket == 0 else "above" if score.bucket == inbound + 1 else "inbound"
    event(f"bucket: {kind}")
    event(f"closed bounds: {2 - score.open_bounds}")


@given(valid=valid_inputs())
def test_a_bound_marker_is_the_bucket_outside_that_bound(
    valid: tuple[list[float], str, dict[str, object]],
) -> None:
    cdf, _, scaling = valid
    inbound = scaling["inbound_outcome_count"]
    assert isinstance(inbound, int)
    below = _try(cdf, BELOW_LOWER_BOUND, scaling)
    above = _try(cdf, ABOVE_UPPER_BOUND, scaling)
    assert (below is None) == (cdf[0] == 0.0)
    assert (above is None) == (1 - cdf[-1] == 0.0)
    if below is not None:
        assert below.bucket == 0 and below.log_mass == math.log(cdf[0])
    if above is not None:
        assert above.bucket == inbound + 1 and above.log_mass == math.log(1 - cdf[-1])
    event(f"markers scored: {(below is not None) + (above is not None)}")


# ── 3. the value lies in its bucket ──────────────────────────────────────────


@given(valid=valid_inputs(markers=False))
def test_the_resolved_value_lies_inside_the_bucket_it_was_assigned(
    valid: tuple[list[float], str, dict[str, object]],
) -> None:
    _, resolution, scaling = valid
    inbound = scaling["inbound_outcome_count"]
    low, high = scaling["range_min"], scaling["range_max"]
    assert isinstance(inbound, int) and isinstance(low, float) and isinstance(high, float)
    # A uniform CDF with mass outside both bounds, so no bucket is empty and none refuses.
    uniform = [(index + 1) / (inbound + 2) for index in range(inbound + 1)]
    score = score_continuous(uniform, resolution, **_open(scaling))
    value = float(resolution)
    # The two outside buckets are exactly the two strict inequalities, on every scale: a
    # value one float below the lower bound is below it, however a log scale would round.
    assert (score.bucket == 0) == (value < low)
    assert (score.bucket == inbound + 1) == (value > high)
    if 1 <= score.bucket <= inbound:
        # The edges are rebuilt through the inverse scaling, so allow its rounding, no more.
        slack = 1e-6 * (high - low)
        lower_edge = _value_at((score.bucket - 1) / inbound, scaling)
        upper_edge = _value_at(score.bucket / inbound, scaling)
        assert min(lower_edge, upper_edge) - slack <= value <= max(lower_edge, upper_edge) + slack
    kind = "below" if value < low else "above" if value > high else "inbound"
    event(f"value: {kind}")


# How far, in buckets, an exact location may sit outside the bucket the scorer chose. The
# platform's own rule moves an edge up by 1e-10 of a bucket; float rounding of the location
# measured at most 3.6e-16 over 200,000 scales of every size, times at most 24 buckets here.
_EDGE_TOLERANCE = Decimal("1e-9")


@given(
    data=st.data(),
    scaling=scalings(kinds=("zero_below", "zero_above")),
    inbound=INBOUND,
)
def test_a_log_scale_bucket_holds_the_exact_location_of_its_value(
    data: st.DataObject, scaling: dict[str, object], inbound: int
) -> None:
    """Review round 1, B2, and the two failures of the same kind found while fixing it.

    The value-space check above allows a millionth of the range, and on a log scale spanning
    two hundred decades a millionth of the range is wider than most buckets. This one is in
    location space: the bucket must hold the location the platform's formula gives in exact
    arithmetic, to a billionth of a bucket, on every log scale the scorer accepts. That
    covers a ratio of 1e300 (the platform's product overflows), a zero point 1e-200 above
    the range (its sum cancels) and a zero point 1e15 range-widths away (its two logarithms
    cancel).
    """
    low, high, zero = scaling["range_min"], scaling["range_max"], scaling["zero_point"]
    assert isinstance(low, float) and isinstance(high, float) and isinstance(zero, float)
    value = data.draw(
        st.one_of(
            # Spread evenly over the locations, so every bucket of a wide scale is reached.
            LOCATIONS.map(lambda location: min(max(_value_at(location, scaling), low), high)),
            # A hair either side of a bucket edge, from a third of a bucket down to 1e-8 of
            # one. A bucket is all the scorer returns, so an error in the location only
            # shows when it carries a value across an edge; this is what makes a small one
            # visible (the platform's expression is 1e-5 of the range out at a zero point
            # 1e9 range-widths away, and nothing else here would notice).
            st.builds(
                lambda edge, decade, side: min(
                    max(_value_at((edge + side * 10.0**-decade) / inbound, scaling), low), high
                ),
                st.integers(min_value=0, max_value=inbound),
                st.floats(min_value=0.5, max_value=8.0),
                st.sampled_from([1.0, -1.0]),
            ),
            st.floats(min_value=low, max_value=high),
            st.sampled_from([low, high]),
        )
    )
    uniform = [(index + 1) / (inbound + 2) for index in range(inbound + 1)]
    score = score_continuous(uniform, repr(value), inbound_outcome_count=inbound, **_open(scaling))
    assert 1 <= score.bucket <= inbound
    exact = _exact_location(value, scaling) * inbound
    assert score.bucket - 1 - _EDGE_TOLERANCE <= exact <= score.bucket + _EDGE_TOLERANCE
    # Reach: did the platform's own product, measured from the nearest bound, overflow?
    near, far = (low - zero, high - zero) if zero < low else (zero - high, zero - low)
    offset = value - low if zero < low else high - value
    overflowed = not math.isfinite(offset * (far / near - 1.0) + (high - low))
    side = "below" if zero < low else "above"
    event(f"zero {side}: product {'overflowed' if overflowed else 'finite'}")
    event(
        f"ratio: {'under 1 + 1e-9' if far / near - 1.0 < 1e-9 else 'above 1e16' if far / near > 1e16 else 'between'}"
    )
    where = "only" if inbound == 1 else "first" if score.bucket == 1 else "last"
    event(f"bucket: {where if score.bucket in (1, inbound) else 'between'}")


@given(data=st.data(), scaling=scalings(), inbound=INBOUND)
def test_an_exact_edge_of_a_linear_scale_belongs_to_the_lower_bucket(
    data: st.DataObject, scaling: dict[str, object], inbound: int
) -> None:
    """The platform's ``+ 1 - 1e-10``: edge ``k`` of ``N`` is bucket ``k``, and edge 0 is 1."""
    edge = data.draw(st.integers(min_value=0, max_value=inbound))
    # A range of exactly [0, N] makes edge k the integer k, with no rounding in the way.
    exact = {**scaling, "range_min": 0.0, "range_max": float(inbound), "zero_point": None}
    uniform = [(index + 1) / (inbound + 2) for index in range(inbound + 1)]
    score = score_continuous(
        uniform, repr(float(edge)), inbound_outcome_count=inbound, **_open(exact)
    )
    assert score.bucket == max(edge, 1)
    event("edge: lower bound" if edge == 0 else "edge: upper bound" if edge == inbound else "edge")


# ── 4. order ─────────────────────────────────────────────────────────────────


@given(valid=valid_inputs(), data=st.data())
def test_a_larger_resolution_never_lands_in_an_earlier_bucket(
    valid: tuple[list[float], str, dict[str, object]], data: st.DataObject
) -> None:
    _, _, scaling = valid
    inbound = scaling["inbound_outcome_count"]
    low, high = scaling["range_min"], scaling["range_max"]
    assert isinstance(inbound, int) and isinstance(low, float) and isinstance(high, float)
    width = high - low
    values = st.one_of(
        st.floats(min_value=max(low - width, -1.7e308), max_value=min(high + width, 1.7e308)),
        # Spread evenly over the locations, which on a log scale plain floats are not.
        LOCATIONS.map(lambda location: min(max(_value_at(location, scaling), low), high)),
    )
    first, second = sorted([data.draw(values), data.draw(values)])
    uniform = [(index + 1) / (inbound + 2) for index in range(inbound + 1)]
    opened = _open(scaling)
    earlier = score_continuous(uniform, repr(first), **opened).bucket
    later = score_continuous(uniform, repr(second), **opened).bucket
    assert earlier <= later
    assert score_continuous(uniform, BELOW_LOWER_BOUND, **opened).bucket <= earlier
    assert later <= score_continuous(uniform, ABOVE_UPPER_BOUND, **opened).bucket
    event(f"buckets apart: {min(later - earlier, 3)}")


# ── 5. the buckets partition the distribution ────────────────────────────────


@given(valid=valid_inputs())
def test_one_resolution_per_bucket_recovers_all_the_mass(
    valid: tuple[list[float], str, dict[str, object]],
) -> None:
    cdf, _, scaling = valid
    inbound = scaling["inbound_outcome_count"]
    assert isinstance(inbound, int)
    resolutions = [BELOW_LOWER_BOUND, ABOVE_UPPER_BOUND] + [
        repr(_value_at((bucket - 0.5) / inbound, scaling)) for bucket in range(1, inbound + 1)
    ]
    seen: dict[int, float] = {}
    empty = 0
    for resolution in resolutions:
        score = _try(cdf, resolution, scaling)
        if score is None:
            empty += 1
        else:
            assert score.bucket not in seen
            seen[score.bucket] = math.exp(score.log_mass)
    assert len(seen) + empty == inbound + 2
    assert math.fsum(seen.values()) == pytest.approx(1.0, abs=1e-9)
    event(f"empty buckets: {min(empty, 3)}")


# ── 6. the score follows the mass on the resolved bucket ─────────────────────


@given(valid=valid_inputs(), weight=st.floats(min_value=0.01, max_value=0.99))
def test_more_mass_on_the_resolved_bucket_scores_higher(
    valid: tuple[list[float], str, dict[str, object]], weight: float
) -> None:
    cdf, resolution, scaling = valid
    before = _try(cdf, resolution, scaling)
    assume(before is not None)
    assert before is not None
    pmf = _pmf(cdf)
    assume(pmf[before.bucket] < 0.999)
    # Mix the forecast with a spike on the resolved bucket and rebuild the CDF from it.
    mixed = [(1 - weight) * mass for mass in pmf]
    mixed[before.bucket] += weight
    rebuilt: list[float] = []
    running = 0.0
    for mass in mixed[:-1]:
        running = min(running + mass, 1.0)
        rebuilt.append(running)
    if mixed[-1] == 0.0:
        # Nothing above the upper bound, before or after: the sum is 1 but for rounding, and
        # a closed bound needs it exact.
        rebuilt[-1] = 1.0
    after = score_continuous(rebuilt, resolution, **scaling)
    assert after.bucket == before.bucket
    assert after.log_mass > before.log_mass
    assert platform_baseline_points(after) > platform_baseline_points(before)
    event("mass raised")


@given(valid=valid_inputs(min_inbound=5), data=st.data())
def test_mass_moved_between_other_buckets_does_not_change_the_score(
    valid: tuple[list[float], str, dict[str, object]], data: st.DataObject
) -> None:
    cdf, resolution, scaling = valid
    before = _try(cdf, resolution, scaling)
    assume(before is not None)
    assert before is not None
    inbound = scaling["inbound_outcome_count"]
    assert isinstance(inbound, int)
    # Interior CDF points that are not an edge of the resolved bucket may move freely
    # between their neighbours without touching its mass.
    movable = [
        index
        for index in range(1, inbound)
        if index not in (before.bucket, before.bucket - 1) and cdf[index - 1] < cdf[index + 1]
    ]
    assume(movable)
    index = data.draw(st.sampled_from(movable))
    moved = list(cdf)
    moved[index] = data.draw(st.floats(min_value=cdf[index - 1], max_value=cdf[index + 1]))
    after = score_continuous(moved, resolution, **scaling)
    assert (after.bucket, after.log_mass) == (before.bucket, before.log_mass)
    event("mass moved elsewhere")


# ── 7. the persisted form ────────────────────────────────────────────────────


@given(valid=valid_inputs())
def test_the_score_is_stable_across_the_persisted_form(
    valid: tuple[list[float], str, dict[str, object]],
) -> None:
    cdf, resolution, scaling = valid
    stored = json.loads(
        json.dumps(
            {"cdf": cdf, "resolution": resolution, "scaling": scaling},
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    direct = _try(cdf, resolution, scaling)
    replayed = _try(stored["cdf"], stored["resolution"], stored["scaling"])
    assert direct == replayed
    assert _try(tuple(cdf), resolution, scaling) == direct
    event("replayed: scored" if direct else "replayed: refused")


@given(valid=valid_inputs())
def test_an_integer_zero_or_one_scores_as_the_float_does(
    valid: tuple[list[float], str, dict[str, object]],
) -> None:
    """The platform stores a closed bound's CDF end as the JSON integer 0 or 1."""
    cdf, resolution, scaling = valid
    as_stored: list[float | int] = [int(point) if point in (0.0, 1.0) else point for point in cdf]
    assume(any(type(point) is int for point in as_stored))
    low, high = scaling["range_min"], scaling["range_max"]
    assert isinstance(low, float) and isinstance(high, float)
    whole = {**scaling}
    if low == int(low) and high == int(high):
        whole.update(range_min=int(low), range_max=int(high))
    assert _try(as_stored, resolution, whole) == _try(cdf, resolution, scaling)
    event("integer points")


# ── 8. the platform's baseline scale ─────────────────────────────────────────


@given(valid=valid_inputs())
def test_the_baseline_scale_is_the_platforms_formula(
    valid: tuple[list[float], str, dict[str, object]],
) -> None:
    cdf, resolution, scaling = valid
    score = _try(cdf, resolution, scaling)
    assume(score is not None)
    assert score is not None
    pmf = _pmf(cdf)
    open_bounds = bool(scaling["open_upper_bound"]) + bool(scaling["open_lower_bound"])
    # scoring/score_math.py, evaluate_forecasts_baseline_spot_forecast, restated.
    if score.bucket in [0, len(pmf) - 1]:
        baseline = 0.05
    else:
        baseline = (1 - 0.05 * open_bounds) / (len(pmf) - 2)
    expected = 100 * math.log(pmf[score.bucket] / baseline) / 2
    assert platform_baseline_points(score) == pytest.approx(expected, rel=1e-9, abs=1e-9)
    event(f"open bounds: {open_bounds}")


# ── 9. a closed bound pins its end of the CDF ────────────────────────────────


@given(valid=valid_inputs(), lower_open=st.booleans(), upper_open=st.booleans())
def test_a_closed_bound_refuses_exactly_when_the_cdf_puts_mass_outside_it(
    valid: tuple[list[float], str, dict[str, object]], lower_open: bool, upper_open: bool
) -> None:
    """Review round 1, B1. The flags are redrawn here, independently of the CDF, on purpose.

    Every other property builds the CDF from the flags and so can only say what a consistent
    input scores. This one says what an inconsistent input does: it is refused, with its own
    bound's sentence, the lower bound first, before the resolution is looked at.
    """
    cdf, resolution, scaling = valid
    flagged = {**scaling, "open_lower_bound": lower_open, "open_upper_bound": upper_open}
    lower_broken = not lower_open and cdf[0] != 0.0
    upper_broken = not upper_open and cdf[-1] != 1.0
    try:
        score = score_continuous(cdf, resolution, **flagged)
    except ScoreError as refusal:
        expected = LOWER_ENDPOINT if lower_broken else UPPER_ENDPOINT if upper_broken else ZERO_MASS
        assert str(refusal) == expected
        event(f"refused: {'endpoint' if lower_broken or upper_broken else 'zero mass'}")
    else:
        assert not lower_broken and not upper_broken
        assert score.open_bounds == lower_open + upper_open
        event("scored")
    event(f"mass outside a closed bound: {lower_broken + upper_broken}")
