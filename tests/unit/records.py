"""A real `ForecastRecord` under a caller-chosen identifier, for ledger-level tests.

Not a test module -- pytest collects nothing here. It exists because M2-707's round-1 fix
made :func:`approval.approve` derive the payload it authorizes from the record itself, so
an approval test can no longer seed the `'{}'` placeholder row that four of these files
used to write. That placeholder was always a small lie (`read_forecast_record` refuses it,
and `forecast.store` has been able to write a real row since M1-602); it stopped being a
harmless one the moment approving required reading.

`append_forecast_version` is the real writer and is used wherever a test does not care what
the identifier is. :func:`seed_record` exists for the ones that do: they address the record
by a fixed `record_id` on a command line or in a SQL literal, and the writer mints a UUID.
The columns come from `store._projection`, imported rather than transcribed, so a column
added to `forecast_records` cannot leave this helper writing a row `read_forecast_record`
then refuses -- which is the failure it exists to avoid.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from whiskeyjack_bot.config import NumericCalibrationConfig, validate_config_data
from whiskeyjack_bot.forecast.generate import ForecastGeneration, ModelSettings
from whiskeyjack_bot.forecast.inputs import SourceReference
from whiskeyjack_bot.forecast.record import ForecastRecord, build_forecast_record_draft
from whiskeyjack_bot.forecast.store import _utc_text
from whiskeyjack_bot.forecast.schema import (
    ForecastResponse,
    response_model_for,
    validate_forecast_response,
)
from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion, CanonicalQuestion

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")

GENERATED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
# The text the writer stores for it, rendered by the writer's own formatter rather than
# transcribed: a test that asserts on the stored column is asserting on this.
GENERATED_AT_TEXT = _utc_text(GENERATED_AT)


def calibration(**overrides: Any) -> NumericCalibrationConfig:
    """The committed `config.example.yaml`'s calibration, not a hand-built one.

    `approve` takes this now, and a test that invented its own would stop noticing the
    committed defaults drifting away from what the pipeline is actually configured with.
    """
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    committed = validate_config_data(data).numeric_calibration
    return committed.model_copy(update=overrides) if overrides else committed


CALIBRATION = calibration()


def _json_block(heading: str) -> str:
    body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
    match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def binary_response(question_id: int, **overrides: Any) -> ForecastResponse:
    """The prompt's own binary example, validated by the real validator."""
    payload: dict[str, Any] = {
        **json.loads(_json_block("Shared fields")),
        **json.loads("{" + _json_block("Binary schema") + "}"),
    }
    payload["question_id"] = question_id
    payload.update(overrides)
    return validate_forecast_response(payload, response_model_for(payload["question_type"]))


def _generation(forecast: ForecastResponse) -> ForecastGeneration:
    return ForecastGeneration(
        forecast=forecast,
        settings=ModelSettings(
            provider="openrouter",
            name="openrouter/test-model",
            temperature=0.1,
            max_output_tokens=2048,
            timeout_seconds=60.0,
            allowed_tries=2,
            prompt_version="1.1.0",
            prompt_sha256="b" * 64,
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


def build_record(
    *,
    record_id: str,
    question_id: int = 100,
    tournament_id: str = "minibench",
    forecast_version: int = 1,
    parent_record_id: str | None = None,
    retrieval_run_id: str = "run-1",
    question: CanonicalQuestion | None = None,
    forecast: ForecastResponse | None = None,
) -> ForecastRecord:
    """A validated `ForecastRecord` with the identity the caller asked for."""
    resolved = (
        question
        if question is not None
        else CanonicalBinaryQuestion(
            question_id=question_id, post_id=question_id + 1, title="Will the thing happen?"
        )
    )
    draft = build_forecast_record_draft(
        question=resolved,
        generation=_generation(forecast if forecast is not None else binary_response(question_id)),
        tournament_id=tournament_id,
        attempt_id=f"att-{record_id}",
        retrieval_run_id=retrieval_run_id,
        research_packet_sha256="d" * 64,
        generated_at=GENERATED_AT,
    )
    return ForecastRecord(
        **draft.model_dump(),
        record_id=record_id,
        forecast_version=forecast_version,
        parent_record_id=parent_record_id,
    )


def seed_record(conn: Any, *, status: str = "draft", created_at_utc: str, **kwargs: Any) -> str:
    """Insert a real record at `status` and return its stored `forecast_sha256`.

    The hash is returned rather than assumed because it is derived from the record's own
    content: two records differing only in `record_id` hash differently, so a test that
    needs to name the hash has to be told it.
    """
    from whiskeyjack_bot.forecast.store import _projection

    record = build_record(**kwargs)
    projected = _projection(record)
    columns = ", ".join((*projected, "status", "created_at_utc"))
    placeholders = ", ".join("?" for _ in range(len(projected) + 2))
    conn.execute(
        f"INSERT INTO forecast_records ({columns}) VALUES ({placeholders})",
        (*projected.values(), status, created_at_utc),
    )
    return str(projected["forecast_sha256"])
