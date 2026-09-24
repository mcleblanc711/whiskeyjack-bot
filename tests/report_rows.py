"""A ledger that reaches every state, axis and cell the attribution report has (M5-804).

Imported by bare name (``from report_rows import ...``), like ``score_rows``. Every record is a
**real** ``ForecastRecord`` -- the report reads each one back through ``read_forecast_record``,
which refuses a placeholder -- carried through the production writers: validated, approved,
posted, observed by ``record_resolution_observation`` from a committed post fixture, and
scored by ``record_local_scores``/``record_platform_scores``. Evidence-gap markers are written
through ``tournament_state.append`` bound to the record's own hash, the way ``pipeline_live``
writes them.

The layout is fixed so the report's numbers can be written down **by hand** in the tests,
independently of the code that computes them. :data:`EXPECTED_STATES` is the partition;
:data:`SPOT_PEER` holds the one platform score each scored record's latest observation carries.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from resolution_rows import SCORE_DATA, post_payload
from score_rows import walk_to_submitted

from whiskeyjack_bot.forecast.generate import ForecastGeneration, ModelSettings
from whiskeyjack_bot.forecast.inputs import SourceReference
from whiskeyjack_bot.forecast.record import ForecastRecord, build_forecast_record_draft
from whiskeyjack_bot.forecast.schema import response_model_for, validate_forecast_response
from whiskeyjack_bot.forecast.store import _projection
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import (
    record_local_scores,
    record_platform_scores,
    record_resolution_observation,
    record_validation,
)
from whiskeyjack_bot.questions.model import (
    CanonicalBinaryQuestion,
    CanonicalDiscreteQuestion,
    CanonicalMultipleChoiceQuestion,
    CanonicalNumericQuestion,
    SourceCategory,
)
from whiskeyjack_bot.tournament_state import append

_PROMPT = (Path(__file__).resolve().parents[1] / "prompts" / "forecaster.md").read_text(
    encoding="utf-8"
)
_GENERATED_AT = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
_CREATED = "2026-09-10T00:00:00.000000+00:00"
FIRST_OBSERVED = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)
FIRST_SCORED = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
SECOND_OBSERVED = datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc)
SECOND_SCORED = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)

MODEL = ("openrouter", "openrouter/test-model")
OTHER_MODEL = ("openrouter", "openrouter/other-model")
PROMPT = ("1.1.0", "b" * 64)
OTHER_PROMPT = ("1.2.0", "e" * 64)
MC_OPTIONS = (("Option Alpha", 0.2), ("Option Beta", 0.5), ("Other", 0.3))


@dataclass(frozen=True)
class Spec:
    """One record of the fixture ledger and everything that happens to it."""

    record_id: str
    question_id: int
    question_type: str = "binary"
    probability_yes: float = 0.5
    tournament_id: str = "33125"
    model: tuple[str, str] = MODEL
    prompt: tuple[str, str] = PROMPT
    tags: tuple[str, ...] = ("base_rate",)
    categories: tuple[tuple[int, str | None], ...] = ()
    domain: str | None = None
    cost_usd: float | None = None
    evidence_gaps: tuple[str, ...] = ()
    posted: bool = True
    forecast_version: int = 1
    parent_record_id: str | None = None
    # Each observation: (resolution value or kind, spot_peer_score for the payload, score it?).
    observations: tuple[tuple[str | None, float | None, bool], ...] = ()


def _json_block(heading: str) -> str:
    body = _PROMPT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def _response(spec: Spec) -> Any:
    qtype = spec.question_type
    heading = {
        "binary": "Binary schema",
        "multiple_choice": "Multiple-choice schema",
        "numeric": "Numeric schema",
        "discrete": "Numeric schema",
    }[qtype]
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block(heading) + "}"),
    }
    payload["question_id"] = spec.question_id
    payload["question_type"] = qtype
    payload["reasoning_strategy_tags"] = list(spec.tags)
    if qtype != "binary":
        payload["model_prior"] = None
        payload["base_rate"] = {**payload["base_rate"], "prior_probability": None}
    if qtype == "binary":
        payload["final_prediction"] = {"probability_yes": spec.probability_yes}
    elif qtype == "multiple_choice":
        payload["final_prediction"] = {
            "options": [{"option": label, "probability": p} for label, p in MC_OPTIONS]
        }
    return validate_forecast_response(payload, response_model_for(qtype))


def _question(spec: Spec) -> Any:
    common: dict[str, Any] = {
        "question_id": spec.question_id,
        "post_id": spec.question_id + 1000,
        "title": f"Question {spec.question_id}?",
        "source_categories": [
            SourceCategory(id=category_id, name=f"Category {category_id}", slug=slug)
            for category_id, slug in spec.categories
        ],
    }
    if spec.question_type == "binary":
        return CanonicalBinaryQuestion(**common)
    if spec.question_type == "multiple_choice":
        return CanonicalMultipleChoiceQuestion(**common, options=[label for label, _ in MC_OPTIONS])
    bounded: dict[str, Any] = {
        **common,
        "lower_bound": 0.0,
        "upper_bound": 100.0,
        "open_lower_bound": True,
        "open_upper_bound": True,
    }
    if spec.question_type == "numeric":
        return CanonicalNumericQuestion(**bounded, cdf_size=201)
    return CanonicalDiscreteQuestion(**bounded, cdf_size=16)


def _generation(spec: Spec, forecast: Any) -> ForecastGeneration:
    return ForecastGeneration(
        forecast=forecast,
        settings=ModelSettings(
            provider=spec.model[0],
            name=spec.model[1],
            temperature=0.1,
            max_output_tokens=2048,
            timeout_seconds=60.0,
            allowed_tries=2,
            prompt_version=spec.prompt[0],
            prompt_sha256=spec.prompt[1],
        ),
        sources=tuple(
            SourceReference(
                source_id=source_id,
                document_id=None,
                canonical_url=f"https://example.test/{source_id}",
                content_sha256="c" * 64,
            )
            for source_id in ("src-001", "src-002")
        ),
        request="the rendered reasoning packet",
        raw_responses=("{}",),
        invocations=1,
        repair_attempted=False,
        cost_usd=None,
        failure_code=None,
        failure_problems=(),
    )


def seed_record(conn: sqlite3.Connection, spec: Spec) -> str:
    """Store ``spec``'s real draft record and return its ``forecast_sha256``."""
    conn.execute(
        "INSERT OR IGNORE INTO research_runs (retrieval_run_id, provider, question_id, "
        "started_at_utc, created_at_utc) VALUES ('run-report', 'asknews', ?, ?, ?)",
        (spec.question_id, _CREATED, _CREATED),
    )
    draft = build_forecast_record_draft(
        question=_question(spec),
        generation=_generation(spec, _response(spec)),
        tournament_id=spec.tournament_id,
        attempt_id=f"att-{spec.record_id}",
        retrieval_run_id="run-report",
        research_packet_sha256="d" * 64,
        generated_at=_GENERATED_AT,
        question_domain=spec.domain,
    )
    record = ForecastRecord(
        **draft.model_dump(),
        record_id=spec.record_id,
        forecast_version=spec.forecast_version,
        parent_record_id=spec.parent_record_id,
    )
    projected = _projection(record)
    columns = (*projected, "status", "created_at_utc", "cost_usd", "model_invocations")
    values = (
        *projected.values(),
        "draft",
        _CREATED,
        spec.cost_usd,
        None if spec.cost_usd is None else 1,
    )
    conn.execute(
        f"INSERT INTO forecast_records ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        values,
    )
    return str(projected["forecast_sha256"])


def _observe(
    conn: sqlite3.Connection,
    spec: Spec,
    value: str | None,
    spot_peer: float | None,
    observed_at: datetime,
) -> None:
    """Record one observation: a resolution value, or one of the non-scorable kinds."""
    post_id = spec.question_id + 1000
    kwargs: dict[str, Any] = {}
    if value in ("annulled", "ambiguous"):
        kwargs["resolution"] = value
    elif value == "withheld":
        kwargs["resolution"] = None
    elif value == "unresolved":
        kwargs.update(
            status="closed", resolution=None, actual_resolve_time=None, resolution_set_time=None
        )
    else:
        kwargs["resolution"] = value
    if spot_peer is not None:
        kwargs["score_data"] = {**SCORE_DATA, "spot_peer_score": spot_peer}
    payload = post_payload(
        spec.question_type, post_id=post_id, question_id=spec.question_id, **kwargs
    )
    record_resolution_observation(
        conn, record_id=spec.record_id, source_response=payload, observed_at=observed_at
    )


def seed(conn: sqlite3.Connection, spec: Spec) -> None:
    """Write ``spec``'s record and carry it through everything the spec says happens to it."""
    digest = seed_record(conn, spec)
    for code in spec.evidence_gaps:
        append(
            conn,
            "evidence_gap",
            spec.record_id,
            {
                "at": _CREATED,
                "code": code,
                "question_id": spec.question_id,
                "tournament_id": spec.tournament_id,
                "forecast_sha256": digest,
            },
        )
    if not spec.posted:
        record_validation(conn, record_id=spec.record_id, occurred_at=_GENERATED_AT)
        return
    walk_to_submitted(conn, spec.record_id, digest)
    times = ((FIRST_OBSERVED, FIRST_SCORED), (SECOND_OBSERVED, SECOND_SCORED))
    for (value, spot_peer, score), (observed_at, scored_at) in zip(
        spec.observations, times, strict=False
    ):
        _observe(conn, spec, value, spot_peer, observed_at)
        if score:
            if spec.question_type in ("binary", "multiple_choice"):
                record_local_scores(conn, record_id=spec.record_id, computed_at=scored_at)
            record_platform_scores(conn, record_id=spec.record_id, computed_at=scored_at)


# The fixture ledger. Numbers chosen so every expectation in the tests is hand arithmetic.
SPECS: tuple[Spec, ...] = (
    # Scored binary, on the 0.3 edge; both evidence-gap codes, a domain, one category.
    Spec(
        "rec-01",
        101,
        probability_yes=0.3,
        tags=("base_rate", "trend"),
        categories=((10, "economy"),),
        domain="econ_data",
        cost_usd=0.25,
        evidence_gaps=("named_source_absent", "evidence_poor"),
        observations=(("yes", 10.0, True),),
    ),
    # Scored binary on another model, in two categories.
    Spec(
        "rec-02",
        102,
        probability_yes=0.75,
        model=OTHER_MODEL,
        categories=((10, "economy"), (20, "geopolitics")),
        evidence_gaps=("named_source_absent",),
        observations=(("no", -20.0, True),),
    ),
    # Resolved, never scored; no reasoning tag at all (the axis's null group).
    Spec("rec-03", 103, probability_yes=0.05, tags=(), observations=(("yes", 1.5, False),)),
    # Scored multiple choice: local multiclass and platform rows.
    Spec(
        "rec-04",
        104,
        question_type="multiple_choice",
        categories=((20, "geopolitics"),),
        cost_usd=0.5,
        observations=(("__scorable__", 5.0, True),),
    ),
    # Scored numeric on another prompt; category 10 under a renamed slug.
    Spec(
        "rec-05",
        105,
        question_type="numeric",
        prompt=OTHER_PROMPT,
        categories=((10, "economy-renamed"),),
        observations=(("__scorable__", 1.0, True),),
    ),
    # Scored discrete.
    Spec(
        "rec-06",
        106,
        question_type="discrete",
        observations=(("__scorable__", 2.0, True),),
    ),
    Spec("rec-07", 107, question_type="numeric", observations=(("annulled", None, False),)),
    Spec("rec-08", 108, observations=(("ambiguous", None, False),)),
    Spec("rec-09", 109, observations=(("withheld", None, False),)),
    # Scored, then retracted: six stale score rows and an `unresolved` latest observation.
    Spec(
        "rec-10",
        110,
        probability_yes=0.4,
        observations=(("yes", 50.0, True), ("unresolved", None, False)),
    ),
    # Posted, never observed; evidence-poor.
    Spec("rec-11", 111, evidence_gaps=("evidence_poor",)),
    # Validated, never posted.
    Spec("rec-12", 112, posted=False),
    # Two posted versions of one question: v1 is superseded even though it was scored.
    Spec(
        "rec-13",
        113,
        probability_yes=0.2,
        observations=(("yes", 70.0, True),),
    ),
    Spec(
        "rec-14",
        113,
        probability_yes=0.6,
        model=OTHER_MODEL,
        forecast_version=2,
        parent_record_id="rec-13",
        observations=(("yes", 3.0, True),),
    ),
    # A test tournament: excluded from every summary, still in records.jsonl.
    Spec(
        "rec-15",
        115,
        probability_yes=0.95,
        tournament_id="bot-testing-area",
        observations=(("yes", 90.0, True),),
    ),
    # Re-resolved after scoring: six stale rows, six current ones against the second outcome.
    Spec(
        "rec-16",
        116,
        probability_yes=0.9,
        observations=(("yes", 100.0, True), ("no", 4.0, True)),
    ),
)

EXPECTED_STATES: dict[str, str] = {
    "rec-01": "scored",
    "rec-02": "scored",
    "rec-03": "resolved_unscored",
    "rec-04": "scored",
    "rec-05": "scored",
    "rec-06": "scored",
    "rec-07": "annulled",
    "rec-08": "ambiguous",
    "rec-09": "withheld",
    "rec-10": "unresolved",
    "rec-11": "awaiting_resolution",
    "rec-12": "not_posted",
    "rec-13": "superseded",
    "rec-14": "scored",
    "rec-15": "scored",
    "rec-16": "scored",
}

# The platform spot-peer score each included, scored record's LATEST observation carries.
SPOT_PEER: dict[str, float] = {
    "rec-01": 10.0,
    "rec-02": -20.0,
    "rec-04": 5.0,
    "rec-05": 1.0,
    "rec-06": 2.0,
    "rec-14": 3.0,
    "rec-16": 4.0,
}


def build_ledger(path: Path, specs: tuple[Spec, ...] = SPECS) -> Path:
    """Initialize a ledger at ``path`` and write ``specs`` into it."""
    initialize_ledger(path)
    conn = connect(path)
    try:
        for spec in specs:
            seed(conn, spec)
    finally:
        conn.close()
    return path


__all__ = [
    "EXPECTED_STATES",
    "SPECS",
    "SPOT_PEER",
    "Spec",
    "build_ledger",
    "seed",
    "seed_record",
]
