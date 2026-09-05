"""T-902: "model timeout and one repair attempt", asserted where it lands in the ledger.

``CODEX_HANDOFF.md:324`` asks for both, and both are already covered *in memory*:
``tests/unit/test_forecast_generate.py`` drives ``_Model(raises=...)`` and asserts the
returned ``ForecastGeneration``'s ``failure_code`` and ``invocations`` across a dozen repair
cases. That suite holds no ledger, so what it structurally cannot assert is the **persisted**
consequence -- ``CODEX_HANDOFF.md:295``: *"a malformed model response ... must produce a
ledger event and leave the last valid record intact."*

``tests/unit/test_pipeline_live.py`` covers most of that gap already and this module is
deliberately small because of it: a malformed reply reaching ``generation_failed`` is
``test_an_unusable_reply_records_generation_failed_and_keeps_the_text_it_paid_for``, and a
non-terminating CDF conversion is ``test_a_conversion_that_never_returns_is_recorded_as_a_
timeout_and_the_batch_continues``. Only two claims in this bullet have no owner:

1. a **model-call** timeout, as distinct from the conversion timeout that suite covers,
   reaching ``pipeline_failure_events`` as ``timeout``;
2. ``forecast_records.model_invocations`` reading back **exactly 2** after a real repair --
   the only thing that closes the loop between ``generate.py``'s in-memory count and the
   column the attribution record is read from. Nothing asserts that today; the ``== 2``
   in ``tests/unit/test_forecast_replay.py`` is a constructed ``ModelCall``, not a repair
   that happened.

Anything else in this bullet would be a second copy, and is not written here.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from typing import Any


from tests.unit.test_pipeline_live import BINARY, MODEL_NAME, SNAPSHOT, questions, reply_for
from whiskeyjack_bot.config import AppConfig
from whiskeyjack_bot.pipeline_live import run_live

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


class _ScriptedForecaster:
    """Answers a **sequence** per question, so a repair turn can be given a different reply.

    ``tests/unit/test_pipeline_live.py``'s ``_Forecaster`` maps one reply per question and
    cannot express "bad, then good" -- which is the only shape that drives a real repair
    through the pipeline. Everything else about it is that double's, on purpose: it reads
    the **first** user message, because the repair turn appends the model's own reply plus a
    plain-text instruction and reading the last one parses that instruction as JSON. Getting
    it wrong reports ``provider_error`` and looks like a product defect rather than a broken
    double -- which is exactly what happened here on the first draft.
    """

    def __init__(self, scripts: dict[int, list[str]], *, raises: BaseException | None = None):
        self.model = MODEL_NAME
        self._scripts = scripts
        self._raises = raises
        self.calls: list[int] = []

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        request = json.loads(next(m for m in prompt if m["role"] == "user")["content"])
        question_id = int(request["question_id"])
        self.calls.append(question_id)
        if self._raises is not None:
            raise self._raises
        script = self._scripts.get(question_id)
        if script is None:
            return reply_for(questions()[question_id])
        return script[min(self.calls.count(question_id), len(script)) - 1]


def _rows(conn: sqlite3.Connection, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in conn.execute(sql).fetchall()]


def _run(conn: sqlite3.Connection, config: AppConfig, forecaster: Any, **overrides: Any) -> Any:
    from tests.unit.test_pipeline_live import _TrackingSDK

    call: dict[str, Any] = {
        "snapshot": SNAPSHOT,
        "now": NOW,
        "news_client": _TrackingSDK(),
        "web_client": object(),
        "forecaster": forecaster,
        "question_id": BINARY,
    }
    call.update(overrides)
    return run_live(conn, config, **call)


def test_a_model_timeout_is_recorded_as_a_timeout_and_writes_no_record(
    ledger: sqlite3.Connection, config: AppConfig
) -> None:
    """The handoff's "model timeout", persisted.

    Distinct from the conversion timeout ``test_pipeline_live.py`` covers: that one is the
    numeric CDF search failing to terminate *after* a reply exists, this one is the provider
    call itself. Both map to ``detail_code='timeout'``, which is what makes it worth checking
    that the model-call path really reaches it rather than falling into ``provider_error``.

    "Leaves the last valid record intact" is asserted as **no** ``forecast_records`` row: a
    failure that also wrote a record would be the worse defect, and a test that only checked
    the failure event could not see it.
    """
    outcome = _run(
        ledger, config, _ScriptedForecaster({}, raises=TimeoutError("the model timed out"))
    )

    assert _rows(ledger, "SELECT event_type, detail_code FROM pipeline_failure_events") == [
        ("generation_failed", "timeout")
    ]
    assert _rows(ledger, "SELECT record_id FROM forecast_records") == []
    assert outcome.records_written == 0


def test_a_repaired_reply_stores_exactly_two_model_invocations(
    ledger: sqlite3.Connection, config: AppConfig
) -> None:
    """The bounded repair, counted where the attribution record is actually read from.

    ``== 2``, never ``>= 2``. The defect this is aimed at is retry layers multiplying -- the
    finding that cost ``M1-402`` four billable calls for one forecast -- and a lower bound
    cannot see it. The count is read back out of ``forecast_records.model_invocations``
    rather than off the returned object, because the column is what an audit reads and
    nothing else asserts a repair reaches it.
    """
    forecaster = _ScriptedForecaster({BINARY: ["this is not json", reply_for(questions()[BINARY])]})

    outcome = _run(ledger, config, forecaster)

    assert outcome.records_written == 1
    assert forecaster.calls == [BINARY, BINARY], "one call plus exactly one repair"
    assert _rows(ledger, "SELECT model_invocations FROM forecast_records") == [(2,)]
    assert _rows(ledger, "SELECT event_type FROM pipeline_failure_events") == []
