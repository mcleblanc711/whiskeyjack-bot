"""One seeded replay scenario, built through production writers only (T-903).

Shared by ``tests/acceptance`` (which drives the command) and ``tests/unit/test_pipeline.py``
(which drives the module's refusals), because both need the same preconditions and building
them twice would let the two drift into testing different situations.

Every precondition below is created by the code that creates it in production --
``persist_retrieval`` for the research run, ``write_raw_model_output`` for the saved reply,
``load_snapshot``/``normalize_questions`` for the question. Nothing is hand-written JSON, and
that is ``docs/LESSONS.md`` § "a simulated boundary tests the simulation": M1-306's ``-0.0``
and surrogate-pair defects both lived in the half of storage a JSON round trip simulates
away, so a fixture that shortcut the writers would be asserting against its own shortcut.

The one thing the seed may **not** do is call ``persist_generation``. That would write a
forecast record before the command under test runs, and "``run`` produces exactly one
validated record" is then unfalsifiable -- the row would already be there. So the artifact is
written on its own, and the ledger holds research and nothing else when ``run`` starts.

There are no other shared fixtures in this suite; the shapes here follow
``tests/unit/test_cli_replay.py`` (config file), ``tests/unit/test_research_store.py``
(research run and documents) and ``tests/unit/test_forecast_artifacts.py`` (artifact).
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from whiskeyjack_bot.config import AppConfig, load_config
from whiskeyjack_bot.forecast.artifacts import write_raw_model_output
from whiskeyjack_bot.forecast.inputs import build_model_input, render_model_input
from whiskeyjack_bot.forecast.parse import ForecastGeneration, ModelSettings
from whiskeyjack_bot.forecast.schema import response_model_for, validate_forecast_response
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.metaculus.snapshots import load_snapshot
from whiskeyjack_bot.prompt import load_prompt
from whiskeyjack_bot.questions.model import CanonicalQuestion
from whiskeyjack_bot.research.model import ResearchDocument, ResearchRun, validate_document
from whiskeyjack_bot.research.model import validate_run
from whiskeyjack_bot.research.packet import ResearchPacket
from whiskeyjack_bot.research.store import (
    list_retrieval_run_ids,
    persist_retrieval,
    replay_research,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO_ROOT / "tests" / "fixtures" / "snapshots" / "minibench_sample_snapshot.json"
PROMPT_PATH = REPO_ROOT / "prompts" / "forecaster.md"
PROMPT_TEXT = PROMPT_PATH.read_text(encoding="utf-8")

# The committed snapshot's binary question. Its two siblings (91002 numeric, 91003
# multiple-choice) stay in the file on purpose: `_select_question` has to pick one out of a
# normalized batch, and a single-question snapshot would not exercise that.
QUESTION_ID = 91001
TOURNAMENT = "minibench"
SEED_ATTEMPT = "seed-attempt"
RUN_ID = "run-1"

STARTED = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
COMPLETED = datetime(2026, 8, 18, 12, 5, tzinfo=timezone.utc)
# The model's claim about the world, baked into the stored request and recovered from it by
# `_as_of_from_request`. Deliberately not equal to NOW: the two are different claims and a
# fixture that made them the same could not catch them being confused.
AS_OF = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 21, 15, 30, tzinfo=timezone.utc)

# Low-entropy on purpose: a realistic-looking key would trip the repository's gitleaks
# full-history scan on every unrelated PR (docs/LESSONS.md).
ENV_VARS = ("METACULUS_TOKEN", "OPENROUTER_API_KEY", "ASKNEWS_API_KEY", "EXA_API_KEY")
FAKE_VALUES = {name: f"notreal{name.lower()}12345" for name in ENV_VARS}


@dataclass(frozen=True)
class Seed:
    """The state one `run` invocation is about to consume, plus what built it."""

    config_file: Path
    config: AppConfig
    attempt_id: str
    question: CanonicalQuestion
    packet: ResearchPacket
    request: str
    payload: dict[str, Any]
    settings: ModelSettings
    artifact: Path


def config_data(tmp_path: Path) -> dict[str, Any]:
    """The example config with every writable path moved under ``tmp_path``.

    Both replay switches are turned on, because ``pipeline._require_replay_enabled`` reads
    them together and both are committed as ``false``. The existing per-module fixtures flip
    one each, which is why neither could be reused here.
    """
    data: dict[str, Any] = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    data["storage"]["sqlite_path"] = str(tmp_path / "data" / "bot.sqlite3")
    data["storage"]["artifact_root"] = str(tmp_path / "data" / "artifacts")
    data["storage"]["export_root"] = str(tmp_path / "data" / "exports")
    data["logging"]["file"] = str(tmp_path / "data" / "logs" / "bot.jsonl")
    data["forecast"]["prompt_path"] = str(PROMPT_PATH)
    data["forecast"]["replay_saved_model_output"] = True
    data["retrieval"]["replay_saved_research"] = True
    data["retrieval"]["social"]["account_allowlist_path"] = str(
        REPO_ROOT / "config" / "x_accounts.yaml"
    )
    return data


def write_config(tmp_path: Path, data: dict[str, Any], name: str = "config.yaml") -> Path:
    """Write a config file. ``name`` lets a test write a *second* one over the same storage.

    That is how the switch-off refusals are built: the scenario has to be seeded with replay
    enabled (the seeding code reads the packet back through ``replay_research``), and the
    run then has to happen under a config that turns it off. Two files, one data directory.
    """
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def open_ledger(config: AppConfig) -> sqlite3.Connection:
    config.storage.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    config.storage.artifact_root.mkdir(parents=True, exist_ok=True)
    initialize_ledger(config.storage.sqlite_path)
    return connect(config.storage.sqlite_path)


def research_run(**overrides: Any) -> ResearchRun:
    payload: dict[str, Any] = {
        "retrieval_run_id": RUN_ID,
        "question_id": QUESTION_ID,
        "provider": "asknews",
        "provider_config": {"hours_back": 720, "strategy": "news knowledge"},
        "queries": ["example agency july release"],
        "started_at_utc": STARTED,
        # What makes the run *completed*, and therefore replayable at all:
        # `list_retrieval_run_ids` is `completed_only=True`, so an open run is invisible.
        "completed_at_utc": COMPLETED,
        "freshness_cutoff_utc": datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
        "cost_usd": None,
    }
    payload.update(overrides)
    return validate_run(payload)


def research_documents(count: int = 2) -> list[ResearchDocument]:
    """Two documents, which is not arbitrary.

    ``forecast/inputs.py`` mints one ``src-NNN`` per document in ``dedup_key`` order, and
    M1-501's attribution rules resolve every citation in the reply against exactly that set.
    The reply this fixture replays is the forecaster prompt's own example, which cites
    ``src-001`` and ``src-002``; one document would fail it for a reason that has nothing to
    do with what any test here is about.
    """
    return [
        validate_document(
            {
                "retrieval_run_id": RUN_ID,
                "original_url": f"https://example.org/a{index}",
                "canonical_url": f"https://example.org/a{index}",
                "title": f"Article {index}",
                "retrieved_at_utc": STARTED,
                "source_type": "news",
                "provenance": "direct_api",
                "content_sha256": f"{index:064x}",
            }
        )
        for index in range(count)
    ]


def snapshot_question(question_id: int = QUESTION_ID) -> CanonicalQuestion:
    _, loaded = load_snapshot(SNAPSHOT)
    from whiskeyjack_bot.questions.normalize import normalize_questions

    result = normalize_questions(loaded)
    return next(q for q in result.questions if q.question_id == question_id)


def reply_payload(**overrides: Any) -> dict[str, Any]:
    """The forecaster prompt's own example reply, assembled from its schema blocks.

    Taken from ``prompts/forecaster.md`` rather than restated here (the
    ``test_cli_replay.py`` trick) so that a prompt edit which invalidates the example
    surfaces as a test failure instead of as a fixture that quietly disagrees with the file
    the model is actually shown.
    """

    def block(heading: str) -> str:
        body = PROMPT_TEXT.split(f"\n## {heading}\n", 1)[1]
        match = re.search(r"```json\n(.*?)\n```", body, re.DOTALL)
        assert match is not None, heading
        return match.group(1)

    payload: dict[str, Any] = {
        **json.loads(block("Shared fields")),
        **json.loads("{" + block("Binary schema") + "}"),
    }
    payload["question_id"] = QUESTION_ID
    payload.update(overrides)
    return payload


def model_settings(config: AppConfig) -> ModelSettings:
    """Settings that agree with the config, including the prompt's *real* digest.

    ``pipeline._require_settings_agree`` compares provider, model name, prompt version and
    prompt sha256, recomputing the last from the file on disk. A placeholder digest -- which
    is what the per-module fixtures use, because nothing there reads it -- would refuse every
    replay here for a reason unrelated to the test.
    """
    prompt = load_prompt(config.forecast.prompt_path, config.forecast.prompt_version)
    return ModelSettings(
        provider=config.model.provider,
        name=config.model.name,
        temperature=config.model.temperature,
        max_output_tokens=config.model.max_output_tokens,
        timeout_seconds=config.model.timeout_seconds,
        allowed_tries=2,
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
    )


def build_generation(
    *,
    config: AppConfig,
    question: CanonicalQuestion,
    packet: ResearchPacket,
    payload: dict[str, Any],
    request: str | None = None,
    settings: ModelSettings | None = None,
) -> tuple[ForecastGeneration, str]:
    """A generation whose ``request`` is the packet the reply really answered.

    ``pipeline._rebuilt_input`` requires the rebuilt reasoning packet to equal the stored one
    byte for byte, so the seed has to render the real thing. A placeholder string -- which is
    all ``test_cli_replay.py`` needs, because ``replay --record-id`` never rebuilds it --
    would fail every replay here.
    """
    model_input = build_model_input(
        question=question, packet=packet, tournament_id=TOURNAMENT, as_of=AS_OF
    )
    rendered = render_model_input(model_input) if request is None else request
    generation = ForecastGeneration(
        forecast=validate_forecast_response(
            payload, response_model_for(str(payload["question_type"]))
        ),
        settings=settings or model_settings(config),
        sources=model_input.sources,
        request=rendered,
        raw_responses=(json.dumps(payload),),
        invocations=1,
        repair_attempted=False,
        cost_usd=0.25,
        failure_code=None,
        failure_problems=(),
    )
    return generation, rendered


def seed_scenario(
    config_file: Path,
    *,
    documents: int = 2,
    attempt_id: str = SEED_ATTEMPT,
    payload: dict[str, Any] | None = None,
    request: str | None = None,
    settings: ModelSettings | None = None,
) -> Seed:
    """Persist the research run and write the saved reply. Writes no forecast record."""
    config = load_config(config_file)
    conn = open_ledger(config)
    try:
        # Idempotent: a test that seeds twice over one ledger -- to write a second artifact
        # whose request differs, say -- must not re-persist the research. `research_runs`
        # has a unique id and `research_documents` is append-only, so re-persisting is not
        # something the product allows and not something a fixture should ask for.
        if not list_retrieval_run_ids(conn, question_id=QUESTION_ID):
            persist_retrieval(conn, research_run(), research_documents(documents))
        run_ids = list_retrieval_run_ids(conn, question_id=QUESTION_ID)
        packet = replay_research(conn, config, question_id=QUESTION_ID, retrieval_run_ids=run_ids)
    finally:
        conn.close()

    question = snapshot_question()
    reply = reply_payload() if payload is None else payload
    generation, rendered = build_generation(
        config=config,
        question=question,
        packet=packet,
        payload=reply,
        request=request,
        settings=settings,
    )
    relative = write_raw_model_output(
        config.storage.artifact_root,
        attempt_id=attempt_id,
        question_id=QUESTION_ID,
        generation=generation,
        written_at_utc=COMPLETED,
        retain=True,
    )
    assert relative is not None
    return Seed(
        config_file=config_file,
        config=config,
        attempt_id=attempt_id,
        question=question,
        packet=packet,
        request=rendered,
        payload=reply,
        settings=generation.settings,
        artifact=config.storage.artifact_root / relative,
    )
