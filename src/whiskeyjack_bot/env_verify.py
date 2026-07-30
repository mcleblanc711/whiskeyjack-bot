"""Environment verifier (M0-004).

Checks, in order: the config file parses and validates (which already rejects
every invalid live-submit combination, M0-005), required data directories
exist or can be created, referenced files exist, the X account allowlist is
structurally valid (M1-308, unconditionally -- see
``load_and_verify_account_allowlist``), and every required credential
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
from whiskeyjack_bot.prompt import PromptError, load_prompt
from whiskeyjack_bot.research.allowlist import AccountAllowlist, AllowlistError, load_allowlist

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
    """Existence-check referenced files that nothing else opens (M0-004).

    The account allowlist is deliberately *not* here: ``_verify_account_allowlist``
    reaches its absence too (M1-308 round 4), and reporting it in both places gave
    ``verify-env`` two lines for one missing file.
    """
    references = {"forecast.prompt_path": config.forecast.prompt_path}
    for label, path in references.items():
        if path.is_file():
            report.checks_passed.append(f"{label} exists: {path}")
        else:
            report.filesystem_problems.append(f"{label} does not exist: {path}")


def load_and_verify_account_allowlist(config: AppConfig) -> AccountAllowlist | None:
    """Load+validate the account allowlist unconditionally (M1-308 round 3).

    Runs regardless of ``retrieval.social.enabled``: the committed default file must
    always be structurally valid, and a malformed one is a config problem the moment it
    can be read, not only when the feature happens to be turned on. This is the shared
    boundary every config-consuming command goes through --
    ``cli._load_verified_config`` calls it too, not just ``verify-env`` -- so an
    enabled-but-malformed allowlist can no longer reach a command that never runs
    ``verify_environment()``.

    Returns ``None`` (skips) in exactly one case: social retrieval is disabled *and* the
    file is absent. An absent allowlist while ``enabled`` is true is a hard startup
    failure (round 4) -- it used to skip here too, on the theory that absence belonged to
    ``_verify_referenced_files``, but that function only runs inside
    ``verify_environment()``, so ``questions fetch`` started clean with an enabled social
    config and no allowlist at all.

    The enabled-and-absent raise is delegated to ``load_allowlist`` rather than built
    here: its ``OSError`` branch already yields a sanitized, ``from None``-chained,
    ``is_filesystem_error=True`` error, and its ``strerror`` stays accurate across every
    case ``is_file()`` collapses into one answer -- absent, a directory, a dangling
    symlink, unreadable.
    """
    social = config.retrieval.social
    path = social.account_allowlist_path
    if not social.enabled and not path.is_file():
        return None
    return load_allowlist(path)


def _verify_account_allowlist(config: AppConfig, report: VerificationReport) -> None:
    """Report the account allowlist's structural validity (M1-308).

    A readable-but-invalid allowlist (duplicate username, unknown reliability tag, ...)
    is a config-content problem, not a filesystem one -- unlike a missing/unreadable
    file, it can never be fixed by waiting for the filesystem to change -- so it goes to
    config_problems. An unreadable file (permission, race) is still a filesystem
    problem; the two are told apart by AllowlistError.is_filesystem_error, not by which
    function raised it.
    """
    try:
        allowlist = load_and_verify_account_allowlist(config)
    except AllowlistError as exc:
        # AllowlistError is already sanitized; it never echoes entry content.
        bucket = report.filesystem_problems if exc.is_filesystem_error else report.config_problems
        bucket.append(str(exc))
        return
    if allowlist is None:
        return
    report.checks_passed.append(
        f"retrieval.social.account_allowlist_path loads clean: {len(allowlist.entries)} accounts"
    )


def _verify_prompt_version(config: AppConfig, report: VerificationReport) -> None:
    """Cross-check the prompt's declared version against config (M1-401).

    Catches prompt/config drift before a run rather than at the first forecast,
    when the mismatched version would already have been recorded. Skipped when
    the file is missing so that case reports one problem, not two.
    """
    path = config.forecast.prompt_path
    if not path.is_file():
        return
    try:
        loaded = load_prompt(path, config.forecast.prompt_version)
    except PromptError as exc:
        # PromptError is already sanitized; it never echoes prompt contents.
        report.filesystem_problems.append(str(exc))
        return
    report.checks_passed.append(
        f"forecast.prompt_path declares version {loaded.version} (sha256 {loaded.sha256[:12]}…)"
    )


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
    _verify_account_allowlist(config, report)
    _verify_prompt_version(config, report)
    _verify_env_vars(config, report)
    return report
