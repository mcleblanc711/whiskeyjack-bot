"""Command-line entry point.

Subcommands are registered incrementally as their backlog issues land;
the scaffold ships only the program frame (M0-001).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from whiskeyjack_bot import __version__

if TYPE_CHECKING:
    import sqlite3

    from whiskeyjack_bot.config import AppConfig
    from whiskeyjack_bot.lifecycle import ApprovalDecision

# A command that refused to act: an unusable ledger, an unknown record, an illegal
# transition, or a hash the operator supplied that the record does not store (M2-701).
#
# Defined here rather than beside EXIT_CONFIG_INVALID / EXIT_ENV_MISSING, which live in
# env_verify.py. Those two are that module's report vocabulary and predate any other
# command; an approval refusal is not an environment verdict, and moving the existing pair
# into a shared home is a change to every caller of them, which is not this item's.
EXIT_REFUSED = 4


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

    _add_approval_parser(
        subparsers,
        "approve",
        "record an approval of a validated forecast, bound to its exact content hash",
    )
    _add_approval_parser(
        subparsers,
        "reject",
        "record a rejection; the record stays validated and may be approved later",
    )
    return parser


def _add_approval_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], name: str, help_text: str
) -> None:
    """Register `approve` or `reject`; the two take identical arguments (M2-701).

    ``--actor`` is **required and has no default**. An approval is an attribution claim
    about a person, and inferring one from the OS login would write ``getpass.getuser()``
    into the one table that exists to be trusted -- permanently, since it is append-only.

    ``--forecast-sha256`` is optional and is verified when supplied: it is the hash the
    operator actually reviewed, and a mismatch refuses the command without writing
    anything. The hash the decision binds to is printed either way, so a review and the
    approval that follows it can be tied together.
    """
    command = subparsers.add_parser(name, help=help_text)
    command.add_argument("--config", default="config.yaml", type=Path)
    command.add_argument("--record-id", required=True, help="the forecast record to decide on")
    command.add_argument(
        "--actor", required=True, help="who is making this decision; recorded verbatim"
    )
    command.add_argument("--note", help="optional free-text note, stored with the decision")
    command.add_argument(
        "--forecast-sha256",
        help=(
            "the content hash you reviewed; the command refuses and writes nothing if the "
            "record does not store this exact hash"
        ),
    )


def _run_verify_env(config_path: Path) -> int:
    from whiskeyjack_bot.env_verify import verify_environment

    report = verify_environment(config_path)
    print(report.render())
    return report.exit_code


def _load_verified_config(path: Path) -> AppConfig:
    """``load_config()`` plus the unconditional allowlist structural check (M1-308).

    Every config-consuming command must call this instead of ``load_config()``
    directly: it is the boundary that makes a malformed committed allowlist fail
    here, for every command -- not just ``verify-env``, and not only when
    ``retrieval.social.enabled`` happens to be true. Raises ``ConfigError`` or
    ``AllowlistError``; callers handle both the same way ``verify-env`` does.
    """
    from whiskeyjack_bot.config import load_config
    from whiskeyjack_bot.env_verify import load_and_verify_account_allowlist

    config = load_config(path)
    load_and_verify_account_allowlist(config)
    return config


def _run_questions_fetch(args: argparse.Namespace) -> int:
    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING
    from whiskeyjack_bot.logging_setup import configure_logging
    from whiskeyjack_bot.metaculus.client import MissingCredentialError
    from whiskeyjack_bot.metaculus.fetch import (
        fetch_open_questions_fixture,
        fetch_open_questions_live,
    )
    from whiskeyjack_bot.metaculus.snapshots import SnapshotError, save_snapshot
    from whiskeyjack_bot.research.allowlist import AllowlistError

    try:
        config = _load_verified_config(args.config)
    except ConfigError as exc:
        print(exc)
        return EXIT_CONFIG_INVALID
    except AllowlistError as exc:
        print(exc)
        return EXIT_ENV_MISSING if exc.is_filesystem_error else EXIT_CONFIG_INVALID
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


def _run_approval(args: argparse.Namespace, decision: ApprovalDecision) -> int:
    """Record one approval decision against a stored forecast record (M2-701).

    Prints what is being decided on -- identity, derived status and the content hash --
    *before* writing, because an approval that binds to a hash the operator never saw is
    an attribution claim with nothing behind it.

    Nothing here contacts Metaculus. Approval and submission are separate commands (D23),
    and the gateway is M2-703/M2-704.
    """
    from datetime import datetime, timezone

    from whiskeyjack_bot.approval import ApprovalError, approve, read_forecast_summary, reject
    from whiskeyjack_bot.config import ConfigError
    from whiskeyjack_bot.env_verify import EXIT_CONFIG_INVALID, EXIT_ENV_MISSING, EXIT_OK
    from whiskeyjack_bot.logging_setup import configure_logging
    from whiskeyjack_bot.research.allowlist import AllowlistError

    try:
        config = _load_verified_config(args.config)
    except ConfigError as exc:
        print(exc)
        return EXIT_CONFIG_INVALID
    except AllowlistError as exc:
        print(exc)
        return EXIT_ENV_MISSING if exc.is_filesystem_error else EXIT_CONFIG_INVALID
    configure_logging(config)

    connection = _open_existing_ledger(config.storage.sqlite_path)
    if connection is None:
        return EXIT_REFUSED
    try:
        try:
            summary = read_forecast_summary(connection, args.record_id)
        except ApprovalError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        print(f"record:    {summary.record_id}")
        print(
            f"question:  {summary.question_id}  tournament: {summary.tournament_id}  "
            f"version: {summary.forecast_version}  type: {summary.question_type}"
        )
        print(f"status:    {summary.status}")
        print(f"hash:      {summary.forecast_sha256 or '(none stored)'}")

        writer = approve if decision == "approved" else reject
        try:
            recorded = writer(
                connection,
                record_id=args.record_id,
                actor=args.actor,
                occurred_at=datetime.now(tz=timezone.utc),
                note=args.note,
                expected_sha256=args.forecast_sha256,
            )
        except ApprovalError as exc:
            print(f"refused: {exc}")
            return EXIT_REFUSED
        print(
            f"{recorded.decision} {recorded.forecast_record_id} "
            f"(approval event {recorded.event_id}, lifecycle seq {recorded.event_seq})"
        )
        return EXIT_OK
    finally:
        connection.close()


def _open_existing_ledger(path: Path) -> sqlite3.Connection | None:
    """Open an existing ledger, or print why not and return ``None`` (M2-701).

    **The file must already exist, and one connection must carry the whole command.**
    A mistyped ``--config`` would otherwise mint an empty database and report "no such
    record" against it -- a true statement about the wrong ledger. The existence check
    answers that case, and only that case: it is what produces an actionable message
    rather than a bare open failure. Everything else is ``ledger.open_verified_ledger``'s,
    which neither creates nor hands back a second open of the pathname -- see its
    docstring for the two races that motivate each half.
    """
    from whiskeyjack_bot.ledger import LedgerError, open_verified_ledger

    try:
        exists = path.is_file()
    except OSError:
        # from None is not needed (nothing is re-raised), but the message must not carry
        # the OSError's text; the path itself is operator-supplied configuration and is
        # rendered under the settled M1-401 carve-out.
        print(f"cannot read the ledger database at {path}")
        return None
    if not exists:
        print(f"no ledger database at {path}; nothing has been recorded there yet")
        return None
    try:
        # The verified connection itself, not a fresh open of the same name: the schema
        # this checked and the database the command writes must be one file. Review
        # rounds 1 and 2 both landed here, from opposite directions.
        return open_verified_ledger(path)
    except LedgerError as exc:
        print(exc)
        return None


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
    if args.command == "approve":
        return _run_approval(args, "approved")
    if args.command == "reject":
        return _run_approval(args, "rejected")
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
