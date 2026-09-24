"""The attribution report dataset (M5-804).

The ledger records every forecast, its evidence and its outcome; this module derives the
dataset the project exists to produce from it -- outcomes grouped by the attribution axes, with
counts, calibration bins and score summaries, and a small-sample warning on every cell. It is a
**derived artifact** (D29), not a second source of truth, and it is kept apart from
:mod:`whiskeyjack_bot.export` on purpose: an export is *what the ledger holds*, a report is
*what was derived from it*, and the two version independently (``REPORT_SCHEMA_VERSION``).

**Replayable by construction.** The report is a pure function of the ledger's content:

- The ledger is opened read-only (:func:`ledger.connect_readonly`) and every read runs inside
  one deferred transaction, so the report is one consistent snapshot.
- Every fact is read through the ledger's own **verified** readers -- the forecast through
  ``read_forecast_record`` (which re-verifies ``forecast_sha256``), the latest resolution
  through ``latest_resolution`` (both digests recomputed), and every score through
  ``read_local_scores``/``read_platform_scores`` (recomputed or re-read, exactly). A row that
  does not attest to itself is refused, never repaired.
- ``records.jsonl`` cites the inputs each row was computed from (``forecast_sha256``,
  ``observation_sha256``, ``source_response_sha256``, score ``event_id``), ``report.json`` is
  bound to it by ``records_sha256``, and neither carries a timestamp. Only ``manifest.json`` does.

**What a cell may and may not mix.** A score cell is one ``(metric, implementation_version)``,
so a local score (M4-802, this program's arithmetic) and a platform score (M4-803, Metaculus's
own number) can never share one, and a local score is never labelled a Metaculus score. The
platform scores are the only ones comparable across question types. A *peer* score is a score
**of** this account's forecast measured against other forecasters; the community aggregate
itself is never read, shown or derived.

**Overlapping axes.** A record carries several reasoning tags, may carry several Metaculus
categories and several evidence-gap codes, so on those axes a record sits in several groups at
once. Their group counts do not sum to the population, and nothing in the report sums them.

**One scored subject per question.** Platform scores are per question, so if two posted
versions of one question existed, counting both would count one platform number twice. The
latest posted version is the subject; an earlier posted one is ``superseded``. No live question
has a second version today.

Error hygiene follows ``ExportError``: a :class:`ReportError` never echoes a stored value.
Collaborators' errors are re-raised with their own already-sanitized messages (``show.py``'s
pattern). Filesystem paths are the settled M1-401 carve-out and are rendered.

Purely local: no network access on any path through here, and no import of the paid or
submission paths (``tournament.py`` is deliberately not imported).
"""

from __future__ import annotations

import bisect
import hashlib
import math
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final, Literal, cast, get_args

from whiskeyjack_bot.artifacts import write_new_file
from whiskeyjack_bot.export import ExportError, canonical_json
from whiskeyjack_bot.forecast.record import ForecastRecordError, record_sha256
from whiskeyjack_bot.forecast.schema import BinaryForecastResponse
from whiskeyjack_bot.forecast.store import read_forecast_record, read_model_call
from whiskeyjack_bot.ledger import LEDGER_SCHEMA_VERSION, connect_readonly
from whiskeyjack_bot.lifecycle import (
    LifecycleError,
    LifecycleStatus,
    current_status,
    latest_resolution,
    read_local_scores,
    read_platform_scores,
)
from whiskeyjack_bot.platform_scores import (
    COMPARISON_BASELINES,
    PLATFORM_METRICS,
    PlatformMetric,
)
from whiskeyjack_bot.resolution import RESOLUTION_KINDS
from whiskeyjack_bot.scoring import LOCAL_METRICS
from whiskeyjack_bot.tournament_state import TournamentError, events

if TYPE_CHECKING:
    from pathlib import Path

REPORT_SCHEMA_VERSION: Final = 1

RECORDS_FILENAME: Final = "records.jsonl"
REPORT_FILENAME: Final = "report.json"
MANIFEST_FILENAME: Final = "manifest.json"

_WHAT: Final = "attribution report"

# Tournament ids whose records are excluded from every summary (owner decision 2026-09-23,
# D43). `bot-testing-area` is Metaculus's test area and `minibench` is the pre-launch slug the
# 2026-09-03 rehearsal ran under; the live series records carry their numeric project ids
# (33122, 33125). Excluded records stay in `records.jsonl`, marked, and are counted.
TEST_TOURNAMENTS: Final[tuple[str, ...]] = ("bot-testing-area", "minibench")

# A cell or bin with fewer observations than this is flagged `small_sample` (owner decision
# 2026-09-23, D43). The conventional minimum for a mean's normal approximation; it is a flag
# on a descriptive number, not a significance test.
SMALL_SAMPLE_THRESHOLD: Final = 30

# Fixed calibration bin edges: the IEEE doubles nearest the decimal tenths, written as
# literals so a probability parsed from the same decimal compares equal to its edge.
CALIBRATION_BIN_EDGES: Final[tuple[float, ...]] = (
    0.0,
    0.1,
    0.2,
    0.3,
    0.4,
    0.5,
    0.6,
    0.7,
    0.8,
    0.9,
    1.0,
)
CALIBRATION_BIN_RULE: Final = (
    "left-closed [lower, upper); the last bin is closed at 1.0; a probability equal to an "
    "edge lies in the bin that edge opens"
)
CALIBRATION_QUESTION_TYPES: Final[tuple[str, ...]] = ("binary",)

# Exactly one per record, assigned by precedence in this order (see `_state`).
RecordState = Literal[
    "not_posted",
    "superseded",
    "awaiting_resolution",
    "withheld",
    "unresolved",
    "annulled",
    "ambiguous",
    "resolved_unscored",
    "scored",
]
STATES: Final[tuple[RecordState, ...]] = get_args(RecordState)

Exclusion = Literal["test_tournament"]
EXCLUSIONS: Final[tuple[Exclusion, ...]] = get_args(Exclusion)

# The codes `pipeline_live.py` writes on an `evidence_gap` journal row (M1-327, M1-349). A
# closed vocabulary: a code nobody defined is refused rather than grouped, so a new writer
# code fails the report loudly until it is described here and in docs/SCHEMA.md.
EvidenceGapCode = Literal["named_source_absent", "evidence_poor"]
_EVIDENCE_GAP_CODES: Final[frozenset[str]] = frozenset(get_args(EvidenceGapCode))

Axis = Literal[
    "all",
    "tournament_id",
    "question_type",
    "model",
    "prompt",
    "question_domain",
    "source_category",
    "reasoning_strategy_tag",
    "evidence_gap",
]
# The three axes on which one record can sit in several groups at once.
OVERLAPPING_AXES: Final[frozenset[Axis]] = frozenset(
    {"source_category", "reasoning_strategy_tag", "evidence_gap"}
)
AXES: Final[tuple[Axis, ...]] = get_args(Axis)

ScoreProvenance = Literal["local", "platform"]

WarningCode = Literal[
    "small_sample",
    "overlapping_axes",
    "unknown_model_cost",
    "resolved_unscored",
    "stale_score_rows",
]
WARNING_CODES: Final[tuple[WarningCode, ...]] = get_args(WarningCode)

_WARNING_MESSAGES: Final[dict[WarningCode, str]] = {
    "small_sample": (
        f"cells below {SMALL_SAMPLE_THRESHOLD} observations; read them as descriptive, not as "
        "evidence of skill"
    ),
    "overlapping_axes": (
        "source_category, reasoning_strategy_tag and evidence_gap groups overlap; their record "
        "counts do not sum to the population and must not be added"
    ),
    "unknown_model_cost": (
        "included records whose model cost is unknown (NULL is unknown, not free); "
        "known_sum_usd covers the known ones only"
    ),
    "resolved_unscored": (
        "included records whose latest observation is resolved but carries no score row; run "
        "`whiskeyjack-bot score`"
    ),
    "stale_score_rows": (
        "score rows citing an observation that is no longer the record's latest; excluded from "
        "every summary"
    ),
}

# Where a record must be to count as posted: `submitted` and the two states after it.
_POSTED: Final[frozenset[str]] = frozenset({"submitted", "resolved", "scored"})
_LIFECYCLE_STATUSES: Final[frozenset[str]] = frozenset(get_args(LifecycleStatus))
_RESOLVED_STATES: Final[frozenset[str]] = frozenset({"scored", "resolved_unscored"})
_BINARY_OUTCOMES: Final[dict[str, int]] = {"yes": 1, "no": 0}


class ReportError(Exception):
    """The attribution report cannot be derived from the ledger.

    Same hygiene rule as ``ExportError`` and ``ShowError``: the message never echoes a stored
    value, a row identifier or raw bytes, and sanitizing raises use ``from None``. A
    collaborator's error is re-raised with its own already-sanitized message. Filesystem
    paths are the settled M1-401 carve-out and are rendered.
    """


@dataclass(frozen=True)
class ResolutionFact:
    """A record's latest resolution observation, as the report cites it."""

    event_id: int
    kind: str
    outcome: str | None
    observation_sha256: str
    source_response_sha256: str
    observed_at_utc: str


@dataclass(frozen=True)
class ScoreFact:
    """One verified score row that cites the record's latest observation."""

    event_id: int
    resolution_event_id: int
    metric: str
    implementation_version: str
    comparison_baseline: str | None
    value: float


@dataclass(frozen=True)
class RecordFacts:
    """Everything the report reads about one forecast record, already verified.

    ``scores`` holds only rows citing the latest observation; rows citing an earlier one are
    counted in ``stale_score_rows`` and summarized nowhere.
    """

    record_id: str
    question_id: int
    post_id: int
    tournament_id: str
    forecast_version: int
    parent_record_id: str | None
    forecast_sha256: str
    generated_at_utc: str
    question_type: str
    question_domain: str | None
    source_categories: tuple[tuple[int, str | None], ...]
    model_provider: str
    model_name: str
    prompt_version: str
    prompt_sha256: str
    reasoning_strategy_tags: tuple[str, ...]
    evidence_gaps: tuple[str, ...]
    lifecycle_status: str
    probability_yes: float | None
    resolution: ResolutionFact | None
    scores: tuple[ScoreFact, ...]
    stale_score_rows: int
    model_cost_usd: float | None
    model_invocations: int | None


@dataclass(frozen=True)
class AttributionRow:
    """A record's facts plus what the report decided about it."""

    facts: RecordFacts
    exclusion: Exclusion | None
    state: RecordState

    @property
    def included(self) -> bool:
        return self.exclusion is None


@dataclass(frozen=True)
class ReportResult:
    """What a completed report wrote, for the CLI to print."""

    destination: Path
    ledger_schema_version: int
    records: int
    excluded: int
    included: int
    states: tuple[tuple[RecordState, int], ...]
    warnings: tuple[tuple[WarningCode, int], ...]


# ── pure layer ───────────────────────────────────────────────────────────────


def calibration_bin(probability: float) -> int:
    """The index of the calibration bin ``probability`` falls in.

    Left-closed bins over :data:`CALIBRATION_BIN_EDGES`, the last one closed at 1.0. A value
    equal to an edge's double lies in the bin that edge opens (0.3 lies in [0.3, 0.4)).
    """
    if type(probability) is not float or not 0.0 <= probability <= 1.0:
        # NaN fails the chained comparison too. The value is not named.
        raise ReportError("a forecast probability is not a number in [0, 1]")
    return min(
        bisect.bisect_right(CALIBRATION_BIN_EDGES, probability) - 1,
        len(CALIBRATION_BIN_EDGES) - 2,
    )


def _finite(value: object, what: str) -> float:
    """``value`` as a finite float with ``-0.0`` read as ``0.0``, or a refusal.

    The normalization is what SQLite's REAL already does to a stored ``-0.0``; without it
    ``min``/``max`` over equal zeros would depend on row order.
    """
    if type(value) is not float or not math.isfinite(value):
        raise ReportError(f"{what} is not a finite number")
    return value + 0.0


def _mean(values: Sequence[float], total: float) -> float:
    """``total / n``, clamped into ``[min, max]``.

    ``math.fsum`` is correctly rounded, but the final division can still round a mean of equal
    values one ulp past them (three 0.1s give 0.10000000000000002); the clamp absorbs it.
    """
    return min(max(total / len(values), values[0]), values[-1])


def summarize_values(values: Sequence[float]) -> dict[str, Any]:
    """``n``, ``sum``, ``mean``, ``min``, ``max`` and ``sample_sd`` of finite values.

    Order-independent: the values are normalized and sorted, and every sum is ``math.fsum``.
    ``sample_sd`` uses ``n - 1`` and is null below two observations. A statistic that
    overflows is a refusal, never an infinity.
    """
    ordered = sorted(_finite(value, "a score value") for value in values)
    n = len(ordered)
    if n == 0:
        raise ReportError("a score cell has no values")
    try:
        total = math.fsum(ordered)
        mean = _mean(ordered, total)
        spread: float | None = None
        if n >= 2:
            # d * d rather than d ** 2: `**` raises OverflowError, `*` gives an infinity that
            # the finiteness check below refuses the same way.
            squares = math.fsum((value - mean) * (value - mean) for value in ordered)
            spread = math.sqrt(squares / (n - 1))
    except (OverflowError, ValueError):
        # fsum raises OverflowError on an intermediate overflow and ValueError on inf - inf.
        raise ReportError("a score summary is not representable as a finite number") from None
    statistics = (total, mean) if spread is None else (total, mean, spread)
    if not all(math.isfinite(statistic) for statistic in statistics):
        raise ReportError("a score summary is not representable as a finite number")
    return {
        "n": n,
        "sum": total,
        "mean": mean,
        "min": ordered[0],
        "max": ordered[-1],
        "sample_sd": spread,
        "small_sample": n < SMALL_SAMPLE_THRESHOLD,
    }


def provenance(metric: str) -> ScoreProvenance:
    """``local`` for M4-802's own arithmetic, ``platform`` for Metaculus's own number."""
    if metric in LOCAL_METRICS:
        return "local"
    if metric in PLATFORM_METRICS:
        return "platform"
    raise ReportError("a score row names a metric this report does not recognize")


def _baseline(metric: str) -> str | None:
    """What a score row of ``metric`` must carry as ``comparison_baseline``: none for local."""
    if provenance(metric) == "local":
        return None
    return COMPARISON_BASELINES[cast(PlatformMetric, metric)]


def _require_consistent(facts: RecordFacts) -> None:
    """Refuse facts that could not have come out of a valid ledger.

    The reader builds these from verified rows, so none of this fires on a real ledger; it is
    here so the pure layer states its own preconditions instead of trusting its caller.
    """
    if facts.lifecycle_status not in _LIFECYCLE_STATUSES:
        raise ReportError("a record's lifecycle status is not a recognized status")
    resolution = facts.resolution
    if resolution is not None and resolution.kind not in RESOLUTION_KINDS:
        raise ReportError("a record's latest observation has an unrecognized kind")
    seen: set[tuple[str, str]] = set()
    for score in facts.scores:
        if resolution is None or score.resolution_event_id != resolution.event_id:
            raise ReportError("a summarized score does not cite the record's latest observation")
        if resolution.kind != "resolved":
            raise ReportError("a summarized score cites an observation that is not resolved")
        if score.comparison_baseline != _baseline(score.metric):
            raise ReportError("a score row does not carry its metric's comparison baseline")
        _finite(score.value, "a score value")
        key = (score.metric, score.implementation_version)
        if key in seen:
            raise ReportError("a record carries two score rows for one metric and version")
        seen.add(key)
    if facts.question_type == "binary":
        if facts.probability_yes is None:
            raise ReportError("a binary record carries no probability")
        calibration_bin(facts.probability_yes)
        if (
            resolution is not None
            and resolution.kind == "resolved"
            and resolution.outcome not in _BINARY_OUTCOMES
        ):
            raise ReportError("a resolved binary record's outcome is neither yes nor no")
    elif facts.probability_yes is not None:
        raise ReportError("a non-binary record carries a binary probability")
    if facts.model_cost_usd is not None and _finite(facts.model_cost_usd, "a model cost") < 0:
        raise ReportError("a model cost is negative")
    if type(facts.stale_score_rows) is not int or facts.stale_score_rows < 0:
        raise ReportError("a stale score row count is not a non-negative integer")


def _state(facts: RecordFacts, subject: tuple[int, str] | None) -> RecordState:
    """The one state a record is in, by precedence (see :data:`RecordState`)."""
    if facts.lifecycle_status not in _POSTED:
        return "not_posted"
    if subject != (facts.forecast_version, facts.record_id):
        return "superseded"
    resolution = facts.resolution
    if resolution is None:
        return "awaiting_resolution"
    if resolution.kind == "resolved":
        return "scored" if facts.scores else "resolved_unscored"
    if resolution.kind == "withheld":
        return "withheld"
    if resolution.kind == "unresolved":
        return "unresolved"
    if resolution.kind == "annulled":
        return "annulled"
    return "ambiguous"


def classify(facts: Sequence[RecordFacts]) -> tuple[AttributionRow, ...]:
    """Give every record its exclusion and its state, in ``record_id`` order.

    The scored subject of a question is its latest posted version -- highest
    ``forecast_version``, then ``record_id`` (a UUIDv7, so time order) -- and any other posted
    version of the same question is ``superseded``.
    """
    identifiers = [item.record_id for item in facts]
    if len(set(identifiers)) != len(identifiers):
        raise ReportError("two records share one record_id")
    for item in facts:
        _require_consistent(item)
    subjects: dict[int, tuple[int, str]] = {}
    for item in facts:
        if item.lifecycle_status in _POSTED:
            candidate = (item.forecast_version, item.record_id)
            current = subjects.get(item.question_id)
            if current is None or candidate > current:
                subjects[item.question_id] = candidate
    return tuple(
        AttributionRow(
            facts=item,
            exclusion="test_tournament" if item.tournament_id in TEST_TOURNAMENTS else None,
            state=_state(item, subjects.get(item.question_id)),
        )
        for item in sorted(facts, key=lambda each: each.record_id)
    )


def _group_keys(axis: Axis, facts: RecordFacts) -> list[dict[str, Any]]:
    """The group(s) a record belongs to on ``axis``; exactly one unless the axis overlaps."""
    if axis == "all":
        return [{}]
    if axis == "tournament_id":
        return [{"tournament_id": facts.tournament_id}]
    if axis == "question_type":
        return [{"question_type": facts.question_type}]
    if axis == "model":
        return [{"model_provider": facts.model_provider, "model_name": facts.model_name}]
    if axis == "prompt":
        return [{"prompt_version": facts.prompt_version, "prompt_sha256": facts.prompt_sha256}]
    if axis == "question_domain":
        return [{"question_domain": facts.question_domain}]
    if axis == "source_category":
        ids = sorted({category_id for category_id, _ in facts.source_categories})
        return [{"category_id": category_id} for category_id in ids] or [{"category_id": None}]
    if axis == "reasoning_strategy_tag":
        tags = sorted(set(facts.reasoning_strategy_tags))
        return [{"tag": tag} for tag in tags] or [{"tag": None}]
    codes = sorted(set(facts.evidence_gaps))
    return [{"code": code} for code in codes] or [{"code": None}]


def _labels(axis: Axis, key: dict[str, Any], members: Sequence[AttributionRow]) -> list[str]:
    """Human labels for a group: the Metaculus slugs seen for a category id, else none.

    The category is keyed on its id (M1-201: a slug can be renamed and is optional), so the
    slugs are carried beside the key rather than in it.
    """
    if axis != "source_category" or key["category_id"] is None:
        return []
    return sorted(
        {
            slug
            for row in members
            for category_id, slug in row.facts.source_categories
            if category_id == key["category_id"] and slug is not None
        }
    )


def _state_counts(rows: Sequence[AttributionRow]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(STATES, 0)
    for row in rows:
        counts[row.state] += 1
    return counts


def _score_cells(rows: Sequence[AttributionRow]) -> list[dict[str, Any]]:
    """One cell per ``(metric, implementation_version)``, over ``scored`` rows only."""
    values: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        if row.state != "scored":
            continue
        for score in row.facts.scores:
            values.setdefault((score.metric, score.implementation_version), []).append(score.value)
    return [
        {
            "metric": metric,
            "implementation_version": version,
            "provenance": provenance(metric),
            "comparison_baseline": _baseline(metric),
            **summarize_values(values[(metric, version)]),
        }
        for metric, version in sorted(values)
    ]


def _calibration(rows: Sequence[AttributionRow]) -> dict[str, Any]:
    """Binary records whose latest observation is resolved, in the fixed bins."""
    members: list[list[tuple[float, int]]] = [[] for _ in CALIBRATION_BIN_EDGES[:-1]]
    for row in rows:
        facts = row.facts
        if (
            facts.question_type not in CALIBRATION_QUESTION_TYPES
            or row.state not in _RESOLVED_STATES
        ):
            continue
        resolution = facts.resolution
        if facts.probability_yes is None or resolution is None:
            raise ReportError("a resolved binary record lacks its probability or its observation")
        outcome = _BINARY_OUTCOMES.get(resolution.outcome or "")
        if outcome is None:
            raise ReportError("a resolved binary record's outcome is neither yes nor no")
        probability = facts.probability_yes + 0.0
        members[calibration_bin(probability)].append((probability, outcome))
    bins = []
    for index, contents in enumerate(members):
        n = len(contents)
        forecasts = sorted(probability for probability, _ in contents)
        bins.append(
            {
                "lower": CALIBRATION_BIN_EDGES[index],
                "upper": CALIBRATION_BIN_EDGES[index + 1],
                "n": n,
                "mean_forecast": _mean(forecasts, math.fsum(forecasts)) if n else None,
                "observed_frequency": sum(outcome for _, outcome in contents) / n if n else None,
                "small_sample": n < SMALL_SAMPLE_THRESHOLD,
            }
        )
    n = sum(len(contents) for contents in members)
    return {"n": n, "small_sample": n < SMALL_SAMPLE_THRESHOLD, "bins": bins}


def _model_cost(rows: Sequence[AttributionRow]) -> dict[str, Any]:
    known = [row.facts.model_cost_usd for row in rows if row.facts.model_cost_usd is not None]
    try:
        total = math.fsum(sorted(known)) if known else None
    except OverflowError:
        raise ReportError("a model cost sum is not representable as a finite number") from None
    if total is not None and not math.isfinite(total):
        raise ReportError("a model cost sum is not representable as a finite number")
    return {"known": len(known), "unknown": len(rows) - len(known), "known_sum_usd": total}


def _render_key(key: dict[str, Any]) -> str:
    return _canonical(key, "a group key")


def _axis(axis: Axis, rows: Sequence[AttributionRow]) -> dict[str, Any]:
    groups: dict[str, tuple[dict[str, Any], list[AttributionRow]]] = {}
    for row in rows:
        for key in _group_keys(axis, row.facts):
            groups.setdefault(_render_key(key), (key, []))[1].append(row)
    return {
        "axis": axis,
        "overlapping": axis in OVERLAPPING_AXES,
        "groups": [
            {
                "key": key,
                "labels": _labels(axis, key, members),
                "records": len(members),
                "states": _state_counts(members),
                "scores": _score_cells(members),
                "calibration": _calibration(members),
                "model_cost": _model_cost(members),
            }
            for _, (key, members) in sorted(groups.items())
        ],
    }


def _warnings(
    rows: Sequence[AttributionRow], axes: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    small = sum(
        1
        for axis in axes
        for group in axis["groups"]
        for cell in [*group["scores"], *group["calibration"]["bins"]]
        if 0 < cell["n"] < SMALL_SAMPLE_THRESHOLD
    )
    counts: dict[WarningCode, int] = {
        "small_sample": small,
        "overlapping_axes": len(OVERLAPPING_AXES),
        "unknown_model_cost": sum(1 for row in rows if row.facts.model_cost_usd is None),
        "resolved_unscored": sum(1 for row in rows if row.state == "resolved_unscored"),
        "stale_score_rows": sum(row.facts.stale_score_rows for row in rows),
    }
    return [
        {"code": code, "count": counts[code], "message": _WARNING_MESSAGES[code]}
        for code in WARNING_CODES
        if counts[code] > 0
    ]


def build_report(
    rows: Sequence[AttributionRow], *, ledger_schema_version: int, records_sha256: str
) -> dict[str, Any]:
    """The summary: population, one block per axis, and warnings. Every key is static.

    Every group is computed over **included** records only. No field anywhere sums across the
    groups of an axis -- on an overlapping axis that total would count records twice.
    """
    included = [row for row in rows if row.included]
    axes = [_axis(axis, included) for axis in AXES]
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "ledger_schema_version": ledger_schema_version,
        "records_sha256": records_sha256,
        "parameters": {
            "calibration_bin_edges": list(CALIBRATION_BIN_EDGES),
            "calibration_bin_rule": CALIBRATION_BIN_RULE,
            "calibration_question_types": list(CALIBRATION_QUESTION_TYPES),
            "small_sample_threshold": SMALL_SAMPLE_THRESHOLD,
            "excluded_tournaments": list(TEST_TOURNAMENTS),
            "states": list(STATES),
        },
        "population": {
            "records": len(rows),
            "excluded": {
                exclusion: sum(1 for row in rows if row.exclusion == exclusion)
                for exclusion in EXCLUSIONS
            },
            "included": len(included),
            "states": _state_counts(included),
        },
        "axes": axes,
        "warnings": _warnings(included, axes),
    }


def record_row(row: AttributionRow) -> dict[str, Any]:
    """One ``records.jsonl`` line: the record's facts, its inputs' digests, and its state."""
    facts = row.facts
    resolution = facts.resolution
    return {
        "record_id": facts.record_id,
        "question_id": facts.question_id,
        "post_id": facts.post_id,
        "tournament_id": facts.tournament_id,
        "forecast_version": facts.forecast_version,
        "parent_record_id": facts.parent_record_id,
        "forecast_sha256": facts.forecast_sha256,
        "generated_at_utc": facts.generated_at_utc,
        "question_type": facts.question_type,
        "question_domain": facts.question_domain,
        "source_categories": [
            {"id": category_id, "slug": slug} for category_id, slug in facts.source_categories
        ],
        "model_provider": facts.model_provider,
        "model_name": facts.model_name,
        "prompt_version": facts.prompt_version,
        "prompt_sha256": facts.prompt_sha256,
        "reasoning_strategy_tags": list(facts.reasoning_strategy_tags),
        "evidence_gaps": list(facts.evidence_gaps),
        "lifecycle_status": facts.lifecycle_status,
        "probability_yes": facts.probability_yes,
        "resolution": None
        if resolution is None
        else {
            "event_id": resolution.event_id,
            "kind": resolution.kind,
            "outcome": resolution.outcome,
            "observation_sha256": resolution.observation_sha256,
            "source_response_sha256": resolution.source_response_sha256,
            "observed_at_utc": resolution.observed_at_utc,
        },
        "scores": [
            {
                "metric": score.metric,
                "implementation_version": score.implementation_version,
                "provenance": provenance(score.metric),
                "comparison_baseline": score.comparison_baseline,
                "value": score.value,
                "event_id": score.event_id,
                "resolution_event_id": score.resolution_event_id,
            }
            for score in facts.scores
        ],
        "stale_score_rows": facts.stale_score_rows,
        "model_cost_usd": facts.model_cost_usd,
        "model_invocations": facts.model_invocations,
        "included": row.included,
        "exclusion": row.exclusion,
        "state": row.state,
    }


def render_records(rows: Sequence[AttributionRow]) -> bytes:
    """``records.jsonl``: one canonical JSON object per line, trailing newline on the last."""
    return "".join(f"{_canonical(record_row(row), 'a report record')}\n" for row in rows).encode(
        "utf-8"
    )


def _canonical(payload: object, what: str) -> str:
    try:
        return canonical_json(payload, what)
    except ExportError as exc:
        raise ReportError(str(exc)) from None


# ── ledger layer ─────────────────────────────────────────────────────────────


def _wrap(call: Callable[[], Any], what: str) -> Any:
    """Run a collaborator's verified read, re-raising its own sanitized error as ours."""
    try:
        return call()
    except (ForecastRecordError, LifecycleError) as exc:
        raise ReportError(f"{what}: {exc}") from None


def _record_ids(connection: sqlite3.Connection) -> list[str]:
    try:
        rows = connection.execute(
            "SELECT record_id FROM forecast_records ORDER BY record_id"
        ).fetchall()
    except sqlite3.Error:
        # from None: a database message can quote stored bytes.
        raise ReportError(
            "the ledger's forecast records cannot be listed (detail withheld: a database "
            "message can echo stored values)"
        ) from None
    identifiers: list[str] = []
    for row in rows:
        if type(row[0]) is not str:
            raise ReportError("a stored record_id is not text (detail withheld)")
        identifiers.append(row[0])
    return identifiers


def _evidence_gaps(connection: sqlite3.Connection, record_id: str, digest: str) -> tuple[str, ...]:
    """The record's evidence-gap codes, each marker checked against the record's hash.

    ``tournament.is_evidence_poor``'s rule: a marker bound to a different ``forecast_sha256``
    describes other content, and reading it as absent -- or as present -- would misattribute
    the evidence the forecast had. The journal is read through its owner's reader.
    """
    try:
        markers = events(connection, "evidence_gap", record_id)
    except (TournamentError, RecursionError):
        raise ReportError("the tournament journal cannot be read") from None
    codes: set[str] = set()
    for marker in markers:
        if type(marker) is not dict:
            raise ReportError("an evidence-gap marker is not an object")
        code = marker.get("code")
        # Type first: an unhashable code would raise TypeError from the membership test.
        if type(code) is not str or code not in _EVIDENCE_GAP_CODES:
            raise ReportError("an evidence-gap marker carries an unrecognized code")
        if marker.get("forecast_sha256") != digest:
            raise ReportError("an evidence-gap marker does not match the forecast hash")
        codes.add(code)
    return tuple(sorted(codes))


def read_facts(connection: sqlite3.Connection, record_id: str) -> RecordFacts:
    """Read one record's facts through the ledger's verified readers."""
    record = _wrap(lambda: read_forecast_record(connection, record_id), "a forecast record")
    call = _wrap(lambda: read_model_call(connection, record_id), "a forecast record")
    status = _wrap(lambda: current_status(connection, record_id), "a lifecycle status")
    latest = _wrap(lambda: latest_resolution(connection, record_id), "a resolution")
    local = _wrap(lambda: read_local_scores(connection, record_id), "a local score")
    platform = _wrap(lambda: read_platform_scores(connection, record_id), "a platform score")
    digest = record_sha256(record)

    probability: float | None = None
    if record.question_type == "binary":
        # Dispatch on the literal and re-check the concrete type (DiscreteQuestion subclasses
        # NumericQuestion; a stored record is untrusted).
        if type(record.forecast) is not BinaryForecastResponse:
            raise ReportError("a stored binary record does not carry a binary forecast")
        probability = float(record.forecast.final_prediction.probability_yes)

    resolution: ResolutionFact | None = None
    if latest is not None:
        resolution = ResolutionFact(
            event_id=latest.event_id,
            kind=latest.kind,
            outcome=latest.observation.outcome,
            observation_sha256=latest.observation_sha256,
            source_response_sha256=latest.source_response_sha256,
            observed_at_utc=latest.observed_at_utc,
        )
    rows = [
        ScoreFact(
            event_id=score.event_id,
            resolution_event_id=score.resolution_event_id,
            metric=score.metric,
            implementation_version=score.implementation_version,
            comparison_baseline=None,
            value=score.value,
        )
        for score in local
    ] + [
        ScoreFact(
            event_id=score.event_id,
            resolution_event_id=score.resolution_event_id,
            metric=score.metric,
            implementation_version=score.implementation_version,
            comparison_baseline=score.comparison_baseline,
            value=score.value,
        )
        for score in platform
    ]
    current = [
        score
        for score in rows
        if resolution is not None and score.resolution_event_id == resolution.event_id
    ]
    cost = call.cost_usd
    return RecordFacts(
        record_id=record.record_id,
        question_id=record.question_id,
        post_id=record.post_id,
        tournament_id=record.tournament_id,
        forecast_version=record.forecast_version,
        parent_record_id=record.parent_record_id,
        forecast_sha256=digest,
        # The `forecast_records.generated_at_utc` column's own form (`store._utc_text`).
        generated_at_utc=record.generated_at_utc.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f+00:00"
        ),
        question_type=record.question_type,
        question_domain=record.question_domain,
        source_categories=tuple(
            sorted(
                {(category.id, category.slug) for category in record.question.source_categories},
                key=_category_order,
            )
        ),
        model_provider=record.model_settings.provider,
        model_name=record.model_settings.name,
        prompt_version=record.model_settings.prompt_version,
        prompt_sha256=record.model_settings.prompt_sha256,
        reasoning_strategy_tags=tuple(sorted(record.forecast.reasoning_strategy_tags)),
        evidence_gaps=_evidence_gaps(connection, record_id, digest),
        lifecycle_status=status,
        probability_yes=probability,
        resolution=resolution,
        scores=tuple(
            sorted(current, key=lambda s: (s.metric, s.implementation_version, s.event_id))
        ),
        stale_score_rows=len(rows) - len(current),
        model_cost_usd=None if cost is None else float(cost),
        model_invocations=call.model_invocations,
    )


def _category_order(category: tuple[int, str | None]) -> tuple[int, str]:
    return (category[0], "" if category[1] is None else category[1])


def write_report(
    ledger_path: Path, destination: Path, *, now: datetime | None = None
) -> ReportResult:
    """Derive the attribution report from the ledger into ``destination``; never mutate it.

    Takes a path, not a connection, and opens it read-only itself -- the property
    ``export.export_ledger`` settled. Every read runs inside one deferred transaction, so the
    report is one snapshot. Every file is rendered before the first is written, and the
    manifest is written last: a directory without one is not a report. ``destination`` must
    not already hold these files; nothing is overwritten.
    """
    stamped = now if now is not None else datetime.now(timezone.utc)
    connection = connect_readonly(ledger_path)
    try:
        try:
            connection.execute("BEGIN")
        except sqlite3.Error:
            raise ReportError(f"cannot read ledger database at {ledger_path}") from None
        try:
            facts = [read_facts(connection, record_id) for record_id in _record_ids(connection)]
        finally:
            # Nothing to commit on a read-only connection; a failure to end the read must
            # not mask the report's own outcome.
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
    finally:
        connection.close()

    rows = classify(facts)
    records = render_records(rows)
    records_sha256 = hashlib.sha256(records).hexdigest()
    summary = build_report(
        rows, ledger_schema_version=LEDGER_SCHEMA_VERSION, records_sha256=records_sha256
    )
    report = f"{_canonical(summary, 'the report')}\n".encode()
    manifest = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "ledger_schema_version": LEDGER_SCHEMA_VERSION,
        "generated_at_utc": stamped.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "files": [
            {"name": RECORDS_FILENAME, "rows": len(rows), "sha256": records_sha256},
            {"name": REPORT_FILENAME, "rows": 1, "sha256": hashlib.sha256(report).hexdigest()},
        ],
    }
    rendered_manifest = f"{_canonical(manifest, 'the report manifest')}\n".encode()

    write_new_file(destination / RECORDS_FILENAME, records, what=_WHAT, error=ReportError)
    write_new_file(destination / REPORT_FILENAME, report, what=_WHAT, error=ReportError)
    write_new_file(
        destination / MANIFEST_FILENAME, rendered_manifest, what=_WHAT, error=ReportError
    )

    population = summary["population"]
    return ReportResult(
        destination=destination,
        ledger_schema_version=LEDGER_SCHEMA_VERSION,
        records=len(rows),
        excluded=len(rows) - population["included"],
        included=population["included"],
        states=tuple((state, population["states"][state]) for state in STATES),
        warnings=tuple((warning["code"], warning["count"]) for warning in summary["warnings"]),
    )
