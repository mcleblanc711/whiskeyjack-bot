"""M1-324: the two generation-refusal log lines fire, and stay value-free.

M1-323 added a sanitized ``_LOGGER.error`` at both generation-failure sites in
``pipeline_live._attempt_question``, because ``QuestionOutcome.problems`` reached neither the
ledger nor the log: eight live failures recorded only ``internal_error``. Those two lines are
the only durable account of *why* a generation was refused. This file is the test that can
fail on them: each site is driven, its record is asserted present, and every record the run
emitted -- rendered through a real formatter, traceback included -- is searched for a
sentinel planted where a value would come from.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from test_pipeline_live import (  # type: ignore[import-not-found]
    BINARY,
    _Forecaster,
    config,
    ledger,
    live,
    questions,
    reply_for,
)
from whiskeyjack_bot import pipeline_live
from whiskeyjack_bot.forecast.generate import ForecastGenerationError

__all__ = ["config", "ledger"]

SENTINEL = "SENTINEL-c41d-do-not-log"
LOGGER = "whiskeyjack_bot.pipeline_live"
_FORMAT = logging.Formatter("%(levelname)s %(name)s %(message)s")


def _rendered(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every record as a handler would write it, with any traceback appended."""
    out = []
    for record in caplog.records:
        text = _FORMAT.format(record)
        if record.exc_info:
            text += _FORMAT.formatException(record.exc_info)
        out.append(text)
    return out


def _site(caplog: pytest.LogCaptureFixture, prefix: str) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == LOGGER
        and record.levelno == logging.ERROR
        and record.getMessage().startswith(prefix)
    ]


def _assert_no_sentinel(caplog: pytest.LogCaptureFixture) -> None:
    assert caplog.records, "the run must have logged something for this to mean anything"
    for text in _rendered(caplog):
        assert SENTINEL not in text


def _malformed_reply() -> str:
    """A reply whose field VALUE carries the sentinel and fails the schema."""
    payload = json.loads(reply_for(questions()[BINARY]))
    payload["final_prediction"] = {"probability_yes": SENTINEL}
    payload["rationale_summary"] = SENTINEL * 400  # also over its length bound
    return json.dumps(payload)


# --- the no-forecast branch -------------------------------------------------------------


def test_a_malformed_reply_logs_its_sanitized_problems_and_never_the_value(
    config: Any, ledger: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    forecaster = _Forecaster(replies={BINARY: _malformed_reply()})
    batch = live(ledger, config, question_id=BINARY, forecaster=forecaster)
    assert [o.status for o in batch.outcomes] == ["generation_failed"]

    (record,) = _site(caplog, "generation produced no forecast")
    message = record.getMessage()
    assert f"question {BINARY}" in message
    assert "code=schema_invalid" in message and "invocations=2" in message
    assert "final_prediction" in message, "the sanitized reason is the schema's own problem"
    assert record.exc_info is None
    _assert_no_sentinel(caplog)


def test_a_provider_exception_logs_the_code_and_never_its_text(
    config: Any, ledger: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    forecaster = _Forecaster(raises=RuntimeError(f"upstream quoted {SENTINEL}"))
    batch = live(ledger, config, question_id=BINARY, forecaster=forecaster)
    assert [o.detail_code for o in batch.outcomes] == ["provider_error"]

    (record,) = _site(caplog, "generation produced no forecast")
    message = record.getMessage()
    assert "code=provider_error" in message and "invocations=1" in message
    assert "the provider call did not complete (detail withheld)" in message
    _assert_no_sentinel(caplog)


# --- the caught-exception handler -------------------------------------------------------


def test_a_refused_generation_logs_its_type_and_message_and_never_its_cause(
    config: Any, ledger: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module-owned refusal whose *cause* is a provider error quoting a value.

    Simulated at the seam: every member of the handler's tuple is raised before the spend
    by contract, and this stands in for one that wraps what it caught -- the reachable shape
    a traceback renderer would expose if the handler ever logged with ``exc_info``.
    """
    caplog.set_level(logging.DEBUG)

    def refuse(**kwargs: Any) -> Any:
        try:
            raise RuntimeError(f"provider said {SENTINEL}")
        except RuntimeError as cause:
            raise ForecastGenerationError("the configured client cannot be used") from cause

    monkeypatch.setattr(pipeline_live, "generate_forecast", refuse)
    batch = live(ledger, config, question_id=BINARY)
    assert [(o.status, o.detail_code) for o in batch.outcomes] == [
        ("generation_failed", "internal_error")
    ]

    (record,) = _site(caplog, "generation refused")
    assert record.getMessage() == (
        f"generation refused for question {BINARY}: "
        "ForecastGenerationError: the configured client cannot be used"
    )
    assert record.exc_info is None
    _assert_no_sentinel(caplog)
