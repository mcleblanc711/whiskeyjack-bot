"""M1-604: the `export` command at the CLI boundary.

What is under test here is the command layer's own behaviour -- required arguments, where
the output lands when `--output` is omitted, the exit code a refusal produces, and that a
mistyped `--config` cannot silently mint an empty ledger and then export it. The export
semantics themselves are `tests/unit/test_export.py`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from whiskeyjack_bot.cli import EXIT_REFUSED, main
from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_OK
from whiskeyjack_bot.export import EXPORTED_TABLES, MANIFEST_FILENAME
from whiskeyjack_bot.ledger import connect, initialize_ledger

from tests.unit.test_export import _seed_every_table

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """A valid config whose data paths live under tmp_path (the test_env_verify shape)."""
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
    initialize_ledger(ledger_path)
    conn = connect(ledger_path)
    try:
        _seed_every_table(conn)
    finally:
        conn.close()
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


@pytest.mark.parametrize("export_format", ["jsonl", "parquet"])
def test_export_writes_every_table_under_the_configured_export_root(
    seeded: Path, export_format: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--output` is optional, and its default is the config field nothing else reads.

    `storage.export_root` has existed since M1-601 and `verify-env` has been checking it
    all along with no writer behind it; this is the command that finally uses it.
    """
    _, export_root = _paths(seeded)
    assert main(["export", "--config", str(seeded), "--format", export_format]) == EXIT_OK

    directories = sorted(p for p in export_root.iterdir() if p.is_dir())
    assert len(directories) == 1
    destination = directories[0]
    assert destination.name.startswith(f"{export_format}-")

    written = {p.name for p in destination.iterdir()}
    assert written == {f"{spec.name}.{export_format}" for spec in EXPORTED_TABLES} | {
        MANIFEST_FILENAME
    }
    assert f"format:    {export_format}" in capsys.readouterr().out


def test_export_honours_an_explicit_output_directory(seeded: Path, tmp_path: Path) -> None:
    destination = tmp_path / "elsewhere" / "run-1"
    assert (
        main(
            [
                "export",
                "--config",
                str(seeded),
                "--format",
                "jsonl",
                "--output",
                str(destination),
            ]
        )
        == EXIT_OK
    )
    manifest = json.loads((destination / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["format"] == "jsonl"

    _, export_root = _paths(seeded)
    assert not export_root.exists() or list(export_root.iterdir()) == []


def test_a_second_export_to_the_same_directory_is_refused(seeded: Path, tmp_path: Path) -> None:
    """An export never overwrites one already written, and says so rather than exiting 0."""
    destination = tmp_path / "twice"
    assert (
        main(["export", "--config", str(seeded), "--format", "jsonl", "--output", str(destination)])
        == EXIT_OK
    )
    original = (destination / "forecast_records.jsonl").read_bytes()
    assert (
        main(["export", "--config", str(seeded), "--format", "jsonl", "--output", str(destination)])
        == EXIT_REFUSED
    )
    assert (destination / "forecast_records.jsonl").read_bytes() == original


def test_a_mistyped_config_path_cannot_mint_a_ledger_and_export_it(
    config_file: Path, tmp_path: Path
) -> None:
    """The `_open_existing_ledger` guarantee, restated for the one command that skips it.

    `export` is the only ledger command that does not go through `_open_existing_ledger` --
    it hands the path to `export_ledger`, which opens read-only itself. So the "a wrong
    path must not create an empty database and then answer questions about it" property
    has to be checked here rather than inherited.
    """
    ledger_path, _ = _paths(config_file)
    assert not ledger_path.exists()
    assert main(["export", "--config", str(config_file), "--format", "jsonl"]) == EXIT_REFUSED
    assert not ledger_path.exists()


def test_a_ledger_path_pointing_at_a_non_database_is_refused_not_crashed(
    config_file: Path, tmp_path: Path
) -> None:
    """GPT review round 1, B1, at the operator's level: a refusal, not a traceback."""
    ledger_path, _ = _paths(config_file)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("definitely not sqlite\n" * 64, encoding="utf-8")
    assert main(["export", "--config", str(config_file), "--format", "jsonl"]) == EXIT_REFUSED


def test_an_invalid_config_is_reported_before_any_ledger_work(tmp_path: Path) -> None:
    broken = tmp_path / "config.yaml"
    broken.write_text("environment: development\n", encoding="utf-8")
    assert main(["export", "--config", str(broken), "--format", "jsonl"]) == EXIT_CONFIG_INVALID


def test_the_format_argument_is_required_and_closed(seeded: Path) -> None:
    """argparse `choices` refuses an unknown format, so the module never sees one."""
    with pytest.raises(SystemExit) as excinfo:
        main(["export", "--config", str(seeded)])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(["export", "--config", str(seeded), "--format", "csv"])
    assert excinfo.value.code == 2


def test_the_command_prints_a_row_count_for_every_table(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator reads this to know the export is not silently empty."""
    assert main(["export", "--config", str(seeded), "--format", "jsonl"]) == EXIT_OK
    out = capsys.readouterr().out
    for spec in EXPORTED_TABLES:
        assert spec.name in out
    assert f"tables:    {len(EXPORTED_TABLES)}" in out
