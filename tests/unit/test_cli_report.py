"""M5-804: the `report` command at the CLI boundary.

The command layer's own behaviour -- where the output lands when `--output` is omitted, what
it prints, the exit code a refusal produces, and that a mistyped `--config` cannot mint an empty
ledger and then report on it. The report's semantics are `tests/unit/test_report.py`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from report_rows import build_ledger

from whiskeyjack_bot.cli import EXIT_REFUSED, main
from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_OK
from whiskeyjack_bot.report import MANIFEST_FILENAME, RECORDS_FILENAME, REPORT_FILENAME

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """A valid config whose data paths live under tmp_path (`test_cli_export`'s shape)."""
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


def _paths(config_file: Path) -> tuple[Path, Path]:
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    return Path(data["storage"]["sqlite_path"]), Path(data["storage"]["export_root"])


@pytest.fixture()
def seeded(config_file: Path) -> Path:
    ledger_path, _ = _paths(config_file)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    build_ledger(ledger_path)
    return config_file


@pytest.fixture(autouse=True)
def fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The credentials `_load_verified_config` checks for; never used, nothing is called."""
    for name in (
        "METACULUS_TOKEN",
        "OPENROUTER_API_KEY",
        "ASKNEWS_CLIENT_ID",
        "ASKNEWS_CLIENT_SECRET",
        "EXA_API_KEY",
    ):
        monkeypatch.setenv(name, "fakeFAKE1234")


def test_report_writes_under_the_configured_export_root_and_prints_counts(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, export_root = _paths(seeded)
    assert main(["report", "--config", str(seeded)]) == EXIT_OK

    (destination,) = [path for path in export_root.iterdir() if path.is_dir()]
    assert destination.name.startswith("report-")
    assert {path.name for path in destination.iterdir()} == {
        RECORDS_FILENAME,
        REPORT_FILENAME,
        MANIFEST_FILENAME,
    }
    out = capsys.readouterr().out
    assert "schema:    version 17" in out
    assert "records:   16 (1 excluded, 15 included)" in out
    lines = out.splitlines()
    assert "        7 scored" in lines
    assert "        1 superseded" in lines
    assert "warning:   overlapping_axes (3)" in lines
    assert "warning:   stale_score_rows (12)" in lines


def test_the_printed_state_lines_partition_the_included_count(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The only numbers printed per state add up to `included`; no axis total is printed."""
    assert main(["report", "--config", str(seeded)]) == EXIT_OK
    out = capsys.readouterr().out
    counts = [int(line.split()[0]) for line in out.splitlines() if line.startswith("  ")]
    assert len(counts) == 9
    assert sum(counts) == 15
    for axis in ("source_category", "reasoning_strategy_tag", "evidence_gap"):
        assert axis not in out.replace("overlapping_axes", "")


def test_report_honours_an_explicit_output_directory(seeded: Path, tmp_path: Path) -> None:
    destination = tmp_path / "elsewhere" / "run-1"
    assert main(["report", "--config", str(seeded), "--output", str(destination)]) == EXIT_OK
    manifest = json.loads((destination / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["report_schema_version"] == 1
    _, export_root = _paths(seeded)
    assert not export_root.exists() or list(export_root.iterdir()) == []


def test_a_second_report_to_the_same_directory_is_refused(seeded: Path, tmp_path: Path) -> None:
    destination = tmp_path / "twice"
    assert main(["report", "--config", str(seeded), "--output", str(destination)]) == EXIT_OK
    original = (destination / REPORT_FILENAME).read_bytes()
    assert main(["report", "--config", str(seeded), "--output", str(destination)]) == EXIT_REFUSED
    assert (destination / REPORT_FILENAME).read_bytes() == original


def test_a_mistyped_config_path_cannot_mint_a_ledger_and_report_on_it(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path, _ = _paths(config_file)
    assert not ledger_path.exists()
    assert main(["report", "--config", str(config_file)]) == EXIT_REFUSED
    assert not ledger_path.exists()
    assert capsys.readouterr().out.startswith("refused: ")


def test_a_ledger_path_pointing_at_a_non_database_is_refused_not_crashed(
    config_file: Path,
) -> None:
    ledger_path, _ = _paths(config_file)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("definitely not sqlite\n" * 64, encoding="utf-8")
    assert main(["report", "--config", str(config_file)]) == EXIT_REFUSED


def test_an_invalid_config_is_reported_before_any_ledger_work(tmp_path: Path) -> None:
    broken = tmp_path / "config.yaml"
    broken.write_text("environment: development\n", encoding="utf-8")
    assert main(["report", "--config", str(broken)]) == EXIT_CONFIG_INVALID
