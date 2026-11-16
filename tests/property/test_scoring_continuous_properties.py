"""Property tests for the local log score of numeric and discrete forecasts (M4-809).

The CLAUDE.md pre-review fuzz pass over ``scoring.score_continuous``:

1. Nothing but ``ScoreError`` escapes, over structured and arbitrary inputs, and every
   refusal's text is one of a fixed set of sentences written out here -- so no message can
   carry a value, whatever the value was.
2. On every valid input the score is the natural log of the mass an independently derived
   PMF puts on the returned bucket, or a refusal exactly when that mass is zero.
3. The resolved value lies inside the bucket it was assigned, checked by mapping the bucket's
   edges back through the platform's *inverse* scaling, which this module does not contain.
4. Buckets are ordered as resolutions are: a larger value never lands in an earlier bucket.
5. One representative resolution per bucket recovers the whole distribution: the masses sum
   to one.
6. The score rises with the mass on the resolved bucket and with nothing else.
7. The persisted form replays: JSON out and back gives the same score to the bit, and an
   integer 0 or 1 where the platform stores one scores as the float does.
8. The platform's baseline scale, restated from its source, agrees with the helper.

Reach is measured with ``hypothesis.event``: scale kind, bucket kind and refusals.
"""

from __future__ import annotations

import json
import math

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
        "resolution must be a non-empty string",
        "resolution must be a decimal number or a bound marker",
        "resolution must be a finite number",
        "the log score is undefined: the resolved bucket had probability 0",
        "question_type has no peer scale this module knows",
    }
)

# ── strategies ───────────────────────────────────────────────────────────────

INBOUND = st.integers(min_value=1, max_value=24)


@st.composite
def cdfs(draw: st.DrawFn, inbound: int) -> list[float]:
    """A valid CDF, built from the masses of its ``inbound + 2`` buckets.

    About a quarter of the masses are zero, so empty buckets, flat stretches and closed bounds
    (no mass outside) are all drawn, without being the common case.
    """
    mass = st.one_of(
        st.floats(min_value=0.01, max_value=1.0),
        st.floats(min_value=0.01, max_value=1.0),
        st.floats(min_value=1e-9, max_value=1.0),
        st.just(0.0),
    )
    masses = draw(st.lists(mass, min_size=inbound + 2, max_size=inbound + 2))
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


@st.composite
def scalings(draw: st.DrawFn) -> dict[str, object]:
    low = draw(st.floats(min_value=-1e6, max_value=1e6))
    width = draw(st.floats(min_value=1e-3, max_value=1e6))
    high = low + width
    assume(high > low)
    kind = draw(st.sampled_from(["linear", "zero_below", "zero_above"]))
    zero: float | None = None
    if kind != "linear":
        gap = draw(st.floats(min_value=1e-3, max_value=1e6))
        zero = low - gap if kind == "zero_below" else high + gap
        assume(not low <= zero <= high)
        ratio = (high - zero) / (low - zero)
        assume(math.isfinite(ratio) and ratio > 0.0 and ratio != 1.0)
    event(f"scale: {kind}")
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
    inbound = draw(st.integers(min_value=min_inbound, max_value=24))
    scaling = {**draw(scalings()), "inbound_outcome_count": inbound}
    low, high = scaling["range_min"], scaling["range_max"]
    assert isinstance(low, float) and isinstance(high, float)
    width = high - low
    numbers = st.one_of(
        st.floats(min_value=low - width, max_value=high + width).map(repr),
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
    )
    choices = [numbers, st.sampled_from([BELOW_LOWER_BOUND, ABOVE_UPPER_BOUND])]
    resolution = draw(st.one_of(choices if markers else choices[:1]))
    return draw(cdfs(inbound)), resolution, scaling


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


def _value_at(location: float, scaling: dict[str, object]) -> float:
    """The platform's ``unscaled_location_to_scaled_location``: the inverse of what is tested."""
    low, high, zero = scaling["range_min"], scaling["range_max"], scaling["zero_point"]
    assert isinstance(low, float) and isinstance(high, float)
    if zero is None:
        return low + (high - low) * location
    assert isinstance(zero, float)
    ratio = (high - zero) / (low - zero)
    return low + (high - low) * (ratio**location - 1) / (ratio - 1)


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
        assert str(refusal) == "the log score is undefined: the resolved bucket had probability 0"
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
    score = score_continuous(uniform, resolution, **scaling)
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


@given(data=st.data(), scaling=scalings(), inbound=INBOUND)
def test_an_exact_edge_of_a_linear_scale_belongs_to_the_lower_bucket(
    data: st.DataObject, scaling: dict[str, object], inbound: int
) -> None:
    """The platform's ``+ 1 - 1e-10``: edge ``k`` of ``N`` is bucket ``k``, and edge 0 is 1."""
    edge = data.draw(st.integers(min_value=0, max_value=inbound))
    # A range of exactly [0, N] makes edge k the integer k, with no rounding in the way.
    exact = {**scaling, "range_min": 0.0, "range_max": float(inbound), "zero_point": None}
    uniform = [(index + 1) / (inbound + 2) for index in range(inbound + 1)]
    score = score_continuous(uniform, repr(float(edge)), inbound_outcome_count=inbound, **exact)
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
    values = st.floats(min_value=low - width, max_value=high + width)
    first, second = sorted([data.draw(values), data.draw(values)])
    uniform = [(index + 1) / (inbound + 2) for index in range(inbound + 1)]
    earlier = score_continuous(uniform, repr(first), **scaling).bucket
    later = score_continuous(uniform, repr(second), **scaling).bucket
    assert earlier <= later
    assert score_continuous(uniform, BELOW_LOWER_BOUND, **scaling).bucket <= earlier
    assert later <= score_continuous(uniform, ABOVE_UPPER_BOUND, **scaling).bucket
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
