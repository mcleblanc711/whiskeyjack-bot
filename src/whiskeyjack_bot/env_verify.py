"""Environment verifier (M0-004).

Checks, in order: the config file parses and validates (which already rejects
every invalid live-submit combination, M0-005), required data directories
exist or can be created, referenced files exist, and every required credential
environment variable is present. Reports environment variable *names* only —
a value is never read further than a presence/emptiness check and never
echoed.

Exit codes are distinct so operators and scripts can tell config problems
from environment problems without parsing output.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from whiskeyjack_bot.config import AppConfig, ConfigError, load_config

EXIT_OK = 0
EXIT_CONFIG_INVALID = 2
EXIT_ENV_MISSING = 3


@dataclass
class VerificationReport:
    config_problems: list[str] = field(default_factory=list)
    filesystem_problems: list[str] = field(default_factory=list)
    missing_env_vars: list[str] = field(default_factory=list)
    checks_passed: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.config_problems:
            return EXIT_CONFIG_INVALID
        if self.filesystem_problems or self.missing_env_vars:
            return EXIT_ENV_MISSING
        return EXIT_OK

    def render(self) -> str:
        lines: list[str] = []
        for check in self.checks_passed:
            lines.append(f"ok: {check}")
        for problem in self.config_problems:
            lines.append(f"config error: {problem}")
        for problem in self.filesystem_problems:
            lines.append(f"filesystem error: {problem}")
        for name in self.missing_env_vars:
            lines.append(f"missing env var: {name} (set it in the environment; never in config)")
        verdict = "environment OK" if self.exit_code == EXIT_OK else "environment NOT ready"
        lines.append(verdict)
        return "\n".join(lines)


def _verify_directories(config: AppConfig, report: VerificationReport) -> None:
    directories = {
        "storage.sqlite_path parent": config.storage.sqlite_path.parent,
        "storage.artifact_root": config.storage.artifact_root,
        "storage.export_root": config.storage.export_root,
        "logging.file parent": config.logging.file.parent,
    }
    for label, directory in directories.items():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            report.checks_passed.append(f"{label} directory ready: {directory}")
        except OSError as exc:
            report.filesystem_problems.append(
                f"{label} directory {directory} cannot be created: {exc.strerror or exc}"
            )


def _verify_referenced_files(config: AppConfig, report: VerificationReport) -> None:
    references = {"forecast.prompt_path": config.forecast.prompt_path}
    if config.retrieval.social.enabled:
        references["retrieval.social.account_allowlist_path"] = (
            config.retrieval.social.account_allowlist_path
        )
    for label, path in references.items():
        if path.is_file():
            report.checks_passed.append(f"{label} exists: {path}")
        else:
            report.filesystem_problems.append(f"{label} does not exist: {path}")


def _verify_env_vars(config: AppConfig, report: VerificationReport) -> None:
    for name in config.secret_env_var_names():
        # Presence and non-emptiness only; the value itself is not retained.
        if os.environ.get(name):
            report.checks_passed.append(f"env var {name} is set")
        else:
            report.missing_env_vars.append(name)


def verify_environment(config_path: Path | str) -> VerificationReport:
    report = VerificationReport()
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        report.config_problems.extend(exc.problems)
        return report
    report.checks_passed.append(f"config valid: {config_path}")
    _verify_directories(config, report)
    _verify_referenced_files(config, report)
    _verify_env_vars(config, report)
    return report
