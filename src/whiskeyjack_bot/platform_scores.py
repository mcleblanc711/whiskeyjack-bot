"""Metaculus's own scores for this account's forecasts, read from a stored observation (M4-803).

Pure: no ledger, no clock, no network, no SDK. :func:`lifecycle.record_platform_scores` reads
a record's latest scorable resolution -- its stored, digest-verified ``source_response`` -- and
calls this module; ``017_platform_score_events.sql`` constrains the rows that result.

**These are the platform's numbers, not a replica.** D30 forbids computing a local stand-in
for Metaculus's continuous scores. Nothing here computes anything: every value is copied out
of the payload Metaculus returned for the resolved question, at
``question.my_forecasts.score_data`` (the forecasting account's own scores; the endpoint
unmasks them only for a question the account predicted on, as it does the resolution). Each
row's metric carries a ``platform_`` prefix, its ``comparison_baseline`` names what the score
is measured against, and its ``implementation_version`` names the path it was read from --
together, the score's source (M4-803's acceptance criterion). The row also cites the
``resolution_events`` row whose stored response holds the value, so it replays exactly.

The four metrics are the platform's four score types for a forecaster:

- ``platform_baseline_score`` / ``platform_peer_score``: time-weighted over the forecast's
  coverage of the question's lifetime, against a uniform prior and against the other
  forecasters respectively.
- ``platform_spot_baseline_score`` / ``platform_spot_peer_score``: the same comparisons, taken
  at the question's spot-scoring time. ``spot_peer`` is MiniBench's ``default_score_type``.

``relative_legacy_score`` is the platform's pre-2023 score and is not recorded;
``coverage`` and ``weighted_coverage`` qualify the time-weighted scores rather than being
scores, and stay in the stored response (``docs/M4-NOTES.md``, M4-803).

**Refused, never defaulted.** A scorable observation with no scores, a score of the wrong
type or a non-finite one is a :class:`PlatformScoreError`: the caller reports the record
``failed`` and ``score`` exits non-zero (owner decision 2026-09-23). A missing score is never
read as zero, and a missing ``score_data`` is never read as "nothing to record".
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, get_args

from whiskeyjack_bot.resolution import ResolutionError, select_question

PlatformMetric = Literal[
    "platform_spot_peer_score",
    "platform_spot_baseline_score",
    "platform_peer_score",
    "platform_baseline_score",
]

# What a platform score is measured against: `score_events.comparison_baseline`, which 001
# created for exactly this label and 015 forbids on a local score.
ComparisonBaseline = Literal["peer", "baseline"]

PLATFORM_METRICS: Final[frozenset[str]] = frozenset(get_args(PlatformMetric))

# Write order. The first is the row a `scored` lifecycle event links when the platform writer
# is the one that moves the record (numeric and discrete; 003 lets an event link exactly one
# row). `spot_peer` first: it is the tournament's own `default_score_type`.
PLATFORM_METRIC_ORDER: Final[tuple[PlatformMetric, ...]] = get_args(PlatformMetric)

# metric -> the key it is read from under `question.my_forecasts.score_data`.
SCORE_DATA_KEYS: Final[Mapping[PlatformMetric, str]] = MappingProxyType(
    {
        "platform_spot_peer_score": "spot_peer_score",
        "platform_spot_baseline_score": "spot_baseline_score",
        "platform_peer_score": "peer_score",
        "platform_baseline_score": "baseline_score",
    }
)

COMPARISON_BASELINES: Final[Mapping[PlatformMetric, ComparisonBaseline]] = MappingProxyType(
    {
        "platform_spot_peer_score": "peer",
        "platform_spot_baseline_score": "baseline",
        "platform_peer_score": "peer",
        "platform_baseline_score": "baseline",
    }
)

# The version names the extraction -- where in the payload the value was read -- because there
# is no formula here to version. `017_platform_score_events.sql` requires this exact shape.
IMPLEMENTATION_VERSIONS: Final[Mapping[PlatformMetric, str]] = MappingProxyType(
    {metric: f"{metric}/metaculus_score_data/1" for metric in PLATFORM_METRIC_ORDER}
)

# Version string -> (metric, score_data key). Append-only, `scoring._IMPLEMENTATIONS`'s rule: a
# retired version keeps its entry so a row written under it can still be re-read and checked.
_IMPLEMENTATIONS: Final[Mapping[str, tuple[PlatformMetric, str]]] = MappingProxyType(
    {
        IMPLEMENTATION_VERSIONS[metric]: (metric, SCORE_DATA_KEYS[metric])
        for metric in PLATFORM_METRIC_ORDER
    }
)

_SQLITE_INT_MAX: Final = 2**63 - 1


class PlatformScoreError(Exception):
    """A stored observation does not carry a readable platform score.

    The message names the rule and this module's own key names, never a value: a score, a
    label or an identifier read out of the payload is content from the ledger.
    """


@dataclass(frozen=True)
class PlatformScore:
    """One platform score, as ``score_events`` stores it."""

    metric: PlatformMetric
    value: float
    implementation_version: str
    comparison_baseline: ComparisonBaseline


def _score_data(source_response: object, question_id: object) -> dict[str, object]:
    if type(question_id) is not int or not 1 <= question_id <= _SQLITE_INT_MAX:
        raise PlatformScoreError("question_id must be a positive integer")
    if type(source_response) is not dict:
        raise PlatformScoreError("the source response must be a JSON object")
    try:
        question = select_question(source_response, question_id)
    except ResolutionError as exc:
        # resolution.py's messages name fields only.
        raise PlatformScoreError(f"the source response cannot be read: {exc}") from None
    mine = question.get("my_forecasts")
    if type(mine) is not dict:
        raise PlatformScoreError("the question carries no my_forecasts object")
    data = mine.get("score_data")
    # Missing, null and empty are one refusal: each is a scorable observation with no scores.
    if data is None or data == {}:
        raise PlatformScoreError("the observation carries no platform scores")
    if type(data) is not dict:
        raise PlatformScoreError("my_forecasts.score_data must be a JSON object")
    return data


def _read(data: dict[str, object], key: str) -> float:
    if key not in data:
        raise PlatformScoreError(f"my_forecasts.score_data has no {key}")
    value = data[key]
    # Exact type: a bool is an int, and neither is how the platform serializes a score (every
    # one of the 140 live values observed 2026-09-23 was a float). An int would also be
    # admitted by SQLite's `=` against a REAL while this module refuses it.
    if type(value) is not float:
        raise PlatformScoreError(f"my_forecasts.score_data.{key} must be a float")
    if not math.isfinite(value):
        raise PlatformScoreError(f"my_forecasts.score_data.{key} must be finite")
    return value


def extract_platform_scores(
    source_response: object, question_id: object
) -> tuple[PlatformScore, ...]:
    """Every current platform metric for ``question_id``, in :data:`PLATFORM_METRIC_ORDER`.

    ``source_response`` is the parsed post payload stored on a resolution row. All four are
    read or none is: one malformed score refuses the observation.
    """
    data = _score_data(source_response, question_id)
    return tuple(
        PlatformScore(
            metric=metric,
            value=_read(data, SCORE_DATA_KEYS[metric]),
            implementation_version=IMPLEMENTATION_VERSIONS[metric],
            comparison_baseline=COMPARISON_BASELINES[metric],
        )
        for metric in PLATFORM_METRIC_ORDER
    )


def recompute(
    implementation_version: object, metric: object, source_response: object, question_id: object
) -> float:
    """Re-read one stored platform score with the implementation that wrote it.

    Refuses a version this build does not register, and a version registered under a
    different metric: a row whose metric and version disagree was not written by this module.
    """
    if type(implementation_version) is not str or implementation_version not in _IMPLEMENTATIONS:
        raise PlatformScoreError("implementation_version is not a registered platform version")
    registered_metric, key = _IMPLEMENTATIONS[implementation_version]
    if metric != registered_metric:
        raise PlatformScoreError("the metric does not match its implementation_version")
    return _read(_score_data(source_response, question_id), key)
