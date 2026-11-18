"""A live-shaped ledger for the replay harness's tests (M5-806).

Built by the real worker pass -- ``tournament.run_once`` with the production ``PricedClient``
behind an ``httpx.MockTransport`` -- so everything the fidelity gate reads is what the worker
really writes: the ``research_checkpoint`` event, the stored research runs, the raw-output
artifact and the ``model_started`` journal scope of the priced call. A fake ``Forecaster`` would
bypass the priced client and leave no ``model_started`` row (M1-351), which would make the gate's
full-body check untestable. The record is then resolved through the production writer, so it is
in the arm population.

Imported by bare name (``from experiment_case import ...``), like ``score_rows``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from forecasting_tools.data_models.data_organizer import DataOrganizer

from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.forecast.priced import PricedClient
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.questions.model import CanonicalQuestion
from whiskeyjack_bot.questions.normalize import normalize_questions
from whiskeyjack_bot.tournament import run_once
from whiskeyjack_bot.tournament_state import enable, utcnow
from score_rows import resolve
from tests.unit.test_pipeline_live import config as base_config, reply_for
from tests.unit.test_tournament import News, Platform

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT, PROJECT = 42, 32977
SOL = "openrouter/openai/gpt-5.6-sol"
ASTRA = "openrouter/openai/gpt-6-astra"
LIVE_COST = 0.0123


class OpenRouter:
    """A fake OpenRouter endpoint: a valid reply for the question asked, at a fixed cost.

    Counts every POST it receives, which is the measure of a purchase: a count taken on a fake
    ``Forecaster`` would bypass the priced client and prove nothing (M1-351).
    """

    def __init__(
        self,
        reply: str,
        *,
        cost: float = LIVE_COST,
        model: str = "openai/gpt-6-astra-20260910",
        first_reply: str | None = None,
    ) -> None:
        self.reply = reply
        self.cost = cost
        self.model = model
        self.first_reply = first_reply
        self.bodies: list[dict[str, Any]] = []

    @property
    def posts(self) -> int:
        return len(self.bodies)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        user = json.loads(body["messages"][1]["content"])
        if self.first_reply is not None and len(self.bodies) == 1:
            content = self.first_reply
        elif not self.reply.startswith("{"):
            content = self.reply  # a malformed reply, sent as written
        else:
            reply = json.loads(self.reply)
            reply["as_of_utc"] = user["as_of_utc"]
            if not user["research_documents"]:
                reply["base_rate"]["source_ids"] = []
                for name in ("evidence_adjustments", "load_bearing_facts"):
                    for entry in reply.get(name) or []:
                        entry["source_ids"] = []
            content = json.dumps(reply)
        return httpx.Response(
            200,
            json={
                "id": f"gen-{len(self.bodies)}",
                "model": self.model,
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {"cost": self.cost},
            },
        )


# The real class, captured once: a second ``install`` in one test must wrap this, not the
# previous install's lambda -- wrapping that passes ``transport`` twice, the call fails inside
# the priced client, and it is recorded as an unknown outcome instead of reaching the endpoint.
_REAL_ASYNC_CLIENT: Final = httpx.AsyncClient


def install(monkeypatch: pytest.MonkeyPatch, endpoint: OpenRouter, key_env: str) -> None:
    """Route every priced call to ``endpoint`` and give the configured key a test value."""
    monkeypatch.setenv(key_env, "test-secret")
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(endpoint), **kw),
    )


@dataclass(frozen=True)
class LiveCase:
    """One built live ledger: its config, its one control record and the question it forecast."""

    root: Path
    config: AppConfig
    record_id: str
    question: CanonicalQuestion
    prompt: Path

    @property
    def ledger(self) -> Path:
        return self.config.storage.sqlite_path

    def copy_to(self, destination: Path) -> LiveCase:
        """The same ledger and artifacts under ``destination``, with the config re-pointed."""
        shutil.copytree(self.root, destination)
        storage = self.config.storage.model_copy(
            update={
                "sqlite_path": destination / self.ledger.relative_to(self.root),
                "artifact_root": destination
                / self.config.storage.artifact_root.relative_to(self.root),
                "export_root": destination / "exports",
            }
        )
        prompt = destination / self.prompt.relative_to(self.root)
        forecast = self.config.forecast.model_copy(update={"prompt_path": prompt})
        config = self.config.model_copy(update={"storage": storage, "forecast": forecast})
        return LiveCase(destination, config, self.record_id, self.question, prompt)


def _question(raw: dict[str, Any]) -> CanonicalQuestion:
    return normalize_questions([DataOrganizer.get_question_from_post_json(raw)]).questions[0]


def build_live(root: Path, *, edit_raw: Callable[[dict[str, Any]], None] | None = None) -> LiveCase:
    """Run one real worker pass into ``root`` and resolve the record it posted."""
    root.mkdir(parents=True, exist_ok=True)
    config = base_config.__wrapped__(root)
    data = config.model_dump(mode="json")
    prompt = root / "prompts" / "forecaster.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_bytes(config.forecast.prompt_path.read_bytes())
    data["forecast"]["prompt_path"] = str(prompt)
    data["metaculus"]["tournament"].update(id=PROJECT, use_sdk_current_id=False)
    data["model"].update(name=SOL, max_output_tokens=6000, timeout_seconds=120, temperature=None)
    data["submission"].update(
        enabled=True, dry_run=False, no_submit=False, post_private_reasoning_comment=True
    )
    data["retrieval"]["primary"]["retries"] = 0
    data["retrieval"]["fallback"]["retries"] = 0
    config = validate_config_data(data)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    try:
        raw = json.loads((ROOT / "tests/fixtures/api_posts/binary_post.json").read_text())
        raw["projects"]["default_project"]["id"] = PROJECT
        raw["question"]["scheduled_close_time"] = (utcnow() + timedelta(hours=2)).isoformat()
        raw["question"]["open_time"] = (utcnow() - timedelta(days=1)).isoformat()
        if edit_raw is not None:
            edit_raw(raw)
        platform, news = Platform(raw), News(raw)
        question = _question(raw)
        enable(
            conn,
            config,
            account_id=ACCOUNT,
            project_id=PROJECT,
            starts=utcnow() - timedelta(minutes=1),
            ends=utcnow() + timedelta(days=1),
        )
        endpoint = OpenRouter(reply_for(question), cost=LIVE_COST)
        with pytest.MonkeyPatch.context() as patch:
            install(patch, endpoint, config.model.api_key_env)
            result = run_once(
                conn,
                config,
                client=platform,
                poster=platform,
                news_client=news,
                forecaster=PricedClient(config),
            )
        assert result["forecast_confirmed"] == 1, result
        (record_id,) = [r[0] for r in conn.execute("SELECT record_id FROM forecast_records")]
        resolve(conn, str(record_id), observed_at=utcnow())
    finally:
        conn.close()
    return LiveCase(root, config, str(record_id), question, prompt)


def arm_spec(case: LiveCase, directory: Path, **overrides: Any) -> Path:
    """Write an arm spec over the case's prompt into ``directory``; returns its path."""
    from whiskeyjack_bot.prompt import load_prompt

    loaded = load_prompt(
        case.prompt,
        case.config.forecast.prompt_version,
        min_probability=case.config.forecast.min_probability,
        max_probability=case.config.forecast.max_probability,
    )
    spec: dict[str, Any] = {
        "spec_schema_version": "1.0.0",
        "name": "test-arm",
        "model": ASTRA,
        "reasoning_effort": "high",
        "prompt": {
            "path": str(case.prompt),
            "version": loaded.version,
            "sha256": loaded.sha256,
        },
        "transform": {"kind": "identity"},
        "samples": 1,
        "selection": {},
    }
    spec.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]
    path = directory / f"arm-{name}.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path
