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

    questions = subparsers.add_parser(
        "questions",
        help="fetch or replay tournament questions",
    )
    questions_sub = questions.add_subparsers(dest="questions_command", metavar="<subcommand>")
    fetch = questions_sub.add_parser(
        "fetch",
        help="load questions from a snapshot (default) or live from Metaculus (--live)",
    )
    fetch.add_argument("--config", default="config.yaml", type=Path)
    fetch.add_argument(
        "--live",
        action="store_true",
        help="fetch from the Metaculus API (requires METACULUS_TOKEN); default is snapshot replay",
    )
    fetch.add_argument(
        "--snapshot",
        type=Path,
        help="snapshot file to load in fixture mode",
    )
    fetch.add_argument(
        "--tournament",
        help=(
            "override the configured tournament id/slug, e.g. bot-testing-area "
            "for smoke tests; the config file is not touched"
        ),
    )
    fetch.add_argument(
        "--save",
        type=Path,
        help="write the fetched questions to this snapshot file",
    )
    return parser


def _run_verify_env(config_path: Path) -> int:
    from whiskeyjack_bot.env_verify import verify_environment

    report = verify_environment(config_path)
    print(report.render())
    return report.exit_code


def _run_questions_fetch(args: argparse.Namespace) -> int:
    from whiskeyjack_bot.config import ConfigError, load_config
    from whiskeyjack_bot.logging_setup import configure_logging
    from whiskeyjack_bot.metaculus.client import MissingCredentialError
    from whiskeyjack_bot.metaculus.fetch import (
        fetch_open_questions_fixture,
        fetch_open_questions_live,
    )
    from whiskeyjack_bot.metaculus.snapshots import SnapshotError, save_snapshot

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(exc)
        return 2
    configure_logging(config)

    tournament_override: int | str | None = args.tournament
    try:
        if args.live:
            resolved, questions = fetch_open_questions_live(config, tournament_override)
            tournament_id: int | str = resolved.id
            source = "live"
        else:
            if args.snapshot is None:
                print("fixture mode needs --snapshot PATH (or pass --live to fetch)")
                return 2
            meta, questions = fetch_open_questions_fixture(args.snapshot)
            tournament_id = meta.tournament_id
            source = "fixture"
    except MissingCredentialError as exc:
        print(exc)
        return 3
    except SnapshotError as exc:
        print(exc)
        return 2

    if args.save is not None:
        save_snapshot(
            args.save,
            questions,
            tournament_id=tournament_id,
            group_question_mode=config.metaculus.group_question_mode,
            source=source,
        )
        print(f"snapshot written: {args.save}")

    print(f"tournament: {tournament_id} (source: {source})")
    print(f"questions: {len(questions)}")
    for q in questions:
        q_type = getattr(q, "question_type", type(q).__name__)
        print(f"  [{q_type}] question={q.id_of_question} post={q.id_of_post} {q.question_text}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "verify-env":
        return _run_verify_env(args.config)
    if args.command == "questions":
        if args.questions_command != "fetch":
            parser.parse_args(["questions", "--help"])
            return 2
        return _run_questions_fetch(args)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
