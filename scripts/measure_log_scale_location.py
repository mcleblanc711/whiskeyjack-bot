#!/usr/bin/env python3
"""Measure the continuous scorer's log-scale location against exact arithmetic (M4-809).

    uv run python scripts/measure_log_scale_location.py

``docs/M4-NOTES.md`` § M4-809 states two things about ``scoring._unscaled_location`` on a log
scale, and this prints the numbers behind both. It reads nothing and writes nothing; the
draws are seeded, so a second run prints the same tables.

1. **How far the location is from exact**, by regime, for the scorer and for the platform's
   own expression evaluated in floats. Exact means: the distances as exact rationals (a float
   is one), the two logarithms in 60-digit decimals.
2. **Whether the two choose the same bucket** on scales the size of a real question, for
   values inside a bucket and for values on an exact bucket edge.

The platform's expression is restated here from Metaculus's ``utils/the_math/formulas.py``
(``scaled_location_to_unscaled_location``), not imported from ``scoring.py``, which no
longer contains it.
"""

from __future__ import annotations

import math
import random
from decimal import Context, Decimal
from fractions import Fraction
from typing import Final

from whiskeyjack_bot.scoring import _log_scale_ratio, _unscaled_location

_EXACT: Final = Context(prec=60)
_BUCKETS: Final = 200
_BUCKET_EPSILON: Final = 1e-10
_PER_REGIME: Final = 20_000
_PER_ROW: Final = 100_000

Scale = tuple[float, float, float]  # range_min, range_max, zero_point


def exact_location(value: float, scale: Scale) -> Decimal:
    """``ln((x - zero) / (min - zero)) / ln((max - zero) / (min - zero))``, exactly."""
    low, high, zero = (Fraction(number) for number in scale)

    def ln(ratio: Fraction) -> Decimal:
        return _EXACT.divide(Decimal(ratio.numerator), Decimal(ratio.denominator)).ln(_EXACT)

    return _EXACT.divide(
        ln((Fraction(value) - zero) / (low - zero)), ln((high - zero) / (low - zero))
    )


def platform_location(value: float, scale: Scale) -> float | None:
    """The platform's expression in floats; ``None`` where it has no finite value."""
    low, high, zero = scale
    ratio = (high - zero) / (low - zero)
    argument = (value - low) * (ratio - 1.0) + (high - low)
    if not argument > 0.0 or not math.isfinite(argument):
        return None
    return (math.log(argument) - math.log(high - low)) / math.log(ratio)


def bucket(location: float) -> int:
    return max(int(min(max(location, 0.0), 1.0) * _BUCKETS + 1.0 - _BUCKET_EPSILON), 1)


def usable(scale: Scale) -> bool:
    low, high, zero = scale
    if not (math.isfinite(low) and math.isfinite(high) and high > low):
        return False
    if not math.isfinite(high - low) or low <= zero <= high:
        return False
    ratio = _log_scale_ratio(low, high, zero)
    return math.isfinite(ratio) and ratio > 1.0


def near_scale(
    rng: random.Random, decades: tuple[float, float], ratio_decades: tuple[float, float]
) -> Scale | None:
    """A zero point within one range-width of the nearest bound: a ratio of 2 or more."""
    width = 10.0 ** rng.uniform(*decades)
    gap = width * 10.0 ** -rng.uniform(*ratio_decades)
    if not 1e-300 <= gap:
        return None
    zero = rng.choice([0.0, 0.0, 1.0, -1.0]) * gap * 10.0 ** rng.uniform(-9.0, 9.0)
    if rng.random() < 0.5:
        scale = (zero + gap, zero + gap + width, zero)
    else:
        scale = (zero - gap - width, zero - gap, zero)
    return scale if usable(scale) else None


def far_scale(rng: random.Random, decade: int) -> Scale | None:
    """A range within 1e6 of zero whose zero point is ``10 ** decade`` range-widths away."""
    low = rng.uniform(-1e6, 1e6)
    width = 10.0 ** rng.uniform(-3.0, 6.0)
    gap = width * 10.0**decade * rng.uniform(1.0, 9.9)
    scale = (low, low + width, low - gap if rng.random() < 0.5 else low + width + gap)
    return scale if usable(scale) else None


def value_in(rng: random.Random, scale: Scale) -> float:
    """Inside the range: anywhere, crowded against either bound, or a bound itself."""
    low, high, zero = scale
    width = high - low
    choice = rng.randrange(4)
    if choice == 0:
        value = rng.uniform(low, high)
    elif choice == 1:
        value = low + width * 10.0 ** rng.uniform(-300.0, 0.0)
    elif choice == 2:
        value = high - width * 10.0 ** rng.uniform(-300.0, 0.0)
    else:
        value = rng.choice([low, high])
    return min(max(value, low), high)


def edge_value(rng: random.Random, scale: Scale) -> float:
    """A bucket edge of the 200-bucket scale, as nearly as a float allows."""
    low, high, zero = scale
    ratio = (high - zero) / (low - zero)
    edge = rng.randrange(0, _BUCKETS + 1) / _BUCKETS
    return min(max(zero + (low - zero) * ratio**edge, low), high)


def error_by_regime(rng: random.Random) -> None:
    print("1. Largest |location - exact|, as a fraction of the range")
    print(f"   {_PER_REGIME} scales per row, one value each\n")
    print(f"   {'regime':46s} {'scorer':>10s} {'platform, floats':>18s} {'no finite value':>16s}")
    regimes: list[tuple[str, tuple[float, float] | None, tuple[float, float] | None, int]] = [
        ("real-question size, ratio 2 to 1e9", (-3.0, 6.0), (0.0, 9.0), 0),
        ("any size, ratio 2 to 1e300", (-280.0, 280.0), (0.0, 300.0), 0),
        ("platform's product overflows", (160.0, 280.0), (150.0, 300.0), 0),
    ]
    regimes += [(f"zero point 1e{decade} range-widths away", None, None, decade) for decade in
                (3, 6, 9, 12, 14, 15)]  # fmt: skip
    for name, decades, ratio_decades, decade in regimes:
        worst_scorer = worst_platform = 0.0
        undefined = done = 0
        lowest, highest = 1.0, 0.0
        while done < _PER_REGIME:
            if decades is not None and ratio_decades is not None:
                scale = near_scale(rng, decades, ratio_decades)
            else:
                scale = far_scale(rng, decade)
            if scale is None:
                continue
            value = value_in(rng, scale)
            exact = exact_location(value, scale)
            location = _unscaled_location(value, *scale)
            lowest, highest = min(lowest, location), max(highest, location)
            worst_scorer = max(worst_scorer, float(abs(Decimal(location) - exact)))
            platform = platform_location(value, scale)
            if platform is None:
                undefined += 1
            else:
                worst_platform = max(worst_platform, float(abs(Decimal(platform) - exact)))
            done += 1
        assert 0.0 <= lowest and highest <= 1.0, (name, lowest, highest)
        print(f"   {name:46s} {worst_scorer:10.1e} {worst_platform:18.1e} {undefined:16d}")
    print("\n   The scorer's location was inside [0, 1] on every draw.\n")


def agreement(rng: random.Random) -> None:
    print(f"2. Same bucket of {_BUCKETS} as the platform's expression in floats?")
    print(f"   real-question size (width 1e-3 to 1e6), {_PER_ROW} values per row\n")
    print(
        f"   {'zero point':11s} {'ratio up to':>11s} {'values':>9s} {'differ':>7s}"
        f" {'platform error':>15s} {'scorer error':>13s}"
    )
    for side in ("below", "above"):
        for ratio_decade in (3.0, 6.0, 9.0):
            for kind in ("interior", "on an edge"):
                differ = done = 0
                worst_scorer = worst_platform = 0.0
                while done < _PER_ROW:
                    scale = near_scale(rng, (-3.0, 6.0), (0.0, ratio_decade))
                    if scale is None or (scale[2] < scale[0]) != (side == "below"):
                        continue
                    if kind == "interior":
                        value = rng.uniform(scale[0], scale[1])
                    else:
                        value = edge_value(rng, scale)
                    platform = platform_location(value, scale)
                    if platform is None:
                        continue
                    location = _unscaled_location(value, *scale)
                    exact = exact_location(value, scale)
                    differ += bucket(location) != bucket(platform)
                    worst_scorer = max(worst_scorer, float(abs(Decimal(location) - exact)))
                    worst_platform = max(worst_platform, float(abs(Decimal(platform) - exact)))
                    done += 1
                print(
                    f"   {side:11s} {f'1e{ratio_decade:.0f}':>11s} {kind:>9s} {differ:7d}"
                    f" {worst_platform:15.1e} {worst_scorer:13.1e}"
                )
    print()


def main() -> int:
    rng = random.Random(809)
    error_by_regime(rng)
    agreement(rng)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
