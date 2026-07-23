"""M0-004 acceptance: verify-env reports missing variable names only, exits
non-zero on invalid live-submit settings, and never echoes a secret value."""

import copy
from pathlib import Path

import pytest
import yaml

from whiskeyjack_bot.cli import main
from whiskeyjack_bot.env_verify import (
    EXIT_CONFIG_INVALID,
    EXIT_ENV_MISSING,
    EXIT_OK,
    verify_environment,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

ALL_ENV_VARS = ["METACULUS_TOKEN", "OPENROUTER_API_KEY", "ASKNEWS_API_KEY", "EXA_API_KEY"]
FAKE_VALUES = {name: f"fake-{name.lower()}-value-12345" for name in ALL_ENV_VARS}


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """A valid config whose data paths live under tmp_path."""
    data = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data["model"]["name"] = "openrouter/test-model"
    data["storage"]["sqlite_path"] = str(tmp_path / "data" / "bot.sqlite3")
    data["storage"]["artifact_root"] = str(tmp_path / "data" / "artifacts")
    data["storage"]["export_root"] = str(tmp_path / "data" / "exports")
    data["logging"]["file"] = str(tmp_path / "data" / "logs" / "bot.jsonl")
    data["forecast"]["prompt_path"] = str(REPO_ROOT / "prompts" / "forecaster.md")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def set_all_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in FAKE_VALUES.items():
        monkeypatch.setenv(name, value)


def clear_all_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [*ALL_ENV_VARS, "XAI_API_KEY"]:
        monkeypatch.delenv(name, raising=False)


def test_all_present_exits_ok(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_all_env(monkeypatch)
    report = verify_environment(config_file)
    assert report.exit_code == EXIT_OK
    assert report.missing_env_vars == []
    # Directories were created.
    assert (config_file.parent / "data" / "artifacts").is_dir()


def test_missing_vars_reported_by_name_only(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clear_all_env(monkeypatch)
    exit_code = main(["verify-env", "--config", str(config_file)])
    captured = capsys.readouterr()
    assert exit_code == EXIT_ENV_MISSING
    for name in ALL_ENV_VARS:
        assert name in captured.out
    assert "XAI_API_KEY" not in captured.out  # social disabled -> not required


def test_secret_values_never_appear_in_output(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_all_env(monkeypatch)
    main(["verify-env", "--config", str(config_file)])
    captured = capsys.readouterr()
    for value in FAKE_VALUES.values():
        assert value not in captured.out
        assert value not in captured.err


def test_empty_string_counts_as_missing(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_all_env(monkeypatch)
    monkeypatch.setenv("METACULUS_TOKEN", "")
    report = verify_environment(config_file)
    assert report.exit_code == EXIT_ENV_MISSING
    assert report.missing_env_vars == ["METACULUS_TOKEN"]


def test_invalid_live_submit_config_exits_config_invalid(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_all_env(monkeypatch)
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["submission"]["enabled"] = True
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")
    report = verify_environment(bad)
    assert report.exit_code == EXIT_CONFIG_INVALID
    assert any("Milestone 2" in p for p in report.config_problems)


def test_social_enabled_requires_xai_key_and_allowlist(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_all_env(monkeypatch)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["retrieval"]["social"]["enabled"] = True
    data["retrieval"]["social"]["agent_model"] = "grok-fixture"
    data["retrieval"]["social"]["account_allowlist_path"] = str(
        REPO_ROOT / "config" / "x_accounts.yaml"
    )
    social = tmp_path / "social.yaml"
    social.write_text(yaml.safe_dump(data), encoding="utf-8")
    report = verify_environment(social)
    assert report.exit_code == EXIT_ENV_MISSING
    assert report.missing_env_vars == ["XAI_API_KEY"]


def test_missing_prompt_file_is_reported(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_all_env(monkeypatch)
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["forecast"]["prompt_path"] = str(tmp_path / "no-such-prompt.md")
    bad = tmp_path / "no-prompt.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")
    report = verify_environment(bad)
    assert report.exit_code == EXIT_ENV_MISSING
    assert any("prompt_path" in p for p in report.filesystem_problems)
    # One problem, not two: the version check is skipped when the file is absent.
    assert len(report.filesystem_problems) == 1


def test_prompt_version_mismatch_is_reported(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1-401: prompt/config version drift is caught before a run, not at the
    first forecast, when the wrong version would already have been recorded."""
    set_all_env(monkeypatch)
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["forecast"]["prompt_version"] = "9.9.9"
    bad = tmp_path / "version-drift.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")
    report = verify_environment(bad)
    assert report.exit_code == EXIT_ENV_MISSING
    assert any("prompt_version" in p for p in report.filesystem_problems)


def test_matching_prompt_version_passes(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_all_env(monkeypatch)
    report = verify_environment(config_file)
    assert report.exit_code == EXIT_OK
    assert any("declares version" in c for c in report.checks_passed)
