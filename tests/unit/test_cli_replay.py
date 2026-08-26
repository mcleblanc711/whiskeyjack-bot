"""M1-406: the `replay` command at the CLI boundary.

What is under test here is the command layer's own behaviour -- the exit code a mismatch
produces, that both hashes are printed whatever the verdict, and that a mistyped
``--config`` cannot silently mint an empty ledger and report against it. The replay
semantics themselves are ``tests/unit/test_forecast_replay.py``.

The exit code is the load-bearing part. This is the command Codex's **T-903** needs, and a
``replay`` that exited 0 on a mismatch would be a check nothing in CI could gate.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from whiskeyjack_bot.cli import EXIT_REFUSED, main
from whiskeyjack_bot.config import load_config
from whiskeyjack_bot.env_verify import EXIT_OK
from whiskeyjack_bot.forecast.inputs import SourceReference
from whiskeyjack_bot.forecast.parse import ForecastGeneration, ModelSettings
from whiskeyjack_bot.forecast.persist import persist_generation
from whiskeyjack_bot.forecast.record import build_forecast_record_draft
from whiskeyjack_bot.forecast.schema import response_model_for, validate_forecast_response
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.lifecycle import transaction
from whiskeyjack_bot.questions.model import CanonicalBinaryQuestion

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_TEXT = (REPO_ROOT / "prompts" / "forecaster.md").read_text(encoding="utf-8")

QUESTION_ID = 123
TOURNAMENT = "minibench"
RUN_ID = "run-1"
ATTEMPT = "attempt-1"
GENERATED_AT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
TS = "2026-08-22T00:00:00.000000+00:00"

HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """A valid config whose data paths live under tmp_path (the test_cli_approval shape)."""
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    data["storage"]["sqlite_path"] = str(tmp_path / "data" / "bot.sqlite3")
    data["storage"]["artifact_root"] = str(tmp_path / "data" / "artifacts")
    data["storage"]["export_root"] = str(tmp_path / "data" / "exports")
    data["logging"]["file"] = str(tmp_path / "data" / "logs" / "bot.jsonl")
    data["forecast"]["prompt_path"] = str(REPO_ROOT / "prompts" / "forecaster.md")
    data["forecast"]["replay_saved_model_output"] = True
    data["retrieval"]["social"]["account_allowlist_path"] = str(
        REPO_ROOT / "config" / "x_accounts.yaml"
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _payload(**overrides: Any) -> dict[str, Any]:
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


def _seed(config_file: Path) -> str:
    """Persist one real forecast through the production writer; return its record id."""
    config = load_config(config_file)
    config.storage.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    config.storage.artifact_root.mkdir(parents=True, exist_ok=True)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO research_runs "
                "(retrieval_run_id, provider, started_at_utc, created_at_utc, question_id) "
                "VALUES (?, 'exa', ?, ?, ?)",
                (RUN_ID, TS, TS, QUESTION_ID),
            )
        payload = _payload()
        generation = ForecastGeneration(
            forecast=validate_forecast_response(
                payload, response_model_for(payload["question_type"])
            ),
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
            raw_responses=(json.dumps(payload),),
            invocations=1,
            repair_attempted=False,
            cost_usd=0.25,
            failure_code=None,
            failure_problems=(),
        )
        draft = build_forecast_record_draft(
            question=CanonicalBinaryQuestion(
                question_id=QUESTION_ID, post_id=456, title="Will the thing happen?"
            ),
            generation=generation,
            tournament_id=TOURNAMENT,
            attempt_id=ATTEMPT,
            retrieval_run_id=RUN_ID,
            research_packet_sha256="d" * 64,
            generated_at=GENERATED_AT,
        )
        stored = persist_generation(
            conn,
            config,
            draft=draft,
            generation=generation,
            written_at=GENERATED_AT,
        )
        assert stored.record is not None
        assert stored.artifact_outcome == "written"
        return stored.record.record_id
    finally:
        conn.close()


def _artifact_path(config_file: Path) -> Path:
    config = load_config(config_file)
    paths = list(config.storage.artifact_root.rglob("*.json"))
    assert len(paths) == 1
    return paths[0]


def test_replay_of_an_intact_record_exits_zero_and_prints_both_hashes(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record_id = _seed(config_file)
    assert main(["replay", "--config", str(config_file), "--record-id", record_id]) == EXIT_OK
    out = capsys.readouterr().out
    assert "verdict:   match" in out
    stored, replayed = (
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith(("stored:", "replayed:"))
    )
    # Both are printed, and both are real digests -- an operator acting on a replay needs
    # the values it compared, not a word that summarizes them.
    assert HEX64.match(stored) and stored == replayed
    assert "cost 0.250000 USD" in out


def test_a_mismatch_exits_refused_and_still_prints_both_hashes(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The load-bearing exit code: a command that exited 0 on a mismatch is a check
    nothing in CI could gate, and T-903 is the caller that would rest on it."""
    record_id = _seed(config_file)
    path = _artifact_path(config_file)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    reply = json.loads(envelope["raw_responses"][0])
    reply["final_prediction"]["probability_yes"] = 0.42
    envelope["raw_responses"] = [json.dumps(reply)]
    path.write_text(json.dumps(envelope), encoding="utf-8")

    assert main(["replay", "--config", str(config_file), "--record-id", record_id]) == EXIT_REFUSED
    out = capsys.readouterr().out
    assert "verdict:   MISMATCH" in out
    stored, replayed = (
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith(("stored:", "replayed:"))
    )
    assert HEX64.match(stored) and HEX64.match(replayed) and stored != replayed


def test_the_disabled_default_refuses_at_the_command_boundary(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record_id = _seed(config_file)
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["forecast"]["replay_saved_model_output"] = False
    config_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["replay", "--config", str(config_file), "--record-id", record_id]) == EXIT_REFUSED
    assert "refused:" in capsys.readouterr().out


def test_an_unknown_record_is_refused_rather_than_reported_as_a_mismatch(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(config_file)
    assert main(["replay", "--config", str(config_file), "--record-id", "nope"]) == EXIT_REFUSED
    out = capsys.readouterr().out
    assert "refused:" in out
    assert "verdict:" not in out


def test_a_mistyped_config_does_not_mint_an_empty_ledger(
    tmp_path: Path, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_open_existing_ledger`'s rule (M2-701 round 1), asserted for this command too:
    otherwise `replay` would report "no such record" against a database it just created --
    a true statement about the wrong ledger."""
    _seed(config_file)
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    elsewhere = tmp_path / "elsewhere" / "bot.sqlite3"
    data["storage"]["sqlite_path"] = str(elsewhere)
    other = tmp_path / "other.yaml"
    other.write_text(yaml.safe_dump(data), encoding="utf-8")

    assert main(["replay", "--config", str(other), "--record-id", "rec-1"]) == EXIT_REFUSED
    assert "no ledger database at" in capsys.readouterr().out
    assert not elsewhere.exists()


def test_the_command_makes_no_network_call(config_file: Path) -> None:
    """Belt and braces over the import-graph test, which is the real proof.

    ``tests/conftest.py`` blocks sockets and DNS for the whole suite, so a command that
    reached a provider would fail here rather than succeed quietly -- but this passing
    proves only that *this* invocation made no call. That zero-calls holds for every
    invocation is
    ``test_forecast_generate.py::test_the_response_schema_reaches_no_provider_client``.
    """
    record_id = _seed(config_file)
    assert main(["replay", "--config", str(config_file), "--record-id", record_id]) == EXIT_OK


def test_the_command_requires_a_record_id(config_file: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["replay", "--config", str(config_file)])
    assert caught.value.code == 2


def test_the_seeded_ledger_holds_exactly_one_record(config_file: Path) -> None:
    """Positive control for the tests above: they all rest on `_seed` having really
    written one row through the production writer."""
    _seed(config_file)
    config = load_config(config_file)
    conn = sqlite3.connect(config.storage.sqlite_path)
    try:
        assert conn.execute("SELECT count(*) FROM forecast_records").fetchone()[0] == 1
    finally:
        conn.close()
