"""M1-356: the read-only `research language-census` command at the CLI boundary."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from whiskeyjack_bot.cli import EXIT_REFUSED, main
from whiskeyjack_bot.config import load_config
from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
from whiskeyjack_bot.ledger import connect, initialize_ledger
from whiskeyjack_bot.research.model import ResearchRun
from whiskeyjack_bot.research.persist import persist_paid_run

REPO_ROOT = Path(__file__).resolve().parents[2]
WHEN = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    data = copy.deepcopy(
        yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    )
    data["model"]["name"] = "openrouter/test-model"
    data["storage"]["sqlite_path"] = str(tmp_path / "data" / "bot.sqlite3")
    data["storage"]["artifact_root"] = str(tmp_path / "data" / "artifacts")
    data["storage"]["export_root"] = str(tmp_path / "data" / "exports")
    data["logging"]["file"] = str(tmp_path / "data" / "logs" / "bot.jsonl")
    data["forecast"]["prompt_path"] = str(REPO_ROOT / "prompts" / "forecaster.md")
    data["retrieval"]["social"]["account_allowlist_path"] = str(
        REPO_ROOT / "config" / "x_accounts.yaml"
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "METACULUS_TOKEN",
        "OPENROUTER_API_KEY",
        "ASKNEWS_CLIENT_ID",
        "ASKNEWS_CLIENT_SECRET",
        "EXA_API_KEY",
    ):
        monkeypatch.setenv(name, "fakeFAKE1234")


@pytest.fixture()
def seeded(config_file: Path) -> Path:
    config = load_config(config_file)
    config.storage.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    config.storage.artifact_root.mkdir(parents=True, exist_ok=True)
    initialize_ledger(config.storage.sqlite_path)
    conn = connect(config.storage.sqlite_path)
    try:
        run = ResearchRun.model_validate(
            {
                "retrieval_run_id": "run-a",
                "question_id": 7,
                "provider": "asknews",
                "started_at_utc": WHEN,
                "completed_at_utc": WHEN,
            }
        )
        body = {
            "as_dicts": [
                {"article_url": "https://www.mehrnews.com/1", "language": "fa", "country": "IR"},
                {"article_url": "https://www.bbc.com/persian/2", "language": "fa", "country": "GB"},
            ]
        }
        persist_paid_run(conn, config, run, (), raw_responses=(body,), written_at_utc=WHEN)
    finally:
        conn.close()
    return config_file


def test_the_census_prints_counts_as_json_and_writes_nothing(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = load_config(seeded).storage.sqlite_path
    before = ledger.read_bytes()
    assert main(["research", "language-census", "--config", str(seeded)]) == EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["articles"] == 2
    assert printed["languages"] == {"fa": 2}
    assert printed["publisher_origins"] == {"IR:external": 1, "IR:local": 1}
    assert len(printed["registry_sha256"]) == 64
    assert ledger.read_bytes() == before


def test_a_missing_ledger_is_refused_rather_than_created(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = load_config(config_file).storage.sqlite_path
    assert main(["research", "language-census", "--config", str(config_file)]) == EXIT_REFUSED
    assert not ledger.exists()


def test_a_missing_registry_is_an_environment_failure(
    seeded: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.yaml"
    code = main(
        ["research", "language-census", "--config", str(seeded), "--registry", str(missing)]
    )
    assert code == EXIT_ENV_MISSING
    assert str(missing) in capsys.readouterr().out


def test_an_invalid_registry_is_a_config_failure(
    seeded: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "locales.yaml"
    bad.write_text("schema_version: 1\nlocales: []\n", encoding="utf-8")
    code = main(["research", "language-census", "--config", str(seeded), "--registry", str(bad)])
    assert code == EXIT_CONFIG_INVALID


def test_the_lens_report_is_read_only_and_empty_on_a_ledger_without_lens_runs(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = load_config(seeded).storage.sqlite_path
    before = ledger.read_bytes()
    assert main(["research", "lens-report", "--config", str(seeded)]) == EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["questions"] == []
    assert printed["totals"]["checkpoints"] == 0
    assert ledger.read_bytes() == before


def test_the_lens_report_refuses_a_missing_ledger(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = load_config(config_file).storage.sqlite_path
    assert main(["research", "lens-report", "--config", str(config_file)]) == EXIT_REFUSED
    assert not ledger.exists()
