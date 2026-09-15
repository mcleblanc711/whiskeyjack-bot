"""Local Brier and log scores for binary and multiple-choice forecasts (M4-802).

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
"""

from __future__ import annotations

import math
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
