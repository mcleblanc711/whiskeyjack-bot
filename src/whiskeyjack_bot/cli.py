"""Command-line entry point.

Subcommands are registered incrementally as their backlog issues land;
the scaffold ships only the program frame (M0-001).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from whiskeyjack_bot import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whiskeyjack-bot",
        description=(
            "Metaculus MiniBench forecasting pipeline; primary product is an "
            "attribution ledger of forecasts, evidence, and outcomes."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    verify_env = subparsers.add_parser(
        "verify-env",
        help="validate config, data directories and credential presence (names only)",
    )
    verify_env.add_argument(
        "--config",
        default="config.yaml",
        type=Path,
        help="path to the YAML config file (default: config.yaml)",
    )
    return parser


def _run_verify_env(config_path: Path) -> int:
    from whiskeyjack_bot.env_verify import verify_environment

    report = verify_environment(config_path)
    print(report.render())
    return report.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "verify-env":
        return _run_verify_env(args.config)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
