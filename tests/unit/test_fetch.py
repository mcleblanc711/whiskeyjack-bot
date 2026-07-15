"""M0-102/M0-104 acceptance: fixture and live-read modes return typed
questions; the tournament alias can be overridden without code changes; the
SDK alias is checked but never silently adopted (D31)."""

import copy
import json
import logging
from pathlib import Path

import pytest
import yaml
from forecasting_tools.data_models.questions import BinaryQuestion
from forecasting_tools.helpers.metaculus_client import MetaculusClient

from whiskeyjack_bot.cli import main
from whiskeyjack_bot.config import AppConfig, validate_config_data
from whiskeyjack_bot.metaculus.client import MissingCredentialError
from whiskeyjack_bot.metaculus.fetch import (
    fetch_open_questions_fixture,
    fetch_open_questions_live,
    resolve_tournament_id,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO_ROOT / "tests" / "fixtures" / "snapshots" / "minibench_sample_snapshot.json"


def make_config(tmp_path: Path, **metaculus_overrides: object) -> AppConfig:
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data["model"]["name"] = "openrouter/test-model"
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    data["metaculus"]["tournament"].update(metaculus_overrides)
    return validate_config_data(data)


# ── D31 tournament resolution ────────────────────────────────────────────────


def test_configured_id_matching_sdk_resolves_quietly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = make_config(tmp_path)
    with caplog.at_level(logging.WARNING):
        resolved = resolve_tournament_id(config)
    assert resolved.id == "minibench"
    assert resolved.origin == "config"
    assert not caplog.records


def test_configured_id_differing_from_sdk_warns_but_wins(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = make_config(tmp_path, id="minibench-s99")
    with caplog.at_level(logging.WARNING):
        resolved = resolve_tournament_id(config)
    assert resolved.id == "minibench-s99"
    assert resolved.origin == "config"
    assert any("CURRENT_MINIBENCH_ID" in r.getMessage() for r in caplog.records)


def test_use_sdk_current_id_adopts_sdk_alias(tmp_path: Path) -> None:
    config = make_config(tmp_path, id="stale-configured-slug", use_sdk_current_id=True)
    resolved = resolve_tournament_id(config)
    assert resolved.id == MetaculusClient.CURRENT_MINIBENCH_ID
    assert resolved.origin == "sdk_current"


def test_cli_override_wins_without_config_change(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    resolved = resolve_tournament_id(config, override="bot-testing-area")
    assert resolved.id == "bot-testing-area"
    assert resolved.origin == "cli_override"
    # Production config object is untouched.
    assert config.metaculus.tournament.id == "minibench"


# ── fixture mode (zero network by conftest guard) ────────────────────────────


def test_fixture_mode_returns_typed_questions() -> None:
    meta, questions = fetch_open_questions_fixture(SNAPSHOT)
    assert meta.tournament_id == "minibench"
    assert len(questions) == 3
    assert any(isinstance(q, BinaryQuestion) for q in questions)
    assert all(q.id_of_question is not None for q in questions)


def test_cli_questions_fetch_fixture_mode(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        [
            "questions",
            "fetch",
            "--config",
            str(REPO_ROOT / "config.example.yaml"),
            "--snapshot",
            str(SNAPSHOT),
        ]
    )
    captured = capsys.readouterr()
    # config.example.yaml still carries the model placeholder -> config error.
    assert exit_code == 2
    assert "placeholder" in captured.out


def test_cli_fixture_mode_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["model"]["name"] = "openrouter/test-model"
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    exit_code = main(
        ["questions", "fetch", "--config", str(config_path), "--snapshot", str(SNAPSHOT)]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "questions: 3" in captured.out
    assert "[binary]" in captured.out
    assert "[numeric]" in captured.out
    assert "[multiple_choice]" in captured.out


def test_cli_reports_malformed_snapshot_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Cross-review finding 5: a malformed snapshot must exit 2 through the
    # SnapshotError path, not escape the CLI as a raw traceback.
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["model"]["name"] = "openrouter/test-model"
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    del snapshot["questions"][0]["data"]
    bad_snapshot = tmp_path / "malformed_snapshot.json"
    bad_snapshot.write_text(json.dumps(snapshot), encoding="utf-8")

    exit_code = main(
        ["questions", "fetch", "--config", str(config_path), "--snapshot", str(bad_snapshot)]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "missing its data payload" in captured.out
    assert "Traceback" not in captured.out


def test_cli_snapshot_error_output_never_echoes_snapshot_contents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Re-review finding 1: the CLI prints str(SnapshotError); a credential
    # planted in a snapshot payload must not reach stdout or stderr.
    secret = "privateFAKE123456"
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["model"]["name"] = "openrouter/test-model"
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    snapshot["questions"][0]["data"] = json.dumps({"question_text": secret})
    bad_snapshot = tmp_path / "leaky_snapshot.json"
    bad_snapshot.write_text(json.dumps(snapshot), encoding="utf-8")

    exit_code = main(
        ["questions", "fetch", "--config", str(config_path), "--snapshot", str(bad_snapshot)]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "does not deserialize" in captured.out
    assert secret not in captured.out
    assert secret not in captured.err
    assert "Traceback" not in captured.out


def test_cli_fixture_mode_requires_snapshot_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data["model"]["name"] = "openrouter/test-model"
    data["logging"]["file"] = str(tmp_path / "logs" / "bot.jsonl")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    exit_code = main(["questions", "fetch", "--config", str(config_path)])
    assert exit_code == 2
    assert "--snapshot" in capsys.readouterr().out


# ── live mode ────────────────────────────────────────────────────────────────


def test_live_mode_without_token_fails_before_any_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("METACULUS_TOKEN", raising=False)
    config = make_config(tmp_path)
    with pytest.raises(MissingCredentialError):
        fetch_open_questions_live(config)


def test_live_mode_plumbs_tournament_and_group_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("METACULUS_TOKEN", "fake-token-for-test-0001")
    seen: dict[str, object] = {}

    def fake_get(
        self: MetaculusClient, tournament_id: object, group_question_mode: str = "exclude"
    ) -> list:
        seen["tournament_id"] = tournament_id
        seen["group_question_mode"] = group_question_mode
        return []

    monkeypatch.setattr(MetaculusClient, "get_all_open_questions_from_tournament", fake_get)
    config = make_config(tmp_path)
    resolved, questions = fetch_open_questions_live(config, override="bot-testing-area")
    assert questions == []
    assert resolved.id == "bot-testing-area"
    assert seen == {
        "tournament_id": "bot-testing-area",
        "group_question_mode": "unpack_subquestions",
    }
