"""M0-004 acceptance: verify-env reports missing variable names only, exits
non-zero on invalid live-submit settings, and never echoes a secret value."""

import copy
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from whiskeyjack_bot.cli import _load_verified_config, main
from whiskeyjack_bot.env_verify import (
    EXIT_CONFIG_INVALID,
    EXIT_ENV_MISSING,
    EXIT_OK,
    verify_environment,
)
from whiskeyjack_bot.research.allowlist import AllowlistError

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


def test_corrupted_allowlist_is_reported_as_config_problem(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A readable-but-invalid allowlist is config-content, not filesystem: it fails
    EXIT_CONFIG_INVALID, same as any other malformed config, not EXIT_ENV_MISSING."""
    set_all_env(monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "fake-xai-key-value")
    real_accounts = yaml.safe_load(
        (REPO_ROOT / "config" / "x_accounts.yaml").read_text(encoding="utf-8")
    )
    # Inject a case-insensitive duplicate username into a real, otherwise-valid copy.
    real_accounts["accounts"].append(dict(real_accounts["accounts"][0]))
    corrupted = tmp_path / "corrupted_accounts.yaml"
    corrupted.write_text(yaml.safe_dump(real_accounts), encoding="utf-8")

    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["retrieval"]["social"]["enabled"] = True
    data["retrieval"]["social"]["agent_model"] = "grok-fixture"
    data["retrieval"]["social"]["account_allowlist_path"] = str(corrupted)
    social = tmp_path / "social.yaml"
    social.write_text(yaml.safe_dump(data), encoding="utf-8")

    report = verify_environment(social)
    assert report.exit_code == EXIT_CONFIG_INVALID
    assert any("allowlist" in p for p in report.config_problems)
    assert report.filesystem_problems == []


def test_disabled_social_with_malformed_allowlist_still_fails_config(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1-308 round-3 P1 regression: retrieval.social.enabled stays the committed
    default (false), but a malformed allowlist at account_allowlist_path must still
    fail config validation -- the acceptance criterion is unconditional."""
    set_all_env(monkeypatch)
    real_accounts = yaml.safe_load(
        (REPO_ROOT / "config" / "x_accounts.yaml").read_text(encoding="utf-8")
    )
    real_accounts["accounts"].append(dict(real_accounts["accounts"][0]))
    corrupted = tmp_path / "corrupted_accounts.yaml"
    corrupted.write_text(yaml.safe_dump(real_accounts), encoding="utf-8")

    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert data["retrieval"]["social"]["enabled"] is False
    data["retrieval"]["social"]["account_allowlist_path"] = str(corrupted)
    social = tmp_path / "social.yaml"
    social.write_text(yaml.safe_dump(data), encoding="utf-8")

    report = verify_environment(social)
    assert report.exit_code == EXIT_CONFIG_INVALID
    assert any("allowlist" in p for p in report.config_problems)


def test_unreadable_allowlist_is_reported_as_filesystem_problem(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1-308 round-3 P2 regression: a file that exists but can't be read (permission,
    race) is a filesystem problem, not a config-content one.

    The interception point is ``os.open``, not ``Path.read_bytes``: the round-6 FIFO guard
    replaced the latter, and this test went on passing a patch nothing called until it
    started failing outright. ``intercepted`` is asserted below so a future move of the read
    makes this test fail loudly rather than pass vacuously -- a simulated failure that
    simulates nothing proves nothing.
    """
    set_all_env(monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "fake-xai-key-value")
    unreadable = tmp_path / "unreadable_accounts.yaml"
    unreadable.write_text(
        (REPO_ROOT / "config" / "x_accounts.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    original_open = os.open
    intercepted = False

    def _raise_for_target(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal intercepted
        if Path(path) == unreadable:
            intercepted = True
            raise PermissionError(13, "Permission denied")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _raise_for_target)

    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["retrieval"]["social"]["enabled"] = True
    data["retrieval"]["social"]["agent_model"] = "grok-fixture"
    data["retrieval"]["social"]["account_allowlist_path"] = str(unreadable)
    social = tmp_path / "social.yaml"
    social.write_text(yaml.safe_dump(data), encoding="utf-8")

    report = verify_environment(social)
    assert intercepted, "the loader never opened the allowlist; this test proved nothing"
    assert report.exit_code == EXIT_ENV_MISSING
    assert any("allowlist" in p for p in report.filesystem_problems)
    assert report.config_problems == []


def test_enabled_social_with_missing_allowlist_is_a_filesystem_problem(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1-308 round-4 P1 regression: an enabled social config whose allowlist file is
    absent must fail startup, and must say so exactly once -- the missing-file report
    used to come from _verify_referenced_files, which now leaves it to the loader."""
    set_all_env(monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "fake-xai-key-value")
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    data["retrieval"]["social"]["enabled"] = True
    data["retrieval"]["social"]["agent_model"] = "grok-fixture"
    data["retrieval"]["social"]["account_allowlist_path"] = str(tmp_path / "no-such-accounts.yaml")
    social = tmp_path / "social.yaml"
    social.write_text(yaml.safe_dump(data), encoding="utf-8")

    report = verify_environment(social)
    assert report.exit_code == EXIT_ENV_MISSING
    assert report.config_problems == []
    # One problem, not two: only the allowlist loader reports the absent file.
    assert len(report.filesystem_problems) == 1
    assert "allowlist" in report.filesystem_problems[0]


def test_disabled_social_with_missing_allowlist_is_not_a_problem(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of round-4 P1: absence is only fatal while social is enabled. With
    the committed default (false) a missing allowlist stays a non-event -- the fix must
    not turn an optional file into a required one for every operator."""
    set_all_env(monkeypatch)
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert data["retrieval"]["social"]["enabled"] is False
    data["retrieval"]["social"]["account_allowlist_path"] = str(tmp_path / "no-such-accounts.yaml")
    social = tmp_path / "social.yaml"
    social.write_text(yaml.safe_dump(data), encoding="utf-8")

    report = verify_environment(social)
    assert report.exit_code == EXIT_OK
    # The check's own wording: "allowlist" alone would also match tmp_path, which pytest
    # names after the test -- this one survives only because pytest truncates it first.
    assert not any("loads clean" in c for c in report.checks_passed), report.checks_passed
    assert report.config_problems == []
    assert report.filesystem_problems == []


def _config_with_allowlist(
    tmp_path: Path, config_file: Path, allowlist_path: Path, *, name: str, enabled: bool = False
) -> Path:
    """A copy of the valid config pointing at allowlist_path, social off unless asked.

    The committed default is off, so that is the default here too -- an enabled variant
    also needs agent_model, which config validation requires alongside it.
    """
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert data["retrieval"]["social"]["enabled"] is False
    data["retrieval"]["social"]["account_allowlist_path"] = str(allowlist_path)
    if enabled:
        data["retrieval"]["social"]["enabled"] = True
        data["retrieval"]["social"]["agent_model"] = "grok-fixture"
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# --- disabled social, path present but not a readable file (round-5 review finding 2) ---
#
# The skip is documented as "disabled *and* absent", but it was written with
# Path.is_file(), which answers False for a directory, a dangling symlink and a stat
# failure just as it does for a missing file -- so each of those started clean.


@pytest.mark.parametrize("kind", ["directory", "dangling_symlink"])
def test_disabled_social_with_a_non_regular_allowlist_path_is_a_filesystem_problem(
    kind: str, tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Something exists at the configured path and it cannot be loaded: that is a real
    misconfiguration, and only true absence is the optional-file case."""
    set_all_env(monkeypatch)
    target = tmp_path / f"accounts_{kind}"
    if kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(tmp_path / "no-such-target.yaml")

    report = verify_environment(
        _config_with_allowlist(tmp_path, config_file, target, name=f"{kind}.yaml")
    )
    assert report.exit_code == EXIT_ENV_MISSING
    assert any("allowlist" in p for p in report.filesystem_problems)
    assert report.config_problems == []


def test_disabled_social_with_an_unsearchable_allowlist_parent_is_a_filesystem_problem(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stat failure is not evidence of absence. Under the old is_file() guard the
    permission error was swallowed and startup reported OK."""
    if os.geteuid() == 0:
        pytest.skip("root ignores the directory permission bits this test relies on")
    set_all_env(monkeypatch)
    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "accounts.yaml"
    target.write_text("accounts: []\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        report = verify_environment(
            _config_with_allowlist(tmp_path, config_file, target, name="locked.yaml")
        )
        assert report.exit_code == EXIT_ENV_MISSING
        assert any("allowlist" in p for p in report.filesystem_problems)
    finally:
        locked.chmod(0o700)


def test_second_entry_point_also_rejects_a_non_regular_allowlist_path(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cli._load_verified_config is the boundary every other command goes through, and it
    is where the round-3 and round-4 regressions actually landed -- verify-env was fine
    both times. Assert the truth table at both entry points, not one."""
    set_all_env(monkeypatch)
    directory = tmp_path / "accounts_dir"
    directory.mkdir()
    social = _config_with_allowlist(tmp_path, config_file, directory, name="cli-dir.yaml")

    with pytest.raises(AllowlistError) as excinfo:
        _load_verified_config(social)
    assert excinfo.value.is_filesystem_error is True

    # And the case that must stay silent: nothing at the path at all.
    absent = _config_with_allowlist(
        tmp_path, config_file, tmp_path / "no-such-accounts.yaml", name="cli-absent.yaml"
    )
    assert _load_verified_config(absent) is not None


# --- the whole truth table, at both entry points ---
#
# The hole in load_and_verify_account_allowlist moved in rounds 3, 4 and 5 (enabled-and-
# malformed, then enabled-and-absent, then disabled-and-not-a-regular-file) because each
# round tested the case it had just been shown. The rows below are the docstring's table
# enumerated mechanically, with "anything else" expanded into the three shapes that reach
# it, so a future change to the guard is checked against the whole contract rather than
# against the last reported symptom. Overlap with the named regression tests above is
# deliberate: those carry the *why*, this carries the completeness.

# "fifo" and "character_device" joined the list in round 6: the round-5 move from
# Path.is_file() to an lstat/ENOENT absence test was right about absence, but is_file() had
# also been filtering out special files as a side effect, and nothing replaced that -- so a
# FIFO here blocked the loader forever instead of being reported.
_PATH_KINDS = (
    "absent",
    "valid",
    "invalid",
    "directory",
    "dangling_symlink",
    "unsearchable_parent",
    "fifo",
    "character_device",
)

# (enabled, path kind) -> outcome. "enabled" buys exactly one thing: permission for the
# file to be absent. It never excuses a directory, a dangling symlink, an unreachable file
# or malformed content.
_TRUTH_TABLE = [
    (enabled, kind, _expected)
    for enabled in (True, False)
    for kind, _expected in (
        ("absent", "filesystem" if enabled else "skipped"),
        ("valid", "loaded"),
        ("invalid", "config"),
        ("directory", "filesystem"),
        ("dangling_symlink", "filesystem"),
        ("unsearchable_parent", "filesystem"),
        ("fifo", "filesystem"),
        ("character_device", "filesystem"),
    )
]


@contextmanager
def _allowlist_at(tmp_path: Path, kind: str) -> Iterator[Path]:
    """Materialize one column of the table and clean up after it."""
    real = (REPO_ROOT / "config" / "x_accounts.yaml").read_text(encoding="utf-8")
    if kind == "absent":
        yield tmp_path / "no-such-accounts.yaml"
        return
    if kind == "valid":
        target = tmp_path / "valid_accounts.yaml"
        target.write_text(real, encoding="utf-8")
        yield target
        return
    if kind == "invalid":
        # A real, otherwise-valid copy with a case-insensitive duplicate username.
        data = yaml.safe_load(real)
        data["accounts"].append(dict(data["accounts"][0]))
        target = tmp_path / "invalid_accounts.yaml"
        target.write_text(yaml.safe_dump(data), encoding="utf-8")
        yield target
        return
    if kind == "directory":
        target = tmp_path / "accounts_dir"
        target.mkdir()
        yield target
        return
    if kind == "dangling_symlink":
        target = tmp_path / "accounts_link.yaml"
        target.symlink_to(tmp_path / "no-such-target.yaml")
        yield target
        return
    if kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("POSIX special files are not available on this platform")
        target = tmp_path / "accounts_fifo.yaml"
        os.mkfifo(target)
        yield target
        return
    if kind == "character_device":
        if not Path("/dev/zero").exists():
            pytest.skip("no /dev/zero on this platform")
        target = tmp_path / "accounts_dev.yaml"
        target.symlink_to("/dev/zero")
        yield target
        return
    assert kind == "unsearchable_parent", kind
    if os.geteuid() == 0:
        pytest.skip("root ignores the directory permission bits this case relies on")
    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "accounts.yaml"
    target.write_text(real, encoding="utf-8")
    locked.chmod(0o000)
    try:
        yield target
    finally:
        locked.chmod(0o700)


@pytest.mark.parametrize(("enabled", "kind", "expected"), _TRUTH_TABLE)
def test_allowlist_truth_table_at_verify_env(
    enabled: bool,
    kind: str,
    expected: str,
    tmp_path: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    deadline: None,
) -> None:
    set_all_env(monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "fake-xai-key-value")
    with _allowlist_at(tmp_path, kind) as allowlist_path:
        report = verify_environment(
            _config_with_allowlist(
                tmp_path, config_file, allowlist_path, name="table.yaml", enabled=enabled
            )
        )

    if expected == "loaded":
        assert report.exit_code == EXIT_OK, report.render()
        assert any("loads clean" in c for c in report.checks_passed), report.checks_passed
    elif expected == "skipped":
        assert report.exit_code == EXIT_OK, report.render()
        # Silent, not merely non-fatal: no line at all, not even a passing one. Keyed on
        # the check's own wording, not on "allowlist" appearing anywhere in the render --
        # every path line carries tmp_path, whose name is derived from this test's name.
        assert not any("loads clean" in c for c in report.checks_passed), report.checks_passed
        assert report.config_problems == []
        assert report.filesystem_problems == []
    elif expected == "config":
        assert report.exit_code == EXIT_CONFIG_INVALID, report.render()
        # The loader's own prefix, not a bare "allowlist": the reported path lives under
        # tmp_path, which pytest names after this test.
        assert any(p.startswith("invalid account allowlist") for p in report.config_problems)
        assert report.filesystem_problems == []
    else:
        assert report.exit_code == EXIT_ENV_MISSING, report.render()
        assert report.config_problems == []
        # One problem, not two: only the loader reports the path.
        assert len(report.filesystem_problems) == 1, report.filesystem_problems
        assert report.filesystem_problems[0].startswith("invalid account allowlist")


@pytest.mark.parametrize(("enabled", "kind", "expected"), _TRUTH_TABLE)
def test_allowlist_truth_table_at_the_cli_boundary(
    enabled: bool,
    kind: str,
    expected: str,
    tmp_path: Path,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    deadline: None,
) -> None:
    """cli._load_verified_config is where the round-3 and round-4 regressions actually
    landed -- verify-env was fine both times. It discards the loaded allowlist, so the
    observable here is raise-or-not plus the classification the CLI maps to an exit code."""
    set_all_env(monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "fake-xai-key-value")
    with _allowlist_at(tmp_path, kind) as allowlist_path:
        config_path = _config_with_allowlist(
            tmp_path, config_file, allowlist_path, name="table-cli.yaml", enabled=enabled
        )
        if expected in ("loaded", "skipped"):
            assert _load_verified_config(config_path) is not None
            return
        with pytest.raises(AllowlistError) as excinfo:
            _load_verified_config(config_path)
    assert excinfo.value.is_filesystem_error is (expected == "filesystem")


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


# --- startup cost (round-4 review finding) ---

# Provider SDKs that must not be loaded just to validate config and an allowlist. Both are
# slow and noisy at import: forecasting_tools alone took ~7s and printed a Metaculus token
# warning, a model-cost warning and a Streamlit cache warning into verify-env's output.
_PROVIDER_MODULES = ("forecasting_tools", "asknews_sdk")

# The marker is load-bearing: an imported SDK can print to stdout as well as stderr, so the
# answer has to be findable in output it does not control.
_PROBE = """
import importlib, sys
importlib.import_module({module!r})
print("LOADED:" + ",".join(sorted(m for m in sys.modules if m in {providers!r})))
"""


def _providers_loaded_by(module: str) -> list[str]:
    probe = _PROBE.format(module=module, providers=_PROVIDER_MODULES)
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    marked = [line for line in result.stdout.splitlines() if line.startswith("LOADED:")]
    assert len(marked) == 1, result.stdout
    return [name for name in marked[0].removeprefix("LOADED:").split(",") if name]


@pytest.mark.parametrize("module", ["whiskeyjack_bot.env_verify", "whiskeyjack_bot.cli"])
def test_startup_module_does_not_import_provider_sdks(module: str) -> None:
    """Must run in a fresh interpreter: inside pytest the SDKs are already in sys.modules
    from the adapter suites, so an in-process assertion would fail for the wrong reason.

    The coupling this guards is invisible at the import site -- env_verify imports
    ``research.allowlist``, and it was the *package* __init__ that pulled in the AskNews
    adapter and, through it, forecasting_tools.
    """
    assert _providers_loaded_by(module) == []


def test_the_provider_probe_would_notice_a_regression() -> None:
    """The test above passes trivially if the probe is wrong -- misspelled SDK names, a
    marker that never prints. Importing the AskNews adapter must make it report both."""
    assert _providers_loaded_by("whiskeyjack_bot.research.asknews") == sorted(_PROVIDER_MODULES)
